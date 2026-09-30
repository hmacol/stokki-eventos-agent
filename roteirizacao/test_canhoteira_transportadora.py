# -*- coding: utf-8 -*-
"""
Canhoteira de transportadora no romaneio (gerar_pdf_romaneios.py):
1 folha por galpão de redespacho presente na rota, identificada pelo
ENDEREÇO do serviço na VUUPT. Tudo offline (catálogo em memória, sem
banco, sem documentos). Rodar com:
    python -m pytest roteirizacao/test_canhoteira_transportadora.py -q
"""
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from pypdf import PdfReader  # noqa: E402

import gerar_pdf_romaneios as gpr  # noqa: E402
from regras.transportadoras import (  # noqa: E402
    CatalogoTransportadoras, EnderecoRedespacho, _Entrada, _normalizar,
)

DATA = date(2026, 9, 10)


def _entrada(nome, municipio, bairro, logradouro, numero, complemento, cep):
    end = EnderecoRedespacho(uf="SP", municipio=municipio, bairro=bairro, logradouro=logradouro,
                             numero=numero, complemento=complemento, cep=cep)
    return _Entrada(nome_original=nome, nome_normalizado=_normalizar(nome), cnpj="",
                    tipo="TERCEIROS", endereco=end)


def _catalogo():
    return CatalogoTransportadoras([
        _entrada("Transfrios Transportes Ltda", "Itapecerica da Serra", "Potuvera",
                 "Est. Francisco Hengles", "591", "TRANSFRIOS", "06885-160"),
        _entrada("KANEJO", "São Paulo", "Jardim Japão", "Rua Osaka", "880", "KANEJO", "02124-040"),
    ])


def _servico(codigo, titulo, endereco, sender_id=1, volumes=2):
    return {"code": f"#{codigo}", "title": f"#{codigo} - 123 / EMB / {titulo}",
            "address": endereco, "sender_id": sender_id, "dimension_3": volumes}


END_TRANSFRIOS = "Est. Francisco Hengles, 591, TRANSFRIOS, Potuvera, Itapecerica da Serra - SP, 06885-160, Brasil"
END_KANEJO = "Rua Osaka, 880, KANEJO, Jardim Japão, São Paulo - SP, 02124-040, Brasil"
END_COMUM = "Rua Capote Valente, 851, Pinheiros, São Paulo - SP, 05409-002, Brasil"


def _montar(servicos, catalogo):
    with tempfile.TemporaryDirectory() as tmp:
        saida = Path(tmp) / "romaneio.pdf"
        stats = gpr.montar_pdf_rota({"name": "Planejamento - 10/09/2026 - #4", "id": 999},
                                    servicos, {}, {1: "COGUMELADO"}, {1: 1.0},
                                    "MOTORISTA TESTE", DATA, saida,
                                    catalogo_transportadoras=catalogo)
        paginas = len(PdfReader(str(saida)).pages)
    return stats, paginas


def test_uma_folha_por_transportadora_na_ordem_da_rota():
    servicos = [
        _servico("PS-1001", "CLIENTE A", END_COMUM),
        _servico("PS-1002", "CLIENTE B", END_KANEJO),
        _servico("PS-1003", "CLIENTE C", END_TRANSFRIOS),
        _servico("PS-1004", "CLIENTE D", END_KANEJO),
    ]
    stats, paginas = _montar(servicos, _catalogo())
    # capa + 1 folha KANEJO + 1 folha TRANSFRIOS (sem NF/boleto emendados)
    assert paginas == 3
    # nome curto (normalizado) no resumo/capa; a folha leva o nome completo
    assert stats["canhoteira_transportadoras"] == [("KANEJO", 2), ("TRANSFRIOS", 1)]
    assert stats["canhoteira"] == 0          # canhoteira de embarcador não muda


def test_rota_sem_transportadora_nao_ganha_folha():
    stats, paginas = _montar([_servico("PS-2001", "CLIENTE A", END_COMUM)], _catalogo())
    assert paginas == 1
    assert stats["canhoteira_transportadoras"] == []


def test_sem_catalogo_o_romaneio_sai_normal():
    stats, paginas = _montar([_servico("PS-3001", "CLIENTE A", END_KANEJO)], None)
    # catalogo=None força o carregamento da planilha real (se existir);
    # com ou sem ela o PDF sai -- o que se garante aqui é não quebrar.
    assert paginas >= 1
    assert "canhoteira_transportadoras" in stats


def test_nome_curto_apara_conectivo_solto():
    dafran = _entrada("DAFRAN TRANSPORTES E SERVICOS LTDA EPP", "Itapecerica da Serra", "Vila",
                      "Estrada dos Maciéis", "193", "DAFRAN", "06854-120")
    ponto = CatalogoTransportadoras([dafran]).pontos_redespacho()[0]
    assert ponto.nome_normalizado == "DAFRAN E"      # como a planilha normaliza hoje
    assert gpr.nome_curto_transportadora(ponto) == "DAFRAN"


def test_agrupar_por_transportadora_ignora_endereco_comum():
    itens = [{"codigo": "PS-1", "endereco_vuupt": END_COMUM},
             {"codigo": "PS-2", "endereco_vuupt": END_TRANSFRIOS},
             {"codigo": "PS-3", "endereco_vuupt": ""}]
    grupos = gpr.agrupar_por_transportadora(itens, _catalogo())
    assert len(grupos) == 1
    ponto, entregas = grupos[0]
    assert ponto.nome_normalizado == "TRANSFRIOS"
    assert [e["codigo"] for e in entregas] == ["PS-2"]
    assert gpr.agrupar_por_transportadora(itens, None) == []


def test_muitos_pedidos_quebram_pagina_e_mantem_bloco_de_recebimento():
    servicos = [_servico(f"PS-{5000 + i}", f"CLIENTE {i}", END_KANEJO) for i in range(30)]
    stats, paginas = _montar(servicos, _catalogo())
    # capa (2 páginas: 30 linhas não cabem numa) + canhoteira KANEJO em 2+ folhas
    assert paginas >= 4
    assert stats["canhoteira_transportadoras"] == [("KANEJO", 30)]
