# -*- coding: utf-8 -*-
"""
regras/alteracoes_entrega.py

Registro das alteracoes de entrega (endereco, data, horario de recebimento)
que o cliente faz pelo portal. Lido pelo portal, pela importacao horaria
(pipeline.py) e pela escolha da fonte do pool (nucleo/pool.py).
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from nucleo.normalizacao import normalizar_codigo

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"

_FMT = "%Y-%m-%d %H:%M:%S"
_SUFIXO = re.compile(r"-[RC]\d+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alteracoes_entrega_portal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    criado_em TEXT NOT NULL,
    cnpj_embarcador TEXT,
    sender_id INTEGER,
    service_id INTEGER,
    codigo TEXT,
    codigo_base TEXT,
    por TEXT,
    antes_json TEXT,
    depois_json TEXT,
    endereco_json TEXT,
    latitude REAL,
    longitude REAL,
    rota_id_tirada INTEGER,
    status TEXT NOT NULL,
    erro TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alteracoes_entrega_codigo_base
    ON alteracoes_entrega_portal (codigo_base);
"""


def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def codigo_base(codigo) -> str | None:
    """'#PS-1234-R1' -> 'PS-1234'. Vazio -> None."""
    norm = normalizar_codigo(codigo)
    if not norm:
        return None
    return _SUFIXO.sub("", norm) or None


def registrar(conn, *, cnpj_embarcador, sender_id, service_id, codigo, por,
              antes, depois, endereco, latitude, longitude, rota_id_tirada,
              status, erro="") -> int:
    cur = conn.execute(
        "INSERT INTO alteracoes_entrega_portal (criado_em, cnpj_embarcador, sender_id,"
        " service_id, codigo, codigo_base, por, antes_json, depois_json, endereco_json,"
        " latitude, longitude, rota_id_tirada, status, erro)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now().strftime(_FMT), cnpj_embarcador, sender_id, service_id,
         codigo, codigo_base(codigo), por,
         json.dumps(antes, ensure_ascii=False), json.dumps(depois, ensure_ascii=False),
         None if endereco is None else json.dumps(endereco, ensure_ascii=False),
         latitude, longitude, rota_id_tirada, status, erro))
    conn.commit()
    return cur.lastrowid


def endereco_vigente(conn, codigo) -> dict | None:
    """Endereco da ultima alteracao APLICADA com endereco, valendo tambem pra reentrega."""
    base = codigo_base(codigo)
    if not base:
        return None
    row = conn.execute(
        "SELECT endereco_json, latitude, longitude FROM alteracoes_entrega_portal"
        " WHERE codigo_base=? AND status='APLICADA' AND endereco_json IS NOT NULL"
        " ORDER BY id DESC LIMIT 1", (base,)).fetchone()
    if not row:
        return None
    return {"endereco": json.loads(row["endereco_json"]),
            "latitude": row["latitude"], "longitude": row["longitude"]}


def houve_alteracao_desde(conn, minutos: int, agora: datetime | None = None) -> bool:
    agora = agora or datetime.now()
    corte = (agora - timedelta(minutes=minutos)).strftime(_FMT)
    return conn.execute(
        "SELECT 1 FROM alteracoes_entrega_portal WHERE status='APLICADA'"
        " AND criado_em >= ? LIMIT 1", (corte,)).fetchone() is not None
