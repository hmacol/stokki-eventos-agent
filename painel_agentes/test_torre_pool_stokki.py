# -*- coding: utf-8 -*-
"""
Torre com o pool direto da Stokki (planejamento.fonte_pool = stokki, Task 7): o backlog
("sem rota") vem do nucleo, com o pedido ainda sem servico na Vuupt, e nao le a Vuupt.
Chave desligada: a mesma leitura da Vuupt de sempre. Rodar de dentro de painel_agentes/:
    py -3.11 -m unittest test_torre_pool_stokki
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import torre_controle as tc  # noqa: E402

HOJE = date(2026, 11, 27)
POOL_NUCLEO = [
    {"id": -1, "code": "#PS-1", "title": "#PS-1", "sender_id": 10, "scheduled_start": None},
    {"id": -2, "code": "#PS-2", "title": "#PS-2", "sender_id": 10, "scheduled_start": None},  # no rascunho
    {"id": 900, "code": "#PS-3", "title": "#PS-3", "sender_id": 10, "scheduled_start": "2026-11-26 11:00:00"},
]


class BacklogTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(tc.rascunhos_rota, "listar_rascunhos_do_dia",
                              return_value=[{"paradas": [{"service_id": -2}]}])
        p.start()
        self.addCleanup(p.stop)

    def test_fonte_stokki_le_o_nucleo_com_pedido_sem_servico(self):
        vuupt = mock.Mock()
        with mock.patch("nucleo.pool.listar_pool", return_value=POOL_NUCLEO) as listar:
            b = tc._coletar_backlog(vuupt, HOJE, {"planejamento": {"fonte_pool": "stokki"}})
        listar.assert_called_once_with(incluir_sem_servico=True)
        vuupt.listar_servicos.assert_not_called()
        self.assertEqual(b["pool"], 1)                                   # PS-1 (o PS-2 ja esta em rascunho)
        self.assertEqual([a["codigo"] for a in b["atrasados"]], ["#PS-3"])

    def test_chave_desligada_igual_a_hoje(self):
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = POOL_NUCLEO[2:]
        with mock.patch("nucleo.pool.listar_pool", side_effect=AssertionError("nao le o nucleo")):
            b = tc._coletar_backlog(vuupt, HOJE, {})
            self.assertEqual(tc._coletar_backlog(vuupt, HOJE), b)
        vuupt.listar_servicos.assert_called_with([{"field": "status", "operator": "eq", "value": "not_assigned"}],
                                                 per_page=100)
        self.assertEqual([a["codigo"] for a in b["atrasados"]], ["#PS-3"])


if __name__ == "__main__":
    unittest.main()
