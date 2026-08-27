# -*- coding: utf-8 -*-
"""
atendimento/alertas.py

E-mails de alerta da central de atendimento -- reaproveita email_utils
(mesmo template/SMTP dos outros agentes). Compartilhado por app.py (falha
de entrega reportada pelo WhatsApp, erro 463) e reenviar_pendentes.py
(mensagem que esgotou as tentativas de envio).

config.yaml:
    atendimento:
      email_alerta: "hugo@freshlogbr.com"   # opcional, default abaixo
    email: { ... }                          # ver email_utils.py
"""
import html
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

import email_utils

DESTINATARIO_PADRAO = "hugo@freshlogbr.com"


def _destinatario(config: dict) -> str:
    return (config.get("atendimento", {}) or {}).get("email_alerta", DESTINATARIO_PADRAO)


def avisar_mensagem_nao_entregue(config: dict, protocolo: str | None, telefone: str | None,
                                 corpo: str | None, motivo: str, suspenso_ate: str | None = None) -> bool:
    """Um e-mail por mensagem perdida (nunca por tentativa). Quando o
    disjuntor entrou em ação junto (463), a mesma mensagem já avisa até
    quando os envios automáticos ficam suspensos -- não manda dois e-mails."""
    linhas = [
        "<p>Uma mensagem do atendimento não foi entregue pelo WhatsApp.</p>",
        f"<p><strong>Motivo:</strong> {html.escape(motivo)}<br>",
        f"<strong>Protocolo:</strong> {html.escape(protocolo or '-')}<br>",
        f"<strong>Telefone:</strong> {html.escape(telefone or '-')}<br>",
        f"<strong>Mensagem:</strong> {html.escape(corpo or '(vazia)')}</p>",
    ]
    if suspenso_ate:
        linhas.append(
            f"<p><strong>Envios automáticos (bot + reenvio) suspensos até {html.escape(suspenso_ate[:16])}</strong> "
            "-- cada envio a mais renova a trava do WhatsApp. Respostas manuais pela central continuam "
            "possíveis, mas use só o essencial até a trava expirar.</p>"
        )
    linhas.append(
        "<p>Acompanhe em atendimento.freshhub.com.br/admin/whatsapp e reenvie manualmente pela conversa se preciso.</p>"
    )
    return email_utils.enviar_email(
        [_destinatario(config)], f"Atendimento: mensagem não entregue ({protocolo or 'sem protocolo'})",
        email_utils.envelope_html("".join(linhas), cor_acento=email_utils.COR_ERRO),
        config.get("email", {}),
    )


def avisar_resultado_sonda(config: dict, telefone: str, resultado: str, detalhe: str) -> bool:
    """Veredito da sonda de entrega (atendimento/sonda_entrega.py): ENTREGUE,
    RECUSADA (463), INDETERMINADA ou FALHA_NO_POST -- com a instrução do que
    fazer em cada caso, pra decisão não depender de abrir log."""
    entregue = resultado == "ENTREGUE"
    conteudo = (
        f"<p><strong>Sonda de entrega pelo WhatsApp (API): {html.escape(resultado)}</strong></p>"
        f"<p>Telefone: {html.escape(telefone)}<br>{html.escape(detalhe)}</p>"
        "<p>ENTREGUE = a trava 463 expirou -- pode clicar em <em>Retomar</em> em "
        "atendimento.freshhub.com.br/admin/whatsapp.<br>"
        "RECUSADA = ainda travada -- manter a pausa e esperar mais antes de outra sonda (nunca em rajada).<br>"
        "INDETERMINADA = sem ✓✓ nem recusa no prazo -- conferir no celular se chegou antes de decidir.</p>"
    )
    return email_utils.enviar_email(
        [_destinatario(config)], f"Atendimento: sonda de entrega -- {resultado}",
        email_utils.envelope_html(conteudo, cor_acento=email_utils.COR_ACENTO if entregue else email_utils.COR_ERRO),
        config.get("email", {}),
    )
