# -*- coding: utf-8 -*-
"""Dias da semana em que o cliente recebe (ajuste manual, 27/11)."""
import unittest
from datetime import date

from regras import complexidade_entrega as ce
from regras import feriados

DOC = "11222333000144"
SEG, SAB = date(2026, 11, 30), date(2026, 11, 28)
SEG_A_SEX = {DOC: {"dias_semana": "0,1,2,3,4"}}


def serv(sid, doc=DOC, agendado=None):
    return {"id": sid, "customer": {"code": doc}, "scheduled_start": agendado}


class DiasAtendimento(unittest.TestCase):
    def test_sem_ajuste_recebe_todos_os_dias(self):
        self.assertEqual(ce.dias_efetivos(DOC, {}), list(range(7)))
        self.assertIsNone(ce.aviso_dia_fechado(serv(1), SAB, {}))

    def test_sabado_fechado_avisa(self):
        aviso = ce.aviso_dia_fechado(serv(1), SAB, SEG_A_SEX)
        self.assertIn("sábado", aviso)
        self.assertIn("Seg", aviso)
        self.assertIsNone(ce.aviso_dia_fechado(serv(1), SEG, SEG_A_SEX))

    def test_agendamento_do_proprio_dia_vence(self):
        self.assertIsNone(ce.aviso_dia_fechado(serv(1, agendado="2026-11-28T08:00:00"), SAB, SEG_A_SEX))

    def test_separar_tira_so_os_fechados(self):
        abertos, fechados = ce.separar_dia_fechado([serv(1), serv(2, doc="999")], SAB, SEG_A_SEX)
        self.assertEqual([s["id"] for s in abertos], [2])
        self.assertEqual([s["id"] for s in fechados], [1])

    def test_proximo_dia_atendido(self):
        qua = date(2026, 12, 2)
        # cliente só recebe quarta: de segunda vai pra quarta
        self.assertEqual(ce.proximo_dia_atendido(SEG, [2], feriados.eh_dia_util), qua)
        # recebe todos os dias: não mexe
        self.assertEqual(ce.proximo_dia_atendido(SEG, list(range(7)), feriados.eh_dia_util), SEG)
        # nunca cai em fim de semana mesmo que o cliente receba
        self.assertEqual(ce.proximo_dia_atendido(SAB, [5, 6], feriados.eh_dia_util), SAB)


if __name__ == "__main__":
    unittest.main()
