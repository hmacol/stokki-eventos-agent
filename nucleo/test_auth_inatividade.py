# -*- coding: utf-8 -*-
"""
test_auth_inatividade.py

Motorista inativo na bd_motoristas continua entrando no app por 30 dias;
depois o login e o refresh sao recusados. Reativar libera na hora.
    py -3.11 -m unittest nucleo.test_auth_inatividade -v
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import auth_motorista as auth, banco  # noqa: E402
from regras import motoristas_banco as mb  # noqa: E402

CPF, PIN, SEGREDO = "12345678901", "482913", "segredo-de-teste"
FMT = "%Y-%m-%d %H:%M:%S"


class TestAuthInatividade(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()
        auth.criar_ou_atualizar_motorista(self.conn, CPF, "Motorista Teste", PIN)

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _linha(self, ativo, dias=None, cpf=CPF):
        mid = mb.inserir(self.conn, {"NOME_MOTORISTA": "M", "CPF_MOTORISTA": cpf, "ATIVO": ativo}, "teste")
        quando = None if dias is None else (datetime.now() - timedelta(days=dias)).strftime(FMT)
        self.conn.execute("UPDATE bd_motoristas SET inativado_em = ? WHERE id = ?", (quando, mid))
        self.conn.commit()
        return mid

    def test_inativo_ha_29_dias_entra(self):
        self._linha("NAO", 29)
        self.assertFalse(mb.login_bloqueado(self.conn, CPF))
        self.assertEqual(auth.autenticar(self.conn, CPF, PIN)["cpf"], CPF)

    def test_inativo_ha_31_dias_recusado(self):
        self._linha("NAO", 31)
        self.assertTrue(mb.login_bloqueado(self.conn, CPF))
        with self.assertRaises(auth.AutenticacaoInvalida) as ctx:
            auth.autenticar(self.conn, CPF, PIN)
        self.assertEqual(ctx.exception.codigo, 403)
        self.assertEqual(ctx.exception.mensagem, "Seu cadastro está inativo. Fale com a Fresh Log.")

    def test_pin_errado_em_cpf_vencido_fica_generico_e_conta_tentativa(self):
        self._linha("NAO", 31)
        with self.assertRaises(auth.AutenticacaoInvalida) as ctx:
            auth.autenticar(self.conn, CPF, "000000")
        self.assertEqual(ctx.exception.mensagem, "CPF ou PIN incorretos.")
        self.assertEqual(ctx.exception.codigo, 401)
        self.assertEqual(auth.buscar_motorista(self.conn, CPF)["tentativas_pin"], 1)

    def test_pin_certo_em_cpf_vencido_nao_registra_login(self):
        self._linha("NAO", 31)
        with self.assertRaises(auth.AutenticacaoInvalida):
            auth.autenticar(self.conn, CPF, "000000")
        with self.assertRaises(auth.AutenticacaoInvalida) as ctx:
            auth.autenticar(self.conn, CPF, PIN)
        self.assertEqual(ctx.exception.codigo, 403)
        m = auth.buscar_motorista(self.conn, CPF)
        self.assertEqual(m["tentativas_pin"], 0)
        self.assertIsNone(m["ultimo_login_em"])

    def test_reativado_entra_na_hora(self):
        mid = self._linha("NAO", 40)
        self.assertTrue(mb.login_bloqueado(self.conn, CPF))
        mb.atualizar(self.conn, mid, {"ATIVO": "SIM"}, "teste")
        self.assertFalse(mb.login_bloqueado(self.conn, CPF))
        self.assertEqual(auth.autenticar(self.conn, CPF, PIN)["cpf"], CPF)

    def test_duas_linhas_uma_ativa_entra(self):
        self._linha("NAO", 90)
        self._linha("SIM")
        self.assertFalse(mb.login_bloqueado(self.conn, CPF))

    def test_duas_inativas_vale_a_mais_recente(self):
        self._linha("NAO", 90)
        self._linha("NAO", 10)
        self.assertFalse(mb.login_bloqueado(self.conn, CPF))

    def test_cpf_fora_da_tabela_entra(self):
        self._linha("NAO", 90, cpf="99999999999")
        self.assertFalse(mb.login_bloqueado(self.conn, CPF))
        self.assertEqual(auth.autenticar(self.conn, CPF, PIN)["cpf"], CPF)

    def test_inativo_sem_data_nao_bloqueia(self):
        # dado legado importado como NAO sem inativado_em: data desconhecida, nao bloqueia
        self._linha("NAO", None)
        self.assertFalse(mb.login_bloqueado(self.conn, CPF))

    def test_hoje_injetado(self):
        self._linha("NAO", 0)
        self.assertTrue(mb.login_bloqueado(self.conn, CPF, hoje=datetime.now() + timedelta(days=31)))
        self.assertFalse(mb.login_bloqueado(self.conn, CPF, hoje=datetime.now() + timedelta(days=29)))

    def test_token_de_inativo_vencido_recusado(self):
        m = auth.buscar_motorista(self.conn, CPF)
        tokens = auth.emitir_tokens(SEGREDO, m)
        mid = self._linha("NAO", 10)
        self.assertIn("acesso", auth.renovar(SEGREDO, tokens["refresh"], self.conn))
        self.conn.execute("UPDATE bd_motoristas SET inativado_em = ? WHERE id = ?",
                          ((datetime.now() - timedelta(days=31)).strftime(FMT), mid))
        self.conn.commit()
        for chamada in (lambda: auth.renovar(SEGREDO, tokens["refresh"], self.conn),
                        lambda: auth.motorista_do_token_acesso(SEGREDO, tokens["acesso"], self.conn)):
            with self.assertRaises(auth.AutenticacaoInvalida) as ctx:
                chamada()
            self.assertEqual(ctx.exception.mensagem, "Sessão inválida. Entre de novo.")

    def test_falha_na_consulta_nao_bloqueia(self):
        with mock.patch.object(mb, "por_cpf", side_effect=RuntimeError("tabela sumiu")):
            with self.assertLogs("nucleo.auth_motorista", level="WARNING"):
                self.assertEqual(auth.autenticar(self.conn, CPF, PIN)["cpf"], CPF)


if __name__ == "__main__":
    unittest.main()
