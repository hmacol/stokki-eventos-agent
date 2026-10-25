# -*- coding: utf-8 -*-
"""
nucleo/eventos_freshhub.py

O legado registra no sistema novo (POST /api/eventos, nucleo/freshhub_client) o que decide no
planejamento -- plano 1.2 do freshhub, decisão 5:
  roteirizado   rascunho gravado (rascunhos_rota.criar_lote_rascunhos) ou pedido posto numa rota existente
                no incremento das 22h;
  nao_aprontou  parada tirada da rota no envio do rascunho / pedido barrado no incremento porque o retrato
                da Stokki ainda o mostra em separação/conferência/espera;
  rota_enviada  rascunho enviado à Vuupt (rascunhos_rota.enviar_rascunho);
  servico_criado é devolvido pela fila (pipeline --fila-freshhub), não por aqui.

A API é idempotente por (codigo, tipo, ocorrido_em), então reenviar nunca duplica. Falha da API NÃO derruba
a rodada: o lote fica em PENDENTES (jsonl ao lado do dados.db) e vai na próxima chamada que der certo.
Sem `freshhub_api` no config.yaml (sombra desligada) nada é mandado nem guardado. --modo-teste não manda.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nucleo import banco, freshhub_client
from nucleo.normalizacao import normalizar_codigo

logger = logging.getLogger("nucleo.eventos_freshhub")

SP = ZoneInfo("America/Sao_Paulo")
TIPOS = ("roteirizado", "nao_aprontou", "servico_criado", "rota_enviada")
PENDENTES = banco.DB_PATH.parent / "eventos_freshhub_pendentes.jsonl"


def evento(codigo: str, tipo: str, dados: dict | None = None, ocorrido_em: datetime | None = None) -> dict:
    if tipo not in TIPOS:
        raise ValueError(f"tipo de evento fora do contrato do legado: {tipo}")
    quando = ocorrido_em or datetime.now(SP)
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=SP)
    return {"codigo": normalizar_codigo(codigo) or str(codigo), "tipo": tipo,
            "ocorrido_em": quando.isoformat(timespec="seconds"), "dados": dict(dados or {})}


def _ler_pendentes() -> list[dict]:
    try:
        return [json.loads(linha) for linha in PENDENTES.read_text(encoding="utf-8").splitlines() if linha.strip()]
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"pendentes ilegíveis em {PENDENTES} ({e}); seguem como estão")
        return []


def _guardar_pendentes(eventos: list[dict]) -> None:
    try:
        PENDENTES.parent.mkdir(parents=True, exist_ok=True)
        with PENDENTES.open("a", encoding="utf-8") as f:
            for e in eventos:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        logger.error(f"não consegui guardar {len(eventos)} evento(s) pendente(s) em {PENDENTES}: {e}")


def registrar(config: dict | None, eventos: list[dict], *, modo_teste: bool = False) -> dict | None:
    """Manda `eventos` (antes, os pendentes de rodadas anteriores). Devolve a resposta da API do lote novo
    ou None quando nada foi mandado (modo teste, lista vazia, sombra desligada ou API fora -- aí o lote
    fica guardado)."""
    if not eventos:
        return None
    if modo_teste:
        logger.info(f"[TESTE] {len(eventos)} evento(s) pro freshhub não enviado(s): "
                    + ", ".join(f"{e['tipo']} {e['codigo']}" for e in eventos[:10]))
        return None
    cliente = freshhub_client.de_config(config)
    if cliente is None:
        return None
    pendentes = _ler_pendentes()
    try:
        if pendentes:
            cliente.eventos(pendentes)
            PENDENTES.unlink(missing_ok=True)
            logger.info(f"{len(pendentes)} evento(s) pendente(s) entregue(s) ao freshhub")
        resposta = cliente.eventos(eventos)
    except Exception as e:  # noqa: BLE001 - a API nova fora nunca derruba a rodada
        logger.error(f"eventos_freshhub.falha: {len(eventos)} evento(s) guardado(s) pra proxima rodada ({e})")
        _guardar_pendentes(eventos)
        return None
    logger.info("eventos pro freshhub: %s", resposta)
    return resposta


# ── atalhos pelos tres momentos do planejamento ──────────────────────────────

def roteirizados(config: dict | None, paradas: list[dict], *, rascunho_id: int, rota_nome: str,
                 data_alvo: str, modo_teste: bool = False) -> dict | None:
    dados = {"rascunho_id": rascunho_id, "rota_nome": rota_nome, "data": data_alvo}
    return registrar(config, [evento(p["codigo"], "roteirizado", {**dados, "service_id": p.get("service_id")})
                              for p in paradas], modo_teste=modo_teste)


def nao_aprontaram(config: dict | None, itens: list[dict], *, contexto: dict, modo_teste: bool = False) -> dict | None:
    """`itens` como separar_prontos devolve (codigo, service_id, situacao_stokki)."""
    return registrar(config, [evento(p["codigo"], "nao_aprontou", {**contexto, "situacao_stokki": p.get("situacao_stokki"),
                                                                   "service_id": p.get("service_id")})
                              for p in itens], modo_teste=modo_teste)


def rota_enviada(config: dict | None, paradas: list[dict], *, vuupt_route_id: int, rascunho_id: int,
                 rota_nome: str, modo_teste: bool = False) -> dict | None:
    dados = {"vuupt_route_id": vuupt_route_id, "rascunho_id": rascunho_id, "rota_nome": rota_nome}
    return registrar(config, [evento(p["codigo"], "rota_enviada", {**dados, "service_id": p.get("service_id")})
                              for p in paradas], modo_teste=modo_teste)
