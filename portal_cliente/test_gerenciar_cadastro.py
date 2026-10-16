"""CLI gerenciar_clientes.py cadastro (pre-cadastro automatico, 10/10).

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_gerenciar_cadastro
"""
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_AQUI = Path(__file__).parent
sys.path.insert(0, str(_AQUI.parent))
sys.path.insert(0, str(_AQUI))

import cadastro_embarcador as cad  # noqa: E402
import gerenciar_clientes as gc  # noqa: E402

CNPJ = "49874051000131"


class TestCliCadastro(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        for p in (patch.object(gc.auth, "DB_PATH", self.db), patch.object(gc, "_config", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        conn = gc.auth.conectar()
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "habilidade TEXT, atualizado_em TEXT)")
        conn.execute("INSERT INTO interno VALUES (?, 'NONNA', 'FABENNE', NULL, NULL)", (CNPJ,))
        conn.commit()
        cad.registrar(conn, CNPJ, 100, cad.CONVIDADO)
        cad.salvar_preenchimento(conn, CNPJ, "Refrigerado")
        conn.close()

    def rodar(self, *argv):
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = gc.main(list(argv))
        return codigo, saida.getvalue()

    def habilidade(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT habilidade FROM interno").fetchone()[0]
        finally:
            conn.close()

    def test_listar(self):
        codigo, saida = self.rodar("cadastro")
        self.assertEqual(codigo, 0)
        self.assertIn("49.874.051/0001-31", saida)
        self.assertIn("PREENCHIDO", saida)
        self.assertIn("Refrigerado", saida)

    def test_aprovar(self):
        codigo, saida = self.rodar("cadastro", "aprovar", CNPJ)
        self.assertEqual(codigo, 0, saida)
        self.assertEqual(self.habilidade(), "Refrigerado")
        self.assertEqual(self.rodar("cadastro")[1].strip(), "Nenhum pré-cadastro aberto.")

    def test_corrigir_tipo(self):
        self.assertEqual(self.rodar("cadastro", "habilidade", CNPJ, "seco")[0], 0)
        self.assertEqual(self.habilidade(), "Seco")

    def test_erros(self):
        self.assertEqual(self.rodar("cadastro", "aprovar")[0], 2)
        self.assertEqual(self.rodar("cadastro", "habilidade", CNPJ)[0], 2)
        codigo, saida = self.rodar("cadastro", "habilidade", CNPJ, "quente")
        self.assertEqual(codigo, 2)
        self.assertIn("Nada gravado", saida)
        self.assertIsNone(self.habilidade())


if __name__ == "__main__":
    unittest.main()
