# -*- coding: utf-8 -*-
"""
transportadoras_cadastro.py

Cadastro de transportadoras no dados.db (substitui a planilha
BD_TRANSPORTADORAS.xlsx como fonte do catalogo). Tabelas:
  transportadoras            1 linha por cadastro (situacao ATIVA|PENDENTE|INATIVA)
  transportadoras_historico  1 linha por campo alterado (antes/depois/quem)

Quem le o catalogo usa CatalogoTransportadoras.carregar_do_banco (so ATIVA).
Linha PENDENTE (tipo None) e INATIVA nao passam pela checagem de endereco
nem de conflito; a checagem roda quando a linha vira ATIVA.
"""
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from regras.transportadoras import _normalizar

DB_PATH = Path(__file__).parent / "dados" / "dados.db"

TIPOS = ("ENTREGA", "RETIRADA", "TERCEIROS")
SITUACOES = ("ATIVA", "PENDENTE", "INATIVA")
CAMPOS_EDITAVEIS = (
    "nome", "tipo", "situacao", "uf", "municipio", "bairro", "logradouro", "numero",
    "complemento", "cep", "cidades_atendidas", "ufs_atendidas", "cnpj", "email",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transportadoras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    tipo TEXT,
    situacao TEXT NOT NULL,
    uf TEXT, municipio TEXT, bairro TEXT, logradouro TEXT, numero TEXT,
    complemento TEXT, cep TEXT,
    cidades_atendidas TEXT, ufs_atendidas TEXT,
    cnpj TEXT, email TEXT,
    origem TEXT NOT NULL,
    criado_em TEXT, atualizado_em TEXT, atualizado_por TEXT
);
CREATE TABLE IF NOT EXISTS transportadoras_historico (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transportadora_id INTEGER,
    quando TEXT, quem TEXT, campo TEXT, antes TEXT, depois TEXT
);
CREATE INDEX IF NOT EXISTS idx_transp_hist_tid ON transportadoras_historico(transportadora_id);
"""


class ErroCadastro(ValueError):
    pass


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def conectar(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(transportadoras)")]
    if "classificacao" in cols:  # schema antigo de jun/26
        conn.execute("ALTER TABLE transportadoras RENAME TO transportadoras_antiga_jun26")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def normalizar_cnpj(valor) -> str:
    """So digitos; 13 -> zfill 14 (Excel perde o zero a esquerda); vazio -> ''."""
    d = re.sub(r"\D", "", str(valor if valor is not None else ""))
    if not d:
        return ""
    if len(d) == 13:
        d = d.zfill(14)
    if len(d) != 14:
        raise ErroCadastro(f"CNPJ invalido: {valor!r} (precisa ter 14 digitos)")
    return d


def _limpar(campo: str, valor):
    if campo == "cnpj":
        return normalizar_cnpj(valor)
    s = "" if valor is None else str(valor).strip()
    if campo == "tipo":
        return s.upper() or None
    if campo == "situacao":
        return s.upper()
    return s


def _validar(conn, linha: dict, tid: int | None):
    nome = linha.get("nome") or ""
    if not nome:
        raise ErroCadastro("Nome obrigatorio.")
    if linha.get("situacao") not in SITUACOES:
        raise ErroCadastro(f"Situacao invalida: {linha.get('situacao')!r}.")
    tipo = linha.get("tipo")
    if tipo is not None and tipo not in TIPOS:
        raise ErroCadastro(f"Tipo invalido: {tipo!r}.")
    if linha["situacao"] != "ATIVA":
        return
    if tipo is None:
        raise ErroCadastro("Transportadora ATIVA precisa de tipo.")
    if tipo == "TERCEIROS":
        faltam = [c for c in ("logradouro", "numero", "municipio", "uf") if not linha.get(c)]
        if faltam:
            raise ErroCadastro(f"TERCEIROS ATIVA precisa de endereco; faltam: {', '.join(faltam)}.")
    norm = _normalizar(nome)
    for o in conn.execute("SELECT * FROM transportadoras WHERE situacao='ATIVA' AND id IS NOT ?", (tid,)):
        if o["tipo"] == tipo:
            continue
        mesmo_cnpj = bool(linha.get("cnpj")) and o["cnpj"] == linha["cnpj"]
        if mesmo_cnpj or _normalizar(o["nome"]) == norm:
            raise ErroCadastro(
                f"Conflito com {o['nome']!r} (id {o['id']}, {o['tipo']}): mesmo "
                f"{'CNPJ' if mesmo_cnpj else 'nome'} com tipo diferente."
            )


def buscar(conn, tid: int) -> dict | None:
    r = conn.execute("SELECT * FROM transportadoras WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def criar(conn, dados: dict, origem: str, por: str) -> dict:
    linha = {c: _limpar(c, dados.get(c)) for c in CAMPOS_EDITAVEIS}
    _validar(conn, linha, None)
    agora = _agora()
    cols = list(CAMPOS_EDITAVEIS) + ["origem", "criado_em", "atualizado_em", "atualizado_por"]
    vals = [linha[c] for c in CAMPOS_EDITAVEIS] + [origem, agora, agora, por]
    cur = conn.execute(
        f"INSERT INTO transportadoras ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    conn.commit()
    return buscar(conn, cur.lastrowid)


def atualizar(conn, tid: int, mudancas: dict, por: str) -> dict:
    atual = buscar(conn, tid)
    if atual is None:
        raise ErroCadastro(f"Transportadora {tid} nao existe.")
    novos = {}
    for c, v in mudancas.items():
        if c not in CAMPOS_EDITAVEIS:
            raise ErroCadastro(f"Campo nao editavel: {c!r}.")
        novos[c] = _limpar(c, v)
    alterados = {c: v for c, v in novos.items() if (atual[c] or None) != (v or None)}
    if not alterados:
        return atual
    _validar(conn, {**atual, **alterados}, tid)
    agora = _agora()
    sets = ", ".join(f"{c}=?" for c in alterados)
    conn.execute(f"UPDATE transportadoras SET {sets}, atualizado_em=?, atualizado_por=? WHERE id=?",
                 [*alterados.values(), agora, por, tid])
    for c, v in alterados.items():
        conn.execute(
            "INSERT INTO transportadoras_historico (transportadora_id, quando, quem, campo, antes, depois) "
            "VALUES (?,?,?,?,?,?)", (tid, agora, por, c, atual[c], v))
    conn.commit()
    return buscar(conn, tid)


def listar(conn, situacao: str | None = None, busca: str = "") -> list[dict]:
    sql, params = "SELECT * FROM transportadoras WHERE 1=1", []
    if situacao:
        sql += " AND situacao=?"
        params.append(situacao)
    if busca.strip():
        sql += " AND (nome LIKE ? OR cnpj LIKE ?)"
        params += [f"%{busca.strip()}%"] * 2
    sql += " ORDER BY nome COLLATE NOCASE"
    return [dict(r) for r in conn.execute(sql, params)]


def historico(conn, tid: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM transportadoras_historico WHERE transportadora_id=? ORDER BY id", (tid,))]
