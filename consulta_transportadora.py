# -*- coding: utf-8 -*-
"""
consulta_transportadora.py

Fluxo automatico para transportadora desconhecida: cria o cadastro PENDENTE,
guarda os pedidos parados e uma consulta por (transportadora, embarcador).
Quando a transportadora (ou a equipe) responde, o cadastro vira ATIVA e todos
os pedidos parados dela sao liberados. Tabelas:
  transportadora_pedidos_parados   1 linha por pedido segurado (liberado_em ao responder)
  transportadora_consultas         1 linha por par transportadora x embarcador
"""
from datetime import date, datetime

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import transportadoras_cadastro as tc
from regras.transportadoras import _normalizar
from transportadoras_cadastro import DB_PATH, ErroCadastro

_SALT = "consulta-transportadora"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transportadora_pedidos_parados (
    pedido TEXT PRIMARY KEY,
    transportadora_id INTEGER, embarcador_cnpj TEXT, stkkc_id INTEGER,
    desde TEXT, liberado_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_transp_parados_tid ON transportadora_pedidos_parados(transportadora_id);
CREATE TABLE IF NOT EXISTS transportadora_consultas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transportadora_id INTEGER, embarcador_cnpj TEXT,
    criado_em TEXT, enviado_em TEXT, ultimo_lembrete_em TEXT, alerta_equipe_em TEXT,
    respondido_em TEXT, respondido_por TEXT, canal_resposta TEXT,
    UNIQUE(transportadora_id, embarcador_cnpj)
);
"""


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def conectar_consultas(db_path=DB_PATH):
    conn = tc.conectar(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _achar_pendente(conn, nome: str, cnpj: str) -> dict | None:
    norm = _normalizar(nome)
    por_nome = None
    for r in conn.execute("SELECT * FROM transportadoras WHERE situacao='PENDENTE' ORDER BY id"):
        if cnpj and r["cnpj"] == cnpj:
            return dict(r)
        if por_nome is None and _normalizar(r["nome"]) == norm:
            por_nome = dict(r)
    return por_nome


def registrar_desconhecida(conn, nome: str, cnpj: str, pedido: str, embarcador_cnpj: str,
                           stkkc_id: int | None) -> dict:
    cnpj = tc.normalizar_cnpj(cnpj)
    transp = _achar_pendente(conn, nome, cnpj)
    nova = transp is None
    if nova:
        transp = tc.criar(conn, {"nome": nome, "cnpj": cnpj, "situacao": "PENDENTE", "tipo": None},
                          origem="AUTOMATICO", por="automatico")
    tid = transp["id"]
    agora = _agora()
    conn.execute(
        "INSERT INTO transportadora_pedidos_parados (pedido, transportadora_id, embarcador_cnpj, stkkc_id, desde) "
        "VALUES (?,?,?,?,?) ON CONFLICT(pedido) DO UPDATE SET transportadora_id=excluded.transportadora_id, "
        "embarcador_cnpj=excluded.embarcador_cnpj, stkkc_id=excluded.stkkc_id, liberado_em=NULL",
        (pedido, tid, embarcador_cnpj, stkkc_id, agora))
    conn.execute(
        "INSERT INTO transportadora_consultas (transportadora_id, embarcador_cnpj, criado_em) VALUES (?,?,?) "
        "ON CONFLICT(transportadora_id, embarcador_cnpj) DO NOTHING", (tid, embarcador_cnpj, agora))
    conn.commit()
    consulta = conn.execute(
        "SELECT * FROM transportadora_consultas WHERE transportadora_id=? AND embarcador_cnpj=?",
        (tid, embarcador_cnpj)).fetchone()
    return {"transportadora": transp, "consulta": dict(consulta), "nova": nova}


def pedidos_parados(conn, transportadora_id: int, embarcador_cnpj: str | None = None) -> list[dict]:
    sql = "SELECT * FROM transportadora_pedidos_parados WHERE transportadora_id=? AND liberado_em IS NULL"
    params = [transportadora_id]
    if embarcador_cnpj is not None:
        sql += " AND embarcador_cnpj=?"
        params.append(embarcador_cnpj)
    return [dict(r) for r in conn.execute(sql + " ORDER BY desde, pedido", params)]


def responder(conn, transportadora_id: int, tipo: str, endereco: dict | None, email: str,
              por: str, canal: str) -> dict:
    atual = tc.buscar(conn, transportadora_id)
    if atual is None:
        raise ErroCadastro(f"Transportadora {transportadora_id} nao existe.")
    if atual["situacao"] == "ATIVA":
        raise ErroCadastro("ja respondida")
    tipo = (tipo or "").strip().upper()
    if tipo not in tc.TIPOS:
        raise ErroCadastro(f"Tipo invalido: {tipo!r}.")
    mud = {"tipo": tipo, "situacao": "ATIVA"}
    if email:
        mud["email"] = email
    if tipo == "TERCEIROS":
        for c in ("uf", "municipio", "bairro", "logradouro", "numero", "complemento", "cep"):
            mud[c] = (endereco or {}).get(c, "")
    # Validacao/conflito acontece aqui; se falhar, nada abaixo roda.
    novo = tc.atualizar(conn, transportadora_id, mud, por)
    agora = _agora()
    conn.execute(
        "UPDATE transportadora_consultas SET respondido_em=?, respondido_por=?, canal_resposta=? "
        "WHERE transportadora_id=? AND respondido_em IS NULL", (agora, por, canal, transportadora_id))
    conn.execute(
        "UPDATE transportadora_pedidos_parados SET liberado_em=? "
        "WHERE transportadora_id=? AND liberado_em IS NULL", (agora, transportadora_id))
    conn.commit()
    return novo


def deve_avisar_hoje(consulta: dict, hoje: date) -> bool:
    d = hoje.isoformat()
    return not any((consulta.get(c) or "")[:10] == d for c in ("enviado_em", "ultimo_lembrete_em"))


def gerar_token(secret: str, consulta_id: int) -> str:
    return URLSafeTimedSerializer(secret).dumps({"c": consulta_id}, salt=_SALT)


def ler_token(secret: str, token: str, validade_dias: int) -> int | None:
    """id da consulta; None se invalido; -1 se expirado."""
    try:
        dados = URLSafeTimedSerializer(secret).loads(token, salt=_SALT, max_age=int(validade_dias) * 86400)
    except SignatureExpired:
        return -1
    except BadSignature:
        return None
    try:
        return int(dados["c"])
    except (KeyError, TypeError, ValueError):
        return None


def consultas_abertas(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, t.nome AS transportadora_nome, "
        "(SELECT COUNT(*) FROM transportadora_pedidos_parados p WHERE p.transportadora_id=c.transportadora_id "
        " AND p.embarcador_cnpj=c.embarcador_cnpj AND p.liberado_em IS NULL) AS pedidos_parados "
        "FROM transportadora_consultas c JOIN transportadoras t ON t.id=c.transportadora_id "
        "WHERE c.respondido_em IS NULL ORDER BY c.id")
    return [dict(r) for r in rows]
