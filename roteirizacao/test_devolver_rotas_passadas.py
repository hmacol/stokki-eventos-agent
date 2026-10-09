# -*- coding: utf-8 -*-
"""
test_devolver_rotas_passadas.py

Passo 2 do cancelar_rotas_sem_motorista (Hugo, 28/09): pedido não
iniciado em rota de dia anterior volta pro pool antes das 18h; pedido
iniciado (on_route/arrived) nunca é mexido.

Rodar (da raiz):
    python -m unittest roteirizacao.test_devolver_rotas_passadas -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import cancelar_rotas_sem_motorista as crsm  # noqa: E402

HOJE = date(2026, 9, 29)


def _rota(servicos, status="in_progress", rota_id=10, start_at="2026-09-28 08:00:00", agent_id=None):
    return {"id": rota_id, "name": "Planejamento - 28/09/2026 - #1", "status": status,
            "start_at": start_at, "agent_id": agent_id, "services": {"data": servicos}}


def _s(sid, status):
    return {"id": sid, "code": f"#PS-{sid}", "status": status}


class TestPlanoDevolucao(unittest.TestCase):
    def test_rota_mista_reescreve_so_com_o_que_fica(self):
        plano = crsm.plano_devolucao(_rota([_s(1, "done"), _s(2, "assigned"), _s(3, "on_route"), _s(4, "accepted")]))
        self.assertEqual(plano["acao"], "atualizar")
        self.assertEqual([s["id"] for s in plano["devolver"]], [2, 4])
        self.assertEqual(plano["manter_ids"], [1, 3])
        self.assertEqual(plano["iniciados"], ["PS-3"])

    def test_nada_iniciado_nem_concluido_cancela_a_rota(self):
        plano = crsm.plano_devolucao(_rota([_s(1, "assigned"), _s(2, "not_assigned")]))
        self.assertEqual(plano["acao"], "cancelar_rota")

    def test_so_iniciados_nao_mexe(self):
        plano = crsm.plano_devolucao(_rota([_s(1, "done"), _s(2, "arrived")]))
        self.assertEqual(plano["acao"], "nada")
        self.assertEqual(plano["iniciados"], ["PS-2"])

    def test_rota_com_movimento_hoje_nao_mexe(self):
        entregue_hoje = {**_s(1, "done"), "completed_at": "2026-09-29 13:00:00"}  # UTC -> 10h local
        plano = crsm.plano_devolucao(_rota([entregue_hoje, _s(2, "assigned")]), HOJE)
        self.assertEqual(plano["acao"], "nada")
        self.assertTrue(plano["rodando_hoje"])
        self.assertEqual(plano["iniciados"], ["PS-2"])

    def test_rota_encerrada_nao_mexe(self):
        plano = crsm.plano_devolucao(_rota([_s(1, "assigned")], status="finished"))
        self.assertEqual(plano["acao"], "nada")


class TestDevolverPendentes(unittest.TestCase):
    def test_rota_de_hoje_nao_entra_e_devolucao_registra(self):
        rotas = [_rota([_s(1, "done"), _s(2, "assigned")], rota_id=10),
                 _rota([_s(5, "assigned")], rota_id=11, start_at="2026-09-29 08:00:00")]
        with mock.patch.object(crsm, "listar_rotas", return_value=rotas), \
             mock.patch.object(crsm, "atualizar_rota") as atualizar, \
             mock.patch.object(crsm, "cancelar_rota") as cancelar, \
             mock.patch.object(crsm.tratativas, "registrar_evento") as evento, \
             mock.patch("nucleo.sincronizar_servicos_vuupt.ressincronizar_ids"), \
             mock.patch("vuupt_client.VuuptClient"):
            r = crsm.devolver_pendentes_de_rotas_passadas("t", HOJE, modo_teste=False)
        atualizar.assert_called_once_with("t", 10, [1])
        cancelar.assert_not_called()
        self.assertEqual(r["devolvidos"], ["PS-2"])
        self.assertEqual(evento.call_args[0][2], "DEVOLVIDO_AO_POOL")

    def test_modo_teste_nao_escreve(self):
        with mock.patch.object(crsm, "listar_rotas", return_value=[_rota([_s(2, "assigned")])]), \
             mock.patch.object(crsm, "atualizar_rota") as atualizar, \
             mock.patch.object(crsm, "cancelar_rota") as cancelar:
            r = crsm.devolver_pendentes_de_rotas_passadas("t", HOJE, modo_teste=True)
        atualizar.assert_not_called()
        cancelar.assert_not_called()
        self.assertEqual(r["devolvidos"], ["PS-2"])

    def test_motorista_sem_app_nao_e_devolvido(self):
        rotas = [_rota([_s(1, "assigned"), _s(2, "done")], rota_id=10, agent_id=50191),
                 _rota([_s(3, "assigned")], rota_id=11, agent_id=999)]
        with mock.patch.object(crsm, "listar_rotas", return_value=rotas),              mock.patch.object(crsm, "atualizar_rota") as atualizar,              mock.patch.object(crsm, "cancelar_rota") as cancelar,              mock.patch.object(crsm, "reverter_por_vuupt_route_id"),              mock.patch.object(crsm.tratativas, "registrar_evento"),              mock.patch("nucleo.sincronizar_servicos_vuupt.ressincronizar_ids"),              mock.patch("vuupt_client.VuuptClient"):
            r = crsm.devolver_pendentes_de_rotas_passadas("t", HOJE, modo_teste=False, sem_app={50191})
        cancelar.assert_called_once_with("t", 11, services_action="unassign")
        atualizar.assert_not_called()
        self.assertEqual(r["devolvidos"], ["PS-3"])
        self.assertEqual(len(r["aguardando_baixa"]), 1)
        self.assertIn("1 pedido", r["aguardando_baixa"][0])


if __name__ == "__main__":
    unittest.main()
