# -*- coding: utf-8 -*-
"""
Desenho do trajeto (polyline) pela Routes API, pro mapa do app do motorista.
Sem rede: _post_com_mascara é mockado.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_km_rodoviario_polyline -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import km_rodoviario

CD = (-23.55, -46.63)


def _codificar_valor(v):
    v = ~(v << 1) if v < 0 else (v << 1)
    saida = ""
    while v >= 0x20:
        saida += chr((0x20 | (v & 0x1F)) + 63)
        v >>= 5
    return saida + chr(v + 63)


def _codificar(pontos):
    saida, ant_lat, ant_lng = "", 0, 0
    for lat, lng in pontos:
        la, ln = round(lat * 1e5), round(lng * 1e5)
        saida += _codificar_valor(la - ant_lat) + _codificar_valor(ln - ant_lng)
        ant_lat, ant_lng = la, ln
    return saida


def _resposta(pontos):
    return {"routes": [{"polyline": {"encodedPolyline": _codificar(pontos)}}]}


class DecodificarPolylineTest(unittest.TestCase):
    def test_decodifica_exemplo_do_google(self):
        r = km_rodoviario.decodificar_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        esperado = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
        self.assertEqual(len(r), 3)
        for (a, b), (c, d) in zip(r, esperado):
            self.assertAlmostEqual(a, c, places=5)
            self.assertAlmostEqual(b, d, places=5)


class TracarGoogleTest(unittest.TestCase):
    def test_uma_chamada_ate_25_intermediarias(self):
        pontos = [CD] + [(-23.5 - i * 0.001, -46.6) for i in range(25)] + [CD]
        self.assertEqual(len(pontos), 27)
        with mock.patch.object(km_rodoviario, "_post_com_mascara",
                               return_value=_resposta([CD, (-23.6, -46.7)])) as m:
            km_rodoviario.tracar_google(pontos, "chave")
        self.assertEqual(m.call_count, 1)
        corpo, _, _, mascara = m.call_args[0]
        self.assertEqual(len(corpo["intermediates"]), 25)
        self.assertNotIn("extraComputations", corpo)
        self.assertEqual(mascara, km_rodoviario.FIELD_MASK_POLYLINE)

    def test_47_paradas_partem_e_nao_repetem_emenda(self):
        pontos = [CD] + [(-23.5 - i * 0.001, -46.6) for i in range(47)] + [CD]
        self.assertEqual(len(pontos), 49)
        emenda = pontos[26]

        def falso(corpo, api_key, timeout, mascara):
            lat = corpo["origin"]["location"]["latLng"]["latitude"]
            if abs(lat - CD[0]) < 1e-9:
                return _resposta([CD, (-23.51, -46.61), emenda])
            return _resposta([emenda, (-23.57, -46.65), CD])

        with mock.patch.object(km_rodoviario, "_post_com_mascara", side_effect=falso) as m:
            r = km_rodoviario.tracar_google(pontos, "chave")
        self.assertEqual(m.call_count, 2)
        self.assertEqual(len(r), 5)
        for a, b in zip(r, r[1:]):
            self.assertNotEqual(a, b)
        self.assertAlmostEqual(r[0][0], CD[0], places=5)
        self.assertAlmostEqual(r[-1][0], CD[0], places=5)

    def test_sem_chave_levanta(self):
        with self.assertRaises(ValueError):
            km_rodoviario.tracar_google([CD, CD], "")

    def test_menos_de_2_pontos_levanta(self):
        with self.assertRaises(ValueError):
            km_rodoviario.tracar_google([CD], "chave")

    def test_resposta_sem_routes_levanta(self):
        with mock.patch.object(km_rodoviario, "_post_com_mascara", return_value={}):
            with self.assertRaises(ValueError):
                km_rodoviario.tracar_google([CD, (-23.6, -46.7)], "chave")

    def test_resposta_sem_polyline_levanta(self):
        with mock.patch.object(km_rodoviario, "_post_com_mascara", return_value={"routes": [{}]}):
            with self.assertRaises(ValueError):
                km_rodoviario.tracar_google([CD, (-23.6, -46.7)], "chave")


if __name__ == "__main__":
    unittest.main()
