# -*- coding: utf-8 -*-
"""
painel_agentes/contadores_menu.py

Os números que aparecem ao lado dos itens do menu lateral (pedido do
Hugo, 11/09/2026): quantas coisas estão esperando ação em Atendimento,
Pedágios, Pedidos Parados e na Fila de ação da Torre. A ideia é não
precisar abrir a tela pra descobrir se tem algo lá.

REGRA QUE MANDA AQUI: o menu lateral está em TODA página do painel, e o
badge NUNCA pode deixar uma página mais lenta. Por isso:

- A contagem não é calculada na renderização da página. O menu sobe sem
  número nenhum e o JS busca /api/sidebar/contadores depois do load.
- Fonte barata (SQLite local: Atendimento, Pedágios) é lida ao vivo a
  cada chamada -- são COUNTs em tabela pequena.
- Fonte cara (Torre e Pedidos Parados falam com VUUPT/Fresh Hub e levam
  segundos) NUNCA é calculada dentro do request: devolve o último valor
  conhecido e, se ele estiver velho, manda UMA thread renovar em segundo
  plano (single-flight). Na pior hipótese o badge aparece um ciclo
  depois -- e o número nunca segura a resposta.
- Falha numa fonte não derruba as outras nem a chamada: aquele badge
  simplesmente não vem.

Quem já paga a conta alimenta o cache de graça: a tela da Torre publica
o tamanho da Fila de ação a cada carga dela (torre_controle.
snapshot_fila_acao), e listar_com_classificacao tem cache próprio de 60 s
com single-flight. Então ter a tela aberta não dobra o trabalho.
"""
import logging
import sys
import threading
import time
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import pedidos_parados_triagem  # noqa: E402
import torre_controle  # noqa: E402

logger = logging.getLogger("painel.contadores")

# Idade máxima de um valor caro antes de mandar renovar em segundo plano.
# 3 min: a Fila de ação e os pedidos parados não mudam de minuto a minuto,
# e é o que evita martelar a VUUPT/Fresh Hub com N abas abertas.
TTL_CARO_SEG = 180

# A coleta da Torre é a mais cara de todas -- medida em 11/09, ~58 s
# (rotas + serviços + backlog na VUUPT). Com 3 min, teria uma thread
# pesada rodando um terço do tempo sempre que ninguém estivesse com a
# tela aberta. Pra um indicador de "tem ocorrência ou não?", 5 min basta.
TTL_POR_CONTADOR = {"torre": 300}

# Quem vê cada contador -- espelha o `niveis=` do @requer_auth da tela
# correspondente em painel_agentes.py. Contador de tela que o usuário não
# pode abrir não é calculado (nem vaza pra ele).
NIVEIS_POR_CONTADOR = {
    "torre": ("total", "operador", "leitura"),
    "pedidos_parados": ("total", "operador", "leitura"),
    "pedagios": ("total", "operador", "leitura"),
    "canhotos": ("total", "operador", "leitura"),
    "atendimento": ("total", "operador", "atendimento"),
}

_snapshots: dict[str, dict] = {}   # chave -> {"quando": monotonic, "valor": dict|None}
_em_voo: set[str] = set()
_lock = threading.Lock()


# ── Cache dos contadores caros ────────────────────────────────────────────────

def _renovar(chave: str, calcular) -> None:
    """Roda fora do request. Grava "quando" mesmo em caso de erro: sem
    isso, uma fonte quebrada faria nascer uma thread a cada chamada."""
    valor = None
    try:
        valor = calcular()
    except Exception as e:
        logger.warning(f"[contadores] Falha ao renovar '{chave}': {e}")
    with _lock:
        anterior = _snapshots.get(chave, {}).get("valor")
        _snapshots[chave] = {"quando": time.monotonic(),
                             "valor": valor if valor is not None else anterior}
        _em_voo.discard(chave)


def _caro(chave: str, calcular, ja_fresco=None) -> dict | None:
    """Último valor conhecido, sem nunca bloquear. `ja_fresco` é um
    atalho opcional: se devolver algo, é porque outro caminho (a própria
    tela) acabou de calcular e não há o que renovar."""
    if ja_fresco is not None:
        pronto = ja_fresco()
        if pronto is not None:
            return pronto

    ttl = TTL_POR_CONTADOR.get(chave, TTL_CARO_SEG)
    with _lock:
        snap = _snapshots.get(chave)
        velho = not snap or time.monotonic() - snap["quando"] >= ttl
        if velho and chave not in _em_voo:
            _em_voo.add(chave)
            disparar = True
        else:
            disparar = False
        valor = snap["valor"] if snap else None

    if disparar:
        threading.Thread(target=_renovar, args=(chave, calcular),
                         name=f"contador-{chave}", daemon=True).start()
    return valor


# ── Fontes ────────────────────────────────────────────────────────────────────

def _contar_torre() -> dict:
    """Fila de ação da Torre de hoje. Caro: coleta ao vivo na VUUPT."""
    torre_controle.buscar_dados_torre(date.today())
    return torre_controle.snapshot_fila_acao(date.today()) or {"qtd": 0, "criticas": 0}


def _torre_ja_coletada():
    """Se a tela da Torre carregou faz pouco, o número já está publicado
    -- aproveita em vez de coletar de novo."""
    snap = torre_controle.snapshot_fila_acao(date.today())
    if snap and snap["idade_seg"] < TTL_POR_CONTADOR["torre"]:
        return snap
    return None


def _contar_pedidos_parados() -> dict:
    """Pedidos parados que PEDEM AÇÃO: ainda sem classificação, ou
    marcados como revisão urgente (classificados como entregues e
    registrados como parados de novo depois disso). O total puro nunca
    zeraria e o badge viraria enfeite. Caro: Fresh Hub + VUUPT por
    dentro (com cache próprio de 60 s)."""
    lista = pedidos_parados_triagem.listar_com_classificacao()
    urgentes = sum(1 for p in lista if p["revisao_urgente"])
    pendentes = sum(1 for p in lista if p["revisao_urgente"] or not p["classificacao"])
    return {"qtd": pendentes, "criticas": urgentes}


def _contar_pedagios() -> dict:
    """Pedágios aguardando aprovação. Barato: COUNT no SQLite do núcleo."""
    from nucleo import banco as nucleo_banco, operacao as nucleo_operacao
    conn = nucleo_banco.conectar()
    try:
        por_status = nucleo_operacao.contar_pedagios_painel(conn)
    finally:
        conn.close()
    return {"qtd": por_status.get(nucleo_banco.PEDAGIO_PENDENTE, 0), "criticas": 0}


def _contar_canhotos() -> dict:
    """Canhotos que a conferência automática reprovou e ainda esperam a
    palavra do humano (validado_por='IA'; revisado vira 'HUMANO'). Com a
    validação desligada isso é sempre 0 e o badge não aparece. Barato:
    COUNT no SQLite do núcleo."""
    from nucleo import banco as nucleo_banco
    conn = nucleo_banco.conectar()
    try:
        qtd = conn.execute("SELECT COUNT(*) FROM nucleo_comprovantes "
                           "WHERE resultado_validacao = 'REPROVADO' AND validado_por = 'IA'").fetchone()[0]
    finally:
        conn.close()
    return {"qtd": qtd, "criticas": qtd}


def _contar_atendimento() -> dict:
    """Chamados esperando a equipe (na fila + aguardando a Freshlog).
    Barato: COUNT no SQLite do portal. Sem "crítico": ter alguém na fila
    é o normal do dia, e badge vermelho o tempo todo vira ruído."""
    import chamados as ch  # portal_cliente/chamados.py
    conn = ch.conectar()
    try:
        c = ch.contagens_fila(conn)
    finally:
        conn.close()
    return {"qtd": c["fila"], "criticas": 0}


# ── Entrada ───────────────────────────────────────────────────────────────────

def contadores(nivel_acesso: str) -> dict:
    """{"torre": {"qtd": 3, "criticas": 1}, ...} só com o que esse nível
    pode abrir. Contador que falhou ou que ainda não tem leitura fica
    de fora -- o menu simplesmente não desenha o badge."""
    fontes = {
        "torre": lambda: _caro("torre", _contar_torre, ja_fresco=_torre_ja_coletada),
        "pedidos_parados": lambda: _caro("pedidos_parados", _contar_pedidos_parados),
        "pedagios": _contar_pedagios,
        "canhotos": _contar_canhotos,
        "atendimento": _contar_atendimento,
    }
    resultado = {}
    for chave, fonte in fontes.items():
        if nivel_acesso not in NIVEIS_POR_CONTADOR[chave]:
            continue
        try:
            valor = fonte()
        except Exception as e:
            logger.warning(f"[contadores] Falha em '{chave}': {e}")
            continue
        if valor is not None:
            resultado[chave] = {"qtd": int(valor["qtd"]), "criticas": int(valor.get("criticas") or 0)}
    return resultado
