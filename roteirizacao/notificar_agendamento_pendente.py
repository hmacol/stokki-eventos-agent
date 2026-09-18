# -*- coding: utf-8 -*-
"""
notificar_agendamento_pendente.py

Pedido do Hugo, 02/08: "Todo o Destinatário que tiver Agendamento e
estiver com o agendamento em branco ou com data antiga, enviar
notificação ao Remetente solicitando data e horário do agendamento
urgente."

Fluxo:
  1. Entre os pedidos not_assigned, identifica os cujo DESTINATÁRIO
     exige agendamento (regras/clientes_agendamento.py, mesma fonte
     usada na importação) mas o scheduled_start no VUUPT está em
     BRANCO ou com data ANTERIOR a hoje (agendamento vencido/nunca
     confirmado).
  2. Agrupa esses pedidos por REMETENTE (sender_id) e manda 1 e-mail
     urgente por remetente, listando os pedidos pendentes, pedindo
     data/horário do agendamento.

Só busca o customer (pra achar o documento/CNPJ do destinatário, via
buscar_customer_por_id) para os pedidos que JÁ estão sem scheduled_start
válido -- evita gastar uma chamada de API por pedido pra quem já tem
agendamento em dia.
"""
import html
import logging
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))

from email_utils import envelope_html, enviar_email, COR_PRIMARIA, COR_ERRO, COR_TEXTO, COR_BORDA, COR_FUNDO
import preferencias_notificacao

logger = logging.getLogger(__name__)

EMAIL_TESTE = "hugo@freshlogbr.com"


def _precisa_de_agendamento_valido(servico: dict, hoje: date) -> bool:
    """True se o scheduled_start está em branco OU com data anterior a
    hoje (agendamento vencido/nunca preenchido)."""
    scheduled_start = servico.get("scheduled_start")
    if not scheduled_start:
        return True
    try:
        data_agendada = datetime.fromisoformat(scheduled_start).date()
    except (ValueError, TypeError):
        return False  # formato inesperado -- não arrisca marcar como pendente
    return data_agendada < hoje


def identificar_pendentes(servicos: list[dict], conjunto_agendamento: set[str],
                          vuupt, tem_agendamento_fn, hoje: date | None = None,
                          buscar_confirmacao_fn=None) -> list[dict]:
    """
    Filtra os serviços cujo destinatário exige agendamento (documento
    no conjunto_agendamento) mas o scheduled_start está em branco ou
    vencido. `tem_agendamento_fn` é a função tem_agendamento() de
    regras/clientes_agendamento.py (injetada pra facilitar teste).

    `buscar_confirmacao_fn` (opcional, normalmente agendamento_
    confirmacao.py::buscar_confirmacao) — quando informado, pedidos que
    JÁ têm uma confirmação recebida (aguardando só o agente de
    atualização aplicar no VUUPT) NÃO entram na lista de pendentes —
    já foram resolvidos do lado do e-mail, só falta a aplicação técnica
    (pedido do Hugo, 02/08: não notificar de novo quem já respondeu).
    """
    hoje = hoje or date.today()
    pendentes = []
    for s in servicos:
        if not _precisa_de_agendamento_valido(s, hoje):
            continue
        if buscar_confirmacao_fn and buscar_confirmacao_fn(s.get("code", "").lstrip("#")):
            continue  # já respondido, só falta aplicar -- não é mais "pendente"
        customer_id = s.get("customer_id")
        if not customer_id:
            continue
        customer = vuupt.buscar_customer_por_id(customer_id)
        time.sleep(0.3)  # evita rajada de chamadas (achado em produção, 02/08: causava HTTP 429)
        if not customer:
            continue
        documento = customer.get("code", "")
        if tem_agendamento_fn(documento, conjunto_agendamento):
            # destinatário guardado no próprio dict (09/09): o e-mail
            # urgente registra a solicitação em agendamentos_pedido, pra
            # resposta dele ser lida (ler_respostas_agendamento.py)
            s["_destinatario_doc"] = documento
            s["_destinatario_nome"] = customer.get("name", "") or ""
            pendentes.append(s)
    return pendentes


def _carregar_embarcadores_por_sender_id() -> dict:
    """Nome, e-mails, cnpj e a chave "desligado" de cada remetente -- vem
    das preferências do portal (17/09: o embarcador escolhe no botão
    Notificações o e-mail e se quer receber os avisos de agendamento)."""
    return preferencias_notificacao.carregar_embarcadores("agendamento")


def _codigo_pedido(servico: dict) -> str:
    return (servico.get("code", "") or "").lstrip("#")


def _montar_conteudo(nome_remetente: str, pedidos: list[dict], cnpj_emb: str = "") -> str:
    linhas = "".join(f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape('#' + _codigo_pedido(p))}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape((p.get('title','') or '')[:60])}</td>
    </tr>""" for p in pedidos)

    # Marcadores ocultos (09/09): mesmo formato do e-mail de solicitação
    # por pedido (agendamento_confirmacao.py) -- ler_respostas_agendamento
    # .py acha a thread pelo(s) pedido(s) e aplica a resposta. Sem isso a
    # resposta a ESTE e-mail era ignorada.
    marcador = (
        '<div style="display:none">'
        + (f"[[AGENTE_AGENDAMENTO_EMBARCADOR:{cnpj_emb}]]" if cnpj_emb else "")
        + "".join(f"[[PEDIDO:{_codigo_pedido(p)}]]" for p in pedidos)
        + "</div>"
    )

    return f"""{marcador}
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_ERRO};letter-spacing:0.5px;">URGENTE</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Confirmação de agendamento pendente
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Olá, {html.escape(nome_remetente or '')}.<br>
  Os pedidos abaixo têm destinatários que exigem agendamento de entrega, mas ainda não há uma
  data e horário confirmados (ou o agendamento já está vencido). Solicitamos o envio da data e
  do horário de agendamento com a maior brevidade possível, para evitar atrasos na entrega.
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Descrição</th>
</tr></thead><tbody>{linhas}</tbody></table>
<p style="margin:16px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Por favor, <strong>responda este e-mail</strong> informando, para cada pedido, a <strong>data</strong> e o
  <strong>horário</strong> em que o destinatário recebe (ex: "PS-12345 dia 25/06 das 8h às 12h") — nosso
  sistema atualiza os pedidos automaticamente e a rota é montada respeitando esse horário.
</p>
<p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Atenciosamente,<br><strong>Freshlog Logística</strong>
</p>
"""


def notificar_remetentes(pendentes: list[dict], config_email: dict, modo_teste: bool = False) -> dict:
    """
    Agrupa os pedidos pendentes por remetente (sender_id) e manda 1
    e-mail urgente por remetente. Retorna {"enviados", "falhas",
    "sem_email"} pra quem chama montar um resumo.
    """
    embarcadores = _carregar_embarcadores_por_sender_id()
    grupos = defaultdict(list)
    for s in pendentes:
        grupos[s.get("sender_id")].append(s)

    enviados = falhas = sem_email = desligados = 0
    for sender_id, pedidos in grupos.items():
        emb = embarcadores.get(sender_id)
        if not emb or not emb["emails"]:
            logger.warning(f"  sender_id={sender_id}: {len(pedidos)} pedido(s) pendente(s) -- sem e-mail cadastrado.")
            sem_email += 1
            continue
        if emb.get("desligado"):
            logger.info(f"  {emb['nome']}: {len(pedidos)} pedido(s) pendente(s) -- aviso de agendamento "
                        f"desligado pelo cliente no portal, e-mail NÃO enviado.")
            desligados += 1
            continue

        assunto = f"[URGENTE] Confirmação de agendamento necessária — {len(pedidos)} pedido(s)"
        conteudo = _montar_conteudo(emb["nome"], pedidos, emb.get("cnpj", ""))
        corpo = envelope_html(conteudo, rodape="Mensagem automática — Agente Stokki Eventos.",
                              cor_acento=COR_ERRO)
        destinos = [EMAIL_TESTE] if modo_teste else emb["emails"]

        if modo_teste:
            logger.info(f"  [TESTE] {emb['nome']} -> {EMAIL_TESTE} (original: {emb['emails']}) | "
                       f"{len(pedidos)} pedido(s): {[p.get('code') for p in pedidos]}")

        if enviar_email(destinos, assunto, corpo, config_email):
            enviados += 1
            if not modo_teste:
                _registrar_pendentes(pedidos, emb)
        else:
            falhas += 1

    return {"enviados": enviados, "falhas": falhas, "sem_email": sem_email, "desligados": desligados}


def _registrar_pendentes(pedidos: list[dict], emb: dict) -> None:
    """Registra cada pedido do e-mail urgente como solicitação PENDENTE em
    agendamentos_pedido (mesma tabela/limitador de agendamento_
    confirmacao.py) -- é o que permite ler_respostas_agendamento.py
    casar a resposta com o pedido. Pedido já RESPONDIDO não muda de
    status (ON CONFLICT só atualiza solicitado_em/e-mail). Falha aqui
    não afeta o envio, que já aconteceu."""
    try:
        from agendamento_confirmacao import _registrar_solicitacao
        for p in pedidos:
            _registrar_solicitacao(
                _codigo_pedido(p), p.get("_destinatario_doc", "") or "", p.get("_destinatario_nome", "") or "",
                emb.get("cnpj", "") or "", (emb.get("emails") or [""])[0],
            )
    except Exception as e:
        logger.warning(f"Falha ao registrar os pedidos do e-mail urgente como pendentes (resposta pode "
                       f"não casar automaticamente): {e}")
