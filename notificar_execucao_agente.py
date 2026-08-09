# -*- coding: utf-8 -*-
"""
notificar_execucao_agente.py

Envia um e-mail de resumo a cada execução do executar_tudo.py (sucesso
ou erro), similar ao relatório de execução que já existe no agente de
importação por e-mail (C:\\agente_importacao_stokki) — pedido do Hugo,
31/07: "criar notificação de agente processado, similar ao que temos
no Agente de importação pelo e-mail".

Uma linha por etapa (Agendamento, Impressão, Expedição, Pipeline) com
status (OK/erro) e detalhe; pra Pipeline, puxa a contagem detalhada
(criados/atualizados/pulados/erros/revisão) do último registro salvo
por historico.py — não duplica lógica de contagem.

Falha no envio NUNCA derruba o executar_tudo.py — só loga o erro.

Ativado/desativado via config.yaml:
    notificacao_execucao:
      ativo: true
      destinatario: hugo@freshlogbr.com   # opcional, usa email.remetente por padrão
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent

COR_PRIMARIA    = "#141428"
COR_ACENTO      = "#00C896"
COR_ERRO        = "#EF4444"
COR_TEXTO       = "#1F2937"
COR_TEXTO_SUAVE = "#6B7280"
COR_FUNDO       = "#F3F4F6"
COR_BORDA       = "#E5E7EB"


def _linha_etapa(nome: str, info: dict) -> str:
    ok = info.get("status") == "ok"
    cor_status = COR_ACENTO if ok else COR_ERRO
    rotulo_status = "OK" if ok else "ERRO"
    detalhe = info.get("detalhe", "")
    return f"""
    <tr>
      <td style="padding:10px 14px;border-bottom:1px solid {COR_BORDA};font-weight:600;color:{COR_TEXTO};">{nome}</td>
      <td style="padding:10px 14px;border-bottom:1px solid {COR_BORDA};">
        <span style="display:inline-block;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700;
                    color:#fff;background:{cor_status};">{rotulo_status}</span>
      </td>
      <td style="padding:10px 14px;border-bottom:1px solid {COR_BORDA};font-size:13px;color:{COR_TEXTO_SUAVE};">{detalhe}</td>
    </tr>"""


def _montar_corpo(resumo_etapas: dict, duracao_total_seg: float, modo_teste: bool) -> str:
    linhas = "".join(_linha_etapa(nome, info) for nome, info in resumo_etapas.items())
    prefixo_teste = "[MODO TESTE] " if modo_teste else ""
    minutos = duracao_total_seg / 60

    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:{COR_FUNDO};
font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{COR_FUNDO};padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
      style="max-width:640px;width:100%;background:#fff;border-radius:10px;overflow:hidden;">
<tr><td style="background:{COR_ACENTO};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
<tr><td style="padding:28px 32px;">
<p style="margin:0 0 4px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  {prefixo_teste}Agente Stokki Eventos — execução concluída
</p>
<p style="margin:0 0 20px 0;font-size:13px;color:{COR_TEXTO_SUAVE};">Duração total: {minutos:.1f} min</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Etapa</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Status</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Detalhe</th>
</tr></thead><tbody>{linhas}</tbody></table>
</td></tr>
<tr><td style="padding:16px 32px;background:{COR_FUNDO};border-top:1px solid {COR_BORDA};">
<p style="margin:0;font-size:12px;color:{COR_TEXTO_SUAVE};">
  Relatório automático — executar_tudo.py, Agente Stokki Eventos.</p>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def notificar_execucao(resumo_etapas: dict, duracao_total_seg: float,
                       modo_teste: bool, config: dict):
    """
    Envia o e-mail de resumo da execução. Nunca levanta exceção —
    falha no envio não pode derrubar o executar_tudo.py.
    """
    cfg_notif = config.get("notificacao_execucao", {})
    if not cfg_notif.get("ativo", True):
        logger.debug("Notificação de execução desativada (notificacao_execucao.ativo=false).")
        return

    config_email = config.get("email", {})
    destinatario = cfg_notif.get("destinatario") or config_email.get("remetente", "hugo@freshlogbr.com")

    try:
        assunto_prefixo = "[MODO TESTE] " if modo_teste else ""
        tem_erro = any(info.get("status") != "ok" for info in resumo_etapas.values())
        assunto = f"{assunto_prefixo}Agente Stokki Eventos — {'com erro(s)' if tem_erro else 'execução OK'}"

        corpo = _montar_corpo(resumo_etapas, duracao_total_seg, modo_teste)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = assunto
        msg["From"] = config_email.get("remetente", "hugo@freshlogbr.com")
        msg["To"] = destinatario
        msg.attach(MIMEText(corpo, "html", "utf-8"))

        usuario = config_email.get("remetente", "hugo@freshlogbr.com")
        senha = config_email.get("senha_app") or config_email.get("senha", "")
        host = config_email.get("smtp_host", "smtp.gmail.com")
        port = int(config_email.get("smtp_port", 587))

        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(usuario, senha)
            smtp.send_message(msg)
        logger.info(f"Notificação de execução enviada para {destinatario}.")
    except Exception as e:
        logger.warning(f"Falha ao enviar notificação de execução (não afeta o resultado): {e}")
