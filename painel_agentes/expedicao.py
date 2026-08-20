# -*- coding: utf-8 -*-
"""
expedicao.py

Dados e geração de PDF da tela de Expedição (`/expedicao`) -- pedido do
Hugo, 18/08: uma página simples pro time de expedição imprimir a
papelada (romaneio: NFs + boletos + canhoteira) de cada rota do dia
antes do motorista sair, sem precisar entender Torre/Planejamento.

Reaproveita 100% o motor de roteirizacao/gerar_pdf_romaneios.py (mesmo
usado no job automático das 04h e no botão "Imprimir rota" do
planejamento) -- aqui só lista as rotas JÁ CRIADAS na VUUPT (não
rascunhos, diferente de planejamento_rotas.py::gerar_romaneio_pdf) e
gera o PDF sob demanda, sempre fresco (mesmo padrão do botão "Imprimir
rota": reflete o estado atual dos documentos, não o que o job das 04h
viu), gravando no MESMO caminho que o job das 04h usaria -- o
print-agent local não precisa saber a diferença.
"""
import re
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import yaml

from avisar_motoristas_rotas import buscar_rotas_do_dia, _extrair_servicos_da_rota
from regras.preferencias_motoristas import CatalogoMotoristas
import gerar_pdf_romaneios as gpr

# Mesma normalização de reentrega ('PS-36327-R1' -> 'PS-36327') que
# roteirizacao/gerar_pdf_romaneios.py::_codigo_base e
# painel_agentes/planejamento_rotas.py::_codigo_base -- documentos e o
# pedido na Stokki vivem sob o código BASE, sem sufixo de reentrega.
_PADRAO_SUFIXO_REENTREGA = re.compile(r"-R\d+$")


def _codigo_base(codigo: str) -> str:
    return _PADRAO_SUFIXO_REENTREGA.sub("", (codigo or "").lstrip("#"))


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _catalogo_motoristas() -> dict:
    """agent_id -> MotoristaPreferencias, pra resolver nome e placa."""
    cfg_motoristas = _carregar_config().get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(
        cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
    return {m.agent_id: m for m in catalogo.motoristas}


def listar_rotas_do_dia(data_alvo: date) -> list[dict]:
    """Rotas reais (VUUPT) da data, uma linha por rota: nome, motorista,
    placa, quantidade de pedidos e horário de saída -- ordenadas por
    horário de saída. Rota sem serviço (vazia) fica fora, mesmo
    critério do job das 04h."""
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    motorista_por_agent_id = _catalogo_motoristas()

    rotas = []
    for r in buscar_rotas_do_dia(token, data_alvo):
        servicos = _extrair_servicos_da_rota(r)
        if not servicos:
            continue
        motorista = motorista_por_agent_id.get(r.get("agent_id"))
        rotas.append({
            "id": r.get("id"),
            "nome": r.get("name") or f"Rota {r.get('id')}",
            "motorista": motorista.nome if motorista else "(sem motorista)",
            "placa": motorista.placa if motorista else None,
            "pedidos": len(servicos),
            "inicio": (r.get("start_at") or "")[11:16],
        })

    rotas.sort(key=lambda r: (r["inicio"], r["nome"]))
    return rotas


def gerar_romaneio_rota(data_alvo: date, rota_id: int) -> Path:
    """Gera (sempre fresco -- mesmo padrão do botão "Imprimir rota" do
    planejamento) e devolve o caminho do PDF de romaneio de uma rota já
    criada na VUUPT. Levanta ValueError (404 pro chamador) se a rota
    não existir mais na data ou não tiver paradas."""
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    nome_por_agent_id = {aid: m.nome for aid, m in _catalogo_motoristas().items()}

    rotas = buscar_rotas_do_dia(token, data_alvo)
    rota = next((r for r in rotas if r.get("id") == rota_id), None)
    if rota is None:
        raise ValueError(f"Rota {rota_id} não encontrada em {data_alvo.isoformat()} (ou está cancelada).")

    servicos = _extrair_servicos_da_rota(rota)
    if not servicos:
        raise ValueError("Rota sem paradas -- nada pra imprimir.")

    nome_motorista = nome_por_agent_id.get(rota.get("agent_id"), "(sem motorista)")
    codigos = {_codigo_base((s.get("code") or "").lstrip("#")) for s in servicos}
    docs_por_pedido, _em_revisao = gpr.carregar_documentos_por_pedido(codigos)
    embarcadores, fatores = gpr.carregar_embarcadores()

    pasta = gpr.PASTA_ROMANEIOS / data_alvo.isoformat()
    caminho_saida = pasta / gpr.nome_arquivo_saida(rota, nome_motorista, data_alvo)
    gpr.montar_pdf_rota(rota, servicos, docs_por_pedido, embarcadores, fatores,
                        nome_motorista, data_alvo, caminho_saida)
    return caminho_saida
