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

Revisao de seguranca 22/11 (docs/revisoes/2026-11-22-checklist-carregamento-seguranca.md):
M1 - a ida a Vuupt e interativa (1 tentativa, timeout curto, sem retry de 429), o laco
     tem orcamento total (ORCAMENTO_S, contado desde o inicio do request, abaixo do timeout
     de 20 s do app), cada (rota, motorista) aceita no maximo TETO_FALHAS POSTs com falha em
     JANELA_FALHAS_S (depois 429 sem chamar a Vuupt; so quando ainda ha parada a tirar) e
     so um POST do checklist por rota roda de cada vez (o segundo leva 409).
M2 - a rota ao vivo na Vuupt tem que ser do motorista que confirma, e o checklist so
     vale pro motorista atual da rota: trocou o motorista, o novo faz o dele e o antigo
     e arquivado (rota_id = -id, rota em rota_id_original, evento CARREGAMENTO_SUBSTITUIDO).
"""
import logging
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date
from functools import partial
from pathlib import Path

from nucleo import banco, operacao, pedidos as nucleo_pedidos, ponte_vuupt

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent

# Subconjunto de expedicao.MOTIVOS_EXCLUSAO que o motorista pode escolher.
MOTIVOS_DESMARCAR = ("Não coube", "Não Encontrado", "Outros")
MOTIVO_OUTROS = "Outros"

# M1: teto de POSTs com falha na Vuupt por (rota, motorista) e orcamento de tempo do request.
TETO_FALHAS = 5
JANELA_FALHAS_S = 600
ORCAMENTO_S = 12          # o app desiste em 20 s (app_motorista/src/api.ts TIMEOUT_MS)
MSG_MUITAS_TENTATIVAS = "Muitas tentativas. Chame a logística."
MSG_TEMPO_ESGOTADO = "Tempo esgotado, tente de novo."       # igual a edicao_rota.MSG_TEMPO_ESGOTADO
MSG_EM_ANDAMENTO = "Checklist em andamento, aguarde."

_relogio = time.monotonic     # segundos; trocado em teste


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
    """Checklist gravado na rota (de qualquer motorista; os arquivados tem rota_id negativo)."""
    row = conn.execute("SELECT * FROM nucleo_carregamentos WHERE rota_id = ?", (rota_id,)).fetchone()
    return dict(row) if row else None


def carregamento_do_motorista_atual(conn: sqlite3.Connection, rota: sqlite3.Row) -> dict | None:
    """O checklist so vale pro motorista atual da rota (M2): trocou o motorista, o novo faz o dele."""
    c = carregamento_da_rota(conn, rota["id"])
    return c if c and c["agent_id"] == rota["agent_id"] else None


# ── Teto de tentativas com falha e trava de POST em andamento (M1) ────────────
# Conta so POST que chamou a Vuupt e falhou (validacao 400/409, replay de uuid e "rota de
# outro motorista" nao contam), por (rota_id, agent_id): depois da troca de motorista o novo
# nao herda o 429 do antigo. Estado em memoria do processo: o motorista-api e UM waitress com
# threads, dai o lock. Reiniciar o servico zera contadores e travas (aceitavel: so freiam
# repeticao). A trava de POST em andamento (por rota) torna o teto atomico.

_falhas_por_rota: dict[tuple[int, int], list[float]] = {}
_em_andamento: set[int] = set()
_trava_falhas = threading.Lock()


def _falhas_na_janela(chave: tuple, agora: float) -> list[float]:
    """Chamar com _trava_falhas: descarta as falhas fora da janela e devolve as que ficam."""
    lista = [t for t in _falhas_por_rota.get(chave, []) if agora - t < JANELA_FALHAS_S]
    if lista:
        _falhas_por_rota[chave] = lista
    else:
        _falhas_por_rota.pop(chave, None)
    return lista


def exigir_abaixo_do_teto(rota_id: int, agent_id: int, agora: float | None = None) -> None:
    """TETO_FALHAS falhas do motorista na rota em JANELA_FALHAS_S -> OperacaoInvalida 429."""
    agora = _relogio() if agora is None else agora
    with _trava_falhas:
        if len(_falhas_na_janela((rota_id, agent_id), agora)) >= TETO_FALHAS:
            raise operacao.OperacaoInvalida(MSG_MUITAS_TENTATIVAS, 429)


def registrar_falha(rota_id: int, agent_id: int, agora: float | None = None) -> None:
    agora = _relogio() if agora is None else agora
    with _trava_falhas:
        _falhas_na_janela((rota_id, agent_id), agora)
        _falhas_por_rota.setdefault((rota_id, agent_id), []).append(agora)


def zerar_falhas(rota_id: int, agent_id: int) -> None:
    with _trava_falhas:
        _falhas_por_rota.pop((rota_id, agent_id), None)


@contextmanager
def checklist_em_andamento(rota_id: int):
    """Um POST do checklist por rota de cada vez: o segundo leva 409 sem modelo nem Vuupt.
    Liberada no finally (excecao inclusive)."""
    with _trava_falhas:
        if rota_id in _em_andamento:
            raise operacao.OperacaoInvalida(MSG_EM_ANDAMENTO, 409)
        _em_andamento.add(rota_id)
    try:
        yield
    finally:
        with _trava_falhas:
            _em_andamento.discard(rota_id)


def _ja_removida(conn: sqlite3.Connection, rota_id: int, parada: sqlite3.Row) -> bool:
    """Saiu da rota numa tentativa anterior (nao vai mais a Vuupt)."""
    item = _item(conn, rota_id, parada)
    return bool(item and item["removido_em"] and parada["situacao"] == banco.PARADA_CANCELADA)


def conferir_teto(conn: sqlite3.Connection, rota: sqlite3.Row, agent_id: int, pedidos: list[tuple],
                  agora: float | None = None) -> None:
    """Teto so quando ao menos 1 parada ainda vai a Vuupt: POST sem desmarcados (ou so com
    paradas ja removidas) e rota do app nunca levam 429. `pedidos` = _validar_desmarcados."""
    if rota["provedor"] == banco.PROVEDOR_APP:
        return
    if any(not _ja_removida(conn, rota["id"], parada) for parada, _m, _o in pedidos):
        exigir_abaixo_do_teto(rota["id"], agent_id, agora)


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


def _tirar_da_vuupt_ou_local(rota: sqlite3.Row, parada: sqlite3.Row, motivo: str, observacao: str | None,
                             tempo_esgotado=None) -> dict:
    """Tira a parada da rota de verdade. Rota APP: so confere se a parada ainda nao comecou
    (os efeitos locais sao de `confirmar`). Rota da Vuupt: edicao_rota.tirar_pedido (status ao
    vivo; recusa pedido comecado e rota que na Vuupt ja e de outro motorista); `fora_da_rota`
    conta como sucesso. Interativo: 1 tentativa e timeout curto, 429/timeout viram erro curto.
    `tempo_esgotado()` e conferido de novo entre a leitura e a escrita na Vuupt.
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
    # rota veio de rota_do_motorista: rota["agent_id"] e o motorista que esta confirmando
    resultado = edicao_rota.tirar_pedido(token, rota["vuupt_route_id"], parada["service_id"],
                                         agent_id_esperado=rota["agent_id"], interativo=True,
                                         tempo_esgotado=tempo_esgotado)
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


def _arquivar_de_outro_motorista(conn: sqlite3.Connection, rota: sqlite3.Row, antigo: dict, agent_novo: int) -> None:
    """Motorista da rota trocou: o checklist do anterior deixa de valer (M2). rota_id e NOT NULL
    UNIQUE, entao a linha fica com rota_id = -id (unico; rota real e sempre positiva) e a rota em
    rota_id_original. Os itens continuam ligados a ela; nada e desfeito na Vuupt."""
    conn.execute("UPDATE nucleo_carregamentos SET rota_id = ?, rota_id_original = ?, substituido_em = ? WHERE id = ?",
                 (-antigo["id"], antigo["rota_id"], banco.agora(), antigo["id"]))
    operacao.registrar_evento(
        conn, "CARREGAMENTO_SUBSTITUIDO", banco.ORIGEM_APP, None, rota_id=rota["id"], agent_id=agent_novo,
        dados={"carregamento_antigo_id": antigo["id"], "agent_antigo": antigo["agent_id"], "agent_novo": agent_novo})
    logger.info(f"Carregamento rota {rota['id']}: checklist {antigo['id']} do agent {antigo['agent_id']} "
                f"arquivado (motorista agora e {agent_novo})")


def confirmar(conn: sqlite3.Connection, rota: sqlite3.Row, agent_id: int, *, carregador: str, uuid: str,
              desmarcados: list[dict], foto: dict, tirar=None, relogio=None, inicio=None) -> dict:
    """Checklist de carregamento: tira cada parada desmarcada da rota e, se TODAS sairam, grava
    nucleo_carregamentos. Idempotente por uuid da mesma rota e motorista; outro uuid numa rota ja
    confirmada pelo mesmo motorista (ou uuid ja usado) levanta 409. Checklist de OUTRO motorista
    (rota trocou de dono) nao bloqueia: e arquivado quando este grava o dele.
    `foto` ja vem gravada em disco por quem chama: {caminho, sha256, resultado, ocupacao_pct, revisao_humana}.
    `tirar(rota, parada, motivo, observacao, **kw)` e `relogio()` (segundos, monotonic) sao injetaveis
    (teste). `inicio` = relogio() do comeco do request (o orcamento conta a foto/modelo); padrao: agora.
    Devolve {"ok": True, "carregamento", "rota_cancelada"} ou
    {"ok": False, "falhas": [{parada_id, codigo, erro}], "removidos": [parada_id]} sem gravar o checklist.
    Rota com TETO_FALHAS falhas na Vuupt em JANELA_FALHAS_S levanta 429 sem chamar a Vuupt
    (so se ainda houver parada a tirar). Quem chama pelo app segura checklist_em_andamento."""
    relogio = relogio or _relogio
    inicio = relogio() if inicio is None else inicio
    vai_a_vuupt = rota["provedor"] != banco.PROVEDOR_APP

    def sem_tempo() -> bool:
        return vai_a_vuupt and relogio() - inicio >= ORCAMENTO_S

    tirar = tirar or partial(_tirar_da_vuupt_ou_local, tempo_esgotado=sem_tempo)
    if not uuid:
        raise operacao.OperacaoInvalida("Carregamento sem uuid (gerado no aparelho).")
    rota = operacao.rota_do_motorista(conn, rota["id"], agent_id)
    ja = conn.execute("SELECT * FROM nucleo_carregamentos WHERE uuid = ? AND rota_id = ?",
                      (uuid, rota["id"])).fetchone()
    if ja and ja["agent_id"] == agent_id:
        return {"ok": True, "carregamento": dict(ja), "rota_cancelada": rota["status"] == banco.ROTA_CANCELADA}
    if carregamento_do_motorista_atual(conn, rota):
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
    conferir_teto(conn, rota, agent_id, pedidos, relogio())

    replica = operacao.eh_rota_replica(rota)
    falhas, removidos, rota_cancelada = [], [], False
    parar, chamou_vuupt, de_outro = None, False, False
    for parada, motivo, observacao in pedidos:
        if _ja_removida(conn, rota["id"], parada):
            removidos.append(parada["id"])          # saiu numa tentativa anterior
            continue
        if parar is None and sem_tempo():
            parar = MSG_TEMPO_ESGOTADO              # orcamento do request acabou: nao chama a Vuupt pras outras
        if parar:
            falhas.append({"parada_id": parada["id"], "codigo": parada["codigo"], "erro": parar})
            continue
        chamou_vuupt = chamou_vuupt or vai_a_vuupt
        try:
            resultado = tirar(rota, parada, motivo, observacao)
        except Exception as e:  # noqa: BLE001
            resultado = {"ok": False, "erro": f"Falha ao tirar da rota: {e}"}
        if not resultado.get("ok"):
            erro = resultado.get("erro") or "Não foi possível tirar da rota."
            falhas.append({"parada_id": parada["id"], "codigo": parada["codigo"], "erro": erro})
            de_outro = de_outro or bool(resultado.get("rota_de_outro_motorista"))
            if (resultado.get("rota_de_outro_motorista") or resultado.get("vuupt_indisponivel")
                    or resultado.get("tempo_esgotado")):
                parar = erro                        # rota de outro / Vuupt fora / sem tempo: nenhuma outra sai
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
        if not replica and parada["service_id"] and not resultado.get("ja_fora_da_rota"):
            try:
                nucleo_pedidos.marcar_aberto_por_service_ids([parada["service_id"]], conn=conn)
            except Exception as e:  # noqa: BLE001 - a remocao ja esta gravada; o sync recupera o pedido
                logger.warning(f"Carregamento rota {rota['id']}: pedido da parada {parada['id']} nao voltou ao pool: {e}")
        removidos.append(parada["id"])

    if falhas:
        logger.warning(f"Carregamento rota {rota['id']}: {len(falhas)} parada(s) nao sairam da rota: {falhas}")
        if chamou_vuupt and not de_outro:
            # so falha de verdade na Vuupt conta (rota do app, sem tempo antes de chamar e rota de
            # outro motorista nao)
            registrar_falha(rota["id"], agent_id, relogio())
        return {"ok": False, "falhas": falhas, "removidos": removidos}
    zerar_falhas(rota["id"], agent_id)

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
    antigo = carregamento_da_rota(conn, rota["id"])
    if antigo and antigo["agent_id"] != agent_id:
        _arquivar_de_outro_motorista(conn, rota, antigo, agent_id)
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
