# Devolução automática de estoque no cancelamento — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** depois de um cancelamento confirmado na Stokki (portal, solicitação ou /pedidos), o saldo que o pedido bloqueou sai do Virtual para um endereço físico, sozinho, e o galpão é avisado de onde guardar a caixa.

**Architecture:** os três caminhos de cancelamento só gravam o pedido numa fila no `dados/dados.db`. O worker do portal (ciclo de 20 s) lê a fila, monta o plano com leituras na área `/administrator` (conta principal) e executa a movimentação na área `/provider` (conta wms@, Playwright), sempre sob `stokki/sessao_uso.py` com espera curta. Regras de decisão ficam em funções puras.

**Tech Stack:** Python 3.11, requests (`StokkiSession`), Playwright sync, SQLite, unittest.

**Spec:** `DOC_EXECUCAO_CLAUDE_DEVOLUCAO_ESTOQUE_CANCELAMENTO.md` (raiz). Ler antes de qualquer tarefa.

## Global Constraints

- Ramo próprio `devolucao-estoque-cancelamento` a partir de `origin/master`, em worktree (o working tree principal tem edições de outras sessões). `git add` só dos arquivos da tarefa. Commit local por tarefa; **push, PR e deploy só quando o Hugo pedir**.
- Python: `py -3.11`. Testes em `unittest` com sessão falsa; nenhum teste fala com a Stokki.
- Nada roda contra a Stokki no notebook. Leitura real: script em `/tmp` na VPS como www-data, só GET (skill operar-stokki). Escrita real na Stokki: **nenhuma neste plano** antes da Tarefa 8, e ali só com o Hugo.
- Chave `stokki.devolucao_cancelamento.ativo` no `config.yaml`, ausente = desligada. Desligada: nada entra na fila, nada roda.
- Mover só pela Movimentação de Estoque Manual do provider. Nunca Ajuste Manual.
- Nunca mover mais que o Bloqueio do pedido nem mais que o `product.quantity` oferecido pela tela.
- Na dúvida, não mover e avisar a operação.
- Falha da devolução nunca desfaz nem trava o cancelamento, o ciclo do worker ou a ação do painel.
- Endereços de sistema nunca são destino: `Virtual`, `Falta`, `Sobra`, `Ajuste` (comparar sem diferenciar maiúsculas).
- Motivo gravado na Stokki: `Retorno ao estoque do PS-<id> cancelado`.
- Comentários e logs sem acento nos arquivos de `stokki/` (padrão do diretório).
- `config.yaml` nunca é commitado nem colado em lugar nenhum.

## Review Focus

1. **Desbloqueio manual da equipe entre o cancelamento e a vez do robô** → o robô não move de novo (Tarefa 2, `ja_devolvido`).
2. **Mesmo pedido cancelado duas vezes (clique repetido, "já estava cancelado")** → uma linha só na fila, uma devolução só (Tarefa 3).
3. **Worker cai depois de mover o item 1 e antes do item 2** → a nova tentativa move só o item 2 (Tarefa 5).
4. **Stokki ocupada por muito tempo (pipeline horário)** → a fila espera sem consumir tentativas e sem atrasar o envio de pedidos dos clientes (Tarefa 5).
5. **Grupo ❌PEDIDOS CANCELADOS❌ sem id no config, ou teto diário do WhatsApp estourado** → a devolução acontece igual e o log diz que o aviso não saiu (Tarefa 6).

---

### Task 1: Provar as leituras na Stokki e guardar amostras

Decide a regra de destino: se a origem da separação é legível ou não.

**Files:**
- Create: `stokki/amostras_devolucao/` (respostas reais, usadas como fixture nas Tarefas 2 e 4)
- Modify: `DOC_EXECUCAO_CLAUDE_DEVOLUCAO_ESTOQUE_CANCELAMENTO.md` (seção "Para onde", item 1: provado ou descartado)

**Interfaces:**
- Produces: arquivos `movimentos_2278_2026_11.json` (resposta de `v2/moviment/table`), `enderecos_2278.html` (`moviment/addressing/2278/1`), `origem_2278_2026_11.json` (resposta de `addressing/table/moviment/table`), `show_43101.html` (show do pedido cancelado). E a resposta escrita à pergunta "a separação do PS-43101 (03/11 12h12, 4 un) aparece como movimento `<endereço> → Virtual`?".

- [ ] **Step 1:** Escrever sonda só GET (scratchpad → `/tmp` na VPS, www-data, `setsid nohup`, saída em arquivo: a fila pode demorar mais que o SSH). Base: `StokkiSession(contas._ler_config())`, `ajuste_estoque.ADM`, `AJAX`, `_params_tabela`. Para descobrir os parâmetros de `addressing/table/moviment/table`, ler o JavaScript do DataTable na página `moviment/addressing/moviment/2278/1` (colunas e filtros `month`, `year`, localização inicial/destino) em vez de chutar.
- [ ] **Step 2:** Rodar e baixar as quatro respostas para `stokki/amostras_devolucao/`. Conferir que nenhuma contém token (`_token`, `csrf-token`): remover essas linhas dos HTML antes de salvar.
- [ ] **Step 3:** No show do pedido, localizar: id do cliente (`#stkkc-100`), a tabela de itens com colunas `ID`/`SKU`/`Nome` (product_id) e a linha de cancelamento em "Histórico do pedido" (data e hora). Anotar os seletores no cabeçalho de um `LEIA.txt` na pasta.
- [ ] **Step 4:** Registrar no spec o resultado da origem. Se **não** for legível, a regra de destino começa no item 2 e a Tarefa 4 não implementa `ler_origem`: dizer isso ao Hugo na entrega.
- [ ] **Step 5:** Commit `Stokki: amostras de leitura para a devolucao de estoque no cancelamento`.

### Task 2: Regras puras de decisão

**Files:**
- Create: `stokki/devolucao_estoque.py`
- Test: `stokki/test_devolucao_estoque.py`

**Interfaces:**
- Produces:
  - `SISTEMA = ("virtual", "falta", "sobra", "ajuste")`
  - `bloqueado_pelo_pedido(movimentos: list[dict], id_stokki: int) -> float` — soma das linhas `{"tipo": "Bloqueio", "referencia": "#PS-<id>", "quantidade": float, "quando": datetime}`.
  - `ja_devolvido(movimentos: list[dict], qtd: float, cancelado_em: datetime) -> str` — `"nao"` | `"sim"` | `"duvida"`.
  - `quanto_mover(bloqueado: float, oferecido: float) -> float` — `min`, nunca negativo.
  - `escolher_destinos(qtd: float, origens: list[tuple[str, float]], saldo_produto: list[dict], saldo_embarcador: list[dict]) -> list[tuple[str, float]]` — lista ordenada de candidatos `(endereco, qtd)`; `saldo_*` = `[{"endereco": str, "estoque": float}]`.
  - `cancelamento_antigo(cancelado_em: datetime | None, agora: datetime, ja_estava: bool) -> bool` — `True` só com `ja_estava` e mais de 24 h (ou sem data).

Movimento normalizado (o que a Tarefa 4 entrega): `{"tipo": "Bloqueio"|"Desbloqueio"|..., "movimento": "Interno"|..., "referencia": str, "quantidade": float, "quando": datetime}`.

- [ ] **Step 1: testes que falham** (`stokki/test_devolucao_estoque.py`):

```python
T = datetime(2026, 11, 3, 12, 30)   # cancelamento
def mov(tipo, ref, q, h): return {"tipo": tipo, "movimento": "Interno", "referencia": ref, "quantidade": q, "quando": datetime(2026, 11, 3, h, 0)}

def test_bloqueado_soma_so_o_pedido(self):
    ms = [mov("Bloqueio", "#PS-43101", 4, 12), mov("Bloqueio", "#PE-2609", 20, 11), mov("Bloqueio", "#PS-43102", 6, 12)]
    self.assertEqual(de.bloqueado_pelo_pedido(ms, 43101), 4)
def test_bloqueado_nao_confunde_prefixo(self):   # PS-4310 x PS-43101
    self.assertEqual(de.bloqueado_pelo_pedido([mov("Bloqueio", "#PS-43101", 4, 12)], 4310), 0)
def test_sem_desbloqueio_nao_devolvido(self):
    self.assertEqual(de.ja_devolvido([mov("Bloqueio", "#PS-43101", 4, 12)], 4, T), "nao")
def test_desbloqueio_igual_sem_referencia_depois_do_cancelamento(self):
    self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 4, 13)], 4, T), "sim")
def test_desbloqueio_antes_do_cancelamento_nao_conta(self):
    self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 4, 11)], 4, T), "nao")
def test_desbloqueio_de_outra_quantidade_e_duvida(self):
    self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 3, 13)], 4, T), "duvida")
def test_desbloqueio_com_referencia_nao_conta(self):   # "#stkkadj-" e ajuste, nao devolucao
    self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "#stkkadj-1097", 4, 13)], 4, T), "nao")
def test_quanto_mover(self):
    self.assertEqual((de.quanto_mover(4, 12), de.quanto_mover(4, 3), de.quanto_mover(4, 0)), (4, 3, 0))
def test_destino_origem_primeiro(self):
    r = de.escolher_destinos(4, [("FABENNE-2", 4)], [{"endereco": "FABENNE-1", "estoque": 5}], [])
    self.assertEqual(r[0], ("FABENNE-2", 4))
def test_destino_origem_em_dois_enderecos(self):
    r = de.escolher_destinos(4, [("A-1", 3), ("A-2", 1)], [], [])
    self.assertEqual(r[:2], [("A-1", 3), ("A-2", 1)])
def test_destino_sem_origem_maior_saldo_do_produto(self):
    r = de.escolher_destinos(4, [], [{"endereco": "Virtual", "estoque": 28}, {"endereco": "FABENNE-1", "estoque": 5}, {"endereco": "FABENNE-3", "estoque": 9}], [])
    self.assertEqual(r[0], ("FABENNE-3", 4))
def test_destino_sem_produto_maior_saldo_do_embarcador(self):
    r = de.escolher_destinos(4, [], [{"endereco": "Virtual", "estoque": 28}], [{"endereco": "Ajuste", "estoque": 99}, {"endereco": "FABENNE-1", "estoque": 40}])
    self.assertEqual(r, [("FABENNE-1", 4)])
def test_destino_nenhum(self):
    self.assertEqual(de.escolher_destinos(4, [], [], [{"endereco": "VIRTUAL", "estoque": 9}]), [])
def test_origem_de_sistema_e_ignorada(self):
    self.assertEqual(de.escolher_destinos(4, [("Sobra", 4)], [{"endereco": "X-1", "estoque": 1}], [])[0], ("X-1", 4))
def test_antigo(self):
    agora = datetime(2026, 11, 5, 12, 0)
    self.assertTrue(de.cancelamento_antigo(datetime(2026, 11, 3, 12, 0), agora, True))
    self.assertFalse(de.cancelamento_antigo(datetime(2026, 11, 5, 9, 0), agora, True))
    self.assertFalse(de.cancelamento_antigo(datetime(2026, 11, 1, 9, 0), agora, False))
    self.assertTrue(de.cancelamento_antigo(None, agora, True))
```

- [ ] **Step 2:** `py -3.11 -m unittest stokki.test_devolucao_estoque` → FAIL (módulo não existe).
- [ ] **Step 3:** Implementar. `escolher_destinos`: candidatos na ordem origem (cada parte com a sua quantidade; se as partes não somam `qtd`, o resto vai para o próximo candidato) → maior estoque do produto → maior estoque do embarcador; sem repetir endereço; a Tarefa 5 tenta o candidato seguinte quando a Stokki recusa um destino. `ja_devolvido` olha só `Desbloqueio | Interno` com referência vazia e `quando > cancelado_em`: algum com quantidade igual = `"sim"`; existe algum mas nenhum igual = `"duvida"`; nenhum = `"nao"`.
- [ ] **Step 4:** mesmo comando → PASS.
- [ ] **Step 5:** Commit `Stokki: regras da devolucao de estoque no cancelamento (quanto, para onde, ja devolvido)`.

### Task 3: Fila no banco e os três ganchos

**Files:**
- Modify: `stokki/devolucao_estoque.py`, `stokki/test_devolucao_estoque.py`
- Modify: `portal_cliente/cancelamento.py` (`_processar_um`, logo depois do `cancelar_stokki` ok), `portal_cliente/solicitacoes_execucao.py` (`cancelar_stokki` de `dependencias_reais`), `painel_agentes/acoes_pedidos.py` (`cancelar_stokki`, nos dois ramos `r.get("ok")`)
- Test: `portal_cliente/test_cancelamento.py`, `portal_cliente/test_solicitacoes_execucao.py`, `painel_agentes/test_acoes_pedidos.py`

**Interfaces:**
- Produces:
  - Tabela `stokki_devolucoes` (`CREATE TABLE IF NOT EXISTS` dentro de `garantir_tabela(conn)`): `id_stokki INTEGER PRIMARY KEY, origem TEXT, ja_estava INTEGER, status TEXT, tentativas INTEGER DEFAULT 0, itens_json TEXT, erro TEXT, criado_em TEXT, atualizado_em TEXT`.
  - Status: `PENDENTE`, `FEITA`, `PARCIAL`, `NADA`, `JA_DEVOLVIDO`, `ANTIGO`, `DUVIDA`, `FALHA`. Só `PENDENTE` é processado.
  - `ligado(config: dict) -> bool` — `config["stokki"]["devolucao_cancelamento"]["ativo"]` verdadeiro.
  - `registrar(id_stokki: int, origem: str, config: dict, ja_estava: bool = False, conn=None) -> bool` — `origem` ∈ `"portal"`, `"solicitacao"`, `"pedidos"`. Desligado → `False` sem tocar o banco. `INSERT OR IGNORE`. `conn=None` abre `dados/dados.db` (mesmo caminho de `stokki/sessao_uso._conectar`). **Nunca levanta**: erro vira `logger.warning` e `False`.
  - `pendentes(conn) -> list[dict]`, `gravar(conn, id_stokki, *, status=None, itens=None, erro=None, mais_uma_tentativa=False) -> None`.

- [ ] **Step 1: testes que falham.** Em `stokki/test_devolucao_estoque.py` (sqlite `:memory:`): desligado não grava; ligado grava `PENDENTE` com a origem; segundo `registrar` do mesmo id devolve `False` e mantém uma linha; pedido já `FEITA` não volta para `PENDENTE`; `conn` que levanta `sqlite3.OperationalError` → `False`, sem exceção. Nos três testes vizinhos, um caso cada: cancelamento ok chama `registrar` com o id e a origem certos (`mock.patch("stokki.devolucao_estoque.registrar")`); cancelamento com `ok=False` não chama; `registrar` levantando não muda o resultado do cancelamento (`cancelados == 1` / `ok is True`).
- [ ] **Step 2:** rodar e ver falhar: `py -3.11 -m unittest stokki.test_devolucao_estoque`; `py -3.11 -m unittest discover -s portal_cliente -p "test_cancelamento.py"`; idem `test_solicitacoes_execucao.py`; de dentro de `painel_agentes/`: `py -3.11 -m unittest test_acoes_pedidos`.
- [ ] **Step 3:** Implementar a tabela e `registrar`; nos três ganchos, chamada dentro de `try/except Exception` com `logger.warning` (import local do módulo, como os vizinhos fazem com `stokki.cancelar`). No portal o `config` já chega em `processar_cancelamentos`; passar adiante até `_processar_um`. No painel usar `torre_controle._carregar_config()`. `ja_estava=bool(r.get("ja_estava"))`.
- [ ] **Step 4:** mesmos comandos → PASS, mais as suítes inteiras dos três arquivos vizinhos sem regressão.
- [ ] **Step 5:** Commit `Cancelamento: os tres caminhos registram o pedido na fila de devolucao de estoque`.

### Task 4: Leituras na Stokki e montagem do plano

**Files:**
- Modify: `stokki/devolucao_estoque.py`, `stokki/test_devolucao_estoque.py`

**Interfaces:**
- Consumes: amostras da Tarefa 1; funções puras da Tarefa 2; `ajuste_estoque.ADM`, `AJAX`, `_params_tabela`, `numero`, `limpa`, `saldo_por_endereco`.
- Produces:
  - `ler_pedido(sess, id_stokki) -> {"cliente_id": int, "cancelado_em": datetime | None, "itens": [{"product_id": int, "sku": str, "nome": str}]}`
  - `ler_movimentos(sess, product_id, ano, mes) -> list[dict]` (formato normalizado da Tarefa 2)
  - `ler_saldo_enderecos(sess, product_id) -> [{"endereco": str, "estoque": float, "bloqueado": float, "disponivel": float}]`
  - `ler_origem(sess, product_id, quando: datetime) -> list[tuple[str, float]]` — movimentos `<endereço> → Virtual` no minuto do Bloqueio (tolerância de 2 min). Só se a Tarefa 1 provou; senão a função não existe e `montar_plano` passa `[]`.
  - `montar_plano(sess, id_stokki, agora, ja_estava=False) -> {"status": "MOVER"|"NADA"|"JA_DEVOLVIDO"|"ANTIGO"|"DUVIDA", "cliente_id": int, "itens": [{"product_id", "sku", "nome", "qtd", "destinos": [(endereco, qtd)], "situacao": "mover"|"nada"|"ja_devolvido"|"duvida"|"sem_destino"}], "motivo": str}`

- [ ] **Step 1: testes que falham**, com sessão falsa que devolve os arquivos de `stokki/amostras_devolucao/` por trecho de URL (padrão `_Sessao` de `stokki/test_ajuste_estoque.py`): `ler_pedido` do show 43101 → `cliente_id == 100`, contém `product_id` 2278 com SKU `FABMLT`, `cancelado_em` no dia 03/11/2026; `ler_movimentos` → existe `{"tipo": "Bloqueio", "referencia": "#PS-43101", "quantidade": 4.0}` às 12:12 e o `#PE-2609` de 20; `ler_saldo_enderecos` → contém `FABENNE-1` e `Virtual`; `montar_plano` com a amostra → item 2278 `situacao == "ja_devolvido"` (o Desbloqueio de 4 às 12:48 está na amostra). Com movimentos sem Desbloqueio (amostra editada num dicionário do teste) → `MOVER`, `qtd == 4`, primeiro destino conforme a regra. Pedido sem Bloqueio em nenhum item → `NADA`. `ja_estava=True` com cancelamento de 03/11 e `agora` em 12/11 → `ANTIGO`, sem nenhuma leitura de movimentos (contar os GETs). Um item `duvida` torna o plano inteiro `DUVIDA`.
- [ ] **Step 2:** `py -3.11 -m unittest stokki.test_devolucao_estoque` → FAIL.
- [ ] **Step 3:** Implementar. Movimentos: procurar do mês do pedido até o mês de `agora` (no máximo 3 meses para trás). Bloqueio do pedido em mais de uma linha: usar o `quando` da última para a origem. `r.raise_for_status()` em toda leitura; resposta sem a tabela esperada levanta `ValueError` com a URL (vira tentativa na Tarefa 5, não "nada a devolver").
- [ ] **Step 4:** mesmo comando → PASS.
- [ ] **Step 5:** Commit `Stokki: leituras e plano da devolucao de estoque (bloqueio do pedido, saldo por endereco, origem)`.

### Task 5: Movimentação pelo provider e executor da fila

**Files:**
- Create: `stokki/mover_virtual.py`
- Modify: `stokki/devolucao_estoque.py`, `stokki/test_devolucao_estoque.py`
- Modify: `portal_cliente/enviar_stokki.py` (`ciclo`: bloco novo depois de `processar_retomadas_do_ciclo`, e a condição do `logger.info` do loop)
- Test: `stokki/test_mover_virtual.py`, `portal_cliente/test_cancelamento.py`

**Interfaces:**
- Consumes: `montar_plano`, `pendentes`, `gravar`, `ligado`.
- Produces:
  - `mover_virtual.mover(config, cliente_id: int, movimentos: list[dict], motivo: str, headless: bool = True) -> list[dict]` — entrada `[{"product_id", "sku", "qtd", "destinos": [(endereco, qtd)]}]`; saída por movimento `{"product_id", "destino", "pedido": float, "movido": float, "adjust_id": int | None, "erro": str}`. Um login para todos os itens. **Não pega trava**: quem chama já está com a provider.
  - `mover_virtual.montar_campos(cliente_id, id_virtual, product_id, oferecido, qtd, id_destino, motivo) -> dict` (puro; os campos do `form_adjust`).
  - `devolucao_estoque.processar_pendentes(conn, config, *, simular: bool, abrir_admin, mover, avisar_galpao, avisar_operacao, agora=None) -> dict` — devolve `{"feitas": n, "parciais": n, "nada": n, "falhas": n, "esperando": n}`. `abrir_admin()` é gerenciador de contexto que entrega a sessão admin ou levanta `RuntimeError` se ocupada.
  - `enviar_stokki.processar_devolucoes_do_ciclo(conn, config, simular=False) -> dict`.
  - Constantes em `devolucao_estoque`: `MAX_TENTATIVAS = 3`, `ESPERA_CONTA_SEGUNDOS = 20`.

- [ ] **Step 1: testes que falham.**
  - `test_mover_virtual.py`: `montar_campos(100, 3, 2278, 12, 4, 223, "Retorno ao estoque do PS-43101 cancelado")` == `{"client_id": 100, "origin_location_id": 3, "product_id": 2278, "quantity_available": 12, "quantity": 4, "destination_location_id": 223, "damaged": 0, "reason": "Retorno ao estoque do PS-43101 cancelado"}`.
  - `processar_pendentes` com dependências falsas:
    - desligado → não chama nada, devolve zeros;
    - plano `NADA` → status `NADA`, `mover` não é chamado, nenhum aviso;
    - plano `MOVER` com `mover` devolvendo tudo movido → `FEITA`, `itens_json` com `adjust_id`, `avisar_galpao` chamado 1 vez, `avisar_operacao` nenhuma;
    - `mover` devolve `movido < pedido` → `PARCIAL`, os dois avisos;
    - plano `DUVIDA` → status `DUVIDA`, `mover` não chamado, `avisar_operacao` 1 vez;
    - `abrir_admin` levanta `RuntimeError` → continua `PENDENTE`, `tentativas` não sobe, `esperando == 1`;
    - `montar_plano` levanta `ValueError` três ciclos seguidos → `tentativas == 3`, status `FALHA`, `avisar_operacao` 1 vez (não 3);
    - retomada: linha com `itens_json` já contendo `adjust_id` do produto A → `mover` recebe só o produto B;
    - `simular=True` → plano no log, `mover` não chamado, linha continua `PENDENTE`;
    - exceção num pedido não impede o seguinte da fila.
  - `portal_cliente/test_cancelamento.py`: `ciclo` com `processar_devolucoes_do_ciclo` levantando → o ciclo termina e `processar_lote` ainda é chamado.
- [ ] **Step 2:** rodar os três módulos → FAIL.
- [ ] **Step 3: `mover_virtual.mover`.** Playwright como `stokki/avancar_cross.executar`: `ei._credenciais_provider(config)`, `ei._novo_contexto_disfarcado(browser)`, `ei._preencher_form_login`. Abrir `/pt-br/provider/operation/inventory/adjust/option` (página que carrega o `form_adjust` e o `_token`); as chamadas saem de dentro da página (`page.request` ou `fetch` no contexto), com o `_token` do formulário e `X-Requested-With`. Sequência por movimento: `adjust/location/search/<cliente>/VIRTUAL` → id do Virtual; `adjust/location/search/<cliente>/<destino>` → id do destino (código exato; não achou = tenta o próximo destino); `adjust/product/search/<cliente>/<id_virtual>/<sku>` → `product.quantity` (oferecido); `quantity = quanto_mover(qtd, oferecido)`; `POST adjust/store/process` com `montar_campos`; poll `adjust/processed/<adjust_id>` a cada 3 s por até 90 s. Resposta 4xx no destino → próximo destino da lista. Fazer logout ao fim. Os nomes exatos dos campos de resposta vêm do JavaScript da página: ler antes de escrever, não chutar.
- [ ] **Step 4: `processar_pendentes` e o gancho no worker.** Ordem por pedido: abrir admin → `montar_plano` → fechar admin → se `MOVER`, pegar a provider (`sessao_uso.adquirir(dono, ttl_segundos=600, esperar_segundos=ESPERA_CONTA_SEGUNDOS, conta=contas.PROVIDER)`), `mover`, `liberar` no `finally`. Gravar `itens_json` depois de **cada** resultado. Conta ocupada (admin ou provider) → `esperando`, sem tentativa. Em `enviar_stokki`, `abrir_admin` usa `DONO_TRAVA` com `esperar_segundos=ESPERA_CONTA_SEGUNDOS, alternativa=True`; sem pendente não abre sessão nenhuma (mesmo padrão de `processar_cancelamentos_do_ciclo`). O bloco no `ciclo` fica em `try/except Exception` com `logger.error`, igual aos vizinhos.
- [ ] **Step 5:** rodar os três módulos e a suíte de `portal_cliente/test_cancelamento.py` → PASS.
- [ ] **Step 6:** Commit `Portal: worker devolve ao estoque o saldo do pedido cancelado (fila stokki_devolucoes)`.

### Task 6: Avisos (galpão e operação)

**Files:**
- Modify: `notificar_whatsapp.py`, `portal_cliente/enviar_stokki.py`
- Test: `test_notificar_whatsapp_cancelamento.py`

**Interfaces:**
- Consumes: resultado por item da Tarefa 5.
- Produces:
  - `notificar_whatsapp.texto_devolucao_galpao(id_stokki: int, embarcador: str, itens: list[dict]) -> str` — `itens = [{"sku", "nome", "movido", "destino"}]`.
  - `notificar_whatsapp.avisar_devolucao_galpao(id_stokki, embarcador, itens, config, **kw) -> str` — grupo `whatsapp_notificacoes.grupo_cancelados_id`; sem id → `"desligado"`. Origem `"devolucao_estoque"`, tipo `"devolucao"`, assinatura `f"devolucao:{id_stokki}"`.
  - `notificar_whatsapp.avisar_devolucao_pendente(id_stokki, embarcador, motivo, config, **kw) -> str` — grupo `grupo_atendimento_id`, assinatura `f"devolucao_pendente:{id_stokki}"`.
  - Em `enviar_stokki`: `_avisar_devolucao_operacao(config, id_stokki, embarcador, motivo)` — e-mail para `email.email_atendimento` (ou `email_responsavel`) com `envelope_html`/`enviar_email`, mais `avisar_devolucao_pendente`. Best-effort, como `cancelamento.avisar_operacao`.

- [ ] **Step 1: testes que falham.** Texto do galpão para PS-43101, Fabenne, dois itens: contém `PS-43101`, `FABMLT`, `4`, `FABENNE-1` e a palavra `guardar`; cabe em `MAX_MENSAGEM` com 15 itens (trunca com `… e mais N` em vez de cortar no meio de uma linha). `avisar_devolucao_galpao` sem `grupo_cancelados_id` → `"desligado"`; com id → `despachar` chamado com `grupo_id` igual ao id e a assinatura acima. `avisar_devolucao_pendente` usa `grupo_atendimento_id`.
- [ ] **Step 2:** `py -3.11 -m unittest test_notificar_whatsapp_cancelamento` → FAIL.
- [ ] **Step 3:** Implementar. Formato da mensagem do galpão:

```
Pedido cancelado PS-43101 · FABENNE — guardar de volta:
• FABMLT Fabenne Merlot — 4 un → FABENNE-1
• FABMO1 Fabenne Moscato — 4 un → FABENNE-1
```

Ligar em `processar_devolucoes_do_ciclo` como `avisar_galpao`/`avisar_operacao`; situação `"desligado"`, `"nao_enviado"` ou `"falhou"` do aviso do galpão vira `logger.warning` com o PS e o endereço (a devolução já aconteceu). Documentar a chave nova no bloco de comentário de config no topo de `notificar_whatsapp.py`.
- [ ] **Step 4:** mesmo comando e `py -3.11 -m unittest test_notificar_whatsapp` → PASS.
- [ ] **Step 5:** Commit `WhatsApp: aviso ao grupo de cancelados para guardar a caixa devolvida ao estoque`.

### Task 7: Documentação e conferência final

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (seção da Stokki: `devolucao_estoque.py`, `mover_virtual.py`; tabela `stokki_devolucoes`; passo novo no worker do portal), `.claude/skills/operar-stokki/SKILL.md` (linha nova na tabela de operações; tirar "Soltar a reserva de pedido cancelado" de "Não sabemos fazer"; corrigir a armadilha do cancelado após separação), `CLAUDE.md` só se alguma regra nova de operação surgir.

- [ ] **Step 1:** Atualizar os arquivos. O `MAPA_DO_SISTEMA.txt` pode ter edição não commitada de outra sessão no working tree principal: editar só no worktree do ramo e resolver no merge.
- [ ] **Step 2:** `py -3.11 -m py_compile` em todos os `.py` tocados; rodar todas as suítes das Tarefas 2 a 6 (stokki e raiz num comando; `portal_cliente` em outro; `painel_agentes` de dentro da pasta).
- [ ] **Step 3:** Commit `Mapa e skill operar-stokki: devolucao de estoque no cancelamento`.

### Task 8: Prova na VPS (com o Hugo, depois de push e deploy pedidos por ele)

Não é automatizável. Só começa quando o Hugo pedir deploy.

- [ ] **Step 1:** Deploy com a chave **ausente** no config da VPS; reiniciar `portal-cliente-envios`, `portal-cliente` e `painel-agentes`; conferir no log do worker que o ciclo segue normal e que `stokki_devolucoes` não recebe linhas.
- [ ] **Step 2:** Simulação: escolher com o Hugo um PS cancelado nas últimas 24 h; na VPS, como www-data, um `venv/bin/python -c` que chama `montar_plano` (só leitura) e imprime o plano. Mostrar ao Hugo: itens, quantidades, destino.
- [ ] **Step 3:** Hugo põe o número do OpenWA no grupo ❌PEDIDOS CANCELADOS❌ e o id em `whatsapp_notificacoes.grupo_cancelados_id`.
- [ ] **Step 4:** Caso real acompanhado: pedido do cliente teste (CNPJ 00.000.000/0001-91), separado pelo galpão e cancelado pelo portal, com a chave ligada só para esse teste. Conferir na Stokki: "Desbloqueio | Interno" da quantidade certa, saldo disponível no destino, Virtual dos outros pedidos igual ao de antes, mensagem no grupo.
- [ ] **Step 5:** Hugo decide ligar em definitivo.
