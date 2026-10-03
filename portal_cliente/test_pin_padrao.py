# -*- coding: utf-8 -*-
"""
Testes da criacao de contas com PIN padrao + troca obrigatoria no
primeiro acesso (pedido do Hugo, 02/10).

    py -3.11 -m unittest portal_cliente.test_pin_padrao
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

SEM_CONTA = "12345678000195"
COM_CONTA = "22222222000122"
CANCELADO = "33333333000133"
SEM_SENDER = "44444444000144"
LOGIN_GRUPO = "55555555000155"
MEMBRO = "66666666000166"
PADRAO = "246810"


def _montar_banco():
    conn = auth.conectar()
    conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                 "apelido TEXT, email TEXT, observacoes TEXT)")
    conn.executemany("INSERT INTO interno VALUES (?, ?, ?, NULL, 'x@y.com', ?)", [
        (SEM_CONTA, 1, "SEM CONTA", None),
        (COM_CONTA, 2, "COM CONTA", ""),
        (CANCELADO, 3, "CANCELADO", "CANCELADO em 02/10/2026 (lista do Hugo)."),
        (SEM_SENDER, None, "SEM SENDER", None),
        (LOGIN_GRUPO, 5, "LOGIN GRUPO", None),
        (MEMBRO, 6, "MEMBRO", None),
    ])
    conn.commit()
    auth.definir_grupo(conn, LOGIN_GRUPO, [MEMBRO])
    auth.definir_pin(conn, COM_CONTA, "111111")
    return conn


class CriarComPinPadraoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(auth, "DB_PATH", Path(self._tmp.name) / "dados.db")
        self._patch.start()
        self.conn = _montar_banco()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def test_candidatos_pulam_conta_existente_cancelado_e_membro_de_grupo(self):
        cnpjs = [e["cnpj"] for e in auth.candidatos_pin_padrao(self.conn)]
        self.assertEqual(sorted(cnpjs), sorted([SEM_CONTA, LOGIN_GRUPO]))

    def test_cria_contas_marcadas_pra_trocar(self):
        criados = auth.criar_contas_pin_padrao(self.conn, PADRAO)
        self.assertEqual(sorted(e["cnpj"] for e in criados), sorted([SEM_CONTA, LOGIN_GRUPO]))
        for cnpj in (SEM_CONTA, LOGIN_GRUPO):
            self.assertEqual(auth.buscar_conta(self.conn, cnpj)["trocar_pin"], 1)
            self.assertTrue(auth.autenticar(self.conn, cnpj, PADRAO))

    def test_nao_mexe_em_quem_ja_tem_conta(self):
        auth.criar_contas_pin_padrao(self.conn, PADRAO)
        self.assertTrue(auth.autenticar(self.conn, COM_CONTA, "111111"))
        self.assertEqual(auth.buscar_conta(self.conn, COM_CONTA)["trocar_pin"], 0)

    def test_rodar_de_novo_nao_cria_nada(self):
        auth.criar_contas_pin_padrao(self.conn, PADRAO)
        self.assertEqual(auth.criar_contas_pin_padrao(self.conn, PADRAO), [])

    def test_pin_padrao_com_formato_errado_nao_grava(self):
        with self.assertRaises(ValueError):
            auth.criar_contas_pin_padrao(self.conn, "12ab")
        self.assertIsNone(auth.buscar_conta(self.conn, SEM_CONTA))

    def test_trocar_pin_desliga_a_marca_e_recusa_o_mesmo(self):
        auth.criar_contas_pin_padrao(self.conn, PADRAO)
        with self.assertRaises(ValueError):
            auth.trocar_pin_obrigatorio(self.conn, SEM_CONTA, PADRAO)
        conta = auth.trocar_pin_obrigatorio(self.conn, SEM_CONTA, "135790")
        self.assertEqual(conta["trocar_pin"], 0)
        self.assertTrue(auth.autenticar(self.conn, SEM_CONTA, "135790"))

    def test_definir_pin_pelo_link_tambem_desliga_a_marca(self):
        auth.criar_contas_pin_padrao(self.conn, PADRAO)
        self.assertEqual(auth.definir_pin(self.conn, SEM_CONTA, "135790")["trocar_pin"], 0)


class RotaTrocarPinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as portal   # le o config.yaml local (precisa de portal_cliente.secret_key)
        cls.portal = portal

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(auth, "DB_PATH", Path(self._tmp.name) / "dados.db")
        self._patch.start()
        conn = _montar_banco()
        auth.criar_contas_pin_padrao(conn, PADRAO)
        conn.close()
        self.cli = self.portal.app.test_client()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _entrar(self, cnpj=SEM_CONTA, pin=PADRAO):
        return self.cli.post("/login", data={"cnpj": cnpj, "pin": pin})

    def test_login_com_pin_padrao_vai_pra_troca(self):
        self._entrar()
        r = self.cli.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/trocar-pin", r.headers["Location"])

    def test_api_bloqueada_ate_trocar(self):
        self._entrar()
        r = self.cli.get("/api/dia")
        self.assertEqual(r.status_code, 403)
        self.assertIn("trocar", r.get_json()["erro"])

    def test_troca_libera_e_mantem_logado(self):
        self._entrar()
        r = self.cli.post("/trocar-pin", data={"pin": "135790", "pin2": "135790"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/trocar-pin", r.headers["Location"])
        # ainda logado (sessao nova com o PIN novo) e sem a marca: a tela de troca manda pro inicio
        r = self.cli.get("/trocar-pin")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/login", r.headers["Location"])
        self.assertNotIn("/trocar-pin", r.headers["Location"])

    def test_troca_recusa_pin_padrao_e_pins_diferentes(self):
        self._entrar()
        r = self.cli.post("/trocar-pin", data={"pin": PADRAO, "pin2": PADRAO})
        self.assertIn("diferente", r.get_data(as_text=True))
        r = self.cli.post("/trocar-pin", data={"pin": "135790", "pin2": "135791"})
        self.assertIn("não conferem", r.get_data(as_text=True))
        self.assertIn("/trocar-pin", self.cli.get("/").headers["Location"])

    def test_quem_ja_trocou_nao_ve_a_tela(self):
        self._entrar(COM_CONTA, "111111")
        r = self.cli.get("/trocar-pin")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/trocar-pin", r.headers["Location"])

    def test_sem_login_nao_abre_a_troca(self):
        r = self.cli.get("/trocar-pin")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
