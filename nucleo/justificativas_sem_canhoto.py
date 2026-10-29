# -*- coding: utf-8 -*-
"""
nucleo/justificativas_sem_canhoto.py

Lista fixa de justificativas para entrega sem canhoto (checklist Vuupt pelas telas). Usada pela API da
Torre/Planejamento e pela tela /baixa-sem-app para mostrar os rotulos e validar a escolha.

Regra: com foto do canhoto a justificativa e ignorada; sem foto do canhoto ela e obrigatoria. So o
"outro" pede texto, de 5 a 200 caracteres (apara espacos antes de medir).
"""

JUSTIFICATIVAS: dict[str, str] = {
    "canhoto_depois": "Canhoto chega depois (motorista vai trazer)",
    "nao_assinou": "Cliente não assinou / recusou assinar",
    "confirmado_contato": "Entrega confirmada por telefone ou WhatsApp com o cliente",
    "sem_bateria": "Motorista sem celular ou sem bateria na hora da entrega",
    "foto_perdida": "Foto do canhoto perdida ou ilegível",
    "acordo_embarcador": "Entrega sem canhoto por acordo com o embarcador",
    "outro": "Outro",
}

_MIN_OUTRO = 5
_MAX_OUTRO = 200


def rotulo(codigo: str) -> str:
    """Texto que a tela mostra para o codigo; ValueError se o codigo nao esta na lista."""
    if codigo not in JUSTIFICATIVAS:
        raise ValueError("Escolha uma justificativa da lista.")
    return JUSTIFICATIVAS[codigo]


def validar(codigo: str | None, texto_outro: str | None) -> tuple[str, str]:
    """Devolve (codigo, texto). Texto e "" fora do "outro". ValueError com mensagem para a tela."""
    if not codigo or codigo not in JUSTIFICATIVAS:
        raise ValueError("Escolha uma justificativa da lista.")
    if codigo != "outro":
        return codigo, ""
    texto = (texto_outro or "").strip()
    if not (_MIN_OUTRO <= len(texto) <= _MAX_OUTRO):
        raise ValueError("Descreva o motivo em 5 a 200 caracteres.")
    return codigo, texto
