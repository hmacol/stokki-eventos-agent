# Motoristas: planilha BD_MOTORISTAS → banco — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Os motoristas passam a morar numa tabela do `dados/dados.db` (fonte única), com o xlsx virando espelho só-leitura, botão Editar na tela /motoristas e login do app recusado 1 mês depois de o motorista ficar inativo.

**Architecture:** Tabela `bd_motoristas` com as MESMAS 15 colunas da planilha (nomes em maiúsculo, valores de texto preservados) + `id`, `inativado_em`, `criado_em`, `atualizado_em`; histórico em `bd_motoristas_alteracoes`. Um módulo novo, `regras/motoristas_banco.py`, é o único que lê/grava a tabela e regera o espelho xlsx a cada gravação. `CatalogoMotoristas.carregar()` mantém a assinatura e passa a ler da tabela quando o caminho pedido é a planilha padrão — os ~20 pontos que chamam `carregar(cfg["planilha"], ...)` não mudam.

**Tech Stack:** Python 3.11, sqlite3, pandas/openpyxl (só no espelho e na importação), Flask (painel), unittest.

**Spec:** `docs/superpowers/specs/2026-10-28-motoristas-planilha-para-banco-design.md`

## Global Constraints

- Python local: `py -3.11`; VPS: `/opt/stokki-eventos/venv/bin/python` como `sudo -u www-data`.
- Testes: `py -3.11 -m unittest <modulo>`; testes de `painel_agentes` rodam de dentro de `painel_agentes/`; nunca misturar `nucleo`/`roteirizacao` com `painel_agentes` no mesmo comando.
- **Commit e push só quando o Hugo disser "commit"/"deploy"** (pedido explícito de 28/10). Trabalhar no ramo local `motoristas-banco` (worktree via superpowers:using-git-worktrees). Os passos "Commit" abaixo são pontos de commit: na 1ª vez, mostrar arquivos + mensagem e pedir ao Hugo autorização pra commitar no ramo local; sem push até ele pedir deploy. Só `git add` dos arquivos da tarefa.
- Mensagens de commit: `Motoristas: <o que mudou e por quê>`.
- Código, comentários e mensagens em português; seguir o padrão de acento do arquivo editado.
- AGENT_ID_VUUPT pode repetir (Erick "CARRO 2"): **sem UNIQUE** nele; chave da tabela é `id`.
- CPF e telefone sempre TEXT (zero à esquerda, sem `.0`); AGENT_ID_VUUPT, VEHICLE_ID_VUUPT e MAX_ROTAS_DIA INTEGER.
- Prazo do login de inativo: **30 dias** contados de `inativado_em` (Hugo: "continua podendo entrar por 1 mês").
- Níveis do Editar = os mesmos do cadastro de hoje: `total`, `operador`, `logistica` (`pode_editar` em `/motoristas`). *Corrige a spec, que dizia só "total".*
- `MAPA_DO_SISTEMA.txt` atualizado no mesmo commit que cria arquivo/tabela.

## Review Focus

1. **Espelho xlsx falha ao gravar** (disco, permissão): a gravação no banco tem que valer mesmo assim e o erro só vai pro log — a alocação lê o banco. Teste na Task 1.
2. **Placa vinda do app com 2+ linhas no mesmo CPF**: não sobrescreve nenhuma linha e registra no histórico "nao_repassada". Teste na Task 3.
3. **Tabela vazia (antes do corte / ambiente de dev)**: `carregar()` continua lendo o xlsx, sem quebrar nada. Teste na Task 2.
4. **Inativar e reativar no mesmo dia / reativar depois de 30 dias**: reativar limpa `inativado_em` e o login volta na hora; inativar de novo recomeça a contagem. Teste na Task 5.
5. **Editar com valor "vazio" vs. campo ausente**: campo ausente no PUT não mexe; `""` limpa (exceto AGENT_ID, NOME, ATIVO, que não aceitam vazio). Teste na Task 4.

---

### Task 1: Tabela `bd_motoristas` + módulo de acesso + espelho

**Files:**
- Modify: `nucleo/banco.py` (DDL novo + `executescript` em `garantir_esquema`)
- Create: `regras/motoristas_banco.py`
- Test: `regras/test_motoristas_banco.py`

**Interfaces:**
- Produces (em `regras/motoristas_banco.py`):
  - `COLUNAS: list[str]` — as 15 da spec, nesta ordem: AGENT_ID_VUUPT, VEHICLE_ID_VUUPT, NOME_MOTORISTA, ACEITA_VIAGENS, DIAS_DISPONIVEIS, MAX_ROTAS_DIA, ATIVO, ZONAS_PREFERIDAS, TELEFONE_MOTORISTA, EMAIL_MOTORISTA, PLACA, CPF_MOTORISTA, TIPO_VEICULO, APENAS_CARGA_SECA, SEM_APP.
  - `ESPELHO_PADRAO: Path` = `banco.DB_PATH.parent / "BD_MOTORISTAS.xlsx"`.
  - `tem_registros(conn) -> bool`
  - `listar(conn) -> list[dict]` — ordem por `id`; chaves = COLUNAS + `id`, `inativado_em`.
  - `por_cpf(conn, cpf: str) -> list[dict]`
  - `inserir(conn, registro: dict, origem: str) -> int` (id novo; registra no histórico campo `"*criado*"`).
  - `atualizar(conn, motorista_id: int, campos: dict, origem: str) -> list[dict]` — devolve mudanças `{campo, de, para}`; levanta `ValueError` se `AGENT_ID_VUUPT` estiver em `campos` com valor diferente do atual, se `CPF_MOTORISTA` já preenchido mudar, ou se NOME/ATIVO vierem vazios. ATIVO passando a falso grava `inativado_em = banco.agora()`; passando a verdadeiro limpa. Usa `_parse_bool` de `regras.preferencias_motoristas` pra decidir verdadeiro/falso, mas grava o texto como veio.
  - `escrever_espelho(conn, caminho: Path = ESPELHO_PADRAO) -> bool` — grava em arquivo temporário na mesma pasta e `os.replace`; só COLUNAS, linhas na ordem de `id`; erro → `logger.exception` e `False` (nunca levanta).
  - `importar_planilha(conn, caminho: Path, substituir: bool = False) -> int` — lê com `dtype={"CPF_MOTORISTA": str}`, normaliza nomes de coluna como `_carregar_excel` faz, `NaN` → NULL, telefone sem `.0`; com tabela já preenchida e `substituir=False` levanta `RuntimeError("bd_motoristas ja tem linhas")`; numa transação só.
- DDL (em `nucleo/banco.py`, `_DDL_BD_MOTORISTAS`): `bd_motoristas(id INTEGER PRIMARY KEY AUTOINCREMENT, <15 colunas>, inativado_em TEXT, criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL)`, índices em AGENT_ID_VUUPT e CPF_MOTORISTA; `bd_motoristas_alteracoes(id, motorista_id INTEGER NOT NULL, agent_id INTEGER, campo TEXT NOT NULL, valor_antigo TEXT, valor_novo TEXT, origem TEXT NOT NULL, alterado_em TEXT NOT NULL)`.

- [ ] **Step 1: Testes que falham** (`regras/test_motoristas_banco.py`, banco em `tempfile` via `banco.conectar(caminho)`):
  - `test_importar_preserva_cpf_telefone_e_ids`: xlsx de fixture com CPF `"09474142742"`, telefone `11999998888` (número no Excel), VEHICLE_ID vazio numa linha → `listar()` traz `"09474142742"`, `"11999998888"`, `VEHICLE_ID_VUUPT is None`, `AGENT_ID_VUUPT` int.
  - `test_importar_agent_id_repetido`: duas linhas com o mesmo AGENT_ID → 2 linhas.
  - `test_importar_recusa_tabela_cheia`: 2ª importação sem `substituir` → `RuntimeError`; com `substituir=True` → mesma contagem.
  - `test_atualizar_registra_historico_e_origem`: mudar PLACA → 1 linha em `bd_motoristas_alteracoes` com `origem="painel:hugo"`, de/para certos.
  - `test_atualizar_ativo_grava_e_limpa_inativado_em`: ATIVO "SIM"→"NAO" preenche; "NAO"→"SIM" volta a NULL.
  - `test_atualizar_recusa_agent_id_e_cpf_preenchido`: `ValueError` nos dois; CPF vazio→preenchido passa.
  - `test_espelho_mesmas_colunas_e_ordem`: `escrever_espelho` + `pd.read_excel(dtype=str)` → colunas == COLUNAS, linhas na ordem de id, CPF com zero à esquerda.
  - `test_espelho_falha_nao_levanta` (Review Focus 1): caminho numa pasta inexistente → `False`, sem exceção.
- [ ] **Step 2:** `py -3.11 -m unittest regras.test_motoristas_banco` → FAIL (módulo não existe).
- [ ] **Step 3:** Implementar DDL em `nucleo/banco.py` e as funções acima em `regras/motoristas_banco.py`.
- [ ] **Step 4:** `py -3.11 -m unittest regras.test_motoristas_banco` → OK; `py -3.11 -m unittest nucleo.test_cadastro_motorista` continua OK.
- [ ] **Step 5: Commit** — `nucleo/banco.py regras/motoristas_banco.py regras/test_motoristas_banco.py` — `Motoristas: tabela bd_motoristas com historico e espelho xlsx (planilha vai pro banco)`.

### Task 2: `CatalogoMotoristas` lê do banco

**Files:**
- Modify: `regras/preferencias_motoristas.py` (`CatalogoMotoristas.carregar`, `__init__`, `agent_ids_sem_app`, docstring do módulo)
- Test: `regras/test_preferencias_motoristas.py`

**Interfaces:**
- Consumes: `motoristas_banco.tem_registros`, `listar`, `ESPELHO_PADRAO`.
- Produces: `CatalogoMotoristas.fonte: str` (`"banco"`, `"planilha"`, `"json"` ou `"vazio"`); `carregar(caminho_excel, caminho_json_fallback=None, *, conn=None)` — quando `Path(caminho_excel).resolve() == ESPELHO_PADRAO.resolve()` **e** a tabela tem linhas, monta o catálogo de `listar()` com o mesmo `_construir_motorista` (chaves já são as COLUNAS); senão, comportamento de hoje. `conn` só pra teste (padrão: `nucleo.banco.conectar()`, fechada no fim).
- `agent_ids_sem_app()`: chave de cache passa a ser `max(atualizado_em)`+contagem da tabela quando ela tem linhas (o mtime do espelho deixa de ser a referência).

- [ ] **Step 1: Testes que falham:**
  - `test_carregar_do_banco_igual_planilha`: importa a fixture pra um banco temporário e compara `carregar(planilha_fixture)` (xlsx) com `carregar(ESPELHO_PADRAO, conn=conn)` (banco): mesma lista de `MotoristaPreferencias` **na mesma ordem** (`dataclasses.asdict`), `fonte == "banco"`.
  - `test_tabela_vazia_le_planilha` (Review Focus 3): banco sem linhas → `fonte == "planilha"`.
  - `test_caminho_de_teste_nao_vai_pro_banco`: xlsx em pasta temporária → `fonte == "planilha"` mesmo com a tabela cheia.
- [ ] **Step 2:** `py -3.11 -m unittest regras.test_preferencias_motoristas` → os 3 novos FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** `py -3.11 -m unittest regras.test_preferencias_motoristas roteirizacao.test_documentacao_rota` → OK; de dentro de `painel_agentes/`: `py -3.11 -m unittest test_baixa_sem_app_tela` → OK.
- [ ] **Step 5: Commit** — `Motoristas: catalogo le da tabela bd_motoristas (mesma interface, planilha so se a tabela estiver vazia)`.

### Task 3: Gravadores de hoje passam pro banco

**Files:**
- Modify: `regras/cadastro_motoristas.py` (`_agent_ids_cadastrados`, `listar_agentes_vuupt_nao_cadastrados`, `cadastrar_motorista`, `atualizar_contato_planilha`), `nucleo/api_motorista.py:233-244` (origem)
- Test: `regras/test_cadastro_motoristas.py` (novo), `nucleo/test_cadastro_motorista.py`

**Interfaces:**
- Consumes: `motoristas_banco.inserir`, `atualizar`, `por_cpf`, `listar`, `escrever_espelho`.
- Produces (assinaturas mantidas + `conn` opcional pra teste):
  - `cadastrar_motorista(config, dados, *, origem: str = "painel", conn=None) -> dict` — devolve `{"agent_id": int, "nome": str}` (hoje devolve `None` e a rota `POST /api/motoristas` quebra no `**resultado`: corrigido aqui). `ValueError` se o agent_id já tem linha. Some o `PermissionError` (não há mais Excel aberto); a rota mantém o `except` inofensivo.
  - `atualizar_contato_planilha(config, cpf, telefone=None, email=None, placa=None, *, conn=None) -> bool` — origem `f"app:{cpf[-4:]}"`; telefone/e-mail em todas as linhas do CPF; **placa só se o CPF tiver exatamente 1 linha**; com 2+ grava no histórico de cada linha `campo="PLACA"`, `valor_novo=<placa>`, `origem=f"app:{cpf[-4:]}:nao_repassada"` sem mudar a coluna.
  - Toda gravação termina com `escrever_espelho(conn)`.

- [ ] **Step 1: Testes que falham** (`regras/test_cadastro_motoristas.py`):
  - `test_cadastrar_grava_no_banco_e_devolve_dict`
  - `test_cadastrar_recusa_agent_id_existente`
  - `test_contato_telefone_em_todas_as_linhas_do_cpf`
  - `test_placa_nao_repassa_com_duas_linhas` (Review Focus 2): 2 linhas com o mesmo CPF e placas `AAA1111`/`BBB2222` → placas intactas, histórico com `nao_repassada`.
  - `test_placa_repassa_com_uma_linha`
- [ ] **Step 2:** `py -3.11 -m unittest regras.test_cadastro_motoristas` → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** `py -3.11 -m unittest regras.test_cadastro_motoristas nucleo.test_cadastro_motorista` → OK; de dentro de `painel_agentes/`: `py -3.11 -m unittest test_cadastros_motoristas` → OK.
- [ ] **Step 5: Commit** — `Motoristas: cadastro e "Meus dados" gravam na tabela; placa do app nao sobrescreve quem tem 2 carros`.

### Task 4: Editar na tela /motoristas (+ repasse ao login)

**Files:**
- Modify: `regras/cadastro_motoristas.py` (nova `editar_motorista`), `painel_agentes/motoristas.py` (`dados_pagina_motoristas` lista de `motoristas_banco.listar` com `id` e todos os campos), `painel_agentes/painel_agentes.py` (rota nova, perto de `api_cadastrar_motorista` ~3550), `painel_agentes/templates/motoristas.html` (botão Editar por linha + modal reaproveitando o formulário de cadastro)
- Test: `painel_agentes/test_cadastros_motoristas.py`, `regras/test_cadastro_motoristas.py`

**Interfaces:**
- Consumes: `motoristas_banco.atualizar`, `escrever_espelho`; `nucleo.auth_motorista.buscar_motorista`.
- Produces:
  - `editar_motorista(config, motorista_id: int, campos: dict, usuario: str, *, conn=None) -> dict` — `{"mudancas": [...], "motorista": <linha>}`. Aceita as chaves do formulário (`nome, aceita_viagens, dias_disponiveis (lista), max_rotas_dia, ativo, zonas_preferidas (lista), telefone, email, placa, cpf, tipo_veiculo, vehicle_id, apenas_carga_seca, sem_app`), traduz pras COLUNAS no mesmo formato de `cadastrar_motorista` ("SIM"/"NAO", csv). Chave ausente = não mexe; `""` = limpa (Review Focus 5). Origem `f"painel:{usuario}"`.
  - Repasse ao login (mesmo CPF, se existir em `motoristas`): `nome`, `telefone`, `email`, `tipo_veiculo`, `vehicle_id`; `placa` só se o CPF tiver 1 linha. **Nunca** `ativo` (ver Task 5).
  - Rota `PUT /api/motoristas/<int:motorista_id>` com `@requer_auth(niveis=("total", "operador", "logistica"))` e `@exige_mesma_origem`; `ValueError` → 400; usuário = `session.get("usuario") or g.nivel_acesso` (mesmo padrão de `api_cadastro_aprovar`).
  - Tela: AGENT_ID mostrado sem campo editável; CPF editável só quando vazio. Botão só aparece com `pode_editar`.

- [ ] **Step 1: Testes que falham:**
  - `test_editar_campo_ausente_nao_mexe_vazio_limpa` (regras)
  - `test_editar_repassa_tipo_veiculo_ao_login_mas_nao_ativo` (regras): login com FIORINO, editar pra VUC e ATIVO NAO → login VUC, `ativo` do login continua 1.
  - `test_put_editar_niveis` (painel): `leitura` → 403; `operador` → 200.
  - `test_put_editar_agent_id_400` (painel)
- [ ] **Step 2:** rodar os dois módulos de teste → FAIL.
- [ ] **Step 3:** Implementar função, rota e tela.
- [ ] **Step 4:** testes OK; subir painel em `127.0.0.1:8099` (comando do CLAUDE.md) e, pelo Playwright local (ver memória "Testar painel com Playwright local"), abrir /motoristas, editar uma zona de um motorista do banco **local** e conferir a mudança na tabela e no espelho. Nunca na 8070.
- [ ] **Step 5: Commit** — `Motoristas: botao Editar na tela /motoristas, com historico e repasse ao login do app`.

### Task 5: Login do app recusado 30 dias depois de inativo

**Files:**
- Modify: `regras/motoristas_banco.py` (nova função), `nucleo/auth_motorista.py:144-150` (`autenticar`) e `:196-205` (`_validar_token`)
- Test: `nucleo/test_auth_inatividade.py` (novo)

**Interfaces:**
- Produces: `motoristas_banco.login_bloqueado(conn, cpf: str, hoje: datetime | None = None) -> bool` — `True` só se o CPF tem linha(s) em `bd_motoristas`, **nenhuma** ativa, e o `inativado_em` mais recente é de mais de 30 dias. CPF sem linha (ex. usuário TESTE) → `False`.
- `autenticar` levanta `AutenticacaoInvalida("Seu cadastro está inativo. Fale com a Fresh Log.", 403)` quando `login_bloqueado`; `_validar_token` recusa com `"Sessão inválida. Entre de novo."`.

- [ ] **Step 1: Testes que falham:** `test_inativo_ha_29_dias_entra`, `test_inativo_ha_31_dias_recusado`, `test_reativado_entra_na_hora` (Review Focus 4), `test_duas_linhas_uma_ativa_entra`, `test_cpf_fora_da_tabela_entra`, `test_token_de_inativo_vencido_recusado`.
- [ ] **Step 2:** `py -3.11 -m unittest nucleo.test_auth_inatividade` → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** `py -3.11 -m unittest nucleo.test_auth_inatividade nucleo.test_cadastro_motorista` → OK, e os testes da API do app que existirem (`py -3.11 -m unittest discover -s nucleo -p "test_api*.py"`) → OK.
- [ ] **Step 5: Commit** — `Motoristas: inativo continua entrando no app por 30 dias, depois o login e recusado`.

### Task 6: Scripts manuais gravam no banco

**Files:**
- Modify: `sincronizar_placas_motoristas.py`, `sincronizar_cpf_motoristas.py`, `vincular_veiculos_motoristas.py`
- Test: nenhum novo (scripts manuais, sem teste hoje); verificação por execução a seco.

**Interfaces:**
- Consumes: `motoristas_banco.listar`, `atualizar` (origem = nome do script), `escrever_espelho`.
- Cada script troca `pd.read_excel(CAMINHO_PLANILHA)` por `listar(conn)` e `df.to_excel` por `atualizar(...)` linha a linha, mantendo as regras de hoje (placa nunca sobrescreve uma existente; casamento por agent_id). `CAMINHO_PLANILHA` some. Mantêm o modo que só mostra o que faria (se existir hoje; senão, acrescentar `--aplicar` com padrão a seco).

- [ ] **Step 1:** Implementar nos 3 scripts.
- [ ] **Step 2:** `py -3.11 -m py_compile` nos 3; rodar cada um a seco contra o banco local de dev e conferir que lista o que faria sem gravar (nenhuma linha nova em `bd_motoristas_alteracoes`).
- [ ] **Step 3: Commit** — `Motoristas: scripts de placa, CPF e veiculo gravam na tabela`.

### Task 7: Migração/conferência, saída do git e mapa

**Files:**
- Modify: `regras/motoristas_banco.py` (bloco `if __name__ == "__main__":` com `importar` e `conferir`), `.gitignore` (tirar as duas linhas `!dados/BD_MOTORISTAS.xlsx`), `MAPA_DO_SISTEMA.txt` (tabela `bd_motoristas`/`bd_motoristas_alteracoes` na seção de tabelas; linha de `regras/motoristas_banco.py` na seção 7/regras; nota de que o xlsx é espelho; os 3 scripts), docstring de `regras/preferencias_motoristas.py` (fonte = tabela)
- Git: `git rm --cached dados/BD_MOTORISTAS.xlsx` (o arquivo local fica)

**Interfaces:**
- `py -3.11 -m regras.motoristas_banco importar --planilha <xlsx> [--substituir]` → imprime contagem.
- `py -3.11 -m regras.motoristas_banco conferir --planilha <xlsx>` → compara `CatalogoMotoristas` do xlsx com o do banco (mesmo teste da Task 2, sobre os dados reais): imprime `IGUAL (N motoristas)` ou cada diferença `agent_id campo xlsx=... banco=...` e sai com código 1. A ordem também é comparada — com a mesma lista na mesma ordem, a alocação (função do catálogo) dá o mesmo resultado; isso substitui o replay da spec com uma prova mais forte.
- `importar` termina chamando `escrever_espelho`.

- [ ] **Step 1:** Implementar CLI; ensaio local: copiar a planilha local pra pasta temporária, `importar` num banco temporário (`--db` só pra teste), `conferir` → `IGUAL`.
- [ ] **Step 2:** `.gitignore`, `git rm --cached`, mapa, docstring.
- [ ] **Step 3:** Rodar tudo: `py -3.11 -m unittest regras.test_motoristas_banco regras.test_preferencias_motoristas regras.test_cadastro_motoristas nucleo.test_cadastro_motorista nucleo.test_auth_inatividade roteirizacao.test_documentacao_rota` e, de dentro de `painel_agentes/`, `py -3.11 -m unittest test_cadastros_motoristas test_baixa_sem_app_tela` → OK.
- [ ] **Step 4: Commit** — `Motoristas: migracao e conferencia da planilha, xlsx sai do git (vira espelho so-leitura)`.
- [ ] **Step 5:** Pedir ao Hugo "commit/deploy" do ramo (mostrar a lista de commits). Sem isso, parar aqui.

### Task 8: Corte na VPS (só com "deploy" do Hugo)

Fora dos horários de rodada: **não** entre 14h00–14h30, 15h40–16h20, 17h50–18h20, 21h50–22h30, nem no pipeline horário em andamento (`systemctl list-timers` antes). Seguir a skill `deploy-vps`.

- [ ] **Step 1:** `ssh ... "cd /opt/stokki-eventos && git status --short && git log --oneline -3"`; guardar a planilha viva: `cp dados/BD_MOTORISTAS.xlsx /root/BD_MOTORISTAS_pre_corte_$(date +%Y%m%d_%H%M).xlsx` e `cp` igual pra `dados/BD_MOTORISTAS_pre_corte.xlsx`.
- [ ] **Step 2:** `git checkout -- dados/BD_MOTORISTAS.xlsx` (só pra o pull não abortar; a cópia viva está guardada), `git pull --no-rebase` (apaga o arquivo do disco), **na sequência imediata** `cp dados/BD_MOTORISTAS_pre_corte.xlsx dados/BD_MOTORISTAS.xlsx` (o código antigo ainda rodando continua lendo os mesmos dados), `chown -R www-data:www-data /opt/stokki-eventos`.
- [ ] **Step 3:** `sudo -u www-data venv/bin/python -m regras.motoristas_banco importar --planilha dados/BD_MOTORISTAS_pre_corte.xlsx` → contagem = linhas da planilha (40 em 28/10); `... conferir --planilha dados/BD_MOTORISTAS_pre_corte.xlsx` → `IGUAL`.
- [ ] **Step 4:** `systemctl restart painel-agentes motorista-api portal-cliente`; provar como www-data: `venv/bin/python -c` carregando o catálogo com o caminho do config (como os serviços fazem) e imprimindo só `fonte` e a contagem → `banco`, 40 — sem imprimir nada do config. Abrir `/painel/motoristas` (curl na 8071 com login de teste ou pedir ao Hugo) e conferir que a lista aparece.
- [ ] **Step 5:** Conferir o espelho: `git status --short` não mostra mais `dados/BD_MOTORISTAS.xlsx`; o freshhub segue lendo (rodar o adaptador de legado dele, se houver comando de conferência, ou conferir que o arquivo existe com as 15 colunas).
- [ ] **Step 6:** Pedir ao Hugo pra desativar a rotina mensal `trig_01AAVXMibeeJNQVHZ2aRLo9e` em claude.ai (ou fazer, se ele pedir); conferir a primeira rodada de rota depois do corte com a skill `conferir-rodada`; atualizar a memória do projeto.
