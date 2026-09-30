# -*- coding: utf-8 -*-
"""obter_coordenadas prefere latitude/longitude embutidas no dict (18/09):
sem isso os agrupadores so enxergam coordenada via geocodificacao do
endereco, e o replay nao roda sem chave do Google.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_coordenadas_embutidas -v"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd


class CoordenadasEmbutidasTestCase(unittest.TestCase):

    def setUp(self):
        rd._cache_coordenadas.clear()

    def test_usa_lat_lng_do_dict_sem_geocodificar(self):
        with mock.patch.object(rd, "geocodificar", side_effect=AssertionError("nao deveria geocodificar")):
            self.assertEqual(rd.obter_coordenadas({"latitude": "-23.5", "longitude": -46.6}, "chave"), (-23.5, -46.6))
            self.assertEqual(rd.obter_coordenadas({"latitude": -23.5, "longitude": -46.6, "address": "Rua X"}, None), (-23.5, -46.6))

    def test_sem_lat_lng_cai_na_geocodificacao(self):
        with mock.patch.object(rd, "geocodificar", return_value=(-1.0, -2.0)) as geo:
            self.assertEqual(rd.obter_coordenadas({"latitude": "", "longitude": None, "address": "Rua X"}, "k"), (-1.0, -2.0))
            geo.assert_called_once_with("Rua X", "k")

    def test_zero_zero_nao_conta_como_coordenada(self):
        with mock.patch.object(rd, "geocodificar", return_value=(-1.0, -2.0)):
            self.assertEqual(rd.obter_coordenadas({"latitude": 0, "longitude": "0", "address": "Rua X"}, "k"), (-1.0, -2.0))

    def test_lat_invalida_cai_na_geocodificacao(self):
        with mock.patch.object(rd, "geocodificar", return_value=(-1.0, -2.0)):
            self.assertEqual(rd.obter_coordenadas({"latitude": "abc", "longitude": "1", "address": "Rua X"}, "k"), (-1.0, -2.0))

    def test_sem_endereco_nem_coordenada(self):
        self.assertIsNone(rd.obter_coordenadas({}, "k"))


if __name__ == "__main__":
    unittest.main()
