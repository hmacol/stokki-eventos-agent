# -*- coding: utf-8 -*-
"""
Batimento de endereço da VUUPT contra os pontos de redespacho da planilha
(regras/transportadoras.py::resolver_por_endereco). Rodar com:
    python -m pytest regras/test_transportadoras_endereco.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from regras.transportadoras import (  # noqa: E402
    CatalogoTransportadoras, EnderecoRedespacho, _Entrada, _normalizar,
)


def _entrada(nome, tipo="TERCEIROS", uf="SP", municipio="", bairro="", logradouro="",
             numero="", complemento="", cep="", email=""):
    end = None
    if tipo == "TERCEIROS":
        end = EnderecoRedespacho(uf=uf, municipio=municipio, bairro=bairro, logradouro=logradouro,
                                 numero=numero, complemento=complemento, cep=cep)
    return _Entrada(nome_original=nome, nome_normalizado=_normalizar(nome), cnpj="",
                    tipo=tipo, endereco=end, email=email)


def _catalogo():
    return CatalogoTransportadoras([
        _entrada("Transfrios Transportes Ltda", municipio="Itapecerica da Serra", bairro="Potuvera",
                 logradouro="Est. Francisco Hengles", numero="591", complemento="TRANSFRIOS",
                 cep="06885-160", email="sp01@transfrios.com.br, pr02@transfrios.com.br"),
        _entrada("TRANSFRIOS", municipio="Itapecerica da Serra", bairro="Potuvera",
                 logradouro="Est. Francisco Hengles", numero="591", complemento="TRANSFRIOS",
                 cep="06885-160", email="sp01@transfrios.com.br, pr02@transfrios.com.br"),
        _entrada("IMG TRANSPORTES EIRELLI ME", municipio="São Paulo", bairro="Jardim Japão",
                 logradouro="Rua Osaka", numero="880", complemento="KANEJO", cep="02124-040"),
        _entrada("KANEJO", municipio="São Paulo", bairro="Jardim Japão",
                 logradouro="Rua Osaka", numero="880", complemento="KANEJO", cep="02124-040"),
        _entrada("TAFF BRASIL TRANSPORTES LTDA", municipio="Barueri\xa0", bairro="Jardim Belval",
                 logradouro="Av. Pref. João Vila-Lobos Quero", numero="1505",
                 complemento="Quada 19 - TAFF BRASIL", cep="06422-122", email="xml@grupotaff.com.br"),
        _entrada("JAGUARE TRANSPORTES", municipio="GUARULHOS", bairro="VILA SAIAGO",
                 logradouro="RUA BARTOLOMEU BUENO DA SILVA", numero="102",
                 complemento="REDESPACHO - RECEBEM DE SEGUNDA A QUARTA", cep=""),
        _entrada("LOGGI TECNOLOGIA LTDA", municipio="São Paulo", bairro="Casa Verde Alta",
                 logradouro="Rua Zilda", numero="675", complemento="LOGGI", cep="02545-000"),
        _entrada("CLIENTE RETIRA", tipo="RETIRADA"),
        _entrada("FRESHLOG", tipo="ENTREGA"),
    ])


def test_pontos_agrupam_linhas_do_mesmo_galpao():
    pontos = {p.chave: p for p in _catalogo().pontos_redespacho()}
    assert len(pontos) == 5
    transfrios = pontos["06885160-591"]
    assert transfrios.nome == "Transfrios Transportes Ltda"
    assert transfrios.emails == ["sp01@transfrios.com.br", "pr02@transfrios.com.br"]
    assert len(transfrios.nomes) == 2
    kanejo = pontos["02124040-880"]
    # "dona" do ponto é quem aparece no complemento, não a primeira linha
    assert kanejo.nome == "KANEJO"
    assert kanejo.emails == []
    assert set(kanejo.nomes) == {"IMG TRANSPORTES EIRELLI ME", "KANEJO"}


def test_endereco_gerado_pelo_redespacho_bate_por_cep():
    p = _catalogo().resolver_por_endereco(
        "Est. Francisco Hengles, 591, TRANSFRIOS, Potuvera, Itapecerica da Serra - SP, 06885-160, Brasil")
    assert p is not None and p.nome_normalizado == "TRANSFRIOS"


def test_endereco_digitado_pelo_cliente_bate_por_cep_e_numero():
    # Caso real 10/09 (#PS-38850): cliente não informou transportadora na
    # Stokki, mas o endereço de entrega é o galpão da TAFF (formato do Google)
    p = _catalogo().resolver_por_endereco(
        "Av. Pref. João Vila-Lobos Quero, 1505, Jardim Belval, Barueri - SP, 06422-122, Brasil")
    assert p is not None and p.nome == "TAFF BRASIL TRANSPORTES LTDA"


def test_mesmo_cep_numero_diferente_nao_bate():
    # R. Zilda 288 tem o mesmo CEP da LOGGI (Rua Zilda 675) -- é outro imóvel
    assert _catalogo().resolver_por_endereco(
        "R. Zilda, 288 - Casa Verde Alta, São Paulo - SP, 02545-000, Brasil") is None


def test_numero_com_zero_a_esquerda_e_cep_diferente_cai_no_fallback_por_rua():
    # CEP digitado errado pelo cliente; rua + número + cidade ainda identificam
    p = _catalogo().resolver_por_endereco(
        "RUA OSAKA 0880, JARDIM JAPAO, SAO PAULO - SP, 02124-000, Brasil")
    assert p is not None and p.nome == "KANEJO"


def test_transportadora_sem_cep_na_planilha_bate_por_rua():
    p = _catalogo().resolver_por_endereco(
        "Rua Bartolomeu Bueno da Silva, 102 - Vila Saiago, Guarulhos - SP, 07110-000, Brasil")
    assert p is not None and p.nome == "JAGUARE TRANSPORTES"


def test_rua_igual_em_outra_cidade_nao_bate():
    assert _catalogo().resolver_por_endereco(
        "Rua Osaka, 880, Centro, Campinas - SP, 13010-000, Brasil") is None


def test_endereco_residencial_comum_nao_bate():
    cat = _catalogo()
    assert cat.resolver_por_endereco(
        "Rua Capote Valente 851 - Apto 13, Pinheiros, São Paulo - SP, 05409-002, Brasil") is None
    assert cat.resolver_por_endereco("") is None
    assert cat.resolver_por_endereco(None) is None


def test_resolver_por_nome_continua_funcionando():
    r = _catalogo().resolver("KANEJO LOGISTICA LTDA")
    assert r.tipo == "TERCEIROS" and r.endereco_redespacho.cep == "02124-040"
