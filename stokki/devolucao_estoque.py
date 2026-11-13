# -*- coding: utf-8 -*-
"""Devolucao de estoque no cancelamento: regras puras de decisao (sem rede, sem banco).

Pedido cancelado depois da separacao deixa o saldo preso no endereco Virtual.
Aqui so se decide QUANTO devolver e PARA ONDE; quem le a Stokki e move e outro trecho.
Na duvida, nao mover e avisar a operacao.
"""
from datetime import datetime, timedelta

SISTEMA = ("virtual", "falta", "sobra", "ajuste")


def _e_sistema(endereco: str) -> bool:
    """Endereco de sistema ou vazio nunca e destino."""
    e = (endereco or "").strip().lower()
    return not e or e in SISTEMA


def _ref_pedido(referencia: str) -> str:
    """Referencia limpa (sem espaco, sem '#', minuscula) para comparar numero exato."""
    return (referencia or "").strip().lstrip("#").strip().lower()


def bloqueado_pelo_pedido(movimentos: list, id_stokki: int) -> float:
    """Soma dos Bloqueios Internos cuja referencia e exatamente #PS-<id> (PE-, outros pedidos
    e movimentos nao Internos nao contam)."""
    alvo = f"ps-{id_stokki}"
    return sum(m["quantidade"] for m in movimentos
               if m.get("tipo") == "Bloqueio" and m.get("movimento") == "Interno"
               and _ref_pedido(m.get("referencia")) == alvo)


def ja_devolvido(movimentos: list, qtd: float, cancelado_em: datetime) -> str:
    """'sim' se ha Desbloqueio Interno sem referencia a partir do minuto do cancelamento com a mesma
    qtd (tolerancia 0.001); 'duvida' se ha desses mas de outra qtd ou se nao ha data de cancelamento;
    'nao' se nao ha nenhum. Linha sem data conta como posterior (lado seguro)."""
    if cancelado_em is None:
        return "duvida"
    desbloqueios = [m for m in movimentos
                    if m.get("tipo") == "Desbloqueio" and m.get("movimento") == "Interno"
                    and not _ref_pedido(m.get("referencia")) and (m.get("quando") is None or m["quando"] >= cancelado_em)]
    if not desbloqueios:
        return "nao"
    if any(abs(m["quantidade"] - qtd) < 0.001 for m in desbloqueios):
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
    Contrato para quem executa: as partes da origem somam; se nao chegam a qtd, o resto vai como
    candidato extra. Os candidatos das regras 2 e 3 sao ALTERNATIVAS que levam o que falta: o
    executor acumula o que ja moveu e nunca passa de qtd no total. Se a soma movida ficar menor
    que qtd, o item e parcial."""
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
    """Cancelamento que ja existia na primeira leitura e tem 24 h ou mais (ou sem data): so avisar."""
    if not ja_estava:
        return False
    return cancelado_em is None or agora - cancelado_em >= timedelta(hours=24)
