# -*- coding: utf-8 -*-
"""
notificar_agendamento_dia_fixo.py

Avisa o remetente (embarcador) quando pedidos dele são agendados
automaticamente pro dia fixo de entrega da região/ponto (pedido do
Hugo, 12/08: "quando houver esse processamento de ajuste de datas,
precisamos criar uma notificação aos clientes que o pedido deles foi
agendado para a data correta").

Recebe uma lista de itens {"servico", "regiao", "dias", "data"} (e
opcionalmente "data_original", quando já havia uma data e ela foi
MOVIDA pelo dia fixo), agrupa por remetente e manda 1 e-mail por
remetente com a tabela pedido -> região -> data. Quem chama (13/08 --
antes só aplicar_regioes_dia_fixo):
  - aplicar_regioes_dia_fixo (criar_rotas_diarias/incrementar_rotas):
    pedido sem data nenhuma agendado pro dia da região;
  - pipeline.py: data que chega pronta na criação (mensagem da Stokki/
    confirmação) caindo em dia sem entrega -- vem com "data_original";
  - atualizar_agendamentos_confirmados.py: data confirmada por e-mail
    caindo em dia sem entrega -- idem.
Sem fingerprint próprio: cada fluxo garante que o mesmo pedido não é
notificado duas vezes (aplicar_regioes_dia_fixo só agenda quem não
tinha scheduled_start; o pipeline compara com a data já no VUUPT; o
agente de confirmados marca aplicado_vuupt_em).

Mesmo padrão de notificar_agendamento_pendente.py (agrupamento por
sender_id + e-mails do BD Interno + envelope visual da Freshlog).
"""
import html
import logging
import sys
from collections import defaultdict
from pathlib import Path

_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(Path(__file__).parent))  # regioes_dia_fixo é módulo irmão

from email_utils import (
    envelope_html, enviar_email, COR_PRIMARIA, COR_TEXTO, COR_BORDA, COR_FUNDO, COR_ACENTO,
)
from regioes_dia_fixo import nomes_dias
import preferencias_notificacao

logger = logging.getLogger(__name__)

EMAIL_TESTE = "hugo@freshlogbr.com"


def _carregar_embarcadores_por_sender_id() -> dict:
    """Nome, e-mails e a chave "desligado" de cada remetente -- vem das
    preferências do portal (17/09: o embarcador escolhe no botão
    Notificações o e-mail e se quer receber os avisos de agendamento)."""
    return preferencias_notificacao.carregar_embarcadores("agendamento")


def _montar_conteudo(nome_remetente: str, itens: list[dict]) -> str:
    def _celula_data(i: dict) -> str:
        # Itens com "data_original" (13/08): a data NÃO nasceu em branco --
        # já existia (planilha/confirmação/mensagem) mas caía num dia sem
        # entrega na região, e foi movida. Mostra as duas pra ficar claro.
        celula = f"<strong>{i['data'].strftime('%d/%m/%Y')}</strong>"
        if i.get("data_original"):
            celula += (f"<br><span style=\"font-size:11px;color:{COR_TEXTO};\">"
                       f"no lugar de {i['data_original'].strftime('%d/%m')} "
                       f"(dia sem entrega na região)</span>")
        return celula

    linhas = "".join(f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape('#' + (i['servico'].get('code', '') or '').lstrip('#'))}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape((i['servico'].get('title') or '')[:50])}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape(i['regiao'])} ({nomes_dias(i['dias'])})</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{_celula_data(i)}</td>
    </tr>""" for i in itens)

    obs_ajuste = ""
    if any(i.get("data_original") for i in itens):
        obs_ajuste = (
            " Um ou mais pedidos tinham uma <strong>data prevista</strong> que cai num dia em que a "
            "região não recebe — nesses casos, o agendamento foi movido para a próxima data válida."
        )

    return f"""
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_ACENTO};letter-spacing:0.5px;">
  AGENDAMENTO AUTOMÁTICO — DIA FIXO DE ENTREGA
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Pedidos agendados para o dia de entrega da região
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Olá, {html.escape(nome_remetente or '')}.<br>
  Os pedidos abaixo têm destino em regiões (ou pontos de entrega) atendidos em
  <strong>dias fixos da semana</strong>, e por isso foram agendados automaticamente
  para a <strong>próxima data de entrega</strong> da região, conforme a tabela.{obs_ajuste}
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Descrição</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Região (dias de entrega)</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Data agendada</th>
</tr></thead><tbody>{linhas}</tbody></table>
<p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Caso precise de outra data, basta responder este e-mail.<br><br>
  Atenciosamente,<br><strong>Freshlog Logística</strong>
</p>
"""


def notificar_agendamentos_dia_fixo(agendados: list[dict], config_email: dict,
                                    modo_teste: bool = False) -> dict:
    """
    Agrupa os agendamentos de dia fixo por remetente (sender_id) e
    manda 1 e-mail por remetente. Retorna {"enviados", "falhas",
    "sem_email"} pra quem chama montar um resumo.
    """
    if not agendados:
        return {"enviados": 0, "falhas": 0, "sem_email": 0, "desligados": 0}

    embarcadores = _carregar_embarcadores_por_sender_id()
    grupos = defaultdict(list)
    for item in agendados:
        grupos[item["servico"].get("sender_id")].append(item)

    enviados = falhas = sem_email = desligados = 0
    for sender_id, itens in grupos.items():
        emb = embarcadores.get(sender_id)
        if not emb or not emb["emails"]:
            logger.warning(f"  sender_id={sender_id}: {len(itens)} pedido(s) agendado(s) por dia fixo -- sem e-mail cadastrado.")
            sem_email += 1
            continue
        if emb.get("desligado"):
            logger.info(f"  {emb['nome']}: {len(itens)} pedido(s) agendado(s) por dia fixo -- aviso de agendamento "
                        f"desligado pelo cliente no portal, e-mail NÃO enviado.")
            desligados += 1
            continue

        assunto = f"[Freshlog] Entrega agendada para o dia da região — {len(itens)} pedido(s)"
        conteudo = _montar_conteudo(emb["nome"], itens)
        corpo = envelope_html(conteudo, rodape="Mensagem automática — Agente Stokki Eventos.")
        destinos = [EMAIL_TESTE] if modo_teste else emb["emails"]

        if modo_teste:
            logger.info(f"  [TESTE] {emb['nome']} -> {EMAIL_TESTE} (original: {emb['emails']}) | "
                       f"{len(itens)} pedido(s): {[i['servico'].get('code') for i in itens]}")

        if enviar_email(destinos, assunto, corpo, config_email):
            enviados += 1
        else:
            falhas += 1

    return {"enviados": enviados, "falhas": falhas, "sem_email": sem_email, "desligados": desligados}
