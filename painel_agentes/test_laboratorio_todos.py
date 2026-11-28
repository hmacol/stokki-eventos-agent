# -*- coding: utf-8 -*-
"""Laboratorio de roteirizacao: particao "Todos" (18/09) roda os esquemas
sobre Seco + Refrigerado juntos, como a producao passou a fazer.
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_laboratorio_todos -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import laboratorio_rotas as lab


class FiltroParticaoTestCase(unittest.TestCase):

    def setUp(self):
        self.servicos = [{"id": 1, "_tipo_carga": "Seco"}, {"id": 2, "_tipo_carga": "Refrigerado"},
                         {"id": 3, "_tipo_carga": "Congelado"}]

    def test_todos_e_o_padrao_e_nao_filtra(self):
        self.assertEqual(lab.PARTICOES_VALIDAS[0], "Todos")
        self.assertEqual([s["id"] for s in lab.filtrar_particao(self.servicos, "Todos")], [1, 2, 3])

    def test_seco_e_frio_continuam_disponiveis(self):
        self.assertEqual([s["id"] for s in lab.filtrar_particao(self.servicos, "Seco")], [1])
        self.assertEqual([s["id"] for s in lab.filtrar_particao(self.servicos, "Refrigerado/Congelado")], [2, 3])

    def test_particao_invalida(self):
        with self.assertRaises(ValueError):
            lab.filtrar_particao(self.servicos, "Misto")


class PoolDiretoDaStokkiTestCase(unittest.TestCase):
    """Fonte stokki (Task 7): a simulacao le o pool do nucleo, com o pedido ainda sem servico."""

    def test_fonte_stokki_le_o_nucleo(self):
        from unittest import mock
        with mock.patch("nucleo.pool.listar_pool", return_value=[{"id": -1}]) as listar,                 mock.patch.object(lab, "VuuptClient", side_effect=AssertionError("Vuupt")):
            self.assertEqual(lab._pool_vivo({"planejamento": {"fonte_pool": "stokki"}}), [{"id": -1}])
        listar.assert_called_once_with(incluir_sem_servico=True)

    def test_chave_desligada_igual_a_hoje(self):
        from unittest import mock
        with mock.patch.object(lab, "VuuptClient") as vc:
            vc.return_value.listar_servicos.return_value = [{"id": 5}]
            self.assertEqual(lab._pool_vivo({"vuupt_api": {"token": "t"}}), [{"id": 5}])
        vc.assert_called_once_with("t")
        vc.return_value.listar_servicos.assert_called_once_with(
            [{"field": "status", "operator": "eq", "value": "not_assigned"}], per_page=100, include=["customer"])


if __name__ == "__main__":
    unittest.main()
