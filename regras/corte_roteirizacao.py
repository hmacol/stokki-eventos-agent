# -*- coding: utf-8 -*-
"""
Horario de corte da roteirizacao (unico). Editavel na tela /planejamento so por
administrador (nivel "total"). Ate o corte: pedido entra como "finalizado hoje"
e pode ser roteirizado; depois do corte: vai pra agendamento (dia seguinte ou
proximo dia de rota da regiao).
Lido por regras/agendamento_automatico.py e roteirizacao/criar_rotas_diarias.py.
Falha ao ler o banco => padrao (nunca derruba a rodada).
"""
import re
import sqlite3
from datetime import datetime, time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
PADRAO = time(14, 15)
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CHAVE = "corte_roteirizacao"


def _conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS corte_roteirizacao_config ("
        "chave TEXT PRIMARY KEY, valor TEXT NOT NULL, alterado_por TEXT, alterado_em TEXT)")
    return conn


def _para_time(hhmm: str) -> time:
    return time(int(hhmm[:2]), int(hhmm[3:5]))


def obter_corte(db_path=DB_PATH) -> time:
    try:
        conn = _conectar(db_path)
        try:
            linha = conn.execute("SELECT valor FROM corte_roteirizacao_config WHERE chave = ?",
                                 (_CHAVE,)).fetchone()
        finally:
            conn.close()
        if linha and _HHMM.match(linha[0]):
            return _para_time(linha[0])
    except Exception:
        pass
    return PADRAO


def detalhe_corte(db_path=DB_PATH) -> dict:
    """Corte + quem alterou por ultimo (pra tela)."""
    base = {"hhmm": obter_corte(db_path).strftime("%H:%M"), "alterado_por": "", "alterado_em": ""}
    try:
        conn = _conectar(db_path)
        try:
            linha = conn.execute("SELECT alterado_por, alterado_em FROM corte_roteirizacao_config "
                                 "WHERE chave = ?", (_CHAVE,)).fetchone()
        finally:
            conn.close()
        if linha:
            base["alterado_por"], base["alterado_em"] = linha[0] or "", linha[1] or ""
    except Exception:
        pass
    return base


def definir_corte(hhmm: str, usuario: str, db_path=DB_PATH) -> str:
    """Grava o corte. Levanta ValueError se nao for HH:MM valido."""
    hhmm = (hhmm or "").strip()
    if not _HHMM.match(hhmm):
        raise ValueError("Horario invalido: use HH:MM (00:00 a 23:59).")
    conn = _conectar(db_path)
    try:
        conn.execute(
            "INSERT INTO corte_roteirizacao_config (chave, valor, alterado_por, alterado_em) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor, "
            "alterado_por = excluded.alterado_por, alterado_em = excluded.alterado_em",
            (_CHAVE, hhmm, usuario or "", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    return hhmm


def dentro_do_corte(hora: time, db_path=DB_PATH) -> bool:
    """True se `hora` esta ate o corte (inclusive): pedido do dia."""
    return hora <= obter_corte(db_path)
