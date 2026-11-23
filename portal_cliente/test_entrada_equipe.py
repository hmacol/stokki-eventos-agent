# -*- coding: utf-8 -*-
"""
Entrada da equipe no portal vinda do painel (botao Cotacao da Torre,
Hugo 16/11): o passe assinado abre a sessao de equipe sem senha, mas so
pra quem o login /equipe ja deixaria entrar, e sempre passa pela escolha
do cliente antes da cotacao.

    py -3.11 -m unittest portal_cliente.test_entrada_equipe
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

import auth_cliente as auth  # noqa: E402
import entrada_equipe  # noqa: E402


class PasseTest(unittest.TestCase):
    def test_ida_e_volta(self):
        passe = entrada_equipe.gerar("segredo", "valeria", "atendimento")
        lido = entrada_equipe.ler("segredo", passe)
        self.assertEqual((lido["usuario"], lido["nivel"]), ("valeria", "atendimento"))
        outro = entrada_equipe.ler("segredo", entrada_equipe.gerar("segredo", "valeria", "atendimento"))
        self.assertNotEqual(lido["unico"], outro["unico"])

    def test_outro_segredo_vazio_ou_adulterado_nao_vale(self):
        passe = entrada_equipe.gerar("segredo", "valeria", "atendimento")
        self.assertIsNone(entrada_equipe.ler("outro", passe))
        self.assertIsNone(entrada_equipe.ler("segredo", ""))
        self.assertIsNone(entrada_equipe.ler("segredo", passe[:-2] + "xx"))

    def test_vence(self):
        passe = entrada_equipe.gerar("segredo", "valeria", "atendimento")
        with mock.patch.object(entrada_equipe, "VALIDADE_SEGUNDOS", -1):
            self.assertIsNone(entrada_equipe.ler("segredo", passe))

    def test_sem_usuario_nao_vale(self):
        self.assertIsNone(entrada_equipe.ler("segredo", entrada_equipe.gerar("segredo", "", "total")))


class EntradaPeloPainelTest(unittest.TestCase):
    CNPJ = "12345678000195"

    @classmethod
    def setUpClass(cls):
        import app as portal   # le o config.yaml local (precisa de portal_cliente.secret_key)
        cls.portal = portal
        cls.telas = portal.acesso_telas

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = Path(self._tmp.name) / "dados.db"
        self._patches = [
            mock.patch.object(auth, "DB_PATH", db), mock.patch.object(self.telas, "DB_PATH", db),
            mock.patch.object(self.portal, "_carregar_config", return_value={"painel_agentes": {
                "usuario": "chefe", "usuario_operador": "oper", "usuario_leitura": "leitor"}}),
        ]
        for p in self._patches:
            p.start()
        conn = self.telas.conectar()
        self.telas.criar(conn, "valeria", "Valéria", "atendimento", "senha-dela", "hugo", set(), "Atendimento")
        conn.execute("UPDATE painel_usuarios SET trocar_senha = 0")
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                     "apelido TEXT, email TEXT)")
        conn.execute("INSERT INTO interno VALUES (?, 1, 'CLIENTE A', NULL, 'a@cliente.com')", (self.CNPJ,))
        conn.commit()
        self.conn = conn
        self.cli = self.portal.app.test_client()

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _entrar(self, usuario, nivel, proximo="/cotacao", segredo=None):
        passe = entrada_equipe.gerar(segredo or self.portal._SECRET, usuario, nivel)
        return self.cli.get("/equipe/entrar", query_string={"t": passe, "proximo": proximo})

    def _equipe(self):
        with self.cli.session_transaction() as s:
            return s.get("equipe")

    def test_nominal_sem_a_caixa_do_portal_nao_entra(self):
        r = self._entrar("valeria", "atendimento")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Portal do cliente liberado", r.get_data(as_text=True))
        self.assertIsNone(self._equipe())

    def test_nominal_com_a_caixa_entra_como_operador_e_escolhe_o_cliente(self):
        self.telas.mudar_portal(self.conn, "valeria", True, "hugo")
        r = self._entrar("valeria", "total")   # o nivel do passe nao manda em login nominal
        self.assertEqual(r.status_code, 302)
        self.assertIn("/equipe/cliente?proximo=/cotacao", r.headers["Location"])
        self.assertEqual(self._equipe()["nivel"], "operador")
        self.assertTrue(self._equipe()["nominal"])
        tela = self.cli.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn('name="proximo" value="/cotacao"', tela)
        self.assertIn("CLIENTE A", tela)
        r = self.cli.post("/equipe/cliente", data={"cnpj": self.CNPJ, "proximo": "/cotacao"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/cotacao"))

    def test_nominal_inativo_ou_com_senha_provisoria_nao_entra(self):
        self.telas.mudar_portal(self.conn, "valeria", True, "hugo")
        self.conn.execute("UPDATE painel_usuarios SET trocar_senha = 1")
        self.conn.commit()
        self.assertEqual(self._entrar("valeria", "atendimento").status_code, 403)
        self.conn.execute("UPDATE painel_usuarios SET trocar_senha = 0, ativo = 0")
        self.conn.commit()
        self.assertEqual(self._entrar("valeria", "atendimento").status_code, 403)

    def test_par_fixo_entra_com_o_proprio_nivel(self):
        self.assertEqual(self._entrar("leitor", "leitura").status_code, 302)
        self.assertEqual(self._equipe(), {"usuario": "leitor", "nivel": "leitura"})

    def test_par_fixo_nao_sobe_de_nivel_nem_vale_pra_nivel_sem_portal(self):
        self.assertEqual(self._entrar("leitor", "total").status_code, 403)
        self.assertEqual(self._entrar("alguem", "expedicao").status_code, 403)
        self.assertIsNone(self._equipe())

    def test_passe_de_outro_segredo_ou_vencido_cai_no_login_da_equipe(self):
        r = self._entrar("chefe", "total", segredo="outro-segredo")
        self.assertEqual(r.status_code, 403)
        corpo = r.get_data(as_text=True)
        self.assertIn("venceu", corpo)
        self.assertIn("/equipe?proximo=/cotacao", corpo)   # o formulario leva pra cotacao depois da senha
        self.assertIsNone(self._equipe())

    def test_passe_so_vale_uma_vez(self):
        passe = entrada_equipe.gerar(self.portal._SECRET, "chefe", "total")
        self.assertEqual(self.cli.get("/equipe/entrar", query_string={"t": passe}).status_code, 302)
        outro_navegador = self.portal.app.test_client()
        r = outro_navegador.get("/equipe/entrar", query_string={"t": passe})
        self.assertEqual(r.status_code, 403)
        with outro_navegador.session_transaction() as s:
            self.assertNotIn("equipe", s)

    def test_sempre_pede_o_cliente_mas_lembra_o_ultimo_do_mesmo_usuario(self):
        self._entrar("chefe", "total")
        self.cli.post("/equipe/cliente", data={"cnpj": self.CNPJ})
        r = self._entrar("chefe", "total")
        self.assertIn("/equipe/cliente", r.headers["Location"])
        self.assertIn(f'value="{self.CNPJ}" selected', self.cli.get(r.headers["Location"]).get_data(as_text=True))
        self._entrar("oper", "operador")   # outro usuario no mesmo navegador nao herda
        with self.cli.session_transaction() as s:
            self.assertNotIn("cnpj_equipe", s)

    def test_proximo_de_fora_nao_redireciona_pra_fora(self):
        self._entrar("chefe", "total", proximo="//mal.example/x")
        r = self.cli.post("/equipe/cliente", data={"cnpj": self.CNPJ, "proximo": "//mal.example/x"})
        self.assertNotIn("mal.example", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
