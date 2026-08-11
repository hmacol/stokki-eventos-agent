# -*- coding: utf-8 -*-
"""
sincronizar_placas_motoristas.py

Preenche a coluna PLACA de dados/BD_MOTORISTAS.xlsx a partir do VUUPT
(pedido do Hugo, 11/08 -- rodízio de placas de SP na alocação, ver
roteirizacao/rodizio_sp.py e regras/preferencias_motoristas.py).

Cruza GET /agents (traz vehicle_id por agente, quando o motorista tem
veículo vinculado no VUUPT) com GET /vehicles (traz license_plate por
vehicle_id). CONFIRMADO em 11/08: a maioria dos motoristas NÃO tem
vehicle_id vinculado ao agente no VUUPT (só 2 de 22 na 1ª execução) --
esse script preenche o que a API souber e AVISA quem ficou sem placa
resolvida, pra preenchimento manual direto na planilha.

Só ATUALIZA a coluna PLACA -- nunca sobrescreve uma placa já
preenchida manualmente na planilha (a API não é necessariamente mais
atual que um cadastro manual feito depois; evita apagar correção
manual numa execução futura deste script). Roda sob demanda (não é
uma rotina agendada) -- execute de novo sempre que motoristas novos
forem vinculados a um veículo no VUUPT.

COMO USAR:
    py -3.11 sincronizar_placas_motoristas.py
"""
import logging
import sys
from pathlib import Path

import pandas as pd
import requests
import yaml

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://api.vuupt.com/api/v1"
CAMINHO_PLANILHA = RAIZ / "dados" / "BD_MOTORISTAS.xlsx"


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


def main():
    config = yaml.safe_load(open(RAIZ / "config.yaml", encoding="utf-8"))
    token = config.get("vuupt_api", {}).get("token", "")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    if not CAMINHO_PLANILHA.exists():
        logger.error(f"{CAMINHO_PLANILHA} não encontrada -- nada a fazer.")
        return

    agentes = _listar_tudo("/agents", headers)
    veiculos = _listar_tudo("/vehicles", headers)
    logger.info(f"{len(agentes)} agente(s) e {len(veiculos)} veículo(s) carregados do VUUPT.")

    veiculo_por_id = {v["id"]: v for v in veiculos}
    placa_por_agent_id: dict[int, str] = {}
    for agente in agentes:
        vehicle_id = agente.get("vehicle_id")
        if not vehicle_id:
            continue
        placa = (veiculo_por_id.get(vehicle_id) or {}).get("license_plate")
        if placa:
            placa_por_agent_id[agente["id"]] = str(placa).strip().upper()

    df = pd.read_excel(CAMINHO_PLANILHA)
    colunas_originais = list(df.columns)
    if "PLACA" not in colunas_originais:
        df["PLACA"] = ""

    atualizados, ja_tinha, sem_dado = [], 0, []
    for idx, linha in df.iterrows():
        try:
            agent_id = int(float(linha.get("AGENT_ID_VUUPT")))
        except (TypeError, ValueError):
            continue

        placa_atual = str(linha.get("PLACA") or "").strip()
        if placa_atual and placa_atual.lower() != "nan":
            ja_tinha += 1
            continue

        placa_vuupt = placa_por_agent_id.get(agent_id)
        if placa_vuupt:
            df.at[idx, "PLACA"] = placa_vuupt
            atualizados.append((agent_id, linha.get("NOME_MOTORISTA"), placa_vuupt))
        else:
            sem_dado.append((agent_id, linha.get("NOME_MOTORISTA")))

    # PLACA nova -- inclui no fim se ainda não existia na planilha original
    colunas_finais = colunas_originais + (["PLACA"] if "PLACA" not in colunas_originais else [])
    df = df[colunas_finais]
    df.to_excel(CAMINHO_PLANILHA, index=False)

    logger.info(f"{len(atualizados)} placa(s) preenchida(s) via VUUPT, {ja_tinha} já tinham placa cadastrada.")
    for agent_id, nome, placa in atualizados:
        logger.info(f"  + {nome} (agent_id={agent_id}): {placa}")

    if sem_dado:
        logger.warning(
            f"{len(sem_dado)} motorista(s) SEM placa resolvida (sem vehicle_id vinculado ao agente "
            f"no VUUPT) -- preencher manualmente na coluna PLACA de {CAMINHO_PLANILHA}:"
        )
        for agent_id, nome in sem_dado:
            logger.warning(f"  - {nome} (agent_id={agent_id})")


if __name__ == "__main__":
    main()
