# -*- coding: utf-8 -*-
"""
Criacao direta de rota (criar_rotas_diarias sem --gerar-rascunho) com o pool direto da
Stokki (Task 5): pedido de id sintetico ganha o servico na Vuupt antes da rota; quem falha
fica fora com o motivo no log; lista so com id real nem chama a materializacao.

Rodar (da raiz):  py -3.11 -m unittest roteirizacao.test_criar_rotas_sem_servico -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import criar_rotas_diarias as crd  # noqa: E402


class ComServicoTest(unittest.TestCase):
    def test_troca_o_id_e_tira_quem_falhou(self):
        sublote = [{"id": 5, "code": "#PS-5"}, {"id": -1, "code": "#PS-1"}, {"id": -2, "code": "#PS-2"}]
        retorno = ([{"id": 5, "code": "#PS-5"}, {"id": 900, "code": "#PS-1"}], {"PS-2": "pedido PS-2 esta CANCELADO"})
        with mock.patch.object(crd.materializar, "materializar_lote", return_value=retorno) as m, \
                self.assertLogs(crd.logger, "ERROR") as logs:
            saida = crd._com_servico(sublote, "vuupt", {}, "Planejamento - 27/11/2026 - #1")
        m.assert_called_once_with(sublote, "vuupt", {})
        self.assertEqual([s["id"] for s in saida], [5, 900])
        self.assertTrue(any("PS-2" in l and "CANCELADO" in l for l in logs.output))

    def test_so_id_real_nao_chama_a_vuupt(self):
        sublote = [{"id": 5, "code": "#PS-5"}]
        vuupt = mock.Mock()
        self.assertEqual(crd._com_servico(sublote, vuupt, {}, "x"), sublote)
        self.assertEqual(vuupt.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
