# -*- coding: utf-8 -*-
"""
Posicao DOCA: espera do recebimento por etapas (a conferencia lanca aqui,
a fila "Guardar" leva dali para a posicao final).
"""
from pathlib import Path

import wms
import wms_caixas

DOCA_AREA = "DOC1"
DOCA_POSICAO = "DOC1-P1"


def conectar(caminho: Path | None = None):
    return wms_caixas.conectar(caminho)


def e_doca(codigo: str | None) -> bool:
    return bool(codigo) and wms.normalizar_codigo(codigo) == DOCA_POSICAO


def temperatura_do_produto(conn, produto_id: int) -> str:
    """Cadastro; senao a temperatura da area onde ha mais saldo (sem a doca); senao ''."""
    row = conn.execute("SELECT temperatura FROM wms_produtos WHERE id = ?", (produto_id,)).fetchone()
    if row and row["temperatura"]:
        return row["temperatura"]
    row = conn.execute(
        "SELECT a.temperatura FROM wms_saldos s "
        "JOIN wms_posicoes p ON p.codigo = s.posicao JOIN wms_areas a ON a.codigo = p.area_codigo "
        "WHERE s.produto_id = ? AND s.quantidade > 0 AND a.codigo <> ? "
        "GROUP BY a.temperatura ORDER BY SUM(s.quantidade) DESC LIMIT 1",
        (produto_id, DOCA_AREA)).fetchone()
    return row["temperatura"] if row else ""


def garantir_doca(conn) -> str:
    """Cria (ou reativa) a area e a posicao da doca. Devolve o codigo da posicao."""
    area = wms.obter_area(conn, DOCA_AREA)
    if not area or not area["ativo"]:
        wms.criar_area(conn, DOCA_AREA, "DOCA")
    wms.criar_posicao(conn, DOCA_POSICAO)
    return DOCA_POSICAO


def _distancia(a, b):
    """Chave de proximidade entre duas posicoes da mesma area (menor = mais perto)."""
    if a["pallet"] is not None and b["pallet"] is not None:
        return (0, abs(a["pallet"] - b["pallet"]), 0)
    if a["estante"] is not None and b["estante"] is not None:
        de = abs(a["estante"] - b["estante"])
        dn = abs(a["nivel"] - b["nivel"])
        return (0, dn, 0) if de == 0 else (1, de, dn)
    return (2, 0, 0)


def sugerir_posicoes(conn, produto_id: int, lote: str, validade: str | None, limite: int = 3) -> list[dict]:
    """Ate `limite` posicoes para guardar o produto, com o motivo. So leitura.
    Ordem fixa: MESMO_LOTE, AO_LADO, EMBARCADOR, VAZIA."""
    lote, validade = lote or "", validade or ""
    temperatura = temperatura_do_produto(conn, produto_id)
    sql = ("SELECT p.codigo, p.area_codigo, p.estante, p.nivel, p.pallet FROM wms_posicoes p "
           "JOIN wms_areas a ON a.codigo = p.area_codigo "
           "WHERE p.ativo = 1 AND a.ativo = 1 AND p.codigo <> ?")
    args = [DOCA_POSICAO]
    if temperatura:
        sql += " AND a.temperatura = ?"
        args.append(temperatura)
    posicoes = {r["codigo"]: r for r in conn.execute(sql + " ORDER BY p.codigo", args)}

    ocupadas, do_produto, mesmo_lote = set(), set(), {}
    emb = conn.execute("SELECT embarcador_id FROM wms_produtos WHERE id = ?", (produto_id,)).fetchone()
    emb_id = emb["embarcador_id"] if emb else None
    por_area_emb = {}
    for s in conn.execute(
            "SELECT s.posicao, s.produto_id, s.lote, s.validade, s.quantidade, pr.embarcador_id "
            "FROM wms_saldos s JOIN wms_produtos pr ON pr.id = s.produto_id WHERE s.quantidade > 0"):
        ocupadas.add(s["posicao"])
        pos = posicoes.get(s["posicao"])
        if not pos:
            continue
        if s["produto_id"] == produto_id:
            do_produto.add(s["posicao"])
            if s["lote"] == lote and s["validade"] == validade:
                mesmo_lote[s["posicao"]] = mesmo_lote.get(s["posicao"], 0) + s["quantidade"]
        if emb_id is not None and s["embarcador_id"] == emb_id:
            por_area_emb.setdefault(pos["area_codigo"], set()).add(s["posicao"])

    vazias = [p for c, p in posicoes.items() if c not in ocupadas]
    resultado, usadas = [], set()

    def adicionar(codigo, criterio, motivo):
        if codigo not in usadas:
            usadas.add(codigo)
            resultado.append({"posicao": codigo, "criterio": criterio, "motivo": motivo})

    for codigo in sorted(mesmo_lote):
        qtd = mesmo_lote[codigo]
        motivo = (f"Já tem este produto aqui (lote {lote}, {qtd:g} UN)" if lote
                  else f"Já tem este produto aqui ({qtd:g} UN)")
        adicionar(codigo, "MESMO_LOTE", motivo)

    candidatas = []
    for v in vazias:
        dists = [_distancia(v, posicoes[c]) for c in do_produto
                 if posicoes[c]["area_codigo"] == v["area_codigo"]]
        if dists:
            candidatas.append((min(dists), v["codigo"]))
    for _, codigo in sorted(candidatas):
        adicionar(codigo, "AO_LADO", "Ao lado do mesmo produto")

    por_emb = sorted((v for v in vazias if v["area_codigo"] in por_area_emb),
                     key=lambda v: (-len(por_area_emb[v["area_codigo"]]), v["codigo"]))
    for v in por_emb:
        adicionar(v["codigo"], "EMBARCADOR", "Perto de outros produtos do mesmo embarcador")

    for v in vazias:
        adicionar(v["codigo"], "VAZIA", "Posição vazia na área certa")

    return resultado[:limite]
