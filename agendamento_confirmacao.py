# -*- coding: utf-8 -*-
"""
agendamento_confirmacao.py

Solicitação de confirmação de agendamento de entrega por pedido —
portado de notificador_email.py::notificar_pedidos_sem_agendamento do
agente_relatorio, adaptado para usar email_utils.py deste projeto e
com uma diferença de conteúdo pedida pelo Hugo (29/07): o e-mail avisa
que, se o agendamento já foi informado na mensagem do pedido na Stokki,
não é necessário responder.

Fluxo (ver integração em pipeline.py):
  1. Destinatário está no cadastro de clientes_agendamento (exige
     agendamento) MAS nenhum horário foi extraído das mensagens da
     Stokki (regras/endereco.py já tentou e não achou nada).
  2. Antes de mandar e-mail, buscar_confirmacao() confere se JÁ existe
     uma confirmação (de uma resposta anterior, lida por
     ler_respostas_agendamento.py) — se sim, usa ela, sem mandar e-mail.
  3. Sem confirmação ainda: enviar_solicitacao() manda o e-mail (no
     máximo 1x por dia por pedido, e nunca mais depois de RESPONDIDO —
     mesmo limitador do agente_relatorio) e registra em
     agendamentos_pedido com status PENDENTE.
  4. O pedido segue SEM scheduled_start/end até a confirmação chegar
     (rodada seguinte do pipeline, depois que ler_respostas_agendamento.py
     tiver processado a resposta).
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from email_utils import enviar_email, envelope_html, COR_PRIMARIA, COR_PRIMARIA_CLARA, COR_TEXTO, COR_TEXTO_SUAVE, COR_BORDA, COR_ACENTO, COR_DESTAQUE

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def buscar_confirmacao(pedido: str) -> dict | None:
    """
    Retorna a confirmação de agendamento já recebida pra este pedido
    (status='RESPONDIDO'), ou None se ainda não houver resposta.
    Dict: {"data": "DD/MM/YYYY", "inicio": "HH:MM", "fim": "HH:MM"}
    """
    if not DB_PATH.exists():
        return None
    try:
        conn = _conectar()
        row = conn.execute(
            "SELECT data_agendada, horario_inicio_agendado, horario_fim_agendado "
            "FROM agendamentos_pedido WHERE pedido = ? AND status = 'RESPONDIDO'",
            (pedido,),
        ).fetchone()
        conn.close()
        if row and row["data_agendada"]:
            return {
                "data": row["data_agendada"],
                "inicio": row["horario_inicio_agendado"] or "08:00",
                "fim": row["horario_fim_agendado"] or "18:00",
            }
    except Exception as e:
        logger.debug(f"Erro ao buscar confirmação de agendamento para {pedido}: {e}")
    return None


def _pode_notificar(pedido: str) -> tuple[bool, str]:
    """
    Verifica se o pedido pode receber notificação de agendamento hoje.
    Retorna (pode_notificar, motivo_bloqueio). Bloqueado se:
      - já existe registro com status != 'PENDENTE' (já respondido/ignorado)
      - já foi solicitado HOJE (no máximo 1 e-mail por dia por pedido)
    Mesmo limitador do agente_relatorio (notificador_email.py).
    """
    try:
        conn = _conectar()
        row = conn.execute(
            "SELECT status, solicitado_em FROM agendamentos_pedido WHERE pedido = ?",
            (pedido,),
        ).fetchone()
        conn.close()

        if row is None:
            return True, ""  # primeiro envio — libera

        if row["status"] and row["status"].upper() not in ("PENDENTE", ""):
            return False, f"já respondido (status={row['status']})"

        if row["solicitado_em"]:
            data_envio = str(row["solicitado_em"])[:10]
            hoje = datetime.now().strftime("%Y-%m-%d")
            if data_envio == hoje:
                return False, f"já enviado hoje ({row['solicitado_em']})"

        return True, ""
    except Exception as e:
        logger.warning(f"Erro ao verificar limitador de agendamento para {pedido}: {e}")
        return True, ""  # em caso de erro, permite o envio


def _registrar_solicitacao(pedido: str, cnpj_dest: str, nome_dest: str,
                           cnpj_emb: str, email_emb: str, numero_nf: str = ""):
    """Registra/atualiza a solicitação enviada, para rastreio e o limitador diário."""
    conn = _conectar()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO agendamentos_pedido
            (pedido, cnpj_destinatario, nome_destinatario, cnpj_embarcador,
             email_embarcador, numero_nf, status, solicitado_em)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDENTE', ?)
        ON CONFLICT(pedido) DO UPDATE SET
            solicitado_em = excluded.solicitado_em,
            email_embarcador = excluded.email_embarcador
    """, (pedido, cnpj_dest, nome_dest, cnpj_emb, email_emb, numero_nf, agora))
    conn.commit()
    conn.close()


def enviar_solicitacao(pedido: str, nome_dest: str, numero_nf: str,
                       cnpj_dest: str, cnpj_emb: str, email_emb: str,
                       config_email: dict) -> tuple[bool, str]:
    """
    Envia o e-mail de solicitação de confirmação de agendamento pro
    embarcador, respeitando o limitador (1x/dia, nunca após resposta).

    Retorna (enviado: bool, motivo: str) — motivo preenchido quando
    NÃO enviado (bloqueado pelo limitador, sem e-mail cadastrado, ou
    falha no envio), pra registrar/logar na chamada.
    """
    if not email_emb:
        return False, "embarcador sem e-mail cadastrado"

    pode, motivo_bloqueio = _pode_notificar(pedido)
    if not pode:
        return False, motivo_bloqueio

    assunto = f"Agendamento de entrega — {nome_dest} (Pedido {pedido} / NF {numero_nf or '—'})"

    # Marcador oculto: mesmo formato do agente_relatorio, pra ler_respostas_
    # agendamento.py (portado igual) identificar a thread pelo pedido.
    marcador = (
        f'<div style="display:none">'
        f'[[AGENTE_AGENDAMENTO_EMBARCADOR:{cnpj_emb}]]'
        f'[[PEDIDO:{pedido}]]'
        f'</div>'
    )

    conteudo = f"""
    <p style="margin:0 0 4px 0;font-size:20px;font-weight:700;color:{COR_PRIMARIA};">Agendamento de entrega necessário</p>
    <p style="margin:0 0 24px 0;font-size:14px;color:{COR_TEXTO_SUAVE};">
      Este cliente exige agendamento prévio. Precisamos da data e horário de entrega deste pedido.
    </p>

    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="border:1px solid {COR_BORDA};border-top:2px solid {COR_ACENTO};border-radius:8px;overflow:hidden;margin-bottom:20px;">
      <tr>
        <td style="padding:14px 18px;background-color:{COR_PRIMARIA_CLARA};">
          <p style="margin:0 0 2px 0;font-size:11px;font-weight:600;color:{COR_PRIMARIA};text-transform:uppercase;letter-spacing:0.4px;">Cliente</p>
          <p style="margin:0;font-size:16px;font-weight:600;color:{COR_TEXTO};">{nome_dest}</p>
        </td>
      </tr>
      <tr>
        <td style="padding:12px 18px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="font-size:12px;color:{COR_TEXTO_SUAVE};padding-right:16px;">Pedido</td>
              <td style="font-size:13px;color:{COR_TEXTO};font-weight:600;">{pedido}</td>
            </tr>
            <tr>
              <td style="font-size:12px;color:{COR_TEXTO_SUAVE};padding-top:6px;">Nota Fiscal</td>
              <td style="font-size:13px;color:{COR_TEXTO};font-weight:600;padding-top:6px;">{numero_nf or '—'}</td>
            </tr>
          </table>
        </td>
      </tr>
    </table>

    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background-color:#FFF8EC;border-left:4px solid {COR_DESTAQUE};border-radius:6px;margin-bottom:20px;">
      <tr>
        <td style="padding:14px 18px;">
          <p style="margin:0;font-size:14px;color:{COR_TEXTO};">
            <strong>Este pedido não será roteirizado até recebermos a confirmação</strong> da
            data e horário de entrega agendado com o cliente.
          </p>
        </td>
      </tr>
    </table>

    <p style="margin:0 0 8px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      Por favor, <strong>responda este e-mail</strong> informando a <strong>data</strong> e o
      <strong>horário</strong> agendados (ex: "dia 25/06 às 14h") — nosso sistema atualiza o
      pedido automaticamente.
    </p>

    <p style="margin:16px 0 0 0;font-size:13px;color:{COR_TEXTO_SUAVE};">
      Se esse agendamento já foi informado na mensagem do próprio pedido na Stokki,
      <strong>não é necessário responder este e-mail</strong> — nosso sistema já vai
      capturar essa informação automaticamente.
    </p>

    <p style="margin:24px 0 0 0;font-size:14px;color:{COR_TEXTO};">
      Atenciosamente,<br><strong>Freshlog Logística</strong>
    </p>
    {marcador}
    """
    corpo = envelope_html(conteudo)

    enviado = enviar_email([email_emb], assunto, corpo, config_email)
    if enviado:
        _registrar_solicitacao(pedido, cnpj_dest, nome_dest, cnpj_emb, email_emb, numero_nf)
        return True, ""
    return False, "falha ao enviar e-mail"
