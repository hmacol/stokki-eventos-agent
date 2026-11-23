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
a rodada: o lote fica em PENDENTES (jsonl ao lado do dados.db) e vai na próxima chamada que der certo, em
fatias de até LOTE_MAX (teto da API). Recusa 4xx (validação, token) não melhora repetindo: o lote é
descartado com erro no log, nunca guardado -- senão travaria tudo que viesse depois (revisão 25/10).
Linha ilegível no arquivo é pulada. Código combinado da Vuupt ("#PS-1, #PS-2") vira um evento por pedido.
Sem `freshhub_api` no config.yaml (sombra desligada) nada é mandado nem guardado. --modo-teste não manda.
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from nucleo import banco, freshhub_client
from nucleo.normalizacao import normalizar_codigo

logger = logging.getLogger("nucleo.eventos_freshhub")

SP = ZoneInfo("America/Sao_Paulo")
TIPOS = ("roteirizado", "nao_aprontou", "servico_criado", "rota_enviada")
PENDENTES = banco.DB_PATH.parent / "eventos_freshhub_pendentes.jsonl"
LOTE_MAX = 500  # EventosCorpo.eventos max_length na API nova


def evento(codigo: str, tipo: str, dados: dict | None = None, ocorrido_em: datetime | None = None) -> dict:
    if tipo not in TIPOS:
        raise ValueError(f"tipo de evento fora do contrato do legado: {tipo}")
    quando = ocorrido_em or datetime.now(SP)
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=SP)
    return {"codigo": normalizar_codigo(codigo) or str(codigo), "tipo": tipo,
            "ocorrido_em": quando.isoformat(timespec="seconds"), "dados": dict(dados or {})}


def _codigos_individuais(codigo: str | None) -> list[str]:
    """'#PS-1, #PS-2' -> ['PS-1', 'PS-2'] (o -R fica: e o code do servico da reentrega)."""
    return [c for c in (normalizar_codigo(parte.strip()) for parte in str(codigo or "").split(",")) if c]


def _eventos_do_item(codigo: str | None, tipo: str, dados: dict) -> list[dict]:
    """Um evento por pedido do codigo da Vuupt; o codigo combinado inteiro fica em dados.codigo_vuupt."""
    individuais = _codigos_individuais(codigo) or [str(codigo or "")]
    extra = {"codigo_vuupt": ", ".join(individuais)} if len(individuais) > 1 else {}
    return [evento(c, tipo, {**dados, **extra}) for c in individuais]


def _fatias(eventos: list[dict]) -> list[list[dict]]:
    return [eventos[i:i + LOTE_MAX] for i in range(0, len(eventos), LOTE_MAX)]


def _ler_pendentes() -> list[dict]:
    try:
        linhas = PENDENTES.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning(f"pendentes ilegíveis em {PENDENTES} ({e}); seguem como estão")
        return []
    eventos: list[dict] = []
    ruins = 0
    for linha in linhas:
        if not linha.strip():
            continue
        try:
            e = json.loads(linha)
        except json.JSONDecodeError:
            ruins += 1
            continue
        if isinstance(e, dict) and e.get("codigo") and e.get("tipo") in TIPOS:
            eventos.append(e)
        else:
            ruins += 1
    if ruins:
        logger.warning(f"{ruins} linha(s) ilegível(is) em {PENDENTES} descartada(s)")
    return eventos


def _gravar_pendentes(eventos: list[dict]) -> None:
    """Reescreve o arquivo com exatamente `eventos` (vazio = apaga)."""
    try:
        if not eventos:
            PENDENTES.unlink(missing_ok=True)
            return
        PENDENTES.parent.mkdir(parents=True, exist_ok=True)
        PENDENTES.write_text("".join(json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in eventos),
                             encoding="utf-8")
    except OSError as e:
        logger.error(f"não consegui guardar {len(eventos)} evento(s) pendente(s) em {PENDENTES}: {e}")


def _somar(total: dict | None, resposta: dict) -> dict:
    if total is None:
        return dict(resposta)
    return {"aceitos": total.get("aceitos", 0) + resposta.get("aceitos", 0),
            "repetidos": total.get("repetidos", 0) + resposta.get("repetidos", 0),
            "desconhecidos": list(total.get("desconhecidos") or []) + list(resposta.get("desconhecidos") or [])}


def registrar(config: dict | None, eventos: list[dict], *, modo_teste: bool = False) -> dict | None:
    """Manda `eventos` (antes, os pendentes de rodadas anteriores), em fatias de ate LOTE_MAX. Devolve a
    soma das respostas do lote novo ou None quando nada do lote novo foi mandado (modo teste, lista vazia,
    sombra desligada, API fora -- ai o que faltou fica guardado; recusa 4xx e descartada)."""
    if not eventos:
        return None
    if modo_teste:
        logger.info(f"[TESTE] {len(eventos)} evento(s) pro freshhub não enviado(s): "
                    + ", ".join(f"{e['tipo']} {e['codigo']}" for e in eventos[:10]))
        return None
    cliente = freshhub_client.de_config(config)
    if cliente is None:
        return None
    fila = [("pendente", f) for f in _fatias(_ler_pendentes())] + [("novo", f) for f in _fatias(list(eventos))]
    total: dict | None = None
    entregues_pendentes = 0
    for i, (origem, fatia) in enumerate(fila):
        try:
            resposta = cliente.eventos(fatia)
        except Exception as e:  # noqa: BLE001 - a API nova fora nunca derruba a rodada
            if isinstance(e, freshhub_client.ErroFreshhub) and e.permanente:
                logger.error(f"eventos_freshhub.falha: lote de {len(fatia)} evento(s) ({origem}) recusado pela API "
                             f"({e}) -- descartado, repetir nao resolve")
                continue
            restantes = [ev for _, fatia_r in fila[i:] for ev in fatia_r]
            logger.error(f"eventos_freshhub.falha: {len(restantes)} evento(s) guardado(s) pra proxima rodada ({e})")
            _gravar_pendentes(restantes)
            return None
        if origem == "pendente":
            entregues_pendentes += len(fatia)
        else:
            total = _somar(total, resposta if isinstance(resposta, dict) else {})
    _gravar_pendentes([])
    if entregues_pendentes:
        logger.info(f"{entregues_pendentes} evento(s) pendente(s) entregue(s) ao freshhub")
    logger.info("eventos pro freshhub: %s", total)
    return total


# ── atalhos pelos tres momentos do planejamento ──────────────────────────────

def roteirizados(config: dict | None, paradas: list[dict], *, rascunho_id: int, rota_nome: str,
                 data_alvo: str, modo_teste: bool = False) -> dict | None:
    dados = {"rascunho_id": rascunho_id, "rota_nome": rota_nome, "data": data_alvo}
    return registrar(config, [e for p in paradas for e in _eventos_do_item(
        p.get("codigo"), "roteirizado", {**dados, "service_id": p.get("service_id")})], modo_teste=modo_teste)


def nao_aprontaram(config: dict | None, itens: list[dict], *, contexto: dict, modo_teste: bool = False) -> dict | None:
    """`itens` como separar_prontos devolve (codigo, service_id, situacao_stokki)."""
    return registrar(config, [e for p in itens for e in _eventos_do_item(
        p.get("codigo"), "nao_aprontou",
        {**contexto, "situacao_stokki": p.get("situacao_stokki"), "service_id": p.get("service_id")})],
        modo_teste=modo_teste)


def rota_enviada(config: dict | None, paradas: list[dict], *, vuupt_route_id: int, rascunho_id: int,
                 rota_nome: str, modo_teste: bool = False) -> dict | None:
    dados = {"vuupt_route_id": vuupt_route_id, "rascunho_id": rascunho_id, "rota_nome": rota_nome}
    return registrar(config, [e for p in paradas for e in _eventos_do_item(
        p.get("codigo"), "rota_enviada", {**dados, "service_id": p.get("service_id")})], modo_teste=modo_teste)
