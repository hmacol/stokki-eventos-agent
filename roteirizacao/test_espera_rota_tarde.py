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
import math
import sys
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import selecao_modelo as sm
import rota_tarde as rt
import criar_rotas_diarias as crd

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


class PesoEsperaTest(unittest.TestCase):
    def setUp(self):
        rd.definir_coords_base(*BASE)

    def test_peso_espera_nao_gera_ziguezague(self):
        ordem = rd.ordenar_com_janelas(paradas_rota_13(), *BASE)
        km, anterior = 0.0, BASE
        for p in ordem:
            c = (p["latitude"], p["longitude"])
            km += rd._distancia_km(anterior[0], anterior[1], c[0], c[1]) * rd.FATOR_ESTRADA
            anterior = c
        self.assertLess(km, 50, km)

    def test_espera_tardia_conta_so_janela_tardia(self):
        perto = {"code": "A", "latitude": -23.4935, "longitude": -46.6645,
                 "_janela_inicio": "08:00", "_janela_fim": "18:00", "_janela_fonte": "teste"}
        tarde = {"code": "B", "latitude": -23.4940, "longitude": -46.6650,
                 "_janela_inicio": "15:00", "_janela_fim": "23:59", "_janela_fonte": "teste"}
        self.assertTrue(rd.eh_janela_tardia(tarde))
        self.assertFalse(rd.eh_janela_tardia(perto))
        sim = rd.simular_horarios([perto, tarde], coords_fn=lambda s: (s["latitude"], s["longitude"]),
                                  coords_base=BASE, hora_saida=6.0)
        self.assertGreater(sim["espera_tardia_h"], 0)
        self.assertLess(sim["espera_tardia_h"], sim["espera_h"])
        self.assertAlmostEqual(sim["espera_tardia_h"], 15.0 - sim["chegadas"][1], places=6)

    def test_hora_saida_desloca_simulacao(self):
        p = {"code": "A", "latitude": -23.4935, "longitude": -46.6645}
        sim = rd.simular_horarios([p], coords_fn=lambda s: (s["latitude"], s["longitude"]),
                                  coords_base=BASE, hora_saida=13.0)
        self.assertGreater(sim["chegadas"][0], 13.0)

    def test_start_at_sem_fuso_e_utc(self):
        self.assertEqual(rd.hora_saida_de_start_at("2026-10-23 09:00:00"), 6.0)
        self.assertEqual(rd.hora_saida_de_start_at("2026-10-23T16:15:00"), 13.25)

    def test_start_at_ida_e_volta(self):
        self.assertEqual(rd.start_at_na_hora(date(2026, 10, 23), 13.25), "2026-10-23T16:15:00Z")
        self.assertEqual(rd.hora_saida_de_start_at("2026-10-23T16:15:00Z"), 13.25)
        self.assertEqual(rd.hora_saida_de_start_at("2026-10-23T13:15:00-03:00"), 13.25)
        self.assertIsNone(rd.hora_saida_de_start_at("lixo"))
        self.assertEqual(rd.start_at_rota(date(2026, 10, 23)), "2026-10-23T09:00:00Z")



def _jan(code, lat, lng, ini, fim):
    return {"code": code, "latitude": lat, "longitude": lng, "_nivel_dificuldade": 1,
            "_janela_inicio": ini, "_janela_fim": fim, "_janela_fonte": "teste"}


class TravaEsperaTest(unittest.TestCase):
    def setUp(self):
        rd.definir_coords_base(*BASE)

    def test_trava_reprova_espera_tardia(self):
        # tempo_maximo_horas=24 isola a trava da espera (a #13 tambem estoura o orcamento de 9h)
        self.assertFalse(rd.janela_respeitada(paradas_rota_13(), coords_base=BASE, hora_saida=6.0,
                                           tempo_maximo_horas=24))

    def test_trava_aceita_um_pedido(self):
        wilian = [p for p in paradas_rota_13() if p["code"] == "#PS-42147-R1"]
        self.assertTrue(rd.janela_respeitada(wilian, coords_base=BASE, hora_saida=6.0))

    def test_trava_ignora_espera_da_manha(self):
        a = _jan("A", -23.4935, -46.6645, "08:00", "18:00")
        b = _jan("B", -23.4940, -46.6650, "09:00", "17:00")
        sim = rd.simular_horarios([a, b], coords_base=BASE, hora_saida=6.0)
        self.assertGreater(sim["espera_h"], rd.ESPERA_MAXIMA_ROTA_HORAS)
        self.assertEqual(sim["espera_tardia_h"], 0)
        self.assertTrue(rd.janela_respeitada([a, b], coords_base=BASE, hora_saida=6.0))

    def test_trava_respeita_hora_saida(self):
        bar = _jan("bar", -23.5489, -46.6388, "13:00", "18:00")
        outro = _jan("outro", -23.5579, -46.6388, "13:30", "18:00")
        self.assertTrue(rd.janela_respeitada([bar, outro], coords_base=BASE, hora_saida=12.5))
        self.assertFalse(rd.janela_respeitada([bar, outro], coords_base=BASE))

    def test_trava_desligada_pela_chave(self):
        anterior = rd.ROTA_TARDE_ATIVA
        rd.ROTA_TARDE_ATIVA = False
        try:
            self.assertTrue(rd.janela_respeitada(paradas_rota_13(), coords_base=BASE, hora_saida=6.0,
                                           tempo_maximo_horas=24))
        finally:
            rd.ROTA_TARDE_ATIVA = anterior


def _floor4(h):
    return math.floor(h * 4 + 1e-9) / 4


def _perna_da_base(s):
    return rd._tempo_perna_horas(rd._distancia_km(BASE[0], BASE[1], s["latitude"], s["longitude"]))


def _wilian():
    return next(p for p in paradas_rota_13() if p["code"] == "#PS-42147-R1")


class RotaTardeTest(unittest.TestCase):
    def setUp(self):
        rd.definir_coords_base(*BASE)

    def test_13_wilian_vai_pra_tarde(self):
        manha, tardios = rt.separar_pedidos_tarde([paradas_rota_13()], BASE, None)
        self.assertEqual([s["code"] for s in tardios], ["#PS-42147-R1"])
        self.assertEqual(len(manha), 1)
        self.assertEqual(len(manha[0]), 13)
        self.assertTrue(rd.janela_respeitada(manha[0], coords_base=BASE))

    def test_rota_de_um_tardio_vai_pra_tarde(self):
        manha, tardios = rt.separar_pedidos_tarde([[_wilian()]], BASE, None)
        self.assertEqual(manha, [])
        self.assertEqual([s["code"] for s in tardios], ["#PS-42147-R1"])

    def test_chave_desligada_nao_separa(self):
        sublotes = [paradas_rota_13(), [_wilian()]]
        with mock.patch.object(rd, "ROTA_TARDE_ATIVA", False):
            manha, tardios = rt.separar_pedidos_tarde(sublotes, BASE, None)
        self.assertIs(manha, sublotes)
        self.assertEqual(tardios, [])

    def test_hora_saida_tarde(self):
        w = _wilian()
        saida = rt.hora_saida_tarde([w], BASE, None)
        self.assertEqual(saida, _floor4(15.0 - _perna_da_base(w)))
        self.assertEqual(saida * 4, int(saida * 4))

    def test_hora_saida_tem_piso(self):
        # Janela 12:00 a ~220 km: a ida (~6,8 h) passa das 6h de folga.
        # Com ~80 km do brief a ida e ~3,7 h e a saida seria 08:15 (sem piso).
        longe = _jan("longe", BASE[0] - 2.0, BASE[1], "12:00", "18:00")
        self.assertLess(12.0 - _perna_da_base(longe), rd.HORA_SAIDA_BASE)
        self.assertEqual(rt.hora_saida_tarde([longe], BASE, None), 6.0)

    def test_tarde_mesmo_endereco_seguidos(self):
        a = _jan("A", -23.5489, -46.6388, "13:00", "18:00")
        outro = _jan("O", -23.5579, -46.6388, "13:30", "18:00")
        a2 = _jan("A2", -23.5489, -46.6388, "13:00", "18:00")
        rotas = rt.montar_rotas_tarde([a, outro, a2], BASE, None, tamanho_maximo=15, volume_maximo=100)
        com_a = [sub for sub, _ in rotas if any(s["code"] == "A" for s in sub)]
        self.assertEqual(len(com_a), 1)
        codes = [s["code"] for s in com_a[0]]
        self.assertIn("A2", codes)
        self.assertEqual(abs(codes.index("A") - codes.index("A2")), 1, codes)

    def test_tarde_bar_e_talita_quebra(self):
        bar = _jan("bar", -23.5489, -46.6388, "13:00", "18:00")
        talita = _jan("talita", -23.5600, -46.7000, "18:30", "19:30")
        rotas = rt.montar_rotas_tarde([bar, talita], BASE, None, tamanho_maximo=15, volume_maximo=100)
        self.assertEqual(sorted([s["code"] for s in sub] for sub, _ in rotas), [["bar"], ["talita"]])
        saidas = {sub[0]["code"]: h for sub, h in rotas}
        self.assertEqual(saidas["bar"], rt.hora_saida_tarde([bar], BASE, None))
        self.assertEqual(saidas["talita"], rt.hora_saida_tarde([talita], BASE, None))
        self.assertGreater(saidas["talita"], saidas["bar"])

    def test_tarde_respeita_tetos(self):
        pedidos = [_jan(f"P{i}", -23.5489 + 0.001 * i, -46.6388, "13:00", "18:00") for i in range(4)]
        rotas = rt.montar_rotas_tarde(pedidos, BASE, None, tamanho_maximo=3, volume_maximo=100)
        self.assertEqual(sorted(len(sub) for sub, _ in rotas), [1, 3])
        for p in pedidos:
            p["dimension_3"] = 40
        rotas = rt.montar_rotas_tarde(pedidos, BASE, None, tamanho_maximo=15, volume_maximo=100)
        self.assertTrue(all(sum(s["dimension_3"] for s in sub) <= 100 for sub, _ in rotas))
        self.assertEqual(sum(len(sub) for sub, _ in rotas), 4)

    def test_tardio_sem_coordenada_fica(self):
        manha_a = _jan("A", -23.4935, -46.6645, "08:00", "18:00")
        sem = {"code": "S", "_nivel_dificuldade": 1, "_janela_inicio": "15:00",
               "_janela_fim": "23:59", "_janela_fonte": "teste"}
        sub = [manha_a, sem]
        self.assertFalse(rd.janela_respeitada(sub, coords_base=BASE))
        manha, tardios = rt.separar_pedidos_tarde([sub, [dict(sem)]], BASE, None)
        self.assertEqual(tardios, [])
        self.assertEqual([[s["code"] for s in x] for x in manha], [["A", "S"], ["S"]])

    def test_tardio_nivel4_e_viagem_ficam(self):
        manha_a = _jan("A", -23.4935, -46.6645, "08:00", "18:00")
        w4 = dict(_wilian(), _nivel_dificuldade=4)
        longe = _jan("L", BASE[0] - 3.0, BASE[1], "08:00", "23:00")
        inviavel = [dict(manha_a), _wilian(), longe]
        self.assertFalse(rd.exige_orcamento_horas(inviavel, None, BASE))
        # Litoral (~50 km, macro VIAGEM): cabe no orcamento, a trava reprova, mas nao sai
        viagem = [_jan("V1", -23.950, -46.660, "08:00", "18:00"),
                  _jan("V2", -23.951, -46.661, "15:00", "23:59")]
        self.assertFalse(rd.janela_respeitada(viagem, coords_base=BASE))
        sublotes = [[manha_a, w4], [dict(w4)], inviavel, viagem, [dict(viagem[1])]]
        manha, tardios = rt.separar_pedidos_tarde(sublotes, BASE, None)
        self.assertEqual(tardios, [])
        self.assertEqual([len(x) for x in manha], [2, 1, 3, 2, 1])

    def test_dia_sem_tardio_sem_tarde(self):
        self.assertEqual(rt.montar_rotas_tarde([], BASE, None, tamanho_maximo=15, volume_maximo=100), [])
        sub = [p for p in paradas_rota_13() if p["code"] != "#PS-42147-R1"]
        manha, tardios = rt.separar_pedidos_tarde([sub], BASE, None)
        self.assertEqual(tardios, [])
        self.assertEqual(len(manha[0]), 13)


class PlanejarSublotesTardeTest(unittest.TestCase):
    def setUp(self):
        rd.definir_coords_base(*BASE)

    def test_planejar_sublotes_13_ponta_a_ponta(self):
        servicos = [dict(p, id=i, _tipo_carga="Seco") for i, p in enumerate(paradas_rota_13())]
        with mock.patch.object(sm, "_registrar_historico"):
            planos = crd.planejar_sublotes(servicos, BASE, None, date(2026, 10, 23),
                                           registrar_historico=False)
        manha = [sub for p in planos for sub in p["sublotes"]]
        tarde = [sub for p in planos for sub, _ in p["tarde"]]
        ids = Counter(s["id"] for sub in manha + tarde for s in sub)
        self.assertEqual(ids, Counter(s["id"] for s in servicos))
        self.assertIn("#PS-42147-R1", [s["code"] for sub in tarde for s in sub])
        for sub in manha:
            self.assertTrue(rd.janela_respeitada(sub, coords_base=BASE), [s["code"] for s in sub])


if __name__ == "__main__":
    unittest.main()
