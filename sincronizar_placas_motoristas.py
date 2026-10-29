# -*- coding: utf-8 -*-
"""
sincronizar_placas_motoristas.py

Preenche a coluna PLACA da tabela bd_motoristas a partir do VUUPT
(pedido do Hugo, 11/08 -- rodízio de placas de SP na alocação, ver
roteirizacao/rodizio_sp.py e regras/preferencias_motoristas.py).

Cruza GET /agents (traz vehicle_id por agente, quando o motorista tem
veículo vinculado no VUUPT) com GET /vehicles (traz license_plate por
vehicle_id). CONFIRMADO em 11/08: a maioria dos motoristas NÃO tem
vehicle_id vinculado ao agente no VUUPT (só 2 de 22 na 1ª execução) --
esse script preenche o que a API souber e AVISA quem ficou sem placa
resolvida, pra preenchimento manual na tela /motoristas.

Só ATUALIZA a coluna PLACA -- nunca sobrescreve uma placa já
preenchida manualmente (a API não é necessariamente mais
atual que um cadastro manual feito depois; evita apagar correção
manual numa execução futura deste script). Roda sob demanda (não é
uma rotina agendada) -- execute de novo sempre que motoristas novos
forem vinculados a um veículo no VUUPT.

COMO USAR:
    py -3.11 sincronizar_placas_motoristas.py            # a seco: só mostra
    py -3.11 sincronizar_placas_motoristas.py --aplicar  # grava na tabela
"""
import argparse
import logging
import sys
from pathlib import Path

import requests
import yaml

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from nucleo.banco import conectar  # noqa: E402
from regras.motoristas_banco import atualizar, escrever_espelho, listar  # noqa: E402

API_BASE = "https://api.vuupt.com/api/v1"
ORIGEM = Path(__file__).name


def _listar_tudo(endpoint: str, headers: dict) -> list[dict]:
    todos = []
    pagina = 1
    while True:
        resp = requests.get(f"{API_BASE}{endpoint}", headers=headers,
                            params={"per_page": 100, "page": pagina}, timeout=20)
        resp.raise_for_status()
        corpo = resp.json()
        dados = corpo.get("data", [])
        todos.extend(dados)
        paginacao = corpo.get("meta", {}).get("pagination", {})
        if pagina >= paginacao.get("total_pages", pagina):
            break
        pagina += 1
    return todos


def sincronizar(conn, agentes: list[dict], veiculos: list[dict], aplicar: bool) -> dict:
    """Preenche PLACA vazia em bd_motoristas com a placa do veículo do agente
    no VUUPT. Sem `aplicar` só calcula (nada é gravado)."""
    veiculo_por_id = {v["id"]: v for v in veiculos}
    placa_por_agent_id: dict[int, str] = {}
    for agente in agentes:
        vehicle_id = agente.get("vehicle_id")
        if not vehicle_id:
            continue
        placa = (veiculo_por_id.get(vehicle_id) or {}).get("license_plate")
        if placa:
            placa_por_agent_id[agente["id"]] = str(placa).strip().upper()

    atualizados, ja_tinha, sem_dado = [], 0, []
    try:
        for linha in listar(conn):
            agent_id = linha.get("AGENT_ID_VUUPT")
            if agent_id is None:
                continue

            placa_atual = str(linha.get("PLACA") or "").strip()
            if placa_atual and placa_atual.lower() != "nan":
                ja_tinha += 1
                continue

            placa_vuupt = placa_por_agent_id.get(agent_id)
            if placa_vuupt:
                if aplicar:
                    atualizar(conn, linha["id"], {"PLACA": placa_vuupt}, ORIGEM)
                atualizados.append((agent_id, linha.get("NOME_MOTORISTA"), placa_vuupt))
            else:
                sem_dado.append((agent_id, linha.get("NOME_MOTORISTA")))
    finally:  # grava o espelho mesmo se uma linha falhar no meio
        if aplicar and atualizados:
            escrever_espelho(conn)

    return {"atualizados": atualizados, "ja_tinha": ja_tinha, "sem_dado": sem_dado}


def main():
    parser = argparse.ArgumentParser(description="Preenche PLACA vazia em bd_motoristas a partir do VUUPT.")
    parser.add_argument("--aplicar", action="store_true",
                        help="grava na tabela (sem isso só mostra o que faria)")
    args = parser.parse_args()

    config = yaml.safe_load(open(RAIZ / "config.yaml", encoding="utf-8"))
    token = config.get("vuupt_api", {}).get("token", "")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    agentes = _listar_tudo("/agents", headers)
    veiculos = _listar_tudo("/vehicles", headers)
    logger.info(f"{len(agentes)} agente(s) e {len(veiculos)} veículo(s) carregados do VUUPT.")

    conn = conectar()
    try:
        r = sincronizar(conn, agentes, veiculos, args.aplicar)
    finally:
        conn.close()

    rotulo = "" if args.aplicar else "[A SECO -- use --aplicar para gravar] "
    logger.info(f"{rotulo}{len(r['atualizados'])} placa(s) preenchida(s) via VUUPT, {r['ja_tinha']} já tinham placa cadastrada.")
    for agent_id, nome, placa in r["atualizados"]:
        logger.info(f"  + {nome} (agent_id={agent_id}): {placa}")

    if r["sem_dado"]:
        logger.warning(
            f"{len(r['sem_dado'])} motorista(s) SEM placa resolvida (sem vehicle_id vinculado ao agente "
            f"no VUUPT) -- preencher manualmente na tela /motoristas (coluna PLACA):"
        )
        for agent_id, nome in r["sem_dado"]:
            logger.warning(f"  - {nome} (agent_id={agent_id})")


if __name__ == "__main__":
    main()
