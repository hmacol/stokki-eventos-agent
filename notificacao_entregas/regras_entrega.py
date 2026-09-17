# -*- coding: utf-8 -*-
"""
notificacao_entregas/regras_entrega.py

Regras PURAS (sem rede, sem banco) da notificação de entrega concluída:
o que fazer com cada serviço concluído da Vuupt. A tabela de estados
está em DOC_EXECUCAO_CLAUDE_NOTIFICACAO_ENTREGAS.md e é coberta linha a
linha por test_regras_entrega.py.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from retiradas.regras_retirada import eh_servico_retirada

FUSO_LOCAL = ZoneInfo("America/Sao_Paulo")
EMAIL_PADRAO_FORCADO = "hugo@freshlogbr.com"

# Ações devolvidas por decidir()
NADA = "NADA"                          # já tem estado final, não mexe
ENVIAR = "ENVIAR"
ESPERAR = "ESPERAR"                    # sucesso sem canhoto, dentro do prazo
IGNORAR = "IGNORAR"
SEM_DESTINATARIO = "SEM_DESTINATARIO"

# Estados gravados em notificacoes_entrega.estado
ESTADO_ENVIADO = "ENVIADO"
ESTADO_IGNORADO = "IGNORADO"
ESTADO_SEM_DESTINATARIO = "SEM_DESTINATARIO"
ESTADO_AGUARDANDO_CANHOTO = "AGUARDANDO_CANHOTO"
ESTADO_FALHA_ENVIO = "FALHA_ENVIO"     # SMTP falhou, tenta de novo
ESTADO_ERRO_ENVIO = "ERRO_ENVIO"       # desistiu depois de MAX_TENTATIVAS_ENVIO
ESTADOS_FINAIS = {ESTADO_ENVIADO, ESTADO_IGNORADO, ESTADO_SEM_DESTINATARIO, ESTADO_ERRO_ENVIO}

MAX_TENTATIVAS_ENVIO = 3

_PADRAO_CODIGO = re.compile(r"^PS-\d+(-R\d+)?$")
_PADRAO_CODIGO_BASE = re.compile(r"PS-\d+")


@dataclass
class ConfigEntregas:
    """Seção `notificacao_entregas` do config.yaml. Os defaults são os
    SEGUROS: desligado e com todo e-mail redirecionado pro Hugo. Envio
    real ao embarcador exige ativo=true E forcar_destino="" explícito."""
    ativo: bool = False
    forcar_destino: str = EMAIL_PADRAO_FORCADO
    embarcadores_piloto: list[int] = field(default_factory=list)
    espera_canhoto_min: int = 30
    max_atraso_horas: int = 12
    cc: list[str] = field(default_factory=list)


@dataclass
class Decisao:
    acao: str
    motivo: str = ""
    com_canhoto: bool = False


def carregar_cfg(config: dict) -> ConfigEntregas:
    sec = (config or {}).get("notificacao_entregas") or {}
    padrao = ConfigEntregas()
    forcar = sec.get("forcar_destino", padrao.forcar_destino)
    return ConfigEntregas(
        ativo=bool(sec.get("ativo", False)),
        forcar_destino=str(forcar or "").strip(),
        embarcadores_piloto=[int(s) for s in (sec.get("embarcadores_piloto") or []) if str(s).strip()],
        espera_canhoto_min=int(sec.get("espera_canhoto_min", padrao.espera_canhoto_min)),
        max_atraso_horas=int(sec.get("max_atraso_horas", padrao.max_atraso_horas)),
        cc=[str(e).strip() for e in (sec.get("cc") or []) if "@" in str(e)],
    )


# ── Helpers do serviço Vuupt ───────────────────────────────────────────────────

def parse_completed_at(valor) -> datetime | None:
    """completed_at da Vuupt vem em UTC SEM fuso ('2026-09-17 12:22:19').
    Devolve datetime com tzinfo=UTC, ou None."""
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor).strip().replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def concluido_em_local(valor) -> str:
    dt = parse_completed_at(valor)
    return dt.astimezone(FUSO_LOCAL).strftime("%d/%m/%Y %H:%M") if dt else ""


def codigo_limpo(code) -> str:
    """'#PS-36327-r2' -> 'PS-36327-R2'."""
    return str(code or "").strip().lstrip("#").strip().upper()


def codigo_base(code) -> str:
    """'#PS-36327-R2' -> 'PS-36327' (chave de documentos_processados)."""
    m = _PADRAO_CODIGO_BASE.search(codigo_limpo(code))
    return m.group(0) if m else codigo_limpo(code)


def _checklists(servico: dict) -> list[dict]:
    w = (servico or {}).get("checklistAnswers")
    lista = w.get("data") if isinstance(w, dict) else w
    return [c for c in (lista or []) if isinstance(c, dict)]


def checklist_id_com_foto(servico: dict) -> int | None:
    """Id do primeiro checklist do serviço que tem foto (o canhoto)."""
    for cl in _checklists(servico):
        try:
            if int(cl.get("images_quantity") or 0) > 0:
                return cl.get("id")
        except (TypeError, ValueError):
            continue
    return None


def volumes_reais(dimension_3, fator_ponderado) -> int | None:
    """dimension_3 da Vuupt é o volume PONDERADO (qtd real x fator do
    embarcador) -- nunca exibir direto. Devolve a quantidade real só
    quando a conta fecha num inteiro; senão None (a linha some do e-mail,
    melhor que mostrar número errado pro cliente)."""
    try:
        ponderado, fator = float(dimension_3), float(fator_ponderado)
    except (TypeError, ValueError):
        return None
    if ponderado <= 0 or fator <= 0:
        return None
    real = ponderado / fator
    return int(round(real)) if abs(real - round(real)) < 0.01 and round(real) >= 1 else None


def destinos_finais(embarcador: dict, cfg: ConfigEntregas) -> tuple[list[str], list[str]]:
    """(to, cc). Com forcar_destino, vai SÓ pra ele, sem cc."""
    if cfg.forcar_destino:
        return [cfg.forcar_destino], []
    return list(embarcador.get("emails") or []), list(cfg.cc)


# ── Decisão ────────────────────────────────────────────────────────────────────

def decidir(servico: dict, embarcador: dict | None, cfg: ConfigEntregas, agora: datetime,
            estado_atual: str | None = None) -> Decisao:
    """agora: datetime com fuso (UTC). embarcador: linha de `interno`
    ({"nome","emails","notificar_email","fator_ponderado"}) ou None."""
    if estado_atual in ESTADOS_FINAIS:
        return Decisao(NADA)

    if not _PADRAO_CODIGO.match(codigo_limpo(servico.get("code"))):
        return Decisao(IGNORAR, "codigo fora do padrao PS")
    # Retirada no galpao: fechada por API quando a Stokki marca "Enviado"
    # (acompanhar_retiradas.py). Vira o e-mail de "Retirado" (Hugo, 17/09).
    retirada = eh_servico_retirada(servico)
    if retirada and servico.get("status_done") == "failed":
        return Decisao(IGNORAR, "retirada sem sucesso")
    if not servico.get("sender_id"):
        return Decisao(IGNORAR, "sem sender_id")

    concluido = parse_completed_at(servico.get("completed_at"))
    if concluido is None:
        return Decisao(IGNORAR, "sem completed_at")

    if not cfg.ativo:
        return Decisao(IGNORAR, "desligado")
    if agora - concluido > timedelta(hours=cfg.max_atraso_horas):
        return Decisao(IGNORAR, "antigo")
    if cfg.embarcadores_piloto and int(servico["sender_id"]) not in cfg.embarcadores_piloto:
        return Decisao(IGNORAR, "fora do piloto")

    if not embarcador or not embarcador.get("emails"):
        return Decisao(SEM_DESTINATARIO, "embarcador sem e-mail cadastrado")
    if not int(embarcador.get("notificar_email", 1) or 0):
        return Decisao(SEM_DESTINATARIO, "interno.notificar_email=0 (sem modulo de preferencias)")
    if embarcador.get("desligado"):
        return Decisao(SEM_DESTINATARIO, "desligado nas preferencias do portal")

    if servico.get("status_done") == "failed":
        return Decisao(ENVIAR)
    if retirada:
        return Decisao(ENVIAR, "retirada")   # nunca tem checklist: nao espera canhoto
    if checklist_id_com_foto(servico) is not None:
        return Decisao(ENVIAR, com_canhoto=True)
    if agora - concluido < timedelta(minutes=cfg.espera_canhoto_min):
        return Decisao(ESPERAR, "aguardando canhoto")
    return Decisao(ENVIAR, "canhoto nao chegou no prazo")
