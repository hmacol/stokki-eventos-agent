# -*- coding: utf-8 -*-
"""
Data agendada pela equipe no Planejamento não é data do cliente (Hugo,
03/10 -- dias fixos v2): reagendar_pedido e reagendar_pedidos registram a
origem EQUIPE em agendamentos_origem.
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_agendamento_equipe -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas
import registro_dia_fixo as reg

QUINTA = date(2026, 10, 8)


class AgendamentoEquipe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        for alvo, obj, kw in (("DB_PATH", reg, {"new": self.db}),
                              ("_carregar_config", planejamento_rotas, {"return_value": {"vuupt_api": {"token": "t"}}}),
                              ("_ressincronizar", planejamento_rotas, {"return_value": None})):
            p = mock.patch.object(obj, alvo, **kw)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(planejamento_rotas, "VuuptClient")
        self.vuupt = p.start().return_value
        self.addCleanup(p.stop)

    def _equipe(self, service_id, data=QUINTA):
        conn = reg.conectar(self.db)
        try:
            return reg.data_nao_e_do_cliente(conn, {"id": service_id, "code": f"#PS-{service_id}"}, data)
        finally:
            conn.close()

    def test_reagendar_um_registra_equipe(self):
        self.assertEqual(planejamento_rotas.reagendar_pedido(111, "2026-10-08", "08:00", "12:00"), {"ok": True})
        self.assertTrue(self._equipe(111))
        conn = reg.conectar(self.db)
        try:
            self.assertEqual(conn.execute("SELECT origem FROM agendamentos_origem").fetchone()[0], reg.ORIGEM_EQUIPE)
        finally:
            conn.close()

    def test_falha_na_vuupt_nao_registra(self):
        from vuupt_client import VuuptAPIError
        self.vuupt.atualizar_servico.side_effect = VuuptAPIError("recusado")
        self.assertFalse(planejamento_rotas.reagendar_pedido(111, "2026-10-08", "08:00", "12:00")["ok"])
        self.assertFalse(self._equipe(111))

    def test_lote_registra_so_os_que_deram_certo(self):
        def atualizar(service_id, payload):
            if service_id == 222:
                raise RuntimeError("timeout")
        self.vuupt.atualizar_servico.side_effect = atualizar
        r = planejamento_rotas.reagendar_pedidos([{"service_id": 111}, {"service_id": 222}], "2026-10-08", "08:00", "12:00")
        self.assertEqual([f["service_id"] for f in r["falhas"]], [222])
        self.assertTrue(self._equipe(111))
        self.assertFalse(self._equipe(222))

    def test_falha_no_registro_nao_derruba_o_reagendamento(self):
        with mock.patch.object(reg, "DB_PATH", Path(self.tmp.name) / "nao_existe" / "t.db"):
            self.assertEqual(planejamento_rotas.reagendar_pedido(111, "2026-10-08", "08:00", "12:00"), {"ok": True})


if __name__ == "__main__":
    unittest.main()
