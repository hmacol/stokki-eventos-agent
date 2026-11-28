# -*- coding: utf-8 -*-
"""Salvar os dias no modal realoca os pedidos do cliente (Hugo, 27/11).
Rodar de dentro de painel_agentes/."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import planejamento_rotas as pr
import rascunhos_rota
import registro_dia_fixo

DOC = "11222333000144"
AJUSTES = {DOC: {"dias_semana": "0,1,2,3,4", "horario_atendimento_inicio": "08:00",
                 "horario_atendimento_fim": "17:00"}}
HOJE = date.today()
SAB = HOJE + timedelta(days=(5 - HOJE.weekday()) % 7 or 7)
SEG = SAB + timedelta(days=2)
while not pr.feriados.eh_dia_util(SEG):
    SEG += timedelta(days=1)


def servico(sid, agendado=None):
    return {"id": sid, "code": f"#PS-{sid}", "status": "not_assigned", "address": "Rua Augusta, 100 - Sao Paulo - SP",
            "scheduled_start": agendado, "scheduled_end": None}


def rascunho(rid, status, route_id=None):
    return {"id": rid, "status": status, "vuupt_route_id": route_id, "data_alvo": SAB.isoformat(), "nome": f"Rota {rid}"}


class RealocarCliente(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "t.db"
        self.vuupt = mock.Mock()
        self.servicos = {
            1: servico(1),                                          # rascunho de sabado
            2: servico(2),                                          # enviada nao iniciada
            3: servico(3),                                          # enviada iniciada
            4: servico(4, f"{SAB.isoformat()}T12:00:00"),           # data do cliente
            5: servico(5, f"{SEG.isoformat()}T12:00:00"),           # dia aberto
        }
        self.vuupt.buscar_servico_por_id.side_effect = lambda sid: dict(self.servicos[sid])
        em_rota = {1: rascunho(10, rascunhos_rota.STATUS_RASCUNHO),
                   2: rascunho(20, rascunhos_rota.STATUS_ENVIADO, 200),
                   3: rascunho(30, rascunhos_rota.STATUS_ENVIADO, 300)}
        self.remover_parada = mock.Mock()
        self.remover_enviada = mock.Mock(return_value={"ok": True})
        import edicao_rota
        patches = [
            mock.patch.object(pr, "carregar_ajustes_manuais", return_value=AJUSTES),
            mock.patch.object(pr, "_service_ids_pendentes_do_cliente", return_value=[1, 2, 3, 4, 5]),
            mock.patch.object(pr, "_rascunhos_ativos_com", return_value=em_rota),
            mock.patch.object(pr, "_carregar_config", return_value={"vuupt_api": {"token": "t"}}),
            mock.patch.object(pr, "VuuptClient", return_value=self.vuupt),
            mock.patch.object(pr, "_ressincronizar"),
            mock.patch.object(rascunhos_rota, "remover_parada", self.remover_parada),
            mock.patch.object(rascunhos_rota, "remover_de_rota_enviada", self.remover_enviada),
            mock.patch.object(edicao_rota, "ler_rota", side_effect=lambda token, rid: {"route": rid}),
            mock.patch.object(edicao_rota, "rota_iniciada", side_effect=lambda rota: rota["route"] == 300),
            mock.patch.object(registro_dia_fixo, "DB_PATH", self.db),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        original = registro_dia_fixo.conectar
        p = mock.patch.object(registro_dia_fixo, "conectar", side_effect=lambda db_path=None: original(self.db))
        p.start()
        self.addCleanup(p.stop)

    def test_realoca_conforme_a_regra(self):
        r = pr.realocar_pedidos_do_cliente(DOC, "hugo")
        self.assertEqual(sorted(x["codigo"] for x in r["realocados"]), ["#PS-1", "#PS-2"])
        self.assertTrue(all(x["para"] == SEG.isoformat() for x in r["realocados"]))
        self.assertEqual(r["mantidos"], ["#PS-4"])
        self.assertEqual(r["rota_iniciada"], ["#PS-3"])
        self.assertEqual(r["falhas"], [])
        self.remover_parada.assert_called_once_with(10, 1)
        args = self.remover_enviada.call_args[0]
        self.assertEqual(args[:3], (20, 2, "Outros"))
        self.assertIn("não recebe sábado", args[3])
        self.assertEqual(sorted(c[0][0] for c in self.vuupt.atualizar_servico.call_args_list), [1, 2])

    def test_recusa_da_rota_enviada_nao_reagenda(self):
        self.remover_enviada.side_effect = ValueError("Rota encerrada")
        r = pr.realocar_pedidos_do_cliente(DOC, "hugo")
        self.assertEqual([x["codigo"] for x in r["realocados"]], ["#PS-1"])
        self.assertEqual(r["falhas"], [{"codigo": "#PS-2", "erro": "Rota encerrada"}])
        self.assertEqual([c[0][0] for c in self.vuupt.atualizar_servico.call_args_list], [1])


class TirarDeRascunhosDaRodada(unittest.TestCase):
    """roteirizacao/realocar_dia_fechado.tirar_de_rascunhos num banco temporário."""
    def test_tira_so_de_rascunho_e_ofertada_de_hoje_em_diante(self):
        import realocar_dia_fechado as rdf
        db = Path(tempfile.mkdtemp()) / "r.db"
        with mock.patch.object(rascunhos_rota, "DB_PATH", db), \
             mock.patch.object(rascunhos_rota, "_recalcular_km_silencioso", create=True):
            conn = rascunhos_rota._conectar()
            amanha, ontem = (HOJE + timedelta(days=1)).isoformat(), (HOJE - timedelta(days=1)).isoformat()
            for rid, data_alvo, status in [(1, amanha, "RASCUNHO"), (2, amanha, "OFERTADA"),
                                           (3, amanha, "ENVIADO"), (4, ontem, "RASCUNHO")]:
                conn.execute("INSERT INTO rascunhos_rota (id, data_alvo, status, nome, lote_id, start_location_base_id, "
                             "start_at) VALUES (?, ?, ?, ?, 'L', 1, '07:00')", (rid, data_alvo, status, f"Rota {rid}"))
                conn.execute("INSERT INTO rascunhos_parada (rascunho_id, ordem, service_id) VALUES (?, 0, 77)", (rid,))
            conn.commit()
            conn.close()
            self.assertEqual(sorted(rdf.tirar_de_rascunhos(77, HOJE)), ["Rota 1", "Rota 2"])
            conn = rascunhos_rota._conectar()
            sobrou = sorted(r[0] for r in conn.execute("SELECT rascunho_id FROM rascunhos_parada WHERE service_id = 77"))
            conn.close()
        self.assertEqual(sobrou, [3, 4])


if __name__ == "__main__":
    unittest.main()
