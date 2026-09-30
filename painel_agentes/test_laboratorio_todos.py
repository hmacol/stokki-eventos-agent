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


if __name__ == "__main__":
    unittest.main()
