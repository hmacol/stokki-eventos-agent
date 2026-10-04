# -*- coding: utf-8 -*-
"""
notificar_fora_dia_fixo.py

Aviso ao embarcador (Hugo, 03/10/2026 -- spec dias fixos v2, seção 5.2):
pedido com data escolhida fora do dia de visita da região virou envio
dedicado (fora_dia_fixo.py). Um aviso por pedido (registro_dia_fixo.
avisos_fora_dia_fixo), por e-mail (um por embarcador por rodada) e por
WhatsApp (notificar_whatsapp.avisar_cliente_fora_dia_fixo).

Chaves do config.yaml:
  notificacoes_automaticas.ativo  desligada -> nada sai, nada é registrado
  fora_dia_fixo.forcar_destino    ausente = hugo@ (piloto); "" = envio real
  whatsapp_notificacoes.clientes  ativo / forcar_destino / teto_diario
Destinatário do e-mail: preferências do portal, tipo "agendamento" (o mesmo
dos avisos de dia fixo; quem desligou não recebe e-mail).

Registro: grava quando pelo menos um canal saiu, ou quando o embarcador não
tem canal nenhum ("nenhum"). E-mail que falhou sem WhatsApp enviado não
grava: a próxima rodada tenta de novo.
"""
import html
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from email_utils import (  # noqa: E402
    envelope_html, enviar_email, notificacoes_automaticas_ativas,
    COR_ACENTO, COR_BORDA, COR_FUNDO, COR_PRIMARIA, COR_TEXTO,
)
from regioes_dia_fixo import descricao_dias  # noqa: E402
import notificar_whatsapp  # noqa: E402
import pedidos_dedicados  # noqa: E402
import preferencias_notificacao  # noqa: E402
import registro_dia_fixo  # noqa: E402

logger = logging.getLogger(__name__)

EMAIL_TESTE = "hugo@freshlogbr.com"


def forcar_destino_do_config(config: dict) -> str:
    """Ausente = hugo@ (piloto). Envio real exige forcar_destino: "" explícito."""
    secao = (config or {}).get("fora_dia_fixo") or {}
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def _brl(valor) -> str:
    return "R$ " + f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _codigo(servico: dict) -> str:
    codigos = pedidos_dedicados.codigos_do_servico(servico)
    return codigos[0] if codigos else str(servico.get("code") or "").lstrip("#")


def _regiao(regra: dict) -> str:
    return regra.get("regiao") or regra["nome"]


def _montar_conteudo(nome: str, itens: list[dict], iria_para: list[str] | None) -> str:
    faixa = ""
    if iria_para:
        faixa = (f'<p style="margin:0 0 16px 0;padding:8px 12px;background:{COR_FUNDO};border:1px dashed {COR_BORDA};'
                 f'font-size:12px;color:{COR_TEXTO};">PILOTO: este e-mail iria para '
                 f'{html.escape(", ".join(iria_para))}.</p>')
    celula = f"padding:8px 14px;border-bottom:1px solid {COR_BORDA};"
    linhas = "".join(f"""
    <tr>
      <td style="{celula}">{html.escape('#' + _codigo(i['servico']))}</td>
      <td style="{celula}">{html.escape((i['servico'].get('title') or '')[:50])}</td>
      <td style="{celula}"><strong>{i['data'].strftime('%d/%m/%Y')}</strong></td>
      <td style="{celula}">{html.escape(_regiao(i['regra']))} ({html.escape(descricao_dias(i['regra']))})</td>
      <td style="{celula}text-align:right;">{'a confirmar' if i['valor_pendente'] else _brl(i['valor'])}</td>
    </tr>""" for i in itens)
    return f"""
{faixa}
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_ACENTO};letter-spacing:0.5px;">
  DATA FORA DO DIA DE VISITA DA REGIÃO
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Pedidos tratados como envio dedicado
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Olá, {html.escape(nome or '')}.<br>
  Os pedidos abaixo têm destino em regiões atendidas em <strong>dias fixos</strong>, e a data
  escolhida não é um dia de visita da região. Para cumprir a data, eles serão tratados como
  <strong>envio dedicado</strong>, com o valor estimado pela tabela de frete dedicado da Fresh Log.
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Descrição</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Data escolhida</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Região (dias de visita)</th>
<th style="padding:8px 14px;text-align:right;font-size:11px;color:{COR_PRIMARIA};">Valor estimado</th>
</tr></thead><tbody>{linhas}</tbody></table>
<p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Se preferir uma data dentro dos dias de visita da região, responda este e-mail ou fale com a
  equipe pelo portal do cliente.<br><br>
  Atenciosamente,<br><strong>Freshlog Logística</strong>
</p>
"""


def _texto_whatsapp(item: dict) -> str:
    return notificar_whatsapp.texto_fora_dia_fixo(
        _codigo(item["servico"]), item["data"], _regiao(item["regra"]), descricao_dias(item["regra"]),
        None if item["valor_pendente"] else item["valor"])


def avisar(itens: list[dict], config: dict, db_path=None, embarcadores: dict | None = None) -> dict:
    """Avisa os embarcadores dos itens (formato de fora_dia_fixo.pendentes_de_aviso)
    e registra o aviso. `embarcadores` só pra teste."""
    resultado = {"emails": 0, "falhas": 0, "whatsapp": 0, "registrados": 0, "desligado": 0}
    if not itens:
        return resultado
    if not notificacoes_automaticas_ativas(config):
        logger.info(f"{len(itens)} aviso(s) de data fora do dia fixo não enviados: notificações automáticas desligadas.")
        resultado["desligado"] = len(itens)
        return resultado
    db = db_path or registro_dia_fixo.DB_PATH
    if embarcadores is None:
        try:
            embarcadores = preferencias_notificacao.carregar_embarcadores("agendamento", db_path=db)
        except Exception as e:
            logger.warning(f"nao carregou os embarcadores ({e}); avisos sem e-mail")
            embarcadores = {}
    forcar = forcar_destino_do_config(config)
    grupos: dict = defaultdict(list)
    for item in itens:
        grupos[item["servico"].get("sender_id")].append(item)

    conn_reg = registro_dia_fixo.conectar(db)
    conn_pref = sqlite3.connect(str(db), timeout=10)
    try:
        for sender_id, grupo in grupos.items():
            emb = embarcadores.get(sender_id) or {}
            email_ok = email_falhou = False
            if emb.get("emails") and not emb.get("desligado"):
                destinos = [forcar] if forcar else emb["emails"]
                corpo = envelope_html(_montar_conteudo(emb.get("nome", ""), grupo, emb["emails"] if forcar else None),
                                      rodape="Mensagem automática — Agente Stokki Eventos.")
                assunto = f"[Freshlog] Data fora do dia de visita da região — {len(grupo)} pedido(s) como envio dedicado"
                if enviar_email(destinos, assunto, corpo, (config or {}).get("email", {})):
                    email_ok = True
                    resultado["emails"] += 1
                else:
                    email_falhou = True
                    resultado["falhas"] += 1
            telefone = None
            if emb.get("cnpj"):
                try:
                    telefone = preferencias_notificacao.whatsapp_do_embarcador(conn_pref, emb["cnpj"])
                except Exception as e:
                    logger.warning(f"nao leu o WhatsApp do embarcador {sender_id} ({e})")
            for item in grupo:
                canais = ["email"] if email_ok else []
                if telefone and notificar_whatsapp.avisar_cliente_fora_dia_fixo(
                        _codigo(item["servico"]), telefone, _texto_whatsapp(item), config) == "enviado":
                    canais.append("whatsapp")
                    resultado["whatsapp"] += 1
                if email_falhou and not canais:
                    continue  # tenta de novo na próxima rodada
                registro_dia_fixo.registrar_aviso(conn_reg, item["servico"], item["data"], canais)
                resultado["registrados"] += 1
    finally:
        conn_reg.close()
        conn_pref.close()
    return resultado
