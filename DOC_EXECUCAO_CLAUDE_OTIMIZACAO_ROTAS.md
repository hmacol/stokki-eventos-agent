# Documento de Especificação e Execução: Otimização da Roteirização — 3 Modelos de Eficiência

Este documento contém todas as instruções, regras de negócio, estrutura de código e comandos de validação para que o **Claude Code** (ou qualquer agente/desenvolvedor) implemente de forma autônoma e completa as **3 otimizações de roteirização** e o **benchmark comparativo** no módulo `roteirizacao/` do projeto `agente_stokki_eventos`.

---

## 1. Contexto do Projeto e Arquitetura Existente

* **Diretório Raiz:** `C:\agente_stokki_eventos`
* **Python Target:** Python 3.11 (`py -3.11`)
* **Configuração Central:** `config.yaml` (credenciais VUUPT, Google Maps, SMTP, etc.)
* **Módulo de Roteirização:** `roteirizacao/`
* **Dados de Geocodificação:** Cache SQLite em `dados/dados.db`, tabela `geocache` (gerenciado por `geocodificacao.py`)

### 1.1. Fluxo Atual de Roteirização (`roteirizacao_dados.py`)

O fluxo é executado diariamente por `criar_rotas_diarias.py` (job das 13h) e incrementado por hora por `incrementar_rotas.py` (14h–20h). As etapas são:

1. **Agrupamento Geográfico (`agrupar_por_regiao`):** Células de grade estática de `0.1°` (lat/lng, ~11 km). Com coordenada = célula de grade; sem coordenada = prefixo do CEP (2 dígitos).
2. **Consolidação de Regiões Pequenas (`consolidar_regioes_pequenas`):** Fusão iterativa de regiões com < 10 pedidos na região vizinha mais próxima (centroide de coordenadas reais; CEP médio como reserva).
3. **Divisão em Sublotes (`dividir_em_sublotes`):** Ordenação 1D por `(latitude, longitude)`, empacotamento ganancioso (greedy bin packing) respeitando 4 travas simultâneas:
   - ≤ 18 entregas por sublote
   - ≤ 100 caixas/volumes por sublote (`dimension_3` do serviço VUUPT)
   - ≤ 15 km de distância entre qualquer par de pedidos do mesmo sublote (Grande SP) ou sem limite (Viagem)
   - Nível de dificuldade: nível 3 limita a 4 entregas/rota; nível 4 exige rota exclusiva
4. **Sequenciamento (`ordenar_por_distancia_base`):** Ordena da entrega mais DISTANTE até a mais PRÓXIMA da base (`Rua Zilda, 288, Casa Verde Alta, São Paulo`), estritamente por distância euclidiana.

### 1.2. Constantes Existentes (Fonte de Verdade)

```python
# criar_rotas_diarias.py
ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"
BASE_LOCATION_ID = 6950
TAMANHO_MINIMO_ROTA = 10
TAMANHO_MAXIMO_ROTA = 18
VOLUME_MAXIMO_ROTA = 100
DISTANCIA_MAXIMA_ROTA_KM = 20      # Grande SP
DISTANCIA_MAXIMA_VIAGEM_KM = None   # Viagem (sem limite)
PREFIXO_NOME_ROTA = "Planejamento"
```

```python
# roteirizacao_dados.py
NIVEL_3_TAMANHO_MAXIMO_ROTA = 4
NIVEL_ROTA_EXCLUSIVA = 4
```

### 1.3. Funções Utilitárias Existentes que DEVEM ser Reutilizadas (NÃO duplicar)

| Função | Módulo | O que faz |
|---|---|---|
| `obter_coordenadas(servico, api_key)` | `roteirizacao_dados.py` | Retorna `(lat, lng)` com cache em memória + SQLite |
| `_distancia_km(lat1, lng1, lat2, lng2)` | `roteirizacao_dados.py` | Haversine em km |
| `extrair_cep(servico)` | `roteirizacao_dados.py` | CEP 8 dígitos sem hífen |
| `extrair_volume_caixas(servico)` | `roteirizacao_dados.py` | `dimension_3` com fallback pra 1 |
| `extrair_nivel_dificuldade(servico)` | `roteirizacao_dados.py` | Chave `_nivel_dificuldade` injetada previamente |
| `classificar_rota_viagem(sublote, api_key)` | `alocacao_motoristas.py` | True se ≥1 entrega fora da Grande SP |
| `geocodificar(endereco, api_key)` | `geocodificacao.py` | Geocodificação com cache SQLite |

---

## 2. Objetivo da Tarefa

Implementar **3 modelos de otimização** e um **script de benchmark** que compare os 4 algoritmos (Atual + 3 novos) lado a lado, usando dados reais da API VUUPT:

1. **Modelo 1 — Sweep Polar:** Substituir o agrupamento por grade estática por varredura angular (cones geográficos em relação à base).
2. **Modelo 2 — Clarke-Wright Savings:** Agrupamento por economia de distância (matriz de savings), maximizando preenchimento de carga.
3. **Modelo 3 — Sequenciamento 2-Opt:** Refinamento do trajeto interno de cada rota, eliminando cruzamentos por busca local 2-opt.

---

## 3. Especificação Detalhada dos 3 Modelos

### 3.1. Modelo 1 — Agrupamento por Varredura Polar (Sweep Algorithm)

**Conceito:** Converte coordenadas de cada pedido em ângulo polar $\theta$ em relação à base. Ordena por $\theta$ e agrupa sequencialmente, fechando a rota ao atingir qualquer trava.

**Implementação — Função `agrupar_por_sweep`:**

```python
import math

def agrupar_por_sweep(
    servicos: list[dict],
    base_lat: float,
    base_lng: float,
    tamanho_maximo: int = 18,
    volume_maximo: int = 100,
    distancia_maxima_km: float | None = 20,
    api_key: str | None = None,
) -> list[list[dict]]:
    """
    Agrupa pedidos por varredura angular (Sweep) em relação à base.
    Ordena por ângulo polar (theta) e preenche rotas sequencialmente,
    respeitando as MESMAS 4 travas do dividir_em_sublotes atual:
      - ≤ tamanho_maximo entregas
      - ≤ volume_maximo caixas
      - ≤ distancia_maxima_km entre pares (quando ambos têm coordenada)
      - nível de dificuldade 3 (limita a NIVEL_3_TAMANHO_MAXIMO_ROTA)
        e nível 4 (rota exclusiva)

    Serviços sem coordenada vão pro final (theta = +inf), agrupados
    entre si depois que a varredura dos georreferenciados termina.
    """
    def _theta(servico):
        coords = obter_coordenadas(servico, api_key)
        if not coords:
            return float("inf")
        return math.atan2(coords[0] - base_lat, coords[1] - base_lng)

    ordenados = sorted(servicos, key=_theta)

    # Daqui em diante, o empacotamento ganancioso é IDÊNTICO ao
    # dividir_em_sublotes -- inclusive travas de nível 3/4 e distância.
    # Reutilizar a MESMA lógica de empacotamento do dividir_em_sublotes,
    # trocando APENAS a ordenação de entrada (coordenada 1D -> theta).
    # ...
```

**Regras Obrigatórias:**
- TODAS as 4 travas existentes (entregas, caixas, distância, nível de dificuldade) DEVEM ser respeitadas — a Sweep muda apenas a ORDENAÇÃO dos serviços antes do bin packing, não as regras de corte.
- Serviços sem coordenada (`obter_coordenadas` retorna `None`): atribuir `theta = float("inf")` e agrupar normalmente no final da varredura.
- A coordenada da base (`base_lat`, `base_lng`) é obtida por `geocodificar(ENDERECO_BASE, gmaps_key)`.

---

### 3.2. Modelo 2 — Clarke-Wright Savings (Economia de Distância)

**Conceito:** Para cada par $(i, j)$ de pedidos, calcular a economia de combiná-los na mesma rota: $S_{ij} = d_{0i} + d_{0j} - d_{ij}$ onde $d_{0i}$ é a distância da base ao pedido $i$. Ordenar pares por $S_{ij}$ decrescente e fundir rotas gananciosamente.

**Implementação — Função `agrupar_por_savings`:**

```python
def agrupar_por_savings(
    servicos: list[dict],
    base_lat: float,
    base_lng: float,
    tamanho_maximo: int = 18,
    volume_maximo: int = 100,
    distancia_maxima_km: float | None = 20,
    api_key: str | None = None,
) -> list[list[dict]]:
    """
    Agrupa pedidos pelo algoritmo Clarke-Wright Savings (CVRP).
    Parte de rotas individuais (1 pedido cada) e funde pares com
    maior economia (S_ij), respeitando as MESMAS 4 travas.

    Passos:
    1. Calcular d_0i para todo pedido i (distância base -> i).
    2. Calcular S_ij para todos os pares (i,j).
    3. Ordenar pares por S_ij decrescente.
    4. Para cada par (i,j), se i e j estão em rotas DIFERENTES e a
       fusão não viola nenhuma trava, fundir as duas rotas numa só.
    5. Retornar a lista de rotas (sublotes) resultante.

    Pedidos sem coordenada: usar distância d_0i = 0 (savings zero,
    ficam por último nas fusões) -- nunca perde um pedido.
    """
```

**Regras Obrigatórias:**
- TODAS as 4 travas (entregas, caixas, distância, nível) verificadas ANTES de cada fusão — se qualquer uma estourar, a fusão é rejeitada e o algoritmo tenta o próximo par.
- **Verificação de distância pra fusão:** ao fundir rota A com rota B, checar que NENHUM par de pedidos da rota combinada excede `distancia_maxima_km` (se aplicável) — exatamente como `_cabe_na_distancia` no código atual.
- **Complexidade aceitável:** para até ~200 pedidos/dia (volume real observado nos logs), a matriz de savings tem ~20.000 pares, processada em <1 segundo.

---

### 3.3. Modelo 3 — Sequenciamento 2-Opt (Refinamento de Trajeto)

**Conceito:** Dado um sublote já agrupado (pelo modelo atual OU por qualquer outro), melhora a ORDEM das entregas eliminando cruzamentos. Mantém a 1ª entrega fixa (a mais distante da base, requisito do Hugo) e aplica 2-opt nas posições 2..N.

**Implementação — Substituição da função `ordenar_por_distancia_base`:**

```python
def ordenar_2opt(
    servicos: list[dict],
    base_lat: float,
    base_lng: float,
    api_key: str | None = None,
) -> list[dict]:
    """
    Sequencia uma rota usando 2-opt:
    1. Começa com a ordem farthest-first (mais longe da base primeiro) -- MANTÉM
       o primeiro ponto obrigatório como a entrega mais distante.
    2. Aplica iterações de 2-opt nas posições [1, N-1] (pontos
       intermediários): para cada par de arestas, tenta reverter o
       segmento entre elas e aceita se reduzir a distância total.
    3. Para quando não encontrar mais melhoria ou atingir MAX_ITER.

    A posição 0 (entrega mais distante) NUNCA é movida -- requisito
    de negócio: o motorista sempre sai da base indo direto pro ponto
    mais longe e vai "esvaziando" no caminho de volta.

    Para N ≤ 18 (nosso máximo), roda em <2ms por rota.
    """
    # 1. Ordenação farthest-first (mesmo que o atual)
    ordem_inicial = sorted(servicos, key=_distancia_do_servico, reverse=True)

    # 2. Montar vetor de coordenadas (incluindo base como "depósito virtual")
    coords = []
    for s in ordem_inicial:
        c = _obter_coords_servico(s, api_key)
        coords.append(c)  # None se sem coordenada

    # 3. Aplicar 2-opt nas posições [1, N-1]
    rota = list(range(len(ordem_inicial)))
    melhorou = True
    while melhorou:
        melhorou = False
        for i in range(1, len(rota) - 1):
            for j in range(i + 1, len(rota)):
                # ambos precisam de coordenada pra calcular
                if not coords[rota[i]] or not coords[rota[j]]:
                    continue
                # custo antes vs depois da reversão do segmento [i, j]
                delta = _calcular_delta_2opt(rota, coords, i, j, base_lat, base_lng)
                if delta < -0.01:  # melhoria significativa (> 10m)
                    rota[i:j+1] = reversed(rota[i:j+1])
                    melhorou = True

    return [ordem_inicial[idx] for idx in rota]
```

**Regras Obrigatórias:**
- Posição 0 (entrega mais distante) é FIXA — NUNCA movida pelo 2-opt.
- Serviço sem coordenada: permanece na posição original do farthest-first, não é candidato a swap.
- O cálculo de `_calcular_delta_2opt` usa `_distancia_km` (haversine) entre: o predecessor de $i$, $i$, $j$, e o sucessor de $j$ — comparando a soma antes e depois da reversão do segmento.

---

## 4. Especificação dos Arquivos a Criar e Alterar

### 4.1. [NOVO] `roteirizacao/otimizacao_rotas.py`

Módulo central contendo as implementações dos 3 modelos. Razão: isolar os algoritmos novos num módulo independente, sem mexer em `roteirizacao_dados.py` (que é o módulo de produção) -- permite benchmark side-by-side e rollback trivial.

```python
# -*- coding: utf-8 -*-
"""
roteirizacao/otimizacao_rotas.py

Modelos de otimização de roteirização:
  - Modelo 1: Sweep Polar (agrupar_por_sweep)
  - Modelo 2: Clarke-Wright Savings (agrupar_por_savings)
  - Modelo 3: Sequenciamento 2-Opt (ordenar_2opt)

Reaproveita funções do ecossistema existente (roteirizacao_dados.py,
geocodificacao.py) -- nunca duplica lógica de geocodificação, cache
ou cálculo de distância.

Uso standalone (benchmark):
    from otimizacao_rotas import agrupar_por_sweep, agrupar_por_savings, ordenar_2opt

Uso em produção (substituição gradual):
    Importar a função escolhida em criar_rotas_diarias.py no lugar de
    agrupar_por_regiao + consolidar_regioes_pequenas + dividir_em_sublotes.
"""
import logging
import math
from collections import defaultdict

from roteirizacao_dados import (
    obter_coordenadas, _distancia_km, extrair_cep,
    extrair_volume_caixas, extrair_nivel_dificuldade,
    NIVEL_3_TAMANHO_MAXIMO_ROTA, NIVEL_ROTA_EXCLUSIVA,
)

logger = logging.getLogger(__name__)


def agrupar_por_sweep(servicos, base_lat, base_lng,
                      tamanho_maximo=18, volume_maximo=100,
                      distancia_maxima_km=20, api_key=None):
    """Modelo 1: Sweep Polar. Ver seção 3.1 do doc."""
    # ... (implementação completa conforme seção 3.1)


def agrupar_por_savings(servicos, base_lat, base_lng,
                        tamanho_maximo=18, volume_maximo=100,
                        distancia_maxima_km=20, api_key=None):
    """Modelo 2: Clarke-Wright Savings. Ver seção 3.2 do doc."""
    # ... (implementação completa conforme seção 3.2)


def ordenar_2opt(servicos, base_lat, base_lng, api_key=None):
    """Modelo 3: Sequenciamento 2-Opt. Ver seção 3.3 do doc."""
    # ... (implementação completa conforme seção 3.3)
```

**Importações permitidas** (APENAS estas, não adicionar novas dependências):

```python
import logging
import math
from collections import defaultdict

from roteirizacao_dados import (
    obter_coordenadas, _distancia_km, extrair_cep,
    extrair_volume_caixas, extrair_nivel_dificuldade,
    NIVEL_3_TAMANHO_MAXIMO_ROTA, NIVEL_ROTA_EXCLUSIVA,
)
```

---

### 4.2. [NOVO] `roteirizacao/benchmark_modelos.py`

Script de benchmark que compara os 4 algoritmos (Atual + 3 novos) contra dados reais do VUUPT. Leitura pura (`listar_servicos`), NENHUMA escrita na API.

```python
# -*- coding: utf-8 -*-
"""
roteirizacao/benchmark_modelos.py

Benchmark comparativo dos 4 modelos de roteirização, usando dados reais
da API VUUPT (apenas leitura). Nenhuma rota é criada ou alterada.

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
from datetime import date, timedelta
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))

import yaml

from vuupt_client import VuuptClient
from geocodificacao import geocodificar
from roteirizacao_dados import (
    agrupar_por_regiao, consolidar_regioes_pequenas, dividir_em_sublotes,
    ordenar_por_distancia_base, elegivel_para_data,
    obter_coordenadas, _distancia_km, extrair_volume_caixas,
)
from otimizacao_rotas import agrupar_por_sweep, agrupar_por_savings, ordenar_2opt
from alocacao_motoristas import classificar_rota_viagem
from criar_rotas_diarias import (
    ENDERECO_BASE, TAMANHO_MINIMO_ROTA, TAMANHO_MAXIMO_ROTA,
    VOLUME_MAXIMO_ROTA, DISTANCIA_MAXIMA_ROTA_KM, DISTANCIA_MAXIMA_VIAGEM_KM,
)

# ... logging setup, _carregar_config, etc. seguindo padrão do projeto


def _km_total_rota(sublote, base_lat, base_lng, api_key=None):
    """
    Estima o KM total de UMA rota: base -> ponto1 -> ponto2 -> ... -> base.
    Serviços sem coordenada são ignorados no somatório de distância
    (não dá pra medir, sem inflar artificialmente).
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


def _metricas_modelo(sublotes, base_lat, base_lng, api_key=None):
    """Calcula as métricas de um conjunto de sublotes/rotas."""
    total_rotas = len(sublotes)
    total_entregas = sum(len(s) for s in sublotes)
    total_caixas = sum(sum(extrair_volume_caixas(e) for e in s) for s in sublotes)
    total_km = sum(_km_total_rota(s, base_lat, base_lng, api_key) for s in sublotes)
    media_entregas = total_entregas / total_rotas if total_rotas else 0
    media_caixas = total_caixas / total_rotas if total_rotas else 0
    media_km = total_km / total_rotas if total_rotas else 0
    return {
        "rotas": total_rotas,
        "entregas": total_entregas,
        "caixas": total_caixas,
        "km_total": total_km,
        "media_entregas": media_entregas,
        "media_caixas": media_caixas,
        "media_km": media_km,
    }


def main():
    # 1. Carregar config, token, gmaps_key
    # 2. Buscar serviços not_assigned via API (apenas leitura)
    # 3. Filtrar elegíveis para a data alvo
    # 4. Geocodificar a base (ENDERECO_BASE)
    # 5. Executar cada modelo:

    #    a) MODELO ATUAL: agrupar_por_regiao -> consolidar -> dividir_em_sublotes
    #       -> ordenar_por_distancia_base (sequenciamento atual)

    #    b) MODELO 1 (SWEEP): agrupar_por_sweep (JÁ inclui divisão em sublotes)
    #       -> ordenar_por_distancia_base (sequenciamento atual, pra comparar justo)

    #    c) MODELO 2 (SAVINGS): agrupar_por_savings (JÁ inclui divisão)
    #       -> ordenar_por_distancia_base

    #    d) MODELO 3 (2-OPT): agrupar usando modelo ATUAL
    #       -> ordenar_2opt (novo sequenciamento, em vez de ordenar_por_distancia_base)

    # 6. Calcular métricas com _metricas_modelo
    # 7. Imprimir tabela comparativa formatada
    # 8. Salvar em roteirizacao/dados/benchmark_resultado.txt
```

**Argumentos CLI:**

```bash
py -3.11 roteirizacao/benchmark_modelos.py                     # usa pedidos atuais
py -3.11 roteirizacao/benchmark_modelos.py --data 2026-08-11   # simula para data específica
```

**Formato da Saída (tabela no terminal):**

```
╔══════════════════════╦════════╦══════════╦════════╦═══════════╦══════════════╦═══════════════╗
║ Modelo               ║ Rotas  ║ Entregas ║ Caixas ║ KM Total  ║ Média Entr.  ║ Média KM/Rota ║
╠══════════════════════╬════════╬══════════╬════════╬═══════════╬══════════════╬═══════════════╣
║ Atual (Grade+Greedy) ║   12   ║   180    ║  850   ║   320.4   ║     15.0     ║     26.7      ║
║ Sweep Polar          ║   11   ║   180    ║  850   ║   278.1   ║     16.4     ║     25.3      ║
║ Clarke-Wright        ║   10   ║   180    ║  850   ║   285.2   ║     18.0     ║     28.5      ║
║ Atual + 2-Opt        ║   12   ║   180    ║  850   ║   290.7   ║     15.0     ║     24.2      ║
╚══════════════════════╩════════╩══════════╩════════╩═══════════╩══════════════╩═══════════════╝
```

---

### 4.3. [NÃO ALTERAR] `roteirizacao_dados.py`, `criar_rotas_diarias.py`, `incrementar_rotas.py`

> [!CAUTION]
> Os arquivos de produção NÃO devem ser alterados nesta fase. Os novos modelos ficam em `otimizacao_rotas.py` (módulo isolado) e o benchmark roda de forma independente, apenas leitura. A substituição em produção será decidida DEPOIS de analisar os resultados do benchmark.

---

## 5. Regras de Implementação Obrigatórias

1. **NUNCA duplicar funções utilitárias** (coordenadas, distância, CEP, volume, nível de dificuldade). Sempre importar de `roteirizacao_dados.py`.
2. **RESPEITAR as 4 travas em TODOS os modelos** — os limites de 18 entregas, 100 caixas, distância máxima entre pares e nível de dificuldade (3=máx 4 entregas, 4=rota exclusiva) são OBRIGATÓRIOS e não negociáveis.
3. **Diferenciar distância por tipo de rota:** se `classificar_rota_viagem(sublote) == True`, usar `DISTANCIA_MAXIMA_VIAGEM_KM` (None = sem limite). Se False, usar `DISTANCIA_MAXIMA_ROTA_KM` (20 km).
4. **Padrão de logs e encoding:** seguir o mesmo padrão de `logging.basicConfig` com `FileHandler(encoding="utf-8")` e `sys.stdout.reconfigure(encoding="utf-8")` usado nos outros scripts do módulo.
5. **Zero dependências novas:** usar apenas `math`, `logging`, `collections.defaultdict` e as funções já importadas de `roteirizacao_dados.py` e `geocodificacao.py`. Não introduzir `numpy`, `scipy`, `scikit-learn` ou qualquer pacote externo.

---

## 6. Plano de Validação e Testes

### 6.1. Teste do Benchmark (Validação Principal)

```bash
py -3.11 roteirizacao/benchmark_modelos.py
```

**Critérios de Sucesso:**
- Executa sem erros com Python 3.11.
- Imprime tabela comparativa com os 4 modelos.
- O número total de entregas e caixas é IDÊNTICO entre os 4 modelos (todos roteirizam EXATAMENTE os mesmos pedidos — a diferença é SÓ no agrupamento/sequenciamento).
- Nenhum sublote de NENHUM modelo excede as 4 travas (verificar via assertions no script).

### 6.2. Teste de Integridade das Travas

Adicionar no final de cada função de agrupamento/sequenciamento:

```python
# Assertions de segurança -- devem ser True pra TODOS os sublotes gerados
for sublote in sublotes:
    assert len(sublote) <= tamanho_maximo, f"Sublote excede {tamanho_maximo} entregas"
    caixas = sum(extrair_volume_caixas(s) for s in sublote)
    assert caixas <= volume_maximo, f"Sublote excede {volume_maximo} caixas"
```

### 6.3. Teste de Cobertura de Pedidos

```python
# No benchmark: garantir que TODOS os pedidos apareceram em algum sublote
ids_originais = {s["id"] for s in servicos}
ids_alocados = {s["id"] for sublote in sublotes for s in sublote}
assert ids_originais == ids_alocados, "Pedidos perdidos na otimização"
```

### 6.4. Comparação Numérica

Após rodar o benchmark, validar nos resultados:
- Modelo 1 (Sweep): espera-se **redução de 10-18% no KM total** vs. Atual.
- Modelo 2 (Savings): espera-se **redução de 10-20% no nº de rotas** (maior preenchimento).
- Modelo 3 (2-Opt): espera-se **redução de 5-15% no KM total** vs. mesmo agrupamento + farthest-first.

---

## 7. Critérios de Aceitação

- [ ] Arquivo `roteirizacao/otimizacao_rotas.py` criado com os 3 modelos funcionais.
- [ ] Arquivo `roteirizacao/benchmark_modelos.py` criado e executável.
- [ ] `py -3.11 roteirizacao/benchmark_modelos.py` roda sem erros e imprime tabela comparativa.
- [ ] Assertions de integridade (travas, cobertura de pedidos) passam em todos os modelos.
- [ ] NENHUM arquivo de produção (`roteirizacao_dados.py`, `criar_rotas_diarias.py`, `incrementar_rotas.py`) foi alterado.
- [ ] Zero dependências externas novas (sem pip install de nada).

---

## 8. Janela de horário de entrega do cliente (implementado 09/09)

Até 09/09 a roteirização só respeitava a **data** do agendamento (`elegivel_para_data`); a **hora** era descartada em todo ponto do cálculo. Pedido do Hugo: passar a considerar o horário, "tanto dos e-mails quanto no algoritmo".

### 8.1. Fonte da janela de cada pedido (`roteirizacao_dados.resolver_janela` / `injetar_janelas`)

Prioridade, por pedido (chaves injetadas `_janela_inicio`, `_janela_fim`, `_janela_fonte`):

1. **Agendamento confirmado com hora** em `agendamentos_pedido` (resposta do embarcador por e-mail, portal do cliente) — `carregar_janelas_confirmadas`. Hora **não informada** fica `NULL` desde 09/09 (antes virava o chute 08:00-18:00).
2. **`scheduled_start`/`scheduled_end`** do serviço na Vuupt quando a hora é "real" — pares padrão que o pipeline grava sem ninguém informar hora (`08:00-18:00`, `08:00-16:00`, `00:00-23:59`) são ignorados (`JANELAS_PADRAO_IGNORADAS`). `scheduled_start == scheduled_end` ("às 11h") vira janela de 1h.
3. **Horário de atendimento** do cadastro (`BD_CLIENTES.xlsx` / ajuste manual da tela de Planejamento, já injetado em `_horario_atendimento_*`; `customer.operating_hour_*` da Vuupt como reserva). `00:00-23:59` = sem janela.

Agendamento **e** cadastro: vale a interseção; interseção vazia → vale o agendamento (fonte `agendamento`).

### 8.2. Onde a janela entra no algoritmo

| Ponto | Função | Regra |
|---|---|---|
| Simulação | `simular_horarios(sublote)` | Linha do tempo na ordem dada: saída da base às `HORA_SAIDA_BASE` (= `HORA_INICIO_ROTA`, **06:00 BRT desde 09/09** — antes 10:00; é a mesma constante que gera o `start_at` das rotas via `start_at_rota`, "T09:00:00Z"; `config.yaml` → `roteirizacao.hora_saida_base` sobrepõe só o simulador), mesmas pernas/tempos de parada de `estimar_tempo_rota`; chegar antes da janela → **espera**, depois → **atraso**. |
| Sequenciamento | `ordenar_com_janelas` (chamada por `otimizacao_rotas.ordenar_2opt`) | Sem janela em nenhum pedido: 2-opt de sempre, byte a byte. Com janela: objetivo `km + 60·atraso_h + 30·espera_h`, 2-opt + realocação de parada (or-opt); posição 0 (mais distante) fixa numa 1ª passada e liberada numa 2ª só se ainda sobrar atraso/espera acima da tolerância (15 min). |
| Formação/fusão | `janela_viavel(candidato)` em `dividir_em_sublotes`, `_empacotar_ganancioso` (Sweep/CEP/K-means), `_fusao_valida` (Clarke-Wright), `fundir_sublotes_pequenos` | Sequencia o candidato com janelas e exige atraso evitável ≤ 15 min e duração **com esperas** ≤ 9h. Custo zero quando nenhum pedido do candidato tem janela. |
| Ordem final | `janela_respeitada` em `reparar_sublotes_por_horas` e `selecao_modelo._validar` | Mesma regra na ordem final; rota que falha é quebrada em pedaços que respeitam. |
| Incremento por hora | `incrementar_rotas.py` | Rota candidata precisa continuar viável com o pedido novo (`_cabe_na_janela`); resequenciamento passou a usar `ordenar_2opt` (era farthest-first puro). |
| Tela de Planejamento | `planejamento_rotas._simular_rascunho` | Badge "fora da janela de horário: …" no card da rota, chegada prevista por parada e selo "⏱ agendado/atende HH–HH" no pedido; botão "Otimizar sequência" respeita a janela. |

**Atraso intrínseco** (`_atraso_intrinseco`): pedido que, sozinho e saindo direto da base, já chega depois da própria janela (ex.: cliente que recebe só até 10h com saída às 10h) **não bloqueia** agrupamento nem reprova candidato — fragmentar não resolve. Mesmo princípio de `_orcamento_inviavel_por_distancia`. Aparece como aviso na tela.

### 8.3. Lado dos e-mails (a hora precisa chegar inteira ao banco)

- `ler_respostas_agendamento.py`: a IA extrai a data e, **se informado**, o horário; hora não informada fica `NULL` (não descarta mais a resposta inteira por falta de hora). Responde também ao e-mail **URGENTE** de vários pedidos (assunto "Confirmação de agendamento", marcadores `[[PEDIDO:…]]` por pedido, prompt de lista).
- `roteirizacao/notificar_agendamento_pendente.py`: o e-mail urgente passou a levar os marcadores ocultos, o pedido explícito de "data e horário" e a registrar cada pedido como `PENDENTE` em `agendamentos_pedido`.
- `ler_planilha_entregas_nuu.py`: `extrair_hora_agenda` lê hora do texto da coluna AGENDA ("às 14h", "entre 8h e 12h", "até 11h", "manhã"…); sem hora → `NULL` (migração idempotente zera o antigo chute 08:00-18:00 de origem `PLANILHA_NUU`).
- `regras/endereco.py`: `horario_entrega` do LLM normalizado pra `HH:MM` e mantido mesmo quando o endereço da mesma mensagem é rejeitado.

### 8.4. Validação

- `py -3.11 -m unittest roteirizacao.test_janelas_horario test_janela_horario_email roteirizacao.test_orcamento_horas` (44 testes).
- Dados reais de 09/09 (92 not_assigned, 35 com janela): 18 rotas nas duas rodadas, 2105 → 2098 km, 18 s → 28 s; zero atraso evitável — as 5 paradas "fora" restantes são intrínsecas (janela que fecha às 10h/11h com saída às 10h) ou dentro dos 15 min de tolerância.
