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

Motivos NOVOS (pedido do Hugo, 11/08): quando um failed_reason_id
aparece sem estar neste de-para, a DESCRIÇÃO oficial é registrada
automaticamente a partir do próprio VUUPT (include=failedReason na
busca de insucessos -> aprender_motivos), num cache persistente em
dados/motivos_vuupt_auto.json. Só o TEXTO é automático -- a regra
(duplicar / aguardar resposta / atraso) continua sendo decisão
manual aqui no dicionário; motivo aprendido nunca duplica sozinho.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_ARQ_MOTIVOS_AUTO = Path(__file__).parent.parent / "dados" / "motivos_vuupt_auto.json"
_cache_auto: dict[int, str] | None = None  # carregado 1x por processo

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
    # 6830 e 6829 saíram do dicionário (11/08): estavam como "(texto
    # pendente)" e aqui dentro BLOQUEAVAM o aprendizado automático da
    # descrição real -- como "duplicar" era None, remover não muda
    # comportamento nenhum (motivo fora do de-para também não duplica).
    5807: {"texto": "Sem agendamento", "duplicar": True},
    8366: {"texto": "Produto Avariado", "duplicar": False},
    5731: {"texto": "Produto Faltando", "duplicar": True},
    5732: {"texto": "Produto Divergente", "duplicar": True},
    8220: {"texto": "Falta de Boleto", "duplicar": True},
    8353: {
        "texto": "Loja ou Câmara em Manutenção", "duplicar": False,
        "duplicar_apos_dias_uteis": 3,
    },
    5562: {"texto": "Fora da temperatura", "duplicar": True},
}


def _motivos_auto() -> dict[int, str]:
    """Cache de motivos aprendidos automaticamente do VUUPT
    ({failed_reason_id: descricao}). Lido do JSON uma vez por processo."""
    global _cache_auto
    if _cache_auto is None:
        try:
            with open(_ARQ_MOTIVOS_AUTO, encoding="utf-8") as f:
                _cache_auto = {int(k): v for k, v in json.load(f).items()}
        except FileNotFoundError:
            _cache_auto = {}
        except Exception as e:
            logger.warning(f"Falha ao ler {_ARQ_MOTIVOS_AUTO.name}: {e}")
            _cache_auto = {}
    return _cache_auto


def aprender_motivos(servicos: list) -> int:
    """
    Registra automaticamente a descrição oficial de motivos de falha
    NOVOS, a partir de serviços que vieram da API com
    include=failedReason (pedido do Hugo, 11/08 -- caso real: motivo
    #8037 apareceu sem estar no de-para e a notificação saiu genérica).

    Só registra o TEXTO -- a regra de duplicação/pergunta continua
    manual no MOTIVOS_FALHA (motivo aprendido nunca duplica sozinho).
    Retorna quantos motivos novos foram registrados.
    """
    auto = _motivos_auto()
    novos = 0
    for s in servicos or []:
        rid = s.get("failed_reason_id")
        fr = s.get("failedReason") or s.get("failed_reason") or {}
        descricao = (fr.get("description") or "").strip() if isinstance(fr, dict) else ""
        if not rid or not descricao:
            continue
        if rid in MOTIVOS_FALHA or auto.get(rid) == descricao:
            continue
        auto[rid] = descricao
        novos += 1
        logger.info(f"Motivo de falha novo aprendido do VUUPT: #{rid} = {descricao!r} "
                    "(sem regra de duplicação -- definir em MOTIVOS_FALHA se precisar)")
    if novos:
        try:
            _ARQ_MOTIVOS_AUTO.parent.mkdir(parents=True, exist_ok=True)
            with open(_ARQ_MOTIVOS_AUTO, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in sorted(auto.items())},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Falha ao salvar {_ARQ_MOTIVOS_AUTO.name}: {e}")
    return novos


def texto_do_motivo(failed_reason_id) -> str:
    """Texto compreensível do motivo, pra usar na notificação por
    e-mail. Ordem: de-para manual -> motivos aprendidos do VUUPT ->
    texto genérico com o ID. Nunca quebra por motivo desconhecido."""
    if failed_reason_id is None:
        return "Motivo não informado"
    info = MOTIVOS_FALHA.get(failed_reason_id)
    if info is not None:
        return info["texto"]
    descricao = _motivos_auto().get(failed_reason_id)
    if descricao:
        return f"{descricao} (novo -- sem regra de duplicação)"
    return f"Motivo #{failed_reason_id} (ainda não cadastrado no de-para)"


def deve_duplicar(failed_reason_id) -> bool:
    """
    NOVA REGRA (pedido do Hugo, 11/08): TODO insucesso duplica
    IMEDIATAMENTE -- motivo conhecido, aprendido ou desconhecido --
    exceto os motivos com duplicação AGENDADA (duplicar_apos_dias_
    uteis, ex: Loja/Câmara em Manutenção), que continuam esperando os
    N dias úteis ("mantém os 3 dias para câmara quebrada").

    O controle passou a ser DEPOIS do fato: o remetente recebe um
    aviso de que o pedido foi duplicado pra reentrega no dia seguinte
    e, se responder pedindo cancelamento, a reentrega é cancelada no
    VUUPT (ler_respostas_insucesso.py). Os campos "duplicar" do
    dicionário acima NÃO são mais consultados aqui -- ficam como
    histórico da regra antiga (03/08-11/08, duplicação por motivo).
    """
    if duplicar_com_atraso(failed_reason_id):
        return False
    return True


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
