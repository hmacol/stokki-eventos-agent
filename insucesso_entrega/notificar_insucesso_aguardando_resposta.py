# -*- coding: utf-8 -*-
"""
notificar_insucesso_aguardando_resposta.py

NOVA REGRA (pedido do Hugo, 11/08): TODO insucesso é duplicado
imediatamente (ver motivos_falha.py::deve_duplicar) e este módulo
manda o AVISO ao remetente: "seus pedidos foram duplicados pra
reentrega no próximo dia útil; responda se quiser cancelar". A
resposta é lida por ler_respostas_insucesso.py, que cancela a
reentrega no VUUPT quando o remetente pedir.
(Regra anterior, 03/08: alguns motivos perguntavam ANTES de duplicar
e aguardavam resposta -- o esqueleto de agrupamento/fingerprint/
marcador é o mesmo, só o texto e o momento da duplicação mudaram.)

Agrupa por (remetente, motivo) -- cada motivo tem uma pergunta
diferente, então viram e-mails separados mesmo pro mesmo remetente.
Rate-limit diário (fingerprint_aguardando_resposta.py): no máximo 1
e-mail por dia por pedido, até a resposta chegar (status muda pra algo
diferente de PENDENTE -- a leitura/parsing automático da resposta vem
de ler_respostas_insucesso.py).

Layout do e-mail (06/08, pedido do Hugo: "adequar pro padrão que já
temos em outros agentes"): usa email_utils.py (módulo central da
identidade visual Freshlog) em vez de montar o HTML na mão -- estava
com cor de destaque errada (roxo, não a #00C896 padrão) e o logo
embutido de um jeito ligeiramente diferente do resto do projeto.
"""
import html
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))

from email_utils import envelope_html, enviar_email, COR_PRIMARIA, COR_TEXTO, COR_BORDA, COR_FUNDO

logger = logging.getLogger(__name__)

EMAIL_TESTE = "hugo@freshlogbr.com"


def identificar_aguardando_resposta(insucessos: list[dict]) -> list[dict]:
    """
    NOVA REGRA (pedido do Hugo, 11/08): TODOS os insucessos geram o
    aviso de duplicação ao remetente (não só os motivos marcados com
    aguarda_resposta, que era a regra de 03/08). O filtro que fica é o
    rate-limit do fingerprint (pode_notificar): 1 e-mail por dia por
    pedido, até chegar resposta.
    """
    from fingerprint_aguardando_resposta import pode_notificar

    return [s for s in insucessos if pode_notificar(s.get("id"))]


def _carregar_embarcadores_por_sender_id() -> dict:
    import sqlite3
    db_path = _RAIZ_PROJETO / "dados" / "dados.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Banco nao encontrado: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sender_id, nome_remetente, apelido, email FROM interno WHERE sender_id IS NOT NULL"
    ).fetchall()
    conn.close()
    embs = {}
    for r in rows:
        raw = r["email"] or ""
        emails = [e.strip() for e in re.split(r"[,;\t]+", raw) if e.strip() and "@" in e]
        embs[r["sender_id"]] = {"nome": r["apelido"] or r["nome_remetente"] or "", "emails": emails}
    return embs


def _montar_conteudo(nome_remetente: str, motivo_texto: str, pedidos: list[dict],
                     sender_id, failed_reason_id, dias_uteis_atraso: int | None = None) -> str:
    """Só o CONTEÚDO (título, aviso de duplicação, tabela) -- o envelope
    (logo, cores, rodapé) vem de email_utils.envelope_html().

    Nova regra (Hugo, 11/08): o e-mail deixou de ser uma PERGUNTA
    ("podemos reenviar?") e virou um AVISO ("já duplicamos; responda
    se quiser cancelar"). Pra motivos de duplicação agendada
    (dias_uteis_atraso), o texto informa o prazo em dias úteis."""
    linhas = "".join(f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape('#' + (p.get('code','') or '').lstrip('#'))}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape((p.get('title') or '')[:60])}</td>
    </tr>""" for p in pedidos)

    if dias_uteis_atraso:
        prazo = (f"A nova tentativa está programada para "
                 f"<strong>{dias_uteis_atraso} dia(s) útil(eis)</strong> após a ocorrência.")
    else:
        prazo = "A nova tentativa está programada para o <strong>próximo dia útil</strong>."

    aviso = (
        f"Os pedidos abaixo tiveram <strong>insucesso na entrega</strong> "
        f"(motivo: {html.escape(motivo_texto)}) e <strong>já foram duplicados</strong> "
        f"para uma nova tentativa. {prazo}<br><br>"
        "Caso <strong>não</strong> deseje o reenvio, basta responder este e-mail "
        "solicitando o cancelamento. Sem resposta, a reentrega segue normalmente."
    )

    return f"""
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_PRIMARIA};letter-spacing:0.5px;">
  INSUCESSO NA ENTREGA — {html.escape(motivo_texto.upper())}
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Pedidos duplicados para reentrega
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Olá, {html.escape(nome_remetente or '')}.<br>{aviso}
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Descrição</th>
</tr></thead><tbody>{linhas}</tbody></table>
<p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Atenciosamente,<br><strong>Freshlog Logística</strong>
</p>
<p style="margin:16px 0 0 0;font-size:1px;color:{COR_FUNDO};">[[INSUCESSO_GRUPO:{sender_id}:{failed_reason_id}]]</p>
"""


def notificar_remetentes(pendentes: list[dict], config_email: dict, modo_teste: bool = False) -> dict:
    """
    Agrupa por (remetente, motivo) -- cada motivo tem pergunta
    diferente -- e manda 1 e-mail por combinação. Marca cada pedido
    como notificado (fingerprint_aguardando_resposta.py), mesmo em
    modo_teste NÃO marca (deixa livre pra testar de novo).
    """
    from motivos_falha import texto_do_motivo, duplicar_com_atraso
    from fingerprint_aguardando_resposta import marcar_notificado

    embarcadores = _carregar_embarcadores_por_sender_id()
    grupos = defaultdict(list)
    for s in pendentes:
        grupos[(s.get("sender_id"), s.get("failed_reason_id"))].append(s)

    enviados = falhas = sem_email = 0
    for (sender_id, failed_reason_id), pedidos in grupos.items():
        emb = embarcadores.get(sender_id)
        if not emb or not emb["emails"]:
            logger.warning(f"  sender_id={sender_id}: {len(pedidos)} pedido(s) aguardando resposta -- sem e-mail cadastrado.")
            sem_email += 1
            continue

        motivo_texto = texto_do_motivo(failed_reason_id)
        # "aguardando retorno" segue no assunto de propósito: é o que o
        # ler_respostas_insucesso.py usa pra filtrar as respostas no IMAP.
        assunto = (f"[Freshlog] {motivo_texto} — {len(pedidos)} pedido(s) "
                   f"duplicado(s) para reentrega, aguardando retorno")
        conteudo = _montar_conteudo(emb["nome"], motivo_texto, pedidos, sender_id,
                                    failed_reason_id,
                                    dias_uteis_atraso=duplicar_com_atraso(failed_reason_id))
        corpo = envelope_html(conteudo, rodape="Mensagem automática — Agente Stokki Eventos.")
        destinos = [EMAIL_TESTE] if modo_teste else emb["emails"]

        if modo_teste:
            logger.info(f"  [TESTE] {emb['nome']} ({motivo_texto}) -> {EMAIL_TESTE} "
                       f"(original: {emb['emails']}) | {len(pedidos)} pedido(s): "
                       f"{[p.get('code') for p in pedidos]}")

        if enviar_email(destinos, assunto, corpo, config_email):
            enviados += 1
            if not modo_teste:
                for p in pedidos:
                    marcar_notificado(p["id"], failed_reason_id, sender_id=sender_id, code=p.get("code"))
        else:
            falhas += 1

    return {"enviados": enviados, "falhas": falhas, "sem_email": sem_email}
