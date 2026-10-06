# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_regras_documentos
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from regras_documentos import cobranca_da_danfe, boleto_esperado  # noqa: E402

DOURADO = 11426239
QUATRO_ESTRELAS = 21785428


class Cobranca(unittest.TestCase):
    def test_nfephp_com_duplicata(self):
        t = "FATURA / DUPLICATA\nNum. 001\nVenc. 21/10/2026\nValor R$ 1.125,60\nCÁLCULO DO IMPOSTO"
        self.assertEqual(cobranca_da_danfe(t), 1)

    def test_tres_parcelas(self):
        t = ("FATURA / DUPLICATA\nNum. 001 Num. 002 Num. 003\nVenc. 22/10/2026 Venc. 22/11/2026 "
             "Venc. 22/12/2026\nValor R$ 532,16\nCÁLCULO DO IMPOSTO")
        self.assertEqual(cobranca_da_danfe(t), 1)

    def test_layout_tabela(self):
        t = "FATURA / DUPLICATAS\nNº DUPLICATA VENC. VALOR\n001 31/08/2026 1.342,86\nBASE DE CÁLCULO DO ICMS"
        self.assertEqual(cobranca_da_danfe(t), 1)

    def test_quadro_vazio(self):
        t = "FATURA / DUPLICATA\nNÚMERO VENCIMENTO VALOR\nCÁLCULO DO IMPOSTO\nVenc. 01/01/2027"
        self.assertEqual(cobranca_da_danfe(t), 0)   # o "Venc." depois do quadro não conta

    def test_sem_quadro(self):
        self.assertIsNone(cobranca_da_danfe("DANFE Nº 178130 CHAVE DE ACESSO ..."))
        self.assertIsNone(cobranca_da_danfe(""))


class BoletoEsperado(unittest.TestCase):
    def test_duplicata_manda(self):
        self.assertTrue(boleto_esperado(QUATRO_ESTRELAS, [1]))

    def test_quadro_vazio_dispensa_mesmo_na_lista(self):
        self.assertFalse(boleto_esperado(DOURADO, [0]))

    def test_sem_quadro_cai_na_lista_do_hugo(self):
        self.assertTrue(boleto_esperado(DOURADO, [None]))
        self.assertTrue(boleto_esperado(DOURADO, []))          # sem NF nenhuma

    def test_sem_quadro_fora_da_lista_nao_acusa(self):
        self.assertFalse(boleto_esperado(QUATRO_ESTRELAS, [None]))
        self.assertFalse(boleto_esperado(None, []))

    def test_duas_nfs_uma_com_duplicata(self):
        self.assertTrue(boleto_esperado(QUATRO_ESTRELAS, [0, 1]))

    def test_menos_um_e_sem_quadro(self):
        self.assertFalse(boleto_esperado(QUATRO_ESTRELAS, [-1]))
        self.assertTrue(boleto_esperado(DOURADO, [-1]))


if __name__ == "__main__":
    unittest.main()
