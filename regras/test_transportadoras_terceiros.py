# -*- coding: utf-8 -*-
"""
Lista de transportadoras TERCEIROS e endereco de redespacho pelo nome
escolhido -- base da opcao "Transportadora (redespacho)" do menu de
contexto da tela de planejamento (Hugo, 16/09). Rodar com:
    py -3.11 -m unittest regras.test_transportadoras_terceiros -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from regras.transportadoras import (  # noqa: E402
    CatalogoTransportadoras, EnderecoRedespacho, _Entrada, _normalizar,
)


def _entrada(nome, tipo="TERCEIROS", municipio="", bairro="", logradouro="",
             numero="", complemento="", cep=""):
    end = None
    if tipo == "TERCEIROS":
        end = EnderecoRedespacho(uf="SP", municipio=municipio, bairro=bairro,
                                 logradouro=logradouro, numero=numero,
                                 complemento=complemento, cep=cep)
    return _Entrada(nome_original=nome, nome_normalizado=_normalizar(nome), cnpj="",
                    tipo=tipo, endereco=end)


def _catalogo():
    return CatalogoTransportadoras([
        _entrada("KANEJO", municipio="São Paulo", bairro="Jardim Japão",
                 logradouro="Rua Osaka", numero="880", complemento="KANEJO", cep="02124-040"),
        _entrada("Transfrios Transportes Ltda", municipio="Itapecerica da Serra", bairro="Potuvera",
                 logradouro="Est. Francisco Hengles", numero="591", complemento="TRANSFRIOS",
                 cep="06885-160"),
        # mesma transportadora, grafia repetida na planilha: a lista nao duplica
        _entrada("Transfrios Transportes Ltda", municipio="Itapecerica da Serra", bairro="Potuvera",
                 logradouro="Est. Francisco Hengles", numero="591", complemento="TRANSFRIOS",
                 cep="06885-160"),
        # TERCEIROS sem endereco preenchido: nao serve pra redespacho
        _entrada("SEM ENDERECO LTDA", logradouro="", numero=""),
        _entrada("CLIENTE RETIRA", tipo="RETIRADA"),
        _entrada("FRESHLOG", tipo="ENTREGA"),
    ])


class ListarTerceirosTestCase(unittest.TestCase):

    def test_lista_so_terceiros_com_endereco_em_ordem_alfabetica(self):
        lista = _catalogo().listar_terceiros()

        self.assertEqual([t["nome"] for t in lista], ["KANEJO", "Transfrios Transportes Ltda"])

    def test_cada_item_traz_o_endereco_formatado(self):
        lista = _catalogo().listar_terceiros()

        self.assertEqual(
            lista[0]["endereco"],
            "Rua Osaka, 880, KANEJO, Jardim Japão, São Paulo - SP, 02124-040",
        )


class EnderecoTerceirosTestCase(unittest.TestCase):

    def test_nome_exato_devolve_o_endereco(self):
        end = _catalogo().endereco_terceiros("KANEJO")

        self.assertIsNotNone(end)
        self.assertEqual(end.logradouro, "Rua Osaka")
        self.assertEqual(end.numero, "880")

    def test_nome_tolera_caixa_e_espacos(self):
        end = _catalogo().endereco_terceiros("  transfrios transportes ltda ")

        self.assertIsNotNone(end)
        self.assertEqual(end.numero, "591")

    def test_nome_de_retirada_ou_entrega_nao_serve(self):
        self.assertIsNone(_catalogo().endereco_terceiros("CLIENTE RETIRA"))
        self.assertIsNone(_catalogo().endereco_terceiros("FRESHLOG"))

    def test_terceiros_sem_endereco_devolve_none(self):
        self.assertIsNone(_catalogo().endereco_terceiros("SEM ENDERECO LTDA"))

    def test_nome_desconhecido_devolve_none(self):
        self.assertIsNone(_catalogo().endereco_terceiros("NAO EXISTE"))
        self.assertIsNone(_catalogo().endereco_terceiros(""))


if __name__ == "__main__":
    unittest.main()
