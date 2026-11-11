# Agendamento automático no próximo dia útil — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Todo pedido novo sem agendamento de verdade nasce na Vuupt com data (corte 14h, próximo dia útil) e horário do destinatário; se não sair na data, a data anda sozinha para o próximo dia útil.

**Architecture:** A regra de data/horário é pura (`regras/agendamento_automatico.py`). O pipeline aplica a regra só na **criação** do serviço e registra a data com a origem nova `AUTOMATICO` em `agendamentos_origem` (data do sistema, nunca vira dedicado). Na roteirização, `roteirizacao/datas_automaticas.py` marca os pedidos com data automática (`_data_automatica`), para que o SEGURAR das rotas fracas e a junção de nível 4 tratem esses pedidos como pedidos sem agendamento. O mesmo módulo faz a data andar no começo da rodada das 16h. A elegibilidade não muda: data futura fica fora, e é isso que faz o corte valer.

**Tech Stack:** Python 3.11 (`py -3.11`), unittest (sem pytest), SQLite `dados/dados.db`, API Vuupt via `vuupt_client.VuuptClient`.

**Spec:** `docs/superpowers/specs/2026-11-02-agendamento-automatico-proximo-dia-util-design.md` (aprovada pelo Hugo em 02/11). Leia antes de começar.

## Global Constraints

- Trabalhar num ramo novo `agendamento-automatico` criado de `origin/master` (worktree próprio). A master local está atrasada: não usar. Commits locais só no ramo; push, merge e deploy só com o OK do Hugo.
- Só `git add` dos arquivos da tarefa; nunca `git add -A`. Nunca ler/copiar/commitar `config.yaml`.
- Corte: **14h**. A rodada horária das **14h05** ainda conta como antes do corte → constante `CORTE_RODADA = time(14, 15)` comparada com a hora de **início da rodada** do pipeline.
- Só vale para serviço criado a partir de **03/11/2026** → `A_PARTIR_DE = date(2026, 11, 3)`.
- Sem horário cadastrado = dia todo `("00:00", "23:59")`, nunca 08h–16h.
- Origem nova: `ORIGEM_AUTOMATICO = "AUTOMATICO"`, dentro de `ORIGENS_NAO_CLIENTE`.
- AGENDA (`clientes_agendamento`) e data informada (`buscar_confirmacao`) não mudam em nada.
- A data anda **só** se a origem da data atual for `AUTOMATICO`, o pedido estiver `not_assigned`, fora de rascunho/rota e não pausado. A data anda só na rodada com `--gerar-rascunho` (16h). Quando anda, o horário continua o mesmo.
- `--modo-teste` não grava na Vuupt nem em `agendamentos_origem`.
- Datas da Vuupt chegam em UTC sem fuso: ler sempre com `nucleo.normalizacao.vuupt_para_local` (ou `fora_dia_fixo.data_agendada`).
- Testes: `py -3.11 -m unittest <modulo> -v` da raiz. Nunca misturar `roteirizacao`/`nucleo` com `painel_agentes` no mesmo comando.
- Comentários e logs no padrão do arquivo (pipeline com acento; `roteirizacao/registro_dia_fixo.py` e `reagendar_feriado.py` sem acento).

## Review Focus

1. **Pedido importado depois das 14h** (rodadas das 15h05, 16h05, sequências das 16h/18h/22h) ganha D+2 e **não** entra no rascunho de amanhã → teste de `data_automatica` com início às 16:00 e 22:00 (Task 2) e teste de elegibilidade com a data gerada (Task 5).
2. **Reprocessamento horário de serviço que já existe** (inclusive os do pool de hoje, sem data) não ganha nem muda data → teste `agendamento_automatico` com `servico_existente` devolve `None` (Task 3).
3. **Data automática fora do dia fixo da região não vira dedicado** → teste `fora_dia_fixo.detectar` com linha `AUTOMATICO` devolve `None` (Task 1).
4. **Rota ENVIADA sem motorista de dias anteriores** (continua `not_assigned`) não anda → teste `ids_em_rascunho_recentes` cobre os 6 dias anteriores (Task 4).
5. **Data em UTC sem fuso** (`"2026-11-10 03:00:00"` = 10/11 00:00 local) é lida como 10/11, e o horário 00:00–23:59 se mantém quando a data anda → teste em `andar_datas_automaticas` (Task 4).

---

### Task 1: Origem `AUTOMATICO` no registro

**Files:**
- Modify: `roteirizacao/registro_dia_fixo.py` (constantes, `data_nao_e_do_cliente`, nova `data_e_automatica`)
- Test: `roteirizacao/test_registro_dia_fixo.py`, `roteirizacao/test_fora_dia_fixo.py`

**Interfaces:**
- Produces: `ORIGEM_AUTOMATICO = "AUTOMATICO"`; `ORIGENS_NAO_CLIENTE = (ORIGEM_DIA_FIXO, ORIGEM_EQUIPE, ORIGEM_AUTOMATICO)`; `data_e_automatica(conn: sqlite3.Connection, servico: dict, data: date) -> bool`.

- [ ] **Step 1: Testes que falham** em `test_registro_dia_fixo.TestRegistro`:

```python
def test_data_automatica_nao_e_do_cliente(self):
    s = {"id": 7, "code": "#PS-1001"}
    reg.registrar_origem(self.conn, s, QUINTA, reg.ORIGEM_AUTOMATICO)
    self.assertTrue(reg.data_nao_e_do_cliente(self.conn, s, QUINTA))
    self.assertTrue(reg.data_e_automatica(self.conn, s, QUINTA))
    self.assertFalse(reg.data_e_automatica(self.conn, s, QUINTA + timedelta(days=1)))

def test_dia_fixo_e_equipe_nao_sao_automaticas(self):
    s = {"id": 8, "code": "#PS-1002"}
    reg.registrar_origem(self.conn, s, QUINTA, reg.ORIGEM_DIA_FIXO)
    reg.registrar_origem(self.conn, s, QUINTA, reg.ORIGEM_EQUIPE)
    self.assertFalse(reg.data_e_automatica(self.conn, s, QUINTA))
    self.assertFalse(reg.data_e_automatica(self.conn, {}, QUINTA))
```

Em `test_fora_dia_fixo.py`, ao lado do teste que já registra `ORIGEM_DIA_FIXO` (linha ~77), crie um teste igual com `reg.ORIGEM_AUTOMATICO` e confira que `detectar(...)` devolve `None`.

- [ ] **Step 2: Rodar e ver falhar**
Run: `py -3.11 -m unittest roteirizacao.test_registro_dia_fixo roteirizacao.test_fora_dia_fixo -v`
Expected: FAIL com `AttributeError: ... 'ORIGEM_AUTOMATICO'`

- [ ] **Step 3: Implementar.** Acrescentar a constante e a tupla. Em `data_nao_e_do_cliente`, trocar o `origem IN (?, ?)` fixo por placeholders gerados de `ORIGENS_NAO_CLIENTE`. Extrair um `_tem_origem(conn, servico, data, origens) -> bool` usado pelas duas funções, com a mesma regra de código ou `service_id`. Atualizar a docstring do módulo: AUTOMATICO = data gravada pelo pipeline / data que andou.

- [ ] **Step 4: Rodar e ver passar** (mesmo comando, todos OK).

- [ ] **Step 5: Commit**
```bash
git add roteirizacao/registro_dia_fixo.py roteirizacao/test_registro_dia_fixo.py roteirizacao/test_fora_dia_fixo.py
git commit -m "Agendamento automatico: origem AUTOMATICO no registro de datas (nao vira dedicado)"
```

---

### Task 2: Regra pura de data e horário

**Files:**
- Create: `regras/agendamento_automatico.py`
- Test: `regras/test_agendamento_automatico.py`

**Interfaces:**
- Produces: `CORTE_RODADA = time(14, 15)`, `A_PARTIR_DE = date(2026, 11, 3)`, `DIA_TODO = ("00:00", "23:59")`;
  `data_automatica(inicio_rodada: datetime, data_saida: str = "") -> date`;
  `janela_automatica(horario_mensagem: dict | None, documento: str, mapa_horarios: dict, ajustes_manuais: dict) -> tuple[str, str]`.

Algoritmo de `data_automatica`:
```python
hoje = inicio_rodada.date()
d1 = feriados.proximo_dia_util(hoje)
data = d1 if feriados.eh_dia_util(hoje) and inicio_rodada.time() <= CORTE_RODADA else feriados.proximo_dia_util(d1)
saida = <data_saida em "%d/%m/%Y" ou "%Y-%m-%d"; inválida/vazia = None>
if saida and saida > data:
    data = feriados.rolar_para_dia_util(saida)
return data
```
`janela_automatica`: `horario_mensagem` com `inicio` e `fim` preenchidos tem prioridade. Sem ela, vale `complexidade_entrega.horario_efetivo(documento, mapa_horarios, ajustes_manuais)` (já devolve `DIA_TODO` quando não há nada).

- [ ] **Step 1: Testes que falham**

```python
from datetime import date, datetime
from regras import agendamento_automatico as aa

def dt(d, h, m=0): return datetime(2026, 11, d, h, m)

class TestData(unittest.TestCase):
    def test_corte(self):
        self.assertEqual(aa.data_automatica(dt(9, 13, 59)), date(2026, 11, 10))  # seg antes -> ter
        self.assertEqual(aa.data_automatica(dt(9, 14, 10)), date(2026, 11, 10))  # rodada 14h05 conta antes
        self.assertEqual(aa.data_automatica(dt(9, 15, 5)), date(2026, 11, 11))   # seg depois -> qua
        self.assertEqual(aa.data_automatica(dt(9, 16, 0)), date(2026, 11, 11))   # sequencia 16h
        self.assertEqual(aa.data_automatica(dt(9, 22, 0)), date(2026, 11, 11))   # sequencia 22h
    def test_fim_de_semana_e_sexta(self):
        self.assertEqual(aa.data_automatica(dt(7, 10)), date(2026, 11, 10))      # sab -> ter
        self.assertEqual(aa.data_automatica(dt(13, 15)), date(2026, 11, 17))     # sex depois -> ter
    def test_vespera_de_feriado(self):  # 20/11 sexta = Consciencia Negra
        self.assertEqual(aa.data_automatica(dt(19, 10)), date(2026, 11, 23))
        self.assertEqual(aa.data_automatica(dt(19, 15)), date(2026, 11, 24))
    def test_data_de_saida(self):
        self.assertEqual(aa.data_automatica(dt(9, 10), "12/11/2026"), date(2026, 11, 12))  # posterior vence
        self.assertEqual(aa.data_automatica(dt(9, 10), "14/11/2026"), date(2026, 11, 16))  # sab rola p/ seg
        self.assertEqual(aa.data_automatica(dt(9, 10), "09/11/2026"), date(2026, 11, 10))  # anterior ignora
        self.assertEqual(aa.data_automatica(dt(9, 10), "lixo"), date(2026, 11, 10))

class TestJanela(unittest.TestCase):
    def test_prioridades(self):
        self.assertEqual(aa.janela_automatica({"inicio": "09:00", "fim": "12:00"}, "123", {"123": ("07:00", "10:00")}, {}), ("09:00", "12:00"))
        self.assertEqual(aa.janela_automatica(None, "12.3", {"123": ("07:00", "10:00")}, {}), ("07:00", "10:00"))
        self.assertEqual(aa.janela_automatica({"inicio": "", "fim": ""}, "999", {}, {}), aa.DIA_TODO)
```

- [ ] **Step 2: Rodar e ver falhar**
Run: `py -3.11 -m unittest regras.test_agendamento_automatico -v`
Expected: FAIL com `ModuleNotFoundError`

- [ ] **Step 3: Implementar** o módulo (docstring curta citando a spec e a decisão de 02/11, opção A).

- [ ] **Step 4: Rodar e ver passar.**

- [ ] **Step 5: Commit**
```bash
git add regras/agendamento_automatico.py regras/test_agendamento_automatico.py
git commit -m "Agendamento automatico: regra de data (corte 14h, dia util) e horario do destinatario"
```

---

### Task 3: Pipeline grava a data na criação

**Files:**
- Modify: `pipeline.py`: imports; nova `agendamento_automatico` e `registrar_data_automatica`; `processar_pedido` (bloco de agendamento ~linha 764–815 e criação ~linha 900); `main` (carregar horários/ajustes, `inicio_rodada`, as 2 chamadas de `processar_pedido` ~1227 e ~1380)
- Test: `test_pipeline_agendamento_automatico.py` (raiz)

**Interfaces:**
- Consumes: Task 1 (`registro_dia_fixo.ORIGEM_AUTOMATICO`, `registrar_origens`), Task 2 (`data_automatica`, `janela_automatica`, `A_PARTIR_DE`).
- Produces: `FONTE_PROXIMO_DIA_UTIL = "proximo_dia_util"`;
  `agendamento_automatico(servico_existente: dict | None, horario_mensagem: dict | None, documento: str, data_saida: str, inicio_rodada: datetime | None, mapa_horarios: dict, ajustes_manuais: dict) -> tuple[str, str] | None` (ISO via `_converter_data_para_iso`; `None` se `servico_existente` não for vazio, se `inicio_rodada` for None ou se `inicio_rodada.date() < A_PARTIR_DE`);
  `registrar_data_automatica(servico: dict, scheduled_start: str, db_path=None) -> int`.
  `processar_pedido` ganha os kwargs `mapa_horarios: dict | None = None, ajustes_manuais: dict | None = None, inicio_rodada: datetime | None = None` (sem eles nada muda, então os testes e chamadas antigas continuam valendo).

- [ ] **Step 1: Testes que falham**

```python
class TestAgendamentoAutomatico(unittest.TestCase):
    INICIO = datetime(2026, 11, 9, 15, 5)
    def test_servico_novo_ganha_data_e_horario(self):
        r = pipeline.agendamento_automatico(None, None, "123", "", self.INICIO, {"123": ("07:00", "10:00")}, {})
        self.assertEqual(r, (pipeline._converter_data_para_iso("11/11/2026 07:00"),
                             pipeline._converter_data_para_iso("11/11/2026 10:00")))
    def test_servico_que_ja_existe_nao_ganha_nem_muda(self):
        for existente in ({"id": 1, "scheduled_start": None}, {"id": 1, "scheduled_start": "2026-11-10 03:00:00"}):
            self.assertIsNone(pipeline.agendamento_automatico(existente, None, "123", "", self.INICIO, {}, {}))
    def test_antes_de_03_11_ou_sem_inicio_nao_age(self):
        self.assertIsNone(pipeline.agendamento_automatico(None, None, "1", "", datetime(2026, 11, 2, 22), {}, {}))
        self.assertIsNone(pipeline.agendamento_automatico(None, None, "1", "", None, {}, {}))
    def test_registra_origem_automatica(self):
        # banco temporario (tempfile), servico {"id": 9, "code": "#PS-1"}, scheduled_start "2026-11-11T07:00:00-03:00"
        # -> registrar_data_automatica(...) == 1 e data_e_automatica(conn, servico, date(2026, 11, 11)) is True
```

- [ ] **Step 2: Rodar e ver falhar**
Run: `py -3.11 -m unittest test_pipeline_agendamento_automatico -v`
Expected: FAIL com `AttributeError: ... 'agendamento_automatico'`

- [ ] **Step 3: Implementar as duas funções.** `registrar_data_automatica` importa `registro_dia_fixo` sob demanda, como `regioes_dia_fixo` já faz (`from roteirizacao import registro_dia_fixo`), e chama `registrar_origens([{"servico": servico, "data": date.fromisoformat(scheduled_start[:10])}], ORIGEM_AUTOMATICO, db_path or registro_dia_fixo.DB_PATH)`. Essa chamada é tolerante: se falhar, só registra no log.

- [ ] **Step 4: Ligar em `processar_pedido`:**
  - Depois do `elif tem_agendamento(...)`, acrescentar um `else:` que chama `agendamento_automatico(servico_existente, res_end.horario_entrega, cnpj_destino_bruto, data_saida, inicio_rodada, mapa_horarios or {}, ajustes_manuais or {})`. Se vier um par, preencher `scheduled_*_final` e `fonte_agendamento = FONTE_PROXIMO_DIA_UTIL`.
  - Trocar `resultado["tem_agendamento"]` para `fonte_agendamento not in ("nao_exigido", FONTE_PROXIMO_DIA_UTIL)`. Data automática não é agendamento do cliente.
  - O ajuste de dia fixo que já existe vale também para essa data. Nada muda nele.
  - Depois de `criar_ou_atualizar_servico`: se `acao == "criado"`, a fonte for `FONTE_PROXIMO_DIA_UTIL` e houver `resultado["vuupt_service_id"]`, chamar `registrar_data_automatica(servico, payload["scheduled_start"])`. Usar a data **final**, depois do dia fixo. Registrar no log `"{codigo}: agendado automaticamente pra dd/mm (hh:mm-hh:mm)"`. Em modo teste, só o mesmo log com `[TESTE]`.
- [ ] **Step 5: Ligar em `main`:**
  - Ao lado de `carregar_niveis`: `mapa_horarios = carregar_horarios(caminho_niveis)` e `ajustes_manuais = carregar_ajustes_manuais()`.
  - `inicio_rodada = datetime.now(timezone(timedelta(hours=-3)))` no começo de `main`.
  - Passar os três nas duas chamadas de `processar_pedido`: a do lote e a da fila do freshhub.

- [ ] **Step 6: Rodar** `py -3.11 -m unittest test_pipeline_agendamento_automatico test_pipeline_fila_freshhub -v`, tudo OK, e `py -3.11 -m py_compile pipeline.py`.

- [ ] **Step 7: Commit**
```bash
git add pipeline.py test_pipeline_agendamento_automatico.py
git commit -m "Pipeline: pedido novo sem agendamento nasce com data do proximo dia util (corte 14h) e horario do destinatario"
```

---

### Task 4: Marcar e andar as datas automáticas

**Files:**
- Create: `roteirizacao/datas_automaticas.py`
- Test: `roteirizacao/test_datas_automaticas.py`

**Interfaces:**
- Consumes: Task 1 (`registro_dia_fixo.data_e_automatica`, `registrar_origens`, `ORIGEM_AUTOMATICO`); `fora_dia_fixo.data_agendada`; `regioes_dia_fixo.ajustar_data_por_dia_fixo`; `nucleo.normalizacao.vuupt_para_local`.
- Produces:
  - `marcar_datas_automaticas(servicos: list[dict], db_path=None) -> int`: põe `s["_data_automatica"] = True` em quem tem data cuja origem é AUTOMATICO e devolve quantos marcou. É tolerante: se falhar, registra no log e devolve 0.
  - `ids_em_rascunho_recentes(hoje: date, data_alvo: date, listar=None) -> set[int]`: junta os `service_id` das paradas de `listar(d)` para `d` de `hoje - 6 dias` até `hoje`, mais `data_alvo`. Por padrão, `listar` é `rascunhos_rota.listar_rascunhos_do_dia`, importado sob demanda.
  - `andar_datas_automaticas(servicos: list[dict], vuupt, data_alvo: date, ids_em_rascunho: set[int], modo_teste: bool, db_path=None) -> list[dict]`: recebe só os que estão marcados e fora de `ids_em_rascunho`, com `data_agendada(s) < data_alvo`. A nova data é `ajustar_data_por_dia_fixo(s, data_alvo)[0]`. O horário `hh:mm` de início e fim é o atual, lido em hora local. Grava com `vuupt.atualizar_servico(id, {"scheduled_start": f"{d}T{hh:mm}:00-03:00", "scheduled_end": ...})`, atualiza o dict em memória e registra AUTOMATICO para a nova data. Devolve `[{"servico", "de", "para"}]`. Em `modo_teste`, devolve a mesma lista sem gravar nada. Se um pedido falhar na Vuupt, registra no log e segue para os outros.

- [ ] **Step 1: Testes que falham** (banco temporário e `FakeVuupt`, como em `test_registro_dia_fixo`):

```python
TER, QUA = date(2026, 11, 10), date(2026, 11, 11)
def svc(sid, start="2026-11-10 03:00:00", end="2026-11-11 02:59:00"):  # UTC = 10/11 00:00-23:59 local
    return {"id": sid, "code": f"#PS-{sid}", "address": "Rua A 1, Centro, Sao Paulo - SP, 01001-000, Brasil",
            "scheduled_start": start, "scheduled_end": end}

def test_marca_so_data_automatica(self):
    a, b, c = svc(1), svc(2), {"id": 3, "code": "#PS-3"}
    reg.registrar_origem(self.conn, a, TER, reg.ORIGEM_AUTOMATICO)
    reg.registrar_origem(self.conn, b, TER, reg.ORIGEM_EQUIPE)
    self.assertEqual(da.marcar_datas_automaticas([a, b, c], self.db), 1)
    self.assertTrue(a.get("_data_automatica")); self.assertFalse(b.get("_data_automatica")); self.assertFalse(c.get("_data_automatica"))

def test_anda_mantendo_o_horario(self):
    a = svc(1); reg.registrar_origem(self.conn, a, TER, reg.ORIGEM_AUTOMATICO)
    da.marcar_datas_automaticas([a], self.db)
    feitos = da.andar_datas_automaticas([a], self.vuupt, QUA, set(), False, self.db)
    self.assertEqual([(f["de"], f["para"]) for f in feitos], [(TER, QUA)])
    self.assertEqual(self.vuupt.chamadas, [(1, {"scheduled_start": "2026-11-11T00:00:00-03:00",
                                                "scheduled_end": "2026-11-11T23:59:00-03:00"})])
    self.assertTrue(reg.data_e_automatica(self.conn, a, QUA))

def test_nao_anda(self):
    # em rascunho (id em ids_em_rascunho); data == data_alvo; data de cliente (sem linha); modo_teste (sem chamadas e sem registro)

def test_regiao_de_dia_fixo_vai_pro_dia_de_visita(self):
    # endereco de Sorocaba (SOROCABA de test_registro_dia_fixo): data automatica TER 10/11, data_alvo QUA 11/11
    # -> para == rdf.proxima_data_valida(rdf.regra_dia_fixo_do_servico(s), QUA)  (nao fixar o dia da semana no teste)

def test_ids_em_rascunho_recentes(self):
    vistos = []
    def listar(d):
        vistos.append(d); return [{"paradas": [{"service_id": d.day}]}]
    ids = da.ids_em_rascunho_recentes(date(2026, 11, 10), QUA, listar)
    self.assertEqual(ids, {4, 5, 6, 7, 8, 9, 10, 11})
```

- [ ] **Step 2: Rodar e ver falhar**
Run: `py -3.11 -m unittest roteirizacao.test_datas_automaticas -v`
Expected: FAIL com `ModuleNotFoundError`

- [ ] **Step 3: Implementar** o módulo, com o mesmo cabeçalho de `sys.path` de `fora_dia_fixo.py`.

- [ ] **Step 4: Rodar e ver passar.**

- [ ] **Step 5: Commit**
```bash
git add roteirizacao/datas_automaticas.py roteirizacao/test_datas_automaticas.py
git commit -m "Roteirizacao: marcar datas automaticas e andar pro proximo dia util quem nao saiu"
```

---

### Task 5: Roteirização trata a data automática como "sem agendamento do cliente"

**Files:**
- Modify: `roteirizacao/rotas_fracas.py` (`motivo_nao_segurar`, linha ~113)
- Modify: `roteirizacao/roteirizacao_dados.py` (`_chave_nivel4`, linha ~250, e a docstring)
- Modify: `roteirizacao/criar_rotas_diarias.py` (`main`, logo depois de `separar_pausados`, ~linha 877)
- Modify: `roteirizacao/incrementar_rotas.py` (`main`, logo depois de `separar_pausados`, ~linha 465)
- Modify: `painel_agentes/planejamento_rotas.py` (`roteirizar_selecionados`, logo depois de `separar_pausados`, ~linha 1243)
- Test: `roteirizacao/test_datas_automaticas.py` (casos novos)

**Interfaces:**
- Consumes: Task 4 (`marcar_datas_automaticas`, `andar_datas_automaticas`, `ids_em_rascunho_recentes`); `nucleo.sincronizar_servicos_vuupt.ressincronizar_ids`.

- [ ] **Step 1: Testes que falham**

```python
def test_data_automatica_pode_ser_segurada(self):
    # recreated_order_origin_id faz a funcao parar no motivo seguinte, sem geocodificar
    s = {"scheduled_start": "2026-11-10 03:00:00", "_data_automatica": True, "code": "#PS-1",
         "recreated_order_origin_id": 5}
    self.assertEqual(rotas_fracas.motivo_nao_segurar(s, QUA, set()), "é reentrega")
    s.pop("_data_automatica")
    self.assertEqual(rotas_fracas.motivo_nao_segurar(s, QUA, set()), "tem agendamento")

def test_nivel4_junta_data_automatica(self):
    a = {"address": "X", "sender_id": 1, "scheduled_start": "2026-11-10T00:00:00-03:00", "_data_automatica": True}
    b = {"address": "X", "sender_id": 1, "scheduled_start": "2026-11-09T00:00:00-03:00", "_data_automatica": True}
    c = {"address": "X", "sender_id": 1}
    self.assertEqual(rd._chave_nivel4(a), rd._chave_nivel4(b)); self.assertEqual(rd._chave_nivel4(a), rd._chave_nivel4(c))

def test_data_gerada_depois_do_corte_fica_fora_do_rascunho_de_amanha(self):
    # Review Focus 1: data_automatica(seg 09/11 16:00) = 11/11 -> elegivel_para_data(servico, date(2026,11,10)) is False
```

- [ ] **Step 2: Rodar e ver falhar** (`py -3.11 -m unittest roteirizacao.test_datas_automaticas -v`).

- [ ] **Step 3: Implementar:**
  - `motivo_nao_segurar`: `if servico.get("scheduled_start") and not servico.get("_data_automatica")`.
  - `_chave_nivel4`: o dia é `None` quando `servico.get("_data_automatica")`.
  - `criar_rotas_diarias.main`: logo depois de `separar_pausados`, num `try/except` que só registra erro no log:
    - chamar `marcar_datas_automaticas(servicos_brutos)`;
    - se `gerar_rascunho`, chamar `andar_datas_automaticas(servicos_brutos, vuupt, data_alvo, ids_em_rascunho_recentes(agora_brasilia.date(), data_alvo), modo_teste)`;
    - se andou algum e não é modo teste, chamar `ressincronizar_ids(vuupt, [ids])`;
    - registrar no log quantos andaram.
    - Tudo isso vem **antes** de `aplicar_regioes_dia_fixo` e de `tratar_fora_dia_fixo`.
  - `incrementar_rotas.main` e `planejamento_rotas.roteirizar_selecionados`: só `marcar_datas_automaticas(...)` logo depois de `separar_pausados`.

- [ ] **Step 4: Rodar a bateria da roteirização** (cada comando separado):
  - `py -3.11 -m unittest roteirizacao.test_datas_automaticas roteirizacao.test_rotas_fracas roteirizacao.test_rotas_fracas_integracao roteirizacao.test_nivel4_veiculo_grande roteirizacao.test_pausados_criar_rotas roteirizacao.test_incrementar_rotas roteirizacao.test_fora_dia_fixo_integracao -v`
  - De dentro de `painel_agentes/`: `py -3.11 -m unittest test_pausados_planejamento test_agendamento_equipe -v`
  - Esperado: tudo OK.

- [ ] **Step 5: Commit**
```bash
git add roteirizacao/rotas_fracas.py roteirizacao/roteirizacao_dados.py roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py painel_agentes/planejamento_rotas.py roteirizacao/test_datas_automaticas.py
git commit -m "Roteirizacao: data automatica nao trava SEGURAR nem juncao de nivel 4; rodada das 16h anda a data"
```

---

### Task 6: Feriado preserva a origem automática + mapa

**Files:**
- Modify: `reagendar_feriado.py` (`aplicar`)
- Modify: `MAPA_DO_SISTEMA.txt`
- Test: `test_reagendar_feriado.py`

**Interfaces:**
- Consumes: Task 1 (`data_e_automatica`, `ORIGEM_AUTOMATICO`).

- [ ] **Step 1: Teste que falha:** um serviço com linha AUTOMATICO no feriado é reagendado (`FakeVuupt` e banco temporário, como no teste que já existe na linha ~62) e a nova data fica `data_e_automatica(...) is True`. Um serviço sem linha continua `DIA_FIXO`.
- [ ] **Step 2: Rodar e ver falhar:** `py -3.11 -m unittest test_reagendar_feriado -v`.
- [ ] **Step 3: Implementar:** em `aplicar`, separar `feitos` em automáticos (a data `i["de"]` tem linha AUTOMATICO) e os demais. Registrar cada grupo com a sua origem.
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: MAPA_DO_SISTEMA.txt:**
  - acrescentar `regras/agendamento_automatico.py` e `roteirizacao/datas_automaticas.py`;
  - na linha do pipeline: "pedido novo sem agendamento nasce com data (corte 14h)";
  - na sequência das 16h: "anda a data automática de quem não saiu";
  - na tabela `agendamentos_origem`: a origem nova AUTOMATICO.
- [ ] **Step 6: Commit**
```bash
git add reagendar_feriado.py test_reagendar_feriado.py MAPA_DO_SISTEMA.txt
git commit -m "Agendamento automatico: feriado preserva a origem AUTOMATICO; mapa do sistema"
```

---

### Task 7: Verificação final e entrega (deploy só com OK do Hugo)

- [ ] **Step 1:** Fazer `py -3.11 -m py_compile` em todos os arquivos tocados e rodar de novo as baterias das Tasks 1–6. Tudo deve sair OK.
- [ ] **Step 2:** Fazer `git merge origin/master` no ramo, se o master andou, e rodar as baterias de novo.
- [ ] **Step 3:** Parar e pedir ao Hugo o OK de push, merge e deploy. Nenhum módulo exposto à internet foi tocado (`portal_cliente/`, `nucleo/api_motorista.py` etc.), então não há review de segurança obrigatória.
- [ ] **Step 4 (depois do OK):** `py -3.11 .claude/scripts/vps_ops.py deploy --restart painel-agentes`. O painel importa `planejamento_rotas`, `roteirizacao_dados` e `rotas_fracas`. Pipeline e rotas são lote e não precisam de restart. Não há unit nova.
- [ ] **Step 5: Provar na VPS:**
  - `vps_ler.py log painel-agentes -n 40 --grep Traceback` depois de abrir o Planejamento: nada novo.
  - Na primeira rodada horária de 03/11, no log do `stokki-pipeline-horario`, aparece "agendado automaticamente".
  - `vps_ler.py sql "SELECT origem, data, count(*) FROM agendamentos_origem WHERE origem='AUTOMATICO' GROUP BY 1,2"` mostra as linhas.
  - Um serviço criado conferido na Vuupt com a data e o horário certos.
  - Na rodada das 16h, o log mostra quantas datas andaram. Usar a skill `conferir-rodada`.
