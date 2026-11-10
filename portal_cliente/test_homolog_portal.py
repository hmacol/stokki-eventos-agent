# -*- coding: utf-8 -*-
"""
Homologacao (Task 5): o portal so deixa entrar o cliente teste.

    py -3.11 -m unittest portal_cliente.test_homolog_portal
"""
import os
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
from homologacao import ambiente  # noqa: E402

TESTE, OUTRO = "00000000000191", "54993021000184"
PIN = "123456"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        cfg = Path(self._tmp.name) / "cfg.yaml"
        cfg.write_text("homologacao:\n  portal_cnpjs_permitidos: []\n", encoding="utf-8")
        for p in (mock.patch.object(auth, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                  mock.patch.object(ambiente, "CAMINHO_CONFIG", cfg),
                  mock.patch.object(ambiente, "_cache_cnpjs", None)):
            p.start()
            self.addCleanup(p.stop)
        self.conn = auth.conectar()
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                          "apelido TEXT, email TEXT)")
        self.conn.executemany("INSERT INTO interno VALUES (?, ?, ?, NULL, ?)", [
            (TESTE, 1, "TESTE", "t@x.com"), (OUTRO, 2, "OUTRO", "o@x.com")])
        self.conn.commit()
        auth.definir_pin(self.conn, TESTE, PIN)
        auth.definir_pin(self.conn, OUTRO, PIN)

    def _tentativas(self, cnpj):
        return auth.buscar_conta(self.conn, cnpj)["tentativas_pin"]


class TestHomologPortal(_Base):
    def test_cnpj_fora_da_lista_recusado(self):
        with mock.patch.dict(os.environ, {"FRESH_AMBIENTE": "homolog"}):
            # PIN errado de proposito: a recusa vem antes de mexer em tentativas
            with self.assertRaises(auth.AutenticacaoInvalida) as ctx:
                auth.autenticar(self.conn, OUTRO, "000000")
        self.assertEqual(ctx.exception.codigo, 403)
        self.assertEqual(ctx.exception.mensagem, "Este CNPJ não tem acesso à homologação.")
        self.assertEqual(self._tentativas(OUTRO), 0)

    def test_cliente_teste_entra(self):
        with mock.patch.dict(os.environ, {"FRESH_AMBIENTE": "homolog"}):
            conta = auth.autenticar(self.conn, "00.000.000/0001-91", PIN)
        self.assertEqual(conta["cnpj"], TESTE)

    def test_sem_variavel_qualquer_cnpj(self):
        env = {k: v for k, v in os.environ.items() if k != "FRESH_AMBIENTE"}
        with mock.patch.dict(os.environ, env, clear=True):
            conta = auth.autenticar(self.conn, OUTRO, PIN)
        self.assertEqual(conta["cnpj"], OUTRO)


if __name__ == "__main__":
    unittest.main()
