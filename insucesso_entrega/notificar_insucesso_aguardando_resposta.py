# -*- coding: utf-8 -*-
"""
notificar_insucesso_aguardando_resposta.py

Pedido do Hugo, 03/08: pra alguns motivos de insucesso (ver
motivos_falha.py::aguarda_resposta), em vez de duplicar automaticamente,
notifica o remetente com uma PERGUNTA ESPECÍFICA daquele motivo e
aguarda resposta antes de qualquer ação.

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
    Filtra os insucessos cujo motivo exige aguardar resposta (ver
    motivos_falha.py) e que ainda podem ser notificados (não
    respondidos, e não notificados hoje ainda -- fingerprint_
    aguardando_resposta.py::pode_notificar).
    """
    from motivos_falha import aguarda_resposta
    from fingerprint_aguardando_resposta import pode_notificar

    return [
        s for s in insucessos
        if aguarda_resposta(s.get("failed_reason_id")) and pode_notificar(s.get("id"))
    ]


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


def _montar_conteudo(nome_remetente: str, pergunta: str, motivo_texto: str, pedidos: list[dict],
                     sender_id, failed_reason_id) -> str:
    """Só o CONTEÚDO (título, pergunta, tabela) -- o envelope (logo,
    cores, rodapé) vem de email_utils.envelope_html()."""
    linhas = "".join(f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape('#' + (p.get('code','') or '').lstrip('#'))}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape((p.get('title') or '')[:60])}</td>
    </tr>""" for p in pedidos)

    return f"""
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_PRIMARIA};letter-spacing:0.5px;">
  INSUCESSO NA ENTREGA — {html.escape(motivo_texto.upper())}
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Precisamos de uma resposta
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Olá, {html.escape(nome_remetente or '')}.<br>{pergunta}
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
    from motivos_falha import texto_do_motivo, pergunta_do_motivo
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
        pergunta = pergunta_do_motivo(failed_reason_id)
        assunto = f"[Freshlog] {motivo_texto} — {len(pedidos)} pedido(s), aguardando retorno"
        conteudo = _montar_conteudo(emb["nome"], pergunta, motivo_texto, pedidos, sender_id, failed_reason_id)
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
