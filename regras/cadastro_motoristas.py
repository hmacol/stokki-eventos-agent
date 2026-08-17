# -*- coding: utf-8 -*-
"""
regras/cadastro_motoristas.py

Cadastro de motorista NOVO na planilha dados/BD_MOTORISTAS.xlsx (pedido
do Hugo, 16/08 -- UI em /motoristas no painel_agentes). Contraparte de
escrita de regras/preferencias_motoristas.py (só leitura).

O motorista precisa já existir como "agente" no VUUPT antes de chegar
aqui -- não existe endpoint de criação de agente na API do VUUPT, só de
listagem (GET /agents, mesmo endpoint usado por
sincronizar_placas_motoristas.py). Este módulo cobre só a 2ª parte do
cadastro: gravar a linha na planilha com o AGENT_ID_VUUPT que o Hugo
escolher.
"""
import logging
from pathlib import Path

import pandas as pd
import requests

from regras.tipo_veiculo import tipo_por_codigo

logger = logging.getLogger(__name__)

API_BASE = "https://api.vuupt.com/api/v1"

COLUNAS_PADRAO = [
    "AGENT_ID_VUUPT", "VEHICLE_ID_VUUPT", "NOME_MOTORISTA", "ACEITA_VIAGENS",
    "DIAS_DISPONIVEIS", "MAX_ROTAS_DIA", "ATIVO", "ZONAS_PREFERIDAS",
    "TELEFONE_MOTORISTA", "EMAIL_MOTORISTA", "PLACA",
]


def _listar_tudo_vuupt(endpoint: str, headers: dict) -> list[dict]:
    """Mesma paginação de sincronizar_placas_motoristas.py::_listar_tudo."""
    todos = []
    pagina = 1
    while True:
        resp = requests.get(f"{API_BASE}{endpoint}", headers=headers,
                            params={"per_page": 100, "page": pagina}, timeout=20)
        resp.raise_for_status()
        corpo = resp.json()
        dados = corpo.get("data", [])
        todos.extend(dados)
        paginacao = corpo.get("meta", {}).get("pagination", {})
        if pagina >= paginacao.get("total_pages", pagina):
            break
        pagina += 1
    return todos


def _agent_ids_cadastrados(caminho_planilha: Path) -> set[int]:
    if not caminho_planilha.exists():
        return set()
    df = pd.read_excel(caminho_planilha)
    ids = set()
    for valor in df.get("AGENT_ID_VUUPT", []):
        try:
            ids.add(int(float(valor)))
        except (TypeError, ValueError):
            continue
    return ids


def listar_agentes_vuupt_nao_cadastrados(config: dict) -> list[dict]:
    """Agentes do VUUPT que ainda não têm linha em BD_MOTORISTAS.xlsx --
    fonte do dropdown "Motorista (VUUPT)" do formulário de cadastro.
    Devolve [{"agent_id": int, "nome": str}], ordenado por nome."""
    token = config.get("vuupt_api", {}).get("token", "")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    caminho_planilha = Path(config.get("motoristas", {}).get("planilha", ""))

    ja_cadastrados = _agent_ids_cadastrados(caminho_planilha)
    agentes_vuupt = _listar_tudo_vuupt("/agents", headers)

    disponiveis = [
        {"agent_id": agente["id"], "nome": str(agente.get("name") or f"Agente {agente['id']}")}
        for agente in agentes_vuupt
        if agente["id"] not in ja_cadastrados
    ]
    disponiveis.sort(key=lambda a: a["nome"])
    return disponiveis


def cadastrar_motorista(config: dict, dados: dict) -> dict:
    """Grava uma linha nova em BD_MOTORISTAS.xlsx a partir do payload do
    formulário de cadastro. Levanta ValueError (400 na rota) pra
    validação de dado, PermissionError (409 na rota) se o arquivo
    estiver aberto no Excel."""
    try:
        agent_id = int(dados["agent_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("agent_id inválido ou ausente.")

    nome = str(dados.get("nome") or "").strip()
    if not nome:
        raise ValueError("Nome do motorista é obrigatório.")

    caminho_planilha = Path(config.get("motoristas", {}).get("planilha", ""))
    if not caminho_planilha.exists():
        raise ValueError(f"Planilha de motoristas não encontrada em {caminho_planilha}.")

    df = pd.read_excel(caminho_planilha)
    colunas_originais = list(df.columns)

    if agent_id in _agent_ids_cadastrados(caminho_planilha):
        raise ValueError(f"Motorista já cadastrado (agent_id {agent_id}).")

    vehicle_id = dados.get("vehicle_id")
    tipo_veiculo_codigo = (dados.get("tipo_veiculo") or "").strip().upper() or None
    if tipo_veiculo_codigo and not tipo_por_codigo(tipo_veiculo_codigo):
        raise ValueError(f"Tipo de veículo '{tipo_veiculo_codigo}' não reconhecido.")

    nova_linha = {
        "AGENT_ID_VUUPT": agent_id,
        "VEHICLE_ID_VUUPT": int(vehicle_id) if vehicle_id not in (None, "") else None,
        "NOME_MOTORISTA": nome,
        "ACEITA_VIAGENS": "SIM" if dados.get("aceita_viagens") else "NAO",
        "DIAS_DISPONIVEIS": ",".join(dados.get("dias_disponiveis") or []),
        "MAX_ROTAS_DIA": int(dados.get("max_rotas_dia") or 1),
        "ATIVO": "SIM" if dados.get("ativo") else "NAO",
        "ZONAS_PREFERIDAS": ",".join(dados.get("zonas_preferidas") or []),
        "TELEFONE_MOTORISTA": str(dados.get("telefone") or "").strip() or None,
        "EMAIL_MOTORISTA": str(dados.get("email") or "").strip() or None,
        "PLACA": str(dados.get("placa") or "").strip().upper() or None,
    }
    if tipo_veiculo_codigo:
        nova_linha["TIPO_VEICULO"] = tipo_veiculo_codigo

    # TIPO_VEICULO pode não existir ainda na planilha (pendente desde a
    # implementação de regras/tipo_veiculo.py, 15/08) -- só é criada aqui
    # se este cadastro realmente informou um tipo de veículo, mesmo
    # padrão de "adiciona coluna no fim se não existia" já usado em
    # sincronizar_placas_motoristas.py pra PLACA.
    colunas_finais = list(colunas_originais)
    for coluna in nova_linha:
        if coluna not in colunas_finais:
            colunas_finais.append(coluna)

    df = pd.concat([df, pd.DataFrame([nova_linha])], ignore_index=True)
    df = df[colunas_finais]

    try:
        df.to_excel(caminho_planilha, index=False)
    except PermissionError:
        raise PermissionError(
            f"{caminho_planilha.name} está aberto no Excel -- feche o arquivo e tente de novo."
        )

    logger.info(f"Motorista cadastrado: {nome} (agent_id={agent_id}).")
    return {"agent_id": agent_id, "nome": nome}
