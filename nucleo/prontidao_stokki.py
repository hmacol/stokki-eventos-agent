# -*- coding: utf-8 -*-
"""
nucleo/prontidao_stokki.py

Prontidão do pedido na Stokki pelo RETRATO que o pipeline grava no fim de cada rodada
(vigia_stokki_abertos, vigia/banco.py) -- só leitura, nunca chama a Stokki. Veio de
painel_agentes/planejamento_rotas.py (bloco "Em separação na Stokki" do pool, 19/10) e passou pra cá
no plano 1.2 do sistema novo, porque o envio do rascunho (rascunhos_rota.enviar_rascunho) e o corte das
22h (roteirizacao/incrementar_rotas.py) passaram a usar a mesma regra: a rota só sai com pedido liberado
pela Stokki; quem não aprontou volta ao pool e vira evento `nao_aprontou` no freshhub.

Chave = status_stokki como o pipeline grava (código da URL da Stokki; "Em espera" é literal da fonte
estacao_impressao). "Waiting for Carrier" ou ausente do retrato = pronto.
"""
import logging
from datetime import datetime
from pathlib import Path

from nucleo.normalizacao import normalizar_codigo

logger = logging.getLogger("nucleo.prontidao_stokki")

SITUACAO_STOKKI_NAO_PRONTA = {
    "Open": "Em aberto",
    "Separating": "Em separação",
    "Ready to Pack": "Em conferência",
    "On hold": "Em espera",
    "Em espera": "Em espera",
}


def _codigo_base(codigo: str) -> str:
    """'#PS-36327-R1' -> 'PS-36327': reentrega é o mesmo pedido na Stokki (mesma regra de
    planejamento_rotas._codigo_base / gerar_pdf_romaneios._codigo_base)."""
    normal = normalizar_codigo(codigo) or ""
    base, sep, sufixo = normal.rpartition("-R")
    return base if sep and sufixo.isdigit() else normal


def codigos_base(codigo: str | None) -> list[str]:
    """'code' da Vuupt pode agrupar pedidos por vírgula ('#PS-1, #PS-2'): um código base por pedido."""
    return [_codigo_base(c.strip()) for c in str(codigo or "").split(",") if c.strip()]


def situacao_nao_pronta(codigo: str | None, retrato: dict[str, str]) -> str | None:
    """Rótulo de SITUACAO_STOKKI_NAO_PRONTA quando o pedido ainda não está liberado; None quando está
    "Waiting for Carrier" ou não aparece no retrato. Código combinado: basta um ainda não pronto."""
    if not retrato:
        return None
    return next((SITUACAO_STOKKI_NAO_PRONTA[retrato[c]] for c in codigos_base(codigo)
                 if retrato.get(c) in SITUACAO_STOKKI_NAO_PRONTA), None)


def separar_prontos(itens: list[dict], retrato: dict[str, str], chave: str = "codigo") -> tuple[list[dict], list[dict]]:
    """(prontos, nao_prontos): os não prontos saem com `situacao_stokki` preenchido. `chave` é onde está o
    código: 'codigo' (parada de rascunho) ou 'code' (serviço da Vuupt / item do pool)."""
    prontos: list[dict] = []
    nao_prontos: list[dict] = []
    for item in itens:
        situacao = situacao_nao_pronta(item.get(chave), retrato)
        if situacao is None:
            prontos.append(item)
        else:
            nao_prontos.append({**item, "situacao_stokki": situacao})
    return prontos, nao_prontos


def anotar_situacao_stokki(pool: list[dict], retrato: dict[str, str]) -> None:
    """Põe `situacao_stokki` em cada item do pool da tela (None = pronto / fora do retrato)."""
    for p in pool:
        p["situacao_stokki"] = situacao_nao_pronta(p.get("codigo"), retrato)


def carregar_retrato_stokki(db_path: Path | None = None) -> tuple[dict[str, str], str | None]:
    """Retrato do que está aberto na Stokki, gravado pelo pipeline no fim de cada rodada. Devolve
    ({codigo_base: status_stokki}, "dd/mm HH:MM" da última listagem). Falha vira ({}, None): quem chama
    segue sem a marcação (ninguém é barrado sem retrato)."""
    try:
        from vigia import banco as vigia_banco
        conn = vigia_banco.conectar(db_path)
        try:
            por_codigo = {r["codigo"]: r["status_stokki"]
                          for r in conn.execute("SELECT codigo, status_stokki FROM vigia_stokki_abertos")}
            ultima = conn.execute("SELECT concluida_em FROM vigia_listagens ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        quando = None
        if ultima and ultima["concluida_em"]:
            quando = datetime.strptime(ultima["concluida_em"], vigia_banco.FMT).strftime("%d/%m %H:%M")
        return por_codigo, quando
    except Exception as e:  # noqa: BLE001 - retrato e so apoio
        logger.warning(f"Falha ao ler o retrato da Stokki (segue sem a marcação): {e}")
        return {}, None
