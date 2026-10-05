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


class HorasDaReceptoraTestCase(unittest.TestCase):
    """Receptora de rota fraca com 101-110 caixas continua Fiorino (Hugo,
    03/10): o replay estima as horas dela (exige_orcamento_horas a trataria
    como veiculo grande e devolveria 0, escondendo rota acima de 10h30)."""

    def test_receptora_acima_de_100_caixas_tem_horas(self):
        rota = [{"id": i, "latitude": -23.5, "longitude": -46.6 + 0.01 * i, "dimension_3": 35,
                 "address": f"Rua {i}", "_nivel_dificuldade": 1} for i in range(3)]  # 105 caixas
        with mock.patch.object(rr.rd, "estimar_tempo_rota", return_value=7.0):
            self.assertEqual(rr._horas([rota], {id(rota)}), [7.0])
            self.assertEqual(rr._horas([rota]), [0.0])


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
            self.assertEqual(chamada.kwargs["horas_fraca"], rotas_fracas.HORAS_ROTA_FRACA)


class DiasFixosV2TestCase(unittest.TestCase):
    ABCD = "Rua A 1, Centro, Santo André - SP, 09000-000, Brasil"
    TRANSFRIOS = "Estrada Francisco Hengles, 591, Potuvera, Itapecerica da Serra - SP, 06885-160, Brasil"
    CAMPINAS = "Rua B 2, Centro, Campinas - SP, 13000-000, Brasil"
    SP = "Rua C 3, Centro, Sao Paulo - SP, 01000-000, Brasil"

    def test_move_so_quem_caiu_no_dia_pela_regra_antiga(self):
        seg, ter, qua, qui, sex = (date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30),
                                   date(2026, 10, 1), date(2026, 10, 2))
        s = lambda i, endereco: {"id": i, "address": endereco}  # noqa: E731
        mapa = {seg: [s(1, self.ABCD), s(6, self.TRANSFRIOS)],
                qua: [s(2, self.ABCD), s(3, self.CAMPINAS), s(4, self.SP)],
                qui: [s(5, self.CAMPINAS)],
                sex: [s(7, self.ABCD)]}
        novo, movidos = rr.redistribuir_dias_fixos_v2(mapa)
        ids = lambda d: sorted(x["id"] for x in novo.get(d, []))  # noqa: E731
        self.assertEqual(movidos, 3)
        self.assertEqual(ids(seg), [1])
        self.assertEqual(ids(ter), [6])              # Transfrios segunda -> terça
        self.assertEqual(ids(qua), [3, 4])
        self.assertEqual(ids(qui), [2, 5])           # ABCD quarta -> quinta; Campinas quinta (embarcador) fica
        self.assertEqual(ids(sex), [])
        self.assertEqual(ids(date(2026, 10, 5)), [7])  # ABCD sexta -> segunda


if __name__ == "__main__":
    unittest.main()
