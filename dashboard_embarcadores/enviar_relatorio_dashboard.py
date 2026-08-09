# -*- coding: utf-8 -*-
"""
enviar_relatorio_dashboard.py

Rotina semanal (toda sexta-feira às 17h, ver setup_tarefas.ps1) que
captura o dashboard de embarcadores (foto PNG + PDF, via
capturar_dashboard.py/Playwright) e manda por e-mail: a foto embutida
no corpo do e-mail, o PDF em anexo (pedido do Hugo, 30/07).

Depende do app Flask (dashboard_embarcadores.py, via waitress) já
estar rodando em http://127.0.0.1:8060 — a mesma tarefa agendada
StokkiEventos_DashboardApp cuida disso continuamente.

COMO USAR:
    py -3.11 enviar_relatorio_dashboard.py
    py -3.11 enviar_relatorio_dashboard.py --destinatario outro@freshlogbr.com
"""
import argparse
import logging
import smtplib
import sys
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "enviar_relatorio_dashboard.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("enviar_relatorio_dashboard")

import yaml

from capturar_dashboard import capturar_dashboard
from email_utils import COR_ACENTO, COR_BORDA, COR_FUNDO, COR_PRIMARIA, COR_TEXTO, COR_TEXTO_SUAVE, LOGO_PATH

DESTINATARIO_PADRAO = "hugo@freshlogbr.com"
URL_DASHBOARD_LOCAL = "http://127.0.0.1:8060/"


def _montar_email(caminho_png: Path) -> tuple[str, str]:
    """Retorna (assunto, corpo_html) com a foto embutida via cid."""
    from datetime import date
    hoje = date.today().strftime("%d/%m/%Y")
    assunto = f"Dashboard de Embarcadores — resumo da semana ({hoje})"

    corpo = f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:{COR_FUNDO};
font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{COR_FUNDO};padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="700" cellpadding="0" cellspacing="0"
      style="max-width:700px;width:100%;background:#fff;border-radius:10px;overflow:hidden;">
<tr><td style="padding:24px 32px;border-bottom:1px solid {COR_BORDA};">
<img src="cid:logo_freshlog" alt="Freshlog Logistica" width="150" style="display:block;border:0;">
</td></tr>
<tr><td style="background:{COR_ACENTO};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
<tr><td style="padding:28px 32px;">
<p style="margin:0 0 4px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Dashboard de Embarcadores — resumo semanal
</p>
<p style="margin:0 0 20px 0;font-size:13px;color:{COR_TEXTO_SUAVE};">{hoje}</p>
<img src="cid:foto_dashboard" alt="Dashboard de Embarcadores" width="636"
    style="display:block;max-width:100%;border:1px solid {COR_BORDA};border-radius:8px;">
<p style="margin:20px 0 0 0;font-size:13px;color:{COR_TEXTO};line-height:1.6;">
  A versão em PDF está em anexo. Pra ver o dashboard interativo (com filtros por
  ano/mês/semana/embarcador), acesse a versão online.
</p>
</td></tr>
<tr><td style="padding:20px 32px;background:{COR_FUNDO};border-top:1px solid {COR_BORDA};">
<p style="margin:0;font-size:12px;color:{COR_TEXTO_SUAVE};">
  Relatório automático — Agente Stokki Eventos, toda sexta-feira às 17h.
</p>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""
    return assunto, corpo


def enviar_relatorio(destinatario: str, config_email: dict) -> bool:
    pasta_export = _RAIZ_LOCAL / "dados" / "exportacoes"
    caminho_png = pasta_export / "dashboard_semanal.png"
    caminho_pdf = pasta_export / "dashboard_semanal.pdf"

    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    cfg_dash = config.get("dashboard_embarcadores", {})
    usuario, senha = cfg_dash.get("usuario"), cfg_dash.get("senha")
    if not usuario or not senha:
        logger.error("config.yaml sem dashboard_embarcadores.usuario/.senha -- não dá pra capturar o dashboard.")
        return False

    logger.info(f"Capturando o dashboard em {URL_DASHBOARD_LOCAL}...")
    try:
        capturar_dashboard(URL_DASHBOARD_LOCAL, usuario, senha, caminho_png, caminho_pdf)
    except Exception as e:
        logger.error(f"Falha ao capturar o dashboard (ele está rodando? StokkiEventos_DashboardApp ativo?): {e}")
        return False

    assunto, corpo_html = _montar_email(caminho_png)

    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = assunto
        msg["From"] = config_email.get("remetente", DESTINATARIO_PADRAO)
        msg["To"] = destinatario

        relacionado = MIMEMultipart("related")
        alternativo = MIMEMultipart("alternative")
        alternativo.attach(MIMEText(corpo_html, "html", "utf-8"))
        relacionado.attach(alternativo)

        for cid, caminho in [("logo_freshlog", LOGO_PATH), ("foto_dashboard", caminho_png)]:
            if Path(caminho).exists():
                with open(caminho, "rb") as f:
                    img = MIMEImage(f.read())
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=Path(caminho).name)
                relacionado.attach(img)

        msg.attach(relacionado)

        with open(caminho_pdf, "rb") as f:
            from email.mime.application import MIMEApplication
            anexo_pdf = MIMEApplication(f.read(), _subtype="pdf")
        anexo_pdf.add_header("Content-Disposition", "attachment", filename="dashboard_embarcadores.pdf")
        msg.attach(anexo_pdf)

        usuario_smtp = config_email.get("remetente", DESTINATARIO_PADRAO)
        senha_smtp = config_email.get("senha_app") or config_email.get("senha", "")
        host = config_email.get("smtp_host", "smtp.gmail.com")
        port = int(config_email.get("smtp_port", 587))

        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(usuario_smtp, senha_smtp)
            smtp.send_message(msg)
        logger.info(f"Relatório semanal do dashboard enviado para {destinatario}.")
        return True
    except Exception as e:
        logger.error(f"Falha ao enviar o relatório semanal do dashboard: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Envia foto + PDF do dashboard de embarcadores por e-mail")
    parser.add_argument("--destinatario", default=DESTINATARIO_PADRAO,
                        help=f"E-mail de destino (padrão: {DESTINATARIO_PADRAO})")
    args = parser.parse_args()

    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    enviado = enviar_relatorio(args.destinatario, config.get("email", {}))
    if not enviado:
        sys.exit(1)


if __name__ == "__main__":
    main()
