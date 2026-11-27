# -*- coding: utf-8 -*-
"""
nucleo/materializar.py

Pool direto da Stokki (planejamento.fonte_pool = stokki, plano
docs/superpowers/plans/2026-11-26-pool-direto-da-stokki.md, Task 5): o pedido
vive no pool e nos rascunhos com id sintetico negativo (-id_pool) e so ganha
servico na Vuupt AO ENVIAR (rascunho, incremento, parada em rota enviada,
criacao direta de rota, fila do freshhub). Aqui o servico nasce:

  1. recusa, com motivo, pedido que nao esta ABERTO no nucleo (cancelado,
     em rota, entregue...) e reentrega (-R) sem servico: nunca cria duplicado;
  2. trava por codigo no banco (nucleo_materializacao): clique duplo,
     "enviar todas" e o incremento (outro processo) nunca criam dois servicos;
     quem perde a corrida espera e reaproveita o id que o outro gravou;
  3. busca por code antes de criar: servico not_assigned que ja existe e
     adotado; servico em outro status (done/canceled/atribuido) e recusa;
  4. cria com o payload do pipeline (dados_json["payload"]) com as colunas
     do nucleo por cima (fonte da verdade do que o Planejamento edita);
  5. grava o id no nucleo e troca -id pelo real nos rascunhos na MESMA
     transacao (registrar_importacao -> upsert_pedido -> adotar_servico).

Falha de um pedido nunca derruba os outros. NAO importa vuupt_client nem
painel_agentes (nucleo/test_acoplamento_vuupt): quem chama passa a instancia
`vuupt` pronta (buscar_servico_por_code, criar_ou_atualizar_servico,
skill_ids_por_nome).
"""
import copy
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from nucleo import banco, normalizacao, pedidos
from nucleo.adocao_servico import adotar_servico

logger = logging.getLogger(__name__)

ESPERA_TRAVA_S = 30          # quem perde a corrida espera o outro terminar ate isso
INTERVALO_ESPERA_S = 0.25
TRAVA_VELHA_MIN = 10         # trava mais velha que isso e de processo que morreu: expira
STATUS_LIVRE_VUUPT = "not_assigned"
_REENTREGA = re.compile(r"-R\d+")


@dataclass
class ResultadoMaterializacao:
    ok: dict[str, int] = field(default_factory=dict)       # codigo normalizado -> service_id real
    falhas: dict[str, str] = field(default_factory=dict)   # codigo normalizado -> motivo legivel
    # falhas por trava ocupada (outro envio criando o servico agora): quem chama NAO descarta o pedido
    ocupados: set[str] = field(default_factory=set)
    codigos: dict[int, str] = field(default_factory=dict)  # id sintetico do item -> codigo do nucleo


class _Recusa(Exception):
    """Pedido que nao vira servico agora; a mensagem e o motivo mostrado na tela e no log."""


class _Ocupado(_Recusa):
    """Outro envio segura a trava do pedido: nao e recusa do pedido, e "tente de novo"."""


def _modo_teste(config: dict | None, modo_teste: bool) -> bool:
    return bool(modo_teste or (config or {}).get("modo_teste"))


def _id_do_item(item: dict):
    sid = item.get("id", item.get("service_id"))
    try:
        return int(sid) if sid is not None else None
    except (TypeError, ValueError):
        return None


def _codigo_do_item(conn: sqlite3.Connection, item: dict, sid) -> str | None:
    """O id sintetico e a identidade; o code do item so vale sem id (fila do freshhub)."""
    pelo_id = pedidos.codigo_por_id_sintetico(conn, sid) if sid is not None else None
    return pelo_id or normalizacao.normalizar_codigo(item.get("code") or item.get("codigo"))


def materializar_servicos(itens: list[dict], vuupt, config: dict | None, conn: sqlite3.Connection | None = None,
                          *, modo_teste: bool = False, espera_s: float | None = None) -> ResultadoMaterializacao:
    """Cria na Vuupt o servico dos itens SEM servico (id <= 0, ou sem id) e devolve
    {codigo: service_id} em `ok` e {codigo: motivo} em `falhas`; item com id real e ignorado
    (nem aparece no resultado). Ordem do lote = ordem dos itens. Commita na `conn` a cada pedido
    (a trava precisa ser vista pelos outros processos na hora). Modo teste: nada vai na Vuupt e
    cada pedido sem servico volta em `falhas` com o motivo. Trava ocupada alem de `espera_s` (padrao
    ESPERA_TRAVA_S): falha com o codigo tambem em `ocupados`. `codigos` = {id do item: codigo do nucleo}
    (quem chama casa o resultado pelo id, nao pelo codigo que tem na mao)."""
    espera_s = ESPERA_TRAVA_S if espera_s is None else espera_s
    resultado = ResultadoMaterializacao()
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        vistos: set[str] = set()
        for item in itens or []:
            sid = _id_do_item(item)
            if sid is not None and sid > 0:
                continue
            codigo = _codigo_do_item(conn, item, sid)
            if not codigo:
                resultado.falhas[str(sid)] = f"id {sid} sem pedido no nucleo: servico nao criado"
                continue
            if sid is not None:
                resultado.codigos[sid] = codigo
            if codigo in vistos:
                continue
            vistos.add(codigo)
            try:
                resultado.ok[codigo] = _materializar_um(conn, codigo, vuupt, _modo_teste(config, modo_teste), espera_s)
                logger.info(f"materializar {codigo}: servico {resultado.ok[codigo]} na Vuupt")
            except _Recusa as e:
                resultado.falhas[codigo] = str(e)
                if isinstance(e, _Ocupado):
                    resultado.ocupados.add(codigo)
                logger.warning(f"materializar {codigo}: servico NAO criado -- {e}")
            except Exception as e:  # noqa: BLE001 - um pedido nunca derruba os outros
                resultado.falhas[codigo] = f"erro ao criar o servico na Vuupt: {e}"
                logger.exception(f"materializar {codigo}: erro inesperado")
    finally:
        if fechar:
            conn.close()
    return resultado


def materializar_lote(itens: list[dict], vuupt, config: dict | None, conn: sqlite3.Connection | None = None,
                      *, modo_teste: bool = False) -> tuple[list[dict], dict[str, str]]:
    """Para quem monta a rota com uma lista de servicos (criacao direta, incremento): devolve
    (copia da lista so com quem tem servico, ja com o id real; falhas {codigo: motivo}).
    Lista sem id sintetico volta igual e nada e chamado."""
    if not any((_id_do_item(s) or 0) < 0 for s in itens or []):
        return list(itens or []), {}
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        res = materializar_servicos(itens, vuupt, config, conn, modo_teste=modo_teste)
        saida = []
        for s in itens:
            sid = _id_do_item(s)
            if sid is not None and sid > 0:
                saida.append(s)
                continue
            real = res.ok.get(_codigo_do_item(conn, s, sid))
            if real:
                saida.append(dict(s, id=real))
        return saida, res.falhas
    finally:
        if fechar:
            conn.close()


# ── Um pedido ──────────────────────────────────────────────────────────────────

def _linha(conn: sqlite3.Connection, codigo: str):
    return conn.execute("SELECT * FROM nucleo_pedidos WHERE codigo = ?", (codigo,)).fetchone()


def _conferir_pedido(linha, codigo: str) -> None:
    if linha is None:
        raise _Recusa(f"pedido {codigo} nao esta no nucleo")
    if linha["status"] != banco.PEDIDO_ABERTO:
        raise _Recusa(f"pedido {codigo} esta {linha['status']} no nucleo (nao ABERTO): servico nao criado")
    if linha["vuupt_service_id"] is None and _REENTREGA.search(codigo):
        raise _Recusa(f"reentrega {codigo} sem servico na Vuupt: a reentrega nasce na Vuupt (recriar o pedido "
                      f"la), aqui nunca se cria outro servico")


def _materializar_um(conn: sqlite3.Connection, codigo: str, vuupt, modo_teste: bool, espera_s: float) -> int:
    linha = _linha(conn, codigo)
    _conferir_pedido(linha, codigo)
    if linha["vuupt_service_id"] is not None:
        return _ja_tem_servico(conn, codigo, int(linha["vuupt_service_id"]))
    if modo_teste:
        raise _Recusa("modo teste: servico nao criado na Vuupt")

    processo = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex[:8]}"
    limite = time.monotonic() + espera_s
    while not _travar(conn, codigo, processo):
        # outro envio esta criando este servico: espera e reaproveita o id que ele gravar
        sid = conn.execute("SELECT vuupt_service_id FROM nucleo_pedidos WHERE codigo = ?", (codigo,)).fetchone()
        if sid and sid[0] is not None:
            return int(sid[0])
        if time.monotonic() >= limite:
            raise _Ocupado(f"outro envio esta criando o servico de {codigo} ha mais de {espera_s:g} s: "
                           f"pedido em materializacao por outro envio, tente de novo em instantes")
        time.sleep(INTERVALO_ESPERA_S)
    try:
        linha = _linha(conn, codigo)   # dentro da trava: o outro pode ter gravado antes de soltar
        _conferir_pedido(linha, codigo)
        if linha["vuupt_service_id"] is not None:
            return _ja_tem_servico(conn, codigo, int(linha["vuupt_service_id"]))
        return _criar_ou_adotar(conn, codigo, linha, vuupt)
    finally:
        try:
            if conn.in_transaction:
                conn.rollback()
            conn.execute("DELETE FROM nucleo_materializacao WHERE codigo = ? AND processo = ?", (codigo, processo))
            conn.commit()
        except sqlite3.Error as e:   # nao mascara o resultado: a trava expira sozinha (TRAVA_VELHA_MIN)
            logger.warning(f"materializar {codigo}: trava nao liberada ({e}); expira em {TRAVA_VELHA_MIN} min")


def _ja_tem_servico(conn: sqlite3.Connection, codigo: str, sid: int) -> int:
    """O pedido ja ganhou servico (outro envio, sync, fila): so garante o rascunho no id real."""
    adotar_servico(conn, codigo, sid)
    conn.commit()
    return sid


def _travar(conn: sqlite3.Connection, codigo: str, processo: str) -> bool:
    velha = (datetime.now() - timedelta(minutes=TRAVA_VELHA_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    apagadas = conn.execute("DELETE FROM nucleo_materializacao WHERE codigo = ? AND iniciado_em < ?",
                            (codigo, velha)).rowcount
    if apagadas:
        logger.warning(f"materializar {codigo}: trava com mais de {TRAVA_VELHA_MIN} min expirada (processo morto?)")
    pegou = conn.execute("INSERT OR IGNORE INTO nucleo_materializacao (codigo, iniciado_em, processo) VALUES (?, ?, ?)",
                         (codigo, banco.agora(), processo)).rowcount == 1
    conn.commit()
    return pegou


def _criar_ou_adotar(conn: sqlite3.Connection, codigo: str, linha, vuupt) -> int:
    code = f"#{codigo}"
    existente = vuupt.buscar_servico_por_code(code)
    if existente:
        status = existente.get("status")
        if status != STATUS_LIVRE_VUUPT or existente.get("id") is None:
            raise _Recusa(f"ja existe o servico {existente.get('id')} na Vuupt com o codigo {codigo} e status "
                          f"'{status}': nao crio outro (pulado_atribuido); conferir o pedido a mao")
        sid = int(existente["id"])
        if not pedidos.campos_editados(linha["edicao_local_json"]):
            _ja_tem_servico(conn, codigo, sid)
            _propagar_origens(conn, codigo, sid, linha["agendamento_inicio"])
            logger.info(f"materializar {codigo}: servico {sid} ja existia na Vuupt (not_assigned), adotado sem criar")
            return sid
        # edicao local (Planejamento) que a Vuupt ainda nao tem: reenvia o editado no servico existente
        logger.info(f"materializar {codigo}: servico {sid} ja existia na Vuupt (not_assigned); reenvia a edicao local")

    base = _payload_do_pipeline(linha, vuupt)
    payload = montar_payload(linha, vuupt, base)
    resposta, acao = vuupt.criar_ou_atualizar_servico(payload)
    servico = pedidos.servico_da_resposta(resposta)
    if acao == "pulado_atribuido":
        raise _Recusa(f"a Vuupt ja tem servico de {codigo} fora de 'nao atribuido' (pulado_atribuido): "
                      f"nao crio outro; conferir o pedido a mao")
    if servico.get("id") is None and existente and acao == "pulado_sem_alteracao":
        sid = int(existente["id"])   # a Vuupt ja tem exatamente o editado
        _ja_tem_servico(conn, codigo, sid)
    else:
        if servico.get("id") is None:
            raise _Recusa(f"a Vuupt devolveu '{acao}' sem id do servico de {codigo}")
        sid = int(servico["id"])
        try:
            pedidos.registrar_importacao(payload, resposta, acao, conn=conn)   # id no nucleo + rascunhos, 1 transacao
        except Exception as e:
            raise _Recusa(f"servico {sid} criado na Vuupt mas nao gravado no nucleo ({e}); o proximo envio adota "
                          f"o mesmo servico") from e
    _fingerprint_do_pipeline(codigo, base, servico, vuupt)
    _propagar_origens(conn, codigo, sid, linha["agendamento_inicio"])
    return sid


def _fingerprint_do_pipeline(codigo: str, base: dict, servico: dict, vuupt) -> None:
    """A Vuupt recebeu o payload com as colunas do nucleo por cima (edicao do Planejamento), e o
    criar_ou_atualizar gravou o fingerprint DELE. Regrava com o payload do pipeline: a proxima rodada
    (caminho antigo, servico de volta a not_assigned) da pulado_sem_alteracao e nao desfaz a edicao,
    salvo mudanca real na Stokki (mesmo comportamento de quando o Planejamento edita a Vuupt direto).
    Best-effort: o servico ja existe."""
    try:
        vuupt.salvar_fingerprint_importacao(base, customer_id=servico.get("customer_id"),
                                            sender_id=servico.get("sender_id"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"materializar {codigo}: fingerprint do pipeline nao regravado ({e}); a proxima rodada "
                       f"do pipeline pode reenviar o payload da Stokki")


# ── Payload ────────────────────────────────────────────────────────────────────

def _para_payload(ts) -> str | None:
    """Hora local do nucleo -> o que o pipeline manda (ISO com -03:00)."""
    if not ts:
        return None
    dt = datetime.fromisoformat(str(ts).replace(" ", "T", 1))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=normalizacao.FUSO_LOCAL)
    return dt.isoformat(timespec="seconds")


def _payload_do_pipeline(linha, vuupt) -> dict:
    """O payload como o pipeline mandaria pra Vuupt (dados_json["payload"] sem as chaves "_" e com a
    skill, gravada pelo nome em _skill_nome, ja em id): base da materializacao e do fingerprint."""
    try:
        dados = json.loads(linha["dados_json"] or "{}")
    except (TypeError, ValueError):
        dados = {}
    base = (dados or {}).get("payload")
    if not isinstance(base, dict) or not base.get("code"):
        raise _Recusa(f"pedido {linha['codigo']} sem o payload do pipeline no nucleo: nao da pra montar o servico")
    payload = copy.deepcopy(base)
    nome_skill = payload.get("_skill_nome")
    for chave in [k for k in payload if k.startswith("_")]:
        payload.pop(chave)
    if nome_skill and not payload.get("skills"):
        ids = vuupt.skill_ids_por_nome(nome_skill)
        if ids:
            payload["skills"] = [{"id": i} for i in ids]
    return payload


def montar_payload(linha, vuupt, base: dict | None = None) -> dict:
    """Payload do pipeline (_payload_do_pipeline, ou `base` ja montada) com as colunas do nucleo por cima
    (fonte da verdade do que o Planejamento/portal edita no pedido sem servico): titulo, destinatario
    (nome, telefone), janela de atendimento, endereco, coordenadas, agendamento, caixas, nota e
    complemento. Coluna vazia nao apaga o valor do payload (exceto agendamento, ver abaixo)."""
    payload = copy.deepcopy(base if base is not None else _payload_do_pipeline(linha, vuupt))
    customer = payload.setdefault("customer", {})
    if linha["titulo"]:
        payload["title"] = linha["titulo"]
    if linha["destinatario_nome"]:
        customer["name"] = linha["destinatario_nome"]
    if linha["destinatario_telefone"]:
        payload["phone_number"] = customer["phone_number"] = linha["destinatario_telefone"]
    if linha["horario_inicio"]:
        customer["operating_hour_start"] = linha["horario_inicio"]
    if linha["horario_fim"]:
        customer["operating_hour_end"] = linha["horario_fim"]
    if linha["endereco"]:
        customer["address"] = linha["endereco"]
    if linha["latitude"] is not None and linha["longitude"] is not None:
        payload["latitude"] = customer["latitude"] = linha["latitude"]
        payload["longitude"] = customer["longitude"] = linha["longitude"]
    for coluna, chave in (("agendamento_inicio", "scheduled_start"), ("agendamento_fim", "scheduled_end")):
        valor = _para_payload(linha[coluna])
        if valor:
            payload[chave] = valor
        else:
            payload.pop(chave, None)
    if linha["caixas"] is not None:
        payload["dimension_3"] = linha["caixas"]
    if linha["nota"]:
        payload["note"] = linha["nota"]
    if linha["complemento"]:
        payload["address_complement"] = linha["complemento"]
    return payload


# ── Origem da data (dia fixo / data automatica) ────────────────────────────────

def _propagar_origens(conn: sqlite3.Connection, codigo: str, service_id: int, agendamento_inicio) -> None:
    """A data do pedido sem servico foi marcada pelo codigo (DIA_FIXO/EQUIPE/AUTOMATICO, Task 4);
    aqui a mesma origem passa a valer tambem pelo id real (telas que so mandam o id). Best-effort."""
    if not agendamento_inicio:
        return
    try:
        from roteirizacao import registro_dia_fixo   # tardio: o nucleo nao depende de roteirizacao
        caminho = conn.execute("PRAGMA database_list").fetchone()[2]
        dia = date.fromisoformat(str(agendamento_inicio)[:10])
        so_codigo = {"code": f"#{codigo}"}
        c = registro_dia_fixo.conectar(caminho)
        try:
            for origem in registro_dia_fixo.ORIGENS_NAO_CLIENTE:
                if registro_dia_fixo._tem_origem(c, so_codigo, dia, (origem,)):
                    registro_dia_fixo.registrar_origem(c, {"id": service_id, "code": f"#{codigo}"}, dia, origem)
        finally:
            c.close()
    except Exception as e:  # noqa: BLE001 - o servico ja existe; a origem pelo codigo continua valendo
        logger.warning(f"materializar {codigo}: origem da data nao copiada pro servico {service_id} ({e})")
