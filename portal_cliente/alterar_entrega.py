# -*- coding: utf-8 -*-
"""
Regra pura da alteracao de entrega pelo portal (endereco, data e horario de
recebimento): quem pode alterar, o que validar e que avisos mostrar.
As regras (avaliar/validar/planejar) nao falam com Vuupt/Stokki; quem fala e
aplicar, so por Dependencias (dependencias_reais em producao, falsas nos testes).

Desligado por padrao: portal_cliente.alteracao_entrega.ativo (com
piloto_cnpjs, so esses CNPJs enxergam).
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

_RAIZ = Path(__file__).resolve().parent.parent
for _p in (_RAIZ, Path(__file__).resolve().parent, _RAIZ / "roteirizacao"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import endereco_entrega  # noqa: E402
from cancelamento import _PROVEDOR_ROTA_COMECOU, _ROTA_COMECOU  # noqa: E402
from nucleo.normalizacao import vuupt_para_local  # noqa: E402
from retiradas.regras_retirada import eh_servico_retirada  # noqa: E402

CHAT = "Fale com a Fresh Log pelo chat do portal."
logger = logging.getLogger("portal_alterar_entrega")
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


# ── Aplicação ──────────────────────────────────────────────────────────────

@dataclass
class Dependencias:
    """Cada callable devolve {"ok": bool, "erro": str, ...} (geocodificar devolve
    (lat, lng) ou None; avisar_equipe nao devolve nada)."""
    tirar_da_rota: Callable[[int], dict]
    gravar_endereco: Callable[[int, str, dict], dict]
    reagendar: Callable[[int, str, str, str], dict]
    atualizar_horario_contato: Callable[[int, str, str], dict]
    geocodificar: Callable[[str], tuple | None]
    avisar_equipe: Callable[[dict], None]


class _Falha(Exception):
    pass


def _exige(resultado: dict, rotulo: str) -> dict:
    if not (resultado or {}).get("ok"):
        raise _Falha((resultado or {}).get("erro") or f"{rotulo} não respondeu")
    return resultado


def _br(dia: date) -> str:
    return dia.strftime("%d/%m/%Y")


def _janela_da_data(horario_novo, doc: str, mapa_horarios: dict) -> tuple[str, str]:
    """Horario novo (se veio junto) ou o de recebimento do destinatario;
    o padrao 00:00-23:59 (sem restricao) vira 08:00-18:00."""
    from regras.complexidade_entrega import carregar_ajustes_manuais, horario_efetivo
    ini, fim = horario_novo or horario_efetivo(doc, mapa_horarios, carregar_ajustes_manuais())
    return ("08:00", "18:00") if (ini, fim) == ("00:00", "23:59") else (ini, fim)


def _registrar_data(conn, servico: dict, dia: date, ini: str, fim: str, plano: Plano, contexto: dict, codigos: list) -> None:
    """Data do cliente vai pra agendamentos_pedido (ja marcada como aplicada: o
    PUT na Vuupt acabou de acontecer); data recalculada pelo dia fixo, pra origem."""
    try:
        _gravar_data_local(conn, servico, dia, ini, fim, plano, contexto, codigos)
    except Exception as e:  # noqa: BLE001 -- a Vuupt ja mudou; falha local so vira aviso no log
        logger.warning(f"data do servico {servico.get('id')} gravada na Vuupt, mas o registro local falhou: {e}")


def _gravar_data_local(conn, servico, dia, ini, fim, plano, contexto, codigos) -> None:
    if plano.origem_data == "DIA_FIXO":
        import registro_dia_fixo
        registro_dia_fixo.registrar_origem(conn, servico, dia, registro_dia_fixo.ORIGEM_DIA_FIXO)
        return
    import envio_pedidos
    for codigo in codigos:
        if not envio_pedidos.registrar_agendamento_pedido(conn, {
                "codigo_pedido": codigo, "agendamento_data": dia.isoformat(),
                "agendamento_hora_inicio": ini, "agendamento_hora_fim": fim,
                "destinatario_doc": contexto.get("destinatario_doc"),
                "destinatario_nome": contexto.get("destinatario_nome"),
                "cnpj_embarcador": contexto["cnpj_embarcador"], "numero_nf": None}, contexto["por"]):
            logger.warning(f"data do cliente de {codigo} nao ficou em agendamentos_pedido")
            continue
        conn.execute("UPDATE agendamentos_pedido SET aplicado_vuupt_em = ? WHERE pedido = ?",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), codigo))
    conn.commit()


def _gravar_horario(conn, servico: dict, horario: tuple, contexto: dict, deps: Dependencias) -> None:
    """Horario de recebimento do destinatario (portal + ajuste manual global) e
    contato da Vuupt. Nunca mexe em scheduled_*. Preserva o requer_agendamento."""
    import envio_pedidos
    doc = _digitos(contexto.get("destinatario_doc"))
    if not doc:
        raise _Falha("destinatário sem documento")
    cnpj = _digitos(contexto["cnpj_embarcador"])
    linha = conn.execute("SELECT requer_agendamento FROM portal_destinatarios WHERE cnpj_embarcador = ? AND documento = ?",
                         (cnpj, doc)).fetchone()
    try:
        envio_pedidos.gravar_destinatario(conn, cnpj, doc, contexto.get("destinatario_nome") or "",
                                          horario[0], horario[1], bool(linha and linha[0]), contexto.get("config"))
    except envio_pedidos.ErroEnvio as e:
        raise _Falha(str(e)) from e
    customer_id = servico.get("customer_id")
    if customer_id:
        r = deps.atualizar_horario_contato(int(customer_id), horario[0], horario[1])
        if not r.get("ok"):
            logger.warning(f"contato {customer_id} da Vuupt ficou sem o horario novo: {r.get('erro')}")


def aplicar(conn, servico: dict, validado: dict, plano: Plano, contexto: dict, deps: Dependencias) -> dict:
    """Aplica na Vuupt e nas tabelas locais, parando no primeiro erro. conn e a
    conexao do dados.db (agendamentos_pedido, portal_destinatarios,
    agendamentos_origem e o registro das alteracoes). contexto: cnpj_embarcador,
    sender_id, por, destinatario_doc, destinatario_nome (+ config e
    mapa_horarios, opcionais). Devolve {"ok", "mensagem", "feito"}."""
    from regras import alteracoes_entrega as registro
    from regras.complexidade_entrega import carregar_ajustes_manuais, horario_efetivo
    from pedidos_dedicados import codigos_do_servico
    registro.garantir_tabela(conn)
    contexto = dict(contexto, cnpj_embarcador=_digitos(contexto["cnpj_embarcador"]))
    sid = int(servico["id"])
    codigos = codigos_do_servico(servico)
    codigo = codigos[0] if codigos else ""
    doc = _digitos(contexto.get("destinatario_doc"))
    mapa = contexto.get("mapa_horarios") or {}
    if validado.get("horario") and not doc:
        return {"ok": False, "feito": [], "mensagem":
                f"Não foi possível identificar o destinatário para alterar o horário. {CHAT}"}
    antes: dict = {}
    depois: dict = {}
    feito: list[str] = []
    rota_id = lat = lng = None
    nova_data = validado.get("data") or plano.data_final
    try:
        data_antes = _data_agendada(servico)
        antes = {"endereco": servico.get("address"), "data": data_antes.isoformat() if data_antes else None,
                 "horario": list(horario_efetivo(doc, mapa, carregar_ajustes_manuais()))}
        if plano.tirar_da_rota:
            r = _exige(deps.tirar_da_rota(sid), "Vuupt")
            rota_id = r.get("rota_id")
            feito.append("pedido tirado da rota" if rota_id else "pedido fora de rota confirmado")
        if plano.endereco_str:
            dados = {"address": plano.endereco_str}
            try:
                geo = deps.geocodificar(plano.endereco_str)
            except Exception as e:  # noqa: BLE001 -- sem coordenada a Vuupt geocodifica sozinha
                logger.warning(f"geocodificacao falhou ({e}); a Vuupt geocodifica")
                geo = None
            if geo:
                lat, lng = geo
                dados.update(latitude=lat, longitude=lng)
            _exige(deps.gravar_endereco(sid, plano.endereco_str, dados), "Vuupt")
            depois["endereco"] = plano.endereco_str
            feito.append("endereço gravado")
        if nova_data:
            ini, fim = _janela_da_data(validado.get("horario"), doc, mapa)
            _exige(deps.reagendar(sid, nova_data.isoformat(), ini, fim), "Vuupt")
            depois["data"], depois["janela_data"] = nova_data.isoformat(), [ini, fim]
            feito.append("data gravada")
            _registrar_data(conn, servico, nova_data, ini, fim, plano, contexto, codigos)
        if validado.get("horario"):
            _gravar_horario(conn, servico, validado["horario"], contexto, deps)
            depois["horario"] = list(validado["horario"])
            feito.append("horário de recebimento gravado")
    except Exception as e:  # noqa: BLE001 -- qualquer falha para aqui e fica registrada
        if isinstance(e, _Falha):
            erro = cliente = str(e) or "falha sem detalhe"
            logger.error(f"alteracao do servico {sid} parou: {erro} (feito: {feito})")
        else:  # detalhe tecnico fica no log e no registro, nunca na mensagem do cliente
            erro = f"{e.__class__.__name__}: {e}"
            cliente = "erro inesperado"
            logger.exception(f"alteracao do servico {sid} parou com erro inesperado (feito: {feito})")
        _registrar_seguro(registro, conn, servico, contexto, codigo, antes, dict(depois, feito=feito),
                          validado.get("endereco"), lat, lng, rota_id, "FALHOU", erro)
        if rota_id:
            _avisar(deps, servico, contexto, codigo, rota_id, feito, erro)
            return {"ok": False, "feito": feito, "mensagem":
                    f"Não conseguimos concluir a alteração ({cliente}). O pedido já saiu da rota e aguarda a "
                    "programação; a Fresh Log foi avisada."}
        return {"ok": False, "feito": feito, "mensagem":
                f"Não conseguimos concluir a alteração ({cliente}). {CHAT}"}
    partes = []
    if "endereco" in depois:
        partes.append("endereço atualizado")
    if "data" in depois:
        partes.append(f"entrega em {_br(nova_data)}")
    if "horario" in depois:
        partes.append(f"recebimento das {depois['horario'][0]} às {depois['horario'][1]}")
    mensagem = f"Alteração feita. {', '.join(partes).capitalize()}."
    if rota_id:
        mensagem += " O pedido saiu da rota e volta na próxima programação."
        _avisar(deps, servico, contexto, codigo, rota_id, feito, "")
    _registrar_seguro(registro, conn, servico, contexto, codigo, antes, dict(depois, feito=feito),
                      validado.get("endereco"), lat, lng, rota_id, "APLICADA", "")
    return {"ok": True, "mensagem": mensagem, "feito": feito}


def _registrar_seguro(registro, conn, servico, contexto, codigo, antes, depois, endereco, lat, lng, rota_id, status, erro):
    try:
        registro.registrar(conn, cnpj_embarcador=contexto["cnpj_embarcador"], sender_id=contexto.get("sender_id"),
                           service_id=int(servico["id"]), codigo=codigo, por=contexto["por"], antes=antes, depois=depois,
                           endereco=endereco, latitude=lat, longitude=lng, rota_id_tirada=rota_id, status=status, erro=erro)
    except Exception as e:  # noqa: BLE001 -- o registro nao pode esconder a falha original
        logger.error(f"nao registrei a falha da alteracao do servico {servico.get('id')}: {e}")


def _avisar(deps: Dependencias, servico: dict, contexto: dict, codigo: str, rota_id, feito: list, falha: str) -> None:
    try:
        deps.avisar_equipe({"codigo": codigo, "service_id": servico.get("id"), "rota_id": rota_id,
                            "empresa": contexto.get("empresa_nome") or contexto["cnpj_embarcador"],
                            "cnpj_embarcador": contexto["cnpj_embarcador"], "por": contexto["por"],
                            "feito": list(feito), "falha": falha})
    except Exception as e:  # noqa: BLE001 -- aviso por e-mail nao desfaz a alteracao
        logger.error(f"aviso a equipe sobre {codigo} falhou: {e}")


# ── Produção ───────────────────────────────────────────────────────────────

def dependencias_reais(config: dict, usuario: str) -> Dependencias:
    """Callables de producao. Imports dentro das funcoes: o processo web do
    portal importa este modulo e nao deve carregar painel/Vuupt/Google a toa."""
    token = (config.get("vuupt_api") or {}).get("token", "")
    agent_lalamove = int((config.get("lalamove") or {}).get("agent_id_vuupt") or 0)
    chave_google = (config.get("google_maps") or {}).get("api_key", "")

    def tirar_da_rota(service_id):
        import solicitacoes_execucao
        return solicitacoes_execucao._tirar_da_rota_real(token, service_id, agent_lalamove, usuario)

    def gravar_endereco(service_id, texto, dados):
        import solicitacoes_execucao
        solicitacoes_execucao._caminhos_painel()
        try:
            import planejamento_rotas
            from roteirizacao.cancelar_servico import rascunho_do_servico
            from vuupt_client import VuuptClient
            rascunho_id, _ = rascunho_do_servico(service_id)
            planejamento_rotas._gravar_endereco_pedido(VuuptClient(token), service_id, texto, dados, rascunho_id)
        except Exception as e:  # noqa: BLE001 -- vira {"ok": False} pro aplicar parar e registrar
            return {"ok": False, "erro": str(e)}
        return {"ok": True, "erro": ""}

    def reagendar(service_id, dia, ini, fim):
        import solicitacoes_execucao
        return solicitacoes_execucao.reagendar_servico_vuupt(token, service_id, dia, ini, fim)

    def atualizar_horario_contato(customer_id, ini, fim):
        import solicitacoes_execucao
        solicitacoes_execucao._caminhos_painel()
        try:
            from vuupt_client import VuuptClient
            VuuptClient(token).atualizar_customer(customer_id, {"operating_hour_start": ini, "operating_hour_end": fim})
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erro": str(e)}
        return {"ok": True, "erro": ""}

    def geocodificar(texto):
        import geocodificacao
        return geocodificacao.geocodificar(texto, chave_google)

    def avisar_equipe(info):
        import html
        from email_utils import enviar_email, envelope_html
        email_cfg = config.get("email") or {}
        destino = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
        if not destino:
            return
        esc = lambda v: html.escape(str(v if v is not None else ""))  # noqa: E731
        corpo = (f"<p><b>{esc(info['empresa'])}</b> alterou a entrega do pedido <b>{esc(info['codigo'])}</b> pelo portal "
                 f"({esc(info['por'])}) e ele <b>saiu da rota #{esc(info['rota_id'])}</b>.</p>"
                 f"<p>Feito: {esc('; '.join(info['feito']) or 'nada')}.</p>")
        if info["falha"]:
            corpo += (f"<p><b>A alteração não terminou:</b> {esc(info['falha'])}. O pedido está fora da rota "
                      "e precisa ser conferido na Vuupt.</p>")
        html_email = envelope_html(corpo, rodape="Fresh Log · Portal do cliente · alteração de entrega", cor_acento="#F5A623")
        enviar_email([destino], f"[Portal] Pedido saiu da rota · {info['codigo']} · {info['empresa']}", html_email, email_cfg)

    return Dependencias(tirar_da_rota=tirar_da_rota, gravar_endereco=gravar_endereco, reagendar=reagendar,
                        atualizar_horario_contato=atualizar_horario_contato, geocodificar=geocodificar,
                        avisar_equipe=avisar_equipe)
