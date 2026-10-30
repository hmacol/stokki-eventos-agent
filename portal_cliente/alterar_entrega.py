# -*- coding: utf-8 -*-
"""
Regra pura da alteracao de entrega pelo portal (endereco, data e horario de
recebimento): quem pode alterar, o que validar e que avisos mostrar.
Nada aqui fala com Vuupt/Stokki; a aplicacao e outra etapa.

Desligado por padrao: portal_cliente.alteracao_entrega.ativo (com
piloto_cnpjs, so esses CNPJs enxergam).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
for _p in (_RAIZ, Path(__file__).resolve().parent, _RAIZ / "roteirizacao"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import endereco_entrega  # noqa: E402
from cancelamento import _PROVEDOR_ROTA_COMECOU, _ROTA_COMECOU  # noqa: E402
from nucleo.normalizacao import vuupt_para_local  # noqa: E402
from retiradas.regras_retirada import eh_servico_retirada  # noqa: E402

CHAT = "Fale com a Fresh Log pelo chat do portal."
TEXTO_ATRASO = ("As rotas do dia seguinte são fechadas às 16h e esse pedido já está numa rota. "
                "Para alterar, ele sai da rota e volta para a programação, e a entrega pode atrasar 1 dia.")
_STATUS_PERMITIDOS = ("not_assigned", "assigned")
_HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ErroAlteracao(ValueError):
    """Mensagem pronta pro cliente."""


def _regioes_dia_fixo():
    import regioes_dia_fixo
    return regioes_dia_fixo


def _data_agendada(servico: dict) -> date | None:
    """Dia de Brasilia do scheduled_start (a Vuupt devolve UTC sem fuso)."""
    try:
        return date.fromisoformat(str(vuupt_para_local(servico.get("scheduled_start")) or "")[:10])
    except ValueError:
        return None


def _digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def ativa_para(config: dict | None, cnpj: str) -> bool:
    cfg = (((config or {}).get("portal_cliente") or {}).get("alteracao_entrega") or {})
    if cfg.get("ativo"):
        return True
    alvo = _digitos(cnpj)
    return bool(alvo) and alvo in {_digitos(c) for c in (cfg.get("piloto_cnpjs") or [])}


@dataclass
class Avaliacao:
    pode: bool
    motivo: str = ""
    em_rota: bool = False
    rota_id: int | None = None


def _bloqueio(motivo: str) -> Avaliacao:
    return Avaliacao(False, f"{motivo} {CHAT}")


def avaliar(servico: dict, rotas_nucleo: dict, status_nucleo: str | None, agent_lalamove: int = 0) -> Avaliacao:
    """servico = GET da Vuupt; rotas_nucleo/status_nucleo vem de cancelamento.servicos_do_pedido."""
    status = servico.get("status")
    if status == "canceled":
        return _bloqueio("Esse pedido foi cancelado.")
    if eh_servico_retirada(servico):
        return _bloqueio("Retirada não pode ser alterada por aqui.")
    if status in ("on_route", "arrived", "done"):
        return _bloqueio("Esse pedido já está em entrega ou foi entregue.")
    if status not in _STATUS_PERMITIDOS:
        return _bloqueio("Esse pedido não pode ser alterado no estado em que está.")
    if status_nucleo in ("ENTREGUE", "INSUCESSO"):
        return _bloqueio("Esse pedido já foi entregue ou teve insucesso.")
    try:
        rota_id = int(servico.get("route_id") or 0) or None
    except (TypeError, ValueError):
        return _bloqueio("A rota desse pedido ainda está sendo atualizada, tente de novo em alguns minutos.")
    if rota_id:
        rota = (rotas_nucleo or {}).get(rota_id)
        if not rota:
            return _bloqueio("A rota desse pedido ainda está sendo atualizada, tente de novo em alguns minutos.")
        if (rota.get("status") in _ROTA_COMECOU or rota.get("status_provedor") in _PROVEDOR_ROTA_COMECOU
                or rota.get("iniciada_em")):
            return _bloqueio("A rota desse pedido já começou.")
        if agent_lalamove and rota.get("agent_id") == agent_lalamove:
            return _bloqueio("Esse pedido está numa rota Lalamove.")
    return Avaliacao(True, "", bool(rota_id), rota_id)


def _horario(valor, nome: str) -> str:
    texto = str(valor or "").strip()
    if not _HORA.match(texto):
        raise ErroAlteracao(f"{nome} inválido: use o formato HH:MM.")
    return texto


def validar(corpo: dict, hoje: date, consultar_cep=None) -> dict:
    """Corpo do navegador: {"endereco": {cep, numero, ...}, "data": "AAAA-MM-DD",
    "horario": {"inicio": "HH:MM", "fim": "HH:MM"}}; cada bloco e opcional."""
    corpo = corpo or {}
    endereco = data = horario = None
    for bloco in ("endereco", "horario"):
        if corpo.get(bloco) and not isinstance(corpo[bloco], dict):
            raise ErroAlteracao("Dados inválidos.")
    if corpo.get("endereco"):
        try:
            endereco = endereco_entrega.montar_endereco(corpo["endereco"], consultar_cep)
        except endereco_entrega.ErroEndereco as e:
            raise ErroAlteracao(str(e)) from e
    if corpo.get("data"):
        try:
            data = date.fromisoformat(str(corpo["data"])[:10])
        except ValueError:
            raise ErroAlteracao("Data inválida.") from None
        if data <= hoje:
            raise ErroAlteracao("A nova data precisa ser depois de hoje.")
    if corpo.get("horario"):
        h = corpo["horario"]
        inicio, fim = _horario(h.get("inicio"), "Horário inicial"), _horario(h.get("fim"), "Horário final")
        if fim <= inicio:
            raise ErroAlteracao("O horário final precisa ser depois do inicial.")
        horario = (inicio, fim)
    if not (endereco or data or horario):
        raise ErroAlteracao("Nada foi alterado.")
    return {"endereco": endereco, "data": data, "horario": horario}


def endereco_vuupt(end: dict) -> str:
    """Mesmo formato do pipeline.montar_payload_vuupt (o que extrair_cidade le)."""
    cidade_uf = " - ".join(p for p in (end.get("municipio"), end.get("uf")) if p)
    partes = [end.get("logradouro"), end.get("numero"), end.get("complemento"), end.get("bairro"),
              cidade_uf, end.get("cep"), "Brasil"]
    return ", ".join(str(p).strip() for p in partes if p and str(p).strip())


@dataclass
class Plano:
    tirar_da_rota: bool
    avisos: list = field(default_factory=list)
    data_final: date | None = None
    origem_data: str | None = None
    endereco_str: str | None = None


def _aviso_dedicado(regra: dict, rdf) -> dict:
    return {"codigo": "dedicado",
            "texto": f"Essa data não é dia de entrega na região ({rdf.descricao_dias(regra)}). "
                     "A entrega vira dedicada e é cobrada à parte."}


def planejar(servico: dict, validado: dict, avaliacao: Avaliacao, conn_registro, hoje: date) -> Plano:
    novo_end, nova_data = validado.get("endereco"), validado.get("data")
    plano = Plano(tirar_da_rota=bool(nova_data) or (avaliacao.em_rota and bool(novo_end)))
    if avaliacao.em_rota and (novo_end or nova_data):
        plano.avisos.append({"codigo": "atraso", "texto": TEXTO_ATRASO})
    if novo_end:
        plano.endereco_str = endereco_vuupt(novo_end)
    rdf = _regioes_dia_fixo()
    considerado = dict(servico, address=plano.endereco_str) if novo_end else servico
    regra = rdf.regra_dia_fixo_do_servico(considerado)
    if nova_data:
        plano.data_final, plano.origem_data = nova_data, "CLIENTE"
        if regra and not rdf.data_valida_na_regiao(regra, nova_data):
            plano.avisos.append(_aviso_dedicado(regra, rdf))
        return plano
    atual = _data_agendada(servico)
    if novo_end and atual and regra and not rdf.data_valida_na_regiao(regra, atual):
        import registro_dia_fixo
        if registro_dia_fixo.data_nao_e_do_cliente(conn_registro, servico, atual):
            plano.data_final = rdf.proxima_data_valida(regra, hoje)
            plano.origem_data = "DIA_FIXO"
        else:
            plano.avisos.append(_aviso_dedicado(regra, rdf))
    return plano
