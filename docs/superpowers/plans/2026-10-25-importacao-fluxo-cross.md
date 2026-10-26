# Importação no fluxo Cross (item XD + troca de NF) — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embarcador marcado como Cross tem o XML da NF convertido num item XD (quantidade = volumes), o pedido criado na Stokki e, em seguida, o XML original colocado no pedido pela "Informar Nota Fiscal"; pendências de troca são retomadas e avisadas.

**Architecture:** O fluxo de cada embarcador mora na tabela `interno` (coluna `fluxos`) e o código XD é descoberto em `wms_produtos`. A regra de XML `cross` (parametrizada) vive no importador (`processar_xml.py`) e é usada pelos dois caminhos (portal e e-mail). A troca de NF é um módulo HTTP `stokki/trocar_nf.py`; a fila de trocas é a tabela `trocas_nf_pendentes` no `dados.db`, processada pelo worker do portal (loop de 20 s, já com trava). O importador por e-mail só grava na fila.

**Tech Stack:** Python 3.11, SQLite (`dados/dados.db`), requests via `stokki.auth.StokkiSession`, Playwright só no wizard existente, Flask (portal), unittest.

**Spec:** `docs/superpowers/specs/2026-10-24-importacao-fluxo-cross-design.md` (ler antes; este plano argumenta a partir dela).

## Global Constraints

- Dois repositórios: principal `c:\agente_stokki_eventos` (ramo de trabalho `fluxo-cross`, em worktree `.claude/worktrees/fluxo-cross`, como os ramos existentes) e importador `c:\agente_importacao_stokki` (ramo `fluxo-cross` no próprio diretório, porque o portal local o importa pelo caminho `portal_cliente.caminho_importador`). O worktree não tem `config.yaml` nem `dados/` (gitignored): os testes usam banco temporário e não precisam deles; a prova manual da Tarefa 6 e o `--simular` da Tarefa 10 precisam, então criar no worktree uma junção pra `dados/` e um link simbólico pro `config.yaml` da árvore principal (PowerShell `New-Item -ItemType SymbolicLink`; é link, não cópia do segredo), ou rodar essas provas na árvore principal depois do merge. Nunca copiar o `config.yaml`.
- Python local sempre `py -3.11`. Testes com `py -3.11 -m unittest <modulo>`; nunca misturar `portal_cliente`/`nucleo`/`stokki` com `painel_agentes` no mesmo comando. `py -3.11 -m py_compile` em tudo que editar.
- Commit por tarefa, mensagem `Área: o que mudou e por quê` em português, terminando com `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. **Nunca `git push`, nunca `git add -A`**, só os arquivos da tarefa. Antes de cada commit: `git status --short` e `git log --oneline -5` (outras sessões editam o mesmo tree).
- Nada toca a Stokki fora do piloto da Tarefa 10: `--simular` (portal) e `--modo-teste` (importador) não chamam a Stokki. Toda chamada real à Stokki roda dentro da trava `stokki/sessao_uso.py`.
- `config.yaml` e `sessao_*.json` são segredos: não ler, não imprimir, não copiar. Chaves novas de config só por nome.
- Valores fixos da spec: fluxos `armazenagem` | `full_cross` | `cross`; vazio = `armazenagem`. Quantidade XD = soma de `<transp><vol><qVol>`, `int(float(qVol))` por volume, **1 se soma 0 ou sem volume**, formatada `"N.0000"`. Nome do item XD = `"Mercadoria " + apelido`. Código XD = sku do embarcador começando com `XD`. Troca de NF: POST `/pt-br/administrator/inventory/outbound/nfe/store` com `_token`, `order_id_nfe`, `action=invoice`, `page=index`, `nfe=<chave 44 dígitos>`, `file_nfe=<xml>`. 3 tentativas de troca, mínimo 10 min entre elas, depois `ERRO` + e-mail só pra equipe.
- Comentários e strings de log sem acento nos arquivos que já seguem esse padrão (`stokki/cancelar.py`, `stokki/pedidos.py`). Mensagens pro cliente com acento normal.
- Ao criar arquivo ou rotina, atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit (Tarefa 9 concentra isso).

## Review Focus

1. NF com vários `<vol>`, alguns com `qVol` vazio, `"0"` ou decimal (`"3.000"`): a soma ignora vazios/zero e converte decimais; quando tudo é vazio, a quantidade é 1. (Teste na Tarefa 2.)
2. XML sem `<transp>` ou sem `<vol>`: quantidade 1, sem exceção. (Tarefa 2.)
3. Embarcador com dois fluxos envia planilha (sem NF-e) e escolhe Cross: recusa com mensagem; XML sem `fluxo` quando há dois disponíveis: recusa. (Tarefa 6.)
4. Stokki recusa o XML Cross como "chave já utilizada" (duplicado): o envio vira DUPLICADO **e** a troca entra na fila; troca já feita (mesma chave, mesmo nº de `<det>`) termina FEITA sem POST. (Tarefas 5 e 7.)
5. Stokki devolve erro JSON na troca: PENDENTE com o texto do erro, nova tentativa só depois de 10 min, `ERRO` na 3ª com e-mail pra equipe e nenhum e-mail pro cliente. (Tarefa 5.)

---

### Tarefa 1: Sonda só leitura do download do XML e do `_token` (pré-requisito, com o Hugo)

**Files:**
- Create (scratchpad, não versionado): `sondar_show_xml.py` — já existe em `C:\Users\HUGOMA~1\AppData\Local\Temp\claude\c--agente-stokki-eventos\8a1b03af-f367-45a3-905b-b66e1e10f2a7\scratchpad\sondar_show_xml.py`
- Modify: `docs/superpowers/specs/2026-10-24-importacao-fluxo-cross-design.md` (parágrafo "Pendente de sonda" em 6.5)

**Interfaces:**
- Produces: a constante `MODO_CONFERENCIA` da Tarefa 4 (`"itens"` se o download por `/pt-br/xml/nfe/<base64 do caminho>` devolve o XML com HTTP 200 na sessão admin; `"caminho"` caso contrário) e a forma exata da URL de download.

- [ ] **Passo 1: Pedir ao Hugo a autorização de leitura em produção** (a execução de 25/10 foi negada pelo classificador "Production Reads"). Sem autorização, parar aqui.

- [ ] **Passo 2: Rodar a sonda na VPS como www-data**

```bash
scp -i ~/.ssh/atendimento_vps "<scratchpad>/sondar_show_xml.py" root@187.127.52.197:/tmp/sonda_trocar_nf/
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "chmod a+r /tmp/sonda_trocar_nf/sondar_show_xml.py && cd /opt/stokki-eventos && sudo -u www-data venv/bin/python /tmp/sonda_trocar_nf/sondar_show_xml.py"
```
Esperado: linhas `show HTTP 200`, `tem _token: True`, e uma das linhas `GET /xml/nfe/<...> -> HTTP 200, N bytes, det=K, chave=True` (K ≥ 1). Se todas as variantes de base64 derem 302/404, anotar também o resultado de `/document/download/document/<b64>`.

- [ ] **Passo 3: Registrar o resultado na spec** (6.5, trocar "Pendente de sonda" por "Sondado em DD/MM: ..." com a URL que funcionou e o modo de conferência escolhido). Sem commit separado: entra no commit da Tarefa 4.

---

### Tarefa 2: Importador — regra de XML `cross` parametrizada

**Files:**
- Modify: `c:\agente_importacao_stokki\etapa2_processamento_xml\processar_xml.py` (seções "Transformação: Empório Quatro Estrelas" e "Registro de transformações", linhas 118-181 e 262-270; `processar_xml()` em 302-324)
- Create: `c:\agente_importacao_stokki\etapa2_processamento_xml\test_processar_xml.py`

**Interfaces:**
- Produces:
  - `somar_volumes(infNFe: ET.Element) -> int` — soma `int(float(qVol))` de cada `nfe:transp/nfe:vol/nfe:qVol` não vazio; devolve 1 quando a soma é 0 ou não há volume.
  - `transformar_xml_cross(caminho_origem: Path, caminho_destino: Path, codigo_xd: str, nome_item: str) -> None` — levanta `ValueError` se `codigo_xd` vazio, se não há `<infNFe>` ou `<det>`.
  - `TRANSFORMACOES["cross"] = transformar_xml_cross` (parametrizada) e `TRANSFORMACOES["emporio_quatro_estrelas"] = functools.partial(transformar_xml_cross, codigo_xd="XDQUATROESTRELAS", nome_item="Mercadoria Quatro Estrelas")`.
  - `processar_xml(nome_cliente: str, transformar_fn=None) -> list[Path]` — `transformar_fn` sobrepõe o lookup por slug (o `main.py` passa a função já parametrizada na Tarefa 8).
  - As constantes `CODIGO_CONSOLIDADO`, `NOME_CONSOLIDADO`, `QUANTIDADE_CONSOLIDADA` deixam de existir (eram da regra antiga).

- [ ] **Passo 1: Escrever os testes que falham** em `test_processar_xml.py` (unittest; fixtures como strings XML mínimas com namespace `http://www.portalfiscal.inf.br/nfe`, gravadas em `tempfile`):

```python
class SomarVolumes(unittest.TestCase):
    def test_soma_varios_vol_ignorando_vazios_e_decimais(self):   # vol: "2", "", "3.000", "0" -> 5
    def test_sem_transp_devolve_1(self):
    def test_soma_zero_devolve_1(self):

class TransformarXmlCross(unittest.TestCase):
    def test_um_det_so_com_codigo_nome_e_quantidade_volumes(self):
        # 3 <det>, volumes 2+3 -> 1 <det nItem="1">, cProd XDDOURADO, xProd "Mercadoria DOURADO", qCom == qTrib == "5.0000"
    def test_chave_destinatario_e_transp_intactos(self):           # Id="NFe...", <dest>, <transp> iguais aos da origem
    def test_sem_volume_quantidade_1(self):                        # qCom "1.0000"
    def test_codigo_vazio_levanta_value_error(self):
    def test_regra_quatro_estrelas_e_cross_com_xdquatroestrelas(self):  # TRANSFORMACOES["emporio_quatro_estrelas"] gera cProd XDQUATROESTRELAS e qCom = volumes
```

- [ ] **Passo 2: Rodar e ver falhar**

Run: `cd c:\agente_importacao_stokki && py -3.11 -m unittest etapa2_processamento_xml.test_processar_xml -v`
Esperado: FAIL/ERROR com `AttributeError: ... somar_volumes` (ou import).

- [ ] **Passo 3: Implementar** `somar_volumes` e `transformar_xml_cross` no lugar de `transformar_xml_quatro_estrelas`; registrar `TRANSFORMACOES` como acima; adicionar `transformar_fn=None` em `processar_xml()`.

- [ ] **Passo 4: Rodar e ver passar**

Run: `py -3.11 -m unittest etapa2_processamento_xml.test_processar_xml -v` e `py -3.11 -m unittest test_relatorio_email`
Esperado: todos OK.

- [ ] **Passo 5: Commit (repo importador, ramo fluxo-cross)**

```bash
git add etapa2_processamento_xml/processar_xml.py etapa2_processamento_xml/test_processar_xml.py
git commit -m "Etapa 2: regra cross parametrizada (item XD, quantidade = volumes da NF); Quatro Estrelas vira caso dela"
```

---

### Tarefa 3: Cadastro — coluna `fluxos`, leitura única e descoberta do XD

**Files:**
- Create: `nucleo/fluxo_embarcador.py`
- Create: `nucleo/test_fluxo_embarcador.py`
- Modify: `portal_cliente/gerenciar_clientes.py` (`main()`, subparsers a partir da linha 254)

**Interfaces:**
- Produces (`nucleo/fluxo_embarcador.py`):
  - `FLUXOS = ("armazenagem", "full_cross", "cross")`; `ROTULOS = {"armazenagem": "Armazenagem", "full_cross": "Full cross", "cross": "Cross"}`.
  - `garantir_coluna(conn) -> None` — `ALTER TABLE interno ADD COLUMN fluxos TEXT` se faltar (mesmo padrão de `_garantir_colunas` em `envio_pedidos.py`).
  - `normalizar_fluxos(texto: str | None) -> list[str]` — separa por vírgula, ignora desconhecidos, vazio -> `["armazenagem"]`.
  - `definir_fluxos(conn, cnpj: str, fluxos: list[str]) -> None` — valida contra `FLUXOS`, grava `fluxos` e `atualizado_em`; `ValueError` em valor inválido ou CNPJ ausente em `interno`.
  - `nome_item_xd(apelido: str) -> str` — `"Mercadoria " + apelido.strip()`.
  - `resolver_codigo_xd(conn, stkkc: int, apelido: str) -> tuple[str | None, str | None]` — consulta `wms_produtos WHERE embarcador_id = ? AND ativo = 1 AND UPPER(sku) LIKE 'XD%'`; 1 resultado -> ele; vários -> o que for igual a `"XD" + apelido` sem espaços/acentos/pontuação em maiúsculas; sem casamento -> `(None, "mais de um produto XD no catálogo: A, B")`; nenhum -> `(None, "nenhum produto XD no catálogo do embarcador")`.
  - `fluxos_do_embarcador(conn, cnpj: str | None = None, stkkc: int | None = None) -> dict` — `{"cnpj", "stkkc", "apelido", "fluxos": [...], "codigo_xd": str|None, "nome_item": str, "erro_xd": str|None}`; `codigo_xd`/`erro_xd` só são resolvidos quando `"cross"` está em `fluxos`; `ValueError` se o embarcador não existe.
- CLI (`gerenciar_clientes.py`): subcomando `fluxos` com `--cnpj` (lista um) ou sem (lista todos com fluxo ≠ armazenagem), `--definir "cross,armazenagem"`, e `--carga <json>` que lê um JSON `[{"cnpj": ..., "armazenagem": bool, "cross": bool, "outro": str}]` (formato dos documentos do artifact: `"outro"` igual a `"Full cross"`, sem distinguir maiúsculas, vira `full_cross`) e grava todos, imprimindo por embarcador Cross o XD encontrado ou o `erro_xd`.

- [ ] **Passo 1: Escrever os testes que falham** em `nucleo/test_fluxo_embarcador.py` (banco `sqlite3.connect(":memory:")` com `CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER, atualizado_em TEXT)` e `wms_produtos (sku TEXT, embarcador_id INTEGER, ativo INTEGER)`):

```python
def test_normalizar_vazio_e_armazenagem(self):            # None -> ["armazenagem"]; " cross , full_cross" -> ["cross","full_cross"]
def test_definir_fluxos_invalido_levanta(self):           # ["xpto"] -> ValueError
def test_resolver_xd_unico(self):                         # 1 sku XDDOURADO -> ("XDDOURADO", None)
def test_resolver_xd_varios_casa_apelido(self):           # XDQUATROESTRELAS e XDQE, apelido "QUATRO ESTRELAS" -> XDQUATROESTRELAS
def test_resolver_xd_varios_sem_casamento_erro(self):     # -> (None, "mais de um produto XD ...")
def test_resolver_xd_nenhum(self):                        # inativo nao conta -> (None, "nenhum produto XD ...")
def test_fluxos_do_embarcador_por_stkkc_sem_cross_nao_resolve_xd(self):   # codigo_xd None, erro_xd None
def test_fluxos_do_embarcador_cross_traz_xd_e_nome_item(self):             # nome_item "Mercadoria DOURADO"
def test_carga_json_full_cross_em_outro(self):            # {"outro": "Full cross", "armazenagem": False, "cross": False} -> ["full_cross"]
```

- [ ] **Passo 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest nucleo.test_fluxo_embarcador -v`
Esperado: ERROR `ModuleNotFoundError: nucleo.fluxo_embarcador`.

- [ ] **Passo 3: Implementar `nucleo/fluxo_embarcador.py`** com as assinaturas acima (normalização do apelido: `unicodedata.normalize("NFKD")`, só `[A-Z0-9]`). Função pura `carga_de_marcacoes(docs: list[dict]) -> dict[str, list[str]]` pra o `--carga` ser testável sem CLI.

- [ ] **Passo 4: Adicionar o subcomando `fluxos` em `gerenciar_clientes.py`** chamando `garantir_coluna` antes de ler/gravar.

- [ ] **Passo 5: Rodar e ver passar**

Run: `py -3.11 -m unittest nucleo.test_fluxo_embarcador -v` e `py -3.11 portal_cliente/gerenciar_clientes.py fluxos` (lista; o banco local é o congelado, só confere que roda).
Esperado: testes OK; CLI imprime a lista sem traceback.

- [ ] **Passo 6: Commit**

```bash
git add nucleo/fluxo_embarcador.py nucleo/test_fluxo_embarcador.py portal_cliente/gerenciar_clientes.py
git commit -m "Cadastro: fluxos por embarcador (armazenagem/full_cross/cross) e descoberta do produto XD no catalogo"
```

---

### Tarefa 4: `stokki/trocar_nf.py` — troca de NF por HTTP

**Files:**
- Create: `stokki/trocar_nf.py`
- Create: `stokki/test_trocar_nf.py` (padrão de `stokki/test_cancelar.py`: `_Resp`, `_Sessao` falsos)

**Interfaces:**
- Consumes: `stokki.pedidos.listar_pedidos(sessao, status, cliente, busca, por_pagina, ordenar_coluna, ordenar_dir)`, `extrair_id_da_linha(linha)`, `extrair_codigo_ps_da_linha(linha)`, `BASE_URL`.
- Produces:
  - `MODO_CONFERENCIA = "itens"` ou `"caminho"` (Tarefa 1).
  - `URL_NFE_STORE = f"{BASE_URL}/pt-br/administrator/inventory/outbound/nfe/store"`; `URL_LISTAGEM = f"{BASE_URL}/pt-br/administrator/inventory/outbound"`; `URL_XML = f"{BASE_URL}/pt-br/xml/nfe/{{b64}}"`.
  - `chave_do_xml(conteudo: bytes) -> str | None` — regex `Id="NFe(\d{44})"`.
  - `contar_itens(conteudo: bytes) -> int` — nº de `<det` (com namespace).
  - `token_csrf(sessao) -> str` — GET `URL_LISTAGEM`, regex `name="_token" value="([^"]+)"` do `#form_invoice_standardized`; `""` se não achar.
  - `localizar_pedido_por_nf(sessao, numero_nf: str, client_id: str) -> dict | None` — `listar_pedidos(status="", cliente=client_id, busca=numero_nf, por_pagina=20, ordenar_coluna="1", ordenar_dir="desc")`; devolve `{"id_stokki": int, "codigo": "PS-...", "caminho_nf": str|None}` da primeira linha cujo texto contém a NF como número inteiro (mesma regex de `_buscar_codigos_na_listagem` em `enviar_stokki.py`); `caminho_nf` vem de `btn_danfe[^>]*data-url="([^"]+)"` na célula `action`.
  - `caminho_nf_do_pedido(sessao, id_stokki: int, numero_nf: str, client_id: str) -> str | None` — mesma listagem, filtra pela linha com `/show/{id_stokki}`.
  - `baixar_xml_por_caminho(sessao, caminho: str) -> bytes | None` — GET `URL_XML` com `base64.b64encode(caminho)` sem `=`; `None` se não 200 ou sem `<infNFe`.
  - `trocar_nf(sessao, id_stokki: int, caminho_xml_original: Path, numero_nf: str, client_id: str) -> dict` — devolve `{"ok": bool, "ja_estava": bool, "erro": str, "itens_original": int, "itens_pedido": int | None, "caminho_antes": str|None, "caminho_depois": str|None}`:
    1. lê original: `chave`, `itens_original`; sem chave -> `ok=False, erro="XML original sem chave de acesso"`.
    2. `caminho_antes` pela listagem; em `MODO_CONFERENCIA == "itens"`, baixa e, se chave igual e `contar_itens == itens_original`, devolve `ok=True, ja_estava=True` sem POST.
    3. POST multipart (`files={"_token": (None, token), "order_id_nfe": (None, str(id)), "action": (None, "invoice"), "page": (None, "index"), "nfe": (None, chave), "file_nfe": (nome, bytes, "text/xml")}`, headers `X-Requested-With: XMLHttpRequest`, `Referer: URL_LISTAGEM`). Não-200 ou JSON com `errors`/`message` -> `ok=False, erro="Stokki <status>: <erros>"`.
    4. confere: `"itens"` -> baixa de novo e exige chave igual e `itens_pedido == itens_original`; `"caminho"` -> exige `caminho_depois != caminho_antes`. Falha de conferência -> `ok=False, erro="Stokki aceitou mas ..."`.
  - Nunca levanta por resposta da Stokki; só por falha de rede do requests.

- [ ] **Passo 1: Escrever os testes que falham** em `stokki/test_trocar_nf.py` (fixture: trecho da linha real da listagem com `btn_danfe data-url="users/clients/inventories/outbounds/invoices/abc.xml"` e `/show/42542`; XML mínimo com `Id="NFe<44 dígitos>"` e N `<det>`):

```python
def test_chave_e_contagem_do_xml(self):
def test_token_csrf_da_listagem(self):
def test_localizar_pedido_por_nf_devolve_id_codigo_e_caminho(self):
def test_ja_estava_quando_pedido_tem_mesma_chave_e_itens(self):      # nenhum POST em s.posts
def test_sucesso_posta_campos_exatos_e_confere_itens(self):          # files: _token, order_id_nfe, action=invoice, page=index, nfe=<chave>, file_nfe
def test_erro_json_da_stokki_vira_ok_false_com_motivo(self):         # {"errors":["Chave inválida"]} -> erro contem "Chave inválida"
def test_aceitou_mas_itens_nao_bateram(self):                        # ok False, erro comeca com "Stokki aceitou mas"
def test_modo_caminho_exige_data_url_diferente(self):                # MODO_CONFERENCIA patched para "caminho"
```

- [ ] **Passo 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest stokki.test_trocar_nf -v`
Esperado: ERROR de import.

- [ ] **Passo 3: Implementar `stokki/trocar_nf.py`** (docstring no padrão de `cancelar.py`, citando a sonda de 24/10 e a Tarefa 1; sem acento em comentários/log).

- [ ] **Passo 4: Rodar e ver passar**

Run: `py -3.11 -m unittest stokki.test_trocar_nf stokki.test_cancelar -v`
Esperado: OK.

- [ ] **Passo 5: Commit** (inclui a atualização da spec feita na Tarefa 1)

```bash
git add stokki/trocar_nf.py stokki/test_trocar_nf.py docs/superpowers/specs/2026-10-24-importacao-fluxo-cross-design.md
git commit -m "Stokki: troca de NF do pedido por HTTP (Informar Nota Fiscal), com conferencia idempotente"
```

---

### Tarefa 5: Fila `trocas_nf_pendentes` e processamento no worker

**Files:**
- Create: `portal_cliente/trocas_nf.py`
- Create: `portal_cliente/test_trocas_nf.py`
- Modify: `portal_cliente/enviar_stokki.py` (`ciclo()`, linhas 824-851: chamar o processamento depois dos cancelamentos)

**Interfaces:**
- Consumes: `stokki.trocar_nf.trocar_nf`, `stokki.trocar_nf.localizar_pedido_por_nf`, `stokki.sessao_uso.adquirir/liberar/em_uso`, `stokki.auth.StokkiSession(config)`, `email_utils.enviar_email`, `email_utils.envelope_html`, `portal_cliente.bloqueio_area.forcar_destino(config)`.
- Produces (`portal_cliente/trocas_nf.py`):
  - DDL `trocas_nf_pendentes (id INTEGER PK, cnpj_embarcador TEXT, client_id TEXT, numero_nf TEXT, chave_nfe TEXT UNIQUE, id_stokki INTEGER, codigo_pedido TEXT, caminho_xml_original TEXT, origem TEXT /* portal | email | virada */, envio_id INTEGER, status TEXT /* PENDENTE | FEITA | ERRO */, tentativas INTEGER DEFAULT 0, erro TEXT, criado_em TEXT, ultima_tentativa_em TEXT, concluido_em TEXT)`; `garantir_tabela(conn)`.
  - `STATUS_PENDENTE = "PENDENTE"`, `STATUS_FEITA = "FEITA"`, `STATUS_ERRO = "ERRO"`, `MAX_TENTATIVAS = 3`, `INTERVALO_MINIMO_MINUTOS = 10`, `DONO_TRAVA = "portal-trocas-nf"`.
  - `enfileirar(conn, *, cnpj_embarcador, client_id, numero_nf, chave_nfe, caminho_xml_original, origem, envio_id=None, id_stokki=None, codigo_pedido=None) -> int` — `INSERT OR IGNORE` pela chave; se já existe FEITA, não reabre; se existe ERRO, volta a PENDENTE com `tentativas=0`. Devolve o id da linha.
  - `pendentes_elegiveis(conn, agora: datetime) -> list[dict]` — PENDENTE com `tentativas < 3` e (`ultima_tentativa_em` nula ou ≥ 10 min atrás).
  - `processar_pendentes(conn, config, simular=False, fabrica_sessao=None, trocar=None, agora=None) -> dict` — `{"feitas", "pendentes", "erros", "adiados"}`; sem elegíveis não abre sessão; adquire trava (`ttl 10 min`, `esperar 60 s`, `alternativa=True`), senão `adiados`; pra cada linha: resolve `id_stokki` por `localizar_pedido_por_nf` quando nulo (sem achar -> conta tentativa com erro "pedido não encontrado na Stokki"); chama `trocar`; ok -> FEITA + `concluido_em`; falha -> `tentativas += 1`, `erro`, `ultima_tentativa_em`; na 3ª falha -> ERRO e `avisar_equipe(config, linha)`. `fabrica_sessao`/`trocar`/`agora` são injetáveis pros testes; `simular=True` só loga e devolve zeros.
  - `espelhar_em_portal_envios(conn, linha) -> None` — quando `envio_id` não é nulo, grava `portal_envios.troca_nf_status/troca_nf_em/troca_nf_erro/id_stokki` (colunas criadas na Tarefa 6; chamar só se existirem, via `PRAGMA table_info`).
  - `avisar_equipe(config, linha) -> None` — e-mail pra `forcar_destino(config)` se houver, senão `email.email_atendimento` ou `email_responsavel`; assunto `[Portal] Troca de NF falhou · NF <n> · <embarcador>`; nunca pro cliente.
- `ciclo()` em `enviar_stokki.py`: depois de `processar_cancelamentos_do_ciclo`, `total["trocas_nf"] = trocas_nf.processar_pendentes(conn, config, simular=simular)` dentro de `try/except` que só loga.

- [ ] **Passo 1: Escrever os testes que falham** em `portal_cliente/test_trocas_nf.py` (padrão `test_bloqueio_area.py`: `mock.patch.object(ep, "DB_PATH", tmp/"d.db")`, `conn = ep.conectar()`; `trocar` e `fabrica_sessao` falsos; `sessao_uso.adquirir/liberar` patchados pra `True`):

```python
def test_enfileirar_ignora_chave_repetida_e_reabre_erro(self):
def test_sem_pendentes_nao_abre_sessao(self):                        # fabrica_sessao nao chamada
def test_feita_grava_concluido_e_espelha_no_envio(self):
def test_falha_conta_tentativa_e_so_tenta_depois_de_10_min(self):    # 2a chamada com agora+5min nao chama trocar
def test_terceira_falha_vira_erro_e_avisa_equipe_nao_cliente(self):  # enviar_email patchado: destinatarios == [forcar_destino], 1 chamada
def test_id_stokki_nulo_resolve_pela_listagem(self):                 # localizar_pedido_por_nf patchado
def test_trava_ocupada_adia_sem_tocar(self):                         # adquirir -> False: adiados == n, trocar nao chamado
def test_simular_nao_toca(self):
```

- [ ] **Passo 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_trocas_nf -v`
Esperado: ERROR de import.

- [ ] **Passo 3: Implementar `portal_cliente/trocas_nf.py`** e a chamada em `ciclo()`.

- [ ] **Passo 4: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_trocas_nf portal_cliente.test_cancelamento -v`
Esperado: OK (o teste de cancelamento garante que `ciclo` continua íntegro).

- [ ] **Passo 5: Commit**

```bash
git add portal_cliente/trocas_nf.py portal_cliente/test_trocas_nf.py portal_cliente/enviar_stokki.py
git commit -m "Portal: fila de trocas de NF (trocas_nf_pendentes) processada pelo worker, 3 tentativas e aviso a equipe"
```

---

### Tarefa 6: Portal — fluxo por pedido na prévia, confirmação e lista

**Files:**
- Modify: `portal_cliente/envio_pedidos.py` (`conectar()` 212-221: colunas; `confirmar_envios()` 1199-1300; constantes perto de `REGRAS_XML` 110-116)
- Modify: `portal_cliente/app.py` (`api_envios_analisar` 770-851, `api_envios_confirmar` 863-911, `api_envios_listar` 747)
- Modify: `portal_cliente/templates/_envios.html` (`renderPrevia` 334-375, `confirmar` 390-417, chips da lista ~255-265)
- Create: `portal_cliente/test_fluxo_envio.py`

**Interfaces:**
- Consumes: `nucleo.fluxo_embarcador.fluxos_do_embarcador`, `ROTULOS`, `FLUXOS`.
- Produces:
  - Colunas novas em `portal_envios` via `_garantir_colunas`: `fluxo TEXT`, `id_stokki INTEGER`, `troca_nf_status TEXT`, `troca_nf_em TEXT`, `troca_nf_erro TEXT`.
  - `envio_pedidos.FLUXO_ROTULOS = fluxo_embarcador.ROTULOS` (reexport) e `envio_pedidos.escolher_fluxo(item: dict, disponiveis: list[str], origem: str) -> str` — regra pura: 1 disponível -> ele; vários -> exige `item["fluxo"]` em `disponiveis`, senão `ErroEnvio("<rotulo>: informe o fluxo (Armazenagem, Full cross ou Cross).")`; `origem == "planilha"` com `cross` -> `ErroEnvio("<rotulo>: pedido de planilha não pode ir em Cross; envie o XML da NF-e.")`.
  - `confirmar_envios(conn, cnpj_embarcador, itens, enviado_por, config, regra_xml, fluxos_disponiveis: list[str] | None = None)` — grava `fluxo` em cada linha (default `"armazenagem"` quando `fluxos_disponiveis` é None, pra não quebrar chamadores antigos).
  - `/api/envios/analisar` devolve também `fluxos_disponiveis: [...]`, `fluxo_rotulos: {...}`, `xd: {"codigo": str|None, "erro": str|None}`; quando `erro` e `cross` era o único fluxo, cada item XML vai pra `rejeitados` com `erro = "Produto XD não encontrado no cadastro do embarcador na Stokki. A Fresh Log foi avisada."`; quando há outros fluxos, `cross` sai de `fluxos_disponiveis`. Em ambos os casos **um** e-mail pro atendimento por chamada (`email.email_atendimento`, respeitando `bloqueio_area.forcar_destino`), assunto `[Portal] Produto XD não encontrado · <embarcador>`.
  - `/api/envios/confirmar` lê `item["fluxo"]` e passa `fluxos_disponiveis`.
  - `/api/envios/listar`: cada envio traz `fluxo` e `troca_nf_status`.
  - Template: coluna "Fluxo" na prévia só quando `fluxos_disponiveis.length > 1` (`<select class="fl">` com os rótulos; opção vazia "escolha"), botão "aplicar a todos" no cabeçalho da coluna; `confirmar()` manda `item.fluxo`; na lista, chip `Cross` pra `fluxo === "cross"` e chip `NF a trocar` (PENDENTE) / `NF trocada` (FEITA) / `Troca de NF com erro` (ERRO).

- [ ] **Passo 1: Escrever os testes que falham** em `portal_cliente/test_fluxo_envio.py` (padrão `test_fora_dia_fixo_envio.py`; `interno` e `wms_produtos` criados no banco temporário; `fluxos_do_embarcador` pode ser patchado):

```python
def test_escolher_fluxo_unico_nao_exige_campo(self):
def test_escolher_fluxo_dois_disponiveis_sem_campo_levanta(self):
def test_escolher_fluxo_planilha_cross_levanta(self):
def test_confirmar_envios_grava_fluxo_cross(self):              # linha em portal_envios com fluxo == "cross"
def test_confirmar_envios_sem_disponiveis_grava_armazenagem(self):
def test_conectar_cria_colunas_novas(self):                     # PRAGMA table_info(portal_envios) contem fluxo, id_stokki, troca_nf_status
```

- [ ] **Passo 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_fluxo_envio -v`
Esperado: FAIL/ERROR (`escolher_fluxo` inexistente).

- [ ] **Passo 3: Implementar em `envio_pedidos.py`** (colunas, `escolher_fluxo`, `confirmar_envios`).

- [ ] **Passo 4: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_fluxo_envio portal_cliente.test_fora_dia_fixo_envio -v`
Esperado: OK.

- [ ] **Passo 5: Rotas em `app.py` e template** conforme Interfaces. E-mail do XD ausente numa função `_avisar_xd_ausente(cfg, erro)` em `app.py` ao lado de `_json_erro_envio`, com `enviar_email` e `envelope_html`.

- [ ] **Passo 6: Prova manual na porta alternativa** (de dentro de `portal_cliente/`): `py -3.11 -c "import app; app.app.run(host='127.0.0.1', port=8098)"`, login como equipe em `/equipe`, escolher o Cliente Teste (CNPJ 00.000.000/0001-91; antes, `py -3.11 portal_cliente/gerenciar_clientes.py fluxos --cnpj 00000000000191 --definir armazenagem,full_cross,cross` no banco local), subir um XML de NF-e qualquer da pasta `dados/portal_envios/` e ver: coluna Fluxo com 3 opções, confirmação recusada sem escolha, aceita com escolha, chip `Cross` na lista. Sem `wms_produtos` do Cliente Teste, a prévia deve mostrar a recusa "Produto XD não encontrado" quando só Cross; com `armazenagem,full_cross,cross`, Cross some das opções. Encerrar o processo ao final (nunca `Stop-Process` em PID anterior à sessão).
Esperado: o comportamento acima, e `py -3.11 -m py_compile portal_cliente/app.py portal_cliente/envio_pedidos.py` limpo.

- [ ] **Passo 7: Commit**

```bash
git add portal_cliente/envio_pedidos.py portal_cliente/app.py portal_cliente/templates/_envios.html portal_cliente/test_fluxo_envio.py
git commit -m "Portal: embarcador escolhe o fluxo por pedido (Armazenagem/Full cross/Cross); XD ausente recusa e avisa"
```

---

### Tarefa 7: Worker do portal — conversão por fluxo e troca depois da criação

**Files:**
- Modify: `portal_cliente/enviar_stokki.py` (`transformar_xml` 112-120; `processar_lote` 508-648)
- Create: `portal_cliente/test_enviar_stokki_cross.py`

**Interfaces:**
- Consumes: `processar_xml.TRANSFORMACOES["cross"]` (Tarefa 2), `fluxo_embarcador.fluxos_do_embarcador`, `trocas_nf.enfileirar`, `_e_duplicado`, `_gravar_codigo`, `_marcar`.
- Produces:
  - `transformar_xml(regra: str, origem: Path, destino: Path, px, fluxo: str = "armazenagem", xd: dict | None = None) -> None` — `fluxo == "cross"` -> `px.TRANSFORMACOES["cross"](origem, destino, codigo_xd=xd["codigo_xd"], nome_item=xd["nome_item"])`, com `ValueError("XD não encontrado: <erro_xd>")` quando `xd["codigo_xd"]` é None; outros fluxos -> comportamento atual por `regra`.
  - `processar_lote`: carrega `xd = fluxos_do_embarcador(conn, cnpj=cnpj)` uma vez por lote quando algum envio é Cross; falha de preparação por XD vira ERRO + `_avisar_erros` (cliente com cópia do atendimento, como hoje: é a rede de segurança de 6.2). Depois do wizard: envio Cross **criado** -> `_gravar_codigo(...)` e `trocas_nf.enfileirar(conn, cnpj_embarcador=cnpj, client_id=cfg["client_id"], numero_nf=e["numero_nf"], chave_nfe=e["chave_nfe"], caminho_xml_original=e["xml_path"], origem="portal", envio_id=e["id"], id_stokki=<int do PS, se codigo>, codigo_pedido=codigo)` e `_marcar(conn, e["id"], troca_nf_status="PENDENTE")`; envio Cross **DUPLICADO** -> mesma fila (sem id_stokki; o worker resolve). Em `simular=True` não enfileira.
  - `_id_do_codigo(codigo: str | None) -> int | None` — `"PS-42584"` -> 42584; `"PS-36327-R1"` -> None (reentrega não é o pedido novo).

- [ ] **Passo 1: Escrever os testes que falham** em `portal_cliente/test_enviar_stokki_cross.py` (sem Stokki: `executar_wizard` patchado devolvendo `resultados/respostas/codigos` prontos; `carregar_importador` patchado pra um `px` falso com `TRANSFORMACOES["cross"]` espião; `sessao_uso.adquirir` patchado):

```python
def test_transformar_xml_cross_usa_regra_parametrizada(self):
def test_transformar_xml_cross_sem_xd_levanta_value_error(self):
def test_id_do_codigo(self):
def test_envio_cross_criado_enfileira_troca_com_id_e_marca_pendente(self):
def test_envio_cross_duplicado_enfileira_troca_sem_id(self):
def test_envio_armazenagem_nao_enfileira(self):
def test_simular_nao_enfileira(self):
```

- [ ] **Passo 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_enviar_stokki_cross -v`
Esperado: FAIL (assinatura antiga de `transformar_xml`, fila vazia).

- [ ] **Passo 3: Implementar** em `enviar_stokki.py`.

- [ ] **Passo 4: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_enviar_stokki_cross portal_cliente.test_trocas_nf -v`
Esperado: OK.

- [ ] **Passo 5: Commit**

```bash
git add portal_cliente/enviar_stokki.py portal_cliente/test_enviar_stokki_cross.py
git commit -m "Portal: worker converte por fluxo (XD no Cross) e enfileira a troca de NF do pedido criado ou duplicado"
```

---

### Tarefa 8: Importador por e-mail — fluxo do cadastro, XD ausente e fila de trocas

**Files:**
- Modify: `c:\agente_importacao_stokki\main.py` (`_trava_stokki` 154-176 -> extrair `_repo_principal`; `executar_pipeline` 378-500)
- Modify: `c:\agente_importacao_stokki\relatorio_email.py` (`_bloco_numeros` 140-153, `_linha_etapas` 156-165)
- Create: `c:\agente_importacao_stokki\avisos_cliente.py`
- Create: `c:\agente_importacao_stokki\test_main_cross.py`

**Interfaces:**
- Consumes (do repo principal, via `sys.path`): `nucleo.fluxo_embarcador.fluxos_do_embarcador(conn, stkkc=...)`, `portal_cliente.trocas_nf.enfileirar`, `portal_cliente.envio_pedidos.conectar` (ou `sqlite3.connect` em `<repo>/dados/dados.db` com `timeout=30`), `processar_xml.TRANSFORMACOES["cross"]`.
- Produces:
  - `_repo_principal(pasta_cliente: Path) -> Path | None` — mesma resolução de caminho que `_trava_stokki` usa hoje (refatorar pra ambos usarem).
  - `_fluxo_do_cliente(pasta_cliente, stokki_cfg) -> dict | None` — `fluxos_do_embarcador(conn, stkkc=int(stokki_cfg["client_id"]))`; `None` quando o repo principal não está acessível.
  - Regras em `executar_pipeline`: `fluxos` com mais de um valor -> `RuntimeError("cliente com mais de um fluxo não pode ser importado por e-mail")` antes da etapa 2; `"cross"` sem repo principal -> `RuntimeError`; `"cross"` com `codigo_xd` -> `processar_xml(nome_cliente, transformar_fn=partial(TRANSFORMACOES["cross"], codigo_xd=..., nome_item=...))`; `"cross"` com `erro_xd` -> etapa 2 e 3 **não rodam**, `registro["resultado"]="erro"`, `registro["erro"]="XD não encontrado: ..."`, e `avisos_cliente.avisar_xd_nao_encontrado(email_cfg, destinatarios, cc, nome_cliente, erro)` com `destinatarios = [interno.email]` e `cc = [email_atendimento]`, redirecionados pra `email.forcar_destino` do config do importador quando a chave existir (ausente = redirecionar pra `hugo@freshlogbr.com`, convenção de `bloqueio_area.forcar_destino`); `--modo-teste` não envia.
  - Depois da etapa 3 (modo real), pra cada resultado `criado` **ou** duplicado de cliente Cross: `enfileirar(conn, cnpj_embarcador=<interno.cnpj_embarcador>, client_id=stokki_cfg["client_id"], numero_nf=<nNF do XML original>, chave_nfe=<stem do arquivo>, caminho_xml_original=<xmls_extraidos/<chave>.xml, absoluto>, origem="email")`. `registro["trocas_enfileiradas"] = n`.
  - `relatorio_email`: tile "NF p/ trocar" com `trocas_enfileiradas` em `_bloco_numeros` quando a chave existe no registro.

- [ ] **Passo 1: Escrever os testes que falham** em `test_main_cross.py` (unittest; `baixar_zips`, `processar_xml`, `_importar_com_retentativas`, `_fluxo_do_cliente`, `enviar_relatorio_email` e `enfileirar` patchados; pasta de cliente em `tempfile` com `config.yaml` mínimo só com `stokki.client_id` e `dados/`):

```python
def test_cliente_cross_passa_transformar_fn_parametrizada(self):
def test_cliente_com_dois_fluxos_para_antes_da_etapa_2(self):
def test_xd_nao_encontrado_nao_importa_e_avisa_cliente_e_atendimento(self):   # avisar_xd_nao_encontrado chamado 1x; importar nao chamado
def test_criados_e_duplicados_cross_enfileiram_troca(self):                   # 2 criados + 1 duplicado -> 3 enfileirar
def test_cliente_armazenagem_nao_enfileira(self):
def test_modo_teste_nao_avisa_nem_enfileira(self):
```

- [ ] **Passo 2: Rodar e ver falhar**

Run: `cd c:\agente_importacao_stokki && py -3.11 -m unittest test_main_cross -v`
Esperado: ERROR/FAIL.

- [ ] **Passo 3: Implementar** `_repo_principal`, `_fluxo_do_cliente`, `avisos_cliente.py` (SMTP igual a `enviar_relatorio_email`), as regras em `executar_pipeline`, o tile no relatório.

- [ ] **Passo 4: Rodar e ver passar**

Run: `py -3.11 -m unittest test_main_cross test_relatorio_email etapa2_processamento_xml.test_processar_xml -v`
Esperado: OK. `py -3.11 main.py "Empório Quatro Estrelas" --modo-teste` roda até o fim sem traceback (precisa do config local do cliente; se não houver, pular e anotar).

- [ ] **Passo 5: Commit (repo importador)**

```bash
git add main.py relatorio_email.py avisos_cliente.py test_main_cross.py
git commit -m "Importador: fluxo vem do cadastro (Cross usa XD do catalogo), XD ausente avisa cliente e atendimento, trocas de NF vao pra fila"
```

---

### Tarefa 9: Virada — carga dos fluxos, backfill dos pedidos abertos, mapa

**Files:**
- Create: `portal_cliente/enfileirar_trocas_virada.py`
- Create: `portal_cliente/test_enfileirar_trocas_virada.py`
- Modify: `MAPA_DO_SISTEMA.txt` (seção 6 `stokki/`: `trocar_nf.py`; seção do portal: `trocas_nf.py`, `enfileirar_trocas_virada.py`, tabela `trocas_nf_pendentes`, coluna `interno.fluxos`; seção do 2º projeto: regra `cross`)

**Interfaces:**
- Consumes: `fluxo_embarcador.fluxos_do_embarcador`, `stokki.pedidos.listar_pedidos`/`iterar_todos_pedidos`, `stokki.trocar_nf.contar_itens`, `trocas_nf.enfileirar`, `sessao_uso`.
- Produces: script `enfileirar_trocas_virada.py --modo-teste | --executar [--cnpj X]`: pra cada embarcador com `cross` em `fluxos`, lista na Stokki (status `Open`, `Waiting for Carrier`, `Separating`, `cliente=stkkc`) e, pra cada pedido, procura o XML original: `portal_envios.xml_path` pela chave ou `numero_nf`, senão `<caminho_importador>/clientes/<slug>/dados/xmls_extraidos/<chave>.xml`; com original -> `enfileirar(..., origem="virada", id_stokki=<id>, codigo_pedido=<PS>)`; sem original -> linha no relatório final "troca manual". `--modo-teste` só imprime o que faria. Função pura `planejar(pedidos: list[dict], originais: dict[str, Path]) -> tuple[list[dict], list[dict]]` (enfileirar, manuais) testável sem Stokki.

- [ ] **Passo 1: Testes que falham** em `test_enfileirar_trocas_virada.py`: `test_planejar_separa_com_e_sem_original`, `test_modo_teste_nao_grava` (banco temporário, `listar_pedidos` patchado).

- [ ] **Passo 2: Rodar e ver falhar.** Run: `py -3.11 -m unittest portal_cliente.test_enfileirar_trocas_virada -v`

- [ ] **Passo 3: Implementar o script** e atualizar `MAPA_DO_SISTEMA.txt`.

- [ ] **Passo 4: Rodar e ver passar.** Mesmo comando; `py -3.11 portal_cliente/enfileirar_trocas_virada.py --modo-teste` contra o banco local imprime o plano (a Stokki local não responde sem sessão: o script deve falhar com mensagem clara, não com traceback, quando não há cookies; aceitar isso como resultado local).

- [ ] **Passo 5: Commit**

```bash
git add portal_cliente/enfileirar_trocas_virada.py portal_cliente/test_enfileirar_trocas_virada.py MAPA_DO_SISTEMA.txt
git commit -m "Portal: script da virada enfileira troca de NF dos pedidos Cross ja abertos; mapa atualizado"
```

---

### Tarefa 10: Prova integrada local, revisão e roteiro de piloto (sem deploy)

**Files:**
- Create: `docs/superpowers/plans/2026-10-25-importacao-fluxo-cross-piloto.md` (roteiro operacional)

- [ ] **Passo 1: Suíte completa dos dois repos**

Run (principal): `py -3.11 -m unittest nucleo.test_fluxo_embarcador stokki.test_trocar_nf stokki.test_cancelar portal_cliente.test_trocas_nf portal_cliente.test_fluxo_envio portal_cliente.test_enviar_stokki_cross portal_cliente.test_enfileirar_trocas_virada portal_cliente.test_fora_dia_fixo_envio portal_cliente.test_cancelamento -v`
Run (importador): `cd c:\agente_importacao_stokki && py -3.11 -m unittest test_main_cross test_relatorio_email etapa2_processamento_xml.test_processar_xml -v`
Esperado: tudo OK.

- [ ] **Passo 2: Ciclo do worker em simulação com um envio Cross do Cliente Teste** (banco local): confirmar um XML pela tela da Tarefa 6 com fluxo Cross, depois `py -3.11 portal_cliente/enviar_stokki.py --uma-vez --simular`. Esperado no banco local: `portal_envios.status = CRIADO`, `fluxo = cross`, `trocas_nf_pendentes` **vazia** (simulação não enfileira) e log sem traceback.

- [ ] **Passo 3: Escrever o roteiro de piloto** no arquivo acima, com os comandos exatos, nesta ordem: (a) carga de fluxos na VPS a partir do JSON exportado do artifact (`ArtifactData list fluxos` salvo em arquivo; `gerenciar_clientes.py fluxos --carga <json>` como www-data), conferindo o XD impresso de cada Cross; (b) deploy do repo principal (git pull, chown, restart `portal-cliente` e `portal-cliente-envios`) e do importador (scp de `processar_xml.py`, `main.py`, `relatorio_email.py`, `avisos_cliente.py`); (c) **um** XML do Quatro Estrelas pela `/equipe`, com o Hugo acompanhando: conferir na Stokki item XD com quantidade = volumes, status da troca na lista do portal, DANFE com os produtos reais; (d) `enfileirar_trocas_virada.py --modo-teste` e depois `--executar`; (e) conferir a 1ª rodada do importador (16:20) pela skill `conferir-rodada`. Deploy e piloto só com o Hugo pedindo.

- [ ] **Passo 4: Revisão final do ramo** (superpowers:requesting-code-review ou `/code-review`), corrigir o que vier, commitar.

- [ ] **Passo 5: Commit do roteiro**

```bash
git add docs/superpowers/plans/2026-10-25-importacao-fluxo-cross-piloto.md
git commit -m "Docs: roteiro de piloto e virada do fluxo Cross"
```
