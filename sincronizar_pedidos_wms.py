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
# (_baixar_pedidos_ja_expedidos).
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

# Rotulos de situacao (coluna 'state' da listagem, ja sem HTML) que
# significam "a mercadoria saiu do galpao" -- ver _esta_expedido. Lista
# branca de proposito: e ela que autoriza dar baixa em estoque.
ROTULOS_EXPEDIDO = frozenset({
    "sent", "shipped", "delivered",      # listagem em ingles
    "enviado", "enviada", "expedido", "expedida", "entregue",  # tela em pt-br
})

# Fim de rodada: a consulta que diz quem sumiu da Stokki pagina de
# verdade. Uma pagina so, dos mais recentes, fazia o pedido antigo cair
# fora da janela e ser lido como "nao existe mais" -- e cancelado, com a
# mercadoria ja fora do galpao (achado critico C1 da revisao, 23/09). O
# teto de paginas existe porque status="all" traz o historico inteiro do
# piloto e cresce pra sempre; estourado o teto, a lista e tratada como
# INCOMPLETA e nenhuma liberacao acontece.
STOKKI_POR_PAGINA = 200
STOKKI_MAX_PAGINAS = 10
PAUSA_ENTRE_PAGINAS = 0.4

# Teto de baixas por rodada. A primeira rodada em producao encontra o
# passivo inteiro acumulado desde que a fase 2 comecou a reservar, e cada
# baixa e um commit no dados.db compartilhado com painel e portal. O que
# nao couber nesta rodada cabe na proxima, 15 min depois.
BAIXAS_POR_RODADA = 20
PAUSA_ENTRE_BAIXAS = 0.3


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


def _esta_expedido(situacao: str) -> bool:
    """
    A Stokki diz que o pedido ja saiu do galpao. A tela em pt-br mostra
    "Enviado", a listagem em ingles devolve "Sent"; entregue/delivered
    tambem conta (se chegou ao cliente, saiu do galpao ha mais tempo
    ainda).

    Lista BRANCA de rotulos inteiros, nao "contem a palavra": esta funcao
    decide dar BAIXA em estoque, e casar por pedaco faria "Nao entregue",
    "Nao enviado" e "Reenviado" virarem saida de mercadoria. Nao ha
    evidencia de que a Stokki use rotulo negado no outbound, mas o custo
    da lista branca e nenhum e ela elimina a classe inteira de erro.

    Rotulo desconhecido nao vira baixa E nao vira cancelamento: cai no
    "reserva mantida" do _liberar_pedidos_sumidos, que loga a situacao
    literal -- e assim um rotulo novo da Stokki aparece no log em vez de
    mexer no estoque por conta propria.
    """
    s = " ".join(str(situacao or "").split()).strip(" .:-").casefold()
    return s in ROTULOS_EXPEDIDO


def _candidatos_com_reserva_ativa(conn, vistos: set) -> list:
    """
    Pedido nosso com reserva ATIVA que NAO apareceu em nenhum dos status
    varridos nesta rodada -- a materia-prima das duas varreduras do fim
    da rodada (baixar o que ja foi expedido, liberar o que sumiu).
    """
    rows = conn.execute("""
        SELECT p.id, p.id_stokki, p.codigo_ps, p.estado_reserva
          FROM wms_pedidos p
         WHERE p.estado_reserva IN ('PENDENTE', 'RESERVADO', 'PARCIAL')
           AND EXISTS (SELECT 1 FROM wms_reservas r WHERE r.pedido_id = p.id AND r.estado = 'ATIVA')
    """).fetchall()
    return [r for r in rows if r["id_stokki"] not in vistos]


def _situacoes_na_stokki(sess, piloto_id: str) -> tuple[dict, bool] | None:
    """
    Situacao de cada pedido do piloto na Stokki (listagem sem filtro de
    status), PAGINADA. Devolve (situacoes, completa):

      situacoes -- {id_stokki: rotulo da coluna 'state', sem HTML}
      completa  -- True so quando a lista foi vista ate o fim.

    None quando a consulta falha em qualquer pagina: sem a lista,
    nenhuma das varreduras conclui coisa alguma.

    Achado critico C1 da revisao (23/09): a versao anterior pedia UMA
    pagina de 200, ordenada dos mais recentes pros mais antigos, e nao
    olhava se havia mais. Um pedido com reserva ATIVA que ja rolou pra
    fora dos 200 mais recentes sumia do dicionario, e o
    _liberar_pedidos_sumidos lia isso como "nao existe mais na Stokki" e
    CANCELAVA -- devolvendo ao disponivel mercadoria que ja tinha saido
    do galpao. Com status="all" trazendo o historico inteiro, o piloto
    passa de 200 em semanas, e o passivo acumulado (o alvo desta leva) e
    justamente o mais antigo, o mais provavel de estar fora da janela.

    Fim da lista = pagina que voltou com menos linhas do que foi pedido
    (ou vazia) -- criterio que nao depende de qual chave de total a
    Stokki devolve (recordsFiltered/iTotalDisplayRecords/iTotalRecords
    variam, e a "sem filtro" contaria pedido de outro embarcador).

    O teto de paginas existe porque o historico cresce pra sempre e esta
    rotina roda a cada 15 min. Estourado o teto, `completa` volta False e
    quem chama NAO conclui "sumiu" de ninguem. A pausa entre paginas e a
    mesma de stokki_pedidos.iterar_todos_pedidos.
    """
    situacoes = {}
    completa = False
    for pagina in range(STOKKI_MAX_PAGINAS):
        try:
            resposta = stokki_pedidos.listar_pedidos(
                sess, status="all", cliente=piloto_id, pagina=pagina,
                por_pagina=STOKKI_POR_PAGINA, ordenar_coluna="1", ordenar_dir="desc")
        except Exception as e:  # noqa: BLE001 -- sem a lista, nao mexe em nada
            logger.warning("Nao deu pra conferir a situacao dos pedidos na Stokki (pagina %d: %s) -- "
                            "nenhuma baixa nem liberacao de reserva nesta rodada.", pagina + 1, e)
            return None
        linhas = resposta.get("aaData") or []
        for linha in linhas:
            id_pedido = stokki_pedidos.extrair_id_da_linha(linha)
            if id_pedido:
                situacoes[id_pedido] = re.sub(
                    r"<[^>]+>", " ", str(linha.get("state", ""))).strip()
        if len(linhas) < STOKKI_POR_PAGINA:
            completa = True
            break
        time.sleep(PAUSA_ENTRE_PAGINAS)
    if not completa:
        logger.warning("A listagem geral do piloto passou de %d pedidos (teto de %d paginas) -- "
                        "lista INCOMPLETA: nenhuma reserva sera liberada por omissao nesta rodada.",
                        STOKKI_MAX_PAGINAS * STOKKI_POR_PAGINA, STOKKI_MAX_PAGINAS)
    return situacoes, completa


def _baixar_pedidos_ja_expedidos(conn, candidatos: list, situacoes: dict) -> int:
    """
    Pedido que saiu pela Stokki SEM passar por rota da Vuupt (retirada no
    galpao, redespacho, transportadora propria) nunca chega no unico
    chamador de baixar_por_expedicao (expedir_pedidos.py, dentro do laco
    dos servicos entregues). A reserva dele ficava ATIVA pra sempre, sumia
    do disponivel e fazia pedido novo nascer PARCIAL por falta que nao
    existe. Mesmo pelo caminho da Vuupt, se a baixa estourava o
    fingerprint ja marcava o pedido como processado e nao havia
    retentativa nenhuma.

    Aqui a rodada de 15 min fecha o buraco: pedido com reserva ATIVA que
    sumiu dos status varridos E esta "Enviado"/"Sent" na Stokki leva a
    baixa. Nao e cancelamento -- a mercadoria SAIU, entao vira SAIDA de
    estoque, nunca reserva liberada (as duas coisas sao diferentes: uma
    corrige o saldo, a outra o falsificaria).

    baixar_por_expedicao e idempotente por uuid de movimento
    (ps-<id_stokki>-reserva-<id da reserva>), entao rodar de novo nao
    baixa em dobro -- e por isso tambem que uma baixa que falhou hoje
    pode ser completada pela rodada de amanha.

    A baixa so depende de evidencia POSITIVA ("a Stokki diz que este
    pedido saiu"), entao ela roda mesmo quando a listagem veio
    incompleta: quem nao apareceu simplesmente nao e baixado. Concluir
    "sumiu" e que exige ter visto a lista inteira, e isso e problema do
    _liberar_pedidos_sumidos.

    Teto por rodada + pausa entre pedidos (I2 da revisao): a primeira
    rodada em producao encontra o passivo inteiro de uma vez e cada baixa
    faz commit no dados.db compartilhado com painel e portal. O que
    sobrar entra na proxima rodada, 15 min depois.
    """
    baixados = 0
    for c in candidatos:
        situacao = situacoes.get(c["id_stokki"])
        if situacao is None or not _esta_expedido(situacao):
            continue
        if baixados >= BAIXAS_POR_RODADA:
            logger.warning("Teto de %d baixas por rodada atingido -- o resto do passivo entra na "
                            "proxima rodada (15 min).", BAIXAS_POR_RODADA)
            break
        # baixar_por_expedicao faz o proprio commit (por reserva).
        r = wms_pedidos.baixar_por_expedicao(conn, c["codigo_ps"])
        for erro in r["erros"]:
            logger.warning("%s: baixa da expedicao falhou -- %s", c["codigo_ps"], erro)
        for neg in r.get("negativos") or []:
            logger.warning("%s: saldo negativo na baixa -- %s", c["codigo_ps"], neg)
        if r["baixas"]:
            baixados += 1
            logger.warning("%s: expedido na Stokki (%s) sem passar por rota -- %d reserva(s) baixada(s).",
                           c["codigo_ps"], situacao, r["baixas"])
            time.sleep(PAUSA_ENTRE_BAIXAS)
    return baixados


def _liberar_pedidos_sumidos(conn, candidatos: list, situacoes: dict) -> int:
    """
    Spec 7.3 item 6: pedido cancelado ou sumido da Stokki tem as reservas
    liberadas. Sem isso a reserva ATIVA trava o disponivel pra sempre e o
    galpao "nao tem" mercadoria que esta la na prateleira.

    A rotina nao cancela por omissao: so libera quem
      - nao existe mais na Stokki (sumiu de vez), ou
      - esta com situacao de CANCELADO la.
    Pedido que saiu dos status varridos por outro motivo mantem a reserva
    -- ela ainda vai virar a SAIDA da baixa -- e so gera aviso no log.
    Cancelar ali baixaria estoque nenhum e deixaria o saldo mentindo pra
    sempre. Quem estava expedido ja foi baixado antes desta varredura
    (_baixar_pedidos_ja_expedidos) e nem chega aqui como candidato; se
    chegar, e porque a baixa nao saiu inteira -- e ai a reserva que sobrou
    tem que continuar ATIVA pra proxima rodada completar.
    """
    liberados = 0
    for c in candidatos:
        situacao = situacoes.get(c["id_stokki"])
        if situacao is None:
            motivo = "pedido nao existe mais na Stokki"  # so chega aqui com a lista COMPLETA
        elif "CANCEL" in situacao.upper():
            motivo = f"pedido cancelado na Stokki ({situacao})"
        else:
            logger.info("%s saiu dos status varridos (Stokki: %s) com reserva ATIVA -- "
                        "reserva mantida pra baixa da expedicao.", c["codigo_ps"], situacao or "?")
            continue
        n = wms_pedidos.cancelar_reservas(conn, c["id"], motivo)
        conn.commit()
        liberados += 1
        logger.warning("%s: %d reserva(s) liberada(s) -- %s", c["codigo_ps"], n, motivo)
    return liberados


def _ensaiar_varreduras(candidatos: list, situacoes: dict, completa: bool) -> dict:
    """
    --modo-teste: percorre os mesmos candidatos e DIZ no log o que faria,
    sem gravar nada (I1 da revisao). O modo teste e a mitigacao
    recomendada antes da primeira rodada de verdade, quando o passivo
    inteiro sera baixado de uma vez -- um modo teste que nao imprime nada
    nao mitiga coisa nenhuma.
    """
    ensaio = {"baixaria": 0, "liberaria": 0, "manteria": 0}
    for c in candidatos:
        situacao = situacoes.get(c["id_stokki"])
        if situacao is not None and _esta_expedido(situacao):
            ensaio["baixaria"] += 1
            logger.info("[teste] %s: expedido na Stokki (%s) -- BAIXARIA as reservas ATIVAS.",
                        c["codigo_ps"], situacao)
        elif situacao is None and not completa:
            ensaio["manteria"] += 1
            logger.info("[teste] %s: nao apareceu numa listagem INCOMPLETA -- nada a concluir.",
                        c["codigo_ps"])
        elif situacao is None:
            ensaio["liberaria"] += 1
            logger.info("[teste] %s: nao existe mais na Stokki -- LIBERARIA as reservas.", c["codigo_ps"])
        elif "CANCEL" in situacao.upper():
            ensaio["liberaria"] += 1
            logger.info("[teste] %s: cancelado na Stokki (%s) -- LIBERARIA as reservas.",
                        c["codigo_ps"], situacao)
        else:
            ensaio["manteria"] += 1
            logger.info("[teste] %s: Stokki diz '%s' -- reserva mantida.", c["codigo_ps"], situacao)
    if ensaio["baixaria"] > BAIXAS_POR_RODADA:
        logger.info("[teste] %d pedidos a baixar, teto de %d por rodada -- seriam necessarias %d rodadas.",
                    ensaio["baixaria"], BAIXAS_POR_RODADA,
                    -(-ensaio["baixaria"] // BAIXAS_POR_RODADA))
    return ensaio


def rodar(conn, sess, piloto_nome: str, piloto_id: str, limite: int, modo_teste: bool) -> dict:
    res = {"lidos": 0, "do_piloto": 0, "ignorados_outro_embarcador": 0,
           "reservados": 0, "parciais": 0, "pendencias": 0, "baixados": 0,
           "liberados": 0, "erros": 0}
    vistos = set()
    pagina_cheia = False
    for status in STATUS_INTERESSANTES:
        # Filtro do lado do servidor (correcao 1): a Stokki ja devolve so
        # os pedidos do piloto quando cliente=piloto_id.
        resposta = stokki_pedidos.listar_pedidos(
            sess, status=status, cliente=piloto_id, por_pagina=limite,
            ordenar_coluna="1", ordenar_dir="desc")
        linhas = resposta.get("aaData") or []
        # A pagina encheu: pode haver pedido do piloto neste status que
        # nao foi lido -- ele nao pode ser tratado como "sumido" depois.
        if len(linhas) >= limite:
            pagina_cheia = True
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

    # Depois de varrer tudo: quem tem reserva ATIVA e nao apareceu em
    # nenhum status ou ja foi expedido por fora da rota (baixa que nunca
    # veio) ou foi cancelado/sumiu (spec 7.3 item 6).
    if pagina_cheia:
        logger.warning("Alguma listagem encheu a pagina (limite %d) -- pulando as varreduras de fim de "
                        "rodada nesta volta, pra nao concluir nada sobre quem so nao foi lido.", limite)
        return res
    candidatos = _candidatos_com_reserva_ativa(conn, vistos)
    if not candidatos:
        return res
    # Uma consulta a Stokki so (paginada), e so quando ha candidato -- no
    # dia a dia, nenhuma requisicao a mais.
    resultado = _situacoes_na_stokki(sess, piloto_id)
    if resultado is None:
        return res
    situacoes, completa = resultado

    if modo_teste:
        # Nao grava nada, mas DIZ o que faria (I1 da revisao).
        res["ensaio"] = _ensaiar_varreduras(candidatos, situacoes, completa)
        return res

    # A baixa vem PRIMEIRO: quem ja foi expedido sai da lista de
    # candidatos (nao sobra reserva ATIVA) e nem e oferecido a varredura
    # de sumidos, que cancelaria -- e cancelar mercadoria que ja saiu do
    # galpao deixaria o saldo mentindo pra sempre. Ela roda mesmo com
    # lista incompleta: depende de evidencia positiva ("a Stokki diz que
    # saiu"), nunca de omissao.
    res["baixados"] = _baixar_pedidos_ja_expedidos(conn, candidatos, situacoes)
    if not completa:
        # Sem ter visto a lista inteira, "nao apareceu" nao prova nada --
        # e o pedido antigo, justamente o que cai fora da janela, e o que
        # mais tem reserva presa (C1 da revisao).
        return res
    res["liberados"] = _liberar_pedidos_sumidos(
        conn, _candidatos_com_reserva_ativa(conn, vistos), situacoes)
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
