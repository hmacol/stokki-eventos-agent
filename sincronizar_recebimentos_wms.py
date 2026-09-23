# -*- coding: utf-8 -*-
"""
sincronizar_recebimentos_wms.py

Le os recebimentos do embarcador piloto na Stokki e cria a entrada
esperada no WMS. Quem endereca de fato e o operador, pela tela do
celular (tarefa seguinte): aqui so nasce a lista do que deve chegar, com
a QUANTIDADE que a Stokki informa -- lote e validade nao vem da Stokki
(decisao do Hugo, 23/09/2026), o operador digita lendo a caixa fisica.

Mesmo desenho de sincronizar_pedidos_wms.py (Task 7), adaptado pro
endpoint de incoming:
  1. Filtro do lado do servidor -- listar_recebimentos(cliente=piloto_id)
     ja devolve so os recebimentos do piloto (client=48 confirmado ao
     vivo pelo Hugo, 22/09/2026, so recebimentos da Maria Dolores).
  2. Rede de seguranca -- mesmo com o filtro do servidor, confere o
     #stkkc-<id> que vem no campo 'client' da propria linha antes de
     gravar qualquer coisa (mesmo padrao usado pelos pedidos outbound).
     Se uma versao futura da Stokki ignorar o parametro 'client', a
     rotina nao pode sair criando entrada esperada de outro embarcador.
  3. Trava cooperativa de stokki/sessao_uso.py em volta da rodada
     inteira, com liberar() no finally -- a sessao Stokki e UNICA e um
     login concorrente (stokki-wms-sincronizar-produtos,
     stokki-wms-reservar-pedidos, agente-importacao-stokki, o painel)
     derruba os cookies com 401. Vale tambem pro --modo-teste, que so
     pula a escrita mas ainda le da Stokki de verdade na mesma sessao.
     Se a trava nao vier, a rotina desiste -- a proxima rodada e em
     30 min e o registro e idempotente, entao nada se perde.
  4. Leitura HTTP fora da transacao, commit por recebimento -- o
     dados.db e compartilhado com o painel e todos os agentes.

Recebimento "Em transito" (mercadoria ainda nao chegou) normalmente nao
tem a tabela de itens ainda -- isso NAO e erro, so ainda nao ha nada pra
registrar. Um "Recebido" sem a tabela (tambem visto ao vivo numa amostra
de 8 recebimentos) e tratado como erro -- pulando, sem derrubar a
rodada.

COMO USAR (timer systemd a cada 30 min, infra/stokki-wms-recebimentos.*):
    python sincronizar_recebimentos_wms.py
    python sincronizar_recebimentos_wms.py --modo-teste      # nao grava nada
    python sincronizar_recebimentos_wms.py --limite 20
"""
import argparse
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

import wms_pedidos
from stokki import recebimentos as stokki_recebimentos
from stokki import sessao_uso
from stokki.auth import StokkiSession

logger = logging.getLogger("sincronizar_recebimentos_wms")

# Dono da trava cooperativa da sessao Stokki (stokki/sessao_uso.py). TTL
# folgado pro tamanho real de uma rodada -- so requests, sem Playwright.
# Espera curta de proposito: o timer se repete a cada 30 min, entao nao
# faz sentido esperar mais que isso -- so perderia a proxima janela tambem.
DONO_TRAVA = "wms-recebimentos"
TRAVA_TTL_SEGUNDOS = 600
TRAVA_ESPERA_SEGUNDOS = 120

_PADRAO_STKKC_ID = re.compile(r"#stkkc-(\d+)")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _piloto_id(config: dict) -> str:
    """Id numerico do embarcador piloto na Stokki (ex.: '48' = Maria
    Dolores) -- e o valor que vai no filtro client= do listar_recebimentos
    e na rede de seguranca do #stkkc-<id> de cada linha."""
    return str((config.get("wms", {}) or {}).get("embarcador_piloto_id", "48")).strip()


def _piloto_nome(config: dict) -> str:
    """So pra log e pra preencher wms_recebimentos.embarcador quando a
    linha nao trouxer nome -- NUNCA usado pra decidir se um recebimento e
    do piloto (o nome pode vir truncado na listagem)."""
    return (config.get("wms", {}) or {}).get("embarcador_piloto", "MARIA DOLORES").strip().upper()


def _stkkc_id_da_linha(linha) -> str | None:
    """Tira o id estavel do embarcador (#stkkc-NN) do campo 'client' da
    linha do aaData -- mesmo padrao de sincronizar_pedidos_wms.py."""
    client_html = str(linha.get("client", "") if isinstance(linha, dict) else "")
    m = _PADRAO_STKKC_ID.search(client_html)
    return m.group(1) if m else None


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c))


def rodar(conn, sess, piloto_nome: str, piloto_id: str, limite: int, modo_teste: bool) -> dict:
    res = {"lidos": 0, "do_piloto": 0, "ignorados_outro_embarcador": 0,
           "gravados": 0, "em_transito": 0, "ja_fechados": 0, "erros": 0}
    # Recebimento ja ENDERECADO esta fechado: nao ha nada pra reler nele.
    # Sem este filtro a rotina rebuscava o detalhe dos ~50 mais recentes a
    # cada 30 min, pra sempre, contra um sistema de sessao unica e fragil.
    fechados = {r["id_stokki"] for r in conn.execute(
        "SELECT id_stokki FROM wms_recebimentos WHERE estado = 'ENDERECADO'")}

    # Filtro do lado do servidor (correcao 1): a Stokki ja devolve so os
    # recebimentos do piloto quando cliente=piloto_id.
    resposta = stokki_recebimentos.listar_recebimentos(sess, cliente=piloto_id, por_pagina=limite)
    for linha in (resposta.get("aaData") or []):
        res["lidos"] += 1
        dados = stokki_recebimentos.extrair_cabecalho_da_linha(linha)
        if not dados.get("id_stokki"):
            logger.warning("Linha sem id reconhecivel -- pulando: %r", linha)
            res["erros"] += 1
            continue

        # Rede de seguranca (correcao 2): confere o #stkkc-<id> da propria
        # linha mesmo com o filtro do servidor.
        stkkc_id = _stkkc_id_da_linha(linha)
        if stkkc_id is None or stkkc_id != piloto_id:
            logger.warning(
                "Recebimento %s ignorado: #stkkc-%s nao bate com o piloto (%s) -- "
                "veio no filtro client=%s mesmo assim", dados["id_stokki"], stkkc_id, piloto_id, piloto_id)
            res["ignorados_outro_embarcador"] += 1
            continue
        res["do_piloto"] += 1
        if not dados.get("embarcador"):
            dados["embarcador"] = piloto_nome

        if dados["id_stokki"] in fechados:
            res["ja_fechados"] += 1
            continue

        if modo_teste:
            logger.info("[teste] recebimento %s (%s) seria gravado", dados["codigo"], piloto_nome)
            continue

        # Leitura HTTP fora da transacao; commit por recebimento, porque o
        # dados.db e compartilhado com o painel e todos os agentes.
        try:
            itens = stokki_recebimentos.ler_itens(sess, dados["id_stokki"])
        except Exception as e:  # noqa: BLE001 -- um recebimento ruim nao derruba a rodada
            logger.warning("Leitura do recebimento %s falhou: %s", dados["id_stokki"], e)
            res["erros"] += 1
            continue

        if not itens:
            # "Em transito" ainda nao tem a tabela de itens -- normal, nao
            # e erro. Qualquer outra situacao sem itens e caso raro (visto
            # ao vivo mesmo em "Recebido") -- registra como erro, mas nao
            # derruba a rodada.
            if "TRANSITO" in _sem_acento(dados.get("situacao", "")).upper():
                logger.info("Recebimento %s ainda em transito -- sem itens pra registrar.", dados["id_stokki"])
                res["em_transito"] += 1
            else:
                logger.warning("Recebimento %s (%s) sem itens legiveis -- pulando.",
                                dados["id_stokki"], dados.get("situacao", ""))
                res["erros"] += 1
            continue

        try:
            wms_pedidos.registrar_recebimento(conn, dados, itens)
            conn.commit()
            res["gravados"] += 1
        except Exception as e:  # noqa: BLE001 -- um recebimento ruim nao derruba a rodada
            conn.rollback()
            logger.warning("Recebimento %s falhou: %s", dados["id_stokki"], e)
            res["erros"] += 1
            continue
        time.sleep(0.3)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modo-teste", action="store_true", help="nao grava nada, so mostra o que faria")
    ap.add_argument("--limite", type=int, default=50, help="recebimentos nesta rodada")
    ap.add_argument("--embarcador", help="sobrescreve o nome do embarcador piloto do config.yaml (so log)")
    ap.add_argument("--embarcador-id", help="sobrescreve o id (stkkc) do embarcador piloto do config.yaml")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "wms_recebimentos.log", encoding="utf-8")])
    config = _carregar_config()
    piloto_id = (args.embarcador_id or _piloto_id(config)).strip()
    piloto_nome = (args.embarcador or _piloto_nome(config)).strip().upper()

    # Trava cooperativa da sessao Stokki (sessao unica -- ver stokki/sessao_uso.py
    # e sincronizar_pedidos_wms.py). Adquire ANTES de abrir a conexao com o
    # banco ou a StokkiSession: se a trava nao vier, a rotina desiste sem
    # tocar em nada. Vale tambem pro --modo-teste (ele le da Stokki de
    # verdade, so pula a escrita).
    if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=TRAVA_TTL_SEGUNDOS,
                                esperar_segundos=TRAVA_ESPERA_SEGUNDOS):
        # Sai com 0: desistir por trava ocupada e operacao NORMAL, nao
        # falha (o .service tem OnFailure=stokki-alerta-falha@%n, que
        # alertava a cada rodada). Mesmo precedente de
        # notificar_transportadoras.py e roteirizacao/documentacao_rota.py.
        ocupante = sessao_uso.em_uso()
        logger.warning("Stokki ocupada por '%s' -- desistindo desta rodada (proxima em 30 min).", ocupante)
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
