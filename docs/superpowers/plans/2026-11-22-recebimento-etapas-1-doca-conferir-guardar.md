# Recebimento por etapas, plano 1: doca, Conferir Recebimento e Guardar

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No `/wms`, a conferência de um recebimento deixa de pedir posição (o saldo entra numa posição DOCA) e uma fila nova "Guardar" leva o produto da doca para a posição, com sugestão de lugar e com a reserva dos pedidos acompanhando.

**Architecture:** Um módulo de dados novo, `painel_agentes/wms_doca.py` (sem Flask), concentra doca, temperatura do produto, sugestão, fila e guarda. As rotas ficam em `painel_agentes.py` junto das outras `/api/wms/*`, e as telas no SPA `templates/wms.html`. Tudo é ligado por embarcador (`etapas: true` na lista `wms.pilotos`); embarcador fora da lista recebe exatamente como hoje.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db`), JS puro no `wms.html`, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-22-recebimento-duas-etapas-design.md`. Este plano cobre: Posição DOCA, Etapa 2 (Conferir Recebimento), "Guardar com pedido reservado na doca", Etapa 3 (Guardar), Sugestão de posição, Temperatura do produto (só o tipo; a faixa em °C é do plano 2) e Entrada no ar. A Etapa 1 (Receber: checklist, liberação do cliente, e-mail, PDF) é o **plano 2** (`2026-11-22-recebimento-etapas-2-receber-checklist.md`) e depende deste.

## Global Constraints

- Idioma: português em código, comentários, mensagens e commits. Comentário e string de log sem acento quando o arquivo já segue esse padrão (`wms_caixas.py`, `wms_separacao.py` seguem; `wms.py` usa acento).
- Python: sempre `py -3.11`. Testes de `painel_agentes` rodam **de dentro de `painel_agentes/`**: `py -3.11 -m unittest test_wms_doca -v`. Não há pytest.
- Nunca ler, editar nem commitar `config.yaml`. Testes passam o config como dict.
- Sem `etapas: true` em nenhum piloto, nada muda: nenhuma tela nova aparece, nenhuma rota existente muda de resposta, a área DOCA não é criada.
- `dados.db` é compartilhado com o painel inteiro: transação curta, commit por operação, nunca um lote grande num commit só.
- Todo caminho que mexe em saldo passa por `wms.registrar_movimento` (que faz o próprio commit; nunca chamar com transação aberta).
- Código da doca: área `DOC1`, tipo `DOCA`, posição única `DOC1-P1`. Na tela aparece sempre como `DOCA`.
- A doca atende pedido: reserva, separação e escolha de lote tratam `DOC1-P1` como posição comum. Não adicionar filtro que a esconda.
- Mover entre posições que não a doca continua recusando caixa reservada, como hoje.
- Commits locais por tarefa, no ramo da feature, com mensagem `WMS: ...`. Sem push, sem merge e sem deploy: isso é decisão do Hugo. Só `git add` dos arquivos da tarefa.
- Painel de teste local sempre em outra porta que não a 8070.
- Criou arquivo ou rota: atualizar `MAPA_DO_SISTEMA.txt` na mesma tarefa.

## Review Focus

1. **Resposta perdida na guarda**: o celular repete o POST com o mesmo `uuid`. Esperado: nada é movido duas vezes e as reservas terminam no destino. (Task 5)
2. **Dois aparelhos guardando o mesmo item**: o segundo pede mais do que sobrou na doca. Esperado: erro claro, saldo nunca negativo. (Task 5)
3. **Embarcador tirado da lista com saldo na doca**: esperado que a fila Guardar continue mostrando o item até zerar. (Task 6)
4. **Lote vazio e validade nula**: produto sem lote e sem validade. Esperado: fila, sugestão e guarda funcionam (a chave usa `''`, não `None`). (Tasks 5 e 6)
5. **Caixa aberta ou com unidade já separada na doca**: a aberta é guardada com a quantidade atual; a que tem unidade separada para pedido não é movida e volta na resposta com o motivo. (Task 5)

---

## Estrutura de arquivos

| Arquivo | Papel |
|---|---|
| `painel_agentes/wms_pilotos.py` (modificar) | chave `etapas` por piloto, `ids_com_etapas` |
| `painel_agentes/wms.py` (modificar) | tipo de área `DOCA`; colunas de temperatura do produto |
| `painel_agentes/wms_doca.py` (criar) | doca, temperatura do produto, sugestão, fila, guarda |
| `painel_agentes/wms_caixas.py` (modificar) | `mover_caixa(..., levar_reserva=False)` |
| `painel_agentes/painel_agentes.py` (modificar) | rotas `/api/wms/guarda/*`, bootstrap, `usa_etapas`, temperatura do produto |
| `painel_agentes/templates/wms.html` (modificar) | Conferir Recebimento sem posição; telas Guardar |
| `painel_agentes/test_wms_doca.py` (criar) | testes de dados |
| `painel_agentes/test_wms_doca_tela.py` (criar) | testes de rota |
| `MAPA_DO_SISTEMA.txt` (modificar) | registro do módulo, rotas e chave |

Os testes novos montam o banco como os vizinhos já fazem no `setUp`:
- Dados: `test_wms_caixas.py`, classe `BaseCaixas` (l.23–36): `TemporaryDirectory`, `wms_caixas.conectar(tmp/"dados.db")`, `INSERT` direto em `wms_produtos`, helper `n(tabela)`.
- Rotas: `test_wms_caixas_tela.py`, classe `Base` (l.31–59): `mock.patch.object(wms, "DB_PATH", ...)`, `mock.patch.object(painel_agentes, "_carregar_config", return_value=CFG)`, app importado de `test_fila_etiquetas_tela`, sessão com `nivel_acesso` e helper `operador()`, POST com `headers={"Origin": "http://localhost"}`. Para ligar as etapas no teste: `CFG["wms"]["pilotos"] = [{"id": <embarcador>, "nome": "X", "caixas": True, "etapas": True}]`.

Se um teste de dados existente só rodar da raiz (`py -3.11 -m unittest painel_agentes.test_wms_caixas`), use nos novos o mesmo estilo de import do arquivo vizinho.

---

### Task 1: Chave `etapas` por embarcador

**Files:**
- Modify: `painel_agentes/wms_pilotos.py`
- Test: `painel_agentes/test_wms_pilotos.py`

**Interfaces:**
- Produces: cada item de `wms_pilotos.pilotos(config)` ganha `"etapas": bool`; `wms_pilotos.ids_com_etapas(config: dict) -> set[int]`.

- [ ] **Step 1: Testes que falham** em `test_wms_pilotos.py`

```python
def test_etapas_por_piloto(self):
    cfg = {"wms": {"pilotos": [{"id": 48, "nome": "A", "caixas": False, "etapas": True},
                               {"id": 22, "nome": "B", "caixas": True}]}}
    self.assertEqual(wms_pilotos.ids_com_etapas(cfg), {48})
    self.assertEqual([p["etapas"] for p in wms_pilotos.pilotos(cfg)], [True, False])

def test_etapas_desligado_sem_lista(self):
    self.assertEqual(wms_pilotos.ids_com_etapas({"wms": {"embarcador_piloto_id": 48, "caixas_ativo": True}}), set())
    self.assertEqual(wms_pilotos.ids_com_etapas({}), set())
```

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest test_wms_pilotos -v` → FAIL (`ids_com_etapas` não existe).
- [ ] **Step 3: Implementar** `etapas` em `pilotos()` (mesma leitura tolerante de `caixas`, via `_verdadeiro`; nas chaves antigas, sem lista, sempre `False`) e `ids_com_etapas`. Atualizar o docstring do módulo com o exemplo `etapas: true`.
- [ ] **Step 4: Rodar e ver passar** — mesmo comando → PASS, incluindo os testes antigos.
- [ ] **Step 5: Commit** — `git add painel_agentes/wms_pilotos.py painel_agentes/test_wms_pilotos.py` · `WMS: chave etapas por embarcador piloto`

---

### Task 2: Posição DOCA

**Files:**
- Modify: `painel_agentes/wms.py` (`TIPOS_AREA`, linha ~39)
- Create: `painel_agentes/wms_doca.py`
- Test: `painel_agentes/test_wms_doca.py`

**Interfaces:**
- Produces: `wms.TIPOS_AREA["DOCA"] = ("Doca", "AMBIENTE")`; `wms_doca.DOCA_AREA = "DOC1"`; `wms_doca.DOCA_POSICAO = "DOC1-P1"`; `wms_doca.garantir_doca(conn) -> str` (devolve `DOCA_POSICAO`); `wms_doca.e_doca(codigo: str | None) -> bool`; `wms_doca.conectar(caminho=None)` no mesmo molde de `wms_caixas.conectar`.

- [ ] **Step 1: Testes que falham**

```python
def test_garantir_doca_cria_uma_vez(self):
    self.assertEqual(wms_doca.garantir_doca(self.conn), "DOC1-P1")
    self.assertEqual(wms_doca.garantir_doca(self.conn), "DOC1-P1")
    self.assertEqual(wms.obter_area(self.conn, "DOC1")["tipo"], "DOCA")
    self.assertEqual(len(wms.listar_posicoes(self.conn, area="DOC1")), 1)

def test_e_doca(self):
    self.assertTrue(wms_doca.e_doca("doc1-p1"))
    self.assertFalse(wms_doca.e_doca("C1-E1-N1"))
    self.assertFalse(wms_doca.e_doca(None))

def test_saldo_na_doca_atende_pedido(self):
    # decisao do Hugo 22/11: a doca vale como posicao comum na reserva
    wms_doca.garantir_doca(self.conn)
    wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=10,
                            lote="L1", validade="2027-01-01", destino="DOC1-P1")
    lotes = wms_pedidos.disponivel_por_lote(self.conn, self.pid)
    self.assertEqual([(l["posicao"], l["disponivel"]) for l in lotes], [("DOC1-P1", 10.0)])
```

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest test_wms_doca -v` → FAIL (módulo não existe).
- [ ] **Step 3: Implementar** o tipo de área e o módulo. `garantir_doca` usa `wms.criar_area` e `wms.criar_posicao`, criando só o que falta; se a área existir desativada, reativa.
- [ ] **Step 4: Rodar e ver passar** — PASS. Rodar também `py -3.11 -m unittest test_wms_telas test_wms_caixas -v` → PASS (o tipo novo não quebra a tela Posições).
- [ ] **Step 5: Commit** — `WMS: posicao DOCA (area DOC1, tipo Doca)`

---

### Task 3: Temperatura do produto

**Files:**
- Modify: `painel_agentes/wms.py` (DDL/migração de `wms_produtos`, função nova ao lado de `definir_controla_validade`, linha ~538)
- Modify: `painel_agentes/wms_doca.py`
- Modify: `painel_agentes/painel_agentes.py` (rota ao lado de `/api/wms/produtos/<int:produto_id>/controla-validade`, linha ~6282)
- Modify: `painel_agentes/templates/wms.html` (cartão "Regra do produto", linha ~489)
- Test: `painel_agentes/test_wms_doca.py`, `painel_agentes/test_wms_doca_tela.py`

**Interfaces:**
- Produces: coluna `wms_produtos.temperatura TEXT NOT NULL DEFAULT ''` (valores `''`, `AMBIENTE`, `REFRIGERADO`, `CONGELADO`); `wms.definir_temperatura_produto(conn, produto_id: int, temperatura: str) -> dict` (devolve o produto; `''` limpa; valor fora de `wms.TEMPERATURAS` levanta `ErroWMS`); `wms_doca.temperatura_do_produto(conn, produto_id: int) -> str`; rota `POST /api/wms/produtos/<int:produto_id>/temperatura` com corpo `{"temperatura": "..."}`, níveis `_NIVEIS_WMS_ADMIN`.

Regra de `temperatura_do_produto`: (1) o cadastro, se preenchido; (2) senão, a temperatura da área onde o produto tem mais saldo, ignorando a doca; (3) senão `''`.

- [ ] **Step 1: Testes que falham**

```python
def test_temperatura_do_cadastro(self):
    wms.definir_temperatura_produto(self.conn, self.pid, "REFRIGERADO")
    self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "REFRIGERADO")

def test_temperatura_deduzida_de_onde_esta(self):
    wms.criar_area(self.conn, "CG1", "CAM_CONGELADA"); wms.criar_posicao(self.conn, "CG1-P1")
    wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=5, destino="CG1-P1", validade="2027-01-01")
    self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "CONGELADO")

def test_temperatura_desconhecida_e_doca_nao_conta(self):
    wms_doca.garantir_doca(self.conn)
    wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=5, destino="DOC1-P1", validade="2027-01-01")
    self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "")

def test_temperatura_invalida(self):
    with self.assertRaises(wms.ErroWMS):
        wms.definir_temperatura_produto(self.conn, self.pid, "MORNO")
```

Na rota: nível `galpao` recebe 403; `total` grava e a resposta traz `produto.temperatura == "CONGELADO"`.

- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.** A coluna entra pelo mesmo mecanismo de migração de coluna que `wms.py` já usa para `wms_produtos` (banco existente tem que ganhar a coluna ao conectar). Na tela, um `<select>` "Temperatura do produto" (Não informada / Ambiente / Refrigerado / Congelado) no mesmo cartão e com a mesma condição `S.podeAdministrar` do checkbox de validade.
- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest test_wms_doca test_wms_doca_tela test_wms_telas -v`.
- [ ] **Step 5: Commit** — `WMS: temperatura no cadastro do produto`

---

### Task 4: Sugestão de posição

**Files:**
- Modify: `painel_agentes/wms_doca.py`
- Test: `painel_agentes/test_wms_doca.py`

**Interfaces:**
- Consumes: `temperatura_do_produto` (Task 3), `DOCA_POSICAO` (Task 2).
- Produces: `wms_doca.sugerir_posicoes(conn, produto_id: int, lote: str, validade: str | None, limite: int = 3) -> list[dict]`, cada item `{"posicao": str, "criterio": str, "motivo": str}`, em ordem de preferência, sem repetir posição.

Regras, nesta ordem (a spec fixa a ordem):

| `criterio` | Quem entra | `motivo` (texto exato) |
|---|---|---|
| `MESMO_LOTE` | posição com saldo > 0 do mesmo produto, lote e validade | `Já tem este produto aqui (lote {lote}, {qtd:g} UN)`; sem lote: `Já tem este produto aqui ({qtd:g} UN)` |
| `AO_LADO` | posição vazia na mesma área de uma posição que tem o produto (qualquer lote) | `Ao lado do mesmo produto` |
| `EMBARCADOR` | posição vazia na área com mais posições ocupadas por produtos do mesmo `embarcador_id` | `Perto de outros produtos do mesmo embarcador` |
| `VAZIA` | qualquer outra posição vazia | `Posição vazia na área certa` |

- Filtro geral: só posição e área ativas; nunca a doca; se `temperatura_do_produto` não for `''`, só áreas com essa temperatura (sem temperatura conhecida, não filtra).
- "Vazia" = nenhuma linha de `wms_saldos` com quantidade > 0.
- Lote diferente do mesmo produto não entra em `MESMO_LOTE`.
- Ordem dentro de `AO_LADO`: mesma estante primeiro (menor diferença de nível), depois menor diferença de estante; em pallet, menor diferença de número. Dentro dos outros critérios: código da posição.

- [ ] **Step 1: Testes que falham** — um por regra, nomes: `test_sugere_mesmo_lote_primeiro`, `test_lote_diferente_cai_para_ao_lado`, `test_ao_lado_prefere_mesma_estante`, `test_embarcador_quando_produto_e_novo`, `test_vazia_como_ultimo_recurso`, `test_respeita_temperatura`, `test_sem_temperatura_nao_filtra`, `test_nunca_sugere_doca`, `test_sem_posicao_vazia_devolve_lista_vazia`, `test_lote_vazio_e_validade_nula`. Exemplo do primeiro, que fixa formato e texto:

```python
def test_sugere_mesmo_lote_primeiro(self):
    wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=80,
                            lote="L2611", validade="2027-06-30", destino="C1-E1-N1")
    s = wms_doca.sugerir_posicoes(self.conn, self.pid, "L2611", "2027-06-30")
    self.assertEqual(s[0], {"posicao": "C1-E1-N1", "criterio": "MESMO_LOTE",
                            "motivo": "Já tem este produto aqui (lote L2611, 80 UN)"})
    self.assertEqual(s[1]["criterio"], "AO_LADO")
    self.assertLessEqual(len(s), 3)
```

- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.** Leitura só; nenhuma escrita, nenhum commit.
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** — `WMS: sugestao de posicao para a guarda`

---

### Task 5: Guardar a partir da doca, com a reserva acompanhando

**Files:**
- Modify: `painel_agentes/wms_caixas.py` (`mover_caixa`, linha ~399)
- Modify: `painel_agentes/wms_doca.py`
- Test: `painel_agentes/test_wms_doca.py`, `painel_agentes/test_wms_caixas.py`

**Interfaces:**
- Consumes: `garantir_doca`, `DOCA_POSICAO`, `temperatura_do_produto`.
- Produces:
  - `wms_caixas.mover_caixa(conn, serial, destino, *, uuid, operador=None, levar_reserva: bool = False)`: com `levar_reserva=True` não chama `_recusar_se_reservada`. O padrão não muda nada.
  - `class wms_doca.TemperaturaDiferente(wms.ErroWMS)` com atributo `dados = {"produto": str, "area": str, "sugerida": str | None}` (temperatura do produto, temperatura da área bipada, primeira sugestão).
  - `wms_doca.guardar(conn, *, produto_id: int, lote: str, validade: str | None, destino: str, uuid: str, operador: dict | None = None, quantidade: float | None = None, seriais: list[str] | None = None, confirmar_temperatura: bool = False) -> dict`
  - Retorno: `{"movidas": list[str], "quantidade": float, "restante_na_doca": float, "nao_movidas": list[{"serial": str, "motivo": str}], "reservas_levadas": int, "destino": str}`.

Comportamento de `guardar`:

- `uuid` obrigatório; `destino` normalizado, ativo e diferente da doca (`ErroWMS("Bipe a posição de destino, não a doca.")`).
- Temperatura do produto conhecida e diferente da temperatura da área de destino: levanta `TemperaturaDiferente`, a menos que `confirmar_temperatura=True`. Confirmado: segue, e os movimentos levam na observação ` · temperatura diferente confirmada`.
- **Produto com caixa** (existe caixa `EM_ESTOQUE`/`ABERTA` desse produto + lote + validade na doca): `seriais=None` move todas; `seriais=[...]` move só essas (cada uma tem que estar na doca e ser dessa chave, senão `ErroWMS` antes de mover qualquer uma). Cada caixa: `mover_caixa(..., uuid=f"g-{uuid}-{serial}", levar_reserva=True)`. Caixa com unidade separada (`wms_caixas._prometido(conn, serial) > 0`) não é movida e vai para `nao_movidas` com motivo `unidades separadas para pedido`. `quantidade` é ignorada.
- **Produto sem caixa**: `quantidade=None` move todo o saldo da chave na doca; senão move `quantidade`. Uma `TRANSFERENCIA` com `uuid=f"g-{uuid}"` e observação `Guarda da doca`.
- Depois dos movimentos, leva as reservas:

```
chave = (produto_id, lote, validade or '')
excedente = soma das reservas ATIVAS da chave em DOC1-P1  -  saldo da chave em DOC1-P1
se excedente > 0, numa transacao curta (BEGIN IMMEDIATE), percorre essas reservas por id crescente:
    q <= excedente  -> UPDATE posicao = destino, atualizado_em;                  excedente -= q
    q >  excedente  -> UPDATE quantidade_un = q - excedente;
                       INSERT copia (pedido_id, item_id, produto_id, lote, validade, origem, estado ATIVA,
                       movimento_uuid '') com posicao = destino e quantidade_un = excedente;   excedente = 0
reservas_levadas = quantas linhas foram reapontadas ou criadas
```

  Essa etapa roda sempre, inclusive quando os movimentos voltaram como duplicados (é o que conserta uma queda entre o movimento e a reserva).

- [ ] **Step 1: Testes que falham** em `test_wms_doca.py`. Nomes e o que fixam:
  - `test_guardar_sem_caixa_tudo`: 120 UN na doca, `guardar(destino="C1-E1-N1")` → saldo doca 0, destino 120, `restante_na_doca == 0`.
  - `test_guardar_sem_caixa_parte`: `quantidade=40` → doca 80, destino 40.
  - `test_guardar_caixas_todas`: 3 caixas de 12 UN ativadas na doca → `movidas` com os 3 seriais, cada `obter_caixa(...)["posicao"] == "C1-E1-N1"`.
  - `test_guardar_caixas_so_as_bipadas`: `seriais=[s1]` → só `s1` muda; `restante_na_doca == 24`.
  - `test_serial_de_outro_lote_recusa_sem_mover_nada`.
  - `test_reserva_acompanha_inteira`: reserva ATIVA de 24 UN na doca, guarda tudo → a reserva passa a ter `posicao == "C1-E1-N1"`, `reservas_levadas == 1`.
  - `test_reserva_divide_quando_guarda_parte`: 36 UN na doca, reserva de 30; guarda 12 → sobra 24 na doca; reserva original fica com 24 na doca e nasce uma de 6 no destino, mesmo `pedido_id` e `item_id`; soma continua 30.
  - `test_reserva_que_cabe_na_doca_nao_muda`: 36 na doca, reserva de 10, guarda 12 → reserva intacta, `reservas_levadas == 0`.
  - `test_retry_mesmo_uuid_nao_move_duas_vezes` (Review Focus 1): chamar duas vezes com o mesmo `uuid` → saldos iguais aos da primeira; reservas no destino.
  - `test_pedir_mais_do_que_sobrou` (Review Focus 2): doca com 10, `quantidade=20` → `ErroWMS`, saldo da doca continua 10.
  - `test_lote_vazio_validade_nula` (Review Focus 4): produto com `controla_validade` desligado, `lote=""`, `validade=None` → guarda funciona.
  - `test_caixa_aberta_guarda_com_quantidade_atual` e `test_caixa_com_unidade_separada_nao_move` (Review Focus 5): a segunda aparece em `nao_movidas` e continua na doca.
  - `test_temperatura_diferente_pede_confirmacao`: produto `REFRIGERADO`, destino em área `AMBIENTE` → `TemperaturaDiferente` com `dados["produto"] == "REFRIGERADO"`; com `confirmar_temperatura=True` passa.
  - `test_destino_doca_recusa`.

  Em `test_wms_caixas.py`: `test_mover_reservada_continua_recusando_fora_da_doca` (posição comum, sem `levar_reserva` → `ErroWMS` com "reservada").

- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar** o parâmetro em `mover_caixa` e `guardar` em `wms_doca.py`.
- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest test_wms_doca test_wms_caixas test_wms_pedidos test_wms_separacao -v` → PASS.
- [ ] **Step 5: Commit** — `WMS: guardar a partir da doca levando a reserva dos pedidos`

---

### Task 6: Fila de guarda

**Files:**
- Modify: `painel_agentes/wms_doca.py`
- Test: `painel_agentes/test_wms_doca.py`

**Interfaces:**
- Consumes: `sugerir_posicoes`, `temperatura_do_produto`.
- Produces: `wms_doca.fila_guarda(conn) -> list[dict]`, um item por produto + lote + validade com saldo > 0 na doca:

```python
{"produto_id": int, "descricao": str, "sku": str, "embarcador": str, "embarcador_id": int | None,
 "lote": str, "validade": str,            # '' quando nao tem
 "quantidade": float, "unidade": str,
 "caixas": int,                           # caixas EM_ESTOQUE/ABERTA na doca sem unidade separada; 0 = produto sem caixa
 "temperatura": str,                      # '' = sem temperatura
 "desde": str,                            # criado_em da primeira ENTRADA da chave na doca
 "recebimentos": list[str],               # codigos, pela observacao dos movimentos/caixas; pode ser []
 "pedidos_reservados": int,               # pedidos distintos com reserva ATIVA da chave na doca
 "sugestao": {"posicao": str, "motivo": str} | None}
```

- Ordem: `temperatura` em (`REFRIGERADO`, `CONGELADO`) primeiro; depois `desde` crescente.
- Não filtra por embarcador (Review Focus 3).

- [ ] **Step 1: Testes que falham**: `test_fila_agrupa_por_produto_lote_validade`, `test_fila_frio_primeiro_depois_mais_antigo`, `test_fila_conta_caixas_e_pedidos_reservados`, `test_fila_traz_sugestao_ou_none`, `test_fila_vazia_sem_doca` (banco sem a área DOCA devolve `[]`, sem erro), `test_fila_mostra_qualquer_embarcador` (Review Focus 3), `test_fila_lote_vazio_validade_nula` (Review Focus 4).
- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.** Leitura só.
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** — `WMS: fila de guarda da doca`

---

### Task 7: Rotas

**Files:**
- Modify: `painel_agentes/painel_agentes.py` (bloco WMS, linhas ~6080–7270)
- Test: `painel_agentes/test_wms_doca_tela.py`
- Modify: `MAPA_DO_SISTEMA.txt`

**Interfaces:**
- Consumes: tudo de `wms_doca`, `wms_pilotos.ids_com_etapas`.
- Produces:
  - `_wms_ids_etapas() -> set` (ao lado de `_wms_ids_caixas`).
  - Bootstrap (`_wms_bootstrap`) ganha `"etapas_embarcadores": list[int]`, `"etapas_ativo": bool`, `"doca": "DOC1-P1"`, `"guarda_pendente": int` (tamanho de `fila_guarda`; 0 sem a área DOCA). Com `etapas_ativo`, o bootstrap chama `wms_doca.garantir_doca`. A tela mostra o botão Guardar quando `etapas_ativo` **ou** `guarda_pendente > 0` (embarcador tirado da lista com saldo na doca continua podendo ser guardado; teste `test_bootstrap_guarda_pendente_sem_etapas`).
  - `GET /api/wms/recebimentos/<id>`: cada item ganha `"usa_etapas": bool` (embarcador do produto em `_wms_ids_etapas()`).
  - `POST /api/wms/caixas/<serial>/ativar`: quando vem `recebimento_id` e o produto da caixa é de embarcador com etapas, a posição usada é a doca, qualquer que seja a enviada.
  - `GET /api/wms/guarda/fila` → `{"itens": fila_guarda(...)}`. Níveis `_NIVEIS_WMS`.
  - `GET /api/wms/guarda/item?produto_id=&lote=&validade=` → `{"item": <o item da fila ou 404>, "sugestoes": sugerir_posicoes(...), "caixas": [{"serial", "qtd_un", "situacao"}]}`.
  - `POST /api/wms/guarda/guardar`, corpo `{"produto_id", "lote", "validade", "destino", "uuid", "quantidade"?, "seriais"?, "confirmar_temperatura"?}` → retorno de `guardar`. `@exige_mesma_origem`, `_wms_exige_operador()`, `_wms_exige_uuid_do_aparelho`. `TemperaturaDiferente` → HTTP 409 `{"erro": str, "temperatura": dados}`. Outros `ErroWMS` → 400 pelo `_wms_json_erro`.

- [ ] **Step 1: Testes que falham**: `test_bootstrap_sem_etapas_nao_cria_doca` (config sem `etapas` → `etapas_ativo` falso e `wms.obter_area(conn, "DOC1") is None`), `test_bootstrap_com_etapas_cria_doca`, `test_recebimento_marca_usa_etapas`, `test_ativar_caixa_de_etapas_vai_para_doca` (corpo manda `posicao: "C1-E1-N1"`, a caixa termina em `DOC1-P1`), `test_ativar_caixa_sem_etapas_respeita_posicao`, `test_fila_e_item`, `test_guardar_exige_operador` (409 com `sem_operador`), `test_guardar_ok`, `test_guardar_temperatura_409`, `test_guardar_uuid_reservado_recusa` (uuid `cx-...` → 400).
- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar** as rotas e acrescentar ao `MAPA_DO_SISTEMA.txt`: `wms_doca.py` na lista de módulos do WMS, as três rotas `/api/wms/guarda/*`, e a chave `etapas` de `wms.pilotos`.
- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest test_wms_doca_tela test_wms_caixas_tela test_wms_telas -v`.
- [ ] **Step 5: Commit** — `WMS: rotas da guarda e conferencia direto na doca`

---

### Task 8: Tela Conferir Recebimento

**Files:**
- Modify: `painel_agentes/templates/wms.html` (`TELAS.inicio` ~368, `TELAS.receber` ~750, `TELAS.recebimento` ~772, `confirmarRecebimentoItem` ~1037, `TELAS.encerramento` ~858)
- Test: `painel_agentes/test_wms_doca_tela.py`

**Interfaces:**
- Consumes: bootstrap `etapas_ativo` e `doca`; `usa_etapas` por item.
- Produces: função JS `rotuloPos(codigo)` que devolve `'DOCA'` para `S.doca` e o próprio código para o resto; usada em todo lugar que mostra posição ao operador (lista de saldos, "onde buscar" da separação, log dos bipes, toasts).

O que muda, só para item com `usa_etapas` (item sem a marca fica idêntico a hoje):

- Sem o campo Posição (nos dois ramos, com e sem caixa). O envio usa `S.doca` como posição/destino.
- Botão `Registrar entrada` vira `Registrar conferido`; selo `endereçado` vira `conferido`; "Recebido X de Y" vira "Conferido X de Y"; toast `Conferido, está na DOCA`.
- Com `etapas_ativo`: o botão do Início e o título da lista passam a `Conferir Recebimento`; `Encerrar recebimento` vira `Encerrar conferência`; a tela de encerramento ganha um cartão `Os N itens conferidos vão para a fila Guardar.`
- Botão `Pedir etiquetas de todas as linhas` no topo, visível quando há mais de uma linha com `usa_caixas`. Percorre as linhas com lote/validade válidos e nº de caixas de 1 a 200 e faz, para cada uma, o mesmo que o `data-pedir` faz hoje (um `POST /caixas` por linha, guardando seriais e `impressaoId` no estado daquela linha). No fim, um toast `Etiquetas pedidas para N linha(s)`; linha incompleta é pulada e citada no toast.

- [ ] **Step 1: Teste que falha** — o HTML de `/wms` contém os marcadores novos: `Registrar conferido`, `Conferir Recebimento`, `Pedir etiquetas de todas as linhas`, `function rotuloPos`. Siga o que `test_wms_telas.py` já faz para verificar o template.
- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar** no `wms.html`, no estilo do arquivo (template strings, `esc()`, `toast()`, `api()`).
- [ ] **Step 4: Rodar e ver passar**, e provar na tela: subir o painel em outra porta (`py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"`, de dentro de `painel_agentes/`) e, com Playwright, conferir uma linha de um recebimento de embarcador com `etapas` num banco de teste: a caixa/quantidade termina em `DOC1-P1` e a tela mostra `DOCA`. Conferir também um recebimento de embarcador sem `etapas`: o campo Posição continua lá.
- [ ] **Step 5: Commit** — `WMS: Conferir Recebimento sem posicao, direto na doca`

---

### Task 9: Telas Guardar

**Files:**
- Modify: `painel_agentes/templates/wms.html`
- Test: `painel_agentes/test_wms_doca_tela.py`

**Interfaces:**
- Consumes: rotas `/api/wms/guarda/*` (Task 7), `rotuloPos` (Task 8), `abrirCamera`, `serialDe`, `uuid()`, `enfileirar` já existentes.
- Produces: `TELAS.guardar`, `TELAS.guardarItem`.

- **Início**: com `etapas_ativo` ou `guarda_pendente > 0`, botão `Guardar` com o número de itens da fila e o texto `itens na doca sem lugar`. Cartão de atenção quando a fila tem item refrigerado ou congelado: `Na doca: N refrigerado(s)/congelado(s) há X min. Guarde primeiro.`
- **`TELAS.guardar`** (fila): um cartão por item, com descrição, quantidade, recebimento(s), embarcador, lote, selo de temperatura (`sem temperatura` quando `''`), tempo na doca, sugestão em `mono` ou selo `sem sugestão`, e `reservado para N pedido(s)` quando houver. Toque abre o item. Bipar uma caixa nesta tela abre o item dela.
- **`TELAS.guardarItem`**: bloco de sugestão em destaque (posição grande + motivo), até duas alternativas, campo Posição (bipe ou câmera). Depois de ler a posição:
  - sem caixa: campo Quantidade (padrão: tudo) e `Confirmar guarda`;
  - com caixa: `Guardar as N caixas aqui` e `Guardar só uma parte aqui`. A segunda abre o bipe de caixas com contador e lista; o botão vira `Guardar as K bipadas aqui`, com a linha `As outras M caixas continuam na doca e na fila Guardar.`
- Resposta 409 com `temperatura`: cartão `Posição não combina`, com as temperaturas e a sugerida, e os botões `Bipar outra` e `Guardar aqui mesmo` (reenvia com `confirmar_temperatura: true` e o mesmo `uuid`).
- `nao_movidas` não vazio: toast listando os seriais e o motivo.
- Sem rede: a confirmação entra na fila local (`enfileirar`) com o mesmo corpo e o mesmo `uuid`; a sugestão simplesmente não aparece (`sem rede: bipe a posição`).
- Depois de guardar: volta para a fila, recarregada.

- [ ] **Step 1: Teste que falha** — o HTML de `/wms` contém `TELAS.guardar`, `TELAS.guardarItem`, `Guardar só uma parte aqui`, `Guardar aqui mesmo`.
- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar**, e provar na tela (porta 8099, Playwright, banco de teste), os quatro caminhos: guardar tudo sem caixa; guardar todas as caixas; guardar parte bipando; posição de temperatura diferente com `Guardar aqui mesmo`. Em cada um, conferir no banco o saldo da doca e do destino.
- [ ] **Step 5: Commit** — `WMS: fila Guardar com sugestao de posicao`

---

### Task 10: Regressão e mapa

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (conferir que Tasks 7–9 deixaram tudo registrado: módulo, rotas, telas do SPA, chave `etapas`, posição `DOC1-P1`)

- [ ] **Step 1:** `py -3.11 -m py_compile painel_agentes/wms.py painel_agentes/wms_doca.py painel_agentes/wms_caixas.py painel_agentes/wms_pilotos.py painel_agentes/painel_agentes.py` → sem saída.
- [ ] **Step 2:** de dentro de `painel_agentes/`, todos os testes do WMS, um módulo por argumento: `py -3.11 -m unittest test_wms_caixas test_wms_caixas_tela test_wms_doca test_wms_doca_tela test_wms_etiqueta_produto test_wms_etiqueta_volume test_wms_faltas_recebimento test_wms_fila_impressao test_wms_pedidos test_wms_pilotos test_wms_separacao test_wms_separacao_tela test_wms_telas` → OK, 0 falhas.
- [ ] **Step 3:** de dentro de `painel_agentes/`, `py -3.11 -m unittest test_controle_faltas` e, da raiz, `py -3.11 -m unittest test_sincronizar_recebimentos_wms` → OK (mexem em recebimento).
- [ ] **Step 4: Commit** do mapa, se mudou — `Mapa: recebimento por etapas (doca e guarda)`.
- [ ] **Step 5:** Parar aqui. Push, homologação (`homolog subir <ramo>`) e deploy são decisão do Hugo. Para ligar: `etapas: true` no piloto escolhido, em `wms.pilotos`, no `config.yaml` da VPS, feito pelo Hugo.
