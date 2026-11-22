# -*- coding: utf-8 -*-
"""
carregamento.py

Checklist de carregamento do app dos motoristas: antes de sair, o motorista
fotografa o bau carregado e desmarca as paradas que nao foram carregadas.
Config, quando a rota exige o checklist, leitura do registro e `confirmar`
(tira os desmarcados da rota e grava o checklist).
Chave `api_motorista.checklist_carregamento.ativo` DESLIGADA por padrao.

Desmarcar NUNCA cancela pedido: so tira a parada da rota (volta ao pool).
Falha ao tirar trava o checklist: o que ja saiu fica gravado em
nucleo_carregamento_itens (carregamento_id NULL) e a nova tentativa so
repete os que faltam.
"""
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

from nucleo import banco, operacao, pedidos as nucleo_pedidos, ponte_vuupt

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent

# Subconjunto de expedicao.MOTIVOS_EXCLUSAO que o motorista pode escolher.
MOTIVOS_DESMARCAR = ("Não coube", "Não Encontrado", "Outros")
MOTIVO_OUTROS = "Outros"


def config_carregamento(config: dict | None = None) -> dict:
    bruto = ((config if config is not None else ponte_vuupt._config_projeto()).get("api_motorista") or {}) \
        .get("checklist_carregamento") or {}
    return {
        "ativo": bool(bruto.get("ativo")),
        "pilotos": {int(a) for a in (bruto.get("pilotos") or [])},
    }


def exigido(rota: sqlite3.Row, cfg: dict) -> bool:
    """O checklist so trava rota editavel ainda nao iniciada, de piloto (ou de todos se `pilotos` vazio)."""
    return bool(cfg["ativo"] and operacao.rota_editavel(rota)
                and rota["status"] in (banco.ROTA_PLANEJADA, banco.ROTA_ACEITA)
                and (not cfg["pilotos"] or rota["agent_id"] in cfg["pilotos"]))


def carregamento_da_rota(conn: sqlite3.Connection, rota_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM nucleo_carregamentos WHERE rota_id = ?", (rota_id,)).fetchone()
    return dict(row) if row else None


# ── Selo no pool ──────────────────────────────────────────────────────────────

def nao_carregados_recentes(conn: sqlite3.Connection, service_ids: list[int], dias: int = 2) -> dict[int, dict]:
    """Pedidos que o motorista desmarcou no carregamento (removido_em preenchido) nos ultimos `dias`
    dias: o item mais recente por service_id. Tabela ausente (banco antigo) -> {}."""
    ids = [int(i) for i in service_ids if i is not None]
    if not ids:
        return {}
    marcas = ",".join("?" * len(ids))
    try:
        linhas = conn.execute(
            f"SELECT service_id, motivo, observacao, criado_em FROM nucleo_carregamento_itens "
            f"WHERE service_id IN ({marcas}) AND removido_em IS NOT NULL "
            f"AND removido_em >= datetime('now','localtime', ?) ORDER BY removido_em, id",
            (*ids, f"-{int(dias)} days")).fetchall()
    except sqlite3.OperationalError:
        return {}
    # ordem crescente: o ultimo de cada service_id (mais recente) sobrescreve
    return {r[0]: {"motivo": r[1], "observacao": r[2], "criado_em": r[3]} for r in linhas}


# ── Tirar da rota ─────────────────────────────────────────────────────────────

def _efeitos_no_painel(rota: sqlite3.Row, parada: sqlite3.Row, resultado: dict, motivo: str,
                       observacao: str | None) -> None:
    """Card do Planejamento + auditoria + romaneio, mesmo caminho do "Tirar da rota" do painel
    (rascunhos_rota.aplicar_efeitos_pos_remocao). Best-effort: a Vuupt ja mudou."""
    try:
        if str(_RAIZ / "painel_agentes") not in sys.path:
            sys.path.insert(0, str(_RAIZ / "painel_agentes"))
        import rascunhos_rota
        rascunho = rascunhos_rota.rascunho_por_vuupt_route_id(rota["vuupt_route_id"])
        if resultado.get("ja_fora_da_rota"):
            if rascunho:
                rascunhos_rota.remover_parada(rascunho["id"], parada["service_id"])
            return
        # pedido desmarcado nao foi carregado: esta no galpao, nunca "Retornando"
        rascunhos_rota.aplicar_efeitos_pos_remocao(
            rascunho=rascunho, resultado={**resultado, "rota_iniciada": False},
            vuupt_route_id=rota["vuupt_route_id"], service_id=parada["service_id"], codigo=parada["codigo"] or "",
            agent_id=rota["agent_id"], motorista_nome=rota["motorista_nome"], motivo=motivo,
            observacao=observacao or "", usuario=f"app:{rota['motorista_nome'] or rota['agent_id']}",
            data_auditoria=date.fromisoformat(rota["data_rota"]), rota_nome=rota["nome"] or "")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Carregamento rota {rota['id']}: card/auditoria do painel nao atualizados "
                       f"apos tirar {parada['codigo']}: {e}")


def _tirar_da_vuupt_ou_local(rota: sqlite3.Row, parada: sqlite3.Row, motivo: str, observacao: str | None) -> dict:
    """Tira a parada da rota de verdade. Rota APP: so confere se a parada ainda nao comecou
    (os efeitos locais sao de `confirmar`). Rota da Vuupt: edicao_rota.tirar_pedido (status ao
    vivo; recusa pedido comecado); `fora_da_rota` conta como sucesso.
    Devolve o formato de tirar_pedido: {"ok", "rota_cancelada", "rota_iniciada"} | {"ok": False, "erro"}."""
    if rota["provedor"] == banco.PROVEDOR_APP:
        if parada["situacao"] not in (banco.PARADA_PENDENTE, banco.PARADA_CANCELADA):
            return {"ok": False, "erro": f"Pedido {parada['codigo']} já começou ({parada['situacao'].lower()}) "
                                         f"e não pode sair da rota."}
        return {"ok": True, "rota_cancelada": False, "rota_iniciada": False}

    if str(_RAIZ / "roteirizacao") not in sys.path:
        sys.path.insert(0, str(_RAIZ / "roteirizacao"))
    import edicao_rota
    token = (ponte_vuupt._config_projeto().get("vuupt_api") or {}).get("token") or ""
    resultado = edicao_rota.tirar_pedido(token, rota["vuupt_route_id"], parada["service_id"])
    if not resultado["ok"] and resultado.get("fora_da_rota"):
        resultado = {"ok": True, "ja_fora_da_rota": True, "rota_cancelada": False, "rota_iniciada": False}
    if resultado["ok"]:
        _efeitos_no_painel(rota, parada, resultado, motivo, observacao)
    return resultado


# ── Confirmar ─────────────────────────────────────────────────────────────────

def _item(conn: sqlite3.Connection, rota_id: int, parada: sqlite3.Row) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM nucleo_carregamento_itens WHERE rota_id = ? AND service_id IS ? AND codigo IS ? "
        "ORDER BY id DESC LIMIT 1", (rota_id, parada["service_id"], parada["codigo"])).fetchone()


def _gravar_removido(conn: sqlite3.Connection, rota_id: int, parada: sqlite3.Row, motivo: str,
                     observacao: str | None) -> None:
    item = _item(conn, rota_id, parada)
    if item:
        conn.execute("UPDATE nucleo_carregamento_itens SET motivo = ?, observacao = ?, removido_em = ? WHERE id = ?",
                     (motivo, observacao, banco.agora(), item["id"]))
    else:
        conn.execute("INSERT INTO nucleo_carregamento_itens (rota_id, service_id, codigo, motivo, observacao, "
                     "removido_em) VALUES (?, ?, ?, ?, ?, ?)",
                     (rota_id, parada["service_id"], parada["codigo"], motivo, observacao, banco.agora()))


def _validar_desmarcados(conn: sqlite3.Connection, rota_id: int, desmarcados: list[dict]) -> list[tuple]:
    """[(parada, motivo, observacao)] sem repetir parada; entrada ruim levanta OperacaoInvalida."""
    saida, vistos = [], set()
    for d in desmarcados or []:
        try:
            parada_id = int(d.get("parada_id"))
        except (TypeError, ValueError):
            raise operacao.OperacaoInvalida("Parada desmarcada sem parada_id.")
        motivo = str(d.get("motivo") or "").strip()
        if motivo not in MOTIVOS_DESMARCAR:
            raise operacao.OperacaoInvalida(f"Motivo inválido: {motivo!r}. Escolha um de: {', '.join(MOTIVOS_DESMARCAR)}.")
        observacao = (str(d.get("observacao") or "").strip()[:500]) or None
        if motivo == MOTIVO_OUTROS and not observacao:
            raise operacao.OperacaoInvalida("Motivo \"Outros\" pede uma observação.")
        parada = conn.execute("SELECT * FROM nucleo_paradas WHERE id = ?", (parada_id,)).fetchone()
        if not parada or parada["rota_id"] != rota_id:
            raise operacao.OperacaoInvalida(f"Parada {parada_id} não é dessa rota.", 409)
        if parada_id not in vistos:
            vistos.add(parada_id)
            saida.append((parada, motivo, observacao))
    return saida


def _veiculo(conn: sqlite3.Connection, agent_id: int) -> tuple:
    row = conn.execute("SELECT placa, tipo_veiculo FROM motoristas WHERE agent_id = ? ORDER BY ativo DESC LIMIT 1",
                       (agent_id,)).fetchone()
    return (row["placa"] or None, row["tipo_veiculo"] or None) if row else (None, None)


def confirmar(conn: sqlite3.Connection, rota: sqlite3.Row, agent_id: int, *, carregador: str, uuid: str,
              desmarcados: list[dict], foto: dict, tirar=None) -> dict:
    """Checklist de carregamento: tira cada parada desmarcada da rota e, se TODAS sairam, grava
    nucleo_carregamentos. Idempotente por uuid da mesma rota; outro uuid numa rota ja
    confirmada (ou uuid de outra rota) levanta 409.
    `foto` ja vem gravada em disco por quem chama: {caminho, sha256, resultado, ocupacao_pct, revisao_humana}.
    `tirar(rota, parada, motivo, observacao)` e injetavel (teste sem rede).
    Devolve {"ok": True, "carregamento", "rota_cancelada"} ou
    {"ok": False, "falhas": [{parada_id, codigo, erro}], "removidos": [parada_id]} sem gravar o checklist."""
    tirar = tirar or _tirar_da_vuupt_ou_local
    if not uuid:
        raise operacao.OperacaoInvalida("Carregamento sem uuid (gerado no aparelho).")
    rota = operacao.rota_do_motorista(conn, rota["id"], agent_id)
    ja = conn.execute("SELECT * FROM nucleo_carregamentos WHERE uuid = ? AND rota_id = ?",
                      (uuid, rota["id"])).fetchone()
    if ja:
        return {"ok": True, "carregamento": dict(ja), "rota_cancelada": rota["status"] == banco.ROTA_CANCELADA}
    if carregamento_da_rota(conn, rota["id"]):
        raise operacao.OperacaoInvalida("Checklist de carregamento já confirmado para esta rota.", 409)
    if conn.execute("SELECT 1 FROM nucleo_carregamentos WHERE uuid = ?", (uuid,)).fetchone():
        # uuid de outro checklist: o INSERT ia quebrar no UNIQUE depois de ja ter tirado pedidos da rota
        raise operacao.OperacaoInvalida("Identificador do checklist já usado em outra rota.", 409)

    carregador = str(carregador or "").strip()[:120]
    if not carregador:
        raise operacao.OperacaoInvalida("Informe quem carregou o veículo.")
    if not foto or not foto.get("caminho"):
        raise operacao.OperacaoInvalida("Falta a foto do baú carregado.")
    if rota["status"] == banco.ROTA_EM_ROTA:
        raise operacao.OperacaoInvalida("Rota já iniciada: o checklist de carregamento é antes de sair.", 409)
    operacao._exigir_liberada(rota)
    if not exigido(rota, config_carregamento()):
        raise operacao.OperacaoInvalida("Essa rota não pede checklist de carregamento.", 409)
    pedidos = _validar_desmarcados(conn, rota["id"], desmarcados)

    replica = operacao.eh_rota_replica(rota)
    falhas, removidos, rota_cancelada = [], [], False
    for parada, motivo, observacao in pedidos:
        item = _item(conn, rota["id"], parada)
        if item and item["removido_em"] and parada["situacao"] == banco.PARADA_CANCELADA:
            removidos.append(parada["id"])          # saiu numa tentativa anterior
            continue
        try:
            resultado = tirar(rota, parada, motivo, observacao)
        except Exception as e:  # noqa: BLE001
            resultado = {"ok": False, "erro": f"Falha ao tirar da rota: {e}"}
        if not resultado.get("ok"):
            falhas.append({"parada_id": parada["id"], "codigo": parada["codigo"],
                           "erro": resultado.get("erro") or "Não foi possível tirar da rota."})
            continue
        agora = banco.agora()
        conn.execute("UPDATE nucleo_paradas SET situacao = ?, atualizado_em = ? WHERE id = ?",
                     (banco.PARADA_CANCELADA, agora, parada["id"]))
        _gravar_removido(conn, rota["id"], parada, motivo, observacao)
        if resultado.get("rota_cancelada"):
            rota_cancelada = True
            conn.execute("UPDATE nucleo_rotas SET status = ?, cancelada_em = COALESCE(cancelada_em, ?), "
                         "atualizado_em = ? WHERE id = ?", (banco.ROTA_CANCELADA, agora, agora, rota["id"]))
        operacao._recalcular_contadores(conn, rota["id"])
        conn.commit()                               # cada remocao fica gravada mesmo se outra falhar
        if not replica and parada["service_id"]:
            nucleo_pedidos.marcar_aberto_por_service_ids([parada["service_id"]], conn=conn)
        removidos.append(parada["id"])

    if falhas:
        logger.warning(f"Carregamento rota {rota['id']}: {len(falhas)} parada(s) nao sairam da rota: {falhas}")
        return {"ok": False, "falhas": falhas, "removidos": removidos}

    contadores = operacao._recalcular_contadores(conn, rota["id"])
    if (not rota_cancelada and pedidos and contadores["total"] == 0
            and rota["provedor"] == banco.PROVEDOR_APP):
        # rota do app sem nenhuma parada: cancela como a Vuupt faz com a ultima
        rota_cancelada = True
        agora = banco.agora()
        conn.execute("UPDATE nucleo_rotas SET status = ?, cancelada_em = COALESCE(cancelada_em, ?), "
                     "atualizado_em = ? WHERE id = ?", (banco.ROTA_CANCELADA, agora, agora, rota["id"]))
    placa, tipo_veiculo = _veiculo(conn, agent_id)
    # desmarcados = tudo que saiu desta rota pelo checklist, inclusive em tentativa anterior
    qtd_desmarcados = conn.execute(
        "SELECT COUNT(*) FROM nucleo_carregamento_itens "
        "WHERE rota_id = ? AND carregamento_id IS NULL AND removido_em IS NOT NULL", (rota["id"],)).fetchone()[0]
    cur = conn.execute("""
        INSERT INTO nucleo_carregamentos (rota_id, agent_id, uuid, placa, tipo_veiculo, carregador, foto_caminho,
                                          foto_sha256, resultado_foto, ocupacao_pct, revisao_humana,
                                          qtd_carregados, qtd_desmarcados)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (rota["id"], agent_id, uuid, placa, tipo_veiculo, carregador, foto.get("caminho"), foto.get("sha256"),
          foto.get("resultado"), foto.get("ocupacao_pct"), 1 if foto.get("revisao_humana") else 0,
          contadores["total"], qtd_desmarcados))
    carregamento_id = cur.lastrowid
    conn.execute("UPDATE nucleo_carregamento_itens SET carregamento_id = ? "
                 "WHERE rota_id = ? AND carregamento_id IS NULL AND removido_em IS NOT NULL",
                 (carregamento_id, rota["id"]))
    operacao.registrar_evento(
        conn, "CARREGAMENTO", banco.ORIGEM_APP, None, rota_id=rota["id"], agent_id=agent_id, uuid=uuid,
        dados={"carregamento_id": carregamento_id, "carregador": carregador, "qtd_carregados": contadores["total"],
               "qtd_desmarcados": qtd_desmarcados, "ocupacao_pct": foto.get("ocupacao_pct"),
               "rota_cancelada": rota_cancelada,
               "desmarcados": [{"parada_id": p["id"], "codigo": p["codigo"], "motivo": m, "observacao": o}
                               for p, m, o in pedidos]})
    conn.commit()
    logger.info(f"Carregamento rota {rota['id']} (agent {agent_id}): {contadores['total']} carregados, "
                f"{qtd_desmarcados} desmarcados, rota_cancelada={rota_cancelada}")
    return {"ok": True, "carregamento": carregamento_da_rota(conn, rota["id"]), "rota_cancelada": rota_cancelada}
