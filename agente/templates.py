# -*- coding: utf-8 -*-
"""
agente/templates.py

Todos os textos que o agente manda ou propoe. Sem LLM: template fixo
(decisao do Hugo, 04/10). Regras de escrita:
  - fala com o EMBARCADOR, nunca com o destinatario;
  - nao promete reentrega, cancelamento, reagendamento, devolucao, estorno
    nem mudanca de endereco: pede, informa, pergunta;
  - toda mensagem ao embarcador termina com o link da central de
    atendimento (a resposta no WhatsApp cai no celular do Hugo e nao entra
    no chamado -- o cliente precisa continuar pela central);
  - WhatsApp curto (teto do notificar_whatsapp e 200 caracteres por
    mensagem fora o link; aqui o link vai numa linha propria).
"""
import html

from agente import regras

LINK_PADRAO = "https://app.freshhub.com.br/cliente"


def link_central(config: dict | None) -> str:
    return str(((config or {}).get("agente") or {}).get("link_atendimento") or LINK_PADRAO)


def _rodape(link: str) -> str:
    return f"Continue pela central de atendimento: {link}"


def _nome(dados: dict, chave: str, padrao: str) -> str:
    return str(dados.get(chave) or padrao).strip()


# --- WhatsApp (uma funcao por template AVISAR) --------------------------------

def whatsapp(template: str, dados: dict, link: str) -> str:
    codigo = _nome(dados, "codigo", "?")
    dest = _nome(dados, "destinatario", "o destinatario")
    if template == regras.COBRAR_DATA_AGENDAMENTO:
        corpo = (f"Fresh Log: o pedido {codigo} ({dest}) exige agendamento e ainda nao recebemos "
                 f"a data. Sem ela o pedido nao entra em rota.")
    elif template == regras.AVISO_FALHA_ENTREGA:
        motivo = _nome(dados, "motivo", "motivo nao informado")
        t = int(dados.get("tentativa") or 1)
        tent = f" ({t}a tentativa)" if t > 1 else ""
        corpo = (f"Fresh Log: nao conseguimos entregar o pedido {codigo} ({dest}){tent}. "
                 f"Motivo: {motivo}. Enviamos por e-mail a pergunta sobre o reenvio.")
    elif template == regras.PEDIR_CONFIRMACAO_DEVOLUCAO:
        corpo = (f"Fresh Log: o pedido {codigo} ({dest}) nao sera reenviado. "
                 f"Precisamos da sua confirmacao para devolver a mercadoria ao seu estoque.")
    else:
        corpo = f"Fresh Log: atualizacao sobre o pedido {codigo}."
    return f"{corpo}\n{_rodape(link)}"


# --- E-mail (assunto + corpo HTML, sem o envelope) ----------------------------

def email(template: str, dados: dict, link: str) -> tuple[str, str]:
    codigo = html.escape(_nome(dados, "codigo", "?"))
    dest = html.escape(_nome(dados, "destinatario", "o destinatario"))
    remetente = html.escape(_nome(dados, "remetente", ""))
    saud = f"<p>Ola, {remetente}.</p>" if remetente else "<p>Ola.</p>"
    botao = (f'<p><a href="{html.escape(link)}" style="display:inline-block;padding:10px 16px;'
             f'background:#0f766e;color:#fff;text-decoration:none;border-radius:6px;">'
             f'Responder pela central de atendimento</a></p>')
    if template == regras.COBRAR_DATA_AGENDAMENTO:
        assunto = f"Pedido {codigo}: precisamos da data de agendamento"
        corpo = (f"{saud}<p>O pedido <b>{codigo}</b> ({dest}) exige agendamento com o destinatario "
                 f"e ainda nao recebemos a data. Enquanto ela nao chega, o pedido nao entra em rota.</p>"
                 f"<p>Informe a data e o horario combinados pela central de atendimento.</p>{botao}")
    elif template == regras.AVISO_FALHA_ENTREGA:
        motivo = html.escape(_nome(dados, "motivo", "motivo nao informado"))
        t = int(dados.get("tentativa") or 1)
        assunto = f"Pedido {codigo}: falha na entrega" + (f" ({t}a tentativa)" if t > 1 else "")
        corpo = (f"{saud}<p>Nao conseguimos entregar o pedido <b>{codigo}</b> ({dest}).</p>"
                 f"<p>Motivo registrado pelo motorista: <b>{motivo}</b>.</p>"
                 f"<p>A pergunta sobre o reenvio segue em e-mail separado. Qualquer orientacao, "
                 f"use a central de atendimento.</p>{botao}")
    elif template == regras.PEDIR_CONFIRMACAO_DEVOLUCAO:
        assunto = f"Pedido {codigo}: confirmar devolucao ao estoque"
        corpo = (f"{saud}<p>O pedido <b>{codigo}</b> ({dest}) nao sera reenviado.</p>"
                 f"<p>Para devolver a mercadoria ao seu estoque precisamos da sua confirmacao. "
                 f"Responda pela central de atendimento confirmando a devolucao, ou nos diga "
                 f"o que prefere fazer com o pedido.</p>{botao}")
    else:
        assunto = f"Pedido {codigo}: atualizacao"
        corpo = f"{saud}<p>Ha uma atualizacao sobre o pedido <b>{codigo}</b>.</p>{botao}"
    return assunto, corpo


# --- Propostas (texto curto pra Torre) ----------------------------------------

_PROPOSTAS = {
    regras.PROPOSTA_EXPEDIR_STOKKI: "Expedir na Stokki e anexar o canhoto (entregue e nao expedido).",
    regras.PROPOSTA_COBRAR_CANHOTO: "Cobrar o canhoto assinado do motorista (expedido sem documento).",
    regras.PROPOSTA_COBRAR_COMPROVANTE_TRANSPORTADORA: "Cobrar o comprovante de recebimento da transportadora.",
    regras.PROPOSTA_COBRAR_COMPROVANTE_RETIRADA: "Cobrar a assinatura de quem retirou no galpao.",
    regras.PROPOSTA_CONFERIR_ENTREGA: "Conferir com a operacao se a entrega aconteceu (expedido sem entrega registrada).",
    regras.PROPOSTA_RECRIAR_OU_CANCELAR: "Servico cancelado na Vuupt com pedido aberto na Stokki: recriar o servico ou cancelar na Stokki, conforme o embarcador.",
    regras.PROPOSTA_CANCELAR_SERVICO_VUUPT: "Pedido cancelado na Stokki com servico vivo na Vuupt: cancelar o servico.",
}


def proposta(template: str, dados: dict) -> str:
    base = _PROPOSTAS.get(template, template)
    ev = dados.get("evidencias") or {}
    if ev:
        itens = "; ".join(f"{k}={v}" for k, v in list(ev.items())[:6])
        return f"{base} Evidencias: {itens}."
    return base
