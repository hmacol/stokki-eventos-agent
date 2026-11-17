# -*- coding: utf-8 -*-
"""
notificar_ciencia_dedicado.py

Ciencia obrigatoria do dedicado fora do dia fixo (spec
docs/superpowers/specs/2026-11-17-ciencia-dedicado-dia-fixo-design.md, secoes
3 e 7): uma rodada de aviso por embarcador, pelos tres canais, com um envio
gravado por pedido e por canal (ciencia_dedicado.registrar_envio).

  e-mail    preferencias do portal (tipo agendamento), IGNORANDO o "desligado"
            do embarcador: e aviso de cobranca. Uma mensagem com todos os pedidos.
  WhatsApp  numero gravado nas preferencias, mesmo com o aviso de WhatsApp
            desligado (preferencias_notificacao.numero_whatsapp). Teto dos clientes.
  chat      conversa do portal aberta pelo sistema, aguardando o cliente (fora da
            fila da equipe). Lembrete entra na mesma conversa; resolvida -> abre outra.
            So com conta no portal. Segue a decisao do e-mail na tela.

Tela de Notificacoes, tipo "ciencia_dedicado": ENVIAR / PILOTO / NAO por canal.
Sem linha na tela = PILOTO: e-mail pra ciencia_dedicado.forcar_destino (ausente =
hugo@), WhatsApp pro numero do piloto da tela; o chat so registra 'piloto'.

Falha de um canal nao impede os outros. Embarcador sem canal nenhum: os tres
'nao_enviado: sem_canal' e um alerta pra equipe na rodada 0.

Os canais chegam prontos (Canais): os testes passam falsos; canais_reais() e o
unico lugar que toca e-mail, WhatsApp e chat de verdade.
"""
import html
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

import ciencia_dedicado as cd
import controle_notificacoes as cn
import notificar_whatsapp
import preferencias_notificacao as pn
from email_utils import (
    envelope_html, enviar_email,
    COR_ACENTO, COR_BORDA, COR_FUNDO, COR_PRIMARIA, COR_TEXTO,
)
from portal_cliente import auth_cliente, link_ciencia
from portal_cliente import chamados as ch

logger = logging.getLogger(__name__)

TIPO = "ciencia_dedicado"
EMAIL_PILOTO = "hugo@freshlogbr.com"
ASSUNTO_CHAT = "Entrega dedicada: confirme a ciência"
CANAIS = ("email", "whatsapp", "chat")
# motivos de falta de cadastro: os tres juntos viram sem_canal
_SEM_CADASTRO = ("sem_email", "sem_whatsapp", "sem_conta_portal")


@dataclass
class Canais:
    email: Callable[[list, str, str], bool]                 # (destinos, assunto, html) -> saiu?
    whatsapp: Callable[[str, str, str, str], str]           # (assinatura, telefone, texto, destino_forcado) -> situacao
    chat_abrir: Callable[[str, str, str, str], "int | None"]  # (cnpj, assunto, texto, pedido_ref) -> id do chamado
    chat_mensagem: Callable[[int, str], bool]               # False = conversa resolvida/sumiu (abre outra)
    tem_conta_portal: Callable[[str], bool]
    alerta_equipe: Callable[[str], None]


def abrir_conversa(conn, cnpj: str, assunto: str, texto: str, pedido_ref: str) -> int | None:
    """Conversa do sistema no chat do portal, aguardando o cliente (RESPONDIDO:
    fora da fila de trabalho da equipe). None se o CNPJ nao e embarcador."""
    cliente = auth_cliente.buscar_embarcador(conn, cnpj)
    if not cliente:
        return None
    chamado = ch.criar_chamado(conn, cliente, ch.ORIGEM_SISTEMA, ch.STATUS_RESPONDIDO, assunto=assunto,
                               area="entrega", pedido_ref=pedido_ref)
    ch.mensagem_sistema(conn, chamado, texto)
    return chamado["id"]


def canais_reais(config: dict, db_path=None) -> Canais:
    def conectar():
        if db_path is None:
            return ch.conectar()
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def com_conexao(fn):
        conn = conectar()
        try:
            return fn(conn)
        finally:
            conn.close()

    def chat_mensagem(chamado_id, texto):
        def postar(conn):
            chamado = ch.buscar_chamado(conn, chamado_id)
            if not chamado or chamado["status"] == ch.STATUS_RESOLVIDO:
                return False
            ch.mensagem_sistema(conn, chamado, texto)
            return True
        return com_conexao(postar)

    def tem_conta_portal(cnpj):
        conta = com_conexao(lambda conn: auth_cliente.buscar_conta(conn, cnpj))
        return bool(conta and conta.get("ativo"))

    return Canais(
        email=lambda destinos, assunto, corpo: enviar_email(destinos, assunto, corpo, (config or {}).get("email", {})),
        whatsapp=lambda assinatura, telefone, texto, destino_forcado: notificar_whatsapp.avisar_cliente_ciencia_dedicado(
            assinatura, telefone, texto, config, destino_forcado=destino_forcado),
        chat_abrir=lambda cnpj, assunto, texto, ref: com_conexao(
            lambda conn: abrir_conversa(conn, cnpj, assunto, texto, ref)),
        chat_mensagem=chat_mensagem,
        tem_conta_portal=tem_conta_portal,
        alerta_equipe=lambda texto: notificar_whatsapp.despachar(config, TIPO, "alerta_equipe", texto),
    )


# ── textos ─────────────────────────────────────────────────────────────────────

def _brl(valor) -> str:
    return "R$ " + f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _valor(p: dict) -> str:
    return "a confirmar" if p.get("valor_pendente") or p.get("valor") is None else _brl(p["valor"])


def _data(p: dict) -> str:
    try:
        return date.fromisoformat(str(p.get("data_entrega"))[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return str(p.get("data_entrega") or "")


def _assunto(n: int, lembrete: bool) -> str:
    assunto = f"[Freshlog] Confirme a ciência: {n} pedido(s) com entrega dedicada"
    return f"Lembrete: {assunto}" if lembrete else assunto


def _conteudo_email(nome: str, pendentes: list[dict], link: str, lembrete: bool, iria_para: list[str] | None) -> str:
    faixa = ""
    if iria_para:
        faixa = (f'<p style="margin:0 0 16px 0;padding:8px 12px;background:{COR_FUNDO};border:1px dashed {COR_BORDA};'
                 f'font-size:12px;color:{COR_TEXTO};">PILOTO: este e-mail iria para '
                 f'{html.escape(", ".join(iria_para))}.</p>')
    celula = f"padding:8px 10px;border-bottom:1px solid {COR_BORDA};font-size:13px;"
    titulo = f"padding:8px 10px;text-align:left;font-size:11px;color:{COR_PRIMARIA};"
    linhas = "".join(f"""
    <tr>
      <td style="{celula}"><strong>{html.escape(p['codigo'])}</strong></td>
      <td style="{celula}">{html.escape(p.get('destinatario') or '')}</td>
      <td style="{celula}">{html.escape(p.get('cidade') or '')}</td>
      <td style="{celula}">{_data(p)}</td>
      <td style="{celula}">{html.escape(p.get('dias_visita') or '')}</td>
      <td style="{celula}text-align:right;">{_valor(p)}</td>
    </tr>""" for p in pendentes)
    rotulo = "LEMBRETE: FALTA CONFIRMAR A CIÊNCIA" if lembrete else "ENTREGA DEDICADA: CONFIRME A CIÊNCIA"
    return f"""
{faixa}
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_ACENTO};letter-spacing:0.5px;">{rotulo}</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Pedidos fora do dia de visita da região
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Olá, {html.escape(nome or '')}.<br>
  A data pedida para os pedidos abaixo não é um dia de visita da região de destino. Para cumprir
  essa data, eles vão como <strong>entrega dedicada</strong> (carro só para eles), com o valor abaixo.<br>
  Precisamos que você <strong>confirme que está ciente</strong> ou escolha um dia de visita da região.
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="{titulo}">Pedido</th><th style="{titulo}">Destinatário</th><th style="{titulo}">Cidade</th>
<th style="{titulo}">Data pedida</th><th style="{titulo}">Dias de visita</th>
<th style="{titulo}text-align:right;">Valor</th>
</tr></thead><tbody>{linhas}</tbody></table>
<p style="margin:24px 0 8px 0;text-align:center;">
  <a href="{html.escape(link)}" style="display:inline-block;padding:14px 32px;background:{COR_ACENTO};color:#FFFFFF;
     font-size:16px;font-weight:800;text-decoration:none;border-radius:8px;">Responder</a>
</p>
<p style="margin:0 0 20px 0;text-align:center;font-size:12px;color:{COR_TEXTO};word-break:break-all;">
  Ou copie o link: {html.escape(link)}
</p>
<p style="margin:0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Enquanto não houver resposta, enviaremos lembretes. Dúvidas: fale com a equipe pelo portal do cliente.<br><br>
  Atenciosamente,<br><strong>Freshlog Logística</strong>
</p>
"""


def _texto_chat(pendentes: list[dict], link: str, lembrete: bool) -> str:
    linhas = [f"- {p['codigo']} · {p.get('destinatario') or ''} ({p.get('cidade') or ''}) · {_data(p)} · "
              f"dias de visita: {p.get('dias_visita') or ''} · {_valor(p)}" for p in pendentes]
    inicio = "Lembrete: ainda falta confirmar a ciência. " if lembrete else ""
    return (f"{inicio}A data pedida para os pedidos abaixo não é dia de visita da região, então eles vão "
            f"como entrega dedicada (carro só para eles), com o valor indicado:\n" + "\n".join(linhas) +
            f"\nConfirme que está ciente ou reagende para um dia de visita: {link}")


def _texto_alerta(nome: str, pendentes: list[dict], link: str) -> str:
    codigos = ", ".join(p["codigo"] for p in pendentes)
    return (f"Ciência do dedicado: {nome or 'embarcador'} sem e-mail, WhatsApp nem conta no portal. "
            f"Avisar à mão ({codigos}): {link}")


# ── rodada ─────────────────────────────────────────────────────────────────────

def _piloto_whatsapp(db_path) -> str:
    """Numero do piloto da tela, usado tambem quando a tela nao tem linha do tipo."""
    try:
        conn = cn.conectar(db_path)
        try:
            return cn.piloto(conn)["whatsapp"]
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(f"nao leu o piloto da tela ({e})")
        return ""


def _conversa_anterior(conn, ids: list[int]) -> int | None:
    """Ultima conversa em que algum destes pedidos foi avisado pelo chat."""
    marcas = ",".join("?" * len(ids))
    row = conn.execute(f"SELECT destino FROM ciencias_dedicado_envios WHERE ciencia_id IN ({marcas}) "
                       f"AND canal = 'chat' AND resultado = 'enviado' ORDER BY id DESC LIMIT 1", ids).fetchone()
    destino = str(row[0] if row else "")
    return int(destino) if destino.isdigit() else None


def _bloqueado(d: cn.Decisao) -> bool:
    return d.acao == "NAO" or (d.acao == "PILOTO" and not d.destino_piloto)


def enviar_embarcador(conn, sender_id, pendentes: list[dict], emb: dict, config: dict, canais: Canais,
                      agora: datetime, db_path=None) -> dict:
    """Uma rodada para o embarcador: uma mensagem por canal com todos os `pendentes`
    (linhas de ciencias_dedicado), um envio gravado por pedido e por canal, e o
    proximo lembrete agendado. Devolve quantas mensagens sairam por canal (enviado
    ou piloto) e quantos canais ficaram em nao_enviado."""
    contadores = {"email": 0, "whatsapp": 0, "chat": 0, "nao_enviado": 0}
    if not pendentes:
        return contadores
    emb = emb or {}
    cnpj = emb.get("cnpj") or pendentes[0].get("cnpj") or None
    nome = emb.get("nome") or ""
    c = cd.cfg(config)
    rodadas = {p["id"]: cd.rodadas_enviadas(conn, p["id"]) for p in pendentes}
    rodada_msg = min(rodadas.values())
    lembrete = rodada_msg > 0
    link = link_ciencia.url(sender_id, config["portal_cliente"]["secret_key"], ch.url_base(config))

    d_email = cn.decidir(TIPO, cnpj, fallback_acao="PILOTO", fallback_piloto=c["forcar_destino"] or EMAIL_PILOTO,
                         canal="email", db_path=db_path)
    d_wpp = cn.decidir(TIPO, cnpj, fallback_acao="PILOTO", fallback_piloto=_piloto_whatsapp(db_path),
                       canal="whatsapp", db_path=db_path)

    def email():
        if _bloqueado(d_email):
            return "nao_enviado", "desligado", ""
        if not emb.get("emails"):
            return "nao_enviado", "sem_email", ""
        piloto = d_email.acao == "PILOTO"
        destinos = [d_email.destino_piloto] if piloto else list(emb["emails"])
        corpo = envelope_html(_conteudo_email(nome, pendentes, link, lembrete, emb["emails"] if piloto else None),
                              rodape="Mensagem automática — Agente Stokki Eventos.")
        if not canais.email(destinos, _assunto(len(pendentes), lembrete), corpo):
            return "nao_enviado", "falha", ", ".join(destinos)
        return ("piloto" if piloto else "enviado"), "", ", ".join(destinos)

    def whatsapp():
        if _bloqueado(d_wpp):
            return "nao_enviado", "desligado", ""
        telefone = pn.numero_whatsapp(conn, cnpj) if cnpj else None
        if not telefone:
            return "nao_enviado", "sem_whatsapp", ""
        piloto = d_wpp.acao == "PILOTO"
        texto = notificar_whatsapp.texto_ciencia_dedicado(nome, pendentes, link, lembrete)
        assinatura = f"{TIPO}:{sender_id}:{rodada_msg}:{agora:%Y%m%d%H%M}"
        situacao = canais.whatsapp(assinatura, telefone, texto, d_wpp.destino_piloto if piloto else "")
        if situacao == "enviado":
            return ("piloto" if piloto else "enviado"), "", (d_wpp.destino_piloto if piloto else telefone)
        motivo = {"teto": "teto", "desligado": "desligado", "numero_sem_whatsapp": "sem_whatsapp"}.get(situacao, "falha")
        return "nao_enviado", motivo, telefone

    def chat():
        if _bloqueado(d_email):
            return "nao_enviado", "desligado", ""
        if d_email.acao == "PILOTO":
            return "piloto", "", ""
        if not cnpj or not canais.tem_conta_portal(cnpj):
            return "nao_enviado", "sem_conta_portal", ""
        texto = _texto_chat(pendentes, link, lembrete)
        anterior = _conversa_anterior(conn, [p["id"] for p in pendentes])
        if anterior and canais.chat_mensagem(anterior, texto):
            return "enviado", "", str(anterior)
        novo = canais.chat_abrir(cnpj, ASSUNTO_CHAT, texto, pendentes[0]["codigo"])
        return ("enviado", "", str(novo)) if novo else ("nao_enviado", "falha", "")

    resultados = {}
    for canal, fn in (("email", email), ("whatsapp", whatsapp), ("chat", chat)):
        try:
            resultados[canal] = fn()
        except Exception as e:  # noqa: BLE001  (um canal nunca derruba os outros)
            logger.warning(f"ciencia do dedicado: {canal} do embarcador {sender_id} falhou ({e})")
            resultados[canal] = ("nao_enviado", "falha", "")

    if all(resultados[canal][1] in _SEM_CADASTRO for canal in CANAIS):
        resultados = {canal: ("nao_enviado", "sem_canal", "") for canal in CANAIS}
        if rodada_msg == 0:
            try:
                canais.alerta_equipe(_texto_alerta(nome, pendentes, link))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"ciencia do dedicado: alerta da equipe do embarcador {sender_id} falhou ({e})")

    for canal, (resultado, _, _) in resultados.items():
        contadores[canal if resultado != "nao_enviado" else "nao_enviado"] += 1
    for p in pendentes:
        for canal, (resultado, motivo, destino) in resultados.items():
            cd.registrar_envio(conn, p["id"], canal, resultado, destino=destino, motivo=motivo,
                               rodada=rodadas[p["id"]], agora=agora)
        cd.agendar_proximo(conn, p["id"], agora, c["intervalo_horas"])
    logger.info(f"ciencia do dedicado: embarcador {sender_id}, {len(pendentes)} pedido(s), rodada {rodada_msg}: "
                + ", ".join(f"{canal}={r[0]}{':' + r[1] if r[1] else ''}" for canal, r in resultados.items()))
    return contadores
