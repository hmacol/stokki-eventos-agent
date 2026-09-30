# -*- coding: utf-8 -*-
"""
sincronizar_pedidos_wms.py

Le os pedidos do embarcador piloto na Stokki, espelha itens em
wms_pedido_itens e reserva o estoque (FEFO) pra cada um.

Reserva nao mexe em saldo fisico: o disponivel e derivado. Falta de saldo
nao bloqueia nada -- vira pendencia (decisao do Hugo, 21/09).

Correcoes ao desenho original (Hugo, 22/09, depois de sondar a Stokki de
producao ao vivo):
  1. Filtro do lado do servidor -- listar_pedidos(cliente=piloto_id) ja
     devolve so os pedidos do piloto (provado: cliente='48' trouxe 32
     "Waiting for Carrier" + 5 "Separating", so Maria Dolores). Buscar o
     detalhe dos ~270 pedidos de TODOS os embarcadores so pra descobrir de
     quem era e descartar, a cada 15 min, contra um sistema de terceiros e
     de sessao unica, e inaceitavel.
  2. Rede de seguranca -- mesmo com o filtro do servidor, confere o
     #stkkc-<id> que vem na propria linha antes de reservar qualquer coisa.
     Se uma versao futura da Stokki ignorar o parametro 'client', a rotina
     nao pode sair reservando estoque de outro embarcador. O nome do
     cliente na listagem vem TRUNCADO ("PADRAO PURO LTD ... #stkkc-23") --
     nunca filtrar por nome, so pelo id estavel.
  3. Uma busca so da pagina do pedido -- o desenho original buscava
     obter_detalhe(id) (que ja baixa a pagina) e DEPOIS buscava a mesma
     pagina de novo pra extrair os itens. Um sess.get() so; o HTML serve
     tanto pra extrair_itens_do_pedido quanto, se precisar, pra
     stokki_pedidos._parsear_pagina_detalhe.

Correcao 2 (revisao, 22/09): a rotina agora segura a trava cooperativa de
stokki/sessao_uso.py em volta da rodada inteira -- a sessao Stokki e
UNICA, e o login de outro processo no meio (stokki-wms-sincronizar-
produtos, agente-importacao-stokki, o painel) derruba os cookies com 401.
Vale tambem pro --modo-teste, que so pula a escrita mas ainda le da Stokki
de verdade na mesma sessao. Se a trava nao vier, a rotina desiste (nao
atropela quem esta usando) -- a proxima rodada e em 15 min e a reserva e
idempotente, entao nada se perde.

COMO USAR (timer systemd a cada 15 min, infra/stokki-wms-reservar-pedidos.*):
    python sincronizar_pedidos_wms.py
    python sincronizar_pedidos_wms.py --modo-teste      # nao grava nada
    python sincronizar_pedidos_wms.py --limite 20
"""
import argparse
import logging
import re
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

import wms_pedidos
from stokki import pedidos as stokki_pedidos
from stokki import sessao_uso
from stokki.auth import StokkiSession

logger = logging.getLogger("sincronizar_pedidos_wms")

# Estados de pedido que interessam pra reserva -- antes da expedicao. Um
# pedido que ja saiu (Sent/Delivered) nao precisa mais de reserva, so de
# baixa -- feita pelo expedir_pedidos.py quando o pedido passa por rota da
# Vuupt e, pra quem sai por fora dela, pela varredura do fim desta rodada
# (_varrer_candidatos).
STATUS_INTERESSANTES = ("Waiting for Carrier", "Separating", "Pack")

# Dono da trava cooperativa da sessao Stokki (stokki/sessao_uso.py). TTL
# folgado pro tamanho real de uma rodada (~37 pedidos hoje, so requests,
# sem Playwright -- minutos, nao dezenas de minutos). Espera curta de
# proposito: o timer se repete a cada 15 min, entao nao faz sentido
# esperar mais que isso -- so perderia a proxima janela tambem.
DONO_TRAVA = "wms-reservar-pedidos"
TRAVA_TTL_SEGUNDOS = 600
TRAVA_ESPERA_SEGUNDOS = 120

_PADRAO_STKKC_ID = re.compile(r"#stkkc-(\d+)")

# Rotulos de situacao lidos da pagina do pedido que significam "a
# mercadoria saiu do galpao" -- ver _esta_expedido. Lista branca de
# proposito: e ela que autoriza dar baixa em estoque.
ROTULOS_EXPEDIDO = frozenset({
    "sent", "shipped", "delivered",      # rotulo em ingles
    "enviado", "enviada", "expedido", "expedida", "entregue",  # tela em pt-br
})

# Rotulos que significam "este pedido nao vai mais sair" e liberam a
# reserva (spec 7.3 item 6). Lista branca de rotulos INTEIROS pelo mesmo
# motivo da de cima, e aqui pesa ainda mais: casar por pedaco fazia
# "Cancelamento solicitado" liberar a reserva e marcar o pedido CANCELADO
# pra sempre (reservar_pedido pula CANCELADO) -- se ele embarcasse depois,
# sairia sem SAIDA e viraria mercadoria fantasma na prateleira.
#
# ATENCAO -- esta e a UNICA via automatica de liberacao que sobrou. Pedido
# que SUMIU da Stokki nao e mais detectado pela rotina (ver
# _situacao_do_pedido): quem resolve esse caso e o botao "Liberar reserva"
# na tela de reservas antigas (/wms/estoque), na mao.
ROTULOS_CANCELADO = frozenset({
    "canceled", "cancelled",             # rotulo em ingles
    "cancelado", "cancelada",            # tela em pt-br
})

# Teto de candidatos verificados por rodada. Cada candidato custa um GET
# na Stokki (sessao unica, sistema de terceiros) e, se for o caso, uma
# baixa com commit no dados.db compartilhado com painel e portal. No dia
# a dia a lista de candidatos e vazia; o teto existe pro passivo da
# primeira rodada. O que nao couber cabe na proxima, 15 min depois.
# Como so candidato verificado pode virar baixa, este teto tambem limita
# quantas baixas uma rodada faz.
CANDIDATOS_POR_RODADA = 20
PAUSA_ENTRE_CONSULTAS = 0.3

# Tamanho da janela de rotacao: a fila de candidatos comeca num ponto
# diferente a cada rodada (ver _varrer_candidatos). 900s = os 15 min do
# timer, entao cada rodada anda uma janela inteira pra frente.
SEGUNDOS_POR_RODADA = 900

# O status e o PRIMEIRO badge-status depois de "Situacao:" na pagina do
# pedido (os seguintes sao marcadores tipo "Remessa Expressa"). Mesmo
# regex de retiradas/acompanhar_retiradas.py e de
# painel_agentes/pedidos_parados_triagem.py.
_RE_SITUACAO_STOKKI = re.compile(
    r"Situa[çc][ãa]o:\s*</th>\s*<td>\s*<span[^>]*badge-status[^>]*>(.*?)</span>", re.S | re.IGNORECASE,
)


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _piloto_id(config: dict) -> str:
    """Id numerico do embarcador piloto na Stokki (ex.: '48' = Maria
    Dolores) -- e o valor que vai no filtro client= do listar_pedidos e na
    rede de seguranca do #stkkc-<id> de cada linha."""
    return str((config.get("wms", {}) or {}).get("embarcador_piloto_id", "48")).strip()


def _piloto_nome(config: dict) -> str:
    """So pra log e pra preencher wms_pedidos.embarcador quando o detalhe
    do pedido nao tiver o nome completo -- NUNCA usado pra decidir se um
    pedido e do piloto (o nome vem truncado na listagem)."""
    return (config.get("wms", {}) or {}).get("embarcador_piloto", "MARIA DOLORES").strip().upper()


def _stkkc_id_da_linha(linha) -> str | None:
    """
    Tira o id estavel do embarcador (#stkkc-NN) do campo 'client' da
    linha do aaData. E o mesmo padrao ja usado em notificar_pedidos_em_espera.py
    e documentos_pedido/selecionar_pedidos.py -- o nome vem truncado
    ("PADRAO PURO LTD ... #stkkc-23"), o id nao.
    """
    client_html = str(linha.get("client", "") if isinstance(linha, dict) else "")
    m = _PADRAO_STKKC_ID.search(client_html)
    return m.group(1) if m else None


def _normalizar_rotulo(situacao) -> str:
    """Rotulo do badge pronto pra comparar com as listas brancas: sem
    espaco sobrando, sem pontuacao nas pontas, caixa unificada."""
    return " ".join(str(situacao or "").split()).strip(" .:-").casefold()


def _esta_expedido(situacao: str) -> bool:
    """
    A Stokki diz que o pedido ja saiu do galpao. A tela em pt-br mostra
    "Enviado"; entregue/delivered tambem conta (se chegou ao cliente, saiu
    do galpao ha mais tempo ainda).

    Lista BRANCA de rotulos inteiros, nao "contem a palavra": esta funcao
    decide dar BAIXA em estoque, e casar por pedaco faria "Nao entregue",
    "Nao enviado" e "Reenviado" virarem saida de mercadoria. Nao ha
    evidencia de que a Stokki use rotulo negado no outbound, mas o custo
    da lista branca e nenhum e ela elimina a classe inteira de erro.

    Rotulo desconhecido nao vira baixa E nao vira liberacao: cai no
    "reserva mantida" do _varrer_candidatos, que loga a situacao literal
    -- e assim um rotulo novo da Stokki aparece no log em vez de mexer no
    estoque por conta propria.
    """
    return _normalizar_rotulo(situacao) in ROTULOS_EXPEDIDO


def _esta_cancelado(situacao: str) -> bool:
    """
    O pedido foi cancelado na Stokki e nao vai mais sair: a reserva e
    liberada (spec 7.3 item 6).

    Lista BRANCA de rotulos inteiros pelo mesmo motivo do _esta_expedido,
    e aqui o estrago do casamento por pedaco e pior: "Cancelamento
    solicitado" liberaria a reserva e marcaria o pedido CANCELADO pra
    sempre (reservar_pedido pula CANCELADO), e se ele embarcasse depois
    sairia sem SAIDA -- mercadoria fantasma na prateleira, em silencio.
    """
    return _normalizar_rotulo(situacao) in ROTULOS_CANCELADO


def _candidatos_com_reserva_ativa(conn, vistos: set) -> list:
    """
    Pedido nosso com reserva ATIVA que NAO apareceu em nenhum dos status
    varridos nesta rodada -- a materia-prima da varredura de fim de
    rodada.

    ORDER BY explicito porque a fila e rotacionada por rodada (ver
    _varrer_candidatos): sem ordem estavel, rotacionar nao garante
    cobertura nenhuma.
    """
    rows = conn.execute("""
        SELECT p.id, p.id_stokki, p.codigo_ps, p.estado_reserva
          FROM wms_pedidos p
         WHERE p.estado_reserva IN ('PENDENTE', 'RESERVADO', 'PARCIAL')
           AND EXISTS (SELECT 1 FROM wms_reservas r WHERE r.pedido_id = p.id AND r.estado = 'ATIVA')
         ORDER BY p.id
    """).fetchall()
    return [r for r in rows if r["id_stokki"] not in vistos]


def _rodada_atual() -> int:
    """
    Numero da rodada (uma a cada 15 min). So serve pra rotacionar a fila
    de candidatos -- e relogio, nao estado gravado, porque a alternativa
    seria coluna nova em wms_pedidos e a fila e efemera de qualquer jeito.
    """
    return int(time.time() // SEGUNDOS_POR_RODADA)


def _situacao_do_pedido(sess, id_stokki: int) -> str | None:
    """
    Situacao do pedido lida da PAGINA DELE, um GET por pedido. Devolve:

      <texto> -- o rotulo do badge ("Enviado", "Cancelado", "Aguardando
                 Transportador"...).
      None    -- NAO DA PRA CONCLUIR NADA. Cai aqui qualquer resposta que
                 nao seja uma pagina legivel: erro de rede, 401 de sessao
                 derrubada, 404, 500, ou HTML sem o bloco de situacao.
                 Quem chama nao mexe em nada.

    PEDIDO QUE SUMIU DA STOKKI NAO E MAIS DETECTADO POR ESTA ROTINA -- e
    decisao do Hugo (23/09, rodada 3), nao esquecimento. Medido em
    producao: pedido inexistente devolve 500, nao 404. O 500 tambem e o
    que a Stokki devolve quando ela mesma esta quebrada, entao trata-lo
    como sumico faria uma janela de instabilidade cancelar reserva de
    pedido que ja saiu do galpao. E o 404, que sobrava como unica
    evidencia de sumico, na pratica so pode acontecer se a ROTA
    /pt-br/administrator/inventory/outbound/show/<id> parar de resolver
    (mudanca de path na Stokki, proxy, WAF) -- e ai TODOS os candidatos
    dariam 404 e a rotina cancelaria em massa, 20 por rodada, 4 rodadas
    por hora. Um ramo que so pode abrir pelo motivo errado e pior que
    ramo nenhum. Sumico agora se resolve na mao, pelo botao "Liberar
    reserva" da tela de reservas antigas (/wms/estoque), que existe
    exatamente pra isso. Pedido CANCELADO na Stokki (o caso comum da
    spec 7.3 item 6) continua liberado automaticamente, pelo rotulo.

    Por que pedido a pedido e nao por listagem (achado critico da rodada
    2, medido na Stokki de producao em 23/09):

      status="all"       pedi 200 -> voltaram   0   (iTotalDisplayRecords=0)
      status="Sent"      pedi 200 -> voltaram 200   (iTotalDisplayRecords=3465)

    O "all" que a rotina usava devolve LISTA VAZIA. Com zero linhas o
    criterio de fim de lista concluia "lista completa", todo candidato
    ficava sem situacao e era lido como "sumiu" -- CANCELADO em vez de
    baixado, toda rodada, desde a primeira. E varrer o "Sent" no lugar
    seria pior: 3.465 pedidos so desse cliente, a cada 15 min, contra um
    sistema de sessao unica.

    Aqui nao ha listagem nenhuma: a evidencia e direta e vale so pra
    aquele pedido. Os candidatos sao poucos (so pedido com reserva ATIVA
    fora dos status varridos; no dia a dia, nenhum).
    """
    try:
        resp = sess.get(
            f"{stokki_pedidos.BASE_URL}/pt-br/administrator/inventory/outbound/show/{id_stokki}")
        resp.raise_for_status()
        m = _RE_SITUACAO_STOKKI.search(resp.text)
    except Exception as e:  # noqa: BLE001 -- um pedido ruim nao derruba a rodada
        logger.warning("Nao deu pra ler a situacao do pedido %s na Stokki (%s) -- "
                        "nada concluido sobre ele nesta rodada.", id_stokki, e)
        return None
    if not m:
        logger.warning("Pagina do pedido %s sem o bloco de situacao -- nada concluido sobre ele.",
                        id_stokki)
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()


def _varrer_candidatos(conn, sess, candidatos: list, modo_teste: bool = False) -> dict:
    """
    Fecha a rodada olhando, um por um, os pedidos que ficaram com reserva
    ATIVA fora dos status varridos. Para cada um, a pagina dele na Stokki
    decide:

      expedido (lista branca)  -> BAIXA. A mercadoria saiu, entao vira
                                  SAIDA de estoque. Nunca cancelamento --
                                  cancelar devolveria ao disponivel algo
                                  que ja foi embora.
      cancelado (lista branca) -> LIBERA (spec 7.3 item 6). E a UNICA
                                  liberacao automatica que existe.
      qualquer outro rotulo    -> so loga. A reserva fica de pe pra
                                  proxima rodada; rotulo novo da Stokki
                                  aparece no log em vez de mexer no
                                  estoque por conta propria.
      nao deu pra ler (inclui
      404 e 500)               -> so loga. Ausencia de resposta nunca e
                                  evidencia de nada; PEDIDO SUMIDO NAO E
                                  DETECTADO AQUI (ver _situacao_do_pedido),
                                  quem resolve e o botao "Liberar reserva"
                                  da tela /wms/estoque.

    Teto de candidatos por rodada + pausa entre as consultas: cada
    candidato e um GET numa Stokki de sessao unica e, quando baixa, um
    commit no dados.db compartilhado.

    A fila e ROTACIONADA por rodada. Sem isso a ordem seria a mesma toda
    vez e um candidato permanentemente inconclusivo (pagina que nao
    responde, rotulo fora das listas brancas, pedido parado num status
    nao varrido) ficaria pra sempre na cabeca, escondendo quem esta
    atras dele enquanto o teto nao alcanca todo mundo.

    modo_teste percorre e RELATA o que faria, sem gravar nada -- e a
    mitigacao recomendada antes da primeira rodada de verdade, quando o
    passivo acumulado inteiro sera baixado.
    """
    res = {"baixados": 0, "liberados": 0, "consultados": 0,
           "ensaio": {"baixaria": 0, "liberaria": 0, "manteria": 0}}
    if len(candidatos) > CANDIDATOS_POR_RODADA:
        # Comeca num ponto diferente a cada rodada, andando uma janela
        # inteira por vez: em ceil(n / teto) rodadas todo mundo e visto.
        inicio = (_rodada_atual() * CANDIDATOS_POR_RODADA) % len(candidatos)
        candidatos = candidatos[inicio:] + candidatos[:inicio]
    for c in candidatos:
        if res["consultados"] >= CANDIDATOS_POR_RODADA:
            nao_alcancados = [x["codigo_ps"] for x in candidatos[res["consultados"]:]]
            logger.warning(
                "Teto de %d candidatos por rodada atingido -- %d candidato(s) sem verificacao "
                "nesta rodada: %s%s. A fila roda: a proxima rodada comeca em outro ponto.",
                CANDIDATOS_POR_RODADA, len(nao_alcancados), ", ".join(nao_alcancados[:20]),
                ", ..." if len(nao_alcancados) > 20 else "")
            break
        if res["consultados"]:
            time.sleep(PAUSA_ENTRE_CONSULTAS)
        situacao = _situacao_do_pedido(sess, c["id_stokki"])
        res["consultados"] += 1
        codigo = c["codigo_ps"]

        if situacao is None:
            res["ensaio"]["manteria"] += 1
            continue  # o proprio _situacao_do_pedido ja logou o porque

        if _esta_expedido(situacao):
            if modo_teste:
                res["ensaio"]["baixaria"] += 1
                logger.info("[teste] %s: expedido na Stokki (%s) -- BAIXARIA as reservas ATIVAS.",
                            codigo, situacao)
                continue
            # baixar_por_expedicao faz o proprio commit (por reserva).
            r = wms_pedidos.baixar_por_expedicao(conn, codigo)
            for erro in r["erros"]:
                logger.warning("%s: baixa da expedicao falhou -- %s", codigo, erro)
            for neg in r.get("negativos") or []:
                logger.warning("%s: saldo negativo na baixa -- %s", codigo, neg)
            if r["baixas"]:
                res["baixados"] += 1
                logger.warning("%s: expedido na Stokki (%s) sem passar por rota -- "
                                "%d reserva(s) baixada(s).", codigo, situacao, r["baixas"])
            continue

        if not _esta_cancelado(situacao):
            res["ensaio"]["manteria"] += 1
            logger.info("%s saiu dos status varridos (Stokki: %s) com reserva ATIVA -- "
                        "reserva mantida.", codigo, situacao)
            continue
        motivo = f"pedido cancelado na Stokki ({situacao})"

        if modo_teste:
            res["ensaio"]["liberaria"] += 1
            logger.info("[teste] %s: %s -- LIBERARIA as reservas.", codigo, motivo)
            continue
        n = wms_pedidos.cancelar_reservas(conn, c["id"], motivo)
        conn.commit()
        res["liberados"] += 1
        logger.warning("%s: %d reserva(s) liberada(s) -- %s", codigo, n, motivo)
    return res


def rodar(conn, sess, piloto_nome: str, piloto_id: str, limite: int, modo_teste: bool) -> dict:
    res = {"lidos": 0, "do_piloto": 0, "ignorados_outro_embarcador": 0,
           "reservados": 0, "parciais": 0, "pendencias": 0, "baixados": 0,
           "liberados": 0, "erros": 0}
    vistos = set()
    for status in STATUS_INTERESSANTES:
        # Filtro do lado do servidor (correcao 1): a Stokki ja devolve so
        # os pedidos do piloto quando cliente=piloto_id.
        resposta = stokki_pedidos.listar_pedidos(
            sess, status=status, cliente=piloto_id, por_pagina=limite,
            ordenar_coluna="1", ordenar_dir="desc")
        linhas = resposta.get("aaData") or []
        # Pagina cheia aqui quer dizer que pode ter sobrado pedido do
        # piloto sem ler NESTE status -- ele so entra na proxima rodada.
        # Ate a rodada 2 isso desligava a varredura de fim de rodada
        # inteira, porque ela concluia por omissao numa listagem. Hoje ela
        # confere a pagina de cada candidato, entao completude de listagem
        # nao importa mais: pedido nao lido vira candidato, e a pagina
        # dele diz que ainda esta em status normal -- so log. Desligar a
        # varredura aqui desligava tambem a BAIXA, justamente nos dias de
        # mais movimento (o .service roda sem --limite, default 50, e o
        # piloto ja bate 30 num status so).
        if len(linhas) >= limite:
            logger.info("Listagem de '%s' encheu a pagina (limite %d) -- o que sobrou entra na "
                        "proxima rodada.", status, limite)
        for linha in linhas:
            res["lidos"] += 1
            id_pedido = stokki_pedidos.extrair_id_da_linha(linha)
            if not id_pedido or id_pedido in vistos:
                continue
            vistos.add(id_pedido)

            # Rede de seguranca (correcao 2): confere o #stkkc-<id> da
            # propria linha mesmo com o filtro do servidor -- se uma versao
            # futura da Stokki ignorar o parametro client, isto barra antes
            # de reservar estoque de outro embarcador.
            stkkc_id = _stkkc_id_da_linha(linha)
            if stkkc_id is None or stkkc_id != piloto_id:
                logger.warning(
                    "Pedido %s ignorado: #stkkc-%s nao bate com o piloto (%s) -- "
                    "veio no filtro client=%s mesmo assim", id_pedido, stkkc_id, piloto_id, piloto_id)
                res["ignorados_outro_embarcador"] += 1
                continue
            res["do_piloto"] += 1
            codigo_ps = stokki_pedidos.extrair_codigo_ps_da_linha(linha)

            if modo_teste:
                logger.info("[teste] %s (%s) seria reservado", codigo_ps, piloto_nome)
                continue

            # A leitura HTTP fica FORA da transacao; commit por pedido, porque
            # o dados.db e compartilhado com o painel e todos os agentes.
            # Uma busca so da pagina (correcao 3) -- serve pros itens e,
            # se precisar, pro detalhe estruturado via _parsear_pagina_detalhe.
            try:
                resp = sess.get(f"{stokki_pedidos.BASE_URL}/pt-br/administrator/inventory/outbound/show/{id_pedido}")
                resp.raise_for_status()
                html = resp.text
            except Exception as e:  # noqa: BLE001 -- um pedido ruim nao derruba a rodada
                logger.warning("Leitura do pedido %s falhou: %s", id_pedido, e)
                res["erros"] += 1
                continue

            itens = stokki_pedidos.extrair_itens_do_pedido(html)
            if not itens:
                logger.warning("Pedido %s sem itens legiveis -- pulando.", id_pedido)
                res["erros"] += 1
                continue

            detalhe = stokki_pedidos._parsear_pagina_detalhe(html, id_pedido)
            embarcador = ((detalhe.get("cliente") or {}).get("nome") or piloto_nome).strip().upper()

            try:
                pedido_id = wms_pedidos.registrar_pedido(conn, {
                    "id_stokki": id_pedido,
                    "codigo_ps": codigo_ps,
                    "embarcador": embarcador,
                    "situacao": status,
                }, itens)
                r = wms_pedidos.reservar_pedido(conn, pedido_id)
                conn.commit()
            except Exception as e:  # noqa: BLE001 -- um pedido ruim nao derruba a rodada
                conn.rollback()
                logger.warning("Reserva do pedido %s falhou: %s", id_pedido, e)
                res["erros"] += 1
                continue
            res["reservados" if r["estado"] == "RESERVADO" else "parciais"] += 1
            res["pendencias"] += len(r["pendencias"])
            for p in r["pendencias"]:
                logger.info("  %s: %s", codigo_ps, p)
            time.sleep(0.3)

    # Depois de varrer tudo: quem ficou com reserva ATIVA fora dos status
    # varridos ou ja foi expedido por fora da rota (baixa que nunca veio)
    # ou foi cancelado (spec 7.3 item 6). Cada um e conferido na PROPRIA
    # pagina dele -- ver _situacao_do_pedido.
    candidatos = _candidatos_com_reserva_ativa(conn, vistos)
    if not candidatos:
        return res  # o caso normal: nenhuma requisicao a mais
    varredura = _varrer_candidatos(conn, sess, candidatos, modo_teste)
    res["baixados"] = varredura["baixados"]
    res["liberados"] = varredura["liberados"]
    if modo_teste:
        res["ensaio"] = varredura["ensaio"]
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modo-teste", action="store_true", help="nao grava nada, so mostra o que faria")
    ap.add_argument("--limite", type=int, default=50, help="pedidos por status nesta rodada")
    ap.add_argument("--embarcador", help="sobrescreve o nome do embarcador piloto do config.yaml (so log)")
    ap.add_argument("--embarcador-id", help="sobrescreve o id (stkkc) do embarcador piloto do config.yaml")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "wms_reservar_pedidos.log", encoding="utf-8")])
    config = _carregar_config()
    piloto_id = (args.embarcador_id or _piloto_id(config)).strip()
    piloto_nome = (args.embarcador or _piloto_nome(config)).strip().upper()

    # Trava cooperativa da sessao Stokki (sessao unica -- ver stokki/sessao_uso.py
    # e o comentario "Correcao 2" no topo do arquivo). Adquire ANTES de abrir a
    # conexao com o banco ou a StokkiSession: se a trava nao vier, a rotina
    # desiste sem tocar em nada. Vale tambem pro --modo-teste (ele le da
    # Stokki de verdade, so pula a escrita).
    if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=TRAVA_TTL_SEGUNDOS,
                                esperar_segundos=TRAVA_ESPERA_SEGUNDOS):
        # Sai com 0: desistir por trava ocupada e operacao NORMAL, nao
        # falha. O .service tem OnFailure=stokki-alerta-falha@%n -- sair
        # com 1 aqui alertava a cada rodada em que outro processo estava
        # usando a Stokki. Mesmo precedente de notificar_transportadoras.py
        # e roteirizacao/documentacao_rota.py: loga e segue. A proxima
        # rodada e em 15 min e a reserva e idempotente, nada se perde.
        ocupante = sessao_uso.em_uso()
        logger.warning("Stokki ocupada por '%s' -- desistindo desta rodada (proxima em 15 min).", ocupante)
        return 0

    t0 = time.time()
    try:
        conn = wms_pedidos.conectar()
        sess = StokkiSession(config)
        try:
            res = rodar(conn, sess, piloto_nome, piloto_id, args.limite, args.modo_teste)
        finally:
            conn.close()
    finally:
        sessao_uso.liberar(DONO_TRAVA)
    logger.info("Piloto %s (#stkkc-%s): %s (%.0fs)", piloto_nome, piloto_id, res, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
