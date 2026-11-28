# -*- coding: utf-8 -*-
"""Dias da semana em que o cliente recebe (ajuste manual, 27/11). A regra de
aviso/realocação está em roteirizacao/test_realocar_dia_fechado.py."""
import unittest
from datetime import date

from regras import complexidade_entrega as ce
from regras import feriados

DOC = "11222333000144"
SEG, SAB = date(2026, 11, 30), date(2026, 11, 28)


class DiasAtendimento(unittest.TestCase):
    def test_sem_ajuste_recebe_todos_os_dias(self):
        self.assertEqual(ce.dias_efetivos(DOC, {}), list(range(7)))

    def test_dias_do_ajuste(self):
        self.assertEqual(ce.dias_efetivos("11.222.333/0001-44", {DOC: {"dias_semana": "0,2"}}), [0, 2])

    def test_proximo_dia_atendido(self):
        qua = date(2026, 12, 2)
        self.assertEqual(ce.proximo_dia_atendido(SEG, [2], feriados.eh_dia_util), qua)
        self.assertEqual(ce.proximo_dia_atendido(SEG, list(range(7)), feriados.eh_dia_util), SEG)
        # nunca cai em fim de semana mesmo que o cliente receba
        self.assertEqual(ce.proximo_dia_atendido(SAB, [5, 6], feriados.eh_dia_util), SAB)


if __name__ == "__main__":
    unittest.main()
