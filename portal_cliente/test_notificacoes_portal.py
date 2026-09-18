"""Rotas do botao Notificacoes do portal (GET/POST /api/notificacoes).

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_notificacoes_portal
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_AQUI = Path(__file__).parent
sys.path.insert(0, str(_AQUI.parent))
sys.path.insert(0, str(_AQUI))

import app as portal
import preferencias_notificacao as pn

CNPJ_A = "11111111000111"
CNPJ_B = "22222222000122"
ORIGEM = {"Origin": "http://localhost"}


class TestApiNotificacoes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        patcher = patch.object(portal.auth, "DB_PATH", self.db)
        patcher.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(patcher.stop)

        conn = portal.auth.conectar()
        conn.execute(
            "CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
            "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)"
        )
        conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?)", [
            (CNPJ_A, "Alfa LTDA", "Alfa", "cadastro@alfa.com", 1, 101, 9001),
            (CNPJ_B, "Beta SA", "Beta", "cadastro@beta.com", 1, 102, 9002),
        ])
        conn.commit()
        portal.auth.definir_pin(conn, CNPJ_A, "123456")
        self.versao_a = portal.auth.versao_conta(portal.auth.buscar_conta(conn, CNPJ_A))
        conn.close()

        portal.app.config["TESTING"] = True
        self.tc = portal.app.test_client()

    def _entrar_como_cliente(self):
        with self.tc.session_transaction() as s:
            s["cnpj"], s["v"] = CNPJ_A, self.versao_a

    def _entrar_como_equipe(self, nivel="operador", cnpj=CNPJ_B):
        with self.tc.session_transaction() as s:
            s["equipe"] = {"usuario": "hugo", "nivel": nivel}
            s["cnpj_equipe"] = cnpj

    def _prefs(self, cnpj):
        conn = sqlite3.connect(self.db)
        try:
            return pn.ler(conn, cnpj)
        finally:
            conn.close()

    def test_sem_sessao_da_401(self):
        self.assertEqual(self.tc.get("/api/notificacoes").status_code, 401)
        self.assertEqual(self.tc.post("/api/notificacoes", json={}, headers=ORIGEM).status_code, 401)

    def test_get_devolve_os_seis_tipos_com_rotulo_e_os_defaults(self):
        self._entrar_como_cliente()
        corpo = self.tc.get("/api/notificacoes").get_json()
        self.assertEqual([t["tipo"] for t in corpo["tipos"]], list(pn.TIPOS))
        ligados = {t["tipo"]: t["ligado"] for t in corpo["tipos"]}
        self.assertFalse(ligados["resumo_diario"])
        self.assertTrue(ligados["entrega_concluida"])
        self.assertTrue(all(t["rotulo"] and t["descricao"] for t in corpo["tipos"]))
        self.assertEqual([t["grupo"] for t in corpo["tipos"]],
                         ["acompanhamento"] * 3 + ["acao"] * 3)
        self.assertFalse(corpo["somente_leitura"])
        self.assertEqual(corpo["emails"], [])
        self.assertEqual(corpo["emails_cadastro"], ["cadastro@alfa.com"])

    def test_post_grava_e_devolve_o_estado_novo(self):
        self._entrar_como_cliente()
        r = self.tc.post("/api/notificacoes", headers=ORIGEM, json={
            "emails": "avisos@alfa.com\nfiscal@alfa.com", "tipos": {"resumo_diario": True, "insucesso": False}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["emails"], ["avisos@alfa.com", "fiscal@alfa.com"])
        prefs = self._prefs(CNPJ_A)
        self.assertTrue(prefs["tipos"]["resumo_diario"])
        self.assertFalse(prefs["tipos"]["insucesso"])
        self.assertEqual(prefs["atualizado_por"], "cliente")

    def test_cnpj_no_corpo_e_ignorado(self):
        self._entrar_como_cliente()
        self.tc.post("/api/notificacoes", headers=ORIGEM,
                     json={"cnpj": CNPJ_B, "emails": "x@alfa.com", "tipos": {"insucesso": False}})
        self.assertEqual(self._prefs(CNPJ_A)["emails"], ["x@alfa.com"])
        self.assertEqual(self._prefs(CNPJ_B)["emails"], [])
        self.assertTrue(self._prefs(CNPJ_B)["tipos"]["insucesso"])

    def test_email_invalido_da_400_com_mensagem_e_nao_grava(self):
        self._entrar_como_cliente()
        r = self.tc.post("/api/notificacoes", headers=ORIGEM,
                         json={"emails": "nao-e-email", "tipos": {"resumo_diario": True}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("inválido", r.get_json()["erro"])
        self.assertFalse(self._prefs(CNPJ_A)["tipos"]["resumo_diario"])

    def test_corpo_malformado_da_400(self):
        self._entrar_como_cliente()
        for corpo in ({"tipos": ["resumo_diario"]}, {"tipos": {"promocoes": True}}, {"emails": {"a": 1}}):
            with self.subTest(corpo=corpo):
                self.assertEqual(self.tc.post("/api/notificacoes", headers=ORIGEM, json=corpo).status_code, 400)

    def test_origem_de_outro_site_da_403(self):
        self._entrar_como_cliente()
        r = self.tc.post("/api/notificacoes", headers={"Origin": "https://malicioso.example"},
                         json={"tipos": {"insucesso": False}})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self._prefs(CNPJ_A)["tipos"]["insucesso"])

    def test_equipe_grava_em_nome_do_cliente_escolhido(self):
        self._entrar_como_equipe()
        r = self.tc.post("/api/notificacoes", headers=ORIGEM, json={"tipos": {"resumo_diario": True}})
        self.assertEqual(r.status_code, 200)
        prefs = self._prefs(CNPJ_B)
        self.assertTrue(prefs["tipos"]["resumo_diario"])
        self.assertEqual(prefs["atualizado_por"], "equipe:hugo")

    def test_equipe_so_leitura_ve_mas_nao_altera(self):
        self._entrar_como_equipe(nivel="leitura")
        self.assertTrue(self.tc.get("/api/notificacoes").get_json()["somente_leitura"])
        r = self.tc.post("/api/notificacoes", headers=ORIGEM, json={"tipos": {"resumo_diario": True}})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self._prefs(CNPJ_B)["tipos"]["resumo_diario"])

    def test_tela_inicial_tem_o_botao_notificacoes(self):
        self._entrar_como_cliente()
        with patch.object(portal.dados, "montar_dia", return_value={}):
            html = self.tc.get("/").get_data(as_text=True)
        self.assertTrue('id="btn-notificacoes"' in html, "botao Notificacoes ausente do cabecalho")
        self.assertTrue("/api/notificacoes" in html, "script do modal nao aponta pra API")


if __name__ == "__main__":
    unittest.main()
