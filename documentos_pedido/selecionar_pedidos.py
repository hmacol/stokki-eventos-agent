# -*- coding: utf-8 -*-
"""
selecionar_pedidos.py

Decide quais pedidos (PS-XXXXX) o agente de documentos vai buscar na
Stokki -- pedido do Hugo, 10/08: "seguir a mesma lógica da subida pro
vuupt": Padrão Puro, Jersey Vale e Dourado entram em QUALQUER etapa
aberta da Stokki; todo o resto só quando estiver "Aguardando
Transportador".

Mesma regra (e mesmos IDs) de EMBARCADORES_IMPORTAR_ABERTOS em
pipeline.py -- duplicada aqui (em vez de importada) pra não arrastar
os imports pesados do pipeline inteiro (geocodificação, VUUPT, etc)
só pra pegar 2 constantes. Se a lista de embarcadores prioritários
mudar lá, precisa mudar aqui também.
"""
import logging

from stokki.auth import StokkiSession
from stokki import pedidos as stokki_pedidos

logger = logging.getLogger(__name__)

STATUS_AGUARDANDO_TRANSPORTADOR = "Waiting for Carrier"
STATUSES_EM_ABERTO = [
    STATUS_AGUARDANDO_TRANSPORTADOR,
    "Open",
    "Separating",
    "Ready to Pack",
    "On hold",
]

# {id_stokki: nome_legivel} -- mesmos 3 embarcadores de
# pipeline.py::EMBARCADORES_IMPORTAR_ABERTOS.
EMBARCADORES_QUALQUER_STATUS: dict[str, str] = {
    "23": "PADRAO PURO LTDA",
    "18": "LATICINIOS DOURADO - INDUSTRIA E COMERCIO LTDA",
    "79": "JERSEY VALE AGROINDUSTRIAL LTDA",
}


def _codigo_da_linha(linha) -> str | None:
    id_stokki = stokki_pedidos.extrair_id_da_linha(linha)
    return f"PS-{id_stokki}" if id_stokki else None


def descobrir_pedidos(config: dict) -> list[str]:
    """
    Retorna a lista de códigos PS-XXXXX a processar nesta execução:
    todos os pedidos em aberto dos embarcadores prioritários +
    todos os pedidos "Aguardando Transportador" do resto.
    """
    sessao = StokkiSession(config)
    codigos_vistos: set[str] = set()
    codigos: list[str] = []

    for id_emb, nome_emb in EMBARCADORES_QUALQUER_STATUS.items():
        for status in STATUSES_EM_ABERTO:
            try:
                for linha in stokki_pedidos.iterar_todos_pedidos(
                    sessao, status=status, cliente=id_emb, pausa_entre_paginas=0.3
                ):
                    codigo = _codigo_da_linha(linha)
                    if codigo and codigo not in codigos_vistos:
                        codigos_vistos.add(codigo)
                        codigos.append(codigo)
            except Exception as e:
                logger.warning(f"Erro ao buscar {nome_emb!r} status={status!r}: {e}")
    logger.info(f"Embarcadores prioritários (documentos): {len(codigos)} pedido(s) em aberto.")

    n_antes = len(codigos)
    for linha in stokki_pedidos.iterar_todos_pedidos(sessao, status=STATUS_AGUARDANDO_TRANSPORTADOR):
        codigo = _codigo_da_linha(linha)
        if codigo and codigo not in codigos_vistos:
            codigos_vistos.add(codigo)
            codigos.append(codigo)
    logger.info(f"Aguardando Transportador (outros embarcadores): "
               f"{len(codigos) - n_antes} pedido(s) adicionados.")

    return codigos
