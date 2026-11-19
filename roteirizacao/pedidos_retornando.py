# -*- coding: utf-8 -*-
"""
Pedidos Retornando: pedido tirado de uma rota que o motorista ja iniciou
esta fisicamente no carro dele. Fica retido (fora do pool, do rascunho e
do incremento) ate o galpao confirmar o recebimento na tela /retornos.

Uma linha por retirada. O indice unico parcial (service_id enquanto
recebido_em IS NULL) garante no maximo 1 Retornando ativo por pedido; depois
de recebido o pedido pode voltar a ser retirado em outro dia.

Quem usa: edicao de rota enviada (marcar), /retornos (marcar_recebidos),
pool / rascunho / incrementar_rotas (separar_retornando).
"""
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"


def garantir_tabela(conn: sqlite3.Connection) -> None:
    """Cria tabela e indice (idempotente). executescript faz COMMIT de qualquer
    transacao aberta: chamar fora de transacao aberta."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pedidos_retornando (
            id INTEGER PRIMARY KEY,
            service_id INTEGER NOT NULL,
            codigo TEXT,
            vuupt_route_id INTEGER,
            parada_id INTEGER,
            agent_id INTEGER,
            motorista_nome TEXT,
            motivo TEXT,
            observacao TEXT,
            usuario TEXT,
            criado_em TEXT NOT NULL,
            recebido_em TEXT,
            descarga_id INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_retornando_ativo
            ON pedidos_retornando (service_id) WHERE recebido_em IS NULL;
    """)
    conn.commit()


def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    garantir_tabela(conn)
    return conn


def resolver_parada_id(conn: sqlite3.Connection, vuupt_route_id, service_id) -> int | None:
    """Parada mais recente do servico naquela rota no nucleo (None se nao achar)."""
    try:
        row = conn.execute(
            "SELECT p.id FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id "
            "WHERE r.vuupt_route_id = ? AND p.service_id = ? ORDER BY p.id DESC LIMIT 1",
            (vuupt_route_id, service_id)).fetchone()
    except sqlite3.OperationalError as e:
        logger.warning(f"nao resolveu parada do nucleo ({e})")
        return None
    return row[0] if row else None


def marcar(conn: sqlite3.Connection, *, service_id, codigo, vuupt_route_id, agent_id,
           motorista_nome, motivo, observacao, usuario) -> int | None:
    """Grava e comita. Pedido que ja esta Retornando ativo devolve None."""
    parada_id = resolver_parada_id(conn, vuupt_route_id, service_id)
    if parada_id is None:
        logger.warning(f"servico {service_id}: parada do nucleo ainda nao existe; "
                       "resolver_pendentes completa depois")
    cur = conn.execute(
        "INSERT OR IGNORE INTO pedidos_retornando (service_id, codigo, vuupt_route_id, parada_id, "
        "agent_id, motorista_nome, motivo, observacao, usuario, criado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (service_id, codigo, vuupt_route_id, parada_id, agent_id, motorista_nome, motivo,
         observacao, usuario, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def resolver_pendentes(conn: sqlite3.Connection) -> int:
    """Preenche parada_id dos Retornando ativos que ficaram sem (nucleo ainda nao
    tinha a parada). Comita. Devolve quantos foram resolvidos."""
    rows = conn.execute("SELECT id, vuupt_route_id, service_id FROM pedidos_retornando "
                        "WHERE recebido_em IS NULL AND parada_id IS NULL").fetchall()
    resolvidos = 0
    for r in rows:
        parada_id = resolver_parada_id(conn, r["vuupt_route_id"], r["service_id"])
        if parada_id is not None:
            conn.execute("UPDATE pedidos_retornando SET parada_id = ? WHERE id = ?", (parada_id, r["id"]))
            resolvidos += 1
    conn.commit()
    return resolvidos


def ativos(conn: sqlite3.Connection) -> dict[int, dict]:
    """{service_id: linha} dos pedidos ainda nao recebidos pelo galpao."""
    rows = conn.execute("SELECT * FROM pedidos_retornando WHERE recebido_em IS NULL").fetchall()
    return {r["service_id"]: dict(r) for r in rows}


def marcar_recebidos(conn: sqlite3.Connection, parada_ids: list[int], descarga_id: int) -> None:
    """NAO comita: quem chama comita (roda dentro da transacao do recebimento)."""
    if not parada_ids:
        return
    marcas = ",".join("?" * len(parada_ids))
    conn.execute(
        f"UPDATE pedidos_retornando SET recebido_em = ?, descarga_id = ? "
        f"WHERE recebido_em IS NULL AND parada_id IN ({marcas})",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), descarga_id, *parada_ids))


def marcar_recebidos_por_ids(conn: sqlite3.Connection, retornando_ids: list[int], descarga_id: int) -> int:
    """Baixa por pedidos_retornando.id (Retornando sem parada no nucleo).
    NAO comita. Devolve quantas linhas ativas baixou."""
    if not retornando_ids:
        return 0
    marcas = ",".join("?" * len(retornando_ids))
    cur = conn.execute(
        f"UPDATE pedidos_retornando SET recebido_em = ?, descarga_id = ? "
        f"WHERE recebido_em IS NULL AND id IN ({marcas})",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), descarga_id, *retornando_ids))
    return cur.rowcount


def _ativos_ou_vazio(db_path) -> dict[int, dict]:
    try:
        conn = conectar(db_path)
        try:
            return ativos(conn)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"nao carregou pedidos retornando ({e}); seguindo sem a marca")
        return {}


def separar_retornando(servicos: list[dict], db_path=DB_PATH) -> tuple[list[dict], list[dict]]:
    """(ficam na rodada, retornando). Casa por service_id, nunca pelo codigo."""
    ativos_ = _ativos_ou_vazio(db_path)
    if not ativos_:
        return servicos, []
    ficam, retornando = [], []
    for s in servicos:
        (retornando if s.get("id") in ativos_ else ficam).append(s)
    if retornando:
        logger.info(f"{len(retornando)} pedido(s) retornando (no carro), fora desta rodada: "
                    f"{[s.get('code') for s in retornando]}")
    return ficam, retornando


def retornando_por_servico(servicos: list[dict], db_path=DB_PATH) -> dict[int, dict]:
    """{service_id: linha} so dos servicos dados que estao Retornando."""
    ativos_ = _ativos_ou_vazio(db_path)
    return {s["id"]: ativos_[s["id"]] for s in servicos if s.get("id") in ativos_}
