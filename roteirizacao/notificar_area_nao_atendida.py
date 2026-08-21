# -*- coding: utf-8 -*-
"""
notificar_area_nao_atendida.py

Pedido do Hugo, 02/08: cidades fora das regiões atendidas (Grande SP +
as 5 direções com dia fixo) geram notificação ao REMETENTE, em vez de
serem roteirizadas normalmente:

  - Dentro de SP, mas fora de qualquer região atendida: "não atendemos
    essa região atualmente, gostaria de uma cotação pra entrega
    dedicada?"
  - Fora do estado de SP: "fora da área de atendimento -- seria um
    Redespacho por alguma transportadora? Se sim, informar endereço
    completo com CEP e nome da transportadora."

Só busca coordenada (pra checar o raio da Grande SP) pros pedidos que
JÁ não estão numa região com dia fixo -- evita gastar chamada à toa.
Cada pedido só é notificado UMA VEZ (fingerprint_area_nao_atendida.py)
-- não é um fluxo com resposta estruturada esperada, então não faz
sentido reenviar todo dia.
"""
import html
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))

from email_utils import envelope_html, enviar_email, COR_PRIMARIA, COR_DESTAQUE, COR_TEXTO, COR_BORDA, COR_FUNDO

logger = logging.getLogger(__name__)

EMAIL_TESTE = "hugo@freshlogbr.com"

TIPO_SP_NAO_ATENDIDO = "sp_nao_atendido"
TIPO_FORA_SP = "fora_sp"


def classificar_pedido(servico: dict, api_key: str | None, coords_sp,
                       extrair_cidade_fn, extrair_uf_fn, dia_fixo_da_cidade_fn,
                       obter_coordenadas_fn, distancia_km_fn, raio_grande_sp_km: float) -> str | None:
    """
    Retorna None se o pedido está OK (numa região com dia fixo -- já
    tratado por regioes_dia_fixo.py -- ou dentro do raio da Grande SP,
    entrega normal sem restrição). Retorna TIPO_SP_NAO_ATENDIDO se está
    em SP mas fora do raio da Grande SP e fora de toda região definida.
    Retorna TIPO_FORA_SP se a UF não é SP.

    Defensivo: sem UF ou coordenada suficiente pra decidir, NÃO
    notifica (melhor deixar passar um caso raro do que incomodar
    remetente à toa por falta de dado).
    """
    cidade = extrair_cidade_fn(servico)
    if cidade and dia_fixo_da_cidade_fn(cidade) is not None:
        return None  # já numa região definida, tratado em outro lugar

    uf = extrair_uf_fn(servico)
    if uf and uf != "SP":
        return TIPO_FORA_SP

    coords = obter_coordenadas_fn(servico, api_key)
    if coords and coords_sp:
        distancia = distancia_km_fn(*coords, *coords_sp)
        if distancia <= raio_grande_sp_km:
            return None  # Grande SP, sem restrição
        return TIPO_SP_NAO_ATENDIDO

    return None


def identificar_area_nao_atendida(servicos: list[dict], api_key: str | None) -> list[tuple[dict, str]]:
    """
    Filtra os serviços que precisam de notificação de área não
    atendida (excluindo quem já foi notificado antes). Retorna lista
    de (servico, tipo).
    """
    from geocodificacao import geocodificar
    from regioes_dia_fixo import (
        extrair_cidade, extrair_uf, dia_fixo_da_cidade,
        RAIO_GRANDE_SP_KM, ENDERECO_REFERENCIA_SP,
    )
    from roteirizacao_dados import obter_coordenadas, _distancia_km
    from fingerprint_area_nao_atendida import ja_notificado

    coords_sp = geocodificar(ENDERECO_REFERENCIA_SP, api_key)

    resultado = []
    for s in servicos:
        if ja_notificado(s["id"]):
            continue
        tipo = classificar_pedido(
            s, api_key, coords_sp, extrair_cidade, extrair_uf, dia_fixo_da_cidade,
            obter_coordenadas, _distancia_km, RAIO_GRANDE_SP_KM,
        )
        if tipo:
            resultado.append((s, tipo))
    return resultado


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


def _montar_conteudo(nome_remetente: str, tipo: str, pedidos: list[dict]) -> str:
    from regioes_dia_fixo import extrair_cidade, extrair_uf

    if tipo == TIPO_SP_NAO_ATENDIDO:
        titulo = "Região fora da área de atendimento"
        texto = (
            "Os pedidos abaixo têm entrega em regiões que atualmente não fazem parte da "
            "área de atendimento padrão da Freshlog. Caso haja interesse, podemos "
            "providenciar uma cotação para entrega dedicada a esses destinos."
        )
    else:
        titulo = "Entrega fora do estado de São Paulo"
        texto = (
            "Os pedidos abaixo têm entrega fora do estado de São Paulo, fora da área de "
            "atendimento da Freshlog. Solicitamos a confirmação se haverá redespacho por "
            "alguma transportadora. Em caso afirmativo, favor informar o endereço completo "
            "com CEP e o nome da transportadora."
        )

    linhas = "".join(f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape('#' + (p.get('code','') or '').lstrip('#'))}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape(extrair_cidade(p) or '-')} - {html.escape(extrair_uf(p) or '-')}</td>
    </tr>""" for p in pedidos)

    return f"""
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">{titulo}</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Olá, {html.escape(nome_remetente or '')}.<br>{texto}
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Cidade - UF</th>
</tr></thead><tbody>{linhas}</tbody></table>
<p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Atenciosamente,<br><strong>Freshlog Logística</strong>
</p>
"""


def notificar_remetentes(pendentes_com_tipo: list[tuple[dict, str]], config_email: dict,
                         modo_teste: bool = False, forcar_destino: str | None = None) -> dict:
    """
    Agrupa por (remetente, tipo) e manda 1 e-mail por combinação
    (um remetente pode ter pedidos dos 2 tipos, viram e-mails
    separados, já que a mensagem é diferente). Marca cada pedido como
    notificado (fingerprint_area_nao_atendida.py), mesmo em modo_teste
    NÃO marca (deixa pra confirmar de verdade antes de considerar
    "já avisado").

    forcar_destino (pedido do Hugo, 20/08, junto com a redução do raio
    da Grande SP pra 35km): manda TODO mundo pra esse endereço em vez
    do e-mail real do remetente, sem entrar no modo_teste (que também
    deixaria de marcar o pedido como notificado) -- ele quer acompanhar
    manualmente o volume de "área não atendida" antes de deixar ir
    direto pro cliente, mas sem reenviar o mesmo pedido toda vez que o
    job rodar.
    """
    from fingerprint_area_nao_atendida import marcar_notificado

    embarcadores = _carregar_embarcadores_por_sender_id()
    grupos = defaultdict(list)
    for servico, tipo in pendentes_com_tipo:
        grupos[(servico.get("sender_id"), tipo)].append(servico)

    enviados = falhas = sem_email = 0
    for (sender_id, tipo), pedidos in grupos.items():
        emb = embarcadores.get(sender_id)
        if not emb or not emb["emails"]:
            logger.warning(f"  sender_id={sender_id}: {len(pedidos)} pedido(s) ({tipo}) -- sem e-mail cadastrado.")
            sem_email += 1
            continue

        assunto_tipo = "cotação necessária" if tipo == TIPO_SP_NAO_ATENDIDO else "fora de SP — confirmar redespacho"
        assunto = f"[Freshlog] {len(pedidos)} pedido(s) — {assunto_tipo}"
        conteudo = _montar_conteudo(emb["nome"], tipo, pedidos)
        corpo = envelope_html(conteudo, rodape="Mensagem automática — Agente Stokki Eventos.",
                              cor_acento=COR_DESTAQUE)
        destinos = [EMAIL_TESTE] if modo_teste else ([forcar_destino] if forcar_destino else emb["emails"])

        if modo_teste:
            logger.info(f"  [TESTE] {emb['nome']} ({tipo}) -> {EMAIL_TESTE} (original: {emb['emails']}) | "
                       f"{len(pedidos)} pedido(s): {[p.get('code') for p in pedidos]}")
        elif forcar_destino:
            logger.info(f"  [REDIRECIONADO] {emb['nome']} ({tipo}) -> {forcar_destino} (original: {emb['emails']}) | "
                       f"{len(pedidos)} pedido(s): {[p.get('code') for p in pedidos]}")

        if enviar_email(destinos, assunto, corpo, config_email):
            enviados += 1
            if not modo_teste:
                for p in pedidos:
                    marcar_notificado(p["id"], tipo)
        else:
            falhas += 1

    return {"enviados": enviados, "falhas": falhas, "sem_email": sem_email}
