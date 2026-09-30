# -*- coding: utf-8 -*-
"""
email_utils.py

Envio de e-mail HTML com a identidade visual da Freshlog — extraído do
padrão já usado em notificar_pedidos_em_espera.py (e replicado também em
expedir_pedidos.py) para não duplicar pela terceira vez com o relatório
operacional. Os dois arquivos antigos continuam com suas cópias próprias
por ora (não mexidos aqui) — só o código novo usa este módulo.

Uso:
    from email_utils import enviar_email, envelope_html

    corpo = envelope_html("<p>Conteúdo em HTML aqui.</p>")
    enviar_email(["hugo@freshlogbr.com"], "Assunto", corpo, config["email"])
"""
import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)


def sanitizar_cabecalho(texto) -> str:
    """Remove CR/LF de um valor antes de usá-lo em cabeçalho de e-mail
    (Subject/From/To). Sem isso, um dado externo (nome de cliente,
    título de pedido, etc.) contendo \\r\\n poderia injetar cabeçalhos
    extras na mensagem enviada."""
    if texto is None:
        return ""
    return str(texto).replace("\r", " ").replace("\n", " ")


_RAIZ = Path(__file__).parent
LOGO_PATH = _RAIZ / "assets" / "logo_freshlog.png"

# Paleta Freshlog (mesma usada em notificar_pedidos_em_espera.py)
COR_PRIMARIA       = "#141428"
COR_PRIMARIA_CLARA = "#E6FBF5"
COR_ACENTO         = "#00C896"
COR_DESTAQUE       = "#F5A623"
COR_ERRO           = "#EF4444"
COR_TEXTO          = "#1F2937"
COR_TEXTO_SUAVE    = "#6B7280"
COR_FUNDO          = "#F3F4F6"
COR_BORDA          = "#E5E7EB"


def envelope_html(conteudo: str, rodape: str = "Esta e uma mensagem automatica da Freshlog.",
                  cor_acento: str = COR_ACENTO) -> str:
    """Envolve o HTML do corpo no template visual padrão (logo, cores, rodapé).
    cor_acento é opcional -- por padrão usa o verde-azulado da marca
    (COR_ACENTO), mas outros agentes podem passar COR_DESTAQUE (laranja,
    "atenção") ou outra cor da paleta pra mensagens de urgência/tipo
    diferente, sem duplicar o resto do template (06/08, pedido do Hugo:
    "adequar o layout dos e-mails para o padrão que já temos")."""
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:{COR_FUNDO};font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{COR_FUNDO};padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="max-width:640px;width:100%;background-color:#FFFFFF;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
        <tr>
          <td style="background-color:#FFFFFF;padding:24px 32px;border-bottom:1px solid {COR_BORDA};">
            <img src="cid:logo_freshlog" alt="Freshlog Logistica" width="150" style="display:block;border:0;">
          </td>
        </tr>
        <tr><td style="background-color:{cor_acento};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
        <tr><td style="padding:32px;">{conteudo}</td></tr>
        <tr>
          <td style="padding:20px 32px;background-color:{COR_FUNDO};border-top:1px solid {COR_BORDA};">
            <p style="margin:0;font-size:12px;color:{COR_TEXTO_SUAVE};line-height:1.6;">
              {rodape}
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def enviar_email(destinatarios: list[str], assunto: str, corpo_html: str, config_email: dict,
                 cc: list[str] | None = None, anexos: list[tuple[Path, str]] | None = None,
                 cabecalhos_extra: dict | None = None) -> bool:
    """
    Envia um e-mail HTML via SMTP (mesmo padrão de notificar_pedidos_em_espera.py).

    config_email: a seção `email:` do config.yaml (remetente, senha_app/senha,
    smtp_host, smtp_port). cc: e-mails em cópia (opcional). anexos: lista de
    (caminho_arquivo, nome_arquivo) pra anexar como MIMEApplication (opcional
    -- pedido do Hugo, 13/08, pra notificação de transportadoras com XML da
    NF-e, ver notificacao_transportadoras/). cabecalhos_extra: cabeçalhos
    adicionais (Message-ID, In-Reply-To, References, Reply-To...) -- usado
    pelos chamados do portal do cliente (09/09) pra manter a conversa
    encadeada no e-mail; valores passam por sanitizar_cabecalho. Retorna
    True em sucesso, False em falha (logada, nunca levanta exceção — quem
    chama decide o que fazer com o retorno).
    """
    try:
        msg = MIMEMultipart("related")
        msg["Subject"] = sanitizar_cabecalho(assunto)
        msg["From"]    = config_email.get("remetente", "hugo@freshlogbr.com")
        msg["To"]      = ", ".join(destinatarios)
        if cc:
            msg["Cc"] = ", ".join(cc)
        for chave, valor in (cabecalhos_extra or {}).items():
            if valor:
                msg[chave] = sanitizar_cabecalho(valor)

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(corpo_html, "html", "utf-8"))
        msg.attach(alt)

        if LOGO_PATH.exists():
            with open(LOGO_PATH, "rb") as f:
                img = MIMEImage(f.read())
            img.add_header("Content-ID", "<logo_freshlog>")
            img.add_header("Content-Disposition", "inline", filename="logo_freshlog.png")
            msg.attach(img)

        for caminho, nome_arquivo in (anexos or []):
            subtipo = Path(nome_arquivo).suffix.lstrip(".").lower() or "octet-stream"
            with open(caminho, "rb") as f:
                anexo = MIMEApplication(f.read(), _subtype=subtipo)
            anexo.add_header("Content-Disposition", "attachment", filename=nome_arquivo)
            msg.attach(anexo)

        # usuario_login: conta que autentica no SMTP quando o remetente é um
        # alias dela (09/09: entregas@ é alias de hugo@ -- login hugo@,
        # From entregas@). Sem a chave, login = remetente como sempre foi.
        usuario = config_email.get("usuario_login") or config_email.get("remetente", "hugo@freshlogbr.com")
        senha   = config_email.get("senha_app") or config_email.get("senha", "")
        host    = config_email.get("smtp_host", "smtp.gmail.com")
        port    = int(config_email.get("smtp_port", 587))

        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(usuario, senha)
            smtp.send_message(msg, to_addrs=list(destinatarios) + list(cc or []))
        logger.info(f"E-mail enviado para {destinatarios}"
                   f"{f' (cc: {cc})' if cc else ''}: {assunto!r}")
        return True
    except Exception as e:
        logger.error(f"Falha ao enviar e-mail para {destinatarios}: {e}")
        return False


def notificacoes_automaticas_ativas(config: dict) -> bool:
    """Chave-mestra pra desligar os e-mails automáticos que vão pra fora
    (embarcador/transportadora) sem tocar nos alertas internos (que usam
    notificacao_execucao.ativo, separado). config.yaml: notificacoes_automaticas.ativo
    -- default True (não quebra quem ainda não tem essa seção no config)."""
    return bool((config or {}).get("notificacoes_automaticas", {}).get("ativo", True))


def notificacao_pedidos_em_espera_ativa(config: dict) -> bool:
    """Flag PRÓPRIO da cobrança de XML dos pedidos em espera
    (notificar_pedidos_em_espera.py). Separado da chave-mestra
    notificacoes_automaticas de propósito (28/08, pedido do Hugo): esse fluxo
    roda em timer próprio (08:20 e 15:20) e pode ficar ligado mesmo com os
    outros e-mails externos desligados. config.yaml:
    notificacao_pedidos_em_espera.ativo -- default True."""
    return bool((config or {}).get("notificacao_pedidos_em_espera", {}).get("ativo", True))
