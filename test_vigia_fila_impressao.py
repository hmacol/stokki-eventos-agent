# -*- coding: utf-8 -*-
"""Testes das regras do vigia da fila da Estação de Impressão."""
import sqlite3
import unittest
from datetime import datetime

from vigia_fila_impressao import (EM_ESPERA, FATURADO, atrasados, corpo_email, marcar_avisados,
                                  separar_novos, texto_whatsapp)

AGORA = datetime(2026, 10, 22, 18, 35)


def _p(codigo, desde, cliente="ITAUEIRA CAMAROES LTDA"):
    return {"codigo_ps": f"#{codigo}", "desde": desde, "cliente": cliente, "transportadora": "FRESHLOG"}


class TestAtrasados(unittest.TestCase):
    def test_faturado_ha_mais_de_2h_entra_e_o_recente_nao(self):
        pedidos = [_p("PS-42383", datetime(2026, 10, 22, 16, 1)),    # 2h34
                   _p("PS-42390", datetime(2026, 10, 22, 17, 0))]    # 1h35
        self.assertEqual([p["codigo_ps"] for p in atrasados(pedidos, AGORA, 2, set())], ["#PS-42383"])

    def test_em_espera_24h_e_ordem_do_mais_antigo(self):
        pedidos = [_p("PS-38317", datetime(2026, 9, 1, 18, 46)),
                   _p("PS-42389", datetime(2026, 10, 22, 16, 14)),
                   _p("PS-35908", datetime(2026, 8, 4, 20, 40))]
        self.assertEqual([p["codigo_ps"] for p in atrasados(pedidos, AGORA, 24, set())],
                         ["#PS-35908", "#PS-38317"])

    def test_sem_data_entra(self):
        self.assertEqual(len(atrasados([_p("PS-1", None)], AGORA, 2, set())), 1)

    def test_ignorar_codigos(self):
        pedidos = [_p("PS-35908", datetime(2026, 8, 4, 20, 40))]
        self.assertEqual(atrasados(pedidos, AGORA, 24, {"PS-35908"}), [])


class TestUmAvisoPorDia(unittest.TestCase):
    def test_avisado_hoje_nao_volta_e_amanha_volta(self):
        conn = sqlite3.connect(":memory:")
        pedidos = [_p("PS-42383", None), _p("PS-42384", None)]
        marcar_avisados(conn, "2026-10-22", FATURADO, pedidos[:1], AGORA)
        self.assertEqual([p["codigo_ps"] for p in separar_novos(conn, "2026-10-22", FATURADO, pedidos)],
                         ["#PS-42384"])
        self.assertEqual(len(separar_novos(conn, "2026-10-22", EM_ESPERA, pedidos)), 2)
        self.assertEqual(len(separar_novos(conn, "2026-10-23", FATURADO, pedidos)), 2)


class TestTextos(unittest.TestCase):
    def test_whatsapp_cabe_no_limite_e_resume_o_resto(self):
        faturados = [_p(f"PS-{40000 + i}", None) for i in range(30)]
        em_espera = [_p(f"PS-{30000 + i}", None) for i in range(30)]
        texto = texto_whatsapp(faturados, em_espera, 2, 24, 200)
        self.assertLessEqual(len(texto), 200)
        self.assertIn("30 faturado há +2h sem imprimir", texto)
        self.assertIn("30 em espera há +24h sem faturar", texto)
        self.assertIn("e mais", texto)
        self.assertIn("Detalhes no e-mail.", texto)

    def test_whatsapp_so_com_faturados(self):
        texto = texto_whatsapp([_p("PS-42383", None)], [], 2, 24, 200)
        self.assertIn("PS-42383", texto)
        self.assertNotIn("em espera", texto)

    def test_email_lista_os_dois_blocos(self):
        corpo = corpo_email([_p("PS-42383", datetime(2026, 10, 22, 16, 1))],
                            [_p("PS-35908", datetime(2026, 8, 4, 20, 40), cliente="PADRAO <PURO>")],
                            2, 24, AGORA)
        self.assertIn("PS-42383", corpo)
        self.assertIn("22/10 16:01", corpo)
        self.assertIn("PADRAO &lt;PURO&gt;", corpo)
        self.assertIn("79 dias", corpo)


if __name__ == "__main__":
    unittest.main()
