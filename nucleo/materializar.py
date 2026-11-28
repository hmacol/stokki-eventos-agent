# -*- coding: utf-8 -*-
"""
nucleo/materializar.py

Pool direto da Stokki (planejamento.fonte_pool = stokki, plano
docs/superpowers/plans/2026-11-26-pool-direto-da-stokki.md, Task 5): o pedido
vive no pool e nos rascunhos com id sintetico negativo (-id_pool) e so ganha
servico na Vuupt AO ENVIAR (rascunho, incremento, parada em rota enviada,
criacao direta de rota, fila do freshhub). Aqui o servico nasce:

  1. recusa, com motivo, pedido que nao esta ABERTO no nucleo (cancelado,
     em rota, entregue...), reentrega (-R) sem servico, pedido agrupado e linha
     sem servico que nao NASCEU sem servico ou ja teve servico (nucleo_servicos):
     nunca cria duplicado. So recusa de regra tira a parada; falha tecnica
     (rede, 429, 5xx) vai em `transitorios` e a parada fica;
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
    # falha TECNICA (rede, timeout, HTTP 5xx/429 depois do backoff, banco ocupado, modo teste, erro
    # inesperado): codigo -> causa. Tambem esta em `falhas`. Nao e recusa do pedido: quem chama NAO
    # descarta a parada (o envio do rascunho para; incremento/criacao direta/fila pulam so nesta rodada)
    transitorios: dict[str, str] = field(default_factory=dict)
    codigos: dict[int, str] = field(default_factory=dict)  # id sintetico do item -> codigo do nucleo


class _Recusa(Exception):
    """Recusa de REGRA: pedido que nao vira servico (nao ABERTO, agrupado, -R, pulado_atribuido,
    done/canceled...); a mensagem e o motivo mostrado na tela e no log. So ela tira a parada do envio."""


class _RecusaDadoLocal(_Recusa):
    """Recusa por dado do nucleo malformado (ValueError/TypeError ao montar o payload). `tipo_erro` entra
    na regra do erro sistemico: o mesmo erro local em todo o lote e bug nosso, nao do pedido."""

    def __init__(self, mensagem: str, tipo_erro: str):
        super().__init__(mensagem)
        self.tipo_erro = tipo_erro


class _Ocupado(_Recusa):
    """Outro envio segura a trava do pedido: nao e recusa do pedido, e "tente de novo"."""


class _Transitorio(Exception):
    """Falha tecnica conhecida (modo teste, servico criado mas nao gravado no nucleo): a mensagem vale
    como esta; o pedido continua no envio."""


class EnvioInterrompido(Exception):
    """Levantada pelo `pulso` de quem chama (ex.: a trava do envio do rascunho foi assumida por outro):
    para o lote na hora, sem criar mais nenhum servico. Nunca vira falha de pedido: sobe."""


FALHA_RECUSA, FALHA_OCUPADO, FALHA_TRANSITORIA = "recusa", "ocupado", "transitoria"


# Erro HTTP da Vuupt: o client (vuupt_client.VuuptAPIError, nao importado aqui de proposito) so traz o
# texto "Status NNN: {...}" (ou "<acao> do servico X: status NNN: ..."), sem atributo de status.
_STATUS_HTTP = re.compile(r"\bstatus (\d{3})\b:?\s*", re.IGNORECASE)
# PERMANENTE: a Vuupt recusou ESTE pedido (payload invalido, nao existe, conflito -- 409 pode ser "codigo ja
# existe": recusa com motivo, nunca tenta criar de novo). Repetir nao resolve.
STATUS_RECUSA_VUUPT = {400, 404, 409, 410, 422}
STATUS_CREDENCIAL_VUUPT = {401, 403}   # transitorio, mas com mensagem propria (token/permissao)
# Erro sistemico (Hugo, onda 3): TODOS os pedidos sem servico do lote recusados com o MESMO status permanente
# e nenhum sucesso = provavel problema nosso (payload/config) ou da Vuupt, nao dos pedidos: vira transitorio
# (nada e removido). Limiar 2: com 1 pedido so nao da pra distinguir, segue a regra normal.
MINIMO_LOTE_SISTEMICO = 2


def _erro_http_vuupt(e: BaseException) -> tuple[int, str] | None:
    """(status, mensagem da Vuupt) do VuuptAPIError; None pra qualquer outra excecao ou sem status."""
    if not any(c.__name__ == "VuuptAPIError" for c in type(e).__mro__):
        return None
    m = _STATUS_HTTP.search(str(e))
    return (int(m.group(1)), str(e)[m.end():].strip()) if m else None


def classificar_falha(e: BaseException) -> str:
    """Recusa de regra (_Recusa) ou erro PERMANENTE da Vuupt (VuuptAPIError 400/404/409/410/422) x trava
    ocupada (_Ocupado) x falha tecnica (qualquer outra excecao: 408/425/429/5xx, 401/403, rede, timeout,
    banco, inesperado). Na duvida e transitoria: nunca descarta parada por erro tecnico."""
    if isinstance(e, _Ocupado):
        return FALHA_OCUPADO
    if isinstance(e, _Recusa):
        return FALHA_RECUSA
    http = _erro_http_vuupt(e)
    if http and http[0] in STATUS_RECUSA_VUUPT:
        return FALHA_RECUSA
    return FALHA_TRANSITORIA


def _motivo_da_falha(e: BaseException, tipo: str) -> tuple[str, str]:
    """(motivo em `falhas`, causa em `transitorios`) legiveis pra tela e log."""
    http = _erro_http_vuupt(e)
    if tipo == FALHA_RECUSA and http:
        return f"a Vuupt recusou o pedido (Status {http[0]}): {http[1]}", ""
    if tipo != FALHA_TRANSITORIA or isinstance(e, _Transitorio):
        return str(e), str(e)
    if http and http[0] in STATUS_CREDENCIAL_VUUPT:
        msg = (f"credencial/permissao da Vuupt recusada (Status {http[0]}): avise o Hugo; nada descartado, "
               f"tente de novo depois -- {http[1]}")
        return msg, msg
    return f"falha tecnica ao criar servico na Vuupt ({e}): tente de novo", str(e)


def _modo_teste(config: dict | None, modo_teste: bool) -> bool:
    return bool(modo_teste or (config or {}).get("modo_teste"))


def _id_do_item(item: dict):
    sid = item.get("id", item.get("service_id"))
    try:
        return int(sid) if sid is not None else None
    except (TypeError, ValueError):
        return None


def _codigo_do_item(conn: sqlite3.Connection, item: dict, sid) -> str | None:
    """O id sintetico e a identidade; o code do item so vale sem id (fila do freshhub). Id negativo
    que nao esta no nucleo devolve None (recusa), nunca cai pro code do item (pode ser de outro pedido)."""
    if sid is not None and sid < 0:
        return pedidos.codigo_por_id_sintetico(conn, sid)
    return normalizacao.normalizar_codigo(item.get("code") or item.get("codigo"))


def materializar_servicos(itens: list[dict], vuupt, config: dict | None, conn: sqlite3.Connection | None = None,
                          *, modo_teste: bool = False, espera_s: float | None = None,
                          pulso=None) -> ResultadoMaterializacao:
    """Cria na Vuupt o servico dos itens SEM servico (id <= 0, ou sem id) e devolve
    {codigo: service_id} em `ok` e {codigo: motivo} em `falhas`; item com id real e ignorado
    (nem aparece no resultado). Ordem do lote = ordem dos itens. Commita na `conn` a cada pedido
    (a trava precisa ser vista pelos outros processos na hora). Modo teste: nada vai na Vuupt e
    cada pedido sem servico volta em `falhas` (e em `transitorios`) com o motivo. Trava ocupada alem de
    `espera_s` (padrao ESPERA_TRAVA_S): falha com o codigo tambem em `ocupados`. Excecao que nao e
    recusa de regra (classificar_falha): falha com o codigo tambem em `transitorios` (quem chama nao
    descarta o pedido). `codigos` = {id do item: codigo do nucleo}
    (quem chama casa o resultado pelo id, nao pelo codigo que tem na mao). `pulso()` (opcional) e chamado
    antes de cada pedido e de cada passo na Vuupt (heartbeat da trava de quem chama); se levantar
    EnvioInterrompido o lote para e a excecao sobe."""
    espera_s = ESPERA_TRAVA_S if espera_s is None else espera_s
    resultado = ResultadoMaterializacao()
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        vistos: set[str] = set()
        # codigo -> (status, mensagem) das recusas da Vuupt; recusa por dado local entra com status 0 e o
        # tipo do erro como mensagem (regra do erro sistemico)
        recusas_http: dict[str, tuple[int, str]] = {}
        for item in itens or []:
            sid = _id_do_item(item)
            if sid is not None and sid > 0:
                continue
            codigo = _codigo_do_item(conn, item, sid)
            if not codigo:
                resultado.falhas[str(sid)] = (f"id sintetico {sid} nao encontrado no nucleo: servico nao criado "
                                              f"(recarregue a tela)" if sid is not None and sid < 0 else
                                              f"id {sid} sem pedido no nucleo: servico nao criado")
                logger.warning(f"materializar: {resultado.falhas[str(sid)]}")
                continue
            if sid is not None:
                resultado.codigos[sid] = codigo
            if codigo in vistos:
                continue
            vistos.add(codigo)
            if pulso is not None:
                pulso()
            try:
                resultado.ok[codigo] = _materializar_um(conn, codigo, vuupt, _modo_teste(config, modo_teste), espera_s,
                                                        pulso)
                logger.info(f"materializar {codigo}: servico {resultado.ok[codigo]} na Vuupt")
            except EnvioInterrompido:
                raise
            except Exception as e:  # noqa: BLE001 - um pedido nunca derruba os outros
                tipo = classificar_falha(e)
                motivo, causa = _motivo_da_falha(e, tipo)
                if tipo == FALHA_TRANSITORIA:
                    resultado.transitorios[codigo] = causa
                    resultado.falhas[codigo] = motivo
                    if isinstance(e, _Transitorio):
                        logger.warning(f"materializar {codigo}: servico NAO criado (transitorio) -- {e}")
                    else:
                        logger.exception(f"materializar {codigo}: falha tecnica, pedido segue no envio -- {e}")
                    continue
                resultado.falhas[codigo] = motivo
                if tipo == FALHA_OCUPADO:
                    resultado.ocupados.add(codigo)
                http = _erro_http_vuupt(e)
                if tipo == FALHA_RECUSA and http:
                    recusas_http[codigo] = http
                elif isinstance(e, _RecusaDadoLocal):
                    recusas_http[codigo] = (0, e.tipo_erro)
                logger.warning(f"materializar {codigo}: servico NAO criado -- {e}")
        _aplicar_erro_sistemico(resultado, recusas_http)
    finally:
        if fechar:
            conn.close()
    return resultado


def _aplicar_erro_sistemico(resultado: ResultadoMaterializacao, recusas_http: dict[str, tuple[int, str]]) -> None:
    """Todas as falhas do lote sao recusas da Vuupt com o MESMO status (>= MINIMO_LOTE_SISTEMICO pedidos)
    e nenhum pedido deu certo: o lote inteiro vira transitorio (nada sai do rascunho/pool)."""
    # chave: o status HTTP; recusa por dado local (status 0) conta pelo tipo do erro
    chaves = {(s, m if s == 0 else None) for s, m in recusas_http.values()}
    if (resultado.ok or len(recusas_http) < MINIMO_LOTE_SISTEMICO or set(resultado.falhas) != set(recusas_http)
            or len(chaves) != 1):
        return
    status, mensagem = next(iter(recusas_http.values()))
    if status == 0:
        texto = (f"todos os pedidos falharam com o mesmo erro local ao montar o servico ({mensagem}): possivel "
                 f"problema sistemico: {mensagem}; nada foi removido, avise o Hugo")
    else:
        texto = (f"a Vuupt recusou todos os pedidos com o mesmo erro (Status {status}: {mensagem}): possivel "
                 f"problema sistemico, nada foi removido, avise o Hugo")
    for codigo in recusas_http:
        resultado.falhas[codigo] = resultado.transitorios[codigo] = texto
    logger.error(f"materializar: {len(recusas_http)} pedido(s) recusado(s) com o mesmo erro "
                 f"({'Status ' + str(status) if status else mensagem}) -- tratado como erro sistemico (nada removido): "
                 f"{', '.join(recusas_http)}")


def materializar_lote(itens: list[dict], vuupt, config: dict | None, conn: sqlite3.Connection | None = None,
                      *, modo_teste: bool = False) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    """Para quem monta a rota com uma lista de servicos (criacao direta): devolve (copia da lista so com
    quem tem servico, ja com o id real; falhas {codigo: motivo}; pendentes {codigo: motivo} = as falhas
    TECNICAS ou de trava ocupada, que nao sao recusa do pedido). Lista sem id sintetico volta igual e
    nada e chamado."""
    if not any((_id_do_item(s) or 0) < 0 for s in itens or []):
        return list(itens or []), {}, {}
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
        pendentes = {c: m for c, m in res.falhas.items() if c in res.transitorios or c in res.ocupados}
        return saida, res.falhas, pendentes
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


def _conferir_agrupamento(conn: sqlite3.Connection, codigo: str) -> None:
    """Pedido que alguem agrupou num servico da Vuupt ("PS-10" virou "PS-10, PS-20"): a busca por code
    "#PS-10" nao acha o composto e criaria outro servico (entrega em dobro)."""
    agrupado = pedidos.servicos_que_agrupam(conn).get(codigo)
    if agrupado:
        sid, composto = agrupado
        raise _Recusa(f"pedido {codigo} agrupado no servico {sid} ('{composto}') na Vuupt: nao crio outro; "
                      f"conferir o pedido a mao")


def _conferir_historico(conn: sqlite3.Connection, linha, codigo: str) -> None:
    """Linha sem id que NAO nasceu sem servico (servico renomeado/apagado na Vuupt, pulado do pipeline
    antigo) ou codigo que ja teve servico (nucleo_servicos): a busca por "#codigo" pode nao achar o
    servico de verdade e criaria um SEGUNDO. Conservador: recusa e o operador confere a mao."""
    anterior = conn.execute("SELECT vuupt_service_id, status FROM nucleo_servicos WHERE codigo = ? "
                            "ORDER BY vuupt_service_id DESC LIMIT 1", (codigo,)).fetchone()
    if anterior is not None:
        raise _Recusa(f"pedido {codigo} ja teve servico na Vuupt (servico {anterior[0]}, {anterior[1]} no "
                      f"historico): nao crio outro; conferir o pedido na Vuupt a mao")
    if not pedidos.nasceu_sem_servico(linha):
        raise _Recusa(f"pedido {codigo} sem servico na Vuupt mas nao nasceu sem servico (ja teve servico: "
                      f"renomeado/apagado na Vuupt, ou pulado pelo pipeline antigo): nao crio outro; conferir o "
                      f"pedido na Vuupt a mao")


def _materializar_um(conn: sqlite3.Connection, codigo: str, vuupt, modo_teste: bool, espera_s: float,
                     pulso=None) -> int:
    linha = _linha(conn, codigo)
    _conferir_pedido(linha, codigo)
    if linha["vuupt_service_id"] is not None:
        return _ja_tem_servico(conn, codigo, int(linha["vuupt_service_id"]))
    _conferir_agrupamento(conn, codigo)
    _conferir_historico(conn, linha, codigo)
    if modo_teste:
        raise _Transitorio("modo teste: servico nao criado na Vuupt (nada descartado)")

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
        return _criar_ou_adotar(conn, codigo, linha, vuupt, lambda: _passo(conn, codigo, processo, pulso))
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


def _renovar_trava(conn: sqlite3.Connection, codigo: str, processo: str) -> bool:
    """Heartbeat da trava do pedido (so o dono renova; commit curto, nunca atravessa HTTP).
    False = outro processo assumiu (achou a trava velha)."""
    cur = conn.execute("UPDATE nucleo_materializacao SET iniciado_em = ? WHERE codigo = ? AND processo = ?",
                       (banco.agora(), codigo, processo))
    conn.commit()
    return cur.rowcount == 1


def _passo(conn: sqlite3.Connection, codigo: str, processo: str, pulso=None) -> None:
    """Antes de cada chamada a Vuupt: pulso de quem chama + renova a trava do pedido."""
    if pulso is not None:
        pulso()
    if not _renovar_trava(conn, codigo, processo):
        raise _Ocupado(f"a trava do pedido {codigo} foi assumida por outro envio (este passou de "
                       f"{TRAVA_VELHA_MIN} min?): servico nao criado por este envio, tente de novo")


def _criar_ou_adotar(conn: sqlite3.Connection, codigo: str, linha, vuupt, passo=lambda: None) -> int:
    code = f"#{codigo}"
    passo()
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
    try:
        payload = montar_payload(linha, vuupt, base)   # so local (base pronta): sem chamada a Vuupt
    except (ValueError, TypeError) as e:
        # dado do nucleo malformado (ex. agendamento_* que nao e data) e erro LOCAL deterministico: tentar de
        # novo nao resolve, entao e recusa com o motivo (a parada sai), nunca transitorio (travaria a rota)
        raise _RecusaDadoLocal(f"pedido {codigo} com dado invalido no nucleo ({e}): servico nao criado; corrigir "
                               f"o pedido (data/endereco) e montar de novo", type(e).__name__) from e
    passo()
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
            # anomalia da Vuupt (resposta sem id): nao e recusa do pedido, a parada fica
            raise _Transitorio(f"falha tecnica: a Vuupt devolveu '{acao}' sem id do servico de {codigo} (nada "
                               f"descartado): tente de novo; se repetir, conferir o pedido na Vuupt")
        sid = int(servico["id"])
        try:
            pedidos.registrar_importacao(payload, resposta, acao, conn=conn)   # id no nucleo + rascunhos, 1 transacao
        except Exception as e:
            raise _Transitorio(f"falha tecnica: servico {sid} criado na Vuupt mas nao gravado no nucleo ({e}); "
                               f"tente de novo, o proximo envio adota o mesmo servico") from e
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
