# -*- coding: utf-8 -*-
"""
notificar_whatsapp_embarcador.py

WhatsApp para o EMBARCADOR (pedido do Hugo, 30/09/2026), junto com o e-mail
de dois tipos de aviso: insucesso aguardando retorno e agendamento
(pendente e dia fixo). O embarcador informa o celular no botao Notificacoes
do portal (preferencias_notificacao.py, coluna `celulares`).

Regras:
  - Vai SO depois que o e-mail do mesmo aviso saiu. E-mail desligado, sem
    e-mail ou com falha = sem WhatsApp. O e-mail continua sendo o registro
    completo; aqui vai uma mensagem curta com os codigos dos pedidos.
  - Sai pelo mesmo gateway/numero dos avisos internos (integracao_openwa.py,
    o numero do Hugo -- decisao dele, 30/09). Por isso reaproveita
    notificar_whatsapp.despachar: disjuntor, intervalo minimo, janela de
    repeticao e tabela notificacoes_whatsapp sao os mesmos. O teto diario e
    proprio (origem "embarcador:<tipo>") e nao gasta o dos avisos internos.
  - Resposta do cliente pelo WhatsApp cai no celular do Hugo e NINGUEM le
    automaticamente. Por isso o texto manda responder pelo link (insucesso)
    ou pelo e-mail (agendamento).
  - Desligado por padrao. `forcar_destino` manda tudo pra um numero de teste.
    Falha aqui nunca derruba a rotina que chamou.

config.yaml (dentro da secao whatsapp_notificacoes, que ja tem o gateway):
    whatsapp_notificacoes:
      ativo: true                     # chave do gateway (avisos internos)
      embarcadores:
        ativo: false                  # chave destes avisos
        forcar_destino: "11999998888" # opcional: tudo vai pra este celular
        teto_diario: 30               # total de mensagens a embarcadores/dia
        teto_por_embarcador: 6        # por embarcador/dia (somando celulares)
"""
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import yaml

import notificar_whatsapp
from notificar_whatsapp import ORIGEM_EMBARCADOR, _uma_linha

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
MAX_CODIGOS = 10
RODAPE = "Mensagem automática da Freshlog. Para não receber, apague seu celular em Notificações no portal."


# --- Texto --------------------------------------------------------------------

def _codigo(servico: dict) -> str:
    return _uma_linha((servico or {}).get("code"), 30).lstrip("#")


def _lista_codigos(codigos: list) -> str:
    codigos = [c for c in codigos if c]
    texto = ", ".join(codigos[:MAX_CODIGOS])
    if len(codigos) > MAX_CODIGOS:
        texto += f" e mais {len(codigos) - MAX_CODIGOS}"
    return texto


def _ola(nome) -> str:
    nome = _uma_linha(nome, 60)
    return f"Olá, {nome}. " if nome else "Olá. "


def _pedidos(n: int) -> str:
    return "1 pedido" if n == 1 else f"{n} pedidos"


def texto_insucesso(nome: str, motivo: str, pedidos: list, url_resposta: str) -> str:
    linhas = [
        "⚠️ *Freshlog · Insucesso na entrega*",
        _ola(nome) + f"Não conseguimos entregar {_pedidos(len(pedidos))} "
        f"({_uma_linha(motivo, 80) or 'motivo não informado'}):",
        _lista_codigos([_codigo(p) for p in pedidos]),
        "Deseja o reenvio? Responda por este link (sem resposta, não reenviamos):",
        url_resposta,
        RODAPE,
    ]
    return "\n".join(linhas)


def texto_agendamento_pendente(nome: str, pedidos: list) -> str:
    linhas = [
        "📅 *Freshlog · Agendamento pendente*",
        _ola(nome)
        + ("Este pedido tem destinatário que exige agendamento e está sem data confirmada:"
           if len(pedidos) == 1 else
           f"Estes {len(pedidos)} pedidos têm destinatário que exige agendamento e estão sem data confirmada:"),
        _lista_codigos([_codigo(p) for p in pedidos]),
        "Responda o e-mail que enviamos com a data e o horário de cada pedido.",
        RODAPE,
    ]
    return "\n".join(linhas)


def texto_agendamento_dia_fixo(nome: str, itens: list) -> str:
    codigos = [f"{_codigo(i['servico'])} ({i['data'].strftime('%d/%m')})" for i in itens]
    linhas = [
        "📅 *Freshlog · Entrega agendada*",
        _ola(nome)
        + ("Este pedido foi agendado para o dia de entrega da região:" if len(itens) == 1 else
           f"Estes {len(itens)} pedidos foram agendados para o dia de entrega da região:"),
        _lista_codigos(codigos),
        "Precisa de outra data? Responda o e-mail que enviamos.",
        RODAPE,
    ]
    return "\n".join(linhas)


# --- Envio --------------------------------------------------------------------

def _carregar_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _cfg(config: dict) -> dict:
    return notificar_whatsapp._cfg(config).get("embarcadores") or {}


def ligado(config: dict | None = None) -> bool:
    config = _carregar_config() if config is None else config
    return bool(notificar_whatsapp._cfg(config).get("ativo") and _cfg(config).get("ativo"))


def _so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _chat_id(celular: str) -> str:
    return f"55{celular}@c.us"


def _motivo_teto(conn, cfg: dict, cnpj: str, agora: datetime) -> str | None:
    inicio_do_dia = notificar_whatsapp._iso(agora.replace(hour=0, minute=0, second=0, microsecond=0))
    total, do_embarcador = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(assinatura LIKE ?), 0) FROM notificacoes_whatsapp "
        "WHERE situacao = 'enviado' AND criado_em >= ? AND origem LIKE ?",
        (f"{cnpj}|%", inicio_do_dia, ORIGEM_EMBARCADOR + "%")).fetchone()
    if total >= notificar_whatsapp._inteiro(cfg, "teto_diario", 30):
        return "teto diario de embarcadores atingido"
    if do_embarcador >= notificar_whatsapp._inteiro(cfg, "teto_por_embarcador", 6):
        return "teto diario do embarcador atingido"
    return None


def _avisar(emb, tipo, chave, texto, modo_teste, config, conn, agora, dormir) -> list[str]:
    config = _carregar_config() if config is None else config
    if not ligado(config):
        return ["desligado"]
    celulares = emb.get("celulares") or []
    if not celulares:
        return ["sem_celular"]
    cfg = _cfg(config)
    forcar = _so_digitos(cfg.get("forcar_destino"))[-11:]
    if forcar:
        texto = f"[TESTE · iria para {_uma_linha(emb.get('nome'), 40)}: {', '.join(celulares)}]\n{texto}"
        celulares = [forcar]
    if modo_teste:
        logger.info(f"[MODO TESTE] WhatsApp ao embarcador nao enviado ({tipo}, {emb.get('nome')} -> "
                    f"{celulares}). Texto:\n{texto}")
        return ["modo_teste"]

    fechar = conn is None
    if fechar:
        conn = sqlite3.connect(notificar_whatsapp.DB_PATH, timeout=10)
    try:
        conn.execute(notificar_whatsapp._SCHEMA)
        cnpj = _so_digitos(emb.get("cnpj")) or "sem-cnpj"
        origem = ORIGEM_EMBARCADOR + tipo
        situacoes = []
        for celular in celulares:
            quando = agora or datetime.now()
            assinatura = f"{cnpj}|{chave}|{celular}"
            motivo = _motivo_teto(conn, cfg, cnpj, quando)
            if motivo:
                notificar_whatsapp._registrar(conn, quando, origem, tipo, assinatura, "nao_enviado", motivo)
                logger.info(f"WhatsApp ao embarcador nao enviado ({emb.get('nome')}): {motivo}.")
                situacoes.append("nao_enviado")
                continue
            situacoes.append(notificar_whatsapp.despachar(
                config, origem, tipo, texto, assinatura, conn=conn, agora=quando, dormir=dormir,
                grupo_id=_chat_id(celular)))
        return situacoes
    finally:
        if fechar:
            conn.close()


def avisar(emb: dict, tipo: str, chave: str, texto: str, modo_teste: bool = False,
           config: dict | None = None, conn=None, agora: datetime | None = None,
           dormir=time.sleep) -> list[str]:
    """Manda `texto` pra cada celular do embarcador (`emb` no formato de
    preferencias_notificacao.carregar_embarcadores). `chave` identifica o
    aviso (mesma chave = repeticao, barrada pela janela do despachar).
    `config` None le o config.yaml. Devolve a situacao de cada envio.
    Nunca levanta excecao."""
    try:
        return _avisar(emb, tipo, chave, texto, modo_teste, config, conn, agora, dormir)
    except Exception as exc:
        logger.warning(f"Falha no WhatsApp ao embarcador (nao afeta a rotina): {exc}")
        return ["falhou"]
