# -*- coding: utf-8 -*-
"""
lembrar_consultas_transportadora.py

Seg-sex 08h (stokki-consulta-transportadora.timer): para cada consulta de
transportadora aberta, lembra o embarcador (1x por dia) e, passados 2 dias
uteis sem resposta (ou se o embarcador nao tem contato), avisa a equipe uma
unica vez. O 1o aviso normal sai da rodada do pipeline; aqui so entra o que
ficou sem ele.

    py -3.11 lembrar_consultas_transportadora.py --modo-teste
"""
import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

import consulta_transportadora as ct  # noqa: E402
import notificar_whatsapp  # noqa: E402
from regras.feriados import eh_dia_util, somar_dias_uteis  # noqa: E402

logger = logging.getLogger("lembrar_consultas_transportadora")

CONFIG_PATH = _RAIZ / "config.yaml"
URL_PAINEL_PADRAO = "https://app.freshhub.com.br/painel"
DIAS_UTEIS_ATE_ALERTA = 2


def link_tela(config: dict) -> str:
    base = ((config or {}).get("painel_agentes") or {}).get("url_base") or URL_PAINEL_PADRAO
    return base.rstrip("/") + "/transportadoras?aba=pendentes"


def _processar(conn, consulta: dict, config: dict, hoje: date, modo_teste: bool, res: dict):
    cid = consulta["id"]
    sem_contato = False
    if ct.deve_avisar_hoje(consulta, hoje):
        r = ct.enviar_aviso(conn, consulta, config, lembrete=bool(consulta.get("enviado_em")),
                            modo_teste=modo_teste)
        if r.get("email") or r.get("whatsapp") == "enviado":
            res["lembretes"] += 1
        sem_contato = bool(r.get("sem_contato"))
    if consulta.get("alerta_equipe_em"):
        return
    criado = datetime.fromisoformat(consulta["criado_em"]).date()
    if not (sem_contato or somar_dias_uteis(criado, DIAS_UTEIS_ATE_ALERTA) <= hoje):
        return
    situacao = notificar_whatsapp.avisar_alerta_transportadora(
        consulta["transportadora_nome"], consulta["embarcador_cnpj"], consulta["pedidos_parados"],
        (hoje - criado).days, link_tela(config), config, consulta_id=cid, modo_teste=modo_teste)
    if situacao == "enviado" and not modo_teste:
        conn.execute("UPDATE transportadora_consultas SET alerta_equipe_em=? WHERE id=?", (ct._agora(), cid))
        conn.commit()
        res["alertas"] += 1


def rodar(conn, config: dict, hoje: date, modo_teste: bool = False) -> dict:
    res = {"lembretes": 0, "alertas": 0}
    if not ct.config_consulta(config)["ativo"] or not eh_dia_util(hoje):
        return res
    for consulta in ct.consultas_abertas(conn):
        if not consulta["pedidos_parados"]:
            continue
        try:
            _processar(conn, consulta, config, hoje, modo_teste, res)
        except Exception as exc:
            logger.error(f"Consulta {consulta['id']} falhou: {exc!r}")
    return res


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Lembrete diario e alerta de transportadora desconhecida.")
    parser.add_argument("--modo-teste", action="store_true", help="nao envia nem marca nada")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    conn = ct.conectar_consultas(ct.DB_PATH)
    try:
        print(rodar(conn, config, date.today(), args.modo_teste))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
