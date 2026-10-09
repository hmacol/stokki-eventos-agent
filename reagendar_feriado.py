# -*- coding: utf-8 -*-
"""
reagendar_feriado.py

Pedidos do pool da Vuupt agendados pra um FERIADO (Hugo, 07/10/2026: nao
ha operacao em feriado) sao movidos pro dia util seguinte -- ou, se o
pedido e de regiao de dia fixo, pro proximo dia de visita da regiao, que
desde 07/10 ja e o dia util seguinte ao feriado (regioes_dia_fixo.
data_valida_na_regiao). Serve pra limpar o que o sistema agendou ANTES de
conhecer o calendario (12/10/2026 foi o primeiro caso) e pra qualquer
feriado futuro em que um embarcador escolha a data errada.

A data nova e registrada em agendamentos_origem como DIA_FIXO (data do
sistema), entao fora_dia_fixo.py nao a trata como data do cliente.

Uso (da raiz, na VPS como www-data):
  venv/bin/python reagendar_feriado.py --modo-teste            # proximo feriado, so lista
  venv/bin/python reagendar_feriado.py --data 2026-10-12       # grava na Vuupt
"""
import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent
for _p in (_RAIZ, _RAIZ / "roteirizacao"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from regras import feriados  # noqa: E402
from regioes_dia_fixo import (HORARIO_FIM_PADRAO, HORARIO_INICIO_PADRAO, proxima_data_valida,  # noqa: E402
                              regra_dia_fixo_do_servico)
from nucleo.normalizacao import vuupt_para_local  # noqa: E402

logger = logging.getLogger("reagendar_feriado")

DIAS_A_FRENTE = 21


def proximo_feriado(hoje: date, dias: int = DIAS_A_FRENTE) -> date | None:
    """Primeiro feriado em dia de semana (segunda a sexta) nos proximos `dias`."""
    for i in range(dias + 1):
        d = hoje + timedelta(days=i)
        if feriados.eh_feriado(d) and d.weekday() < 5:
            return d
    return None


def data_agendada(servico: dict) -> date | None:
    try:
        return date.fromisoformat(str(vuupt_para_local(servico.get("scheduled_start")) or "")[:10])
    except ValueError:
        return None


def nova_data(servico: dict, feriado: date) -> date:
    """Regiao de dia fixo: proximo dia de visita depois do feriado (que
    herda a visita do feriado). Sem regra: dia util seguinte."""
    regra = regra_dia_fixo_do_servico(servico)
    if regra:
        return proxima_data_valida(regra, feriado)
    return feriados.proximo_dia_util(feriado)


def plano(servicos: list[dict], feriado: date) -> list[dict]:
    """[{"servico", "de", "para", "regiao"}] dos pedidos agendados pro feriado."""
    itens = []
    for s in servicos:
        if data_agendada(s) != feriado:
            continue
        regra = regra_dia_fixo_do_servico(s)
        itens.append({"servico": s, "de": feriado, "para": nova_data(s, feriado),
                      "regiao": (regra or {}).get("regiao") or (regra or {}).get("nome") or "Grande SP"})
    return itens


def aplicar(itens: list[dict], vuupt, db_path=None) -> int:
    import registro_dia_fixo
    feitos = []
    for i in itens:
        s, d = i["servico"], i["para"]
        try:
            vuupt.atualizar_servico(s["id"], {
                "scheduled_start": f"{d.isoformat()}T{HORARIO_INICIO_PADRAO}-03:00",
                "scheduled_end": f"{d.isoformat()}T{HORARIO_FIM_PADRAO}-03:00",
            })
            feitos.append({"servico": s, "data": d})
            logger.info(f"  {s.get('code')}: {i['de']:%d/%m} -> {d:%d/%m} ({i['regiao']})")
        except Exception as e:
            logger.warning(f"  {s.get('code')}: falha ao reagendar ({e})")
    registro_dia_fixo.registrar_origens(feitos, registro_dia_fixo.ORIGEM_DIA_FIXO,
                                        db_path or registro_dia_fixo.DB_PATH)
    return len(feitos)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reagenda pedidos do pool marcados pra um feriado.")
    parser.add_argument("--data", help="feriado (AAAA-MM-DD); padrao: proximo feriado em dia de semana")
    parser.add_argument("--modo-teste", action="store_true", help="so lista, nao grava na Vuupt")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    feriado = date.fromisoformat(args.data) if args.data else proximo_feriado(date.today())
    if feriado is None:
        logger.info(f"Nenhum feriado em dia de semana nos proximos {DIAS_A_FRENTE} dias.")
        return 0
    if not feriados.eh_feriado(feriado):
        logger.error(f"{feriado:%d/%m/%Y} nao e feriado no calendario (regras/feriados.py).")
        return 1
    logger.info(f"Feriado: {feriado:%d/%m/%Y} ({feriados.nome_feriado(feriado)})"
                f"{' [MODO TESTE]' if args.modo_teste else ''}")

    import yaml
    config = yaml.safe_load((_RAIZ / "config.yaml").read_text(encoding="utf-8")) or {}
    token = (config.get("vuupt_api") or {}).get("token", "")
    if not token:
        logger.error("vuupt_api.token ausente.")
        return 1
    from vuupt_client import VuuptClient
    vuupt = VuuptClient(token)
    pool = vuupt.listar_servicos([{"field": "status", "operator": "eq", "value": "not_assigned"}])
    itens = plano(pool, feriado)
    logger.info(f"{len(pool)} pedido(s) no pool; {len(itens)} agendado(s) pro feriado.")
    for i in itens:
        logger.info(f"  {i['servico'].get('code')}: {i['de']:%d/%m} -> {i['para']:%d/%m} ({i['regiao']})")
    if args.modo_teste or not itens:
        return 0
    n = aplicar(itens, vuupt)
    logger.info(f"{n} pedido(s) reagendado(s) na Vuupt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
