# -*- coding: utf-8 -*-
"""replay_rotas: leitura de rascunhos ENVIADOS -> dicts de servico no
formato que o pipeline aceita.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_replay_rotas -v"""
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import replay_rotas as rr


def _banco():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, data_alvo TEXT, status TEXT, criado_em TEXT);
        CREATE TABLE rascunhos_parada (id INTEGER PRIMARY KEY, rascunho_id INTEGER, ordem INTEGER,
            service_id INTEGER, codigo TEXT, titulo TEXT, endereco TEXT, latitude REAL, longitude REAL,
            sender_id INTEGER, destinatario_nome TEXT, nivel_dificuldade INTEGER, volume_caixas INTEGER,
            janela_inicio TEXT, janela_fim TEXT, janela_fonte TEXT);
        INSERT INTO rascunhos_rota VALUES (1, '2026-09-17', 'ENVIADO', '2026-09-16 22:00'),
                                          (2, '2026-09-17', 'ENVIADO', '2026-09-16 22:00'),
                                          (3, '2026-09-17', 'DESCARTADO', '2026-09-16 21:00');
        INSERT INTO rascunhos_parada VALUES
            (1, 1, 1, 100, '#PS-100', 'A', 'Rua A, Sao Paulo - SP, 01000-000, Brasil', -23.50, -46.60, 7, 'Cli A', 2, 3, '09:00', '12:00', 'teste'),
            (2, 1, 0, 101, 'PS-101', 'B', 'Rua B, Sao Paulo - SP, 01000-000, Brasil', -23.51, -46.61, 7, 'Cli B', 1, 1, NULL, NULL, NULL),
            (3, 2, 0, 102, 'PS-102', 'C', 'Rua C, Sao Paulo - SP, 01000-000, Brasil', -23.52, -46.62, 8, 'Cli C', 3, 5, NULL, NULL, NULL),
            (4, 3, 0, 999, 'PS-999', 'X', 'Rua X', -23.0, -46.0, 8, 'Cli X', 1, 1, NULL, NULL, NULL);
    """)
    return conn


class LeituraTestCase(unittest.TestCase):

    def test_servicos_e_rotas_enviadas(self):
        servicos, rotas = rr.servicos_do_dia(_banco(), "2026-09-17", {7: "Seco", 8: "Refrigerado"})
        self.assertEqual(sorted(s["id"] for s in servicos), [100, 101, 102])
        self.assertEqual([[s["id"] for s in r] for r in rotas], [[101, 100], [102]])
        s100 = next(s for s in servicos if s["id"] == 100)
        self.assertEqual(s100["code"], "#PS-100")
        self.assertEqual(s100["dimension_3"], 3)
        self.assertEqual(s100["_nivel_dificuldade"], 2)
        self.assertEqual(s100["_tipo_carga"], "Seco")
        self.assertEqual((s100["_janela_inicio"], s100["_janela_fim"]), ("09:00", "12:00"))
        self.assertEqual((s100["latitude"], s100["longitude"]), (-23.50, -46.60))
        self.assertEqual(next(s for s in servicos if s["id"] == 102)["_tipo_carga"], "Refrigerado")

    def test_dia_sem_rotas(self):
        servicos, rotas = rr.servicos_do_dia(_banco(), "2026-09-18", {})
        self.assertEqual((servicos, rotas), ([], []))


class CorteDaRotaFracaTestCase(unittest.TestCase):
    """O replay conta rota fraca com o MESMO corte da roteirizacao
    (rotas_fracas.py), nao com o padrao de metricas_plano."""

    def test_metricas_usam_o_corte_de_rotas_fracas(self):
        import criar_rotas_diarias as crd
        import rotas_fracas
        rota = [{"id": 1, "latitude": -23.5, "longitude": -46.6, "dimension_3": 1, "_nivel_dificuldade": 1}]
        with mock.patch.object(rotas_fracas, "PARADAS_ROTA_FRACA", 3),              mock.patch.object(rotas_fracas, "CAIXAS_ROTA_FRACA", 11),              mock.patch.object(crd, "planejar_sublotes", return_value=[{"sublotes": [rota]}]),              mock.patch.object(rr.mp, "metricas_plano", return_value={}) as metricas:
            rr.rodar_dia(rota, [rota], (-23.49, -46.66), date(2026, 9, 17))
        self.assertEqual(metricas.call_count, 2)
        for chamada in metricas.call_args_list:
            self.assertEqual((chamada.kwargs["paradas_fraca"], chamada.kwargs["caixas_fraca"]), (3, 11))


if __name__ == "__main__":
    unittest.main()
