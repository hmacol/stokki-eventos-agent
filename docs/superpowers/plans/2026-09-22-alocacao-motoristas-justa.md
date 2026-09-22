# Alocacao de motoristas justa + FIORINO — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cadastrar FIORINO como tipo de veiculo, acionar veiculo grande so quando 1 endereco passa de 100 caixas, e fazer a alocacao automatica rodar os motoristas de forma justa (7d/30d) com rodizio proprio para rotas longas.

**Architecture:** Tres blocos independentes que se encontram em `roteirizacao/alocacao_motoristas.py`. (1) `regras/tipo_veiculo.py` ganha FIORINO e faixas contiguas, com uma lista separada para os tipos que geram rota exclusiva — FIORINO nunca pode sair de `classificar_tipo_veiculo`. (2) `roteirizacao_dados._extrair_grupos_veiculo_grande` perde o crescimento por enderecos vizinhos e passa a extrair por endereco. (3) `selecionar_motorista_equitativo` troca a chave de ordenacao `(carga, agent_id)` por uma que inclui rotas longas em 7d, rotas em 7d e rotas em 30d — sempre como ordenacao, nunca como filtro.

**Tech Stack:** Python 3.11 (`py -3.11`), SQLite (`dados/dados.db`), pandas + openpyxl (planilha de motoristas), unittest (sem pytest no projeto).

**Spec:** `docs/superpowers/specs/2026-09-22-alocacao-motoristas-justa-design.md`

## Global Constraints

- **Interpretador:** sempre `py -3.11` local. Nunca `python` ou `py` sem versao.
- **Testes:** `py -3.11 -m unittest <pacote>.<modulo> -v`, executado **da raiz** do projeto (`C:\agente_stokki_eventos`). Nao ha pytest.
- **Idioma:** codigo, comentarios, docstrings e mensagens de commit em portugues. Comentarios e strings de log **sem acento** nos arquivos que ja seguem esse padrao (`regras/tipo_veiculo.py` usa acento; `regras/prioridade_ofertas.py` usa acento; seguir o arquivo).
- **Commits:** formato `Area: o que mudou e por que`. So `git add` dos arquivos da propria task — **nunca** `git add -A`. Rodar `git status --short` antes de cada commit: o working tree tem ~17 arquivos modificados por outra sessao que **nao** podem entrar em nenhum commit deste plano.
- **Nao fazer deploy.** Nenhuma task deste plano sobe para a VPS. O portao é a Task 6 (replay), cujo resultado vai para o Hugo decidir.
- **`VOLUME_MAXIMO_ROTA = 100`** (`roteirizacao/criar_rotas_diarias.py:127`) é o teto da rota comum e o numero que define o gatilho de veiculo grande.
- **Limiar de rota longa:** 7.0 horas, configuravel em `config.yaml` como `roteirizacao.rota_longa_horas`.
- **Janelas de justica:** 7 e 30 dias, ja em `config.yaml` como `marketplace_rotas.janela_curta_dias` / `janela_longa_dias`.
- **FIORINO nunca pode ser devolvido por `classificar_tipo_veiculo`.** Esse retorno é o gatilho de "vira rota exclusiva" em ~12 pontos do pipeline.

## Mapa das fases da spec

| Fase da spec (§10) | Tasks |
|---|---|
| 1. FIORINO no catalogo | Task 1 |
| 2. Planilha e leitura | Tasks 2 e 3 |
| 3. Regra de 1 endereco | Tasks 4 e 5 |
| 4. **Replay — portao** | Task 6 |
| 5. `horas_estimadas` e contagem | Tasks 7 e 8 |
| 6. Ordem nova na alocacao | Tasks 9 e 10 |

**A Task 6 é um portao humano.** As Tasks 7 a 10 nao comecam sem o Hugo aprovar os numeros do replay.

---

### Task 1: FIORINO no catalogo de veiculos e faixas contiguas

**Files:**
- Modify: `regras/tipo_veiculo.py` (dataclass `:25-33`, `TIPOS_VEICULO` `:40-49`, derivadas `:51-64`, `classificar_tipo_veiculo` `:90-102`, `teto_caixas_para_enderecos` `:78-87`)
- Test: `regras/test_tipo_veiculo_fiorino.py` (criar)

**Interfaces:**
- Consumes: nada (primeira task)
- Produces:
  - `TipoVeiculo` com campo novo `gera_rota_exclusiva: bool = True`
  - `TIPOS_VEICULO: list[TipoVeiculo]` — 5 tipos, capacidade crescente, FIORINO primeiro
  - `TIPOS_VEICULO_EXCLUSIVOS: list[TipoVeiculo]` — os 4 que geram rota exclusiva
  - `ordem_capacidade(codigo: str | None) -> int | None`
  - `classificar_tipo_veiculo(caixas: int, enderecos_distintos: int) -> TipoVeiculo | None` — nunca devolve FIORINO
  - `veiculo_comporta(tipo_motorista: str | None, tipo_necessario: str | None) -> bool` — assinatura inalterada

- [ ] **Step 1: Escrever o teste que falha**

Criar `regras/test_tipo_veiculo_fiorino.py`:

```python
# -*- coding: utf-8 -*-
"""
test_tipo_veiculo_fiorino.py

FIORINO entrou no catalogo em 22/09/2026 (pedido do Hugo) como o veiculo
padrao da ultima milha, com 100 caixas -- o mesmo teto da rota comum
(VOLUME_MAXIMO_ROTA). Estes testes travam as duas propriedades que nao
podem quebrar:

  1. FIORINO NUNCA sai de classificar_tipo_veiculo. Esse retorno e o
     gatilho de "vira rota exclusiva de veiculo grande" em ~12 pontos do
     pipeline -- se FIORINO vazar, toda rota comum vira exclusiva.
  2. As faixas ficaram CONTIGUAS (o volume_minimo virou o teto do tipo
     anterior), fechando o buraco de 101-149 caixas que nao tinha veiculo.

Rodar (da raiz):
    py -3.11 -m unittest regras.test_tipo_veiculo_fiorino -v
"""
import unittest

from regras.tipo_veiculo import (
    TIPOS_VEICULO,
    TIPOS_VEICULO_EXCLUSIVOS,
    classificar_tipo_veiculo,
    ordem_capacidade,
    teto_caixas_para_enderecos,
    tipo_por_codigo,
    veiculo_comporta,
)


class TestFiorinoNoCatalogo(unittest.TestCase):
    def test_fiorino_existe_e_e_o_menor(self):
        self.assertEqual(TIPOS_VEICULO[0].codigo, "FIORINO")
        self.assertEqual(TIPOS_VEICULO[0].volume_maximo_cx, 100)
        self.assertEqual(len(TIPOS_VEICULO), 5)

    def test_fiorino_fora_dos_exclusivos(self):
        codigos = [t.codigo for t in TIPOS_VEICULO_EXCLUSIVOS]
        self.assertNotIn("FIORINO", codigos)
        self.assertEqual(codigos, ["VAN_HR", "VUC", "TRES_QUARTOS", "TRUCK"])

    def test_apelidos_resolvem(self):
        for apelido in ("FIORINO", "fiorino", "FIO", "UTILITARIO"):
            self.assertEqual(tipo_por_codigo(apelido).codigo, "FIORINO", apelido)

    def test_ordem_capacidade(self):
        self.assertEqual(ordem_capacidade("FIORINO"), 0)
        self.assertEqual(ordem_capacidade("VAN_HR"), 1)
        self.assertEqual(ordem_capacidade("TRUCK"), 4)
        self.assertIsNone(ordem_capacidade(None))
        self.assertIsNone(ordem_capacidade("CARROCA"))


class TestClassificacaoNuncaDevolveFiorino(unittest.TestCase):
    def test_nenhuma_entrada_classifica_fiorino(self):
        for caixas in (0, 1, 50, 99, 100, 101, 400, 401, 2500, 2501, 9999):
            for enderecos in (1, 2, 3, 4, 5):
                tipo = classificar_tipo_veiculo(caixas, enderecos)
                if tipo is not None:
                    self.assertNotEqual(tipo.codigo, "FIORINO", f"{caixas}cx/{enderecos}end")

    def test_ate_o_teto_da_rota_comum_nao_ha_veiculo_grande(self):
        for caixas in (0, 1, 50, 99, 100):
            self.assertIsNone(classificar_tipo_veiculo(caixas, 1), f"{caixas}cx")


class TestFaixasContiguas(unittest.TestCase):
    def test_faixas_por_caixas_com_um_endereco(self):
        esperado = [
            (101, "VAN_HR"), (149, "VAN_HR"), (400, "VAN_HR"),
            (401, "VUC"), (600, "VUC"),
            (601, "TRES_QUARTOS"), (1200, "TRES_QUARTOS"),
            (1201, "TRUCK"), (2500, "TRUCK"),
        ]
        for caixas, codigo in esperado:
            tipo = classificar_tipo_veiculo(caixas, 1)
            self.assertIsNotNone(tipo, f"{caixas}cx deveria classificar")
            self.assertEqual(tipo.codigo, codigo, f"{caixas}cx")

    def test_acima_do_truck_nao_classifica(self):
        self.assertIsNone(classificar_tipo_veiculo(2501, 1))

    def test_buraco_de_101_a_149_fechou(self):
        # Era o caso quebrado ate 22/09: 2 pedidos de 60cx no mesmo
        # endereco (120cx) estouravam o teto de 100 da rota comum e nao
        # alcancavam o minimo de 150 da VAN/HR -- ficavam sem veiculo.
        self.assertEqual(classificar_tipo_veiculo(120, 1).codigo, "VAN_HR")


class TestTetoPorEnderecos(unittest.TestCase):
    def test_teto_ignora_fiorino(self):
        # Truck (2500) comporta ate 2 enderecos; acima disso o teto cai
        # pro maior tipo de 4 enderecos (3/4, 1200).
        self.assertEqual(teto_caixas_para_enderecos(1), 2500)
        self.assertEqual(teto_caixas_para_enderecos(2), 2500)
        self.assertEqual(teto_caixas_para_enderecos(3), 1200)
        self.assertEqual(teto_caixas_para_enderecos(4), 1200)
        self.assertEqual(teto_caixas_para_enderecos(5), 0)


class TestVeiculoComporta(unittest.TestCase):
    def test_fiorino_serve_rota_comum(self):
        self.assertTrue(veiculo_comporta("FIORINO", None))

    def test_fiorino_nao_serve_veiculo_grande(self):
        for necessario in ("VAN_HR", "VUC", "TRES_QUARTOS", "TRUCK"):
            self.assertFalse(veiculo_comporta("FIORINO", necessario), necessario)

    def test_maior_cobre_menor(self):
        self.assertTrue(veiculo_comporta("TRUCK", "FIORINO"))
        self.assertTrue(veiculo_comporta("VUC", "VAN_HR"))
        self.assertTrue(veiculo_comporta("VAN_HR", "VAN_HR"))

    def test_menor_nao_cobre_maior(self):
        self.assertFalse(veiculo_comporta("VAN_HR", "VUC"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar o teste para ver falhar**

Run: `py -3.11 -m unittest regras.test_tipo_veiculo_fiorino -v`
Expected: FAIL — `ImportError: cannot import name 'TIPOS_VEICULO_EXCLUSIVOS'`

- [ ] **Step 3: Implementar em `regras/tipo_veiculo.py`**

Trocar o dataclass (`:25-33`) acrescentando o campo no fim:

```python
@dataclass(frozen=True)
class TipoVeiculo:
    codigo: str                  # identificador estável (planilha de motoristas, tags de rota)
    nome: str                    # nome de exibição
    peso_maximo_kg: int          # documentado, não aplicado na roteirização ainda
    volume_maximo_cx: int
    volume_minimo_cx: int
    max_enderecos_distintos: int
    # FIORINO é o único False: ele é o veículo da rota comum (100 cx é o
    # mesmo teto de VOLUME_MAXIMO_ROTA), não um veículo que justifica
    # sair da roteirização normal. Ver TIPOS_VEICULO_EXCLUSIVOS abaixo.
    gera_rota_exclusiva: bool = True
```

Trocar `TIPOS_VEICULO` (`:40-49`) — FIORINO no topo e faixas contíguas:

```python
# Ordenado por capacidade CRESCENTE -- classificar_tipo_veiculo devolve o
# primeiro que servir (o menor/mais barato que comporta o lote), e
# veiculo_comporta usa essa mesma ordem pra saber se o veículo de um
# motorista "cobre pra cima" (ex: motorista de Truck também serve rota
# classificada VUC).
#
# Faixas CONTÍGUAS desde 22/09/2026 (Hugo): o volume_minimo_cx virou o
# teto do tipo anterior. Antes havia um buraco -- 101 a 149 caixas num
# endereço estourava a rota comum (100) e não alcançava o mínimo da
# VAN/HR (150), ficando sem veículo nenhum.
TIPOS_VEICULO = [
    TipoVeiculo("FIORINO", "Fiorino", peso_maximo_kg=650, volume_maximo_cx=100,
                volume_minimo_cx=0, max_enderecos_distintos=4,
                gera_rota_exclusiva=False),
    TipoVeiculo("VAN_HR", "VAN/HR", peso_maximo_kg=1300, volume_maximo_cx=400,
                volume_minimo_cx=101, max_enderecos_distintos=4),
    TipoVeiculo("VUC", "VUC", peso_maximo_kg=2000, volume_maximo_cx=600,
                volume_minimo_cx=401, max_enderecos_distintos=4),
    TipoVeiculo("TRES_QUARTOS", "3/4", peso_maximo_kg=6000, volume_maximo_cx=1200,
                volume_minimo_cx=601, max_enderecos_distintos=4),
    TipoVeiculo("TRUCK", "Truck", peso_maximo_kg=10000, volume_maximo_cx=2500,
                volume_minimo_cx=1201, max_enderecos_distintos=2),
]

# Tipos que JUSTIFICAM uma rota exclusiva de veículo grande. FIORINO
# fica de fora: classificar_tipo_veiculo devolver FIORINO faria toda
# rota comum (<=100 cx) virar "rota exclusiva" nos ~12 pontos do
# pipeline que testam `classificar_tipo_veiculo(...) is not None`.
TIPOS_VEICULO_EXCLUSIVOS = [t for t in TIPOS_VEICULO if t.gera_rota_exclusiva]
```

Ajustar as derivadas (`:51-64`) — a ordem de capacidade usa TODOS, o volume geral só os exclusivos:

```python
_TIPOS_POR_CODIGO = {t.codigo: t for t in TIPOS_VEICULO}
_ORDEM_CODIGO = {t.codigo: i for i, t in enumerate(TIPOS_VEICULO)}

# Apelidos pro preenchimento manual na planilha de motoristas (coluna
# TIPO_VEICULO) -- "3/4" é a forma que o Hugo realmente usa (não
# "TRES_QUARTOS"); depois de normalizado (maiúsculo, não-alfanumérico
# vira '_', ver regras/preferencias_motoristas.py::_construir_motorista)
# "3/4" chega aqui como "3_4". "UTILITARIO" é como a planilha antiga
# chamava o Fiorino (ver regras/tarifa_motorista.py, mesma tarifa).
_APELIDOS_CODIGO = {
    "3_4": "TRES_QUARTOS", "34": "TRES_QUARTOS",
    "FIO": "FIORINO", "UTILITARIO": "FIORINO",
}

# Maior teto de caixas entre os tipos que geram rota EXCLUSIVA -- usado
# como limite inicial ao tentar crescer um cluster de endereços em
# roteirizacao_dados.py.
VOLUME_MAXIMO_GERAL_CX = max(t.volume_maximo_cx for t in TIPOS_VEICULO_EXCLUSIVOS)
```

Acrescentar `ordem_capacidade` logo depois de `tipo_por_codigo` (`:75`):

```python
def ordem_capacidade(codigo: str | None) -> int | None:
    """Posição do tipo na ordem de capacidade CRESCENTE (FIORINO=0,
    VAN_HR=1, VUC=2, TRES_QUARTOS=3, TRUCK=4), ou None se vazio/não
    reconhecido. Acessor público de _ORDEM_CODIGO -- a alocação precisa
    dessa ordem pra preferir o menor veículo que serve."""
    tipo = tipo_por_codigo(codigo)
    return _ORDEM_CODIGO.get(tipo.codigo) if tipo else None
```

Em `teto_caixas_para_enderecos` (`:86`) e `classificar_tipo_veiculo` (`:98`), trocar `TIPOS_VEICULO` por `TIPOS_VEICULO_EXCLUSIVOS`:

```python
    candidatos = [t.volume_maximo_cx for t in TIPOS_VEICULO_EXCLUSIVOS if t.max_enderecos_distintos >= qtd_enderecos]
```

```python
    for tipo in TIPOS_VEICULO_EXCLUSIVOS:
        if (enderecos_distintos <= tipo.max_enderecos_distintos
                and tipo.volume_minimo_cx <= caixas <= tipo.volume_maximo_cx):
            return tipo
    return None
```

Atualizar a docstring de `classificar_tipo_veiculo` para dizer que FIORINO nunca é devolvido e por quê.

- [ ] **Step 4: Rodar o teste para ver passar**

Run: `py -3.11 -m unittest regras.test_tipo_veiculo_fiorino -v`
Expected: PASS, 12 testes

- [ ] **Step 5: Rodar a suite inteira para medir o estrago**

Run:
```
py -3.11 -m unittest discover -s regras -p "test_*.py" 2>&1 | tail -20
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -20
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -20
py -3.11 -m unittest discover -s nucleo -p "test_*.py" 2>&1 | tail -20
```

Expected: `regras`, `painel_agentes` e `nucleo` **devem passar inteiros**. Em `roteirizacao`, os testes que verificam formacao de veiculo grande (`test_particao_carga.py`, `test_nivel4_veiculo_grande.py`) **podem falhar** — as faixas mudaram de proposito. Anotar exatamente quais falharam e por que, sem corrigir ainda: a correcao é da Task 5.

Se falhar um teste de `nucleo` (tarifa) ou de `regras`, **parar**: é sinal de efeito colateral nao previsto.

- [ ] **Step 6: Commit**

```bash
git status --short
git add regras/tipo_veiculo.py regras/test_tipo_veiculo_fiorino.py
git commit -m "Roteirizacao: FIORINO no catalogo de veiculos e faixas contiguas

FIORINO (100 cx) entra como o menor tipo, fora da lista que classifica
rota exclusiva (TIPOS_VEICULO_EXCLUSIVOS) -- e o veiculo da rota comum,
nao um que justifique sair da roteirizacao normal. Os volume_minimo
viraram o teto do tipo anterior, fechando o buraco de 101-149 caixas
que nao tinha veiculo nenhum. Pedido do Hugo, 22/09.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Motorista sem TIPO_VEICULO é lido como FIORINO

**Files:**
- Modify: `regras/preferencias_motoristas.py:227-254`
- Test: `regras/test_preferencias_motoristas.py` (acrescentar classe)

**Interfaces:**
- Consumes: `tipo_por_codigo` da Task 1 (agora resolve `"FIORINO"`, `"FIO"`, `"UTILITARIO"`)
- Produces: `MotoristaPreferencias.tipo_veiculo` passa a ser **sempre** uma string (nunca `None`) — `"FIORINO"` quando a planilha esta vazia. Tasks seguintes podem contar com isso.

**Nota sobre os cadastros virtuais:** o default do codigo vale para todos, inclusive `LALAMOVE (virtual)` e `Hugo Macol (TESTE)`. Deixa-los com a celula vazia na planilha (Task 3) é so para nao afirmar que um cadastro virtual tem carro — **nao muda comportamento**, porque `None` e `"FIORINO"` sao igualmente inelegiveis a rota de veiculo grande.

- [ ] **Step 1: Escrever o teste que falha**

Acrescentar ao fim de `regras/test_preferencias_motoristas.py` (antes do `if __name__`):

```python
class TestTipoVeiculoPadraoFiorino(unittest.TestCase):
    """TIPO_VEICULO vazio passa a ser FIORINO (Hugo, 22/09) -- 27 dos 30
    motoristas estavam com a celula em branco, e Fiorino e o carro da
    maioria da frota."""

    def _motorista(self, tipo_veiculo):
        return _construir_motorista({
            "AGENT_ID_VUUPT": 1,
            "NOME_MOTORISTA": "Teste",
            "ATIVO": "SIM",
            "TIPO_VEICULO": tipo_veiculo,
        })

    def test_celula_vazia_vira_fiorino(self):
        self.assertEqual(self._motorista(None).tipo_veiculo, "FIORINO")
        self.assertEqual(self._motorista("").tipo_veiculo, "FIORINO")

    def test_fiorino_explicito(self):
        self.assertEqual(self._motorista("FIORINO").tipo_veiculo, "FIORINO")
        self.assertEqual(self._motorista("Fiorino").tipo_veiculo, "FIORINO")
        self.assertEqual(self._motorista("UTILITARIO").tipo_veiculo, "FIORINO")

    def test_valor_nao_reconhecido_cai_em_fiorino(self):
        self.assertEqual(self._motorista("CARROCA").tipo_veiculo, "FIORINO")

    def test_tipos_grandes_preservados(self):
        self.assertEqual(self._motorista("VAN_HR").tipo_veiculo, "VAN_HR")
        self.assertEqual(self._motorista("VUC").tipo_veiculo, "VUC")
        self.assertEqual(self._motorista("3/4").tipo_veiculo, "TRES_QUARTOS")

    def test_tarifa_nao_muda_com_o_default(self):
        # regras/tarifa_motorista.py ja tratava "" e "FIORINO" como a
        # mesma tarifa (R$340/65km). Preencher a planilha nao pode mexer
        # em pagamento -- este teste trava isso.
        from regras import tarifa_motorista
        self.assertEqual(
            tarifa_motorista.calcular_valor_rota(None, 50).valor_total,
            tarifa_motorista.calcular_valor_rota("FIORINO", 50).valor_total,
        )
```

Conferir no topo do arquivo que `_construir_motorista` esta importado; se nao estiver, acrescentar ao import existente de `regras.preferencias_motoristas`.

- [ ] **Step 2: Rodar o teste para ver falhar**

Run: `py -3.11 -m unittest regras.test_preferencias_motoristas -v`
Expected: FAIL em `test_celula_vazia_vira_fiorino` — `None != 'FIORINO'`

- [ ] **Step 3: Implementar**

Em `regras/preferencias_motoristas.py`, substituir o bloco `:227-238`:

```python
    tipo_veiculo_bruto = registro.get("TIPO_VEICULO")
    codigo_tipo_veiculo = re.sub(r"[^A-Z0-9]", "_", _normalizar_texto(tipo_veiculo_bruto)).strip("_") or None
    tipo_veiculo = tipo_por_codigo(codigo_tipo_veiculo)
    if codigo_tipo_veiculo and tipo_veiculo is None:
        logger.warning(
            f"TIPO_VEICULO '{tipo_veiculo_bruto}' não reconhecido pro motorista {agent_id} -- "
            f"tratado como {TIPO_VEICULO_PADRAO} (veículo padrão da última milha)."
        )
    # Célula vazia = FIORINO (Hugo, 22/09): é o carro da maior parte da
    # frota, e a planilha historicamente só registrava tipo pra veículo
    # grande. Não afrouxa elegibilidade -- FIORINO é o MENOR tipo, então
    # continua fora de qualquer rota de veículo grande, igual a quando
    # isso era None. A tarifa também não muda: regras/tarifa_motorista.py
    # já tratava vazio e "FIORINO" como a mesma tarifa.
    if tipo_veiculo is None:
        tipo_veiculo = tipo_por_codigo(TIPO_VEICULO_PADRAO)
```

E no retorno (`:252`), simplificar, já que agora nunca é None:

```python
        tipo_veiculo=tipo_veiculo.codigo,
```

Declarar a constante perto do topo do módulo, junto das outras constantes de parsing:

```python
# Veículo assumido pra quem está sem TIPO_VEICULO na planilha (Hugo,
# 22/09) -- ver comentário em _construir_motorista.
TIPO_VEICULO_PADRAO = "FIORINO"
```

Atualizar a docstring do módulo (`:40`) onde ela diz que motorista sem tipo nunca é elegível pra veículo grande — continua verdade, mas agora pelo motivo novo (FIORINO é o menor), não por ausência de dado.

- [ ] **Step 4: Rodar o teste para ver passar**

Run: `py -3.11 -m unittest regras.test_preferencias_motoristas -v`
Expected: PASS, incluindo os 5 testes novos

- [ ] **Step 5: Conferir que tarifa e app do motorista nao quebraram**

Run: `py -3.11 -m unittest discover -s nucleo -p "test_*.py" 2>&1 | tail -5`
Expected: OK. Se `test_nucleo_fase_a.py` falhar numa asserção de tarifa, **parar** — significa que o default vazou para o cálculo de pagamento.

- [ ] **Step 6: Commit**

```bash
git status --short
git add regras/preferencias_motoristas.py regras/test_preferencias_motoristas.py
git commit -m "Motoristas: TIPO_VEICULO vazio passa a ser lido como FIORINO

27 dos 30 motoristas estavam com a celula em branco e Fiorino e o carro
da maioria. Nao afrouxa elegibilidade (FIORINO e o menor tipo, segue
fora de rota de veiculo grande) nem muda tarifa (tarifa_motorista ja
tratava vazio e FIORINO como a mesma). Pedido do Hugo, 22/09.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Preencher TIPO_VEICULO na planilha de motoristas

**Files:**
- Modify: `dados/BD_MOTORISTAS.xlsx` (planilha versionada — excecao no `.gitignore:24`)
- Create: `C:\Users\HUGOMA~1\AppData\Local\Temp\claude\c--agente-stokki-eventos\<sessao>\scratchpad\preencher_fiorino.py` (script descartavel, **fora** do repo)

**Interfaces:**
- Consumes: nada de codigo
- Produces: planilha com `TIPO_VEICULO = FIORINO` nas linhas que estavam vazias, exceto os dois cadastros virtuais

**Nada de tela a mudar:** `painel_agentes/motoristas.py:65` ja monta as opcoes a partir de `TIPOS_VEICULO`, entao FIORINO aparece sozinho na tela `/motoristas`. `regras/cadastro_motoristas.py:119` valida via `tipo_por_codigo`, que ja aceita FIORINO desde a Task 1. Confirmar os dois, nao editar.

- [ ] **Step 1: Fotografar o estado atual da planilha**

Run:
```
py -3.11 -c "import pandas as pd; df=pd.read_excel('dados/BD_MOTORISTAS.xlsx', dtype=str); print(len(df)); print(df['TIPO_VEICULO'].fillna('(vazio)').value_counts()); print(df[['NOME_MOTORISTA','TIPO_VEICULO']].fillna('').to_string())"
```
Expected: 30 linhas, 27 vazios, 2 VAN_HR, 1 VUC. **Se os numeros diferirem, parar** — a planilha mudou desde o desenho e o Hugo precisa saber antes.

- [ ] **Step 2: Escrever o script de preenchimento no scratchpad**

Usar **openpyxl** e nao `pandas.to_excel`: reescrever com pandas perde formatacao, largura de coluna e qualquer formula da planilha.

```python
# -*- coding: utf-8 -*-
"""Preenche TIPO_VEICULO = FIORINO nas celulas vazias da
dados/BD_MOTORISTAS.xlsx, preservando o resto do arquivo.
Descartavel -- nao vai pro repo. Hugo, 22/09."""
from openpyxl import load_workbook

CAMINHO = r"C:\agente_stokki_eventos\dados\BD_MOTORISTAS.xlsx"
# Cadastros virtuais: nao sao carros reais, ficam sem tipo na planilha.
# (O codigo os le como FIORINO de qualquer forma -- ver Task 2 --, isto
# e so pra planilha nao afirmar que um cadastro virtual tem carro.)
PULAR = {"LALAMOVE (VIRTUAL)", "HUGO MACOL SOUSA (TESTE)"}


def _chave(nome):
    import unicodedata
    texto = unicodedata.normalize("NFKD", str(nome or ""))
    return "".join(c for c in texto if not unicodedata.combining(c)).strip().upper()


wb = load_workbook(CAMINHO)
ws = wb.active
cabecalho = {str(c.value).strip().upper(): c.column for c in ws[1] if c.value}
col_tipo = cabecalho["TIPO_VEICULO"]
col_nome = cabecalho["NOME_MOTORISTA"]

alterados, pulados, ja_tinham = [], [], []
for linha in range(2, ws.max_row + 1):
    nome = ws.cell(row=linha, column=col_nome).value
    if nome is None:
        continue
    celula = ws.cell(row=linha, column=col_tipo)
    atual = str(celula.value).strip() if celula.value is not None else ""
    if atual:
        ja_tinham.append((nome, atual))
    elif _chave(nome) in PULAR:
        pulados.append(nome)
    else:
        celula.value = "FIORINO"
        alterados.append(nome)

wb.save(CAMINHO)
print(f"preenchidos FIORINO: {len(alterados)}")
for n in alterados:
    print("  +", n)
print(f"pulados (virtuais): {pulados}")
print(f"ja tinham tipo: {ja_tinham}")
```

- [ ] **Step 3: Rodar o script**

Run: `py -3.11 <caminho do scratchpad>\preencher_fiorino.py`
Expected: `preenchidos FIORINO: 25`, `pulados (virtuais): ['LALAMOVE (virtual)', 'Hugo Maçol Sousa (TESTE)']`, `ja tinham tipo: [(..., 'VAN_HR'), (..., 'VAN_HR'), (..., 'VUC')]`

Se o numero de preenchidos nao for 25, conferir os nomes impressos antes de seguir.

- [ ] **Step 4: Conferir que so a coluna TIPO_VEICULO mudou**

Run:
```
py -3.11 -c "import pandas as pd; df=pd.read_excel('dados/BD_MOTORISTAS.xlsx', dtype=str); print(df['TIPO_VEICULO'].fillna('(vazio)').value_counts()); print(len(df), 'linhas', len(df.columns), 'colunas')"
git diff --stat dados/BD_MOTORISTAS.xlsx
```
Expected: `FIORINO 25`, `VAN_HR 2`, `VUC 1`, `(vazio) 2`; 30 linhas, mesmo numero de colunas de antes.

- [ ] **Step 5: Confirmar que a tela /motoristas oferece FIORINO**

Run: `py -3.11 -c "from painel_agentes.motoristas import dados_pagina_motoristas" ` e conferir a lista de tipos:
```
py -3.11 -c "from regras.tipo_veiculo import TIPOS_VEICULO; print([(t.codigo, t.nome) for t in TIPOS_VEICULO])"
```
Expected: FIORINO na primeira posicao. É essa lista que `painel_agentes/motoristas.py:65` entrega ao template.

- [ ] **Step 6: Commit**

```bash
git status --short
git add dados/BD_MOTORISTAS.xlsx
git commit -m "Motoristas: TIPO_VEICULO preenchido com FIORINO nos 25 sem carro

So a coluna TIPO_VEICULO das celulas vazias; LALAMOVE (virtual) e o
cadastro de teste ficaram em branco de proposito. Hugo, 22/09.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Veiculo grande so por endereco acima de 100 caixas

**Files:**
- Modify: `roteirizacao/roteirizacao_dados.py:1520-1589` (`_extrair_grupos_veiculo_grande`)
- Test: `roteirizacao/test_veiculo_grande_um_endereco.py` (criar)

**Interfaces:**
- Consumes: `classificar_tipo_veiculo` da Task 1
- Produces: `separar_pedidos_exclusivos(...)` mantem a assinatura `-> tuple[list[list[dict]], list[dict]]`. Muda so o criterio interno de extracao.

- [ ] **Step 1: Escrever o teste que falha**

Criar `roteirizacao/test_veiculo_grande_um_endereco.py`:

```python
# -*- coding: utf-8 -*-
"""
test_veiculo_grande_um_endereco.py

Regra nova de 22/09/2026 (Hugo): veiculo grande e acionado SO por
endereco -- quando os pedidos de um MESMO endereco somam mais que o teto
da rota comum (100 caixas). O crescimento guloso por enderecos vizinhos
(ate 4 enderecos, 2 no Truck) que existia antes saiu.

Caso que motivou a mudanca: 2 pedidos de 60 caixas pro mesmo endereco
saiam em rotas DIFERENTES -- 120 estourava o teto de 100 da rota comum e
nao alcancava o minimo de 150 da VAN/HR.

Nenhum teste geocodifica: obter_coordenadas e substituida por leitura
direta de latitude/longitude dos dicts.

Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_veiculo_grande_um_endereco -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import roteirizacao_dados as rd  # noqa: E402


def _pedido(codigo, endereco, caixas, lat=-23.5, lng=-46.6, nivel=1):
    return {
        "code": codigo,
        "address": endereco,
        "dimension_3": caixas,
        "latitude": lat,
        "longitude": lng,
        "difficulty_level": nivel,
    }


def _coords(servico, api_key=None):
    lat, lng = servico.get("latitude"), servico.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


class BaseSemGeocodificar(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(rd, "obter_coordenadas", _coords)
        patcher.start()
        self.addCleanup(patcher.stop)

    def separar(self, pedidos):
        return rd.separar_pedidos_exclusivos(
            pedidos, volume_maximo=100, distancia_maxima_km=15, api_key=None,
        )


class TestUmEnderecoAcimaDoTeto(BaseSemGeocodificar):
    def test_dois_pedidos_mesmo_endereco_viram_uma_rota(self):
        # O caso que falhava ate 22/09.
        pedidos = [
            _pedido("A", "Rua X, 100", 60),
            _pedido("B", "Rua X, 100", 60),
        ]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1, "os dois deviam sair na mesma rota exclusiva")
        self.assertEqual({s["code"] for s in prontos[0]}, {"A", "B"})
        self.assertEqual(demais, [])

    def test_o_tipo_classificado_e_van_hr(self):
        pedidos = [_pedido("A", "Rua X, 100", 60), _pedido("B", "Rua X, 100", 60)]
        prontos, _ = self.separar(pedidos)
        tipo = rd.classificar_tipo_veiculo(*rd.caixas_e_enderecos(prontos[0]))
        self.assertEqual(tipo.codigo, "VAN_HR")

    def test_endereco_dentro_do_teto_volta_pro_pool(self):
        pedidos = [_pedido("A", "Rua X, 100", 40), _pedido("B", "Rua X, 100", 50)]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(prontos, [])
        self.assertEqual({s["code"] for s in demais}, {"A", "B"})

    def test_exatamente_no_teto_nao_aciona(self):
        pedidos = [_pedido("A", "Rua X, 100", 100)]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(prontos, [])
        self.assertEqual(len(demais), 1)


class TestSemCrescimentoPorVizinhos(BaseSemGeocodificar):
    def test_enderecos_diferentes_nao_se_juntam(self):
        # Ate 22/09 estes dois viravam 1 cluster de veiculo grande.
        pedidos = [
            _pedido("A", "Rua X, 100", 80, lat=-23.50, lng=-46.60),
            _pedido("B", "Rua Y, 200", 80, lat=-23.501, lng=-46.601),
        ]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(prontos, [], "enderecos diferentes nao formam mais veiculo grande")
        self.assertEqual({s["code"] for s in demais}, {"A", "B"})

    def test_um_endereco_grande_nao_arrasta_o_vizinho(self):
        pedidos = [
            _pedido("A", "Rua X, 100", 120, lat=-23.50, lng=-46.60),
            _pedido("B", "Rua Y, 200", 30, lat=-23.501, lng=-46.601),
        ]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1)
        self.assertEqual({s["code"] for s in prontos[0]}, {"A"})
        self.assertEqual({s["code"] for s in demais}, {"B"})


class TestPedidoGigante(BaseSemGeocodificar):
    def test_pedido_individual_acima_do_teto_sai_isolado(self):
        pedidos = [_pedido("A", "Rua X, 100", 150)]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1)
        self.assertEqual(prontos[0][0]["code"], "A")
        self.assertEqual(demais, [])


class TestAcimaDaMaiorCapacidade(BaseSemGeocodificar):
    def test_endereco_acima_do_truck_sai_exclusivo_com_alerta(self):
        # 40 pedidos de 80cx no mesmo endereco = 3200cx, acima do Truck.
        # Nao pode virar 32 rotas pequenas silenciosas.
        pedidos = [_pedido(f"P{i}", "Rua X, 100", 80) for i in range(40)]
        with self.assertLogs(rd.logger, level="WARNING") as captura:
            prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1)
        self.assertEqual(len(prontos[0]), 40)
        self.assertEqual(demais, [])
        self.assertTrue(any("ALERTA_ALOCACAO" in linha for linha in captura.output))


class TestNivel4Intocado(BaseSemGeocodificar):
    def test_nivel4_continua_agrupando_por_endereco_e_embarcador(self):
        pedidos = [
            _pedido("A", "Rua X, 100", 60, nivel=4) | {"sender_id": 1},
            _pedido("B", "Rua X, 100", 60, nivel=4) | {"sender_id": 1},
        ]
        prontos, demais = self.separar(pedidos)
        self.assertEqual(len(prontos), 1)
        self.assertEqual({s["code"] for s in prontos[0]}, {"A", "B"})
        self.assertEqual(demais, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar o teste para ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_veiculo_grande_um_endereco -v`
Expected: FAIL em `test_enderecos_diferentes_nao_se_juntam` (o crescimento ainda existe) e em `test_endereco_acima_do_truck_sai_exclusivo_com_alerta` (nao ha alerta e o grupo volta pro pool).

- [ ] **Step 3: Implementar**

Substituir `_extrair_grupos_veiculo_grande` inteira (`roteirizacao_dados.py:1520-1589`) por:

```python
    def _extrair_grupos_veiculo_grande(candidatos: list[dict]) -> tuple[list[list[dict]], list[dict]]:
        """
        Extrai como rota exclusiva de veículo grande todo ENDEREÇO cujos
        pedidos somam mais que `volume_maximo` (o teto da rota comum, 100
        caixas) -- pedido do Hugo, 22/09.

        Até 22/09 isto era um crescimento guloso que juntava até 4
        endereços vizinhos (2 no Truck) buscando alcançar o volume mínimo
        do tipo. Saiu: juntar endereços diferentes num veículo grande não
        é o que a operação quer, e o mínimo de 150 da VAN/HR deixava
        descoberto o caso real -- 2 pedidos de 60 caixas pro MESMO
        endereço (120) estouravam a rota comum e não alcançavam a VAN/HR,
        saindo em 2 rotas.

        Pedido individual acima do teto nunca chega aqui: já foi isolado
        como "gigante" antes (ver separar_pedidos_exclusivos).

        Endereço acima da maior capacidade do catálogo (2500 cx, Truck)
        é extraído do mesmo jeito, com alerta -- 1 rota sinalizada é
        melhor que dezenas de rotas pequenas silenciosas pro mesmo
        portão. Dividir em várias rotas de Truck é outro projeto.

        Endereço dentro do teto volta pro pool comum (`sobras`).
        """
        extraidos: list[list[dict]] = []
        sobras: list[dict] = []

        for grupo in _agrupar_por_endereco(candidatos):
            if grupo["caixas"] <= volume_maximo:
                sobras.extend(grupo["pedidos"])
                continue
            if classificar_tipo_veiculo(grupo["caixas"], 1) is None:
                endereco = grupo["pedidos"][0].get("address")
                logger.warning(
                    f"[ALERTA_ALOCACAO] Endereço '{endereco}' soma {grupo['caixas']} caixas, "
                    f"acima da maior capacidade do catálogo ({VOLUME_MAXIMO_GERAL_CX}) -- "
                    f"sai como 1 rota exclusiva sem tipo de veículo definido."
                )
            extraidos.append(grupo["pedidos"])

        return extraidos, sobras
```

Conferir os imports no topo do modulo: `VOLUME_MAXIMO_GERAL_CX` precisa estar entre os nomes importados de `regras.tipo_veiculo` (`:37`). Se nao estiver, acrescentar.

`_distancia_para_cluster` (`:1513-1518`) fica sem uso depois disso — remover a funcao, ja que o orfao foi criado por esta mudanca. Conferir antes com `grep -n "_distancia_para_cluster" roteirizacao/roteirizacao_dados.py` que nao ha outro chamador. **Nao** remover `_agrupar_por_endereco` nem `_cabe_na_distancia`, que continuam em uso.

- [ ] **Step 4: Rodar o teste para ver passar**

Run: `py -3.11 -m unittest roteirizacao.test_veiculo_grande_um_endereco -v`
Expected: PASS, 9 testes

- [ ] **Step 5: Rodar a suite de roteirizacao e anotar o que quebrou**

Run: `py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -30`
Expected: falhas em `test_particao_carga.py` e/ou `test_nivel4_veiculo_grande.py`. Anotar cada uma. **Nao corrigir aqui** — é a Task 5.

- [ ] **Step 6: Commit**

```bash
git status --short
git add roteirizacao/roteirizacao_dados.py roteirizacao/test_veiculo_grande_um_endereco.py
git commit -m "Roteirizacao: veiculo grande so por endereco acima de 100 caixas

Sai o crescimento guloso por enderecos vizinhos; entra a regra do Hugo
(22/09): o endereco cujos pedidos somam mais que o teto da rota comum
vira 1 rota exclusiva. Resolve o caso de 2 pedidos de 60cx no mesmo
endereco saindo em rotas diferentes. Endereco acima de 2500cx sai
exclusivo com ALERTA_ALOCACAO em vez de virar dezenas de rotas.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Atualizar os testes existentes que a regra nova invalidou

**Files:**
- Modify: `roteirizacao/test_particao_carga.py` (so os casos que falharam)
- Modify: `roteirizacao/test_nivel4_veiculo_grande.py` (so os casos que falharam)

**Interfaces:**
- Consumes: comportamento das Tasks 1 e 4
- Produces: suite de `roteirizacao` verde

**Regra desta task:** cada teste alterado precisa de um comentario dizendo **qual decisao** o invalidou (faixas contiguas da Task 1, ou fim do crescimento por vizinhos da Task 4). Um teste que quebrou por outro motivo é **bug**, nao regra nova — nesse caso, parar e investigar em vez de ajustar a asserção.

- [ ] **Step 1: Listar exatamente o que falha**

Run: `py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | grep -E "^(FAIL|ERROR):"`
Expected: uma lista curta de nomes de teste.

- [ ] **Step 2: Para cada falha, decidir entre ajustar ou investigar**

Para cada teste da lista, ler o teste e responder por escrito:
- A asserção depende de um volume entre 101 e 149 caixas, ou de um minimo de tipo (150/300/500/1500)? → invalidado pela **Task 1** (faixas contiguas). Ajustar.
- A asserção depende de pedidos de enderecos **diferentes** virando 1 rota de veiculo grande? → invalidado pela **Task 4** (fim do crescimento). Ajustar.
- Nenhum dos dois? → **parar e investigar**. Provavelmente FIORINO vazou para `classificar_tipo_veiculo`.

- [ ] **Step 3: Ajustar as asserções, com comentario**

Padrao do comentario a acrescentar acima de cada asserção mudada:

```python
        # Mudou em 22/09: faixas contiguas (VAN/HR passou a valer de 101 cx,
        # era 150) -- ver docs/superpowers/specs/2026-09-22-alocacao-motoristas-justa-design.md
```

ou

```python
        # Mudou em 22/09: veiculo grande so por 1 endereco, o crescimento
        # por enderecos vizinhos saiu -- ver a spec de 22/09.
```

Nao apagar testes. Se um teste perdeu completamente o sentido (testava exclusivamente o crescimento por vizinhos), reescrever a asserção para travar o comportamento **novo** e renomear o metodo de acordo.

- [ ] **Step 4: Rodar a suite inteira**

Run:
```
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -5
py -3.11 -m unittest discover -s regras -p "test_*.py" 2>&1 | tail -5
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -5
py -3.11 -m unittest discover -s nucleo -p "test_*.py" 2>&1 | tail -5
```
Expected: OK nas quatro.

- [ ] **Step 5: Commit**

```bash
git status --short
git add roteirizacao/test_particao_carga.py roteirizacao/test_nivel4_veiculo_grande.py
git commit -m "Roteirizacao: testes acompanham a regra nova de veiculo grande

Faixas contiguas e fim do crescimento por enderecos vizinhos invalidaram
asserces destes testes. Cada mudanca tem o comentario da decisao que a
motivou. Nenhum teste removido.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: PORTAO — replay de 30 dias e numeros para o Hugo

**Files:**
- Read-only: `roteirizacao/replay_rotas.py`
- Create: `roteirizacao/dados/replay_resultado.txt` (saida do proprio script, ja gitignored por estar em `dados/`)

**Interfaces:**
- Consumes: Tasks 1, 4 e 5 aplicadas no working tree
- Produces: relatorio comparativo para decisao do Hugo. **Nenhuma task seguinte sobe para producao sem esse aval.**

Esta task nao escreve codigo de producao. É medicao.

- [ ] **Step 1: Copiar o banco de producao**

A copia tem que sair da VPS de forma consistente com WAL, e rodar como `www-data`:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python -c \"import sqlite3; s=sqlite3.connect('dados/dados.db'); d=sqlite3.connect('/tmp/dados_replay.db'); s.backup(d)\""
scp -i ~/.ssh/atendimento_vps root@187.127.52.197:/tmp/dados_replay.db dados/dados_replay.db
```

- [ ] **Step 2: Rodar o replay com o codigo NOVO**

Run (da raiz):
```
py -3.11 roteirizacao/replay_rotas.py --de 2026-08-22 --ate 2026-09-21
```
Expected: tabela no terminal + `roteirizacao/dados/replay_resultado.txt`. Guardar esse arquivo como `replay_depois.txt`.

- [ ] **Step 3: Rodar o replay com o codigo ANTIGO**

**Nunca usar `git stash` aqui.** O stash é compartilhado entre o worktree e o checkout principal, e a sessao do WMS tem arquivos modificados — um `stash pop` pode trazer o trabalho dela. Usar checkout de arquivos, que é local e reversivel:

```bash
# volta os dois arquivos ao estado de master (sem tocar em commit nenhum)
git checkout master -- regras/tipo_veiculo.py roteirizacao/roteirizacao_dados.py
py -3.11 roteirizacao/replay_rotas.py --de 2026-08-22 --ate 2026-09-21
# guardar a saida como replay_antes.txt

# devolve a versao do branch
git checkout HEAD -- regras/tipo_veiculo.py roteirizacao/roteirizacao_dados.py
```

Conferir com `git status --short` que a arvore voltou limpa antes de seguir. Se `git diff` nao estiver vazio nesses dois arquivos, **parar** — a comparacao teria rodado com codigo misturado.

- [ ] **Step 4: Montar a comparacao**

Comparar, antes x depois:

| Metrica | Por que importa |
|---|---|
| numero total de rotas | o risco principal: a regra nova pode criar mais rotas |
| rotas exclusivas de veiculo grande, por tipo | o ganho esperado: VAN/HR novas na faixa 101-149 cx |
| km total | custo |
| rotas acima de 9h | **tem que continuar 0** |
| rotas sem motorista alocado | **nao pode aumentar** |

- [ ] **Step 5: Entregar ao Hugo e PARAR**

Apresentar a tabela comparativa e perguntar explicitamente se pode seguir. **As Tasks 7 a 10 nao comecam sem o "sim".** Se o numero de rotas subir de forma que o Hugo nao aceite, a decisao de ajustar o teto (ou reverter a Task 4) é dele, e vira replanejamento — nao ajuste silencioso.

---

### Task 7: Persistir `horas_estimadas` por rota

**Files:**
- Modify: `painel_agentes/rascunhos_rota.py` (schema `:64-86`, migracao junto das existentes `~:113`, insert `:301-307`)
- Modify: `nucleo/banco.py:117-146` (schema `nucleo_rotas`)
- Modify: `nucleo/sincronizar_vuupt.py:132` e `:187-198` (copia do rascunho pra nucleo_rotas)
- Modify: `roteirizacao/criar_rotas_diarias.py:586-604` e `:825-843` (calcular e gravar)
- Test: `painel_agentes/test_horas_estimadas.py` (criar)

**Interfaces:**
- Consumes: `estimar_tempo_rota(sublote, api_key, coords_base, coords_fn) -> float` (`roteirizacao_dados.py:826`)
- Produces: coluna `horas_estimadas REAL` em `rascunhos_rota` e `nucleo_rotas`; chave `"horas_estimadas"` nos dicts de rascunho montados em `criar_rotas_diarias`

- [ ] **Step 1: Escrever o teste que falha**

Criar `painel_agentes/test_horas_estimadas.py`:

```python
# -*- coding: utf-8 -*-
"""
test_horas_estimadas.py

A coluna horas_estimadas (22/09, Hugo) alimenta o rodizio de rotas
longas na alocacao: so da pra saber quem pegou rota pesada na semana se
a duracao de cada rota ficar gravada. A duracao REAL (iniciada_em/
concluida_em) nao serve -- o motorista confirma paradas em lote na Vuupt
e o timestamp sai distorcido.

Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_horas_estimadas -v
"""
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from painel_agentes import rascunhos_rota


class TestColunaHorasEstimadas(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        caminho = Path(self.tmp.name) / "dados.db"
        patcher = mock.patch.object(rascunhos_rota, "DB_PATH", caminho)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.caminho = caminho

    def _colunas(self):
        conn = rascunhos_rota._conectar()
        try:
            return {r["name"] for r in conn.execute("PRAGMA table_info(rascunhos_rota)")}
        finally:
            conn.close()

    def test_coluna_existe(self):
        self.assertIn("horas_estimadas", self._colunas())

    def test_migracao_em_banco_antigo(self):
        # Banco criado antes de 22/09 nao tem a coluna; _conectar precisa
        # adiciona-la (CREATE TABLE IF NOT EXISTS nao altera tabela).
        conn = sqlite3.connect(self.caminho)
        conn.execute("""
            CREATE TABLE rascunhos_rota (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_alvo TEXT NOT NULL, lote_id TEXT NOT NULL, nome TEXT NOT NULL,
                start_location_base_id INTEGER NOT NULL, start_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'RASCUNHO',
                criado_em TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                atualizado_em TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.commit()
        conn.close()
        self.assertIn("horas_estimadas", self._colunas())

    def test_valor_gravado_volta_na_leitura(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 22), [{
            "nome": "Rota 1", "start_location_base_id": 1,
            "start_at": "2026-09-22T09:00:00Z",
            "km_estimado": 42.0, "horas_estimadas": 7.5, "sublote": [],
        }])
        rotas = rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 22))
        self.assertEqual(rotas[0]["horas_estimadas"], 7.5)

    def test_sem_valor_fica_nulo(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 22), [{
            "nome": "Rota 2", "start_location_base_id": 1,
            "start_at": "2026-09-22T09:00:00Z", "sublote": [],
        }])
        rotas = rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 22))
        self.assertIsNone(rotas[0]["horas_estimadas"])


if __name__ == "__main__":
    unittest.main()
```

A funcao de gravacao é `criar_lote_rascunhos(data_alvo, rascunhos, lote_id=None) -> str` (`rascunhos_rota.py:271`). Se ela exigir campos obrigatorios alem dos usados acima, acrescenta-los aos dicts do teste — o objetivo aqui é so provar que `horas_estimadas` vai e volta.

- [ ] **Step 2: Rodar o teste para ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_horas_estimadas -v`
Expected: FAIL em `test_coluna_existe` — `'horas_estimadas' not found`

- [ ] **Step 3: Implementar a coluna e a gravacao**

Em `painel_agentes/rascunhos_rota.py`:

1. Acrescentar `horas_estimadas REAL,` logo depois de `km_estimado REAL,` no `CREATE TABLE rascunhos_rota` (`:79`).
2. Junto do bloco de migracoes existente, acrescentar (usando o mesmo padrao de `PRAGMA table_info` ja usado ali para `rascunhos_parada`, mas agora para `rascunhos_rota`):

```python
    # Migração 22/09: duração estimada da rota, usada pelo rodízio de
    # rotas longas na alocação (ver alocacao_motoristas.py). A duração
    # REAL não serve -- confirmação em lote na Vuupt distorce o
    # timestamp (nucleo/tempos.py).
    colunas_rota = {row["name"] for row in conn.execute("PRAGMA table_info(rascunhos_rota)")}
    if "horas_estimadas" not in colunas_rota:
        conn.execute("ALTER TABLE rascunhos_rota ADD COLUMN horas_estimadas REAL")
```

3. No INSERT (`:301-307`), acrescentar a coluna e o valor `r.get("horas_estimadas")`, na mesma posicao relativa em que `km_estimado` aparece nas duas listas.
4. Conferir com `grep -n "km_estimado" painel_agentes/rascunhos_rota.py` todos os SELECT que listam colunas explicitamente e acrescentar `horas_estimadas` neles (em especial `:1058`).

Em `nucleo/banco.py`, acrescentar `horas_estimadas REAL,` depois de `km_estimado REAL,` (`:132`). Conferir se o modulo tem bloco de migracao para `nucleo_rotas`; se tiver, acrescentar o `ALTER TABLE` no mesmo padrao. Se nao tiver, criar um, seguindo o estilo de `rascunhos_rota.py`.

Em `nucleo/sincronizar_vuupt.py`, acrescentar `horas_estimadas` ao SELECT do rascunho (`:132`) e ao INSERT em `nucleo_rotas` (`:187-198`), ao lado de `km_estimado`.

Em `roteirizacao/criar_rotas_diarias.py`, nos **dois** pontos que montam o dict de rascunho (`:586-604` e `:825-843`), calcular junto do `km_estimado`:

```python
                    horas_estimadas = estimar_tempo_rota(sublote, gmaps_key, coords_base)
```

e acrescentar `"horas_estimadas": horas_estimadas,` ao dict, logo depois de `"km_estimado"`.

Conferir que `estimar_tempo_rota` esta importado no topo de `criar_rotas_diarias.py`; se nao estiver, acrescentar ao import existente de `roteirizacao_dados`.

- [ ] **Step 4: Rodar o teste para ver passar**

Run: `py -3.11 -m unittest painel_agentes.test_horas_estimadas -v`
Expected: PASS, 4 testes

- [ ] **Step 5: Provar que a gravacao acontece de verdade**

Rodar o pipeline em modo teste e conferir que a coluna sai preenchida:

Run: `py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste --gerar-rascunho 2>&1 | tail -20`

Conferir os parametros reais aceitos com `py -3.11 roteirizacao/criar_rotas_diarias.py --help` antes. Depois:

```
py -3.11 -c "import sqlite3; c=sqlite3.connect('dados/dados.db'); print(c.execute('SELECT nome, km_estimado, horas_estimadas FROM rascunhos_rota ORDER BY id DESC LIMIT 5').fetchall())"
```
Expected: `horas_estimadas` preenchido (nao `None`) nas rotas novas.

- [ ] **Step 6: Commit**

```bash
git status --short
git add painel_agentes/rascunhos_rota.py painel_agentes/test_horas_estimadas.py nucleo/banco.py nucleo/sincronizar_vuupt.py roteirizacao/criar_rotas_diarias.py
git commit -m "Roteirizacao: gravar horas_estimadas de cada rota

Coluna nova em rascunhos_rota e nucleo_rotas, preenchida na criacao da
rota com estimar_tempo_rota. Alimenta o rodizio de rotas longas na
alocacao. A duracao real nao serve: confirmacao em lote na Vuupt
distorce iniciada_em/concluida_em.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Contar rotas longas recentes por motorista

**Files:**
- Modify: `regras/prioridade_ofertas.py` (acrescentar funcao depois de `contar_rotas_recentes`, `:187`)
- Test: `regras/test_prioridade_ofertas.py` (acrescentar classe)

**Interfaces:**
- Consumes: coluna `horas_estimadas` da Task 7
- Produces:
  ```python
  def contar_rotas_longas_recentes(
      data_alvo: date, dias: int, limiar_horas: float,
      conn: sqlite3.Connection | None = None,
  ) -> dict[int, int]
  ```
  `{agent_id: quantas rotas acima de limiar_horas na janela [data_alvo - dias, data_alvo]}`. Rota sem `horas_estimadas` nao conta.

- [ ] **Step 1: Escrever o teste que falha**

Acrescentar a `regras/test_prioridade_ofertas.py`. Seguir o padrao de setup que os testes existentes ja usam para criar as tabelas em memoria (ler a classe existente que testa `contar_rotas_recentes` e reaproveitar o mesmo helper):

```python
class TestContarRotasLongasRecentes(unittest.TestCase):
    """Rodizio de rotas longas (Hugo, 22/09): quem pegou rota pesada na
    semana nao pega a proxima. Longa = acima do limiar (7h por padrao)."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE nucleo_rotas (
                id INTEGER PRIMARY KEY AUTOINCREMENT, data_rota TEXT, agent_id INTEGER,
                status TEXT, rascunho_id INTEGER, vuupt_route_id INTEGER,
                horas_estimadas REAL
            )
        """)
        self.conn.execute("""
            CREATE TABLE rascunhos_rota (
                id INTEGER PRIMARY KEY AUTOINCREMENT, data_alvo TEXT, agent_id INTEGER,
                status TEXT, vuupt_route_id INTEGER, horas_estimadas REAL
            )
        """)
        self.addCleanup(self.conn.close)

    def _rota(self, agent_id, dias_atras, horas, status="CONCLUIDA"):
        data = (date(2026, 9, 22) - timedelta(days=dias_atras)).isoformat()
        self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, agent_id, status, horas_estimadas) VALUES (?,?,?,?)",
            (data, agent_id, status, horas),
        )

    def _contar(self, dias=7, limiar=7.0):
        return prioridade_ofertas.contar_rotas_longas_recentes(
            date(2026, 9, 22), dias, limiar, self.conn,
        )

    def test_conta_so_acima_do_limiar(self):
        self._rota(1, 1, 8.5)
        self._rota(1, 2, 6.0)
        self._rota(2, 1, 7.0)   # exatamente no limiar nao conta
        contagem = self._contar()
        self.assertEqual(contagem.get(1), 1)
        self.assertIsNone(contagem.get(2))

    def test_ignora_cancelada(self):
        self._rota(1, 1, 9.0, status="CANCELADA")
        self.assertEqual(self._contar(), {})

    def test_ignora_rota_sem_horas_gravadas(self):
        self._rota(1, 1, None)
        self.assertEqual(self._contar(), {})

    def test_respeita_a_janela(self):
        self._rota(1, 3, 8.0)
        self._rota(1, 20, 8.0)
        self.assertEqual(self._contar(dias=7).get(1), 1)
        self.assertEqual(self._contar(dias=30).get(1), 2)

    def test_nao_conta_duas_vezes_a_mesma_rota(self):
        # Mesma rota nas duas tabelas (rascunho que virou rota na Vuupt).
        self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, agent_id, status, rascunho_id, horas_estimadas) "
            "VALUES ('2026-09-21', 1, 'CONCLUIDA', 55, 8.0)"
        )
        self.conn.execute(
            "INSERT INTO rascunhos_rota (id, data_alvo, agent_id, status, horas_estimadas) "
            "VALUES (55, '2026-09-21', 1, 'ENVIADO', 8.0)"
        )
        self.assertEqual(self._contar().get(1), 1)

    def test_conta_rascunho_que_ainda_nao_virou_rota(self):
        self.conn.execute(
            "INSERT INTO rascunhos_rota (id, data_alvo, agent_id, status, horas_estimadas) "
            "VALUES (77, '2026-09-21', 3, 'RASCUNHO', 8.0)"
        )
        self.assertEqual(self._contar().get(3), 1)
```

Conferir que `sqlite3`, `date`, `timedelta` e `prioridade_ofertas` estao importados no topo do arquivo de teste.

- [ ] **Step 2: Rodar o teste para ver falhar**

Run: `py -3.11 -m unittest regras.test_prioridade_ofertas -v`
Expected: FAIL — `module 'regras.prioridade_ofertas' has no attribute 'contar_rotas_longas_recentes'`

- [ ] **Step 3: Implementar**

Acrescentar em `regras/prioridade_ofertas.py`, logo depois de `contar_rotas_recentes` (`:187`):

```python
def contar_rotas_longas_recentes(data_alvo: date, dias: int, limiar_horas: float,
                                 conn: sqlite3.Connection | None = None) -> dict[int, int]:
    """{agent_id: quantidade de rotas LONGAS} na janela [data_alvo - dias,
    data_alvo]. Longa = `horas_estimadas` acima de `limiar_horas` (pedido
    do Hugo, 22/09: quem pegou rota pesada na semana não pega a próxima).

    Mesma união e mesmo dedup de contar_rotas_recentes (nucleo_rotas +
    rascunhos_rota, cancelada de fora), com um filtro a mais: rota sem
    `horas_estimadas` gravada NÃO conta -- a coluna só existe desde
    22/09, então o critério entra em vigor conforme o histórico acumula.
    """
    inicio = (data_alvo - timedelta(days=dias)).isoformat()
    fim = data_alvo.isoformat()
    fechar = conn is None
    if conn is None:
        if not _DB_PATH.exists():
            return {}
        conn = sqlite3.connect(_DB_PATH)
    try:
        contagem: dict[int, int] = {}
        vistos_rascunho: set[int] = set()
        vistos_vuupt: set[int] = set()

        if _tem_tabela(conn, "nucleo_rotas"):
            for agent_id, rascunho_id, vuupt_route_id in conn.execute(
                "SELECT agent_id, rascunho_id, vuupt_route_id FROM nucleo_rotas "
                "WHERE agent_id IS NOT NULL AND status != 'CANCELADA' "
                "AND horas_estimadas IS NOT NULL AND horas_estimadas > ? "
                "AND data_rota BETWEEN ? AND ?",
                (limiar_horas, inicio, fim),
            ):
                contagem[agent_id] = contagem.get(agent_id, 0) + 1
                if rascunho_id is not None:
                    vistos_rascunho.add(rascunho_id)
                if vuupt_route_id is not None:
                    vistos_vuupt.add(vuupt_route_id)

        if _tem_tabela(conn, "rascunhos_rota"):
            for rid, agent_id, vuupt_route_id in conn.execute(
                "SELECT id, agent_id, vuupt_route_id FROM rascunhos_rota "
                "WHERE agent_id IS NOT NULL AND status IN ('RASCUNHO', 'OFERTADA', 'ENVIADO') "
                "AND horas_estimadas IS NOT NULL AND horas_estimadas > ? "
                "AND data_alvo BETWEEN ? AND ?",
                (limiar_horas, inicio, fim),
            ):
                if rid in vistos_rascunho or (vuupt_route_id is not None and vuupt_route_id in vistos_vuupt):
                    continue
                contagem[agent_id] = contagem.get(agent_id, 0) + 1
        return contagem
    finally:
        if fechar:
            conn.close()
```

Acrescentar `"rota_longa_horas": 7.0` a `PADRAO_CONFIG` (`:75-81`)? **Nao** — `carregar_config` converte tudo com `int()`. O limiar é float e pertence a `roteirizacao`, nao a `marketplace_rotas`. Ele é lido direto do config pelos chamadores, na Task 10.

- [ ] **Step 4: Rodar o teste para ver passar**

Run: `py -3.11 -m unittest regras.test_prioridade_ofertas -v`
Expected: PASS, 16 testes (10 antigos + 6 novos)

- [ ] **Step 5: Commit**

```bash
git status --short
git add regras/prioridade_ofertas.py regras/test_prioridade_ofertas.py
git commit -m "Regras: contar rotas longas recentes por motorista

Irma de contar_rotas_recentes, com filtro de horas_estimadas acima do
limiar. Alimenta o rodizio de rotas longas na alocacao (Hugo, 22/09).
Rota sem horas gravadas nao conta -- o criterio entra em vigor conforme
o historico acumula.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Ordem nova em `selecionar_motorista_equitativo`

**Files:**
- Modify: `roteirizacao/alocacao_motoristas.py:47-122`
- Test: `roteirizacao/test_alocacao_justa.py` (criar)

**Interfaces:**
- Consumes: nada das Tasks 7/8 diretamente — recebe as contagens **prontas** por parametro
- Produces:
  ```python
  def selecionar_motorista_equitativo(
      sublote, data_rota, motoristas, contagem_alocacoes_dia,
      api_key=None, ajustes_disponibilidade=None,
      rotas_7d: dict[int, int] | None = None,
      rotas_30d: dict[int, int] | None = None,
      longas_7d: dict[int, int] | None = None,
      rota_longa: bool = False,
  ) -> MotoristaPreferencias | None
  ```
  `rota_longa` é **booleano ja decidido pelo chamador** — a funcao nao estima tempo. Isso mantem `alocacao_motoristas.py` sem dependencia de `estimar_tempo_rota` e deixa o teste simples.

- [ ] **Step 1: Escrever o teste que falha**

Criar `roteirizacao/test_alocacao_justa.py`:

```python
# -*- coding: utf-8 -*-
"""
test_alocacao_justa.py

Ordem nova da alocacao (Hugo, 22/09). Antes era (carga do dia, agent_id)
-- e como 28 dos 30 motoristas tem MAX_ROTAS_DIA=1, a carga do dia era
quase sempre 0 pra todos e a escolha virava "menor agent_id". Quem tinha
id baixo rodava quase todo dia.

Agora: carga do dia, rotas longas em 7d (so quando a rota e longa),
rotas em 7d, rotas em 30d, agent_id.

A mudanca e de ORDENACAO, nunca de filtro -- nenhuma rota pode ficar sem
motorista por causa dela.

Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_alocacao_justa -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

from regras.preferencias_motoristas import MotoristaPreferencias  # noqa: E402
import alocacao_motoristas  # noqa: E402

# Terca-feira: dia util (rodizio ativo), dentro do padrao semanal abaixo.
DATA = date(2026, 9, 22)
SUBLOTE = [{"address": "Rua X, 100", "dimension_3": 10}]


def _motorista(agent_id, zonas=("ZONA NORTE",), placa=None, tipo="FIORINO"):
    return MotoristaPreferencias(
        agent_id=agent_id, vehicle_id=agent_id, nome=f"Motorista {agent_id}",
        aceita_viagens=True, dias_disponiveis={0, 1, 2, 3, 4, 5, 6},
        max_rotas_dia=1, ativo=True, zonas_preferidas=set(zonas),
        telefone=None, email=None, placa=placa, tipo_veiculo=tipo, cpf=None,
    )


class BaseAlocacao(unittest.TestCase):
    """Neutraliza os classificadores que dependem de geocodificacao: o
    foco aqui e a ORDEM, nao a elegibilidade."""

    def setUp(self):
        for nome, valor in (
            ("classificar_rota_viagem", lambda *a, **k: False),
            ("classificar_rota_zona", lambda *a, **k: None),
            ("sublote_em_area_rodizio", lambda *a, **k: False),
        ):
            patcher = unittest.mock.patch.object(alocacao_motoristas, nome, valor)
            patcher.start()
            self.addCleanup(patcher.stop)

    def escolher(self, motoristas, **kwargs):
        return alocacao_motoristas.selecionar_motorista_equitativo(
            SUBLOTE, DATA, motoristas, kwargs.pop("carga", {}), None, **kwargs,
        )


class TestJusticaPorHistorico(BaseAlocacao):
    def test_menos_rotas_em_7d_ganha_do_menor_agent_id(self):
        motoristas = [_motorista(1), _motorista(99)]
        escolhido = self.escolher(motoristas, rotas_7d={1: 4, 99: 0})
        self.assertEqual(escolhido.agent_id, 99)

    def test_empate_em_7d_decide_por_30d(self):
        motoristas = [_motorista(1), _motorista(99)]
        escolhido = self.escolher(motoristas, rotas_7d={1: 2, 99: 2},
                                  rotas_30d={1: 10, 99: 3})
        self.assertEqual(escolhido.agent_id, 99)

    def test_empate_total_decide_por_agent_id(self):
        motoristas = [_motorista(99), _motorista(1)]
        escolhido = self.escolher(motoristas, rotas_7d={1: 2, 99: 2},
                                  rotas_30d={1: 5, 99: 5})
        self.assertEqual(escolhido.agent_id, 1)

    def test_carga_do_dia_vem_antes_do_historico(self):
        motoristas = [_motorista(1), _motorista(2)]
        m1 = _motorista(1)
        m1 = MotoristaPreferencias(**{**m1.__dict__, "max_rotas_dia": 2})
        escolhido = self.escolher([m1, _motorista(2)],
                                  carga={1: 1}, rotas_7d={1: 0, 2: 9})
        self.assertEqual(escolhido.agent_id, 2, "quem esta zerado no dia vem primeiro")


class TestRodizioDeRotasLongas(BaseAlocacao):
    def test_rota_longa_prefere_quem_pegou_menos_longas(self):
        motoristas = [_motorista(1), _motorista(2)]
        escolhido = self.escolher(
            motoristas, rota_longa=True,
            longas_7d={1: 2, 2: 0},
            rotas_7d={1: 0, 2: 5},   # 2 rodou MAIS no total e ainda assim ganha
        )
        self.assertEqual(escolhido.agent_id, 2)

    def test_rota_curta_ignora_o_rodizio_de_longas(self):
        motoristas = [_motorista(1), _motorista(2)]
        escolhido = self.escolher(
            motoristas, rota_longa=False,
            longas_7d={1: 2, 2: 0},
            rotas_7d={1: 0, 2: 5},
        )
        self.assertEqual(escolhido.agent_id, 1, "numa rota curta vale o historico geral")


class TestNadaVirouFiltro(BaseAlocacao):
    def test_zona_continua_travando(self):
        # Zona reconhecida e nenhum motorista atende -> ninguem elegivel.
        with unittest.mock.patch.object(
            alocacao_motoristas, "classificar_rota_zona", lambda *a, **k: "ZONA SUL"
        ):
            escolhido = self.escolher([_motorista(1, zonas=("ZONA NORTE",))],
                                      rotas_7d={1: 0})
        self.assertIsNone(escolhido)

    def test_historico_nao_exclui_ninguem(self):
        # Um unico motorista, com historico pessimo: ainda assim e escolhido.
        escolhido = self.escolher([_motorista(1)], rota_longa=True,
                                  longas_7d={1: 99}, rotas_7d={1: 99},
                                  rotas_30d={1: 99})
        self.assertEqual(escolhido.agent_id, 1)


class TestCompatibilidade(BaseAlocacao):
    def test_sem_parametros_novos_escolhe_igual_a_antes(self):
        # Comportamento historico: menor carga do dia, desempate por agent_id.
        escolhido = self.escolher([_motorista(99), _motorista(1)])
        self.assertEqual(escolhido.agent_id, 1)


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
```

Mover `import unittest.mock` para o topo do arquivo (junto dos outros imports) em vez de deixar no `__main__` — o `BaseAlocacao.setUp` usa `unittest.mock` e precisa dele carregado sempre.

- [ ] **Step 2: Rodar o teste para ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_alocacao_justa -v`
Expected: FAIL — `selecionar_motorista_equitativo() got an unexpected keyword argument 'rotas_7d'`

- [ ] **Step 3: Implementar**

Em `roteirizacao/alocacao_motoristas.py`, trocar a assinatura (`:47-54`) e a ordenacao (`:117-122`):

```python
def selecionar_motorista_equitativo(
    sublote: list[dict],
    data_rota: date,
    motoristas: list[MotoristaPreferencias],
    contagem_alocacoes_dia: dict[int, int],
    api_key: str | None = None,
    ajustes_disponibilidade: dict[int, dict] | None = None,
    rotas_7d: dict[int, int] | None = None,
    rotas_30d: dict[int, int] | None = None,
    longas_7d: dict[int, int] | None = None,
    rota_longa: bool = False,
) -> "MotoristaPreferencias | None":
```

Acrescentar ao fim da docstring, antes do paragrafo de retorno:

```
    `rotas_7d`/`rotas_30d` (pedido do Hugo, 22/09): quantas rotas
    cada motorista fez nos últimos 7 e 30 dias (ver regras/
    prioridade_ofertas.py::contar_rotas_recentes). Sem elas, a escolha é
    a de antes de 22/09 -- menor carga do dia, desempate por agent_id.
    Como 28 dos 30 motoristas têm MAX_ROTAS_DIA=1, a carga do dia é
    quase sempre 0 pra todos, e o que decidia de fato era o agent_id:
    quem tinha id baixo rodava quase todo dia.

    `longas_7d` + `rota_longa` (Hugo, 22/09): rodízio das rotas
    pesadas -- quem pegou rota longa na semana não pega a próxima.
    `longas_7d` é quantas rotas acima do limiar cada um fez em 7 dias
    (contar_rotas_longas_recentes); `rota_longa` diz se a rota SENDO
    alocada é longa, e só nesse caso o critério pesa. Quem chama decide
    isso (estimar_tempo_rota > roteirizacao.rota_longa_horas) -- este
    módulo não estima tempo.

    Tudo isto é ORDENAÇÃO, nunca filtro: nenhum motorista deixa de ser
    elegível por histórico, e nenhuma rota fica sem motorista por causa
    dessa ordem.
```

E a ordenacao:

```python
    # Ordem (Hugo, 22/09): carga do próprio dia primeiro -- não dar a 2ª
    # rota a alguém enquanto outro está zerado --, depois o rodízio de
    # rotas longas (só quando a rota é longa), depois quem rodou menos na
    # semana e no mês. agent_id deixou de ser o critério efetivo e virou
    # o último desempate, estável.
    rotas_7d = rotas_7d or {}
    rotas_30d = rotas_30d or {}
    longas_7d = longas_7d or {}
    elegiveis.sort(key=lambda m: (
        contagem_alocacoes_dia.get(m.agent_id, 0),
        longas_7d.get(m.agent_id, 0) if rota_longa else 0,
        rotas_7d.get(m.agent_id, 0),
        rotas_30d.get(m.agent_id, 0),
        m.agent_id,
    ))
    return elegiveis[0]
```

- [ ] **Step 4: Rodar o teste para ver passar**

Run: `py -3.11 -m unittest roteirizacao.test_alocacao_justa -v`
Expected: PASS, 9 testes

- [ ] **Step 5: Rodar a suite inteira**

Run: `py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -5`
Expected: OK. Os chamadores ainda nao passam os parametros novos, entao nada mais deve mudar de comportamento.

- [ ] **Step 6: Commit**

```bash
git status --short
git add roteirizacao/alocacao_motoristas.py roteirizacao/test_alocacao_justa.py
git commit -m "Roteirizacao: alocacao passa a rodar os motoristas de forma justa

A chave de ordenacao era (carga do dia, agent_id) -- com MAX_ROTAS_DIA=1
em quase todo mundo, isso era so "menor agent_id", e quem tinha id baixo
rodava quase todo dia. Agora entram rotas longas em 7d (so em rota
longa), rotas em 7d e rotas em 30d. Continua sendo ordenacao, nunca
filtro. Sem os parametros novos, o comportamento e o de antes.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Chamadores carregam o historico e decidem o que e rota longa

**Files:**
- Modify: `roteirizacao/criar_rotas_diarias.py` (`_rotear_particao` `:778-818`, fluxo de rascunhos `:572-585`)
- Modify: `painel_agentes/planejamento_rotas.py:1238-1308`
- Modify: `config.yaml` (chave `roteirizacao.rota_longa_horas`)
- Test: coberto pelos testes das Tasks 8 e 9 + prova manual no Step 5

**Interfaces:**
- Consumes: `contar_rotas_recentes`, `contar_rotas_longas_recentes` (Task 8); `selecionar_motorista_equitativo` com os parametros novos (Task 9); `estimar_tempo_rota` (`roteirizacao_dados.py:826`)
- Produces: nada de novo — é a ligacao final

- [ ] **Step 1: Acrescentar o limiar ao config**

Em `config.yaml`, na secao `roteirizacao` (se nao existir a secao, criar junto das outras de topo):

```yaml
roteirizacao:
  # Acima de quantas horas estimadas uma rota conta como "longa" pro
  # rodizio da alocacao (Hugo, 22/09): quem pegou rota longa na semana
  # nao pega a proxima. Ver roteirizacao/alocacao_motoristas.py.
  rota_longa_horas: 7.0
```

**Nao commitar o `config.yaml`** — é gitignored e é o arquivo de segredos. A VPS tem o dela; anotar que o Hugo precisa acrescentar a mesma chave la antes do deploy.

- [ ] **Step 2: Carregar as contagens uma vez em `criar_rotas_diarias.py`**

Dentro de `_rotear_particao` (`:778`), **antes** do `sorted(...)` de `:790`, carregar uma unica vez:

```python
            # Histórico de justiça (Hugo, 22/09): 3 queries por execução,
            # nunca por rota -- contar_rotas_recentes varre nucleo_rotas
            # e rascunhos_rota inteiras.
            rota_longa_horas, janela_curta, janela_longa = _parametros_justica(config)
            rotas_7d = contar_rotas_recentes(data_alvo, janela_curta)
            rotas_30d = contar_rotas_recentes(data_alvo, janela_longa)
            longas_7d = contar_rotas_longas_recentes(data_alvo, janela_curta, rota_longa_horas)
```

Se `config` nao estiver no escopo de `_rotear_particao`, pega-lo da funcao externa que ja o carregou (`:522` ou `:618`) via `nonlocal`/parametro — **nao** chamar `_carregar_config()` de novo dentro do laco.

**Nao** criar constantes de topo: `criar_rotas_diarias.py` nao tem config global — ele chama `_carregar_config()` dentro das funcoes (`:201`, usado em `:522` e `:618`). Ler do `config` que a funcao ja tem em maos, com um helper no proprio modulo:

```python
def _parametros_justica(config: dict) -> tuple[float, int, int]:
    """(limiar de rota longa em horas, janela curta em dias, janela longa
    em dias) -- Hugo, 22/09. As janelas sao as MESMAS do marketplace de
    ofertas de proposito: os dois caminhos que decidem quem roda precisam
    contar do mesmo jeito."""
    rot = config.get("roteirizacao") or {}
    mkt = config.get("marketplace_rotas") or {}
    return (
        float(rot.get("rota_longa_horas", 7.0)),
        int(mkt.get("janela_curta_dias", 7)),
        int(mkt.get("janela_longa_dias", 30)),
    )
```

e usar `rota_longa_horas, janela_curta, janela_longa = _parametros_justica(config)` onde o `config` ja existe no escopo.

- [ ] **Step 3: Desempatar a ordem de processamento por duracao decrescente**

Trocar o `sorted(...)` de `:790-796` por:

```python
            # Escassez primeiro (20/08, ver contar_motoristas_elegiveis) e,
            # entre sublotes igualmente escassos, o mais LONGO primeiro
            # (22/09): senão a rota pesada é processada por último e sobra
            # só quem já pegou muitas longas na semana.
            sublotes_do_dia = sorted(
                sublotes_do_dia,
                key=lambda sub: (
                    contar_motoristas_elegiveis(
                        sub, data_alvo, catalogo_motoristas.motoristas, contagem_alocacoes_dia, gmaps_key,
                        ajustes_disponibilidade=ajustes_disponibilidade,
                    ),
                    -estimar_tempo_rota(sub, gmaps_key, coords_base),
                ),
            )
```

- [ ] **Step 4: Passar tudo nas duas chamadas de `criar_rotas_diarias.py`**

No laco principal (`:815-818`):

```python
                horas_sublote = estimar_tempo_rota(sublote, gmaps_key, coords_base)
                motorista = selecionar_motorista_equitativo(
                    sublote, data_alvo, catalogo_motoristas.motoristas, contagem_alocacoes_dia, gmaps_key,
                    ajustes_disponibilidade=ajustes_disponibilidade,
                    rotas_7d=rotas_7d,
                    rotas_30d=rotas_30d,
                    longas_7d=longas_7d,
                    rota_longa=horas_sublote > rota_longa_horas,
                )
```

Reaproveitar `horas_sublote` no dict de rascunho (`"horas_estimadas": horas_sublote`) em vez de estimar duas vezes — a Task 7 acrescentou essa chave.

Fazer o mesmo no fluxo de rascunhos (`:580-583` e `:586-604`), carregando as tres contagens antes do laco `for plano in planos:` (`:572`).

- [ ] **Step 5: Ligar o botao "Alocar motoristas" do painel**

Em `painel_agentes/planejamento_rotas.py`, dentro de `alocar_motoristas_rascunhos` (`:1238`), depois de montar `contagem_alocacoes_dia` (`:1270`):

```python
    rota_longa_horas = float((config.get("roteirizacao") or {}).get("rota_longa_horas", 7.0))
    janela_curta = int((config.get("marketplace_rotas") or {}).get("janela_curta_dias", 7))
    janela_longa = int((config.get("marketplace_rotas") or {}).get("janela_longa_dias", 30))
    rotas_7d = contar_rotas_recentes(data_alvo, janela_curta)
    rotas_30d = contar_rotas_recentes(data_alvo, janela_longa)
    longas_7d = contar_rotas_longas_recentes(data_alvo, janela_curta, rota_longa_horas)
```

E na chamada (`:1293`), passar as coordenadas ja gravadas em vez de deixar geocodificar de novo — as paradas do rascunho tem `latitude`/`longitude` (`rascunhos_parada`), e `estimar_tempo_rota` aceita `coords_fn` justamente pra isso:

```python
        sublote = [{"address": p["endereco"], "dimension_3": p["volume_caixas"],
                    "latitude": p["latitude"], "longitude": p["longitude"]} for p in r["paradas"]]

        def _coords_da_parada(servico):
            lat, lng = servico.get("latitude"), servico.get("longitude")
            return (lat, lng) if lat is not None and lng is not None else None

        horas_rota = estimar_tempo_rota(sublote, gmaps_key, coords_fn=_coords_da_parada)
        motorista = selecionar_motorista_equitativo(
            sublote, data_alvo, catalogo.motoristas, contagem_alocacoes_dia, gmaps_key,
            ajustes_disponibilidade=ajustes_disponibilidade,
            rotas_7d=rotas_7d,
            rotas_30d=rotas_30d,
            longas_7d=longas_7d,
            rota_longa=horas_rota > rota_longa_horas,
        )
```

Acrescentar os imports necessarios no topo de cada arquivo (`contar_rotas_recentes`, `contar_rotas_longas_recentes` de `regras.prioridade_ofertas`; `estimar_tempo_rota` de `roteirizacao_dados`), seguindo o estilo de import ja usado no arquivo.

- [ ] **Step 6: Provar que funciona de verdade**

Suite inteira:
```
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -5
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -5
py -3.11 -m unittest discover -s regras -p "test_*.py" 2>&1 | tail -5
```
Expected: OK nas tres.

Pipeline em modo teste (nao escreve na Stokki/Vuupt):
```
py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste 2>&1 | tail -40
```
Expected: roda sem erro e os motoristas escolhidos **nao** sao os de menor `agent_id` em bloco. Conferir no log quais motoristas apareceram e comparar com quem rodou pouco nos ultimos 7 dias:
```
py -3.11 -c "
from datetime import date
from regras.prioridade_ofertas import contar_rotas_recentes
print(sorted(contar_rotas_recentes(date.today(), 7).items(), key=lambda p: p[1]))
"
```

Painel, em porta alternativa (**nunca** a 8070, que pode estar rodando de verdade):
```
py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"
```
Abrir `/planejamento`, clicar "Alocar motoristas" num dia com rascunhos sem motorista e conferir que aloca sem erro.

- [ ] **Step 7: Commit**

```bash
git status --short
git add roteirizacao/criar_rotas_diarias.py painel_agentes/planejamento_rotas.py
git commit -m "Roteirizacao: ligar o historico de justica na alocacao

criar_rotas_diarias e o botao Alocar motoristas passam a carregar as
contagens de 7d/30d e de rotas longas uma vez por execucao e repassar a
selecionar_motorista_equitativo. Entre sublotes igualmente escassos, o
mais longo e alocado primeiro. O painel usa as coordenadas ja gravadas
nas paradas em vez de geocodificar de novo.

config.yaml (gitignored) precisa de roteirizacao.rota_longa_horas: 7.0
tambem na VPS antes do deploy.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Depois do plano

Nada deste plano vai para a VPS automaticamente. Antes de qualquer deploy:

1. O Hugo precisa ter aprovado os numeros do replay (Task 6).
2. `roteirizacao.rota_longa_horas: 7.0` precisa estar no `config.yaml` **da VPS** (arquivo proprio, diferente do local).
3. Deploy segue o procedimento do CLAUDE.md: commit + push, `git pull --no-rebase` na VPS, `chown -R www-data:www-data`, restart so do `painel-agentes` (unico servico que importa os modulos alterados), e prova real chamando a rota alterada — nao so `systemctl is-active`.
4. Na primeira noite apos o deploy, conferir no log do job noturno quantas rotas de veiculo grande sairam e se apareceu `[ALERTA_ALOCACAO]` de endereco acima da capacidade.
