# -*- coding: utf-8 -*-
"""
stokki/sessao_uso.py

Trava cooperativa de "quem está usando a Stokki agora" (pedido do Hugo,
08/09/2026, pra máscara de envio de pedidos do portal do cliente): a
Stokki derruba a sessão anterior quando outro processo faz login com o
mesmo usuário (ver memória stokki-sessao-e-anexo-limitacoes), então quem
vai abrir um login novo (worker do portal, importador por e-mail) checa
antes se alguém está no meio de uma execução e, se estiver, ESPERA a vez
em vez de atropelar.

É uma linha única na tabela `stokki_sessao_uso` do dados.db (mesmo banco
que o painel e o portal compartilham na VPS), com dono + validade
(expira_em) -- processo que morre sem liberar não trava ninguém pra
sempre. Além da linha, `em_uso()` também considera "em uso" qualquer
execução do painel de agentes em RODANDO (painel_execucoes), já que quase
todo agente disparado pelo painel abre a StokkiSession.

Uso:
    from stokki.sessao_uso import adquirir, liberar, em_uso

    if adquirir("portal-envios", ttl_segundos=900, esperar_segundos=1800):
        try:
            ...usa a Stokki...
        finally:
            liberar("portal-envios")
"""
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
_CHAVE = "principal"
_FMT = "%Y-%m-%d %H:%M:%S"


def _conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stokki_sessao_uso (
            chave      TEXT PRIMARY KEY,
            dono       TEXT NOT NULL,
            desde      TEXT NOT NULL,
            expira_em  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _agora() -> datetime:
    return datetime.now().replace(microsecond=0)


def _execucao_painel_rodando(conn: sqlite3.Connection) -> str | None:
    """Nome do agente do painel em RODANDO (se houver) -- a tabela pode
    não existir num banco novo/local, aí não conta."""
    try:
        row = conn.execute(
            "SELECT agente_nome FROM painel_execucoes WHERE status = 'RODANDO' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["agente_nome"] if row else None


def em_uso(conn: sqlite3.Connection | None = None, ignorar_dono: str | None = None) -> str | None:
    """Quem está usando a Stokki agora ('painel: <agente>' ou o dono da
    trava), ou None se está livre. Trava vencida conta como livre."""
    proprio = conn is None
    conn = conn or _conectar()
    try:
        agente = _execucao_painel_rodando(conn)
        if agente:
            return f"painel: {agente}"
        row = conn.execute("SELECT dono, expira_em FROM stokki_sessao_uso WHERE chave = ?", (_CHAVE,)).fetchone()
        if not row:
            return None
        if row["dono"] == ignorar_dono:
            return None
        try:
            if datetime.strptime(row["expira_em"], _FMT) <= _agora():
                return None
        except ValueError:
            return None
        return row["dono"]
    finally:
        if proprio:
            conn.close()


def adquirir(dono: str, ttl_segundos: int = 900, esperar_segundos: int = 0, intervalo: float = 10.0) -> bool:
    """Tenta ficar com a Stokki. Se estiver ocupada, espera (até
    esperar_segundos, checando a cada `intervalo`) -- é a "fila" pedida
    pelo Hugo. Devolve True se conseguiu. ttl_segundos é a validade da
    trava (renove com `renovar` em trabalhos longos)."""
    limite = time.monotonic() + max(0, esperar_segundos)
    avisado = None
    while True:
        conn = _conectar()
        try:
            ocupante = em_uso(conn, ignorar_dono=dono)
            if not ocupante:
                agora = _agora()
                conn.execute("""
                    INSERT INTO stokki_sessao_uso (chave, dono, desde, expira_em) VALUES (?, ?, ?, ?)
                    ON CONFLICT(chave) DO UPDATE SET dono = excluded.dono, desde = excluded.desde,
                                                     expira_em = excluded.expira_em
                """, (_CHAVE, dono, agora.strftime(_FMT), (agora + timedelta(seconds=ttl_segundos)).strftime(_FMT)))
                conn.commit()
                if avisado:
                    logger.info(f"[sessao_uso] Stokki liberada por '{avisado}' -- '{dono}' assumiu.")
                return True
        finally:
            conn.close()
        if ocupante != avisado:
            logger.info(f"[sessao_uso] Stokki em uso por '{ocupante}' -- '{dono}' aguardando a vez.")
            avisado = ocupante
        if time.monotonic() >= limite:
            return False
        time.sleep(min(intervalo, max(0.0, limite - time.monotonic())) or 0.1)


def renovar(dono: str, ttl_segundos: int = 900) -> None:
    conn = _conectar()
    try:
        conn.execute("UPDATE stokki_sessao_uso SET expira_em = ? WHERE chave = ? AND dono = ?",
                     ((_agora() + timedelta(seconds=ttl_segundos)).strftime(_FMT), _CHAVE, dono))
        conn.commit()
    finally:
        conn.close()


def liberar(dono: str) -> None:
    conn = _conectar()
    try:
        conn.execute("DELETE FROM stokki_sessao_uso WHERE chave = ? AND dono = ?", (_CHAVE, dono))
        conn.commit()
    finally:
        conn.close()
