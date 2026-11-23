# -*- coding: utf-8 -*-
"""
Pedidos segurados (Hugo, 29/09/2026): pedido de rota fraca adiado por 1
dia util pra juntar com o volume do dia seguinte (ver rotas_fracas.py).
Uma linha por pedido (codigo base PS-NNNNN): e a chave primaria que
garante o maximo de 1 adiamento. A linha nunca e apagada.

Quem le:
  - criar_rotas_diarias / incrementar_rotas: separar_segurados tira da
    rodada o pedido adiado pra DEPOIS da data alvo;
  - planejamento (pool): segurados_por_servico pro chip "Segurado";
  - vigia: le a tabela direto por SQL (vigia/vigiar.py).
"""
import logging
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402  (normalizacao do codigo do pedido)

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"


def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pedidos_segurados (
            codigo TEXT PRIMARY KEY,
            service_id INTEGER,
            segurado_em TEXT NOT NULL,
            data_alvo_original TEXT NOT NULL,
            data_nova TEXT NOT NULL,
            prazo_final TEXT NOT NULL,
            motivo TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_segurados_data_nova ON pedidos_segurados (data_nova);
    """)
    conn.commit()
    return conn


def marcar(conn: sqlite3.Connection, itens: list[tuple[dict, date]], data_alvo: date, data_nova: date,
           motivo: str, agora: datetime | None = None) -> int:
    """Cada item: (servico, prazo_final). Pedido ja marcado e ignorado
    (INSERT OR IGNORE). Devolve quantos codigos foram gravados."""
    quando = (agora or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    gravados = 0
    for servico, prazo in itens:
        for codigo in pedidos_dedicados.codigos_do_servico(servico):
            cur = conn.execute(
                "INSERT OR IGNORE INTO pedidos_segurados (codigo, service_id, segurado_em, data_alvo_original, "
                "data_nova, prazo_final, motivo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (codigo, servico.get("id"), quando, data_alvo.isoformat(), data_nova.isoformat(),
                 prazo.isoformat(), motivo))
            gravados += cur.rowcount
    conn.commit()
    return gravados


def codigos_segurados(conn: sqlite3.Connection) -> set[str]:
    return {r["codigo"] for r in conn.execute("SELECT codigo FROM pedidos_segurados")}


def segurados_ativos(conn: sqlite3.Connection, data: date) -> dict[str, dict]:
    """{codigo: linha} dos pedidos adiados pra DEPOIS de `data`."""
    rows = conn.execute("SELECT * FROM pedidos_segurados WHERE data_nova > ?", (data.isoformat(),)).fetchall()
    return {r["codigo"]: dict(r) for r in rows}


def _ativos_ou_vazio(data: date, db_path) -> dict[str, dict]:
    try:
        conn = conectar(db_path)
        try:
            return segurados_ativos(conn, data)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"nao carregou pedidos segurados ({e}); seguindo sem a marca")
        return {}


def separar_segurados(servicos: list[dict], data_alvo: date, db_path=DB_PATH) -> tuple[list[dict], list[dict]]:
    """(seguem na rodada, segurados pra depois de data_alvo). Servico com
    mais de um codigo sai inteiro se qualquer um estiver segurado."""
    ativos = _ativos_ou_vazio(data_alvo, db_path)
    if not ativos:
        return servicos, []
    restantes, fora = [], []
    for s in servicos:
        (fora if any(c in ativos for c in pedidos_dedicados.codigos_do_servico(s)) else restantes).append(s)
    if fora:
        logger.info(f"{len(fora)} pedido(s) segurado(s) pra consolidar, fora desta rodada: "
                    f"{[s.get('code') for s in fora]}")
    return restantes, fora


def segurados_por_servico(servicos: list[dict], data_alvo: date, db_path=DB_PATH) -> dict[int, dict]:
    """{service_id: {data_nova, prazo_final}} pros servicos segurados."""
    ativos = _ativos_ou_vazio(data_alvo, db_path)
    mapa: dict[int, dict] = {}
    for s in servicos:
        for c in pedidos_dedicados.codigos_do_servico(s):
            if c in ativos:
                mapa[s["id"]] = {"data_nova": ativos[c]["data_nova"], "prazo_final": ativos[c]["prazo_final"]}
                break
    return mapa
