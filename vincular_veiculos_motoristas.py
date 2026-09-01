# -*- coding: utf-8 -*-
"""
vincular_veiculos_motoristas.py

Vincula o carro de cada motorista ao agente no VUUPT e preenche a coluna
VEHICLE_ID_VUUPT de dados/BD_MOTORISTAS.xlsx (pedido do Hugo, 01/09 --
"adicionar os carros nas rotas criadas": a rota enviada pro VUUPT leva o
vehicle_id do motorista, ver roteirizacao/rotas_client.py e
roteirizacao/criar_rotas_diarias.py, mas isso só acontece se a coluna
VEHICLE_ID_VUUPT estiver preenchida -- e ela nunca foi).

Fonte do cruzamento: a PLACA da planilha (cadastro manual) contra o
license_plate de GET /vehicles. Com match ÚNICO de placa:

  1. VUUPT  -- se o agente ainda não tem veículo vinculado, faz
     PUT /users/{agent_id} {"vehicle_id": ...} (atualização parcial;
     testado 01/09 no agente TESTE 28438: só o vehicle_id muda, resto
     preservado). NUNCA sobrescreve um vínculo já existente diferente
     (só avisa). Motorista com 2 linhas na planilha (2 carros, caso
     Erick): a PRIMEIRA linha com match vira o veículo principal do
     agente no VUUPT; as demais só preenchem a planilha.
  2. PLANILHA -- preenche VEHICLE_ID_VUUPT vazio com o veículo da
     PRÓPRIA linha (cada linha carrega o carro da sua placa; a
     alocação/envio de rota usa o vehicle_id da linha escolhida).
     Não sobrescreve valor já preenchido diferente (só avisa).

Placa sem carro no VUUPT ou linha sem placa: relatadas no fim pra ação
manual. Idempotente -- rodar de novo é seguro (o que já está vinculado
vira "OK"). Roda sob demanda, igual sincronizar_placas_motoristas.py.

COMO USAR:
    py -3.11 vincular_veiculos_motoristas.py --dry-run   # só mostra o plano
    py -3.11 vincular_veiculos_motoristas.py             # aplica de verdade
"""
import argparse
import logging
import re
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
        todos.extend(corpo.get("data", []))
        paginacao = corpo.get("meta", {}).get("pagination", {})
        if pagina >= paginacao.get("total_pages", pagina):
            break
        pagina += 1
    return todos


def _norm_placa(valor) -> str:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(valor).upper())


def main():
    parser = argparse.ArgumentParser(description="Vincula carros aos agentes no VUUPT e preenche VEHICLE_ID_VUUPT.")
    parser.add_argument("--dry-run", action="store_true",
                        help="só mostra o que faria, sem PUT no VUUPT nem gravar a planilha")
    args = parser.parse_args()

    config = yaml.safe_load(open(RAIZ / "config.yaml", encoding="utf-8"))
    token = config.get("vuupt_api", {}).get("token", "")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}

    if not CAMINHO_PLANILHA.exists():
        logger.error(f"{CAMINHO_PLANILHA} não encontrada -- nada a fazer.")
        return

    agentes = _listar_tudo("/agents", headers)
    veiculos = _listar_tudo("/vehicles", headers)
    logger.info(f"{len(agentes)} agente(s) e {len(veiculos)} veículo(s) carregados do VUUPT.")

    agente_por_id = {a["id"]: a for a in agentes}
    veiculos_por_placa: dict[str, list[dict]] = {}
    for v in veiculos:
        placa = _norm_placa(v.get("license_plate"))
        if placa:
            veiculos_por_placa.setdefault(placa, []).append(v)

    df = pd.read_excel(CAMINHO_PLANILHA)
    if "VEHICLE_ID_VUUPT" not in df.columns:
        df["VEHICLE_ID_VUUPT"] = None

    vinculados_vuupt, planilha_preenchida = [], []
    ja_ok, avisos, sem_placa, placa_sem_carro = 0, [], [], []
    agentes_ja_tratados: set[int] = set()  # 2ª linha do mesmo agente não disputa o vínculo principal
    planilha_mudou = False

    for idx, linha in df.iterrows():
        try:
            agent_id = int(float(linha.get("AGENT_ID_VUUPT")))
        except (TypeError, ValueError):
            continue
        nome = str(linha.get("NOME_MOTORISTA") or "").strip() or f"Motorista {agent_id}"
        placa = _norm_placa(linha.get("PLACA"))

        if not placa:
            sem_placa.append(nome)
            continue

        matches = veiculos_por_placa.get(placa, [])
        if not matches:
            placa_sem_carro.append((nome, placa))
            continue
        if len(matches) > 1:
            avisos.append(f"{nome}: placa {placa} bate com {len(matches)} veículos no VUUPT "
                          f"({', '.join(str(m['id']) for m in matches)}) -- resolver lá e rodar de novo.")
            continue
        veiculo = matches[0]

        # 1) vínculo no VUUPT (veículo principal do agente)
        agente = agente_por_id.get(agent_id)
        if agente is None:
            avisos.append(f"{nome}: AGENT_ID_VUUPT {agent_id} não existe no VUUPT.")
        elif agent_id not in agentes_ja_tratados:
            agentes_ja_tratados.add(agent_id)
            vinculo_atual = agente.get("vehicle_id")
            if vinculo_atual == veiculo["id"]:
                ja_ok += 1
            elif vinculo_atual:
                avisos.append(f"{nome}: agente {agent_id} já vinculado ao veículo {vinculo_atual} no VUUPT "
                              f"(planilha aponta {veiculo['id']}/{placa}) -- NÃO sobrescrevi.")
            else:
                if not args.dry_run:
                    resp = requests.put(f"{API_BASE}/users/{agent_id}", headers=headers,
                                        json={"vehicle_id": veiculo["id"]}, timeout=20)
                    resp.raise_for_status()
                vinculados_vuupt.append((nome, agent_id, veiculo["id"], veiculo.get("code"), placa))

        # 2) planilha: VEHICLE_ID_VUUPT da própria linha
        try:
            valor_atual = int(float(linha.get("VEHICLE_ID_VUUPT")))
        except (TypeError, ValueError):
            valor_atual = None
        if valor_atual == veiculo["id"]:
            continue
        if valor_atual is not None:
            avisos.append(f"{nome}: VEHICLE_ID_VUUPT já preenchido com {valor_atual} na planilha "
                          f"(placa {placa} aponta {veiculo['id']}) -- NÃO sobrescrevi.")
            continue
        df.at[idx, "VEHICLE_ID_VUUPT"] = veiculo["id"]
        planilha_mudou = True
        planilha_preenchida.append((nome, veiculo["id"]))

    if planilha_mudou and not args.dry_run:
        # Int64 anulável: sem isso o pandas grava a coluna como float e os
        # ids viram 20758.0 na planilha (mesmo artefato do TELEFONE ".0")
        df["VEHICLE_ID_VUUPT"] = df["VEHICLE_ID_VUUPT"].astype("Int64")
        df.to_excel(CAMINHO_PLANILHA, index=False)

    rotulo = "[DRY-RUN] " if args.dry_run else ""
    logger.info(f"{rotulo}{len(vinculados_vuupt)} vínculo(s) novo(s) no VUUPT, {ja_ok} já estavam OK.")
    for nome, agent_id, vid, code, placa in vinculados_vuupt:
        logger.info(f"  + {nome} (agente {agent_id}) -> veículo {vid} ({code} / {placa})")
    logger.info(f"{rotulo}{len(planilha_preenchida)} linha(s) de VEHICLE_ID_VUUPT preenchida(s) na planilha.")
    for nome, vid in planilha_preenchida:
        logger.info(f"  + {nome}: VEHICLE_ID_VUUPT = {vid}")

    for aviso in avisos:
        logger.warning(aviso)
    if placa_sem_carro:
        logger.warning(f"{len(placa_sem_carro)} placa(s) sem veículo correspondente no VUUPT "
                       f"(cadastrar o carro lá e rodar de novo):")
        for nome, placa in placa_sem_carro:
            logger.warning(f"  - {nome}: {placa}")
    if sem_placa:
        logger.warning(f"{len(sem_placa)} linha(s) sem PLACA na planilha (fora do vínculo): "
                       + ", ".join(sem_placa))


if __name__ == "__main__":
    main()