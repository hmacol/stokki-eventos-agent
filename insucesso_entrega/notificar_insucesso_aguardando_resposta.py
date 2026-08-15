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
from urllib.parse import quote

_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))

from email_utils import (
    envelope_html, enviar_email, COR_PRIMARIA, COR_TEXTO, COR_BORDA, COR_FUNDO,
    COR_ACENTO, COR_ERRO, COR_TEXTO_SUAVE,
)
import tratativas

logger = logging.getLogger(__name__)

EMAIL_TESTE = "hugo@freshlogbr.com"

# Pedido do Hugo (13/08): os botões de resposta sempre abrem o rascunho
# endereçado ao e-mail de atendimento, independente da conta remetente.
EMAIL_RESPOSTA_BOTOES = "entregas@freshlogbr.com"


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


def _dias_fixos_do_grupo(pedidos: list[dict]) -> tuple[dict[str, str], bool]:
    """
    Regras de dia fixo de entrega que se aplicam aos pedidos deste
    grupo (regioes_dia_fixo.py -- cidade da região, ex.: Sorocaba só
    recebe às terças, OU endereço/galpão cadastrado, ex.: Transfrios às
    segundas/quartas), mapeadas pro texto dos dias ("Terças e Quintas").
    Usado pra avisar o embarcador no e-mail/rascunho de reagendamento
    (pedido do Hugo, 12/08: "dependendo do pedido a duplicação ou
    reagendamento só pode ser em dias específicos"). Pedido sem
    'address' (ou fora de todas as regras) fica de fora.

    Retorna (dias, todos): `todos` diz se TODOS os pedidos do grupo
    caem em regra de dia fixo -- quando sim, o texto do prazo não pode
    falar em "próximo dia útil" (pedido do Hugo, 13/08).
    """
    from roteirizacao.regioes_dia_fixo import regra_dia_fixo_do_servico, nomes_dias

    dias = {}
    todos = bool(pedidos)
    for p in pedidos:
        regra = regra_dia_fixo_do_servico(p)
        if regra:
            dias[regra["nome"]] = nomes_dias(regra["dias"])
        else:
            todos = False
    return dias, todos


def _botoes_resposta(email_resposta: str, assunto_original: str, pedidos: list[dict],
                     sender_id, failed_reason_id, dias_fixos: dict[str, str]) -> str:
    """Dois botões de ação (pedido do Hugo, 12/08: "facilitar a resposta").

    São links mailto: -- o painel não tem URL pública, então botão com
    link HTTP não funcionaria pro embarcador; o que já existe é a
    leitura de respostas via IMAP (ler_respostas_insucesso.py). Cada
    botão abre no cliente de e-mail do remetente um RASCUNHO de resposta
    já preenchido (cancelar / reagendar com campo de data), que ao ser
    enviado cai no fluxo normal de leitura.

    O marcador [[INSUCESSO_GRUPO:...]] vai DENTRO do corpo do rascunho:
    um compose via mailto NÃO cita o e-mail original, então o marcador
    invisível do rodapé não iria junto e a leitura não acharia o grupo.
    """
    codigos = ", ".join("#" + (p.get("code", "") or "").lstrip("#") for p in pedidos)
    marcador = f"[[INSUCESSO_GRUPO:{sender_id}:{failed_reason_id}]]"
    rodape_marcador = ("--- nao apague a linha abaixo (identificacao automatica dos pedidos) ---\r\n"
                       f"{marcador}")

    corpo_cancelar = ("CANCELAR o reenvio dos pedidos: " + codigos + "\r\n\r\n"
                      "Solicito o cancelamento da nova tentativa de entrega.\r\n\r\n"
                      + rodape_marcador)

    # Cidades com dia fixo: avisa no próprio rascunho, pra data pedida
    # já vir num dia válido (se vier em outro dia, a leitura ajusta pra
    # próxima ocorrência do dia da região -- ler_respostas_insucesso.py).
    obs_dias_fixos = ""
    if dias_fixos:
        obs_dias_fixos = ("Obs: " + "; ".join(
            f"entregas para {rotulo} ocorrem somente às {dias_texto}"
            for rotulo, dias_texto in sorted(dias_fixos.items())
        ) + ".\r\n\r\n")

    corpo_reagendar = ("REAGENDAR o reenvio dos pedidos: " + codigos + "\r\n\r\n"
                       "Nova data desejada: ___/___/______   <- preencha aqui antes de enviar\r\n\r\n"
                       + obs_dias_fixos + rodape_marcador)

    def _mailto(corpo: str) -> str:
        return (f"mailto:{quote(email_resposta)}"
                f"?subject={quote('Re: ' + assunto_original)}"
                f"&body={quote(corpo)}")

    estilo_botao = ("display:inline-block;padding:12px 22px;font-size:14px;font-weight:700;"
                    "color:#FFFFFF;text-decoration:none;border-radius:8px;")
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px auto 0 auto;">
  <tr>
    <td style="border-radius:8px;background:{COR_ERRO};">
      <a href="{html.escape(_mailto(corpo_cancelar))}" style="{estilo_botao}">Cancelar reenvio</a>
    </td>
    <td style="width:16px;font-size:0;">&nbsp;</td>
    <td style="border-radius:8px;background:{COR_ACENTO};">
      <a href="{html.escape(_mailto(corpo_reagendar))}" style="{estilo_botao}">Reagendar para outra data</a>
    </td>
  </tr>
</table>
<p style="margin:12px 0 0 0;font-size:12px;color:{COR_TEXTO_SUAVE};line-height:1.6;text-align:center;">
  Os botões abrem uma resposta pronta no seu e-mail — é só enviar.
  No reagendamento, preencha a data desejada antes de enviar.
  Se preferir, responda este e-mail normalmente.
</p>"""


def _montar_conteudo(nome_remetente: str, motivo_texto: str, pedidos: list[dict],
                     sender_id, failed_reason_id, dias_uteis_atraso: int | None = None,
                     assunto_original: str = "", email_resposta: str = "") -> str:
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

    # Cidades/galpões com dia fixo de entrega (regioes_dia_fixo.py): a
    # reentrega (e qualquer reagendamento) cai no dia da região, então o
    # prazo NÃO pode prometer "próximo dia útil" pra esses pedidos
    # (pedido do Hugo, 13/08: "o e-mail tá falando sempre que a entrega
    # ocorrerá no próximo dia útil").
    dias_fixos, todos_dia_fixo = _dias_fixos_do_grupo(pedidos)

    if dias_uteis_atraso and todos_dia_fixo:
        prazo = (f"A nova tentativa será agendada a partir de "
                 f"<strong>{dias_uteis_atraso} dia(s) útil(eis)</strong> após a ocorrência, "
                 "na <strong>próxima data de entrega da região</strong> (dia fixo — veja abaixo).")
    elif dias_uteis_atraso:
        prazo = (f"A nova tentativa está programada para "
                 f"<strong>{dias_uteis_atraso} dia(s) útil(eis)</strong> após a ocorrência.")
    elif todos_dia_fixo:
        prazo = ("A nova tentativa será agendada para a <strong>próxima data de entrega "
                 "da região</strong> (dia fixo — veja abaixo).")
    elif dias_fixos:
        prazo = ("A nova tentativa está programada para o <strong>próximo dia útil</strong> — "
                 "exceto os pedidos de regiões com dia fixo de entrega, que caem na "
                 "<strong>próxima data de entrega da região</strong> (veja abaixo).")
    else:
        prazo = "A nova tentativa está programada para o <strong>próximo dia útil</strong>."

    if dias_fixos:
        prazo += " " + " ".join(
            f"Atenção: entregas para <strong>{html.escape(rotulo)}</strong> ocorrem "
            f"somente às <strong>{dias_texto}</strong> (dia fixo da região)."
            for rotulo, dias_texto in sorted(dias_fixos.items())
        )

    aviso = (
        f"Os pedidos abaixo tiveram <strong>insucesso na entrega</strong> "
        f"(motivo: {html.escape(motivo_texto)}) e <strong>já foram duplicados</strong> "
        f"para uma nova tentativa. {prazo}<br><br>"
        "Caso <strong>não</strong> deseje o reenvio, ou prefira a reentrega em "
        "<strong>outra data</strong>, use os botões abaixo da lista de pedidos "
        "(ou responda este e-mail). Sem resposta, a reentrega segue normalmente."
    )

    botoes = _botoes_resposta(email_resposta, assunto_original, pedidos,
                              sender_id, failed_reason_id, dias_fixos) if email_resposta else ""

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
{botoes}
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
                                    dias_uteis_atraso=duplicar_com_atraso(failed_reason_id),
                                    assunto_original=assunto,
                                    email_resposta=EMAIL_RESPOSTA_BOTOES)
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
                    if p.get("code"):
                        tratativas.registrar_evento(
                            p["code"], "INSUCESSO_ENTREGA", "AVISO_ENVIADO",
                            service_id=p.get("id"), motivo_id=failed_reason_id,
                            motivo_texto=motivo_texto, remetente_email=", ".join(emb["emails"]),
                            texto=f"Aviso enviado a {emb['nome']} ({', '.join(emb['emails'])}) sobre {motivo_texto}.",
                        )
        else:
            falhas += 1

    return {"enviados": enviados, "falhas": falhas, "sem_email": sem_email}
