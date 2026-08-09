# -*- coding: utf-8 -*-
"""
motivos_falha.py

De-para dos motivos de insucesso na entrega (failed_reason_id do VUUPT)
— pedido do Hugo, 03/08: "criar um de-para para a notificação chegar
correta no e-mail com o motivo compreensível" + "duplicar dependendo
do motivo do insucesso" (sem gate de validated_by_agent, que nunca é
preenchido — confirmado com dado real, 155 de 155 insucessos sem esse
campo).

Pra ALGUNS motivos (pedido do Hugo, 03/08), em vez de duplicar direto,
o sistema deve NOTIFICAR o remetente com uma pergunta específica e
AGUARDAR RESPOSTA antes de agir -- esses têm "aguarda_resposta": True
e uma "pergunta" própria. Nesses casos, "duplicar" fica None (a
decisão de duplicar, se houver, depende da resposta -- ainda não
temos o mecanismo de leitura/decisão automática da resposta, só o
envio da pergunta por enquanto, ver notificar_insucesso_aguardando_
resposta.py).

Pra 1 motivo específico (Loja/Câmara em Manutenção, pedido do Hugo,
06/08), a duplicação é automática mas ATRASADA -- "duplicar_apos_
dias_uteis": N -- não duplica na hora nem espera resposta, só
AGENDA a duplicação pra N dias úteis depois da detecção do
insucesso. Ver fingerprint_duplicacao_agendada.py.

MOTIVOS_FALHA: cada failed_reason_id do VUUPT mapeado pra um texto
compreensível + se deve duplicar automaticamente o serviço ou não (ou
aguardar resposta, ou duplicar com atraso, pra alguns). Motivo NÃO
cadastrado aqui: notifica normalmente mas NÃO duplica nem aguarda
resposta (mais seguro por padrão).
"""
import logging

logger = logging.getLogger(__name__)

MOTIVOS_FALHA: dict[int, dict] = {
    # failed_reason_id: {"texto": "...", "duplicar": True/False/None,
    #                    "aguarda_resposta": True (opcional), "pergunta": "..." (opcional)}
    # Confirmado com o Hugo, 03/08 (textos reais + decisão de duplicar).
    # 6830 e 6829 ainda pendentes (não estavam na lista que ele confirmou).
    5744: {"texto": "Não coletado", "duplicar": False},
    5452: {"texto": "Fora do horário de recebimento", "duplicar": True},
    5563: {
        "texto": "Cliente sem pedido / Não reconheceu", "duplicar": None,
        "aguarda_resposta": True,
        "pergunta": "O destinatário não reconheceu o pedido, ou este não estava liberado para "
                    "recebimento no momento da entrega. Solicitamos a confirmação da situação "
                    "junto ao cliente.",
    },
    5433: {"texto": "Endereço Incorreto", "duplicar": True},
    8490: {
        "texto": "Cliente ausente", "duplicar": None,
        "aguarda_resposta": True,
        "pergunta": "O cliente estava ausente no momento da entrega. Solicitamos contato com o "
                    "destinatário para definirmos uma nova data e horário de entrega.",
    },
    5431: {
        "texto": "Local fechado", "duplicar": None,
        "aguarda_resposta": True,
        "pergunta": "O local estava fechado no momento da entrega. Solicitamos a informação do "
                    "horário de funcionamento do destinatário para a nova tentativa.",
    },
    8156: {
        "texto": "Problema fiscal", "duplicar": None,
        "aguarda_resposta": True,
        "pergunta": "Foi identificado um problema fiscal durante a entrega. Solicitamos a "
                    "confirmação de que a questão já foi regularizada para prosseguirmos com "
                    "o reenvio.",
    },
    5733: {"texto": "Validade ou Lote Rasurado", "duplicar": False},
    6830: {"texto": "(texto pendente -- 2 ocorrência(s))", "duplicar": None},
    5807: {"texto": "Sem agendamento", "duplicar": True},
    8366: {"texto": "Produto Avariado", "duplicar": False},
    5731: {"texto": "Produto Faltando", "duplicar": True},
    5732: {"texto": "Produto Divergente", "duplicar": True},
    6829: {"texto": "(texto pendente -- 1 ocorrência(s))", "duplicar": None},
    8220: {"texto": "Falta de Boleto", "duplicar": True},
    8353: {
        "texto": "Loja ou Câmara em Manutenção", "duplicar": False,
        "duplicar_apos_dias_uteis": 3,
    },
    5562: {"texto": "Fora da temperatura", "duplicar": True},
}


def texto_do_motivo(failed_reason_id) -> str:
    """Texto compreensível do motivo, pra usar na notificação por
    e-mail. Cai num texto genérico (com o ID) se o motivo não estiver
    no de-para -- nunca quebra por motivo desconhecido/novo."""
    if failed_reason_id is None:
        return "Motivo não informado"
    info = MOTIVOS_FALHA.get(failed_reason_id)
    if info is None:
        return f"Motivo #{failed_reason_id} (ainda não cadastrado no de-para)"
    return info["texto"]


def deve_duplicar(failed_reason_id) -> bool:
    """True só se o motivo estiver cadastrado E explicitamente marcado
    duplicar=True. Motivo desconhecido, aguardando resposta, ou
    cadastrado mas ainda duplicar=None/False, NUNCA duplica
    automaticamente -- seguro por padrão."""
    info = MOTIVOS_FALHA.get(failed_reason_id)
    if info is None:
        return False
    return info["duplicar"] is True


def aguarda_resposta(failed_reason_id) -> bool:
    """True se esse motivo precisa de uma notificação com pergunta e
    aguardar resposta do remetente antes de qualquer ação automática
    (pedido do Hugo, 03/08)."""
    info = MOTIVOS_FALHA.get(failed_reason_id)
    if info is None:
        return False
    return info.get("aguarda_resposta", False) is True


def pergunta_do_motivo(failed_reason_id) -> str:
    """Pergunta específica a mandar pro remetente, pros motivos que
    aguardam resposta. Texto genérico de reserva se, por algum motivo,
    chamado pra um motivo sem pergunta cadastrada."""
    info = MOTIVOS_FALHA.get(failed_reason_id)
    if info is None or "pergunta" not in info:
        return "Solicitamos mais informações sobre a situação deste insucesso de entrega."
    return info["pergunta"]


def duplicar_com_atraso(failed_reason_id) -> int | None:
    """
    Retorna quantos DIAS ÚTEIS depois da detecção do insucesso a
    duplicação automática deve acontecer, ou None se esse motivo não
    usa duplicação atrasada (pedido do Hugo, 06/08 -- Loja/Câmara em
    Manutenção: duplica sozinho, mas só 3 dias úteis depois, dando
    tempo da manutenção terminar, sem precisar de resposta de ninguém).
    """
    info = MOTIVOS_FALHA.get(failed_reason_id)
    if info is None:
        return None
    return info.get("duplicar_apos_dias_uteis")
