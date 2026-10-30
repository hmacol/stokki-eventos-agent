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
  em_espera  tira da rota na Vuupt (sem cancelar, como o "Excluir da
             rota" da Expedição) e grava a pausa em pedidos_pausados_cliente
             (pedidos_pausados_cliente.abrir) + portal_envios.status =
             EM_ESPERA_CLIENTE. NADA muda na Stokki (decisão do Hugo
             27/10): o pedido segue onde está lá e a roteirização pula quem
             tem pausa aberta. Falhando a Vuupt, nada é gravado. Repetir
             depois de falha parcial é seguro: serviço sem rota não é
             mexido de novo e a pausa aberta não duplica.
  reagendar  serviço em rota sai da rota primeiro (mesma etapa do
             em_espera; data nova em serviço roteirizado não muda nada),
             depois PUT /services/{id} com a data/janela gravada no envio
             e registra a data em agendamentos_pedido como DO CLIENTE
             (envio_pedidos.registrar_agendamento_pedido, caminho normal
             do portal). Não usa planejamento_rotas.reagendar_pedido: ele
             grava origem EQUIPE e a regra de dia fixo deixaria de virar
             dedicado. Não usa a Stokki.

Tudo que fala com fora vem em `Dependencias` (injetável nos testes);
`dependencias_reais(config)` monta as de produção. A trava da Stokki
(stokki/sessao_uso) é pega só por cancelar, espera até 60 s e é
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
import pedidos_pausados_cliente as ppc  # noqa: E402

logger = logging.getLogger("portal_solicitacoes_execucao")

STOKKI_OCUPADA = "Stokki ocupada, tente em instantes."
_USAM_STOKKI = ("cancelar",)

# Em espera a pedido do cliente (27/10): so tira da rota e grava a pausa; a
# Stokki nao e tocada ("Em espera" la e a fila de entrada da Fresh Log).


@dataclass
class Dependencias:
    """Cada callable devolve {"ok": bool, "erro": str} (menos buscar_servico,
    que devolve o serviço da Vuupt pelo código ou None)."""
    buscar_servico: Callable[[str], dict | None]
    cancelar_vuupt: Callable[[int], dict]
    cancelar_stokki: Callable[[int, str], dict]
    tirar_da_rota_vuupt: Callable[[int], dict]
    reagendar_vuupt: Callable[[int, str, str, str], dict]
    abrir_stokki: Callable[[], bool]
    fechar_stokki: Callable[[], None]
    agent_lalamove: int = 0


class _Falha(Exception):
    pass


AVISO_REROTEIRIZACAO = "Pedido segurado até o cliente retomar pelo portal."
_AVISOS_OPERADOR = {"em_espera": AVISO_REROTEIRIZACAO}


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


def _tirar_da_rota(deps: Dependencias, s: dict, feito: list[str]) -> None:
    """Etapa comum do em_espera e do reagendar. tirar_da_rota_vuupt devolve
    rota_id quando tirou de uma rota e rota_cancelada quando era a última
    parada (a rota sumiu)."""
    r = deps.tirar_da_rota_vuupt(s["vuupt_service_id"])
    _etapa(r, f"Vuupt ({s['codigo']})")
    if r.get("rota_id"):
        txt = (f"serviço {s['vuupt_service_id']} ({s['codigo']}) tirado da rota {r['rota_id']} na Vuupt "
               f"(está fora da rota, não cancelado)")
        if r.get("rota_cancelada"):
            txt += f"; a rota {r['rota_id']} ficou vazia e foi cancelada"
        feito.append(txt)


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
    # EM_ESPERA_CLIENTE: repeticao depois de falha ao concluir (a pausa ja foi
    # gravada); abrir() nao duplica e o resto e idempotente.
    if envio["status"] not in (ep.STATUS_CRIADO, ep.STATUS_DUPLICADO, ep.STATUS_EM_ESPERA_CLIENTE):
        raise _Falha(f"Envio está {envio['status']}: só pedido criado na Stokki vai pra espera.")
    codigo, id_stokki = _codigo_e_id(envio)
    vivos = _servicos_vivos(conn, codigo, deps)
    for s in vivos:
        _tirar_da_rota(deps, s, feito)
    ppc.garantir_tabela(conn)
    # Status (com guarda) e pausa na mesma transacao: o cliente pode ter
    # cancelado enquanto tirava da rota, e CANCELANDO/CANCELADO nunca e pisado.
    cur = conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ? AND status IN (?, ?, ?)",
                       (ep.STATUS_EM_ESPERA_CLIENTE, _agora(), envio["id"],
                        ep.STATUS_CRIADO, ep.STATUS_DUPLICADO, ep.STATUS_EM_ESPERA_CLIENTE))
    if cur.rowcount == 0:
        conn.rollback()
        raise _Falha("O envio mudou de situação durante a execução (o cliente cancelou?). "
                     "Nada foi gravado: confira o pedido no portal.")
    ppc.abrir(conn, codigo, id_stokki, envio["id"], [s["vuupt_service_id"] for s in vivos], _agora(), commit=False)
    conn.commit()
    feito.append(f"{codigo} segurado em espera (nada muda na Stokki)")
    return "Pedido retirado da rota e mantido em espera até você retomar pelo portal."


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
    vivos = _servicos_vivos(conn, codigo, deps)
    for s in vivos:
        _tirar_da_rota(deps, s, feito)
        _etapa(deps.reagendar_vuupt(s["vuupt_service_id"], dia, ini, fim), f"Vuupt ({s['codigo']})")
        feito.append(f"serviço {s['vuupt_service_id']} ({s['codigo']}) com a data nova na Vuupt")
    # Data do CLIENTE em agendamentos_pedido (também marca
    # portal_envios.agendamento_aplicado_em). Sem serviço na Vuupt ainda,
    # atualizar_agendamentos_confirmados aplica quando ele nascer; com
    # serviço, a data já foi aplicada acima e a linha sai marcada.
    if not ep.registrar_agendamento_pedido(conn, envio, "painel"):
        raise _Falha("Agendamento do cliente não pôde ser registrado (agendamentos_pedido).")
    if vivos:
        conn.execute("UPDATE agendamentos_pedido SET aplicado_vuupt_em = ? WHERE pedido = ?",
                     (_agora(), envio["codigo_pedido"]))
        conn.commit()
    feito.append("data registrada como do cliente")
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
    if usa_stokki:
        try:
            livre = deps.abrir_stokki()
        except Exception as e:  # noqa: BLE001 -- banco da trava travado etc.: mensagem, nunca 500
            logger.error(f"solicitação {solicitacao_id}: trava da Stokki falhou: {e}", exc_info=True)
            return {"ok": False,
                    "mensagem": f"Não foi possível pegar a vez na Stokki ({type(e).__name__}: {e}). Nada foi alterado."}
        if not livre:
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
    try:
        _solicitacao_pendente(conn, solicitacao_id)  # alguém fechou durante a execução?
    except _Falha as e:
        return {"ok": False, "mensagem": _mensagem_falha(f"{e} Fechada por outra pessoa durante a execução", feito)}
    ep.concluir_solicitacao(conn, solicitacao_id, resposta)
    logger.info(f"solicitação {solicitacao_id} ({tipo}) executada: {'; '.join(feito) or resposta}")
    mensagem = f"{resposta} ({'; '.join(feito)})" if feito else resposta
    if tipo in _AVISOS_OPERADOR:
        mensagem += " " + _AVISOS_OPERADOR[tipo]
    return {"ok": True, "mensagem": mensagem}


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


def _data_da_rota(token: str, route_id: int) -> date:
    """Dia da rota (start_at da Vuupt vem em UTC -> hora local). Rota de dia
    passado ainda aberta usa hoje, como a Torre (excluir_pedido_da_rota
    recusa data passada)."""
    from rotas_client import buscar_rota
    from expedicao import _rota_do_corpo
    from nucleo.normalizacao import vuupt_para_local
    local = vuupt_para_local(_rota_do_corpo(buscar_rota(token, route_id)).get("start_at"))
    try:
        dia = date.fromisoformat(str(local)[:10])
    except ValueError:
        dia = date.today()
    return max(dia, date.today())


def _tirar_da_rota_real(token: str, service_id: int, agent_lalamove: int, usuario: str) -> dict:
    """Tira 1 serviço da rota na Vuupt sem cancelar: rota enviada pelo
    expedicao.excluir_pedido_da_rota (só rota que ainda não saiu; a rota
    some se era a última parada) e rascunho local pelo remover_parada.
    Devolve {"ok", "erro", "rota_id", "rota_cancelada"} (rota_id só quando
    tirou de uma rota)."""
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
    rota_id, rota_cancelada = None, False
    if s.get("route_id"):
        rota_id = int(s["route_id"])
        try:
            dia = _data_da_rota(token, rota_id)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erro": f"Falha ao consultar a rota #{rota_id} na Vuupt: {e}"}
        from expedicao import excluir_pedido_da_rota
        r = excluir_pedido_da_rota(dia, rota_id, service_id, "Outros",
                                   "Portal: solicitação do cliente (em espera/reagendar)", usuario or "painel-solicitacoes")
        if not r.get("ok"):
            return {"ok": False, "erro": r.get("erro") or "Vuupt não tirou o pedido da rota"}
        rota_cancelada = bool(r.get("rota_cancelada"))
    if rascunho_id:
        import rascunhos_rota
        rascunhos_rota.remover_parada(rascunho_id, service_id)
    return {"ok": True, "erro": "", "rota_id": rota_id, "rota_cancelada": rota_cancelada}


def reagendar_servico_vuupt(token: str, service_id: int, dia: str, ini: str, fim: str) -> dict:
    """PUT direto, como atualizar_agendamentos_confirmados (data do
    cliente). planejamento_rotas.reagendar_pedido NÃO: grava origem EQUIPE.
    Também usado pelo worker da retomada (portal_cliente/retomada.py)."""
    _caminhos_painel()
    from vuupt_client import VuuptAPIError, VuuptClient, _converter_data_para_iso
    inicio = _converter_data_para_iso(f"{dia} {str(ini)[:5]}:00")
    termino = _converter_data_para_iso(f"{dia} {str(fim)[:5]}:00")
    if not inicio or not termino:
        return {"ok": False, "erro": f"data/horário inválidos: {dia} {ini}-{fim}"}
    vuupt = VuuptClient(token)
    try:
        vuupt.atualizar_servico(service_id, {"scheduled_start": inicio, "scheduled_end": termino})
    except VuuptAPIError as e:
        return {"ok": False, "erro": str(e)}
    try:
        from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids
        ressincronizar_ids(vuupt, [service_id])
    except Exception as e:  # noqa: BLE001 -- a escrita na Vuupt já aconteceu; o timer de 15 min alcança
        logger.warning(f"ressincronização do serviço {service_id} falhou: {e}")
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

    def tirar_da_rota_vuupt(service_id):
        return _tirar_da_rota_real(token, service_id, agent_lalamove, usuario)

    def reagendar_vuupt(service_id, dia, ini, fim):
        return reagendar_servico_vuupt(token, service_id, dia, ini, fim)

    return Dependencias(buscar_servico=buscar_servico, cancelar_vuupt=cancelar_vuupt, cancelar_stokki=cancelar_stokki,
                        tirar_da_rota_vuupt=tirar_da_rota_vuupt,
                        reagendar_vuupt=reagendar_vuupt, abrir_stokki=abrir_stokki, fechar_stokki=fechar_stokki,
                        agent_lalamove=agent_lalamove)
