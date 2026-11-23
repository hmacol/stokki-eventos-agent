# -*- coding: utf-8 -*-
"""
painel_agentes/wms_chegada_avisos.py

E-mails da etapa Receber (checklist da chegada do caminhao):
  - avisar_escritorio: aviso interno (temperatura fora, avaria, resposta do cliente);
  - avisar_cliente_chegada: "sua carga chegou", com o Relatorio de Recebimento em PDF;
  - pedir_liberacao: pede ao cliente a liberacao de carga fora da temperatura (link);
  - enviar_pendentes: varredura, chamada no fim de cada rodada de sincronizar_recebimentos_wms.

*** O ENVIO REAL PRO CLIENTE NASCE PRESO ***
Mesmo desenho de wms_faltas_recebimento.py. Sem a secao `notificacao_chegada_recebimento` no config,
tudo que iria ao cliente vai so pro EMAIL_TESTE. Alem disso o tipo `chegada_recebimento` so chega
ao cliente real quando o Hugo o LIGA na tela Notificacoes: sem decisao gravada na tela, mesmo com
`forcar_destino: ""` no config, o e-mail continua redirecionado.

Envio que devolve False nao registra a chave (wms_chegada_envios): a varredura tenta de novo.
Nada aqui levanta por causa de e-mail.
"""
import html
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

import email_utils  # noqa: E402
import preferencias_notificacao  # noqa: E402
import wms_chegada  # noqa: E402
import wms_chegada_pdf  # noqa: E402
from controle_notificacoes import acao_do_config, decidir  # noqa: E402
from wms_faltas_recebimento import (  # noqa: E402
    COPIA_INTERNA, EMAIL_TESTE, _caminho_do_banco, resolver_destinos, stkkc_id_do_recebimento)

logger = logging.getLogger("wms_chegada_avisos")

TIPO_NOTIFICACAO = "chegada_recebimento"
SECAO_CONFIG = "notificacao_chegada_recebimento"
EMAIL_ESCRITORIO_PADRAO = "entregas@freshlogbr.com"
MOTIVOS_ESCRITORIO = ("TEMPERATURA", "AVARIA", "RESPOSTA_CLIENTE")
JANELA_PENDENTES_HORAS = 48  # a varredura nao olha chegada mais antiga que isso


# --- Config -------------------------------------------------------------------

def _secao(config: dict) -> dict:
    return (config or {}).get(SECAO_CONFIG) or {}


def forcar_destino_do_config(config: dict) -> str:
    """Ausente = preso em EMAIL_TESTE; "" = envio real (decisao do Hugo)."""
    secao = _secao(config)
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def email_escritorio(config: dict) -> str:
    return str(_secao(config).get("email_escritorio") or "").strip() or EMAIL_ESCRITORIO_PADRAO


# --- Dados --------------------------------------------------------------------

def _recebimentos(conn, chegada: dict) -> list:
    """Linhas completas de wms_recebimentos da chegada (o stkkc_id mora la)."""
    ids = [r["id"] for r in chegada["recebimentos"]]
    if not ids:
        return []
    marcas = ",".join("?" * len(ids))
    return [dict(r) for r in conn.execute(f"SELECT * FROM wms_recebimentos WHERE id IN ({marcas}) ORDER BY id", ids)]


def _codigo(rec: dict) -> str:
    return str(rec.get("codigo") or f"Recebimento {rec.get('id_stokki') or rec.get('id')}")


def _codigos(chegada: dict) -> str:
    return ", ".join(str(r.get("codigo") or f"#{r['id']}") for r in chegada["recebimentos"])


def _embarcador(conn, rec: dict, config: dict) -> dict:
    """{"nome", "emails", "notificar", "cnpj"} do dono do recebimento, ou {} se nao existe no cadastro."""
    alvo = stkkc_id_do_recebimento(rec, config)
    embs = preferencias_notificacao.carregar_embarcadores(
        TIPO_NOTIFICACAO, chave="stkkc_id", db_path=_caminho_do_banco(conn))
    for chave, emb in embs.items():
        if str(chave).strip() == alvo:
            return {"nome": emb["nome"] or rec.get("embarcador") or f"Embarcador {alvo}",
                    "emails": emb["emails"], "notificar": not emb["desligado"], "cnpj": emb.get("cnpj") or ""}
    return {}


def _destinos_do_cliente(conn, rec: dict, config: dict, modo_teste: bool, respeitar_preferencia: bool) -> tuple:
    """(motivo, emb, destinos, redirecionado). motivo != "" explica por que nao deve enviar."""
    forcar = forcar_destino_do_config(config)
    desativado = not email_utils.notificacoes_automaticas_ativas(config) and not forcar
    try:
        emb = _embarcador(conn, rec, config)
    except (sqlite3.Error, FileNotFoundError, ValueError) as e:
        logger.warning("Recebimento %s: nao consegui resolver o embarcador -- %s", _codigo(rec), e)
        emb = {}

    piloto = ""
    if not modo_teste:
        decisao = decidir(TIPO_NOTIFICACAO, emb.get("cnpj") or None,
                          fallback_acao=acao_do_config(not desativado, forcar), fallback_piloto=forcar,
                          db_path=_caminho_do_banco(conn))
        if decisao.acao == "NAO":
            return ("desativado" if decisao.origem == "config" else "bloqueado_pela_tela"), emb, [], False
        if decisao.acao == "PILOTO":
            piloto = decisao.destino_piloto
        elif decisao.origem == "config":
            piloto = forcar or EMAIL_TESTE  # nasce preso: so a tela solta pro cliente real
    if not emb:
        return "sem_embarcador", emb, [], False
    if respeitar_preferencia and not emb["notificar"]:
        return "desligado_pelo_cliente", emb, [], False
    if not emb["emails"]:
        return "sem_email", emb, [], False
    destinos, redirecionado = resolver_destinos(emb["emails"], modo_teste, piloto)
    return "", emb, destinos, redirecionado


# --- Corpo dos e-mails --------------------------------------------------------

def _graus(valor) -> str:
    return f"{float(valor):g}".replace(".", ",") + " °C"


def _linhas_temperatura(chegada: dict) -> str:
    marca_fora = f" <strong style='color:{email_utils.COR_ERRO};'>(fora da faixa)</strong>"
    itens = "".join(
        f"<li>{html.escape(t['classe'].capitalize())}: medido <strong>{_graus(t['medida_c'])}</strong>, "
        f"faixa {_graus(t['faixa_min'])} a {_graus(t['faixa_max'])}{marca_fora if t['fora'] else ''}</li>"
        for t in chegada["temperaturas"])
    return f"<ul style='margin:0 0 16px;padding-left:20px;'>{itens}</ul>" if itens else ""


def _ficha(chegada: dict) -> str:
    linhas = "".join(
        f"<tr><td style='padding:3px 12px 3px 0;font-size:13px;color:{email_utils.COR_TEXTO_SUAVE};'>{rot}</td>"
        f"<td style='padding:3px 0;font-size:13px;font-weight:600;color:{email_utils.COR_TEXTO};'>"
        f"{html.escape(str(val))}</td></tr>"
        for rot, val in (("Motorista", chegada["motorista"]), ("Placa", chegada["placa"]),
                         ("Transportadora", chegada["transportadora"]), ("Volumes", chegada["volumes"]),
                         ("Lacre", chegada["lacre"])) if val)
    return f"<table role='presentation' style='margin:0 0 16px;border-collapse:collapse;'>{linhas}</table>"


def _paragrafo(texto: str) -> str:
    return (f"<p style='margin:0 0 16px;font-size:14px;color:{email_utils.COR_TEXTO};line-height:1.6;'>{texto}</p>")


def _envelope(titulo: str, miolo: str, rodape: str, destaque: bool = False) -> str:
    conteudo = (f"<h2 style='margin:0 0 16px;font-size:20px;color:{email_utils.COR_TEXTO};'>"
                f"{html.escape(titulo)}</h2>{miolo}"
                f"<p style='margin:0;font-size:14px;color:{email_utils.COR_TEXTO};'>Atenciosamente,<br>"
                f"<strong>Freshlog Logística</strong></p>")
    return email_utils.envelope_html(conteudo, rodape, cor_acento=email_utils.COR_DESTAQUE if destaque else
                                     email_utils.COR_ACENTO)


def _aviso_redirecionado(emb: dict, redirecionado: bool) -> str:
    if not redirecionado:
        return ""
    return (f"<p style='margin:0 0 16px;padding:10px 14px;font-size:12px;background-color:#FEF3C7;"
            f"border-radius:6px;'><strong>Redirecionado (piloto).</strong> Destino real: "
            f"{html.escape(', '.join(emb.get('emails') or []) or 'sem e-mail cadastrado')}</p>")


# --- Idempotencia + envio -----------------------------------------------------

def _enviar_e_registrar(conn, enviar, destinos, assunto, corpo, config, cc=None, anexos=None, chave=None) -> bool:
    ok = bool(enviar(destinos, assunto, corpo, (config or {}).get("email", {}), cc=cc or [], anexos=anexos))
    if ok and chave:
        wms_chegada.registrar_envio(conn, chave)
    return ok


# --- Escritorio ---------------------------------------------------------------

def avisar_escritorio(conn, chegada_id: int, config: dict, motivo: str, enviar=None) -> dict:
    """Aviso interno. Idempotente por `escritorio-{motivo}-{chegada_id}`."""
    if motivo not in MOTIVOS_ESCRITORIO:
        raise ValueError(f"Motivo desconhecido: {motivo}")
    enviar = enviar or email_utils.enviar_email
    destino = email_escritorio(config)
    r = {"enviado": False, "motivo": "", "destinos": [destino]}
    chegada = wms_chegada.detalhe(conn, chegada_id)
    chave = f"escritorio-{motivo}-{chegada_id}"
    codigos = _codigos(chegada)
    orientacao = _paragrafo("Entre no <strong>/wms</strong> do painel para decidir: liberar a carga, "
                            "pedir a liberação ao cliente ou não receber.")

    if motivo == "TEMPERATURA":
        if not any(t["fora"] for t in chegada["temperaturas"]):
            r["motivo"] = "sem_ocorrencia"
            return r
        assunto = f"Temperatura fora da faixa no recebimento: {codigos}"
        titulo, miolo = "Temperatura fora da faixa", _linhas_temperatura(chegada) + _ficha(chegada) + orientacao
    elif motivo == "AVARIA":
        if chegada["integra"] != 0:
            r["motivo"] = "sem_ocorrencia"
            return r
        assunto = f"Avaria no recebimento: {codigos}"
        titulo = "Avaria no recebimento"
        miolo = (_paragrafo(f"<strong>Observação:</strong> {html.escape(chegada['observacao'])}")
                 + _linhas_temperatura(chegada) + _ficha(chegada) + orientacao)
    else:
        if not chegada["resposta_cliente"]:
            r["motivo"] = "sem_resposta"
            return r
        autorizou = chegada["resposta_cliente"] == "AUTORIZADO"
        assunto = f"Cliente {'autorizou' if autorizou else 'não autorizou'} o recebimento: {codigos}"
        titulo = assunto.split(":")[0]
        miolo = (_paragrafo(f"{html.escape(chegada['resposta_cliente_nome'])} respondeu em "
                            f"{html.escape(chegada['resposta_cliente_em'])}.")
                 + _linhas_temperatura(chegada) + _ficha(chegada) + orientacao)

    if wms_chegada.ja_enviado(conn, chave):
        r["motivo"] = "ja_enviado"
        return r
    corpo = _envelope(titulo, miolo, "Freshlog Logística - aviso interno do recebimento.", destaque=True)
    if _enviar_e_registrar(conn, enviar, [destino], assunto, corpo, config, chave=chave):
        r["enviado"] = True
    else:
        r["motivo"] = "falha_no_envio"
    return r


# --- Cliente: sua carga chegou ------------------------------------------------

def avisar_cliente_chegada(conn, chegada_id: int, config: dict, enviar=None, modo_teste: bool = False,
                           so_recebimentos=None) -> list:
    """Um e-mail por recebimento da chegada LIBERADA, com o PDF. Idempotente por `chegou-{recebimento_id}`
    (`-teste` quando redirecionado: ligar o envio real depois ainda manda o que so o Hugo viu).
    `so_recebimentos`: ids a considerar (a varredura passa so os que nunca tiveram aviso)."""
    enviar = enviar or email_utils.enviar_email
    chegada = wms_chegada.detalhe(conn, chegada_id)
    resultados = []
    pdf = None  # gerado uma vez por chegada, so se alguem for receber
    for rec in _recebimentos(conn, chegada):
        if so_recebimentos is not None and rec["id"] not in so_recebimentos:
            continue
        r = {"enviado": False, "motivo": "", "destinos": [], "redirecionado": False}
        resultados.append(r)
        if chegada["estado"] != "LIBERADA":
            r["motivo"] = "fora_do_estado"
            continue
        motivo, emb, destinos, redirecionado = _destinos_do_cliente(conn, rec, config, modo_teste, True)
        if motivo:
            r["motivo"] = motivo
            continue
        r["destinos"], r["redirecionado"] = destinos, redirecionado
        chave = f"chegou-{rec['id']}" + ("-teste" if redirecionado else "")
        if not modo_teste and wms_chegada.ja_enviado(conn, chave):
            r["motivo"] = "ja_enviado"
            continue
        codigo = _codigo(rec)
        if pdf is None:
            try:
                pdf = wms_chegada_pdf.do_banco(conn, chegada_id)
            except Exception:  # noqa: BLE001 -- sem PDF nao se manda "relatorio": a varredura tenta de novo
                logger.exception("Chegada %s: falha ao gerar o PDF", chegada_id)
                r["motivo"] = "falha_no_pdf"
                break
        miolo = (_aviso_redirecionado(emb, redirecionado)
                 + _paragrafo(f"Olá, <strong>{html.escape(str(emb['nome']))}</strong>! O caminhão com a carga do "
                              f"recebimento <strong>{html.escape(codigo)}</strong> chegou ao galpão da Freshlog "
                              f"e foi conferido. O relatório completo, com as fotos, está em anexo.")
                 + (_paragrafo(f"Recebimentos desta chegada: <strong>{html.escape(_codigos(chegada))}</strong>.")
                    if len(chegada["recebimentos"]) > 1 else "")
                 + _ficha(chegada) + _linhas_temperatura(chegada)
                 + _paragrafo("Carga chegou inteira." if chegada["integra"] == 1 else
                              f"<strong>Atenção:</strong> {html.escape(chegada['observacao'])}"))
        corpo = _envelope("Sua carga chegou", miolo, "Freshlog Logística - relatório automático de recebimento.")
        fd, caminho = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(pdf)
            copia = [] if redirecionado else [COPIA_INTERNA]
            ok = _enviar_e_registrar(conn, enviar, destinos, f"Sua carga chegou: {codigo}", corpo, config, cc=copia,
                                     anexos=[(Path(caminho), f"Relatorio-de-Recebimento-{codigo}.pdf")],
                                     chave=None if modo_teste else chave)
        finally:
            Path(caminho).unlink(missing_ok=True)
        if ok:
            r["enviado"] = True
        else:
            r["motivo"] = "falha_no_envio"
    return resultados


# --- Cliente: pedido de liberacao ---------------------------------------------

def pedir_liberacao(conn, chegada_id: int, config: dict, link: str, enviar=None) -> dict:
    """Pede ao cliente que libere (ou nao) o recebimento fora da temperatura. Um e-mail por recebimento, para
    os e-mails do embarcador mesmo com a preferencia desligada (e um pedido que precisa de resposta), mas
    ainda sujeito ao redirecionamento. Nao e idempotente: o escritorio pode pedir de novo."""
    enviar = enviar or email_utils.enviar_email
    r = {"enviado": False, "motivo": "", "destinos": []}
    chegada = wms_chegada.detalhe(conn, chegada_id)
    if chegada["estado"] != "AGUARDANDO_ESCRITORIO":
        r["motivo"] = "fora_do_estado"
        return r
    enviados = 0
    for rec in _recebimentos(conn, chegada):
        motivo, emb, destinos, redirecionado = _destinos_do_cliente(conn, rec, config, False, False)
        if motivo:
            r["motivo"] = r["motivo"] or motivo
            continue
        codigo = _codigo(rec)
        miolo = (_aviso_redirecionado(emb, redirecionado)
                 + _paragrafo(f"Olá, <strong>{html.escape(str(emb['nome']))}</strong>! A carga do recebimento "
                              f"<strong>{html.escape(codigo)}</strong> chegou ao galpão da Freshlog com a "
                              f"temperatura <strong>fora da faixa</strong>:")
                 + _linhas_temperatura(chegada)
                 + _paragrafo("Precisamos da sua decisão: autorizar ou não o recebimento desta carga. "
                              f"<a href=\"{html.escape(link, quote=True)}\" style=\"color:#047857;font-weight:600;\">"
                              f"Responder agora</a><br><span style='font-size:12px;color:"
                              f"{email_utils.COR_TEXTO_SUAVE};'>{html.escape(link)}</span>"))
        corpo = _envelope("Liberação de recebimento", miolo, "Freshlog Logística - pedido de liberação.", destaque=True)
        copia = [] if redirecionado else [COPIA_INTERNA]
        r["destinos"] += destinos
        if _enviar_e_registrar(conn, enviar, destinos, f"Liberação de recebimento fora da temperatura: {codigo}",
                               corpo, config, cc=copia):
            enviados += 1
        else:
            r["motivo"] = r["motivo"] or "falha_no_envio"
    r["enviado"] = enviados > 0 and not r["motivo"]
    return r


# --- Varredura ----------------------------------------------------------------

def _ids(conn, sql: str, parametros=()) -> list:
    return [row[0] for row in conn.execute(sql, parametros)]


def enviar_pendentes(conn, config: dict, enviar=None) -> dict:
    """Manda o que ficou por mandar. Conta so o que saiu com sucesso.

    Sem retroativo: recebimento que ja teve qualquer aviso (`chegou-{id}` ou `chegou-{id}-teste`) esta
    resolvido, e so entram chegadas liberadas/concluidas ha ate JANELA_PENDENTES_HORAS. Ligar o tipo na
    tela Notificacoes vale so daqui pra frente."""
    wms_chegada.garantir_tabelas(conn)
    total = {"chegou": 0, "escritorio": 0}
    recente = ("COALESCE(NULLIF(c.liberacao_em, ''), c.concluida_em) >= "
               "datetime('now', 'localtime', '-%d hours')" % JANELA_PENDENTES_HORAS)

    novos = {}  # chegada -> recebimentos que nunca tiveram aviso
    for cid, rid in conn.execute(
            "SELECT c.id, cr.recebimento_id FROM wms_chegadas c "
            "JOIN wms_chegada_recebimentos cr ON cr.chegada_id = c.id "
            f"WHERE c.estado = 'LIBERADA' AND {recente} AND NOT EXISTS (SELECT 1 FROM wms_chegada_envios e "
            "WHERE e.chave IN ('chegou-' || cr.recebimento_id, 'chegou-' || cr.recebimento_id || '-teste')) "
            "ORDER BY c.id, cr.recebimento_id").fetchall():
        novos.setdefault(cid, set()).add(rid)
    for cid, rids in novos.items():
        total["chegou"] += sum(1 for r in avisar_cliente_chegada(conn, cid, config, enviar=enviar,
                                                                 so_recebimentos=rids) if r["enviado"])

    consultas = (
        ("TEMPERATURA", "c.estado = 'AGUARDANDO_ESCRITORIO'"),
        ("AVARIA", "c.estado != 'RASCUNHO' AND c.integra = 0"),
        ("RESPOSTA_CLIENTE", "c.resposta_cliente != ''"),
    )
    for motivo, filtro in consultas:
        for cid in _ids(conn, f"SELECT c.id FROM wms_chegadas c WHERE {filtro} AND {recente} AND NOT EXISTS ("
                              f"SELECT 1 FROM wms_chegada_envios e WHERE e.chave = 'escritorio-{motivo}-' || c.id) "
                              "ORDER BY c.id"):
            if avisar_escritorio(conn, cid, config, motivo, enviar=enviar)["enviado"]:
                total["escritorio"] += 1
    return total
