# Transportadoras no banco + tela + consulta automática — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trocar `dados/BD_TRANSPORTADORAS.xlsx` por uma tabela no `dados.db`, com tela no painel e um fluxo que segura o pedido de transportadora desconhecida e pergunta ao embarcador (retirada / redespacho / entrega) por link.

**Architecture:** `regras/transportadoras.py` ganha `CatalogoTransportadoras.carregar_do_banco()` (mesmas `_Entrada`, mesma regra de casamento). Escrita, pendências, consultas e token ficam em dois módulos novos na raiz (`transportadoras_cadastro.py`, `consulta_transportadora.py`), usados pelo pipeline, pelo portal (página do link), pelo painel (tela) e por um job diário (lembrete/alerta).

**Tech Stack:** Python 3.11, SQLite (`dados/dados.db`), Flask (painel e portal), itsdangerous (`URLSafeTimedSerializer`, como em `portal_cliente/cotacao.py`), OpenWA via `notificar_whatsapp.py`, SMTP via `email_utils.enviar_email`, systemd timer.

**Spec:** `docs/superpowers/specs/2026-10-23-transportadoras-banco-cadastro-automatico-design.md`

## Global Constraints

- Python sempre `py -3.11` local; na VPS `/opt/stokki-eventos/venv/bin/python`, rodando como `www-data`.
- Testes com `unittest` (`py -3.11 -m unittest ...`); testes de `painel_agentes` rodam de dentro de `painel_agentes/`; nunca misturar com `roteirizacao`/`nucleo` no mesmo comando.
- Regra de casamento intocada: prioridade 0 (nome inequívoco RETIRADA vence CNPJ), 1 (CNPJ), 2 (nome normalizado), conflito, `resolver_por_endereco`.
- Tipos válidos: `ENTREGA`, `RETIRADA`, `TERCEIROS`. Situações: `ATIVA`, `PENDENTE`, `INATIVA`. Origens: `PLANILHA`, `AUTOMATICO`, `TELA`.
- CNPJ guardado como texto de 14 dígitos (13 dígitos → `zfill(14)`, igual ao loader atual).
- Config nova no `config.yaml` (não commitado; VPS editada à mão):
  ```yaml
  consulta_transportadora:
    ativo: false              # true só depois da virada (Task 10)
    forcar_destino_email: ""  # "" = envio real (decisão do Hugo 23/10)
    forcar_destino_whatsapp: ""
    validade_dias: 30
  ```
  Ausência da seção = `ativo: false`. Independe de `notificacoes_automaticas.ativo` e de `whatsapp_notificacoes.clientes.forcar_destino`; WhatsApp ainda exige `whatsapp_notificacoes.ativo`.
- Link do cliente: `<portal_cliente.url_base>/transportadora/<token>`, token assinado com `portal_cliente.secret_key`, salt `"consulta-transportadora"`.
- Lembrete 08h00 em dia útil (`regras.feriados.eh_dia_util`); alerta à equipe após 2 dias úteis (`somar_dias_uteis`); 1 aviso por par transportadora+embarcador por dia.
- Texto de WhatsApp ≤ 200 caracteres.
- Strings de log sem acento (padrão dos módulos de lote); commits `Área: o que mudou e por quê`, em português; só `git add` dos arquivos da tarefa; sem push/deploy sem o Hugo pedir.
- Atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit de cada arquivo/rotina/tela nova.

## Review Focus

1. Bloco Transportadora da Stokki vazio, `None` ou "Não informado" → nunca cria PENDENTE nem segura o pedido (teste na Task 6).
2. Vários pedidos da mesma transportadora nova na mesma rodada, de um ou mais embarcadores → 1 PENDENTE, 1 consulta por par, 1 aviso por par no dia (teste na Task 4).
3. Mesma transportadora chegando com grafia diferente mas mesmo CNPJ (ou mesmo nome normalizado sem CNPJ) → reusa a PENDENTE (teste na Task 4).
4. Link aberto depois que outro embarcador ou a equipe já respondeu → mostra o que foi definido, não regrava (teste na Task 7).
5. Embarcador sem e-mail e sem WhatsApp → alerta à equipe na primeira passada do job, sem esperar 2 dias (teste na Task 8).

---

### Task 1: Tabela, escrita e leitura pelo banco

**Files:**
- Create: `transportadoras_cadastro.py`
- Modify: `regras/transportadoras.py` (novo classmethod `carregar_do_banco`; docstring do módulo)
- Test: `test_transportadoras_cadastro.py`

**Interfaces:**
- Produces:
  - `transportadoras_cadastro.DB_PATH: Path` (`dados/dados.db`)
  - `conectar(db_path: Path = DB_PATH) -> sqlite3.Connection` — `row_factory=Row`; cria `transportadoras` e `transportadoras_historico` se não existirem; antes, se existir `transportadoras` com coluna `classificacao` (schema antigo de jun/26), `ALTER TABLE transportadoras RENAME TO transportadoras_antiga_jun26`.
  - Colunas de `transportadoras`: `id INTEGER PK, nome TEXT NOT NULL, tipo TEXT, situacao TEXT NOT NULL, uf, municipio, bairro, logradouro, numero, complemento, cep, cidades_atendidas, ufs_atendidas, cnpj, email, origem TEXT NOT NULL, criado_em, atualizado_em, atualizado_por`.
  - `transportadoras_historico(id PK, transportadora_id, quando, quem, campo, antes, depois)`.
  - `class ErroCadastro(ValueError)`
  - `CAMPOS_EDITAVEIS = ("nome","tipo","situacao","uf","municipio","bairro","logradouro","numero","complemento","cep","cidades_atendidas","ufs_atendidas","cnpj","email")`
  - `criar(conn, dados: dict, origem: str, por: str) -> dict` (linha criada)
  - `atualizar(conn, tid: int, mudancas: dict, por: str) -> dict` — grava 1 linha de histórico por campo alterado.
  - `buscar(conn, tid: int) -> dict | None`, `listar(conn, situacao: str | None = None, busca: str = "") -> list[dict]`
  - `historico(conn, tid: int) -> list[dict]`
  - `normalizar_cnpj(valor) -> str` (só dígitos; 13 → zfill 14; outro tamanho ≠ 14 → `""` só se vazio, senão `ErroCadastro`)
  - Validação em `criar`/`atualizar`: tipo inválido, TERCEIROS ATIVA sem `logradouro`+`numero`+`municipio`+`uf`, ou conflito (outra linha ATIVA com mesmo CNPJ ou mesmo `_normalizar(nome)` e tipo diferente) → `ErroCadastro` com o nome da linha em conflito. Linha PENDENTE (tipo `None`) e INATIVA não passam pela checagem de conflito nem de endereço; a checagem roda quando a linha vira ATIVA.
  - `regras.transportadoras.CatalogoTransportadoras.carregar_do_banco(db_path: Path) -> CatalogoTransportadoras` — lê só `situacao='ATIVA'`, conexão `mode=ro`; monta `_Entrada` como `carregar()` (endereço só para TERCEIROS); tabela ausente → `FileNotFoundError` (os leitores já tratam esse erro).

- [ ] **Step 1: Write the failing tests** em `test_transportadoras_cadastro.py` (banco temporário em `tempfile`):
  - `test_conectar_renomeia_tabela_antiga`: cria `transportadoras(nome TEXT PRIMARY KEY, classificacao TEXT)` com 1 linha; após `conectar`, existe `transportadoras_antiga_jun26` com 1 linha e `transportadoras` nova vazia.
  - `test_criar_e_carregar_do_banco_resolve_igual`: cria RETIRADA "Mandaê Serviço de Consultoria de Logística" cnpj `19782476000150`; `carregar_do_banco(db).resolver("Qualquer", cnpj="19782476000150").tipo == "RETIRADA"` e `resolver("MANDAE SERVICO DE CONSULTORIA DE LOGISTICA LTDA").tipo == "RETIRADA"`.
  - `test_pendente_e_inativa_fora_do_catalogo`: linha PENDENTE e linha INATIVA → `resolver(nome).desconhecida is True`.
  - `test_terceiros_sem_endereco_recusado`: `criar({"nome":"X","tipo":"TERCEIROS","situacao":"ATIVA"}, ...)` → `ErroCadastro`.
  - `test_conflito_de_tipo_recusado`: ATIVA "KANEJO" TERCEIROS com endereço; criar "Kanejo Logistica Ltda" RETIRADA → `ErroCadastro` com "KANEJO" na mensagem.
  - `test_atualizar_grava_historico`: mudar `email` e `tipo` → `historico()` tem 2 linhas com `antes`/`depois` corretos e `quem`.
  - `test_cnpj_13_digitos_ganha_zero`: `normalizar_cnpj(1234567000190) == "01234567000190"`.

- [ ] **Step 2: Run** `py -3.11 -m unittest test_transportadoras_cadastro -v` → FAIL (módulo não existe).
- [ ] **Step 3: Implement** `transportadoras_cadastro.py` e `carregar_do_banco` com as assinaturas acima. `criado_em`/`atualizado_em` em `datetime.now().isoformat(timespec="seconds")`.
- [ ] **Step 4: Run** o mesmo comando e `py -3.11 -m unittest regras.test_transportadoras_endereco regras.test_transportadoras_terceiros` → PASS.
- [ ] **Step 5: Commit** `Transportadoras: tabela no dados.db, escrita com historico e catalogo lido do banco`.

### Task 2: Migração da planilha + prova de equivalência

**Files:**
- Create: `migrar_transportadoras_planilha.py`
- Test: `test_migrar_transportadoras_planilha.py`

**Interfaces:**
- Consumes: Task 1 (`conectar`, `CatalogoTransportadoras.carregar`, `carregar_do_banco`).
- Produces:
  - `importar(xlsx: Path, conn) -> dict` → `{"importadas": int, "ja_existiam": int, "ignoradas": list[str]}`; idempotente (chave: `nome` exato + `origem='PLANILHA'`); copia também colunas Q/R (índices 16/17) e e-mail (19); `situacao='ATIVA'`, `origem='PLANILHA'`, `atualizado_por='migracao'`. Grava direto com INSERT (não passa pela validação de conflito, para trazer a planilha como está).
  - `comparar(xlsx: Path, db: Path, consultas: list[tuple[str, str]]) -> list[str]` — diferenças de `(tipo, str(endereco_redespacho), email, conflito, desconhecida)` entre os dois catálogos para cada `(nome, cnpj)`; inclui automaticamente cada linha da planilha.
  - `consultas_recentes(db: Path, dias: int = 60) -> list[tuple[str, str]]` — `(nome, cnpj)` distintos de `batimento_pedidos.transportadora` nos últimos `dias` (separar CNPJ do texto com o mesmo `_RE_CNPJ` de `batimento/medir.py`).
  - CLI: `--modo-teste` (só mostra), `--comparar` (imprime diferenças, exit 1 se houver).

- [ ] **Step 1: Write the failing tests**: planilha xlsx temporária (openpyxl) com 3 linhas (uma TERCEIROS com endereço e e-mail, uma RETIRADA com CNPJ numérico de 13 dígitos, uma com tipo inválido):
  - `test_importar_traz_validas_e_lista_ignoradas`: `importadas == 2`, `ignoradas` contém o nome de tipo inválido.
  - `test_importar_duas_vezes_nao_duplica`: 2ª chamada → `importadas == 0, ja_existiam == 2`.
  - `test_comparar_sem_diferencas`: após importar, `comparar(...) == []`.
  - `test_comparar_acusa_diferenca`: mudar o tipo de uma linha no banco → lista com 1 item citando o nome.
- [ ] **Step 2: Run** `py -3.11 -m unittest test_migrar_transportadoras_planilha -v` → FAIL.
- [ ] **Step 3: Implement** conforme Interfaces; `main()` reconfigura `sys.stdout` para UTF-8.
- [ ] **Step 4: Run** → PASS. Rodar local `py -3.11 migrar_transportadoras_planilha.py --modo-teste` → lista 129±linhas sem erro (banco local é cópia de ago; só checar que roda).
- [ ] **Step 5: Commit** `Transportadoras: migracao da planilha para o banco com prova de equivalencia` (incluir linha no MAPA, seção de scripts avulsos).

### Task 3: Todos os leitores passam a ler do banco

**Files (Modify):** `pipeline.py:81,964`, `reconciliar_pedidos_retirada.py:55,100`, `inventario.py:153`, `batimento/medir.py:59,408-411`, `notificacao_transportadoras/notificar_transportadoras.py:95,286`, `roteirizacao/gerar_pdf_romaneios.py:107,264-272`, `portal_cliente/envio_pedidos.py:1115-1116`, `painel_agentes/planejamento_rotas.py:2328-2381`, `painel_agentes/pedidos_parados_triagem.py:1103-1107`, `debug/debug_pedido_especifico.py`, `debug/debug_transportadoras.py`; docstrings/mensagens que citam a planilha em `regras/endereco.py`, `redespacho_confirmacao.py`, `retiradas/regras_retirada.py`, `roteirizacao/regioes_dia_fixo.py`, `telefone_origem.py`, `batimento/regras.py`, `notificacao_entregas/montar_email_entrega.py`, `painel_agentes/painel_agentes.py:3257`.
- Test (Modify): `painel_agentes/test_editar_transportadora_pedido.py:111`, `portal_cliente/test_bloqueio_area.py:91,97` (patch passa a mirar `carregar_do_banco`).

**Interfaces:**
- Consumes: `CatalogoTransportadoras.carregar_do_banco(DB_PATH)` da Task 1.
- Produces: nenhum leitor de produção chama `carregar(xlsx)` (só `migrar_transportadoras_planilha.py`).

- [ ] **Step 1: Update tests** — em `test_editar_transportadora_pedido.py:111` a mensagem de erro esperada passa de `"BD_TRANSPORTADORAS"` para `"cadastro de transportadoras"`; em `test_bloqueio_area.py` trocar o alvo do `mock.patch` para `regras.transportadoras.CatalogoTransportadoras.carregar_do_banco`.
- [ ] **Step 2: Run** esses dois testes → FAIL.
- [ ] **Step 3: Replace** cada `CatalogoTransportadoras.carregar(<xlsx>)` por `carregar_do_banco(<caminho do dados.db do próprio módulo>)`, remover a constante do xlsx que ficar órfã, e trocar "planilha BD_TRANSPORTADORAS.xlsx" por "cadastro de transportadoras (tela /transportadoras)" nas mensagens.
- [ ] **Step 4: Verify** `grep -rn "BD_TRANSPORTADORAS" --include=*.py . | grep -v worktrees` → só `migrar_transportadoras_planilha.py`, `backup_dados_gcs.py` e testes de migração. Rodar: `py -3.11 -m unittest regras.test_transportadoras_endereco regras.test_transportadoras_terceiros roteirizacao.test_canhoteira_transportadora portal_cliente.test_bloqueio_area notificacao_entregas.test_montar_email_entrega` e, de dentro de `painel_agentes/`, `py -3.11 -m unittest test_editar_transportadora_pedido` → PASS.
- [ ] **Step 5: Commit** `Transportadoras: pipeline, painel, portal e jobs leem o cadastro do banco` (MAPA: seção 7 `regras/` e armadilha "xlsx é histórico, não editar").

### Task 4: Pendências, pedidos parados e consultas

**Files:**
- Create: `consulta_transportadora.py`
- Test: `test_consulta_transportadora.py`

**Interfaces:**
- Consumes: Task 1 (`conectar`, `criar`, `atualizar`, `normalizar_cnpj`), `regras.transportadoras._normalizar`.
- Produces:
  - Tabelas (criadas em `conectar_consultas(db_path=DB_PATH)`, que chama `transportadoras_cadastro.conectar`):
    - `transportadora_pedidos_parados(pedido TEXT PK, transportadora_id, embarcador_cnpj, stkkc_id, desde, liberado_em)`
    - `transportadora_consultas(id PK, transportadora_id, embarcador_cnpj, criado_em, enviado_em, ultimo_lembrete_em, alerta_equipe_em, respondido_em, respondido_por, canal_resposta, UNIQUE(transportadora_id, embarcador_cnpj))`
  - `registrar_desconhecida(conn, nome: str, cnpj: str, pedido: str, embarcador_cnpj: str, stkkc_id: int | None) -> dict` → `{"transportadora": dict, "consulta": dict, "nova": bool}`. Procura PENDENTE com mesmo CNPJ (se houver) ou mesmo `_normalizar(nome)`; senão `criar(..., origem="AUTOMATICO", situacao="PENDENTE", tipo=None)`. Upsert do pedido parado e da consulta do par.
  - `pedidos_parados(conn, transportadora_id: int, embarcador_cnpj: str | None = None) -> list[dict]` (só `liberado_em IS NULL`)
  - `responder(conn, transportadora_id: int, tipo: str, endereco: dict | None, email: str, por: str, canal: str) -> dict` — `tipo` em ENTREGA/RETIRADA/TERCEIROS; TERCEIROS exige endereço (via validação da Task 1); grava `situacao='ATIVA'`; fecha **todas** as consultas abertas da transportadora (`respondido_em`, `respondido_por`, `canal_resposta`) e marca `liberado_em` em todos os pedidos parados dela. Já ATIVA → `ErroCadastro("ja respondida")`.
  - `deve_avisar_hoje(consulta: dict, hoje: date) -> bool` (nenhum envio hoje: nem `enviado_em` nem `ultimo_lembrete_em` com a data de hoje)
  - `gerar_token(secret: str, consulta_id: int) -> str` / `ler_token(secret: str, token: str, validade_dias: int) -> int | None` (salt `"consulta-transportadora"`; `None` para inválido; levanta `SignatureExpired` tratado → retorna `-1` para expirado).
  - `consultas_abertas(conn) -> list[dict]` (com nome da transportadora e contagem de pedidos parados)

- [ ] **Step 1: Write the failing tests**:
  - `test_mesma_transportadora_varios_pedidos_uma_pendente`: 3 pedidos, mesmo nome, 2 embarcadores → 1 transportadora PENDENTE, 2 consultas, 3 parados.
  - `test_reusa_pendente_por_cnpj_com_grafia_diferente`: "ACME LTDA" cnpj X, depois "Acme Transportes" cnpj X → mesma transportadora id.
  - `test_reusa_pendente_por_nome_sem_cnpj`.
  - `test_deve_avisar_hoje_uma_vez`: após marcar `enviado_em` hoje → False; amanhã → True.
  - `test_responder_retirada_libera_tudo`: após `responder(..., "RETIRADA", None, "", "Fulano (ACME) pelo link", "link")` → transportadora ATIVA, consultas dos 2 embarcadores fechadas, `pedidos_parados() == []`, `carregar_do_banco().resolver(nome).tipo == "RETIRADA"`.
  - `test_responder_terceiros_sem_endereco_recusado`.
  - `test_responder_entrega`: tipo ENTREGA → `resolver(nome).tipo == "ENTREGA"`.
  - `test_responder_duas_vezes_recusado`.
  - `test_token_ida_e_volta_e_expirado`.
- [ ] **Step 2: Run** `py -3.11 -m unittest test_consulta_transportadora -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Transportadoras: pendencia automatica, pedidos parados e consulta por embarcador`.

### Task 5: Aviso ao embarcador (e-mail + WhatsApp)

**Files:**
- Modify: `consulta_transportadora.py` (envio), `notificar_whatsapp.py` (texto + `avisar_cliente_transportadora`)
- Test: `test_consulta_transportadora.py` (novos casos), `test_notificar_whatsapp.py` (texto)

**Interfaces:**
- Consumes: Task 4; `email_utils.enviar_email(destinatarios, assunto, corpo_html, config_email)`, `envelope_html`; `preferencias_notificacao.whatsapp_do_embarcador(conn, cnpj)`; `interno.email`/`notificar_email` por `cnpj_embarcador`.
- Produces:
  - `notificar_whatsapp.texto_consulta_transportadora(transportadora: str, n_pedidos: int, link: str, lembrete: bool = False) -> str` (≤200 caracteres; nome cortado com `_nome_simples`).
  - `notificar_whatsapp.avisar_cliente_transportadora(cnpj: str, telefone: str, texto: str, assinatura: str, config: dict, modo_teste: bool = False, **kw) -> str` — molde de `avisar_cliente_cadastro`, com a trava `consulta_transportadora.forcar_destino_whatsapp` (ausente → `clientes.forcar_destino`).
  - `consulta_transportadora.config_consulta(config: dict) -> dict` (defaults: `ativo False`, `validade_dias 30`, forçar destinos ausentes = `None`).
  - `consulta_transportadora.link(config: dict, consulta_id: int) -> str` (`portal_cliente.url_base` + `/transportadora/` + token).
  - `consulta_transportadora.enviar_aviso(conn, consulta: dict, config: dict, lembrete: bool, modo_teste: bool) -> dict` → `{"email": bool, "whatsapp": str, "sem_contato": bool}`; grava `enviado_em` (1º) ou `ultimo_lembrete_em`. E-mail: assunto `"Fresh Log: pedidos parados aguardando informacao da transportadora <nome>"` (com acento no texto real), corpo com transportadora (nome, CNPJ), tabela dos pedidos parados do embarcador (código, destinatário, cidade — vindos do campo `resumo_pedido` passado ao registrar; ver Task 6), as 3 opções explicadas e botão do link; destino = `forcar_destino_email` se não vazio, senão `interno.email` (respeitando `notificar_email`). `sem_contato=True` quando não há e-mail nem WhatsApp.

- [ ] **Step 1: Write the failing tests** (mock de `enviar_email` e `integracao_openwa`):
  - `test_texto_consulta_cabe_em_200` com nome de 80 caracteres.
  - `test_enviar_aviso_usa_email_do_interno`; `test_enviar_aviso_respeita_forcar_destino_email`.
  - `test_enviar_aviso_sem_contato`: interno sem e-mail e sem WhatsApp → `sem_contato True`, nada enviado.
  - `test_enviar_aviso_marca_lembrete`: `lembrete=True` grava `ultimo_lembrete_em`, não mexe em `enviado_em`.
  - `test_desligado_nao_envia`: `ativo False` → nada enviado.
- [ ] **Step 2: Run** `py -3.11 -m unittest test_consulta_transportadora test_notificar_whatsapp -v` → FAIL.
- [ ] **Step 3: Implement.** Acrescentar `resumo TEXT` (JSON: destinatario, cidade, uf) em `transportadora_pedidos_parados` e parâmetro `resumo: dict | None = None` em `registrar_desconhecida`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Transportadoras: aviso ao embarcador por e-mail e WhatsApp com link de resposta`.

### Task 6: Pipeline segura o pedido

**Files:**
- Modify: `pipeline.py` (após `resolver_endereco_entrega`, antes do bloco RETIRADA, ~linha 499)
- Create: `test_pipeline_transportadora_desconhecida.py`

**Interfaces:**
- Consumes: Tasks 4 e 5.
- Produces:
  - `pipeline._deve_segurar_por_transportadora(detalhe: dict, res_end, servico_existente) -> tuple[str, str] | None` → `(nome, cnpj)` quando: `detalhe["transportadora"]` tem `nome` não vazio e diferente de "Não informado" (comparação sem acento/caixa), `res_end.transportadora.get("desconhecida")` é True **para a transportadora formal**, `servico_existente` é None, e `res_end.fonte in ("destino", "aguardando_redespacho")`. Senão `None`.
  - Ação nova `resultado["acao"] = "aguardando_transportadora"`, `requer_revisao=True`, `observacao="Transportadora <nome> nova: aguardando resposta do embarcador (retirada/redespacho/entrega)."`
  - Com `config_consulta(config)["ativo"]` False → comportamento de hoje (nada muda).
  - `modo_teste` → loga `[TESTE] ... seguraria` e retorna a ação sem gravar no banco nem enviar.
  - Fora de modo teste: `registrar_desconhecida(...)` + `enviar_aviso(..., lembrete=False)` quando `deve_avisar_hoje`; falha no envio é logada e não derruba o pedido.
  - A ação entra no resumo da rodada e no vigia como as demais (onde `aguardando_redespacho` é contado, contar também `aguardando_transportadora`: `grep -n "aguardando_redespacho" pipeline.py vigia/ painel_agentes/`).

- [ ] **Step 1: Write the failing tests** para `_deve_segurar_por_transportadora` (sem rede, `res_end` como `SimpleNamespace`):
  - `test_transportadora_vazia_ou_nao_informado_nao_segura` (None, `{}`, `{"nome": ""}`, `{"nome": "Não informado"}`).
  - `test_desconhecida_com_destino_segura`.
  - `test_desconhecida_com_local_entrega_nao_segura` (`fonte="local_entrega"`) e com `mensagem_llm`.
  - `test_ja_na_vuupt_nao_segura` (`servico_existente={"status":"not_assigned"}`).
  - `test_conhecida_nao_segura`.
- [ ] **Step 2: Run** `py -3.11 -m unittest test_pipeline_transportadora_desconhecida -v` → FAIL.
- [ ] **Step 3: Implement** e encaixar no fluxo; carregar `config_consulta` uma vez em `executar` e passar ao `processar_pedido` por parâmetro novo `config_consulta: dict | None = None`.
- [ ] **Step 4: Run** → PASS; `py -3.11 -m py_compile pipeline.py`.
- [ ] **Step 5: Commit** `Pipeline: segura pedido de transportadora nova ate o embarcador responder`.

### Task 7: Página do link no portal

**Files:**
- Create: `portal_cliente/consulta_transportadora_web.py`, `portal_cliente/templates/consulta_transportadora.html`
- Modify: `portal_cliente/app.py` (registrar, junto de `cotacao_web.registrar` ~linha 1088)
- Test: `portal_cliente/test_consulta_transportadora_web.py`

**Interfaces:**
- Consumes: Task 4 (`ler_token`, `responder`, `pedidos_parados`), `portal_cliente/cotacao.consultar_cep`.
- Produces:
  - `registrar(app, *, config: dict, secret: str)` com `GET/POST /transportadora/<token>` e `GET /transportadora/<token>/cep/<cep>` (JSON do `consultar_cep`, para preencher o formulário).
  - Estados do template: `ok` (formulário), `respondida_agora`, `ja_respondida` (mostra tipo e endereço definidos, sem formulário), `expirado`, `invalido`.
  - POST campos: `opcao` (`retirada|redespacho|entrega`), `nome` (obrigatório), e no redespacho `cep, logradouro, numero, complemento, bairro, municipio, uf` (obrigatórios: cep, logradouro, numero, municipio, uf) e `email_transportadora` (opcional). Erro de validação volta ao formulário com mensagem e os valores digitados.
  - `por = f"{nome} ({apelido do embarcador}) pelo link"`, `canal="link"`.
  - Página sem login, mesma identidade visual do `cotacao_aceite.html`, usável no celular.

- [ ] **Step 1: Write the failing tests** (Flask test client, banco temporário, `secret="segredo"`):
  - `test_get_token_valido_mostra_tres_opcoes` (as 3 frases dos botões da spec no HTML).
  - `test_post_retirada_ativa_e_confirma`.
  - `test_post_redespacho_sem_numero_volta_com_erro`.
  - `test_post_redespacho_completo_grava_terceiros`.
  - `test_link_de_consulta_ja_respondida_mostra_definido` (responder pelo outro embarcador antes; GET mostra "Retirada" e não tem `<form`).
  - `test_token_invalido`.
- [ ] **Step 2: Run** `py -3.11 -m unittest portal_cliente.test_consulta_transportadora_web -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; abrir no Playwright (portal em porta de teste) desktop e celular.
- [ ] **Step 5: Commit** `Portal: pagina do link para o embarcador responder sobre a transportadora` (MAPA: portal).

### Task 8: Lembrete diário e alerta à equipe

**Files:**
- Create: `lembrar_consultas_transportadora.py`, `infra/stokki-consulta-transportadora.service`, `infra/stokki-consulta-transportadora.timer`
- Modify: `notificar_whatsapp.py` (`texto_alerta_transportadora`, `avisar_alerta_transportadora`)
- Test: `test_lembrar_consultas_transportadora.py`

**Interfaces:**
- Consumes: Tasks 4–5; `regras.feriados.eh_dia_util`, `somar_dias_uteis`; `notificar_whatsapp.despachar(config, origem, tipo, texto, assinatura, modo_teste=..., **kw)` (grupo padrão = ALERTAS FRESH).
- Produces:
  - `rodar(conn, config: dict, hoje: date, modo_teste: bool) -> dict` → `{"lembretes": int, "alertas": int}`. Não faz nada se `ativo` False ou `not eh_dia_util(hoje)`. Para cada consulta aberta: `enviar_aviso(lembrete=True)` se `deve_avisar_hoje`; alerta à equipe (1 vez, grava `alerta_equipe_em`) se `somar_dias_uteis(data(criado_em), 2) <= hoje` **ou** o aviso voltou `sem_contato`.
  - `notificar_whatsapp.texto_alerta_transportadora(transportadora: str, embarcador: str, n_pedidos: int, dias: int, link_tela: str) -> str`; `avisar_alerta_transportadora(..., config, **kw)` com origem `"consulta_transportadora"`, assinatura `f"alerta_transportadora:{consulta_id}"`.
  - Link da tela: `<painel url_base>/transportadoras?aba=pendentes` (mesma base que `avisar_clientes_agenda` usa).
  - Timer `OnCalendar=Mon..Fri 08:00:00`, `Persistent=true`; service no molde de `stokki-informativo-mensal.service` (`User=www-data`, `OnFailure=stokki-alerta-falha@%n.service`). CLI `--modo-teste`.

- [ ] **Step 1: Write the failing tests** (data fixa; mocks de envio):
  - `test_lembra_uma_vez_por_dia`.
  - `test_alerta_apos_dois_dias_uteis` (consulta criada sexta 23/10/2026 → sem alerta seg 26/10, alerta ter 27/10).
  - `test_alerta_imediato_sem_contato`.
  - `test_alerta_nao_repete`.
  - `test_fim_de_semana_nao_roda`.
- [ ] **Step 2: Run** `py -3.11 -m unittest test_lembrar_consultas_transportadora -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Transportadoras: lembrete diario 08h ao embarcador e alerta a equipe apos 2 dias uteis` (MAPA: tabela de horários + timer).

### Task 9: Tela /transportadoras no painel

**Files:**
- Create: `painel_agentes/transportadoras_tela.py`, `painel_agentes/templates/transportadoras.html`, `painel_agentes/templates/_tour_transportadoras.html`
- Modify: `painel_agentes/painel_agentes.py` (rotas), `painel_agentes/templates/_menu_lateral_nav.html` (item em "Sistema", ao lado de Clientes AGENDA, badge `transportadoras`), `painel_agentes/acesso_telas.py` (`TELAS`), `painel_agentes/contadores_menu.py` (fonte `transportadoras` = COUNT de PENDENTE; `NIVEIS_POR_CONTADOR["transportadoras"] = ("total","operador","logistica")`)
- Test: `painel_agentes/test_transportadoras_tela.py`

**Interfaces:**
- Consumes: Tasks 1 e 4 (`listar`, `buscar`, `criar`, `atualizar`, `historico`, `consultas_abertas`, `pedidos_parados`, `responder`).
- Produces:
  - `GET /transportadoras` (`?aba=pendentes|todas`, `?busca=`, `?tipo=`, `?situacao=`) — `@requer_auth(niveis=("total","operador","logistica","leitura","atendimento"))`; `pode_editar = g.nivel_acesso in ("total","operador","logistica")`.
  - `GET /api/transportadoras/<id>` (ficha + histórico + parados), `POST /api/transportadoras` (criar, `origem="TELA"`), `POST /api/transportadoras/<id>` (atualizar), `POST /api/transportadoras/<id>/responder` (`{tipo, endereco, email}`, `canal="tela"`, `por=session usuario`) — todos com `@requer_auth(niveis=("total","operador","logistica"))` e `@exige_mesma_origem`; `ErroCadastro` → 400 `{"erro": msg}`.
  - `acesso_telas.TELAS`: `("transportadoras", "Transportadoras", "Sistema", [r"^/transportadoras$", r"^/api/transportadoras(/|$)"])`.
  - Formulário de endereço com busca de CEP (mesmo serviço de CEP já usado no painel, se houver; senão rota `GET /api/transportadoras/cep/<cep>` chamando `portal_cliente.cotacao.consultar_cep`).
  - Aba Pendentes mostra: nome, CNPJ, dias parado, embarcadores consultados, nº de pedidos parados, e os 3 botões de resposta.

- [ ] **Step 1: Write the failing tests** (de dentro de `painel_agentes/`, padrão de `test_nivel_logistica.py`):
  - `test_leitura_ve_mas_nao_edita` (GET 200; POST 403).
  - `test_criar_conflito_devolve_400`.
  - `test_responder_pela_tela_libera`.
  - `test_tela_em_acesso_telas` (`"transportadoras"` em `acesso_telas.CHAVES`).
  - `test_contador_pendentes`.
- [ ] **Step 2: Run** `cd painel_agentes && py -3.11 -m unittest test_transportadoras_tela -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; `cd painel_agentes && py -3.11 -m unittest test_acesso_telas test_nivel_logistica` → PASS; Playwright na porta 8099 (desktop e celular): lista, ficha, criar, editar, responder pendência.
- [ ] **Step 5: Commit** `Painel: tela de transportadoras com pendencias e resposta pela equipe` (MAPA: telas).

### Task 10: Pré-cadastro das desconhecidas recentes e virada

**Files:**
- Modify: `migrar_transportadoras_planilha.py` (flag `--pre-cadastro`)
- Test: `test_migrar_transportadoras_planilha.py` (novo caso)

**Interfaces:**
- Consumes: Task 2 (`consultas_recentes`), Task 1 (`carregar_do_banco`, `criar`).
- Produces: `pre_cadastrar(conn, db: Path, dias: int = 60) -> list[str]` — para cada `(nome, cnpj)` recente que `carregar_do_banco` dá como desconhecido e que não tem PENDENTE ainda, cria PENDENTE `origem="AUTOMATICO"` **sem consulta e sem pedido parado**; devolve os nomes criados. Ignora vazio/"Não informado".

- [ ] **Step 1: Write the failing test** `test_pre_cadastro_cria_pendentes_sem_consulta`: `batimento_pedidos` com 2 nomes (1 conhecido, 1 novo) → 1 PENDENTE, `transportadora_consultas` vazia; 2ª execução não duplica.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Transportadoras: pre-cadastro das transportadoras desconhecidas dos ultimos 60 dias`.

**Roteiro de virada (só quando o Hugo pedir deploy; seguir a skill deploy-vps):**
1. Conferir na VPS: `sqlite3` read-only — existe `transportadoras` antiga? `grep -rn "transportadoras" /opt/agente-importacao-stokki /opt/confirmacao-motoristas` e coletor do freshhub — alguém lê a tabela pelo nome? Se ler, parar e avisar o Hugo.
2. Deploy com `consulta_transportadora.ativo: false`; `sudo -u www-data venv/bin/python migrar_transportadoras_planilha.py` e depois `--comparar` → zero diferenças (senão parar).
3. Reiniciar `painel-agentes` e `portal-cliente`; copiar o timer, `daemon-reload`, `enable --now stokki-consulta-transportadora.timer`.
4. `pipeline.py --modo-teste` antes e depois: mesmas ações por pedido.
5. `--pre-cadastro`; Hugo/equipe classificam a aba Pendentes.
6. Hugo autoriza → `ativo: true` no config da VPS. Conferir a primeira consulta real (e-mail, WhatsApp, resposta, pedido liberado na rodada seguinte) com a skill conferir-rodada.
