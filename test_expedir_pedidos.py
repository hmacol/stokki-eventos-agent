# -*- coding: utf-8 -*-
"""
test_expedir_pedidos.py

Regras puras da expedição na Stokki:
- serviço da VUUPT com códigos combinados ("PS-1, PS-2") vira um item por
  pedido (antes só o 1º era expedido);
- falha persistente volta pra fila uma vez por dia (antes saía pra sempre).
    py -3.11 -m unittest test_expedir_pedidos -v
"""
import unittest
from datetime import datetime, timedelta

import expedir_pedidos as ep


class TestDesmembrarCombinados(unittest.TestCase):
    def test_codigo_simples_fica_igual(self):
        s = {"id": 1, "code": "#PS-37189"}
        self.assertEqual(ep._desmembrar_combinados([s]), [s])

    def test_combinado_vira_um_item_por_pedido(self):
        s = {"id": 7, "code": "PS-37189, PS-37176, #PS-37175-R1", "status": "done"}
        itens = ep._desmembrar_combinados([s])
        self.assertEqual([i["code"] for i in itens], ["PS-37189", "PS-37176", "PS-37175-R1"])
        self.assertTrue(all(i["id"] == 7 and i["status"] == "done" for i in itens))
        self.assertEqual(s["code"], "PS-37189, PS-37176, #PS-37175-R1")   # original intacto


class TestFalhasBloqueadas(unittest.TestCase):
    def test_so_bloqueia_quem_falhou_nas_ultimas_24h(self):
        agora = datetime(2026, 9, 26, 10, 0, 0)
        falhas = {
            "PS-1": {"ultima_em": (agora - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")},
            "PS-2": {"ultima_em": (agora - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")},
            "PS-3": {"ultima_em": None},
        }
        self.assertEqual(set(ep._falhas_bloqueadas(falhas, agora)), {"PS-1"})


if __name__ == "__main__":
    unittest.main()
