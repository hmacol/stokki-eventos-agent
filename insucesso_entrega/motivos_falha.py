# -*- coding: utf-8 -*-
"""
motivos_falha.py

De-para dos motivos de insucesso na entrega (failed_reason_id do VUUPT)
— pedido do Hugo, 03/08: "criar um de-para para a notificação chegar
correta no e-mail com o motivo compreensível".

REGRA ATUAL (pedido do Hugo, 15/08: "quero que as tratativas definam
se vamos ou não duplicar um pedido, não quero mais duplicar
automaticamente; no caso do cliente responder, prevalece o que ele
solicitar"): NENHUM motivo duplica sozinho -- nem na hora, nem depois
de alguns dias. TODO insucesso vira uma PERGUNTA ao embarcador
("houve insucesso, deseja o reenvio?") e só duplica se a resposta
confirmar (ver notificar_insucesso_aguardando_resposta.py e
ler_respostas_insucesso.py). Isso revoga a regra de 11/08 ("duplica
tudo na hora, avisa depois") e a de 06/08 (duplicação agendada pra
Loja/Câmara em Manutenção) -- ambas descritas como histórico abaixo,
mantidas só pelo comentário porque explicam o "porquê" de campos como
"duplicar" no dicionário, que não são mais consultados por código
nenhum.

Alguns motivos (pedido do Hugo, 03/08) têm uma pergunta PRÓPRIA, mais
específica que o texto genérico -- "aguarda_resposta": True e
"pergunta": "...". Usada por notificar_insucesso_aguardando_resposta.py
pra deixar o e-mail mais claro; motivo sem pergunta própria usa o
texto genérico.

MOTIVOS_FALHA: cada failed_reason_id do VUUPT mapeado pra um texto
compreensível (e, pra alguns, uma pergunta específica). Os campos
"duplicar" e "duplicar_apos_dias_uteis" são histórico das regras
antigas (03/08-11/08) e não influenciam mais nada.

Motivos NOVOS (pedido do Hugo, 11/08): quando um failed_reason_id
aparece sem estar neste de-para, a DESCRIÇÃO oficial é registrada
automaticamente a partir do próprio VUUPT (include=failedReason na
busca de insucessos -> aprender_motivos), num cache persistente em
dados/motivos_vuupt_auto.json. Só o TEXTO é automático.
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
