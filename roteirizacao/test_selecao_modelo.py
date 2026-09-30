# -*- coding: utf-8 -*-
"""Criterio de selecao diaria (18/09): menor km total vence, menos rotas
so desempata (antes era o contrario). E `registrar_historico=False`
(replay) nao escreve no historico.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_selecao_modelo -v"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import otimizacao_rotas as ot
import selecao_modelo as sm

BASE = (-23.55, -46.63)  # perto do centro de SP: tudo cai em GRANDE_SP


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, dlat, dlng, caixas=1):
    return {"id": i, "code": f"PS-{i}", "address": f"Rua {i}, Sao Paulo - SP, 01000-000, Brasil",
            "_nivel_dificuldade": 1, "latitude": BASE[0] + dlat, "longitude": BASE[1] + dlng,
            "dimension_3": caixas, "sender_id": 1}


class CriterioTestCase(unittest.TestCase):

    def test_menor_km_vence_mesmo_com_rota_a_mais(self):
        avaliacoes = {"A": {"rotas": 3, "km": 100.0}, "B": {"rotas": 4, "km": 90.0}}
        self.assertEqual(sm._escolher_vencedor(avaliacoes), "B")

    def test_empate_em_km_arredondado_desempata_por_rotas(self):
        avaliacoes = {"A": {"rotas": 4, "km": 100.04}, "B": {"rotas": 3, "km": 100.0}, "C": {"rotas": 5, "km": 99.96}}
        self.assertEqual(sm._escolher_vencedor(avaliacoes), "B")

    def test_empate_total_fica_com_a_ordem_de_insercao(self):
        avaliacoes = {"Atual (Grade+Greedy)": {"rotas": 2, "km": 50.0}, "Sweep Polar": {"rotas": 2, "km": 50.0}}
        self.assertEqual(sm._escolher_vencedor(avaliacoes), "Atual (Grade+Greedy)")


class HistoricoTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        rd.COORDS_BASE = BASE
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.historico = Path(self.tmp.name) / "hist.txt"

    def _servicos(self):
        # dois aglomerados a ~11 km um do outro, 6 pedidos cada
        return ([_servico(i, 0.01 + 0.001 * i, 0.0) for i in range(6)]
                + [_servico(10 + i, 0.01 + 0.001 * i, 0.10) for i in range(6)])

    def test_registrar_historico_false_nao_escreve(self):
        with mock.patch.object(sm, "ARQUIVO_HISTORICO", self.historico):
            vencedor, sublotes = sm.escolher_melhor_modelo(
                self._servicos(), *BASE, None, data_alvo=date(2026, 9, 18), label="teste",
                tamanho_minimo=1, tamanho_maximo=16, volume_maximo=100, distancia_maxima_km=20,
                registrar_historico=False,
            )
        self.assertFalse(self.historico.exists())
        self.assertIn(vencedor, ("Atual (Grade+Greedy)", "Sweep Polar", "Clarke-Wright", "CEP real", "K-means geográfico"))
        self.assertEqual(sorted(s["id"] for sub in sublotes for s in sub), sorted(s["id"] for s in self._servicos()))

    def test_registrar_historico_padrao_escreve(self):
        with mock.patch.object(sm, "ARQUIVO_HISTORICO", self.historico):
            sm.escolher_melhor_modelo(
                self._servicos(), *BASE, None, data_alvo=date(2026, 9, 18), label="teste",
                tamanho_minimo=1, tamanho_maximo=16, volume_maximo=100, distancia_maxima_km=20,
            )
        self.assertTrue(self.historico.exists())
        self.assertIn("vencedor:", self.historico.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
