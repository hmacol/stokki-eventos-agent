# -*- coding: utf-8 -*-
"""
notificar_nfs_em_rota.py

E-mail diario por embarcador com todas as notas que saem pra entrega hoje
(pedido do Hugo, 17/09). Um e-mail por embarcador, so pra quem tem pelo
menos 1 pedido em rota do dia; quem nao tem nada nao recebe.

Fonte: a MESMA do portal do cliente (portal_cliente/dados_cliente) -- rotas
da VUUPT com start_at no dia, situacao parada a parada, NF de
documentos_processados. Entram "em rota" e "programado" (rota do dia que
ainda nao saiu); entregue, insucesso e pedido sem rota ficam de fora. As
rotas sao buscadas UMA vez e recortadas por sender_id.

Destinatarios: preferencias_notificacao.py (tipo "nfs_em_rota") -- o e-mail
e o liga/desliga que o cliente escolhe no botao Notificacoes do portal;
sem preferencia gravada vale o `interno.email` com o aviso ligado.
interno.notificar_email = 0 nao e veto: so faz o aviso nascer desmarcado
(o embarcador liga sozinho no portal).

Travas:
  - config.yaml notificacao_nfs_em_rota.ativo (default True) desliga so
    esta rotina.
  - config.yaml notificacao_nfs_em_rota.forcar_destino: enquanto preenchido,
    TODO e-mail vai so pra ele. SEM a secao no config o padrao e o e-mail
    do Hugo: envio real pro cliente exige `forcar_destino: ""` explicito.
  - chave-mestra notificacoes_automaticas.ativo vale pro envio real (o
    redirecionado nao sai de casa, entao nao depende dela).
  - notificacoes_enviadas (tipo + chave sender|data): rodar duas vezes no
    mesmo dia nao manda duas vezes.

Agendamento (VPS): stokki-notificar-nfs-em-rota.timer, 07:00.

Execute:
  py -3.11 notificar_nfs_em_rota.py --modo-teste                 (tudo pra hugo@, nao marca envio)
  py -3.11 notificar_nfs_em_rota.py --modo-teste --sender-id 123 (um embarcador so)
  py -3.11 notificar_nfs_em_rota.py --data 2026-09-18
"""
import argparse
import html
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from email_utils import (COR_BORDA, COR_DESTAQUE, COR_PRIMARIA, COR_PRIMARIA_CLARA, COR_TEXTO,
                         COR_TEXTO_SUAVE, enviar_email, envelope_html, notificacoes_automaticas_ativas)
from notificar_execucao_agente import notificar_execucao
from portal_cliente import dados_cliente
import preferencias_notificacao

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nfs_em_rota")

CONFIG_PATH = _RAIZ / "config.yaml"
DB_PATH     = _RAIZ / "dados" / "dados.db"
EMAIL_TESTE = "hugo@freshlogbr.com"
URL_PORTAL  = "https://app.freshhub.com.br/cliente"

TIPO_NOTIFICACAO = "nfs_em_rota"
SITUACOES_NO_EMAIL = ("em_rota", "programado")


def _carregar_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _secao(config: dict) -> dict:
    return (config or {}).get("notificacao_nfs_em_rota") or {}


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


# --- Dados --------------------------------------------------------------------

def carregar_embarcadores(db_path: Path = DB_PATH) -> list[dict]:
    """E-mails e liga/desliga vem das preferencias do portal
    (preferencias_notificacao.py, botao Notificacoes): o cliente escolhe o
    e-mail e se quer este aviso; interno.notificar_email = 0 so faz a chave
    nascer desmarcada (nao e veto)."""
    embs = preferencias_notificacao.carregar_embarcadores(TIPO_NOTIFICACAO, db_path=db_path)
    return sorted(({
        "sender_id": int(sender_id),
        "nome": emb["nome"] or f"Remetente {sender_id}",
        "emails": emb["emails"],
        "notificar": not emb["desligado"],
    } for sender_id, emb in embs.items()), key=lambda e: e["nome"].lower())


def pedidos_saindo_hoje(rotas: list[dict], sender_id: int, motoristas: dict) -> list[dict]:
    """Linhas do portal (dados_cliente.linhas_das_rotas) que ainda vao ser
    entregues: em rota ou programadas numa rota do dia."""
    linhas = dados_cliente.linhas_das_rotas(rotas, sender_id, motoristas, {})
    return [p for p in linhas if p["situacao"] in SITUACOES_NO_EMAIL]


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


# --- E-mail -------------------------------------------------------------------

def _chave_rota(nome: str):
    m = re.search(r"\d+", nome or "")
    return (int(m.group(0)) if m else 10**9, nome or "")


def _pilula(texto: str, cor_fundo: str, cor_texto: str) -> str:
    return (f"<span style='display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;"
            f"font-weight:600;background-color:{cor_fundo};color:{cor_texto};white-space:nowrap;'>{texto}</span>")


def _td(conteudo: str, extra: str = "") -> str:
    return (f"<td style='padding:9px 12px;border-bottom:1px solid {COR_BORDA};font-size:13px;"
            f"color:{COR_TEXTO};vertical-align:top;{extra}'>{conteudo}</td>")


def _bloco_rota(nome_rota: str, pedidos: list[dict]) -> str:
    primeiro = pedidos[0]
    partes = [html.escape(nome_rota or "Rota")]
    if primeiro.get("motorista"):
        partes.append("Motorista: " + html.escape(str(primeiro["motorista"])))
    if primeiro.get("placa"):
        partes.append("Placa " + html.escape(str(primeiro["placa"])))

    linhas = ""
    for p in sorted(pedidos, key=lambda p: (p.get("ordem") or 0, p["codigo"])):
        nf = (html.escape(p["nf"]) if p.get("nf")
              else f"<span style='color:{COR_DESTAQUE};font-weight:600;'>NF pendente</span>")
        situacao = (_pilula("Em rota", COR_PRIMARIA_CLARA, "#047857") if p["situacao"] == "em_rota"
                    else _pilula("Programado", "#F3F4F6", COR_TEXTO_SUAVE))
        parada = f"{p['ordem']} de {p['total_paradas']}" if p.get("ordem") else ""
        linhas += ("<tr>"
                   + _td(nf, "font-weight:600;")
                   + _td(html.escape(p["codigo"]), f"color:{COR_TEXTO_SUAVE};white-space:nowrap;")
                   + _td(html.escape(str(p.get("destinatario") or ""))
                         + f"<br><span style='font-size:12px;color:{COR_TEXTO_SUAVE};'>"
                           f"{html.escape(str(p.get('endereco') or ''))}</span>")
                   + _td(parada, "white-space:nowrap;")
                   + _td(situacao)
                   + "</tr>")

    cab = "".join(
        f"<td style='padding:8px 12px;font-size:11px;font-weight:600;color:{COR_PRIMARIA};"
        f"text-transform:uppercase;letter-spacing:0.4px;'>{t}</td>"
        for t in ("NF", "Pedido", "Destinatário", "Parada", "Situação"))
    return f"""
    <p style="margin:0 0 8px;font-size:14px;font-weight:600;color:{COR_PRIMARIA};">{' · '.join(partes)}</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;margin-bottom:24px;">
      <tr style="background-color:{COR_PRIMARIA_CLARA};">{cab}</tr>
      {linhas}
    </table>"""


def montar_email(nome_emb: str, pedidos: list[dict], data_alvo: date,
                 destino_original: list[str] | None = None) -> tuple[str, str] | None:
    """(assunto, corpo_html), ou None se nao ha nada saindo hoje."""
    if not pedidos:
        return None
    qtd = len(pedidos)
    notas = "1 nota" if qtd == 1 else f"{qtd} notas"
    assunto = f"Freshlog | Suas entregas de hoje ({data_alvo.strftime('%d/%m')}): {notas} em rota"

    por_rota: dict[str, list[dict]] = defaultdict(list)
    for p in pedidos:
        por_rota[p.get("rota") or ""].append(p)
    rotas = "1 rota" if len(por_rota) == 1 else f"{len(por_rota)} rotas"
    blocos = "".join(_bloco_rota(nome, por_rota[nome]) for nome in sorted(por_rota, key=_chave_rota))

    aviso = ""
    if destino_original is not None:
        aviso = (f"<p style='margin:0 0 20px;padding:10px 14px;font-size:12px;color:{COR_TEXTO};"
                 f"background-color:#FEF3C7;border-radius:6px;'><strong>Redirecionado (piloto).</strong> "
                 f"Destino real: {html.escape(', '.join(destino_original) or 'sem e-mail cadastrado')}</p>")

    sem_nf = sum(1 for p in pedidos if not p.get("nf"))
    nota_nf = (f"<p style='margin:0 0 20px;font-size:12px;color:{COR_TEXTO_SUAVE};'>"
               f"\"NF pendente\": ainda não recebemos a nota fiscal deste pedido.</p>") if sem_nf else ""

    conteudo = f"""
    {aviso}
    <h2 style="margin:0 0 4px;font-size:20px;color:{COR_TEXTO};">Suas entregas de hoje</h2>
    <p style="margin:0 0 24px;font-size:14px;color:{COR_TEXTO_SUAVE};line-height:1.6;">
      Olá, <strong>{html.escape(str(nome_emb))}</strong>!<br>
      Hoje, {data_alvo.strftime('%d/%m/%Y')}, temos <strong>{notas}</strong> sua{'s' if qtd > 1 else ''}
      saindo para entrega em {rotas}.
    </p>
    {blocos}
    {nota_nf}
    <p style="margin:0 0 24px;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      Acompanhe cada entrega ao vivo, com comprovante, no
      <a href="{URL_PORTAL}" style="color:#047857;font-weight:600;">portal do cliente</a>.
    </p>
    <p style="margin:0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      Atenciosamente,<br><strong>Freshlog Logística</strong>
    </p>"""
    return assunto, envelope_html(conteudo, "Freshlog Logística - resumo diário automático das entregas em rota.")


# --- Execucao -----------------------------------------------------------------

def executar(config: dict, data_alvo: date, modo_teste: bool = False, sender_id: int | None = None,
             rotas: list[dict] | None = None, motoristas: dict | None = None,
             enviar=enviar_email, db_path: Path = DB_PATH) -> dict:
    r = {"enviados": 0, "falhas": 0, "sem_notas": 0, "sem_email": 0, "ja_enviados": 0,
         "notas": 0, "desativado": ""}

    forcar = forcar_destino_do_config(config)
    if not modo_teste:
        if not rotina_ativa(config):
            r["desativado"] = "notificacao_nfs_em_rota.ativo=false"
        elif not forcar and not notificacoes_automaticas_ativas(config):
            r["desativado"] = "notificacoes_automaticas.ativo=false"
        if r["desativado"]:
            logger.info(f"Desativado ({r['desativado']}) -- nada enviado.")
            return r

    embarcadores = [e for e in carregar_embarcadores(db_path)
                    if sender_id is None or e["sender_id"] == sender_id]
    if rotas is None:
        rotas = dados_cliente.buscar_rotas_do_dia(config.get("vuupt_api", {}).get("token", ""), data_alvo)
    if motoristas is None:
        motoristas = dados_cliente._catalogo_motoristas(config)
    logger.info(f"{len(rotas)} rota(s) em {data_alvo.strftime('%d/%m/%Y')}, {len(embarcadores)} embarcador(es).")

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _garantir_tabela(conn)
        for emb in embarcadores:
            nome = emb["nome"]
            try:
                pedidos = pedidos_saindo_hoje(rotas, emb["sender_id"], motoristas)
                if not pedidos:
                    r["sem_notas"] += 1
                    continue
                if not emb["notificar"]:
                    logger.info(f"  {nome}: {len(pedidos)} nota(s) -- aviso desligado (pelo cliente no portal "
                                f"ou nasceu desmarcado por notificar_email = 0).")
                    continue
                if not emb["emails"]:
                    logger.warning(f"  {nome}: {len(pedidos)} nota(s) -- sem e-mail cadastrado.")
                    r["sem_email"] += 1
                    continue

                destinos, redirecionado = resolver_destinos(emb["emails"], modo_teste, forcar)
                chave = _chave(emb["sender_id"], data_alvo, redirecionado)
                if not modo_teste and ja_enviado(conn, chave):
                    logger.info(f"  {nome}: ja enviado hoje -- pulando.")
                    r["ja_enviados"] += 1
                    continue

                assunto, corpo = montar_email(nome, pedidos, data_alvo,
                                              destino_original=emb["emails"] if redirecionado else None)
                if enviar(destinos, assunto, corpo, config.get("email", {})):
                    r["enviados"] += 1
                    r["notas"] += len(pedidos)
                    if not modo_teste:
                        registrar_envio(conn, chave)
                    logger.info(f"  OK: {nome} -> {', '.join(destinos)}"
                                f"{' (original: ' + ', '.join(emb['emails']) + ')' if redirecionado else ''}"
                                f" | {len(pedidos)} nota(s)")
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
    if v in ("amanha", "amanhã"):
        return date.today() + timedelta(days=1)
    return date.fromisoformat(v)


def main(modo_teste: bool = False, data: str = "hoje", sender_id: int | None = None) -> None:
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Notas em rota por embarcador iniciado.")
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
        logger.exception(f"Erro nas notas em rota: {e}")
        resultado["status"] = "erro"
        resultado["detalhe"] = str(e)
    finally:
        try:
            notificar_execucao({"Notas em rota por embarcador": resultado}, time.time() - inicio,
                               modo_teste, config or _carregar_config())
        except Exception as e:
            logger.warning(f"Falha ao notificar execucao (nao afeta o resultado): {e}")
    if resultado["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--modo-teste", action="store_true")
    parser.add_argument("--data", default="hoje", help="hoje, amanha ou AAAA-MM-DD")
    parser.add_argument("--sender-id", type=int, default=None)
    args = parser.parse_args()
    main(modo_teste=args.modo_teste, data=args.data, sender_id=args.sender_id)
