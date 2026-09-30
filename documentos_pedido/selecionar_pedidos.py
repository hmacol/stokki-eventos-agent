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
import re

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

# A listagem de pedidos da Stokki devolve o nome do cliente TRUNCADO
# em ~15 caracteres no campo 'client' ("LATICINIOS DOUR ...", "COMERCIO
# DE CER ..."), mas o ID interno vem junto, sem truncar
# ("<span class="text-muted">#stkkc-18</span>") -- casar por esse ID é
# a forma correta e à prova de truncamento (achado real, 17/08: ver
# EMBARCADORES_SEM_NF_IDS e EMBARCADORES_DANFE_SOMENTE_EMAIL_IDS abaixo).
_PADRAO_STKKC_ID = re.compile(r"#stkkc-(\d+)")


def _stkkc_id_da_linha(linha) -> str | None:
    cliente = str(linha.get("client", "")) if isinstance(linha, dict) else ""
    m = _PADRAO_STKKC_ID.search(cliente)
    return m.group(1) if m else None


# Embarcadores cujas entregas NÃO precisam ir acompanhadas de Nota
# Fiscal (pedido do Hugo, 13/08): a etapa da Stokki pula a geração do
# DANFE desses pedidos (a aba Documentos continua sendo olhada -- boleto
# etc). São (parte d)os mesmos embarcadores da CANHOTEIRA do romaneio
# (roteirizacao/gerar_pdf_romaneios.py::SENDERS_CANHOTEIRA) -- só que
# ESSA lista aqui é especificamente sobre "vale a pena buscar a NF-e na
# Stokki", não sobre "a entrega precisa do canhoto da NF" (são coisas
# diferentes: a prova de entrega deles continua sendo a canhoteira,
# ver SENDERS_SEM_NF em validar_checklists.py e gerar_pdf_romaneios.py,
# que NÃO mudam aqui).
#
# Quatro Estrelas (98) tirada dessa lista em 21/08 (confirmado com o
# Hugo): ela EMITE NF-e normalmente e ela fica anexada no pedido da
# Stokki -- só não buscávamos porque a entrega dela usa canhoteira, não
# porque faltasse o documento. Buscar agora alimenta o campo NF do
# resumo de planejamento (planejamento_rotas.py::numero_nf) sem afetar
# a validação de checklist nem a canhoteira do romaneio.
#
# Conferido por ID (ver _stkkc_id_da_linha), não por nome -- achado
# real, 17/08: "QUATRO ESTRELAS" só aparece no MEIO do nome completo
# do cliente na Stokki ("COMERCIO DE CEREAIS QUATRO ESTRELAS LTDA"),
# nunca sobrevive ao truncamento -- checado contra 786 pedidos reais
# dela, 0 reconhecidos até a correção pra ID em 17/08 (Padrão Puro e
# Pedramoura "funcionavam" só por coincidência, por serem o COMEÇO do
# nome completo deles). IDs tirados de interno.stkkc_id (dados.db) --
# mesmos valores usados em pipeline.py::EMBARCADORES_IMPORTAR_ABERTOS.
EMBARCADORES_SEM_NF_IDS = {"23", "96"}  # Padrão Puro, Pedramoura
EMBARCADORES_SEM_NF = ("PADRAO PURO", "PEDRAMOURA")


def _pedido_sem_nf(linha) -> bool:
    if _stkkc_id_da_linha(linha) in EMBARCADORES_SEM_NF_IDS:
        return True
    cliente = str(linha.get("client", "")) if isinstance(linha, dict) else ""
    nome = _normalizar(cliente)
    return bool(nome) and any(n in nome for n in EMBARCADORES_SEM_NF)


# Embarcadores cuja DANFE NUNCA pode ser gerada a partir do XML anexado
# no pedido da Stokki (pedido do Hugo, 17/08): a Dourado e a Muai tiveram
# caso de XML errado/divergente anexado no pedido -- a DANFE confiável
# só pode vir por e-mail. Diferente de EMBARCADORES_SEM_NF: esses DOIS
# continuam PRECISANDO de Nota Fiscal, só não pela Stokki -- a falta
# dela continua contando como pendência no romaneio (não é tratada como
# "nf_dispensada" em roteirizacao/gerar_pdf_romaneios.py).
#
# A Dourado (id 18, mesmo de EMBARCADORES_QUALQUER_STATUS) é conferida
# por ID -- checado contra 692 pedidos reais dela, 0 reconhecidos antes
# da correção pra ID, 692/692 depois. A Muai ainda não tem ID Stokki
# confirmado (sem pedido real até 17/08 pra conferir) -- fica no
# fallback por nome, sujeito ao mesmo risco de truncamento até isso
# ser confirmado com um pedido real dela.
#
# Fruta Fina (id 94) ficou aqui de 20/08 a 10/09 ("NF só por e-mail").
# Tirada em 10/09 (aprovado pelo Hugo): a "DANFE errada" que motivou o
# bloqueio era o XML PLACEHOLDER da Stokki (ver docstring de
# stokki_documentos.py), e o e-mail nunca chegou (0 e-mails de qualquer
# endereço dela em 45 dias) -- ela ficou 3 semanas sem NF nenhuma.
# Agora a DANFE dela sai do XML real anexado na aba Documentos
# (gerar_danfes), e o e-mail continua só como reforço.
EMBARCADORES_DANFE_SOMENTE_EMAIL_IDS = {"18"}  # Dourado
EMBARCADORES_DANFE_SOMENTE_EMAIL = ("MUAI",)   # sem ID mapeado ainda


def _pedido_danfe_bloqueada(linha) -> bool:
    if _stkkc_id_da_linha(linha) in EMBARCADORES_DANFE_SOMENTE_EMAIL_IDS:
        return True
    cliente = str(linha.get("client", "")) if isinstance(linha, dict) else ""
    nome = _normalizar(cliente)
    return bool(nome) and any(n in nome for n in EMBARCADORES_DANFE_SOMENTE_EMAIL)


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
    "79": "JERSEY VALE AGROINDUSTRIAL LTDA",  # pedido do Hugo, 20/08 -- boleto sempre por e-mail
}
STATUS_EXPEDIDO = "Sent"


def _codigo_da_linha(linha) -> str | None:
    id_stokki = stokki_pedidos.extrair_id_da_linha(linha)
    return f"PS-{id_stokki}" if id_stokki else None


def descobrir_pedidos(config: dict) -> tuple[list[str], set[str], set[str]]:
    """
    Retorna (codigos, codigos_sem_nf, codigos_danfe_somente_email):
      - codigos: lista de códigos PS-XXXXX a processar nesta execução --
        todos os pedidos em aberto dos embarcadores prioritários +
        todos os pedidos "Aguardando Transportador" do resto;
      - codigos_sem_nf: subconjunto cujos embarcadores não precisam de
        Nota Fiscal (EMBARCADORES_SEM_NF) -- pra etapa da Stokki pular
        a geração do DANFE desses pedidos;
      - codigos_danfe_somente_email: subconjunto cujos embarcadores
        PRECISAM de Nota Fiscal, mas nunca gerada da Stokki
        (EMBARCADORES_DANFE_SOMENTE_EMAIL) -- a etapa da Stokki também
        pula a geração do DANFE desses pedidos, mas sem tratar a falta
        dela como dispensada.
    """
    sessao = StokkiSession(config)
    codigos_vistos: set[str] = set()
    codigos: list[str] = []
    codigos_sem_nf: set[str] = set()
    codigos_danfe_somente_email: set[str] = set()

    def _registrar(linha) -> None:
        codigo = _codigo_da_linha(linha)
        if codigo and codigo not in codigos_vistos:
            codigos_vistos.add(codigo)
            codigos.append(codigo)
            if _pedido_sem_nf(linha):
                codigos_sem_nf.add(codigo)
            if _pedido_danfe_bloqueada(linha):
                codigos_danfe_somente_email.add(codigo)

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
    for codigo, danfe_bloqueada in descobrir_expedidos_recentes(sessao):
        if codigo not in codigos_vistos:
            codigos_vistos.add(codigo)
            codigos.append(codigo)
        if danfe_bloqueada:
            codigos_danfe_somente_email.add(codigo)
    logger.info(f"Expedidos recentes (embarcadores de boleto por e-mail): "
               f"{len(codigos) - n_antes} pedido(s) adicionados.")

    return codigos, codigos_sem_nf, codigos_danfe_somente_email


def descobrir_expedidos_recentes(sessao: StokkiSession, limite_por_embarcador: int = 60) -> list[tuple[str, bool]]:
    """
    Os N pedidos "Sent" mais recentes de cada embarcador que manda
    boleto por e-mail (ver EMBARCADORES_BOLETO_EMAIL). Quem já tem a
    Nota Fiscal enviada/indexada é filtrado FORA aqui mesmo -- assim,
    em regime, só os expedidos novos do dia geram visita de página
    (os antigos não custam nada).

    Retorna [(codigo, danfe_bloqueada)] -- danfe_bloqueada indica se o
    pedido é de um embarcador de EMBARCADORES_DANFE_SOMENTE_EMAIL (a
    Dourado passa por aqui, não só pelo loop de EMBARCADORES_QUALQUER_
    STATUS, então essa checagem precisa ser feita de novo aqui).
    """
    from fingerprint_documentos import ja_enviado_para_pedido

    codigos: list[tuple[str, bool]] = []
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
                    codigos.append((codigo, _pedido_danfe_bloqueada(linha)))
        except Exception as e:
            logger.warning(f"Erro ao buscar expedidos recentes de {nome_emb!r}: {e}")
    return codigos
