# -*- coding: utf-8 -*-
"""
Cliente retoma pedido em espera a pedido dele (spec 2026-10-25, secao 4 item 3).

    py -3.11 -m unittest portal_cliente.test_retomar_em_espera
"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402
from test_cancelamento import conn_portal, envio  # noqa: E402


def _linha(conn, id_):
    return ep._linha(conn.execute("SELECT * FROM portal_envios WHERE id = ?", (id_,)).fetchone(), {})


def _retomada(conn, id_):
    return dict(conn.execute("SELECT retomar_modo, retomar_data, retomar_hora_inicio, retomar_hora_fim "
                             "FROM portal_envios WHERE id = ?", (id_,)).fetchone())


class RetomarEmEspera(unittest.TestCase):
    def setUp(self):
        self.conn = conn_portal([(60, ep.STATUS_EM_ESPERA_CLIENTE, "9960", "PS-39960")])

    def test_linha_em_espera_oferece_retomar_e_cancelar(self):
        l = _linha(self.conn, 60)
        self.assertTrue(l["pode_retomar"])
        self.assertTrue(l["pode_cancelar"])
        self.assertFalse(l["pode_reagendar"])
        self.assertFalse(l["pode_em_espera"])

    def test_linha_retomando_sem_acoes(self):
        self.conn.execute("UPDATE portal_envios SET status = ? WHERE id = 60", (ep.STATUS_RETOMANDO,))
        l = _linha(self.conn, 60)
        for k in ("pode_retomar", "pode_cancelar", "pode_reagendar", "pode_em_espera", "pode_reenviar"):
            self.assertFalse(l[k], k)

    def test_linha_criado_nao_oferece_retomar(self):
        conn = conn_portal([(61, ep.STATUS_CRIADO, "9961", "PS-39961")])
        self.assertFalse(_linha(conn, 61)["pode_retomar"])

    def test_retomar_proxima_rota_vira_retomando(self):
        r = ep.aplicar_acao(self.conn, envio(self.conn, 60), "retomar", {"modo": "proxima_rota"}, "cliente")
        self.assertEqual((r["aplicado"], r["em_andamento"]), (False, True))
        self.assertIn("Retomando", r["mensagem"])
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_RETOMANDO)
        self.assertEqual(_retomada(self.conn, 60), {"retomar_modo": "proxima_rota", "retomar_data": None,
                                                    "retomar_hora_inicio": None, "retomar_hora_fim": None})

    def test_retomar_agendamento_grava_data_e_horas(self):
        data = (date.today() + timedelta(days=3)).isoformat()
        ep.aplicar_acao(self.conn, envio(self.conn, 60), "retomar",
                        {"modo": "agendamento", "data": data, "hora_inicio": "09:00", "hora_fim": "12:30"}, "cliente")
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_RETOMANDO)
        self.assertEqual(_retomada(self.conn, 60), {"retomar_modo": "agendamento", "retomar_data": data,
                                                    "retomar_hora_inicio": "09:00", "retomar_hora_fim": "12:30"})

    def test_retomar_agendamento_valida_data(self):
        ontem = (date.today() - timedelta(days=1)).isoformat()
        for dados in ({"modo": "agendamento"},
                      {"modo": "agendamento", "data": "lixo"},
                      {"modo": "agendamento", "data": ontem}):
            with self.assertRaises(ep.ErroEnvio, msg=str(dados)):
                ep.aplicar_acao(self.conn, envio(self.conn, 60), "retomar", dados, "cliente")
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_EM_ESPERA_CLIENTE)

    def test_retomar_modo_invalido_recusa(self):
        for modo in (None, "", "agora", "<script>"):
            with self.assertRaises(ep.ErroEnvio):
                ep.aplicar_acao(self.conn, envio(self.conn, 60), "retomar", {"modo": modo}, "cliente")
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_EM_ESPERA_CLIENTE)

    def test_retomar_fora_de_em_espera_recusa(self):
        for st in (ep.STATUS_CRIADO, ep.STATUS_NA_FILA, ep.STATUS_CANCELADO, ep.STATUS_RETOMANDO, ep.STATUS_CANCELANDO):
            conn = conn_portal([(70, st, "9970", "PS-39970")])
            with self.assertRaises(ep.ErroEnvio, msg=st):
                ep.aplicar_acao(conn, envio(conn, 70), "retomar", {"modo": "proxima_rota"}, "cliente")
            self.assertEqual(envio(conn, 70)["status"], st)

    def test_cancelar_em_espera_vira_cancelando(self):
        r = ep.aplicar_acao(self.conn, envio(self.conn, 60), "cancelar", {"motivo": "desistiu"}, "cliente")
        self.assertTrue(r["em_andamento"])
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_CANCELANDO)
        self.assertEqual(self.conn.execute("SELECT tipo, status FROM portal_solicitacoes").fetchall()[0][:], ("cancelar", "PENDENTE"))

    def test_cancelar_retomando_recusa(self):
        self.conn.execute("UPDATE portal_envios SET status = ? WHERE id = 60", (ep.STATUS_RETOMANDO,))
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(self.conn, envio(self.conn, 60), "cancelar", {}, "cliente")

    def test_reagendar_em_espera_continua_valendo_so_para_os_antigos(self):
        # regressao: reagendar com data passada ainda recusa (validacao compartilhada)
        conn = conn_portal([(71, ep.STATUS_CRIADO, "9971", "PS-39971")])
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, envio(conn, 71), "reagendar", {"data": "2020-01-01"}, "cliente")


if __name__ == "__main__":
    unittest.main()
