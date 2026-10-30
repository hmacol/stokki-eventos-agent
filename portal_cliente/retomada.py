# -*- coding: utf-8 -*-
"""
Retomada de pedido em espera a pedido do cliente (spec 2026-10-25, secao 4
item 3). O portal marca o envio RETOMANDO (envio_pedidos.aplicar_acao
"retomar") e este worker, chamado por enviar_stokki.ciclo, termina o
servico:

  agendamento   a data/janela que o cliente escolheu vai pra Vuupt (PUT) e
                e registrada como DO CLIENTE (registrar_agendamento_pedido,
                o mesmo caminho do reagendar do painel).
  proxima_rota  dia fixo da regiao a partir de amanha (proxima_data); sem
                regra de dia fixo nao mexe na Vuupt: a roteirizacao decide.

Depois fecha a pausa (pedidos_pausados_cliente, fim 'retomado') e o envio
volta a CRIADO. NADA muda na Stokki (nem trava de sessao). Falhou: o envio
volta a EM_ESPERA_CLIENTE, a operacao e avisada e o cliente pode retomar de
novo. Repetir e seguro (PUT de data e idempotente, fechar nao duplica).

Tudo que fala com fora e injetado: aplicar_data_vuupt(service_id, dia, ini,
fim) -> {"ok","erro"}; proxima_data(servico) -> date | None;
avisar(envio, motivo, erro).
"""
import logging
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cancelamento as cm  # noqa: E402
import envio_pedidos as ep  # noqa: E402
import pedidos_pausados_cliente as ppc  # noqa: E402

logger = logging.getLogger("portal_retomada")

# Janela do dia fixo (roteirizacao/regioes_dia_fixo.py) quando o envio nao tem horario proprio
JANELA_PADRAO = ("08:00", "16:00")
FALHA_TECNICA = "falha técnica ao retomar o pedido"


class _Falha(Exception):
    pass


def _aplicar(aplicar_data_vuupt, s: dict, dia: str, ini: str, fim: str) -> None:
    r = aplicar_data_vuupt(s["vuupt_service_id"], dia, ini, fim) or {}
    if not r.get("ok"):
        raise _Falha(f"Vuupt ({s['codigo']}): {r.get('erro') or 'falhou sem detalhe'}")


def _retomar_agendamento(conn, envio: dict, codigo: str, vivos: list[dict], aplicar_data_vuupt, hoje: date) -> None:
    dia, ini, fim = envio.get("retomar_data"), envio.get("retomar_hora_inicio"), envio.get("retomar_hora_fim")
    if not (dia and ini and fim):
        raise _Falha("O envio não tem data/janela de retomada gravada.")
    if date.fromisoformat(dia) < hoje:
        raise _Falha(f"A data escolhida ({dia}) já passou.")
    for s in vivos:
        _aplicar(aplicar_data_vuupt, s, dia, ini, fim)
    # registrar_agendamento_pedido le a data do proprio envio: grava a escolha
    # nos campos de agendamento (como o reagendar do portal) antes de registrar.
    conn.execute("UPDATE portal_envios SET agendamento_data = ?, agendamento_hora_inicio = ?, agendamento_hora_fim = ?, "
                 "agendamento_pendente = 0, requer_agendamento = 1, agendamento_aplicado_em = NULL WHERE id = ?",
                 (dia, ini, fim, envio["id"]))  # sem commit aqui; registrar_agendamento_pedido comita junto (a falha dele dá rollback)
    registrado = {**envio, "codigo_pedido": codigo, "agendamento_data": dia,
                  "agendamento_hora_inicio": ini, "agendamento_hora_fim": fim}
    if not ep.registrar_agendamento_pedido(conn, registrado, "retomada"):
        conn.rollback()
        raise _Falha("Agendamento do cliente não pôde ser registrado (agendamentos_pedido).")
    if vivos:  # com servico, a data ja foi aplicada acima; sem, o atualizar_agendamentos_confirmados aplica quando nascer
        conn.execute("UPDATE agendamentos_pedido SET aplicado_vuupt_em = ? WHERE pedido = ?",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), codigo))
        conn.commit()


def _retomar_proxima_rota(envio: dict, vivos: list[dict], aplicar_data_vuupt, proxima_data) -> None:
    ini = envio.get("horario_inicio") or JANELA_PADRAO[0]
    fim = envio.get("horario_fim") or JANELA_PADRAO[1]
    for s in vivos:
        dia = proxima_data(s)
        if dia:
            _aplicar(aplicar_data_vuupt, s, dia.isoformat(), ini, fim)


def _processar_um(conn, envio: dict, aplicar_data_vuupt, proxima_data, agora_txt: str, hoje: date) -> None:
    id_stokki = cm.id_stokki_do_codigo(envio.get("codigo_pedido"))
    if not id_stokki:
        raise _Falha("Código do pedido (PS-...) ainda não identificado.")
    codigo = f"PS-{id_stokki}"
    servicos, _ = cm.servicos_do_pedido(conn, codigo)
    vivos = cm._vivos(servicos)
    for s in vivos:
        if not s.get("vuupt_service_id"):
            raise _Falha(f"Serviço do {s.get('codigo')} sem id da Vuupt no núcleo.")
        if s.get("status") == "ENTREGUE":
            raise _Falha(f"Pedido já entregue ({s['codigo']}).")
    if envio.get("retomar_modo") == "agendamento":
        _retomar_agendamento(conn, envio, codigo, vivos, aplicar_data_vuupt, hoje)
    else:
        _retomar_proxima_rota(envio, vivos, aplicar_data_vuupt, proxima_data)
    # So solta se o envio ainda estiver RETOMANDO (nao pisa em mudanca no meio do caminho)
    cur = conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ? AND status = ?",
                       (ep.STATUS_CRIADO, agora_txt, envio["id"], ep.STATUS_RETOMANDO))
    if cur.rowcount:
        # status e pausa na mesma transação: ou os dois, ou nenhum (CRIADO com pausa aberta nunca roteiriza)
        ppc.fechar_por_envio(conn, envio["id"], "retomado", agora_txt, commit=False)
        ppc.fechar(conn, codigo, "retomado", agora_txt, commit=False)  # no-op se a linha de cima ja fechou
        conn.commit()
    else:
        conn.commit()
        logger.warning(f"{ep.rotulo_envio(envio)} {codigo}: status mudou durante a retomada -- pausa mantida.")


def _devolver_pra_espera(conn, envio: dict, agora_txt: str) -> None:
    conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ? AND status = ?",
                 (ep.STATUS_EM_ESPERA_CLIENTE, agora_txt, envio["id"], ep.STATUS_RETOMANDO))
    conn.commit()


def processar_retomadas(conn: sqlite3.Connection, config: dict, aplicar_data_vuupt, proxima_data, avisar,
                        agora: datetime | None = None) -> dict:
    """Um envio RETOMANDO por vez. Exceção ou falha vira EM_ESPERA_CLIENTE +
    aviso à operação e a fila segue."""
    quando = agora or datetime.now()
    agora_txt = quando.strftime("%Y-%m-%d %H:%M:%S")
    total = {"retomados": 0, "falhas": 0}
    ep._garantir_colunas(conn, "portal_envios", ep.COLUNAS_RETOMAR)
    rows = conn.execute("SELECT * FROM portal_envios WHERE status = ? ORDER BY id", (ep.STATUS_RETOMANDO,)).fetchall()
    for r in rows:
        envio = dict(r)
        try:
            _processar_um(conn, envio, aplicar_data_vuupt, proxima_data, agora_txt, quando.date())
            total["retomados"] += 1
        except Exception as e:  # noqa: BLE001 -- rede, Vuupt, SQLite: volta pra espera e a fila segue
            erro = str(e) if isinstance(e, _Falha) else f"{type(e).__name__}: {e}"
            logger.warning(f"{ep.rotulo_envio(envio)}: retomada falhou -- {erro}")
            conn.rollback()
            _devolver_pra_espera(conn, envio, agora_txt)
            try:
                avisar(envio, FALHA_TECNICA, erro)
            except Exception as e2:  # noqa: BLE001
                logger.warning(f"aviso da retomada {envio['id']} falhou: {e2}")
            total["falhas"] += 1
    return total
