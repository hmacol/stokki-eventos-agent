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
# baixa (isso ja e feito em outro lugar, na deteccao de expedicao).
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


def _liberar_pedidos_sumidos(conn, sess, piloto_id: str, vistos: set) -> int:
    """
    Spec 7.3 item 6: pedido cancelado ou sumido da Stokki tem as reservas
    liberadas. Sem isso a reserva ATIVA trava o disponivel pra sempre e o
    galpao "nao tem" mercadoria que esta la na prateleira.

    Quem entra na conta: pedido nosso com reserva ATIVA que NAO apareceu
    em nenhum dos status varridos nesta rodada. Como sair dos status
    varridos tambem acontece quando o pedido e EXPEDIDO (e a baixa,
    chamada pelo expedir_pedidos.py, pode vir horas depois no caso
    "ja_expedido"), a rotina nao cancela por omissao: ela consulta a
    Stokki uma vez (listagem sem filtro de status, so do piloto) e so
    libera quem:
      - nao existe mais na Stokki (sumiu de vez), ou
      - esta com situacao de CANCELADO la.
    Pedido que saiu dos status varridos por outro motivo (expedido,
    entregue) mantem a reserva -- ela ainda vai virar a SAIDA da baixa --
    e so gera aviso no log. Cancelar ali baixaria estoque nenhum e
    deixaria o saldo mentindo pra sempre.

    A consulta extra so acontece quando ha candidato -- no dia a dia,
    nenhuma requisicao a mais.
    """
    candidatos = conn.execute("""
        SELECT p.id, p.id_stokki, p.codigo_ps, p.estado_reserva
          FROM wms_pedidos p
         WHERE p.estado_reserva IN ('PENDENTE', 'RESERVADO', 'PARCIAL')
           AND EXISTS (SELECT 1 FROM wms_reservas r WHERE r.pedido_id = p.id AND r.estado = 'ATIVA')
    """).fetchall()
    candidatos = [c for c in candidatos if c["id_stokki"] not in vistos]
    if not candidatos:
        return 0

    situacoes = {}
    try:
        resposta = stokki_pedidos.listar_pedidos(
            sess, status="all", cliente=piloto_id, por_pagina=200,
            ordenar_coluna="1", ordenar_dir="desc")
        for linha in (resposta.get("aaData") or []):
            id_pedido = stokki_pedidos.extrair_id_da_linha(linha)
            if id_pedido:
                situacoes[id_pedido] = re.sub(
                    r"<[^>]+>", " ", str(linha.get("state", ""))).strip()
    except Exception as e:  # noqa: BLE001 -- sem a lista, nao cancela nada
        logger.warning("Nao deu pra conferir os pedidos sumidos na Stokki (%s) -- "
                        "nenhuma reserva foi liberada nesta rodada.", e)
        return 0

    liberados = 0
    for c in candidatos:
        situacao = situacoes.get(c["id_stokki"])
        if situacao is None:
            motivo = "pedido nao existe mais na Stokki"
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


def rodar(conn, sess, piloto_nome: str, piloto_id: str, limite: int, modo_teste: bool) -> dict:
    res = {"lidos": 0, "do_piloto": 0, "ignorados_outro_embarcador": 0,
           "reservados": 0, "parciais": 0, "pendencias": 0, "liberados": 0, "erros": 0}
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
    # nenhum status pode ter sido cancelado ou sumido (spec 7.3 item 6).
    if modo_teste:
        return res
    if pagina_cheia:
        logger.warning("Alguma listagem encheu a pagina (limite %d) -- pulando a liberacao de "
                        "reservas de pedido sumido nesta rodada, pra nao cancelar quem so nao foi lido.",
                        limite)
        return res
    res["liberados"] = _liberar_pedidos_sumidos(conn, sess, piloto_id, vistos)
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
