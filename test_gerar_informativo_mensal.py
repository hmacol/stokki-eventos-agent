# -*- coding: utf-8 -*-
"""Rodar (da raiz): py -3.11 -m unittest test_gerar_informativo_mensal -v"""
import unittest
from datetime import date

import gerar_informativo_mensal as gim
import regioes_atendimento as ra


class TestMesAlvo(unittest.TestCase):
    def test_dia_1_e_o_proprio_mes(self):
        self.assertEqual(gim.mes_alvo(date(2026, 11, 1)), (2026, 11))

    def test_fim_do_mes_ja_mira_o_seguinte(self):
        self.assertEqual(gim.mes_alvo(date(2026, 10, 25)), (2026, 11))
        self.assertEqual(gim.mes_alvo(date(2026, 12, 28)), (2027, 1))

    def test_proximo_forca_o_seguinte(self):
        self.assertEqual(gim.mes_alvo(date(2026, 10, 7), proximo=True), (2026, 11))


class TestHtml(unittest.TestCase):
    def test_html_autossuficiente_com_logo_e_links_absolutos(self):
        html, d = gim.renderizar_html(2026, 11, date(2026, 10, 7))
        self.assertEqual(d["titulo_mes"], "Novembro de 2026")
        self.assertIn("Novembro de 2026", html)
        self.assertIn('src="data:image/png;base64,', html)
        self.assertIn(f'href="{ra.URL_PUBLICA}?mes=2026-10"', html)
        self.assertIn(f'href="{ra.URL_PUBLICA}?mes=2026-12"', html)
        self.assertNotIn("url_for", html)
        # feriados de novembro em dia util: 02/11 (segunda) e 20/11 (sexta)
        self.assertIn("02/11 (segunda) é feriado", " ".join(d["notas"]))
        self.assertIn("20/11 (sexta) é feriado", " ".join(d["notas"]))

    def test_corpo_do_email_lista_notas_quinzenais_e_link(self):
        _, d = gim.renderizar_html(2026, 11, date(2026, 10, 7))
        corpo = gim.corpo_email(d, ra.url_do_mes(2026, 11), tem_pdf=True)
        self.assertIn("Dias de entrega: Novembro de 2026", corpo)
        self.assertIn("PDF e HTML em anexo", corpo)
        self.assertIn("Sorocaba", corpo)
        self.assertIn("Piracicaba", corpo)
        self.assertIn(ra.url_do_mes(2026, 11), corpo)


class TestDestinos(unittest.TestCase):
    def test_sem_config_vai_pro_hugo(self):
        self.assertEqual(gim.destinos({}, modo_teste=False), [gim.EMAIL_TESTE])

    def test_forcar_destino_vazio_libera_a_lista(self):
        cfg = {"informativo_mensal": {"forcar_destino": "", "destinatarios": ["a@x.com", "b@x.com"]}}
        self.assertEqual(gim.destinos(cfg, modo_teste=False), ["a@x.com", "b@x.com"])
        self.assertEqual(gim.destinos(cfg, modo_teste=True), [gim.EMAIL_TESTE])


class TestViradaDeMes(unittest.TestCase):
    def test_fora_da_janela_e_none(self):
        self.assertIsNone(ra.virada_de_mes(date(2026, 10, 7)))
        self.assertIsNone(ra.virada_de_mes(date(2026, 10, 24)))

    def test_ultima_semana_traz_o_mes_seguinte(self):
        v = ra.virada_de_mes(date(2026, 10, 25))
        self.assertEqual((v["mes"], v["titulo_mes"], v["dias_para_virar"]), ("2026-11", "Novembro de 2026", 7))
        self.assertEqual([f["data_br"] for f in v["feriados"]], ["02/11", "20/11"])
        self.assertEqual(len(v["notas"]), 2)
        self.assertEqual([q["nome"] for q in v["quinzenais"]], ["Sorocaba", "Piracicaba"])
        self.assertEqual(v["url"], ra.URL_PUBLICA + "?mes=2026-11")

    def test_dezembro_vira_janeiro(self):
        v = ra.virada_de_mes(date(2026, 12, 31))
        self.assertEqual((v["mes"], v["dias_para_virar"]), ("2027-01", 1))


if __name__ == "__main__":
    unittest.main()
