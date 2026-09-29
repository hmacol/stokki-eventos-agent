# -*- coding: utf-8 -*-
"""
notificar_whatsapp.py

Aviso curto no grupo interno de WhatsApp para notificacoes que ja saem
por e-mail (pedido do Hugo, 28/09/2026). O e-mail continua sendo o
registro completo; aqui vai so o essencial: contagens e nome da rotina,
NUNCA nome de cliente, endereco, NF ou log.

Tres origens chamam este modulo, sempre DEPOIS do e-mail:
    notificar_execucao_agente.notificar_execucao  -> avisar_execucao
    alertar_falha_job.main                        -> avisar_falha_job
    verificar_entregues_nao_expedidos.main        -> avisar_nao_expedidos

O numero que envia e o do proprio Hugo, por um gateway nao-oficial
(integracao_openwa.py). Por isso: desligado por padrao, teto diario,
intervalo minimo, sem repeticao e SEM reenvio automatico (licao do erro
463 em 27/08: insistir piora).

Falha aqui NUNCA derruba a rotina que chamou.

config.yaml:
    whatsapp_notificacoes:
      ativo: false
      base_url: "http://127.0.0.1:2785/api"
      api_key: "..."
      sessao: "..."
      grupo_id: "...@g.us"
      sempre_avisar: [cancelar_rotas_sem_motorista, criar_rotas_diarias, pipeline]
      teto_diario: 20
      intervalo_min_seg: 20
      janela_repeticao_min: 120
      falhas_para_alerta: 3
"""
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import integracao_openwa

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "dados" / "dados.db"
MAX_DETALHE = 200
MAX_ETAPAS_ERRO = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notificacoes_whatsapp (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    criado_em    TEXT NOT NULL,
    origem       TEXT NOT NULL,
    tipo         TEXT NOT NULL,
    assinatura   TEXT,
    situacao     TEXT NOT NULL,
    motivo       TEXT,
    id_mensagem  TEXT
)"""


# --- Texto --------------------------------------------------------------------

def _uma_linha(texto, limite: int = MAX_DETALHE) -> str:
    """Uma linha so, sem os caracteres de formatacao do WhatsApp, cortada."""
    limpo = re.sub(r"[*_~`]", "", str(texto or ""))
    limpo = re.sub(r"\s+", " ", limpo).strip()
    return limpo if len(limpo) <= limite else limpo[:limite - 1].rstrip() + "…"


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def texto_execucao(resumo_etapas: dict, duracao_seg: float, titulo: str | None = None,
                   agora: datetime | None = None) -> str:
    from notificar_execucao_agente import (etapas_com_erro, formatar_duracao, resumir_execucao,
                                           titulo_da_rotina)
    resumo_etapas = resumo_etapas or {}
    erros = etapas_com_erro(resumo_etapas)
    nome = _uma_linha(titulo_da_rotina(resumo_etapas, titulo), 80)
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    resumo = resumir_execucao(resumo_etapas).rstrip(".")
    if not erros:
        return f"✅ *{nome}* · {quando}\n{resumo} ({formatar_duracao(duracao_seg)})"
    linhas = [f"❌ *{nome}* · {quando}", resumo]
    for etapa in erros[:MAX_ETAPAS_ERRO]:
        detalhe = _uma_linha((resumo_etapas.get(etapa) or {}).get("detalhe"))
        if detalhe:
            linhas.append(f"{_uma_linha(etapa, 60)}: {detalhe}")
    linhas.append("Detalhes completos no e-mail.")
    return "\n".join(linhas)


def texto_falha_job(unidade: str, info: dict, agora: datetime | None = None) -> str:
    info = info or {}
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    return "\n".join([
        "🚨 *Job da VPS falhou*",
        _uma_linha(unidade, 80),
        f"Resultado: {_uma_linha(info.get('Result', '?'), 40)} "
        f"(código {_uma_linha(info.get('ExecMainStatus', '?'), 10)}) · {quando}",
        "Log completo no e-mail.",
    ])


def texto_nao_expedidos(n_alertas: int, n_rotas: int, n_retiradas: int) -> str:
    linhas = ["⚠️ *Checagem da expedição*"]
    if n_alertas:
        linhas.append(f"{_plural(n_alertas, 'pedido entregue', 'pedidos entregues')} sem expedição na Stokki")
    partes = []
    if n_rotas:
        partes.append(f"{_plural(n_rotas, 'rota', 'rotas')} sem terminar")
    if n_retiradas:
        partes.append(f"{_plural(n_retiradas, 'retirada parada', 'retiradas paradas')} há mais de 7 dias")
    if partes:
        linhas.append(" · ".join(partes))
    linhas.append("Lista completa no e-mail.")
    return "\n".join(linhas)


# --- Envio --------------------------------------------------------------------

def _cfg(config: dict | None) -> dict:
    return (config or {}).get("whatsapp_notificacoes") or {}


def _inteiro(cfg: dict, chave: str, padrao: int) -> int:
    try:
        return int(cfg.get(chave, padrao))
    except (TypeError, ValueError):
        return padrao


def _iso(quando: datetime) -> str:
    return quando.isoformat(timespec="seconds")


def _registrar(conn, agora, origem, tipo, assinatura, situacao, motivo=None, id_mensagem=None):
    conn.execute(
        "INSERT INTO notificacoes_whatsapp (criado_em, origem, tipo, assinatura, situacao, motivo, id_mensagem) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_iso(agora), origem, tipo, assinatura, situacao, motivo, id_mensagem))
    conn.commit()


def _motivo_para_nao_enviar(conn, cfg: dict, origem: str, assinatura: str | None, agora: datetime) -> str | None:
    if assinatura:
        desde = _iso(agora - timedelta(minutes=_inteiro(cfg, "janela_repeticao_min", 120)))
        if conn.execute(
                "SELECT 1 FROM notificacoes_whatsapp WHERE origem = ? AND assinatura = ? "
                "AND situacao = 'enviado' AND criado_em >= ? LIMIT 1",
                (origem, assinatura, desde)).fetchone():
            return "repetida dentro da janela"
    inicio_do_dia = _iso(agora.replace(hour=0, minute=0, second=0, microsecond=0))
    enviadas = conn.execute(
        "SELECT COUNT(*) FROM notificacoes_whatsapp WHERE situacao = 'enviado' AND criado_em >= ?",
        (inicio_do_dia,)).fetchone()[0]
    if enviadas >= _inteiro(cfg, "teto_diario", 20):
        return "teto diario atingido"
    return None


def _esperar_intervalo(conn, cfg: dict, agora: datetime, dormir) -> None:
    ultima = conn.execute(
        "SELECT MAX(criado_em) FROM notificacoes_whatsapp WHERE situacao = 'enviado'").fetchone()[0]
    if not ultima:
        return
    intervalo = _inteiro(cfg, "intervalo_min_seg", 20)
    falta = intervalo - (agora - datetime.fromisoformat(ultima)).total_seconds()
    if 0 < falta <= intervalo:
        dormir(falta)


def _alertar_se_canal_parou(conn, cfg: dict, config: dict) -> None:
    """Um e-mail so, exatamente na N-esima falha seguida. A N+1 nao repete;
    um envio com sucesso zera a contagem."""
    limite = _inteiro(cfg, "falhas_para_alerta", 3)
    seguidas = 0
    for (situacao,) in conn.execute(
            "SELECT situacao FROM notificacoes_whatsapp WHERE situacao IN ('enviado', 'falhou') "
            "ORDER BY id DESC LIMIT ?", (limite + 1,)):
        if situacao != "falhou":
            break
        seguidas += 1
    if seguidas != limite:
        return
    destino = (config.get("notificacao_execucao") or {}).get("destinatario") \
        or (config.get("email") or {}).get("remetente")
    if not destino:
        return
    from email_utils import envelope_html, enviar_email
    corpo = (f"<h2 style='margin:0 0 12px;color:#EF4444'>WhatsApp das notificações parou</h2>"
             f"<p>{limite} envios seguidos falharam. O gateway pode estar fora do ar ou o número "
             f"desconectado. Os e-mails continuam saindo normalmente.</p>"
             f"<p>Conferir na VPS: <code>docker ps</code> e a tabela <code>notificacoes_whatsapp</code>.</p>")
    enviar_email([destino], "[ALERTA] WhatsApp das notificações parou", envelope_html(corpo),
                 config.get("email", {}))


def _despachar(config, origem, tipo, texto, assinatura, modo_teste, conn, agora, dormir) -> str:
    cfg = _cfg(config)
    if not cfg.get("ativo") or not integracao_openwa.configurado(cfg) or not cfg.get("grupo_id"):
        return "desligado"
    if modo_teste:
        logger.info(f"[MODO TESTE] WhatsApp nao enviado ({origem}). Texto:\n{texto}")
        return "modo_teste"
    agora = agora or datetime.now()
    fechar = conn is None
    if fechar:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute(_SCHEMA)
        motivo = _motivo_para_nao_enviar(conn, cfg, origem, assinatura, agora)
        if motivo:
            _registrar(conn, agora, origem, tipo, assinatura, "nao_enviado", motivo)
            logger.info(f"WhatsApp nao enviado ({origem}): {motivo}.")
            return "nao_enviado"
        _esperar_intervalo(conn, cfg, agora, dormir)
        ok, id_mensagem = integracao_openwa.enviar_texto(cfg, cfg["grupo_id"], texto)
        if ok:
            _registrar(conn, agora, origem, tipo, assinatura, "enviado", None, id_mensagem)
            logger.info(f"WhatsApp enviado ao grupo ({origem}).")
            return "enviado"
        _registrar(conn, agora, origem, tipo, assinatura, "falhou", "gateway fora do ar ou envio recusado")
        _alertar_se_canal_parou(conn, cfg, config)
        return "falhou"
    finally:
        if fechar:
            conn.close()


def despachar(config: dict, origem: str, tipo: str, texto: str, assinatura: str | None = None,
              modo_teste: bool = False, conn=None, agora: datetime | None = None,
              dormir=time.sleep) -> str:
    """Aplica as regras e envia. Devolve a situacao: desligado | modo_teste |
    nao_enviado | enviado | falhou. Nunca levanta excecao."""
    try:
        return _despachar(config, origem, tipo, texto, assinatura, modo_teste, conn, agora, dormir)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


# --- Pontos de chamada -----------------------------------------------------------

def avisar_execucao(resumo_etapas: dict, duracao_seg: float, modo_teste: bool, config: dict,
                    titulo: str | None = None, **kw) -> str:
    """Resumo de rotina. So vai pro grupo se houve erro ou se o script
    esta em whatsapp_notificacoes.sempre_avisar."""
    try:
        if not _cfg(config).get("ativo"):
            return "desligado"
        from notificar_execucao_agente import etapas_com_erro
        origem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
        erros = etapas_com_erro(resumo_etapas or {})
        if not erros and origem not in (_cfg(config).get("sempre_avisar") or []):
            return "nao_relevante"
        assinatura = "erro:" + ",".join(erros) if erros else None
        texto = texto_execucao(resumo_etapas, duracao_seg, titulo, kw.get("agora"))
        return despachar(config, origem or "desconhecido", "execucao", texto, assinatura, modo_teste, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_falha_job(unidade: str, info: dict, config: dict, **kw) -> str:
    """Sem regra de repeticao aqui: alertar_falha_job.py ja tem a janela
    anti-enxurrada e so chama quando o e-mail tambem saiu."""
    try:
        return despachar(config, unidade, "falha_job", texto_falha_job(unidade, info, kw.get("agora")), **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"


def avisar_nao_expedidos(n_alertas: int, n_rotas: int, n_retiradas: int, config: dict, **kw) -> str:
    try:
        return despachar(config, "verificar_entregues_nao_expedidos", "nao_expedidos",
                         texto_nao_expedidos(n_alertas, n_rotas, n_retiradas), **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"
