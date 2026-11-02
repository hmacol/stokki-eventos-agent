# -*- coding: utf-8 -*-
"""
Pedido de alteracao de entrega pelo portal (endereco, data e horario de
recebimento). O cliente NAO altera nada sozinho (Hugo, 01/11): o pedido vira
um chamado na area Entrega e o atendimento trata internamente.
avaliar/validar sao regra pura; abrir_chamado grava no chat do portal.

Desligado por padrao: portal_cliente.alteracao_entrega.ativo (com
piloto_cnpjs, so esses CNPJs enxergam).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
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
_STATUS_PERMITIDOS = ("not_assigned", "assigned")
_HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ErroAlteracao(ValueError):
    """Mensagem pronta pro cliente."""


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


def _br(dia: date) -> str:
    return dia.strftime("%d/%m/%Y")


def _horario_txt(ini: str, fim: str) -> str:
    return "dia todo" if (ini[:5], fim[:5]) == ("00:00", "23:59") else f"{ini[:5]} às {fim[:5]}"


def texto_chamado(codigo: str, destinatario: str, atual: dict, validado: dict) -> str:
    """Mensagem do cliente no chamado: o que pediu e o que vale hoje.
    atual = {"endereco": str, "data": date|None, "horario": (ini, fim)}."""
    linhas = [f"Quero alterar a entrega do pedido {codigo}" + (f" ({destinatario})" if destinatario else "") + ":"]
    if validado.get("endereco"):
        linhas.append(f"- Endereço: {endereco_vuupt(validado['endereco'])} (hoje: {atual.get('endereco') or 'sem endereço'})")
    if validado.get("data"):
        hoje = _br(atual["data"]) if atual.get("data") else "sem data agendada"
        linhas.append(f"- Data da entrega: {_br(validado['data'])} (hoje: {hoje})")
    if validado.get("horario"):
        ini, fim = validado["horario"]
        linhas.append(f"- Horário de recebimento: {_horario_txt(ini, fim)} (hoje: {_horario_txt(*atual['horario'])})")
    return "\n".join(linhas)


def abrir_chamado(cliente: dict, codigo: str, texto: str, config: dict) -> int:
    """Mesmo caminho do "Deixar chamado" do chat (chamados_web): area Entrega,
    fila da equipe, e-mail/WhatsApp pro atendimento e confirmacao pro cliente.
    cliente = {cnpj do login, sender_id, nome}. Devolve o id do chamado."""
    import chamados as ch
    conn = ch.conectar()
    try:
        chamado = ch.criar_chamado(conn, cliente, origem="chamado", status=ch.STATUS_AGUARDANDO_FL,
                                   assunto=f"Alterar entrega {codigo}", area="entrega", pedido_ref=codigo)
        m = ch.adicionar_mensagem(conn, chamado, ch.ORIGEM_CLIENTE, cliente.get("nome") or "Cliente", texto)
        sit = ch.situacao_atendimento(conn, config)
        if sit["estado"] == "online":
            ch.atualizar_chamado(conn, chamado["id"], status=ch.STATUS_NA_FILA)
            ch.mensagem_sistema(conn, chamado, "Pedido de alteração registrado. A equipe está online e responde em instantes.")
        else:
            ch.mensagem_sistema(conn, chamado, f"Pedido de alteração registrado. {sit['texto']}. "
                                               f"A equipe responde aqui e no seu e-mail ({sit['horario']}).")
        chamado = ch.buscar_chamado(conn, chamado["id"])
        ch.em_segundo_plano(ch.email_para_atendimento, chamado, m, config, novo=True)
        ch.whatsapp_para_atendimento(chamado, config)
        emails = ch.emails_do_cliente(conn, cliente["cnpj"])
        if emails:
            ch.em_segundo_plano(ch.email_confirmacao_cliente, chamado, m, emails, config)
        return chamado["id"]
    finally:
        conn.close()
