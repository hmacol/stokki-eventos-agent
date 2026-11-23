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


def garantir_doca(conn) -> str:
    """Cria (ou reativa) a area e a posicao da doca. Devolve o codigo da posicao."""
    area = wms.obter_area(conn, DOCA_AREA)
    if not area or not area["ativo"]:
        wms.criar_area(conn, DOCA_AREA, "DOCA")
    wms.criar_posicao(conn, DOCA_POSICAO)
    return DOCA_POSICAO
