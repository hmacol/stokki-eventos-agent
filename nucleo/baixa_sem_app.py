# -*- coding: utf-8 -*-
"""
nucleo/baixa_sem_app.py

Motorista sem app (iPhone; BD_MOTORISTAS SEM_APP, Hugo 08/10): a rota
dele nao e devolvida as 15h45 e, no dia seguinte, aparece na Torre
"aguardando baixa". A tela /baixa-sem-app marca o que voltou e este
modulo conclui na Vuupt: entregue = sucesso, voltou = insucesso com o
motivo (fluxo normal de insucesso). Spec:
docs/superpowers/specs/2026-10-08-motoristas-sem-app-design.md
"""
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from nucleo import banco

logger = logging.getLogger(__name__)
FMT = "%Y-%m-%d %H:%M:%S"
PARADAS_PENDENTES = (banco.PARADA_PENDENTE, banco.PARADA_EM_DESLOCAMENTO, banco.PARADA_EM_ROTA)
STATUS_FECHADO = {"done", "canceled", "cancelled"}


def _liberar_rota_agendada(vuupt, route_id: int) -> None:
    from lalamove_integracao import _liberar_rota_agendada as liberar
    liberar(vuupt, route_id)


def garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS baixas_sem_app (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            vuupt_route_id INTEGER NOT NULL,
            agent_id       INTEGER,
            criado_por     TEXT,
            criado_em      TEXT NOT NULL,
            itens_json     TEXT NOT NULL,
            terminado_em   TEXT
        )""")
    conn.commit()


# ── Torre ─────────────────────────────────────────────────────────────────────

def rotas_aguardando_baixa(conn: sqlite3.Connection, hoje: date, sem_app: set[int]) -> list[dict]:
    if not sem_app:
        return []
    ags = ",".join("?" * len(sem_app))
    pend = ",".join("?" * len(PARADAS_PENDENTES))
    return [dict(r) for r in conn.execute(f"""
        SELECT r.vuupt_route_id, r.agent_id, r.motorista_nome, r.data_rota, r.nome,
               SUM(CASE WHEN p.situacao IN ({pend}) THEN 1 ELSE 0 END) AS abertos
        FROM nucleo_rotas r JOIN nucleo_paradas p ON p.rota_id = r.id
        WHERE r.agent_id IN ({ags}) AND r.data_rota < ? AND r.vuupt_route_id IS NOT NULL
          AND r.status NOT IN (?, ?)
        GROUP BY r.id HAVING abertos > 0
        ORDER BY r.data_rota, r.id
    """, (*PARADAS_PENDENTES, *sorted(sem_app), hoje.isoformat(), banco.ROTA_CONCLUIDA, banco.ROTA_CANCELADA))]


def excecoes_torre(data_iso: str, db_path: Path | None = None, hoje: date | None = None,
                   sem_app: set[int] | None = None) -> list[dict]:
    """Formato de torre_controle._montar_excecoes (sem _epoch): uma rota de
    motorista sem app, de dia anterior, com pedido pendente = um item."""
    hoje = hoje or date.today()
    if sem_app is None:
        from regras.preferencias_motoristas import agent_ids_sem_app
        sem_app = agent_ids_sem_app()
    if not sem_app:
        return []
    conn = banco.conectar(db_path)
    try:
        rotas = rotas_aguardando_baixa(conn, hoje, sem_app)
    finally:
        conn.close()
    itens = []
    for r in rotas:
        dia = datetime.strptime(r["data_rota"][:10], "%Y-%m-%d").strftime("%d/%m")
        itens.append({
            "id": f"semapp:{r['vuupt_route_id']}",
            "severidade": "atencao", "tipo": "Sem app",
            "descricao": f"Rota de {r['motorista_nome'] or r['agent_id']} de {dia} aguardando baixa "
                         f"({r['abertos']} pedido(s)).",
            "quando": None,
            "acao": {"tipo": "link", "url": f"/baixa-sem-app?rota={r['vuupt_route_id']}", "rotulo": "Dar baixa"},
        })
    return itens


# ── Baixa ─────────────────────────────────────────────────────────────────────

def concluir_servico(vuupt, service_id: int, sucesso: bool, failed_reason_id: int | None,
                     agent_id: int | None) -> str:
    """Mesmo caminho de lalamove_integracao._concluir_na_vuupt, com insucesso.
    GET sem resposta (429/timeout) levanta: nunca escreve num servico que nao leu."""
    servico = vuupt.buscar_servico_por_id(service_id)
    if not servico:
        raise RuntimeError(f"Vuupt nao devolveu o servico {service_id}; nada foi alterado, tente de novo.")
    status = str(servico.get("status") or "")
    if status in STATUS_FECHADO:
        return "ja_fechado"
    if status in ("", "not_assigned"):
        route_id = servico.get("route_id")
        if route_id:
            _liberar_rota_agendada(vuupt, int(route_id))
            status = str((vuupt.buscar_servico_por_id(service_id) or {}).get("status") or "")
        elif agent_id:
            vuupt.atribuir_agente(service_id, agent_id)
            status = "assigned"
    vuupt.concluir_como_agente(service_id, sucesso=sucesso,
                               failed_reason_id=None if sucesso else failed_reason_id, status_atual=status)
    return "entregue" if sucesso else "insucesso"


def criar_lote(conn: sqlite3.Connection, vuupt_route_id: int, agent_id: int, por: str, itens: list[dict]) -> int:
    garantir_tabela(conn)
    itens = [{"service_id": int(i["service_id"]), "codigo": i.get("codigo") or "",
              "entregue": bool(i["entregue"]), "failed_reason_id": i.get("failed_reason_id"),
              "status": "pendente"} for i in itens]
    cur = conn.execute(
        "INSERT INTO baixas_sem_app (vuupt_route_id, agent_id, criado_por, criado_em, itens_json) VALUES (?, ?, ?, ?, ?)",
        (vuupt_route_id, agent_id, por, datetime.now().strftime(FMT), json.dumps(itens, ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid


MINUTOS_LOTE_ATIVO = 10


def lote_em_andamento(conn: sqlite3.Connection, vuupt_route_id: int) -> bool:
    """Ha lote dessa rota sem terminar criado nos ultimos 10 min? Barra dois
    operadores (ou um reload) baixando a mesma rota ao mesmo tempo. Lote
    mais velho que isso e tratado como morto (painel reiniciou no meio) e
    libera nova tentativa -- servico ja fechado e pulado."""
    garantir_tabela(conn)
    limite = (datetime.now() - timedelta(minutes=MINUTOS_LOTE_ATIVO)).strftime(FMT)
    return conn.execute(
        "SELECT 1 FROM baixas_sem_app WHERE vuupt_route_id = ? AND terminado_em IS NULL AND criado_em >= ? LIMIT 1",
        (vuupt_route_id, limite)).fetchone() is not None


def ler_lote(conn: sqlite3.Connection, lote_id: int) -> dict | None:
    garantir_tabela(conn)
    row = conn.execute("SELECT * FROM baixas_sem_app WHERE id = ?", (lote_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["itens"] = json.loads(d.pop("itens_json"))
    d["terminado"] = bool(d["terminado_em"])
    return d


def _salvar_itens(conn: sqlite3.Connection, lote_id: int, itens: list[dict], terminado: bool = False) -> None:
    conn.execute("UPDATE baixas_sem_app SET itens_json = ?" + (", terminado_em = ?" if terminado else "") +
                 " WHERE id = ?",
                 (json.dumps(itens, ensure_ascii=False), *([datetime.now().strftime(FMT)] if terminado else []), lote_id))
    conn.commit()


def _registrar_tratativa(item: dict, por: str) -> None:
    import tratativas
    from insucesso_entrega.motivos_falha import texto_do_motivo
    if item["status"] == "entregue":
        tratativas.registrar_evento(item["codigo"], "BAIXA_SEM_APP", "BAIXA_SEM_APP",
                                    service_id=item["service_id"],
                                    texto=f"Baixa de motorista sem app por {por}: entregue.")
    elif item["status"] == "insucesso":
        motivo = texto_do_motivo(item["failed_reason_id"])
        tratativas.registrar_evento(item["codigo"], "BAIXA_SEM_APP", "BAIXA_SEM_APP",
                                    service_id=item["service_id"], motivo_id=item["failed_reason_id"],
                                    motivo_texto=motivo,
                                    texto=f"Baixa de motorista sem app por {por}: voltou ({motivo}).")


def _ressincronizar(vuupt, ids: list[int]) -> None:
    from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids
    ressincronizar_ids(vuupt, ids)


def executar_lote(lote_id: int, vuupt, db_path: Path | None = None) -> dict:
    """Roda em thread de fundo (painel). Salva o andamento item a item."""
    conn = banco.conectar(db_path)
    try:
        lote = ler_lote(conn, lote_id)
        itens = lote["itens"]
        for item in itens:
            if item["status"] != "pendente":
                continue
            try:
                item["status"] = concluir_servico(vuupt, item["service_id"], item["entregue"],
                                                  item.get("failed_reason_id"), lote["agent_id"])
            except Exception as e:  # noqa: BLE001 -- um item com erro nao para os outros
                item["status"], item["erro"] = "erro", str(e)[:200]
                logger.warning(f"Baixa sem app lote {lote_id}: {item['codigo']} falhou: {e}")
            _salvar_itens(conn, lote_id, itens)
            if item["status"] in ("entregue", "insucesso"):
                try:
                    _registrar_tratativa(item, lote["criado_por"] or "?")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Tratativa de {item['codigo']} nao registrada: {e}")
        _salvar_itens(conn, lote_id, itens, terminado=True)
        feitos = [i["service_id"] for i in itens if i["status"] in ("entregue", "insucesso")]
        if feitos:
            try:
                _ressincronizar(vuupt, feitos)
            except Exception as e:  # noqa: BLE001 -- o timer de 15 min alcanca
                logger.warning(f"Ressincronizacao do nucleo falhou: {e}")
        return ler_lote(conn, lote_id)
    finally:
        conn.close()
