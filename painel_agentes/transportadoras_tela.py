# -*- coding: utf-8 -*-
"""
painel_agentes/transportadoras_tela.py

Dados da tela /transportadoras (23/10): lista, aba de pendencias e ficha.
Quem grava e valida e transportadoras_cadastro / consulta_transportadora;
aqui so se monta o que a tela mostra.
"""
from datetime import date, datetime

import consulta_transportadora as ct
import transportadoras_cadastro as tc

# Rotulos e tipo gravado de cada botao de resposta (os mesmos do portal).
RESPOSTAS = (
    ("RETIRADA", "A transportadora vem retirar no galpão"),
    ("TERCEIROS", "Levar até a transportadora (redespacho)"),
    ("ENTREGA", "Entregar no endereço do pedido"),
)


def conectar():
    """Le DB_PATH na hora da chamada (conectar_consultas congela o default na definicao)."""
    return ct.conectar_consultas(tc.DB_PATH)


def contar_pendentes() -> int:
    conn = conectar()
    try:
        return conn.execute("SELECT COUNT(*) FROM transportadoras WHERE situacao='PENDENTE'").fetchone()[0]
    finally:
        conn.close()


def _dias(iso: str | None, hoje: date) -> int:
    try:
        return max(0, (hoje - datetime.fromisoformat(iso).date()).days)
    except (TypeError, ValueError):
        return 0


def _resumo_pendente(conn, t: dict, hoje: date) -> dict:
    parados = ct.pedidos_parados(conn, t["id"])
    consultas = [c for c in ct.consultas_abertas(conn) if c["transportadora_id"] == t["id"]]
    desde = min([p["desde"] for p in parados if p.get("desde")] or [t.get("criado_em") or ""])
    return {
        "dias_parado": _dias(desde, hoje),
        "n_parados": len(parados),
        "embarcadores": [ct.nome_embarcador(conn, c["embarcador_cnpj"]) for c in consultas],
    }


def montar_lista(conn, aba: str, busca: str = "", tipo: str = "", situacao: str = "", hoje: date | None = None) -> list[dict]:
    hoje = hoje or date.today()
    if aba == "pendentes":
        linhas = tc.listar(conn, "PENDENTE", busca)
    else:
        linhas = tc.listar(conn, situacao or None, busca)
        if tipo:
            linhas = [l for l in linhas if l["tipo"] == tipo]
    if aba == "pendentes":
        for l in linhas:
            l.update(_resumo_pendente(conn, l, hoje))
        linhas.sort(key=lambda l: -l["dias_parado"])
    return linhas


def ficha(conn, tid: int) -> dict | None:
    t = tc.buscar(conn, tid)
    if t is None:
        return None
    t["historico"] = tc.historico(conn, tid)
    t["parados"] = [{"pedido": p["pedido"], "desde": p["desde"],
                     "embarcador": ct.nome_embarcador(conn, p["embarcador_cnpj"]),
                     "destinatario": (p["resumo"] or {}).get("destinatario") or "",
                     "cidade": (p["resumo"] or {}).get("cidade") or ""}
                    for p in ct.pedidos_parados(conn, tid)]
    return t


def so_campos(body: dict) -> dict:
    """Apenas os campos do cadastro; o resto do corpo e ignorado."""
    return {c: body[c] for c in tc.CAMPOS_EDITAVEIS if c in body}
