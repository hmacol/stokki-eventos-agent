# Rotas fracas: plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rota com até 7 pedidos e até 40 caixas deixa de sair calada: primeiro tenta entrar nas vizinhas com folga, depois segura os pedidos por um dia, e o que sobrar sai com etiqueta no Planejamento e resumo no WhatsApp.

**Architecture:** Um módulo novo de regras (`roteirizacao/rotas_fracas.py`) roda dentro de `planejar_sublotes`, depois do polimento, e só move pedidos entre rotas (nunca tira pedido, então a conferência de cobertura continua valendo). O segurar fica fora de `planejar_sublotes`, no `main()` do job das 18h, porque precisa de banco e de dados do pedido. Uma tabela nova (`pedidos_segurados`) registra o adiamento e é lida pelo planejamento, pelo vigia e pelo incrementar.

**Tech Stack:** Python 3.11, SQLite, unittest, Flask + Jinja (tela de Planejamento).

**Spec:** `docs/superpowers/specs/2026-09-29-rotas-fracas-design.md`

## Global Constraints

- Python sempre `py -3.11`. Testes com `py -3.11 -m unittest <modulo> -v`, a partir da raiz. Não há pytest.
- Rodar os pacotes `roteirizacao` e `painel_agentes` em comandos separados (misturar dá AttributeError).
- Código, comentários e mensagens em português. Comentários sem acento nos arquivos que já seguem esse padrão (`polimento_rotas.py`, `notificar_whatsapp.py`, `pedidos_dedicados.py`); com acento nos que já usam (`criar_rotas_diarias.py`, `planejamento_rotas.py`).
- Corte de rota fraca: até **7** pedidos **e** até **40** caixas.
- Folga da junção: distância **20 km**, km acumulado **75 km**, paradas **teto + 2**. Não cedem: **100** caixas, **9h**, janela de horário, mesma macro-região.
- Prazo: **3 dias úteis** a partir da entrada do pedido, contando a data da entrega. No máximo **1** adiamento por pedido.
- `SEGURAR_ATIVO = False` no código entregue. Ligar é decisão do Hugo, depois de uma semana de observação.
- Mensagem de WhatsApp inteira em até `MAX_MENSAGEM` (200) caracteres.
- Trabalhar num worktree (`.claude/worktrees/rotas-fracas`, ramo `rotas-fracas`). O working tree principal tem mudanças não commitadas de outras sessões em `planejamento_rotas.html` e `MAPA_DO_SISTEMA.txt`: não tocar nelas.
- Commits locais no ramo, um por tarefa, com `git add` só dos arquivos da tarefa. Sem push, sem merge e sem deploy até o Hugo pedir.
- Mensagem de commit no formato `Área: o que mudou e por quê`, terminando com `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **Job rodado duas vezes na mesma noite.** Os pedidos segurados na primeira rodada não podem voltar à roteirização na segunda. Teste na Tarefa 3 (`test_separar_tira_so_os_ativos`).
2. **`created_at` em UTC perto da meia-noite.** Pedido criado às 22h30 de segunda em Brasília chega como `01:30` de terça. A entrada tem que contar segunda. Teste na Tarefa 1 (`test_entrada_converte_utc_para_brasilia`).
3. **Código combinado (`#PS-1, PS-2`).** Os dois códigos são registrados, e basta um já ter sido segurado para bloquear. Testes nas Tarefas 1 e 3.
4. **Tabela `pedidos_segurados` ilegível ou banco travado.** Não segura nada e a rota sai com etiqueta; o job não cai. Teste na Tarefa 4 (`test_falha_ao_ler_segurados_nao_segura`).
5. **Pedido sem coordenada dentro da rota fraca.** A junção não pode levantar exceção nem perder pedido. Teste na Tarefa 2 (`test_pedido_sem_coordenada_nao_quebra`).

## Mapa de arquivos

| Arquivo | Ação | Responsabilidade |
|---|---|---|
| `roteirizacao/rotas_fracas.py` | criar | Corte, conta do prazo, motivo de não segurar, junção com folga |
| `roteirizacao/test_rotas_fracas.py` | criar | Testes das regras e da junção |
| `roteirizacao/pedidos_segurados.py` | criar | Tabela `pedidos_segurados` e leituras |
| `roteirizacao/test_pedidos_segurados.py` | criar | Testes da tabela |
| `roteirizacao/criar_rotas_diarias.py` | modificar | Chamar a junção, aplicar o segurar, passar o motivo ao rascunho |
| `roteirizacao/test_rotas_fracas_integracao.py` | criar | Testes da integração |
| `painel_agentes/rascunhos_rota.py` | modificar | Coluna `rota_fraca_motivo` |
| `painel_agentes/planejamento_rotas.py` | modificar | Etiqueta da rota e marca no pool |
| `painel_agentes/templates/planejamento_rotas.html` | modificar | Chip "Segurado" no pool |
| `painel_agentes/test_rota_fraca_painel.py` | criar | Testes da coluna e da etiqueta |
| `vigia/vigiar.py`, `vigia/regras.py` | modificar | Motivo e prazo do pedido segurado |
| `vigia/test_vigiar.py`, `vigia/test_regras.py` | modificar | Testes do vigia |
| `roteirizacao/incrementar_rotas.py` | modificar | Pular segurados |
| `notificar_whatsapp.py`, `test_notificar_whatsapp.py` | modificar | Mensagem de rotas fracas |
| `roteirizacao/replay_rotas.py`, `metricas_plano.py`, `test_metricas_plano.py` | modificar | Medir rotas fracas no replay |
| `MAPA_DO_SISTEMA.txt` | modificar | Registrar módulo, tabela e regra |

---

### Task 0: Preparar o worktree

**Files:** nenhum arquivo do projeto.

- [ ] **Step 1: Criar o worktree a partir do master local (que já tem a spec)**

```bash
cd /c/agente_stokki_eventos
git fetch origin
git worktree add .claude/worktrees/rotas-fracas -b rotas-fracas master
cd .claude/worktrees/rotas-fracas
git merge origin/master
```

Esperado: merge sem conflito (o master local só tem a spec a mais).

- [ ] **Step 2: Rodar a base de testes antes de mexer**

```bash
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -3
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -3
py -3.11 -m unittest vigia.test_regras vigia.test_vigiar test_notificar_whatsapp 2>&1 | tail -3
```

Esperado: `OK` nos três. Anotar a contagem de testes de cada um para comparar no fim. Se algo já falhar aqui, parar e avisar o Hugo antes de seguir.

---

### Task 1: Regras puras (corte, prazo, motivo de não segurar)

**Files:**
- Create: `roteirizacao/rotas_fracas.py`
- Test: `roteirizacao/test_rotas_fracas.py`

**Interfaces:**
- Consumes: `roteirizacao_dados.extrair_volume_caixas(servico) -> int`, `roteirizacao_dados.macro_regiao_do_servico(servico, api_key) -> str`, `roteirizacao_dados.MACRO_GRANDE_SP`, `regioes_dia_fixo.regra_dia_fixo_do_servico(servico) -> dict | None`, `pedidos_dedicados.codigos_do_servico(servico) -> list[str]` (códigos base, formato `PS-1234`).
- Produces:
  - constantes `ROTAS_FRACAS_ATIVO`, `SEGURAR_ATIVO`, `PARADAS_ROTA_FRACA`, `CAIXAS_ROTA_FRACA`, `FOLGA_DISTANCIA_KM`, `FOLGA_KM_ACUMULADO_KM`, `FOLGA_PARADAS_EXTRA`, `PRAZO_ENTREGA_DIAS_UTEIS`
  - `eh_rota_fraca(sublote: list[dict]) -> bool`
  - `resumo_da_rota(sublote: list[dict]) -> str` (ex.: `"3 pedidos, 17 caixas"`)
  - `data_entrada(servico: dict) -> date | None`
  - `proximo_dia_util(d: date) -> date`
  - `prazo_final(entrada: date) -> date`
  - `motivo_nao_segurar(servico: dict, data_alvo: date, ja_segurados: set[str], api_key: str | None = None) -> str | None`

- [ ] **Step 1: Escrever os testes**

Criar `roteirizacao/test_rotas_fracas.py`:

```python
# -*- coding: utf-8 -*-
"""
Rotas fracas (Hugo, 29/09): corte, prazo de 3 dias uteis, motivo de nao
segurar e juncao com folga. Spec: docs/superpowers/specs/
2026-09-29-rotas-fracas-design.md
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_rotas_fracas -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import roteirizacao_dados as rd
import rotas_fracas as rf

BASE = (0.0, 0.0)
TERCA = date(2026, 9, 29)


def _servico(i, lat=0.10, lng=0.0, caixas=1, nivel=1, **extra):
    s = {"id": i, "code": f"#PS-{1000 + i}", "address": f"P{i}", "_nivel_dificuldade": nivel,
         "latitude": lat, "longitude": lng, "dimension_3": caixas, "sender_id": 1,
         "created_at": "2026-09-28 15:00:00"}  # segunda, 12h de Brasilia
    s.update(extra)
    return s


def _ids(sublotes):
    return sorted(s["id"] for sub in sublotes for s in sub)


class TestCorte(unittest.TestCase):
    def test_sete_pedidos_e_quarenta_caixas_e_fraca(self):
        rota = [_servico(i, caixas=5) for i in range(7)] + []
        rota[0]["dimension_3"] = 10  # 10 + 6*5 = 40
        self.assertTrue(rf.eh_rota_fraca(rota))

    def test_oito_pedidos_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([_servico(i) for i in range(8)]))

    def test_quarenta_e_uma_caixas_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([_servico(1, caixas=41)]))

    def test_um_pedido_com_muita_carga_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([_servico(1, caixas=294)]))

    def test_rota_vazia_nao_e_fraca(self):
        self.assertFalse(rf.eh_rota_fraca([]))

    def test_resumo_da_rota(self):
        self.assertEqual(rf.resumo_da_rota([_servico(1, caixas=9), _servico(2, caixas=8)]), "2 pedidos, 17 caixas")
        self.assertEqual(rf.resumo_da_rota([_servico(1, caixas=1)]), "1 pedido, 1 caixa")


class TestPrazo(unittest.TestCase):
    def test_entrada_segunda_vence_quinta(self):
        self.assertEqual(rf.prazo_final(date(2026, 9, 28)), date(2026, 10, 1))

    def test_entrada_sexta_vence_quarta(self):
        self.assertEqual(rf.prazo_final(date(2026, 10, 2)), date(2026, 10, 7))

    def test_entrada_sabado_conta_a_partir_de_segunda(self):
        self.assertEqual(rf.prazo_final(date(2026, 10, 3)), date(2026, 10, 8))

    def test_proximo_dia_util_pula_fim_de_semana(self):
        self.assertEqual(rf.proximo_dia_util(date(2026, 10, 2)), date(2026, 10, 5))
        self.assertEqual(rf.proximo_dia_util(date(2026, 9, 29)), date(2026, 9, 30))

    def test_entrada_converte_utc_para_brasilia(self):
        # 01:30 UTC de terca = 22:30 de segunda em Brasilia
        self.assertEqual(rf.data_entrada({"created_at": "2026-09-29 01:30:00"}), date(2026, 9, 28))

    def test_entrada_respeita_fuso_explicito(self):
        self.assertEqual(rf.data_entrada({"created_at": "2026-09-28T23:30:00-03:00"}), date(2026, 9, 28))

    def test_entrada_ausente_ou_invalida(self):
        self.assertIsNone(rf.data_entrada({}))
        self.assertIsNone(rf.data_entrada({"created_at": "ontem"}))


class TestMotivoNaoSegurar(unittest.TestCase):
    def setUp(self):
        for alvo, valor in ((("macro_regiao_do_servico"), lambda s, k=None: rd.MACRO_GRANDE_SP),
                            (("regra_dia_fixo_do_servico"), lambda s: None)):
            p = mock.patch.object(rf, alvo, valor)
            p.start()
            self.addCleanup(p.stop)

    def test_pedido_comum_pode_esperar(self):
        self.assertIsNone(rf.motivo_nao_segurar(_servico(1), TERCA, set()))

    def test_agendamento(self):
        s = _servico(1, scheduled_start="2026-09-29T09:00:00-03:00")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "tem agendamento")

    def test_reentrega_pelo_campo(self):
        s = _servico(1, recreated_order_origin_id=555)
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "é reentrega")

    def test_reentrega_pelo_sufixo(self):
        s = _servico(1, code="#PS-1001-R1")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "é reentrega")

    def test_ja_segurado(self):
        self.assertEqual(rf.motivo_nao_segurar(_servico(1), TERCA, {"PS-1001"}), "já foi segurado uma vez")

    def test_codigo_combinado_basta_um_ja_segurado(self):
        s = _servico(1, code="#PS-1001, PS-2002")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, {"PS-2002"}), "já foi segurado uma vez")

    def test_dia_fixo(self):
        with mock.patch.object(rf, "regra_dia_fixo_do_servico", lambda s: {"nome": "Americana", "dias": [2]}):
            self.assertEqual(rf.motivo_nao_segurar(_servico(1), TERCA, set()), "é de região de dia fixo")

    def test_viagem(self):
        with mock.patch.object(rf, "macro_regiao_do_servico", lambda s, k=None: "Campinas"):
            self.assertEqual(rf.motivo_nao_segurar(_servico(1), TERCA, set()), "é viagem")

    def test_sem_data_de_entrada(self):
        s = _servico(1)
        del s["created_at"]
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "sem data de entrada")

    def test_prazo_estouraria(self):
        # entrou quarta 23/09 -> prazo segunda 28/09; rota de terca 29/09 ja esta atrasada
        s = _servico(1, created_at="2026-09-23 15:00:00")
        self.assertEqual(rf.motivo_nao_segurar(s, TERCA, set()), "prazo vence em 28/09")

    def test_prazo_no_limite_pode(self):
        # entrou sexta 25/09 -> prazo quarta 30/09; rota de terca 29/09 adiada vai pra quarta 30/09
        s = _servico(1, created_at="2026-09-25 15:00:00")
        self.assertIsNone(rf.motivo_nao_segurar(s, TERCA, set()))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_rotas_fracas -v`
Expected: erro de import, `ModuleNotFoundError: No module named 'rotas_fracas'`.

- [ ] **Step 3: Implementar**

Criar `roteirizacao/rotas_fracas.py`:

```python
# -*- coding: utf-8 -*-
"""
rotas_fracas.py

Rota fraca (Hugo, 29/09 -- spec docs/superpowers/specs/
2026-09-29-rotas-fracas-design.md): rota com poucos pedidos E poucas
caixas ao mesmo tempo. Pouco pedido sozinho nao basta -- medido em
producao (30/08 a 29/09): 30 das 58 rotas com ate 5 pedidos levavam mais
de 40 caixas, ou seja, carga cheia com poucas paradas.

Tres saidas, nesta ordem:
  1. juntar: distribuir os pedidos nas rotas vizinhas com folga de
     distancia e de paradas (absorver_rotas_fracas);
  2. segurar: adiar os pedidos por 1 dia util, dentro do prazo de 3 dias
     uteis da entrada (motivo_nao_segurar decide; quem grava e o
     criar_rotas_diarias.main, so no job automatico);
  3. avisar: a rota sai com o motivo gravado no rascunho.

Retorno rapido: ROTAS_FRACAS_ATIVO = False volta ao comportamento de
antes. SEGURAR_ATIVO liga so o adiamento.
"""
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402  (normalizacao do codigo do pedido)

from roteirizacao_dados import extrair_volume_caixas, macro_regiao_do_servico, MACRO_GRANDE_SP  # noqa: E402
from regioes_dia_fixo import regra_dia_fixo_do_servico  # noqa: E402

logger = logging.getLogger(__name__)

ROTAS_FRACAS_ATIVO = True
SEGURAR_ATIVO = False            # liga depois da primeira semana em producao (decisao do Hugo)
PARADAS_ROTA_FRACA = 7
CAIXAS_ROTA_FRACA = 40
FOLGA_DISTANCIA_KM = 20          # distancia entre dois pedidos da rota que recebe (normal: 15)
FOLGA_KM_ACUMULADO_KM = 75       # km acumulado da rota que recebe (normal: 60)
FOLGA_PARADAS_EXTRA = 2          # paradas alem do teto da rodada (16 + 2 = 18)
PRAZO_ENTREGA_DIAS_UTEIS = 3

TZ_BRASILIA = timezone(timedelta(hours=-3))
_PADRAO_REENTREGA = re.compile(r"-R\d+", re.IGNORECASE)


def _caixas(sublote: list[dict]) -> int:
    return sum(extrair_volume_caixas(s) for s in sublote)


def eh_rota_fraca(sublote: list[dict]) -> bool:
    return bool(sublote) and len(sublote) <= PARADAS_ROTA_FRACA and _caixas(sublote) <= CAIXAS_ROTA_FRACA


def resumo_da_rota(sublote: list[dict]) -> str:
    n, cx = len(sublote), _caixas(sublote)
    return f"{n} {'pedido' if n == 1 else 'pedidos'}, {cx} {'caixa' if cx == 1 else 'caixas'}"


def data_entrada(servico: dict) -> date | None:
    """Dia (Brasilia) em que o pedido entrou. created_at da Vuupt vem em
    UTC sem fuso (confirmado em producao, 10/09 -- ver
    incrementar_rotas._data_criacao); valor com fuso e respeitado."""
    valor = servico.get("created_at")
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor).replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_BRASILIA).date()


def proximo_dia_util(d: date) -> date:
    """Dia util estritamente depois de `d` (sem calendario de feriados,
    igual ao resto do sistema)."""
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def prazo_final(entrada: date) -> date:
    """Ultimo dia em que o pedido pode ser ENTREGUE: entrada + 3 dias
    uteis. Entrada no fim de semana conta a partir da segunda."""
    atual = entrada
    while atual.weekday() >= 5:
        atual += timedelta(days=1)
    for _ in range(PRAZO_ENTREGA_DIAS_UTEIS):
        atual = proximo_dia_util(atual)
    return atual


def motivo_nao_segurar(servico: dict, data_alvo: date, ja_segurados: set[str],
                       api_key: str | None = None) -> str | None:
    """None = o pedido pode esperar 1 dia util. Senao, o motivo (texto
    curto, vai pra etiqueta da rota). Na duvida, nao segura."""
    if servico.get("scheduled_start"):
        return "tem agendamento"
    if servico.get("recreated_order_origin_id") or _PADRAO_REENTREGA.search(str(servico.get("code") or "")):
        return "é reentrega"
    if any(c in ja_segurados for c in pedidos_dedicados.codigos_do_servico(servico)):
        return "já foi segurado uma vez"
    if regra_dia_fixo_do_servico(servico):
        return "é de região de dia fixo"
    if macro_regiao_do_servico(servico, api_key) != MACRO_GRANDE_SP:
        return "é viagem"
    entrada = data_entrada(servico)
    if entrada is None:
        return "sem data de entrada"
    prazo = prazo_final(entrada)
    if proximo_dia_util(data_alvo) > prazo:
        return f"prazo vence em {prazo:%d/%m}"
    return None
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest roteirizacao.test_rotas_fracas -v`
Expected: `OK`, 24 testes.

- [ ] **Step 5: Commit**

```bash
git add roteirizacao/rotas_fracas.py roteirizacao/test_rotas_fracas.py
git commit -m "Roteirizacao: regras de rota fraca (corte, prazo de 3 dias uteis, quem pode esperar)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Junção com folga

**Files:**
- Modify: `roteirizacao/rotas_fracas.py`
- Test: `roteirizacao/test_rotas_fracas.py`

**Interfaces:**
- Consumes: de `polimento_rotas`: `_rota_polivel(sublote, api_key, base) -> bool`, `_rota_valida(sublote, api_key, tamanho_maximo, volume_maximo, distancia_maxima_km, distancia_maxima_viagem_km, eh_viagem_fn, base, km_acumulado_maximo, km_acumulado_maximo_viagem) -> bool`, `_melhor_insercao(parada, sublote, base, api_key) -> (km, sublote_novo)`, `_centroide(sublote, api_key) -> (lat, lng) | None`. De `otimizacao_rotas`: `ordenar_2opt(sublote, base_lat, base_lng, api_key) -> list[dict]`. De `roteirizacao_dados`: `obter_coordenadas`, `_distancia_km`, `macro_regiao_predominante_do_sublote`.
- Produces: `absorver_rotas_fracas(sublotes, base_lat, base_lng, api_key, *, tamanho_maximo, volume_maximo, distancia_maxima_km, distancia_maxima_viagem_km=None, km_acumulado_maximo=None, km_acumulado_maximo_viagem=None, eh_viagem_fn=None) -> tuple[list[list[dict]], dict]`. O dict é `{"juntadas": int, "motivos": {id(sublote_devolvido): str}}`. A chave de `motivos` é o `id()` da lista devolvida, mesmo padrão de `horas_por_sublote` em `criar_rotas_diarias._rotear_particao`.

- [ ] **Step 1: Acrescentar os testes**

No fim de `roteirizacao/test_rotas_fracas.py`, antes do `if __name__`:

```python
class TestAbsorver(unittest.TestCase):
    """Receptoras levam 25 caixas por parada pra NAO serem fracas (senao
    elas mesmas tentariam se juntar e o teste mediria outra coisa)."""

    def setUp(self):
        import otimizacao_rotas as ot
        import polimento_rotas as pr

        def _coords(s, k=None):
            lat, lng = s.get("latitude"), s.get("longitude")
            return (lat, lng) if lat is not None and lng is not None else None

        # obter_coordenadas e importada POR NOME em cada modulo: sem trocar
        # em todos, o pedido sem coordenada cairia na geocodificacao real
        patches = [mock.patch.object(m, "obter_coordenadas", _coords) for m in (rd, ot, pr, rf)]
        patches.append(mock.patch.object(rf, "macro_regiao_predominante_do_sublote",
                                         lambda sub, k=None: "GRANDE_SP"))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        rd.COORDS_BASE = BASE
        rd.HORA_SAIDA_BASE = 6.0

    def _absorver(self, sublotes, **kw):
        params = dict(tamanho_maximo=16, volume_maximo=100, distancia_maxima_km=15, km_acumulado_maximo=60)
        params.update(kw)
        return rf.absorver_rotas_fracas(sublotes, *BASE, None, **params)

    def test_junta_com_folga_de_distancia(self):
        # 1 esta a ~17,8 km de 3 e 4: nao cabia com 15 km, cabe com 20
        fraca = [_servico(1, 0.10, 0.00)]
        vizinha = [_servico(3, 0.10, 0.16, caixas=25), _servico(4, 0.10, 0.161, caixas=25)]
        saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 1)
        self.assertEqual(_ids(saida), [1, 3, 4])
        self.assertEqual(rel["juntadas"], 1)
        self.assertEqual(rel["motivos"], {})

    def test_nao_junta_alem_da_folga(self):
        # ~22 km: passa dos 20
        fraca = [_servico(1, 0.10, 0.00)]
        vizinha = [_servico(3, 0.10, 0.20, caixas=25), _servico(4, 0.10, 0.201, caixas=25)]
        saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(sorted(len(s) for s in saida), [1, 2])
        self.assertEqual(_ids(saida), [1, 3, 4])
        self.assertEqual(rel["juntadas"], 0)
        rota_fraca = next(s for s in saida if len(s) == 1)
        self.assertEqual(rel["motivos"], {id(rota_fraca): "vizinha mais próxima a 22 km"})

    def test_nao_cede_em_caixas(self):
        fraca = [_servico(1, 0.10, 0.00, caixas=30)]
        vizinha = [_servico(3, 0.10, 0.01, caixas=40), _servico(4, 0.10, 0.011, caixas=40)]
        saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 2)
        self.assertEqual(rel["juntadas"], 0)
        self.assertEqual(list(rel["motivos"].values()),
                         ["não coube nas vizinhas (distância, paradas, caixas, tempo ou janela)"])

    def test_folga_de_paradas_e_teto_mais_dois(self):
        fraca = [_servico(1, 0.10, 0.00)]
        tres = [_servico(10 + i, 0.10, 0.01 + 0.001 * i, caixas=25) for i in range(3)]
        saida, rel = self._absorver([list(fraca), tres], tamanho_maximo=2)  # folga: 4 paradas
        self.assertEqual(len(saida), 1)
        self.assertEqual(rel["juntadas"], 1)
        # 20 caixas cada (80 + 1 = 81): quem barra aqui e o teto de paradas, nao o de caixas
        quatro = [_servico(20 + i, 0.10, 0.01 + 0.001 * i, caixas=20) for i in range(4)]
        saida, rel = self._absorver([list(fraca), quatro], tamanho_maximo=2)
        self.assertEqual(len(saida), 2)
        self.assertEqual(rel["juntadas"], 0)

    def test_reparte_entre_duas_vizinhas(self):
        fraca = [_servico(1, 0.10, 0.00), _servico(2, 0.10, 0.30)]
        oeste = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        leste = [_servico(5, 0.10, 0.301, caixas=25), _servico(6, 0.10, 0.302, caixas=25)]
        saida, rel = self._absorver([fraca, oeste, leste])
        grupos = sorted(sorted(s["id"] for s in sub) for sub in saida)
        self.assertEqual(grupos, [[1, 3, 4], [2, 5, 6]])
        self.assertEqual(rel["juntadas"], 1)

    def test_tudo_ou_nada(self):
        # 1 caberia na vizinha, 9 nao cabe em lugar nenhum: nada muda
        fraca = [_servico(1, 0.10, 0.00), _servico(9, 0.10, 0.60)]
        vizinha = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        saida, rel = self._absorver([fraca, vizinha])
        grupos = sorted(sorted(s["id"] for s in sub) for sub in saida)
        self.assertEqual(grupos, [[1, 9], [3, 4]])
        self.assertEqual(rel["juntadas"], 0)
        self.assertEqual(len(rel["motivos"]), 1)

    def test_nivel4_nunca_participa(self):
        exclusiva = [_servico(7, 0.10, 0.00, nivel=4)]
        vizinha = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        saida, rel = self._absorver([exclusiva, vizinha])
        self.assertEqual(sorted(len(s) for s in saida), [1, 2])
        self.assertEqual(rel, {"juntadas": 0, "motivos": {}})

    def test_macro_regiao_diferente_nao_junta(self):
        fraca = [_servico(1, 0.10, 0.00)]
        vizinha = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        with mock.patch.object(rf, "macro_regiao_predominante_do_sublote",
                               lambda sub, k=None: "Campinas" if sub[0]["id"] == 1 else "GRANDE_SP"):
            saida, rel = self._absorver([fraca, vizinha])
        self.assertEqual(len(saida), 2)
        self.assertEqual(list(rel["motivos"].values()), ["sem rota vizinha na mesma região"])

    def test_duas_fracas_vizinhas_viram_uma(self):
        saida, rel = self._absorver([[_servico(1, 0.10, 0.00)], [_servico(2, 0.10, 0.001)]])
        self.assertEqual(len(saida), 1)
        self.assertEqual(_ids(saida), [1, 2])
        self.assertEqual(rel["juntadas"], 1)
        # a que sobrou continua fraca e nao tem mais vizinha
        self.assertEqual(rel["motivos"], {id(saida[0]): "sem rota vizinha na mesma região"})

    def test_pedido_sem_coordenada_nao_quebra(self):
        fraca = [_servico(1, None, None)]
        vizinha = [_servico(3, 0.10, 0.001, caixas=25), _servico(4, 0.10, 0.002, caixas=25)]
        saida, _ = self._absorver([fraca, vizinha])
        self.assertEqual(_ids(saida), [1, 3, 4])

    def test_rota_unica_do_dia(self):
        saida, rel = self._absorver([[_servico(1, 0.10, 0.00)]])
        self.assertEqual(_ids(saida), [1])
        self.assertEqual(list(rel["motivos"].values()), ["sem rota vizinha na mesma região"])
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_rotas_fracas.TestAbsorver -v`
Expected: ERROR nos 11 testes, `AttributeError` dizendo que `rotas_fracas` não tem `obter_coordenadas`.

- [ ] **Step 3: Implementar**

Em `roteirizacao/rotas_fracas.py`, trocar a linha de import de `roteirizacao_dados` por:

```python
from roteirizacao_dados import (  # noqa: E402
    extrair_volume_caixas, macro_regiao_do_servico, MACRO_GRANDE_SP,
    obter_coordenadas, _distancia_km, macro_regiao_predominante_do_sublote,
)
from otimizacao_rotas import ordenar_2opt  # noqa: E402
from polimento_rotas import _rota_polivel, _rota_valida, _melhor_insercao, _centroide  # noqa: E402
```

E acrescentar no fim do arquivo:

```python
MOTIVO_SEM_VIZINHA = "sem rota vizinha na mesma região"
MOTIVO_NAO_COUBE = "não coube nas vizinhas (distância, paradas, caixas, tempo ou janela)"


def _motivo_nao_juntou(fraca: list[dict], vizinhas: list[list[dict]], api_key: str | None) -> str:
    if not vizinhas:
        return MOTIVO_SEM_VIZINHA
    centro = _centroide(fraca, api_key)
    distancias = [_distancia_km(*centro, *c) for c in (_centroide(v, api_key) for v in vizinhas) if centro and c]
    if distancias and min(distancias) > FOLGA_DISTANCIA_KM:
        return f"vizinha mais próxima a {min(distancias):.0f} km"
    return MOTIVO_NAO_COUBE


def absorver_rotas_fracas(sublotes: list[list[dict]], base_lat: float, base_lng: float, api_key: str | None, *,
                          tamanho_maximo: int, volume_maximo: int, distancia_maxima_km: float | None,
                          distancia_maxima_viagem_km: float | None = None,
                          km_acumulado_maximo: float | None = None,
                          km_acumulado_maximo_viagem: float | None = None,
                          eh_viagem_fn=None) -> tuple[list[list[dict]], dict]:
    """Distribui cada rota fraca nas vizinhas da mesma macro-regiao, com
    folga de distancia/km acumulado/paradas SO na rota que recebe. Tudo
    ou nada por rota fraca: se um pedido nao cabe em lugar nenhum, nada
    muda. Diferente do esvaziar do polimento, nao exige queda de km -- o
    ganho aqui e a rota a menos.

    Caixas, 9h, janela e macro-regiao nao cedem. Viagem nao ganha folga
    (distancia de viagem ja e sem teto; o km acumulado de viagem fica
    como esta). Nivel 4, veiculo grande e destino inviavel ficam de fora
    (mesmo criterio de polimento_rotas._rota_polivel).

    Nunca perde nem duplica pedido. Devolve (sublotes, relatorio):
    relatorio = {"juntadas": n, "motivos": {id(sublote): texto}} com o
    motivo de cada rota que continuou fraca."""
    base = (base_lat, base_lng)
    rotas: list[list[dict]] = [list(s) for s in sublotes]
    participantes = [i for i, r in enumerate(rotas) if _rota_polivel(r, api_key, base)]
    macro = {i: macro_regiao_predominante_do_sublote(rotas[i], api_key) for i in participantes}
    distancia_folga = None if distancia_maxima_km is None else max(distancia_maxima_km, FOLGA_DISTANCIA_KM)
    acumulado_folga = None if km_acumulado_maximo is None else max(km_acumulado_maximo, FOLGA_KM_ACUMULADO_KM)
    tamanho_folga = tamanho_maximo + FOLGA_PARADAS_EXTRA

    def _valida(rota: list[dict]) -> bool:
        return _rota_valida(rota, api_key, tamanho_folga, volume_maximo, distancia_folga,
                            distancia_maxima_viagem_km, eh_viagem_fn, base,
                            acumulado_folga, km_acumulado_maximo_viagem)

    def _distancia_da_rota(ponto, rota: list[dict]) -> float:
        centro = _centroide(rota, api_key)
        return _distancia_km(*ponto, *centro) if ponto and centro else 0.0

    juntadas = 0
    motivos_por_indice: dict[int, str] = {}
    # menor primeiro: a rota mais fraca e a que mais precisa de lugar
    fracas = sorted((i for i in participantes if eh_rota_fraca(rotas[i])),
                    key=lambda i: (len(rotas[i]), _caixas(rotas[i]), i))
    for i in fracas:
        if not rotas[i] or not eh_rota_fraca(rotas[i]):
            continue  # ja foi absorvida, ou recebeu outra fraca e deixou de ser
        vizinhas = [j for j in participantes if j != i and rotas[j] and macro[j] == macro[i]]
        # as listas sao TROCADAS a cada insercao (nunca mutadas), entao
        # guardar a referencia basta pra desfazer
        backup = {j: rotas[j] for j in vizinhas}
        coube = bool(vizinhas)
        for parada in rotas[i]:
            if not coube:
                break
            ponto = obter_coordenadas(parada, api_key)
            coube = False
            for j in sorted(vizinhas, key=lambda j: (_distancia_da_rota(ponto, rotas[j]), j)):
                _, candidata = _melhor_insercao(parada, rotas[j], base, api_key)
                sequenciada = ordenar_2opt(candidata, base_lat, base_lng, api_key)
                if _valida(sequenciada):
                    rotas[j] = sequenciada
                    coube = True
                    break
        if coube:
            rotas[i] = []
            juntadas += 1
        else:
            for j, original in backup.items():
                rotas[j] = original
            motivos_por_indice[i] = _motivo_nao_juntou(rotas[i], [backup[j] for j in vizinhas], api_key)

    # motivo so vale pra quem TERMINOU fraca (uma fraca que falhou pode
    # ter recebido outra depois e deixado de ser)
    motivos = {id(rotas[i]): m for i, m in motivos_por_indice.items() if rotas[i] and eh_rota_fraca(rotas[i])}
    return [r for r in rotas if r], {"juntadas": juntadas, "motivos": motivos}
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest roteirizacao.test_rotas_fracas -v`
Expected: `OK`, 35 testes.

Se `test_duas_fracas_vizinhas_viram_uma` falhar no motivo: a rota que recebeu é processada depois, já sem vizinha, e ganha `MOTIVO_SEM_VIZINHA`. Conferir a ordem do laço antes de mexer no teste.

- [ ] **Step 5: Commit**

```bash
git add roteirizacao/rotas_fracas.py roteirizacao/test_rotas_fracas.py
git commit -m "Roteirizacao: rota fraca entra nas vizinhas com folga de distancia e de paradas

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Tabela `pedidos_segurados`

**Files:**
- Create: `roteirizacao/pedidos_segurados.py`
- Test: `roteirizacao/test_pedidos_segurados.py`

**Interfaces:**
- Consumes: `pedidos_dedicados.codigos_do_servico(servico) -> list[str]`.
- Produces:
  - `DB_PATH`
  - `conectar(db_path=DB_PATH) -> sqlite3.Connection` (cria a tabela)
  - `marcar(conn, itens: list[tuple[dict, date]], data_alvo: date, data_nova: date, motivo: str, agora: datetime | None = None) -> int` (cada item é `(servico, prazo_final)`; devolve quantos códigos gravou)
  - `codigos_segurados(conn) -> set[str]`
  - `segurados_ativos(conn, data: date) -> dict[str, dict]` (adiados para depois de `data`)
  - `separar_segurados(servicos: list[dict], data_alvo: date, db_path=DB_PATH) -> tuple[list[dict], list[dict]]` (tolerante a falha)
  - `segurados_por_servico(servicos: list[dict], data_alvo: date, db_path=DB_PATH) -> dict[int, dict]` (`{service_id: {"data_nova": "AAAA-MM-DD", "prazo_final": "AAAA-MM-DD"}}`, tolerante a falha)

- [ ] **Step 1: Escrever os testes**

Criar `roteirizacao/test_pedidos_segurados.py`:

```python
# -*- coding: utf-8 -*-
"""
Tabela pedidos_segurados (rota fraca adiada por 1 dia util, Hugo 29/09).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_pedidos_segurados -v
"""
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pedidos_segurados as ps

TERCA, QUARTA, QUINTA = date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)


def _servico(i, code=None):
    return {"id": i, "code": code or f"#PS-{1000 + i}"}


class TestPedidosSegurados(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = ps.conectar(self.db)
        self.addCleanup(self.conn.close)

    def _marcar(self, servicos, data_alvo=TERCA, data_nova=QUARTA):
        return ps.marcar(self.conn, [(s, QUINTA) for s in servicos], data_alvo, data_nova,
                         "2 pedidos, 9 caixas", agora=datetime(2026, 9, 28, 18, 5))

    def test_marca_e_le(self):
        self.assertEqual(self._marcar([_servico(1), _servico(2)]), 2)
        self.assertEqual(ps.codigos_segurados(self.conn), {"PS-1001", "PS-1002"})
        linha = ps.segurados_ativos(self.conn, TERCA)["PS-1001"]
        self.assertEqual((linha["service_id"], linha["data_alvo_original"], linha["data_nova"],
                          linha["prazo_final"], linha["motivo"], linha["segurado_em"]),
                         (1, "2026-09-29", "2026-09-30", "2026-10-01", "2 pedidos, 9 caixas",
                          "2026-09-28 18:05:00"))

    def test_segunda_marcacao_do_mesmo_pedido_e_ignorada(self):
        self._marcar([_servico(1)])
        self.assertEqual(self._marcar([_servico(1)], data_alvo=QUARTA, data_nova=QUINTA), 0)
        self.assertEqual(ps.segurados_ativos(self.conn, TERCA)["PS-1001"]["data_nova"], "2026-09-30")

    def test_codigo_combinado_grava_os_dois(self):
        self.assertEqual(self._marcar([_servico(1, "#PS-1001, PS-2002")]), 2)
        self.assertEqual(ps.codigos_segurados(self.conn), {"PS-1001", "PS-2002"})

    def test_reentrega_cai_no_codigo_base(self):
        self._marcar([_servico(1, "#PS-1001-R1")])
        self.assertEqual(ps.codigos_segurados(self.conn), {"PS-1001"})

    def test_ativo_so_enquanto_a_data_nova_nao_chegou(self):
        self._marcar([_servico(1)])
        self.assertIn("PS-1001", ps.segurados_ativos(self.conn, TERCA))
        self.assertEqual(ps.segurados_ativos(self.conn, QUARTA), {})

    def test_separar_tira_so_os_ativos(self):
        # job rodado de novo na mesma noite: o segurado nao volta pra rota de terca
        self._marcar([_servico(1)])
        servicos = [_servico(1), _servico(2)]
        restantes, segurados = ps.separar_segurados(servicos, TERCA, self.db)
        self.assertEqual([s["id"] for s in restantes], [2])
        self.assertEqual([s["id"] for s in segurados], [1])
        restantes, segurados = ps.separar_segurados(servicos, QUARTA, self.db)
        self.assertEqual([s["id"] for s in restantes], [1, 2])
        self.assertEqual(segurados, [])

    def test_segurados_por_servico(self):
        self._marcar([_servico(1)])
        self.assertEqual(ps.segurados_por_servico([_servico(1), _servico(2)], TERCA, self.db),
                         {1: {"data_nova": "2026-09-30", "prazo_final": "2026-10-01"}})

    def test_banco_inacessivel_nao_derruba(self):
        ruim = Path(self.tmp.name) / "nao_existe" / "t.db"
        servicos = [_servico(1)]
        self.assertEqual(ps.separar_segurados(servicos, TERCA, ruim), (servicos, []))
        self.assertEqual(ps.segurados_por_servico(servicos, TERCA, ruim), {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_pedidos_segurados -v`
Expected: `ModuleNotFoundError: No module named 'pedidos_segurados'`.

- [ ] **Step 3: Implementar**

Criar `roteirizacao/pedidos_segurados.py`:

```python
# -*- coding: utf-8 -*-
"""
Pedidos segurados (Hugo, 29/09/2026): pedido de rota fraca adiado por 1
dia util pra juntar com o volume do dia seguinte (ver rotas_fracas.py).
Uma linha por pedido (codigo base PS-NNNNN): e a chave primaria que
garante o maximo de 1 adiamento. A linha nunca e apagada.

Quem le:
  - criar_rotas_diarias / incrementar_rotas: separar_segurados tira da
    rodada o pedido adiado pra DEPOIS da data alvo;
  - planejamento (pool): segurados_por_servico pro chip "Segurado";
  - vigia: le a tabela direto por SQL (vigia/vigiar.py).
"""
import logging
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402  (normalizacao do codigo do pedido)

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"


def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pedidos_segurados (
            codigo TEXT PRIMARY KEY,
            service_id INTEGER,
            segurado_em TEXT NOT NULL,
            data_alvo_original TEXT NOT NULL,
            data_nova TEXT NOT NULL,
            prazo_final TEXT NOT NULL,
            motivo TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_segurados_data_nova ON pedidos_segurados (data_nova);
    """)
    conn.commit()
    return conn


def marcar(conn: sqlite3.Connection, itens: list[tuple[dict, date]], data_alvo: date, data_nova: date,
           motivo: str, agora: datetime | None = None) -> int:
    """Cada item: (servico, prazo_final). Pedido ja marcado e ignorado
    (INSERT OR IGNORE). Devolve quantos codigos foram gravados."""
    quando = (agora or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    gravados = 0
    for servico, prazo in itens:
        for codigo in pedidos_dedicados.codigos_do_servico(servico):
            cur = conn.execute(
                "INSERT OR IGNORE INTO pedidos_segurados (codigo, service_id, segurado_em, data_alvo_original, "
                "data_nova, prazo_final, motivo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (codigo, servico.get("id"), quando, data_alvo.isoformat(), data_nova.isoformat(),
                 prazo.isoformat(), motivo))
            gravados += cur.rowcount
    conn.commit()
    return gravados


def codigos_segurados(conn: sqlite3.Connection) -> set[str]:
    return {r["codigo"] for r in conn.execute("SELECT codigo FROM pedidos_segurados")}


def segurados_ativos(conn: sqlite3.Connection, data: date) -> dict[str, dict]:
    """{codigo: linha} dos pedidos adiados pra DEPOIS de `data`."""
    rows = conn.execute("SELECT * FROM pedidos_segurados WHERE data_nova > ?", (data.isoformat(),)).fetchall()
    return {r["codigo"]: dict(r) for r in rows}


def _ativos_ou_vazio(data: date, db_path) -> dict[str, dict]:
    try:
        conn = conectar(db_path)
        try:
            return segurados_ativos(conn, data)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"nao carregou pedidos segurados ({e}); seguindo sem a marca")
        return {}


def separar_segurados(servicos: list[dict], data_alvo: date, db_path=DB_PATH) -> tuple[list[dict], list[dict]]:
    """(seguem na rodada, segurados pra depois de data_alvo). Servico com
    mais de um codigo sai inteiro se qualquer um estiver segurado."""
    ativos = _ativos_ou_vazio(data_alvo, db_path)
    if not ativos:
        return servicos, []
    restantes, fora = [], []
    for s in servicos:
        (fora if any(c in ativos for c in pedidos_dedicados.codigos_do_servico(s)) else restantes).append(s)
    if fora:
        logger.info(f"{len(fora)} pedido(s) segurado(s) pra consolidar, fora desta rodada: "
                    f"{[s.get('code') for s in fora]}")
    return restantes, fora


def segurados_por_servico(servicos: list[dict], data_alvo: date, db_path=DB_PATH) -> dict[int, dict]:
    """{service_id: {data_nova, prazo_final}} pros servicos segurados."""
    ativos = _ativos_ou_vazio(data_alvo, db_path)
    mapa: dict[int, dict] = {}
    for s in servicos:
        for c in pedidos_dedicados.codigos_do_servico(s):
            if c in ativos:
                mapa[s["id"]] = {"data_nova": ativos[c]["data_nova"], "prazo_final": ativos[c]["prazo_final"]}
                break
    return mapa
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest roteirizacao.test_pedidos_segurados -v`
Expected: `OK`, 8 testes.

- [ ] **Step 5: Commit**

```bash
git add roteirizacao/pedidos_segurados.py roteirizacao/test_pedidos_segurados.py
git commit -m "Roteirizacao: tabela de pedidos segurados, uma linha por pedido

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Integração no criador de rotas e coluna do rascunho

**Files:**
- Modify: `roteirizacao/criar_rotas_diarias.py`
- Modify: `painel_agentes/rascunhos_rota.py`
- Test: `roteirizacao/test_rotas_fracas_integracao.py`
- Test: `painel_agentes/test_rota_fraca_painel.py`

**Interfaces:**
- Consumes: tudo que as Tarefas 1, 2 e 3 produzem.
- Produces:
  - `planejar_sublotes` passa a devolver, em cada plano, a chave `"rotas_fracas": {"juntadas": int, "motivos": {id(sublote): str}}`.
  - `criar_rotas_diarias._aplicar_segurar(planos: list[dict], data_alvo: date, gmaps_key: str | None, modo_teste: bool) -> dict`. Muta `planos` (tira os sublotes segurados e completa os motivos). Devolve `{"juntadas", "seguradas", "pedidos_segurados", "data_nova", "sobraram", "pedidos_sobraram", "caixas_sobraram"}`; `data_nova` é `date`.
  - `criar_rotas_diarias._frase_rotas_fracas(resumo: dict) -> str`.
  - Dict de rascunho ganha a chave `"rota_fraca_motivo"`; `rascunhos_rota` ganha a coluna `rota_fraca_motivo TEXT`.

- [ ] **Step 1: Escrever os testes da integração**

Criar `roteirizacao/test_rotas_fracas_integracao.py`:

```python
# -*- coding: utf-8 -*-
"""
Rotas fracas dentro do criador de rotas: a juncao roda em
planejar_sublotes (depois do polimento) e o segurar em _aplicar_segurar.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_rotas_fracas_integracao -v
"""
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
import rotas_fracas as rf
import criar_rotas_diarias as crd

BASE = (-23.55, -46.63)
TERCA, QUARTA = date(2026, 9, 29), date(2026, 9, 30)


def _coords(s):
    lat, lng = s.get("latitude"), s.get("longitude")
    return (lat, lng) if lat is not None and lng is not None else None


def _servico(i, dlat=0.01, dlng=0.0, caixas=1):
    return {"id": i, "code": f"#PS-{1000 + i}", "address": f"Rua {i}, Sao Paulo - SP, 01000-000, Brasil",
            "_nivel_dificuldade": 1, "_tipo_carga": "Seco", "latitude": BASE[0] + dlat,
            "longitude": BASE[1] + dlng, "dimension_3": caixas, "sender_id": 1,
            "created_at": "2026-09-28 15:00:00"}


class TestPlanejarSublotes(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(rd, "obter_coordenadas", lambda s, k=None: _coords(s)),
                  mock.patch.object(ot, "obter_coordenadas", lambda s, k=None: _coords(s)),
                  mock.patch.object(sm, "_registrar_historico")):
            p.start()
            self.addCleanup(p.stop)
        rd.COORDS_BASE = BASE
        self.servicos = [_servico(i, 0.01 + 0.001 * i) for i in range(12)]

    def test_plano_traz_o_relatorio_e_cobre_tudo(self):
        planos = crd.planejar_sublotes(self.servicos, BASE, None, TERCA, registrar_historico=False)
        self.assertEqual(set(planos[0]["rotas_fracas"]), {"juntadas", "motivos"})
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))

    def test_chave_desligada_nao_chama_a_juncao(self):
        with mock.patch.object(rf, "ROTAS_FRACAS_ATIVO", False), \
             mock.patch.object(rf, "absorver_rotas_fracas", side_effect=AssertionError("nao deveria rodar")):
            planos = crd.planejar_sublotes(self.servicos, BASE, None, TERCA, registrar_historico=False)
        self.assertEqual(planos[0]["rotas_fracas"], {"juntadas": 0, "motivos": {}})

    def test_falha_na_juncao_nao_derruba_o_plano(self):
        with mock.patch.object(rf, "absorver_rotas_fracas", side_effect=RuntimeError("boom")):
            planos = crd.planejar_sublotes(self.servicos, BASE, None, TERCA, registrar_historico=False)
        ids = sorted(s["id"] for p in planos for sub in p["sublotes"] for s in sub)
        self.assertEqual(ids, sorted(s["id"] for s in self.servicos))
        self.assertEqual(planos[0]["rotas_fracas"], {"juntadas": 0, "motivos": {}})

    def test_sem_base_nao_chama_a_juncao(self):
        with mock.patch.object(rf, "absorver_rotas_fracas", side_effect=AssertionError("nao deveria rodar")):
            crd.planejar_sublotes(self.servicos, None, None, TERCA, registrar_historico=False)


class TestAplicarSegurar(unittest.TestCase):
    def setUp(self):
        self.fraca = [_servico(1, caixas=4), _servico(2, caixas=5)]
        self.normal = [_servico(10 + i, caixas=6) for i in range(10)]
        self.planos = [{"label": "Geral", "modelo": "X", "sublotes": [self.fraca, self.normal],
                        "rotas_fracas": {"juntadas": 2, "motivos": {id(self.fraca): "vizinha mais próxima a 27 km"}}}]
        self.marcar = mock.Mock(return_value=2)
        for p in (mock.patch.object(crd.pedidos_segurados, "conectar", return_value=mock.MagicMock()),
                  mock.patch.object(crd.pedidos_segurados, "codigos_segurados", return_value=set()),
                  mock.patch.object(crd.pedidos_segurados, "marcar", self.marcar),
                  mock.patch.object(rf, "motivo_nao_segurar", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def test_segurar_desligado_so_conta(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", False):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.fraca, self.normal])
        self.marcar.assert_not_called()
        self.assertEqual(resumo, {"juntadas": 2, "seguradas": 0, "pedidos_segurados": 0, "data_nova": QUARTA,
                                  "sobraram": 1, "pedidos_sobraram": 2, "caixas_sobraram": 9})

    def test_segura_a_rota_inteira(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", True):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.normal])
        self.assertNotIn(id(self.fraca), self.planos[0]["rotas_fracas"]["motivos"])
        itens, data_alvo, data_nova, motivo = self.marcar.call_args.args[1:5]
        self.assertEqual([s["id"] for s, _ in itens], [1, 2])
        self.assertEqual({prazo for _, prazo in itens}, {date(2026, 10, 1)})
        self.assertEqual((data_alvo, data_nova, motivo), (TERCA, QUARTA, "2 pedidos, 9 caixas"))
        self.assertEqual((resumo["seguradas"], resumo["pedidos_segurados"], resumo["sobraram"]), (1, 2, 0))

    def test_modo_teste_tira_da_lista_mas_nao_grava(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", True):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=True)
        self.assertEqual(self.planos[0]["sublotes"], [self.normal])
        self.marcar.assert_not_called()
        self.assertEqual(resumo["seguradas"], 1)

    def test_um_pedido_que_nao_pode_esperar_trava_a_rota(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", True), \
             mock.patch.object(rf, "motivo_nao_segurar",
                               side_effect=lambda s, d, ja, k=None: "tem agendamento" if s["id"] == 2 else None):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.fraca, self.normal])
        self.marcar.assert_not_called()
        self.assertEqual(self.planos[0]["rotas_fracas"]["motivos"][id(self.fraca)],
                         "vizinha mais próxima a 27 km; PS-1002 tem agendamento")
        self.assertEqual((resumo["seguradas"], resumo["sobraram"]), (0, 1))

    def test_falha_ao_ler_segurados_nao_segura(self):
        with mock.patch.object(rf, "SEGURAR_ATIVO", True), \
             mock.patch.object(crd.pedidos_segurados, "conectar", side_effect=RuntimeError("banco travado")):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.fraca, self.normal])
        self.assertEqual((resumo["seguradas"], resumo["sobraram"]), (0, 1))

    def test_falha_ao_gravar_nao_segura(self):
        self.marcar.side_effect = RuntimeError("disco cheio")
        with mock.patch.object(rf, "SEGURAR_ATIVO", True):
            resumo = crd._aplicar_segurar(self.planos, TERCA, None, modo_teste=False)
        self.assertEqual(self.planos[0]["sublotes"], [self.fraca, self.normal])
        self.assertIn("falha ao registrar o adiamento",
                      self.planos[0]["rotas_fracas"]["motivos"][id(self.fraca)])
        self.assertEqual(resumo["sobraram"], 1)

    def test_plano_sem_relatorio_passa_direto(self):
        planos = [{"label": "Geral", "modelo": "X", "sublotes": [self.normal]}]
        resumo = crd._aplicar_segurar(planos, TERCA, None, modo_teste=False)
        self.assertEqual(planos[0]["sublotes"], [self.normal])
        self.assertEqual((resumo["juntadas"], resumo["sobraram"]), (0, 0))


class TestFrase(unittest.TestCase):
    def test_frase_completa(self):
        r = {"juntadas": 2, "seguradas": 1, "pedidos_segurados": 3, "data_nova": QUARTA,
             "sobraram": 1, "pedidos_sobraram": 2, "caixas_sobraram": 9}
        self.assertEqual(crd._frase_rotas_fracas(r),
                         " Rotas fracas: 2 juntada(s) em vizinhas, 1 segurada(s) para 30/09 (3 pedido(s)), "
                         "1 sem solução (2 pedido(s), 9 caixa(s)).")

    def test_sem_rota_fraca_nao_escreve_nada(self):
        r = {"juntadas": 0, "seguradas": 0, "pedidos_segurados": 0, "data_nova": QUARTA,
             "sobraram": 0, "pedidos_sobraram": 0, "caixas_sobraram": 0}
        self.assertEqual(crd._frase_rotas_fracas(r), "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Escrever o teste da coluna**

Criar `painel_agentes/test_rota_fraca_painel.py`:

```python
# -*- coding: utf-8 -*-
"""
Rota fraca no painel (Hugo, 29/09): coluna rota_fraca_motivo do rascunho
e etiqueta no card da rota.
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_rota_fraca_painel -v
"""
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import rascunhos_rota  # noqa: E402


class TestColunaMotivo(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.caminho = Path(self.tmp.name) / "dados.db"
        for patcher in (mock.patch.object(rascunhos_rota, "DB_PATH", self.caminho),
                        mock.patch("mapa_util.carregar_remetentes_por_sender_id", lambda: {})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rascunho(self, **extra):
        return {"nome": "Rota 1", "start_location_base_id": 1, "start_at": "2026-09-30T09:00:00Z",
                "sublote": [], **extra}

    def test_migracao_em_banco_antigo(self):
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
        conn = rascunhos_rota._conectar()
        try:
            self.assertIn("rota_fraca_motivo", {r["name"] for r in conn.execute("PRAGMA table_info(rascunhos_rota)")})
        finally:
            conn.close()

    def test_motivo_gravado_volta_na_leitura(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 30), [
            self._rascunho(rota_fraca_motivo="vizinha mais próxima a 27 km")])
        rotas = rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 30))
        self.assertEqual(rotas[0]["rota_fraca_motivo"], "vizinha mais próxima a 27 km")

    def test_sem_motivo_fica_nulo(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 30), [self._rascunho()])
        self.assertIsNone(rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 30))[0]["rota_fraca_motivo"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_rotas_fracas_integracao -v`
Expected: FAIL, `AttributeError: module 'criar_rotas_diarias' has no attribute 'pedidos_segurados'` e `KeyError: 'rotas_fracas'`.

Run: `py -3.11 -m unittest painel_agentes.test_rota_fraca_painel -v`
Expected: FAIL nos três, coluna `rota_fraca_motivo` ausente.

- [ ] **Step 4: Implementar a coluna em `painel_agentes/rascunhos_rota.py`**

Logo depois do bloco "Migração 22/09" (o `if "horas_estimadas" not in colunas_rota:`), acrescentar:

```python
    # Migração 29/09: motivo de a rota ter saído fraca (poucos pedidos e
    # poucas caixas) sem juntar nem segurar -- ver roteirizacao/
    # rotas_fracas.py. Vira etiqueta no card (planejamento_rotas._badges_trava).
    if "rota_fraca_motivo" not in colunas_rota:
        conn.execute("ALTER TABLE rascunhos_rota ADD COLUMN rota_fraca_motivo TEXT")
```

Em `criar_lote_rascunhos`, trocar o INSERT por:

```python
            cursor = conn.execute("""
                INSERT INTO rascunhos_rota (
                    data_alvo, lote_id, nome, particao, tipo_rota, zona, tipo_veiculo,
                    agent_id, vehicle_id, motorista_nome,
                    start_location_base_id, end_location_base_id,
                    start_at, km_estimado, horas_estimadas, rota_fraca_motivo, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data_alvo.isoformat(), lote_id, r["nome"], r.get("particao"),
                r.get("tipo_rota"), r.get("zona"), r.get("tipo_veiculo"), r.get("agent_id"), r.get("vehicle_id"),
                r.get("motorista_nome"), r["start_location_base_id"], r.get("end_location_base_id"),
                r["start_at"], r.get("km_estimado"), r.get("horas_estimadas"), r.get("rota_fraca_motivo"),
                STATUS_RASCUNHO,
            ))
```

E na docstring de `criar_lote_rascunhos`, acrescentar `rota_fraca_motivo` à lista de chaves, depois de `horas_estimadas`.

- [ ] **Step 5: Implementar em `roteirizacao/criar_rotas_diarias.py`**

**5a.** Depois da linha `from regras.prioridade_ofertas import carregar_historico_justica`, acrescentar:

```python
import rotas_fracas
import pedidos_segurados
```

**5b.** Depois da função `_polir_particao` (antes de `def planejar_sublotes`), acrescentar:

```python
def _absorver_fracas_particao(sublotes: list[list[dict]], coords_base, gmaps_key: str | None, label: str,
                              tamanho_maximo: int = TAMANHO_MAXIMO_ROTA) -> tuple[list[list[dict]], dict]:
    """Junção das rotas fracas de UMA partição (Hugo, 29/09 -- ver
    rotas_fracas.py). Roda depois do polimento. Devolve (sublotes,
    relatório); sem base, com a chave desligada ou com falha, devolve a
    entrada intocada e o relatório vazio."""
    vazio = {"juntadas": 0, "motivos": {}}
    if not rotas_fracas.ROTAS_FRACAS_ATIVO or not coords_base:
        return sublotes, vazio
    try:
        novos, relatorio = rotas_fracas.absorver_rotas_fracas(
            sublotes, coords_base[0], coords_base[1], gmaps_key,
            tamanho_maximo=tamanho_maximo, volume_maximo=VOLUME_MAXIMO_ROTA,
            distancia_maxima_km=DISTANCIA_MAXIMA_ROTA_KM,
            distancia_maxima_viagem_km=DISTANCIA_MAXIMA_VIAGEM_KM,
            km_acumulado_maximo=KM_ACUMULADO_MAXIMO_ROTA_KM,
            km_acumulado_maximo_viagem=KM_ACUMULADO_MAXIMO_VIAGEM_KM,
            eh_viagem_fn=lambda sub: classificar_rota_viagem(sub, gmaps_key),
        )
    except Exception as e:
        # mesma postura do polimento: etapa opcional, plano sem ela já é um plano bom
        logger.exception(f"[{label}] Falha na junção das rotas fracas (seguindo com as rotas como estavam): {e}")
        return sublotes, vazio
    if relatorio["juntadas"] or relatorio["motivos"]:
        logger.info(f"[{label}] Rotas fracas: {relatorio['juntadas']} juntada(s) em vizinhas, "
                    f"{len(relatorio['motivos'])} continuam fraca(s) -- {len(sublotes)} -> {len(novos)} rota(s).")
    return novos, relatorio
```

**5c.** Em `planejar_sublotes`, trocar:

```python
        sublotes = _polir_particao(sublotes, coords_base, gmaps_key, rotulo, tamanho_maximo,
                                   polimento_tempo_maximo_s)
```

por:

```python
        sublotes = _polir_particao(sublotes, coords_base, gmaps_key, rotulo, tamanho_maximo,
                                   polimento_tempo_maximo_s)
        sublotes, fracas = _absorver_fracas_particao(sublotes, coords_base, gmaps_key, rotulo, tamanho_maximo)
```

E trocar:

```python
        planos.append({"label": label, "modelo": modelo, "sublotes": sublotes})
```

por:

```python
        planos.append({"label": label, "modelo": modelo, "sublotes": sublotes, "rotas_fracas": fracas})
```

Na docstring de `planejar_sublotes`, trocar `Devolve [{"label", "modelo", "sublotes"}]` por `Devolve [{"label", "modelo", "sublotes", "rotas_fracas"}]` e acrescentar `-> junção das rotas fracas` depois de `-> polimento entre rotas`.

**5d.** Depois de `planejar_sublotes` (antes de `def roteirizar_para_rascunhos`), acrescentar:

```python
def _tentar_segurar(sublote: list[dict], data_alvo: date, data_nova: date, ja_segurados: set[str],
                    gmaps_key: str | None, modo_teste: bool) -> str | None:
    """None = segurou a rota inteira. Senão, por que não segurou."""
    for s in sublote:
        impedimento = rotas_fracas.motivo_nao_segurar(s, data_alvo, ja_segurados, gmaps_key)
        if impedimento:
            return f"{str(s.get('code') or '').lstrip('#').strip()} {impedimento}"
    if modo_teste:
        return None
    try:
        conn = pedidos_segurados.conectar()
        try:
            pedidos_segurados.marcar(
                conn, [(s, rotas_fracas.prazo_final(rotas_fracas.data_entrada(s))) for s in sublote],
                data_alvo, data_nova, rotas_fracas.resumo_da_rota(sublote))
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Falha ao registrar pedidos segurados (a rota sai normalmente): {e}")
        return "falha ao registrar o adiamento"
    return None


def _aplicar_segurar(planos: list[dict], data_alvo: date, gmaps_key: str | None, modo_teste: bool) -> dict:
    """Segundo passo das rotas fracas (Hugo, 29/09), SÓ no job automático:
    a rota que continuou fraca depois da junção não é criada se TODOS os
    pedidos dela puderem esperar 1 dia útil. Roda antes de alocar
    motorista. MUTA `planos`: tira os sublotes segurados e completa o
    motivo dos que ficaram. Devolve o resumo pro log e pro WhatsApp."""
    data_nova = rotas_fracas.proximo_dia_util(data_alvo)
    resumo = {"juntadas": 0, "seguradas": 0, "pedidos_segurados": 0, "data_nova": data_nova,
              "sobraram": 0, "pedidos_sobraram": 0, "caixas_sobraram": 0}
    ja_segurados: set[str] | None = None
    pode_segurar = rotas_fracas.SEGURAR_ATIVO
    for plano in planos:
        fracas = plano.get("rotas_fracas") or {"juntadas": 0, "motivos": {}}
        resumo["juntadas"] += fracas["juntadas"]
        motivos = fracas["motivos"]
        restantes = []
        for sub in plano["sublotes"]:
            if id(sub) not in motivos:
                restantes.append(sub)
                continue
            if pode_segurar and ja_segurados is None:
                try:
                    conn = pedidos_segurados.conectar()
                    try:
                        ja_segurados = pedidos_segurados.codigos_segurados(conn)
                    finally:
                        conn.close()
                except Exception as e:
                    # sem saber quem já foi segurado não dá pra garantir o máximo de 1 adiamento
                    logger.error(f"Falha ao ler pedidos segurados -- nesta rodada ninguém é segurado: {e}")
                    pode_segurar = False
            impedimento = (_tentar_segurar(sub, data_alvo, data_nova, ja_segurados, gmaps_key, modo_teste)
                           if pode_segurar else "")
            if impedimento is None:
                codigos = [s.get("code") for s in sub]
                logger.info(f"{'[TESTE] Seguraria' if modo_teste else 'Segurada'} rota fraca "
                            f"({rotas_fracas.resumo_da_rota(sub)}) para {data_nova:%d/%m/%Y}: {codigos}")
                resumo["seguradas"] += 1
                resumo["pedidos_segurados"] += len(sub)
                del motivos[id(sub)]
                continue
            if impedimento:
                motivos[id(sub)] = f"{motivos[id(sub)]}; {impedimento}"
            resumo["sobraram"] += 1
            resumo["pedidos_sobraram"] += len(sub)
            resumo["caixas_sobraram"] += sum(extrair_volume_caixas(s) for s in sub)
            restantes.append(sub)
        plano["sublotes"] = restantes
    return resumo


def _frase_rotas_fracas(resumo: dict) -> str:
    partes = []
    if resumo["juntadas"]:
        partes.append(f"{resumo['juntadas']} juntada(s) em vizinhas")
    if resumo["seguradas"]:
        partes.append(f"{resumo['seguradas']} segurada(s) para {resumo['data_nova']:%d/%m} "
                      f"({resumo['pedidos_segurados']} pedido(s))")
    if resumo["sobraram"]:
        partes.append(f"{resumo['sobraram']} sem solução ({resumo['pedidos_sobraram']} pedido(s), "
                      f"{resumo['caixas_sobraram']} caixa(s))")
    return f" Rotas fracas: {', '.join(partes)}." if partes else ""
```

Conferir se `extrair_volume_caixas` já está importado no topo de `criar_rotas_diarias.py`:

Run: `grep -n "extrair_volume_caixas" roteirizacao/criar_rotas_diarias.py | head -3`

Se não aparecer num `from roteirizacao_dados import (...)`, acrescentar o nome a esse import.

**5e.** Em `roteirizar_para_rascunhos`, trocar:

```python
    for plano in planos:
        label, sublotes = plano["label"], plano["sublotes"]
        for sublote in sublotes:
```

por:

```python
    for plano in planos:
        label, sublotes = plano["label"], plano["sublotes"]
        motivos_fracas = (plano.get("rotas_fracas") or {}).get("motivos") or {}
        for sublote in sublotes:
```

E, no dict do `rascunhos.append({...})` dessa função, acrescentar depois de `"horas_estimadas": round(horas_sublote, 2),`:

```python
                "rota_fraca_motivo": motivos_fracas.get(id(sublote)),
```

**5f.** Em `main`, depois do bloco `# Dedicado (Hugo, 23/09)` (o `try/except` de `separar_dedicados`), acrescentar:

```python
        # Segurado (Hugo, 29/09): pedido de rota fraca adiado pra DEPOIS
        # desta data alvo não volta à roteirização -- sem isso, rodar o
        # job de novo na mesma noite recriaria a rota fraca.
        servicos_brutos, _segurados = pedidos_segurados.separar_segurados(servicos_brutos, data_alvo)
```

**5g.** Em `main`, trocar a assinatura do fecho:

```python
        def _rotear_particao(label: str, sublotes_do_dia: list[list[dict]]):
```

por:

```python
        def _rotear_particao(label: str, sublotes_do_dia: list[list[dict]], motivos_fracas: dict[int, str]):
```

No dict do `rascunhos_acumulados.append({...})`, acrescentar depois de `"horas_estimadas": round(horas_sublote, 2),`:

```python
                        "rota_fraca_motivo": motivos_fracas.get(id(sublote)),
```

**5h.** Em `main`, trocar:

```python
        planos = planejar_sublotes(servicos, coords_base, gmaps_key, data_alvo)
        for plano in planos:
            logger.info(f"Partição '{plano['label']}': modelo {plano['modelo']}, {len(plano['sublotes'])} rota(s).")
            modelos_vencedores[plano["label"]] = plano["modelo"]
            _rotear_particao(plano["label"], plano["sublotes"])
```

por:

```python
        planos = planejar_sublotes(servicos, coords_base, gmaps_key, data_alvo)
        resumo_fracas = _aplicar_segurar(planos, data_alvo, gmaps_key, modo_teste)
        for plano in planos:
            logger.info(f"Partição '{plano['label']}': modelo {plano['modelo']}, {len(plano['sublotes'])} rota(s).")
            modelos_vencedores[plano["label"]] = plano["modelo"]
            _rotear_particao(plano["label"], plano["sublotes"], plano["rotas_fracas"]["motivos"])
```

**5i.** Em `main`, no `resumo_etapas["Criação de rotas"]`, trocar:

```python
            "detalhe": detalhe_criacao
                      + (" Modelo do dia: "
```

por:

```python
            "detalhe": detalhe_criacao
                      + _frase_rotas_fracas(resumo_fracas)
                      + (" Modelo do dia: "
```

- [ ] **Step 6: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_rotas_fracas_integracao -v
py -3.11 -m unittest painel_agentes.test_rota_fraca_painel -v
```

Expected: `OK` nos dois (13 e 3 testes).

- [ ] **Step 7: Rodar as suítes vizinhas**

```bash
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -3
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -3
```

Expected: `OK`, com a contagem da Task 0 mais os testes novos. Se `test_planejar_sublotes` quebrar por contagem de rotas, é a junção agindo sobre o cenário do teste: nesse teste, desligar com `mock.patch.object(rotas_fracas, "ROTAS_FRACAS_ATIVO", False)` e registrar no commit qual teste precisou disso.

- [ ] **Step 8: Rodar o job em modo teste**

Run: `py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste 2>&1 | tail -40`

Expected: termina sem exceção. Com pedidos no pool local, aparece a linha `Rotas fracas: ...` no log quando houver rota fraca. O banco local está congelado desde 17/08, então "Nenhum pedido not_assigned elegível" também é saída válida.

- [ ] **Step 9: Commit**

```bash
git add roteirizacao/criar_rotas_diarias.py roteirizacao/test_rotas_fracas_integracao.py painel_agentes/rascunhos_rota.py painel_agentes/test_rota_fraca_painel.py
git commit -m "Roteirizacao: rota fraca junta, segura (desligado) ou sai com o motivo no rascunho

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Etiqueta no Planejamento e chip no pool

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py`
- Modify: `painel_agentes/templates/planejamento_rotas.html`
- Test: `painel_agentes/test_rota_fraca_painel.py`

**Interfaces:**
- Consumes: coluna `rota_fraca_motivo` (Task 4); `pedidos_segurados.segurados_por_servico(servicos, data_alvo) -> {service_id: {"data_nova", "prazo_final"}}` (Task 3).
- Produces: aviso `rota fraca: N pedido(s), M caixa(s). <motivo>` em `rascunho["badges"]`; campo `segurado` em cada item do pool.

- [ ] **Step 1: Acrescentar os testes da etiqueta**

No fim de `painel_agentes/test_rota_fraca_painel.py`, antes do `if __name__`:

```python
import planejamento_rotas  # noqa: E402


def _parada(i, caixas=1):
    return {"service_id": i, "codigo": f"PS-{1000 + i}", "endereco": f"Rua {i}", "sender_id": 1,
            "latitude": -23.50, "longitude": -46.60 + i * 0.001, "nivel_dificuldade": 1,
            "volume_caixas": caixas, "janela_inicio": None, "janela_fim": None}


class TestEtiqueta(unittest.TestCase):
    MOTIVO = "vizinha mais próxima a 27 km"

    def setUp(self):
        for patcher in (mock.patch.object(planejamento_rotas, "_simular_rascunho", lambda paradas: None),
                        mock.patch.object(planejamento_rotas, "_garantir_coords_base", lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _badges(self, paradas, motivo):
        return planejamento_rotas._badges_trava(
            {"paradas": paradas, "tipo_veiculo": None, "tipo_rota": "GRANDE_SP", "rota_fraca_motivo": motivo})

    def test_rota_fraca_com_motivo_ganha_etiqueta(self):
        badges = self._badges([_parada(1, 9), _parada(2, 4), _parada(3, 4)], self.MOTIVO)
        self.assertIn("rota fraca: 3 pedido(s), 17 caixa(s). vizinha mais próxima a 27 km", badges)

    def test_sem_motivo_nao_ganha(self):
        self.assertEqual(self._badges([_parada(1)], None), [])

    def test_passou_de_sete_pedidos_a_etiqueta_some(self):
        self.assertEqual(self._badges([_parada(i) for i in range(8)], self.MOTIVO), [])

    def test_passou_de_quarenta_caixas_a_etiqueta_some(self):
        self.assertEqual(self._badges([_parada(1, 41)], self.MOTIVO), [])
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_rota_fraca_painel.TestEtiqueta -v`
Expected: FAIL em `test_rota_fraca_com_motivo_ganha_etiqueta` (lista sem o aviso).

- [ ] **Step 3: Implementar a etiqueta**

Em `painel_agentes/planejamento_rotas.py`, depois da linha `DISTANCIA_MAXIMA_ROTA_KM = 15 ...`, acrescentar:

```python
# Rota fraca (Hugo, 29/09): mesmo corte de roteirizacao/rotas_fracas.py
# (constantes separadas de propósito, sem import entre os dois módulos).
PARADAS_ROTA_FRACA = 7
CAIXAS_ROTA_FRACA = 40
```

Em `_badges_trava`, imediatamente antes do `return badges` final, acrescentar:

```python
    # Rota fraca (Hugo, 29/09): o motivo vem da roteirização (não coube
    # em vizinha nem pôde ser segurada). Se a rota foi editada e passou
    # do corte, o aviso some sozinho.
    motivo_fraca = rascunho.get("rota_fraca_motivo")
    if motivo_fraca and len(paradas) <= PARADAS_ROTA_FRACA and caixas <= CAIXAS_ROTA_FRACA:
        badges.append(f"rota fraca: {len(paradas)} pedido(s), {caixas} caixa(s). {motivo_fraca}")
```

- [ ] **Step 4: Implementar a marca no pool**

Em `buscar_pool_e_agendados`, depois do bloco `# Dedicado (Hugo, 23/09)` (o `try/except` de `dedicados_por_servico`), acrescentar:

```python
    # Segurado (Hugo, 29/09): pedido de rota fraca adiado pra consolidar.
    # Só rotula o card; continua selecionável pra rota manual.
    segurados_por_id: dict[int, dict] = {}
    try:
        from pedidos_segurados import segurados_por_servico
        segurados_por_id = segurados_por_servico(servicos_brutos, data_alvo)
    except Exception as e:
        logger.warning(f"Falha ao marcar segurados pro pool (tela segue sem essa marcação): {e}")
```

E trocar:

```python
        for p in pool:
            p["fora_dia_fixo"] = fora_dia_fixo.get(p["service_id"])
```

por:

```python
        for p in pool:
            p["fora_dia_fixo"] = fora_dia_fixo.get(p["service_id"])
            p["segurado"] = segurados_por_id.get(p["service_id"])
```

- [ ] **Step 5: Implementar o chip no template**

Em `painel_agentes/templates/planejamento_rotas.html`, na função `badgesPedidoHtml`, logo depois do bloco `if (p.dedicado) { ... }`, acrescentar:

```javascript
    // segurado (Hugo, 29/09): rota fraca adiada pra juntar com o volume do dia seguinte
    if (p.segurado) {
      const dm = iso => `${iso.slice(8, 10)}/${iso.slice(5, 7)}`;
      partes.push(`<span class="badge-dedicado" title="Rota fraca: pedido adiado para ${dm(p.segurado.data_nova)}. Pode ser colocado em rota à mão.">⏸ Segurado · prazo ${dm(p.segurado.prazo_final)}</span>`);
    }
```

O chip reaproveita a classe `badge-dedicado` de propósito: já tem estilo no card, no modo "só números" e no balão do mapa.

- [ ] **Step 6: Rodar e ver passar**

```bash
py -3.11 -m unittest painel_agentes.test_rota_fraca_painel -v
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -3
```

Expected: `OK` (7 testes no primeiro).

- [ ] **Step 7: Conferir a tela de verdade**

De dentro de `painel_agentes/`, subir o painel na porta 8099 (nunca na 8070):

```bash
cd painel_agentes && py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"
```

Em outro terminal, com o painel no ar, gravar um rascunho fraco de teste e um pedido segurado no banco local e abrir `/planejamento?data=<data do rascunho>`. Conferir: a etiqueta "rota fraca: ..." aparece no card, entra no resumo de rotas com alerta, e o console do navegador não tem erro. Apagar o rascunho de teste no fim. Armadilhas conhecidas desse teste estão na memória `feedback_testar_painel_playwright_local` (login cai na Torre, template em cache).

Se não der para subir o painel no worktree (falta `config.yaml`), copiar o `config.yaml` do working tree principal para o worktree só para o teste e não commitar.

- [ ] **Step 8: Commit**

```bash
git add painel_agentes/planejamento_rotas.py painel_agentes/templates/planejamento_rotas.html painel_agentes/test_rota_fraca_painel.py
git commit -m "Planejamento: etiqueta de rota fraca com o motivo e chip de pedido segurado no pool

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Vigia

**Files:**
- Modify: `vigia/regras.py`
- Modify: `vigia/vigiar.py`
- Test: `vigia/test_regras.py`, `vigia/test_vigiar.py`

**Interfaces:**
- Consumes: tabela `pedidos_segurados` (colunas `codigo`, `data_nova`, `prazo_final`).
- Produces: `regras.prazo(estado, desde, *, data_rascunho=None, prazo_segurado: date | None = None)`; fato `prazo_segurado` no dict de `coletar_fatos`.

- [ ] **Step 1: Acrescentar os testes**

Em `vigia/test_regras.py`, dentro de `class TestPrazos`, acrescentar:

```python
    def test_pedido_segurado_vence_na_vespera_do_prazo_final(self):
        desde = datetime(2026, 9, 28, 9, 0)
        # prazo final quinta 01/10 -> alarme quarta 30/09 19h
        self.assertEqual(r.prazo(r.NO_POOL, desde, prazo_segurado=date(2026, 10, 1)),
                         datetime(2026, 9, 30, 19, 0))
        # prazo final segunda 05/10 -> alarme sexta 02/10 19h
        self.assertEqual(r.prazo(r.NO_POOL, desde, prazo_segurado=date(2026, 10, 5)),
                         datetime(2026, 10, 2, 19, 0))

    def test_prazo_segurado_so_vale_no_pool(self):
        desde = datetime(2026, 9, 29, 8, 0)
        self.assertEqual(r.prazo(r.SEM_SERVICO, desde, prazo_segurado=date(2026, 10, 1)),
                         datetime(2026, 9, 29, 12, 0))
```

Em `vigia/test_vigiar.py`, dentro de `class TestRodada`, acrescentar:

```python
    def _segurar(self, codigo, data_nova, prazo_final):
        self.conn.execute("CREATE TABLE IF NOT EXISTS pedidos_segurados (codigo TEXT PRIMARY KEY, "
                          "service_id INTEGER, segurado_em TEXT, data_alvo_original TEXT, data_nova TEXT, "
                          "prazo_final TEXT, motivo TEXT)")
        self.conn.execute("INSERT INTO pedidos_segurados VALUES (?, NULL, '2026-09-28 18:05:00', '2026-09-29', ?, ?, "
                          "'2 pedidos, 9 caixas')", (codigo, data_nova, prazo_final))
        self.conn.commit()

    def test_pedido_segurado_nao_alarma_enquanto_espera(self):
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, vuupt_route_id, "
                          "criado_em_provedor, fluxo) VALUES ('PS-8', 8, 'ABERTO', NULL, '2026-09-28 09:00:00', 'ENTREGA')")
        self._segurar("PS-8", "2026-09-30", "2026-10-01")
        vigiar.rodar(self.conn, agora=AGORA)  # terca 29/09 10h
        e = self._estados()["PS-8"]
        self.assertEqual(e["estado"], "NO_POOL")
        self.assertEqual(e["motivo"], "segurado para consolidar (prazo 01/10)")
        self.assertEqual(e["vence_em"], "2026-09-30 19:00:00")
        self.assertEqual(e["vencido"], 0)

    def test_pedido_segurado_volta_a_alarmar_quando_a_data_chega(self):
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, vuupt_route_id, "
                          "criado_em_provedor, fluxo) VALUES ('PS-8', 8, 'ABERTO', NULL, '2026-09-28 09:00:00', 'ENTREGA')")
        self._segurar("PS-8", "2026-09-30", "2026-10-01")
        vigiar.rodar(self.conn, agora=datetime(2026, 9, 30, 10, 0))  # quarta: devia estar em rota
        e = self._estados()["PS-8"]
        self.assertEqual(e["motivo"], "aguardando roteirização")
        self.assertEqual(e["vencido"], 1)

    def test_sem_a_tabela_de_segurados_o_vigia_roda_igual(self):
        vigiar.rodar(self.conn, agora=AGORA)
        self.assertEqual(self._estados()["PS-1"]["estado"], "NO_POOL")
```

Conferir o formato de `vence_em` antes de rodar: `grep -n "^FMT" vigia/banco.py`. Se `FMT` não for `%Y-%m-%d %H:%M:%S`, ajustar a string esperada do primeiro teste para o formato real.

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest vigia.test_regras vigia.test_vigiar -v`
Expected: FAIL, `TypeError: prazo() got an unexpected keyword argument 'prazo_segurado'` e motivo `aguardando roteirização` no lugar de `segurado para consolidar`.

- [ ] **Step 3: Implementar em `vigia/regras.py`**

Na docstring do módulo, depois da linha do `NO_POOL`, acrescentar:

```
                       (segurado pra consolidar rota fraca)       19h do último dia útil antes do
                                                                    prazo de 3 dias úteis (Hugo, 29/09)
```

Trocar a assinatura e o ramo do NO_POOL em `prazo`:

```python
def prazo(estado: str, desde: datetime, *, data_rascunho: date | None = None,
          prazo_segurado: date | None = None) -> datetime | None:
    """Quando o prazo do estado vence (None = estado sem prazo).
    `prazo_segurado`: prazo final de entrega do pedido segurado de
    propósito pela regra de rota fraca (roteirizacao/rotas_fracas.py)."""
    if estado == SEM_SERVICO:
        return desde + timedelta(hours=HORAS_SEM_SERVICO)
    if estado == NO_POOL:
        if prazo_segurado:
            return datetime.combine(ultimo_dia_util_antes(prazo_segurado), HORA_LIMITE_RASCUNHO)
        return somar_dias_uteis(desde, DIAS_UTEIS_NO_POOL)
```

O resto da função fica igual.

- [ ] **Step 4: Implementar em `vigia/vigiar.py`**

Em `coletar_fatos`, depois da leitura de `area_nao_atendida`, acrescentar:

```python
    # Rota fraca adiada de propósito (roteirizacao/rotas_fracas.py): só
    # conta enquanto a data nova não chegou.
    segurados = {banco.normalizar(r["codigo"]): r["prazo_final"] for r in _consultar(
        conn, "SELECT codigo, prazo_final FROM pedidos_segurados WHERE data_nova > ?", (hoje.isoformat(),))}
```

No dict `f`, trocar o `"motivo_pool"` por:

```python
            "motivo_pool": ("dedicado -- transporte cotado à parte, fora da rota compartilhada"
                            if codigo in dedicados or base in dedicados else
                            f"área não atendida (embarcador avisado em {str(area_nao_atendida[sid])[:10]})"
                            if sid in area_nao_atendida else
                            f"segurado para consolidar (prazo {_d(segurados[base]):%d/%m})"
                            if base in segurados else None),
            "prazo_segurado": _d(segurados[base]) if base in segurados else None,
```

Em `rodar`, trocar:

```python
        vence = regras.prazo(estado, desde, data_rascunho=f.get("rascunho_data"))
```

por:

```python
        vence = regras.prazo(estado, desde, data_rascunho=f.get("rascunho_data"),
                             prazo_segurado=f.get("prazo_segurado"))
```

- [ ] **Step 5: Rodar e ver passar**

Run: `py -3.11 -m unittest vigia.test_regras vigia.test_vigiar -v`
Expected: `OK`, com 5 testes a mais que na Task 0.

Se `test_pedido_segurado_nao_alarma_enquanto_espera` falhar no `desde`: o vigia mantém o `desde` gravado quando o estado não muda, o que não afeta o prazo do segurado (ele não depende do `desde`). Conferir se `base` do pedido bate com o código gravado antes de mexer.

- [ ] **Step 6: Commit**

```bash
git add vigia/regras.py vigia/vigiar.py vigia/test_regras.py vigia/test_vigiar.py
git commit -m "Vigia: pedido segurado mostra o motivo e so alarma na vespera do prazo de 3 dias uteis

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Incrementar Rotas pula segurados

**Files:**
- Modify: `roteirizacao/incrementar_rotas.py`

**Interfaces:**
- Consumes: `pedidos_segurados.separar_segurados(servicos, data_alvo) -> (restantes, segurados)` (testado na Task 3, inclusive com banco inacessível).

- [ ] **Step 1: Implementar**

Em `roteirizacao/incrementar_rotas.py`, depois do bloco `# Dedicado (Hugo, 23/09)` (o `try/except` de `separar_dedicados`), acrescentar:

```python
        # Segurado (Hugo, 29/09): pedido de rota fraca adiado pra depois
        # desta data alvo não é encaixado -- senão o incremento desfaz a
        # decisão da roteirização. Só some do lote deste ciclo.
        from pedidos_segurados import separar_segurados
        servicos, _segurados = separar_segurados(servicos, data_alvo)
```

Conferir o nome da variável de data nesse ponto da função:

Run: `grep -n "data_alvo = " roteirizacao/incrementar_rotas.py`

Expected: uma atribuição de `data_alvo` antes da linha 450. Se o nome for outro, usar o nome real.

- [ ] **Step 2: Conferir**

```bash
py -3.11 -m py_compile roteirizacao/incrementar_rotas.py
py -3.11 -m unittest roteirizacao.test_incrementar_rotas -v 2>&1 | tail -3
py -3.11 roteirizacao/incrementar_rotas.py --modo-teste 2>&1 | tail -20
```

Expected: compila, `OK`, e o modo teste termina sem exceção.

- [ ] **Step 3: Commit**

```bash
git add roteirizacao/incrementar_rotas.py
git commit -m "Incrementar rotas: pula pedido segurado, pra nao desfazer o adiamento da rota fraca

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Resumo no WhatsApp

**Files:**
- Modify: `notificar_whatsapp.py`
- Modify: `roteirizacao/criar_rotas_diarias.py`
- Test: `test_notificar_whatsapp.py`

**Interfaces:**
- Consumes: resumo de `_aplicar_segurar` (Task 4); `notificar_whatsapp.despachar(config, origem, tipo, texto, assinatura, modo_teste=...) -> str`; `notificar_whatsapp._plural(n, singular, plural) -> str`; `MAX_MENSAGEM`.
- Produces: `texto_rotas_fracas(data_alvo: date, resumo: dict) -> str`; `avisar_rotas_fracas(data_alvo: date, resumo: dict, config: dict, modo_teste: bool = False, **kw) -> str`.

- [ ] **Step 1: Acrescentar os testes**

No fim de `test_notificar_whatsapp.py`, antes do `if __name__` (se existir; senão, no fim do arquivo):

```python
import datetime as _dt


class TestRotasFracas(unittest.TestCase):
    ALVO = _dt.date(2026, 9, 30)

    def _resumo(self, **extra):
        base = {"juntadas": 0, "seguradas": 0, "pedidos_segurados": 0, "data_nova": _dt.date(2026, 10, 1),
                "sobraram": 0, "pedidos_sobraram": 0, "caixas_sobraram": 0}
        base.update(extra)
        return base

    def test_completo(self):
        r = self._resumo(juntadas=2, seguradas=1, pedidos_segurados=3,
                         sobraram=1, pedidos_sobraram=2, caixas_sobraram=9)
        self.assertEqual(nw.texto_rotas_fracas(self.ALVO, r), "\n".join([
            "⚠️ *Rotas fracas* · rotas de 30/09",
            "• 2 juntadas em rotas vizinhas",
            "• 1 segurada para 01/10 (3 pedidos)",
            "• 1 saiu fraca (2 pedidos, 9 caixas)",
            "Veja no Planejamento.",
        ]))

    def test_linha_zerada_e_omitida(self):
        texto = nw.texto_rotas_fracas(self.ALVO, self._resumo(juntadas=1))
        self.assertEqual(texto, "\n".join([
            "⚠️ *Rotas fracas* · rotas de 30/09",
            "• 1 juntada em rota vizinha",
            "Veja no Planejamento.",
        ]))

    def test_cabe_em_200_com_numeros_grandes(self):
        r = self._resumo(juntadas=9999, seguradas=9999, pedidos_segurados=9999,
                         sobraram=9999, pedidos_sobraram=9999, caixas_sobraram=9999)
        self.assertLessEqual(len(nw.texto_rotas_fracas(self.ALVO, r)), nw.MAX_MENSAGEM)

    def test_sem_rota_fraca_nao_envia(self):
        with patch.object(nw, "despachar") as despachar:
            self.assertEqual(nw.avisar_rotas_fracas(self.ALVO, self._resumo(), {}), "nao_relevante")
        despachar.assert_not_called()

    def test_envia_uma_por_data_alvo(self):
        with patch.object(nw, "despachar", return_value="enviado") as despachar:
            saida = nw.avisar_rotas_fracas(self.ALVO, self._resumo(sobraram=1, pedidos_sobraram=2, caixas_sobraram=9),
                                           {"x": 1}, modo_teste=True)
        self.assertEqual(saida, "enviado")
        args, kwargs = despachar.call_args
        self.assertEqual(args[:3], ({"x": 1}, "criar_rotas_diarias", "rotas_fracas"))
        self.assertEqual(args[4], "rotas_fracas:2026-09-30")
        self.assertTrue(kwargs["modo_teste"])

    def test_falha_no_envio_nao_levanta(self):
        with patch.object(nw, "despachar", side_effect=RuntimeError("boom")):
            self.assertEqual(nw.avisar_rotas_fracas(self.ALVO, self._resumo(juntadas=1), {}), "falhou")
```

Conferir os nomes já importados no topo do arquivo de teste:

Run: `grep -n "^import\|^from" test_notificar_whatsapp.py`

Expected: `import notificar_whatsapp as nw` e `from unittest.mock import patch`. Se os nomes forem outros, ajustar os testes aos nomes reais.

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_notificar_whatsapp.TestRotasFracas -v`
Expected: FAIL, `AttributeError: module 'notificar_whatsapp' has no attribute 'texto_rotas_fracas'`.

- [ ] **Step 3: Implementar em `notificar_whatsapp.py`**

Depois da função `texto_nao_expedidos`, acrescentar:

```python
def texto_rotas_fracas(data_alvo, resumo: dict) -> str:
    """Resumo das rotas fracas da rodada (roteirizacao/rotas_fracas.py).
    `resumo` e o dict de criar_rotas_diarias._aplicar_segurar."""
    linhas = [f"⚠️ *Rotas fracas* · rotas de {data_alvo:%d/%m}"]
    if resumo.get("juntadas"):
        linhas.append("• " + _plural(resumo["juntadas"], "juntada em rota vizinha", "juntadas em rotas vizinhas"))
    if resumo.get("seguradas"):
        linhas.append("• " + _plural(resumo["seguradas"], "segurada", "seguradas")
                      + f" para {resumo['data_nova']:%d/%m} ("
                      + _plural(resumo["pedidos_segurados"], "pedido", "pedidos") + ")")
    if resumo.get("sobraram"):
        linhas.append("• " + _plural(resumo["sobraram"], "saiu fraca", "saíram fracas") + " ("
                      + _plural(resumo["pedidos_sobraram"], "pedido", "pedidos") + ", "
                      + _plural(resumo["caixas_sobraram"], "caixa", "caixas") + ")")
    linhas.append("Veja no Planejamento.")
    return "\n".join(linhas)
```

Depois da função `avisar_nao_expedidos`, acrescentar:

```python
def avisar_rotas_fracas(data_alvo, resumo: dict, config: dict, modo_teste: bool = False, **kw) -> str:
    """Uma mensagem por data alvo (a assinatura barra a repeticao se o
    job rodar de novo dentro da janela)."""
    try:
        if not (resumo.get("juntadas") or resumo.get("seguradas") or resumo.get("sobraram")):
            return "nao_relevante"
        return despachar(config, "criar_rotas_diarias", "rotas_fracas", texto_rotas_fracas(data_alvo, resumo),
                         f"rotas_fracas:{data_alvo.isoformat()}", modo_teste=modo_teste, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"
```

- [ ] **Step 4: Chamar no job**

Em `roteirizacao/criar_rotas_diarias.py`, no `main`, logo depois da linha `resumo_fracas = _aplicar_segurar(planos, data_alvo, gmaps_key, modo_teste)`, acrescentar:

```python
        try:
            from notificar_whatsapp import avisar_rotas_fracas
            situacao = avisar_rotas_fracas(data_alvo, resumo_fracas, config, modo_teste=modo_teste)
            logger.info(f"Aviso de rotas fracas no WhatsApp: {situacao}")
        except Exception as e:
            logger.warning(f"Falha ao avisar rotas fracas no WhatsApp (não afeta a criação de rotas): {e}")
```

- [ ] **Step 5: Rodar e ver passar**

```bash
py -3.11 -m unittest test_notificar_whatsapp -v 2>&1 | tail -3
py -3.11 -m unittest roteirizacao.test_rotas_fracas_integracao -v 2>&1 | tail -3
py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste 2>&1 | tail -20
```

Expected: `OK` nos dois; o modo teste termina sem exceção e, havendo rota fraca, registra `Aviso de rotas fracas no WhatsApp: modo_teste` (ou `desligado`, conforme o `config.yaml` local).

- [ ] **Step 6: Commit**

```bash
git add notificar_whatsapp.py test_notificar_whatsapp.py roteirizacao/criar_rotas_diarias.py
git commit -m "WhatsApp: resumo das rotas fracas da rodada no grupo interno, um por data

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Replay, medição e mapa do sistema

**Files:**
- Modify: `roteirizacao/metricas_plano.py`
- Modify: `roteirizacao/replay_rotas.py`
- Modify: `roteirizacao/test_metricas_plano.py`
- Modify: `MAPA_DO_SISTEMA.txt`

**Interfaces:**
- Consumes: `rotas_fracas.ROTAS_FRACAS_ATIVO`, `PARADAS_ROTA_FRACA`, `CAIXAS_ROTA_FRACA`.
- Produces: `metricas_plano(rotas, base, horas=None, teto_horas=9.0, caixas=None, paradas_fraca=7, caixas_fraca=40)` com a chave `rotas_fracas` (`None` quando `caixas` não é informado); opção `--sem-rotas-fracas` no replay.

- [ ] **Step 1: Acrescentar os testes**

Em `roteirizacao/test_metricas_plano.py`, dentro da classe de testes existente, acrescentar:

```python
    def test_rotas_fracas_conta_paradas_e_caixas_juntas(self):
        rotas = [[(0.1, 0.0)], [(0.1, 0.01)], [(0.1, 0.02 + 0.001 * i) for i in range(8)]]
        # 1 parada/9 caixas = fraca; 1 parada/294 caixas = carga cheia; 8 paradas/8 caixas = nao e fraca
        m = mp.metricas_plano(rotas, BASE, caixas=[9, 294, 8])
        self.assertEqual(m["rotas_fracas"], 1)
        self.assertIn("fracas 1", mp.formatar_metricas(m, "t"))

    def test_sem_caixas_nao_mede_rotas_fracas(self):
        m = mp.metricas_plano([[(0.1, 0.0)]], BASE)
        self.assertIsNone(m["rotas_fracas"])
        self.assertNotIn("fracas", mp.formatar_metricas(m, "t"))

    def test_caixas_alinhadas_com_rotas_filtradas(self):
        # a segunda rota ficou sem coordenada e sai da medicao junto com as caixas dela
        m = mp.metricas_plano([[(0.1, 0.0)], []], BASE, caixas=[294, 5])
        self.assertEqual(m["rotas_fracas"], 0)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_metricas_plano -v`
Expected: FAIL, `TypeError: metricas_plano() got an unexpected keyword argument 'caixas'`.

- [ ] **Step 3: Implementar em `roteirizacao/metricas_plano.py`**

Trocar a assinatura:

```python
def metricas_plano(rotas: list[list[tuple[float, float]]], base: tuple[float, float],
                   horas: list[float] | None = None, teto_horas: float = 9.0,
                   caixas: list[int] | None = None, paradas_fraca: int = 7, caixas_fraca: int = 40) -> dict:
```

Acrescentar à docstring: `` `caixas` (opcional): soma de caixas por rota, na mesma ordem de `rotas`, pra contar rotas fracas (ate `paradas_fraca` paradas E ate `caixas_fraca` caixas -- ver rotas_fracas.py). Sem ela, rotas_fracas sai None. ``

Depois do bloco `if horas: horas = [...]`, acrescentar:

```python
    if caixas is not None:
        caixas = [caixas[i] for i in indices_validos]
```

No dict `vazio`, acrescentar `"rotas_fracas": 0 if caixas is not None else None,`.

No dict do `return` final, acrescentar:

```python
        "rotas_fracas": (sum(1 for t, c in zip(tamanhos, caixas) if t <= paradas_fraca and c <= caixas_fraca)
                         if caixas is not None else None),
```

Em `formatar_metricas`, antes do `if m.get("rotas_sem_coordenada", 0) > 0:`, acrescentar:

```python
    if m.get("rotas_fracas") is not None:
        linha += f" | fracas {m['rotas_fracas']}"
```

- [ ] **Step 4: Implementar em `roteirizacao/replay_rotas.py`**

Depois da função `_horas`, acrescentar:

```python
def _caixas(sublotes: list[list[dict]]) -> list[int]:
    return [sum(rd.extrair_volume_caixas(s) for s in sub) for sub in sublotes]
```

Em `rodar_dia`, nas duas chamadas de `mp.metricas_plano`, acrescentar o argumento `caixas=`:

```python
    enviado = mp.metricas_plano(mp.plano_de_sublotes(rotas_enviadas, _coords), coords_base,
                                horas=_horas(rotas_enviadas), teto_horas=rd.ROTA_TEMPO_MAXIMO_HORAS,
                                caixas=_caixas(rotas_enviadas))
```

```python
    novo = mp.metricas_plano(mp.plano_de_sublotes(sublotes, _coords), coords_base,
                             horas=_horas(sublotes), teto_horas=rd.ROTA_TEMPO_MAXIMO_HORAS,
                             caixas=_caixas(sublotes))
```

Em `_somar`, acrescentar `"rotas_fracas"` à tupla de chaves somáveis.

Em `main`, depois de `parser.add_argument("--separar-carga", ...)`, acrescentar:

```python
    parser.add_argument("--sem-rotas-fracas", action="store_true", help="desliga a juncao das rotas fracas")
```

Depois do bloco `if args.separar_carga:`, acrescentar:

```python
    if args.sem_rotas_fracas:
        import rotas_fracas
        rotas_fracas.ROTAS_FRACAS_ATIVO = False
```

No `cabecalho`, acrescentar ao f-string, depois do trecho do `separar_carga`:

```python
                 f" | rotas_fracas={'off' if args.sem_rotas_fracas else 'on'}"
```

Na docstring do módulo, em "COMO USAR", acrescentar a linha:

```
    py -3.11 roteirizacao/replay_rotas.py --de ... --ate ... --sem-rotas-fracas
```

E em "LIMITACOES CONHECIDAS", acrescentar o item 4:

```
  4. O SEGURAR NAO E SIMULADO: o replay so mede a JUNCAO das rotas fracas.
     O historico (rascunhos_parada) nao guarda data de entrada nem
     agendamento do pedido, entao nao da pra saber quem poderia esperar.
```

- [ ] **Step 5: Rodar e ver passar**

```bash
py -3.11 -m unittest roteirizacao.test_metricas_plano roteirizacao.test_replay_rotas -v 2>&1 | tail -3
```

Expected: `OK`.

- [ ] **Step 6: Rodar o replay de 31 dias, com e sem a junção**

Precisa de `dados/dados_replay.db` (gitignored; fica no working tree principal). Se não existir no worktree, passar o caminho com `--banco`:

```bash
py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-19 --banco /c/agente_stokki_eventos/dados/dados_replay.db --sem-rotas-fracas 2>&1 | tail -4
py -3.11 roteirizacao/replay_rotas.py --de 2026-08-12 --ate 2026-09-19 --banco /c/agente_stokki_eventos/dados/dados_replay.db 2>&1 | tail -4
```

Anotar, das linhas `TOTAL novo`, os dois lados: rotas, fracas, km, diâmetro mediano, cruzadas e rotas acima do teto. Critério para seguir sem ajuste: rotas acima do teto continua **0** e o número de rotas fracas cai. Se rotas acima do teto passar de 0, é defeito (a junção não pode ceder nas 9h): parar e investigar antes de qualquer commit. Se o km subir mais de 3%, levar o número ao Hugo antes do deploy; a decisão de mexer na folga é dele.

Se o banco do replay não existir em lugar nenhum, registrar isso e pedir ao Hugo autorização para copiar da VPS (o comando está na docstring de `replay_rotas.py`).

- [ ] **Step 7: Atualizar o `MAPA_DO_SISTEMA.txt`**

Ler as seções que citam a roteirização para copiar o formato das linhas vizinhas:

Run: `grep -n "polimento_rotas\|pedidos_dedicados\|Dia fixo por região" MAPA_DO_SISTEMA.txt`

Acrescentar, no mesmo formato das linhas encontradas:

- no índice "onde fica cada coisa", uma linha `Rota fraca (juntar, segurar, avisar)` apontando para `roteirizacao/rotas_fracas.py, pedidos_segurados.py`;
- na seção `8. roteirizacao/`, as entradas `rotas_fracas.py` ("Rota com até 7 pedidos e até 40 caixas: junta nas vizinhas com folga, segura 1 dia útil (SEGURAR_ATIVO, desligado), ou sai com o motivo no rascunho") e `pedidos_segurados.py` ("Tabela pedidos_segurados; lida pelo planejamento, vigia e incrementar");
- na lista de tabelas, `pedidos_segurados` ao lado de `pedidos_roteirizados`;
- na seção de armadilhas, se existir: "Pedido segurado fica no pool de propósito até a data nova; o vigia só alarma na véspera do prazo de 3 dias úteis."

- [ ] **Step 8: Commit**

```bash
git add roteirizacao/metricas_plano.py roteirizacao/replay_rotas.py roteirizacao/test_metricas_plano.py MAPA_DO_SISTEMA.txt
git commit -m "Replay: mede rotas fracas e compara com e sem a juncao; mapa do sistema atualizado

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Conferência final

**Files:** nenhum arquivo novo.

- [ ] **Step 1: Rodar tudo de novo**

```bash
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -3
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -3
py -3.11 -m unittest vigia.test_regras vigia.test_vigiar test_notificar_whatsapp 2>&1 | tail -3
```

Expected: `OK` nos três, com a contagem da Task 0 mais os testes novos (35 + 8 + 13 + 3 em `roteirizacao`, 7 em `painel_agentes`, 5 no vigia, 6 no WhatsApp).

- [ ] **Step 2: Conferir as chaves**

Run: `grep -n "^ROTAS_FRACAS_ATIVO\|^SEGURAR_ATIVO" roteirizacao/rotas_fracas.py`
Expected: `ROTAS_FRACAS_ATIVO = True` e `SEGURAR_ATIVO = False`.

- [ ] **Step 3: Conferir o que o ramo mexeu**

Run: `git diff --stat master...rotas-fracas`
Expected: só os arquivos do mapa de arquivos deste plano.

- [ ] **Step 4: Entregar ao Hugo**

Relatar: contagem de testes, números do replay (com e sem a junção), e o que falta decidir: merge no master, deploy e a data para ligar `SEGURAR_ATIVO`. Não fazer merge, push nem deploy sem pedido.

Pontos de atenção para o merge:
- `painel_agentes/templates/planejamento_rotas.html` e `MAPA_DO_SISTEMA.txt` têm mudanças não commitadas de outra sessão no working tree principal. Podem conflitar.
- No deploy, o painel precisa ser reiniciado (`painel-agentes`), porque importa `planejamento_rotas` e `rascunhos_rota`. Os jobs de lote pegam o código novo sozinhos.
- A tabela `pedidos_segurados` e a coluna `rota_fraca_motivo` são criadas na primeira execução. Rodar a primeira vez como `www-data`, nunca como root.
