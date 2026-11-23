# -*- coding: utf-8 -*-
"""
Reenviar todas as notas com erro de uma vez (Hugo, 02/10/2026).

    py -3.11 -m unittest portal_cliente.test_reenviar_erros
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

import envio_pedidos as ep  # noqa: E402


def _conn(envios):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, atualizado_em TEXT, erro TEXT, bloqueio_motivo TEXT);"
                       "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);")
    conn.executemany("INSERT INTO portal_envios (id, cnpj_embarcador, status, erro, bloqueio_motivo) VALUES (?, ?, ?, ?, ?)", envios)
    conn.commit()
    return conn


def _status(conn):
    return {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM portal_envios")}


class ReenviarErros(unittest.TestCase):
    def test_reenvia_so_os_erros_da_empresa(self):
        conn = _conn([
            (1, "111", ep.STATUS_ERRO, "falhou", None),
            (2, "111", ep.STATUS_ERRO, "falhou", "fora_sp"),
            (3, "111", ep.STATUS_CANCELADO, None, None),
            (4, "111", ep.STATUS_CRIADO, None, None),
            (5, "222", ep.STATUS_ERRO, "falhou", None),
        ])
        r = ep.reenviar_erros(conn, "111", "cliente")
        self.assertEqual(r, {"na_fila": 1, "liberacao": 1})
        self.assertEqual(_status(conn), {1: ep.STATUS_NA_FILA, 2: ep.STATUS_AGUARDANDO_LIBERACAO,
                                         3: ep.STATUS_CANCELADO, 4: ep.STATUS_CRIADO, 5: ep.STATUS_ERRO})
        self.assertIsNone(conn.execute("SELECT erro FROM portal_envios WHERE id = 1").fetchone()[0])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM portal_solicitacoes WHERE tipo = 'reenviar' AND status = 'CONCLUIDA'").fetchone()[0], 2)

    def test_sem_erros_nao_faz_nada(self):
        conn = _conn([(1, "111", ep.STATUS_CRIADO, None, None)])
        self.assertEqual(ep.reenviar_erros(conn, "111", "cliente"), {"na_fila": 0, "liberacao": 0})


if __name__ == "__main__":
    unittest.main()
