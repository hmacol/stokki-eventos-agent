"""Pagina publica do link da consulta de transportadora (embarcador responde sem login).

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_consulta_transportadora_web -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask

_AQUI = Path(__file__).parent
sys.path.insert(0, str(_AQUI.parent))
sys.path.insert(0, str(_AQUI))

import consulta_transportadora as ctr
import consulta_transportadora_web as web

SEGREDO = "segredo"
CNPJ_A = "11222333000181"
CNPJ_B = "44555666000172"


def criar_app(db_path):
    app = Flask(__name__, template_folder=str(_AQUI / "templates"), static_folder=str(_AQUI / "static"))
    app.config["TESTING"] = True
    app.add_url_rule("/regioes-logo", "regioes_logo", lambda: ("", 204))  # o base_acesso.html usa
    web.registrar(app, config={}, secret=SEGREDO, db_path=db_path)
    return app


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "t.db"
        conn = ctr.conectar_consultas(self.db)
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, stkkc_id INTEGER, apelido TEXT, nome_remetente TEXT)")
        conn.execute("INSERT INTO interno VALUES (?, 1, 'LOJA A', 'Razao A')", (CNPJ_A,))
        conn.execute("INSERT INTO interno VALUES (?, 2, NULL, 'Razao B')", (CNPJ_B,))
        conn.commit()
        r1 = ctr.registrar_desconhecida(conn, "TRANSP NOVA", "99888777000166", "PS-1", CNPJ_A, 1,
                                        {"destinatario": "Fulano", "cidade": "Campinas", "uf": "SP"})
        r2 = ctr.registrar_desconhecida(conn, "TRANSP NOVA", "99888777000166", "PS-2", CNPJ_B, 2)
        self.tid = r1["transportadora"]["id"]
        self.cid_a, self.cid_b = r1["consulta"]["id"], r2["consulta"]["id"]
        conn.close()
        self.client = criar_app(self.db).test_client()

    def url(self, cid):
        return f"/transportadora/{ctr.gerar_token(SEGREDO, cid)}"

    def transp(self):
        conn = ctr.conectar_consultas(self.db)
        try:
            return dict(conn.execute("SELECT * FROM transportadoras WHERE id=?", (self.tid,)).fetchone())
        finally:
            conn.close()

    def consulta(self, cid):
        conn = ctr.conectar_consultas(self.db)
        try:
            return dict(conn.execute("SELECT * FROM transportadora_consultas WHERE id=?", (cid,)).fetchone())
        finally:
            conn.close()


class TestPagina(Base):
    def test_get_token_valido_mostra_tres_opcoes(self):
        r = self.client.get(self.url(self.cid_a))
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("A transportadora vem retirar no galpão", html)
        self.assertIn("Levar até a transportadora (redespacho)", html)
        self.assertIn("Entregar no endereço do pedido", html)
        self.assertIn("TRANSP NOVA", html)
        self.assertIn("PS-1", html)
        self.assertNotIn("PS-2", html)  # so os pedidos deste embarcador

    def test_post_retirada_ativa_e_confirma(self):
        r = self.client.post(self.url(self.cid_a), data={"opcao": "retirada", "nome": "Maria"})
        self.assertIn("Resposta registrada", r.get_data(as_text=True))
        t = self.transp()
        self.assertEqual((t["tipo"], t["situacao"]), ("RETIRADA", "ATIVA"))
        c = self.consulta(self.cid_a)
        self.assertEqual(c["respondido_por"], "Maria (LOJA A) pelo link")
        self.assertEqual(c["canal_resposta"], "link")

    def test_apelido_cai_para_nome_remetente(self):
        self.client.post(self.url(self.cid_b), data={"opcao": "entrega", "nome": "Joao"})
        self.assertEqual(self.consulta(self.cid_b)["respondido_por"], "Joao (Razao B) pelo link")

    def test_post_redespacho_sem_numero_volta_com_erro(self):
        r = self.client.post(self.url(self.cid_a), data={
            "opcao": "redespacho", "nome": "Maria", "cep": "13000-000", "logradouro": "Rua X",
            "municipio": "Campinas", "uf": "SP"})
        html = r.get_data(as_text=True)
        self.assertIn("número", html)
        self.assertIn('value="Rua X"', html)  # valores digitados voltam
        self.assertEqual(self.transp()["situacao"], "PENDENTE")

    def test_post_redespacho_completo_grava_terceiros(self):
        r = self.client.post(self.url(self.cid_a), data={
            "opcao": "redespacho", "nome": "Maria", "cep": "13000000", "logradouro": "Rua X", "numero": "10",
            "municipio": "Campinas", "uf": "sp", "bairro": "Centro", "email_transportadora": "t@t.com"})
        self.assertIn("Resposta registrada", r.get_data(as_text=True))
        t = self.transp()
        self.assertEqual((t["tipo"], t["situacao"], t["uf"], t["logradouro"], t["numero"]),
                         ("TERCEIROS", "ATIVA", "SP", "Rua X", "10"))
        self.assertEqual(t["email"], "t@t.com")

    def test_post_sem_nome_volta_com_erro(self):
        r = self.client.post(self.url(self.cid_a), data={"opcao": "retirada", "nome": " "})
        self.assertIn("Informe seu nome", r.get_data(as_text=True))
        self.assertEqual(self.transp()["situacao"], "PENDENTE")

    def test_link_de_consulta_ja_respondida_mostra_definido(self):
        self.client.post(self.url(self.cid_b), data={"opcao": "retirada", "nome": "Joao"})
        r = self.client.get(self.url(self.cid_a))
        html = r.get_data(as_text=True)
        self.assertIn("Retirada", html)
        self.assertNotIn("<form", html)

    def test_post_em_ja_respondida_nao_regrava(self):
        self.client.post(self.url(self.cid_b), data={"opcao": "retirada", "nome": "Joao"})
        r = self.client.post(self.url(self.cid_a), data={"opcao": "entrega", "nome": "Maria"})
        self.assertNotIn("<form", r.get_data(as_text=True))
        self.assertEqual(self.transp()["tipo"], "RETIRADA")

    def test_token_invalido(self):
        r = self.client.get("/transportadora/lixo")
        self.assertIn("Link inválido", r.get_data(as_text=True))
        self.assertNotIn("<form", r.get_data(as_text=True))

    def test_token_de_consulta_inexistente_e_invalido(self):
        r = self.client.get(self.url(9999))
        self.assertIn("Link inválido", r.get_data(as_text=True))

    def test_token_expirado(self):
        app = Flask(__name__, template_folder=str(_AQUI / "templates"), static_folder=str(_AQUI / "static"))
        app.add_url_rule("/regioes-logo", "regioes_logo", lambda: ("", 204))
        web.registrar(app, config={"consulta_transportadora": {"validade_dias": -1}}, secret=SEGREDO, db_path=self.db)
        r = app.test_client().get(self.url(self.cid_a))
        self.assertIn("Link expirado", r.get_data(as_text=True))

    def test_cep_exige_token_valido(self):
        r = self.client.get("/transportadora/lixo/cep/13000000")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
