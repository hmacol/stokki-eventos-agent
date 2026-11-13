# -*- coding: utf-8 -*-
import io
import unittest

from pypdf import PdfReader

from coletas import folha_pdf


def _coleta(n):
    return {"numero": "COL-7", "criada_em": "2026-11-12 14:05:00", "criada_por": "expedicao",
            "transportadora": "CLIENTE RETIRA 15.060.670/0001-70", "embarcador": "PADRAO PURO LTDA",
            "pedidos": [{"codigo": f"PS-{43000 + i}", "embarcador": "PADRAO PURO LTDA ... #stkkc-2",
                         "nf": str(1000 + i) if i % 2 else None,
                         "destinatario": "PADARIA E CONFEITARIA DO ZE DA ESQUINA LTDA" if i % 2 else None,
                         "unidades": 12 if i % 3 else None} for i in range(n)]}


class TestFolhaPdf(unittest.TestCase):
    def test_uma_pagina_com_1_e_com_30_pedidos(self):
        for n in (1, 30):
            pdf = folha_pdf.gerar(_coleta(n))
            self.assertTrue(pdf.startswith(b"%PDF"), n)
            pagina = PdfReader(io.BytesIO(pdf)).pages
            self.assertEqual(len(pagina), 1, n)
            # A4 em pe (595 x 842 pt)
            self.assertAlmostEqual(float(pagina[0].mediabox.width), 595, delta=2)

    def test_trinta_linhas_nao_invadem_o_rodape(self):
        # 30 linhas + a linha do total (12 de respiro + 30 de altura) terminam antes do rodape
        fim = folha_pdf.Y_LINHAS + folha_pdf.MAX_LINHAS * folha_pdf.ALT_LINHA + 12 + 30
        self.assertLess(fim, folha_pdf.Y_RODAPE)


if __name__ == "__main__":
    unittest.main()
