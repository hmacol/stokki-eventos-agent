# -*- coding: utf-8 -*-
"""
Calendario de feriados (Hugo, 07/10/2026: a Fresh Log nao opera em
feriado; visita de dia fixo que cai em feriado vai pro dia util seguinte).
Rodar (da raiz): py -3.11 -m unittest regras.test_feriados -v
"""
import unittest
from datetime import date, datetime

from regras import feriados


class TestCalendario(unittest.TestCase):
    def test_nacionais_fixos_2026(self):
        for d in (date(2026, 1, 1), date(2026, 4, 21), date(2026, 5, 1), date(2026, 9, 7),
                  date(2026, 10, 12), date(2026, 11, 2), date(2026, 11, 15), date(2026, 11, 20),
                  date(2026, 12, 25)):
            self.assertTrue(feriados.eh_feriado(d), d)

    def test_moveis_2026_pela_pascoa(self):
        self.assertTrue(feriados.eh_feriado(date(2026, 2, 16)))   # Carnaval (segunda)
        self.assertTrue(feriados.eh_feriado(date(2026, 2, 17)))   # Carnaval (terca)
        self.assertTrue(feriados.eh_feriado(date(2026, 4, 3)))    # Sexta-feira Santa
        self.assertTrue(feriados.eh_feriado(date(2026, 6, 4)))    # Corpus Christi
        self.assertFalse(feriados.eh_feriado(date(2026, 2, 18)))  # quarta de cinzas: opera

    def test_moveis_2027(self):
        self.assertTrue(feriados.eh_feriado(date(2027, 2, 9)))
        self.assertTrue(feriados.eh_feriado(date(2027, 3, 26)))
        self.assertTrue(feriados.eh_feriado(date(2027, 5, 27)))

    def test_estadual_e_municipal_sp(self):
        self.assertTrue(feriados.eh_feriado(date(2026, 7, 9)))    # Revolucao Constitucionalista
        self.assertTrue(feriados.eh_feriado(date(2027, 1, 25)))   # aniversario de Sao Paulo

    def test_dia_comum_nao_e_feriado(self):
        self.assertFalse(feriados.eh_feriado(date(2026, 10, 13)))
        self.assertIsNone(feriados.nome_feriado(date(2026, 10, 13)))
        self.assertEqual(feriados.nome_feriado(date(2026, 10, 12)), "Nossa Senhora Aparecida")

    def test_aceita_datetime(self):
        self.assertTrue(feriados.eh_feriado(datetime(2026, 10, 12, 18, 0)))


class TestDiaUtil(unittest.TestCase):
    def test_dia_util_exclui_fim_de_semana_e_feriado(self):
        self.assertTrue(feriados.eh_dia_util(date(2026, 10, 13)))
        self.assertFalse(feriados.eh_dia_util(date(2026, 10, 10)))  # sabado
        self.assertFalse(feriados.eh_dia_util(date(2026, 10, 12)))  # feriado

    def test_rolar_para_dia_util_e_inclusivo(self):
        self.assertEqual(feriados.rolar_para_dia_util(date(2026, 10, 13)), date(2026, 10, 13))
        self.assertEqual(feriados.rolar_para_dia_util(date(2026, 10, 12)), date(2026, 10, 13))
        self.assertEqual(feriados.rolar_para_dia_util(date(2026, 10, 10)), date(2026, 10, 13))

    def test_proximo_dia_util_e_estrito(self):
        self.assertEqual(feriados.proximo_dia_util(date(2026, 10, 9)), date(2026, 10, 13))   # sexta -> pula fds e feriado
        self.assertEqual(feriados.proximo_dia_util(date(2026, 10, 13)), date(2026, 10, 14))

    def test_dia_util_anterior(self):
        self.assertEqual(feriados.dia_util_anterior(date(2026, 10, 13)), date(2026, 10, 9))

    def test_somar_dias_uteis(self):
        # quinta 08/10 + 3 uteis: sex 09, ter 13, qua 14
        self.assertEqual(feriados.somar_dias_uteis(date(2026, 10, 8), 3), date(2026, 10, 14))
        # comeca no feriado: conta a partir do dia util seguinte
        self.assertEqual(feriados.somar_dias_uteis(date(2026, 10, 12), 1), date(2026, 10, 14))


if __name__ == "__main__":
    unittest.main()
