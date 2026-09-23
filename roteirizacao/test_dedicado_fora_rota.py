# -*- coding: utf-8 -*-
"""
Pedido dedicado fica fora da rota compartilhada (Hugo, 23/09/2026).

    py -3.11 -m unittest roteirizacao.test_dedicado_fora_rota
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dedicados  # noqa: E402


class CarregarDedicados(unittest.TestCase):
    def test_falha_no_banco_devolve_vazio(self):
        with mock.patch.object(dedicados.pedidos_dedicados, "conectar", side_effect=RuntimeError("db")):
            with self.assertLogs(dedicados.logger, level="WARNING"):
                self.assertEqual(dedicados.carregar_dedicados(), {})

    def test_le_ativos_do_banco(self):
        conn = dedicados.pedidos_dedicados.conectar(":memory:")
        dedicados.pedidos_dedicados.marcar(conn, [{"codigo_pedido": "#PS-9"}], 10, "t")
        with mock.patch.object(dedicados.pedidos_dedicados, "conectar", return_value=conn):
            self.assertEqual(list(dedicados.carregar_dedicados()), ["PS-9"])


class Separar(unittest.TestCase):
    def test_separar(self):
        ativos = {"PS-2": {"valor": 10.0}}
        with mock.patch.object(dedicados, "carregar_dedicados", return_value=ativos):
            restantes, fora = dedicados.separar_dedicados([{"id": 1, "code": "#PS-1"}, {"id": 2, "code": "#PS-2, PS-3"}])
        self.assertEqual([s["id"] for s in restantes], [1])
        self.assertEqual([s["id"] for s in fora], [2])

    def test_sem_dedicados_devolve_tudo(self):
        with mock.patch.object(dedicados, "carregar_dedicados", return_value={}):
            restantes, fora = dedicados.separar_dedicados([{"id": 1, "code": "#PS-1"}])
        self.assertEqual(len(restantes), 1)
        self.assertEqual(fora, [])


class DedicadosPorServico(unittest.TestCase):
    def test_mapa_pro_pool(self):
        ativos = {"PS-1": {"valor": 33.34, "valor_total_grupo": 100.0, "grupo_id": "g"},
                  "PS-2": {"valor": 33.33, "valor_total_grupo": 100.0, "grupo_id": "g"},
                  "PS-3": {"valor": 33.33, "valor_total_grupo": 100.0, "grupo_id": "g"}}
        with mock.patch.object(dedicados, "carregar_dedicados", return_value=ativos):
            m = dedicados.dedicados_por_servico([{"id": 10, "code": "#PS-1"}, {"id": 11, "code": "#PS-5"}])
        self.assertEqual(m, {10: {"valor": 33.34, "valor_total": 100.0, "n_grupo": 3}})


if __name__ == "__main__":
    unittest.main()
