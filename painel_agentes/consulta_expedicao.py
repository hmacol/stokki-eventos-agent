# -*- coding: utf-8 -*-
"""
consulta_expedicao.py

Consulta de UM pedido pra separação da rota (Hugo, 02/10): o operador
não acha o pedido no galpão e quer saber se ele deveria estar ali.
Cruza Vuupt e Stokki AO VIVO e devolve um veredito pronto.

A Vuupt manda (é o registro físico do motorista); a Stokki complementa
e desempata. Regras: docs/superpowers/specs/2026-10-02-consulta-pedido-
expedicao-design.md.

Só leitura: não usa verificar_na_stokki/verificar_na_vuupt da triagem
de Pedidos Parados porque elas GRAVAM classificação.
"""
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", _RAIZ / "insucesso_entrega"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pedidos_parados_triagem as triagem
from expedicao import _STATUS_ROTA_NAO_INICIADA, _carregar_config, _catalogo_motoristas, _rota_do_corpo
from motivos_falha import texto_do_motivo
from retiradas.regras_retirada import eh_servico_retirada
from rotas_client import buscar_rota
from stokki.auth import SessaoExpiradaError, StokkiSession

logger = logging.getLogger(__name__)

FUSO_LOCAL = ZoneInfo("America/Sao_Paulo")

TITULOS = {
    "galpao": "Deveria estar no galpão",
    "entregue": "Já foi entregue",
    "saiu": "Saiu em outra rota",
    "retirado": "Retirado",
    "cancelado": "Cancelado — não procurar",
    "nao_encontrado": "Pedido não encontrado",
}
AVISO_STOKKI_NAO_CONFERIDA = "Stokki não conferida (sessão ocupada por um agente) — tente de novo em 1 min."

# Entregue há mais que isso e ainda sem "Enviado" na Stokki = divergência
# (mesmo corte da checagem das 07h15, verificar_entregues_nao_expedidos.py).
HORAS_DIVERGENCIA_EXPEDICAO = 24

# Situações da Stokki em que o pedido ainda está fisicamente no galpão.
# Qualquer outra (fora Enviado/Cancelado) é desconhecida: vira aviso pra
# conferir, nunca "deveria estar no galpão" calado (revisão, 02/10).
_RE_STOKKI_NO_GALPAO = re.compile(r"aguardando|em espera|aberto|confer|separa|faturament|pendente", re.IGNORECASE)

_RE_CODIGO = re.compile(r"^#?(?:PS[.\-_]*)?(\d{4,6})((?:-[RC]\d+)*)$")


def normalizar_codigo(texto) -> str | None:
    """'ps 38123-r1' -> 'PS-38123-R1'; sem código reconhecível -> None."""
    compacto = re.sub(r"\s+", "", str(texto or "")).upper()
    m = _RE_CODIGO.match(compacto)
    if not m:
        return None
    return f"PS-{m.group(1)}{m.group(2)}"


def _vuupt_local(ts) -> datetime | None:
    """completed_at da Vuupt vem em UTC sem fuso -> hora de São Paulo."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(FUSO_LOCAL)


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%d/%m %H:%M") if dt else "data não informada"


def _quem(rota: dict | None) -> str:
    if not rota:
        return ""
    partes = [rota.get("motorista") or "motorista não identificado"]
    if rota.get("placa"):
        partes.append(rota["placa"])
    return " · ".join(partes)


def _bruto_stokki(stokki: dict) -> str:
    estado = stokki.get("estado")
    if estado == "ok":
        texto = f"Stokki: {stokki.get('status') or '?'}"
        if stokki.get("transportadora"):
            texto += f" · {stokki['transportadora']}"
        return texto
    if estado == "inexistente":
        return "Stokki: pedido não existe"
    return "Stokki: não conferida"


def _bruto_vuupt(servico: dict | None, rota: dict | None) -> str:
    if not servico:
        return "Vuupt: nenhum serviço com esse código"
    texto = f"Vuupt: {servico.get('status') or '?'}"
    if servico.get("status_done"):
        texto += f"/{servico['status_done']}"
    if rota:
        texto += f" · {rota.get('nome') or 'rota ' + str(rota.get('id'))}"
    return texto


def _veredito_vuupt(servico: dict, rota: dict | None, agora: datetime,
                    agente_retirada: int | None) -> tuple[str, str]:
    """(veredito, contexto) só pela Vuupt -- linhas da tabela do spec."""
    status = servico.get("status") or ""
    # Retirada no galpão (cliente/transportadora busca): pelo título
    # [RETIRADA] (critério de retiradas/regras_retirada.py) ou pelo agente
    # de retirada -- achado no teste real (PS-40637). driver_id comparado
    # como texto, como em vuupt_client.
    retirada = eh_servico_retirada(servico) or (
        agente_retirada is not None and str(servico.get("driver_id")) == str(agente_retirada))
    if retirada and status == "done" and servico.get("status_done") != "failed":
        return "retirado", f"Retirado no galpão em {_fmt(_vuupt_local(servico.get('completed_at')))}"
    if retirada and status not in ("done", "canceled"):
        return "galpao", "Separado pra retirada no galpão (cliente ou transportadora vem buscar)"
    if status == "done":
        quando = _fmt(_vuupt_local(servico.get("completed_at")))
        if servico.get("status_done") == "failed":
            motivo = texto_do_motivo(servico.get("failed_reason_id"))
            return "galpao", f"Voltou de insucesso em {quando} ({motivo})"
        quem = _quem(rota)
        return "entregue", f"Entregue em {quando}" + (f" — {quem}" if quem else "")
    if status == "canceled":
        return "cancelado", "Serviço cancelado na Vuupt"
    if status == "on_route" or (rota and rota.get("iniciada")):
        nome = rota.get("nome") if rota else "Rota em andamento"
        return "saiu", f"{nome} — {_quem(rota) or 'motorista não identificado'}"
    if rota:
        return "galpao", f"Na {rota.get('nome')} ({_quem(rota)}), rota ainda não saiu"
    data_ag = triagem._data_agendamento_local(servico.get("scheduled_start"))
    if data_ag and data_ag > agora.date():
        return "galpao", f"Sem rota, agendado pra {data_ag:%d/%m}"
    return "galpao", "Sem rota (no pool da roteirização)"


def decidir_veredito(servico: dict | None, rota: dict | None, stokki: dict, agora: datetime,
                     agente_retirada: int | None = None) -> dict:
    estado_stokki = stokki.get("estado", "nao_conferida")
    status_stokki = stokki.get("status") or ""
    cancelado_stokki = estado_stokki == "ok" and re.search(r"cancel", status_stokki, re.IGNORECASE)
    enviado_stokki = estado_stokki == "ok" and re.search(r"enviado", status_stokki, re.IGNORECASE)
    no_galpao_stokki = estado_stokki == "ok" and _RE_STOKKI_NO_GALPAO.search(status_stokki)
    desconhecida_stokki = estado_stokki == "ok" and not (cancelado_stokki or enviado_stokki or no_galpao_stokki)

    divergencia = None
    if cancelado_stokki:
        veredito, contexto = "cancelado", "Cancelado na Stokki"
    elif enviado_stokki and stokki.get("retira"):
        quando = _vuupt_local((servico or {}).get("completed_at"))
        contexto = f"Retirado no galpão ({stokki.get('transportadora')})"
        veredito, contexto = "retirado", contexto + (f" em {_fmt(quando)}" if quando else "")
    elif servico is None:
        if enviado_stokki:
            veredito, contexto = "saiu", f"Enviado pela transportadora {stokki.get('transportadora') or '?'} (sem serviço na Vuupt)"
        elif no_galpao_stokki:
            veredito, contexto = "galpao", "Ainda não foi pra roteirização (sem serviço na Vuupt)"
        elif desconhecida_stokki:
            veredito, contexto = "nao_encontrado", f"Sem serviço na Vuupt e a Stokki diz '{status_stokki}' — confira com o responsável"
        elif estado_stokki == "inexistente":
            veredito, contexto = "nao_encontrado", "Não existe na Vuupt nem na Stokki — confira o código"
        else:
            veredito, contexto = "nao_encontrado", "Não está na Vuupt e a Stokki não pôde ser conferida"
    else:
        veredito, contexto = _veredito_vuupt(servico, rota, agora, agente_retirada)
        if veredito == "galpao" and (enviado_stokki or desconhecida_stokki):
            divergencia = f"Stokki diz {status_stokki}"
        elif veredito == "entregue" and no_galpao_stokki:
            entregue_em = _vuupt_local(servico.get("completed_at"))
            if entregue_em and agora - entregue_em > timedelta(hours=HORAS_DIVERGENCIA_EXPEDICAO):
                divergencia = (f"Stokki diz {status_stokki} (entregue há mais de "
                               f"{HORAS_DIVERGENCIA_EXPEDICAO}h e não expedido)")

    return {
        "veredito": veredito,
        "titulo": TITULOS[veredito],
        "contexto": contexto,
        "divergencia": divergencia,
        "aviso": AVISO_STOKKI_NAO_CONFERIDA if estado_stokki == "nao_conferida" else None,
        "stokki_bruto": _bruto_stokki(stokki),
        "vuupt_bruto": _bruto_vuupt(servico, rota),
    }


class ConsultaIndisponivel(Exception):
    """Vuupt não respondeu: sem ela não há veredito (a Stokki sozinha não
    diz se o pedido está num caminhão)."""


def _resumo_rota(token: str, route_id: int) -> dict:
    corpo = _rota_do_corpo(buscar_rota(token, route_id, include=["agent"]))
    status = corpo.get("status") or ""
    agent_id = corpo.get("agent_id")
    motorista = placa = None
    if agent_id:
        try:
            m = _catalogo_motoristas().get(agent_id)
        except Exception as e:  # planilha fora do ar não pode derrubar a consulta
            logger.warning(f"[consulta-expedicao] catálogo de motoristas indisponível: {e}")
            m = None
        motorista = m.nome if m else f"agente {agent_id}"
        placa = m.placa if m else None
    return {"id": route_id, "nome": corpo.get("name") or f"rota {route_id}", "status": status,
            "iniciada": status not in _STATUS_ROTA_NAO_INICIADA,
            "motorista": motorista, "placa": placa}


class _SessaoSemLogin(StokkiSession):
    """Sessão Stokki que NUNCA faz login (revisão, 02/10): um login Playwright
    dentro do painel prende a thread por minutos e derruba a sessão dos
    agentes. Cookie vencido vira "Stokki não conferida"; quem renova a
    sessão são os agentes agendados."""

    def _fazer_login_playwright(self):
        raise SessaoExpiradaError("Consulta de pedido não faz login na Stokki.")


def _consultar_stokki(id_stokki: str) -> dict:
    """Mesmas travas da triagem: uma consulta por vez e nunca com agente
    rodando (login concorrente derruba a sessão dele)."""
    if not triagem._lock_consulta_stokki.acquire(blocking=False):
        return {"estado": "nao_conferida"}
    try:
        if triagem._painel_tem_execucao_rodando():
            return {"estado": "nao_conferida"}
        visto = triagem._status_e_transportadora_stokki(_SessaoSemLogin(_carregar_config()), id_stokki)
    except Exception as e:
        logger.warning(f"[consulta-expedicao] Stokki falhou pro pedido {id_stokki}: {e}")
        return {"estado": "nao_conferida"}
    finally:
        triagem._lock_consulta_stokki.release()
    if not visto:
        return {"estado": "inexistente"}
    retira = False
    if re.search(r"enviado", visto["status"], re.IGNORECASE):
        retira = triagem._tipo_retira(visto["transportadora"], triagem._catalogo_transportadoras()) is not None
    return {"estado": "ok", "status": visto["status"], "transportadora": visto["transportadora"], "retira": retira}


def consultar(codigo_digitado: str) -> dict:
    codigo = normalizar_codigo(codigo_digitado)
    if not codigo:
        raise ValueError("Código inválido. Digite PS-12345 ou só o número.")

    try:
        vuupt = triagem._vuupt()
        servico = vuupt.buscar_servico_por_code(codigo)
        if servico:
            servico = triagem._servico_mais_recente_da_cadeia(vuupt, servico)
        rota = None
        if servico and servico.get("route_id"):
            token = _carregar_config().get("vuupt_api", {}).get("token", "")
            rota = _resumo_rota(token, servico["route_id"])
    except Exception as e:
        logger.warning(f"[consulta-expedicao] Vuupt falhou pro pedido {codigo}: {e}")
        raise ConsultaIndisponivel("Vuupt indisponível — tente de novo.") from e

    id_stokki = re.search(r"\d+", codigo).group(0)
    agente_retirada = _carregar_config().get("retiradas", {}).get("agent_id")
    resultado = decidir_veredito(servico, rota, _consultar_stokki(id_stokki), datetime.now(FUSO_LOCAL),
                                 agente_retirada=int(agente_retirada) if agente_retirada else None)
    resultado["codigo"] = codigo
    return resultado
