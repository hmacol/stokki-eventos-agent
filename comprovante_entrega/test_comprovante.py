# -*- coding: utf-8 -*-
"""py -3.11 -m unittest comprovante_entrega.test_comprovante"""
import io
import sqlite3
import unittest
from datetime import datetime

from PIL import Image
from pypdf import PdfReader

from comprovante_entrega import fotos_vuupt, gerar_comprovante, gerar_pdf


def _jpeg(largura=1156, altura=651, cor=(40, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (largura, altura), cor).save(buf, format="JPEG")
    return buf.getvalue()


def _dados(**extra) -> dict:
    base = {"tipo": "entrega", "codigo": "PS-44149", "sucesso": True, "reentrega": False,
            "embarcador": "TIE INDUSTRIA E COMERCIO LTDA", "destinatario": "LAURA ELIZABETH",
            "endereco": "Rua Augustin Luberti 1021 - apto. 422, Fazenda da Juta, São Paulo - SP",
            "concluido_em": "16/11/2026 16:59", "nf": "42377", "volumes": 2, "motivo": "",
            "quem_retira": "", "motorista": "Fábio Barbosa", "rota": "Rota #11 · parada 2 de 10"}
    return {**base, **extra}


def _paginas(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


class TestGerarPdf(unittest.TestCase):
    AGORA = datetime(2026, 11, 17, 8, 0)

    def test_entregue_com_canhoto_uma_pagina_a4(self):
        pdf = gerar_pdf.gerar(_dados(), [("Canhoto assinado", _jpeg())], link="https://exemplo/c/x", emitido_em=self.AGORA)
        self.assertTrue(pdf.startswith(b"%PDF"))
        leitor = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(leitor.pages), 1)
        caixa = leitor.pages[0].mediabox
        self.assertAlmostEqual(float(caixa.width) / float(caixa.height), 210 / 297, places=2)

    def test_cada_foto_a_mais_vira_uma_pagina(self):
        fotos = [("Canhoto assinado", _jpeg()), ("Nota de retorno", _jpeg(600, 900))]
        self.assertEqual(_paginas(gerar_pdf.gerar(_dados(), fotos, emitido_em=self.AGORA)), 2)

    def test_variacoes_sem_foto_nao_quebram(self):
        for dados in (_dados(),
                      _dados(sucesso=False, motivo="Problema fiscal"),
                      _dados(tipo="retirada", endereco="", quem_retira="TRANSPORTADORA X", motorista="", rota=""),
                      _dados(nf="", volumes=None, motorista="", rota="", endereco="", reentrega=True)):
            with self.subTest(dados=dados["tipo"], sucesso=dados["sucesso"]):
                self.assertEqual(_paginas(gerar_pdf.gerar(dados, [], emitido_em=self.AGORA)), 1)

    def test_foto_corrompida_fica_de_fora(self):
        pdf = gerar_pdf.gerar(_dados(), [("Canhoto assinado", b"nao e imagem"), ("Nota de retorno", _jpeg())],
                              emitido_em=self.AGORA)
        self.assertEqual(_paginas(pdf), 1)

    def test_texto_longo_nao_quebra(self):
        longo = "DISTRIBUIDORA DE ALIMENTOS E BEBIDAS COM NOME MUITO COMPRIDO DO BRASIL LTDA " * 4
        dados = _dados(embarcador=longo, destinatario=longo, endereco=longo, sucesso=False, motivo=longo)
        self.assertEqual(_paginas(gerar_pdf.gerar(dados, [("Foto", _jpeg())], emitido_em=self.AGORA)), 1)

    def test_status_de_cada_variacao(self):
        self.assertEqual(gerar_pdf._status(_dados(), True)["palavra"], "ENTREGUE")
        self.assertIn("canhoto assinado", gerar_pdf._status(_dados(), True)["frase"])
        self.assertNotIn("canhoto", gerar_pdf._status(_dados(), False)["frase"])
        falha = gerar_pdf._status(_dados(sucesso=False, motivo="Local fechado"), False)
        self.assertEqual((falha["palavra"], falha["sucesso"]), ("NÃO ENTREGUE", False))
        self.assertIn("Local fechado", falha["frase"])
        retirada = gerar_pdf._status(_dados(tipo="retirada", quem_retira="JOAO"), False)
        self.assertEqual(retirada["palavra"], "RETIRADO")
        self.assertIn("JOAO", retirada["frase"])


class _Resp:
    def __init__(self, json_=None, content=b"", status=200):
        self._json, self.content, self.status = json_, content, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self._json


class TestFotosVuupt(unittest.TestCase):
    def _http(self, checklists, arquivos):
        chamadas = []

        def get(url, **kw):
            chamadas.append((url, kw))
            if url.endswith("/checklists"):
                return _Resp({"data": checklists})
            return arquivos[url]
        return get, chamadas

    def test_canhoto_primeiro_e_rotulos_dos_campos(self):
        checklists = [{"entity_id": 10, "fields": {"data": [
            {"checklist_form_field_id": 15349, "file_url": "u/retorno"},
            {"checklist_form_field_id": 15086, "file_url": "u/canhoto"},
            {"checklist_form_field_id": 1, "value": "texto", "file_url": None}]}}]
        get, chamadas = self._http(checklists, {"u/retorno": _Resp(content=b"R"), "u/canhoto": _Resp(content=b"C")})
        fotos = fotos_vuupt.buscar_fotos("tok", {"id": 10, "status_done": "success"}, http_get=get)
        self.assertEqual(fotos, [("Canhoto assinado", b"C"), ("Nota de retorno", b"R")])
        self.assertEqual(chamadas[0][1]["headers"]["Authorization"], "Bearer tok")
        self.assertNotIn("headers", chamadas[1][1])  # url assinada: o token nao vai pro CDN

    def test_checklist_de_outro_servico_e_ignorado(self):
        checklists = [{"entity_id": 99, "fields": {"data": [{"checklist_form_field_id": 15086, "file_url": "u/x"}]}}]
        get, _ = self._http(checklists, {"u/x": _Resp(content=b"X")})
        self.assertEqual(fotos_vuupt.buscar_fotos("tok", {"id": 10}, http_get=get), [])

    def test_falha_usa_rotulo_de_ocorrencia_e_foto_que_nao_baixa_fica_de_fora(self):
        checklists = [{"entity_id": 10, "fields": {"data": [
            {"checklist_form_field_id": 777, "file_url": "u/ok"},
            {"checklist_form_field_id": 778, "file_url": "u/erro"}]}}]
        get, _ = self._http(checklists, {"u/ok": _Resp(content=b"A"), "u/erro": _Resp(status=403)})
        fotos = fotos_vuupt.buscar_fotos("tok", {"id": 10, "status_done": "failed"}, http_get=get)
        self.assertEqual(fotos, [("Foto da ocorrência", b"A")])

    def test_api_fora_devolve_vazio(self):
        def get(url, **kw):
            raise ConnectionError("fora")
        self.assertEqual(fotos_vuupt.buscar_fotos("tok", {"id": 10}, http_get=get), [])


class TestMontar(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, motorista_nome TEXT, nome TEXT, total_paradas INTEGER);
            CREATE TABLE nucleo_paradas (id INTEGER PRIMARY KEY, rota_id INTEGER, service_id INTEGER, ordem INTEGER);
            INSERT INTO nucleo_rotas VALUES (1, 'Fábio Barbosa', 'Planejamento - 16/11/2026 - #11', 10);
            INSERT INTO nucleo_paradas VALUES (1, 1, 55, 2);
        """)
        return conn

    def test_motorista_e_rota_do_nucleo(self):
        conn = self._conn()
        self.assertEqual(gerar_comprovante.motorista_e_rota(conn, 55), ("Fábio Barbosa", "Rota #11 · parada 2 de 10"))
        self.assertEqual(gerar_comprovante.motorista_e_rota(conn, 999), ("", ""))
        self.assertEqual(gerar_comprovante.motorista_e_rota(sqlite3.connect(":memory:"), 55), ("", ""))

    def test_link_publico_so_com_segredo(self):
        servico = {"id": 55, "sender_id": 7}
        self.assertEqual(gerar_comprovante.link_publico({}, servico), "")
        link = gerar_comprovante.link_publico({"portal_cliente": {"secret_key": "s", "url_base": "https://x/cliente/"}}, servico)
        self.assertTrue(link.startswith("https://x/cliente/c/"))

    def test_montar_gera_pdf_com_foto_e_sem_tocar_na_rede(self):
        servico = {"id": 55, "sender_id": 7, "code": "#PS-44149", "status_done": "success",
                   "completed_at": "2026-11-16 19:59:58", "address": "Rua A 1, São Paulo - SP, 03977-409, Brasil",
                   "title": "x", "customer": {"data": {"name": "LAURA ELIZABETH"}}, "dimension_3": 2}
        pdf = gerar_comprovante.montar(
            servico, config={"vuupt_api": {"token": "t"}}, conn=self._conn(),
            embarcadores={7: {"nome": "TIE", "fator_ponderado": 1}}, buscar_nf=lambda codigo: "42377",
            buscar_fotos=lambda token, s: [("Canhoto assinado", _jpeg())])
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(_paginas(pdf), 1)


class TestAnexoDoEmailEPortal(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pasta = Path(self._tmp.name) / "canhotos"

    def test_salvar_com_foto_grava_pelo_codigo_e_sem_foto_devolve_none(self):
        from unittest import mock
        servico = {"id": 55, "code": "#ps-44149-r1"}
        with mock.patch.object(gerar_comprovante, "do_servico", return_value=b"%PDF proprio") as do:
            destino = gerar_comprovante.salvar_com_foto(servico, {}, self.pasta)
        self.assertEqual(destino.name, "comprovante_PS-44149-R1_55.pdf")
        self.assertEqual(destino.read_bytes(), b"%PDF proprio")
        self.assertTrue(do.call_args.kwargs["exigir_foto"])
        with mock.patch.object(gerar_comprovante, "do_servico", return_value=None):
            self.assertIsNone(gerar_comprovante.salvar_com_foto(servico, {}, self.pasta))

    def test_salvar_com_foto_reaproveita_arquivo_recente_so_com_validade(self):
        from unittest import mock
        servico = {"id": 55, "code": "PS-1"}
        with mock.patch.object(gerar_comprovante, "do_servico", return_value=b"%PDF 1"):
            gerar_comprovante.salvar_com_foto(servico, {}, self.pasta)
        with mock.patch.object(gerar_comprovante, "do_servico", return_value=b"%PDF 2") as do:
            self.assertEqual(gerar_comprovante.salvar_com_foto(servico, {}, self.pasta, validade_s=3600).read_bytes(), b"%PDF 1")
            do.assert_not_called()
            self.assertEqual(gerar_comprovante.salvar_com_foto(servico, {}, self.pasta).read_bytes(), b"%PDF 2")

    def test_reentrega_com_mesmo_codigo_nao_reaproveita_o_do_original(self):
        from unittest import mock
        with mock.patch.object(gerar_comprovante, "do_servico", return_value=b"%PDF original"):
            gerar_comprovante.salvar_com_foto({"id": 55, "code": "PS-1"}, {}, self.pasta)
        with mock.patch.object(gerar_comprovante, "do_servico", return_value=b"%PDF reentrega"):
            destino = gerar_comprovante.salvar_com_foto({"id": 56, "code": "PS-1"}, {}, self.pasta, validade_s=3600)
        self.assertEqual(destino.read_bytes(), b"%PDF reentrega")
        self.assertIsNone(gerar_comprovante.salvar_com_foto({"code": "PS-1"}, {}, self.pasta))

    def test_montar_com_exigir_foto_devolve_none_sem_foto(self):
        pdf = gerar_comprovante.montar({"id": 1, "code": "PS-1"}, config={}, conn=sqlite3.connect(":memory:"),
                                       embarcadores={}, buscar_nf=lambda codigo: "", buscar_fotos=lambda t, s: [],
                                       exigir_foto=True)
        self.assertIsNone(pdf)

    def test_email_usa_o_proprio_e_cai_na_vuupt_quando_nao_da(self):
        from unittest import mock
        from notificacao_entregas import notificar_entrega_concluida as rotina
        servico = {"id": 55, "code": "PS-1"}
        with mock.patch.object(gerar_comprovante, "salvar_com_foto", return_value="proprio.pdf"):
            self.assertEqual(rotina.canhoto_pdf(servico, {}, self.pasta, reserva=lambda: "vuupt.pdf"), "proprio.pdf")
        with mock.patch.object(gerar_comprovante, "salvar_com_foto", return_value=None):
            self.assertEqual(rotina.canhoto_pdf(servico, {}, self.pasta, reserva=lambda: "vuupt.pdf"), "vuupt.pdf")
        with mock.patch.object(gerar_comprovante, "salvar_com_foto", side_effect=RuntimeError("quebrou")),              self.assertLogs("notificar_entregas", level="ERROR"):
            self.assertEqual(rotina.canhoto_pdf(servico, {}, self.pasta, reserva=lambda: "vuupt.pdf"), "vuupt.pdf")
        self.assertEqual(rotina.canhoto_pdf(None, {}, self.pasta, reserva=lambda: "vuupt.pdf"), "vuupt.pdf")


if __name__ == "__main__":
    unittest.main()
