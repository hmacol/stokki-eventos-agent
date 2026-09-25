# -*- coding: utf-8 -*-
"""
nucleo/sincronizar_servicos_vuupt.py

Espelha no núcleo o pedido FORA da rota -- a metade do ciclo de vida que
faltava (Etapa 2 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md). Até aqui o núcleo
só via pedido que estivesse dentro de uma rota sincronizada, então ficavam
invisíveis:

  - o POOL (`not_assigned`): pedido importado esperando roteirização, e o
    que muda nele depois (reagendamento, endereço, cancelamento);
  - a RETIRADA no galpão: serviço avulso com título "[RETIRADA]",
    atribuído ao agente fixo, que NUNCA entra em rota;
  - a REENTREGA (-R1, -R2...) e a duplicação por canhoto (-C1), que só
    apareciam aqui quando (e se) fossem roteirizadas;
  - o pedido CANCELADO na VUUPT (DELETE), que ficava ABERTO pra sempre.

Duas passadas, as duas baratas:

1. INCREMENTAL por `updated_at` (confirmado 16/09: o filtro funciona em
   /services; ~300 serviços mudam por dia, 58 na última hora). O cursor
   fica em `nucleo_sincronismos`, com margem de 15 min pra trás -- é mais
   barato reprocessar do que perder mudança.
2. POOL inteiro (~72 serviços hoje, 1 página): pedido que está ABERTO aqui
   e NÃO está mais no pool da VUUPT é conferido um a um; sumiu (404 ou
   deleted_at) vira CANCELADO com evento. É assim que o DELETE aparece --
   ausência não chega por filtro de data.

Não mexe em rota nem em parada: isso é do nucleo/sincronizar_vuupt.py. Os
dois escrevem em nucleo_pedidos a partir da mesma verdade (o serviço da
VUUPT), então a ordem entre eles não importa.

COMO USAR (timer a cada 15 min na VPS):
    venv/bin/python nucleo/sincronizar_servicos_vuupt.py
    venv/bin/python nucleo/sincronizar_servicos_vuupt.py --dias 7      # recuperar atraso
    venv/bin/python nucleo/sincronizar_servicos_vuupt.py --sem-pool
    venv/bin/python nucleo/sincronizar_servicos_vuupt.py --modo-teste  # lê e mostra, não grava
"""
import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo import banco, pedidos as nucleo_pedidos
from nucleo.normalizacao import normalizar_codigo, vuupt_para_local
from nucleo.rotas import registrar_evento

logger = logging.getLogger("nucleo.sincronizar_servicos_vuupt")

NOME_CURSOR = "servicos_vuupt"
MARGEM_MIN = 15          # reprocessa os últimos 15 min a cada rodada (barato, e não perde mudança)
LIMITE_SUMIDOS = 200     # teto de conferências individuais por rodada

# Código de pedido de verdade: PS-12345, PS-12345-R1, PS-12345-C1, e o
# serviço que agrupa mais de um pedido ("PS-1, PS-2", já sem '#' -- ver
# normalizacao.normalizar_codigo). O que não casa (ex.: "COLETA QUATRO
# ESTRELAS", código fixo que se repete todo dia) NÃO vira linha em
# nucleo_pedidos -- viraria uma linha só, sobrescrita diariamente. Esses
# serviços seguem existindo como parada da rota em que entram.
_UM_CODIGO = r"[A-Z]{1,4}-?\d{2,}(?:-[A-Z0-9]+)*"
_RE_CODIGO_PEDIDO = re.compile(rf"^{_UM_CODIGO}(?:, {_UM_CODIGO})*$")

_STATUS_SERVICO = {
    "not_assigned": banco.PEDIDO_ABERTO,
    "assigned": banco.PEDIDO_EM_ROTA,
    "accepted": banco.PEDIDO_EM_ROTA,
    "on_route": banco.PEDIDO_EM_ROTA,
    "arrived": banco.PEDIDO_EM_ROTA,
    "canceled": banco.PEDIDO_CANCELADO,
    "cancelled": banco.PEDIDO_CANCELADO,
}


def status_do_servico(s: dict) -> str:
    bruto = (s.get("status") or "").lower()
    if bruto == "done":
        return banco.PEDIDO_INSUCESSO if s.get("status_done") == "failed" else banco.PEDIDO_ENTREGUE
    if s.get("deleted_at"):
        return banco.PEDIDO_CANCELADO
    return _STATUS_SERVICO.get(bruto, banco.PEDIDO_ABERTO)


def fluxo_do_servico(s: dict) -> str:
    titulo = (s.get("title") or "").lstrip().upper()
    return banco.FLUXO_RETIRADA if titulo.startswith("[RETIRADA]") else banco.FLUXO_ENTREGA


def _embutido(s: dict, chave: str) -> dict:
    """include=customer,sender,zone traz o objeto inteiro (às vezes dentro
    de {"data": {...}})."""
    valor = s.get(chave) or {}
    if isinstance(valor, dict) and isinstance(valor.get("data"), dict):
        valor = valor["data"]
    return valor if isinstance(valor, dict) else {}


def _campos(s: dict) -> dict:
    """Serviço da VUUPT -> colunas de nucleo_pedidos."""
    customer = _embutido(s, "customer")
    sender = _embutido(s, "sender")
    zona = _embutido(s, "zone")
    return {
        "remetente_nome": sender.get("name"),
        "remetente_codigo": sender.get("code"),
        "zona": zona.get("name"),
        "zona_id": zona.get("id") or s.get("zone_id"),
        "titulo": s.get("title"),
        "tipo": s.get("type"),
        "destinatario_nome": customer.get("name"),
        "destinatario_codigo": customer.get("code"),
        "destinatario_telefone": customer.get("phone_number") or s.get("phone_number"),
        "endereco": s.get("address"),
        "latitude": s.get("latitude"),
        "longitude": s.get("longitude"),
        "horario_inicio": customer.get("operating_hour_start"),
        "horario_fim": customer.get("operating_hour_end"),
        "sender_id": s.get("sender_id"),
        "caixas": s.get("dimension_3"),
        "agendamento_inicio": vuupt_para_local(s.get("scheduled_start")),
        "agendamento_fim": vuupt_para_local(s.get("scheduled_end")),
        "status_provedor": s.get("status"),
        "status_done_provedor": s.get("status_done"),
        "customer_id": s.get("customer_id"),
        "vuupt_route_id": s.get("route_id"),
        "driver_id": s.get("driver_id"),
        "nota": s.get("note"),
        "complemento": s.get("address_complement"),
        "fluxo": fluxo_do_servico(s),
        "criado_em_provedor": vuupt_para_local(s.get("created_at")),
        "atualizado_em_provedor": vuupt_para_local(s.get("updated_at")),
        "excluido_em": vuupt_para_local(s.get("deleted_at")),
        "reentrega_de_service_id": s.get("recreated_order_origin_id"),
        "qtd_checklists": _contar(s, "checklistAnswers"),
        "qtd_anexos": _contar(s, "attachments"),
    }


def _contar(s: dict, chave: str) -> int | None:
    """include=checklistAnswers,attachments devolve {"data": [...]}. None
    quando o include não veio (não dá pra dizer que é zero)."""
    valor = s.get(chave)
    if isinstance(valor, dict) and isinstance(valor.get("data"), list):
        return len(valor["data"])
    if isinstance(valor, list):
        return len(valor)
    return None


def _codigo_por_service_id(conn: sqlite3.Connection, service_id) -> str | None:
    if not service_id:
        return None
    linha = conn.execute("SELECT codigo FROM nucleo_pedidos WHERE vuupt_service_id = ?", (service_id,)).fetchone()
    return linha["codigo"] if linha else None


def sincronizar_servicos(servicos: list[dict], conn: sqlite3.Connection) -> dict:
    """Aplica uma lista de serviços (formato GET /services) em
    nucleo_pedidos. Puro em relação à rede -- é o que os testes exercitam."""
    stats = {"servicos": 0, "novos": 0, "ignorados_sem_codigo": 0, "cancelados": 0, "reentregas": 0,
             "retiradas": 0, "eventos": 0}
    for s in servicos:
        stats["servicos"] += 1
        codigo = normalizar_codigo(s.get("code"))
        if not codigo or not _RE_CODIGO_PEDIDO.match(codigo):
            stats["ignorados_sem_codigo"] += 1
            continue
        anterior = conn.execute("SELECT codigo, status, fluxo FROM nucleo_pedidos WHERE codigo = ?",
                                (codigo,)).fetchone()
        campos = _campos(s)
        if campos["reentrega_de_service_id"]:
            origem = _codigo_por_service_id(conn, campos["reentrega_de_service_id"])
            # A VUUPT também usa recreated_order_origin_id quando o serviço é
            # recriado com o MESMO código (reimportação do mesmo pedido do
            # Stokki). Apontar o pedido pra ele mesmo não diz nada.
            campos["reentrega_de_codigo"] = origem if origem and origem != codigo else None
        status = status_do_servico(s)
        # Revisão 24/09: serviço editado na Vuupt pra agrupar outro pedido
        # ("PS-10" virou "PS-10, PS-20") deixava a linha antiga ABERTA com o
        # mesmo id -- fantasma no pool, pra sempre. O id é de UMA linha só.
        if s.get("id"):
            for outra in conn.execute("SELECT codigo FROM nucleo_pedidos WHERE vuupt_service_id = ? AND codigo != ?",
                                      (s["id"], codigo)).fetchall():
                conn.execute("UPDATE nucleo_pedidos SET vuupt_service_id = NULL, atualizado_em = ? WHERE codigo = ?",
                             (banco.agora(), outra["codigo"]))
                logger.info(f"serviço {s['id']} agora é '{codigo}': a linha '{outra['codigo']}' perdeu o vínculo.")
        nucleo_pedidos.upsert_pedido(conn, codigo, campos, origem=banco.ORIGEM_VUUPT_SYNC,
                                     status=status, vuupt_service_id=s.get("id"))
        # Revisão 24/09: o upsert nunca apaga campo (COALESCE) -- certo pra
        # fonte parcial, errado aqui, onde o serviço vem INTEIRO de GET
        # /services. Campo que a Vuupt esvaziou (agendamento removido, saiu da
        # rota) é esvaziado, e serviço vivo limpa o excluido_em que um
        # cancelamento anterior com o mesmo código deixou -- sem isso o pedido
        # recriado sumia do pool do núcleo pra sempre.
        conn.execute("""UPDATE nucleo_pedidos SET agendamento_inicio = ?, agendamento_fim = ?, vuupt_route_id = ?,
                               driver_id = ?, status_done_provedor = ?, excluido_em = ? WHERE codigo = ?""",
                     (campos["agendamento_inicio"], campos["agendamento_fim"], campos["vuupt_route_id"],
                      campos["driver_id"], campos["status_done_provedor"], campos["excluido_em"], codigo))
        if anterior is None:
            stats["novos"] += 1
            if campos["reentrega_de_service_id"]:
                stats["reentregas"] += 1
                registrar_evento(conn, "REENTREGA_CRIADA", banco.ORIGEM_VUUPT_SYNC,
                                 ocorrido_em=campos["criado_em_provedor"],
                                 dados={"codigo": codigo, "de_codigo": campos.get("reentrega_de_codigo"),
                                        "de_service_id": campos["reentrega_de_service_id"]})
                stats["eventos"] += 1
        if campos["fluxo"] == banco.FLUXO_RETIRADA:
            stats["retiradas"] += 1
        if status == banco.PEDIDO_CANCELADO and (anterior is None or anterior["status"] != banco.PEDIDO_CANCELADO):
            stats["cancelados"] += 1
            registrar_evento(conn, "PEDIDO_CANCELADO", banco.ORIGEM_VUUPT_SYNC,
                             dados={"codigo": codigo, "status_vuupt": s.get("status"),
                                    "deleted_at": campos["excluido_em"]})
            stats["eventos"] += 1
    conn.commit()
    return stats


def rotas_a_ressincronizar(servicos: list[dict], conn: sqlite3.Connection) -> list[int]:
    """Rotas das quais algum serviço mudou e que o espelho de ROTAS pode não
    ter alcançado: a janela dele é de 8 dias, e rota antiga fechada depois
    disso ficava congelada aqui (achado 16/09: a rota 5168818, de 03/09, foi
    finalizada 13 dias depois -- o relatório do financeiro mostrava
    'Finalizada' e o núcleo, 'Atribuída').

    Devolve só o que vale a pena buscar: rota que o núcleo tem em aberto
    (nem concluída nem cancelada) ou que ele nem conhece."""
    ids = {s.get("route_id") for s in servicos if s.get("route_id")}
    if not ids:
        return []
    marcadores = ",".join("?" * len(ids))
    conhecidas = {linha["vuupt_route_id"]: linha["status"] for linha in conn.execute(
        f"SELECT vuupt_route_id, status FROM nucleo_rotas WHERE vuupt_route_id IN ({marcadores})", tuple(ids))}
    return sorted(rid for rid in ids
                  if conhecidas.get(rid) not in (banco.ROTA_CONCLUIDA, banco.ROTA_CANCELADA))


def vincular_sem_service_id(conn: sqlite3.Connection, buscar_por_codigo, limite: int = 50) -> dict:
    """Pedido ABERTO sem `vuupt_service_id`: o gancho do pipeline grava o
    pedido mesmo quando não cria nada na VUUPT ('pulado_atribuido' /
    'pulado_sem_alteracao'), e aí a linha fica sem o id do serviço -- não
    dá pra conferir nem casar com a rota depois. Aqui a gente procura pelo
    CÓDIGO e liga, quando o serviço existe.

    Quem não é achado fica como está (pode ser pedido segurado de
    propósito, ex.: aguardando redespacho) -- só entra na contagem."""
    stats = {"sem_service_id": 0, "vinculados": 0, "nao_achados": 0}
    linhas = conn.execute("""SELECT codigo FROM nucleo_pedidos
                             WHERE status = ? AND vuupt_service_id IS NULL
                             ORDER BY COALESCE(atualizado_em_provedor, atualizado_em) LIMIT ?""",
                          (banco.PEDIDO_ABERTO, limite)).fetchall()
    stats["sem_service_id"] = len(linhas)
    for linha in linhas:
        servico = buscar_por_codigo(linha["codigo"])
        if not servico or not servico.get("id"):
            stats["nao_achados"] += 1
            continue
        sincronizar_servicos([servico], conn)
        stats["vinculados"] += 1
    conn.commit()
    return stats


def reconciliar_pool(servicos_pool: list[dict], conn: sqlite3.Connection, buscar_por_id,
                     limite: int = LIMITE_SUMIDOS) -> dict:
    """Pedido ABERTO aqui que não está mais no pool da VUUPT: confere um a
    um. Sumiu (404/deleted_at) -> CANCELADO com evento; mudou de status ->
    atualizado pelo serviço que voltou. Ausência não chega por filtro de
    data -- é por isso que esta passada existe."""
    stats = {"pool_vuupt": len(servicos_pool), "conferidos": 0, "sumidos": 0, "atualizados": 0}
    ids_no_pool = {s.get("id") for s in servicos_pool}
    abertos = conn.execute("""SELECT codigo, vuupt_service_id FROM nucleo_pedidos
                              WHERE status = ? AND vuupt_service_id IS NOT NULL
                              ORDER BY COALESCE(atualizado_em_provedor, atualizado_em) LIMIT ?""",
                           (banco.PEDIDO_ABERTO, limite)).fetchall()
    for linha in abertos:
        if linha["vuupt_service_id"] in ids_no_pool:
            continue
        stats["conferidos"] += 1
        servico = buscar_por_id(linha["vuupt_service_id"])
        if not servico:
            conn.execute("""UPDATE nucleo_pedidos SET status = ?, excluido_em = COALESCE(excluido_em, ?),
                                   atualizado_em = ? WHERE codigo = ?""",
                         (banco.PEDIDO_CANCELADO, banco.agora(), banco.agora(), linha["codigo"]))
            registrar_evento(conn, "PEDIDO_SUMIU_DA_VUUPT", banco.ORIGEM_VUUPT_SYNC,
                             dados={"codigo": linha["codigo"], "service_id": linha["vuupt_service_id"]})
            stats["sumidos"] += 1
            continue
        sincronizar_servicos([servico], conn)
        stats["atualizados"] += 1
    conn.commit()
    return stats


def ressincronizar_ids(vuupt, service_ids, conn: sqlite3.Connection | None = None) -> int:
    """Traz pro espelho, AGORA, os serviços que acabaram de ser escritos na
    Vuupt pela tela ou por um script (cancelar, reagendar, endereço, dia
    fixo, envio de rota). Sem isto o pool lido do núcleo (nucleo/pool.py)
    ficava até 15 min atrás da Vuupt, o intervalo do timer.

    Best-effort por contrato: a escrita na Vuupt já aconteceu, então falha
    aqui só pode virar aviso no log -- o timer alcança depois. 404 é a única
    resposta que autoriza CANCELADO (mesma regra de reconciliar_pool)."""
    ids = [int(i) for i in (service_ids or []) if i]
    if not ids:
        return 0
    fechar = conn is None
    try:
        conn = conn or banco.conectar()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ressincronizar {ids}: banco indisponível ({str(exc)[:120]})")
        return 0
    aplicados = 0
    try:
        for sid in ids:
            try:
                servico = _buscar_servico(vuupt, sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"ressincronizar {sid}: {str(exc)[:120]}")
                continue
            if servico is None:
                codigo = _codigo_por_service_id(conn, sid)
                if codigo:
                    conn.execute("""UPDATE nucleo_pedidos SET status = ?, excluido_em = COALESCE(excluido_em, ?),
                                           atualizado_em = ? WHERE codigo = ?""",
                                 (banco.PEDIDO_CANCELADO, banco.agora(), banco.agora(), codigo))
                    registrar_evento(conn, "PEDIDO_SUMIU_DA_VUUPT", banco.ORIGEM_VUUPT_SYNC,
                                     dados={"codigo": codigo, "service_id": sid})
                    aplicados += 1
                continue
            if not servico.get("code"):
                continue   # _buscar_servico devolve {"id": sid} em falha de rede/HTTP: nada a concluir
            sincronizar_servicos([servico], conn)
            aplicados += 1
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ressincronizar {ids}: {str(exc)[:160]}")
    finally:
        if fechar:
            conn.close()
    return aplicados


# ── Cursor ────────────────────────────────────────────────────────────────────

def _garantir_tabela(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE IF NOT EXISTS nucleo_sincronismos (
                        nome          TEXT PRIMARY KEY,
                        cursor        TEXT,
                        atualizado_em TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                        dados_json    TEXT)""")


def ler_cursor(conn: sqlite3.Connection, nome: str = NOME_CURSOR) -> str | None:
    _garantir_tabela(conn)
    linha = conn.execute("SELECT cursor FROM nucleo_sincronismos WHERE nome = ?", (nome,)).fetchone()
    return linha["cursor"] if linha else None


def gravar_cursor(conn: sqlite3.Connection, cursor: str, stats: dict, nome: str = NOME_CURSOR):
    _garantir_tabela(conn)
    conn.execute("""INSERT INTO nucleo_sincronismos (nome, cursor, atualizado_em, dados_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(nome) DO UPDATE SET cursor = excluded.cursor,
                        atualizado_em = excluded.atualizado_em, dados_json = excluded.dados_json""",
                 (nome, cursor, banco.agora(), json.dumps(stats, ensure_ascii=False)))
    conn.commit()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _carregar_config() -> dict:
    import yaml
    caminho = next((c for c in (_RAIZ / "config.yaml", Path.cwd() / "config.yaml") if c.exists()), None)
    return (yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}) if caminho else {}


def _cliente(config: dict):
    from vuupt_client import VuuptClient
    return VuuptClient(config["vuupt_api"]["token"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Espelha no núcleo o pedido fora da rota (pool, retirada, reentrega).")
    parser.add_argument("--dias", type=int, help="ignora o cursor e varre os últimos N dias")
    parser.add_argument("--desde", help="ignora o cursor e varre a partir de 'YYYY-MM-DD HH:MM:SS' (hora local)")
    parser.add_argument("--sem-pool", action="store_true", help="pula a reconciliação do pool")
    parser.add_argument("--limite-sumidos", type=int, default=LIMITE_SUMIDOS)
    parser.add_argument("--limite-rotas", type=int, default=20,
                        help="máximo de rotas ressincronizadas por rodada (pedido que mudou fora da janela)")
    parser.add_argument("--modo-teste", action="store_true", help="lê a VUUPT e mostra o resumo, sem gravar")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(_RAIZ / "dados" / "nucleo_sincronizar_servicos.log",
                                                      encoding="utf-8")])

    config = _carregar_config()
    if not (config.get("vuupt_api") or {}).get("token"):
        logger.error("vuupt_api.token ausente no config.yaml.")
        return 1
    vuupt = _cliente(config)
    conn = banco.conectar()
    try:
        # A VUUPT filtra em UTC sem fuso; o cursor é guardado do mesmo jeito.
        agora_utc = datetime.now(timezone.utc)
        inicio = None
        if args.dias or args.desde:
            inicio = (args.desde if args.desde else
                      (agora_utc - timedelta(days=args.dias)).strftime("%Y-%m-%d %H:%M:%S"))
        if args.modo_teste:
            inicio = inicio or ler_cursor(conn) or (agora_utc - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            servicos = vuupt.listar_servicos([{"field": "updated_at", "operator": "gte", "value": inicio}],
                                             include=["customer", "sender", "zone", "checklistAnswers", "attachments"])
            por_status = {}
            for s in servicos:
                por_status[s.get("status")] = por_status.get(s.get("status"), 0) + 1
            logger.info(f"[TESTE] {len(servicos)} serviço(s) desde {inicio} (UTC); por status: {por_status}")
            pool = vuupt.listar_servicos([{"field": "status", "operator": "eq", "value": "not_assigned"}])
            logger.info(f"[TESTE] pool na VUUPT: {len(pool)} serviço(s); nada foi gravado.")
            return 0
        resultado = executar(vuupt, conn, config["vuupt_api"]["token"], inicio=inicio, sem_pool=args.sem_pool,
                             limite_sumidos=args.limite_sumidos, limite_rotas=args.limite_rotas)
        logger.info(f"Resultado: {resultado}")
    finally:
        conn.close()
    return 0


def executar(vuupt, conn: sqlite3.Connection, token: str, inicio: str | None = None, sem_pool: bool = False,
             limite_sumidos: int = LIMITE_SUMIDOS, limite_rotas: int = 20) -> dict:
    """Uma rodada completa (o que o timer roda a cada 15 min), reutilizável
    pelo botão "Atualizar" do planejamento com fonte_pool=nucleo. `inicio`
    é 'YYYY-MM-DD HH:MM:SS' em UTC; None lê o cursor (ou 2 dias atrás)."""
    agora_utc = datetime.now(timezone.utc)
    if not inicio:
        cursor = ler_cursor(conn)
        inicio = cursor if cursor else (agora_utc - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Serviços alterados desde {inicio} (UTC).")
    servicos = vuupt.listar_servicos([{"field": "updated_at", "operator": "gte", "value": inicio}],
                                     include=["customer", "sender", "zone", "checklistAnswers", "attachments"])
    logger.info(f"{len(servicos)} serviço(s) alterado(s).")
    stats = sincronizar_servicos(servicos, conn)
    logger.info(f"Incremental: {stats}")
    resultado = {"incremental": stats, "pool": None, "vinculo": None, "abertos": None, "pool_vuupt": None}

    alvo = rotas_a_ressincronizar(servicos, conn)[:limite_rotas]
    if alvo:
        from nucleo.sincronizar_vuupt import reconciliar_rotas_sumidas
        # `vistas=set()` de propósito: aqui a gente QUER buscar cada uma
        # dessas rotas por id, mesmo que a listagem do dia não as traga.
        res_rotas = reconciliar_rotas_sumidas(conn, token, [date.today()], set(), nomes=None, ids=alvo)
        logger.info(f"Rotas tocadas por serviço que mudou: {len(alvo)} -> {res_rotas}")
    if not sem_pool:
        pool = vuupt.listar_servicos([{"field": "status", "operator": "eq", "value": "not_assigned"}],
                                     include=["customer", "sender", "zone", "checklistAnswers", "attachments"])
        stats_pool = reconciliar_pool(pool, conn, lambda sid: _buscar_servico(vuupt, sid), limite=limite_sumidos)
        # O pool inteiro também entra no espelho (pedido que nunca passou
        # pelo pipeline com dual-write, ex.: criado na tela da VUUPT).
        stats_pool_upsert = sincronizar_servicos(pool, conn)
        stats_vinculo = vincular_sem_service_id(conn, vuupt.buscar_servico_por_code)
        logger.info(f"Pool: {stats_pool} | upsert: {stats_pool_upsert} | vínculo: {stats_vinculo}")
        abertos = conn.execute("SELECT COUNT(*) FROM nucleo_pedidos WHERE status = ?",
                               (banco.PEDIDO_ABERTO,)).fetchone()[0]
        if abertos != len(pool):
            logger.warning(f"Pool do núcleo ({abertos}) != pool da VUUPT ({len(pool)}) -- "
                           f"confira com nucleo/comparar_pool.py.")
        resultado.update({"pool": stats_pool, "vinculo": stats_vinculo, "abertos": abertos, "pool_vuupt": len(pool)})
    # Cursor com margem: perder mudança é pior do que reprocessar.
    gravar_cursor(conn, (agora_utc - timedelta(minutes=MARGEM_MIN)).strftime("%Y-%m-%d %H:%M:%S"), stats)
    return resultado


def _buscar_servico(vuupt, service_id: int) -> dict | None:
    """None SÓ quando a VUUPT diz 404 (o serviço foi apagado) -- é o que
    autoriza marcar o pedido como cancelado.

    De propósito não usa VuuptClient.buscar_servico_por_id: aquele devolve
    None tanto pro 404 quanto pra qualquer falha (timeout, 500), e aqui
    isso cancelaria pedido bom por causa de uma oscilação de rede. Erro
    que não é 404 devolve um dicionário sem código, que o espelho ignora."""
    from http_retry import chamar_com_retry
    from vuupt_client import BASE_URL

    try:
        resp = chamar_com_retry(vuupt.session.get, f"{BASE_URL}/services/{service_id}", timeout=20)
    except Exception as exc:  # noqa: BLE001 -- rede fora: não dá pra concluir nada
        logger.warning(f"serviço {service_id}: {str(exc)[:120]}")
        return {"id": service_id}
    if resp.status_code == 404:
        return None
    if not resp.ok:
        logger.warning(f"serviço {service_id}: HTTP {resp.status_code}")
        return {"id": service_id}
    corpo = resp.json()
    servico = corpo.get("service", corpo) if isinstance(corpo, dict) else None
    return servico or {"id": service_id}


if __name__ == "__main__":
    sys.exit(main())
