# -*- coding: utf-8 -*-
"""
Sequencia livre (Hugo, 18/09): semente vizinho mais proximo saindo da
base, objetivo = km ate a ULTIMA parada (sem volta a base), 2-opt e
or-opt podem mover qualquer posicao, inclusive a primeira. Revoga a
regra "mais longe primeiro" de 03/08.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_sequencia_livre -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import otimizacao_rotas as ot

BASE = (0.0, 0.0)


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, lat, lng, janela=None, nivel=1):
    s = {"id": i, "code": f"PS-{i}", "address": f"P{i}", "_nivel_dificuldade": nivel,
         "latitude": lat, "longitude": lng, "dimension_3": 1}
    if janela:
        s["_janela_inicio"], s["_janela_fim"], s["_janela_fonte"] = janela[0], janela[1], "teste"
    return s


def _km_sem_volta(seq):
    pts = [BASE] + [(s["latitude"], s["longitude"]) for s in seq]
    return sum(rd._distancia_km(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))


class SequenciaLivreTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        rd.COORDS_BASE = BASE

    def test_semente_vizinho_mais_proximo_numa_reta(self):
        rota = [_servico(1, 0.05, 0.0), _servico(2, 0.30, 0.0), _servico(3, 0.10, 0.0),
                _servico(4, 0.20, 0.0), _servico(5, 0.02, 0.0)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertEqual([s["id"] for s in ordem], [5, 1, 3, 4, 2])
        # sem volta: km = distancia ate a mais longe (0,30 grau ~ 33 km)
        self.assertAlmostEqual(_km_sem_volta(ordem), rd._distancia_km(*BASE, 0.30, 0.0), places=6)

    def test_objetivo_sem_volta_termina_longe(self):
        # A e B coladas a ~11 km; C a ~33 km. Sem volta, o melhor e
        # A -> B -> C (termina longe); com a volta ficticia era empate.
        rota = [_servico(1, 0.30, 0.0), _servico(2, 0.10, 0.0), _servico(3, 0.10, 0.01)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertEqual(ordem[-1]["id"], 1)
        self.assertAlmostEqual(_km_sem_volta(ordem), rd._distancia_km(*BASE, 0.10, 0.0)
                               + rd._distancia_km(0.10, 0.0, 0.10, 0.01)
                               + rd._distancia_km(0.10, 0.01, 0.30, 0.0), places=6)

    def test_primeira_parada_pode_mudar(self):
        # semente NN: P1 (mais perto), depois P2 ou P3, depois a outra
        # (~43 km). Otimo: P2 -> P1 -> P3 ou P3 -> P1 -> P2 (~37 km) --
        # exige mover a primeira parada, proibido ate 17/09.
        rota = [_servico(1, 0.10, 0.0), _servico(2, 0.11, -0.10), _servico(3, 0.11, 0.10)]
        semente = [rota[0], rota[1], rota[2]]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertEqual(ordem[1]["id"], 1)
        self.assertNotEqual(ordem[0]["id"], 1)
        self.assertLess(_km_sem_volta(ordem), _km_sem_volta(semente) - 4.0)

    def test_nunca_pior_que_a_semente_nn(self):
        rota = [_servico(i, 0.01 * ((i * 7) % 11), 0.01 * ((i * 3) % 13)) for i in range(1, 13)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        semente = rd._ordem_vizinho_mais_proximo(rota, BASE, _coords)
        self.assertLessEqual(_km_sem_volta(ordem), _km_sem_volta(semente) + 1e-9)
        self.assertEqual({s["id"] for s in ordem}, {s["id"] for s in rota})

    def test_sem_coordenada_vai_pro_final(self):
        rota = [_servico(1, 0.10, 0.0), _servico(2, None, None), _servico(3, 0.05, 0.0)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertEqual([s["id"] for s in ordem], [3, 1, 2])

    def test_janela_continua_valendo_com_primeira_parada_livre(self):
        # a mais PERTO fecha as 11h e a mais longe abre so as 14h: a
        # sequencia livre respeita as duas sem esperar parado
        with mock.patch.object(rd, "HORA_SAIDA_BASE", 10.0):
            rota = [_servico(1, 0.20, 0.0, ("14:00", "17:00")), _servico(2, 0.15, 0.02),
                    _servico(3, 0.10, 0.0), _servico(4, 0.03, 0.0, ("08:00", "11:00"))]
            ordem = ot.ordenar_2opt(rota, *BASE)
            sim = rd.simular_horarios(ordem, coords_base=BASE)
            self.assertLessEqual(sim["atraso_h"], rd.TOLERANCIA_JANELA_HORAS)
            self.assertEqual(ordem[0]["id"], 4)
            self.assertEqual(ordem[-1]["id"], 1)

    def test_calcular_km_estimado_sem_volta(self):
        rota = [_servico(1, 0.10, 0.0)]
        km = rd.calcular_km_estimado(rota, *BASE, None)
        self.assertAlmostEqual(km, rd._distancia_km(*BASE, 0.10, 0.0), places=6)


if __name__ == "__main__":
    unittest.main()
