# -*- coding: utf-8 -*-
"""
resumo_diario_embarcador.py

E-mail diario por embarcador com o status de todas as notas que estavam em
rota no dia (pedido do Hugo, 17/09): entregues (com link do canhoto), com
falha (motivo + tratativa) e nao concluidas. Um e-mail por embarcador, so
pra quem LIGOU o resumo no portal (botao Notificacoes) e teve pelo menos 1
nota em rota; pedido sem rota nao entra.
Spec: DOC_EXECUCAO_CLAUDE_RESUMO_DIARIO_EMBARCADOR.md.

E o irmao das 20h do notificar_nfs_em_rota.py (07h) e segue o mesmo molde:
mesma fonte do portal (portal_cliente/dados_cliente -- rotas da VUUPT
buscadas UMA vez e recortadas por sender_id, NF de documentos_processados),
mesma tabela de dedup (notificacoes_enviadas) e as mesmas travas.

Destinatarios: preferencias_notificacao.py (tipo "resumo_diario", nasce
DESLIGADO -- e opt-in). interno.notificar_email = 0 nao e veto (so muda o
padrao dos outros dois tipos novos; este ja nasce desligado pra todos).

Canhoto: link assinado por nota (portal_cliente/link_canhoto.py), sem login
e sem anexo. A rotina NAO verifica canhoto por canhoto: o link de quem ainda
nao tem mostra "ainda nao disponivel" e passa a abrir quando chegar.

Tratativa: so rotulos padronizados (tratativa_para_cliente). Texto livre da
Torre nunca vai pro cliente.

Travas:
  - config.yaml resumo_diario_embarcador.ativo (default True) desliga so
    esta rotina.
  - resumo_diario_embarcador.forcar_destino: enquanto preenchido, TODO e-mail
    vai so pra ele. SEM a secao no config o padrao e o e-mail do Hugo: envio
    real pro cliente exige `forcar_destino: ""` explicito.
  - chave-mestra notificacoes_automaticas.ativo vale pro envio real (o
    redirecionado nao sai de casa, entao nao depende dela).
  - notificacoes_enviadas (tipo + chave sender|data): rodar duas vezes no
    mesmo dia nao manda duas vezes.

Agendamento (VPS): stokki-resumo-diario-embarcador.timer, 20:00.

Execute:
  py -3.11 resumo_diario_embarcador.py --modo-teste                  (tudo pra hugo@, nao marca envio)
  py -3.11 resumo_diario_embarcador.py --modo-teste --sender-id 123  (um embarcador, mesmo com o resumo desligado)
  py -3.11 resumo_diario_embarcador.py --data 2026-09-16
"""
import argparse
import html
import logging
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from email_utils import (COR_BORDA, COR_ERRO, COR_PRIMARIA, COR_PRIMARIA_CLARA, COR_TEXTO,
                         COR_TEXTO_SUAVE, enviar_email, envelope_html, notificacoes_automaticas_ativas)
from notificar_execucao_agente import notificar_execucao
from portal_cliente import dados_cliente, link_canhoto
import preferencias_notificacao

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("resumo_diario")

CONFIG_PATH = _RAIZ / "config.yaml"
DB_PATH     = _RAIZ / "dados" / "dados.db"
EMAIL_TESTE = "hugo@freshlogbr.com"
URL_PORTAL  = "https://app.freshhub.com.br/cliente"

TIPO_NOTIFICACAO = "resumo_diario"
COR_VERDE_TEXTO = "#047857"
COR_AMBAR_TEXTO = "#B45309"   # COR_DESTAQUE (#F5A623) como texto sobre branco fica em 2:1

# Respostas padronizadas da Torre (painel_agentes/torre_controle.py
# RESPOSTAS_TRATATIVA, menos "Outro") -- as unicas que o cliente ve. Copia
# de proposito: importar o painel aqui arrastaria o Flask pra rotina em lote.
ROTULOS_TRATATIVA = (
    "Reagendado", "Já havia sido enviado", "Já entregue", "Não coletado", "Coletado pelo cliente",
    "Cliente ausente", "Fora do horário de recebimento", "Local fechado", "Duplicado por engano",
    "Pedido cancelado", "Devolvido ao embarcador",
)
TRATATIVA_GENERICA = "Em tratativa com nossa equipe"
EVENTOS_REENVIO = {"REENVIO_AUTOMATICO", "REENVIO_AGENDADO", "REENVIO_MANUAL"}
MOTIVO_NAO_INFORMADO = "Motivo não informado"


def _carregar_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _secao(config: dict) -> dict:
    return (config or {}).get("resumo_diario_embarcador") or {}


def rotina_ativa(config: dict) -> bool:
    return bool(_secao(config).get("ativo", True))


def forcar_destino_do_config(config: dict) -> str:
    """Sem a chave no config.yaml o padrao e redirecionar pro Hugo (piloto):
    ligar o envio real pro cliente e decisao dele, com forcar_destino: ""."""
    secao = _secao(config)
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def resolver_destinos(emails: list[str], modo_teste: bool, forcar_destino: str) -> tuple[list[str], bool]:
    """(destinos, redirecionado)."""
    if modo_teste:
        return [EMAIL_TESTE], True
    if forcar_destino:
        return [forcar_destino], True
    return list(emails), False


# --- Regras (puras) -----------------------------------------------------------

def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", sem_acento.lower()).strip()


_ROTULO_POR_CHAVE = {_normalizar(r): r for r in ROTULOS_TRATATIVA}


def _rotulo_da_torre(motivo_torre: str) -> str | None:
    """Tratativas anteriores a 16/09 sao texto livre ("Local Fechado /
    Reagendado", "Ja havia sido enviado!"). O padrao era "causa / o que foi
    feito", entao o trecho apos a ultima barra vem primeiro -- senao "Local
    fechado" (causa) ganharia de "Reagendado" (tratativa). Aceita sobra
    depois do rotulo ("Reagendado para dia seguinte")."""
    for trecho in (str(motivo_torre).rsplit("/", 1)[-1], motivo_torre):
        chave = _normalizar(trecho)
        for chave_rotulo, rotulo in _ROTULO_POR_CHAVE.items():
            if chave == chave_rotulo or chave.startswith(chave_rotulo + " "):
                return rotulo
    return None


def tratativa_para_cliente(motivo_torre: str | None, eventos: set[str], aguardando: bool) -> str:
    """O que o embarcador le na coluna Tratativa. So rotulo padronizado:
    texto livre da Torre nunca sai daqui."""
    if motivo_torre:
        rotulo = _rotulo_da_torre(motivo_torre)
        if rotulo:
            return rotulo
        # duplicar_pedido_manual da Torre grava "Duplicado → <novo codigo>"
        if re.match(r"\s*duplicado\s*(→|->)", motivo_torre, re.IGNORECASE):
            return "Reentrega programada"
    if eventos & EVENTOS_REENVIO:
        return "Reentrega programada"
    if aguardando:
        return "Aguardando seu retorno"
    if "RESPOSTA_RECEBIDA" in eventos:
        return "Retorno recebido, em andamento"
    return TRATATIVA_GENERICA


def motivo_para_cliente(texto: str) -> str:
    """motivos_falha.texto_do_motivo acrescenta avisos internos ("novo -- sem
    regra de duplicacao", "ainda nao cadastrado no de-para"); tira."""
    texto = str(texto or "").strip()
    if not texto or re.match(r"Motivo #\d+", texto):
        return MOTIVO_NAO_INFORMADO
    return re.sub(r"\s*\(novo -- [^)]*\)\s*$", "", texto) or MOTIVO_NAO_INFORMADO


def separar_blocos(linhas: list[dict]) -> dict[str, list[dict]]:
    blocos = {"entregues": [], "falhas": [], "abertos": []}
    for p in linhas:
        chave = {"entregue": "entregues", "insucesso": "falhas"}.get(p["situacao"], "abertos")
        blocos[chave].append(p)
    return blocos


# --- Dados --------------------------------------------------------------------

def _codigo_limpo(codigo: str) -> str:
    return str(codigo or "").lstrip("#")


def _tabela_existe(conn: sqlite3.Connection, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def carregar_tratativas(conn: sqlite3.Connection, codigos: list[str]) -> dict[str, str]:
    """{codigo sem '#': tratativa pro cliente} dos pedidos com falha. Tabela
    que ainda nao existe no banco conta como vazia."""
    codigos = [_codigo_limpo(c) for c in codigos if c]
    if not codigos:
        return {}
    marcadores = ",".join("?" for _ in codigos)
    torre, eventos, aguardando = {}, {}, set()
    if _tabela_existe(conn, "torre_excecoes_tratadas"):
        for id_excecao, motivo in conn.execute(
                f"SELECT id, motivo FROM torre_excecoes_tratadas WHERE id IN ({marcadores})",
                [f"insucesso:{c}" for c in codigos]):
            torre[id_excecao.split(":", 1)[1]] = motivo
    if _tabela_existe(conn, "tratativas_pedido"):
        for codigo, evento in conn.execute(
                f"SELECT pedido_code, evento FROM tratativas_pedido WHERE pedido_code IN ({marcadores})", codigos):
            eventos.setdefault(codigo, set()).add(evento)
    if _tabela_existe(conn, "insucessos_aguardando_resposta"):
        aguardando = {r[0] for r in conn.execute(
            f"SELECT code FROM insucessos_aguardando_resposta WHERE status = 'PENDENTE' AND code IN ({marcadores})",
            codigos)}
    return {c: tratativa_para_cliente(torre.get(c), eventos.get(c, set()), c in aguardando) for c in codigos}


def _garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notificacoes_enviadas (
            tipo       TEXT NOT NULL,
            chave      TEXT NOT NULL,
            enviado_em TEXT NOT NULL,
            PRIMARY KEY (tipo, chave)
        )""")


def _chave(sender_id: int, data_alvo: date, redirecionado: bool) -> str:
    # o redirecionado (piloto) tem chave propria: ligar o envio real no mesmo
    # dia ainda manda pro cliente.
    return f"{sender_id}|{data_alvo.isoformat()}" + ("|redirecionado" if redirecionado else "")


def ja_enviado(conn: sqlite3.Connection, chave: str) -> bool:
    return conn.execute("SELECT 1 FROM notificacoes_enviadas WHERE tipo = ? AND chave = ?",
                        (TIPO_NOTIFICACAO, chave)).fetchone() is not None


def registrar_envio(conn: sqlite3.Connection, chave: str) -> None:
    conn.execute("INSERT OR REPLACE INTO notificacoes_enviadas (tipo, chave, enviado_em) VALUES (?, ?, ?)",
                 (TIPO_NOTIFICACAO, chave, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


def fabrica_de_links(config: dict):
    """funcao(linha) -> URL do canhoto, ou "" quando o portal nao esta
    configurado (o e-mail sai sem link, apontando pro portal)."""
    portal = (config or {}).get("portal_cliente") or {}
    segredo = portal.get("secret_key")
    url_base = (portal.get("url_base") or URL_PORTAL).rstrip("/")
    if not segredo:
        logger.warning("portal_cliente.secret_key ausente -- e-mails saem sem o link do canhoto.")
        return lambda linha: ""
    return lambda linha: (link_canhoto.url(linha["service_id"], linha["sender_id"], segredo, url_base)
                          if linha.get("service_id") and linha.get("sender_id") else "")


# --- E-mail -------------------------------------------------------------------

def _td(conteudo: str, extra: str = "") -> str:
    return (f"<td style='padding:9px 10px;border-bottom:1px solid {COR_BORDA};font-size:13px;"
            f"color:{COR_TEXTO};vertical-align:top;{extra}'>{conteudo}</td>")


def _pequeno(texto: str) -> str:
    return f"<span style='font-size:12px;color:{COR_TEXTO_SUAVE};'>{texto}</span>"


def _nf(p: dict) -> str:
    """NF em destaque com o codigo do pedido embaixo. Sao 3 colunas por
    tabela de proposito: e-mail nao pode contar com media query, e com 5
    colunas a tabela estourava a largura do celular."""
    nf = html.escape(p["nf"]) if p.get("nf") else "—"
    return _td(f"<strong>{nf}</strong><br>" + _pequeno(html.escape(p["codigo"])), "white-space:nowrap;")


def _destinatario(p: dict) -> str:
    return _td(html.escape(str(p.get("destinatario") or "")) + "<br>"
               + _pequeno(html.escape(str(p.get("endereco") or ""))))


def _tabela(titulo: str, cor: str, colunas: tuple[str, ...], linhas_html: str) -> str:
    cab = "".join(
        f"<td style='padding:8px 10px;font-size:11px;font-weight:600;color:{COR_PRIMARIA};"
        f"text-transform:uppercase;letter-spacing:0.4px;'>{c}</td>" for c in colunas)
    return f"""
    <p style="margin:0 0 8px;font-size:14px;font-weight:600;color:{cor};">{titulo}</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid {COR_BORDA};border-top:2px solid {cor};border-radius:8px;overflow:hidden;margin-bottom:24px;">
      <tr style="background-color:{COR_PRIMARIA_CLARA};">{cab}</tr>
      {linhas_html}
    </table>"""


def _ordenar(pedidos: list[dict]) -> list[dict]:
    return sorted(pedidos, key=lambda p: (p.get("nf") or "~", p.get("codigo") or ""))


def _bloco_entregues(pedidos: list[dict], link_do_canhoto) -> str:
    linhas = ""
    for p in _ordenar(pedidos):
        url = link_do_canhoto(p)
        canhoto = (f"<a href=\"{html.escape(url)}\" style=\"color:{COR_VERDE_TEXTO};font-weight:600;"
                   f"white-space:nowrap;\">Ver canhoto</a>") if url else _pequeno("Canhoto no portal")
        hora = html.escape(str(p.get("concluido_em") or ""))
        linhas += "<tr>" + _nf(p) + _destinatario(p) + _td((hora + "<br>" if hora else "") + canhoto) + "</tr>"
    return _tabela(f"Entregues ({len(pedidos)})", COR_VERDE_TEXTO, ("NF / pedido", "Destinatário", "Entrega"), linhas)


def _bloco_falhas(pedidos: list[dict], tratativas: dict[str, str]) -> str:
    linhas = ""
    for p in _ordenar(pedidos):
        tratativa = tratativas.get(_codigo_limpo(p["codigo"]), TRATATIVA_GENERICA)
        ocorrencia = (html.escape(motivo_para_cliente(p.get("motivo"))) + "<br>"
                      + _pequeno("Tratativa:") + f" <strong>{html.escape(tratativa)}</strong>")
        linhas += "<tr>" + _nf(p) + _destinatario(p) + _td(ocorrencia) + "</tr>"
    return _tabela(f"Com falha na entrega ({len(pedidos)})", COR_ERRO,
                   ("NF / pedido", "Destinatário", "Motivo e tratativa"), linhas)


def _bloco_abertos(pedidos: list[dict]) -> str:
    linhas = ""
    for p in _ordenar(pedidos):
        linhas += ("<tr>" + _nf(p) + _destinatario(p)
                   + _td(html.escape(str(p.get("situacao_rotulo") or ""))) + "</tr>")
    return _tabela(f"Não concluídas até o momento ({len(pedidos)})", COR_AMBAR_TEXTO,
                   ("NF / pedido", "Destinatário", "Situação"), linhas)


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def montar_email(nome_emb: str, blocos: dict[str, list[dict]], data_alvo: date, tratativas: dict[str, str],
                 link_do_canhoto, destino_original: list[str] | None = None) -> tuple[str, str] | None:
    """(assunto, corpo_html), ou None se o embarcador nao teve nota em rota."""
    entregues, falhas, abertos = blocos["entregues"], blocos["falhas"], blocos["abertos"]
    total = len(entregues) + len(falhas) + len(abertos)
    if not total:
        return None

    partes = []
    if entregues:
        partes.append(_plural(len(entregues), "entregue", "entregues"))
    if falhas:
        partes.append(f"{len(falhas)} com falha")
    if abertos:
        partes.append(f"{len(abertos)} em aberto")
    assunto = f"[Freshlog] Entregas de {data_alvo.strftime('%d/%m')}: {', '.join(partes)}"

    aviso = ""
    if destino_original is not None:
        aviso = (f"<p style='margin:0 0 20px;padding:10px 14px;font-size:12px;color:{COR_TEXTO};"
                 f"background-color:#FEF3C7;border-radius:6px;'><strong>Redirecionado (piloto).</strong> "
                 f"Destino real: {html.escape(', '.join(destino_original) or 'sem e-mail cadastrado')}</p>")

    def _numero(valor: int, rotulo: str, cor: str) -> str:
        return (f"<td align='center' style='padding:14px 8px;border:1px solid {COR_BORDA};border-radius:8px;'>"
                f"<span style='display:block;font-size:26px;font-weight:700;color:{cor};line-height:1.1;'>{valor}</span>"
                f"<span style='font-size:12px;color:{COR_TEXTO_SUAVE};'>{rotulo}</span></td>")

    numeros = (f"<table role='presentation' width='100%' cellpadding='0' cellspacing='8' style='margin:0 0 24px;'><tr>"
               + _numero(len(entregues), "entregues", COR_VERDE_TEXTO)
               + _numero(len(falhas), "com falha", COR_ERRO if falhas else COR_TEXTO_SUAVE)
               + _numero(len(abertos), "em aberto", COR_TEXTO if abertos else COR_TEXTO_SUAVE)
               + "</tr></table>")

    corpo_blocos = ((_bloco_falhas(falhas, tratativas) if falhas else "")
                    + (_bloco_abertos(abertos) if abertos else "")
                    + (_bloco_entregues(entregues, link_do_canhoto) if entregues else ""))

    nota_canhoto = (f"<p style='margin:0 0 20px;font-size:12px;color:{COR_TEXTO_SUAVE};line-height:1.6;'>"
                    f"\"Ver canhoto\" abre o comprovante sem precisar de senha. Se o motorista ainda não enviou a "
                    f"foto, o mesmo link passa a funcionar assim que ela chegar.</p>") if entregues else ""

    conteudo = f"""
    {aviso}
    <h2 style="margin:0 0 4px;font-size:20px;color:{COR_TEXTO};">Resumo das entregas de {data_alvo.strftime('%d/%m/%Y')}</h2>
    <p style="margin:0 0 20px;font-size:14px;color:{COR_TEXTO_SUAVE};line-height:1.6;">
      Olá, <strong>{html.escape(str(nome_emb))}</strong>! Este é o status de
      {'todas as ' + str(total) + ' notas' if total > 1 else 'sua nota'} que
      {'estavam' if total > 1 else 'estava'} em rota em {data_alvo.strftime('%d/%m')}.
    </p>
    {numeros}
    {corpo_blocos}
    {nota_canhoto}
    <p style="margin:0 0 24px;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      O detalhe de cada entrega está no
      <a href="{URL_PORTAL}" style="color:{COR_VERDE_TEXTO};font-weight:600;">portal do cliente</a>.
      Para escolher quais e-mails receber, ou trocar o endereço, use o botão <strong>Notificações</strong> do portal.
    </p>
    <p style="margin:0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      Atenciosamente,<br><strong>Freshlog Logística</strong>
    </p>"""
    return assunto, envelope_html(conteudo, "Freshlog Logística - resumo diário automático das entregas.")


# --- Execucao -----------------------------------------------------------------

def executar(config: dict, data_alvo: date, modo_teste: bool = False, sender_id: int | None = None,
             rotas: list[dict] | None = None, motoristas: dict | None = None,
             enviar=enviar_email, db_path: Path = DB_PATH) -> dict:
    r = {"enviados": 0, "falhas": 0, "sem_notas": 0, "sem_email": 0, "ja_enviados": 0,
         "notas": 0, "desativado": ""}

    forcar = forcar_destino_do_config(config)
    if not modo_teste:
        if not rotina_ativa(config):
            r["desativado"] = "resumo_diario_embarcador.ativo=false"
        elif not forcar and not notificacoes_automaticas_ativas(config):
            r["desativado"] = "notificacoes_automaticas.ativo=false"
        if r["desativado"]:
            logger.info(f"Desativado ({r['desativado']}) -- nada enviado.")
            return r

    todos = preferencias_notificacao.carregar_embarcadores(TIPO_NOTIFICACAO, db_path=db_path)
    # --sender-id em modo teste prova o e-mail de quem ainda nao ligou o resumo
    forcado = modo_teste and sender_id is not None
    embarcadores = sorted(((sid, emb) for sid, emb in todos.items()
                           if (sender_id is None or sid == sender_id) and (forcado or not emb["desligado"])),
                          key=lambda par: par[1]["nome"].lower())
    if not embarcadores:
        logger.info("Nenhum embarcador com o resumo diario ligado -- nada a fazer.")
        return r

    if rotas is None:
        rotas = dados_cliente.buscar_rotas_do_dia(config.get("vuupt_api", {}).get("token", ""), data_alvo)
    if motoristas is None:
        motoristas = dados_cliente._catalogo_motoristas(config)
    logger.info(f"{len(rotas)} rota(s) em {data_alvo.strftime('%d/%m/%Y')}, {len(embarcadores)} embarcador(es) "
                f"com o resumo ligado.")
    link_do_canhoto = fabrica_de_links(config)

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _garantir_tabela(conn)
        for sid, emb in embarcadores:
            nome = emb["nome"] or f"Remetente {sid}"
            try:
                blocos = separar_blocos(dados_cliente.linhas_das_rotas(rotas, sid, motoristas, {}))
                total = sum(len(b) for b in blocos.values())
                if not total:
                    r["sem_notas"] += 1
                    continue
                if not emb["emails"]:
                    logger.warning(f"  {nome}: {total} nota(s) -- sem e-mail cadastrado.")
                    r["sem_email"] += 1
                    continue

                destinos, redirecionado = resolver_destinos(emb["emails"], modo_teste, forcar)
                chave = _chave(sid, data_alvo, redirecionado)
                if not modo_teste and ja_enviado(conn, chave):
                    logger.info(f"  {nome}: ja enviado hoje -- pulando.")
                    r["ja_enviados"] += 1
                    continue

                tratativas = carregar_tratativas(conn, [p["codigo"] for p in blocos["falhas"]])
                assunto, corpo = montar_email(nome, blocos, data_alvo, tratativas, link_do_canhoto,
                                              destino_original=emb["emails"] if redirecionado else None)
                if enviar(destinos, assunto, corpo, config.get("email", {})):
                    r["enviados"] += 1
                    r["notas"] += total
                    if not modo_teste:
                        registrar_envio(conn, chave)
                    logger.info(f"  OK: {nome} -> {', '.join(destinos)}"
                                f"{' (original: ' + ', '.join(emb['emails']) + ')' if redirecionado else ''}"
                                f" | {len(blocos['entregues'])} entregue(s), {len(blocos['falhas'])} falha(s), "
                                f"{len(blocos['abertos'])} em aberto")
                else:
                    r["falhas"] += 1
            except Exception as e:
                logger.exception(f"  {nome}: falhou -- {e}")
                r["falhas"] += 1
    finally:
        conn.close()
    return r


def _interpretar_data(valor: str) -> date:
    v = (valor or "hoje").strip().lower()
    if v == "hoje":
        return date.today()
    if v == "ontem":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(v)


def main(modo_teste: bool = False, data: str = "hoje", sender_id: int | None = None) -> None:
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Resumo diario por embarcador iniciado.")
    inicio = time.time()
    resultado = {"status": "ok", "detalhe": ""}
    config = {}
    try:
        config = _carregar_config()
        r = executar(config, _interpretar_data(data), modo_teste=modo_teste, sender_id=sender_id)
        if r["desativado"]:
            resultado["detalhe"] = f"Desativado ({r['desativado']})."
        else:
            forcar = forcar_destino_do_config(config)
            partes = [f"{r['enviados']} e-mail(s), {r['notas']} nota(s)", f"{r['falhas']} falha(s)"]
            if r["sem_email"]:
                partes.append(f"{r['sem_email']} sem e-mail")
            if r["ja_enviados"]:
                partes.append(f"{r['ja_enviados']} ja enviado(s) hoje")
            if forcar and not modo_teste:
                partes.append(f"redirecionado pra {forcar}")
            resultado["detalhe"] = "; ".join(partes)
            if r["falhas"]:
                resultado["status"] = "erro"
        logger.info(f"RESUMO {'(MODO TESTE) ' if modo_teste else ''}{resultado['detalhe']}")
    except Exception as e:
        logger.exception(f"Erro no resumo diario: {e}")
        resultado["status"] = "erro"
        resultado["detalhe"] = str(e)
    finally:
        try:
            notificar_execucao({"Resumo diário por embarcador": resultado}, time.time() - inicio,
                               modo_teste, config or _carregar_config())
        except Exception as e:
            logger.warning(f"Falha ao notificar execucao (nao afeta o resultado): {e}")
    if resultado["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--modo-teste", action="store_true")
    parser.add_argument("--data", default="hoje", help="hoje, ontem ou AAAA-MM-DD")
    parser.add_argument("--sender-id", type=int, default=None)
    args = parser.parse_args()
    main(modo_teste=args.modo_teste, data=args.data, sender_id=args.sender_id)
