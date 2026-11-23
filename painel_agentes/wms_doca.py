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
