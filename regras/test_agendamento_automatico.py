# -*- coding: utf-8 -*-
"""
Regra pura do agendamento automatico (spec 2026-11-02, opcao A do Hugo).
Rodar (da raiz): py -3.11 -m unittest regras.test_agendamento_automatico -v
"""
import unittest
from datetime import date, datetime

from regras import agendamento_automatico as aa


def dt(d, h, m=0):
    return datetime(2026, 11, d, h, m)


class TestData(unittest.TestCase):
    def test_corte(self):
        self.assertEqual(aa.data_automatica(dt(9, 13, 59)), date(2026, 11, 10))  # seg antes -> ter
        self.assertEqual(aa.data_automatica(dt(9, 14, 10)), date(2026, 11, 10))  # rodada 14h05 conta antes
        self.assertEqual(aa.data_automatica(dt(9, 15, 5)), date(2026, 11, 11))   # seg depois -> qua
        self.assertEqual(aa.data_automatica(dt(9, 16, 0)), date(2026, 11, 11))   # sequencia 16h
        self.assertEqual(aa.data_automatica(dt(9, 22, 0)), date(2026, 11, 11))   # sequencia 22h

    def test_fim_de_semana_e_sexta(self):
        self.assertEqual(aa.data_automatica(dt(7, 10)), date(2026, 11, 10))      # sab -> ter
        self.assertEqual(aa.data_automatica(dt(13, 15)), date(2026, 11, 17))     # sex depois -> ter

    def test_vespera_de_feriado(self):  # 20/11 sexta = Consciencia Negra
        self.assertEqual(aa.data_automatica(dt(19, 10)), date(2026, 11, 23))
        self.assertEqual(aa.data_automatica(dt(19, 15)), date(2026, 11, 24))

    def test_data_de_saida(self):
        self.assertEqual(aa.data_automatica(dt(9, 10), "12/11/2026"), date(2026, 11, 12))  # posterior vence
        self.assertEqual(aa.data_automatica(dt(9, 10), "14/11/2026"), date(2026, 11, 16))  # sab rola p/ seg
        self.assertEqual(aa.data_automatica(dt(9, 10), "09/11/2026"), date(2026, 11, 10))  # anterior ignora
        self.assertEqual(aa.data_automatica(dt(9, 10), "lixo"), date(2026, 11, 10))


class TestJanela(unittest.TestCase):
    def test_prioridades(self):
        self.assertEqual(aa.janela_automatica({"inicio": "09:00", "fim": "12:00"}, "123", {"123": ("07:00", "10:00")}, {}), ("09:00", "12:00"))
        self.assertEqual(aa.janela_automatica(None, "12.3", {"123": ("07:00", "10:00")}, {}), ("07:00", "10:00"))
        self.assertEqual(aa.janela_automatica({"inicio": "", "fim": ""}, "999", {}, {}), aa.DIA_TODO)

    def test_janela_invalida_cai_pra_proxima(self):  # revisao final 02/11
        planilha = {"123": ("07:00", "10:00")}
        # mensagem invalida (fim antes do inicio, igual, formato ruim) -> horario do destinatario
        for msg in ({"inicio": "12:00", "fim": "09:00"}, {"inicio": "09:00", "fim": "09:00"},
                    {"inicio": "9h", "fim": "12:00"}, {"inicio": "25:00", "fim": "26:00"}):
            self.assertEqual(aa.janela_automatica(msg, "123", planilha, {}), ("07:00", "10:00"), msg)
        # destinatario invalido (planilha ou ajuste manual) -> dia todo
        self.assertEqual(aa.janela_automatica(None, "123", {"123": ("16:00", "08:00")}, {}), aa.DIA_TODO)
        self.assertEqual(aa.janela_automatica(None, "123", {"123": (None, "08:00")}, {}), aa.DIA_TODO)
        ajuste = {"123": {"horario_atendimento_inicio": "18:00", "horario_atendimento_fim": "07:00"}}
        self.assertEqual(aa.janela_automatica(None, "123", planilha, ajuste), aa.DIA_TODO)
        # mensagem e destinatario invalidos -> dia todo
        self.assertEqual(aa.janela_automatica({"inicio": "12:00", "fim": "09:00"}, "123",
                                              {"123": ("16:00", "08:00")}, {}), aa.DIA_TODO)


if __name__ == "__main__":
    unittest.main()
