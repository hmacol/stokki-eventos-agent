# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest documentos_pedido.test_classificador
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import classificador  # noqa: E402

DANFE_FATURADA = """RECEBEMOS DE MARIA DOLORES OS PRODUTOS CONSTANTES DA NOTA FISCAL INDICADA AO LADO
DANFE Documento Auxiliar da Nota Fiscal Eletrônica Nº 040.119
FATURA / DUPLICATA
Num. 001 Venc. 12/11/2026 Valor R$ 3.801,60
CHAVE DE ACESSO 3526 0811 ..."""

BOLETO_DOURADO = """Local de Pagamento Vencimento
PAGAVEL PREFERENCIALMENTE EM CANAIS ELETRONICOS 24/09/2026
Beneficiário LATICINIOS DOURADO 66.259.821/0001-55
Data do Documento Número do Documento Espécie Doc. Aceite Nosso Número
17/09/2026 151359P01 N 18/09/2026 26/201728-6
Recibo do Pagador"""

DANFE_VENCIMENTO_POR_EXTENSO = DANFE_FATURADA.replace(
    "Venc. 12/11/2026", "Vencimento 12/11/2026")

DANFE_E_BOLETO = DANFE_FATURADA + "\n" + BOLETO_DOURADO


def _tipo(nome: str, texto: str) -> str:
    with patch.object(classificador, "_texto_do_pdf", return_value=texto):
        return classificador.classificar_documento(Path(nome))["tipo"]


class Classificacao(unittest.TestCase):
    def test_danfe_faturada_sem_nome_e_nota_fiscal(self):
        self.assertEqual(_tipo("76649_NFS SP.pdf", DANFE_FATURADA), "Nota Fiscal")

    def test_danfe_com_vencimento_por_extenso_e_nota_fiscal(self):
        self.assertEqual(_tipo("76649_NFS SP.pdf", DANFE_VENCIMENTO_POR_EXTENSO), "Nota Fiscal")

    def test_boleto_por_nome_e_texto(self):
        self.assertEqual(_tipo("119050_BOLETOS_1.pdf", BOLETO_DOURADO), "Boleto")

    def test_boleto_com_nome_neutro_pelo_texto(self):
        self.assertEqual(_tipo("34166536632000182388132106.pdf", BOLETO_DOURADO), "Boleto")

    def test_pdf_misto_com_nome_de_boleto_continua_boleto(self):
        self.assertEqual(_tipo("76803_DANFEs_Boletos_2026-08-11 (1)_NF3.pdf", DANFE_E_BOLETO), "Boleto")

    def test_danfe_pelo_nome(self):
        self.assertEqual(_tipo("PS-40896_DANFE.pdf", DANFE_FATURADA), "Nota Fiscal")


if __name__ == "__main__":
    unittest.main()
