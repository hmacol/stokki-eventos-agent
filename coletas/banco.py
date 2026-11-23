# -*- coding: utf-8 -*-
"""
coletas/banco.py

Tabelas das coletas da Expedicao em dados/dados.db (Hugo, 12/11): pedido de
retirada sai pela tela /expedicao/coletas, com folha impressa, nome, placa e
foto obrigatorios.

  expedicao_coletas_fila     Pedidos em "Aguardando Transportador" na Stokki,
                             lidos por coletas/ler_stokki.py (a cada 10 min).
                             retirada = transportadora e de retirada/coleta;
                             carga_inicial = ja estava la na 1a leitura (pode
                             sair pelo botao antigo da /pedidos, sem foto);
                             saiu_em = sumiu da listagem (virou Enviado).
  expedicao_coletas          Uma folha de coleta (COL-<id>).
                             status ABERTA | CONFIRMADA | CONCLUIDA | CANCELADA.
  expedicao_coletas_pedidos  Pedidos da folha. estado NA_FOLHA | NAO_SAIU |
                             EXPEDINDO | EXPEDIDO | FALHOU | RETIRADO. O indice
                             unico garante 1 folha viva por pedido.

(galpao_coletas e outra coisa: o log do coletor do Galpao 3D.)
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
FMT = "%Y-%m-%d %H:%M:%S"
ESTADOS_VIVOS = ("NA_FOLHA", "EXPEDINDO", "FALHOU")


def conectar(db_path: Path | None = None) -> sqlite3.Connection:
    caminho = db_path or DB_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=30)
    conn.row_factory = sqlite3.Row
    garantir_esquema(conn)
    return conn


def garantir_esquema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS expedicao_coletas_fila (
            id_stokki         INTEGER PRIMARY KEY,
            codigo            TEXT,
            embarcador        TEXT,
            transportadora    TEXT,
            retirada          INTEGER DEFAULT 0,
            carga_inicial     INTEGER DEFAULT 0,
            visto_primeiro_em TEXT,
            visto_ultimo_em   TEXT,
            saiu_em           TEXT
        );
        CREATE TABLE IF NOT EXISTS expedicao_coletas_leituras (
            quando    TEXT PRIMARY KEY,
            resultado TEXT,
            obs       TEXT
        );
        CREATE TABLE IF NOT EXISTS expedicao_coletas (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            transportadora TEXT,
            embarcador     TEXT,
            status         TEXT NOT NULL,
            criada_em      TEXT,
            criada_por     TEXT,
            confirmada_em  TEXT,
            confirmada_por TEXT,
            motorista_nome TEXT,
            placa          TEXT,
            foto_arquivo   TEXT,
            execucao_id    INTEGER,
            token          TEXT,
            simulada       INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS expedicao_coletas_pedidos (
            coleta_id    INTEGER NOT NULL,
            id_stokki    INTEGER NOT NULL,
            codigo       TEXT,
            embarcador   TEXT,
            unidades     INTEGER,
            nf           TEXT,
            destinatario TEXT,
            estado       TEXT NOT NULL,
            mensagem     TEXT,
            PRIMARY KEY (coleta_id, id_stokki)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_expedicao_coletas_pedido_vivo
            ON expedicao_coletas_pedidos (id_stokki) WHERE estado IN ('NA_FOLHA', 'EXPEDINDO', 'FALHOU');
    """)
    conn.commit()


def numero(coleta_id: int) -> str:
    return f"COL-{coleta_id}"
