# -*- coding: utf-8 -*-
"""
nucleo/trajeto_rota.py

Desenho do trajeto de uma rota pro mapa do app do motorista. Pontos = CD +
paradas validas (com coordenada, nao canceladas) por ordem + CD. O desenho
vem da Google Routes API (km_rodoviario.tracar_google) e fica guardado em
nucleo_rotas_trajeto, chaveado pela assinatura dos pontos: reordenar ou
cancelar parada muda a assinatura e recalcula; mudar so a situacao, nao.
Falha ou sem chave: devolve os proprios pontos (linha reta) sem gravar.
"""
import hashlib
import json
import logging
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

from nucleo import banco                               # noqa: E402
from regras.km_cobrado import COORD_CD                  # noqa: E402
import km_rodoviario                                    # noqa: E402

logger = logging.getLogger("nucleo.trajeto_rota")

FONTE_GOOGLE = "GOOGLE_ROUTES"
FONTE_LINHA_RETA = "LINHA_RETA"


def pontos_da_rota(conn, rota_id: int, base=COORD_CD) -> list[tuple[float, float]]:
    paradas = conn.execute(
        "SELECT latitude, longitude FROM nucleo_paradas WHERE rota_id = ? AND situacao != ? ORDER BY ordem",
        (rota_id, banco.PARADA_CANCELADA),
    ).fetchall()
    coords = [(p["latitude"], p["longitude"]) for p in paradas if p["latitude"] is not None and p["longitude"] is not None
              and (p["latitude"], p["longitude"]) != (0, 0)]
    if not coords:
        return []
    return [tuple(base)] + coords + [tuple(base)]


def assinatura(pontos) -> str:
    arred = [[round(lat, 6), round(lng, 6)] for lat, lng in pontos]
    return hashlib.sha1(json.dumps(arred).encode("utf-8")).hexdigest()


def trajeto(conn, rota_id: int, api_key: str | None) -> dict:
    pontos = pontos_da_rota(conn, rota_id)
    ass = assinatura(pontos)
    base = [COORD_CD[0], COORD_CD[1]]
    reta = {"pontos": [list(p) for p in pontos], "fonte": FONTE_LINHA_RETA, "assinatura": ass, "base": base}
    if not pontos:
        return reta
    guardado = conn.execute(
        "SELECT pontos_json, fonte FROM nucleo_rotas_trajeto WHERE rota_id = ? AND assinatura = ?", (rota_id, ass),
    ).fetchone()
    if guardado:
        return {"pontos": json.loads(guardado["pontos_json"]), "fonte": guardado["fonte"], "assinatura": ass, "base": base}
    if not api_key:
        logger.warning("trajeto rota %s: sem chave do Google, linha reta", rota_id)
        return reta
    try:
        desenho = km_rodoviario.tracar_google(pontos, api_key)
    except Exception as e:
        logger.warning("trajeto rota %s: Google falhou (%s), linha reta", rota_id, e)
        return reta
    lista = [[lat, lng] for lat, lng in desenho]
    conn.execute(
        "INSERT INTO nucleo_rotas_trajeto (rota_id, assinatura, pontos_json, fonte, criado_em) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(rota_id) DO UPDATE SET assinatura = excluded.assinatura, pontos_json = excluded.pontos_json, "
        "fonte = excluded.fonte, criado_em = excluded.criado_em",
        (rota_id, ass, json.dumps(lista), FONTE_GOOGLE, banco.agora()),
    )
    conn.commit()
    return {"pontos": lista, "fonte": FONTE_GOOGLE, "assinatura": ass, "base": base}
