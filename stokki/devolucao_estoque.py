# -*- coding: utf-8 -*-
"""Devolucao de estoque no cancelamento: regras puras de decisao (sem rede) e a fila no banco.

Pedido cancelado depois da separacao deixa o saldo preso no endereco Virtual.
Aqui so se decide QUANTO devolver e PARA ONDE; quem le a Stokki e move e outro trecho.
Na duvida, nao mover e avisar a operacao.
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

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


# --- Fila no banco (dados.db): o gancho do cancelamento registra, o robo processa ---

STATUS = ("PENDENTE", "FEITA", "PARCIAL", "NADA", "JA_DEVOLVIDO", "ANTIGO", "DUVIDA", "FALHA")


def ligado(config: dict) -> bool:
    """Chave stokki.devolucao_cancelamento.ativo; ausente = desligada."""
    try:
        return bool(((config or {}).get("stokki") or {}).get("devolucao_cancelamento", {}).get("ativo"))
    except AttributeError:
        return False


def garantir_tabela(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stokki_devolucoes ("
        "id_stokki INTEGER PRIMARY KEY, origem TEXT, ja_estava INTEGER, status TEXT, "
        "tentativas INTEGER DEFAULT 0, itens_json TEXT, erro TEXT, criado_em TEXT, atualizado_em TEXT)")


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def registrar(id_stokki: int, origem: str, config: dict, ja_estava: bool = False, conn=None) -> bool:
    """Poe o pedido cancelado na fila (PENDENTE), uma vez so (INSERT OR IGNORE). Desligado: False sem
    tocar o banco. Nunca levanta: falha vira log e False, o cancelamento segue."""
    if not ligado(config):
        return False
    propria = conn is None
    try:
        if propria:
            from stokki.sessao_uso import DB_PATH
            conn = sqlite3.connect(DB_PATH, timeout=30)
        garantir_tabela(conn)
        agora = _agora()
        cur = conn.execute(
            "INSERT OR IGNORE INTO stokki_devolucoes (id_stokki, origem, ja_estava, status, tentativas, criado_em, atualizado_em) "
            "VALUES (?, ?, ?, 'PENDENTE', 0, ?, ?)", (int(id_stokki), origem, int(bool(ja_estava)), agora, agora))
        conn.commit()
        return cur.rowcount == 1
    except Exception as e:  # noqa: BLE001 -- a fila nunca pode derrubar o cancelamento
        logger.warning(f"devolucao_estoque: nao registrou PS-{id_stokki} na fila: {e}")
        return False
    finally:
        if propria and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def pendentes(conn) -> list:
    """Linhas PENDENTE (mais antigas primeiro) como dict, com `itens` ja desserializado."""
    garantir_tabela(conn)
    cur = conn.execute("SELECT * FROM stokki_devolucoes WHERE status = 'PENDENTE' ORDER BY criado_em, id_stokki")
    nomes = [c[0] for c in cur.description]
    saida = []
    for linha in cur.fetchall():
        d = dict(zip(nomes, tuple(linha)))
        d["itens"] = json.loads(d.get("itens_json") or "[]")
        saida.append(d)
    return saida


def gravar(conn, id_stokki: int, *, status=None, itens=None, erro=None, mais_uma_tentativa=False) -> None:
    """Atualiza so o que foi informado; sempre carimba atualizado_em."""
    garantir_tabela(conn)
    campos, valores = ["atualizado_em = ?"], [_agora()]
    if status is not None:
        campos.append("status = ?")
        valores.append(status)
    if itens is not None:
        campos.append("itens_json = ?")
        valores.append(json.dumps(itens, ensure_ascii=False))
    if erro is not None:
        campos.append("erro = ?")
        valores.append(erro)
    if mais_uma_tentativa:
        campos.append("tentativas = COALESCE(tentativas, 0) + 1")
    conn.execute(f"UPDATE stokki_devolucoes SET {', '.join(campos)} WHERE id_stokki = ?", (*valores, int(id_stokki)))
    conn.commit()
