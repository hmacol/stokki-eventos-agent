# -*- coding: utf-8 -*-
"""
Datas automaticas: marcar e andar (spec 2026-11-02-agendamento-automatico-proximo-dia-util).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_datas_automaticas -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import datas_automaticas as da
import registro_dia_fixo as reg
import regioes_dia_fixo as rdf

SOROCABA = "Rua XV de Novembro 10, Centro, Sorocaba - SP, 18010-080, Brasil"
TER, QUA = date(2026, 11, 10), date(2026, 11, 11)


def svc(sid, start="2026-11-10 03:00:00", end="2026-11-11 02:59:00", address=None):
    # UTC = 10/11 00:00-23:59 local
    return {"id": sid, "code": f"#PS-{sid}",
            "address": address or "Rua A 1, Centro, Sao Paulo - SP, 01001-000, Brasil",
            "scheduled_start": start, "scheduled_end": end}


class FakeVuupt:
    def __init__(self, falha_em=()):
        self.chamadas = []
        self.falha_em = set(falha_em)

    def atualizar_servico(self, service_id, payload):
        if service_id in self.falha_em:
            raise RuntimeError("boom")
        self.chamadas.append((service_id, payload))


class TestDatasAutomaticas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = reg.conectar(self.db)
        self.addCleanup(self.conn.close)
        self.vuupt = FakeVuupt()

    def _auto(self, s, data=TER):
        reg.registrar_origem(self.conn, s, data, reg.ORIGEM_AUTOMATICO)
        da.marcar_datas_automaticas([s], self.db)

    def test_marca_so_data_automatica(self):
        a, b, c = svc(1), svc(2), {"id": 3, "code": "#PS-3"}
        reg.registrar_origem(self.conn, a, TER, reg.ORIGEM_AUTOMATICO)
        reg.registrar_origem(self.conn, b, TER, reg.ORIGEM_EQUIPE)
        self.assertEqual(da.marcar_datas_automaticas([a, b, c], self.db), 1)
        self.assertTrue(a.get("_data_automatica"))
        self.assertFalse(b.get("_data_automatica"))
        self.assertFalse(c.get("_data_automatica"))

    def test_anda_mantendo_o_horario(self):
        a = svc(1)
        self._auto(a)
        feitos = da.andar_datas_automaticas([a], self.vuupt, QUA, set(), False, self.db)
        self.assertEqual([(f["de"], f["para"]) for f in feitos], [(TER, QUA)])
        self.assertEqual(self.vuupt.chamadas, [(1, {"scheduled_start": "2026-11-11T00:00:00-03:00",
                                                    "scheduled_end": "2026-11-11T23:59:00-03:00"})])
        self.assertTrue(reg.data_e_automatica(self.conn, a, QUA))
        self.assertEqual(a["scheduled_start"], "2026-11-11T00:00:00-03:00")

    def test_nao_anda(self):
        em_rascunho, igual, cliente, teste = svc(1), svc(2), svc(3), svc(4)
        self._auto(em_rascunho)
        self._auto(igual, TER)
        self._auto(teste)
        reg.registrar_origem(self.conn, cliente, TER, reg.ORIGEM_EQUIPE)
        da.marcar_datas_automaticas([cliente], self.db)
        # igual: data_alvo == data agendada
        feitos = da.andar_datas_automaticas([em_rascunho, cliente, teste], self.vuupt, QUA, {1}, False, self.db)
        self.assertEqual([f["servico"]["id"] for f in feitos], [4])
        self.vuupt.chamadas.clear()
        self.assertEqual(da.andar_datas_automaticas([igual], self.vuupt, TER, set(), False, self.db), [])
        self.assertEqual(self.vuupt.chamadas, [])

    def test_modo_teste_nao_grava(self):
        a = svc(1)
        self._auto(a)
        feitos = da.andar_datas_automaticas([a], self.vuupt, QUA, set(), True, self.db)
        self.assertEqual([(f["de"], f["para"]) for f in feitos], [(TER, QUA)])
        self.assertEqual(self.vuupt.chamadas, [])
        self.assertEqual(a["scheduled_start"], "2026-11-10 03:00:00")
        self.assertFalse(reg.data_e_automatica(self.conn, a, QUA))

    def test_falha_na_vuupt_segue_e_nao_registra(self):
        a, b = svc(1), svc(2)
        self._auto(a)
        self._auto(b)
        v = FakeVuupt(falha_em={1})
        feitos = da.andar_datas_automaticas([a, b], v, QUA, set(), False, self.db)
        self.assertEqual([f["servico"]["id"] for f in feitos], [2])
        self.assertFalse(reg.data_e_automatica(self.conn, a, QUA))
        self.assertTrue(reg.data_e_automatica(self.conn, b, QUA))

    def test_regiao_de_dia_fixo_vai_pro_dia_de_visita(self):
        a = svc(1, address=SOROCABA)
        self._auto(a)
        feitos = da.andar_datas_automaticas([a], self.vuupt, QUA, set(), False, self.db)
        self.assertEqual(len(feitos), 1)
        esperado = rdf.proxima_data_valida(rdf.regra_dia_fixo_do_servico(a), QUA)
        self.assertEqual(feitos[0]["para"], esperado)

    def test_ids_em_rascunho_recentes(self):
        vistos = []

        def listar(d):
            vistos.append(d)
            return [{"paradas": [{"service_id": d.day}]}]
        ids = da.ids_em_rascunho_recentes(date(2026, 11, 10), QUA, listar)
        self.assertEqual(ids, {4, 5, 6, 7, 8, 9, 10, 11})


class TestRoteirizacaoTrataDataAutomatica(unittest.TestCase):
    def test_data_automatica_pode_ser_segurada(self):
        import rotas_fracas
        # recreated_order_origin_id faz a funcao parar no motivo seguinte, sem geocodificar
        s = {"scheduled_start": "2026-11-10 03:00:00", "_data_automatica": True, "code": "#PS-1",
             "recreated_order_origin_id": 5}
        self.assertEqual(rotas_fracas.motivo_nao_segurar(s, QUA, set()), "é reentrega")
        s.pop("_data_automatica")
        self.assertEqual(rotas_fracas.motivo_nao_segurar(s, QUA, set()), "tem agendamento")

    def test_nivel4_junta_data_automatica(self):
        import roteirizacao_dados as rd
        a = {"address": "X", "sender_id": 1, "scheduled_start": "2026-11-10T00:00:00-03:00", "_data_automatica": True}
        b = {"address": "X", "sender_id": 1, "scheduled_start": "2026-11-09T00:00:00-03:00", "_data_automatica": True}
        c = {"address": "X", "sender_id": 1}
        self.assertEqual(rd._chave_nivel4(a), rd._chave_nivel4(b))
        self.assertEqual(rd._chave_nivel4(a), rd._chave_nivel4(c))

    def test_data_gerada_depois_do_corte_fica_fora_do_rascunho_de_amanha(self):
        import roteirizacao_dados as rd
        from datetime import datetime
        from regras import agendamento_automatico as aa
        # segunda 09/11 16:00 (depois do corte) -> data automatica = 11/11
        self.assertEqual(aa.data_automatica(datetime(2026, 11, 9, 16, 0)), QUA)
        s = svc(1, start="2026-11-11 03:00:00", end="2026-11-12 02:59:00")
        self.assertFalse(rd.elegivel_para_data(s, TER))
        self.assertTrue(rd.elegivel_para_data(s, QUA))


if __name__ == "__main__":
    unittest.main()
