# -*- coding: utf-8 -*-
"""
nucleo/recalcular_km_rotas.py

Recalcula o km ESTIMADO das rotas do núcleo (nucleo_rotas.km_estimado /
km_volta_estimado / km_fonte_estimativa) pela Google Routes API
(roteirizacao/km_rodoviario.py), a partir das paradas gravadas em
nucleo_paradas (não canceladas, na ordem).

Por que: até 11/09 o km era linha reta e só existia pra rota que veio de
rascunho. Com a regra do Hugo (volta só conta com insucesso/parcial ou
parada fora da Grande SP) o extrato precisa do trecho de volta separado
-- e de um km rodoviário honesto. Este backfill deixa o histórico
consistente e preenche as rotas que estavam sem km.

COMO USAR (VPS, como www-data, dentro do venv):
    venv/bin/python nucleo/recalcular_km_rotas.py --dias 30
    venv/bin/python nucleo/recalcular_km_rotas.py --rota 407
    venv/bin/python nucleo/recalcular_km_rotas.py --dias 30 --so-sem-km
    --forcar: recalcula mesmo quem já está como GOOGLE_ROUTES.

Custo: 1 chamada por rota (2 se tiver mais de 25 paradas). ~300 rotas/mês
ficam dentro das 5.000 grátis do nível Pro.
"""
import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

from nucleo import banco                               # noqa: E402
from regras.km_cobrado import COORD_CD                  # noqa: E402
import km_rodoviario                                    # noqa: E402

logger = logging.getLogger("nucleo.recalcular_km_rotas")


def _api_key() -> str:
    import yaml
    caminho = _RAIZ / "config.yaml"
    if not caminho.exists():
        return ""
    with open(caminho, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return (cfg.get("google_maps") or {}).get("api_key", "") or ""


def recalcular_rota(conn, rota_id: int, api_key: str, base=COORD_CD) -> km_rodoviario.ResultadoKm | None:
    paradas = conn.execute(
        "SELECT latitude, longitude FROM nucleo_paradas WHERE rota_id = ? AND situacao != ? ORDER BY ordem",
        (rota_id, banco.PARADA_CANCELADA),
    ).fetchall()
    coords = [(p["latitude"], p["longitude"]) for p in paradas if p["latitude"] is not None and p["longitude"] is not None]
    r = km_rodoviario.calcular_km(base, coords, api_key)
    if r is None:
        return None
    conn.execute(
        "UPDATE nucleo_rotas SET km_estimado = ?, km_volta_estimado = ?, km_fonte_estimativa = ?, "
        "km_fonte = COALESCE(km_fonte, 'ESTIMADO'), atualizado_em = ? WHERE id = ?",
        (r.total_km, r.volta_km, r.fonte, banco.agora(), rota_id),
    )
    rascunho_id = conn.execute("SELECT rascunho_id FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone()[0]
    if rascunho_id and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rascunhos_rota'").fetchone():
        colunas = {c[1] for c in conn.execute("PRAGMA table_info(rascunhos_rota)")}
        if {"km_volta_estimado", "km_fonte_estimativa"} <= colunas:
            conn.execute(
                "UPDATE rascunhos_rota SET km_estimado = ?, km_volta_estimado = ?, km_fonte_estimativa = ? WHERE id = ?",
                (r.total_km, r.volta_km, r.fonte, rascunho_id),
            )
    return r


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dias", type=int, default=30, help="rotas com data_rota nos últimos N dias (padrão 30)")
    parser.add_argument("--rota", type=int, help="só esta nucleo_rotas.id")
    parser.add_argument("--so-sem-km", action="store_true", help="só rotas com km_estimado NULL")
    parser.add_argument("--forcar", action="store_true", help="recalcula também quem já é GOOGLE_ROUTES")
    parser.add_argument("--modo-teste", action="store_true", help="não grava")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    api_key = _api_key()
    if not api_key:
        logger.warning("Sem google_maps.api_key no config.yaml -- tudo vai sair em linha reta (HAVERSINE).")

    conn = banco.conectar()
    try:
        if args.rota:
            rows = conn.execute("SELECT id, km_estimado, km_fonte_estimativa FROM nucleo_rotas WHERE id = ?", (args.rota,)).fetchall()
        else:
            desde = (date.today() - timedelta(days=args.dias)).isoformat()
            rows = conn.execute(
                "SELECT id, km_estimado, km_fonte_estimativa FROM nucleo_rotas WHERE data_rota >= ? AND status != ? ORDER BY id",
                (desde, banco.ROTA_CANCELADA),
            ).fetchall()
        stats = {"rotas": len(rows), "recalculadas": 0, "google": 0, "haversine": 0, "puladas": 0, "sem_paradas": 0}
        for row in rows:
            if args.so_sem_km and row["km_estimado"] is not None:
                stats["puladas"] += 1
                continue
            if not args.forcar and row["km_fonte_estimativa"] == km_rodoviario.FONTE_GOOGLE:
                stats["puladas"] += 1
                continue
            r = recalcular_rota(conn, row["id"], api_key)
            if r is None:
                stats["sem_paradas"] += 1
                continue
            stats["recalculadas"] += 1
            stats["google" if r.fonte == km_rodoviario.FONTE_GOOGLE else "haversine"] += 1
            logger.info(f"rota {row['id']}: {row['km_estimado']} -> {r.total_km} km ({r.fonte}, volta {r.volta_km})")
        if args.modo_teste:
            conn.rollback()
            logger.info("modo-teste: nada gravado.")
        else:
            conn.commit()
        logger.info(f"Concluído: {stats}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
