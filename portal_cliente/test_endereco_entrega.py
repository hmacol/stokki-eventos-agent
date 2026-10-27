# -*- coding: utf-8 -*-
"""
Endereco de entrega escolhido no envio do portal (spec 2026-10-27): regras
puras -- validar pelo CEP, aplicar no pedido, inserir <entrega> no XML.

    py -3.11 -m unittest portal_cliente.test_endereco_entrega -v
"""
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import endereco_entrega as ee  # noqa: E402
import envio_pedidos as ep  # noqa: E402
from test_endereco_entrega_xml import _xml  # noqa: E402

NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
CEP_HADDOCK = {"cep": "01414-001", "logradouro": "Rua Haddock Lobo", "bairro": "Cerqueira César",
               "cidade": "São Paulo", "uf": "SP", "ibge": "3550308"}
END = {"cep": "01414001", "logradouro": "Rua Haddock Lobo", "numero": "595", "complemento": "loja 2",
       "bairro": "Cerqueira César", "municipio": "São Paulo", "uf": "SP", "cmun": "3550308"}


def _consulta(resposta):
    return lambda cep: resposta


class MontarEndereco(unittest.TestCase):
    def test_montar_usa_municipio_uf_ibge_do_cep(self):
        end = ee.montar_endereco({"cep": "01414-001", "numero": "595", "complemento": "loja 2", "municipio": "Outro"},
                                 consultar=_consulta(CEP_HADDOCK))
        self.assertEqual(end, END)

    def test_montar_cep_geral_usa_rua_do_cliente(self):
        geral = {**CEP_HADDOCK, "cep": "13870-000", "logradouro": "", "bairro": "", "cidade": "São João da Boa Vista",
                 "ibge": "3549102"}
        end = ee.montar_endereco({"cep": "13870000", "numero": "10", "logradouro": "Rua das Flores", "bairro": "Centro"},
                                 consultar=_consulta(geral))
        self.assertEqual((end["logradouro"], end["bairro"], end["cmun"]), ("Rua das Flores", "Centro", "3549102"))

    def test_montar_erros(self):
        with self.assertRaisesRegex(ee.ErroEndereco, "^CEP não encontrado$"):
            ee.montar_endereco({"cep": "01414001", "numero": "1"}, consultar=_consulta(None))
        with self.assertRaisesRegex(ee.ErroEndereco, "^CEP não encontrado$"):
            ee.montar_endereco({"cep": "123", "numero": "1"}, consultar=_consulta(CEP_HADDOCK))
        with self.assertRaisesRegex(ee.ErroEndereco, "código de município"):
            ee.montar_endereco({"cep": "01414001", "numero": "1"}, consultar=_consulta({**CEP_HADDOCK, "ibge": ""}))
        with self.assertRaisesRegex(ee.ErroEndereco, "^Informe o número$"):
            ee.montar_endereco({"cep": "01414001", "numero": " "}, consultar=_consulta(CEP_HADDOCK))
        with self.assertRaisesRegex(ee.ErroEndereco, "^Informe a rua$"):
            ee.montar_endereco({"cep": "01414001", "numero": "1"}, consultar=_consulta({**CEP_HADDOCK, "logradouro": ""}))


class AplicarEntrega(unittest.TestCase):
    def test_aplicar_troca_endereco_e_guarda_fiscal(self):
        nfe = ep.ler_nfe(_xml(""))
        novo = ee.aplicar_entrega(nfe, END)
        self.assertEqual(novo["destinatario_endereco"], "Rua Haddock Lobo, 595 - loja 2")
        self.assertEqual((novo["destinatario_bairro"], novo["destinatario_municipio"], novo["destinatario_uf"],
                          novo["destinatario_cep"]), ("Cerqueira César", "São Paulo", "SP", "01414001"))
        self.assertEqual(novo["endereco_fiscal"], "RUA IVAI, 207, TATUAPE, SAO PAULO/SP, CEP 03080-010")
        self.assertEqual(novo["destinatario_telefone"], nfe["destinatario_telefone"])
        self.assertEqual(nfe["destinatario_cep"], "03080010")  # o original não muda


class InserirEntregaXml(unittest.TestCase):
    def _relido(self, xml: bytes) -> dict:
        return ep.ler_nfe(ee.inserir_entrega_xml(xml, END, "51322677000187", "DESTINATARIO TESTE"))

    def test_inserir_depois_de_dest(self):
        nfe = self._relido(_xml(""))
        self.assertEqual(nfe["destinatario_endereco"], "Rua Haddock Lobo, 595 - loja 2")
        self.assertEqual(nfe["destinatario_cep"], "01414001")
        self.assertEqual(nfe["endereco_fiscal"], "RUA IVAI, 207, TATUAPE, SAO PAULO/SP, CEP 03080-010")

    def test_inserir_depois_de_retirada(self):
        xml = _xml("<retirada><CNPJ>22135070000190</CNPJ><xLgr>Rua A</xLgr><nro>1</nro><xBairro>B</xBairro>"
                   "<cMun>3550308</cMun><xMun>Sao Paulo</xMun><UF>SP</UF></retirada>")
        saida = ee.inserir_entrega_xml(xml, END, "51322677000187", "DEST")
        self.assertIn(b"</retirada><entrega>", saida)
        self.assertEqual(ep.ler_nfe(saida)["destinatario_cep"], "01414001")

    def test_inserir_com_prefixo(self):
        xml = (b'<?xml version="1.0"?><nfe:NFe xmlns:nfe="http://www.portalfiscal.inf.br/nfe"><nfe:infNFe>'
               b"<nfe:dest><nfe:CNPJ>1</nfe:CNPJ></nfe:dest><nfe:det/></nfe:infNFe></nfe:NFe>")
        saida = ee.inserir_entrega_xml(xml, END, "51322677000187", "DEST")
        self.assertIn(b"</nfe:dest><nfe:entrega><nfe:CNPJ>51322677000187</nfe:CNPJ>", saida)
        entrega = ET.fromstring(saida).find("nfe:infNFe/nfe:entrega", NS)
        self.assertEqual(entrega.find("nfe:cMun", NS).text, "3550308")

    def test_inserir_escapa_texto(self):
        end = {**END, "complemento": 'Doca 2 & 3 <fundos> "B"'}
        saida = ee.inserir_entrega_xml(_xml(""), end, "51322677000187", "Bar & Cia")
        raiz = ET.fromstring(saida)
        entrega = raiz.find(".//nfe:entrega", NS)
        self.assertEqual(entrega.find("nfe:xCpl", NS).text, 'Doca 2 & 3 <fundos> "B"')
        self.assertEqual(entrega.find("nfe:xNome", NS).text, "Bar & Cia")

    def test_inserir_ordem_do_layout(self):
        saida = ee.inserir_entrega_xml(_xml(""), END, "51322677000187", "DEST")
        entrega = ET.fromstring(saida).find(".//nfe:entrega", NS)
        tags = [c.tag.split("}")[1] for c in entrega]
        self.assertEqual(tags, ["CNPJ", "xNome", "xLgr", "nro", "xCpl", "xBairro", "cMun", "xMun", "UF", "CEP"])

    def test_inserir_sem_complemento_omite_xcpl(self):
        saida = ee.inserir_entrega_xml(_xml(""), {**END, "complemento": ""}, "51322677000187", "DEST")
        self.assertNotIn(b"<xCpl>", saida)

    def test_inserir_recusa_se_ja_tem_entrega(self):
        with self.assertRaises(ValueError):
            ee.inserir_entrega_xml(_xml(), END, "51322677000187", "DEST")

    def test_inserir_cpf(self):
        saida = ee.inserir_entrega_xml(_xml(""), END, "12345678901", "Fulano")
        self.assertIn(b"<entrega><CPF>12345678901</CPF>", saida)


class TextoDaVuupt(unittest.TestCase):
    def test_cep_e_numero_de_texto(self):
        t = "Rua Bela Cintra, 1200, Consolação, São Paulo - SP, 01415-000, Brasil"
        self.assertEqual((ee.cep_de_texto(t), ee.numero_de_texto(t)), ("01415000", "1200"))
        self.assertEqual(ee.cep_de_texto("Rua Bela Cintra, 1200, São Paulo - SP, Brasil"), "")
        self.assertEqual(ee.numero_de_texto("Rua Sem Numero, Centro, São Paulo - SP"), "")


class ConsultaCepComIbge(unittest.TestCase):
    def test_viacep_devolve_ibge_e_brasilapi_vazio(self):
        from unittest import mock
        import cotacao
        via = {"cep": "01414-001", "logradouro": "Rua Haddock Lobo", "bairro": "Cerqueira César",
               "localidade": "São Paulo", "uf": "SP", "ibge": "3550308"}
        with mock.patch.dict(cotacao._CACHE_CEP, clear=True), \
             mock.patch.object(cotacao, "_http_json", side_effect=lambda url: via if "viacep" in url else None):
            self.assertEqual(cotacao.consultar_cep("01414001")["ibge"], "3550308")
        bra = {"city": "São Paulo", "state": "SP", "street": "Rua X", "neighborhood": "Y"}
        with mock.patch.dict(cotacao._CACHE_CEP, clear=True), \
             mock.patch.object(cotacao, "_http_json", side_effect=lambda url: bra if "brasilapi" in url else None):
            self.assertEqual(cotacao.consultar_cep("01414001")["ibge"], "")


class Chave(unittest.TestCase):
    def test_entrega_ativa_padrao_desligada(self):
        self.assertIs(ee.entrega_ativa({}), False)
        self.assertIs(ee.entrega_ativa(None), False)
        self.assertIs(ee.entrega_ativa({"portal_cliente": {"envios": {"endereco_entrega_ativo": True}}}), True)


if __name__ == "__main__":
    unittest.main()
