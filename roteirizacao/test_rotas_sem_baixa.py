# -*- coding: utf-8 -*-
"""
test_rotas_sem_baixa.py

cancelar_rotas_sem_motorista (Hugo, 28/10): às 15h45 só AVISA no grupo
ALERTA LOGISTICA as rotas de dias anteriores com pedido sem baixa.
Nenhum pedido em rota volta pro pool sozinho (só cancelando na mão).

Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_rotas_sem_baixa -v
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

HOJE = date(2026, 10, 29)


def _rota(servicos, status="in_progress", rota_id=10, start_at="2026-10-28 09:00:00", agent_id=47039,
          nome="Planejamento - 28/10/2026 - #1"):
    return {"id": rota_id, "name": nome, "status": status, "start_at": start_at, "agent_id": agent_id,
            "services": {"data": servicos}}


def _s(sid, status):
    return {"id": sid, "code": f"#PS-{sid}", "status": status}


class TestRotasSemBaixa(unittest.TestCase):
    def test_conta_pendentes_e_iniciados(self):
        r = crsm.rotas_sem_baixa([_rota([_s(1, "done"), _s(2, "assigned"), _s(3, "on_route"),
                                         _s(4, "accepted"), _s(5, "canceled")])], HOJE, {47039: "Hedgar"})
        self.assertEqual(r, [{"rota_id": 10, "numero": "#1", "nome": "Planejamento - 28/10/2026 - #1",
                              "data": date(2026, 10, 28), "motorista": "Hedgar", "pendentes": 3,
                              "iniciados": 1}])

    def test_ignora_hoje_encerrada_e_sem_pendencia(self):
        rotas = [_rota([_s(1, "assigned")], start_at="2026-10-29 09:00:00"),
                 _rota([_s(2, "assigned")], status="finished"),
                 _rota([_s(3, "assigned")], status="canceled"),
                 _rota([_s(4, "done"), _s(5, "canceled")]),
                 _rota([_s(6, "assigned")], start_at=None)]
        self.assertEqual(crsm.rotas_sem_baixa(rotas, HOJE, {}), [])

    def test_sem_motorista_e_nome_sem_numero(self):
        r = crsm.rotas_sem_baixa([_rota([_s(1, "assigned")], agent_id=None, nome="Coleta Empório")], HOJE, {})
        self.assertIsNone(r[0]["motorista"])
        self.assertEqual(r[0]["numero"], "Coleta Empório")

    def test_ordena_por_data_e_numero(self):
        rotas = [_rota([_s(1, "assigned")], rota_id=3, nome="Planejamento - 28/10/2026 - #12"),
                 _rota([_s(2, "assigned")], rota_id=2, start_at="2026-10-20 09:00:00",
                       nome="Planejamento - 20/10/2026 - #1 (Hedgar, relancada)"),
                 _rota([_s(3, "assigned")], rota_id=1, nome="Planejamento - 28/10/2026 - #2")]
        self.assertEqual([x["rota_id"] for x in crsm.rotas_sem_baixa(rotas, HOJE, {})], [2, 1, 3])


class TestMainSoAvisa(unittest.TestCase):
    def test_nao_mexe_na_vuupt(self):
        # nada de cancelar/reescrever rota: as funções de escrita nem são importadas
        for nome in ("cancelar_rota", "atualizar_rota", "reverter_por_vuupt_route_id"):
            self.assertFalse(hasattr(crsm, nome), nome)

    def test_main_avisa_o_grupo_com_a_lista(self):
        rotas = [_rota([_s(1, "assigned")])]
        with mock.patch.object(crsm, "_carregar_config", return_value={"vuupt_api": {"token": "t"}}), \
             mock.patch.object(crsm, "listar_rotas", return_value=rotas) as listar, \
             mock.patch.object(crsm, "_nomes_motoristas", return_value={47039: "Hedgar"}), \
             mock.patch.object(crsm, "notificar_execucao") as execucao, \
             mock.patch("notificar_whatsapp.avisar_rotas_sem_baixa", return_value="enviado") as avisar:
            crsm.main(modo_teste=False, hoje=HOJE)
        self.assertEqual(listar.call_args.kwargs["include"], ["services"])
        enviadas = avisar.call_args[0][0]
        self.assertEqual([(x["numero"], x["pendentes"]) for x in enviadas], [("#1", 1)])
        self.assertFalse(avisar.call_args.kwargs["modo_teste"])
        detalhe = execucao.call_args[0][0]["Pedidos sem baixa em rotas de dias anteriores"]["detalhe"]
        self.assertIn("1 rota", detalhe)
        self.assertIn("nada devolvido", detalhe)


if __name__ == "__main__":
    unittest.main()
