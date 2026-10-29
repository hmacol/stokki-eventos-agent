# -*- coding: utf-8 -*-
"""
regras/cadastro_motoristas.py

Cadastro de motorista NOVO na tabela bd_motoristas (pedido do Hugo, 16/08 --
UI em /motoristas no painel_agentes; desde 29/10 a planilha
dados/BD_MOTORISTAS.xlsx é só um espelho gerado por
regras/motoristas_banco.py). Contraparte de escrita de
regras/preferencias_motoristas.py (só leitura).

O motorista precisa já existir como "agente" no VUUPT antes de chegar
aqui -- não existe endpoint de criação de agente na API do VUUPT, só de
listagem (GET /agents, mesmo endpoint usado por
sincronizar_placas_motoristas.py). Este módulo cobre só a 2ª parte do
cadastro: gravar a linha na tabela com o AGENT_ID_VUUPT que o Hugo
escolher.
"""
import logging
import re

import requests

from nucleo import banco
from regras import motoristas_banco
from regras.tipo_veiculo import tipo_por_codigo

logger = logging.getLogger(__name__)

API_BASE = "https://api.vuupt.com/api/v1"

COLUNAS_PADRAO = [
    "AGENT_ID_VUUPT", "VEHICLE_ID_VUUPT", "NOME_MOTORISTA", "ACEITA_VIAGENS",
    "DIAS_DISPONIVEIS", "MAX_ROTAS_DIA", "ATIVO", "ZONAS_PREFERIDAS",
    "TELEFONE_MOTORISTA", "EMAIL_MOTORISTA", "PLACA", "CPF_MOTORISTA",
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


def _com_conexao(conn, funcao):
    """Roda funcao(conn); sem conn abre o banco do projeto e fecha no fim."""
    if conn is not None:
        return funcao(conn)
    conn = banco.conectar()
    try:
        return funcao(conn)
    finally:
        conn.close()


def _espelhar(conn) -> None:
    """Atualiza a planilha-espelho; falha dela nunca desfaz a gravacao."""
    try:
        motoristas_banco.escrever_espelho(conn)
    except Exception:
        logger.exception("Falha ao atualizar o espelho da planilha de motoristas")


def _agent_ids_cadastrados(conn) -> set[int]:
    return {r["AGENT_ID_VUUPT"] for r in motoristas_banco.listar(conn) if r["AGENT_ID_VUUPT"] is not None}


def listar_agentes_vuupt_nao_cadastrados(config: dict, *, conn=None) -> list[dict]:
    """Agentes do VUUPT que ainda não têm linha na tabela bd_motoristas --
    fonte do dropdown "Motorista (VUUPT)" do formulário de cadastro.
    Devolve [{"agent_id": int, "nome": str}], ordenado por nome."""
    token = config.get("vuupt_api", {}).get("token", "")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    ja_cadastrados = _com_conexao(conn, _agent_ids_cadastrados)
    agentes_vuupt = _listar_tudo_vuupt("/agents", headers)

    disponiveis = [
        {"agent_id": agente["id"], "nome": str(agente.get("name") or f"Agente {agente['id']}")}
        for agente in agentes_vuupt
        if agente["id"] not in ja_cadastrados
    ]
    disponiveis.sort(key=lambda a: a["nome"])
    return disponiveis


def cadastrar_motorista(config: dict, dados: dict, *, origem: str = "painel", conn=None) -> dict:
    """Grava uma linha nova na tabela bd_motoristas a partir do payload do
    formulário de cadastro. Levanta ValueError (400 na rota) pra validação
    de dado. Devolve {"agent_id": int, "nome": str}."""
    try:
        agent_id = int(dados["agent_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("agent_id inválido ou ausente.")

    nome = str(dados.get("nome") or "").strip()
    if not nome:
        raise ValueError("Nome do motorista é obrigatório.")

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
        "CPF_MOTORISTA": re.sub(r"\D", "", str(dados.get("cpf") or "")) or None,
    }
    if tipo_veiculo_codigo:
        nova_linha["TIPO_VEICULO"] = tipo_veiculo_codigo

    def gravar(c):
        if agent_id in _agent_ids_cadastrados(c):
            raise ValueError(f"Motorista já cadastrado (agent_id {agent_id}).")
        motoristas_banco.inserir(c, nova_linha, origem)
        _espelhar(c)

    _com_conexao(conn, gravar)
    logger.info(f"Motorista cadastrado: {nome} (agent_id={agent_id}).")
    return {"agent_id": agent_id, "nome": nome}


def atualizar_contato_planilha(config: dict, cpf: str, telefone=None, email=None, placa=None, *, conn=None) -> bool:
    """Espelha o que o motorista mudou no app (08/10) nas linhas dele na
    tabela bd_motoristas, achadas pelo CPF. Só mexe nos campos informados
    (None = não mexe; "" = limpa). Telefone e e-mail valem pra todas as
    linhas do CPF; a placa só se o CPF tem uma linha (com 2+ carros não dá
    pra saber qual mudou: fica só no histórico, "nao_repassada"). Devolve
    False se o CPF não está na tabela."""
    cpf = re.sub(r"\D", "", str(cpf or ""))
    if not cpf:
        return False
    origem = f"app:{cpf[-4:]}"

    def gravar(c):
        linhas = motoristas_banco.por_cpf(c, cpf)
        if not linhas:
            return False
        campos = {}
        if telefone is not None:
            campos["TELEFONE_MOTORISTA"] = str(telefone).strip()
        if email is not None:
            campos["EMAIL_MOTORISTA"] = str(email).strip()
        placa_nova = str(placa).strip().upper() if placa is not None else None
        if placa_nova is not None and len(linhas) == 1:
            campos["PLACA"] = placa_nova
        for linha in linhas:
            if campos:
                motoristas_banco.atualizar(c, linha["id"], campos, origem)
            if placa_nova is not None and len(linhas) > 1:
                motoristas_banco.registrar_historico(c, linha["id"], "PLACA", placa_nova, f"{origem}:nao_repassada")
        _espelhar(c)
        return True

    achou = _com_conexao(conn, gravar)
    if achou:
        informados = ", ".join(k for k, v in (("TELEFONE_MOTORISTA", telefone), ("EMAIL_MOTORISTA", email), ("PLACA", placa)) if v is not None)
        logger.info(f"Motoristas: contato do CPF ...{cpf[-4:]} atualizado ({informados}).")
    return achou
