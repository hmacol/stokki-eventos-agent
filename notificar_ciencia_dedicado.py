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
'nao_enviado: sem_canal' e um alerta pra equipe na rodada 0 (so no envio real).
Cada pedido e reivindicado (ciencia_dedicado.reivindicar_lembrete) antes de
qualquer canal: timer e roteirizacao rodando juntos nao mandam o mesmo lembrete
duas vezes.

Os canais chegam prontos (Canais): os testes passam falsos; canais_reais() e o
unico lugar que toca e-mail, WhatsApp e chat de verdade.

Rotina do timer (stokki-ciencia-dedicado, a cada 15 min; secoes 3, 5 e 6):

    venv/bin/python notificar_ciencia_dedicado.py [--modo-teste]

Encerra pelo espelho do nucleo (cancelado, entregue/insucesso, rota iniciada,
data em dia de visita), por dedicado removido ou data passada; alerta a equipe
as 16h da vespera (uma mensagem com todos, so no envio real); manda o lembrete
vencido (a cada 4h) so entre 08h e 20h. Nos primeiros 30 min da ciencia o
espelho (sincronizado a cada 15 min) nao vale: so dedicado removido e data
passada encerram. A roteirizacao chama executar(somente_ids=...) so com as
ciencias que acabou de abrir: sem encerrar, sem alertar, sem lembrete dos outros.
--modo-teste so loga: nao grava envio, nao encerra, nao mexe no lembrete.
"""
import argparse
import html
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):   # o logging escreve no stderr
    sys.stderr.reconfigure(encoding="utf-8")

import yaml

import ciencia_dedicado as cd
import controle_notificacoes as cn
import notificar_whatsapp
import pedidos_dedicados
import preferencias_notificacao as pn
import regioes_dia_fixo  # roteirizacao/ (ciencia_dedicado poe no sys.path)
from nucleo import banco as nb
from email_utils import (
    envelope_html, enviar_email,
    COR_ACENTO, COR_BORDA, COR_FUNDO, COR_PRIMARIA, COR_TEXTO,
)
from portal_cliente import auth_cliente, link_ciencia
from portal_cliente import chamados as ch

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).resolve().parent
TIPO = "ciencia_dedicado"
EMAIL_PILOTO = "hugo@freshlogbr.com"
ASSUNTO_CHAT = "Entrega dedicada: confirme a ciência"
CANAIS = ("email", "whatsapp", "chat")
# motivos de falta de cadastro: os tres juntos viram sem_canal
_SEM_CADASTRO = ("sem_email", "sem_whatsapp", "sem_conta_portal")
# ciencia aberta ha menos que isso: o espelho do nucleo (sincronismo de 15 min) ainda
# pode ser de antes da marcacao (data velha, servico recriado que ele nao tem)
CARENCIA_ESPELHO_MIN = 30


@dataclass
class Canais:
    email: Callable[[list, str, str], bool]                 # (destinos, assunto, html) -> saiu?
    whatsapp: Callable[[str, str, str, str], str]           # (assinatura, telefone, texto, destino_forcado) -> situacao
    chat_abrir: Callable[[str, str, str, str], "int | None"]  # (cnpj, assunto, texto, pedido_ref) -> id do chamado
    chat_mensagem: Callable[[int, str], bool]               # False = conversa resolvida/sumiu (abre outra)
    tem_conta_portal: Callable[[str], bool]
    alerta_equipe: Callable[[str], bool]                    # True = saiu pro grupo da equipe


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
        alerta_equipe=lambda texto: notificar_whatsapp.despachar(config, TIPO, "alerta_equipe", texto) == "enviado",
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


def _texto_alerta_vespera(itens: list[tuple[dict, str, int]]) -> str:
    """Uma mensagem com todos os pedidos da rodada: (ciencia, nome do embarcador, avisos)."""
    linhas = [f"- {c['codigo']} ({nome or 'embarcador'}), entrega {_data(c)}, {avisos} aviso(s) enviado(s)"
              for c, nome, avisos in itens]
    return ("Ciência do dedicado sem resposta na véspera da entrega:\n" + "\n".join(linhas) +
            "\nFalar com o embarcador antes da saída.")


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


def decisao_email(cnpj, config: dict, db_path=None) -> cn.Decisao:
    """Decisao da tela pro e-mail do embarcador; vale tambem pro chat e pra pagina
    de resposta (portal_cliente/link_ciencia.py). Sem linha na tela = PILOTO."""
    return cn.decidir(TIPO, cnpj, fallback_acao="PILOTO",
                      fallback_piloto=cd.cfg(config)["forcar_destino"] or EMAIL_PILOTO, canal="email", db_path=db_path)


def _bloqueado(d: cn.Decisao) -> bool:
    return d.acao == "NAO" or (d.acao == "PILOTO" and not d.destino_piloto)


def enviar_embarcador(conn, sender_id, pendentes: list[dict], emb: dict, config: dict, canais: Canais,
                      agora: datetime, db_path=None) -> dict:
    """Uma rodada para o embarcador: uma mensagem por canal com todos os `pendentes`
    (linhas de ciencias_dedicado) que esta rodada conseguiu reivindicar, um envio
    gravado por pedido e por canal. Devolve quantas mensagens sairam por canal
    (enviado ou piloto) e quantos canais ficaram em nao_enviado.

    Cada pedido e reivindicado ANTES de enviar (cd.reivindicar_lembrete: adia o
    proximo lembrete so se ainda esta vencido e PENDENTE). Pedido que outra rodada
    ja tomou (timer e roteirizacao juntos) fica fora da mensagem; nenhum tomado =
    nada sai. Se o banco falhar (ex.: dados.db travado), nada sai, em vez de sair e
    o pedido continuar vencido (o cliente receberia de novo a cada rodada do timer).
    Falha ao gravar o envio de um pedido e logada e nao impede os outros."""
    contadores = {"email": 0, "whatsapp": 0, "chat": 0, "nao_enviado": 0}
    if not pendentes:
        return contadores
    c = cd.cfg(config)
    # tudo que pode levantar vem antes de reivindicar: falha aqui sobe sem adiar ninguem
    # (o timer tenta de novo em 15 min), em vez de adiar 4h sem envio nem registro
    emb = emb or {}
    cnpj = emb.get("cnpj") or pendentes[0].get("cnpj") or None
    nome = emb.get("nome") or ""
    rodadas = {p["id"]: cd.rodadas_enviadas(conn, p["id"]) for p in pendentes}
    pilotos = {p["id"]: cd.rodadas_piloto(conn, p["id"]) for p in pendentes}
    segredo, base = config["portal_cliente"]["secret_key"], ch.url_base(config)
    d_email = decisao_email(cnpj, config, db_path=db_path)
    d_wpp = cn.decidir(TIPO, cnpj, fallback_acao="PILOTO", fallback_piloto=_piloto_whatsapp(db_path),
                       canal="whatsapp", db_path=db_path)
    try:
        pendentes = [p for p in pendentes if cd.reivindicar_lembrete(conn, p["id"], agora, c["intervalo_horas"])]
    except Exception as e:  # noqa: BLE001
        logger.error(f"ciencia do dedicado: nao reivindicou os lembretes do embarcador {sender_id} ({e}); "
                     f"nada enviado nesta rodada")
        return contadores
    if not pendentes:
        logger.info(f"ciencia do dedicado: lembretes do embarcador {sender_id} ja tomados por outra rodada")
        return contadores
    rodadas = {p["id"]: rodadas[p["id"]] for p in pendentes}

    def link(origem: str) -> str:
        """Um link por canal, so com os pedidos desta mensagem: a resposta grava de qual veio."""
        return link_ciencia.url(sender_id, segredo, base, list(rodadas), origem)

    rodada_msg = min(rodadas.values())
    lembrete = rodada_msg > 0
    # copia de piloto vira lembrete a partir da 2a rodada (de piloto ou real), sem mexer na contagem real
    lembrete_piloto = min(rodadas[i] + pilotos[i] for i in rodadas) > 0

    def email():
        if _bloqueado(d_email):
            return "nao_enviado", "desligado", ""
        if not emb.get("emails"):
            return "nao_enviado", "sem_email", ""
        piloto = d_email.acao == "PILOTO"
        destinos = [d_email.destino_piloto] if piloto else list(emb["emails"])
        lem = lembrete_piloto if piloto else lembrete
        corpo = envelope_html(_conteudo_email(nome, pendentes, link("email"), lem, emb["emails"] if piloto else None),
                              rodape="Mensagem automática — Agente Stokki Eventos.")
        if not canais.email(destinos, _assunto(len(pendentes), lem), corpo):
            return "nao_enviado", "falha", ", ".join(destinos)
        return ("piloto" if piloto else "enviado"), "", ", ".join(destinos)

    def whatsapp():
        if _bloqueado(d_wpp):
            return "nao_enviado", "desligado", ""
        telefone = pn.numero_whatsapp(conn, cnpj) if cnpj else None
        if not telefone:
            return "nao_enviado", "sem_whatsapp", ""
        piloto = d_wpp.acao == "PILOTO"
        texto = notificar_whatsapp.texto_ciencia_dedicado(nome, pendentes, link("whatsapp"),
                                                          lembrete_piloto if piloto else lembrete)
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
        texto = _texto_chat(pendentes, link("chat"), lembrete)
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
        # so no envio real: no piloto (ou CNPJ desconhecido) ninguem do cliente pode responder
        if rodada_msg == 0 and d_email.acao == "ENVIAR" and cnpj:
            try:
                if not canais.alerta_equipe(_texto_alerta(nome, pendentes, link("equipe"))):
                    logger.warning(f"ciencia do dedicado: alerta da equipe do embarcador {sender_id} "
                                   f"(sem canal) nao saiu")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"ciencia do dedicado: alerta da equipe do embarcador {sender_id} falhou ({e})")

    for canal, (resultado, _, _) in resultados.items():
        contadores[canal if resultado != "nao_enviado" else "nao_enviado"] += 1
    for p in pendentes:
        try:
            for canal, (resultado, motivo, destino) in resultados.items():
                cd.registrar_envio(conn, p["id"], canal, resultado, destino=destino, motivo=motivo,
                                   rodada=rodadas[p["id"]], agora=agora)
        except Exception as e:  # noqa: BLE001
            logger.error(f"ciencia do dedicado: rodada de {p['codigo']} sem registro de envio ({e}); "
                         f"o que saiu: {resultados}")
    logger.info(f"ciencia do dedicado: embarcador {sender_id}, {len(pendentes)} pedido(s), rodada {rodada_msg}: "
                + ", ".join(f"{canal}={r[0]}{':' + r[1] if r[1] else ''}" for canal, r in resultados.items()))
    return contadores


# ── rotina do timer ────────────────────────────────────────────────────────────

def _dia(valor) -> date | None:
    try:
        return date.fromisoformat(str(valor)[:10]) if valor else None
    except ValueError:
        return None


def estado_do_pedido(conn_nucleo, codigo, service_id=None, aberta_em: str | None = None) -> dict | None:
    """Situacao do pedido no espelho do nucleo (tabelas nucleo_*, no mesmo dados.db).
    None = o nucleo nao conhece o pedido. agendamento_inicio ja esta em hora local
    (o sincronismo converte o UTC da Vuupt com vuupt_para_local): a data sao os 10
    primeiros caracteres. Rota iniciada = parada nao cancelada numa rota EM_ROTA ou
    CONCLUIDA (o status EM_ROTA do pedido so diz que ele esta numa rota).

    Com service_id procura primeiro pelo servico (nucleo_pedidos.vuupt_service_id e
    nucleo_paradas.service_id): reentrega com o mesmo codigo nao herda a parada da
    rota antiga, e servico com codigos combinados ('PS-1, PS-2') e achado. Sem
    service_id, ou se o nucleo nao tem o servico, vale o codigo. Se a linha do codigo
    e de OUTRO servico: criado na Vuupt depois de `aberta_em` (hora local da ciencia)
    = a Vuupt recriou o pedido e o espelho trocou a linha, vale ela (com as paradas
    do servico novo); senao None, a linha e de um servico velho. Os ids da Vuupt
    nao seguem a ordem de criacao, por isso a comparacao e pela data."""
    sql = ("SELECT status, agendamento_inicio, vuupt_service_id, criado_em_provedor FROM nucleo_pedidos "
           "WHERE {} = ? LIMIT 1")
    row = None
    if service_id is not None:
        row = conn_nucleo.execute(sql.format("vuupt_service_id"), (service_id,)).fetchone()
    campo, chave = ("pa.service_id", service_id) if row is not None else ("pa.codigo", codigo)
    if row is None:
        row = conn_nucleo.execute(sql.format("codigo"), (codigo,)).fetchone()
        if row is not None and service_id is not None and row[2] is not None and row[2] != service_id:
            if not (aberta_em and row[3] and row[3] >= aberta_em):
                return None
            campo, chave = "pa.service_id", row[2]
    if row is None:
        return None
    iniciada = conn_nucleo.execute(f"""
        SELECT 1 FROM nucleo_paradas pa JOIN nucleo_rotas r ON r.id = pa.rota_id
        WHERE {campo} = ? AND pa.situacao <> ? AND r.status IN (?, ?) LIMIT 1
    """, (chave, nb.PARADA_CANCELADA, nb.ROTA_EM_ROTA, nb.ROTA_CONCLUIDA)).fetchone() is not None
    return {"cancelado": row[0] == nb.PEDIDO_CANCELADO,
            "finalizado": row[0] in (nb.PEDIDO_ENTREGUE, nb.PEDIDO_INSUCESSO),
            "rota_iniciada": iniciada, "data": _dia(row[1])}


def motivo_de_encerramento(ciencia: dict, estado: dict | None, dedicado_ativo: bool, regra: dict | None,
                           hoje: date) -> str | None:
    """Por que parar os lembretes (spec, secao 5), ou None. Pedido que o nucleo nao
    conhece (estado None) so encerra por dedicado removido ou data passada. Sumir do
    pool nao e motivo: o dedicado sai dele quando ganha rota."""
    if estado:
        if estado["cancelado"]:
            return "cancelado"
        if estado["finalizado"]:
            return "entregue ou insucesso"
        if estado["rota_iniciada"]:
            return "rota iniciada"
    if not dedicado_ativo:
        return "dedicado removido"
    data = (estado or {}).get("data") or _dia(ciencia.get("data_entrega"))
    if data and data < hoje:
        return "data passou"
    if estado and estado["data"] and regra and regioes_dia_fixo.data_valida_na_regiao(regra, estado["data"]):
        return "data em dia de visita"
    return None


def _regra_por_nome(nome) -> dict | None:
    """Remonta a regra gravada em regra_nome (galpao de ENDERECOS_DIA_FIXO ou cidade)
    pela mesma funcao que a detectou, com um endereco minimo. None se nao achar."""
    if not nome:
        return None
    for r in regioes_dia_fixo.ENDERECOS_DIA_FIXO:
        if r["nome"] == nome:
            return regioes_dia_fixo.regra_dia_fixo_do_servico({"address": r["padroes"][0]})
    return regioes_dia_fixo.regra_dia_fixo_do_servico({"address": f", {nome} - SP,"})


def _avisos_enviados(conn, ciencia_id) -> int:
    """Rodadas em que algum canal chegou de fato ao embarcador."""
    return conn.execute("SELECT COUNT(DISTINCT enviado_em) FROM ciencias_dedicado_envios "
                        "WHERE ciencia_id = ? AND resultado = 'enviado'", (ciencia_id,)).fetchone()[0]


def _alertar_vespera(conn, alertar: list[dict], embarcador: Callable[[object], dict], config: dict,
                     canais: Canais, agora: datetime, modo_teste: bool, db_path=None) -> int:
    """Alerta da vespera no grupo da equipe: UMA mensagem com todos os pedidos que
    seguem PENDENTE (relidos agora) e marca cada um so se ela saiu; senao tentam de
    novo na proxima rodada. Pedido fora do envio real (piloto, bloqueado ou CNPJ
    desconhecido) nao entra: ninguem do cliente pode responder. Devolve quantos
    pedidos foram alertados."""
    itens, acoes = [], {}
    for ciencia in alertar:
        try:
            fresco = cd.buscar(conn, ciencia["id"])   # quem respondeu durante os envios nao gera alerta
            if not fresco or fresco["situacao"] != cd.PENDENTE or fresco["alerta_equipe_em"]:
                continue
            emb = embarcador(ciencia["sender_id"])
            cnpj = emb.get("cnpj") or fresco["cnpj"]
            if not cnpj:
                continue
            if cnpj not in acoes:
                acoes[cnpj] = decisao_email(cnpj, config, db_path=db_path).acao
            if acoes[cnpj] != "ENVIAR":
                continue
            itens.append(({**fresco, "data_entrega": ciencia["data_entrega"]}, emb.get("nome") or "",
                          _avisos_enviados(conn, ciencia["id"])))
        except Exception as e:  # noqa: BLE001
            logger.error(f"ciencia do dedicado: alerta da equipe de {ciencia['codigo']} falhou ({e})")
    if not itens:
        return 0
    itens.sort(key=lambda i: i[0]["id"])
    texto = _texto_alerta_vespera(itens)
    codigos = ", ".join(c["codigo"] for c, _, _ in itens)
    if modo_teste:
        logger.info(f"[modo-teste] ciencia do dedicado: alertaria a equipe: {texto}")
        return len(itens)
    try:
        saiu = canais.alerta_equipe(texto)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ciencia do dedicado: alerta da equipe falhou ({e})")
        saiu = False
    if not saiu:
        logger.warning(f"ciencia do dedicado: alerta da vespera nao saiu ({codigos}); tenta na proxima rodada")
        return 0
    for c, _, _ in itens:
        try:
            cd.marcar_alerta_equipe(conn, c["id"], agora)
        except Exception as e:  # noqa: BLE001
            logger.error(f"ciencia do dedicado: alerta de {c['codigo']} saiu sem marcar ({e}); pode repetir")
    return len(itens)


def executar(config: dict, agora: datetime | None = None, modo_teste: bool = False, canais: Canais | None = None,
             db_path=None, embarcadores: dict | None = None, somente_ids: set[int] | None = None) -> dict:
    """Uma rodada do timer: confere todos os PENDENTE (encerra ou atualiza a data) e,
    dentro da janela, manda os lembretes vencidos por embarcador, relendo cada pedido
    logo antes do envio (quem respondeu no meio da rodada nao recebe); depois alerta
    a equipe dos que passaram das 16h da vespera e seguem PENDENTE. Erro num pedido
    ou num embarcador e logado e nao para os outros.

    Com `somente_ids` (a roteirizacao, logo depois de abrir) so manda o aviso dessas
    ciencias: nao confere nem encerra ninguem e nao alerta a equipe (fica pro timer)."""
    resumo = {"encerrados": 0, "embarcadores": 0, "enviados": 0, "adiados": 0, "alertas": 0}
    c = cd.cfg(config)
    if not c["ativo"]:
        logger.info("ciencia do dedicado: ciencia_dedicado.ativo desligado, nada a fazer")
        return resumo
    agora = agora or datetime.now()
    teste = "[modo-teste] " if modo_teste else ""
    dentro = cd.proximo_envio_permitido(agora, c["janela"]) == agora
    canais = canais or canais_reais(config, db_path=db_path)

    def embarcador(sender_id) -> dict:
        nonlocal embarcadores
        if embarcadores is None:
            embarcadores = pn.carregar_embarcadores("agendamento", db_path=db_path)
        return embarcadores.get(sender_id) or {}

    conn = cd.conectar(db_path or cd.DB_PATH)
    try:
        ativos = pedidos_dedicados.ativos_por_codigo(conn)
        servicos_ativos = {d["service_id"] for d in ativos.values() if d["service_id"] is not None}
        fora = set()   # nao entram no envio desta rodada: encerrados, ou sem como conferir
        alertar = []   # vespera vencida: alerta da equipe depois dos envios (conta o aviso que acabou de sair)
        conferir = cd.listar(conn, cd.PENDENTE, limite=-1) if somente_ids is None else []   # -1 = sem limite
        for ciencia in conferir:
            try:
                # recem-aberta: o espelho pode ser de antes da marcacao; so as tabelas locais encerram
                idade = agora - datetime.strptime(ciencia["criado_em"], "%Y-%m-%d %H:%M:%S")
                recente = idade < timedelta(minutes=CARENCIA_ESPELHO_MIN)
                estado = None if recente else estado_do_pedido(conn, ciencia["codigo"], ciencia["service_id"],
                                                               aberta_em=ciencia["criado_em"])
                # servico OU codigo: o dedicado guarda o primeiro service_id; reentrega ou servico
                # recriado chega com outro (remocao poe removido_em, entao o codigo tambem a enxerga)
                ativo = (ciencia["service_id"] in servicos_ativos
                         or pedidos_dedicados.normalizar_codigo(ciencia["codigo"]) in ativos)
                motivo = motivo_de_encerramento(ciencia, estado, ativo, _regra_por_nome(ciencia["regra_nome"]),
                                                agora.date())
                if motivo:
                    fora.add(ciencia["id"])
                    resumo["encerrados"] += 1
                    logger.info(f"{teste}ciencia do dedicado: {ciencia['codigo']} encerrado ({motivo})")
                    if not modo_teste:
                        cd.encerrar(conn, ciencia["id"], motivo, agora)
                    continue
                nova = estado["data"] if estado else None
                if nova and nova.isoformat() != ciencia["data_entrega"]:
                    logger.info(f"{teste}ciencia do dedicado: {ciencia['codigo']} mudou de data "
                                f"({ciencia['data_entrega']} -> {nova.isoformat()}) e segue fora do dia de visita")
                    if not modo_teste:
                        cd.atualizar_data(conn, ciencia["id"], nova)
                    ciencia["data_entrega"] = nova.isoformat()
            except Exception as e:  # noqa: BLE001
                fora.add(ciencia["id"])
                logger.error(f"ciencia do dedicado: nao conferiu {ciencia['codigo']} ({e}); "
                             f"fica sem lembrete nesta rodada")
                continue
            data = _dia(ciencia["data_entrega"])
            if data and not ciencia["alerta_equipe_em"] \
                    and agora >= datetime.combine(data - timedelta(days=1), time(16, 0)):
                alertar.append(ciencia)

        vencidos = [p for p in cd.pendentes_vencidos(conn, agora)
                    if p["id"] not in fora and (somente_ids is None or p["id"] in somente_ids)]
        if not dentro:
            resumo["adiados"] = len(vencidos)
            if vencidos:
                logger.info(f"ciencia do dedicado: {len(vencidos)} lembrete(s) fora da janela, "
                            f"ficam para as {c['janela'][0]:%H:%M}")
            return resumo
        por_sender: dict = {}
        for p in vencidos:
            por_sender.setdefault(p["sender_id"], []).append(p["id"])
        for sender_id, ids in por_sender.items():
            try:
                # rele logo antes: o embarcador pode ter respondido enquanto a rodada montava os outros
                frescos = [p for p in (cd.buscar(conn, i) for i in ids) if p and p["situacao"] == cd.PENDENTE]
                if not frescos:
                    continue
                emb = embarcador(sender_id)
                if modo_teste:
                    logger.info(f"[modo-teste] ciencia do dedicado: enviaria ao embarcador {sender_id} "
                                f"({emb.get('nome') or ''}): " + ", ".join(p["codigo"] for p in frescos))
                    saiu = True
                else:
                    saiu = any(enviar_embarcador(conn, sender_id, frescos, emb, config, canais, agora,
                                                 db_path=db_path).values())
                if saiu:
                    resumo["embarcadores"] += 1
                    resumo["enviados"] += len(frescos)
            except Exception as e:  # noqa: BLE001  (um embarcador nunca derruba os outros)
                logger.error(f"ciencia do dedicado: embarcador {sender_id} falhou nesta rodada ({e})")

        resumo["alertas"] = _alertar_vespera(conn, alertar, embarcador, config, canais, agora, modo_teste,
                                             db_path=db_path)
    finally:
        conn.close()
    return resumo


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Lembretes da ciencia do dedicado fora do dia fixo.")
    parser.add_argument("--modo-teste", action="store_true",
                        help="so loga o que faria: nao envia, nao grava envio, nao encerra")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    resumo = executar(config, modo_teste=args.modo_teste)
    logger.info(f"Resumo: {resumo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
