# -*- coding: utf-8 -*-
"""
Custo pre-calculado do sequenciador com janela (16/10): o replay de
16/10 gastava 213 de 229s em simular_horarios chamado pela busca local
de ordenar_com_janelas (janela relida por regex e distancia recalculada
a cada candidata). _custo_janela_precalculado tem que devolver EXATAMENTE
o mesmo numero que km + pesos x simular_horarios -- a ordem escolhida
nao pode mudar, so o tempo.
Rodar (de roteirizacao/): py -3.11 -m unittest test_custo_janela_rapido -v
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd

BASE = (-23.55, -46.63)


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servicos(rng, n):
    saida = []
    for i in range(n):
        s = {"id": i, "_nivel_dificuldade": rng.choice([1, 2, 3]), "dimension_3": 1,
             "latitude": BASE[0] + rng.uniform(-0.3, 0.3), "longitude": BASE[1] + rng.uniform(-0.3, 0.3)}
        if rng.random() < 0.15:
            s["latitude"] = s["longitude"] = None
        if rng.random() < 0.5:
            ini = rng.randint(6, 14)
            s["_janela_inicio"], s["_janela_fim"] = f"{ini:02d}:00", f"{ini + rng.randint(1, 4):02d}:30"
        saida.append(s)
    return saida


def _custo_referencia(seq):
    """O _custo de antes: km sem volta + pesos x simular_horarios."""
    km, anterior = 0.0, BASE
    for s in seq:
        c = _coords(s)
        if c is None:
            continue
        km += rd._distancia_km(anterior[0], anterior[1], c[0], c[1])
        anterior = c
    sim = rd.simular_horarios(seq, None, BASE, coords_fn=_coords)
    return km + rd.PESO_ATRASO_JANELA_KM * sim["atraso_h"] + rd.PESO_ESPERA_JANELA_KM * sim["espera_h"]


class CustoJanelaRapidoTest(unittest.TestCase):
    def test_custo_identico_ao_simulador(self):
        rng = random.Random(16)
        for _ in range(200):
            servicos = _servicos(rng, rng.randint(1, 15))
            custo = rd._custo_janela_precalculado(servicos, BASE, _coords)
            for _ in range(5):
                rota = list(range(len(servicos)))
                rng.shuffle(rota)
                self.assertEqual(custo(rota), _custo_referencia([servicos[k] for k in rota]))

    def test_respeita_hora_de_saida_da_base(self):
        servicos = _servicos(random.Random(3), 8)
        anterior = rd.HORA_SAIDA_BASE
        try:
            rd.definir_hora_saida_base(9.5)
            custo = rd._custo_janela_precalculado(servicos, BASE, _coords)
            rota = list(range(len(servicos)))
            self.assertEqual(custo(rota), _custo_referencia(servicos))
        finally:
            rd.definir_hora_saida_base(anterior)


if __name__ == "__main__":
    unittest.main()
