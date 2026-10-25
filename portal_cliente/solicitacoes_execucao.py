# -*- coding: utf-8 -*-
"""
portal_cliente/solicitacoes_execucao.py

Executa de verdade, na Vuupt e na Stokki, a solicitação que o cliente fez
no portal e ficou PENDENTE pra operação -- botão "Executar" da tela
/solicitacoes-portal do painel (spec aviso-pendencias, seção 7, opção B
do Hugo). "Recusar" só fecha com resposta.

Regra de ouro: a solicitação só é concluída (concluir_solicitacao, texto
que o cliente vê) depois que TODAS as etapas do tipo deram certo. Qualquer
falha devolve {"ok": False, "mensagem"} dizendo o que já foi feito e o que
não foi -- nada é concluído.

  cancelar   Vuupt (PUT /cancel de cada serviço vivo) e depois Stokki
             (outbound/cancel) -- mesma ordem e mesmos blocos do worker
             portal_cliente/cancelamento.py. Rota Lalamove não executa
             (cancelar na Vuupt não cancela na Lalamove).
  em_espera  1º tira da rota na Vuupt (sem cancelar, como o "Excluir da
             rota" da Expedição), 2º pausa na Stokki (stokki/pausar.py).
             Ordem escolhida porque tirar da rota é o passo fácil de
             desfazer (o pedido fica not_assigned e volta na próxima
             roteirização) e é o que mais falha (rota já saiu): falhando
             ele, nada mudou. Pausar é o difícil de desfazer (outbound/open
             não implementado) e fica por último. Repetir depois de falha
             parcial é seguro: serviço sem rota não é mexido de novo e
             pedido já em espera não é pausado de novo.
  reagendar  PUT /services/{id} com a data/janela gravada no envio
             (planejamento_rotas.reagendar_pedido) e marca
             agendamento_aplicado_em, como o worker. Não usa a Stokki.

Tudo que fala com fora vem em `Dependencias` (injetável nos testes);
`dependencias_reais(config)` monta as de produção. A trava da Stokki
(stokki/sessao_uso) é pega só por cancelar/em_espera, espera até 60 s e é
sempre liberada; sem vez -> "Stokki ocupada, tente em instantes", sem
tocar na Vuupt. Monte um Dependencias novo por execução (o dono da trava
é único por instância).
"""
import logging
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cancelamento as cm  # noqa: E402
import envio_pedidos as ep  # noqa: E402

logger = logging.getLogger("portal_solicitacoes_execucao")

STOKKI_OCUPADA = "Stokki ocupada, tente em instantes."
_USAM_STOKKI = ("cancelar", "em_espera")


@dataclass
class Dependencias:
    """Cada callable devolve {"ok": bool, "erro": str} (menos buscar_servico,
    que devolve o serviço da Vuupt pelo código ou None)."""
    buscar_servico: Callable[[str], dict | None]
    cancelar_vuupt: Callable[[int], dict]
    cancelar_stokki: Callable[[int, str], dict]
    pausar_stokki: Callable[[int, str], dict]
    tirar_da_rota_vuupt: Callable[[int], dict]
    reagendar_vuupt: Callable[[int, str, str, str], dict]
    abrir_stokki: Callable[[], bool]
    fechar_stokki: Callable[[], None]
    agent_lalamove: int = 0


class _Falha(Exception):
    pass


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _solicitacao_pendente(conn: sqlite3.Connection, solicitacao_id: int) -> dict:
    r = conn.execute("SELECT * FROM portal_solicitacoes WHERE id = ?", (solicitacao_id,)).fetchone()
    if not r:
        raise _Falha(f"Solicitação {solicitacao_id} não encontrada.")
    if r["status"] != "PENDENTE":
        raise _Falha(f"Solicitação {solicitacao_id} já está {r['status']} -- nada a executar.")
    return dict(r)


def _envio(conn: sqlite3.Connection, envio_id: int) -> dict:
    r = conn.execute("SELECT * FROM portal_envios WHERE id = ?", (envio_id,)).fetchone()
    if not r:
        raise _Falha(f"Envio {envio_id} da solicitação não encontrado.")
    return dict(r)


def _codigo_e_id(envio: dict) -> tuple[str, int]:
    id_stokki = cm.id_stokki_do_codigo(envio.get("codigo_pedido"))
    if not id_stokki:
        raise _Falha("Código do pedido na Stokki (PS-...) ainda não identificado.")
    return f"PS-{id_stokki}", id_stokki


def _servicos_vivos(conn, codigo: str, deps: Dependencias, checar_lalamove: bool = True) -> list[dict]:
    """Serviços vivos do pedido (base + reentregas) pelo núcleo; sem linha
    no núcleo (até 15 min atrás), pergunta à Vuupt pelo código -- igual ao
    worker do cancelamento."""
    servicos, rotas = cm.servicos_do_pedido(conn, codigo)
    if not servicos:
        s = deps.buscar_servico(codigo)
        if s and s.get("id") and s.get("status") != "canceled":
            servicos = [{"codigo": codigo, "vuupt_service_id": s["id"], "status": "ABERTO",
                         "status_provedor": s.get("status"), "vuupt_route_id": s.get("route_id"), "excluido_em": None}]
    vivos = cm._vivos(servicos)
    for s in vivos:
        if not s.get("vuupt_service_id"):
            raise _Falha(f"Serviço do {s.get('codigo')} sem id da Vuupt no núcleo.")
        if s.get("status") == "ENTREGUE":
            raise _Falha(f"Pedido já entregue ({s['codigo']}).")
        rota = rotas.get(s.get("vuupt_route_id")) if s.get("vuupt_route_id") else None
        if checar_lalamove and deps.agent_lalamove and rota and rota.get("agent_id") == deps.agent_lalamove:
            raise _Falha(f"Rota Lalamove ({s['codigo']}): a Vuupt não cancela na Lalamove, resolva na Lalamove à mão.")
    return vivos


def _etapa(resultado: dict, rotulo: str) -> None:
    if not (resultado or {}).get("ok"):
        raise _Falha(f"{rotulo}: {(resultado or {}).get('erro') or 'falhou sem detalhe'}")


# ── Executores por tipo: devolvem a resposta pro cliente; falha = _Falha ────

def _cancelar(conn, sol: dict, envio: dict, deps: Dependencias, feito: list[str]) -> str:
    if envio["status"] == ep.STATUS_CANCELANDO:
        raise _Falha("O cancelamento automático desse pedido ainda está na fila do worker -- espere ele terminar.")
    codigo, id_stokki = _codigo_e_id(envio)
    for s in _servicos_vivos(conn, codigo, deps):
        _etapa(deps.cancelar_vuupt(s["vuupt_service_id"]), f"Vuupt ({s['codigo']})")
        feito.append(f"serviço {s['vuupt_service_id']} ({s['codigo']}) cancelado na Vuupt")
    _etapa(deps.cancelar_stokki(id_stokki, sol.get("detalhes") or ""), "Stokki")
    feito.append(f"{codigo} cancelado na Stokki")
    return "Pedido cancelado pela Fresh Log."


def _em_espera(conn, sol: dict, envio: dict, deps: Dependencias, feito: list[str]) -> str:
    if envio["status"] not in (ep.STATUS_CRIADO, ep.STATUS_DUPLICADO):
        raise _Falha(f"Envio está {envio['status']}: só pedido criado na Stokki vai pra espera.")
    codigo, id_stokki = _codigo_e_id(envio)
    for s in _servicos_vivos(conn, codigo, deps):
        _etapa(deps.tirar_da_rota_vuupt(s["vuupt_service_id"]), f"Vuupt ({s['codigo']})")
        feito.append(f"serviço {s['vuupt_service_id']} ({s['codigo']}) está fora da rota na Vuupt (não cancelado)")
    _etapa(deps.pausar_stokki(id_stokki, sol.get("detalhes") or ""), "Stokki")
    feito.append(f"{codigo} em espera na Stokki")
    return "Pedido retirado da rota e colocado em espera pela Fresh Log."


def _reagendar(conn, sol: dict, envio: dict, deps: Dependencias, feito: list[str]) -> str:
    codigo, _ = _codigo_e_id(envio)
    dia, ini, fim = envio.get("agendamento_data"), envio.get("agendamento_hora_inicio"), envio.get("agendamento_hora_fim")
    if not (dia and ini and fim):
        raise _Falha("O envio não tem data/janela de agendamento gravada.")
    try:
        quando = date.fromisoformat(dia)
    except ValueError:
        raise _Falha(f"Data de agendamento inválida no envio: {dia!r}.")
    if quando < date.today():
        raise _Falha(f"A data pedida ({quando.strftime('%d/%m/%Y')}) já passou -- recuse e peça outra ao cliente.")
    vivos = _servicos_vivos(conn, codigo, deps, checar_lalamove=False)
    for s in vivos:
        _etapa(deps.reagendar_vuupt(s["vuupt_service_id"], dia, ini, fim), f"Vuupt ({s['codigo']})")
        feito.append(f"serviço {s['vuupt_service_id']} ({s['codigo']}) reagendado na Vuupt")
    if vivos:
        conn.execute("UPDATE portal_envios SET agendamento_aplicado_em = ?, atualizado_em = ? WHERE id = ?",
                     (_agora(), _agora(), envio["id"]))
    elif not ep.registrar_agendamento_pedido(conn, envio, "painel"):
        # sem serviço na Vuupt ainda: entra no fluxo normal de agendamento
        # (atualizar_agendamentos_confirmados aplica quando o serviço nascer)
        raise _Falha("Pedido ainda sem serviço na Vuupt e o agendamento não pôde ser registrado.")
    return f"Entrega reagendada para {quando.strftime('%d/%m/%Y')}, entre {ini} e {fim}."


_EXECUTORES = {"cancelar": _cancelar, "em_espera": _em_espera, "reagendar": _reagendar}


def _mensagem_falha(erro: str, feito: list[str]) -> str:
    if feito:
        return f"Não concluído: {erro}. Já feito: {'; '.join(feito)}. A solicitação continua pendente."
    return f"Não concluído: {erro}. Nada foi alterado."


def executar(conn: sqlite3.Connection, solicitacao_id: int, deps: Dependencias) -> dict:
    """{"ok": bool, "mensagem": str}. Só conclui a solicitação se todas as
    etapas do tipo deram certo."""
    try:
        sol = _solicitacao_pendente(conn, solicitacao_id)
    except _Falha as e:
        return {"ok": False, "mensagem": str(e)}
    tipo = sol["tipo"]
    executor = _EXECUTORES.get(tipo)
    if not executor:
        return {"ok": False, "mensagem": f"Solicitação do tipo '{tipo}' não se executa por aqui."}

    usa_stokki = tipo in _USAM_STOKKI
    if usa_stokki and not deps.abrir_stokki():
        return {"ok": False, "mensagem": STOKKI_OCUPADA}
    feito: list[str] = []
    erro = resposta = ""
    try:
        if usa_stokki:
            sol = _solicitacao_pendente(conn, solicitacao_id)  # outra execução pode ter fechado enquanto esperava a vez
        resposta = executor(conn, sol, _envio(conn, sol["envio_id"]), deps, feito)
    except _Falha as e:
        erro = str(e)
    except Exception as e:  # noqa: BLE001 -- rede/Stokki/Vuupt: vira mensagem, nunca conclui
        logger.error(f"solicitação {solicitacao_id} ({tipo}): exceção ao executar: {e}", exc_info=True)
        erro = f"{type(e).__name__}: {e}"
    finally:
        if usa_stokki:
            deps.fechar_stokki()

    if erro:
        logger.warning(f"solicitação {solicitacao_id} ({tipo}) não concluída: {erro} | feito: {feito}")
        return {"ok": False, "mensagem": _mensagem_falha(erro, feito)}
    ep.concluir_solicitacao(conn, solicitacao_id, resposta)
    logger.info(f"solicitação {solicitacao_id} ({tipo}) executada: {'; '.join(feito) or resposta}")
    return {"ok": True, "mensagem": f"{resposta} ({'; '.join(feito)})" if feito else resposta}


def recusar(conn: sqlite3.Connection, solicitacao_id: int, resposta: str) -> None:
    """Fecha como RECUSADA com a resposta pro cliente. ValueError se a
    solicitação não existe, não está pendente ou a resposta está vazia."""
    if not (resposta or "").strip():
        raise ValueError("Escreva uma resposta curta pro cliente.")
    try:
        _solicitacao_pendente(conn, solicitacao_id)
    except _Falha as e:
        raise ValueError(str(e))
    ep.concluir_solicitacao(conn, solicitacao_id, resposta.strip(), recusada=True)


# ── Produção ───────────────────────────────────────────────────────────────

def _caminhos_painel() -> None:
    for p in (_RAIZ / "painel_agentes", _RAIZ / "roteirizacao"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


_EM_ANDAMENTO = ("on_route", "arrived", "done")


def _tirar_da_rota_real(token: str, service_id: int, agent_lalamove: int, usuario: str) -> dict:
    """Tira 1 serviço da rota na Vuupt sem cancelar: rota enviada pelo
    expedicao.excluir_pedido_da_rota (só rota que ainda não saiu; a rota
    some se era a última parada) e rascunho local pelo remover_parada."""
    _caminhos_painel()
    from vuupt_client import VuuptClient
    s = VuuptClient(token).buscar_servico_por_id(service_id)
    if not s:
        return {"ok": False, "erro": f"Vuupt não devolveu o serviço {service_id}"}
    status = s.get("status")
    if status == "canceled":
        return {"ok": True, "erro": ""}
    if status in _EM_ANDAMENTO:
        return {"ok": False, "erro": f"serviço {service_id} está '{status}' na Vuupt: o motorista já está com o pedido"}
    if agent_lalamove and s.get("agent_id") == agent_lalamove:
        return {"ok": False, "erro": f"serviço {service_id} está com a Lalamove: resolva na Lalamove à mão"}
    from roteirizacao.cancelar_servico import rascunho_do_servico
    rascunho_id, _ = rascunho_do_servico(service_id)
    if s.get("route_id"):
        from expedicao import excluir_pedido_da_rota
        r = excluir_pedido_da_rota(date.today(), int(s["route_id"]), service_id, "Outros",
                                   "Portal: cliente pediu o pedido em espera", usuario or "painel-solicitacoes")
        if not r.get("ok"):
            return {"ok": False, "erro": r.get("erro") or "Vuupt não tirou o pedido da rota"}
    if rascunho_id:
        import rascunhos_rota
        rascunhos_rota.remover_parada(rascunho_id, service_id)
    return {"ok": True, "erro": ""}


def dependencias_reais(config: dict, usuario: str = "") -> Dependencias:
    """Callables de produção. A sessão da Stokki só é aberta dentro da trava
    (abrir_stokki) e na primeira chamada que precisa dela."""
    token = (config.get("vuupt_api") or {}).get("token", "")
    agent_lalamove = int((config.get("lalamove") or {}).get("agent_id_vuupt") or 0)
    dono = f"painel-solicitacao-{uuid.uuid4().hex[:8]}"
    estado: dict = {}

    def abrir_stokki() -> bool:
        from stokki import sessao_uso
        conta = sessao_uso.adquirir(dono, ttl_segundos=10 * 60, esperar_segundos=60, alternativa=True)
        if not conta:
            return False
        estado["conta"] = conta
        return True

    def fechar_stokki() -> None:
        if "conta" in estado:
            from stokki import sessao_uso
            sessao_uso.liberar(dono)
        estado.clear()

    def _sessao():
        if "sessao" not in estado:
            from stokki.auth import StokkiSession
            estado["sessao"] = StokkiSession(config, conta=estado["conta"])
        return estado["sessao"]

    def buscar_servico(codigo):
        from vuupt_client import VuuptClient
        return VuuptClient(token).buscar_servico_por_code(codigo)

    def cancelar_vuupt(service_id):
        from roteirizacao.cancelar_servico import cancelar_servico_completo
        return cancelar_servico_completo(token, service_id)

    def cancelar_stokki(id_stokki, motivo):
        from stokki.cancelar import cancelar_pedido
        return cancelar_pedido(_sessao(), id_stokki, motivo)

    def pausar_stokki(id_stokki, motivo):
        # a Stokki não tem campo de motivo no Pausar: o motivo fica no nosso banco (detalhes da solicitação)
        from stokki.pausar import pausar_pedido
        return pausar_pedido(_sessao(), id_stokki)

    def tirar_da_rota_vuupt(service_id):
        return _tirar_da_rota_real(token, service_id, agent_lalamove, usuario)

    def reagendar_vuupt(service_id, dia, ini, fim):
        _caminhos_painel()
        from planejamento_rotas import reagendar_pedido
        return reagendar_pedido(service_id, dia, ini, fim)

    return Dependencias(buscar_servico=buscar_servico, cancelar_vuupt=cancelar_vuupt, cancelar_stokki=cancelar_stokki,
                        pausar_stokki=pausar_stokki, tirar_da_rota_vuupt=tirar_da_rota_vuupt,
                        reagendar_vuupt=reagendar_vuupt, abrir_stokki=abrir_stokki, fechar_stokki=fechar_stokki,
                        agent_lalamove=agent_lalamove)
