# -*- coding: utf-8 -*-
"""
roteirizacao/atualizar_volumes_conferidos.py

Le na Stokki a quantidade de volumes CONFERIDA pelo galpao de cada pedido
das rotas do dia e guarda na tabela volumes_conferidos (dados.db). Roda
as 03h50, antes do romaneio das 04h (pedido do Hugo, 16/11): o romaneio
mostra a caixa real conferida, nao a caixa normalizada (dimension_3 da
Vuupt) nem o numero da nota.

So LE a Stokki e a Vuupt. Nao mexe no dimension_3, no fator_ponderado nem
em nada de faturamento. A StokkiSession entra sozinha na fila das contas
(stokki/sessao_uso.py).

Pedido sem conferencia na hora da leitura fica com o volume da NF-e e
fonte 'nfe' (o romaneio marca com *); sem nenhum dos dois, volumes NULL.

COMO USAR:
    py -3.11 roteirizacao/atualizar_volumes_conferidos.py --data hoje
    py -3.11 roteirizacao/atualizar_volumes_conferidos.py --data 16/11/2026 --rota 5041749
    py -3.11 roteirizacao/atualizar_volumes_conferidos.py --prontos [--limite 80]
        (28/11, de hora em hora: pedidos em aguardando transportador sem leitura,
         pra tela Etiquetar do galpao sugerir o total conferido; nao chama a Vuupt)
"""
import argparse
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
DB_PATH = _RAIZ_PROJETO / "dados" / "dados.db"

logger = logging.getLogger("atualizar_volumes_conferidos")


def id_stokki(codigo: str) -> int | None:
    """'PS-36327-R1' -> 36327 (primeira sequencia de digitos: o sufixo de
    reentrega nao pode virar o id)."""
    m = re.search(r"(\d+)", codigo or "")
    return int(m.group(1)) if m else None


def ids_do_codigo(codigo: str) -> list[int]:
    """Um servico da Vuupt pode juntar pedidos no mesmo 'code' separados
    por virgula ('#PS-37189, PS-37176')."""
    ids = [id_stokki(c) for c in (codigo or "").split(",") if c.strip()]
    return [i for i in ids if i is not None]


def _garantir_tabela(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS volumes_conferidos ("
        " id_pedido INTEGER PRIMARY KEY,"   # id do pedido de saida na Stokki (PS-<id>)
        " volumes INTEGER,"                 # NULL = a Stokki nao tinha numero nenhum
        " fonte TEXT,"                      # 'conferencia' | 'nfe' | NULL
        " lido_em TEXT NOT NULL)")


def gravar(id_pedido: int, volumes: int | None, fonte: str | None, db_path=DB_PATH) -> None:
    con = sqlite3.connect(db_path, timeout=30)
    try:
        _garantir_tabela(con)
        con.execute(
            "INSERT INTO volumes_conferidos (id_pedido, volumes, fonte, lido_em) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id_pedido) DO UPDATE SET volumes=excluded.volumes, fonte=excluded.fonte, "
            "lido_em=excluded.lido_em",
            (id_pedido, volumes, fonte, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
    finally:
        con.close()


def carregar(ids: set[int], db_path=DB_PATH) -> dict[int, tuple[int | None, str | None]]:
    """{id_pedido: (volumes, fonte)} do que ja foi lido. Tabela ausente ou
    banco fora devolve {} -- o romaneio nunca deixa de sair por causa disso."""
    if not ids:
        return {}
    try:
        con = sqlite3.connect(db_path, timeout=30)
        try:
            lista = sorted(ids)
            achados = {}
            for i in range(0, len(lista), 500):
                lote = lista[i:i + 500]
                marcadores = ",".join("?" * len(lote))
                for id_pedido, volumes, fonte in con.execute(
                        f"SELECT id_pedido, volumes, fonte FROM volumes_conferidos "
                        f"WHERE id_pedido IN ({marcadores})", lote):
                    achados[id_pedido] = (volumes, fonte)
            return achados
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.debug(f"volumes_conferidos indisponivel (romaneio segue sem): {e}")
        return {}


def atualizar(sessao, ids: list[int], db_path=DB_PATH) -> dict:
    """Le cada pedido na Stokki e grava. Falha num pedido nao para os outros."""
    from stokki import pedidos as stokki_pedidos
    cont = {"conferencia": 0, "nfe": 0, "sem_numero": 0, "falhas": 0}
    for id_pedido in ids:
        try:
            detalhe = stokki_pedidos.obter_detalhe(sessao, id_pedido)
        except Exception as e:
            logger.warning(f"  PS-{id_pedido}: falha ao ler na Stokki -- {e}")
            cont["falhas"] += 1
            continue
        volumes, fonte = detalhe.get("quantidade_volumes"), detalhe.get("fonte_volumes")
        gravar(id_pedido, volumes, fonte, db_path)
        cont[fonte or "sem_numero"] += 1
    return cont


def main(data_str: str, rota_id: int | None) -> int:
    sys.path.insert(0, str(_RAIZ_PROJETO))
    sys.path.insert(0, str(_RAIZ_LOCAL))
    from avisar_motoristas_rotas import (
        _carregar_config, _extrair_servicos_da_rota, _parse_data, buscar_rotas_do_dia,
    )
    from stokki.auth import StokkiSession

    inicio = time.time()
    config = _carregar_config()
    data_alvo = _parse_data(data_str)
    rotas = buscar_rotas_do_dia(config.get("vuupt_api", {}).get("token", ""), data_alvo)
    if rota_id is not None:
        rotas = [r for r in rotas if r.get("id") == rota_id]

    ids: list[int] = []
    for r in rotas:
        for s in _extrair_servicos_da_rota(r):
            for i in ids_do_codigo(s.get("code") or ""):
                if i not in ids:
                    ids.append(i)
    logger.info(f"{len(rotas)} rota(s) em {data_alvo.strftime('%d/%m/%Y')}, {len(ids)} pedido(s) a ler na Stokki.")
    if not ids:
        return 0

    cont = atualizar(StokkiSession(config), ids)
    logger.info(f"Concluido em {time.time() - inicio:.0f}s: {cont['conferencia']} conferido(s), "
                f"{cont['nfe']} so com volume da NF-e, {cont['sem_numero']} sem numero, "
                f"{cont['falhas']} falha(s).")
    # tudo falhou = Stokki fora: sai com erro pro alerta de falha avisar
    return 1 if cont["falhas"] == len(ids) else 0


# --- modo --prontos (28/11): sugestao de caixas da etiqueta de volume --------------------------
# A tela Etiquetar do galpao (painel_agentes/expedicao_volumes.py) sugere o total CONFERIDO
# quando o pedido esta em "aguardando transportador". A leitura das 03h50 so cobre as rotas
# do dia; este modo roda de hora em hora e le os prontos que ainda nao tem leitura nenhuma.

STATUS_PRONTO_STOKKI = "Waiting for Carrier"
MINIMO_PRA_ALERTA = 3


def ids_prontos_sem_leitura(db_path=DB_PATH, limite: int = 80) -> list[int]:
    """id da Stokki dos pedidos em aguardando transportador (retrato vigia_stokki_abertos) sem
    linha em volumes_conferidos, os mais novos primeiro. Tabela ausente ou banco fora = []."""
    try:
        con = sqlite3.connect(db_path, timeout=30)
        try:
            _garantir_tabela(con)
            return [r[0] for r in con.execute(
                "SELECT a.id_stokki FROM vigia_stokki_abertos a "
                "LEFT JOIN volumes_conferidos v ON v.id_pedido = a.id_stokki "
                "WHERE a.status_stokki = ? AND a.id_stokki IS NOT NULL AND v.id_pedido IS NULL "
                "ORDER BY a.primeira_vez_em DESC, a.id_stokki DESC LIMIT ?",
                (STATUS_PRONTO_STOKKI, int(limite)))]
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.warning(f"prontos sem leitura indisponivel (banco ocupado ou fora): {e}")
        return []


def main_prontos(db_path=DB_PATH, sessao=None, limite: int = 80) -> int:
    """Le na Stokki os prontos sem leitura e grava. Nao chama a Vuupt. Quem ja tem linha
    (mesmo so com o volume da NF-e) nao e relido aqui: as 03h50 releem as rotas do dia."""
    inicio = time.time()
    ids = ids_prontos_sem_leitura(db_path, limite)
    logger.info(f"{len(ids)} pedido(s) pronto(s) na Stokki sem leitura de volumes (teto {limite}).")
    if not ids:
        return 0
    if sessao is None:
        sys.path.insert(0, str(_RAIZ_PROJETO))
        sys.path.insert(0, str(_RAIZ_LOCAL))
        from avisar_motoristas_rotas import _carregar_config
        from stokki.auth import StokkiSession
        sessao = StokkiSession(_carregar_config())
    cont = atualizar(sessao, ids, db_path)
    logger.info(f"Concluido em {time.time() - inicio:.0f}s: {cont['conferencia']} conferido(s), "
                f"{cont['nfe']} so com volume da NF-e, {cont['sem_numero']} sem numero, "
                f"{cont['falhas']} falha(s).")
    # De hora em hora sao poucos pedidos, e quem falha fica sem linha e e relido na rodada seguinte:
    # um pedido com erro permanente nao pode virar alerta de falha toda hora. So trata como "Stokki
    # fora" quando falham todos e sao pelo menos MINIMO_PRA_ALERTA (com a Stokki fora eles acumulam).
    if cont["falhas"] == len(ids) and len(ids) < MINIMO_PRA_ALERTA:
        logger.warning(f"{len(ids)} pedido(s) falharam na leitura; serao tentados de novo na proxima rodada.")
        return 0
    return 1 if cont["falhas"] == len(ids) else 0


if __name__ == "__main__":
    (_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_RAIZ_LOCAL / "dados" / "atualizar_volumes_conferidos.log", encoding="utf-8"),
        ],
    )
    parser = argparse.ArgumentParser(description="Le na Stokki os volumes conferidos dos pedidos das rotas do dia")
    parser.add_argument("--data", default="hoje", help="'hoje' (padrao), 'amanha' ou DD/MM/AAAA")
    parser.add_argument("--rota", type=int, default=None, help="route_id da Vuupt: so os pedidos dessa rota")
    parser.add_argument("--prontos", action="store_true",
                        help="le os pedidos em aguardando transportador sem leitura (em vez das rotas do dia)")
    parser.add_argument("--limite", type=int, default=80, help="com --prontos: maximo de pedidos por rodada")
    args = parser.parse_args()
    if args.prontos:
        sys.exit(main_prontos(limite=args.limite))
    sys.exit(main(args.data, args.rota))
