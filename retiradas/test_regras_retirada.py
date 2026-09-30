# -*- coding: utf-8 -*-
"""py -3.11 -m unittest retiradas.test_regras_retirada"""
import unittest

from retiradas import regras_retirada as rr

CFG = {"agent_id": 50259, "customer_id": 1}


def _payload(nome_transp="TRANSPORTES S.A. LTDA", doc_transp="12.345.678/0001-90",
             nome_dest="MERCADO BOM (FILIAL 2)", doc_dest="98.765.432/0001-10", data_saida="18/09/2026"):
    detalhe = {"destino": {"nome": nome_dest, "documento": doc_dest}, "quantidade_volumes": 3}
    return rr.montar_payload_retirada("PS-1", "REF", detalhe, {"nome": nome_transp, "documento": doc_transp},
                                      {"apelido": "Fruta Boa", "sender_id": 900, "fator_ponderado": 1.0}, CFG,
                                      data_saida=data_saida)


class TestDadosDaNota(unittest.TestCase):
    """Ida e volta com montar_payload_retirada: se o formato da nota mudar
    la, este teste quebra aqui (o e-mail de "retirado" depende dele)."""

    def test_ida_e_volta(self):
        d = rr.dados_da_nota(_payload())
        self.assertEqual(d, {"quem_retira": "TRANSPORTES S.A. LTDA", "cnpj_quem_retira": "12345678000190",
                             "destinatario": "MERCADO BOM (FILIAL 2)"})

    def test_sem_documentos_e_sem_previsao(self):
        d = rr.dados_da_nota(_payload(doc_transp="", doc_dest="", data_saida=""))
        self.assertEqual((d["quem_retira"], d["cnpj_quem_retira"], d["destinatario"]),
                         ("TRANSPORTES S.A. LTDA", "", "MERCADO BOM (FILIAL 2)"))

    def test_sem_transportadora_vira_cliente(self):
        self.assertEqual(rr.dados_da_nota(_payload(nome_transp="", doc_transp=""))["quem_retira"], "cliente")

    def test_nota_fora_do_formato(self):
        vazio = {"quem_retira": "", "cnpj_quem_retira": "", "destinatario": ""}
        self.assertEqual(rr.dados_da_nota({"note": "qualquer coisa"}), vazio)
        self.assertEqual(rr.dados_da_nota({}), vazio)


if __name__ == "__main__":
    unittest.main()
