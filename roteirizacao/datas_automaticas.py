# -*- coding: utf-8 -*-
"""
datas_automaticas.py

Data automatica de pedido novo sem agendamento (spec
docs/superpowers/specs/2026-11-02-agendamento-automatico-proximo-dia-util-design.md):
marca os servicos cuja data foi posta pelo sistema (origem AUTOMATICO em
agendamentos_origem) e, na rodada das 16h, anda pro proximo dia util quem
nao saiu em rota/rascunho. Nunca mexe em data de cliente ou da equipe.
"""
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import registro_dia_fixo  # noqa: E402
from fora_dia_fixo import data_agendada  # noqa: E402
from regioes_dia_fixo import ajustar_data_por_dia_fixo  # noqa: E402
from nucleo.normalizacao import vuupt_para_local  # noqa: E402

logger = logging.getLogger(__name__)

DIAS_RASCUNHO_RECENTE = 6


def marcar_datas_automaticas(servicos: list[dict], db_path=None) -> int:
    """Poe s["_data_automatica"] = True em quem tem data com origem AUTOMATICO.
    Tolerante: falha vai pro log e devolve 0."""
    try:
        conn = registro_dia_fixo.conectar(db_path or registro_dia_fixo.DB_PATH)
        try:
            n = 0
            for s in servicos:
                data = data_agendada(s)
                if data and registro_dia_fixo.data_e_automatica(conn, s, data):
                    s["_data_automatica"] = True
                    n += 1
            return n
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Falha ao marcar datas automaticas: {e}")
        return 0


def ids_em_rascunho_recentes(hoje: date, data_alvo: date, listar=None) -> set[int]:
    """service_id das paradas dos rascunhos de hoje-6 dias ate hoje, mais data_alvo."""
    if listar is None:
        from rascunhos_rota import listar_rascunhos_do_dia
        listar = listar_rascunhos_do_dia
    datas = [hoje - timedelta(days=i) for i in range(DIAS_RASCUNHO_RECENTE, -1, -1)]
    if data_alvo not in datas:
        datas.append(data_alvo)
    ids: set[int] = set()
    for d in datas:
        for rascunho in listar(d) or []:
            for parada in rascunho.get("paradas") or []:
                sid = parada.get("service_id")
                if sid is not None:
                    ids.add(int(sid))
    return ids


def _hhmm_local(ts) -> str | None:
    local = vuupt_para_local(ts)
    return str(local)[11:16] if local and len(str(local)) >= 16 else None


def andar_datas_automaticas(servicos: list[dict], vuupt, data_alvo: date, ids_em_rascunho: set[int],
                            modo_teste: bool, db_path=None) -> list[dict]:
    """Anda pra data_alvo (ou pro dia de visita da regiao) os servicos de data
    automatica anterior a data_alvo que nao estao em rascunho, mantendo o
    horario. Devolve [{"servico", "de", "para"}]. modo_teste: so devolve."""
    candidatos: list[dict] = []
    for s in servicos:
        if not s.get("_data_automatica") or s.get("id") in ids_em_rascunho:
            continue
        de = data_agendada(s)
        if de is None or de >= data_alvo:
            continue
        ini, fim = _hhmm_local(s.get("scheduled_start")), _hhmm_local(s.get("scheduled_end"))
        if not ini or not fim:
            continue
        para = ajustar_data_por_dia_fixo(s, data_alvo)[0]
        candidatos.append({"servico": s, "de": de, "para": para, "_ini": ini, "_fim": fim})

    if modo_teste:
        return [{"servico": c["servico"], "de": c["de"], "para": c["para"]} for c in candidatos]

    feitos: list[dict] = []
    for c in candidatos:
        s, para = c["servico"], c["para"]
        inicio = f"{para.isoformat()}T{c['_ini']}:00-03:00"
        fim = f"{para.isoformat()}T{c['_fim']}:00-03:00"
        try:
            vuupt.atualizar_servico(s["id"], {"scheduled_start": inicio, "scheduled_end": fim})
        except Exception as e:
            logger.warning(f"  {s.get('code')}: falha ao andar data automatica: {e}")
            continue
        s["scheduled_start"] = inicio
        s["scheduled_end"] = fim
        logger.info(f"  {s.get('code')}: data automatica andou de {c['de'].strftime('%d/%m/%Y')} "
                    f"pra {para.strftime('%d/%m/%Y')}.")
        feitos.append({"servico": s, "de": c["de"], "para": para})

    if feitos:
        registro_dia_fixo.registrar_origens(
            [{"servico": f["servico"], "data": f["para"]} for f in feitos],
            registro_dia_fixo.ORIGEM_AUTOMATICO, db_path or registro_dia_fixo.DB_PATH)
    return feitos
