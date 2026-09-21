# Recalibração da roteirização — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rotas mais compactas e sem sobreposição: partição única por dia (Seco+Frio juntos), vencedor por km total, sequência livre (vizinho mais próximo + 2-opt/or-opt sem volta à base), polimento entre rotas e um replay de 30 dias pra calibrar a distância máxima.

**Architecture:** Tudo continua em `roteirizacao/` (Python puro, haversine, sem serviço externo). Dois módulos novos e pequenos (`metricas_plano.py`, `polimento_rotas.py`) e um script de replay (`replay_rotas.py`) que lê `rascunhos_rota`/`rascunhos_parada` de uma cópia do banco de produção e roda o mesmo `planejar_sublotes` que a produção usa. As mudanças em código existente são cirúrgicas: `_particionar_carga_com_fusao`, `escolher_melhor_modelo`, `ordenar_com_janelas`, `calcular_km_estimado`, `obter_coordenadas`.

**Tech Stack:** Python 3.11 (`py -3.11`), `unittest` (sem pytest), SQLite, `unittest.mock`. Sem dependência nova.

**Spec:** `docs/superpowers/specs/2026-09-18-recalibracao-roteirizacao-design.md`

## Global Constraints

- Rodar tudo com `py -3.11` (nunca `python`/`py` sem versão). Testes: `py -3.11 -m unittest <modulo> -v` da raiz do repo.
- Depois de cada Edit/Write em `.py`: `py -3.11 -m py_compile <arquivo>` (o hook já faz; se falhar, corrigir antes de seguir).
- **NÃO commitar nem dar push** em nenhum passo: o CLAUDE.md do projeto manda commitar só quando o Hugo pedir. Cada task termina num "checkpoint" (compile + testes verdes) e lista os arquivos que entrariam no commit. Nunca `git add -A`; nunca tocar em arquivos modificados por outra sessão (ver `git status --short` antes de editar).
- Idioma: português em código, comentários, docstrings, logs e testes. Comentários e strings de log **sem acento** em arquivo que já segue esse padrão (ver o arquivo antes).
- Nenhum teste pode geocodificar, chamar Vuupt, Google ou tocar em `dados/dados.db`. Padrão: `mock.patch.object(rd, "obter_coordenadas", ...)` e coordenadas embutidas nos dicts (`latitude`/`longitude`).
- Não mexer em: `incrementar_rotas.py`, orçamento de horas (`estimar_tempo_rota`, velocidades, `FATOR_ESTRADA`), `benchmark_modelos.py`, `otimizacao_client.py`.
- `config.yaml` é segredo: nunca ler valores dele pro chat, nunca commitar.
- Constantes de roteirização continuam constantes Python (nada em config/banco).
- O valor final de `DISTANCIA_MAXIMA_ROTA_KM` é escolha do Hugo a partir da tabela do replay (Task 8). Até lá fica 20.

---

## Mapa de arquivos

| Arquivo | Ação | Responsabilidade |
|---|---|---|
| `roteirizacao/metricas_plano.py` | criar | Métricas geométricas de um plano (puro, sem I/O) |
| `roteirizacao/test_metricas_plano.py` | criar | Testes das métricas |
| `roteirizacao/roteirizacao_dados.py` | modificar | `obter_coordenadas` prefere lat/lng embutidos; `ordenar_com_janelas` livre e sem volta; `calcular_km_estimado` sem volta; docstrings |
| `roteirizacao/test_coordenadas_embutidas.py` | criar | Teste do `obter_coordenadas` |
| `roteirizacao/test_sequencia_livre.py` | criar | Testes da sequência nova |
| `roteirizacao/test_janelas_horario.py` | modificar | 1 teste reescrito (assumia farthest-first) |
| `roteirizacao/otimizacao_rotas.py` | modificar | docstring de `ordenar_2opt`; `_limite_distancia` ganha alias público `limite_distancia` |
| `roteirizacao/selecao_modelo.py` | modificar | `_escolher_vencedor` (km primeiro), `registrar_historico` opcional, docstrings |
| `roteirizacao/test_selecao_modelo.py` | criar | Critério + histórico opcional |
| `roteirizacao/criar_rotas_diarias.py` | modificar | `SEPARAR_POR_TIPO_CARGA`, `rotulo_carga`, `POLIMENTO_ATIVO`, `planejar_sublotes` (extraído), `main` e `roteirizar_para_rascunhos` usam ele |
| `roteirizacao/test_particao_carga.py` | criar | Partição única x tripla, rótulo |
| `roteirizacao/polimento_rotas.py` | criar | Busca local entre rotas |
| `roteirizacao/test_polimento_rotas.py` | criar | Testes do polimento |
| `roteirizacao/test_planejar_sublotes.py` | criar | Integração leve do pipeline extraído |
| `roteirizacao/replay_rotas.py` | criar | Replay de dias passados a partir do banco |
| `roteirizacao/test_replay_rotas.py` | criar | Leitura do banco -> dicts de serviço |
| `painel_agentes/laboratorio_rotas.py` | modificar | Partição "Todos" (padrão) |
| `painel_agentes/templates/laboratorio_rotas.html` | modificar | Opção "Todos" |
| `painel_agentes/painel_agentes.py` | modificar (1 linha) | Padrão do `request.args.get("particao")` |
| `painel_agentes/test_laboratorio_todos.py` | criar | Filtro de partição |
| `DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md` | modificar | Adendo §9 |
| `.gitignore` | verificar | `dados/*` já ignora `dados/dados_replay.db` (não precisa mexer) |

---

### Task 1: Métricas de plano (`metricas_plano.py`)

**Files:**
- Create: `roteirizacao/metricas_plano.py`
- Test: `roteirizacao/test_metricas_plano.py`

**Interfaces:**
- Produces:
  - `metricas_plano(rotas: list[list[tuple[float, float]]], base: tuple[float, float], horas: list[float] | None = None, teto_horas: float = 9.0) -> dict` com as chaves `rotas, paradas, media_paradas, min_paradas, max_paradas, rotas_pequenas, diametro_mediano_km, diametro_max_km, raio_medio_km, km_total, cruzadas, cruzadas_pct, pares_cruzados, rotas_acima_teto, rotas_sem_coordenada` (15 chaves; `rotas_sem_coordenada` entrou no fix round 1 de 18/09 — rota cujas paradas ficaram todas sem coordenada é descartada da medição e precisa ser sinalizada, não sumir em silêncio; `rotas` e `horas` são filtradas pelos mesmos índices).
  - `plano_de_sublotes(sublotes: list[list[dict]], coords_fn) -> list[list[tuple[float, float]]]` (descarta parada sem coordenada).
  - `formatar_metricas(m: dict, titulo: str = "") -> str` (uma linha).
- Consumes: nada do projeto (módulo puro).

- [ ] **Step 1: Escrever os testes (falham por import)**

Criar `roteirizacao/test_metricas_plano.py`:

```python
# -*- coding: utf-8 -*-
"""Testes de metricas_plano (metricas geometricas de um plano de rotas).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_metricas_plano -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import metricas_plano as mp

BASE = (0.0, 0.0)
GRAU_KM = 111.0


class MetricasTestCase(unittest.TestCase):

    def test_plano_vazio(self):
        m = mp.metricas_plano([], BASE)
        self.assertEqual(m["rotas"], 0)
        self.assertEqual(m["paradas"], 0)
        self.assertEqual(m["km_total"], 0.0)
        self.assertEqual(m["cruzadas_pct"], 0.0)

    def test_km_total_sem_volta(self):
        # 1 rota, 1 parada a 0,1 grau ao norte: so a perna base -> parada
        m = mp.metricas_plano([[(0.1, 0.0)]], BASE)
        self.assertAlmostEqual(m["km_total"], 0.1 * GRAU_KM, delta=0.2)
        self.assertEqual(m["paradas"], 1)
        self.assertEqual(m["rotas_pequenas"], 1)

    def test_duas_rotas_separadas_nao_cruzam(self):
        a = [(0.10, 0.00), (0.11, 0.00), (0.12, 0.00)]
        b = [(0.10, 0.50), (0.11, 0.50), (0.12, 0.50)]
        m = mp.metricas_plano([a, b], BASE)
        self.assertEqual(m["cruzadas"], 0)
        self.assertEqual(m["pares_cruzados"], 0)
        self.assertAlmostEqual(m["diametro_max_km"], 0.02 * GRAU_KM, delta=0.1)
        self.assertEqual(m["media_paradas"], 3.0)

    def test_duas_rotas_entrelacadas_cruzam(self):
        # pontos alternados na mesma reta: a vizinha mais proxima de cada
        # parada esta sempre na OUTRA rota
        a = [(0.10, 0.0), (0.12, 0.0), (0.14, 0.0)]
        b = [(0.11, 0.0), (0.13, 0.0), (0.15, 0.0)]
        m = mp.metricas_plano([a, b], BASE)
        self.assertEqual(m["cruzadas"], 6)
        self.assertEqual(m["cruzadas_pct"], 100.0)
        self.assertEqual(m["pares_cruzados"], 1)

    def test_rotas_acima_do_teto_de_horas(self):
        m = mp.metricas_plano([[(0.1, 0.0)], [(0.2, 0.0)]], BASE, horas=[8.5, 9.4], teto_horas=9.0)
        self.assertEqual(m["rotas_acima_teto"], 1)

    def test_plano_de_sublotes_descarta_sem_coordenada(self):
        sub = [{"latitude": 0.1, "longitude": 0.0}, {"latitude": None, "longitude": None}]
        plano = mp.plano_de_sublotes([sub], lambda s: (s["latitude"], s["longitude"]) if s["latitude"] is not None else None)
        self.assertEqual(plano, [[(0.1, 0.0)]])

    def test_formatar_metricas_uma_linha(self):
        m = mp.metricas_plano([[(0.1, 0.0)]], BASE)
        linha = mp.formatar_metricas(m, "teste")
        self.assertIn("teste", linha)
        self.assertNotIn("\n", linha)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_metricas_plano -v`
Expected: `ModuleNotFoundError: No module named 'metricas_plano'`

- [ ] **Step 3: Implementar `roteirizacao/metricas_plano.py`**

```python
# -*- coding: utf-8 -*-
"""
metricas_plano.py

Metricas geometricas de um plano de rotas (recalibracao de 18/09, spec
docs/superpowers/specs/2026-09-18-recalibracao-roteirizacao-design.md).
Modulo PURO: recebe listas de coordenadas, nao le banco, nao geocodifica.
Usado pelo replay (replay_rotas.py) e pelos testes; serve pra medir
"espalhamento" (diametro/raio), "sobreposicao" (parada cuja vizinha mais
proxima esta em outra rota; pares de rotas cujas bolhas se cruzam) e
tamanho das rotas.

Todas as distancias sao haversine em km, e o km total e o do trajeto
base -> p1 -> ... -> pN, SEM volta a base (mesma convencao de
roteirizacao_dados.calcular_km_estimado a partir de 18/09).
"""
import itertools
import math

PARADAS_ROTA_PEQUENA = 6      # rota com ate este numero de paradas conta como "pequena"
FATOR_CRUZAMENTO_BOLHAS = 0.8  # bolhas se cruzam se centroides < 0,8 x (raio_a + raio_b)


def _hav(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6371.0
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(d))


def _km_rota(pts: list[tuple[float, float]], base: tuple[float, float]) -> float:
    if not pts:
        return 0.0
    total = _hav(base, pts[0])
    for a, b in zip(pts, pts[1:]):
        total += _hav(a, b)
    return total


def _centroide(pts: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def plano_de_sublotes(sublotes: list[list[dict]], coords_fn) -> list[list[tuple[float, float]]]:
    """Converte sublotes (dicts de servico) em listas de coordenadas na
    ordem dada; parada sem coordenada e descartada da medicao."""
    plano = []
    for sub in sublotes:
        pts = [c for c in (coords_fn(s) for s in sub) if c]
        plano.append(pts)
    return plano


def metricas_plano(rotas: list[list[tuple[float, float]]], base: tuple[float, float],
                   horas: list[float] | None = None, teto_horas: float = 9.0) -> dict:
    """Ver docstring do modulo. `horas` (opcional): duracao estimada por
    rota, na mesma ordem de `rotas`, pra contar rotas acima do teto."""
    rotas = [list(r) for r in rotas if r]
    n_rotas = len(rotas)
    tamanhos = [len(r) for r in rotas]
    paradas = sum(tamanhos)
    vazio = {
        "rotas": n_rotas, "paradas": paradas, "media_paradas": 0.0, "min_paradas": 0, "max_paradas": 0,
        "rotas_pequenas": 0, "diametro_mediano_km": 0.0, "diametro_max_km": 0.0, "raio_medio_km": 0.0,
        "km_total": 0.0, "cruzadas": 0, "cruzadas_pct": 0.0, "pares_cruzados": 0, "rotas_acima_teto": 0,
    }
    if not rotas:
        return vazio

    diametros, raios, centroides = [], [], []
    for pts in rotas:
        diametros.append(max((_hav(a, b) for a, b in itertools.combinations(pts, 2)), default=0.0))
        c = _centroide(pts)
        centroides.append(c)
        raios.append(max(_hav(c, p) for p in pts))

    todas = [(p, i) for i, pts in enumerate(rotas) for p in pts]
    cruzadas = 0
    if len(todas) >= 2:
        for k, (p, i) in enumerate(todas):
            melhor = None
            for m, (q, j) in enumerate(todas):
                if m == k:
                    continue
                d = _hav(p, q)
                if melhor is None or d < melhor[0]:
                    melhor = (d, j)
            if melhor is not None and melhor[1] != i:
                cruzadas += 1

    pares = 0
    for a, b in itertools.combinations(range(n_rotas), 2):
        if _hav(centroides[a], centroides[b]) < (raios[a] + raios[b]) * FATOR_CRUZAMENTO_BOLHAS:
            pares += 1

    ordenados = sorted(diametros)
    acima = sum(1 for h in (horas or []) if h > teto_horas)
    return {
        "rotas": n_rotas,
        "paradas": paradas,
        "media_paradas": paradas / n_rotas,
        "min_paradas": min(tamanhos),
        "max_paradas": max(tamanhos),
        "rotas_pequenas": sum(1 for t in tamanhos if t <= PARADAS_ROTA_PEQUENA),
        "diametro_mediano_km": ordenados[len(ordenados) // 2],
        "diametro_max_km": ordenados[-1],
        "raio_medio_km": sum(raios) / n_rotas,
        "km_total": sum(_km_rota(pts, base) for pts in rotas),
        "cruzadas": cruzadas,
        "cruzadas_pct": (cruzadas / paradas * 100.0) if paradas else 0.0,
        "pares_cruzados": pares,
        "rotas_acima_teto": acima,
    }


def formatar_metricas(m: dict, titulo: str = "") -> str:
    """Uma linha, pra tabela do replay e pro log."""
    prefixo = f"{titulo}: " if titulo else ""
    return (f"{prefixo}{m['rotas']} rotas | {m['paradas']} paradas | media {m['media_paradas']:.1f} "
            f"(min {m['min_paradas']}, max {m['max_paradas']}) | pequenas {m['rotas_pequenas']} | "
            f"diam med {m['diametro_mediano_km']:.1f} km (max {m['diametro_max_km']:.1f}) | "
            f"raio med {m['raio_medio_km']:.1f} km | km {m['km_total']:.0f} | "
            f"cruzadas {m['cruzadas']} ({m['cruzadas_pct']:.0f}%) | pares {m['pares_cruzados']} | "
            f"acima do teto {m['rotas_acima_teto']}")
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest roteirizacao.test_metricas_plano -v`
Expected: 7 testes OK.

- [ ] **Step 5: Checkpoint (sem commit)**

`py -3.11 -m py_compile roteirizacao/metricas_plano.py roteirizacao/test_metricas_plano.py`. Arquivos deste task: `roteirizacao/metricas_plano.py`, `roteirizacao/test_metricas_plano.py`.

---

### Task 2: `obter_coordenadas` prefere latitude/longitude embutidas

Sem isso os agrupadores (`agrupar_por_savings`, `agrupar_por_regiao`, `separar_pedidos_exclusivos`...) só enxergam coordenada via geocodificação do `address`, e o replay (Task 8) não roda sem chave do Google. `coords_do_servico` e `_distancia_da_base` já preferem a coordenada embutida; isto unifica.

**Files:**
- Modify: `roteirizacao/roteirizacao_dados.py:311-329` (`obter_coordenadas`), `:1104-1113` (`coords_do_servico`)
- Test: `roteirizacao/test_coordenadas_embutidas.py`

**Interfaces:**
- Produces: `obter_coordenadas(servico, api_key)` devolve `(float(lat), float(lng))` quando o dict traz `latitude`/`longitude` numéricos e diferentes de `(0, 0)`; senão o comportamento de hoje (geocodifica `address`).

- [ ] **Step 1: Escrever o teste**

Criar `roteirizacao/test_coordenadas_embutidas.py`:

```python
# -*- coding: utf-8 -*-
"""obter_coordenadas prefere latitude/longitude embutidas no dict (18/09):
sem isso os agrupadores so enxergam coordenada via geocodificacao do
endereco, e o replay nao roda sem chave do Google.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_coordenadas_embutidas -v"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd


class CoordenadasEmbutidasTestCase(unittest.TestCase):

    def setUp(self):
        rd._cache_coordenadas.clear()

    def test_usa_lat_lng_do_dict_sem_geocodificar(self):
        with mock.patch.object(rd, "geocodificar", side_effect=AssertionError("nao deveria geocodificar")):
            self.assertEqual(rd.obter_coordenadas({"latitude": "-23.5", "longitude": -46.6}, "chave"), (-23.5, -46.6))
            self.assertEqual(rd.obter_coordenadas({"latitude": -23.5, "longitude": -46.6, "address": "Rua X"}, None), (-23.5, -46.6))

    def test_sem_lat_lng_cai_na_geocodificacao(self):
        with mock.patch.object(rd, "geocodificar", return_value=(-1.0, -2.0)) as geo:
            self.assertEqual(rd.obter_coordenadas({"latitude": "", "longitude": None, "address": "Rua X"}, "k"), (-1.0, -2.0))
            geo.assert_called_once_with("Rua X", "k")

    def test_zero_zero_nao_conta_como_coordenada(self):
        with mock.patch.object(rd, "geocodificar", return_value=(-1.0, -2.0)):
            self.assertEqual(rd.obter_coordenadas({"latitude": 0, "longitude": "0", "address": "Rua X"}, "k"), (-1.0, -2.0))

    def test_lat_invalida_cai_na_geocodificacao(self):
        with mock.patch.object(rd, "geocodificar", return_value=(-1.0, -2.0)):
            self.assertEqual(rd.obter_coordenadas({"latitude": "abc", "longitude": "1", "address": "Rua X"}, "k"), (-1.0, -2.0))

    def test_sem_endereco_nem_coordenada(self):
        self.assertIsNone(rd.obter_coordenadas({}, "k"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_coordenadas_embutidas -v`
Expected: `test_usa_lat_lng_do_dict_sem_geocodificar` falha (AssertionError "nao deveria geocodificar" ou retorno None); `test_zero_zero...` pode passar por acaso.

- [ ] **Step 3: Implementar**

Em `roteirizacao_dados.py`, substituir `obter_coordenadas` (linhas 311-329) por:

```python
def coordenada_embutida(servico: dict) -> tuple[float, float] | None:
    """(lat, lng) das chaves latitude/longitude do proprio dict (servico
    vindo de rota existente, rascunho ou replay), ou None se ausentes,
    nao numericas ou (0, 0) -- placeholder que a Vuupt/geocache usam
    pra "sem coordenada"."""
    lat, lng = servico.get("latitude"), servico.get("longitude")
    if lat in (None, "") or lng in (None, ""):
        return None
    try:
        par = (float(lat), float(lng))
    except (TypeError, ValueError):
        return None
    return None if par == (0.0, 0.0) else par


def obter_coordenadas(servico: dict, api_key: str | None) -> tuple[float, float] | None:
    """
    Coordenada do servico: primeiro a EMBUTIDA no dict (latitude/
    longitude -- desde 18/09, mesma preferencia que coords_do_servico e
    otimizacao_rotas._distancia_da_base ja tinham; assim agrupadores,
    sequenciador e replay enxergam a mesma coordenada), senao geocodifica
    o 'address' reaproveitando o MESMO cache de geocodificacao usado pelo
    pipeline de importacao (geocodificacao.py, tabela geocache em
    dados/dados.db) -- a imensa maioria dos pedidos not_assigned ja foi
    geocodificada por la, isso normalmente e um cache hit, sem chamada
    nova no Google Maps. Retorna None se nao houver endereco, chave de
    API, ou coordenada (cache miss + falha do Google).
    """
    embutida = coordenada_embutida(servico)
    if embutida:
        return embutida
    endereco = servico.get("address")
    if not endereco:
        return None
    chave = (endereco, api_key or "")
    if chave in _cache_coordenadas:
        return _cache_coordenadas[chave]
    resultado = geocodificar(endereco, api_key or "")
    _cache_coordenadas[chave] = resultado
    return resultado
```

E simplificar `coords_do_servico` (linhas 1104-1113) para:

```python
def coords_do_servico(servico: dict, api_key: str | None = None) -> tuple[float, float] | None:
    """Mantida pelo nome (sequenciador, laboratorio, benchmark): desde
    18/09 obter_coordenadas ja prefere a coordenada embutida."""
    return obter_coordenadas(servico, api_key)
```

- [ ] **Step 4: Rodar os testes novos e os que dependem de coordenadas**

Run: `py -3.11 -m unittest roteirizacao.test_coordenadas_embutidas roteirizacao.test_orcamento_horas roteirizacao.test_janelas_horario roteirizacao.test_nivel4_veiculo_grande roteirizacao.test_incrementar_rotas -v`
Expected: tudo OK (os testes existentes já mockam `obter_coordenadas` pra ler o dict, então continuam iguais).

- [ ] **Step 5: Checkpoint (sem commit)**

`py -3.11 -m py_compile roteirizacao/roteirizacao_dados.py`. Arquivos: `roteirizacao/roteirizacao_dados.py`, `roteirizacao/test_coordenadas_embutidas.py`.

---

### Task 3: Sequência livre (vizinho mais próximo + 2-opt/or-opt sem volta à base)

**Files:**
- Modify: `roteirizacao/roteirizacao_dados.py:342-359` (`calcular_km_estimado`), `:1203-1333` (`ordenar_com_janelas`), `:1760-1790` (docstring de `ordenar_por_distancia_base`), `:780-797` (comentário de `_orcamento_inviavel_por_distancia`)
- Modify: `roteirizacao/otimizacao_rotas.py:321-345` (docstring de `ordenar_2opt`)
- Modify: `roteirizacao/test_janelas_horario.py:178-190`
- Test: `roteirizacao/test_sequencia_livre.py`

**Interfaces:**
- Produces: `ordenar_com_janelas(servicos, base_lat, base_lng, api_key=None, coords_fn=None) -> list[dict]` (mesma assinatura; semântica nova: semente vizinho mais próximo, objetivo sem volta, 1ª parada livre). `calcular_km_estimado(sublote, base_lat, base_lng, api_key) -> float` sem a perna de volta.
- Consumes: `coords_do_servico`/`obter_coordenadas` da Task 2.

- [ ] **Step 1: Escrever os testes novos**

Criar `roteirizacao/test_sequencia_livre.py`:

```python
# -*- coding: utf-8 -*-
"""
Sequencia livre (Hugo, 18/09): semente vizinho mais proximo saindo da
base, objetivo = km ate a ULTIMA parada (sem volta a base), 2-opt e
or-opt podem mover qualquer posicao, inclusive a primeira. Revoga a
regra "mais longe primeiro" de 03/08.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_sequencia_livre -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import otimizacao_rotas as ot

BASE = (0.0, 0.0)


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, lat, lng, janela=None, nivel=1):
    s = {"id": i, "code": f"PS-{i}", "address": f"P{i}", "_nivel_dificuldade": nivel,
         "latitude": lat, "longitude": lng, "dimension_3": 1}
    if janela:
        s["_janela_inicio"], s["_janela_fim"], s["_janela_fonte"] = janela[0], janela[1], "teste"
    return s


def _km_sem_volta(seq):
    pts = [BASE] + [(s["latitude"], s["longitude"]) for s in seq]
    return sum(rd._distancia_km(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))


class SequenciaLivreTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        rd.COORDS_BASE = BASE

    def test_semente_vizinho_mais_proximo_numa_reta(self):
        rota = [_servico(1, 0.05, 0.0), _servico(2, 0.30, 0.0), _servico(3, 0.10, 0.0),
                _servico(4, 0.20, 0.0), _servico(5, 0.02, 0.0)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertEqual([s["id"] for s in ordem], [5, 1, 3, 4, 2])
        # sem volta: km = distancia ate a mais longe (0,30 grau ~ 33 km)
        self.assertAlmostEqual(_km_sem_volta(ordem), rd._distancia_km(*BASE, 0.30, 0.0), places=6)

    def test_objetivo_sem_volta_termina_longe(self):
        # A e B coladas a ~11 km; C a ~33 km. Sem volta, o melhor e
        # A -> B -> C (termina longe); com a volta ficticia era empate.
        rota = [_servico(1, 0.30, 0.0), _servico(2, 0.10, 0.0), _servico(3, 0.10, 0.01)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertEqual(ordem[-1]["id"], 1)
        self.assertAlmostEqual(_km_sem_volta(ordem), rd._distancia_km(*BASE, 0.10, 0.0)
                               + rd._distancia_km(0.10, 0.0, 0.10, 0.01)
                               + rd._distancia_km(0.10, 0.01, 0.30, 0.0), places=6)

    def test_primeira_parada_pode_mudar(self):
        # semente NN: P1 (mais perto), depois P2 ou P3, depois a outra
        # (~43 km). Otimo: P2 -> P1 -> P3 ou P3 -> P1 -> P2 (~37 km) --
        # exige mover a primeira parada, proibido ate 17/09.
        rota = [_servico(1, 0.10, 0.0), _servico(2, 0.11, -0.10), _servico(3, 0.11, 0.10)]
        semente = [rota[0], rota[1], rota[2]]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertEqual(ordem[1]["id"], 1)
        self.assertNotEqual(ordem[0]["id"], 1)
        self.assertLess(_km_sem_volta(ordem), _km_sem_volta(semente) - 4.0)

    def test_nunca_pior_que_a_semente_nn(self):
        rota = [_servico(i, 0.01 * ((i * 7) % 11), 0.01 * ((i * 3) % 13)) for i in range(1, 13)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        semente = rd._ordem_vizinho_mais_proximo(rota, BASE, _coords)
        self.assertLessEqual(_km_sem_volta(ordem), _km_sem_volta(semente) + 1e-9)
        self.assertEqual({s["id"] for s in ordem}, {s["id"] for s in rota})

    def test_sem_coordenada_vai_pro_final(self):
        rota = [_servico(1, 0.10, 0.0), _servico(2, None, None), _servico(3, 0.05, 0.0)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertEqual([s["id"] for s in ordem], [3, 1, 2])

    def test_janela_continua_valendo_com_primeira_parada_livre(self):
        # a mais PERTO fecha as 11h e a mais longe abre so as 14h: a
        # sequencia livre respeita as duas sem esperar parado
        with mock.patch.object(rd, "HORA_SAIDA_BASE", 10.0):
            rota = [_servico(1, 0.20, 0.0, ("14:00", "17:00")), _servico(2, 0.15, 0.02),
                    _servico(3, 0.10, 0.0), _servico(4, 0.03, 0.0, ("08:00", "11:00"))]
            ordem = ot.ordenar_2opt(rota, *BASE)
            sim = rd.simular_horarios(ordem, coords_base=BASE)
            self.assertLessEqual(sim["atraso_h"], rd.TOLERANCIA_JANELA_HORAS)
            self.assertEqual(ordem[0]["id"], 4)
            self.assertEqual(ordem[-1]["id"], 1)

    def test_calcular_km_estimado_sem_volta(self):
        rota = [_servico(1, 0.10, 0.0)]
        km = rd.calcular_km_estimado(rota, *BASE, None)
        self.assertAlmostEqual(km, rd._distancia_km(*BASE, 0.10, 0.0), places=6)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_sequencia_livre -v`
Expected: `test_semente...` falha (ordem começa pelo 2), `test_nunca_pior...` falha por `AttributeError: _ordem_vizinho_mais_proximo`, `test_calcular_km_estimado_sem_volta` falha (dobro do km).

- [ ] **Step 3: Implementar em `roteirizacao_dados.py`**

(a) `calcular_km_estimado` (linhas 342-359): trocar docstring e remover a última soma.

```python
def calcular_km_estimado(sublote: list[dict], base_lat: float, base_lng: float,
                         api_key: str | None) -> float:
    """
    KM total estimado (haversine) de UMA rota, na ordem de visita:
    base -> p1 -> ... -> pN, SEM a perna de volta (desde 18/09: a rota
    real termina na ultima entrega -- ver COORDS_BASE/estimar_tempo_rota
    -- e o sequenciador otimiza o mesmo objetivo; ate entao somava uma
    volta ficticia a base). Servico sem coordenada e ignorado no
    somatorio. Usada por selecao_modelo._km_total, pelo km dos rascunhos
    (painel_agentes/rascunhos_rota.py) e pelo polimento entre rotas.
    """
    coords = [c for c in (obter_coordenadas(s, api_key) for s in sublote) if c]
    if not coords:
        return 0.0
    total = _distancia_km(base_lat, base_lng, *coords[0])
    for i in range(len(coords) - 1):
        total += _distancia_km(*coords[i], *coords[i + 1])
    return total
```

(b) Antes de `ordenar_com_janelas` (linha 1203), adicionar a semente:

```python
def _ordem_vizinho_mais_proximo(servicos: list[dict], base: tuple[float, float], resolver) -> list[dict]:
    """Semente do sequenciador (18/09): sai da base pro servico mais
    proximo, dali pro mais proximo ainda nao visitado, e assim por
    diante. Servico sem coordenada vai pro FINAL, na ordem original.
    Deterministico: empate resolvido pela ordem original."""
    com, sem = [], []
    for s in servicos:
        (com if resolver(s) else sem).append(s)
    ordem: list[dict] = []
    atual = base
    restantes = list(com)
    while restantes:
        proximo = min(restantes, key=lambda s: _distancia_km(atual[0], atual[1], *resolver(s)))
        restantes.remove(proximo)
        ordem.append(proximo)
        atual = resolver(proximo)
    return ordem + sem
```

(c) Reescrever `ordenar_com_janelas` (linhas 1203-1333) por inteiro:

```python
def ordenar_com_janelas(servicos: list[dict], base_lat: float, base_lng: float,
                        api_key: str | None = None, coords_fn=None) -> list[dict]:
    """
    Sequenciamento de UMA rota (usado por otimizacao_rotas.ordenar_2opt,
    que so delega pra ca). Desde 18/09 (Hugo -- "sequencia livre",
    revoga a regra "mais longe primeiro" de 03/08):

      - semente: vizinho mais proximo saindo da base
        (_ordem_vizinho_mais_proximo);
      - objetivo SEM janela: km do trajeto base -> p1 -> ... -> pN, SEM
        volta a base (a rota real termina na ultima entrega);
      - objetivo COM janela (Hugo, 09/09): km + PESO_ATRASO_JANELA_KM x
        horas de atraso + PESO_ESPERA_JANELA_KM x horas de espera
        (simular_horarios);
      - busca local: 2-opt (reversao de segmento) e or-opt (realocacao
        de 1 parada), em QUALQUER posicao -- a 1a parada tambem se move.

    Servico sem coordenada fica onde a semente o deixou (no final);
    reversoes que envolvam parada sem coordenada sao puladas.
    """
    resolver = coords_fn or (lambda s: coords_do_servico(s, api_key))
    base = (base_lat, base_lng)

    ordem_inicial = _ordem_vizinho_mais_proximo(servicos, base, resolver)
    n = len(ordem_inicial)
    com_janela = tem_janela(ordem_inicial)
    if n <= 1 or (n <= 2 and not com_janela):
        return ordem_inicial

    coords = [resolver(s) for s in ordem_inicial]
    coords_por_objeto = {id(s): c for s, c in zip(ordem_inicial, coords)}
    resolver_cache = lambda s: coords_por_objeto.get(id(s), resolver(s))

    def _ponto(rota: list[int], pos: int):
        """Coordenada na posicao `pos`; base antes da 1a parada; None
        depois da ultima (nao ha perna de volta)."""
        if pos < 0:
            return base
        if pos >= len(rota):
            return None
        return coords[rota[pos]]

    def _perna(a, b) -> float:
        return _distancia_km(*a, *b) if (a is not None and b is not None) else 0.0

    def _delta_km(rota: list[int], i: int, j: int) -> float:
        a, b = _ponto(rota, i - 1), _ponto(rota, i)
        c, d = _ponto(rota, j), _ponto(rota, j + 1)
        return (_perna(a, c) + _perna(b, d)) - (_perna(a, b) + _perna(c, d))

    def _tem_coords(rota: list[int], i: int, j: int) -> bool:
        vizinhos = [k for k in (i - 1, j + 1) if 0 <= k < len(rota)]
        return not (any(coords[rota[k]] is None for k in range(i, j + 1))
                    or any(coords[rota[k]] is None for k in vizinhos))

    def _km_total(rota: list[int]) -> float:
        total = 0.0
        anterior = base
        for k in rota:
            c = coords[k]
            if c is None:
                continue
            total += _distancia_km(anterior[0], anterior[1], c[0], c[1])
            anterior = c
        return total

    def _custo(rota: list[int]) -> float:
        sim = simular_horarios([ordem_inicial[k] for k in rota], api_key, base, coords_fn=resolver_cache)
        return (_km_total(rota) + PESO_ATRASO_JANELA_KM * sim["atraso_h"]
                + PESO_ESPERA_JANELA_KM * sim["espera_h"])

    def _2opt_sem_janela(rota: list[int]) -> list[int]:
        melhorou, iteracoes = True, 0
        while melhorou and iteracoes < MAX_ITERACOES_2OPT:
            melhorou = False
            iteracoes += 1
            for i in range(0, len(rota) - 1):
                for j in range(i + 1, len(rota)):
                    if not _tem_coords(rota, i, j):
                        continue
                    if _delta_km(rota, i, j) < -0.01:  # melhoria significativa (> 10m)
                        rota[i:j + 1] = reversed(rota[i:j + 1])
                        melhorou = True
        return rota

    def _busca_local_com_janela(rota: list[int]) -> list[int]:
        custo_atual = _custo(rota)
        melhorou, iteracoes = True, 0
        while melhorou and iteracoes < MAX_ITERACOES_2OPT:
            melhorou = False
            iteracoes += 1
            for i in range(0, len(rota) - 1):
                for j in range(i + 1, len(rota)):
                    candidata = rota[:i] + rota[i:j + 1][::-1] + rota[j + 1:]
                    custo = _custo(candidata)
                    if custo < custo_atual - 0.01:
                        rota, custo_atual, melhorou = candidata, custo, True
            for i in range(0, len(rota)):
                item = rota[i]
                restante = rota[:i] + rota[i + 1:]
                for pos in range(0, len(rota)):
                    if pos == i:
                        continue
                    candidata = restante[:pos] + [item] + restante[pos:]
                    custo = _custo(candidata)
                    if custo < custo_atual - 0.01:
                        rota, custo_atual, melhorou = candidata, custo, True
                        break
                if melhorou:
                    break
        return rota

    rota = list(range(n))
    rota = _busca_local_com_janela(rota) if com_janela else _2opt_sem_janela(rota)
    return [ordem_inicial[k] for k in rota]
```

(d) Docstring de `ordenar_por_distancia_base` (linha 1762): substituir o 1º parágrafo por:

```
    Ordena os servicos de uma rota da mais LONGE pra mais PERTO da
    base -- regra de sequenciamento de 03/08, REVOGADA em 18/09 (Hugo:
    "sequencia livre", ver ordenar_com_janelas). Sem chamador em
    producao desde entao; mantida pra scripts antigos e benchmark.
```

(e) Em `_orcamento_inviavel_por_distancia` (linhas 786-790) trocar o trecho "Como o 2-opt sempre visita a parada mais distante PRIMEIRO (farthest-first, ver ordenar_2opt), qualquer rota que inclua esse pedido paga aquela mesma perna longa de qualquer forma" por "Qualquer rota que inclua esse pedido paga, em algum trecho, um deslocamento pelo menos tao longo quanto a perna base -> pedido (desigualdade triangular), entao".

(f) Em `otimizacao_rotas.py:321-345` (docstring de `ordenar_2opt`) substituir o texto por:

```
    Sequenciamento de uma rota -- so delega pra
    roteirizacao_dados.ordenar_com_janelas (semente vizinho mais
    proximo, 2-opt + or-opt sem volta a base, janelas de horario; ver
    docstring de la). Mantida pelo nome: e o ponto de entrada de
    selecao_modelo, criar_rotas_diarias, incrementar_rotas e do botao
    "Otimizar sequencia" dos rascunhos.
```
(mantendo o corpo que já delega).

(g) Em `roteirizacao/criar_rotas_diarias.py`, substituir as três strings de log `(mais longe -> mais perto da base)` (linhas 761, 772, 795) por `(sequencia otimizada)`, e o comentário das linhas 633-634 ("sequenciamento (mais LONGE -> mais PERTO, pedido do Hugo, 03/08)") por "sequenciamento (vizinho mais proximo + 2-opt, Hugo 18/09)". No docstring do módulo (linhas 15-24) trocar "os pedidos são ordenados da mais LONGE pra mais PERTO da base (roteirizacao_dados.py::ordenar_por_distancia_base) -- padrão de sequenciamento pedido pelo Hugo, 03/08" por "os pedidos são sequenciados por vizinho mais próximo + 2-opt/or-opt sem volta à base (roteirizacao_dados.py::ordenar_com_janelas, Hugo 18/09; substituiu o 'mais longe primeiro' de 03/08)".

- [ ] **Step 4: Ajustar `test_janelas_horario.py`**

Substituir o método `test_sem_janela_igual_ao_2opt_antigo` (linhas 178-190) por:

```python
    def test_sem_janela_vizinho_mais_proximo_e_2opt(self):
        # semente vizinho mais proximo + 2-opt sem volta a base (18/09)
        rota = [_servico(1, 0.05, 0.0), _servico(2, 0.30, 0.0), _servico(3, 0.10, 0.10),
                _servico(4, 0.20, 0.05), _servico(5, 0.02, 0.02)]
        ordem = ot.ordenar_2opt(rota, *BASE)
        self.assertNotEqual(ordem[0]["id"], 2)  # a mais distante deixou de ser a 1a obrigatoria
        self.assertEqual({s["id"] for s in ordem}, {1, 2, 3, 4, 5})

        def _km(seq):
            pts = [BASE] + [(s["latitude"], s["longitude"]) for s in seq]
            return sum(rd._distancia_km(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))
        semente = rd._ordem_vizinho_mais_proximo(rota, BASE, _coords)
        self.assertLessEqual(_km(ordem), _km(semente) + 1e-9)
```

- [ ] **Step 5: Rodar tudo que toca sequência**

Run: `py -3.11 -m unittest roteirizacao.test_sequencia_livre roteirizacao.test_janelas_horario roteirizacao.test_orcamento_horas roteirizacao.test_nivel4_veiculo_grande roteirizacao.test_incrementar_rotas painel_agentes.test_incrementar_rascunhos -v`
Expected: tudo OK. Se `test_cliente_que_fecha_cedo_vai_pro_comeco` ou `test_cliente_que_abre_tarde_vai_pro_fim` falharem, a causa provável é o or-opt não ter alcançado a posição 0: conferir que os laços começam em 0.

- [ ] **Step 6: Checkpoint (sem commit)**

`py -3.11 -m py_compile roteirizacao/roteirizacao_dados.py roteirizacao/otimizacao_rotas.py roteirizacao/criar_rotas_diarias.py`. Arquivos: os três acima + `roteirizacao/test_sequencia_livre.py` + `roteirizacao/test_janelas_horario.py`.

---

### Task 4: Critério de seleção: km primeiro, rotas como desempate

**Files:**
- Modify: `roteirizacao/selecao_modelo.py:1-39` (docstring), `:151-161` (`_registrar_historico`), `:164-307` (`escolher_melhor_modelo`)
- Test: `roteirizacao/test_selecao_modelo.py`

**Interfaces:**
- Produces: `_escolher_vencedor(avaliacoes: dict[str, dict]) -> str` (chave `(round(km, 1), rotas)`); `escolher_melhor_modelo(..., registrar_historico: bool = True)`.

- [ ] **Step 1: Escrever os testes**

Criar `roteirizacao/test_selecao_modelo.py`:

```python
# -*- coding: utf-8 -*-
"""Criterio de selecao diaria (18/09): menor km total vence, menos rotas
so desempata (antes era o contrario). E `registrar_historico=False`
(replay) nao escreve no historico.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_selecao_modelo -v"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import otimizacao_rotas as ot
import selecao_modelo as sm

BASE = (-23.55, -46.63)  # perto do centro de SP: tudo cai em GRANDE_SP


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, dlat, dlng, caixas=1):
    return {"id": i, "code": f"PS-{i}", "address": f"Rua {i}, Sao Paulo - SP, 01000-000, Brasil",
            "_nivel_dificuldade": 1, "latitude": BASE[0] + dlat, "longitude": BASE[1] + dlng,
            "dimension_3": caixas, "sender_id": 1}


class CriterioTestCase(unittest.TestCase):

    def test_menor_km_vence_mesmo_com_rota_a_mais(self):
        avaliacoes = {"A": {"rotas": 3, "km": 100.0}, "B": {"rotas": 4, "km": 90.0}}
        self.assertEqual(sm._escolher_vencedor(avaliacoes), "B")

    def test_empate_em_km_arredondado_desempata_por_rotas(self):
        avaliacoes = {"A": {"rotas": 4, "km": 100.04}, "B": {"rotas": 3, "km": 100.0}, "C": {"rotas": 5, "km": 99.96}}
        self.assertEqual(sm._escolher_vencedor(avaliacoes), "B")

    def test_empate_total_fica_com_a_ordem_de_insercao(self):
        avaliacoes = {"Atual (Grade+Greedy)": {"rotas": 2, "km": 50.0}, "Sweep Polar": {"rotas": 2, "km": 50.0}}
        self.assertEqual(sm._escolher_vencedor(avaliacoes), "Atual (Grade+Greedy)")


class HistoricoTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        rd.COORDS_BASE = BASE
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.historico = Path(self.tmp.name) / "hist.txt"

    def _servicos(self):
        # dois aglomerados a ~11 km um do outro, 6 pedidos cada
        return ([_servico(i, 0.01 + 0.001 * i, 0.0) for i in range(6)]
                + [_servico(10 + i, 0.01 + 0.001 * i, 0.10) for i in range(6)])

    def test_registrar_historico_false_nao_escreve(self):
        with mock.patch.object(sm, "ARQUIVO_HISTORICO", self.historico):
            vencedor, sublotes = sm.escolher_melhor_modelo(
                self._servicos(), *BASE, None, data_alvo=date(2026, 9, 18), label="teste",
                tamanho_minimo=1, tamanho_maximo=16, volume_maximo=100, distancia_maxima_km=20,
                registrar_historico=False,
            )
        self.assertFalse(self.historico.exists())
        self.assertIn(vencedor, ("Atual (Grade+Greedy)", "Sweep Polar", "Clarke-Wright", "CEP real", "K-means geográfico"))
        self.assertEqual(sorted(s["id"] for sub in sublotes for s in sub), sorted(s["id"] for s in self._servicos()))

    def test_registrar_historico_padrao_escreve(self):
        with mock.patch.object(sm, "ARQUIVO_HISTORICO", self.historico):
            sm.escolher_melhor_modelo(
                self._servicos(), *BASE, None, data_alvo=date(2026, 9, 18), label="teste",
                tamanho_minimo=1, tamanho_maximo=16, volume_maximo=100, distancia_maxima_km=20,
            )
        self.assertTrue(self.historico.exists())
        self.assertIn("vencedor:", self.historico.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_selecao_modelo -v`
Expected: `AttributeError: module 'selecao_modelo' has no attribute '_escolher_vencedor'`; `TypeError: unexpected keyword 'registrar_historico'`.

- [ ] **Step 3: Implementar em `selecao_modelo.py`**

(a) Adicionar antes de `_registrar_historico` (linha 151):

```python
def _escolher_vencedor(avaliacoes: dict[str, dict]) -> str:
    """Criterio do dia (Hugo, 18/09 -- "rota compacta, mesmo que custe
    1 rota a mais"): MENOR km total estimado (arredondado a 0,1 km pra
    nao decidir por ruido), MENOS rotas so como desempate. Ate 17/09 era
    o inverso (menos rotas primeiro) -- premiava o Clarke-Wright por
    esticar cada rota ate o teto de distancia. Como calcular_km_estimado
    conta a perna base -> 1a parada, cada rota extra ja custa km: rota a
    mais so vence quando compensa de verdade."""
    return min(avaliacoes, key=lambda nome: (round(avaliacoes[nome]["km"], 1), avaliacoes[nome]["rotas"]))
```

(b) Em `escolher_melhor_modelo`: adicionar o parâmetro `registrar_historico: bool = True` (depois de `km_acumulado_maximo_viagem`), trocar a linha 297 por `vencedor = _escolher_vencedor(avaliacoes)`, e a linha 305 por:

```python
    if registrar_historico:
        _registrar_historico(data_alvo, label or "-", vencedor, avaliacoes)
```

(c) No docstring de `escolher_melhor_modelo` (linhas 177-178) trocar "Critério: menos rotas; empate decidido pelo menor KM total estimado." por "Critério (18/09): menor KM total estimado; menos rotas só como desempate -- ver _escolher_vencedor. `registrar_historico=False` (replay) não grava linha no histórico." No docstring do módulo (linhas 19-26) trocar o trecho de "sequencia TODOS com 2-opt (...)" até "como desempate." por:

```
sequencia TODOS com otimizacao_rotas.ordenar_2opt (vizinho mais
proximo + 2-opt/or-opt sem volta a base, Hugo 18/09), valida travas +
cobertura de pedidos de cada candidato, e escolhe o vencedor por:
  1o menor KM total estimado (haversine na ordem de visita);
  2o MENOS rotas, so como desempate (ver _escolher_vencedor).
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest roteirizacao.test_selecao_modelo -v`
Expected: 5 OK. Se `HistoricoTestCase` falhar com erro de `classificar_rota_viagem`/`extrair_cidade`, conferir que o `address` dos serviços de teste termina em `"Sao Paulo - SP, 01000-000, Brasil"` (cidade sem região externa -> GRANDE_SP).

- [ ] **Step 5: Checkpoint (sem commit)**

`py -3.11 -m py_compile roteirizacao/selecao_modelo.py`. Arquivos: `roteirizacao/selecao_modelo.py`, `roteirizacao/test_selecao_modelo.py`.

---

### Task 5: Partição única por dia + rótulo de carga + Laboratório "Todos"

**Files:**
- Modify: `roteirizacao/criar_rotas_diarias.py:123-161` (constantes), `:214-250` (`_particionar_carga_com_fusao`), `:453` e `:747` (`"particao": label`)
- Modify: `painel_agentes/laboratorio_rotas.py:50`, `:224-268`
- Modify: `painel_agentes/templates/laboratorio_rotas.html:97-101`
- Modify: `painel_agentes/painel_agentes.py:748`
- Test: `roteirizacao/test_particao_carga.py`, `painel_agentes/test_laboratorio_todos.py`

**Interfaces:**
- Produces: `SEPARAR_POR_TIPO_CARGA = False`, `PARTICAO_GERAL = "Geral"`, `rotulo_carga(sublote: list[dict]) -> str` em `criar_rotas_diarias`; `laboratorio_rotas.filtrar_particao(servicos, particao) -> list[dict]`, `PARTICOES_VALIDAS = ("Todos", "Seco", "Refrigerado/Congelado")`.

- [ ] **Step 1: Escrever os testes**

Criar `roteirizacao/test_particao_carga.py`:

```python
# -*- coding: utf-8 -*-
"""Particao por tipo de carga (18/09): desligada por padrao (Seco e
Refrigerado sempre podem ir juntos -- Hugo), religavel por constante; o
tipo de carga vira rotulo derivado do conteudo da rota.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_particao_carga -v"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import criar_rotas_diarias as crd


def _coords(s):
    return (s["latitude"], s["longitude"])


def _servico(i, tipo, dlat=0.0):
    return {"id": i, "code": f"PS-{i}", "address": f"Rua {i}, Sao Paulo - SP, 01000-000, Brasil",
            "_tipo_carga": tipo, "latitude": -23.55 + dlat, "longitude": -46.63, "dimension_3": 1}


class ParticaoTestCase(unittest.TestCase):

    def setUp(self):
        p = mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s))
        p.start()
        self.addCleanup(p.stop)
        self.servicos = [_servico(i, "Seco", 0.001 * i) for i in range(12)] + \
                        [_servico(20 + i, "Refrigerado", 0.001 * i) for i in range(12)]

    def test_padrao_e_particao_unica(self):
        self.assertFalse(crd.SEPARAR_POR_TIPO_CARGA)
        particoes = crd._particionar_carga_com_fusao(self.servicos, 10, None)
        self.assertEqual([label for label, _ in particoes], [crd.PARTICAO_GERAL])
        self.assertEqual(len(particoes[0][1]), 24)

    def test_religar_volta_a_particao_tripla(self):
        with mock.patch.object(crd, "SEPARAR_POR_TIPO_CARGA", True):
            particoes = dict(crd._particionar_carga_com_fusao(self.servicos, 10, None))
        self.assertEqual(len(particoes["Seco"]), 12)
        self.assertEqual(len(particoes["Refrigerado/Congelado"]), 12)

    def test_rotulo_carga(self):
        self.assertEqual(crd.rotulo_carga([_servico(1, "Seco"), _servico(2, "Seco")]), "Seco")
        self.assertEqual(crd.rotulo_carga([_servico(1, "Refrigerado"), _servico(2, "Congelado")]), "Refrigerado/Congelado")
        self.assertEqual(crd.rotulo_carga([_servico(1, "Seco"), _servico(2, "Congelado")]), "Misto (Seco+Refrigerado)")
        self.assertEqual(crd.rotulo_carga([]), "Seco")


if __name__ == "__main__":
    unittest.main()
```

Criar `painel_agentes/test_laboratorio_todos.py`:

```python
# -*- coding: utf-8 -*-
"""Laboratorio de roteirizacao: particao "Todos" (18/09) roda os esquemas
sobre Seco + Refrigerado juntos, como a producao passou a fazer.
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_laboratorio_todos -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import laboratorio_rotas as lab


class FiltroParticaoTestCase(unittest.TestCase):

    def setUp(self):
        self.servicos = [{"id": 1, "_tipo_carga": "Seco"}, {"id": 2, "_tipo_carga": "Refrigerado"},
                         {"id": 3, "_tipo_carga": "Congelado"}]

    def test_todos_e_o_padrao_e_nao_filtra(self):
        self.assertEqual(lab.PARTICOES_VALIDAS[0], "Todos")
        self.assertEqual([s["id"] for s in lab.filtrar_particao(self.servicos, "Todos")], [1, 2, 3])

    def test_seco_e_frio_continuam_disponiveis(self):
        self.assertEqual([s["id"] for s in lab.filtrar_particao(self.servicos, "Seco")], [1])
        self.assertEqual([s["id"] for s in lab.filtrar_particao(self.servicos, "Refrigerado/Congelado")], [2, 3])

    def test_particao_invalida(self):
        with self.assertRaises(ValueError):
            lab.filtrar_particao(self.servicos, "Misto")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_particao_carga painel_agentes.test_laboratorio_todos -v`
Expected: `AttributeError` em `SEPARAR_POR_TIPO_CARGA`, `rotulo_carga`, `filtrar_particao`.

- [ ] **Step 3: Implementar em `criar_rotas_diarias.py`**

(a) Depois de `SEM_LIMITE_PARADAS = 10_000` (linha 161) adicionar:

```python
# Particao por tipo de carga (Hugo, 18/09): DESLIGADA. Toda a frota tem
# bau com compartimento termico, entao Seco e Refrigerado/Congelado podem
# sempre ir no mesmo carro -- separar so criava rotas paralelas na mesma
# regiao (medido em 16-17/09: metade das paradas cuja vizinha mais
# proxima estava em OUTRA rota era por causa dessa particao). True volta
# ao comportamento anterior (Seco x Refrigerado x Misto por macro).
SEPARAR_POR_TIPO_CARGA = False
PARTICAO_GERAL = "Geral"
```

(b) Em `_particionar_carga_com_fusao` (linha 214), logo depois do docstring, adicionar:

```python
    if not SEPARAR_POR_TIPO_CARGA:
        return [(PARTICAO_GERAL, list(servicos))]
```

e acrescentar ao final do docstring: `"Desde 18/09 (SEPARAR_POR_TIPO_CARGA=False) devolve UMA particao 'Geral' com todos os servicos; o tipo de carga vira rotulo por rota (rotulo_carga)."`

(c) Logo depois de `_particionar_carga_com_fusao`, adicionar:

```python
def rotulo_carga(sublote: list[dict]) -> str:
    """Rotulo de carga de UMA rota, derivado do conteudo (18/09): 'Seco',
    'Refrigerado/Congelado' ou 'Misto (Seco+Refrigerado)'. Gravado em
    rascunhos_rota.particao no lugar do nome da particao, pra tela,
    historico e filtros continuarem enxergando o mesmo vocabulario de
    antes. Rota vazia (nao acontece) rotula 'Seco'."""
    tem_frio = any(s.get("_tipo_carga") in TIPOS_CARGA_FRIA for s in sublote)
    tem_seco = any(s.get("_tipo_carga") not in TIPOS_CARGA_FRIA for s in sublote)
    if tem_frio and tem_seco:
        return "Misto (Seco+Refrigerado)"
    return "Refrigerado/Congelado" if tem_frio else "Seco"
```

(d) Trocar `"particao": label,` por `"particao": rotulo_carga(sublote),` nas duas ocorrências (linhas 453 e 747).

- [ ] **Step 4: Implementar no Laboratório**

(a) `painel_agentes/laboratorio_rotas.py:50`:

```python
PARTICOES_VALIDAS = ("Todos", "Seco", "Refrigerado/Congelado")


def filtrar_particao(servicos: list[dict], particao: str) -> list[dict]:
    """"Todos" (padrao desde 18/09, igual a producao) nao filtra; "Seco"
    e "Refrigerado/Congelado" continuam disponiveis pra investigar um
    tipo de carga isolado."""
    if particao not in PARTICOES_VALIDAS:
        raise ValueError(f"Partição inválida: {particao!r} (esperado {PARTICOES_VALIDAS})")
    if particao == "Todos":
        return list(servicos)
    return [s for s in servicos if (s["_tipo_carga"] in TIPOS_CARGA_FRIA) == (particao == "Refrigerado/Congelado")]
```

(b) Em `buscar_dados_laboratorio`: assinatura `particao: str = "Todos"`; remover as linhas 243-244 (validação) e trocar as linhas 265-268 por `servicos_particao = filtrar_particao(servicos, particao)`. No docstring, trocar `("Seco" ou "Refrigerado/Congelado" -- nunca misturadas, mesma regra de produção)` por `("Todos" -- padrão, igual à produção desde 18/09 -- ou "Seco"/"Refrigerado/Congelado" pra investigar um tipo isolado)`.

(c) `painel_agentes/templates/laboratorio_rotas.html:98-101`: adicionar como primeira opção `<option value="Todos" {% if particao_input == "Todos" %}selected{% endif %}>Todos</option>`.

(d) `painel_agentes/painel_agentes.py:748`: `particao = request.args.get("particao", "Todos")`.

- [ ] **Step 5: Rodar**

Run: `py -3.11 -m unittest roteirizacao.test_particao_carga painel_agentes.test_laboratorio_todos painel_agentes.test_incrementar_rascunhos -v`
Expected: OK.

- [ ] **Step 6: Prova visual do Laboratório em porta alternativa**

De dentro de `painel_agentes/`: `py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"` em background; `curl -s "http://127.0.0.1:8099/laboratorio-rotas?teste=1" | grep -c "Todos"` deve dar >= 1 (se a rota exigir login, conferir com o cookie do painel local ou só checar que o processo sobe sem traceback). Encerrar o processo que **você** subiu (nunca um PID que já existia).

- [ ] **Step 7: Checkpoint (sem commit)**

`py -3.11 -m py_compile roteirizacao/criar_rotas_diarias.py painel_agentes/laboratorio_rotas.py painel_agentes/painel_agentes.py`. Arquivos: os três + template + os dois testes. **Atenção:** `painel_agentes/painel_agentes.py` pode ter hunks de outra sessão; editar só a linha 748 e conferir `git diff painel_agentes/painel_agentes.py` antes do checkpoint.

---

### Task 6: Polimento entre rotas (`polimento_rotas.py`)

**Files:**
- Create: `roteirizacao/polimento_rotas.py`
- Modify: `roteirizacao/otimizacao_rotas.py:90` (alias público `limite_distancia = _limite_distancia` logo após a função)
- Test: `roteirizacao/test_polimento_rotas.py`

**Interfaces:**
- Consumes: `obter_coordenadas`, `_distancia_km`, `calcular_km_estimado`, `extrair_volume_caixas`, `extrair_nivel_dificuldade`, `NIVEL_ROTA_EXCLUSIVA`, `estimar_tempo_rota`, `ROTA_TEMPO_MAXIMO_HORAS`, `_orcamento_inviavel_por_distancia`, `janela_respeitada`, `caixas_e_enderecos`, `macro_regiao_predominante_do_sublote` (roteirizacao_dados); `limite_distancia`, `ordenar_2opt` (otimizacao_rotas); `classificar_tipo_veiculo` (regras.tipo_veiculo).
- Produces: `polir_entre_rotas(sublotes, base_lat, base_lng, api_key, *, tamanho_maximo, volume_maximo, distancia_maxima_km, distancia_maxima_viagem_km=None, eh_viagem_fn=None, tempo_maximo_s=3.0, ganho_minimo_km=0.05) -> tuple[list[list[dict]], dict]`; resumo com `realocacoes, trocas, esvaziadas, km_antes, km_depois, tempo_s, estourou_tempo`.

- [ ] **Step 1: Escrever os testes**

Criar `roteirizacao/test_polimento_rotas.py`:

```python
# -*- coding: utf-8 -*-
"""
Polimento entre rotas (18/09): move/troca paradas entre rotas vizinhas
da mesma macro-regiao enquanto o km total cair e as travas continuarem
valendo; esvazia rota de 1-2 paradas nas vizinhas quando cabe.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_polimento_rotas -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import otimizacao_rotas as ot
import polimento_rotas as pr

BASE = (0.0, 0.0)


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, lat, lng, caixas=1, nivel=1):
    return {"id": i, "code": f"PS-{i}", "address": f"P{i}", "_nivel_dificuldade": nivel,
            "latitude": lat, "longitude": lng, "dimension_3": caixas, "sender_id": 1}


def _ids(sublotes):
    return sorted(s["id"] for sub in sublotes for s in sub)


class PolimentoTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(pr, "macro_regiao_predominante_do_sublote", lambda sub, k=None: "GRANDE_SP"),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        rd.COORDS_BASE = BASE

    def _polir(self, sublotes, **kw):
        params = dict(tamanho_maximo=16, volume_maximo=100, distancia_maxima_km=20)
        params.update(kw)
        return pr.polir_entre_rotas(sublotes, *BASE, None, **params)

    def test_realoca_parada_perdida_pra_rota_vizinha(self):
        # a rota A fica com 3 paradas depois da realocacao (nao entra no
        # esvaziamento, que so olha rota de 1-2 paradas)
        a = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.001), _servico(5, 0.10, 0.002), _servico(9, 0.10, 0.10)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        polidos, resumo = self._polir([a, b])
        rota_do_9 = next(sub for sub in polidos if any(s["id"] == 9 for s in sub))
        self.assertEqual({s["id"] for s in rota_do_9}, {3, 4, 9})
        self.assertGreaterEqual(resumo["realocacoes"], 1)
        self.assertLess(resumo["km_depois"], resumo["km_antes"])
        self.assertEqual(_ids(polidos), [1, 2, 3, 4, 5, 9])

    def test_troca_paradas_cruzadas(self):
        a = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.20)]
        b = [_servico(3, 0.10, 0.20), _servico(4, 0.10, 0.00)]
        polidos, resumo = self._polir([a, b])
        grupos = sorted(sorted(s["id"] for s in sub) for sub in polidos)
        self.assertEqual(grupos, [[1, 4], [2, 3]])
        self.assertGreaterEqual(resumo["realocacoes"] + resumo["trocas"], 1)

    def test_nao_estoura_tamanho_maximo(self):
        # 9 fica a 0,099 (e nao 0,10) pra nenhuma troca empatar em km
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.099)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        polidos, resumo = self._polir([a, b], tamanho_maximo=2)
        self.assertEqual(sorted(len(sub) for sub in polidos), [2, 2])
        self.assertEqual(resumo["realocacoes"], 0)
        self.assertEqual(_ids(polidos), [1, 3, 4, 9])

    def test_nao_estoura_caixas(self):
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.099, caixas=50)]
        b = [_servico(3, 0.10, 0.10, caixas=30), _servico(4, 0.10, 0.101, caixas=30)]
        polidos, _ = self._polir([a, b], volume_maximo=100)
        rota_do_9 = next(sub for sub in polidos if any(s["id"] == 9 for s in sub))
        self.assertTrue({3, 4}.isdisjoint({s["id"] for s in rota_do_9}))
        for sub in polidos:
            self.assertLessEqual(sum(s["dimension_3"] for s in sub), 100)

    def test_nao_estoura_distancia_par_a_par(self):
        # 9 esta a ~22 km das paradas 3 e 4: com teto de 20 km nunca divide
        # rota com elas (a parada 1 pode ir pra rota b -- isso reduz km e e valido)
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.30)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        polidos, _ = self._polir([a, b], distancia_maxima_km=20)
        rota_do_9 = next(sub for sub in polidos if any(s["id"] == 9 for s in sub))
        self.assertTrue({3, 4}.isdisjoint({s["id"] for s in rota_do_9}))
        self.assertEqual(_ids(polidos), [1, 3, 4, 9])

    def test_rota_nivel4_fica_intocada(self):
        exclusiva = [_servico(7, 0.10, 0.10, nivel=4)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        polidos, _ = self._polir([exclusiva, b])
        self.assertIn(exclusiva, polidos)
        self.assertTrue(any(sub is exclusiva or sub == exclusiva for sub in polidos))

    def test_esvazia_rota_pequena(self):
        a = [_servico(1, 0.10, 0.00)]
        b = [_servico(3, 0.10, 0.01), _servico(4, 0.10, 0.02)]
        polidos, resumo = self._polir([a, b])
        self.assertEqual(len(polidos), 1)
        self.assertEqual(_ids(polidos), [1, 3, 4])
        self.assertEqual(resumo["esvaziadas"], 1)

    def test_deterministico(self):
        a = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.001), _servico(9, 0.10, 0.10)]
        b = [_servico(3, 0.10, 0.10), _servico(4, 0.10, 0.101)]
        p1, _ = self._polir([list(a), list(b)])
        p2, _ = self._polir([list(a), list(b)])
        self.assertEqual([[s["id"] for s in sub] for sub in p1], [[s["id"] for s in sub] for sub in p2])

    def test_teto_de_tempo_zero_devolve_igual(self):
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.10)]
        b = [_servico(3, 0.10, 0.10)]
        polidos, resumo = self._polir([a, b], tempo_maximo_s=0.0)
        self.assertEqual(_ids(polidos), [1, 3, 9])
        self.assertTrue(resumo["estourou_tempo"])
        self.assertEqual(resumo["realocacoes"], 0)

    def test_macro_regioes_diferentes_nao_trocam(self):
        a = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.10)]
        b = [_servico(3, 0.10, 0.10)]
        macros = {id(a): "GRANDE_SP", id(b): "Campinas"}
        with mock.patch.object(pr, "macro_regiao_predominante_do_sublote", lambda sub, k=None: macros.get(id(sub), "X")):
            polidos, resumo = self._polir([a, b])
        self.assertEqual(resumo["realocacoes"], 0)
        self.assertEqual(sorted(len(sub) for sub in polidos), [1, 2])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_polimento_rotas -v`
Expected: `ModuleNotFoundError: No module named 'polimento_rotas'`.

- [ ] **Step 3: Alias em `otimizacao_rotas.py`**

Logo depois da função `_limite_distancia` (após a linha 97) adicionar:

```python
limite_distancia = _limite_distancia  # nome publico pra polimento_rotas (18/09)
```

- [ ] **Step 4: Implementar `roteirizacao/polimento_rotas.py`**

```python
# -*- coding: utf-8 -*-
"""
polimento_rotas.py

Busca local ENTRE rotas (Hugo, 18/09 -- spec docs/superpowers/specs/
2026-09-18-recalibracao-roteirizacao-design.md, secao 3.4). Roda depois
do modelo vencedor e da fusao de sublotes pequenos, sobre as rotas de
UMA particao do dia. Nenhum dos 5 modelos olha uma rota em relacao as
outras -- e por isso 20-30% das paradas tinham a vizinha mais proxima em
OUTRA rota (medido em producao, 11-17/09).

Movimentos, aceitos so quando o km total das duas rotas envolvidas cai
pelo menos `ganho_minimo_km` E as duas continuam validas (_rota_valida):
  1. realocar: tirar uma parada da rota A e inserir na rota B na posicao
     de menor acrescimo;
  2. trocar: permutar uma parada de A com uma de B;
  3. esvaziar: rota com 1 ou 2 paradas tenta realocar TODAS nas
     vizinhas -- movimento composto, aceito se todas couberem e o km
     total das rotas envolvidas cair (tirar a rota inteira elimina a
     perna da base, ganho que a realocacao parada a parada nao enxerga).
So rotas "poliveis" participam (_rota_polivel: sem nivel 4, sem veiculo
grande, sem destino inviavel por distancia); so rotas da MESMA
macro-regiao trocam paradas. Deterministico (laco em ordem de indice,
sem aleatoriedade). Teto de tempo (`tempo_maximo_s`) pra nao estourar a
janela das 22h.

Custo: o filtro rapido (km sem resequenciar) roda pra todo candidato; o
resequenciamento (ordenar_2opt) + validacao completa so roda quando o
filtro aponta ganho.
"""
import logging
import time

from roteirizacao_dados import (
    obter_coordenadas, _distancia_km, calcular_km_estimado, extrair_volume_caixas,
    extrair_nivel_dificuldade, NIVEL_ROTA_EXCLUSIVA, estimar_tempo_rota, ROTA_TEMPO_MAXIMO_HORAS,
    _orcamento_inviavel_por_distancia, janela_respeitada, caixas_e_enderecos,
    macro_regiao_predominante_do_sublote,
)
from otimizacao_rotas import limite_distancia, ordenar_2opt
from regras.tipo_veiculo import classificar_tipo_veiculo

logger = logging.getLogger(__name__)

PARADAS_ROTA_ESVAZIAVEL = 2  # rota com ate este numero de paradas tenta se esvaziar nas vizinhas


def _rota_polivel(sublote: list[dict], api_key: str | None) -> bool:
    """Mesmos 3 criterios de roteirizacao_dados.exige_orcamento_horas,
    SEM a exclusao de rota de 1 parada (rota de 1 parada pode ser origem
    -- esvaziar -- e destino)."""
    if not sublote:
        return False
    if any(extrair_nivel_dificuldade(s) == NIVEL_ROTA_EXCLUSIVA for s in sublote):
        return False
    if classificar_tipo_veiculo(*caixas_e_enderecos(sublote)) is not None:
        return False
    if _orcamento_inviavel_por_distancia(sublote, api_key):
        return False
    return True


def _rota_valida(sublote: list[dict], api_key: str | None, tamanho_maximo: int, volume_maximo: int,
                 distancia_maxima_km: float | None, distancia_maxima_viagem_km: float | None,
                 eh_viagem_fn) -> bool:
    """Travas de producao na ORDEM DADA: tamanho, caixas, distancia
    par-a-par, orcamento de horas, janela. Rota vazia e valida (vai
    sumir)."""
    if not sublote:
        return True
    if len(sublote) > tamanho_maximo:
        return False
    if sum(extrair_volume_caixas(s) for s in sublote) > volume_maximo:
        return False
    limite = limite_distancia(sublote, distancia_maxima_km, distancia_maxima_viagem_km, eh_viagem_fn)
    if limite is not None:
        pontos = [c for c in (obter_coordenadas(s, api_key) for s in sublote) if c]
        for a in range(len(pontos)):
            for b in range(a + 1, len(pontos)):
                if _distancia_km(*pontos[a], *pontos[b]) > limite:
                    return False
    if len(sublote) > 1 and not (estimar_tempo_rota(sublote, api_key) <= ROTA_TEMPO_MAXIMO_HORAS
                                 or _orcamento_inviavel_por_distancia(sublote, api_key)):
        return False
    return janela_respeitada(sublote, api_key)


def _km(sublote: list[dict], base: tuple[float, float], api_key: str | None) -> float:
    return calcular_km_estimado(sublote, base[0], base[1], api_key) if sublote else 0.0


def _centroide(sublote: list[dict], api_key: str | None) -> tuple[float, float] | None:
    pontos = [c for c in (obter_coordenadas(s, api_key) for s in sublote) if c]
    if not pontos:
        return None
    return (sum(p[0] for p in pontos) / len(pontos), sum(p[1] for p in pontos) / len(pontos))


def _melhor_insercao(parada: dict, sublote: list[dict], base: tuple[float, float],
                     api_key: str | None) -> tuple[float, list[dict]]:
    """(km, sublote_novo) inserindo `parada` na posicao de menor km."""
    melhor: tuple[float, list[dict]] | None = None
    for pos in range(len(sublote) + 1):
        candidato = sublote[:pos] + [parada] + sublote[pos:]
        km = _km(candidato, base, api_key)
        if melhor is None or km < melhor[0]:
            melhor = (km, candidato)
    return melhor  # type: ignore[return-value]


def polir_entre_rotas(sublotes: list[list[dict]], base_lat: float, base_lng: float, api_key: str | None, *,
                      tamanho_maximo: int, volume_maximo: int, distancia_maxima_km: float | None,
                      distancia_maxima_viagem_km: float | None = None, eh_viagem_fn=None,
                      tempo_maximo_s: float = 3.0, ganho_minimo_km: float = 0.05
                      ) -> tuple[list[list[dict]], dict]:
    """Ver docstring do modulo. Devolve (sublotes_polidos, resumo). Nunca
    perde nem duplica servico; rotas nao poliveis saem identicas (mesmo
    objeto de lista)."""
    inicio = time.monotonic()
    base = (base_lat, base_lng)
    originais = list(sublotes)
    rotas: list[list[dict]] = [list(s) for s in originais]
    poliveis = [i for i, r in enumerate(rotas) if _rota_polivel(r, api_key)]
    # macro-regiao calculada UMA vez, sobre a rota original (a rota muda
    # de conteudo durante o polimento, mas nunca troca de macro-regiao)
    macro = {i: macro_regiao_predominante_do_sublote(originais[i], api_key) for i in poliveis}
    km_antes = sum(_km(r, base, api_key) for r in rotas)
    resumo = {"realocacoes": 0, "trocas": 0, "esvaziadas": 0, "km_antes": km_antes,
              "km_depois": km_antes, "tempo_s": 0.0, "estourou_tempo": False}

    def _valida(rota: list[dict]) -> bool:
        return _rota_valida(rota, api_key, tamanho_maximo, volume_maximo,
                            distancia_maxima_km, distancia_maxima_viagem_km, eh_viagem_fn)

    def _tempo_esgotado() -> bool:
        if time.monotonic() - inicio >= tempo_maximo_s:
            resumo["estourou_tempo"] = True
            return True
        return False

    def _vizinhas(i: int) -> list[int]:
        ci = _centroide(rotas[i], api_key)
        saida = []
        for j in poliveis:
            if j == i or not rotas[j] or macro[j] != macro[i]:
                continue
            cj = _centroide(rotas[j], api_key)
            if (ci and cj and distancia_maxima_km is not None
                    and _distancia_km(*ci, *cj) > 2 * distancia_maxima_km):
                continue
            saida.append(j)
        return saida

    def _tentar(i: int, j: int, nova_i: list[dict], nova_j: list[dict], exigir_ganho: bool = True) -> bool:
        """Filtro rapido (km sem resequenciar) -> resequencia -> valida
        -> confere o ganho de novo. True se aplicou."""
        km_atual = _km(rotas[i], base, api_key) + _km(rotas[j], base, api_key)
        if exigir_ganho and _km(nova_i, base, api_key) + _km(nova_j, base, api_key) > km_atual - ganho_minimo_km:
            return False
        seq_i = ordenar_2opt(nova_i, base_lat, base_lng, api_key) if nova_i else []
        seq_j = ordenar_2opt(nova_j, base_lat, base_lng, api_key) if nova_j else []
        if not _valida(seq_i) or not _valida(seq_j):
            return False
        if exigir_ganho and _km(seq_i, base, api_key) + _km(seq_j, base, api_key) > km_atual - ganho_minimo_km:
            return False
        rotas[i], rotas[j] = seq_i, seq_j
        return True

    # 1 e 2: realocar e trocar, ate nao melhorar mais (ou estourar o tempo)
    melhorou = True
    while melhorou and not _tempo_esgotado():
        melhorou = False
        for i in poliveis:
            if not rotas[i] or _tempo_esgotado():
                continue
            for j in _vizinhas(i):
                if _tempo_esgotado():
                    break
                for parada in list(rotas[i]):
                    resto_i = [s for s in rotas[i] if s is not parada]
                    _, cand_j = _melhor_insercao(parada, rotas[j], base, api_key)
                    if _tentar(i, j, resto_i, cand_j):
                        resumo["realocacoes"] += 1
                        melhorou = True
                if not rotas[i]:
                    break
                trocou = False
                for a in list(rotas[i]):
                    for b in list(rotas[j]):
                        resto_i = [s for s in rotas[i] if s is not a]
                        resto_j = [s for s in rotas[j] if s is not b]
                        _, cand_i = _melhor_insercao(b, resto_i, base, api_key)
                        _, cand_j = _melhor_insercao(a, resto_j, base, api_key)
                        if _tentar(i, j, cand_i, cand_j):
                            resumo["trocas"] += 1
                            melhorou = trocou = True
                            break
                    if trocou:
                        break

    # 3: esvaziar rotas pequenas nas vizinhas. Movimento COMPOSTO: cada
    # parada sozinha pode nao dar ganho (a rota de origem continua pagando
    # a perna da base), mas tirar TODAS elimina a perna inteira -- por
    # isso o ganho e conferido no conjunto, nao parada a parada.
    if not _tempo_esgotado():
        for i in poliveis:
            if not rotas[i] or len(rotas[i]) > PARADAS_ROTA_ESVAZIAVEL:
                continue
            envolvidas = [i] + _vizinhas(i)
            backup = {k: list(rotas[k]) for k in envolvidas}
            km_antes_local = sum(_km(rotas[k], base, api_key) for k in envolvidas)
            ok = True
            for parada in list(rotas[i]):
                colocou = False
                for j in _vizinhas(i):
                    _, cand_j = _melhor_insercao(parada, rotas[j], base, api_key)
                    resto_i = [s for s in rotas[i] if s is not parada]
                    if _tentar(i, j, resto_i, cand_j, exigir_ganho=False):
                        colocou = True
                        break
                if not colocou:
                    ok = False
                    break
            km_depois_local = sum(_km(rotas[k], base, api_key) for k in envolvidas)
            if not (ok and not rotas[i] and km_depois_local <= km_antes_local - ganho_minimo_km):
                for k, v in backup.items():
                    rotas[k] = v

    resultado = []
    for idx, r in enumerate(rotas):
        if idx not in poliveis:
            resultado.append(originais[idx])  # objeto original, intocado
        elif r:
            resultado.append(r)
    # rota polivel que terminou vazia (por realocacao ou por esvaziamento)
    resumo["esvaziadas"] = sum(1 for idx in poliveis if not rotas[idx])
    resumo["km_depois"] = sum(_km(r, base, api_key) for r in resultado)
    resumo["tempo_s"] = time.monotonic() - inicio
    return resultado, resumo
```

- [ ] **Step 5: Rodar e ver passar**

Run: `py -3.11 -m unittest roteirizacao.test_polimento_rotas -v`
Expected: 10 OK. Se `test_esvazia_rota_pequena` falhar, conferir que `_tentar(..., exigir_ganho=False)` ainda resequencia e valida; se `test_rota_nivel4_fica_intocada` falhar, conferir que rotas não políveis voltam pelo objeto original.

- [ ] **Step 6: Checkpoint (sem commit)**

`py -3.11 -m py_compile roteirizacao/polimento_rotas.py roteirizacao/otimizacao_rotas.py roteirizacao/test_polimento_rotas.py`. Arquivos: os três.

---

### Task 7: `planejar_sublotes` extraído em `criar_rotas_diarias.py` (com polimento), usado por `main` e `roteirizar_para_rascunhos`

**Files:**
- Modify: `roteirizacao/criar_rotas_diarias.py` (constantes após `PARTICAO_GERAL`; nova função após `_fundir_sublotes_entre_macrorregioes`; `roteirizar_para_rascunhos:385-433`; `main:627-696`)
- Test: `roteirizacao/test_planejar_sublotes.py`

**Interfaces:**
- Consumes: `polir_entre_rotas` (Task 6), `escolher_melhor_modelo(..., registrar_historico=)` (Task 4), `_particionar_carga_com_fusao`/`rotulo_carga` (Task 5).
- Produces: `POLIMENTO_ATIVO = True`, `POLIMENTO_TEMPO_MAXIMO_S = 3.0`, `_polir_particao(sublotes, coords_base, gmaps_key, label, tamanho_maximo) -> list[list[dict]]`, `planejar_sublotes(servicos, coords_base, gmaps_key, data_alvo, *, sufixo_label="", modelo_forcado=None, tamanho_maximo=TAMANHO_MAXIMO_ROTA, registrar_historico=True) -> list[dict]` com itens `{"label": str, "modelo": str, "sublotes": list[list[dict]]}`.

- [ ] **Step 1: Escrever o teste**

Criar `roteirizacao/test_planejar_sublotes.py`:

```python
# -*- coding: utf-8 -*-
"""planejar_sublotes (18/09): miolo unico do criador de rotas (particao
-> selecao de modelo -> fusao -> polimento), usado pelo job das 22h e
pelo botao Roteirizar. Testa cobertura, particao unica, chave de
polimento e o repasse de registrar_historico.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_planejar_sublotes -v"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import otimizacao_rotas as ot
import selecao_modelo as sm
import criar_rotas_diarias as crd

BASE = (-23.55, -46.63)


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, dlat, dlng, tipo="Seco"):
    return {"id": i, "code": f"PS-{i}", "address": f"Rua {i}, Sao Paulo - SP, 01000-000, Brasil",
            "_nivel_dificuldade": 1, "_tipo_carga": tipo, "latitude": BASE[0] + dlat,
            "longitude": BASE[1] + dlng, "dimension_3": 1, "sender_id": 1}


class PlanejarSublotesTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
            mock.patch.object(sm, "_registrar_historico"),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        rd.COORDS_BASE = BASE
        self.servicos = ([_servico(i, 0.01 + 0.001 * i, 0.0, "Seco") for i in range(8)]
                         + [_servico(20 + i, 0.01 + 0.001 * i, 0.12, "Refrigerado") for i in range(8)])

    def test_particao_unica_e_cobertura(self):
        planos = crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18))
        self.assertEqual([p["label"] for p in planos], [crd.PARTICAO_GERAL])
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))
        self.assertIn(planos[0]["modelo"], ("Atual (Grade+Greedy)", "Sweep Polar", "Clarke-Wright", "CEP real", "K-means geográfico"))

    def test_polimento_desligado_tambem_cobre_tudo(self):
        with mock.patch.object(crd, "POLIMENTO_ATIVO", False), \
             mock.patch.object(crd, "polir_entre_rotas", side_effect=AssertionError("nao deveria polir")):
            planos = crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18))
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))

    def test_repassa_registrar_historico(self):
        crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18), registrar_historico=False)
        sm._registrar_historico.assert_not_called()
        crd.planejar_sublotes(self.servicos, BASE, None, date(2026, 9, 18))
        sm._registrar_historico.assert_called()

    def test_sem_base_usa_fluxo_de_reserva(self):
        planos = crd.planejar_sublotes(self.servicos, None, None, date(2026, 9, 18))
        self.assertEqual(planos[0]["modelo"], "Atual (Grade+Greedy)")
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_planejar_sublotes -v`
Expected: `AttributeError: module 'criar_rotas_diarias' has no attribute 'planejar_sublotes'`.

- [ ] **Step 3: Implementar em `criar_rotas_diarias.py`**

(a) Import: junto dos imports de `roteirizacao` (após a linha 94 `from otimizacao_rotas import ordenar_2opt`) adicionar `from polimento_rotas import polir_entre_rotas`.

(b) Constantes, após `PARTICAO_GERAL = "Geral"`:

```python
# Polimento entre rotas (Hugo, 18/09 -- ver polimento_rotas.py): roda
# depois do modelo vencedor e da fusao de sublotes pequenos. False =
# retorno rapido ao comportamento anterior. Teto de tempo por particao
# pra nao estourar a janela das 22h.
POLIMENTO_ATIVO = True
# 15 s, medido em 20/09 com 18 rotas / 171 paradas entrelacadas (o maior
# dia real): teto de 3 s tirava o entrelacamento de 99% pra 87%, 15 s
# chega perto de 61%, e de 15 s pra 30 s o km so melhora ~1% -- a curva
# satura ai. Desde a particao unica o teto e pago UMA vez por execucao,
# e o job das 22h roda por timer, sem pressa.
POLIMENTO_TEMPO_MAXIMO_S = 15.0
```

(c) Após `_fundir_sublotes_entre_macrorregioes` (depois da linha 318) adicionar:

```python
def _polir_particao(sublotes: list[list[dict]], coords_base, gmaps_key: str | None, label: str,
                    tamanho_maximo: int = TAMANHO_MAXIMO_ROTA) -> list[list[dict]]:
    """Polimento entre rotas de UMA particao (18/09). Sem base
    geocodificada ou com menos de 2 rotas nao ha o que polir."""
    if not POLIMENTO_ATIVO or not coords_base or len(sublotes) < 2:
        return sublotes
    polidos, resumo = polir_entre_rotas(
        sublotes, coords_base[0], coords_base[1], gmaps_key,
        tamanho_maximo=tamanho_maximo, volume_maximo=VOLUME_MAXIMO_ROTA,
        distancia_maxima_km=DISTANCIA_MAXIMA_ROTA_KM,
        distancia_maxima_viagem_km=DISTANCIA_MAXIMA_VIAGEM_KM,
        eh_viagem_fn=lambda sub: classificar_rota_viagem(sub, gmaps_key),
        tempo_maximo_s=POLIMENTO_TEMPO_MAXIMO_S,
    )
    logger.info(f"[{label}] Polimento entre rotas: {len(sublotes)} -> {len(polidos)} rota(s), "
                f"{resumo['realocacoes']} realocacao(oes), {resumo['trocas']} troca(s), "
                f"{resumo['esvaziadas']} esvaziada(s), km {resumo['km_antes']:.1f} -> {resumo['km_depois']:.1f} "
                f"em {resumo['tempo_s']:.1f}s{' (teto de tempo atingido)' if resumo['estourou_tempo'] else ''}.")
    return polidos


def planejar_sublotes(servicos: list[dict], coords_base, gmaps_key: str | None, data_alvo: date, *,
                      sufixo_label: str = "", modelo_forcado: str | None = None,
                      tamanho_maximo: int = TAMANHO_MAXIMO_ROTA,
                      registrar_historico: bool = True) -> list[dict]:
    """Miolo UNICO do criador de rotas (18/09): particao (tipo de carga,
    ver SEPARAR_POR_TIPO_CARGA) -> selecao diaria de modelo (ou fluxo de
    reserva sem base) -> fusao de sublotes pequenos entre macro-regioes
    -> polimento entre rotas. Usado por main() (job das 22h), por
    roteirizar_para_rascunhos (botao Roteirizar) e pelo replay
    (replay_rotas.py, com registrar_historico=False). Os servicos ja
    chegam classificados (_nivel_dificuldade, _tipo_carga, janelas) e
    a base, quando existe, ja foi registrada com definir_coords_base.
    Devolve [{"label", "modelo", "sublotes"}], uma entrada por particao
    nao vazia, sublotes ja sequenciados."""
    particoes = _particionar_carga_com_fusao(servicos, TAMANHO_MINIMO_ROTA, gmaps_key)
    planos: list[dict] = []
    for label, servicos_particao in particoes:
        if not servicos_particao:
            continue
        rotulo = f"{label}{sufixo_label}"
        if coords_base:
            modelo, sublotes = escolher_melhor_modelo(
                servicos_particao, coords_base[0], coords_base[1], gmaps_key,
                data_alvo=data_alvo, label=rotulo,
                tamanho_minimo=TAMANHO_MINIMO_ROTA, tamanho_maximo=tamanho_maximo,
                volume_maximo=VOLUME_MAXIMO_ROTA,
                distancia_maxima_km=DISTANCIA_MAXIMA_ROTA_KM,
                distancia_maxima_viagem_km=DISTANCIA_MAXIMA_VIAGEM_KM,
                modelo_forcado=modelo_forcado,
                distancia_maxima_fusao_regiao_km=DISTANCIA_MAXIMA_FUSAO_REGIAO_KM,
                km_acumulado_maximo=KM_ACUMULADO_MAXIMO_ROTA_KM,
                km_acumulado_maximo_viagem=KM_ACUMULADO_MAXIMO_VIAGEM_KM,
                registrar_historico=registrar_historico,
            )
        else:
            # Fluxo de reserva quando a base nao geocodifica -- mesmo
            # esquema "Atual", reaproveitado de selecao_modelo.py.
            modelo = "Atual (Grade+Greedy)"
            particoes_macro = particionar_por_macro_regiao(
                servicos_particao, gmaps_key, tamanho_minimo=TAMANHO_MINIMO_ROTA,
                distancia_maxima_fusao_km=DISTANCIA_MAXIMA_FUSAO_REGIAO_KM,
            )
            sublotes = [
                sub for svcs in particoes_macro.values()
                for sub in agrupar_atual(svcs, gmaps_key, TAMANHO_MINIMO_ROTA, tamanho_maximo,
                                         VOLUME_MAXIMO_ROTA, DISTANCIA_MAXIMA_ROTA_KM, DISTANCIA_MAXIMA_VIAGEM_KM,
                                         KM_ACUMULADO_MAXIMO_ROTA_KM, KM_ACUMULADO_MAXIMO_VIAGEM_KM)
            ]
        sublotes = _fundir_sublotes_entre_macrorregioes(sublotes, coords_base, gmaps_key, rotulo)
        sublotes = _polir_particao(sublotes, coords_base, gmaps_key, rotulo, tamanho_maximo)
        planos.append({"label": label, "modelo": modelo, "sublotes": sublotes})
    return planos
```

(d) Em `roteirizar_para_rascunhos`: apagar da linha 385 (`particoes = _particionar_carga_com_fusao(...)`) até a linha 433 (`sublotes = _fundir_sublotes_entre_macrorregioes(...)`), **mantendo** o bloco de geocodificação da base (linhas 387-395), e substituir por:

```python
    coords_base = None
    try:
        coords_base = geocodificar(ENDERECO_BASE, gmaps_key)
    except Exception as e:
        logger.warning(f"Não consegui geocodificar a base -- seguindo com o agrupamento fixo: {e}")
    if coords_base:
        # orçamento de horas passa a contar a perna base -> 1ª parada
        # (25/08) em todo agrupamento/fusão deste processo
        definir_coords_base(*coords_base)

    planos = planejar_sublotes(servicos, coords_base, gmaps_key, data_alvo, sufixo_label=sufixo_label,
                               modelo_forcado=modelo_forcado, tamanho_maximo=tamanho_maximo_efetivo)

    indice = indice_inicial
    rascunhos: list[dict] = []
    for plano in planos:
        label, sublotes = plano["label"], plano["sublotes"]
        for sublote in sublotes:
```
(o corpo do `for sublote in sublotes:` continua igual, da linha 436 em diante, já com `"particao": rotulo_carga(sublote)` da Task 5). Atualizar o docstring da função: "partição Seco x Refrigerado/Congelado, seleção de modelo + 2-opt" vira "planejar_sublotes (partição, seleção de modelo, fusão, polimento)".

(e) Em `main()`: apagar as linhas 627-629 (partição + log) e, dentro de `_rotear_particao`, apagar da linha 659 até a 696 (seleção/fallback/fusão), mudando a assinatura pra `def _rotear_particao(label: str, sublotes_do_dia: list[list[dict]]):` e mantendo `nonlocal` + o `sorted(...)` de escassez + o laço. Trocar as linhas 811-812 por:

```python
        planos = planejar_sublotes(servicos, coords_base, gmaps_key, data_alvo)
        for plano in planos:
            logger.info(f"Partição '{plano['label']}': modelo {plano['modelo']}, {len(plano['sublotes'])} rota(s).")
            modelos_vencedores[plano["label"]] = plano["modelo"]
            _rotear_particao(plano["label"], plano["sublotes"])
```

Conferir que `escolher_melhor_modelo`, `agrupar_atual` e `particionar_por_macro_regiao` continuam importados (usados por `planejar_sublotes`).

- [ ] **Step 4: Rodar**

Run: `py -3.11 -m unittest roteirizacao.test_planejar_sublotes roteirizacao.test_orcamento_horas roteirizacao.test_particao_carga roteirizacao.test_nivel4_veiculo_grande painel_agentes.test_incrementar_rascunhos -v`
Expected: OK.

- [ ] **Step 5: Prova de fumaça do job em modo teste (não toca Vuupt)**

Run (da raiz): `py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste`
Expected: termina sem traceback; no log aparecem `Partição 'Geral'`, `Modelo VENCEDOR` e `Polimento entre rotas`. (Ele lê a Vuupt de verdade só pra listar; em `--modo-teste` não cria nada.) Se não houver pedido not_assigned no momento, o log diz "Nenhum pedido not_assigned elegível" e isso basta como prova de que o módulo importa e roda.

- [ ] **Step 6: Checkpoint (sem commit)**

`py -3.11 -m py_compile roteirizacao/criar_rotas_diarias.py`. Arquivos: `roteirizacao/criar_rotas_diarias.py`, `roteirizacao/test_planejar_sublotes.py`.

---

### Task 8: Replay de 30 dias (`replay_rotas.py`) + calibração da distância máxima

**Files:**
- Create: `roteirizacao/replay_rotas.py`
- Test: `roteirizacao/test_replay_rotas.py`
- Dados: `dados/dados_replay.db` (cópia da VPS; já coberto por `dados/*` no `.gitignore`)

**Interfaces:**
- Consumes: `planejar_sublotes` (Task 7), `metricas_plano` (Task 1), `definir_coords_base`, `definir_hora_saida_base`, `HORA_INICIO_ROTA`, `carregar_tipos_carga_por_sender`, `classificar_tipo_carga`, `estimar_tempo_rota`, `exige_orcamento_horas`.
- Produces: `servicos_do_dia(conn, dia: str, mapa_tipos: dict) -> tuple[list[dict], list[list[dict]]]` (todos os serviços do dia e as rotas enviadas, na ordem enviada); `rodar_dia(servicos, rotas_enviadas, coords_base, data_alvo) -> tuple[dict, dict]` (métricas enviado, métricas novo); CLI `--de --ate --banco --distancia-maxima --sem-polimento --separar-carga --modelo`.

- [ ] **Step 1: Escrever o teste da leitura do banco**

Criar `roteirizacao/test_replay_rotas.py`:

```python
# -*- coding: utf-8 -*-
"""replay_rotas: leitura de rascunhos ENVIADOS -> dicts de servico no
formato que o pipeline aceita.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_replay_rotas -v"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import replay_rotas as rr


def _banco():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, data_alvo TEXT, status TEXT, criado_em TEXT);
        CREATE TABLE rascunhos_parada (id INTEGER PRIMARY KEY, rascunho_id INTEGER, ordem INTEGER,
            service_id INTEGER, codigo TEXT, titulo TEXT, endereco TEXT, latitude REAL, longitude REAL,
            sender_id INTEGER, destinatario_nome TEXT, nivel_dificuldade INTEGER, volume_caixas INTEGER,
            janela_inicio TEXT, janela_fim TEXT, janela_fonte TEXT);
        INSERT INTO rascunhos_rota VALUES (1, '2026-09-17', 'ENVIADO', '2026-09-16 22:00'),
                                          (2, '2026-09-17', 'ENVIADO', '2026-09-16 22:00'),
                                          (3, '2026-09-17', 'DESCARTADO', '2026-09-16 21:00');
        INSERT INTO rascunhos_parada VALUES
            (1, 1, 1, 100, '#PS-100', 'A', 'Rua A, Sao Paulo - SP, 01000-000, Brasil', -23.50, -46.60, 7, 'Cli A', 2, 3, '09:00', '12:00', 'teste'),
            (2, 1, 0, 101, 'PS-101', 'B', 'Rua B, Sao Paulo - SP, 01000-000, Brasil', -23.51, -46.61, 7, 'Cli B', 1, 1, NULL, NULL, NULL),
            (3, 2, 0, 102, 'PS-102', 'C', 'Rua C, Sao Paulo - SP, 01000-000, Brasil', -23.52, -46.62, 8, 'Cli C', 3, 5, NULL, NULL, NULL),
            (4, 3, 0, 999, 'PS-999', 'X', 'Rua X', -23.0, -46.0, 8, 'Cli X', 1, 1, NULL, NULL, NULL);
    """)
    return conn


class LeituraTestCase(unittest.TestCase):

    def test_servicos_e_rotas_enviadas(self):
        servicos, rotas = rr.servicos_do_dia(_banco(), "2026-09-17", {7: "Seco", 8: "Refrigerado"})
        self.assertEqual(sorted(s["id"] for s in servicos), [100, 101, 102])
        self.assertEqual([[s["id"] for s in r] for r in rotas], [[101, 100], [102]])
        s100 = next(s for s in servicos if s["id"] == 100)
        self.assertEqual(s100["code"], "#PS-100")
        self.assertEqual(s100["dimension_3"], 3)
        self.assertEqual(s100["_nivel_dificuldade"], 2)
        self.assertEqual(s100["_tipo_carga"], "Seco")
        self.assertEqual((s100["_janela_inicio"], s100["_janela_fim"]), ("09:00", "12:00"))
        self.assertEqual((s100["latitude"], s100["longitude"]), (-23.50, -46.60))
        self.assertEqual(next(s for s in servicos if s["id"] == 102)["_tipo_carga"], "Refrigerado")

    def test_dia_sem_rotas(self):
        servicos, rotas = rr.servicos_do_dia(_banco(), "2026-09-18", {})
        self.assertEqual((servicos, rotas), ([], []))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_replay_rotas -v`
Expected: `ModuleNotFoundError: No module named 'replay_rotas'`.

- [ ] **Step 3: Implementar `roteirizacao/replay_rotas.py`**

```python
# -*- coding: utf-8 -*-
"""
replay_rotas.py

Replay da roteirizacao sobre dias PASSADOS (Hugo, 18/09): le as rotas
ENVIADAS de cada dia (rascunhos_rota/rascunhos_parada de uma copia do
banco de producao), roda o mesmo criar_rotas_diarias.planejar_sublotes
que a producao usa (sem Vuupt, sem geocodificar -- coordenadas ja vem
gravadas -- sem motorista, sem gravar nada, sem escrever no historico)
e compara as metricas (metricas_plano) do "enviado de verdade" com o
"novo". Serve pra calibrar constantes antes de subir.

Copia do banco (na VPS, consistente mesmo com WAL):
    ssh ... "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python -c \\
      \"import sqlite3; s=sqlite3.connect('dados/dados.db'); d=sqlite3.connect('/tmp/dados_replay.db'); s.backup(d)\""
    scp ... root@187.127.52.197:/tmp/dados_replay.db dados/dados_replay.db

COMO USAR (da raiz):
    py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-17
    py -3.11 roteirizacao/replay_rotas.py --de 2026-09-11 --ate 2026-09-17 --distancia-maxima 12
    py -3.11 roteirizacao/replay_rotas.py --de ... --ate ... --sem-polimento --separar-carga
Saida: tabela no terminal + roteirizacao/dados/replay_resultado.txt

Limite: roteiriza o conjunto que FOI enviado no dia, nao o pool inteiro
que o job viu as 22h (pedido removido/adiado a mao fica de fora). A
comparacao e justa porque os dois lados usam o mesmo conjunto.
"""
import argparse
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
sys.path.insert(0, str(_RAIZ_PROJETO / "painel_agentes"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("replay_rotas")

from regras.tipo_carga_embarcador import carregar_tipos_carga_por_sender, classificar_tipo_carga
import roteirizacao_dados as rd
import metricas_plano as mp

COORDS_BASE_PADRAO = (-23.4930, -46.6640)  # Rua Zilda, Casa Verde Alta (aprox.); use --base pra sobrescrever
ARQUIVO_RESULTADO = _RAIZ_LOCAL / "dados" / "replay_resultado.txt"


def servicos_do_dia(conn: sqlite3.Connection, dia: str, mapa_tipos: dict) -> tuple[list[dict], list[list[dict]]]:
    """(todos os servicos do dia, rotas enviadas na ordem enviada) a
    partir dos rascunhos com status ENVIADO. Dict no formato que o
    pipeline aceita (chaves da Vuupt + as injetadas '_...')."""
    rotas_rows = conn.execute(
        "SELECT id FROM rascunhos_rota WHERE data_alvo = ? AND status = 'ENVIADO' ORDER BY criado_em, id",
        (dia,)).fetchall()
    servicos: list[dict] = []
    rotas: list[list[dict]] = []
    for r in rotas_rows:
        paradas = conn.execute(
            "SELECT * FROM rascunhos_parada WHERE rascunho_id = ? ORDER BY ordem", (r["id"],)).fetchall()
        rota: list[dict] = []
        for p in paradas:
            tipo, _ = classificar_tipo_carga(p["sender_id"], mapa_tipos)
            s = {
                "id": p["service_id"], "code": p["codigo"] or "", "title": p["titulo"] or "",
                "address": p["endereco"] or "", "latitude": p["latitude"], "longitude": p["longitude"],
                "sender_id": p["sender_id"], "dimension_3": p["volume_caixas"],
                "customer": {"code": "", "name": p["destinatario_nome"] or ""},
                "_nivel_dificuldade": p["nivel_dificuldade"] or 1, "_tipo_carga": tipo,
                "_janela_inicio": p["janela_inicio"], "_janela_fim": p["janela_fim"],
                "_janela_fonte": p["janela_fonte"],
            }
            rota.append(s)
            servicos.append(s)
        if rota:
            rotas.append(rota)
    return servicos, rotas


def _coords(s: dict):
    return rd.coordenada_embutida(s)


def _horas(sublotes: list[list[dict]]) -> list[float]:
    return [rd.estimar_tempo_rota(sub) if rd.exige_orcamento_horas(sub) else 0.0 for sub in sublotes]


def rodar_dia(servicos: list[dict], rotas_enviadas: list[list[dict]], coords_base: tuple[float, float],
              data_alvo: date, modelo_forcado: str | None = None) -> tuple[dict, dict]:
    """(metricas do enviado, metricas do plano novo) pro mesmo conjunto."""
    import criar_rotas_diarias as crd
    rd.definir_coords_base(*coords_base)
    rd.definir_hora_saida_base(rd.HORA_INICIO_ROTA)
    enviado = mp.metricas_plano(mp.plano_de_sublotes(rotas_enviadas, _coords), coords_base,
                                horas=_horas(rotas_enviadas), teto_horas=rd.ROTA_TEMPO_MAXIMO_HORAS)
    planos = crd.planejar_sublotes([dict(s) for s in servicos], coords_base, None, data_alvo,
                                   sufixo_label=" (replay)", modelo_forcado=modelo_forcado,
                                   registrar_historico=False)
    sublotes = [sub for p in planos for sub in p["sublotes"]]
    novo = mp.metricas_plano(mp.plano_de_sublotes(sublotes, _coords), coords_base,
                             horas=_horas(sublotes), teto_horas=rd.ROTA_TEMPO_MAXIMO_HORAS)
    return enviado, novo


def _somar(acumulado: dict, m: dict) -> None:
    """Soma as metricas somaveis do dia no acumulado. A lista de chaves
    tem que cobrir TODA chave somavel que formatar_metricas imprime --
    inclusive rotas_sem_coordenada, senao o TOTAL levanta KeyError."""
    for chave in ("rotas", "paradas", "rotas_pequenas", "km_total", "cruzadas", "pares_cruzados",
                  "rotas_acima_teto", "rotas_sem_coordenada"):
        acumulado[chave] = acumulado.get(chave, 0) + m[chave]
    acumulado.setdefault("diametros", []).append(m["diametro_mediano_km"])


def _fechar(acumulado: dict) -> dict:
    diam = sorted(acumulado.pop("diametros", [0.0]))
    rotas = acumulado.get("rotas", 0) or 1
    paradas = acumulado.get("paradas", 0)
    return {**acumulado, "media_paradas": paradas / rotas, "min_paradas": 0, "max_paradas": 0,
            "diametro_mediano_km": diam[len(diam) // 2], "diametro_max_km": diam[-1], "raio_medio_km": 0.0,
            "cruzadas_pct": (acumulado.get("cruzadas", 0) / paradas * 100.0) if paradas else 0.0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Replay da roteirizacao sobre dias passados (somente leitura)")
    parser.add_argument("--de", required=True)
    parser.add_argument("--ate", required=True)
    parser.add_argument("--banco", default=str(_RAIZ_PROJETO / "dados" / "dados_replay.db"))
    parser.add_argument("--base", default=None, help="lat,lng da base (padrao: Rua Zilda aprox.)")
    parser.add_argument("--distancia-maxima", type=float, default=None, help="sobrescreve DISTANCIA_MAXIMA_ROTA_KM")
    parser.add_argument("--sem-polimento", action="store_true")
    parser.add_argument("--separar-carga", action="store_true", help="religa a particao Seco x Refrigerado")
    parser.add_argument("--modelo", default=None, help="forca um esquema (nome como no historico)")
    args = parser.parse_args(argv)

    import criar_rotas_diarias as crd
    if args.distancia_maxima is not None:
        crd.DISTANCIA_MAXIMA_ROTA_KM = args.distancia_maxima
    if args.sem_polimento:
        crd.POLIMENTO_ATIVO = False
    if args.separar_carga:
        crd.SEPARAR_POR_TIPO_CARGA = True
    coords_base = tuple(float(x) for x in args.base.split(",")) if args.base else COORDS_BASE_PADRAO

    conn = sqlite3.connect(f"file:{args.banco}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    mapa_tipos = carregar_tipos_carga_por_sender(args.banco)

    cabecalho = (f"Replay {args.de} a {args.ate} | distancia_maxima={crd.DISTANCIA_MAXIMA_ROTA_KM} km | "
                 f"polimento={'off' if args.sem_polimento else 'on'} | separar_carga={'on' if args.separar_carga else 'off'}"
                 f"{' | modelo=' + args.modelo if args.modelo else ''}")
    linhas = [cabecalho, ""]
    total_env: dict = {}
    total_novo: dict = {}
    dia = date.fromisoformat(args.de)
    fim = date.fromisoformat(args.ate)
    while dia <= fim:
        servicos, rotas = servicos_do_dia(conn, dia.isoformat(), mapa_tipos)
        if len(servicos) >= 2:
            try:
                env, novo = rodar_dia(servicos, rotas, coords_base, dia, args.modelo)
            except Exception as e:
                linhas.append(f"{dia}: ERRO {e}")
                dia += timedelta(days=1)
                continue
            _somar(total_env, env)
            _somar(total_novo, novo)
            linhas.append(mp.formatar_metricas(env, f"{dia} enviado"))
            linhas.append(mp.formatar_metricas(novo, f"{dia} novo   "))
        dia += timedelta(days=1)
    if total_env:
        linhas += ["", mp.formatar_metricas(_fechar(total_env), "TOTAL enviado"),
                   mp.formatar_metricas(_fechar(total_novo), "TOTAL novo   ")]
    texto = "\n".join(linhas)
    print(texto)
    ARQUIVO_RESULTADO.parent.mkdir(parents=True, exist_ok=True)
    with open(ARQUIVO_RESULTADO, "a", encoding="utf-8") as f:
        f.write(texto + "\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Rodar o teste**

Run: `py -3.11 -m unittest roteirizacao.test_replay_rotas -v`
Expected: 2 OK.

- [ ] **Step 5: Copiar o banco de produção (somente leitura)**

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python -c \"import sqlite3; s=sqlite3.connect('dados/dados.db'); d=sqlite3.connect('/tmp/dados_replay.db'); s.backup(d); d.close(); s.close()\" && chmod 644 /tmp/dados_replay.db && ls -la /tmp/dados_replay.db"
scp -i ~/.ssh/atendimento_vps root@187.127.52.197:/tmp/dados_replay.db dados/dados_replay.db
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "rm -f /tmp/dados_replay.db"
```
Conferir: `git status --short dados/` não mostra o arquivo (ignorado).

- [ ] **Step 6: Rodar o replay nas 4 distâncias e montar a tabela**

```bash
py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-17 --distancia-maxima 20
py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-17 --distancia-maxima 15
py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-17 --distancia-maxima 12
py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-17 --distancia-maxima 10
py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-17 --distancia-maxima 20 --sem-polimento --separar-carga
```
(a última reproduz as regras antigas menos a sequência, como controle). Cada rodada leva de 1 a 5 min (30 dias x 5 modelos). Se um dia der `ERRO`, investigar antes de seguir: o replay não pode pular dia em silêncio.

Montar pra o Hugo uma tabela com uma linha por rodada (TOTAL novo) e a linha TOTAL enviado: rotas, paradas/rota, rotas pequenas, diâmetro mediano, % cruzadas, pares cruzados, km, rotas acima de 9h. Critério de aceite da spec (§5.1): cruzadas cai pelo menos pela metade, diâmetro mediano não sobe, rotas pequenas caem, km não sobe mais que 5%, 0 rotas acima de 9h fora das exceções.

- [ ] **Step 7: Perguntar ao Hugo o valor de `DISTANCIA_MAXIMA_ROTA_KM`**

Apresentar a tabela e perguntar (AskUserQuestion) qual distância fica. Aplicar o valor em `roteirizacao/criar_rotas_diarias.py:126` **e** em `painel_agentes/planejamento_rotas.py:177` (cópia do badge), com o comentário `# Hugo, <data>: calibrado pelo replay de 30 dias (ver DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md §9)`.

- [ ] **Step 8: Checkpoint (sem commit)**

`py -3.11 -m py_compile roteirizacao/replay_rotas.py roteirizacao/criar_rotas_diarias.py painel_agentes/planejamento_rotas.py`. Arquivos: `roteirizacao/replay_rotas.py`, `roteirizacao/test_replay_rotas.py`, os dois de constante. `roteirizacao/dados/replay_resultado.txt` é ignorado pelo git (`roteirizacao/dados/`).

---

### Task 9: Documentação, docstring desatualizado e verificação completa

**Files:**
- Modify: `DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md` (adendo ao final)
- Modify: `roteirizacao/roteirizacao_dados.py:50` (docstring que diz 70 km)

- [ ] **Step 1: Corrigir o docstring do raio e a limpeza de texto que as revisões acharam**

Em `roteirizacao_dados.py`, no docstring perto da linha 50 que menciona "70 km" pro raio da Grande SP, trocar por "RAIO_GRANDE_SP_KM (35 km desde 20/08, ver regioes_dia_fixo.py)".

Mais estes itens, todos achados pelas revisões das tasks anteriores (localizar por âncora de texto, os números de linha mudaram):

1. `roteirizacao/selecao_modelo.py`: o texto novo (docstring do módulo, de `_escolher_vencedor` e de `escolher_melhor_modelo`) ficou **sem acento** num arquivo que usa acentuação plena em todo o resto ("Seleção diária", "auditável"). O CLAUDE.md manda seguir o padrão do arquivo: **acentuar** esses trechos novos.
2. `roteirizacao/criar_rotas_diarias.py`: se sobrou algum comentário ou string dizendo "farthest-first", "mais longe primeiro" ou "(mais longe -> mais perto da base)", corrigir. Conferir com `grep -n "farthest\|mais longe" roteirizacao/criar_rotas_diarias.py` — a Task 7 reescreve parte desses trechos, então este passo é a varredura final.
3. `painel_agentes/rascunhos_rota.py` (por volta da linha 417): há um comentário justificando **não** reaproveitar `calcular_km_estimado` porque "obter_coordenadas geocodifica pelo campo address e ignora latitude/longitude já presentes no dict". Isso **deixou de ser verdade** na Task 2. Corrigir o comentário (não mexer no código: a decisão de não reusar pode ter outros motivos, então o comentário deve dizer apenas que aquela premissa caducou em 18/09).
4. Conferir que nenhuma docstring do pacote ainda promete "a 1ª parada nunca se move" ou "volta à base": `grep -rn "volta a base\|volta à base\|1. parada nunca" roteirizacao/*.py`.
5. `painel_agentes/templates/planejamento_rotas.html` (por volta da linha 2218): o tooltip do botão "Roteirizar" ainda diz que agrupa "Seco separado de Refrigerado/Congelado". Deixou de ser verdade. Corrigir o texto (é atributo de tooltip, não muda comportamento). **Cuidado:** esse arquivo é editado por outras sessões — conferir `git diff` antes e depois, mexer só nessa string.
6. Varredura final de textos que descrevem a partição por tipo de carga como vigente: `grep -rn "Secas deveriam\|Seco separado\|separadas das refrigeradas" roteirizacao/ painel_agentes/ --include=*.py --include=*.html`.

- [ ] **Step 2: Adendo no doc de otimização**

Acrescentar ao final de `DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md`:

```markdown
## 9. Recalibração de 18/09/2026 (spec: docs/superpowers/specs/2026-09-18-recalibracao-roteirizacao-design.md)

Sintoma (Hugo, 17/09): rotas espalhadas, sobrepostas, sequência ruim, poucas paradas por rota. Medido em produção (rotas enviadas 11-17/09): 19-31% das paradas tinham a vizinha mais próxima em OUTRA rota; 20-45 pares de rotas com bolhas cruzadas por dia; média 9,5 paradas/rota com teto 16.

Decisões do Hugo (18/09) e o que mudou:

| Decisão | Onde | Retorno |
|---|---|---|
| Seco e Refrigerado sempre podem ir juntos (frota toda tem baú térmico) | `criar_rotas_diarias.SEPARAR_POR_TIPO_CARGA = False`; `rotulo_carga()` grava o tipo por rota em `rascunhos_rota.particao`; Laboratório ganhou "Todos" | `SEPARAR_POR_TIPO_CARGA = True` |
| Rota compacta vale mais que menos rotas | `selecao_modelo._escolher_vencedor`: menor km total, rotas só desempata | trocar a chave do `min` |
| Sequência livre (revoga "mais longe primeiro" de 03/08) | `roteirizacao_dados.ordenar_com_janelas`: semente vizinho mais próximo, objetivo sem volta à base, 2-opt + or-opt em todas as posições; `calcular_km_estimado` sem volta | sem chave (decisão de negócio) |
| Polimento entre rotas | `roteirizacao/polimento_rotas.py`, chamado por `criar_rotas_diarias._polir_particao` | `POLIMENTO_ATIVO = False` |
| Coordenada embutida vale antes da geocodificação | `roteirizacao_dados.obter_coordenadas` | — |
| Distância máxima entre paradas (Grande SP) | `DISTANCIA_MAXIMA_ROTA_KM` = **15** (era 20), também em `painel_agentes/planejamento_rotas.py` | voltar a 20 |

Miolo único: `criar_rotas_diarias.planejar_sublotes` (partição -> seleção -> fusão -> polimento), usado pelo job das 22h, pelo botão Roteirizar e pelo replay.

Replay e métricas: `roteirizacao/replay_rotas.py` (lê rascunhos ENVIADOS de uma cópia do banco, `dados/dados_replay.db`, e compara "enviado" x "novo") e `roteirizacao/metricas_plano.py`. `benchmark_modelos.py` (§6) ficou como ferramenta antiga; o replay o substitui.

Resultado da calibração — 31 dias de operação real (12/08 a 19/09), 32 dias processados, 3008 paradas, zero erros:

| Configuração | Rotas | Paradas/rota | Pequenas | Diâmetro mediano | Km | Entrelaçadas | Pares cruzados | Acima de 9h |
|---|---|---|---|---|---|---|---|---|
| **Enviado de verdade** | 339 | 8,9 | 116 | 10,6 km | 18.731 | 658 (22%) | 658 | **71** |
| Controle (agrupamento antigo, só sequência nova) | 363 | 8,3 | 145 | 10,6 km | 15.637 (-16,5%) | 724 (24%) | 554 | 0 |
| 20 km | 350 | 8,6 | 133 | 11,1 km | 15.012 (-19,9%) | 631 (21%) | 577 | 0 |
| **15 km (escolhida pelo Hugo, 20/09)** | 372 | 8,1 | 158 | 10,1 km | 15.234 (-18,7%) | 573 (19%) | 499 | 0 |
| 12 km | 406 | 7,4 | 213 | 8,7 km | 15.551 (-17,0%) | 548 (18%) | 398 | 0 |

Três leituras que importam:

1. **71 das 339 rotas enviadas (21%) não cabiam em 9 horas.** Toda configuração nova zera isso, porque o orçamento passa a ser conferido na ordem final, depois do sequenciamento. Esse achado não estava no diagnóstico inicial. Como consequência, 339 não é um número comparável: se aquelas rotas fossem quebradas para caber no dia, o enviado teria mais de 339 rotas, o que torna a comparação conservadora contra o pipeline novo.
2. **O quilômetro cai entre 17% e 20% em qualquer configuração**, e a maior parte disso vem da sequência livre: a rodada de controle, que mantém o agrupamento antigo, já corta 16,5%.
3. **O critério de aceite §5.1 não foi atingido no entrelaçamento** (meta: cair pela metade, 22% para 11%; melhor resultado: 18%). A causa é estrutural: nem o critério de seleção nem o polimento otimizam sobreposição, os dois otimizam quilômetro, e duas rotas paralelas na mesma via podem ter quilômetro baixo e entrelaçamento alto. O Hugo decidiu em 20/09 atacar isso numa fase seguinte, depois de ver esta em produção. A proposta registrada é penalizar sobreposição explicitamente no objetivo, tanto no critério de escolha do modelo quanto no polimento.

Testes: `test_metricas_plano`, `test_coordenadas_embutidas`, `test_sequencia_livre`, `test_selecao_modelo`, `test_particao_carga`, `test_polimento_rotas`, `test_planejar_sublotes`, `test_replay_rotas`, `painel_agentes/test_laboratorio_todos`. **Rodar em dois comandos** (um por pacote): `criar_rotas_diarias.py` insere `painel_agentes/` no `sys.path`, então num mesmo processo `import painel_agentes` passa a resolver para o arquivo em vez do pacote. Pré-existente, não é falha da suíte.

Viés conhecido, aceito em 20/09 e deixado para uma fase seguinte: no sequenciamento **com** janela de horário, uma parada sem coordenada nenhuma pode ser reposicionada pela busca local para "absorver" tempo de espera, porque a distância dela conta zero. No ramo sem janela isso não acontece (a reversão que a envolveria é pulada). É caso de borda — desde a Task 2 quase todo pedido chega com coordenada embutida ou geocodificada — e o comportamento já era assim antes desta recalibração. Está documentado na docstring de `ordenar_com_janelas`.

Bug corrigido durante a implementação, registrado porque explica o desenho do polimento: a primeira versão do esvaziamento de rota pequena recalculava a vizinhança a cada parada movida. Como a vizinhança é filtrada por distância entre centroides, e o centroide da rota de origem se desloca quando ela perde uma parada, uma rota fora do conjunto salvo no backup podia receber a parada e não ser revertida quando o movimento era desfeito, **duplicando o pedido** (reproduzido: entrada com 4 pedidos saía com 5). Por isso a vizinhança do esvaziamento é congelada no início, o backup cobre exatamente as rotas dessa lista, e há uma rede de segurança final que devolve a entrada original se o km de saída piorar. Todo teste novo do módulo verifica o multiconjunto de identificadores da entrada contra o da saída.
```
(preencher `<valor escolhido>` e `<colar a tabela>` com o resultado real da Task 8; sem placeholder no arquivo final).

- [ ] **Step 3: Rodar a suíte inteira da roteirização e do painel afetado**

Em **dois comandos separados**. Misturar os pacotes `roteirizacao` e `painel_agentes` num único comando `unittest` falha com `AttributeError: module 'painel_agentes' has no attribute ...`: `criar_rotas_diarias.py` insere `painel_agentes/` no `sys.path`, então num processo que já importou esse módulo o `import painel_agentes` resolve para o **arquivo** `painel_agentes/painel_agentes.py` em vez do pacote. É pré-existente (reproduzido no HEAD sem nenhuma alteração da spec) e está fora do escopo desta recalibração — mexer na resolução de import do pipeline das 22h por ergonomia de teste é risco desproporcional.

```
py -3.11 -m unittest roteirizacao.test_metricas_plano roteirizacao.test_coordenadas_embutidas roteirizacao.test_sequencia_livre roteirizacao.test_selecao_modelo roteirizacao.test_particao_carga roteirizacao.test_polimento_rotas roteirizacao.test_planejar_sublotes roteirizacao.test_replay_rotas roteirizacao.test_janelas_horario roteirizacao.test_orcamento_horas roteirizacao.test_nivel4_veiculo_grande roteirizacao.test_incrementar_rotas roteirizacao.test_canhoteira_transportadora roteirizacao.test_documentacao_rota -v
py -3.11 -m unittest painel_agentes.test_laboratorio_todos painel_agentes.test_incrementar_rascunhos -v
```
Expected: tudo OK nos dois. Anotar a contagem final somada. Registrar a explicação acima no adendo do doc (Step 2), pra ninguém achar que a suíte está quebrada.

- [ ] **Step 4: Tempo de execução do job**

Medir `py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste` (tempo total no log "finalizada em Xs") e comparar com o último `criar_rotas_diarias.log` de produção (ler via `ssh ... "grep 'finalizada em' /opt/stokki-eventos/roteirizacao/dados/criar_rotas_diarias.log | tail -3"`). Critério (spec §5.4): não passar de 2x.

- [ ] **Step 5: Relatório final pro Hugo e checkpoint**

`git status --short` e listar só os arquivos deste trabalho (todos os tasks). Não commitar: entregar ao Hugo (a) a lista de arquivos, (b) a tabela do replay com o valor escolhido, (c) a contagem de testes, (d) o tempo do job, e perguntar se commita e faz deploy (skill `deploy-vps`: pull + chown + `systemctl restart painel-agentes`, sem timer novo; prova real na primeira noite comparando a linha nova do `selecao_modelo_historico.txt`).
