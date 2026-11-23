# -*- coding: utf-8 -*-
"""
Trava de trajeto do incremento (16/10): pedido novo que entra em rota
existente (incrementar_rotas.py) ou em rascunho (botão Incrementar /
sobra do incremento) passa pelas mesmas travas de trajeto da montagem
das 16h -- distância até cada parada, km acumulado e jornada de 9h.
Antes só caixas, pedidos, janela e centroide a 15 km eram conferidos.
Rodar (de roteirizacao/): py -3.11 -m unittest test_pedido_cabe_no_trajeto -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import otimizacao_rotas as ot

BASE = (-23.55, -46.63)
KM_POR_GRAU_LAT = 111.19


def _servico(i, km_norte, km_leste=0.0, nivel=1):
    return {"id": i, "_nivel_dificuldade": nivel, "dimension_3": 1,
            "latitude": BASE[0] + km_norte / KM_POR_GRAU_LAT,
            "longitude": BASE[1] + km_leste / (KM_POR_GRAU_LAT * 0.9168)}


def _cabe(servico, rota, **kw):
    params = {"distancia_maxima_km": 15, "km_acumulado_maximo": 60, "base": BASE}
    params.update(kw)
    return ot.pedido_cabe_no_trajeto(servico, rota, None, **params)


class PedidoCabeNoTrajetoTest(unittest.TestCase):
    def test_pedido_perto_cabe(self):
        rota = [_servico(1, 5), _servico(2, 6), _servico(3, 7)]
        self.assertTrue(_cabe(_servico(9, 8), rota))

    def test_pedido_longe_de_uma_parada_nao_cabe(self):
        # centroide da rota a ~10 km do pedido, mas a parada 1 fica a 20 km
        rota = [_servico(1, 0), _servico(2, 14), _servico(3, 15)]
        self.assertFalse(_cabe(_servico(9, 20), rota))

    def test_km_acumulado_estourado_nao_cabe(self):
        rota = [_servico(1, 5), _servico(2, 6)]
        self.assertFalse(_cabe(_servico(9, 14), rota, km_acumulado_maximo=5))

    def test_jornada_acima_de_9h_nao_cabe(self):
        # 7 paradas nível 3 = 8h45 de parada; a 8a passa de 9h
        rota = [_servico(i, 2 + i * 0.1, nivel=3) for i in range(7)]
        self.assertFalse(_cabe(_servico(9, 3, nivel=3), rota))
        self.assertTrue(_cabe(_servico(9, 3, nivel=1), rota[:3]))

    def test_sem_base_so_confere_distancia(self):
        rota = [_servico(i, 2 + i * 0.1, nivel=3) for i in range(7)]
        self.assertTrue(_cabe(_servico(9, 3, nivel=3), rota, base=None))
        self.assertFalse(_cabe(_servico(9, 40), rota, base=None))

    def test_pedido_sem_coordenada_nao_bloqueia_distancia(self):
        rota = [_servico(1, 5)]
        sem_coord = {"id": 9, "_nivel_dificuldade": 1, "dimension_3": 1}
        self.assertTrue(_cabe(sem_coord, rota, base=None))


if __name__ == "__main__":
    unittest.main()
