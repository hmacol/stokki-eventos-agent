# -*- coding: utf-8 -*-
"""
Tela /baixa-sem-app e Torre (motorista sem app, 08/10).

    py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)


class TestTorreSemApp(unittest.TestCase):
    def test_torre_chama_o_modulo(self):
        src = (_AQUI / "torre_controle.py").read_text(encoding="utf-8")
        self.assertIn("from nucleo.baixa_sem_app import excecoes_torre as sem_app_excecoes", src)


ROTA = {"id": 5000, "name": "Planejamento - 07/10/2026 - #18", "agent_id": 50191, "start_at": "2026-10-07 08:00:00",
        "services": {"data": [{"id": 1, "code": "#PS-1", "status": "assigned", "title": "Cliente A"},
                              {"id": 2, "code": "#PS-2", "status": "done", "title": "Cliente B"}]}}


class TestTelaBaixa(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                  mock.patch("rotas_client.buscar_rota", return_value={"route": ROTA}),
                  mock.patch("regras.preferencias_motoristas.agent_ids_sem_app", return_value={50191}),
                  mock.patch("nucleo.baixa_sem_app.executar_lote")):
            p.start()
            self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def _post(self, body):
        return self.cliente.post("/api/baixa-sem-app", json=body, headers={"Origin": "http://localhost"})

    def test_tela_lista_pedidos_e_motivos(self):
        self._logar("operador")
        html = self.cliente.get("/baixa-sem-app?rota=5000").get_data(as_text=True)
        for trecho in ("PS-1", "PS-2", "Cliente A", "Endereço Incorreto", "Confirmar baixa"):
            self.assertIn(trecho, html)

    def test_leitura_nao_entra(self):
        self._logar("leitura")
        self.assertIn(self.cliente.get("/baixa-sem-app?rota=5000").status_code, (302, 401, 403))
        self.assertIn(self._post({"rota": 5000, "itens": []}).status_code, (302, 401, 403))

    def test_post_cria_lote_e_dispara(self):
        self._logar("operador")
        r = self._post({"rota": 5000, "itens": [
            {"service_id": 1, "codigo": "PS-1", "entregue": False, "failed_reason_id": 5433}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        lote = r.get_json()["lote"]
        st = self.cliente.get(f"/api/baixa-sem-app/{lote}").get_json()
        self.assertEqual(st["itens"][0]["failed_reason_id"], 5433)

    def _post_multipart(self, dados, arquivos):
        import io
        import json
        data = {"dados": json.dumps(dados)}
        for nome, conteudo in arquivos.items():
            data[nome] = (io.BytesIO(conteudo), "canhoto.pdf")
        return self.cliente.post("/api/baixa-sem-app", data=data, content_type="multipart/form-data",
                                 headers={"Origin": "http://localhost"})

    def test_canhoto_gravado_antes_do_lote(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar", return_value={}) as salvar:
            r = self._post_multipart({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": True}]},
                                     {"canhoto_1": b"%PDF-1.4 x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        salvar.assert_called_once()
        self.assertEqual(salvar.call_args.args[1], "PS-1")

    def test_codigo_do_canhoto_vem_da_rota_nao_do_navegador(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar", return_value={}) as salvar:
            r = self._post_multipart({"rota": 5000, "itens": [{"service_id": 1, "codigo": "../../X", "entregue": True}]},
                                     {"canhoto_1": b"%PDF-1.4 x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(salvar.call_args.args[1], "PS-1")

    def test_requisicao_grande_demais(self):
        self._logar("operador")
        # o test_client recalcula o Content-Length: baixa o limite em vez de mandar 60 MB
        with mock.patch.object(painel_agentes, "LIMITE_REQUISICAO_BAIXA", 5):
            r = self.cliente.post("/api/baixa-sem-app", data=b"x" * 10, content_type="multipart/form-data",
                                  headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 413)

    def test_canhoto_invalido_nao_grava_nada(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar") as salvar, \
             mock.patch("nucleo.baixa_sem_app.criar_lote") as criar:
            r = self._post_multipart({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": True}]},
                                     {"canhoto_1": b"nao sou imagem"})
        self.assertEqual(r.status_code, 400)
        salvar.assert_not_called()
        criar.assert_not_called()

    def test_canhoto_de_item_que_voltou_e_ignorado(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar") as salvar:
            r = self._post_multipart({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": False,
                                                               "failed_reason_id": 5433}]},
                                     {"canhoto_1": b"%PDF-1.4 x"})
        self.assertEqual(r.status_code, 200)
        salvar.assert_not_called()

    def test_tela_tem_campo_de_canhoto(self):
        self._logar("operador")
        self.assertIn('class="canhoto"', self.cliente.get("/baixa-sem-app?rota=5000").get_data(as_text=True))

    def test_segundo_lote_da_mesma_rota_e_recusado(self):
        self._logar("operador")
        body = {"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": True,
                                                  "justificativa": "sem_bateria"}]}
        self.assertEqual(self._post(body).status_code, 200)
        self.assertEqual(self._post(body).status_code, 409)   # executar_lote mockado: o 1o segue "em andamento"

    def test_post_recusa_sem_motivo_e_motorista_com_app(self):
        self._logar("operador")
        r = self._post({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": False}]})
        self.assertEqual(r.status_code, 400)
        with mock.patch("regras.preferencias_motoristas.agent_ids_sem_app", return_value=set()):
            r = self._post({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": True,
                                                     "justificativa": "sem_bateria"}]})
        self.assertEqual(r.status_code, 400)

    # ── checklist da Vuupt (29/10) ──
    def _png(self, w=40, h=30):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (200, 10, 10)).save(buf, "PNG")
        return buf.getvalue()

    def _item(self, **kw):
        return {"service_id": 1, "codigo": "PS-1", "entregue": True, **kw}

    def test_tela_tem_nota_e_justificativa(self):
        self._logar("operador")
        html = self.cliente.get("/baixa-sem-app?rota=5000").get_data(as_text=True)
        for trecho in ('class="nota"', 'class="justificativa"', "Canhoto chega depois (motorista vai trazer)",
                       'class="texto-outro"'):
            self.assertIn(trecho, html)

    def test_item_entregue_sem_canhoto_sem_justificativa_400(self):
        self._logar("operador")
        with mock.patch("nucleo.baixa_sem_app.criar_lote") as criar:
            r = self._post_multipart({"rota": 5000, "itens": [self._item()]}, {})
        self.assertEqual(r.status_code, 400)
        self.assertIn("PS-1", r.get_json()["erro"])
        criar.assert_not_called()

    def test_outro_sem_texto_400(self):
        self._logar("operador")
        r = self._post_multipart({"rota": 5000, "itens": [self._item(justificativa="outro", texto_outro="ab")]}, {})
        self.assertEqual(r.status_code, 400)

    def test_item_com_justificativa_sem_foto_ok(self):
        self._logar("operador")
        r = self._post_multipart({"rota": 5000, "itens": [
            self._item(justificativa="outro", texto_outro="  cliente ausente ")]}, {})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        item = self.cliente.get(f"/api/baixa-sem-app/{r.get_json()['lote']}").get_json()["itens"][0]
        self.assertEqual((item["justificativa"], item["texto_outro"]), ("outro", "cliente ausente"))
        self.assertNotIn("fotos", item)

    def test_fotos_gravadas_antes_da_thread(self):
        self._logar("operador")
        visto = {}

        def thread_falsa(lote_id, *a, **k):
            from nucleo import baixa_sem_app as bsa
            conn = banco.conectar()
            try:
                visto["item"] = bsa.ler_lote(conn, lote_id)["itens"][0]
            finally:
                conn.close()
            pasta = bsa.pasta_fotos(lote_id)
            visto["existe"] = {c: (pasta / p).exists() for c, p in visto["item"]["fotos"].items()}
            visto["jpeg"] = (pasta / visto["item"]["fotos"]["15086"]).read_bytes().startswith(b"\xff\xd8")
        with mock.patch("nucleo.canhotos_manuais.salvar", return_value={}), \
             mock.patch("nucleo.baixa_sem_app.executar_lote", side_effect=thread_falsa), \
             mock.patch("threading.Thread") as th:
            th.side_effect = lambda target, args, daemon: mock.Mock(start=lambda: target(*args))
            r = self._post_multipart({"rota": 5000, "itens": [self._item(justificativa="sem_bateria")]},
                                     {"canhoto_1": self._png(), "nota_1": self._png(60, 20)})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(visto["existe"], {"15086": True, "15349": True})
        self.assertNotIn("justificativa", visto["item"])        # com canhoto a justificativa e ignorada
        self.assertTrue(visto["jpeg"])
        self.assertEqual(visto["item"]["fotos"]["15086"], "1_15086.jpg")

    def test_canhoto_pdf_so_copia_local_com_aviso(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar", return_value={}) as salvar:
            r = self._post_multipart({"rota": 5000, "itens": [self._item()]}, {"canhoto_1": b"%PDF-1.4 x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        salvar.assert_called_once()
        item = self.cliente.get(f"/api/baixa-sem-app/{r.get_json()['lote']}").get_json()["itens"][0]
        self.assertNotIn("fotos", item)
        self.assertIn("PDF", item["aviso"])

    def test_tela_reduz_imagem_no_navegador_antes_do_append(self):
        self._logar("operador")
        html = self.cliente.get("/baixa-sem-app?rota=5000").get_data(as_text=True)
        self.assertIn("function reduzirImagem(", html)
        self.assertIn("2000", html)
        self.assertIn("'image/jpeg', 0.85", html)
        self.assertIn("reduzirImagem(t[2])", html)
        self.assertLess(html.index("reduzirImagem(t[2])"), html.index("fd.append(tarefas[n][0]"))

    def test_insucesso_ignora_canhoto_e_nota_enviados(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar") as salvar:
            r = self._post_multipart({"rota": 5000, "itens": [
                {"service_id": 1, "codigo": "PS-1", "entregue": False, "failed_reason_id": 5433,
                 "justificativa": "sem_bateria", "texto_outro": "x", "aviso": "forjado"}]},
                {"canhoto_1": self._png(), "nota_1": self._png()})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        salvar.assert_not_called()
        item = self.cliente.get(f"/api/baixa-sem-app/{r.get_json()['lote']}").get_json()["itens"][0]
        for chave in ("fotos", "justificativa", "texto_outro", "aviso"):
            self.assertNotIn(chave, item)
        self.assertFalse((Path(banco.DB_PATH).parent / "checklist_fotos").exists())

    def test_aviso_vindo_do_navegador_e_descartado(self):
        self._logar("operador")
        r = self._post_multipart({"rota": 5000, "itens": [self._item(justificativa="sem_bateria", aviso="forjado")]}, {})
        item = self.cliente.get(f"/api/baixa-sem-app/{r.get_json()['lote']}").get_json()["itens"][0]
        self.assertNotIn("aviso", item)

    def test_justificativa_de_tipo_errado_400_nao_500(self):
        self._logar("operador")
        r = self._post_multipart({"rota": 5000, "itens": [self._item(justificativa=["outro"])]}, {})
        self.assertEqual(r.status_code, 400)

    def test_nota_que_nao_e_imagem_400_sem_gravar(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar") as salvar, \
             mock.patch("nucleo.baixa_sem_app.criar_lote") as criar:
            r = self._post_multipart({"rota": 5000, "itens": [self._item(justificativa="sem_bateria")]},
                                     {"nota_1": b"%PDF-1.4 x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Foto da nota de retorno", r.get_json()["erro"])
        salvar.assert_not_called()
        criar.assert_not_called()

    def test_arquivo_maior_que_15mb_413(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.LIMITE_BYTES", 10), \
             mock.patch("nucleo.baixa_sem_app.criar_lote") as criar:
            r = self._post_multipart({"rota": 5000, "itens": [self._item(justificativa="sem_bateria")]},
                                     {"nota_1": b"x" * 50})
        self.assertEqual(r.status_code, 413)
        criar.assert_not_called()

    def test_falha_ao_gravar_fotos_500_e_lote_erro(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar", return_value={}), \
             mock.patch.object(Path, "write_bytes", side_effect=OSError("disco cheio")):
            r = self._post_multipart({"rota": 5000, "itens": [self._item()]}, {"canhoto_1": self._png()})
        self.assertEqual(r.status_code, 500)
        conn = banco.conectar()
        try:
            from nucleo import baixa_sem_app as bsa
            d = bsa.ler_lote(conn, 1)
        finally:
            conn.close()
        self.assertEqual(d["itens"][0]["status"], "erro")


if __name__ == "__main__":
    unittest.main()
