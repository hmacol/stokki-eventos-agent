# -*- coding: utf-8 -*-
"""
roteirizacao/avisar_motoristas_rotas.py

Gera mensagens de aviso de rota para os motoristas escalados numa data
(padrão: amanhã) -- doc de origem:
DOC_EXECUCAO_CLAUDE_NOTIFICACAO_MOTORISTAS.md.

Fase 1 (simples/imediata): mensagens de WhatsApp prontas para
copiar/colar (individual por motorista + escala geral de grupo),
salvas em roteirizacao/dados/mensagens_whatsapp_amanha.txt, e envio
opcional de e-mail para quem tem EMAIL_MOTORISTA cadastrado (ver
regras/preferencias_motoristas.py). Não integra com nenhuma API de
WhatsApp -- continua manual (copiar/colar).

Fase 2 (16/08, pedido do Hugo): com --gerar-confirmacoes, cada
mensagem (WhatsApp e e-mail) ganha um link de confirmação assinado
pra uma página pública (VPS, ver confirmacao_motoristas/app.py) onde o
motorista confirma ou recusa a rota, sem precisar login -- só os 4
últimos dígitos do telefone cadastrado como checagem leve. O estado
fica em regras/confirmacao_rotas.py (local) sincronizado por
push/pull com a VPS (a máquina local não tem entrada de internet, só
consegue empurrar/puxar).

Fonte dos dados: rotas já criadas no VUUPT (POST /routes, ver
criar_rotas_diarias.py / incrementar_rotas.py), NÃO os serviços
avulsos -- é a rota que carrega o agent_id do motorista, o mesmo
padrão de leitura (listar_rotas com include=services, excluindo
status='canceled') já usado em incrementar_rotas.py.

COMO USAR:
    py -3.11 roteirizacao/avisar_motoristas_rotas.py --modo-teste --data amanhã
    py -3.11 roteirizacao/avisar_motoristas_rotas.py --data amanhã --enviar-emails --gerar-confirmacoes
"""
import argparse
import logging
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "avisar_motoristas_rotas.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("avisar_motoristas_rotas")

import requests
import yaml
from itsdangerous import URLSafeTimedSerializer

from email_utils import COR_ACENTO, enviar_email, envelope_html
from regras import confirmacao_rotas
from regras.preferencias_motoristas import CatalogoMotoristas, MotoristaPreferencias
from rotas_client import listar_rotas
from regioes_dia_fixo import extrair_cidade
from alocacao_motoristas import classificar_rota_viagem
from zonas_sp import classificar_rota_zona
import integracao_evolution

ARQUIVO_SAIDA = _RAIZ_LOCAL / "dados" / "mensagens_whatsapp_amanha.txt"
ARQUIVO_SAIDA_OFERTAS = _RAIZ_LOCAL / "dados" / "mensagens_whatsapp_ofertas.txt"
TZ_BRASILIA = timezone(timedelta(hours=-3))

DIAS_SEMANA_PT = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira",
                  "Sexta-feira", "Sábado", "Domingo"]


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _normalizar_texto(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).upper().strip()


def _parse_data(valor: str) -> date:
    """'amanhã'/'amanha' (padrão) ou 'hoje' -- relativos ao dia atual em
    horário de Brasília; senão aceita DD/MM/AAAA ou AAAA-MM-DD."""
    hoje = datetime.now(TZ_BRASILIA).date()
    valor_norm = _normalizar_texto(valor)
    if valor_norm in ("AMANHA", ""):
        return hoje + timedelta(days=1)
    if valor_norm == "HOJE":
        return hoje

    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(valor.strip(), formato).date()
        except ValueError:
            continue
    raise ValueError(f"Data não reconhecida: {valor!r} (use 'amanhã', 'hoje' ou DD/MM/AAAA).")


def _extrair_servicos_da_rota(rota: dict) -> list[dict]:
    """Mesmo formato confirmado em incrementar_rotas.py: 'services' vem
    embrulhado como {'data': [...]}."""
    servicos_wrapper = rota.get("services")
    if isinstance(servicos_wrapper, dict):
        return servicos_wrapper.get("data", []) or []
    if isinstance(servicos_wrapper, list):
        return servicos_wrapper
    return []


def _zona_da_rota(sublote: list[dict], api_key: str | None) -> str:
    """Região/zona representativa da rota para exibição na mensagem:
    para Viagem, lista as cidades fora da Grande SP (ex: 'CAMPINAS /
    VIAGEM'); dentro da Grande SP, usa a zona predominante (zonas_sp.py).
    Dedup de cidade por forma normalizada (maiúsculo/sem acento) -- o
    endereço bruto do VUUPT traz a mesma cidade grafada de formas
    diferentes entre pedidos ('São José dos Campos' vs 'SAO JOSE DOS
    CAMPOS'), o que duplicava a listagem sem essa normalização."""
    if classificar_rota_viagem(sublote, api_key):
        cidades_por_chave = {}
        for s in sublote:
            cidade = extrair_cidade(s)
            if cidade:
                cidades_por_chave.setdefault(_normalizar_texto(cidade), cidade)
        cidades = sorted(cidades_por_chave.values(), key=str.upper)
        return (", ".join(cidades) if cidades else "Fora da Grande SP") + " / VIAGEM"
    zona = classificar_rota_zona(sublote, api_key)
    return zona or "Zona não identificada"


def buscar_rotas_do_dia(token: str, data_alvo: date) -> list[dict]:
    """Lista as rotas (com serviços) cujo start_at cai no dia informado,
    excluindo rotas com status='canceled' (mesmo padrão de
    incrementar_rotas.py) -- essas não representam entregas de verdade."""
    inicio = data_alvo.strftime("%Y-%m-%d") + " 00:00:00"
    fim = (data_alvo + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    filtro = [
        {"field": "start_at", "operator": "gte", "value": inicio},
        {"field": "start_at", "operator": "lt", "value": fim},
    ]
    rotas = listar_rotas(token, include=["services"], filtro=filtro)
    return [r for r in rotas if r.get("status") != "canceled"]


def agrupar_por_motorista(rotas: list[dict], api_key: str | None) -> dict:
    """
    Agrupa as rotas do dia por agent_id, somando entregas e coletando
    zonas de todas as rotas de um mesmo motorista (um motorista pode ter
    mais de 1 rota no dia, embora MAX_ROTAS_DIA normalmente limite a 1).
    Rota sem agent_id (motorista ainda não atribuído manualmente) é
    ignorada -- não há para quem avisar.
    """
    por_motorista = defaultdict(lambda: {"rotas": 0, "entregas": 0, "zonas": [], "inicio": None,
                                         "rota_id_referencia": None})
    for rota in rotas:
        agent_id = rota.get("agent_id")
        if agent_id is None:
            continue
        servicos = _extrair_servicos_da_rota(rota)
        if not servicos:
            continue

        info = por_motorista[agent_id]
        info["rotas"] += 1
        info["entregas"] += len(servicos)
        zona = _zona_da_rota(servicos, api_key)
        if zona not in info["zonas"]:
            info["zonas"].append(zona)

        start_at = rota.get("start_at")
        if start_at and (info["inicio"] is None or start_at < info["inicio"]):
            info["inicio"] = start_at
            # rota de referência pra confirmação = a de início mais cedo no
            # dia (motorista quase sempre tem só 1, MAX_ROTAS_DIA normalmente
            # limita a isso -- ver regras/preferencias_motoristas.py)
            info["rota_id_referencia"] = rota.get("id")

    return dict(por_motorista)


def _formatar_horario(start_at: str | None) -> str:
    if not start_at:
        return "a confirmar"
    match = re.search(r"(\d{2}):(\d{2})", start_at)
    return f"{match.group(1)}:{match.group(2)}" if match else "a confirmar"


def _mensagem_individual(nome: str, info: dict, data_alvo_br: str, link_confirmacao: str | None = None) -> str:
    linha_confirmacao = f"✅ *Confirme sua participação:* {link_confirmacao}\n\n" if link_confirmacao else ""
    pedido_final = (
        "Por gentileza, confirme sua participação pelo link acima!" if link_confirmacao
        else "Por gentileza, responda a esta mensagem confirmando o recebimento!"
    )
    return (
        "🚚 *AVISO DE ROTA - FRESHLOG*\n"
        f"Olá, *{nome}*!\n"
        f"Você possui *{info['rotas']}* rota(s) alocada(s) para amanhã (*{data_alvo_br}*).\n\n"
        f"📍 *Região/Zona:* {', '.join(info['zonas'])}\n"
        f"⏱️ *Previsão Primeira Parada:* {_formatar_horario(info['inicio'])}\n"
        f"📦 *Total de Clientes:* {info['entregas']} entregas\n"
        "🔗 *Acesse o app da VUUPT para visualizar o roteiro completo.*\n\n"
        f"{linha_confirmacao}"
        f"{pedido_final}"
    )


def preparar_confirmacoes(por_motorista: dict, catalogo: CatalogoMotoristas, data_alvo: date,
                          config_confirmacao: dict) -> dict[int, str]:
    """Gera (ou reaproveita) o token de confirmação de cada motorista do
    dia e grava/atualiza a linha correspondente em
    regras/confirmacao_rotas.py. Retorna {agent_id: link_completo} --
    motorista sem cadastro no catálogo (agent_id não encontrado) ou sem
    rota_id_referencia (sem serviço/horário válido) não recebe link.
    """
    secret = config_confirmacao.get("token_secret")
    url_base = (config_confirmacao.get("url_base") or "").rstrip("/")
    if not secret or not url_base:
        logger.warning(
            "confirmacao_rotas.token_secret/url_base não configurados em config.yaml -- "
            "avisos sairão sem link de confirmação."
        )
        return {}

    serializer = URLSafeTimedSerializer(secret, salt="confirmacao-rota")
    motoristas_por_id = {m.agent_id: m for m in catalogo.motoristas}
    links = {}
    for agent_id, info in por_motorista.items():
        motorista = motoristas_por_id.get(agent_id)
        rota_id = info.get("rota_id_referencia")
        if motorista is None or rota_id is None:
            continue
        token = serializer.dumps({"route_id": rota_id, "agent_id": agent_id})
        digitos_telefone = re.sub(r"\D", "", motorista.telefone or "")
        confirmacao_rotas.criar_ou_atualizar(
            vuupt_route_id=rota_id, agent_id=agent_id, data_rota=data_alvo, token=token,
            nome_motorista=motorista.nome, zona=", ".join(info["zonas"]),
            horario_previsto=_formatar_horario(info["inicio"]), qtd_entregas=info["entregas"],
            telefone_ultimos4=digitos_telefone[-4:] if len(digitos_telefone) >= 4 else None,
        )
        links[agent_id] = f"{url_base}/r/{token}"
    return links


def push_confirmacoes_vps(config_confirmacao: dict) -> dict:
    """Empurra pra VPS as confirmações locais ainda não sincronizadas
    (novas ou reenviadas). Não levanta exceção em falha de rede -- só
    loga e deixa pendente pro próximo run (o pull de respostas roda
    solto, então uma falha de push aqui não trava mais nada)."""
    url_base = (config_confirmacao.get("url_base") or "").rstrip("/")
    sync_secret = config_confirmacao.get("sync_secret")
    pendentes = confirmacao_rotas.listar_pendentes_de_envio()
    if not pendentes:
        return {"enviadas": 0, "falha": False}
    if not url_base or not sync_secret:
        logger.warning(f"{len(pendentes)} confirmação(ões) pendente(s) de push, mas VPS não configurada.")
        return {"enviadas": 0, "falha": True}

    try:
        resp = requests.post(
            f"{url_base}/api/sync/upsert",
            json={"confirmacoes": pendentes},
            headers={"X-Sync-Secret": sync_secret},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Falha ao empurrar confirmações pra VPS ({len(pendentes)} pendente(s)): {exc}")
        return {"enviadas": 0, "falha": True}

    confirmacao_rotas.marcar_sincronizadas([linha["id"] for linha in pendentes])
    logger.info(f"{len(pendentes)} confirmação(ões) sincronizada(s) com a VPS.")
    return {"enviadas": len(pendentes), "falha": False}


def preparar_link_oferta(agent_id: int, data_alvo: date, config_confirmacao: dict) -> str | None:
    """Link assinado da página de escolha (confirmacao_motoristas/app.py,
    rota /escolher/<token>) pro motorista ver/pegar as rotas ABERTA do
    marketplace naquele dia -- token por MOTORISTA+DIA (não por rota,
    ao contrário de preparar_confirmacoes), já que a página lista tudo
    que ele é elegível a escolher no momento em que abre, não uma rota
    fixa gravada no token. Mesmo secret/url_base de confirmacao_rotas
    (config.yaml), salt próprio pra não misturar com os tokens da
    confirmação de rota já enviada."""
    secret = config_confirmacao.get("token_secret")
    url_base = (config_confirmacao.get("url_base") or "").rstrip("/")
    if not secret or not url_base:
        return None
    serializer = URLSafeTimedSerializer(secret, salt="escolha-rota")
    token = serializer.dumps({"agent_id": agent_id, "data_rota": data_alvo.isoformat()})
    return f"{url_base}/escolher/{token}"


def _mensagem_oferta(nome: str, link_escolha: str) -> str:
    return (
        "🚚 *ROTA DISPONÍVEL - FRESHLOG*\n"
        f"Olá, *{nome}*!\n"
        "Tem rota disponível pra você escolher -- veja o resumo (região, paradas, caixas) "
        "e pegue a que preferir:\n\n"
        f"👉 {link_escolha}\n\n"
        "Só o primeiro que escolher leva -- se demorar, pode já não estar mais disponível."
    )


def notificar_oferta_motoristas(elegiveis: list[MotoristaPreferencias], data_alvo: date, config: dict) -> dict:
    """
    Avisa cada motorista elegível de que há rota(s) publicada(s) pra
    escolha (Hugo, 22/08: botão "Publicar para motoristas" do
    planejamento) -- chamado pelo painel depois que
    planejamento_rotas.publicar_oferta_rascunho/publicar_ofertas_em_lote
    já gravaram a(s) oferta(s).

    Ordem de tentativa por motorista: WhatsApp via Evolution API
    (automático de verdade, se configurado e o telefone existir -- ver
    integracao_evolution.py) + e-mail automático (se EMAIL_MOTORISTA
    existir) -- e SEMPRE grava o texto pronto pra copiar/colar em
    ARQUIVO_SAIDA_OFERTAS, mesmo quando os automáticos deram certo
    (mesmo padrão de main(): o arquivo é sempre a cópia completa/
    auditável de tudo que devia ter sido avisado).

    Retorna {"whatsapp_evolution": N, "email": N, "sem_contato": N,
    "arquivo": N} -- N sempre <= len(elegiveis) (um motorista pode
    contar em mais de uma categoria, ex.: Evolution E e-mail).
    """
    config_confirmacao = config.get("confirmacao_rotas", {})
    config_email = config.get("email", {})
    config_evolution = config.get("evolution_api", {})

    blocos = []
    contagem = {"whatsapp_evolution": 0, "email": 0, "sem_contato": 0}
    for motorista in elegiveis:
        link = preparar_link_oferta(motorista.agent_id, data_alvo, config_confirmacao)
        if not link:
            logger.warning(
                "confirmacao_rotas.token_secret/url_base não configurados -- "
                "oferta de rota sairá sem link de escolha."
            )
            continue
        mensagem = _mensagem_oferta(motorista.nome, link)
        blocos.append(
            f"--- {motorista.nome} ({motorista.telefone or 'sem telefone cadastrado'}) ---\n{mensagem}"
        )

        teve_contato = False
        if motorista.telefone and integracao_evolution.enviar_whatsapp_oferta(
                config_evolution, motorista.telefone, motorista.nome, link):
            contagem["whatsapp_evolution"] += 1
            teve_contato = True
        if motorista.email:
            corpo_html = "<p style='white-space:pre-line;font-size:14px;line-height:1.6;'>" \
                        + mensagem.replace("*", "").replace("\n", "<br>") + "</p>"
            corpo = envelope_html(corpo_html, rodape="Mensagem automática — Agente Stokki Eventos.")
            if enviar_email([motorista.email], "[Freshlog] Rota disponível para escolha", corpo, config_email):
                contagem["email"] += 1
                teve_contato = True
        if not teve_contato:
            contagem["sem_contato"] += 1

    if blocos:
        data_hora = datetime.now(TZ_BRASILIA).strftime("%d/%m/%Y %H:%M")
        texto = (
            f"OFERTA DE ROTA PUBLICADA -- {data_hora}\n{'=' * 60}\n\n"
            + "\n\n".join(blocos) + "\n"
        )
        with open(ARQUIVO_SAIDA_OFERTAS, "a", encoding="utf-8") as f:
            f.write(texto + "\n")

    return {**contagem, "arquivo": len(blocos)}


def push_ofertas_vps(config_confirmacao: dict) -> dict:
    """Empurra pra VPS as ofertas do marketplace ainda não sincronizadas
    (publicadas ou despublicadas desde o último push) -- mesmo padrão de
    push_confirmacoes_vps, endpoint irmão na VPS
    (confirmacao_motoristas/app.py::api_sync_ofertas_upsert). Chamado
    SÍNCRONO no momento da publicação (painel_agentes.py), antes de
    avisar o motorista -- o link só funciona depois que a VPS já
    conhece a oferta, então nunca faz sentido avisar antes de empurrar."""
    from regras import ofertas_rota

    url_base = (config_confirmacao.get("url_base") or "").rstrip("/")
    sync_secret = config_confirmacao.get("sync_secret")
    pendentes = ofertas_rota.listar_pendentes_de_envio()
    if not pendentes:
        return {"enviadas": 0, "falha": False}
    if not url_base or not sync_secret:
        logger.warning(f"{len(pendentes)} oferta(s) pendente(s) de push, mas VPS não configurada.")
        return {"enviadas": 0, "falha": True}

    try:
        resp = requests.post(
            f"{url_base}/api/sync/ofertas/upsert",
            json={"ofertas": pendentes},
            headers={"X-Sync-Secret": sync_secret},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Falha ao empurrar ofertas pra VPS ({len(pendentes)} pendente(s)): {exc}")
        return {"enviadas": 0, "falha": True}

    ofertas_rota.marcar_sincronizadas([linha["id"] for linha in pendentes])
    logger.info(f"{len(pendentes)} oferta(s) sincronizada(s) com a VPS.")
    return {"enviadas": len(pendentes), "falha": False}


def _mensagem_grupo(data_alvo: date, motoristas_info: list[tuple[str, dict]]) -> str:
    dia_semana = DIAS_SEMANA_PT[data_alvo.weekday()]
    linhas = "\n".join(
        f"✅ {nome} - {', '.join(info['zonas'])} ({info['entregas']} entregas)"
        for nome, info in motoristas_info
    )
    return (
        f"📋 *ESCALA DE ROTAS - {dia_semana.upper()} ({data_alvo.strftime('%d/%m/%Y')})*\n\n"
        f"{linhas}\n\n"
        "⚠️ Motoristas escalados, por favor confirmem o ciente no privado!"
    )


def gerar_mensagens(por_motorista: dict, catalogo: CatalogoMotoristas, data_alvo: date,
                    links: dict[int, str] | None = None) -> tuple[str, list[tuple[MotoristaPreferencias | None, dict, str, str | None]]]:
    """
    Monta o texto individual de cada motorista + a mensagem de grupo.
    `links` é opcional -- {agent_id: link_de_confirmacao} gerado por
    preparar_confirmacoes(); sem ele, as mensagens saem no formato
    antigo (sem link). Retorna (texto_completo_do_arquivo, lista de
    (motorista_ou_None, info, mensagem_individual, link_ou_None)) --
    essa lista é reaproveitada por enviar_emails() sem duplicar a
    montagem das mensagens.
    """
    motoristas_por_id = {m.agent_id: m for m in catalogo.motoristas}
    data_alvo_br = data_alvo.strftime("%d/%m/%Y")

    linhas = []
    for agent_id, info in por_motorista.items():
        motorista = motoristas_por_id.get(agent_id)
        nome = motorista.nome if motorista else f"Motorista {agent_id}"
        link = (links or {}).get(agent_id)
        mensagem = _mensagem_individual(nome, info, data_alvo_br, link)
        linhas.append((nome, motorista, info, mensagem, link))

    linhas.sort(key=lambda linha: linha[0])

    blocos_individuais = "\n\n".join(
        f"--- {nome} ({motorista.telefone if motorista and motorista.telefone else 'sem telefone cadastrado'}) ---\n{mensagem}"
        for nome, motorista, _info, mensagem, _link in linhas
    )

    motoristas_info_grupo = [(nome, info) for nome, _motorista, info, _mensagem, _link in linhas]
    mensagem_grupo = _mensagem_grupo(data_alvo, motoristas_info_grupo)

    itens = [(motorista, info, mensagem, link) for _nome, motorista, info, mensagem, link in linhas]

    texto_completo = (
        f"MENSAGENS DE AVISO DE ROTA -- {data_alvo_br}\n"
        f"{'=' * 60}\n\n"
        "MENSAGENS INDIVIDUAIS (copiar/colar por motorista)\n"
        f"{'-' * 60}\n\n"
        f"{blocos_individuais}\n\n"
        f"{'=' * 60}\n\n"
        "MENSAGEM DE GRUPO (escala geral)\n"
        f"{'-' * 60}\n\n"
        f"{mensagem_grupo}\n"
    )
    return texto_completo, itens


def enviar_emails(itens: list[tuple[MotoristaPreferencias | None, dict, str, str | None]],
                  data_alvo: date, config_email: dict) -> dict:
    """Envia 1 e-mail por motorista com EMAIL_MOTORISTA cadastrado, com
    a mesma mensagem individual gerada para o WhatsApp (convertida para
    HTML simples) + um botão de confirmação quando houver link. Retorna
    contagem de enviados/falhas/sem_email."""
    data_alvo_br = data_alvo.strftime("%d/%m/%Y")
    enviados = falhas = sem_email = 0

    for motorista, _info, mensagem, link in itens:
        if not motorista or not motorista.email:
            sem_email += 1
            continue

        botao_html = ""
        if link:
            botao_html = (
                "<p style='margin-top:20px;'>"
                f"<a href='{link}' style='background:{COR_ACENTO};color:#FFFFFF;padding:12px 24px;"
                "border-radius:6px;text-decoration:none;font-weight:bold;display:inline-block;'>"
                "Confirmar participação</a></p>"
            )
        corpo_html = "<p style='white-space:pre-line;font-size:14px;line-height:1.6;'>" \
                    + mensagem.replace("*", "").replace("\n", "<br>") + "</p>" + botao_html
        corpo = envelope_html(corpo_html, rodape="Mensagem automática — Agente Stokki Eventos.")
        assunto = f"[Freshlog] Aviso de rota — {data_alvo_br}"

        if enviar_email([motorista.email], assunto, corpo, config_email):
            enviados += 1
        else:
            falhas += 1

    return {"enviados": enviados, "falhas": falhas, "sem_email": sem_email}


def main(modo_teste: bool, data_str: str, enviar_emails_flag: bool, gerar_confirmacoes_flag: bool):
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    cfg_motoristas = config.get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(
        cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""),
    )

    data_alvo = _parse_data(data_str)
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Aviso de rotas para {data_alvo.strftime('%d/%m/%Y')}.")

    rotas = buscar_rotas_do_dia(token, data_alvo)
    logger.info(f"{len(rotas)} rota(s) encontrada(s) para {data_alvo.strftime('%d/%m/%Y')}.")

    por_motorista = agrupar_por_motorista(rotas, gmaps_key)
    rotas_sem_agente = sum(1 for r in rotas if r.get("agent_id") is None)
    if rotas_sem_agente:
        logger.warning(f"{rotas_sem_agente} rota(s) sem motorista atribuído -- não geram aviso.")

    if not por_motorista:
        logger.info("Nenhum motorista com rota alocada para essa data -- nada a avisar.")
        return

    links = {}
    if gerar_confirmacoes_flag:
        links = preparar_confirmacoes(por_motorista, catalogo, data_alvo, config.get("confirmacao_rotas", {}))
        logger.info(f"{len(links)} link(s) de confirmação gerado(s)/atualizado(s).")

    texto_completo, itens = gerar_mensagens(por_motorista, catalogo, data_alvo, links)
    ARQUIVO_SAIDA.write_text(texto_completo, encoding="utf-8")
    logger.info(f"{len(itens)} motorista(s) com aviso gerado -- mensagens salvas em {ARQUIVO_SAIDA}.")

    resultado_email = {"enviados": 0, "falhas": 0, "sem_email": 0}
    if enviar_emails_flag and not modo_teste:
        resultado_email = enviar_emails(itens, data_alvo, config.get("email", {}))
        logger.info(f"E-mails: {resultado_email}.")
    elif enviar_emails_flag and modo_teste:
        logger.info("[MODO TESTE] --enviar-emails ignorado (nenhum e-mail real é enviado em modo teste).")

    resultado_push = {"enviadas": 0, "falha": False}
    if gerar_confirmacoes_flag and not modo_teste:
        resultado_push = push_confirmacoes_vps(config.get("confirmacao_rotas", {}))
    elif gerar_confirmacoes_flag and modo_teste:
        logger.info("[MODO TESTE] --gerar-confirmacoes não empurra pra VPS (só grava local, pra conferência).")

    total_entregas = sum(info["entregas"] for info in por_motorista.values())
    logger.info(
        f"Resumo: {len(itens)} motorista(s), {total_entregas} entrega(s) no total"
        + (f", e-mails: {resultado_email['enviados']} enviado(s)/{resultado_email['falhas']} falha(s)/"
           f"{resultado_email['sem_email']} sem e-mail cadastrado" if enviar_emails_flag and not modo_teste else "")
        + (f", confirmações sincronizadas com a VPS: {resultado_push['enviadas']}"
           if gerar_confirmacoes_flag and not modo_teste else "")
        + "."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gera avisos de rota (WhatsApp + e-mail) para os motoristas escalados numa data")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Gera as mensagens e loga o resumo, sem enviar e-mail real nem empurrar pra VPS")
    parser.add_argument("--data", default="amanhã",
                        help="Data alvo: 'amanhã' (padrão), 'hoje' ou DD/MM/AAAA")
    parser.add_argument("--enviar-emails", action="store_true",
                        help="Envia e-mail de aviso para motoristas com EMAIL_MOTORISTA cadastrado")
    parser.add_argument("--gerar-confirmacoes", action="store_true",
                        help="Gera link de confirmação por motorista (WhatsApp + e-mail) e sincroniza com a VPS")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste, data_str=args.data, enviar_emails_flag=args.enviar_emails,
         gerar_confirmacoes_flag=args.gerar_confirmacoes)
