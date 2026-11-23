# -*- coding: utf-8 -*-
"""
limite_piloto.py

Limite da COPIA DE PILOTO da tela de Notificacoes (controle_notificacoes.py)
nas rotinas que nao registram nada quando o envio e so uma copia pro piloto
(insucesso, agendamento, redespacho): no maximo 1 copia por pedido por dia,
senao o piloto receberia a mesma copia a cada rodada enquanto o pedido
seguir pendente. Usa a tabela notificacoes_enviadas (tipo, chave, enviado_em)
com tipo "piloto_<tipo>" e chave "<codigo do pedido>|<AAAA-MM-DD>".

Nunca derruba a rotina: erro de banco ao ler vale "nao enviado hoje", ao
gravar so loga.
"""
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

import controle_notificacoes

logger = logging.getLogger(__name__)


def _chave(codigo, hoje: date | None) -> str:
    return f"{str(codigo or '').lstrip('#')}|{(hoje or date.today()).isoformat()}"


def _conectar(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path) if db_path else controle_notificacoes.DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS notificacoes_enviadas ("
                 "tipo TEXT NOT NULL, chave TEXT NOT NULL, enviado_em TEXT NOT NULL, PRIMARY KEY (tipo, chave))")
    return conn


def ja_enviado_hoje(tipo: str, codigo, db_path=None, hoje: date | None = None) -> bool:
    try:
        conn = _conectar(db_path)
        try:
            return conn.execute("SELECT 1 FROM notificacoes_enviadas WHERE tipo = ? AND chave = ?",
                                (tipo, _chave(codigo, hoje))).fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(f"limite_piloto: nao leu {tipo} de {codigo} ({e}); vale 'nao enviado hoje'")
        return False


def registrar_enviado(tipo: str, codigo, db_path=None, hoje: date | None = None) -> None:
    try:
        conn = _conectar(db_path)
        try:
            conn.execute("INSERT OR REPLACE INTO notificacoes_enviadas (tipo, chave, enviado_em) VALUES (?, ?, ?)",
                         (tipo, _chave(codigo, hoje), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(f"limite_piloto: nao gravou {tipo} de {codigo} ({e})")
