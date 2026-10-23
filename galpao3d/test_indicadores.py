# -*- coding: utf-8 -*-
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from galpao3d import banco, indicadores


def _ped(conn, i, unid, situacao="Aguardando Transportador"):
    conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, situacao, unidades) VALUES (?,?,?,?)",
                 (i, f"PS-{i}", situacao, unid))


def _conf(conn, i, usuario, inicio, seg):
    conn.execute("INSERT INTO galpao_conferencias (id_stokki, estacao, usuario, inicio, fim, segundos, problema)"
                 " VALUES (?,?,?,?,?,?,0)", (i, "E1", usuario, inicio, inicio, seg))


def _ev(conn, i, situacao, usuario, quando):
    conn.execute("INSERT INTO galpao_eventos (id_stokki, situacao, usuario, quando) VALUES (?,?,?,?)",
                 (i, situacao, usuario, quando))


class TestIndicadores(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = banco.conectar(Path(self._tmp.name) / "t.db")
        self.desde = datetime(2026, 10, 22, 4, 0)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_media_ponderada(self):
        _ped(self.conn, 1, 10)
        _ped(self.conn, 2, 2)
        _conf(self.conn, 1, "Felipe", "2026-10-22 09:00:00", 60)
        _conf(self.conn, 2, "Felipe", "2026-10-22 09:10:00", 30)
        r = indicadores.conferencia_por_pessoa(self.conn, self.desde)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["usuario"], "Felipe")
        self.assertEqual(r[0]["pedidos"], 2)
        self.assertEqual(r[0]["unidades"], 12)
        self.assertEqual(r[0]["media_s_ponderada"], 55)
        g = indicadores.conferencia_geral(self.conn, self.desde)
        self.assertEqual(g, {"pedidos": 2, "media_s_ponderada": 55})

    def test_pedido_sem_unidades_pesa_1(self):
        _ped(self.conn, 1, 0)
        _ped(self.conn, 2, 1)
        _conf(self.conn, 1, "Ana", "2026-10-22 09:00:00", 100)
        _conf(self.conn, 2, "Ana", "2026-10-22 09:10:00", 50)
        r = indicadores.conferencia_por_pessoa(self.conn, self.desde)
        self.assertEqual(r[0]["media_s_ponderada"], 75)

    def test_conferencia_antes_do_dia_fica_fora(self):
        _ped(self.conn, 1, 5)
        _conf(self.conn, 1, "Ana", "2026-10-21 09:00:00", 100)
        self.assertEqual(indicadores.conferencia_por_pessoa(self.conn, self.desde), [])
        self.assertEqual(indicadores.conferencia_geral(self.conn, self.desde),
                         {"pedidos": 0, "media_s_ponderada": 0})

    def test_produtividade(self):
        for i, (u, h) in enumerate([(5, "11:00"), (7, "11:10"), (8, "11:20")], start=1):
            _ped(self.conn, i, u)
            _ev(self.conn, i, "Em Conferência", "Werlly", f"2026-10-22 {h}:00")
        agora = datetime(2026, 10, 22, 12, 0)
        r = indicadores.produtividade_separacao(self.conn, self.desde, agora)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["operador"], "Werlly")
        self.assertEqual(r[0]["pedidos"], 3)
        self.assertEqual(r[0]["unidades"], 20)
        self.assertEqual(r[0]["horas"], 0.5)
        self.assertEqual(r[0]["unidades_por_hora"], 40)

    def test_produtividade_usa_operador_da_tarefa(self):
        _ped(self.conn, 1, 10)
        self.conn.execute("UPDATE galpao_pedidos SET operador_tarefa = 'Werlly' WHERE id_stokki = 1")
        _ev(self.conn, 1, "Em Conferência", "Supervisor", "2026-10-22 11:00:00")
        r = indicadores.produtividade_separacao(self.conn, self.desde, datetime(2026, 10, 22, 12, 0))
        self.assertEqual([x["operador"] for x in r], ["Werlly"])

    def test_produtividade_fallback_usuario_do_evento(self):
        _ped(self.conn, 1, 10)
        _ev(self.conn, 1, "Em Conferência", "Supervisor", "2026-10-22 11:00:00")
        r = indicadores.produtividade_separacao(self.conn, self.desde, datetime(2026, 10, 22, 12, 0))
        self.assertEqual([x["operador"] for x in r], ["Supervisor"])

    def test_espera_noite(self):
        # ainda esperando: entra mesmo tendo chegado antes do inicio do dia operacional
        _ped(self.conn, 1, 3, situacao="Em Conferência")
        _ev(self.conn, 1, "Em Conferência", "X", "2026-10-22 17:50:00")
        r = indicadores.esperas(self.conn, datetime(2026, 10, 23, 4, 0), datetime(2026, 10, 23, 8, 10))
        self.assertEqual(r, [{"id_stokki": 1, "etapa": "separacao→conferencia", "minutos": 20}])

    def test_espera_conferencia_comecou_hoje_chegou_ontem(self):
        _ped(self.conn, 1, 3)
        _ev(self.conn, 1, "Em Conferência", "X", "2026-10-22 17:50:00")
        _conf(self.conn, 1, "Ana", "2026-10-23 08:30:00", 30)
        r = indicadores.esperas(self.conn, datetime(2026, 10, 23, 4, 0), datetime(2026, 10, 23, 9, 0))
        self.assertEqual([x["minutos"] for x in r], [40])

    def test_espera_conferencia_de_ontem_fica_fora(self):
        _ped(self.conn, 1, 3)
        _ev(self.conn, 1, "Em Conferência", "X", "2026-10-22 09:00:00")
        _conf(self.conn, 1, "Ana", "2026-10-22 09:45:00", 30)
        self.assertEqual(indicadores.esperas(self.conn, datetime(2026, 10, 23, 4, 0),
                                             datetime(2026, 10, 23, 9, 0)), [])

    def test_espera_saiu_da_conferencia_sem_inicio_fica_fora(self):
        _ped(self.conn, 1, 3, situacao="Aguardando Transportador")
        _ev(self.conn, 1, "Em Conferência", "X", "2026-10-23 08:00:00")
        self.assertEqual(indicadores.esperas(self.conn, datetime(2026, 10, 23, 4, 0),
                                             datetime(2026, 10, 23, 9, 0)), [])

    def test_espera_ate_inicio_da_conferencia(self):
        _ped(self.conn, 1, 3)
        _ev(self.conn, 1, "Em Conferência", "X", "2026-10-22 09:00:00")
        _conf(self.conn, 1, "Ana", "2026-10-22 09:45:00", 30)
        r = indicadores.esperas(self.conn, self.desde, datetime(2026, 10, 22, 15, 0))
        self.assertEqual(r[0]["minutos"], 45)


if __name__ == "__main__":
    unittest.main()
