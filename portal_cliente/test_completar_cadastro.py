"""Tela /completar-cadastro do portal (pre-cadastro automatico, 10/10).

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_completar_cadastro
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

import app as portal  # noqa: E402
import cadastro_embarcador as cad  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402

CNPJ_A = "49874051000131"   # pre-cadastro aberto
CNPJ_B = "22222222000122"   # cliente antigo, sem pre-cadastro
ORIGEM = {"Origin": "http://localhost"}


class TestCompletarCadastro(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        patcher = patch.object(portal.auth, "DB_PATH", self.db)
        patcher.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(patcher.stop)
        self.despachar = patch("notificar_whatsapp.despachar", return_value="enviado").start()
        self.addCleanup(patch.stopall)

        conn = portal.auth.conectar()
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER, "
                     "habilidade TEXT, atualizado_em TEXT)")
        conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?,NULL,NULL)", [
            (CNPJ_A, "NONNA DISTRIBUICAO", "FABENNE", None, 1, 23084712, 100),
            (CNPJ_B, "Beta SA", "Beta", "cadastro@beta.com", 1, 102, 9002)])
        conn.commit()
        cad.registrar(conn, CNPJ_A, 100, cad.CONVIDADO, email="mariana@fabenne.com")
        for cnpj in (CNPJ_A, CNPJ_B):
            portal.auth.definir_pin(conn, cnpj, "135790")
        self.versao = {c: portal.auth.versao_conta(portal.auth.buscar_conta(conn, c)) for c in (CNPJ_A, CNPJ_B)}
        conn.close()
        portal.app.config["TESTING"] = True
        self.tc = portal.app.test_client()

    def entrar(self, cnpj=CNPJ_A):
        with self.tc.session_transaction() as s:
            s["cnpj"], s["v"] = cnpj, self.versao[cnpj]

    def conn(self):
        c = sqlite3.connect(self.db)
        self.addCleanup(c.close)
        return c

    def test_inicio_e_api_desviam_pra_completar(self):
        self.entrar()
        r = self.tc.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/completar-cadastro", r.headers["Location"])
        r = self.tc.get("/api/notificacoes")
        self.assertEqual(r.status_code, 403)
        self.assertIn("cadastro", r.get_json()["erro"])

    def test_tela_mostra_tipos_e_o_email_do_cadastro(self):
        self.entrar()
        html = self.tc.get("/completar-cadastro").get_data(as_text=True)
        for tipo in ("Seco", "Refrigerado", "Congelado"):
            self.assertIn(f'value="{tipo}"', html)
        self.assertIn("mariana@fabenne.com", html)

    def test_salvar_grava_preferencias_e_cadastro_sem_mexer_em_interno(self):
        self.entrar()
        r = self.tc.post("/completar-cadastro", headers=ORIGEM, data={
            "habilidade": "Refrigerado", "emails": "avisos@fabenne.com\nfiscal@fabenne.com", "whatsapp": "(11) 98888-7777"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True))
        self.assertNotIn("/completar-cadastro", r.headers["Location"])
        prefs = pn.ler(self.conn(), CNPJ_A)
        self.assertEqual((prefs["emails"], prefs["whatsapp"]), (["avisos@fabenne.com", "fiscal@fabenne.com"], "5511988887777"))
        c = cad.buscar(self.conn(), CNPJ_A)
        self.assertEqual((c["situacao"], c["habilidade_informada"]), ("PREENCHIDO", "Refrigerado"))
        self.assertIsNone(self.conn().execute("SELECT habilidade FROM interno WHERE cnpj_embarcador = ?", (CNPJ_A,)).fetchone()[0])
        self.assertIn("FABENNE completou o cadastro: Refrigerado", self.despachar.call_args.args[3])
        self.assertEqual(self.tc.get("/api/notificacoes").status_code, 200)

    def test_email_invalido_nao_grava_nada(self):
        self.entrar()
        r = self.tc.post("/completar-cadastro", headers=ORIGEM, data={
            "habilidade": "Seco", "emails": "isso-nao-e-email", "whatsapp": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIn("E-mail inválido", r.get_data(as_text=True))
        self.assertEqual(cad.buscar(self.conn(), CNPJ_A)["situacao"], "CONVIDADO")
        self.assertEqual(pn.ler(self.conn(), CNPJ_A)["emails"], [])
        self.despachar.assert_not_called()

    def test_sem_tipo_ou_sem_email_nao_grava(self):
        self.entrar()
        r = self.tc.post("/completar-cadastro", headers=ORIGEM, data={"habilidade": "", "emails": "a@b.com"})
        self.assertIn("tipo de carga", r.get_data(as_text=True))
        r = self.tc.post("/completar-cadastro", headers=ORIGEM, data={"habilidade": "Seco", "emails": " "})
        self.assertIn("pelo menos um e-mail", r.get_data(as_text=True))
        self.assertEqual(cad.buscar(self.conn(), CNPJ_A)["situacao"], "CONVIDADO")

    def test_cliente_sem_pre_cadastro_nao_ve_a_tela(self):
        self.entrar(CNPJ_B)
        self.assertEqual(self.tc.get("/api/notificacoes").status_code, 200)
        r = self.tc.get("/completar-cadastro")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/completar-cadastro", r.headers["Location"])

    def test_equipe_nao_e_desviada(self):
        with self.tc.session_transaction() as s:
            s["equipe"] = {"usuario": "hugo", "nivel": "operador"}
            s["cnpj_equipe"] = CNPJ_A
        self.assertEqual(self.tc.get("/api/notificacoes").status_code, 200)
        r = self.tc.get("/completar-cadastro")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/completar-cadastro", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
