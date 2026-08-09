# -*- coding: utf-8 -*-
"""
geocodificacao.py

Geocodificação de endereços de entrega via Google Geocoding API,
com cache local em SQLite (dados/dados.db, tabela geocache).

Equivalente ao steps/geocodificacao.py do agente_relatorio, adaptado
para este projeto. As coordenadas alimentam payload["customer"]
["latitude"/"longitude"] — que o vuupt_client e o fingerprint_importacao
já esperam (lat/long fazem parte do hash e do trigger de atualização).

Cache:
  - Chave: endereço normalizado (maiúsculas, sem acento, espaços colapsados).
  - Endereço que o Google não resolveu também é cacheado (lat/long NULL)
    para não repetir chamadas fadadas a falhar a cada execução.

Uso:
    from geocodificacao import geocodificar

    coords = geocodificar("Rua X, 100, Centro, São Paulo - SP, 01000-000, Brasil",
                          api_key)
    if coords:
        lat, lng = coords
"""
import logging
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "dados" / "dados.db"
URL_GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"

# Evita repetir o mesmo warning de chave ausente a cada pedido
_avisou_sem_chave = False


def _normalizar_endereco(endereco: str) -> str:
    """Normaliza o endereço para servir de chave de cache."""
    s = unicodedata.normalize("NFKD", endereco or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.upper().split())


def _garantir_tabela(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocache (
            endereco       TEXT PRIMARY KEY,
            latitude       REAL,
            longitude      REAL,
            atualizado_em  TEXT
        )
    """)


def _buscar_cache(chave: str):
    """
    Retorna:
      (lat, lng)  — cache hit com coordenadas
      (None, None) — cache hit de falha (Google não resolveu antes)
      None         — cache miss
    """
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        _garantir_tabela(conn)
        row = conn.execute(
            "SELECT latitude, longitude FROM geocache WHERE endereco = ?",
            (chave,)
        ).fetchone()
        conn.close()
        if row is not None:
            return (row[0], row[1])
    except Exception as e:
        logger.debug(f"Erro ao ler geocache: {e}")
    return None


def _salvar_cache(chave: str, lat, lng):
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        _garantir_tabela(conn)
        conn.execute(
            "INSERT INTO geocache (endereco, latitude, longitude, atualizado_em) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(endereco) DO UPDATE SET "
            "latitude=excluded.latitude, longitude=excluded.longitude, "
            "atualizado_em=excluded.atualizado_em",
            (chave, lat, lng, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"Erro ao salvar geocache: {e}")


def geocodificar(endereco: str, api_key: str) -> tuple[float, float] | None:
    """
    Geocodifica um endereço. Retorna (latitude, longitude) ou None.

    - Sem api_key ou endereço vazio: retorna None (o VUUPT geocodifica
      por conta própria nesse caso).
    - Consulta o cache antes de chamar o Google; falhas do Google também
      são cacheadas para não repetir a chamada em toda execução.
    """
    global _avisou_sem_chave

    if not endereco or not endereco.strip():
        return None
    if not api_key:
        if not _avisou_sem_chave:
            logger.warning(
                "google_maps.api_key não configurada — coordenadas omitidas "
                "do payload (o VUUPT geocodificará por conta própria)."
            )
            _avisou_sem_chave = True
        return None

    chave = _normalizar_endereco(endereco)

    cache = _buscar_cache(chave)
    if cache is not None:
        lat, lng = cache
        if lat is None or lng is None:
            return None  # falha cacheada
        return (lat, lng)

    try:
        resp = requests.get(
            URL_GEOCODE,
            params={
                "address": endereco,
                "key": api_key,
                "region": "br",
                "components": "country:BR",
                "language": "pt-BR",
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        logger.warning(f"Falha na chamada de geocodificação: {e}")
        return None  # erro transitório: NÃO cacheia, tenta de novo na próxima

    status = body.get("status", "")
    results = body.get("results") or []

    if status == "OK" and results:
        loc = results[0].get("geometry", {}).get("location", {})
        lat, lng = loc.get("lat"), loc.get("lng")
        if lat is not None and lng is not None:
            _salvar_cache(chave, float(lat), float(lng))
            return (float(lat), float(lng))

    if status == "ZERO_RESULTS":
        # Endereço que o Google não resolve: cacheia a falha para não
        # repetir a chamada a cada execução.
        logger.warning(f"Geocodificação sem resultado para: {endereco[:80]}")
        _salvar_cache(chave, None, None)
        return None

    # OVER_QUERY_LIMIT, REQUEST_DENIED, INVALID_REQUEST, UNKNOWN_ERROR:
    # não cacheia (pode ser transitório ou problema de configuração).
    logger.warning(f"Geocodificação retornou status={status!r} para: {endereco[:80]}")
    return None
