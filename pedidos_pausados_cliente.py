# -*- coding: utf-8 -*-
"""
Pedidos pausados a pedido do cliente (Em espera, 27/10/2026): fonte unica da
verdade. Pausado = linha aberta (retomado_em IS NULL). Nada na Stokki muda;
a pausa so tira o pedido da roteirizacao (e marca na tela de planejamento).

Uma linha por pausa, pelo codigo base (PS-NNNNN, reentrega -R1 conta como o
mesmo pedido). A linha nunca e apagada: fechar grava retomado_em e fim
('retomado' ou 'cancelado').

Quem le: criar_rotas_diarias / incrementar_rotas (separar_pausados) e a tela
de planejamento (pausados_por_servico). Falha de leitura nunca bloqueia nada:
devolve vazio e loga.
"""
import logging
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402  (codigos do servico da Vuupt)
from nucleo.prontidao_stokki import _codigo_base  # noqa: E402  ('#PS-1-R1' -> 'PS-1')

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"
FINS = ("retomado", "cancelado")


def conectar(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pedidos_pausados_cliente (
            id INTEGER PRIMARY KEY,
            codigo TEXT NOT NULL,
            id_stokki INTEGER,
            envio_id INTEGER,
            service_ids TEXT,
            pausado_em TEXT NOT NULL,
            retomado_em TEXT,
            fim TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pausados_cliente_codigo ON pedidos_pausados_cliente (codigo);
    """)
    conn.commit()
    return conn


def abrir(conn: sqlite3.Connection, codigo: str, id_stokki, envio_id, service_ids: list[int], agora: str) -> int:
    """Abre a pausa. Se ja houver uma aberta pro codigo, devolve o id dela."""
    base = _codigo_base(codigo)
    existente = conn.execute(
        "SELECT id FROM pedidos_pausados_cliente WHERE codigo = ? AND retomado_em IS NULL", (base,)).fetchone()
    if existente:
        return existente["id"]
    cur = conn.execute(
        "INSERT INTO pedidos_pausados_cliente (codigo, id_stokki, envio_id, service_ids, pausado_em) "
        "VALUES (?, ?, ?, ?, ?)",
        (base, id_stokki, envio_id, ",".join(str(i) for i in service_ids or []), agora))
    conn.commit()
    return cur.lastrowid


def fechar(conn: sqlite3.Connection, codigo: str, fim: str, agora: str) -> bool:
    """Fecha a pausa aberta do codigo. False se nao havia nenhuma."""
    if fim not in FINS:
        raise ValueError(f"fim invalido: {fim!r} (esperado {FINS})")
    cur = conn.execute(
        "UPDATE pedidos_pausados_cliente SET retomado_em = ?, fim = ? WHERE codigo = ? AND retomado_em IS NULL",
        (agora, fim, _codigo_base(codigo)))
    conn.commit()
    return cur.rowcount > 0


def codigos_pausados(db_path=None) -> set[str]:
    """Codigos base com pausa aberta. Erro -> vazio + log."""
    try:
        conn = conectar(db_path)
        try:
            return {r["codigo"] for r in conn.execute(
                "SELECT codigo FROM pedidos_pausados_cliente WHERE retomado_em IS NULL")}
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"nao carregou pedidos pausados ({e}); seguindo sem a marca")
        return set()


def _bases_do_servico(servico: dict) -> list[str]:
    return [_codigo_base(c) for c in pedidos_dedicados.codigos_do_servico(servico)]


def separar_pausados(servicos: list[dict], db_path=None) -> tuple[list[dict], list[dict]]:
    """(seguem na rodada, pausados). Servico com mais de um codigo sai
    inteiro se qualquer um estiver pausado."""
    pausados = codigos_pausados(db_path)
    if not pausados:
        return servicos, []
    restantes, fora = [], []
    for s in servicos:
        (fora if any(c in pausados for c in _bases_do_servico(s)) else restantes).append(s)
    if fora:
        logger.info(f"{len(fora)} pedido(s) pausado(s) a pedido do cliente, fora desta rodada: "
                    f"{[s.get('code') for s in fora]}")
    return restantes, fora


def pausados_por_servico(servicos: list[dict], db_path=None) -> set[int]:
    """Ids dos servicos pausados, pra tela marcar."""
    pausados = codigos_pausados(db_path)
    if not pausados:
        return set()
    return {s["id"] for s in servicos if any(c in pausados for c in _bases_do_servico(s))}
