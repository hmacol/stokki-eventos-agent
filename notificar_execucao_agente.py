# -*- coding: utf-8 -*-
"""
notificar_execucao_agente.py

Envia um e-mail de resumo a cada execução de uma rotina em lote
(executar_tudo.py, criar_rotas_diarias.py, processar_documentos.py,
lancar_coleta.py... 18 chamadores em 16/09/2026), sucesso ou erro --
pedido do Hugo, 31/07: "criar notificação de agente processado, similar
ao que temos no Agente de importação pelo e-mail".

Uma linha por etapa (nome, status OK/erro, detalhe). Pra Pipeline, o
chamador puxa a contagem detalhada do último registro salvo por
historico.py -- aqui não se duplica lógica de contagem.

16/09/2026: visual alinhado ao relatório da importação Stokki (repo
agente_importacao_stokki/relatorio_email.py): envelope da Freshlog com
logo (email_utils.envelope_html), título = nome da rotina, frase-resumo,
etapas com erro destacadas em vermelho, assunto informativo.

Falha no envio NUNCA derruba a rotina -- só loga o erro.

Ativado/desativado via config.yaml:
    notificacao_execucao:
      ativo: true
      destinatario: hugo@freshlogbr.com   # opcional, usa email.remetente por padrão
"""
import logging
import sys
from datetime import datetime
from html import escape
from pathlib import Path

from email_utils import (COR_ACENTO, COR_BORDA, COR_DESTAQUE, COR_ERRO, COR_FUNDO, COR_PRIMARIA,
                         COR_TEXTO, COR_TEXTO_SUAVE, envelope_html, enviar_email)

logger = logging.getLogger(__name__)

COR_ERRO_CLARA = "#FEF2F2"
COR_ACENTO_ESCURO = "#00A37A"

# Nome legível pra rotinas que reportam várias etapas (o título de quem
# reporta uma etapa só é o próprio nome dela). Chave = nome do script.
TITULOS_POR_SCRIPT = {
    "executar_tudo": "Execução completa",
    "criar_rotas_diarias": "Criação de rotas",
    "processar_documentos": "Documentos dos pedidos",
    "notificar_transportadoras": "Notificação de transportadoras",
    "gerar_pdf_romaneios": "PDFs de romaneio",
}
TITULO_PADRAO = "Agente Stokki Eventos"


# --- Texto --------------------------------------------------------------------

def titulo_da_rotina(resumo_etapas: dict, titulo: str | None = None) -> str:
    if titulo:
        return titulo
    if len(resumo_etapas) == 1:
        return next(iter(resumo_etapas))
    script = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    return TITULOS_POR_SCRIPT.get(script, TITULO_PADRAO)


def formatar_duracao(segundos: float) -> str:
    segundos = max(0.0, float(segundos or 0))
    if segundos < 60:
        return f"{segundos:.0f} s"
    minutos = segundos / 60
    if minutos < 60:
        return f"{minutos:.1f} min".replace(".", ",")
    return f"{minutos / 60:.1f} h".replace(".", ",")


def etapas_com_erro(resumo_etapas: dict) -> list[str]:
    return [nome for nome, info in resumo_etapas.items() if (info or {}).get("status") != "ok"]


def resumir_execucao(resumo_etapas: dict) -> str:
    total = len(resumo_etapas)
    erros = etapas_com_erro(resumo_etapas)
    if not total:
        return "Nenhuma etapa reportada."
    if not erros:
        return "Etapa concluída sem erro." if total == 1 else f"{total} etapas concluídas sem erro."
    if total == 1:
        return f"A etapa terminou com erro."
    return f"{len(erros)} de {total} etapas com erro: {', '.join(erros)}."


def montar_assunto(resumo_etapas: dict, modo_teste: bool, titulo: str | None = None,
                   agora: datetime | None = None) -> str:
    """'Agente Stokki Eventos · Criação de rotas — ✅ OK (16/09 22:05)'.
    Mantém 'Agente Stokki Eventos' por causa dos filtros do Gmail."""
    erros = etapas_com_erro(resumo_etapas)
    nome = titulo_da_rotina(resumo_etapas, titulo)
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    prefixo = "[MODO TESTE] " if modo_teste else ""
    cabeca = TITULO_PADRAO if nome == TITULO_PADRAO else f"{TITULO_PADRAO} · {nome}"
    if erros:
        miolo = "❌ erro em " + ", ".join(erros) if len(resumo_etapas) > 1 else "❌ com erro"
    else:
        miolo = "✅ OK"
    return f"{prefixo}{cabeca} — {miolo} ({quando})"


# --- HTML ---------------------------------------------------------------------

def _pilula(texto: str, cor_fundo: str) -> str:
    return (f'<span style="display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;'
            f'font-weight:700;letter-spacing:.3px;color:#FFFFFF;background:{cor_fundo};">{texto}</span>')


def _linha_etapa(nome: str, info: dict) -> str:
    info = info or {}
    ok = info.get("status") == "ok"
    detalhe = escape(str(info.get("detalhe", "") or "")).replace("\n", "<br>")
    fundo = "" if ok else f"background:{COR_ERRO_CLARA};"
    borda_esq = "" if ok else f"border-left:4px solid {COR_ERRO};"
    return f"""
      <tr>
        <td width="34%" style="padding:12px 14px;border-top:1px solid {COR_BORDA};{fundo}{borda_esq}vertical-align:top;">
          <span style="font-size:14px;font-weight:700;color:{COR_PRIMARIA};">{escape(nome)}</span><br>
          {_pilula("OK", COR_ACENTO) if ok else _pilula("ERRO", COR_ERRO)}
        </td>
        <td style="padding:12px 14px;border-top:1px solid {COR_BORDA};{fundo}font-size:13px;line-height:1.5;color:{COR_TEXTO};vertical-align:top;">{detalhe}</td>
      </tr>"""


def montar_html(resumo_etapas: dict, duracao_total_seg: float, modo_teste: bool,
                titulo: str | None = None, agora: datetime | None = None) -> str:
    erros = etapas_com_erro(resumo_etapas)
    nome = escape(titulo_da_rotina(resumo_etapas, titulo))
    quando = (agora or datetime.now()).strftime("%d/%m/%Y às %H:%M")
    pilula = _pilula("Com erro", COR_ERRO) if erros else _pilula("Sucesso", COR_ACENTO)
    if modo_teste:
        pilula += " " + _pilula("Modo teste", COR_DESTAQUE)
    linhas = "".join(_linha_etapa(n, i) for n, i in resumo_etapas.items())

    conteudo = f"""
      <p style="margin:0 0 8px 0;font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:{COR_TEXTO_SUAVE};">Rotina automática · Agente Stokki Eventos</p>
      <p style="margin:0 0 6px 0;font-size:22px;font-weight:800;color:{COR_PRIMARIA};line-height:1.2;">{nome}</p>
      <p style="margin:0 0 14px 0;font-size:13px;color:{COR_TEXTO_SUAVE};">Execução de {quando} · {formatar_duracao(duracao_total_seg)} &nbsp;{pilula}</p>
      <p style="margin:0 0 20px 0;font-size:16px;color:{COR_TEXTO};line-height:1.5;">{escape(resumir_execucao(resumo_etapas))}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid {COR_BORDA};border-radius:8px;border-collapse:separate;overflow:hidden;">
        <tr style="background:{COR_FUNDO};">
          <th align="left" style="padding:8px 14px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:{COR_TEXTO_SUAVE};">Etapa</th>
          <th align="left" style="padding:8px 14px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:{COR_TEXTO_SUAVE};">O que aconteceu</th>
        </tr>{linhas}
      </table>"""

    cor_faixa = COR_DESTAQUE if modo_teste else (COR_ERRO if erros else COR_ACENTO)
    return envelope_html(conteudo, cor_acento=cor_faixa,
                         rodape="Relatório automático de execução do Agente Stokki Eventos, Freshlog. "
                                "Enviado a cada rodada da rotina, com ou sem erro.")


# --- Envio ---------------------------------------------------------------------

def notificar_execucao(resumo_etapas: dict, duracao_total_seg: float,
                       modo_teste: bool, config: dict, titulo: str | None = None):
    """
    Envia o e-mail de resumo da execução. Nunca levanta exceção --
    falha no envio não pode derrubar a rotina que chamou.
    """
    cfg_notif = config.get("notificacao_execucao", {})
    if not cfg_notif.get("ativo", True):
        logger.debug("Notificação de execução desativada (notificacao_execucao.ativo=false).")
        return

    config_email = config.get("email", {})
    destinatario = cfg_notif.get("destinatario") or config_email.get("remetente", "hugo@freshlogbr.com")

    try:
        assunto = montar_assunto(resumo_etapas, modo_teste, titulo)
        corpo = montar_html(resumo_etapas, duracao_total_seg, modo_teste, titulo)
        if enviar_email([destinatario], assunto, corpo, config_email):
            logger.info(f"Notificação de execução enviada para {destinatario}.")
    except Exception as e:
        logger.warning(f"Falha ao enviar notificação de execução (não afeta o resultado): {e}")
