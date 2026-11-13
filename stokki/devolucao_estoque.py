# -*- coding: utf-8 -*-
"""Devolucao de estoque no cancelamento: regras puras de decisao (sem rede, sem banco).

Pedido cancelado depois da separacao deixa o saldo preso no endereco Virtual.
Aqui so se decide QUANTO devolver e PARA ONDE; quem le a Stokki e move e outro trecho.
Na duvida, nao mover e avisar a operacao.
"""
from datetime import datetime, timedelta

SISTEMA = ("virtual", "falta", "sobra", "ajuste")


def _e_sistema(endereco: str) -> bool:
    return (endereco or "").strip().lower() in SISTEMA


def _ref_pedido(referencia: str) -> str:
    """Referencia limpa (sem espaco, sem '#', minuscula) para comparar numero exato."""
    return (referencia or "").strip().lstrip("#").strip().lower()


def bloqueado_pelo_pedido(movimentos: list, id_stokki: int) -> float:
    """Soma dos Bloqueios cuja referencia e exatamente #PS-<id> (PE- e outros pedidos nao contam)."""
    alvo = f"ps-{id_stokki}"
    return sum(m["quantidade"] for m in movimentos
               if m.get("tipo") == "Bloqueio" and _ref_pedido(m.get("referencia")) == alvo)


def ja_devolvido(movimentos: list, qtd: float, cancelado_em: datetime) -> str:
    """'sim' se ha Desbloqueio Interno sem referencia depois do cancelamento com a mesma qtd;
    'duvida' se ha desses mas de outra qtd; 'nao' se nao ha nenhum."""
    desbloqueios = [m for m in movimentos
                    if m.get("tipo") == "Desbloqueio" and m.get("movimento") == "Interno"
                    and not _ref_pedido(m.get("referencia")) and m["quando"] > cancelado_em]
    if not desbloqueios:
        return "nao"
    if any(m["quantidade"] == qtd for m in desbloqueios):
        return "sim"
    return "duvida"


def quanto_mover(bloqueado: float, oferecido: float) -> float:
    """Nunca mais que o Bloqueio do pedido nem mais que a tela oferece; nunca negativo."""
    return max(0, min(bloqueado, oferecido))


def _por_estoque(saldo: list) -> list:
    """Enderecos fisicos com estoque, do maior para o menor (empate: ordem alfabetica)."""
    fisicos = [s for s in saldo if not _e_sistema(s["endereco"]) and s["estoque"] > 0]
    return [s["endereco"] for s in sorted(fisicos, key=lambda s: (-s["estoque"], s["endereco"]))]


def escolher_destinos(qtd: float, origens: list, saldo_produto: list, saldo_embarcador: list) -> list:
    """Candidatos (endereco, qtd) em ordem de preferencia: origem da separacao, maior saldo do
    produto, maior saldo do embarcador. Sem repetir endereco e sem endereco de sistema.
    Se as partes da origem nao somam qtd, o resto vai para o melhor endereco das regras seguintes."""
    saldos = _por_estoque(saldo_produto) + [e for e in _por_estoque(saldo_embarcador)
                                           if e not in _por_estoque(saldo_produto)]
    cands, vistos, restante = [], set(), qtd
    for endereco, parte in origens:
        chave = endereco.strip().lower()
        if _e_sistema(endereco) or chave in vistos or restante <= 0 or parte <= 0:
            continue
        parte = min(parte, restante)
        cands.append((endereco, parte))
        vistos.add(chave)
        restante -= parte
    if cands and restante > 0:
        resto_para = next((e for e in saldos if e.strip().lower() not in vistos), None)
        if resto_para:
            cands.append((resto_para, restante))
            vistos.add(resto_para.strip().lower())
    for endereco in saldos:
        if endereco.strip().lower() not in vistos:
            cands.append((endereco, qtd))
            vistos.add(endereco.strip().lower())
    return cands


def cancelamento_antigo(cancelado_em, agora: datetime, ja_estava: bool) -> bool:
    """Cancelamento que ja existia na primeira leitura e tem mais de 24 h (ou sem data): so avisar."""
    if not ja_estava:
        return False
    return cancelado_em is None or agora - cancelado_em > timedelta(hours=24)
