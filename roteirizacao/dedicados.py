# -*- coding: utf-8 -*-
"""
Pedidos dedicados na roteirizacao (Hugo, 23/09/2026): transporte cotado a
parte -- ficam fora da rota compartilhada (criar_rotas_diarias e
incrementar_rotas) e nao geram e-mail de area nao atendida. O pool do
planejamento usa dedicados_por_servico() pro chip "Dedicado - R$ X".
A marca mora em pedidos_dedicados.py (raiz).
"""
import logging
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402

logger = logging.getLogger(__name__)


def carregar_dedicados() -> dict[str, dict]:
    """{codigo PS-NNNNN: linha} dos dedicados ativos. Falha -> {} com WARNING
    (a roteirizacao segue sem a marca; nunca derruba o job por isso)."""
    try:
        conn = pedidos_dedicados.conectar()
        try:
            return pedidos_dedicados.ativos_por_codigo(conn)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"nao carregou pedidos dedicados ({e}); seguindo sem a marca")
        return {}


def separar_dedicados(servicos: list[dict]) -> tuple[list[dict], list[dict]]:
    """(fora da marca, dedicados). Servico com mais de um codigo sai
    inteiro se qualquer um for dedicado. Loga os que saem."""
    restantes, fora = pedidos_dedicados.filtrar_dedicados(servicos, carregar_dedicados())
    if fora:
        logger.info(f"{len(fora)} pedido(s) dedicado(s) fora da rota compartilhada: {[s.get('code') for s in fora]}")
    return restantes, fora


def dedicados_por_servico(servicos: list[dict]) -> dict[int, dict]:
    """{service_id: {valor, valor_total, n_grupo}} pros servicos marcados."""
    ativos = carregar_dedicados()
    if not ativos:
        return {}
    n_por_grupo: dict[str, int] = {}
    for d in ativos.values():
        n_por_grupo[d["grupo_id"]] = n_por_grupo.get(d["grupo_id"], 0) + 1
    mapa: dict[int, dict] = {}
    for s in servicos:
        for c in pedidos_dedicados.codigos_do_servico(s):
            if c in ativos:
                d = ativos[c]
                mapa[s["id"]] = {"valor": d["valor"], "valor_total": d["valor_total_grupo"], "n_grupo": n_por_grupo[d["grupo_id"]]}
                break
    return mapa
