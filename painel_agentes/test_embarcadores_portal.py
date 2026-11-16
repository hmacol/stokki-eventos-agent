# -*- coding: utf-8 -*-
"""
Tela /embarcadores do painel: contas do portal do cliente (16/11).

    py -3.11 -m unittest test_embarcadores_portal      (de dentro de painel_agentes/)
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

from portal_cliente import auth_cliente as contas  # noqa: E402

# "painel_agentes" e o nome da pasta: o modulo do app precisa ser carregado pelo caminho
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)

SEM_CONTA = "12345678000195"
COM_CONTA = "22222222000122"
SEM_SENDER = "44444444000144"
SEM_EMAIL = "55555555000155"
ORIGEM = {"Origin": "http://localhost"}


class TestEmbarcadoresPortal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(contas, "DB_PATH", Path(self._tmp.name) / "dados.db")
        p.start()
        self.addCleanup(p.stop)
        conn = contas.conectar()
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                     "apelido TEXT, email TEXT, observacoes TEXT)")
        conn.executemany("INSERT INTO interno VALUES (?, ?, ?, NULL, 'x@y.com', NULL)", [
            (SEM_CONTA, 1, "SEM CONTA"), (COM_CONTA, 2, "COM CONTA"), (SEM_SENDER, None, "SEM SENDER")])
        conn.execute("INSERT INTO interno VALUES (?, 5, 'SEM EMAIL', NULL, '', NULL)", (SEM_EMAIL,))
        conn.commit()
        contas.definir_pin(conn, COM_CONTA, "111111")
        conn.close()
        # Nenhum teste manda e-mail de verdade.
        import email_utils
        p = mock.patch.object(email_utils, "enviar_email", return_value=True)
        self.enviar_email = p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True

    def _cliente(self, nivel="total"):
        cliente = painel_agentes.app.test_client()
        if nivel:
            with cliente.session_transaction() as sess:
                sess["nivel_acesso"] = nivel
                sess["usuario"] = "teste"
        return cliente

    def _conta(self, cnpj):
        conn = contas.conectar()
        try:
            return contas.buscar_conta(conn, cnpj)
        finally:
            conn.close()

    def _acao(self, acao, cnpj, nivel="total", headers=ORIGEM):
        return self._cliente(nivel).post("/api/embarcadores/conta", json={"cnpj": cnpj, "acao": acao}, headers=headers)

    def test_tela_lista_e_oferece_as_acoes(self):
        resp = self._cliente().get("/embarcadores")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("12.345.678/0001-95", html)
        self.assertIn("Criar conta", html)
        self.assertIn("Resetar PIN", html)
        self.assertNotIn("SEM SENDER", html)

    def test_so_o_total_entra(self):
        self.assertEqual(self._cliente("operador").get("/embarcadores").status_code, 403)
        self.assertEqual(self._acao("excluir", COM_CONTA, nivel="operador").status_code, 403)
        self.assertIsNotNone(self._conta(COM_CONTA))

    def test_post_de_outra_origem_e_barrado(self):
        self.assertEqual(self._acao("excluir", COM_CONTA, headers={"Origin": "http://outro.site"}).status_code, 403)
        self.assertIsNotNone(self._conta(COM_CONTA))

    def test_criar_devolve_pin_provisorio_que_entra_e_pede_troca(self):
        resp = self._acao("criar", SEM_CONTA)
        self.assertEqual(resp.status_code, 200)
        pin = resp.get_json()["pin"]
        self.assertRegex(pin, r"^\d{6}$")
        conn = contas.conectar()
        try:
            conta = contas.autenticar(conn, SEM_CONTA, pin)
        finally:
            conn.close()
        self.assertEqual(conta["trocar_pin"], 1)

    def test_criar_e_resetar_mandam_o_link_pros_emails_do_cadastro(self):
        resp = self._acao("criar", SEM_CONTA)
        self.assertEqual(resp.get_json()["email_para"], ["x@y.com"])
        destinos, _assunto, corpo = self.enviar_email.call_args.args[:3]
        self.assertEqual(destinos, ["x@y.com"])
        self.assertIn("/definir-pin/", corpo)
        self.assertNotIn(resp.get_json()["pin"], corpo)
        self._acao("resetar", COM_CONTA)
        self.assertEqual(self.enviar_email.call_count, 2)

    def test_sem_email_ou_envio_falho_ainda_devolve_o_pin(self):
        resp = self._acao("criar", SEM_EMAIL)
        self.assertRegex(resp.get_json()["pin"], r"^\d{6}$")
        self.assertEqual(resp.get_json()["email_para"], [])
        self.enviar_email.assert_not_called()
        self.enviar_email.side_effect = RuntimeError("smtp fora")
        resp = self._acao("resetar", COM_CONTA)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["email_para"], [])
        self.assertEqual(self._conta(COM_CONTA)["trocar_pin"], 1)

    def test_inativar_e_excluir_nao_mandam_email(self):
        self._acao("desativar", COM_CONTA)
        self._acao("excluir", COM_CONTA)
        self.enviar_email.assert_not_called()

    def test_criar_nao_passa_por_cima_de_conta_que_existe(self):
        antes = self._conta(COM_CONTA)["pin_hash"]
        self.assertEqual(self._acao("criar", COM_CONTA).status_code, 400)
        self.assertEqual(self._conta(COM_CONTA)["pin_hash"], antes)

    def test_resetar_troca_o_pin_e_mantem_a_conta_inativa(self):
        self._acao("desativar", COM_CONTA)
        resp = self._acao("resetar", COM_CONTA)
        self.assertEqual(resp.status_code, 200)
        conta = self._conta(COM_CONTA)
        self.assertEqual((conta["ativo"], conta["trocar_pin"]), (0, 1))
        conn = contas.conectar()
        try:
            with self.assertRaises(contas.AutenticacaoInvalida):
                contas.autenticar(conn, COM_CONTA, "111111")
        finally:
            conn.close()

    def test_inativar_e_reativar(self):
        self.assertEqual(self._acao("desativar", COM_CONTA).status_code, 200)
        self.assertEqual(self._conta(COM_CONTA)["ativo"], 0)
        self.assertEqual(self._acao("ativar", COM_CONTA).status_code, 200)
        self.assertEqual(self._conta(COM_CONTA)["ativo"], 1)

    def test_excluir_apaga_a_conta_e_deixa_o_embarcador(self):
        self.assertEqual(self._acao("excluir", COM_CONTA).status_code, 200)
        self.assertIsNone(self._conta(COM_CONTA))
        self.assertIn("COM CONTA", self._cliente().get("/embarcadores").get_data(as_text=True))

    def test_recusa_sem_conta_cnpj_de_fora_e_acao_estranha(self):
        self.assertEqual(self._acao("resetar", SEM_CONTA).status_code, 400)
        self.assertEqual(self._acao("excluir", SEM_CONTA).status_code, 400)
        self.assertEqual(self._acao("criar", SEM_SENDER).status_code, 400)
        self.assertEqual(self._acao("apagar-tudo", COM_CONTA).status_code, 400)


if __name__ == "__main__":
    unittest.main()
