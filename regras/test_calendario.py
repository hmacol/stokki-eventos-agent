# -*- coding: utf-8 -*-
"""Testes do calendario unico de dias uteis (regras/calendario.py).
Rodar: py -3.11 -m unittest regras.test_calendario"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from regras import calendario as cal  # noqa: E402


class TestFeriados(unittest.TestCase):
    def setUp(self):
        cal.recarregar_extras()

    def tearDown(self):
        cal.recarregar_extras()

    def test_fixos_nacionais_e_sp(self):
        f = cal.feriados(2026)
        for d in (date(2026, 1, 1), date(2026, 1, 25), date(2026, 4, 21), date(2026, 5, 1),
                  date(2026, 7, 9), date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 2),
                  date(2026, 11, 15), date(2026, 11, 20), date(2026, 12, 25)):
            self.assertIn(d, f, d)

    def test_moveis_2026(self):
        # Pascoa 2026 = 05/04
        f = cal.feriados(2026)
        self.assertEqual(f[date(2026, 2, 16)], "Carnaval (segunda)")
        self.assertEqual(f[date(2026, 2, 17)], "Carnaval (terca)")
        self.assertEqual(f[date(2026, 4, 3)], "Sexta-feira Santa")
        self.assertEqual(f[date(2026, 6, 4)], "Corpus Christi")

    def test_moveis_2027(self):
        # Pascoa 2027 = 28/03
        f = cal.feriados(2027)
        self.assertIn(date(2027, 2, 8), f)
        self.assertIn(date(2027, 3, 26), f)
        self.assertIn(date(2027, 5, 27), f)

    def test_dia_comum_nao_e_feriado(self):
        self.assertFalse(cal.eh_feriado(date(2026, 10, 5)))
        self.assertIsNone(cal.nome_feriado(date(2026, 10, 5)))

    def test_aceita_datetime(self):
        self.assertTrue(cal.eh_feriado(datetime(2026, 12, 25, 10, 0)))
        self.assertFalse(cal.eh_dia_util(datetime(2026, 12, 25, 10, 0)))

    def test_extras_incluir_e_excluir(self):
        with tempfile.TemporaryDirectory() as tmp:
            arq = Path(tmp) / "feriados.json"
            arq.write_text(json.dumps({"incluir": {"2026-11-16": "Emenda"},
                                       "excluir": ["2026-02-17"]}), encoding="utf-8")
            with mock.patch.object(cal, "_ARQ_EXTRAS", arq):
                cal.recarregar_extras()
                self.assertEqual(cal.nome_feriado(date(2026, 11, 16)), "Emenda")
                self.assertFalse(cal.eh_feriado(date(2026, 2, 17)))
                self.assertTrue(cal.eh_feriado(date(2026, 2, 16)))

    def test_json_quebrado_nao_derruba(self):
        with tempfile.TemporaryDirectory() as tmp:
            arq = Path(tmp) / "feriados.json"
            arq.write_text("{isso nao e json", encoding="utf-8")
            with mock.patch.object(cal, "_ARQ_EXTRAS", arq):
                cal.recarregar_extras()
                self.assertTrue(cal.eh_feriado(date(2026, 12, 25)))


class TestDiasUteis(unittest.TestCase):
    def setUp(self):
        cal.recarregar_extras()

    def test_fim_de_semana(self):
        self.assertFalse(cal.eh_dia_util(date(2026, 10, 3)))  # sabado
        self.assertFalse(cal.eh_dia_util(date(2026, 10, 4)))  # domingo
        self.assertTrue(cal.eh_dia_util(date(2026, 10, 5)))   # segunda

    def test_feriado_no_meio_da_semana_nao_e_util(self):
        self.assertFalse(cal.eh_dia_util(date(2026, 10, 12)))  # segunda, N. S. Aparecida

    def test_proximo_dia_util_pula_feriado(self):
        # sexta 09/10 -> segunda 12/10 e feriado -> terca 13/10
        self.assertEqual(cal.proximo_dia_util(date(2026, 10, 9)), date(2026, 10, 13))

    def test_proximo_dia_util_inclusive(self):
        self.assertEqual(cal.proximo_dia_util(date(2026, 10, 5), inclusive=True), date(2026, 10, 5))
        self.assertEqual(cal.proximo_dia_util(date(2026, 10, 12), inclusive=True), date(2026, 10, 13))
        self.assertEqual(cal.proximo_dia_util(date(2026, 10, 5)), date(2026, 10, 6))

    def test_dia_util_anterior_pula_feriado_e_fim_de_semana(self):
        # terca 13/10 -> seg 12/10 feriado -> dom, sab -> sexta 09/10
        self.assertEqual(cal.dia_util_anterior(date(2026, 10, 13)), date(2026, 10, 9))
        self.assertEqual(cal.dia_util_anterior(date(2026, 10, 13), inclusive=True), date(2026, 10, 13))

    def test_somar_dias_uteis_date(self):
        # quinta 08/10 + 3 d.u. = sex 09, ter 13, qua 14
        self.assertEqual(cal.somar_dias_uteis(date(2026, 10, 8), 3), date(2026, 10, 14))

    def test_somar_dias_uteis_date_comeca_em_feriado(self):
        # 12/10 (feriado) conta a partir de 13/10; +1 = 14/10
        self.assertEqual(cal.somar_dias_uteis(date(2026, 10, 12), 1), date(2026, 10, 14))
        self.assertEqual(cal.somar_dias_uteis(date(2026, 10, 12), 0), date(2026, 10, 13))

    def test_somar_dias_uteis_datetime_mantem_horario(self):
        ini = datetime(2026, 10, 9, 15, 30)  # sexta
        self.assertEqual(cal.somar_dias_uteis(ini, 1), datetime(2026, 10, 13, 15, 30))

    def test_somar_dias_uteis_datetime_fim_de_semana_zera_horario(self):
        ini = datetime(2026, 10, 10, 15, 30)  # sabado
        self.assertEqual(cal.somar_dias_uteis(ini, 1), datetime(2026, 10, 14, 0, 0))

    def test_somar_dias_uteis_preserva_fuso(self):
        ini = datetime(2026, 10, 9, 15, 30, tzinfo=timezone.utc)
        fim = cal.somar_dias_uteis(ini, 1)
        self.assertEqual(fim.tzinfo, timezone.utc)
        self.assertEqual(fim, datetime(2026, 10, 13, 15, 30, tzinfo=timezone.utc))

    def test_contar_dias_uteis(self):
        self.assertEqual(cal.contar_dias_uteis(date(2026, 10, 9), date(2026, 10, 14)), 2)  # 13 e 14
        self.assertEqual(cal.contar_dias_uteis(date(2026, 10, 9), date(2026, 10, 9)), 0)
        self.assertEqual(cal.contar_dias_uteis(date(2026, 10, 14), date(2026, 10, 9)), 0)


if __name__ == "__main__":
    unittest.main()
