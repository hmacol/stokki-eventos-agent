# Tela /pedidos: uma linha por pedido + ações — Plano de execução (Fases 0 e 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A /pedidos passa a ter uma linha por pedido (original + reentregas juntos, com selo "N tentativas ▾") e um menu de ações por linha e em lote: Reenvio, Cancelar Vuupt, Cancelar Stokki, Alterar endereço, Agendar, Ver canhoto e Dar baixa com canhoto.

**Architecture:** O agrupamento fica no SQL de `nucleo/tabela_pedidos.py` (CTE com `ROW_NUMBER` por embarcador + código base), então paginação, ordenação e filtros continuam no banco. As ações moram num módulo novo, `painel_agentes/acoes_pedidos.py`. Ele confere o status na Vuupt na hora e chama as funções que já rodam no portal, na Torre e no Planejamento; `painel_agentes.py` só ganha rotas finas. A tela continua Jinja com JS solto, sem framework.

**Tech Stack:** Python 3.11, Flask, SQLite (funções de janela), unittest, JS puro.

**Spec:** `docs/superpowers/specs/2026-11-10-acoes-tela-pedidos-design.md` (ler inteiro antes de começar). Fases 2 (nota fiscal) e 3 (trocar canhoto, ramo `correcao-canhoto`) ficam para planos próprios. Nesta entrega os itens delas aparecem desabilitados com a dica "em breve".

## Global Constraints

- Worktree `C:\agente_stokki_eventos\.claude\worktrees\tela-pedidos`, ramo `tela-pedidos` (base origin/master c76e0ae). Python **sempre** `py -3.11`.
- Só unittest. Testes de `nucleo` rodam da raiz do worktree (`py -3.11 -m unittest nucleo.test_x -v`). Testes de `painel_agentes` rodam **de dentro de** `painel_agentes/` (`py -3.11 -m unittest test_x -v`). Nunca os dois no mesmo comando.
- Teste da tela: banco temporário com patch de `banco.DB_PATH`, `painel_agentes.acesso_telas.DB_PATH` e `painel_agentes.contadores_menu.contadores` (copiar o setUp de `painel_agentes/test_pedidos_tabela.py`). Vuupt e Stokki **sempre mockadas**. Nunca abrir `config.yaml` nem logar na Stokki pelo notebook.
- Níveis que agem: `("total", "operador", "logistica", "atendimento")`. `leitura` vê a tela sem menu e sem caixinhas.
- Toda rota nova: `POST` + `@requer_auth(niveis=NIVEIS_ACAO)` + `@exige_mesma_origem`, resposta sempre JSON (`try/except` → `{"erro": str}` com 400/500, nunca HTML cru).
- Tratativas: `tratativas.registrar_evento(codigo, "PEDIDOS", EVENTO, service_id=..., texto=f"{usuario}: ...")`. A tabela não tem coluna de usuário, então o usuário vai sempre no começo do texto.
- Nenhuma ação manda e-mail ou WhatsApp.
- Comentários e logs sem acento nos arquivos que já seguem esse padrão (`tabela_pedidos.py` e `painel_agentes.py` usam acento; `stokki/cancelar.py` não usa).
- `MAPA_DO_SISTEMA.txt` atualizado no mesmo commit que cria arquivo ou rota.
- Commit: `git add` só dos arquivos da tarefa, mensagem `Área: o que mudou e por quê`, terminando com a linha `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Sem push e sem deploy: isso só quando o Hugo pedir.

## Review Focus

1. **Duas empresas com o mesmo código PS** (raro, mas existe com código manual). A chave do grupo é `(COALESCE(sender_id,-1), base)`, nunca só `base`. O teste está na Task 1.
2. **Status do núcleo atrasado em relação à Vuupt** (até 15 min). Toda ação relê o serviço com `VuuptClient.buscar_servico_por_id` antes de agir. Se vier `None`, a resposta é "não achei o serviço na Vuupt agora, tente de novo" e nada é feito. Os testes estão nas Tasks 4, 5 e 7.
3. **Clique duplo ou lote repetido** no Reenvio. A trava `fingerprint_duplicacao_insucesso.ja_duplicado(service_id)` continua valendo, e o segundo clique recebe "já foi duplicado (→ PS-…-R1)". O teste está na Task 4.
4. **Stokki ocupada** (outra rotina com a sessão). Cancelar Stokki e o Reenvio, que confere a Stokki, devolvem erro claro, não esperam mais que 60 s e **não** criam nem cancelam nada sem conferir. Os testes estão nas Tasks 4 e 6.
5. **Selo aberto some ao paginar ou filtrar.** As chaves abertas ficam em `sessionStorage` (`pedidos.tentativas.abertas`), protegido por try/catch. A verificação manual está na Task 3.

---

### Task 1: Uma linha por pedido no núcleo (`listar`)

**Files:**
- Modify: `nucleo/tabela_pedidos.py` (`_SQL_BASE`, `COLUNAS`, `_where`, `listar`, nova `tentativas`)
- Test: `nucleo/test_tabela_pedidos.py`

**Interfaces:**
- Produces:
  - `COLUNAS["codigo"]` passa a ter `sql="pedido_base"` (código base). Coluna nova `Coluna("codigo_servico", "Código do serviço", "codigo")`, não visível por padrão. A linha devolvida por `listar` traz também `sender_id`, `pedido_base`, `service_status` (= `status` da linha principal) e `vuupt_service_id`.
  - `tentativas(chaves: list[tuple[int | None, str]], conn) -> dict[tuple[int | None, str], list[dict]]`. Cada item é `{"data", "codigo", "situacao", "motivo", "rota", "rota_id", "motorista", "conclusao"}`, em ordem crescente de data.
  - `listar(...)` devolve cada linha com `"tentativas": list[dict]` (só os grupos da página, numa segunda consulta).

- [ ] **Step 1: Testes que falham** (em `TestListar`, usando os helpers `_pedido`, `_rota` e `_parada`; o helper `_pedido` ganha o parâmetro `reentrega_de=None`):

```python
def test_reentrega_vira_uma_linha_com_a_tentativa_mais_recente(self):
    self._pedido("PS-1", status="INSUCESSO")
    self._parada(self._rota(_dia(-2)), "PS-1", situacao="INSUCESSO")
    self._pedido("PS-1-R1", status="EM_ROTA", reentrega_de="PS-1")
    self._parada(self._rota(_dia(-1), nome="Rota R1"), "PS-1-R1")
    linhas = tp.listar({}, conn=self.conn)["linhas"]
    self.assertEqual([(l["codigo"], l["codigo_servico"], l["status"]) for l in linhas],
                     [("PS-1", "PS-1-R1", "EM_ROTA")])

def test_entregue_vence_mesmo_mais_antigo(self):
    self._pedido("PS-1", status="ENTREGUE"); self._parada(self._rota(_dia(-3)), "PS-1")
    self._pedido("PS-1-R1", status="ABERTO", criado_provedor=f"{_dia(-1)} 09:00:00")
    self.assertEqual([l["codigo_servico"] for l in tp.listar({}, conn=self.conn)["linhas"]], ["PS-1"])

def test_mesmo_dia_reentrega_aberta_vence_insucesso(self):
    self._pedido("PS-1", status="INSUCESSO", criado_provedor=f"{_dia(0)} 07:00:00")
    self._pedido("PS-1-R1", status="ABERTO", criado_provedor=f"{_dia(0)} 15:00:00")
    self.assertEqual([l["codigo_servico"] for l in tp.listar({}, conn=self.conn)["linhas"]], ["PS-1-R1"])

def test_empresas_diferentes_com_mesmo_codigo_nao_se_misturam(self):
    self._pedido("PS-1", sender_id=10); self._pedido("PS-1-R1", sender_id=20)
    self.assertEqual(len(tp.listar({}, conn=self.conn)["linhas"]), 2)

def test_busca_por_codigo_da_reentrega_acha_o_grupo(self):
    self._pedido("PS-1", status="INSUCESSO"); self._pedido("PS-1-R1", status="ABERTO")
    self.assertEqual(self._codigos({"q": "PS-1-R1"}), ["PS-1"])

def test_total_e_caixas_contam_pedidos(self):
    self._pedido("PS-1", status="INSUCESSO", caixas=4); self._pedido("PS-1-R1", status="ABERTO", caixas=4)
    r = tp.listar({}, conn=self.conn)
    self.assertEqual((r["total"], r["total_caixas"]), (1, 4))

def test_tentativas_do_grupo(self):
    self._pedido("PS-1", status="INSUCESSO")
    self._parada(self._rota(_dia(-2), nome="A"), "PS-1", situacao="INSUCESSO", motivo="Ausente")
    self._parada(self._rota(_dia(-2), nome="B"), "PS-1", situacao="CANCELADA")   # remanejo: não conta
    self._pedido("PS-1-R1", status="ABERTO")
    t = tp.listar({}, conn=self.conn)["linhas"][0]["tentativas"]
    self.assertEqual([(x["codigo"], x["situacao"], x["motivo"]) for x in t],
                     [("PS-1", "INSUCESSO", "Ausente"), ("PS-1-R1", "ABERTO", None)])
```

O helper `_parada` ganha o parâmetro `situacao="ENTREGUE"`, e `_pedido` ganha `reentrega_de` (grava `reentrega_de_codigo`). Os testes existentes continuam passando sem mudança: pedido sem `-R` tem código base igual ao código.

- [ ] **Step 2:** `py -3.11 -m unittest nucleo.test_tabela_pedidos -v`. Esperado: os 7 testes novos FALHAM, os antigos passam.

- [ ] **Step 3: Implementar** em `nucleo/tabela_pedidos.py`:
  - Na CTE `base`, adicionar `pedido_base` = `UPPER(LTRIM(codigo,'# '))` cortado no primeiro `-R` seguido de dígito (sem `-R`, o código inteiro). Adicionar também `sufixo_r` = número depois do `-R`, ou 0.
  - Peso da linha principal: `ENTREGUE` 5, `EM_ROTA` 3, `ABERTO` 2, `INSUCESSO` 1, `CANCELADO` 0.
  - Nova CTE `rank` sobre `base` com:
    - `ROW_NUMBER() OVER (PARTITION BY COALESCE(sender_id,-1), pedido_base ORDER BY (status='ENTREGUE') DESC, data_referencia DESC, peso DESC, sufixo_r DESC) AS rn_grupo`;
    - `group_concat(codigo, ' ') OVER (PARTITION BY COALESCE(sender_id,-1), pedido_base) AS codigos_grupo`.
  - A CTE final, que `_select`, `_where` e a contagem leem, continua se chamando `base` e passa a ser `SELECT * FROM rank WHERE rn_grupo = 1`. Assim `_where`, `_order_by` e `_select` não mudam, exceto a busca livre: acrescentar `codigos_grupo` à lista `campos`.
  - `_select` devolve também `sender_id`, `pedido_base` e `vuupt_service_id`, além de `rota_id`.
  - `tentativas(chaves, conn)`: uma consulta, em blocos de 500 chaves:
    - `nucleo_paradas` + `nucleo_rotas` dos códigos do grupo com `situacao IN ('ENTREGUE','PARCIAL','INSUCESSO')`;
    - mais um item por serviço de `nucleo_pedidos` com `status IN ('ABERTO','EM_ROTA','CANCELADO')`, que representa o estado atual desse serviço (data = `data_referencia`);
    - deduplicar por `(data, codigo, situacao, motivo)` e ordenar por data.
    - "Não coletado" entra como qualquer insucesso: não filtrar.

- [ ] **Step 4:** Mesmo comando. Esperado: todos OK.

- [ ] **Step 5:** Commit `nucleo/tabela_pedidos.py nucleo/test_tabela_pedidos.py`, com a mensagem "Pedidos: uma linha por pedido (original e reentregas juntos), como o portal (Hugo, 10/11)".

### Task 2: CSV com histórico de tentativas

**Files:**
- Modify: `nucleo/tabela_pedidos.py` (`exportar_csv`)
- Test: `nucleo/test_tabela_pedidos.py`

**Interfaces:**
- Consumes: `tentativas(...)` da Task 1.
- Produces: o CSV ganha sempre a última coluna `Histórico de tentativas`, no formato do xlsx do portal: `"dd/mm Situação (motivo)"` separados por `"; "`.

- [ ] **Step 1: Teste que falha**:

```python
def test_csv_tem_historico_de_tentativas(self):
    self._pedido("PS-1", status="INSUCESSO")
    self._parada(self._rota(_dia(-1)), "PS-1", situacao="INSUCESSO", motivo="Ausente")
    self._pedido("PS-1-R1", status="ENTREGUE")
    self._parada(self._rota(_dia(0)), "PS-1-R1", situacao="ENTREGUE")
    linhas = tp.exportar_csv({}, ["codigo"], conn=self.conn).lstrip("﻿").splitlines()
    ontem, hoje = (HOJE - timedelta(days=1)).strftime("%d/%m"), HOJE.strftime("%d/%m")
    self.assertEqual(linhas, ["Código;Histórico de tentativas",
                              f"PS-1;{ontem} Insucesso (Ausente); {hoje} Entregue"])
```

Atualizar `test_csv_traz_tudo_do_filtro_nas_colunas_pedidas`:
- o cabeçalho esperado passa a ser `"Código;Status;Histórico de tentativas"`, e cada linha termina em `;` mais o histórico (`;` vazio quando não há tentativa);
- `cabecalho.count(";") == len(tp.COLUNAS)`.

- [ ] **Step 2:** `py -3.11 -m unittest nucleo.test_tabela_pedidos -v`. Esperado: FALHA nos dois.
- [ ] **Step 3:** Em `exportar_csv`, depois de buscar as linhas, chamar `tentativas` das chaves `(sender_id, pedido_base)` e escrever a coluna extra. O texto também passa por `_celula_csv`, tratado como texto livre.
- [ ] **Step 4:** Mesmo comando. Esperado: OK.
- [ ] **Step 5:** Commit, com a mensagem "Pedidos: CSV com histórico de tentativas por pedido".

### Task 3: Selo "N tentativas ▾" na tela

**Files:**
- Modify: `painel_agentes/templates/pedidos_tabela.html`
- Test: `painel_agentes/test_pedidos_tabela.py`

**Interfaces:**
- Consumes: `l.tentativas`, `l.sender_id`, `l.pedido_base` da Task 1.

- [ ] **Step 1: Teste que falha** em `TestTelaPedidos`:
  - inserir `PS-3` INSUCESSO e `PS-3-R1` ABERTO, os dois com `sender_id=10`;
  - fazer `GET /pedidos?q=PS-3`;
  - `assertEqual(html.count('data-tentativas="10|PS-3"'), 1)`;
  - `assertIn("2 tentativas", html)`;
  - `assertIn('class="tentativas"', html)` (linha de histórico renderizada e escondida).
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_pedidos_tabela -v`. Esperado: FALHA.
- [ ] **Step 3: Implementar** no template, copiando o visual do portal (`portal_cliente/templates/acompanhamento.html`: CSS `.selo-tent` e `tr.tentativas`, linhas 81-87 e 490-517):
  - **Selo:** na célula `status`, depois do chip, um `<button type="button" class="selo-tent" data-tentativas="{{ l.sender_id }}|{{ l.pedido_base }}" aria-expanded="false">N tentativas ▾</button>`, só quando `l.tentativas|length > 1`.
  - **Linha do histórico:** logo depois de cada `<tr>` com selo, `<tr class="tentativas" data-de="{{ chave }}" hidden><td colspan="{{ colunas|length }}"><ul>…</ul></td></tr>`. Cada `<li>` traz:
    - dia `dd/mm`;
    - código do serviço como link para `consulta_pedido`;
    - situação formatada (com a classe `falha` no insucesso);
    - motivo, rota (link para `consulta_rota` quando houver `rota_id`), motorista e conclusão `HH:MM`, separados por `·`.
  - **Abrir e fechar:** o clique no selo alterna `hidden` e `aria-expanded`. As chaves abertas ficam em `sessionStorage["pedidos.tentativas.abertas"]` (lista JSON, try/catch na leitura e na escrita) e são reabertas no carregamento.
- [ ] **Step 4:** O mesmo teste passa. Verificação manual:
  - subir o painel na porta 8099 (`py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"` de dentro de `painel_agentes/`);
  - abrir `/pedidos` com o banco local, abrir um selo, trocar de página e voltar: o selo continua aberto.
- [ ] **Step 5:** Commit do template e do teste, com a mensagem "Pedidos: selo de tentativas abre o histórico do pedido".

### Task 4: Reenvio

**Files:**
- Create: `painel_agentes/acoes_pedidos.py`, `painel_agentes/test_acoes_pedidos.py`
- Modify: `painel_agentes/torre_controle.py` (`duplicar_pedido_manual`, ~l.246)
- Modify: `painel_agentes/painel_agentes.py` (rota nova)
- Modify: `painel_agentes/acesso_telas.py:58`

**Interfaces:**
- Produces, em `acoes_pedidos.py`:
  - `NIVEIS_ACAO = ("total", "operador", "logistica", "atendimento")`.
  - `VIVOS_VUUPT_FIM = ("done", "failed", "canceled")` (status em que o serviço não está mais vivo).
  - `servicos_do_grupo(conn, service_id: int) -> list[dict]`: as linhas de `nucleo_pedidos` com o mesmo `(sender_id, código base)` do serviço. Cada linha traz `vuupt_service_id`, `codigo` e `status`.
  - `situacao_stokki(codigo: str) -> str | None`: situação na Stokki do primeiro `PS-<id>` do código, pela conta admin. Pega a trava `sessao_uso.adquirir("painel-pedidos-acao", ttl_segundos=300, esperar_segundos=60, alternativa=True)`, chama `stokki.cancelar.situacao_e_token` e faz `liberar` no `finally`. Devolve `None` se a Stokki estiver ocupada.
  - `reenviar(service_id: int, motivo: str, usuario: str, *, vuupt=None, stokki=situacao_stokki) -> dict` → `{"ok": bool, "novo_code": str, "erro": str}`.
- Produces, em `torre_controle.py`: `duplicar_pedido_manual(service_id, codigo, motorista=None, rota=None, *, origem="TORRE", texto=None, servico_original=None, marcar_tratado=True)`. Os valores padrão mantêm a Torre igual. Com `servico_original` não busca por `code`; com `marcar_tratado=False` não chama `marcar_excecao_tratada`.
- Rota: `POST /api/pedidos/acao/reenvio` com JSON `{service_id, motivo}`.

- [ ] **Step 1: Testes que falham** (`test_acoes_pedidos.py`, banco temporário com `nucleo_pedidos`; `VuuptClient` falso com `buscar_servico_por_id`; `torre_controle.duplicar_pedido_manual` mockado):
  - `test_reenvio_de_entregue_cria_e_registra_motivo`: serviço `done`, Stokki `"Enviado"` → `ok`. A chamada a `duplicar_pedido_manual` recebe `origem="PEDIDOS"`, `marcar_tratado=False`, `servico_original` igual ao serviço lido e texto contendo `"hugo: "` e o motivo.
  - `test_reenvio_de_insucesso_marca_tratado`: serviço `failed` → `marcar_tratado=True`.
  - `test_recusa_pedido_aberto`: serviço `not_assigned` → `ok False`, `"ainda aberto"` no erro, nada duplicado.
  - `test_recusa_quando_outra_tentativa_do_grupo_esta_viva`: linha `PS-1` `done`, mas `PS-1-R1` no núcleo e `on_route` na Vuupt → erro citando `PS-1-R1`.
  - `test_recusa_cancelado_na_stokki`: `stokki` devolve `"Cancelado"` → erro `"cancelado na Stokki"`.
  - `test_stokki_ocupada_nao_cria`: `stokki` devolve `None` → erro `"não deu pra conferir a Stokki"`.
  - `test_motivo_curto_ou_longo`: `"abc"` ou 201 caracteres → erro, sem chamar a Vuupt.
  - `test_servico_sumiu_da_vuupt`: `buscar_servico_por_id` devolve `None` → erro, nada feito.
  - `test_segundo_clique_ja_duplicado`: `duplicar_pedido_manual` devolve `{"ok": False, "erro": "Esse pedido já foi duplicado antes (→ PS-1-R1)."}` → esse erro é repassado.
  - Em `test_pedidos_tabela.py`: `POST /api/pedidos/acao/reenvio` como `leitura` → status diferente de 200. Como `atendimento`, com `acoes_pedidos.reenviar` mockado → 200 com JSON.
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_acoes_pedidos test_pedidos_tabela -v`. Esperado: FALHA (módulo não existe).
- [ ] **Step 3: Implementar.**
  - **Regras do `reenviar`, nesta ordem:**
    1. motivo com 5 a 200 caracteres (depois do `strip`);
    2. serviço lido na Vuupt;
    3. status dele em `VIVOS_VUUPT_FIM`, senão erro "pedido ainda aberto: já existe um serviço vivo";
    4. cada outro serviço de `servicos_do_grupo` lido na Vuupt: qualquer um fora de `VIVOS_VUUPT_FIM` dá erro "a tentativa <codigo> ainda está aberta na Vuupt";
    5. `stokki(codigo)`: `None` dá erro "não deu pra conferir a Stokki, tente de novo"; texto com `cancel` dá erro "pedido cancelado na Stokki";
    6. `duplicar_pedido_manual(...)` com `texto=f"{usuario}: Reenvio pela tela Pedidos → {{novo}}: {motivo}"`.
  - **No `torre_controle.py`:** a tratativa `REENVIO_MANUAL` usa o `origem` e o `texto` recebidos. Quando `texto` vier, ele é montado depois de conhecer `novo_code` (aceitar um texto com o marcador `{novo}`).
  - **Acesso:** em `acesso_telas.py`, a entrada `pedidos_tabela` ganha `r"^/api/pedidos/"`.
  - **MAPA:** linha do `acoes_pedidos.py` e da rota.
- [ ] **Step 4:** Mesmo comando. Esperado: OK. Rodar também `py -3.11 -m unittest test_acesso_telas -v` (de `painel_agentes/`): OK.
- [ ] **Step 5:** Commit dos arquivos da tarefa e do MAPA, com a mensagem "Pedidos: Reenvio pela tela, qualquer status menos aberto, confere Stokki (Hugo, 10/11)".

### Task 5: Cancelar Vuupt

**Files:** Modify `painel_agentes/acoes_pedidos.py`, `painel_agentes/painel_agentes.py`; Test `painel_agentes/test_acoes_pedidos.py`

**Interfaces:**
- Produces: `cancelar_vuupt(service_id: int, usuario: str, *, token: str, agent_lalamove: int = 0, cancelar=cancelar_servico_completo) -> dict` → `{"ok", "ja_estava", "erro", "aviso"}`. `agent_lalamove` vem de `config["lalamove"]["agent_id_vuupt"]`, como em `portal_cliente/solicitacoes_execucao.py:398`.
- Rota: `POST /api/pedidos/acao/cancelar-vuupt` com JSON `{service_id}`.

- [ ] **Step 1: Testes que falham:**
  - `test_cancela_e_registra`: `cancelar` falso devolve ok → tratativa `CANCELADO_MANUAL` com `"hugo: "` no texto.
  - `test_motorista_a_caminho_repassa_recusa`: `cancelar` devolve `{"ok": False, "erro": "servico 1 esta 'on_route' ..."}` → `ok False` e erro em português: "motorista a caminho ou no local: fale com ele antes de cancelar".
  - `test_lalamove_avisa`: serviço com `agent_id == agent_lalamove` no núcleo (última rota) → `aviso` contém "Lalamove".
- [ ] **Step 2:** Rodar `test_acoes_pedidos`. Esperado: FALHA.
- [ ] **Step 3:** Implementar. `from roteirizacao.cancelar_servico import cancelar_servico_completo`: ele já relê o status, acha o rascunho e tira a parada. A recusa por andamento é reconhecida por `"on_route"` ou `"arrived"` no erro.
- [ ] **Step 4:** Rodar de novo. Esperado: OK.
- [ ] **Step 5:** Commit, com a mensagem "Pedidos: Cancelar Vuupt pela tela (bloqueia motorista a caminho, Hugo 10/11)".

### Task 6: Cancelar Stokki

**Files:** Modify `stokki/cancelar.py`, `stokki/test_cancelar.py`, `painel_agentes/acoes_pedidos.py`, `painel_agentes/painel_agentes.py`; Test `painel_agentes/test_acoes_pedidos.py`

**Interfaces:**
- Produces, em `stokki/cancelar.py`:
  - `montar_motivo(motivo_cliente: str, prefixo: str = PREFIXO_MOTIVO) -> str`;
  - `cancelar_pedido(sessao, id_stokki, motivo, dormir=time.sleep, prefixo: str = PREFIXO_MOTIVO)`.
  - Os padrões mantêm o portal igual.
- Produces, em `acoes_pedidos.py`: `cancelar_stokki(codigo: str, motivo: str, usuario: str, *, abrir_sessao=None) -> dict` → `{"ok", "resultados": [{"id_stokki", "ok", "situacao", "erro"}], "erro"}`. `abrir_sessao()` devolve um gerenciador de contexto que pega a trava e entrega a `StokkiSession`, ou levanta `RuntimeError("Stokki ocupada por <dono>")`.
- Rota: `POST /api/pedidos/acao/cancelar-stokki` com JSON `{codigo, motivo}`. Não exige `service_id`.

- [ ] **Step 1: Testes que falham:**
  - Em `stokki/test_cancelar.py`: `montar_motivo("teste de motivo longo", prefixo="Fresh Hub Pedidos (hugo)")` == `"Fresh Hub Pedidos (hugo): teste de motivo longo"`. E o padrão continua `"Portal Fresh Hub: ..."`.
  - `test_cancela_todos_os_ps_do_codigo`: `"PS-1, PS-2"` → `cancelar_pedido` chamado com 1 e 2, com `prefixo="Fresh Hub Pedidos (hugo)"`.
  - `test_reentrega_cancela_o_base`: `"PS-7-R1"` → id 7.
  - `test_motivo_menor_que_15`: erro, sem abrir sessão.
  - `test_stokki_ocupada`: `abrir_sessao` levanta `RuntimeError` → `ok False`, erro "Stokki ocupada…, tente em alguns minutos".
  - `test_registra_tratativa_com_situacao`: tratativa `CANCELADO_STOKKI` com usuário, motivo e situação depois.
- [ ] **Step 2:** `py -3.11 -m unittest stokki.test_cancelar -v` (raiz) e `test_acoes_pedidos` (de `painel_agentes/`). Esperado: FALHA.
- [ ] **Step 3: Implementar.**
  - **Ids:** `re.findall(r"PS-(\d+)", codigo.upper())`. Não importar `portal_cliente.cancelamento`: ele puxa o envio de pedidos e o e-mail.
  - **`abrir_sessao` padrão:** `sessao_uso.adquirir("painel-pedidos-acao", ttl_segundos=300, esperar_segundos=60, alternativa=True)`, depois `StokkiSession(config, conta=conta)` e `liberar` no `finally`. Mesmo padrão de `portal_cliente/solicitacoes_execucao.py:402-421`.
  - **Rota:** a rota do Flask passa `usuario=session["usuario"]`.
  - **Tempo:** a chamada leva até ~30 s. A rota não muda o timeout. O lote é feito pelo front, um pedido por vez (Task 9).
- [ ] **Step 4:** Rodar de novo. Esperado: OK.
- [ ] **Step 5:** Commit, com a mensagem "Pedidos: Cancelar Stokki pela tela, separado da Vuupt (Hugo, 10/11)".

### Task 7: Alterar endereço e Agendar

**Files:** Modify `painel_agentes/acoes_pedidos.py`, `painel_agentes/painel_agentes.py`; Test `painel_agentes/test_acoes_pedidos.py`

**Interfaces:**
- Produces:
  - `alterar_endereco(service_id: int, endereco: str, usuario: str, *, vuupt=None) -> dict`: confere que o status vivo não é `done` nem `canceled`, pega o rascunho com `roteirizacao.cancelar_servico.rascunho_do_servico` e chama `planejamento_rotas.editar_endereco_pedido(service_id, endereco, rascunho_id)`. Registra a tratativa `ENDERECO_ALTERADO` com o endereço novo.
  - `agendar(service_id: int, data: str, hora_inicio: str, hora_fim: str, usuario: str, *, vuupt=None) -> dict`: mesma checagem de status, chama `planejamento_rotas.reagendar_pedido` e registra a tratativa `REAGENDADO` com `dd/mm HH:MM–HH:MM`.
- Rotas:
  - `POST /api/pedidos/acao/endereco` com `{service_id, endereco}`;
  - `POST /api/pedidos/acao/agendar` com `{service_id, data, hora_inicio, hora_fim}`;
  - `POST /api/pedidos/acao/checar-dia-fixo` com `{service_ids, data}`, que chama `checar_reagendamento_dia_fixo`, igual a `api_checar_dia_fixo`.

- [ ] **Step 1: Testes que falham:**
  - `test_endereco_passa_rascunho_e_registra`: `rascunho_do_servico` mockado devolve `(55, "RASCUNHO")` → `editar_endereco_pedido(1, "Rua X, 10", 55)`.
  - `test_endereco_recusa_entregue`: Vuupt `done` → erro, nada chamado.
  - `test_agendar_registra_janela`: tratativa com o texto `"hugo: agendado 12/11 08:00–12:00"`.
  - `test_agendar_recusa_cancelado`.
  - `test_servico_sumiu_nao_altera`: `buscar_servico_por_id` devolve `None` → erro "não achei o serviço na Vuupt agora", nem `editar_endereco_pedido` nem `reagendar_pedido` chamados.
- [ ] **Step 2:** Rodar. Esperado: FALHA.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** Rodar. Esperado: OK.
- [ ] **Step 5:** Commit, com a mensagem "Pedidos: alterar endereço e agendar pela tela".

### Task 8: Canhoto — ver/baixar e dar baixa

**Files:** Modify `painel_agentes/acoes_pedidos.py`, `painel_agentes/painel_agentes.py`; Test `painel_agentes/test_acoes_pedidos.py`, `painel_agentes/test_pedidos_tabela.py`

**Interfaces:**
- Produces:
  - `comprovantes(codigo_servico: str, *, token: str, conn) -> list[dict]`. Cada item é `{"origem": "VUUPT" | "MANUAL" | "APP", "rotulo": str, "url": str}`, nesta ordem:
    - **VUUPT:** `notificacao_entregas.vuupt_entregas.buscar_por_codigo(token, codigo)` → para cada serviço `done` com checklist com foto, o checklist id. A extração é igual a `portal_cliente/dados_cliente.checklist_id_do_servico` (l.829), copiada como `_checklist_id` local, com comentário apontando a origem, porque importar o portal puxa o app dele. URL `/pedidos/canhoto/vuupt/<checklist_id>.pdf?codigo=<codigo>`.
    - **MANUAL:** `nucleo.canhotos_manuais.caminho_canhoto_manual(codigo)` → `/pedidos/canhoto/manual/<codigo>.pdf`.
    - **APP:** linhas de `nucleo_comprovantes` do código → `url_for("canhoto_foto", comprovante_id=...)`.
  - Rotas GET:
    - `/api/pedidos/acao/comprovantes?codigo=` → JSON;
    - `/pedidos/canhoto/vuupt/<int:checklist_id>.pdf` → `vuupt_entregas.baixar_canhoto_pdf(token, id, codigo)` e `send_file`;
    - `/pedidos/canhoto/manual/<codigo>.pdf`, com o código validado por `^PS-\d+(-[RC]\d+)?$` antes de montar o caminho.
  - Níveis das três: `NIVEIS_ACAO + ("leitura",)`, porque só leem.
  - Baixa:
    - `POST /api/pedidos/acao/marcar-entregue` (multipart, mesmos campos da Torre). O corpo de `api_torre_marcar_entregue` (painel_agentes.py ~l.2523) vira um helper `_marcar_entregue_pelo_form()` chamado pelas duas rotas; a da Torre não muda de comportamento.
    - `POST /api/pedidos/acao/validar-lote-foto` chama `torre_controle.validar_lote_foto`, com a mesma resposta da Torre.
  - Acesso: `acesso_telas` também cobre `^/pedidos/canhoto/`. Isso já cabe em `^/pedidos(/|$)`; conferir no teste.

- [ ] **Step 1: Testes que falham:**
  - `test_comprovantes_junta_as_tres_origens`: Vuupt falsa com um `done` e checklist `images_quantity=1`, um canhoto manual gravado pelo `canhotos_manuais.salvar` no banco temporário e uma linha em `nucleo_comprovantes`. Esperado: as origens `["VUUPT", "MANUAL", "APP"]`.
  - `test_sem_canhoto_lista_vazia`.
  - `test_canhoto_manual_rejeita_codigo_estranho`: `GET /pedidos/canhoto/manual/..%2Fconfig.pdf` → 404.
  - `test_marcar_entregue_pela_tela_chama_a_torre`: `atendimento`, com `torre_controle.marcar_entregue_manual` mockado → 200.
  - `test_torre_continua_igual`: `POST /api/torre/marcar-entregue` como `logistica` → mesma resposta de antes. Como `atendimento` → recusado, porque o nível da Torre não muda.
- [ ] **Step 2:** Rodar. Esperado: FALHA.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** Rodar `test_acoes_pedidos test_pedidos_tabela`. Rodar também a suíte da Torre: `py -3.11 -m unittest discover -p "test_torre*"` de `painel_agentes/`. Esperado: tudo OK, ou só as falhas que já existiam (anotar antes de mexer, rodando a suíte na base).
- [ ] **Step 5:** Commit, com a mensagem "Pedidos: ver canhoto (Vuupt, manual, app) e dar baixa com canhoto pela tela".

### Task 9: Menu de ações, caixinhas e lote na tela

**Files:**
- Modify: `painel_agentes/templates/pedidos_tabela.html`, `painel_agentes/painel_agentes.py` (passar `pode_agir` ao template)
- Reuse: `painel_agentes/templates/_reduzir_imagem.js.html` (foto ≤2000 px)
- Test: `painel_agentes/test_pedidos_tabela.py`, verificação no navegador (Playwright local, ver memória `feedback_testar_painel_playwright_local`)

**Interfaces:**
- Consumes: as rotas das Tasks 4 a 8, todas devolvendo `{"ok", "erro", ...}`.
- Produces: `pode_agir = session["nivel_acesso"] in NIVEIS_ACAO` no contexto do template.

- [ ] **Step 1: Testes que falham:**
  - como `leitura`: o HTML **não** tem `class="menu-acoes"` nem `name="sel"`;
  - como `operador`: tem os dois. O `data-service-id`, o `data-codigo` (código do serviço da linha principal) e o `data-status` aparecem em cada `<tr>`.
- [ ] **Step 2:** Rodar `test_pedidos_tabela`. Esperado: FALHA.
- [ ] **Step 3: Implementar.**
  - **Coluna fixa à esquerda** (`position: sticky`), com a caixinha `name="sel"` e o botão `⋯` (`class="menu-acoes"`, `aria-haspopup="menu"`).
  - **Itens do menu:** Reenvio · Cancelar Vuupt · Cancelar Stokki · Alterar endereço · Agendar · Ver canhoto · Dar baixa com canhoto · Trocar canhoto (desabilitado, "em breve") · Nota fiscal (desabilitado, "em breve").
  - **Item desabilitado pelo status do núcleo**, com `title` explicando:
    - Reenvio só com ENTREGUE, INSUCESSO ou CANCELADO;
    - Cancelar Vuupt, Endereço e Agendar não com ENTREGUE nem CANCELADO;
    - Dar baixa só com EM_ROTA;
    - linha sem `vuupt_service_id` desabilita tudo que é Vuupt, mas mantém Cancelar Stokki.
  - **Modais:**
    - Reenvio: motivo com 5 a 200 caracteres, mostrando código e destinatário.
    - Cancelar Vuupt: confirmação.
    - Cancelar Stokki: motivo com no mínimo 15 caracteres, mais os três avisos do spec: estoque no Virtual, reentrega cancela o base, Vuupt ainda viva.
    - Endereço: um campo de texto.
    - Agendar: data, início e fim. Chama `checar-dia-fixo` antes e mostra o aviso.
    - Dar baixa: foto do canhoto e nota de retorno opcional, ou a justificativa da lista `nucleo/justificativas_sem_canhoto.py`, como no modal da Torre.
    - Ver canhoto: abre a lista de `comprovantes`, cada item em aba nova.
  - **Barra de lote** (aparece com 1 ou mais selecionados): "N selecionados" + Reenvio, Cancelar Vuupt, Cancelar Stokki, Agendar, Endereço, Dar baixa.
    - Um modal único por ação, que vale para todos.
    - Um `fetch` por pedido, em sequência, com barra de progresso "k de N". Falha não para o lote.
    - No fim, um resumo com as falhas por código.
    - Dar baixa com foto chama `validar-lote-foto` antes e manda `lote_referencia`, como o Planejamento.
  - **Resultado na linha:** depois de cada sucesso, um selo na própria linha ("Cancelado agora", "Reenvio → PS-N-R1", "Agendado 12/11 08:00–12:00", "Endereço alterado", "Baixa dada"), sem recarregar a página. Fica junto a nota "o status atualiza na próxima sincronização (até 15 min)".
  - **CSRF/origem:** usar o mesmo `fetch` com `credentials: "same-origin"` e os cabeçalhos que o Planejamento usa.
- [ ] **Step 4:** Rodar `test_pedidos_tabela`: OK. Depois, no navegador, com o painel na porta 8099 e as rotas `/api/pedidos/acao/**` interceptadas pelo Playwright devolvendo `{"ok": true}`:
  - abrir o menu, confirmar cada modal e ver o selo na linha;
  - selecionar 3 linhas e fazer um lote com uma falha simulada: o resumo cita o código que falhou;
  - entrar como `leitura`: não aparece menu.
  - Tirar print.
- [ ] **Step 5:** Atualizar o `MAPA_DO_SISTEMA.txt` (tela /pedidos: uma linha por pedido, ações e rotas `/api/pedidos/acao/*`). Commit, com a mensagem "Pedidos: menu de ações e lote na tela (Hugo, 10/11)".

### Task 10: Fechamento

- [ ] **Step 1:** `py -3.11 -m py_compile` em todos os `.py` alterados.
- [ ] **Step 2:** Suítes:
  - da raiz: `py -3.11 -m unittest nucleo.test_tabela_pedidos stokki.test_cancelar -v`;
  - de `painel_agentes/`: `py -3.11 -m unittest test_acoes_pedidos test_pedidos_tabela test_acesso_telas -v`.
  - Esperado: OK.
- [ ] **Step 3:** Revisão de segurança (skill `differential-review`) sobre `git diff origin/master -- painel_agentes/ nucleo/ stokki/`. A /pedidos não é exposta à internet sem login, mas abre ações para `atendimento`, cancela na Stokki e serve arquivo por caminho. Gravar em `docs/revisoes/2026-11-10-acoes-tela-pedidos.md` (não commitado). Achado ALTO ou MÉDIO volta para o Hugo antes do deploy.
- [ ] **Step 4:** Entregar ao Hugo: commits, testes e prints. Lembrar que o deploy (restart só do `painel-agentes`) e a prova real com o cliente teste (CNPJ 00.000.000/0001-91, sender 22812827) dependem do pedido dele, e que Cancelar Stokki só se prova num pedido que ele indicar.
