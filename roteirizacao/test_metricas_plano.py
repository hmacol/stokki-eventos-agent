# -*- coding: utf-8 -*-
"""Testes de metricas_plano (metricas geometricas de um plano de rotas).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_metricas_plano -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import metricas_plano as mp

BASE = (0.0, 0.0)
GRAU_KM = 111.0


class MetricasTestCase(unittest.TestCase):

    def test_plano_vazio(self):
        m = mp.metricas_plano([], BASE)
        self.assertEqual(m["rotas"], 0)
        self.assertEqual(m["paradas"], 0)
        self.assertEqual(m["km_total"], 0.0)
        self.assertEqual(m["cruzadas_pct"], 0.0)

    def test_km_total_sem_volta(self):
        # 1 rota, 1 parada a 0,1 grau ao norte: so a perna base -> parada
        m = mp.metricas_plano([[(0.1, 0.0)]], BASE)
        self.assertAlmostEqual(m["km_total"], 0.1 * GRAU_KM, delta=0.2)
        self.assertEqual(m["paradas"], 1)
        self.assertEqual(m["rotas_pequenas"], 1)

    def test_duas_rotas_separadas_nao_cruzam(self):
        a = [(0.10, 0.00), (0.11, 0.00), (0.12, 0.00)]
        b = [(0.10, 0.50), (0.11, 0.50), (0.12, 0.50)]
        m = mp.metricas_plano([a, b], BASE)
        self.assertEqual(m["cruzadas"], 0)
        self.assertEqual(m["pares_cruzados"], 0)
        self.assertAlmostEqual(m["diametro_max_km"], 0.02 * GRAU_KM, delta=0.1)
        self.assertEqual(m["media_paradas"], 3.0)

    def test_duas_rotas_entrelacadas_cruzam(self):
        # pontos alternados na mesma reta: a vizinha mais proxima de cada
        # parada esta sempre na OUTRA rota
        a = [(0.10, 0.0), (0.12, 0.0), (0.14, 0.0)]
        b = [(0.11, 0.0), (0.13, 0.0), (0.15, 0.0)]
        m = mp.metricas_plano([a, b], BASE)
        self.assertEqual(m["cruzadas"], 6)
        self.assertEqual(m["cruzadas_pct"], 100.0)
        self.assertEqual(m["pares_cruzados"], 1)

    def test_rotas_acima_do_teto_de_horas(self):
        m = mp.metricas_plano([[(0.1, 0.0)], [(0.2, 0.0)]], BASE, horas=[8.5, 9.4], teto_horas=9.0)
        self.assertEqual(m["rotas_acima_teto"], 1)

    def test_plano_de_sublotes_descarta_sem_coordenada(self):
        sub = [{"latitude": 0.1, "longitude": 0.0}, {"latitude": None, "longitude": None}]
        plano = mp.plano_de_sublotes([sub], lambda s: (s["latitude"], s["longitude"]) if s["latitude"] is not None else None)
        self.assertEqual(plano, [[(0.1, 0.0)]])

    def test_formatar_metricas_uma_linha(self):
        m = mp.metricas_plano([[(0.1, 0.0)]], BASE)
        linha = mp.formatar_metricas(m, "teste")
        self.assertIn("teste", linha)
        self.assertNotIn("\n", linha)

    def test_rota_vazia_descartada_com_contagem(self):
        # Uma rota normal e uma rota vazia: rotas_sem_coordenada deve contar 1
        # Se o filtro fosse removido, este teste falharia (rotas seria 2, nao 1)
        sub_com_parada = [{"latitude": 0.1, "longitude": 0.0}]
        sub_vazio = [{"latitude": None, "longitude": None}]
        plano = mp.plano_de_sublotes([sub_com_parada, sub_vazio], lambda s: (s["latitude"], s["longitude"]) if s["latitude"] is not None else None)
        # plano = [[(0.1, 0.0)], []]
        m = mp.metricas_plano(plano, BASE)
        self.assertEqual(m["rotas"], 1)  # apenas 1 rota sobreviveu
        self.assertEqual(m["rotas_sem_coordenada"], 1)  # 1 rota foi descartada

    def test_horas_alinhadas_com_rotas_filtradas(self):
        # horas deve estar alinhada com a lista ORIGINAL, nao com a lista filtrada
        # Se a segunda rota e descartada, a segunda hora nao deve ser contada
        sub_com_parada = [{"latitude": 0.1, "longitude": 0.0}]
        sub_vazio = [{"latitude": None, "longitude": None}]
        plano = mp.plano_de_sublotes([sub_com_parada, sub_vazio], lambda s: (s["latitude"], s["longitude"]) if s["latitude"] is not None else None)
        # plano = [[(0.1, 0.0)], []]
        # horas originais: [8.5, 9.4] (a segunda e acima do teto)
        m = mp.metricas_plano(plano, BASE, horas=[8.5, 9.4], teto_horas=9.0)
        # Depois do filtro, apenas a primeira hora (8.5) deve ser considerada
        self.assertEqual(m["rotas_acima_teto"], 0)

    def test_formatar_metricas_com_e_sem_rotas_sem_coordenada(self):
        # Com rotas sem coordenada, a formatacao deve trazer o trecho extra
        sub_com_parada = [{"latitude": 0.1, "longitude": 0.0}]
        sub_vazio = [{"latitude": None, "longitude": None}]
        plano = mp.plano_de_sublotes([sub_com_parada, sub_vazio], lambda s: (s["latitude"], s["longitude"]) if s["latitude"] is not None else None)
        m = mp.metricas_plano(plano, BASE)
        linha = mp.formatar_metricas(m, "com_vazio")
        self.assertIn("sem coordenada: 1 rota(s)", linha)
        self.assertNotIn("\n", linha)

        # Sem rotas sem coordenada, a formatacao nao deve trazer o trecho extra
        m2 = mp.metricas_plano([[(0.1, 0.0)]], BASE)
        linha2 = mp.formatar_metricas(m2, "sem_vazio")
        self.assertNotIn("sem coordenada", linha2)
        self.assertNotIn("\n", linha2)


if __name__ == "__main__":
    unittest.main()
