# -*- coding: utf-8 -*-
"""
notificar_insucesso_aguardando_resposta.py

REGRA ATUAL (pedido do Hugo, 15/08: "quero que as tratativas definam
se vamos ou não duplicar um pedido, não quero mais duplicar
automaticamente; no caso do cliente responder, prevalece o que ele
solicitar"): NENHUM insucesso é duplicado antes de perguntar. Este
módulo manda a PERGUNTA ao remetente -- "houve insucesso, deseja o
reenvio?" -- com botões pra confirmar, recusar ou pedir outra data. A
reentrega só é criada quando a resposta chega e confirma (ou pede
reagendamento), respondida na página web (ver abaixo) e aplicada por
sincronizar_respostas_insucesso.py. Sem resposta, sem ação -- revoga a
regra de 11/08 (duplicava tudo na hora e só perguntava depois, "responda
se quiser cancelar"), que por sua vez já tinha revogado esta mesma regra
de perguntar antes (03/08) -- o esqueleto de agrupamento/fingerprint
nunca mudou, só o texto e o momento em que a reentrega é criada.

BOTÕES (pedido do Hugo, 18/08: trocar os links mailto: -- que
dependiam do embarcador abrir o cliente de e-mail e enviar, e só eram
lidos por um job de IMAP a cada 30 min -- por um link único que já
aplica a resposta no clique). Cada botão leva pra uma página pública
(insucesso_resposta/app.py, hospedada numa VPS fora da rede local,
mesmo padrão de confirmacao_motoristas/app.py): publicar_grupo() faz o
push do conteúdo do grupo pra lá ANTES do e-mail ser enviado -- se a
VPS não responder, o e-mail deste grupo não é mandado neste ciclo
(tenta de novo no próximo, não faz sentido mandar um botão quebrado).
Revoga a versão anterior (12/08-15/08) que usava mailto: lidos via
IMAP + Claude (ler_respostas_insucesso.py, removido).

Agrupa por (remetente, motivo) -- cada motivo tem uma pergunta
diferente, então viram e-mails separados mesmo pro mesmo remetente.
Rate-limit diário (fingerprint_aguardando_resposta.py): no máximo 1
e-mail por dia por pedido, até a resposta chegar (status muda pra algo
diferente de PENDENTE -- a aplicação da resposta vem de
sincronizar_respostas_insucesso.py).

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

import requests
from itsdangerous import URLSafeTimedSerializer

from email_utils import (
    envelope_html, enviar_email, COR_PRIMARIA, COR_TEXTO, COR_BORDA, COR_FUNDO,
    COR_ACENTO, COR_ERRO, COR_TEXTO_SUAVE,
)
import tratativas

logger = logging.getLogger(__name__)

EMAIL_TESTE = "hugo@freshlogbr.com"


def identificar_aguardando_resposta(insucessos: list[dict]) -> list[dict]:
    """
    TODOS os insucessos geram a pergunta de reenvio ao remetente (não
    só os motivos com "aguarda_resposta" em motivos_falha.py -- esses
    só ganham uma pergunta mais específica, ver _montar_conteudo). O
    filtro que fica é o rate-limit do fingerprint (pode_notificar): 1
    e-mail por dia por pedido, até chegar resposta.
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
    Usado pra avisar o embarcador no e-mail/página de resposta (pedido
    do Hugo, 12/08: "dependendo do pedido a duplicação ou reagendamento
    só pode ser em dias específicos"). Pedido sem 'address' (ou fora de
    todas as regras) fica de fora.

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


def _textos_do_grupo(motivo_texto: str, pedidos: list[dict], failed_reason_id) -> dict:
    """
    Pergunta/prazo/aviso de dia fixo do grupo, em HTML (pro corpo do
    e-mail, com <strong>) e em texto plano (pro push da página de
    resposta) -- fonte única, pra e-mail e página nunca divergirem.
    """
    from motivos_falha import aguarda_resposta, pergunta_do_motivo

    dias_fixos, todos_dia_fixo = _dias_fixos_do_grupo(pedidos)

    if todos_dia_fixo:
        prazo_html = ("Se confirmado, o reenvio é agendado para a <strong>próxima data de "
                      "entrega da região</strong> (dia fixo — veja abaixo).")
    elif dias_fixos:
        prazo_html = ("Se confirmado, o reenvio segue para o <strong>próximo dia útil</strong> — "
                      "exceto os pedidos de regiões com dia fixo de entrega, que caem na "
                      "<strong>próxima data de entrega da região</strong> (veja abaixo).")
    else:
        prazo_html = "Se confirmado, o reenvio segue para o <strong>próximo dia útil</strong>."

    if dias_fixos:
        prazo_html += " " + " ".join(
            f"Atenção: entregas para <strong>{html.escape(rotulo)}</strong> ocorrem "
            f"somente às <strong>{dias_texto}</strong> (dia fixo da região)."
            for rotulo, dias_texto in sorted(dias_fixos.items())
        )

    pergunta_html = (pergunta_do_motivo(failed_reason_id) if aguarda_resposta(failed_reason_id)
                     else f"Não foi possível concluir a entrega dos pedidos abaixo (motivo: "
                          f"{html.escape(motivo_texto)}).")

    aviso_dia_fixo_texto = None
    if dias_fixos:
        aviso_dia_fixo_texto = "Obs: " + "; ".join(
            f"entregas para {rotulo} ocorrem somente às {dias_texto}"
            for rotulo, dias_texto in sorted(dias_fixos.items())
        ) + "."

    return {
        "pergunta_html": pergunta_html,
        "prazo_html": prazo_html,
        "pergunta_texto": re.sub(r"<[^>]+>", "", pergunta_html),
        "prazo_texto": re.sub(r"<[^>]+>", "", prazo_html),
        "aviso_dia_fixo_texto": aviso_dia_fixo_texto,
    }


def publicar_grupo(sender_id, failed_reason_id, motivo_texto: str, pergunta_texto: str,
                   prazo_texto: str, aviso_dia_fixo_texto: str | None,
                   pedidos: list[dict], config_resposta: dict) -> str | None:
    """
    Publica (ou atualiza) o conteúdo do grupo na página pública de
    resposta (insucesso_resposta/app.py, na VPS -- a máquina local não
    tem entrada de internet, então a página só pode existir lá) e
    devolve a URL pra colocar nos botões do e-mail. Devolve None se a
    VPS não respondeu ou se resposta_insucesso não está configurado em
    config.yaml -- quem chama decide não mandar o e-mail nesse caso
    (um botão quebrado é pior que esperar o próximo ciclo).
    """
    url_base = (config_resposta.get("url_base") or "").rstrip("/")
    token_secret = config_resposta.get("token_secret")
    sync_secret = config_resposta.get("sync_secret")
    if not url_base or not token_secret or not sync_secret:
        logger.warning("resposta_insucesso.url_base/token_secret/sync_secret não configurados "
                       "em config.yaml -- botões de resposta não serão publicados.")
        return None

    serializer = URLSafeTimedSerializer(token_secret, salt="resposta-insucesso")
    token = serializer.dumps({"sender_id": sender_id, "failed_reason_id": failed_reason_id})
    pedidos_payload = [
        {"code": "#" + (p.get("code", "") or "").lstrip("#"), "title": (p.get("title") or "")[:60]}
        for p in pedidos
    ]

    try:
        resp = requests.post(
            f"{url_base}/api/sync/upsert",
            json={
                "token": token, "sender_id": sender_id, "failed_reason_id": failed_reason_id,
                "motivo_texto": motivo_texto, "pergunta_texto": pergunta_texto,
                "prazo_texto": prazo_texto, "aviso_dia_fixo_texto": aviso_dia_fixo_texto,
                "pedidos": pedidos_payload,
            },
            headers={"X-Sync-Secret": sync_secret},
            timeout=15,
        )
        resp.raise_for_status()
        token_final = resp.json().get("token") or token
    except requests.RequestException as e:
        logger.error(f"Falha ao publicar grupo (sender_id={sender_id}, "
                    f"failed_reason_id={failed_reason_id}) na página de resposta: {e}")
        return None

    return f"{url_base}/r/{token_final}"


def _botoes_resposta(url_resposta: str) -> str:
    """Três botões de ação -- todos levam pra MESMA página pública
    (insucesso_resposta/app.py), que pergunta Sim/Reagendar/Não e só
    grava a resposta quando o embarcador de fato clica um botão NA
    PÁGINA (nunca por um GET simples do e-mail, pra não correr o risco
    de um scanner/preview de e-mail "clicar" sozinho e registrar uma
    resposta que ninguém deu)."""
    estilo_botao = ("display:inline-block;padding:12px 18px;font-size:13px;font-weight:700;"
                    "color:#FFFFFF;text-decoration:none;border-radius:8px;")
    url_escapada = html.escape(url_resposta)
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px auto 0 auto;">
  <tr>
    <td style="border-radius:8px;background:{COR_ACENTO};">
      <a href="{url_escapada}" style="{estilo_botao}">Sim, reenviar</a>
    </td>
    <td style="width:12px;font-size:0;">&nbsp;</td>
    <td style="border-radius:8px;background:{COR_PRIMARIA};">
      <a href="{url_escapada}" style="{estilo_botao}">Reagendar para outra data</a>
    </td>
    <td style="width:12px;font-size:0;">&nbsp;</td>
    <td style="border-radius:8px;background:{COR_ERRO};">
      <a href="{url_escapada}" style="{estilo_botao}">Não reenviar</a>
    </td>
  </tr>
</table>
<p style="margin:12px 0 0 0;font-size:12px;color:{COR_TEXTO_SUAVE};line-height:1.6;text-align:center;">
  Clique num dos botões acima pra responder -- você será levado a uma página
  onde confirma sua escolha (e, no reagendamento, escolhe a data).
</p>"""


def _montar_conteudo(nome_remetente: str, motivo_texto: str, pedidos: list[dict],
                     textos: dict, url_resposta: str = "") -> str:
    """Só o CONTEÚDO (título, pergunta, tabela) -- o envelope (logo,
    cores, rodapé) vem de email_utils.envelope_html().

    Regra atual (Hugo, 15/08): o e-mail é uma PERGUNTA -- "houve
    insucesso, deseja o reenvio?" -- e NADA é duplicado até a resposta
    confirmar."""
    linhas = "".join(f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape('#' + (p.get('code','') or '').lstrip('#'))}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape((p.get('title') or '')[:60])}</td>
    </tr>""" for p in pedidos)

    aviso = (
        f"{textos['pergunta_html']} <strong>Deseja que reenviemos para uma nova tentativa?</strong> "
        f"{textos['prazo_html']}<br><br>"
        "Clique num dos botões abaixo da lista de pedidos pra responder confirmando o "
        "reenvio, pedindo outra data, ou dizendo que não quer o reenvio. "
        "<strong>Sem resposta, o pedido não será reenviado.</strong>"
    )

    botoes = _botoes_resposta(url_resposta) if url_resposta else ""

    return f"""
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_PRIMARIA};letter-spacing:0.5px;">
  INSUCESSO NA ENTREGA — {html.escape(motivo_texto.upper())}
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Deseja o reenvio deste(s) pedido(s)?
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
"""


def notificar_remetentes(pendentes: list[dict], config_email: dict, config_resposta: dict,
                         modo_teste: bool = False) -> dict:
    """
    Agrupa por (remetente, motivo) -- cada motivo tem pergunta
    diferente -- e manda 1 e-mail por combinação. Marca cada pedido
    como notificado (fingerprint_aguardando_resposta.py), mesmo em
    modo_teste NÃO marca (deixa livre pra testar de novo).
    """
    from motivos_falha import texto_do_motivo
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
        textos = _textos_do_grupo(motivo_texto, pedidos, failed_reason_id)

        url_resposta = publicar_grupo(
            sender_id, failed_reason_id, motivo_texto,
            textos["pergunta_texto"], textos["prazo_texto"], textos["aviso_dia_fixo_texto"],
            pedidos, config_resposta,
        )
        if not url_resposta:
            logger.error(f"  sender_id={sender_id}: não consegui publicar o grupo na página de "
                        f"resposta -- e-mail NÃO enviado neste ciclo (tenta de novo no próximo).")
            falhas += 1
            continue

        assunto = (f"[Freshlog] {motivo_texto} — {len(pedidos)} pedido(s) com "
                   f"insucesso na entrega, aguardando retorno")
        conteudo = _montar_conteudo(emb["nome"], motivo_texto, pedidos, textos,
                                    url_resposta=url_resposta)
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
                            texto=f"Pergunta de reenvio enviada a {emb['nome']} ({', '.join(emb['emails'])}) sobre {motivo_texto}.",
                        )
        else:
            falhas += 1

    return {"enviados": enviados, "falhas": falhas, "sem_email": sem_email}
