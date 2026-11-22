# -*- coding: utf-8 -*-
"""calcular_km_gps só vale com trilha real (pontos tipo GPS): o PWA no iPhone
manda só a posição de chegada/entrega e não pode virar km em linha reta."""
import sqlite3
import unittest

from nucleo import operacao


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE nucleo_eventos (id INTEGER PRIMARY KEY AUTOINCREMENT, rota_id INTEGER, tipo TEXT,
                 latitude REAL, longitude REAL, precisao_m REAL, ocorrido_em TEXT)""")
    return c


def _ev(c, tipo, lat, lng, em):
    c.execute("INSERT INTO nucleo_eventos (rota_id, tipo, latitude, longitude, precisao_m, ocorrido_em) VALUES (1, ?, ?, ?, 10, ?)",
              (tipo, lat, lng, em))


class KmGpsTrilha(unittest.TestCase):
    def test_so_posicao_de_chegada_e_entrega_nao_vira_km(self):
        c = _conn()
        _ev(c, "CHEGADA", -23.50, -46.60, "2026-11-21 08:00:00")
        _ev(c, "ENTREGUE", -23.50, -46.60, "2026-11-21 08:10:00")
        _ev(c, "CHEGADA", -23.60, -46.70, "2026-11-21 09:00:00")
        _ev(c, "ENTREGUE", -23.60, -46.70, "2026-11-21 09:10:00")
        self.assertIsNone(operacao.calcular_km_gps(c, 1))

    def test_trilha_gps_continua_somando(self):
        c = _conn()
        for i in range(5):
            _ev(c, "GPS", -23.50 - i * 0.01, -46.60, f"2026-11-21 08:0{i}:00")
        _ev(c, "ENTREGUE", -23.55, -46.60, "2026-11-21 08:05:00")
        km = operacao.calcular_km_gps(c, 1)
        self.assertIsNotNone(km)
        self.assertGreater(km, 4.0)


if __name__ == "__main__":
    unittest.main()
