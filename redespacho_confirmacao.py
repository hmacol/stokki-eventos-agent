# -*- coding: utf-8 -*-
"""
redespacho_confirmacao.py

Solicitação de confirmação de redespacho por pedido — mesmo padrão de
agendamento_confirmacao.py, mas para o caso descrito por regras/endereco.py
(fonte="aguardando_redespacho"): o Local de Entrega do pedido está fora
da área atendida pela Freshlog e a transportadora não tem redespacho
conhecido no cadastro de transportadoras (pedido do Hugo, 10/08, caso
#PS-36198).

Fluxo (ver integração em pipeline.py):
  1. regras/endereco.py já checou: Local de Entrega fora da área
     atendida + transportadora sem redespacho conhecido.
  2. O pedido NÃO é importado no VUUPT nessa rodada -- fica "segurado"
     até o embarcador responder (não há leitura automática da resposta,
     é uma decisão manual: alguém confirma o nome da transportadora e o
     endereço de redespacho, e atualiza o cadastro (tela /transportadoras) -- daí
     em diante o pedido resolve normalmente pela prioridade 3).
  3. enviar_solicitacao() manda o e-mail (no máximo 1x por dia por
     pedido, mesmo limitador do agendamento) e registra em
     redespacho_pendente pra rastreio.
"""
import html
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from controle_notificacoes import decidir
from email_utils import enviar_email, envelope_html, COR_PRIMARIA, COR_PRIMARIA_CLARA, COR_TEXTO, COR_TEXTO_SUAVE, COR_BORDA, COR_DESTAQUE

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS redespacho_pendente (
            pedido            TEXT PRIMARY KEY,
            cidade            TEXT,
            uf                TEXT,
            transportadora    TEXT,
            email_embarcador  TEXT,
            solicitado_em     TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _pode_notificar(pedido: str) -> tuple[bool, str]:
    """
    No máximo 1 e-mail por dia por pedido -- mesmo limitador usado em
    agendamento_confirmacao.py. Não há status "respondido" aqui (a
    resolução é manual, fora do sistema), então o único bloqueio é o
    envio recente.
    """
    conn = _conectar()
    row = conn.execute(
        "SELECT solicitado_em FROM redespacho_pendente WHERE pedido = ?", (pedido,)
    ).fetchone()
    conn.close()

    if row is None:
        return True, ""

    data_envio = str(row["solicitado_em"])[:10]
    hoje = datetime.now().strftime("%Y-%m-%d")
    if data_envio == hoje:
        return False, f"já enviado hoje ({row['solicitado_em']})"
    return True, ""


def _registrar_solicitacao(pedido: str, cidade: str, uf: str, transportadora: str, email_emb: str):
    conn = _conectar()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO redespacho_pendente
            (pedido, cidade, uf, transportadora, email_embarcador, solicitado_em)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(pedido) DO UPDATE SET
            solicitado_em = excluded.solicitado_em,
            email_embarcador = excluded.email_embarcador
    """, (pedido, cidade, uf, transportadora, email_emb, agora))
    conn.commit()
    conn.close()


def enviar_solicitacao(pedido: str, nome_dest: str, cidade: str, uf: str,
                       nome_transportadora: str, email_emb: str,
                       config_email: dict, cnpj_emb: str = "", db_path=None) -> tuple[bool, str]:
    """
    Envia o e-mail pedindo ao embarcador o nome da transportadora de
    redespacho (se ainda não informada) e o endereço completo (com CEP)
    pra onde a mercadoria deve ser enviada, já que a entrega é fora da
    área atendida pela Freshlog.

    Retorna (enviado: bool, motivo: str) -- motivo preenchido quando
    NÃO enviado (bloqueado pelo limitador, sem e-mail cadastrado, ou
    falha no envio).

    Tela de Notificações (controle_notificacoes.py, tipo "redespacho"):
    sem linha na tela vale o de hoje (envia). NAO não envia nem registra
    (o pedido segue segurado e a pergunta sai quando a tela ligar).
    PILOTO manda só pro piloto, com faixa no topo e sem registrar a
    solicitação (o embarcador ainda não foi perguntado); devolve (False,
    motivo). `cnpj_emb` é quem a tela usa pra achar a exceção do embarcador.
    """
    if not email_emb:
        return False, "embarcador sem e-mail cadastrado"

    pode, motivo_bloqueio = _pode_notificar(pedido)
    if not pode:
        return False, motivo_bloqueio

    decisao = decidir("redespacho", cnpj_emb or None, fallback_acao="ENVIAR", db_path=db_path)
    if decisao.acao == "NAO":
        return False, f"bloqueado pela tela de Notificações ({decisao.motivo})"
    piloto = decisao.acao == "PILOTO"
    faixa_piloto = (
        f'<p style="margin:0 0 16px 0;padding:8px 12px;background-color:{COR_PRIMARIA_CLARA};'
        f'border:1px dashed {COR_BORDA};font-size:12px;color:{COR_TEXTO};">'
        f'Piloto: iria para {html.escape(email_emb)}</p>'
    ) if piloto else ""

    assunto = f"Confirmação de redespacho necessária — {nome_dest} (Pedido #{pedido})"

    linha_transportadora = (
        f"<strong>Transportadora informada:</strong> {html.escape(nome_transportadora)}"
        if nome_transportadora else
        "<strong>Nenhuma transportadora informada</strong> neste pedido"
    )

    conteudo = f"""
    {faixa_piloto}
    <p style="margin:0 0 4px 0;font-size:20px;font-weight:700;color:{COR_PRIMARIA};">Confirmação de redespacho necessária</p>
    <p style="margin:0 0 24px 0;font-size:14px;color:{COR_TEXTO_SUAVE};">
      O endereço de entrega deste pedido está fora da área atendida diretamente pela Freshlog.
    </p>

    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="border:1px solid {COR_BORDA};border-top:2px solid {COR_DESTAQUE};border-radius:8px;overflow:hidden;margin-bottom:20px;">
      <tr>
        <td style="padding:14px 18px;background-color:{COR_PRIMARIA_CLARA};">
          <p style="margin:0 0 2px 0;font-size:11px;font-weight:600;color:{COR_PRIMARIA};text-transform:uppercase;letter-spacing:0.4px;">Destinatário</p>
          <p style="margin:0;font-size:16px;font-weight:600;color:{COR_TEXTO};">{html.escape(nome_dest or '')}</p>
        </td>
      </tr>
      <tr>
        <td style="padding:12px 18px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="font-size:12px;color:{COR_TEXTO_SUAVE};padding-right:16px;">Pedido</td>
              <td style="font-size:13px;color:{COR_TEXTO};font-weight:600;">#{html.escape(pedido or '')}</td>
            </tr>
            <tr>
              <td style="font-size:12px;color:{COR_TEXTO_SUAVE};padding-top:6px;">Cidade - UF</td>
              <td style="font-size:13px;color:{COR_TEXTO};font-weight:600;padding-top:6px;">{html.escape(cidade or '-')} - {html.escape(uf or '-')}</td>
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
            <strong>Este pedido não será roteirizado</strong> até recebermos a confirmação
            de como a entrega deve ser feita nessa região.
          </p>
        </td>
      </tr>
    </table>

    <p style="margin:0 0 8px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      {linha_transportadora}.
    </p>

    <p style="margin:0 0 8px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      Por favor, <strong>responda este e-mail</strong> confirmando se haverá <strong>redespacho por
      alguma transportadora</strong>. Em caso afirmativo, informe o <strong>nome da transportadora</strong>
      e o <strong>endereço completo com CEP</strong> para onde a mercadoria deve ser enviada.
    </p>

    <p style="margin:24px 0 0 0;font-size:14px;color:{COR_TEXTO};">
      Atenciosamente,<br><strong>Freshlog Logística</strong>
    </p>
    """
    corpo = envelope_html(conteudo, cor_acento=COR_DESTAQUE)

    destinos = [decisao.destino_piloto] if piloto else [email_emb]
    enviado = enviar_email(destinos, assunto, corpo, config_email)
    if enviado and piloto:
        return False, f"piloto: cópia enviada para {destinos[0]}, embarcador não avisado"
    if enviado:
        _registrar_solicitacao(pedido, cidade, uf, nome_transportadora, email_emb)
        return True, ""
    return False, "falha ao enviar e-mail"
