# -*- coding: utf-8 -*-
"""
Marcar/remover dedicado pelo Planejamento (Hugo, 23/09/2026).

    py -3.11 -m unittest painel_agentes.test_marcar_dedicados
"""
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pedidos_dedicados as pd  # noqa: E402
from planejamento_rotas import marcar_dedicados, remover_dedicados  # noqa: E402


class MarcarDedicados(unittest.TestCase):
    def test_marca_e_remove(self):
        conn = pd.conectar(":memory:")
        itens = [{"service_id": 1, "codigo": "#PS-1", "sender_id": 5, "remetente_nome": "ACME", "numero_nf": "10"},
                 {"service_id": 2, "codigo": "#PS-2, PS-3", "sender_id": 5, "remetente_nome": "ACME"}]
        r = marcar_dedicados(itens, 100, "hugo", conn=conn)
        self.assertTrue(r["ok"])
        self.assertEqual(r["valores"], {"PS-1": 50.0, "PS-2": 50.0})  # 1o codigo de cada servico
        self.assertEqual(pd.ativos_por_codigo(conn)["PS-1"]["service_id"], 1)
        self.assertEqual(pd.ativos_por_codigo(conn)["PS-1"]["numero_nf"], "10")
        r2 = remover_dedicados(["#PS-1"], "hugo", conn=conn)
        self.assertEqual(r2["removidos"], 1)
        self.assertEqual(list(pd.ativos_por_codigo(conn)), ["PS-2"])

    def test_sem_itens(self):
        with self.assertRaises(ValueError):
            marcar_dedicados([], 10, "x", conn=pd.conectar(":memory:"))

    def test_item_sem_codigo(self):
        with self.assertRaises(ValueError):
            marcar_dedicados([{"service_id": 9, "codigo": ""}], 10, "x", conn=pd.conectar(":memory:"))


if __name__ == "__main__":
    unittest.main()
