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
from regras.embarcadores import _normalizar

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

# Embarcadores cujas entregas NÃO precisam ir acompanhadas de Nota
# Fiscal (pedido do Hugo, 13/08): a etapa da Stokki pula a geração do
# DANFE desses pedidos (a aba Documentos continua sendo olhada -- boleto
# etc). São os mesmos 3 embarcadores da CANHOTEIRA do romaneio
# (roteirizacao/gerar_pdf_romaneios.py::SENDERS_CANHOTEIRA). O casamento
# é pelo NOME do cliente na linha da listagem (normalizado, sem acento),
# não por ID -- o ID Stokki da Pedramoura não está mapeado.
EMBARCADORES_SEM_NF = ("PADRAO PURO", "QUATRO ESTRELAS", "PEDRAMOURA")


def _pedido_sem_nf(linha) -> bool:
    cliente = str(linha.get("client", "")) if isinstance(linha, dict) else ""
    nome = _normalizar(cliente)
    return bool(nome) and any(n in nome for n in EMBARCADORES_SEM_NF)


# Embarcadores que mandam boleto por E-MAIL (ver email_documentos.py::
# REMETENTES_EMBARCADORES). O boleto chega junto/DEPOIS da expedição,
# quando o pedido já saiu dos status em aberto (vira "Sent") -- então a
# DANFE dele nunca entraria no índice NF->pedido só com a seleção de
# abertos, e o boleto ficava preso em revisão manual (falha vista na
# rodada real de 11/08). Pra fechar esse buraco, a seleção também
# inclui os pedidos "Sent" mais recentes desses embarcadores.
EMBARCADORES_BOLETO_EMAIL: dict[str, str] = {
    "18": "LATICINIOS DOURADO - INDUSTRIA E COMERCIO LTDA",
    "48": "MARIA DOLORES INDUSTRIA E COMERCIO DE ALIMENTOS LTDA",
}
STATUS_EXPEDIDO = "Sent"


def _codigo_da_linha(linha) -> str | None:
    id_stokki = stokki_pedidos.extrair_id_da_linha(linha)
    return f"PS-{id_stokki}" if id_stokki else None


def descobrir_pedidos(config: dict) -> tuple[list[str], set[str]]:
    """
    Retorna (codigos, codigos_sem_nf):
      - codigos: lista de códigos PS-XXXXX a processar nesta execução --
        todos os pedidos em aberto dos embarcadores prioritários +
        todos os pedidos "Aguardando Transportador" do resto;
      - codigos_sem_nf: subconjunto cujos embarcadores não precisam de
        Nota Fiscal (EMBARCADORES_SEM_NF) -- pra etapa da Stokki pular
        a geração do DANFE desses pedidos.
    """
    sessao = StokkiSession(config)
    codigos_vistos: set[str] = set()
    codigos: list[str] = []
    codigos_sem_nf: set[str] = set()

    def _registrar(linha) -> None:
        codigo = _codigo_da_linha(linha)
        if codigo and codigo not in codigos_vistos:
            codigos_vistos.add(codigo)
            codigos.append(codigo)
            if _pedido_sem_nf(linha):
                codigos_sem_nf.add(codigo)

    for id_emb, nome_emb in EMBARCADORES_QUALQUER_STATUS.items():
        for status in STATUSES_EM_ABERTO:
            try:
                for linha in stokki_pedidos.iterar_todos_pedidos(
                    sessao, status=status, cliente=id_emb, pausa_entre_paginas=0.3
                ):
                    _registrar(linha)
            except Exception as e:
                logger.warning(f"Erro ao buscar {nome_emb!r} status={status!r}: {e}")
    logger.info(f"Embarcadores prioritários (documentos): {len(codigos)} pedido(s) em aberto.")

    n_antes = len(codigos)
    for linha in stokki_pedidos.iterar_todos_pedidos(sessao, status=STATUS_AGUARDANDO_TRANSPORTADOR):
        _registrar(linha)
    logger.info(f"Aguardando Transportador (outros embarcadores): "
               f"{len(codigos) - n_antes} pedido(s) adicionados.")

    n_antes = len(codigos)
    for codigo in descobrir_expedidos_recentes(sessao):
        if codigo not in codigos_vistos:
            codigos_vistos.add(codigo)
            codigos.append(codigo)
    logger.info(f"Expedidos recentes (embarcadores de boleto por e-mail): "
               f"{len(codigos) - n_antes} pedido(s) adicionados.")

    return codigos, codigos_sem_nf


def descobrir_expedidos_recentes(sessao: StokkiSession, limite_por_embarcador: int = 60) -> list[str]:
    """
    Os N pedidos "Sent" mais recentes de cada embarcador que manda
    boleto por e-mail (ver EMBARCADORES_BOLETO_EMAIL). Quem já tem a
    Nota Fiscal enviada/indexada é filtrado FORA aqui mesmo -- assim,
    em regime, só os expedidos novos do dia geram visita de página
    (os antigos não custam nada).
    """
    from fingerprint_documentos import ja_enviado_para_pedido

    codigos: list[str] = []
    for id_emb, nome_emb in EMBARCADORES_BOLETO_EMAIL.items():
        try:
            pagina = stokki_pedidos.listar_pedidos(
                sessao, status=STATUS_EXPEDIDO, cliente=id_emb,
                pagina=0, por_pagina=limite_por_embarcador,
                ordenar_coluna="1", ordenar_dir="desc",
            )
            for linha in pagina.get("aaData", []):
                codigo = _codigo_da_linha(linha)
                if codigo and not ja_enviado_para_pedido(codigo, "Nota Fiscal"):
                    codigos.append(codigo)
        except Exception as e:
            logger.warning(f"Erro ao buscar expedidos recentes de {nome_emb!r}: {e}")
    return codigos
