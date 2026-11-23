# -*- coding: utf-8 -*-
"""
Layouts reais de boleto (05/10/2026): o número do documento é a NF.
Rodar:  py -3.11 -m unittest documentos_pedido.test_boleto_parser
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from boleto_parser import extrair_metadados_boleto  # noqa: E402

BENEF = "Beneficiário EMBARCADOR TESTE LTDA 66.259.821/0001-55 Agência/Código Beneficiário\n"
PAGADOR = "Pagador CPF/CNPJ do Pagador\nCLIENTE TESTE LTDA CNPJ: 33.764.590/0001-88\n"


def _meta(linha_valores: str, extra: str = "") -> dict:
    texto = (BENEF + "Data do Documento Número do Documento Espécie Doc. Aceite Data do Processamento\n"
             + linha_valores + "\n" + extra + PAGADOR)
    return extrair_metadados_boleto(Path("x.pdf"), texto_pdf=texto)


class LayoutsReais(unittest.TestCase):
    def test_dourado_nf_com_parcela_p01_sem_especie(self):
        m = _meta("17/09/2026 151359P01 N 18/09/2026 26/201728-6")
        self.assertEqual(m["numero_nf"], "151359")
        self.assertEqual(m["parcela_atual"], 1)

    def test_vidaveg_parcela_de_tres_digitos(self):
        m = _meta("10/08/2026 000288185-001 DM N 04/00000117818-1")
        self.assertEqual(m["numero_nf"], "288185")
        self.assertEqual(m["parcela_atual"], 1)

    def test_costa_nilo_letra_no_fim(self):
        m = _meta("02/10/2026 000063849A DM N 05/10/2026 1.448,92")
        self.assertEqual(m["numero_nf"], "63849")
        self.assertIsNone(m["parcela_atual"])

    def test_itaueira_nf_parcela_total(self):
        m = _meta("27/07/2026 8482-1/1 DM NÃO 27/07/2026")
        self.assertEqual((m["numero_nf"], m["parcela_atual"], m["total_parcelas"]), ("8482", 1, 1))

    def test_dourado_antigo_santander(self):
        self.assertEqual(_meta("06/08/2026 149636 DM N 06/08/2026")["numero_nf"], "149636")

    def test_olist_historico_tem_prioridade(self):
        m = _meta("31/07/2026 1040162/01 DM N 31/07/2026", extra="Histórico: Ref. a NF nº 40162, parcela 1\n")
        self.assertEqual(m["numero_nf"], "40162")
        self.assertEqual(m["parcela_atual"], 1)

    def test_de_tommaso_documento_sem_data(self):
        texto = BENEF + "Nº do documento 035880 DM N Processamento 11/08/2026\n" + PAGADOR
        self.assertEqual(extrair_metadados_boleto(Path("x.pdf"), texto_pdf=texto)["numero_nf"], "35880")

    def test_linha_sem_especie_nem_n_nao_vira_nf(self):
        # "Data do Processamento" + valor: não é linha de documento
        m = _meta("05/10/2026 1448 RS 10,00")
        self.assertIsNone(m["numero_nf"])


if __name__ == "__main__":
    unittest.main()
