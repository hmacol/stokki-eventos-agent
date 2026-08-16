# -*- coding: utf-8 -*-
"""
benchmark_modelos.py

Benchmark comparativo dos 4 modelos de roteirização (doc
DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md), usando dados reais da API
VUUPT -- APENAS LEITURA (listar_servicos): nenhuma rota é criada ou
alterada, nenhum e-mail é disparado.

Modelos comparados:
  1. Atual (Grade+Greedy): agrupar_por_regiao -> consolidar_regioes_
     pequenas -> dividir_em_sublotes -> ordenar_por_distancia_base
  2. Sweep Polar: agrupar_por_sweep -> ordenar_por_distancia_base
  3. Clarke-Wright: agrupar_por_savings -> ordenar_por_distancia_base
  4. Atual + 2-Opt: agrupamento do modelo Atual -> ordenar_2opt

Pra comparação justa e fiel à produção, TODOS os modelos partem
exatamente do mesmo conjunto de pedidos (not_assigned elegíveis pra
data alvo), com nível de dificuldade e tipo de carga injetados como em
criar_rotas_diarias.py, e a MESMA partição Seco x Refrigerado/Congelado
aplicada antes do agrupamento.

COMO USAR:
    py -3.11 roteirizacao/benchmark_modelos.py
    py -3.11 roteirizacao/benchmark_modelos.py --data 2026-08-11

Saída: tabela comparativa no terminal + arquivo
       roteirizacao/dados/benchmark_resultado.txt
"""
import argparse
import logging
import sys
import time
from datetime import date, datetime
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
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "benchmark_modelos.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("benchmark_modelos")

import yaml

from vuupt_client import VuuptClient
from geocodificacao import geocodificar
from roteirizacao_dados import (
    agrupar_por_regiao, consolidar_regioes_pequenas, dividir_em_sublotes,
    ordenar_por_distancia_base, elegivel_para_data,
    obter_coordenadas, _distancia_km, extrair_volume_caixas, caixas_e_enderecos,
)
from otimizacao_rotas import agrupar_por_sweep, agrupar_por_savings, ordenar_2opt
from alocacao_motoristas import classificar_rota_viagem
from regras.tipo_veiculo import classificar_tipo_veiculo
from regras.complexidade_entrega import carregar_niveis, classificar_nivel
from regras.tipo_carga_embarcador import carregar_tipos_carga_por_sender, classificar_tipo_carga, TIPOS_CARGA_FRIA
from criar_rotas_diarias import (
    ENDERECO_BASE, TAMANHO_MINIMO_ROTA, TAMANHO_MAXIMO_ROTA,
    VOLUME_MAXIMO_ROTA, DISTANCIA_MAXIMA_ROTA_KM, DISTANCIA_MAXIMA_VIAGEM_KM,
    DB_PATH, TZ_BRASILIA, _data_alvo_rotas,
)

ARQUIVO_RESULTADO = _RAIZ_LOCAL / "dados" / "benchmark_resultado.txt"


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _km_total_rota(sublote: list[dict], base_lat: float, base_lng: float,
                   api_key: str | None = None) -> float:
    """
    Estima o KM total de UMA rota, na ORDEM em que os pedidos estão na
    lista: base -> ponto1 -> ponto2 -> ... -> base. Serviços sem
    coordenada são ignorados no somatório (não dá pra medir, sem
    inflar artificialmente).
    """
    coords = []
    for s in sublote:
        c = obter_coordenadas(s, api_key)
        if c:
            coords.append(c)
    if not coords:
        return 0.0
    total = _distancia_km(base_lat, base_lng, *coords[0])
    for i in range(len(coords) - 1):
        total += _distancia_km(*coords[i], *coords[i + 1])
    total += _distancia_km(*coords[-1], base_lat, base_lng)
    return total


def _metricas_modelo(sublotes: list[list[dict]], base_lat: float, base_lng: float,
                     api_key: str | None = None) -> dict:
    """Calcula as métricas de um conjunto de sublotes/rotas."""
    total_rotas = len(sublotes)
    total_entregas = sum(len(s) for s in sublotes)
    total_caixas = sum(sum(extrair_volume_caixas(e) for e in s) for s in sublotes)
    total_km = sum(_km_total_rota(s, base_lat, base_lng, api_key) for s in sublotes)
    return {
        "rotas": total_rotas,
        "entregas": total_entregas,
        "caixas": total_caixas,
        "km_total": total_km,
        "media_entregas": total_entregas / total_rotas if total_rotas else 0,
        "media_caixas": total_caixas / total_rotas if total_rotas else 0,
        "media_km": total_km / total_rotas if total_rotas else 0,
    }


def _validar_integridade(nome_modelo: str, servicos: list[dict], sublotes: list[list[dict]]) -> None:
    """Testes do doc (seções 6.2 e 6.3): travas de tamanho/volume e
    cobertura total de pedidos (nenhum perdido, nenhum duplicado).

    Sublote classificado como veículo grande (regras/tipo_veiculo.py --
    pedido do Hugo, 15/08) fica de fora dessas duas travas de última
    milha, mesma exceção de _validar em selecao_modelo.py."""
    for sublote in sublotes:
        if classificar_tipo_veiculo(*caixas_e_enderecos(sublote)) is not None:
            continue
        assert len(sublote) <= TAMANHO_MAXIMO_ROTA, \
            f"[{nome_modelo}] Sublote excede {TAMANHO_MAXIMO_ROTA} entregas"
        caixas = sum(extrair_volume_caixas(s) for s in sublote)
        assert caixas <= VOLUME_MAXIMO_ROTA or len(sublote) == 1, \
            f"[{nome_modelo}] Sublote excede {VOLUME_MAXIMO_ROTA} caixas"

    ids_originais = {s["id"] for s in servicos}
    ids_alocados = [s["id"] for sublote in sublotes for s in sublote]
    assert len(ids_alocados) == len(set(ids_alocados)), f"[{nome_modelo}] Pedido duplicado entre sublotes"
    assert ids_originais == set(ids_alocados), f"[{nome_modelo}] Pedidos perdidos na otimização"


def _injetar_classificacoes(servicos: list[dict], config: dict) -> None:
    """Injeta '_nivel_dificuldade' e '_tipo_carga' nos dicts, igual à
    produção (criar_rotas_diarias.py) -- necessário pras travas de nível
    e pra partição Seco x Frio. Best-effort: falha em planilha/DB não
    derruba o benchmark (extrair_nivel_dificuldade assume 1 sem a chave)."""
    try:
        caminho_niveis = config.get("complexidade_entrega", {}).get("planilha", "")
        mapa_niveis = carregar_niveis(caminho_niveis)
        mapa_tipos_carga = carregar_tipos_carga_por_sender(DB_PATH)
        for s in servicos:
            cnpj_destino = (s.get("customer") or {}).get("code", "")
            nivel, _, _ = classificar_nivel(cnpj_destino, mapa_niveis)
            s["_nivel_dificuldade"] = nivel
            tipo_carga, _ = classificar_tipo_carga(s.get("sender_id"), mapa_tipos_carga)
            s["_tipo_carga"] = tipo_carga
    except Exception as e:
        logger.warning(f"Falha ao injetar nível/tipo de carga (seguindo com padrões): {e}")
        for s in servicos:
            s.setdefault("_nivel_dificuldade", 1)
            s.setdefault("_tipo_carga", "Seco")


def _rodar_modelo_atual(particoes, gmaps_key) -> list[list[dict]]:
    """Reproduz o agrupamento de produção (SEM sequenciamento):
    agrupar_por_regiao -> consolidar_regioes_pequenas -> limite de
    distância por tipo da região -> dividir_em_sublotes."""
    sublotes: list[list[dict]] = []
    for _label, servicos_particao in particoes:
        if not servicos_particao:
            continue
        grupos = agrupar_por_regiao(servicos_particao, api_key=gmaps_key)
        grupos = consolidar_regioes_pequenas(grupos, minimo=TAMANHO_MINIMO_ROTA, api_key=gmaps_key)
        for servicos_regiao in grupos.values():
            distancia_maxima = (
                DISTANCIA_MAXIMA_VIAGEM_KM if classificar_rota_viagem(servicos_regiao, gmaps_key)
                else DISTANCIA_MAXIMA_ROTA_KM
            )
            sublotes.extend(dividir_em_sublotes(
                servicos_regiao, tamanho_minimo=TAMANHO_MINIMO_ROTA,
                tamanho_maximo=TAMANHO_MAXIMO_ROTA, volume_maximo=VOLUME_MAXIMO_ROTA,
                distancia_maxima_km=distancia_maxima, api_key=gmaps_key,
            ))
    return sublotes


def _rodar_modelo_novo(fn_agrupamento, particoes, base_lat, base_lng, gmaps_key) -> list[list[dict]]:
    """Roda agrupar_por_sweep ou agrupar_por_savings por partição, com a
    diferenciação Grande SP x Viagem delegada via eh_viagem_fn."""
    sublotes: list[list[dict]] = []
    for _label, servicos_particao in particoes:
        if not servicos_particao:
            continue
        sublotes.extend(fn_agrupamento(
            servicos_particao, base_lat, base_lng,
            tamanho_maximo=TAMANHO_MAXIMO_ROTA, volume_maximo=VOLUME_MAXIMO_ROTA,
            distancia_maxima_km=DISTANCIA_MAXIMA_ROTA_KM, api_key=gmaps_key,
            distancia_maxima_viagem_km=DISTANCIA_MAXIMA_VIAGEM_KM,
            eh_viagem_fn=lambda sub: classificar_rota_viagem(sub, gmaps_key),
        ))
    return sublotes


COLUNAS = [
    ("Modelo", 22), ("Rotas", 8), ("Entregas", 10), ("Caixas", 8),
    ("KM Total", 11), ("Média Entr.", 14), ("Média KM/Rota", 15),
]


def _linha_tabela(esq: str, meio: str, dirt: str, preenchimento: str = "═") -> str:
    return esq + meio.join(preenchimento * larg for _t, larg in COLUNAS) + dirt


def _linha_valores(valores: list[str]) -> str:
    celulas = [f"{v:^{larg}}" for v, (_t, larg) in zip(valores, COLUNAS)]
    # nome do modelo alinhado à esquerda, resto centralizado
    celulas[0] = f" {valores[0]:<{COLUNAS[0][1] - 1}}"
    return "║" + "║".join(celulas) + "║"


def _montar_tabela(resultados: dict[str, dict]) -> str:
    linhas = [
        _linha_tabela("╔", "╦", "╗"),
        _linha_valores([t for t, _l in COLUNAS]),
        _linha_tabela("╠", "╬", "╣"),
    ]
    for nome, m in resultados.items():
        linhas.append(_linha_valores([
            nome, str(m["rotas"]), str(m["entregas"]), str(m["caixas"]),
            f"{m['km_total']:.1f}", f"{m['media_entregas']:.1f}", f"{m['media_km']:.1f}",
        ]))
    linhas.append(_linha_tabela("╚", "╩", "╝"))
    return "\n".join(linhas)


def main(data_alvo: date):
    inicio = time.time()
    logger.info(f"Benchmark de modelos de roteirização -- data alvo: {data_alvo.strftime('%d/%m/%Y')}.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    vuupt = VuuptClient(token)
    filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
    servicos_brutos = vuupt.listar_servicos(filtro, per_page=100)
    logger.info(f"{len(servicos_brutos)} serviço(s) 'not_assigned' encontrado(s) (apenas leitura).")

    servicos = [s for s in servicos_brutos if elegivel_para_data(s, data_alvo)]
    logger.info(f"{len(servicos)} serviço(s) elegível(is) para {data_alvo.strftime('%d/%m/%Y')}.")
    if not servicos:
        logger.info("Nada pra comparar -- benchmark encerrado.")
        return

    _injetar_classificacoes(servicos, config)
    particoes = [
        ("Seco", [s for s in servicos if s.get("_tipo_carga") not in TIPOS_CARGA_FRIA]),
        ("Refrigerado/Congelado", [s for s in servicos if s.get("_tipo_carga") in TIPOS_CARGA_FRIA]),
    ]
    for label, servicos_particao in particoes:
        logger.info(f"Partição '{label}': {len(servicos_particao)} pedido(s).")

    coords_base = geocodificar(ENDERECO_BASE, gmaps_key)
    if not coords_base:
        logger.error("Não consegui geocodificar a base -- benchmark precisa da coordenada da base.")
        return
    base_lat, base_lng = coords_base

    resultados: dict[str, dict] = {}
    tempos: dict[str, float] = {}

    # a) MODELO ATUAL (agrupamento reaproveitado também pelo modelo 2-Opt)
    t0 = time.time()
    sublotes_atual_bruto = _rodar_modelo_atual(particoes, gmaps_key)
    sublotes_atual = [ordenar_por_distancia_base(s, base_lat, base_lng, gmaps_key)
                      for s in sublotes_atual_bruto]
    tempos["Atual (Grade+Greedy)"] = time.time() - t0
    _validar_integridade("Atual (Grade+Greedy)", servicos, sublotes_atual)
    resultados["Atual (Grade+Greedy)"] = _metricas_modelo(sublotes_atual, base_lat, base_lng, gmaps_key)

    # b) MODELO 1 -- SWEEP POLAR (sequenciamento atual, pra comparar justo)
    t0 = time.time()
    sublotes_sweep = _rodar_modelo_novo(agrupar_por_sweep, particoes, base_lat, base_lng, gmaps_key)
    sublotes_sweep = [ordenar_por_distancia_base(s, base_lat, base_lng, gmaps_key)
                      for s in sublotes_sweep]
    tempos["Sweep Polar"] = time.time() - t0
    _validar_integridade("Sweep Polar", servicos, sublotes_sweep)
    resultados["Sweep Polar"] = _metricas_modelo(sublotes_sweep, base_lat, base_lng, gmaps_key)

    # c) MODELO 2 -- CLARKE-WRIGHT SAVINGS (sequenciamento atual)
    t0 = time.time()
    sublotes_savings = _rodar_modelo_novo(agrupar_por_savings, particoes, base_lat, base_lng, gmaps_key)
    sublotes_savings = [ordenar_por_distancia_base(s, base_lat, base_lng, gmaps_key)
                        for s in sublotes_savings]
    tempos["Clarke-Wright"] = time.time() - t0
    _validar_integridade("Clarke-Wright", servicos, sublotes_savings)
    resultados["Clarke-Wright"] = _metricas_modelo(sublotes_savings, base_lat, base_lng, gmaps_key)

    # d) MODELO 3 -- agrupamento ATUAL + sequenciamento 2-OPT
    t0 = time.time()
    sublotes_2opt = [ordenar_2opt(s, base_lat, base_lng, gmaps_key)
                     for s in sublotes_atual_bruto]
    tempos["Atual + 2-Opt"] = time.time() - t0
    _validar_integridade("Atual + 2-Opt", servicos, sublotes_2opt)
    resultados["Atual + 2-Opt"] = _metricas_modelo(sublotes_2opt, base_lat, base_lng, gmaps_key)

    # Sanidade final: entregas e caixas IDÊNTICAS entre os 4 modelos
    entregas = {m["entregas"] for m in resultados.values()}
    caixas = {m["caixas"] for m in resultados.values()}
    assert len(entregas) == 1, f"Total de entregas divergente entre modelos: {entregas}"
    assert len(caixas) == 1, f"Total de caixas divergente entre modelos: {caixas}"

    tabela = _montar_tabela(resultados)
    km_atual = resultados["Atual (Grade+Greedy)"]["km_total"]
    comparativo = "\n".join(
        f"  {nome}: {m['km_total']:.1f} km "
        f"({(m['km_total'] - km_atual) / km_atual * 100:+.1f}% vs. Atual), "
        f"{m['rotas']} rota(s), calculado em {tempos[nome]:.2f}s"
        for nome, m in resultados.items()
    ) if km_atual else ""

    cabecalho = (
        f"Benchmark de modelos de roteirização -- data alvo {data_alvo.strftime('%d/%m/%Y')}, "
        f"executado em {datetime.now(TZ_BRASILIA).strftime('%d/%m/%Y %H:%M')} (Brasília)\n"
        f"{len(servicos)} pedido(s) elegível(is) | base: {ENDERECO_BASE}\n"
    )
    saida = f"{cabecalho}\n{tabela}\n\nComparativo (KM total, estimado por haversine na ordem de visita):\n{comparativo}\n"

    print()
    print(saida)
    ARQUIVO_RESULTADO.write_text(saida, encoding="utf-8")
    logger.info(f"Resultado salvo em {ARQUIVO_RESULTADO}.")
    logger.info(f"Benchmark finalizado em {time.time() - inicio:.1f}s.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark comparativo dos 4 modelos de roteirização (apenas leitura)")
    parser.add_argument("--data", help="Data alvo YYYY-MM-DD (padrão: mesma regra de produção -- "
                                       "antes das 14h = hoje, depois = próximo dia útil)")
    args = parser.parse_args()
    if args.data:
        data_alvo = datetime.strptime(args.data, "%Y-%m-%d").date()
    else:
        data_alvo = _data_alvo_rotas(datetime.now(TZ_BRASILIA))
    main(data_alvo)
