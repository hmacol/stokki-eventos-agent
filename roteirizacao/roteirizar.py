# -*- coding: utf-8 -*-
"""
roteirizar.py

Agente de roteirização automática (pedido do Hugo, 31/07): seleciona
os pedidos 'not_assigned' corretos e aciona o planejamento de rotas no
VUUPT — o próprio VUUPT faz o agrupamento/sequenciamento (solver VRP),
este agente só decide QUAIS pedidos entram em cada chamada.

Fluxo:
  1. Busca todos os serviços 'not_assigned' no VUUPT (app.vuupt.com,
     vuupt_client.py de sempre).
  2. Agrupa por REGIÃO (prefixo de 3 dígitos do CEP) — 1 planejamento
     por região, não um só com tudo junto.
  3. Regiões com menos de 2 serviços não entram (a API exige mínimo de
     2 atividades) — ficam de fora, reportadas separadamente.
  4. Pra cada região válida: chama POST /route-optimization
     (api.vuupt.com, domínio e formato DIFERENTES do resto do
     projeto) com o veículo TAPIOCA (único usado, confirmado com o
     Hugo) + os serviços daquela região, espera processar, registra
     quantos foram roteirizados de fato vs. não atribuídos.
  5. Manda e-mail de resumo (reaproveita notificar_execucao_agente.py
     já existente no projeto).

COMO USAR:
    py -3.11 roteirizar.py                # execução normal
    py -3.11 roteirizar.py --modo-teste   # só mostra o que faria, sem chamar a API
"""
import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "roteirizar.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("roteirizar")

import yaml

from vuupt_client import VuuptClient
from geocodificacao import geocodificar
from notificar_execucao_agente import notificar_execucao

from roteirizacao_dados import agrupar_por_regiao, consolidar_regioes_pequenas, dividir_em_sublotes
from otimizacao_client import aguardar_conclusao, buscar_veiculo_por_code, criar_otimizacao

ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"
CODE_VEICULO = "TAPIOCA"
TAMANHO_MINIMO_ROTA = 10  # pedido do Hugo, 01/08
TAMANHO_MAXIMO_ROTA = 18  # pedido do Hugo, 09/08 -- aumentado de 15 para 18 entregas por rota
VOLUME_MAXIMO_ROTA = 100  # pedido do Hugo, 09/08 -- limite máximo de caixas/volumes por rota
DISTANCIA_MAXIMA_ROTA_KM = 15  # pedido do Hugo, 09/08 -- máximo entre pedidos da mesma rota


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main(modo_teste: bool = False, confirmar_uso_obsoleto: bool = False):
    if not confirmar_uso_obsoleto:
        # rotas_client.py (linhas 5-9) documenta, como achado confirmado em
        # producao, que POST /route-optimization -- o unico endpoint que
        # este script chama -- so calcula uma solucao e NUNCA materializa
        # rotas reais: pedidos "roteirizados" por aqui continuam
        # not_assigned no VUUPT. criar_rotas_diarias.py foi escrito
        # especificamente para substituir este script, usando POST /routes
        # de verdade. Roda-lo sem confirmacao explicita so queimaria
        # geocodificacao + chamadas a API sem rotear nada.
        raise SystemExit(
            "roteirizar.py usa POST /route-optimization, que nao materializa rotas reais "
            "(ver rotas_client.py:5-9) -- os pedidos continuam not_assigned no VUUPT. "
            "Use roteirizacao/criar_rotas_diarias.py, que substitui este fluxo. "
            "Se voce tem certeza que quer rodar mesmo assim, chame com --confirmo-uso-obsoleto."
        )

    inicio = time.time()
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Roteirização iniciada.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    resumo_regioes = {}

    try:
        vuupt = VuuptClient(token)

        # 1. Serviços elegíveis
        filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
        servicos = vuupt.listar_servicos(filtro, per_page=100)
        logger.info(f"{len(servicos)} serviço(s) 'not_assigned' encontrado(s).")

        # 2. Agrupa por região geográfica fina, depois funde regiões
        # pequenas até um MÍNIMO de TAMANHO_MINIMO_ROTA pedidos cada
        # (pedido do Hugo, 01/08: "mínimo de 10 pedidos em cada rota,
        # forçando os endereços mais próximos") -- nenhum pedido fica
        # de fora, e a fusão sempre escolhe a região vizinha mais
        # próxima geograficamente (coordenada real, ou CEP como reserva).
        grupos_iniciais = agrupar_por_regiao(servicos, api_key=gmaps_key)
        logger.info(f"{len(grupos_iniciais)} região(ões) geográfica(s) inicial(is).")
        grupos_validos = consolidar_regioes_pequenas(grupos_iniciais, minimo=TAMANHO_MINIMO_ROTA, api_key=gmaps_key)
        logger.info(f"Após consolidar regiões pequenas: {len(grupos_validos)} região(ões) final(is).")

        if not grupos_validos:
            logger.info("Nenhum pedido elegível pra planejar.")
            resumo_regioes["Roteirização"] = {"status": "ok", "detalhe": "Nenhum pedido elegível."}
            return

        # 3. Base + veículo (só precisa 1x, reaproveitado em todas as regiões)
        coords_base = geocodificar(ENDERECO_BASE, gmaps_key)
        if not coords_base:
            raise RuntimeError(f"Não consegui geocodificar a base '{ENDERECO_BASE}'.")
        base_lat, base_lng = coords_base

        veiculo = buscar_veiculo_por_code(token, CODE_VEICULO)
        if not veiculo:
            raise RuntimeError(f"Veículo '{CODE_VEICULO}' não encontrado no VUUPT.")
        vehicle_id = veiculo["id"]

        amanha = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")

        # 4. Um planejamento por SUBLOTE (regiões grandes viram vários
        # sublotes de até TAMANHO_MAXIMO_SUBLOTE, ordenados por
        # proximidade de CEP — pedido do Hugo, 01/08)
        total_roteirizados = 0
        total_nao_atribuidos = 0
        total_sublotes = 0
        for regiao, servicos_regiao in grupos_validos.items():
            sublotes = dividir_em_sublotes(servicos_regiao, tamanho_minimo=TAMANHO_MINIMO_ROTA,
                                          tamanho_maximo=TAMANHO_MAXIMO_ROTA,
                                          volume_maximo=VOLUME_MAXIMO_ROTA,
                                          distancia_maxima_km=DISTANCIA_MAXIMA_ROTA_KM, api_key=gmaps_key)
            total_sublotes += len(sublotes)

            for indice, sublote in enumerate(sublotes, start=1):
                rotulo = regiao if len(sublotes) == 1 else f"{regiao} (lote {indice}/{len(sublotes)})"
                codigos = [s.get("code") for s in sublote]

                if modo_teste:
                    logger.info(f"[TESTE] Região {rotulo}: {len(sublote)} pedido(s) -- {codigos}")
                    continue

                logger.info(f"Região {rotulo}: planejando {len(sublote)} pedido(s) -- {codigos}")
                service_ids = [s["id"] for s in sublote]
                criado = criar_otimizacao(
                    token, base_lat, base_lng, vehicle_id, service_ids, data_inicio=amanha,
                )
                resultado = aguardar_conclusao(token, criado["id"])

                solucao = resultado.get("solution") or {}
                rotas = solucao.get("routes", [])
                nao_atribuidos = solucao.get("unassigned", [])
                qtd_roteirizados = sum(
                    1 for r in rotas for a in r.get("activities", []) if a.get("type_activity") == "service"
                )
                total_roteirizados += qtd_roteirizados
                total_nao_atribuidos += len(nao_atribuidos)

                logger.info(
                    f"  Região {rotulo}: status={resultado.get('status')}, "
                    f"{qtd_roteirizados} roteirizado(s), {len(nao_atribuidos)} não atribuído(s)."
                )
                if resultado.get("status") == "failed":
                    logger.warning(f"  Região {rotulo} falhou: {resultado.get('fail_reason')}")

        if modo_teste:
            resumo_regioes["Roteirização"] = {
                "status": "ok",
                "detalhe": f"[Teste] {len(grupos_validos)} região(ões), "
                          f"{sum(len(v) for v in grupos_validos.values())} pedido(s) no total.",
            }
        else:
            resumo_regioes["Roteirização"] = {
                "status": "ok",
                "detalhe": f"{len(grupos_validos)} região(ões) em {total_sublotes} lote(s) — "
                          f"{total_roteirizados} roteirizado(s), "
                          f"{total_nao_atribuidos} não atribuído(s).",
            }

    except Exception as e:
        logger.exception(f"Erro na roteirização: {e}")
        resumo_regioes["Roteirização"] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"Roteirização finalizada em {duracao:.1f}s.")

    try:
        notificar_execucao(resumo_regioes, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agente de roteirização automática")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Mostra o que seria planejado, sem chamar a API de otimização")
    parser.add_argument("--confirmo-uso-obsoleto", action="store_true",
                        help="Confirma que sabe que este script usa um endpoint que nao materializa "
                             "rotas reais (ver criar_rotas_diarias.py) e quer rodar mesmo assim")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste, confirmar_uso_obsoleto=args.confirmo_uso_obsoleto)
