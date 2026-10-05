# -*- coding: utf-8 -*-
"""
registro_dia_fixo.py

Duas tabelas de controle da regra de dia fixo v2 (Hugo, 03/10/2026 --
spec docs/superpowers/specs/2026-10-03-dias-fixos-v2-design.md):

  agendamentos_origem   origem de cada scheduled_start que o PROPRIO sistema
                        gravou: DIA_FIXO (regioes_dia_fixo.aplicar_regioes_dia_fixo)
                        ou EQUIPE (Planejamento: reagendar_pedido/reagendar_pedidos).
                        Data sem linha DIA_FIXO/EQUIPE = data do CLIENTE
                        (fora_dia_fixo.py). Edicao direta na tela da Vuupt nao
                        passa por aqui e conta como do cliente.
  avisos_fora_dia_fixo  um aviso por pedido ao embarcador quando a data dele
                        cai fora do dia de visita (notificar_fora_dia_fixo.py).

Chave: codigo base do pedido (PS-NNNNN, pedidos_dedicados.normalizar_codigo:
'-R1' de reentrega cai no mesmo codigo) e/ou service_id (a tela do
Planejamento so manda o id). Linhas nunca sao apagadas.
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

ORIGEM_DIA_FIXO = "DIA_FIXO"
ORIGEM_EQUIPE = "EQUIPE"
ORIGENS_NAO_CLIENTE = (ORIGEM_DIA_FIXO, ORIGEM_EQUIPE)


def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agendamentos_origem (
            codigo TEXT NOT NULL DEFAULT '',
            service_id INTEGER NOT NULL DEFAULT 0,
            data TEXT NOT NULL,
            origem TEXT NOT NULL,
            gravado_em TEXT NOT NULL,
            PRIMARY KEY (codigo, service_id, data, origem)
        );
        CREATE INDEX IF NOT EXISTS idx_agendamentos_origem_sid ON agendamentos_origem (service_id, data);
        CREATE INDEX IF NOT EXISTS idx_agendamentos_origem_data ON agendamentos_origem (data, codigo);
        CREATE TABLE IF NOT EXISTS avisos_fora_dia_fixo (
            codigo TEXT PRIMARY KEY,
            service_id INTEGER,
            data TEXT,
            enviado_em TEXT NOT NULL,
            canais TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def _quando(agora: datetime | None) -> str:
    return (agora or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _service_id(servico: dict) -> int:
    try:
        return int(servico.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def registrar_origem(conn: sqlite3.Connection, servico: dict, data: date, origem: str,
                     agora: datetime | None = None) -> int:
    """Uma linha por codigo do servico (com o service_id junto); servico sem
    codigo grava uma linha so com o service_id. Devolve quantas linhas novas."""
    sid = _service_id(servico)
    codigos = pedidos_dedicados.codigos_do_servico(servico) or ([""] if sid else [])
    gravados = 0
    for codigo in codigos:
        cur = conn.execute(
            "INSERT OR IGNORE INTO agendamentos_origem (codigo, service_id, data, origem, gravado_em) "
            "VALUES (?, ?, ?, ?, ?)", (codigo, sid, data.isoformat(), origem, _quando(agora)))
        gravados += cur.rowcount
    conn.commit()
    return gravados


def data_nao_e_do_cliente(conn: sqlite3.Connection, servico: dict, data: date) -> bool:
    """A data foi gravada pelo dia fixo ou pela equipe pra este pedido
    (qualquer codigo do servico OU o mesmo service_id)?"""
    codigos = pedidos_dedicados.codigos_do_servico(servico)
    sid = _service_id(servico)
    if not codigos and not sid:
        return False
    condicoes, params = [], [data.isoformat(), *ORIGENS_NAO_CLIENTE]
    if codigos:
        condicoes.append(f"codigo IN ({','.join('?' * len(codigos))})")
        params += codigos
    if sid:
        condicoes.append("service_id = ?")
        params.append(sid)
    return conn.execute(
        f"SELECT 1 FROM agendamentos_origem WHERE data = ? AND origem IN (?, ?) AND ({' OR '.join(condicoes)}) LIMIT 1",
        params).fetchone() is not None


def registrar_origens(itens: list[dict], origem: str, db_path=DB_PATH) -> int:
    """Tolerante: falha so loga (o agendamento na Vuupt ja foi gravado e nao
    pode ser desfeito por causa do registro). Cada item: {"servico", "data"}."""
    if not itens:
        return 0
    try:
        conn = conectar(db_path)
        try:
            return sum(registrar_origem(conn, i["servico"], i["data"], origem) for i in itens)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"nao registrou a origem ({origem}) de {len(itens)} agendamento(s) ({e})")
        return 0


def registrar_aviso(conn: sqlite3.Connection, servico: dict, data: date | None, canais: list[str],
                    agora: datetime | None = None) -> None:
    """Um registro por codigo; o segundo e ignorado (INSERT OR IGNORE)."""
    for codigo in pedidos_dedicados.codigos_do_servico(servico):
        conn.execute(
            "INSERT OR IGNORE INTO avisos_fora_dia_fixo (codigo, service_id, data, enviado_em, canais) "
            "VALUES (?, ?, ?, ?, ?)",
            (codigo, servico.get("id"), data.isoformat() if data else None, _quando(agora),
             ",".join(canais) or "nenhum"))
    conn.commit()


def ja_avisado(conn: sqlite3.Connection, servico: dict) -> bool:
    codigos = pedidos_dedicados.codigos_do_servico(servico)
    if not codigos:
        return False
    marcadores = ",".join("?" * len(codigos))
    return conn.execute(f"SELECT 1 FROM avisos_fora_dia_fixo WHERE codigo IN ({marcadores}) LIMIT 1",
                        tuple(codigos)).fetchone() is not None
