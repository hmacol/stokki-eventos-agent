# -*- coding: utf-8 -*-
"""
sincronizar_produtos_stokki.py

Espelha o catálogo de produtos do Stokki (Cadastro → Produtos) na tabela
wms_produtos do dados.db, pro endereçamento do galpão achar o produto pelo
EAN/DUN lido na embalagem.

Duas etapas por execução:
  1. Tabela: uma varredura paginada (4 requisições de 500) com nome,
     cliente, SKU, EAN, categoria, situação e data de atualização.
     Produtos que sumiram da tabela viram ativo=0.
  2. Perfil: pra até --max-perfis produtos que nunca tiveram o perfil lido
     (ou mudaram no Stokki depois da última leitura), abre show/{id} e
     pega unidade, DUN da caixa, quantidade por caixa, "Lote: Sim/Não" e
     peso. Na primeira rodada real são ~1.500 páginas: use --max-perfis 2000
     uma vez, à mão; o timer segue com o padrão (150) só pra manter.

COMO USAR (timer systemd na VPS a cada 30 min, infra/stokki-wms-sincronizar-produtos.*):
    python sincronizar_produtos_stokki.py                 # tabela + até 150 perfis
    python sincronizar_produtos_stokki.py --max-perfis 2000
    python sincronizar_produtos_stokki.py --so-tabela
"""
import argparse
import logging
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

import wms
from stokki import produtos as stokki_produtos
from stokki.auth import StokkiSession

logger = logging.getLogger("sincronizar_produtos_stokki")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def sincronizar_tabela(conn, sess) -> dict:
    lista = stokki_produtos.listar_produtos(sess)
    if not lista:
        raise RuntimeError("Tabela de produtos do Stokki veio vazia -- abortando pra não desativar tudo.")
    vistos = set()
    novos = 0
    for p in lista:
        vistos.add(p["stokki_id"])
        existia = conn.execute("SELECT 1 FROM wms_produtos WHERE stokki_id = ?", (p["stokki_id"],)).fetchone()
        wms.upsert_produto_stokki(conn, p)
        if not existia:
            novos += 1
    marks = ",".join("?" for _ in vistos)
    cur = conn.execute(
        f"UPDATE wms_produtos SET ativo = 0, atualizado_em = ? WHERE stokki_id IS NOT NULL AND ativo = 1 AND stokki_id NOT IN ({marks})",
        (wms.agora(), *vistos))
    conn.commit()
    return {"lidos": len(lista), "novos": novos, "desativados": cur.rowcount,
            "ativos": sum(1 for p in lista if p["ativo"])}


def sincronizar_perfis(conn, sess, maximo: int, pausa: float = 0.4) -> dict:
    ids = wms.produtos_sem_perfil(conn, maximo)
    ok, falhas = 0, 0
    for i, sid in enumerate(ids, 1):
        try:
            perfil = stokki_produtos.ler_perfil(sess, sid)
            wms.atualizar_perfil_produto(conn, sid, perfil)
            ok += 1
        except Exception as e:  # noqa: BLE001 -- um perfil ruim não derruba a rodada
            falhas += 1
            logger.warning("Perfil %s falhou: %s", sid, e)
        if i % 50 == 0:
            conn.commit()
            logger.info("Perfis: %d/%d", i, len(ids))
        time.sleep(pausa)
    conn.commit()
    return {"pendentes_no_inicio": len(ids), "lidos": ok, "falhas": falhas}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-perfis", type=int, default=150, help="perfis (show/{id}) a ler nesta rodada")
    ap.add_argument("--so-tabela", action="store_true", help="não lê perfis")
    ap.add_argument("--so-perfis", action="store_true", help="não varre a tabela")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "wms_sincronizar_produtos.log", encoding="utf-8")])
    config = _carregar_config()
    conn = wms.conectar()
    sess = StokkiSession(config)
    t0 = time.time()
    if not args.so_perfis:
        r = sincronizar_tabela(conn, sess)
        logger.info("Tabela: %s", r)
    if not args.so_tabela:
        r = sincronizar_perfis(conn, sess, args.max_perfis)
        logger.info("Perfis: %s", r)
    logger.info("Catálogo agora: %s (%.0fs)", wms.contar_produtos(conn), time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())