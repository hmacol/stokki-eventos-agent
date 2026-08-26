# -*- coding: utf-8 -*-
"""
nucleo/sincronizar_vuupt.py

Puxa da VUUPT o estado das rotas do dia (status, motorista, paradas com
completed_at / motivo de insucesso) e espelha em nucleo_rotas /
nucleo_paradas / nucleo_eventos / nucleo_pedidos -- Fase A do
DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md.

Enquanto a VUUPT existir, é ESTE job que faz o histórico acumular (a
Torre continua lendo a VUUPT ao vivo; aqui só se grava). Cada mudança de
situação de parada/rota vira um evento com origem=VUUPT_SYNC -- com a
ressalva conhecida de que o `completed_at` da VUUPT chega em lote
(revisar_complexidade_entrega.py:19-26), então `ocorrido_em` desses
eventos é o que a VUUPT diz, e `recebido_em` é quando vimos.

COMO USAR (timer systemd na VPS a cada 30 min, infra/stokki-nucleo-sincronizar-vuupt.*):
    python nucleo/sincronizar_vuupt.py                # hoje e ontem
    python nucleo/sincronizar_vuupt.py --dias 30      # backfill: últimos 30 dias
    python nucleo/sincronizar_vuupt.py --data 2026-08-20
    python nucleo/sincronizar_vuupt.py --modo-teste   # lê a VUUPT, não grava nada

Rota que já existe aqui com provedor=APP (Fase C) nunca é tocada por este
job -- a VUUPT não é fonte de verdade dela.
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger("nucleo.sincronizar_vuupt")

from nucleo import banco, pedidos as nucleo_pedidos, tempos
from nucleo.rotas import registrar_evento

# Status brutos da VUUPT -> status do núcleo (rota). O que não bate cai
# na derivação pelas paradas (_derivar_status_rota).
_STATUS_ROTA_VUUPT = {
    "canceled": banco.ROTA_CANCELADA, "cancelled": banco.ROTA_CANCELADA,
    "done": banco.ROTA_CONCLUIDA, "finished": banco.ROTA_CONCLUIDA, "completed": banco.ROTA_CONCLUIDA,
    "closed": banco.ROTA_CONCLUIDA,
    "on_route": banco.ROTA_EM_ROTA, "in_progress": banco.ROTA_EM_ROTA, "started": banco.ROTA_EM_ROTA,
    "accepted": banco.ROTA_ACEITA,
}

_SITUACAO_PARA_PEDIDO = {
    banco.PARADA_ENTREGUE: banco.PEDIDO_ENTREGUE,
    banco.PARADA_INSUCESSO: banco.PEDIDO_INSUCESSO,
    banco.PARADA_EM_ROTA: banco.PEDIDO_EM_ROTA,
    banco.PARADA_CANCELADA: banco.PEDIDO_CANCELADO,
    banco.PARADA_PENDENTE: banco.PEDIDO_EM_ROTA,   # está numa rota, mesmo que ainda não iniciada
}


def _json(valor) -> str | None:
    return json.dumps(valor, ensure_ascii=False, default=str) if valor is not None else None


def extrair_servicos(rota: dict) -> list[dict]:
    """'services' vem como {"data": [...]} ou lista (mesma lógica de
    painel_agentes/mapa_util.extrair_servicos_da_rota, replicada pra não
    importar o painel só por isso)."""
    wrapper = rota.get("services")
    if isinstance(wrapper, dict):
        return wrapper.get("data", []) or []
    if isinstance(wrapper, list):
        return wrapper
    return []


def situacao_do_servico(s: dict) -> str:
    """Mesma leitura da Torre (torre_controle._coletar_rotas_dia)."""
    status = s.get("status")
    if status == "canceled":
        return banco.PARADA_CANCELADA
    if status == "done":
        return banco.PARADA_INSUCESSO if s.get("status_done") == "failed" else banco.PARADA_ENTREGUE
    if status == "on_route":
        return banco.PARADA_EM_ROTA
    return banco.PARADA_PENDENTE


def _derivar_status_rota(status_bruto: str | None, situacoes: list[str]) -> str:
    mapeado = _STATUS_ROTA_VUUPT.get((status_bruto or "").lower())
    if mapeado:
        return mapeado
    ativas = [x for x in situacoes if x != banco.PARADA_CANCELADA]
    if not ativas:
        return banco.ROTA_CANCELADA if situacoes else banco.ROTA_PLANEJADA
    if all(x in (banco.PARADA_ENTREGUE, banco.PARADA_INSUCESSO) for x in ativas):
        return banco.ROTA_CONCLUIDA
    if any(x in (banco.PARADA_ENTREGUE, banco.PARADA_INSUCESSO, banco.PARADA_EM_ROTA) for x in ativas):
        return banco.ROTA_EM_ROTA
    if (status_bruto or "").lower() == "accepted":
        return banco.ROTA_ACEITA
    return banco.ROTA_PLANEJADA


def _tem_tabela(conn: sqlite3.Connection, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


class _Contexto:
    """Caches por execução: motivos de insucesso e rascunhos por vuupt_route_id."""

    def __init__(self, conn: sqlite3.Connection, nomes_motoristas: dict[int, str] | None):
        self.conn = conn
        self.nomes = nomes_motoristas or {}
        self.motivos: dict[int, tuple[int, str]] = {}
        if _tem_tabela(conn, "motivos_ocorrencia"):
            for r in conn.execute("SELECT id, vuupt_failed_reason_id, motivo_texto FROM motivos_ocorrencia"):
                if r[1] is not None:
                    self.motivos[int(r[1])] = (r[0], r[2])
        self.tem_rascunhos = _tem_tabela(conn, "rascunhos_rota")

    def rascunho_de(self, vuupt_route_id: int) -> sqlite3.Row | None:
        if not self.tem_rascunhos:
            return None
        return self.conn.execute(
            "SELECT id, km_estimado, tipo_veiculo, motorista_nome FROM rascunhos_rota WHERE vuupt_route_id = ? ORDER BY id DESC LIMIT 1",
            (vuupt_route_id,),
        ).fetchone()

    def paradas_do_rascunho(self, rascunho_id: int | None) -> dict[int, sqlite3.Row]:
        """{service_id: linha de rascunhos_parada} -- nível de dificuldade,
        destinatário e caixas que só o planejamento conhece (a VUUPT não
        devolve nada disso)."""
        if rascunho_id is None or not self.tem_rascunhos:
            return {}
        return {
            r["service_id"]: r for r in self.conn.execute(
                "SELECT service_id, nivel_dificuldade, destinatario_nome, remetente_nome, volume_caixas "
                "FROM rascunhos_parada WHERE rascunho_id = ?", (rascunho_id,)
            ).fetchall()
        }


def partes_do_titulo(titulo: str | None) -> tuple[str | None, str | None]:
    """Título do serviço na VUUPT: "#PS-37649 - 036076 / DE TOMMASO / HORTIFRUTI DCE PRECO"
    -> (remetente, destinatário) = penúltimo e último segmento. Sem " / "
    devolve (None, título)."""
    if not titulo:
        return None, None
    partes = [p.strip() for p in str(titulo).split(" / ") if p.strip()]
    if len(partes) >= 3:
        return partes[-2], partes[-1]
    if len(partes) == 2:
        return None, partes[-1]
    return None, partes[0] if partes else None


def _sincronizar_rota(ctx: _Contexto, rota: dict, stats: dict):
    conn = ctx.conn
    vuupt_route_id = rota.get("id")
    if vuupt_route_id is None:
        return
    servicos = extrair_servicos(rota)
    agent_id = rota.get("agent_id")
    start_at = rota.get("start_at") or ""
    data_rota = start_at[:10]
    agora = banco.agora()

    existente = conn.execute("SELECT * FROM nucleo_rotas WHERE vuupt_route_id = ?", (vuupt_route_id,)).fetchone()
    if existente and existente["provedor"] != banco.PROVEDOR_VUUPT:
        return  # rota do app: a VUUPT não manda nela

    rascunho_id = None
    if existente is None:
        rascunho = ctx.rascunho_de(int(vuupt_route_id))
        rascunho_id = rascunho["id"] if rascunho else None
        cur = conn.execute("""
            INSERT INTO nucleo_rotas (data_rota, nome, provedor, vuupt_route_id, rascunho_id, agent_id, vehicle_id,
                                      motorista_nome, tipo_veiculo, start_at, km_estimado, km_fonte, status,
                                      status_provedor, dados_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data_rota, rota.get("name"), banco.PROVEDOR_VUUPT, vuupt_route_id,
            rascunho["id"] if rascunho else None, agent_id, rota.get("vehicle_id"),
            ctx.nomes.get(agent_id) or (rascunho["motorista_nome"] if rascunho else None),
            rascunho["tipo_veiculo"] if rascunho else None, start_at,
            rascunho["km_estimado"] if rascunho else None,
            "ESTIMADO" if rascunho and rascunho["km_estimado"] is not None else None,
            banco.ROTA_PLANEJADA, rota.get("status"),
            _json({k: v for k, v in rota.items() if k != "services"}),
        ))
        rota_id = cur.lastrowid
        status_anterior = None
        registrar_evento(conn, "ROTA_IMPORTADA_VUUPT", banco.ORIGEM_VUUPT_SYNC, rota_id=rota_id, agent_id=agent_id,
                         dados={"vuupt_route_id": vuupt_route_id})
        stats["rotas_novas"] += 1
    else:
        rota_id = existente["id"]
        status_anterior = existente["status"]
        rascunho_id = existente["rascunho_id"]
    extras_rascunho = ctx.paradas_do_rascunho(rascunho_id)

    # Paradas ---------------------------------------------------------------
    # Achado com dados reais (26/08): numa rota CANCELADA a VUUPT continua
    # listando os serviços com o status GLOBAL deles -- que normalmente já
    # foram entregues em OUTRA rota (re-roteirização). Contar isso aqui
    # duplicaria entregas e eventos. Rota cancelada: toda parada dela vira
    # CANCELADA (nesta rota), sem contadores nem evento de entrega.
    rota_cancelada = _STATUS_ROTA_VUUPT.get((rota.get("status") or "").lower()) == banco.ROTA_CANCELADA
    situacoes = []
    entregues = insucessos = 0
    completed_max = None
    started_min = None
    paradas_existentes = {
        r["service_id"]: r for r in conn.execute(
            "SELECT id, service_id, situacao, completed_at FROM nucleo_paradas WHERE rota_id = ?", (rota_id,)
        ).fetchall()
    }
    for ordem, s in enumerate(servicos, start=1):
        service_id = s.get("id")
        situacao = banco.PARADA_CANCELADA if rota_cancelada else situacao_do_servico(s)
        situacoes.append(situacao)
        if situacao == banco.PARADA_ENTREGUE:
            entregues += 1
        elif situacao == banco.PARADA_INSUCESSO:
            insucessos += 1
        completed_at = s.get("completed_at")
        if completed_at and (completed_max is None or completed_at > completed_max):
            completed_max = completed_at
        started_at = s.get("started_at")
        if started_at and (started_min is None or started_at < started_min):
            started_min = started_at

        failed_reason_id = s.get("failed_reason_id")
        motivo = ctx.motivos.get(int(failed_reason_id)) if failed_reason_id is not None else None
        # Destinatário/remetente/nível: do rascunho (planejamento) quando a
        # rota nasceu lá; senão parseados do título do serviço da VUUPT.
        extra = extras_rascunho.get(service_id)
        remetente_titulo, destinatario_titulo = partes_do_titulo(s.get("title"))
        destinatario_nome = (extra["destinatario_nome"] if extra else None) or destinatario_titulo
        remetente_nome = (extra["remetente_nome"] if extra else None) or remetente_titulo
        nivel = extra["nivel_dificuldade"] if extra else None
        caixas = (extra["volume_caixas"] if extra else None) or s.get("dimension_3")
        campos = (
            s.get("code"), s.get("title"), s.get("address"), s.get("latitude"), s.get("longitude"),
            s.get("sender_id"), situacao, s.get("status"), s.get("status_done"),
            motivo[0] if motivo else None, motivo[1] if motivo else None, failed_reason_id,
            started_at, s.get("arrived_at"), completed_at, _json(s), agora,
            destinatario_nome, remetente_nome, nivel, caixas, s.get("customer_id"),
        )
        anterior = paradas_existentes.get(service_id)
        if anterior is None:
            cur = conn.execute("""
                INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, titulo, endereco, latitude, longitude,
                                            sender_id, situacao, status_provedor, status_done_provedor, motivo_id,
                                            motivo_texto, failed_reason_id, started_at, arrived_at, completed_at,
                                            dados_json, atualizado_em,
                                            destinatario_nome, remetente_nome, nivel_dificuldade, volume_caixas, customer_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rota_id, ordem, service_id, *campos))
            parada_id = cur.lastrowid
            situacao_anterior = None
            stats["paradas_novas"] += 1
        else:
            parada_id = anterior["id"]
            situacao_anterior = anterior["situacao"]
            conn.execute("""
                UPDATE nucleo_paradas SET
                    ordem = ?, codigo = COALESCE(?, codigo), titulo = COALESCE(?, titulo),
                    endereco = COALESCE(?, endereco), latitude = COALESCE(?, latitude), longitude = COALESCE(?, longitude),
                    sender_id = COALESCE(?, sender_id), situacao = ?, status_provedor = ?, status_done_provedor = ?,
                    motivo_id = ?, motivo_texto = ?, failed_reason_id = ?, started_at = ?, arrived_at = ?,
                    completed_at = ?, dados_json = ?, atualizado_em = ?,
                    destinatario_nome = COALESCE(destinatario_nome, ?), remetente_nome = COALESCE(remetente_nome, ?),
                    nivel_dificuldade = COALESCE(nivel_dificuldade, ?), volume_caixas = COALESCE(volume_caixas, ?),
                    customer_id = COALESCE(?, customer_id)
                WHERE id = ?
            """, (ordem, *campos, parada_id))

        # Durações a partir dos carimbos da VUUPT (started/arrived/completed)
        # -- com a ressalva da confirmação em lote (tempo ~0 é ruído).
        tempos.atualizar_tempos_parada(conn, parada_id)

        if not rota_cancelada and situacao != situacao_anterior and situacao != banco.PARADA_PENDENTE:
            registrar_evento(
                conn, situacao, banco.ORIGEM_VUUPT_SYNC, ocorrido_em=completed_at or agora,
                rota_id=rota_id, parada_id=parada_id, agent_id=agent_id,
                latitude=s.get("latitude"), longitude=s.get("longitude"),
                dados={"service_id": service_id, "code": s.get("code"), "status": s.get("status"),
                       "status_done": s.get("status_done"), "failed_reason_id": failed_reason_id,
                       "motivo": motivo[1] if motivo else None},
            )
            stats["eventos"] += 1

        # Espelho do pedido (parcial: só o que a rota traz).
        if s.get("code"):
            nucleo_pedidos.upsert_pedido(
                conn, s["code"],
                {"titulo": s.get("title"), "endereco": s.get("address"), "latitude": s.get("latitude"),
                 "longitude": s.get("longitude"), "sender_id": s.get("sender_id"), "caixas": s.get("dimension_3"),
                 "agendamento_inicio": s.get("scheduled_start"), "agendamento_fim": s.get("scheduled_end")},
                origem=banco.ORIGEM_VUUPT_SYNC, dados_json={"service": s},
                # Rota cancelada não diz nada sobre o pedido (ele segue em
                # outra rota): status None = COALESCE mantém o que já havia.
                status=None if rota_cancelada else _SITUACAO_PARA_PEDIDO.get(situacao),
                vuupt_service_id=service_id,
            )

    # Cabeçalho da rota -------------------------------------------------------
    # A rota da VUUPT traz started_at / finished_at / canceled_at próprios
    # (confirmado no payload real, 26/08) -- preferidos ao min/max das
    # paradas, que sofrem da confirmação em lote.
    status_novo = _derivar_status_rota(rota.get("status"), situacoes)
    total_ativas = len(situacoes) if rota_cancelada else sum(1 for x in situacoes if x != banco.PARADA_CANCELADA)
    iniciada = rota.get("started_at") or started_min
    concluida = rota.get("finished_at") or completed_max
    cancelada = rota.get("canceled_at")
    conn.execute("""
        UPDATE nucleo_rotas SET
            nome = COALESCE(?, nome), agent_id = COALESCE(?, agent_id), vehicle_id = COALESCE(?, vehicle_id),
            motorista_nome = COALESCE(?, motorista_nome), start_at = COALESCE(NULLIF(?, ''), start_at),
            status = ?, status_provedor = ?, total_paradas = ?, entregues = ?, insucessos = ?,
            iniciada_em = COALESCE(iniciada_em, ?),
            concluida_em = CASE WHEN ? = ? THEN COALESCE(concluida_em, ?, ?) ELSE concluida_em END,
            cancelada_em = CASE WHEN ? = ? THEN COALESCE(cancelada_em, ?) ELSE cancelada_em END,
            dados_json = ?, atualizado_em = ?
        WHERE id = ?
    """, (
        rota.get("name"), agent_id, rota.get("vehicle_id"), ctx.nomes.get(agent_id), start_at,
        status_novo, rota.get("status"), total_ativas, entregues, insucessos,
        iniciada,
        status_novo, banco.ROTA_CONCLUIDA, concluida, agora,
        status_novo, banco.ROTA_CANCELADA, cancelada or agora,
        _json({k: v for k, v in rota.items() if k != "services"}), agora, rota_id,
    ))
    if status_novo != status_anterior:
        ocorrido = {banco.ROTA_CONCLUIDA: concluida, banco.ROTA_CANCELADA: cancelada,
                    banco.ROTA_EM_ROTA: iniciada}.get(status_novo)
        registrar_evento(conn, f"ROTA_{status_novo}", banco.ORIGEM_VUUPT_SYNC, rota_id=rota_id, agent_id=agent_id,
                         ocorrido_em=ocorrido or agora,
                         dados={"status_vuupt": rota.get("status"), "anterior": status_anterior})
        stats["eventos"] += 1
    stats["rotas"] += 1


def sincronizar_rotas(rotas_brutas: list[dict], conn: sqlite3.Connection,
                      nomes_motoristas: dict[int, str] | None = None) -> dict:
    """Aplica uma lista de rotas (formato GET /routes?include=services) no
    núcleo. Puro em relação à rede -- é o que os testes exercitam."""
    stats = {"rotas": 0, "rotas_novas": 0, "paradas_novas": 0, "eventos": 0}
    ctx = _Contexto(conn, nomes_motoristas)
    for rota in rotas_brutas:
        _sincronizar_rota(ctx, rota, stats)
    conn.commit()
    return stats


# ── CLI ────────────────────────────────────────────────────────────────────────

def _carregar_config() -> dict:
    import yaml
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _nomes_motoristas(config: dict) -> dict[int, str]:
    try:
        from regras.preferencias_motoristas import CatalogoMotoristas
        cfg = config.get("motoristas", {})
        catalogo = CatalogoMotoristas.carregar(cfg.get("planilha", ""), cfg.get("json_fallback", ""))
        return {m.agent_id: m.nome for m in catalogo.motoristas}
    except Exception as e:
        logger.warning(f"Catálogo de motoristas indisponível ({e}) -- rotas ficam sem nome de motorista.")
        return {}


def _listar_rotas_do_dia(token: str, dia: date) -> list[dict]:
    from rotas_client import listar_rotas
    inicio = dia.strftime("%Y-%m-%d") + " 00:00:00"
    fim = (dia + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    filtro = [
        {"field": "start_at", "operator": "gte", "value": inicio},
        {"field": "start_at", "operator": "lt", "value": fim},
    ]
    return listar_rotas(token, include=["services"], filtro=filtro)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Espelha rotas da VUUPT no núcleo próprio.")
    parser.add_argument("--dias", type=int, default=2, help="quantos dias pra trás, contando hoje (padrão 2)")
    parser.add_argument("--data", type=str, help="um dia só (YYYY-MM-DD)")
    parser.add_argument("--modo-teste", action="store_true", help="lê a VUUPT e mostra o resumo, sem gravar")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "nucleo_sincronizar_vuupt.log", encoding="utf-8")],
    )

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    if not token:
        logger.error("vuupt_api.token ausente no config.yaml.")
        return 1

    if args.data:
        dias = [date.fromisoformat(args.data)]
    else:
        hoje = date.today()
        dias = [hoje - timedelta(days=i) for i in range(args.dias)]

    nomes = _nomes_motoristas(config)
    total = {"rotas": 0, "rotas_novas": 0, "paradas_novas": 0, "eventos": 0}
    conn = None if args.modo_teste else banco.conectar()
    try:
        for dia in sorted(dias):
            rotas = _listar_rotas_do_dia(token, dia)
            if args.modo_teste:
                logger.info(f"[TESTE] {dia}: {len(rotas)} rota(s) na VUUPT -- "
                            + ", ".join(f"{r.get('name')}({r.get('status')}, {len(extrair_servicos(r))} paradas)" for r in rotas))
                continue
            stats = sincronizar_rotas(rotas, conn, nomes)
            logger.info(f"{dia}: {stats}")
            for k in total:
                total[k] += stats[k]
    finally:
        if conn is not None:
            conn.close()
    logger.info(f"Concluído: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())