# -*- coding: utf-8 -*-
"""Particao por tipo de carga (18/09): desligada por padrao (Seco e
Refrigerado sempre podem ir juntos -- Hugo), religavel por constante; o
tipo de carga vira rotulo derivado do conteudo da rota.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_particao_carga -v"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import criar_rotas_diarias as crd


def _coords(s):
    return (s["latitude"], s["longitude"])


def _servico(i, tipo, dlat=0.0):
    return {"id": i, "code": f"PS-{i}", "address": f"Rua {i}, Sao Paulo - SP, 01000-000, Brasil",
            "_tipo_carga": tipo, "latitude": -23.55 + dlat, "longitude": -46.63, "dimension_3": 1}


class ParticaoTestCase(unittest.TestCase):

    def setUp(self):
        p = mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s))
        p.start()
        self.addCleanup(p.stop)
        self.servicos = [_servico(i, "Seco", 0.001 * i) for i in range(12)] + \
                        [_servico(20 + i, "Refrigerado", 0.001 * i) for i in range(12)]

    def test_padrao_e_particao_unica(self):
        self.assertFalse(crd.SEPARAR_POR_TIPO_CARGA)
        particoes = crd._particionar_carga_com_fusao(self.servicos, 10, None)
        self.assertEqual([label for label, _ in particoes], [crd.PARTICAO_GERAL])
        self.assertEqual(len(particoes[0][1]), 24)

    def test_religar_volta_a_particao_tripla(self):
        with mock.patch.object(crd, "SEPARAR_POR_TIPO_CARGA", True):
            particoes = dict(crd._particionar_carga_com_fusao(self.servicos, 10, None))
        self.assertEqual(len(particoes["Seco"]), 12)
        self.assertEqual(len(particoes["Refrigerado/Congelado"]), 12)

    def test_rotulo_carga(self):
        self.assertEqual(crd.rotulo_carga([_servico(1, "Seco"), _servico(2, "Seco")]), "Seco")
        self.assertEqual(crd.rotulo_carga([_servico(1, "Refrigerado"), _servico(2, "Congelado")]), "Refrigerado/Congelado")
        self.assertEqual(crd.rotulo_carga([_servico(1, "Seco"), _servico(2, "Congelado")]), "Misto (Seco+Refrigerado)")
        self.assertEqual(crd.rotulo_carga([]), "Seco")


if __name__ == "__main__":
    unittest.main()
