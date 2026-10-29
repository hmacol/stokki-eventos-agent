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


def _sim_nao(valor) -> str:
    return "SIM" if valor else "NAO"


def _inteiro_ou_none(valor):
    return int(valor) if valor not in (None, "") else None


# chave do formulário -> (coluna, tradução). Só entram as chaves presentes no payload.
_FORMULARIO = {
    "vehicle_id": ("VEHICLE_ID_VUUPT", _inteiro_ou_none),
    "nome": ("NOME_MOTORISTA", lambda v: str(v or "").strip()),
    "aceita_viagens": ("ACEITA_VIAGENS", _sim_nao),
    "dias_disponiveis": ("DIAS_DISPONIVEIS", lambda v: ",".join(v or [])),
    "max_rotas_dia": ("MAX_ROTAS_DIA", _inteiro_ou_none),
    "ativo": ("ATIVO", _sim_nao),
    "zonas_preferidas": ("ZONAS_PREFERIDAS", lambda v: ",".join(v or [])),
    "telefone": ("TELEFONE_MOTORISTA", lambda v: str(v or "").strip() or None),
    "email": ("EMAIL_MOTORISTA", lambda v: str(v or "").strip() or None),
    "placa": ("PLACA", lambda v: str(v or "").strip().upper() or None),
    "cpf": ("CPF_MOTORISTA", lambda v: re.sub(r"\D", "", str(v or "")) or None),
    "tipo_veiculo": ("TIPO_VEICULO", lambda v: str(v or "").strip().upper() or None),
    "apenas_carga_seca": ("APENAS_CARGA_SECA", _sim_nao),
    "sem_app": ("SEM_APP", _sim_nao),
}


def _colunas_do_formulario(dados: dict) -> dict:
    """Payload do formulário -> {COLUNA: valor} só das chaves presentes
    (ausente = não mexe; "" = limpa). ValueError pra tipo de veículo inválido."""
    colunas = {coluna: traduzir(dados[chave]) for chave, (coluna, traduzir) in _FORMULARIO.items() if chave in dados}
    tipo = colunas.get("TIPO_VEICULO")
    if tipo and not tipo_por_codigo(tipo):
        raise ValueError(f"Tipo de veículo '{tipo}' não reconhecido.")
    return colunas


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

    nova_linha = {"ACEITA_VIAGENS": "NAO", "ATIVO": "NAO", "MAX_ROTAS_DIA": 1,
                  **_colunas_do_formulario(dados), "AGENT_ID_VUUPT": agent_id}
    if not nova_linha["MAX_ROTAS_DIA"]:
        nova_linha["MAX_ROTAS_DIA"] = 1

    def gravar(c):
        if agent_id in _agent_ids_cadastrados(c):
            raise ValueError(f"Motorista já cadastrado (agent_id {agent_id}).")
        motoristas_banco.inserir(c, nova_linha, origem)
        _espelhar(c)

    _com_conexao(conn, gravar)
    logger.info(f"Motorista cadastrado: {nome} (agent_id={agent_id}).")
    return {"agent_id": agent_id, "nome": nome}


# coluna da bd_motoristas -> coluna do login do app (tabela motoristas). Nunca ATIVO.
_REPASSE_LOGIN = {
    "NOME_MOTORISTA": "nome", "TELEFONE_MOTORISTA": "telefone", "EMAIL_MOTORISTA": "email",
    "TIPO_VEICULO": "tipo_veiculo", "VEHICLE_ID_VUUPT": "vehicle_id",
}
# Com 2+ carros no mesmo CPF não dá pra saber qual veículo o login leva (igual à placa).
_SO_COM_UMA_LINHA = {"NOME_MOTORISTA", "TIPO_VEICULO", "VEHICLE_ID_VUUPT"}


def _repassar_ao_login(conn, motorista: dict, mudancas: list[dict]) -> None:
    """Leva ao login do app (mesmo CPF, se existir) o que mudou na edição.
    Placa, tipo de veículo e vehicle_id só vão se o CPF tem uma linha na
    tabela (com 2+ carros não dá pra saber qual vale). Nunca mexe em `ativo`."""
    cpf = motorista.get("CPF_MOTORISTA")
    if not cpf:
        return
    mudou = {m["campo"]: m["para"] for m in mudancas}
    uma_linha = len(motoristas_banco.por_cpf(conn, cpf)) == 1
    sets = {_REPASSE_LOGIN[c]: v for c, v in mudou.items()
            if c in _REPASSE_LOGIN and (uma_linha or c not in _SO_COM_UMA_LINHA)}
    if "PLACA" in mudou and uma_linha:
        sets["placa"] = mudou["PLACA"]
    if not sets:
        return
    sets["atualizado_em"] = banco.agora()
    conn.execute(f"UPDATE motoristas SET {', '.join(f'{c} = ?' for c in sets)} WHERE cpf = ?",
                 (*sets.values(), re.sub(r"\D", "", str(cpf))))
    conn.commit()   # sem login (rowcount 0) a transacao implicita ficaria aberta segurando o lock durante o espelho


class MotoristaNaoEncontrado(ValueError):
    """Id inexistente na bd_motoristas (a rota devolve 404)."""


def _validar_cpf_novo(conn, atual: dict, cpf: str) -> None:
    """CPF so entra num cadastro sem CPF: 11 digitos e de outro agente nao pode ser."""
    if len(cpf) != 11:
        raise ValueError("CPF deve ter 11 digitos.")
    if any(l["AGENT_ID_VUUPT"] != atual["AGENT_ID_VUUPT"] for l in motoristas_banco.por_cpf(conn, cpf)):
        raise ValueError("Esse CPF ja esta cadastrado em outro motorista.")


def editar_motorista(config: dict, motorista_id: int, campos: dict, usuario: str, *, conn=None) -> dict:
    """Edita uma linha da bd_motoristas pelo formulário da tela /motoristas.
    Chave ausente = não mexe; "" = limpa. Repassa nome/contato/veículo ao
    login do app e atualiza o espelho. Levanta ValueError (400 na rota).
    Devolve {"mudancas": [...], "motorista": <linha>}."""
    colunas = _colunas_do_formulario(campos)
    if "agent_id" in campos:
        colunas["AGENT_ID_VUUPT"] = campos["agent_id"]   # atualizar() recusa qualquer troca

    def gravar(c):
        atual = next((m for m in motoristas_banco.listar(c) if m["id"] == motorista_id), None)
        if atual is None:
            raise MotoristaNaoEncontrado(f"Motorista id={motorista_id} nao existe.")
        cpf_novo = colunas.get("CPF_MOTORISTA")
        if cpf_novo and not atual["CPF_MOTORISTA"]:
            _validar_cpf_novo(c, atual, cpf_novo)
        mudancas = motoristas_banco.atualizar(c, motorista_id, colunas, f"painel:{usuario}")
        motorista = next(m for m in motoristas_banco.listar(c) if m["id"] == motorista_id)
        if mudancas:
            try:
                _repassar_ao_login(c, motorista, mudancas)
            except Exception:
                logger.exception("Falha ao repassar a edicao do motorista id=%s ao login do app", motorista_id)
            _espelhar(c)
        return {"mudancas": mudancas, "motorista": motorista}

    resultado = _com_conexao(conn, gravar)
    if resultado["mudancas"]:
        logger.info(f"Motorista id={motorista_id} editado por {usuario}: {', '.join(m['campo'] for m in resultado['mudancas'])}.")
    return resultado


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
