# -*- coding: utf-8 -*-
"""
test_espera_rota_tarde.py

Testes da espera por janela e da rota da tarde (spec 2026-10-23): mesmo
endereco seguido na sequencia, peso da espera, trava de espera e rota
da tarde. A fixture e a rota #13 de 23/10 (14 paradas, spec secao 9).

Nenhum teste geocodifica nada: os dicts carregam latitude/longitude.
Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_espera_rota_tarde -v
"""
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd

BASE = (-23.4930, -46.6640)

# (code, lat, lng, nivel, janela inicio, janela fim)
_ROTA_13 = [
    ("#PS-42344", -23.57999, -46.58311, 1, None, None),
    ("#PS-42361", -23.58870, -46.60809, 1, None, None),
    ("#PS-42342", -23.59994, -46.64014, 1, "08:00", "18:00"),
    ("#PS-42296", -23.59355, -46.63622, 3, "08:00", "16:00"),
    ("#PS-42191-R1", -23.59608, -46.66801, 2, "08:00", "17:00"),
    ("#PS-42301", -23.60070, -46.66681, 2, "07:00", "15:00"),
    ("#PS-42216-R1", -23.59992, -46.61204, 1, "08:00", "18:00"),
    ("#PS-42348", -23.59200, -46.62702, 1, None, None),
    ("#PS-42216", -23.59992, -46.61204, 1, "08:00", "18:00"),
    ("#PS-42334", -23.60258, -46.67050, 1, None, None),
    ("#PS-42331", -23.60158, -46.67061, 1, None, None),
    ("#PS-42357", -23.60472, -46.66137, 2, None, None),
    ("#PS-42147-R1", -23.58734, -46.64417, 2, "15:00", "23:59"),
    ("#PS-42337", -23.57573, -46.64508, 1, None, None),
]


def paradas_rota_13() -> list[dict]:
    """Dicts novos das 14 paradas da #13 (cada chamada devolve copias)."""
    paradas = []
    for code, lat, lng, nivel, ini, fim in _ROTA_13:
        p = {"code": code, "latitude": lat, "longitude": lng, "_nivel_dificuldade": nivel}
        if ini:
            p["_janela_inicio"], p["_janela_fim"], p["_janela_fonte"] = ini, fim, "teste"
        paradas.append(p)
    return paradas


def _p(code, lat, lng):
    return {"code": code, "latitude": lat, "longitude": lng}


class MesmoEnderecoSeguidoTest(unittest.TestCase):
    def setUp(self):
        rd.definir_coords_base(*BASE)

    def test_mesmo_endereco_fica_seguido(self):
        ordem = rd.ordenar_com_janelas(paradas_rota_13(), *BASE)
        codes = [p["code"] for p in ordem]
        self.assertEqual(
            abs(codes.index("#PS-42216-R1") - codes.index("#PS-42216")), 1, codes)

    def test_juntar_mesmo_endereco_mantem_resto(self):
        a, b = _p("A", -23.5, -46.6), _p("B", -23.6, -46.7)
        a2, c = _p("A2", -23.5, -46.6), _p("C", -23.7, -46.8)
        resolver = lambda s: (s["latitude"], s["longitude"])
        self.assertEqual(rd._juntar_mesmo_endereco([a, b, a2, c], resolver), [a, a2, b, c])

    def test_parada_sem_coordenada_nao_se_move(self):
        a, b = _p("A", -23.5, -46.6), _p("B", -23.6, -46.7)
        sem, a2 = {"code": "S"}, _p("A2", -23.5, -46.6)
        resolver = lambda s: (s["latitude"], s["longitude"]) if "latitude" in s else None
        self.assertEqual(rd._juntar_mesmo_endereco([a, b, sem, a2], resolver), [a, a2, sem, b])


if __name__ == "__main__":
    unittest.main()
