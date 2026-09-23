# -*- coding: utf-8 -*-
"""
Worker do portal grava o PS do envio e vincula a marca de dedicado
(enviar_stokki._gravar_codigo) -- Hugo, 23/09/2026.

    py -3.11 -m unittest portal_cliente.test_gravar_codigo_dedicado
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import enviar_stokki as worker  # noqa: E402


def _conn(com_dedicados: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, numero_nf TEXT, status TEXT, codigo_pedido TEXT, atualizado_em TEXT);
        INSERT INTO portal_envios (id, numero_nf, status) VALUES (7, '500', 'ENVIANDO');""")
    if com_dedicados:
        conn.executescript("CREATE TABLE pedidos_dedicados (id INTEGER PRIMARY KEY, envio_id INTEGER, codigo_pedido TEXT, removido_em TEXT);"
                           "INSERT INTO pedidos_dedicados VALUES (1, 7, NULL, NULL);")
    return conn


class GravarCodigo(unittest.TestCase):
    def test_vincula_dedicado_quando_a_stokki_devolve_o_codigo_na_criacao(self):
        """Revisao 23/09: liberado como dedicado antes de ter PS; a Stokki devolve
        o codigo na criacao (nao passa pela reconciliacao)."""
        conn = _conn()
        worker._gravar_codigo(conn, 7, "PS-40001", status="CRIADO")
        conn.commit()
        r = conn.execute("SELECT status, codigo_pedido FROM portal_envios WHERE id = 7").fetchone()
        self.assertEqual((r["status"], r["codigo_pedido"]), ("CRIADO", "PS-40001"))
        self.assertEqual(conn.execute("SELECT codigo_pedido FROM pedidos_dedicados WHERE id = 1").fetchone()[0], "PS-40001")

    def test_sem_codigo_so_marca(self):
        conn = _conn()
        worker._gravar_codigo(conn, 7, None, status="CRIADO")
        self.assertIsNone(conn.execute("SELECT codigo_pedido FROM pedidos_dedicados WHERE id = 1").fetchone()[0])

    def test_sem_tabela_dedicados_nao_quebra(self):
        conn = _conn(com_dedicados=False)
        worker._gravar_codigo(conn, 7, "PS-1")
        self.assertEqual(conn.execute("SELECT codigo_pedido FROM portal_envios WHERE id = 7").fetchone()[0], "PS-1")


if __name__ == "__main__":
    unittest.main()
