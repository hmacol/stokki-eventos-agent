# -*- coding: utf-8 -*-
"""
Documentos fiscais do pedido pela tela /pedidos: conferir, baixar e mandar por
e-mail o XML, a DANFE e o boleto (Hugo, 13/11).

    py -3.11 -m unittest test_documentos_email   (de dentro de painel_agentes/)
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402
import documentos_email as de  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

ORIGEM = {"Origin": "http://localhost"}
_XML = '<?xml version="1.0"?><nfeProc><NFe><infNFe><ide><cUF>35</cUF><nNF>{}</nNF></ide></infNFe></NFe></nfeProc>'


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.pasta = Path(self._tmp.name)
        self.pdfs = self.pasta / "pdfs"
        self.xmls = self.pasta / "xml"
        self.pdfs.mkdir()
        self.xmls.mkdir()
        self.db = self.pasta / "dados.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE documentos_processados (hash_conteudo TEXT PRIMARY KEY, origem TEXT, nome_arquivo TEXT,"
                          " tipo TEXT, codigo_pedido TEXT, status TEXT, numero_nf TEXT, numero_parcela INTEGER, processado_em TEXT)")
        self.enviados = []

    def resolver(self, nome):
        caminho = self.pdfs / nome
        return caminho if caminho.is_file() else None

    def doc(self, hash_, tipo, codigo, nf=None, parcela=None, status="ENVIADO", arquivo=True, quando="2026-11-10 08:00:00",
            tamanho=100):
        nome = f"{hash_}.pdf"
        if arquivo:
            (self.pdfs / nome).write_bytes(b"%PDF-" + b"x" * (tamanho - 5))
        self.conn.execute("INSERT INTO documentos_processados VALUES (?, 'stokki', ?, ?, ?, ?, ?, ?, ?)",
                          (hash_, nome, tipo, codigo, status, nf, parcela, quando))
        self.conn.commit()

    def xml(self, ps, nf):
        (self.xmls / f"{ps}.xml").write_text(_XML.format(nf), encoding="utf-8")

    def levantar(self, *codigos):
        return de.levantar(self.conn, list(codigos), self.xmls, self.resolver)

    def falso_email(self, ok=True):
        def enviar(destinatarios, assunto, corpo, config, anexos=None, **k):
            self.enviados.append({"para": destinatarios, "assunto": assunto, "corpo": corpo,
                                  "anexos": [(Path(c).name, n) for c, n in anexos or []]})
            return ok if not callable(ok) else ok(len(self.enviados))
        return enviar

    def enviar(self, itens, emails="financeiro@cliente.com.br", ok=True, **k):
        return de.enviar(self.conn, itens, emails, "ana", {}, enviar_email=self.falso_email(ok), pasta_xml=self.xmls,
                         resolver=self.resolver, agora=datetime(2026, 11, 13, 9, 0), **k)


class TestLevantar(_Base):
    def test_nomes_no_padrao_pedido_pelo_hugo(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="000042372")
        self.doc("h2", "Boleto", "PS-10", nf="42372")
        self.xml("PS-10", "42372")
        (p,) = self.levantar("#PS-10")
        self.assertEqual([a["nome"] for a in p["arquivos"]], ["XML_NF42372.xml", "DANFE_NF42372.pdf", "BOLETO_NF42372.pdf"])
        self.assertEqual(p["faltando"], [])

    def test_boleto_parcelado_ganha_sufixo_na_ordem_da_parcela(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="500")
        self.doc("hb", "Boleto", "PS-10", nf="500", parcela=2)
        self.doc("ha", "Boleto", "PS-10", nf="500", parcela=1)
        (p,) = self.levantar("PS-10")
        nomes = {a["ref"]: a["nome"] for a in p["arquivos"]}
        self.assertEqual((nomes["ha"], nomes["hb"]), ("BOLETO_NF500_1.pdf", "BOLETO_NF500_2.pdf"))
        self.assertEqual(p["faltando"], ["XML"])

    def test_boleto_sem_numero_usa_a_nota_do_pedido_e_sem_nota_nenhuma_usa_o_ps(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="500")
        self.doc("h2", "Boleto", "PS-10")
        self.doc("h3", "Boleto", "PS-11")
        p10, p11 = self.levantar("PS-10", "PS-11")
        self.assertEqual(p10["arquivos"][-1]["nome"], "BOLETO_NF500.pdf")
        self.assertEqual(p11["arquivos"][0]["nome"], "BOLETO_PS-11.pdf")
        self.assertEqual(p11["faltando"], ["XML", "DANFE"])

    def test_numero_da_nota_do_xml_vale_quando_nao_ha_danfe(self):
        self.xml("PS-10", "000777")
        self.doc("h2", "Boleto", "PS-10")
        (p,) = self.levantar("PS-10")
        self.assertEqual([a["nome"] for a in p["arquivos"]], ["XML_NF777.xml", "BOLETO_NF777.pdf"])

    def test_so_entra_documento_enviado_com_arquivo_em_disco_e_do_proprio_pedido(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="1", status="REVISAO_MANUAL")
        self.doc("h2", "Boleto", "PS-10", nf="1", arquivo=False)
        self.doc("h3", "Outro", "PS-10")
        self.doc("h4", "Nota Fiscal", "PS-100", nf="9")
        (p,) = self.levantar("PS-10")
        self.assertEqual(p["arquivos"], [])
        self.assertEqual(p["faltando"], ["XML", "DANFE", "BOLETO"])

    def test_mesma_nota_duas_vezes_fica_so_a_mais_nova_e_notas_diferentes_ficam_as_duas(self):
        self.doc("velho", "Nota Fiscal", "PS-10", nf="1", quando="2026-11-01 08:00:00")
        self.doc("novo", "Nota Fiscal", "PS-10", nf="1", quando="2026-11-02 08:00:00")
        self.doc("outra", "Nota Fiscal", "PS-10", nf="2")
        (p,) = self.levantar("PS-10")
        self.assertEqual(sorted(a["ref"] for a in p["arquivos"]), ["novo", "outra"])

    def test_reentrega_usa_os_documentos_do_original_e_nao_repete(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="1")
        pedidos = self.levantar("PS-10", "PS-10-R2")
        self.assertEqual(len(pedidos), 1)
        self.assertEqual(self.levantar("PS-10-R2")[0]["arquivos"][0]["ref"], "h1")

    def test_codigo_guardado_sem_o_prefixo_ps_tambem_acha(self):
        self.doc("h1", "Nota Fiscal", "10", nf="1")
        self.assertEqual(len(self.levantar("PS-10")[0]["arquivos"]), 1)

    def test_mesmo_nome_em_dois_pedidos_ganha_sufixo(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="1")
        self.doc("h2", "Nota Fiscal", "PS-11", nf="1")
        p10, p11 = self.levantar("PS-10", "PS-11")
        self.assertEqual({p10["arquivos"][0]["nome"], p11["arquivos"][0]["nome"]}, {"DANFE_NF1_1.pdf", "DANFE_NF1_2.pdf"})

    def test_arquivo_so_devolve_referencia_do_proprio_pedido(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="1")
        self.doc("h2", "Nota Fiscal", "PS-11", nf="2")
        self.xml("PS-10", "1")
        caminho, nome = de.arquivo(self.conn, "PS-10", "h1", self.xmls, self.resolver)
        self.assertEqual((caminho.name, nome), ("h1.pdf", "DANFE_NF1.pdf"))
        self.assertEqual(de.arquivo(self.conn, "PS-10", "xml:PS-10", self.xmls, self.resolver)[1], "XML_NF1.xml")
        self.assertIsNone(de.arquivo(self.conn, "PS-10", "h2", self.xmls, self.resolver))
        self.assertIsNone(de.arquivo(self.conn, "PS-10", "xml:PS-11", self.xmls, self.resolver))

    def test_banco_sem_a_tabela_de_documentos_nao_quebra(self):
        self.conn.execute("DROP TABLE documentos_processados")
        self.assertEqual(self.levantar("PS-10")[0]["arquivos"], [])


class TestEmails(unittest.TestCase):
    def test_aceita_varios_separadores_e_tira_repetido(self):
        self.assertEqual(de.validar_emails(" A@x.com; b@y.com.br , a@x.com "), ["a@x.com", "b@y.com.br"])

    def test_recusa_vazio_invalido_e_demais(self):
        for texto in ("", "   ", "sem-arroba", "a@b", "a@b.com\nBcc: x@y.com <z>", "a@@b.com", "<a@b.com>"):
            with self.assertRaises(de.ErroDocumentos, msg=texto):
                de.validar_emails(texto)
        with self.assertRaises(de.ErroDocumentos):
            de.validar_emails(",".join(f"p{i}@x.com" for i in range(de.MAX_EMAILS + 1)))


class TestEnviar(_Base):
    ITENS = [{"codigo": "PS-10", "destinatario": "Mercado  Bom ltda"}]

    def test_um_email_com_todos_os_anexos_renomeados_e_registro(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="42")
        self.doc("h2", "Boleto", "PS-10", nf="42")
        self.doc("h3", "Nota Fiscal", "PS-11", nf="43")
        self.xml("PS-10", "42")
        r = self.enviar(self.ITENS + [{"codigo": "PS-11", "destinatario": "Mercado Bom LTDA"}])
        self.assertEqual((r["arquivos"], r["partes"], r["emails"]), (4, 1, ["financeiro@cliente.com.br"]))
        self.assertEqual(r["faltando"], {"PS-11": ["XML", "BOLETO"]})
        (e,) = self.enviados
        self.assertEqual(e["para"], ["financeiro@cliente.com.br"])
        self.assertEqual(e["assunto"], "Documentos fiscais - NF 42, 43")
        self.assertEqual(e["anexos"], [("PS-10.xml", "XML_NF42.xml"), ("h1.pdf", "DANFE_NF42.pdf"),
                                       ("h2.pdf", "BOLETO_NF42.pdf"), ("h3.pdf", "DANFE_NF43.pdf")])
        self.assertIn("BOLETO_NF42.pdf", e["corpo"])
        reg = dict(self.conn.execute("SELECT * FROM pedidos_documentos_envios").fetchone())
        self.assertEqual((reg["usuario"], reg["emails"], reg["ok"], reg["partes"], reg["quando"]),
                         ("ana", "financeiro@cliente.com.br", 1, 1, "2026-11-13 09:00:00"))
        self.assertIn("BOLETO_NF42.pdf", reg["arquivos"])

    def test_segundo_envio_lembra_o_email_pelo_destinatario_e_pelo_pedido(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="42")
        self.doc("h2", "Nota Fiscal", "PS-20", nf="50")
        de.garantir_tabelas(self.conn)
        self.assertEqual(de.lembrados(self.conn, self.ITENS), [])
        self.enviar(self.ITENS)
        self.assertEqual(de.lembrados(self.conn, [{"codigo": "PS-20", "destinatario": "MERCADO BOM LTDA"}]),
                         ["financeiro@cliente.com.br"])
        self.assertEqual(de.lembrados(self.conn, [{"codigo": "PS-10-R2", "destinatario": ""}]), ["financeiro@cliente.com.br"])
        self.assertEqual(de.lembrados(self.conn, [{"codigo": "PS-20", "destinatario": "Outro Cliente"}]), [])
        self.enviar(self.ITENS, emails="novo@cliente.com.br")
        self.assertEqual(de.lembrados(self.conn, self.ITENS), ["novo@cliente.com.br"])

    def test_lote_so_sugere_o_email_quando_todos_os_pedidos_tem_o_mesmo(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="42")
        self.doc("h2", "Nota Fiscal", "PS-20", nf="50")
        outro = {"codigo": "PS-20", "destinatario": "Outro Cliente"}
        self.enviar(self.ITENS)
        self.assertEqual(de.lembrados(self.conn, self.ITENS + [outro]), [])       # o outro cliente nunca recebeu
        self.enviar([outro], emails="b@outro.com.br")
        self.assertEqual(de.lembrados(self.conn, self.ITENS + [outro]), [])       # cada um tem o seu
        mesmo = {"codigo": "PS-30", "destinatario": "MERCADO BOM LTDA"}
        self.assertEqual(de.lembrados(self.conn, self.ITENS + [mesmo]), ["financeiro@cliente.com.br"])

    def test_divide_em_mais_de_um_email_quando_passa_do_tamanho(self):
        for i in range(3):
            self.doc(f"h{i}", "Nota Fiscal", f"PS-{10 + i}", nf=str(i + 1), tamanho=600)
        r = self.enviar([{"codigo": f"PS-{10 + i}", "destinatario": ""} for i in range(3)], limite_bytes=1300)
        self.assertEqual((r["partes"], [len(e["anexos"]) for e in self.enviados]), (2, [2, 1]))
        self.assertEqual([e["assunto"] for e in self.enviados],
                         ["Documentos fiscais - NF 1, 2 (1/2)", "Documentos fiscais - NF 3 (2/2)"])

    def test_falha_no_envio_avisa_registra_e_nao_lembra_o_email(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="42")
        with self.assertRaises(de.ErroDocumentos) as ctx:
            self.enviar(self.ITENS, ok=False)
        self.assertIn("Falha ao enviar", str(ctx.exception))
        self.assertEqual(self.conn.execute("SELECT ok FROM pedidos_documentos_envios").fetchone()[0], 0)
        self.assertEqual(de.lembrados(self.conn, self.ITENS), [])

    def test_recusas_antes_de_enviar(self):
        self.doc("h1", "Nota Fiscal", "PS-10", nf="42")
        for itens, emails in (([], "a@b.com"), (self.ITENS, ""), (self.ITENS, "invalido"),
                              ([{"codigo": "PS-99", "destinatario": ""}], "a@b.com"),
                              ([{"codigo": f"PS-{i}", "destinatario": ""} for i in range(de.MAX_PEDIDOS + 1)], "a@b.com")):
            with self.assertRaises(de.ErroDocumentos):
                self.enviar(itens, emails=emails)
        self.assertEqual(self.enviados, [])


class TestRotas(_Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(banco, "DB_PATH", self.db),
                  mock.patch.object(painel_agentes.acesso_telas, "DB_PATH", self.db),
                  mock.patch.object(painel_agentes.contadores_menu, "contadores", return_value={}),
                  mock.patch.object(de, "PASTA_XML", self.xmls),
                  mock.patch.object(de, "resolver_arquivo_local", side_effect=self.resolver),
                  mock.patch.object(de.email_utils, "enviar_email", side_effect=self.falso_email())):
            p.start()
            self.addCleanup(p.stop)
        banco.conectar().close()
        self.doc("h1", "Nota Fiscal", "PS-10", nf="42")
        self.doc("h2", "Boleto", "PS-10", nf="42")
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        self.corpo = {"itens": [{"codigo": "#PS-10", "destinatario": "Mercado Bom"}]}

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def test_conferir_baixar_e_enviar(self):
        self._logar("atendimento")
        r = self.cliente.post("/api/pedidos/documentos/conferir", json=self.corpo, headers=ORIGEM)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        (p,) = d["pedidos"]
        self.assertEqual([a["nome"] for a in p["arquivos"]], ["DANFE_NF42.pdf", "BOLETO_NF42.pdf"])
        self.assertEqual((p["faltando"], d["emails_lembrados"], d["partes"]), (["XML"], [], 1))
        self.assertNotIn("caminho", p["arquivos"][0])

        b = self.cliente.get(p["arquivos"][1]["url"])
        self.assertEqual(b.status_code, 200)
        self.assertIn("BOLETO_NF42.pdf", b.headers["Content-Disposition"])
        self.assertTrue(b.data.startswith(b"%PDF-"))
        b.close()

        r = self.cliente.post("/api/pedidos/documentos/enviar", json={**self.corpo, "emails": "fin@cliente.com"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.enviados[0]["para"], ["fin@cliente.com"])
        r = self.cliente.post("/api/pedidos/documentos/conferir", json=self.corpo, headers=ORIGEM)
        self.assertEqual(r.get_json()["emails_lembrados"], ["fin@cliente.com"])
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT usuario FROM pedidos_documentos_envios").fetchone()[0], "teste")

    def test_recusas_das_rotas(self):
        self._logar("atendimento")
        ruins = ({}, {"itens": []}, {"itens": "PS-10"}, {"itens": [{"codigo": "../etc"}]}, {"itens": ["PS-10"]},
                 {"itens": [{"codigo": "PS-1"}] * (de.MAX_PEDIDOS + 1)})
        for corpo in ruins:
            for rota in ("conferir", "enviar"):
                r = self.cliente.post(f"/api/pedidos/documentos/{rota}", json=corpo, headers=ORIGEM)
                self.assertEqual(r.status_code, 400, (rota, corpo))
        r = self.cliente.post("/api/pedidos/documentos/enviar", json={**self.corpo, "emails": "ruim"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        self.assertIn("inválido", r.get_json()["erro"])
        self.assertEqual(self.cliente.get("/api/pedidos/documentos/arquivo?codigo=PS-10&ref=nao-existe").status_code, 404)
        self.assertEqual(self.cliente.get("/api/pedidos/documentos/arquivo?codigo=x&ref=h1").status_code, 404)
        self.assertEqual(self.enviados, [])

    def test_leitura_e_galpao_nao_usam_e_sem_origem_recusa(self):
        for nivel in ("leitura", "galpao", "expedicao"):
            self._logar(nivel)
            for rota in ("conferir", "enviar"):
                r = self.cliente.post(f"/api/pedidos/documentos/{rota}", json={**self.corpo, "emails": "a@b.com"}, headers=ORIGEM)
                self.assertEqual(r.status_code, 403, (nivel, rota))
            self.assertEqual(self.cliente.get("/api/pedidos/documentos/arquivo?codigo=PS-10&ref=h1").status_code, 403)
        self._logar("operador")
        r = self.cliente.post("/api/pedidos/documentos/enviar", json={**self.corpo, "emails": "a@b.com"},
                              headers={"Origin": "http://outro.exemplo"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.enviados, [])


if __name__ == "__main__":
    unittest.main()
