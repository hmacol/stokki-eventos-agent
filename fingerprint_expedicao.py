# -*- coding: utf-8 -*-
"""
fingerprint_expedicao.py

Registro local de pedidos JÁ expedidos na Stokki com o canhoto tratado
(anexado com sucesso, ou confirmado que já estava anexado) — pra não
reprocessar o mesmo pedido em toda execução do expedir_pedidos.py
(pedido do Hugo, 30/07: "estamos sempre reprocessando diversas vezes
os mesmos pedidos").

Espelha o espírito de fingerprint_importacao.py (lado da importação),
mas aqui do lado da expedição: uma tabela local (dados/dados.db) que
guarda quais códigos já foram tratados por completo.

IMPORTANTE — só marca como processado quando há CERTEZA de que a
expedição na Stokki aconteceu (ou já tinha acontecido). Falha na
expedição ou falha ao anexar NUNCA são marcadas como processadas — o
pedido continua elegível pra tentar de novo na próxima execução.

28/08 (análise de pedidos acumulados, pedido do Hugo): entrega SEM foto
de canhoto passa a ser expedida mesmo assim (canhoto_anexado=0) -- antes
ela nem entrava na busca e ficava presa na Stokki pra sempre, sem
aparecer em lista nenhuma. E as falhas passam a ser CONTADAS
(`expedicoes_falhas`): depois de MAX tentativas com o mesmo erro o
pedido sai da fila automática e entra no e-mail interno de pendências,
em vez de ser retentado a cada 30 min por 7 dias e depois sumir em
silêncio (caso real: PS-36755, 10 tentativas de "situação inválida").
`avisos_enviados` guarda o que já foi avisado por e-mail, pra não
mandar a mesma lista a cada execução de 30 min.
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expedicoes_processadas (
            codigo_ps        TEXT PRIMARY KEY,
            vuupt_service_id INTEGER,
            canhoto_anexado  INTEGER NOT NULL DEFAULT 0,
            processado_em    TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expedicoes_falhas (
            codigo_ps        TEXT PRIMARY KEY,
            vuupt_service_id INTEGER,
            motivo           TEXT,
            tentativas       INTEGER NOT NULL DEFAULT 0,
            primeira_em      TEXT,
            ultima_em        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expedicao_avisos_enviados (
            chave       TEXT PRIMARY KEY,
            enviado_em  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def registrar_falha(codigo_ps: str, motivo: str, vuupt_service_id=None) -> int:
    """Conta mais uma falha pra este pedido e devolve o total acumulado.
    Motivo diferente do anterior zera a contagem (é outro problema)."""
    codigo = _normalizar(codigo_ps)
    if not codigo:
        return 0
    try:
        conn = _conectar()
        row = conn.execute(
            "SELECT motivo, tentativas FROM expedicoes_falhas WHERE codigo_ps = ?", (codigo,)
        ).fetchone()
        if row and row[0] == motivo:
            tentativas = int(row[1] or 0) + 1
            conn.execute(
                "UPDATE expedicoes_falhas SET tentativas = ?, ultima_em = ?, vuupt_service_id = ? "
                "WHERE codigo_ps = ?", (tentativas, _agora(), vuupt_service_id, codigo))
        else:
            tentativas = 1
            conn.execute("""
                INSERT INTO expedicoes_falhas
                    (codigo_ps, vuupt_service_id, motivo, tentativas, primeira_em, ultima_em)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(codigo_ps) DO UPDATE SET
                    vuupt_service_id = excluded.vuupt_service_id,
                    motivo = excluded.motivo, tentativas = 1,
                    primeira_em = excluded.primeira_em, ultima_em = excluded.ultima_em
            """, (codigo, vuupt_service_id, motivo, _agora(), _agora()))
        conn.commit()
        conn.close()
        return tentativas
    except Exception as e:
        logger.warning(f"Erro ao registrar falha de expedição para {codigo_ps}: {e}")
        return 0


def limpar_falha(codigo_ps: str):
    """Pedido que acabou dando certo sai da tabela de falhas."""
    codigo = _normalizar(codigo_ps)
    if not codigo:
        return
    try:
        conn = _conectar()
        conn.execute("DELETE FROM expedicoes_falhas WHERE codigo_ps = ?", (codigo,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"Erro ao limpar falha de expedição para {codigo_ps}: {e}")


def falhas_persistentes(min_tentativas: int) -> dict:
    """{codigo_ps: {"motivo", "tentativas", "primeira_em", "ultima_em"}} de
    quem já falhou `min_tentativas` vezes ou mais -- sai da fila
    automática e vai pro e-mail interno de pendências."""
    try:
        conn = _conectar()
        rows = conn.execute(
            "SELECT codigo_ps, motivo, tentativas, primeira_em, ultima_em FROM expedicoes_falhas "
            "WHERE tentativas >= ?", (min_tentativas,)).fetchall()
        conn.close()
        return {r[0]: {"motivo": r[1], "tentativas": r[2], "primeira_em": r[3], "ultima_em": r[4]}
                for r in rows}
    except Exception as e:
        logger.warning(f"Erro ao listar falhas persistentes de expedição: {e}")
        return {}


def filtrar_avisos_novos(chaves: list[str]) -> list[str]:
    """Devolve só as chaves (ex.: 'SEM_COMPROVANTE:PS-123') que ainda não
    foram avisadas por e-mail."""
    try:
        conn = _conectar()
        ja = {r[0] for r in conn.execute("SELECT chave FROM expedicao_avisos_enviados")}
        conn.close()
        return [c for c in chaves if c not in ja]
    except Exception as e:
        logger.warning(f"Erro ao filtrar avisos de expedição: {e}")
        return list(chaves)


def marcar_avisos_enviados(chaves: list[str]):
    if not chaves:
        return
    try:
        conn = _conectar()
        conn.executemany(
            "INSERT OR IGNORE INTO expedicao_avisos_enviados (chave, enviado_em) VALUES (?, ?)",
            [(c, _agora()) for c in chaves])
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Erro ao marcar avisos de expedição: {e}")


def _normalizar(codigo_ps: str) -> str:
    return (codigo_ps or "").strip().lstrip("#").upper()


def ja_processado(codigo_ps: str) -> bool:
    """
    True se este pedido já foi expedido + canhoto tratado com sucesso
    numa execução anterior — pode ser pulado sem precisar consultar o
    VUUPT/Stokki de novo.
    """
    codigo = _normalizar(codigo_ps)
    if not codigo:
        return False
    try:
        conn = _conectar()
        row = conn.execute(
            "SELECT 1 FROM expedicoes_processadas WHERE codigo_ps = ?", (codigo,)
        ).fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        logger.debug(f"Erro ao checar fingerprint de expedição para {codigo_ps}: {e}")
        return False  # em dúvida, NÃO pula -- mais seguro tentar de novo


def marcar_processado(codigo_ps: str, vuupt_service_id=None, canhoto_anexado: bool = True):
    """Registra que este pedido foi expedido e o canhoto foi tratado com sucesso."""
    codigo = _normalizar(codigo_ps)
    if not codigo:
        return
    try:
        conn = _conectar()
        conn.execute("""
            INSERT INTO expedicoes_processadas
                (codigo_ps, vuupt_service_id, canhoto_anexado, processado_em)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(codigo_ps) DO UPDATE SET
                vuupt_service_id = excluded.vuupt_service_id,
                canhoto_anexado  = excluded.canhoto_anexado,
                processado_em    = excluded.processado_em
        """, (codigo, vuupt_service_id, int(canhoto_anexado),
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Erro ao marcar fingerprint de expedição para {codigo_ps}: {e}")
