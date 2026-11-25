# -*- coding: utf-8 -*-
"""
Selo "Nao carregado" no pool do planejamento: anotar_nao_carregados.

    py -3.11 -m unittest test_pool_nao_carregado   (de dentro de painel_agentes/)
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas as pr  # noqa: E402


def _conn(com_tabela=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if com_tabela:
        conn.execute("CREATE TABLE nucleo_carregamento_itens (id INTEGER PRIMARY KEY, rota_id INTEGER, "
                     "service_id INTEGER, codigo TEXT, motivo TEXT, observacao TEXT, removido_em TEXT, "
                     "criado_em TEXT DEFAULT (datetime('now','localtime')))")
        conn.execute("INSERT INTO nucleo_carregamento_itens (rota_id, service_id, codigo, motivo, observacao, "
                     "removido_em) VALUES (1, 7, 'PS-7', 'Não coube', 'sem espaço', datetime('now','localtime'))")
    return conn


class TestAnotarNaoCarregados(unittest.TestCase):
    def test_anota_so_quem_saiu(self):
        pool = [{"service_id": 7}, {"service_id": 8}]
        pr.anotar_nao_carregados(pool, _conn())
        self.assertEqual(pool[0]["nao_carregado"]["motivo"], "Não coube")
        self.assertEqual(pool[0]["nao_carregado"]["observacao"], "sem espaço")
        self.assertIsNone(pool[1]["nao_carregado"])

    def test_banco_antigo_nao_quebra(self):
        pool = [{"service_id": 7}]
        pr.anotar_nao_carregados(pool, _conn(com_tabela=False))
        self.assertIsNone(pool[0]["nao_carregado"])


if __name__ == "__main__":
    unittest.main()
