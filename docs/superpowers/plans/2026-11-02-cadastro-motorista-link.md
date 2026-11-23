# Cadastro do motorista pelo link — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Motorista novo abre um link no celular, preenche dados, veículo e documentos, e sai pronto pra receber rota: agente + veículo na Vuupt, linha na `bd_motoristas`, aviso no grupo e WhatsApp de boas-vindas — sem aprovação.

**Architecture:** O formulário (HTML único servido pela `motorista-api`) grava tudo em `motoristas_cadastros` antes de qualquer chamada externa. Um processador idempotente (`nucleo/cadastro_motorista_link.py`) roda as etapas (agente → veículo → tabela → avisos), guardando cada id assim que obtido; roda logo depois do envio e é retentado por um timer a cada 15 min. Fixos ganham dos esporádicos na alocação.

**Tech Stack:** Python 3.11, Flask (motorista-api e painel), SQLite (`dados/dados.db`), API REST da Vuupt (`requests`), WhatsApp via `notificar_whatsapp`/OpenWA, unittest, Playwright (teste da tela).

**Spec:** `docs/superpowers/specs/2026-10-28-cadastro-motorista-link-design.md` (veio do ramo `salvo/master-local-2026-11-01`). Decisões novas do Hugo, 02/11: preferência dos fixos entra **neste** plano; o formulário pergunta Android/iPhone (iPhone → `SEM_APP=SIM`); pode criar e apagar 1 agente descartável na Vuupt pra provar.

## Global Constraints

- Python **sempre** `py -3.11`; na VPS `/opt/stokki-eventos/venv/bin/python` como `www-data`.
- Motoristas só mudam pela tabela `bd_motoristas` via `regras/motoristas_banco.py` (nunca editar o xlsx; o espelho é regerado).
- CPF sempre texto com 11 dígitos (zero à esquerda); telefone só dígitos, sem `55`.
- E-mail Vuupt: 1ª letra de cada nome, minúsculas, sem acento, ignorando `da, de, do, das, dos, e`, `@freshhub.com.br`; colisão → `jps2@`, `jps3@`… (o e-mail é único na plataforma Vuupt inteira).
- Senha Vuupt: 6 primeiros dígitos do CPF; se a Vuupt recusar a senha, os 8 primeiros.
- `POST /users` (JSON): `name`, `email`, `password`, `phone_number` (`+55…`), `is_driver="Y"`, `is_user="N"`, `is_user_readonly="N"`, `is_admin="N"`. `POST /vehicles` (JSON): `license_plate`, `code`, `operational_base_id`, `agentsAllowed`. Ligar: `PUT /users/{id} {"vehicle_id": …}`. Listas: `GET /agents` (campo `phone_number`, `email`) e `GET /vehicles` (`license_plate`, `operational_base_id`), paginadas com `per_page=100`.
- Linha nova na `bd_motoristas`: `ATIVO=SIM`, `MAX_ROTAS_DIA=1`, `ACEITA_VIAGENS=NAO`, `APENAS_CARGA_SECA=SIM` se refrigerado = Não, `SEM_APP=SIM` se iPhone, `POSICAO` = `FIXO`|`ESPORADICO`.
- Documentos obrigatórios: selfie, CNH, CRLV, ANTT, comprovante de residência; foto (jpg/jpeg/png/webp/heic) ou PDF; máx. 10 MB cada; ficam em `dados/cadastros_motoristas/<id>/` e só saem pelo painel com login.
- Mensagem no grupo ≤ 200 caracteres (`MAX_MENSAGEM`).
- Envio ao motorista nasce em piloto: `cadastro_motorista_link.forcar_destino_whatsapp` = número do Hugo; esvaziar é decisão dele. Teto próprio `whatsapp_notificacoes.motoristas.teto_diario` (padrão 20).
- Chave geral `cadastro_motorista_link.ativo` (padrão **false**): desligada, a página mostra "Cadastro indisponível no momento" e as rotas novas devolvem 503.
- Freio por IP que já existe (`_freio_cadastro`, 5/h por IP, 40/dia) vale pro link.
- Strings e comentários sem acento só onde o arquivo já segue esse padrão (`notificar_whatsapp.py`, `motoristas_banco.py`); os demais arquivos usam acento.
- Rota nova do painel entra em `acesso_telas.TELAS` só se for tela nova (aqui não é: a seção fica dentro de `/motoristas`).
- Commit só quando o Hugo pedir; `git add` só dos arquivos da tarefa.

## Review Focus

- **Duplo clique / reenvio no "Enviar"** → um agente só na Vuupt. Teste: `processar` chamado duas vezes em seguida (e com `processando_em` recente) cria 1 agente. (Task 5)
- **Vuupt fora do ar no meio** (agente criado, veículo falhou) → retentativa não recria o agente e termina o resto. Teste em Task 5.
- **Telefone já é de um agente da Vuupt** (motorista antigo fora da tabela) → nada é criado, aviso pede pra equipe resolver, painel resolve. Testes em Task 5 e Task 8.
- **Aparelho fecha no meio do passo 3** → ao voltar, o formulário retoma do mesmo cadastro com os documentos já enviados. Teste da rota `situacao` em Task 7 e no roteiro Playwright de Task 9.
- **Foto de 8 MB pelo 4G / iPhone HEIC** → reduzida no navegador antes de subir; HEIC/PDF sobem como estão. Teste: `guardar_documento` aceita `.heic` e `.pdf` e recusa `.exe` (Task 1); roteiro Playwright sobe uma foto grande (Task 9).

---

### Task 1: Banco e validação do cadastro pelo link

**Files:**
- Modify: `nucleo/banco.py` (migração de `motoristas_cadastros` em `garantir_esquema`)
- Modify: `nucleo/cadastro_motorista.py`
- Test: `nucleo/test_cadastro_link.py` (novo)

**Interfaces:**
- Produces:
  - Colunas novas em `motoristas_cadastros` (via `_migrar_colunas`): `origem TEXT NOT NULL DEFAULT 'app'`, `data_nascimento TEXT` (ISO), `emite_nf INTEGER`, `posicao TEXT`, `celular TEXT` (`ANDROID`|`IPHONE`), `veiculo_modelo TEXT`, `veiculo_ano INTEGER`, `refrigerado INTEGER`, `temp_min_c REAL`, `selfie_arquivo TEXT`, `antt_arquivo TEXT`, `residencia_arquivo TEXT`, `enviado_em TEXT`, `vuupt_email TEXT`, `vuupt_senha_digitos INTEGER` (6 ou 8), `vehicle_id INTEGER`, `bd_motorista_id INTEGER`, `conflito_agent_id INTEGER`, `conflito_resolvido INTEGER NOT NULL DEFAULT 0`, `aviso_grupo_em TEXT`, `aviso_concluido_em TEXT`, `boas_vindas_situacao TEXT`, `ultimo_erro TEXT`, `tentativas INTEGER NOT NULL DEFAULT 0`, `processando_em TEXT`, `concluido_em TEXT`.
  - `STATUS_SUBSTITUIDO = "SUBSTITUIDO"`; `ORIGEM_LINK = "link"`.
  - `DOCUMENTOS = ("cnh", "crlv", "selfie", "antt", "residencia")`; `DOCUMENTOS_LINK = DOCUMENTOS`; `EXTENSOES_DOCUMENTO = {"jpg","jpeg","png","webp","heic","pdf"}`.
  - `cpf_valido(cpf: str) -> bool` — 11 dígitos, não repetidos, dígitos verificadores.
  - `criar_cadastro_link(conn, dados: dict) -> dict` → `{"id", "chave_envio"}`. Campos de `dados`: `nome, cpf, data_nascimento ("AAAA-MM-DD"), telefone, chave_pix, emite_nf (bool), dias, zonas, posicao, celular, tipo_veiculo, placa, veiculo_modelo, veiculo_ano, refrigerado (bool), temp_min_c, dono_veiculo (bool)`.
  - `guardar_documento(...)` passa a aceitar os 5 tipos e PDF/HEIC; `_montar` ganha `tem_<tipo>` pros 5 e `completo` = CNH+CRLV no app, os 5 no link.
  - `marcar_enviado(conn, cadastro_id: int, chave_envio: str) -> dict` — confere chave, exige `completo`, grava `enviado_em` e `status = APROVADO`, `revisado_por = "link"` (idempotente: já enviado devolve o cadastro sem erro).
  - `situacao_link(conn, cadastro_id: int, chave_envio: str) -> dict` → `{"id", "primeiro_nome", "documentos": {tipo: bool}, "enviado": bool}`.
  - `listar_pendentes` passa a filtrar `origem = 'app'`.

- [ ] **Step 1: Escrever os testes que falham** em `nucleo/test_cadastro_link.py` (mesmo `setUp` de `nucleo/test_cadastro_motorista.py`: `banco.DB_PATH` num tmp; `regras.motoristas_banco` usa o mesmo banco):
  - `test_cpf_valido`: `cpf_valido("52998224725")` True; `"52998224724"` False; `"11111111111"` False; `"123"` False.
  - `test_cria_link_completo`: payload válido (CPF `52998224725`, `refrigerado=False`, `celular="ANDROID"`, `posicao="FIXO"`) → linha com `origem='link'`, `status='PENDENTE'`, `data_nascimento='1990-05-17'`, `temp_min_c` None.
  - `test_refrigerado_exige_temperatura`: `refrigerado=True` sem `temp_min_c` → `CadastroInvalido`; com `-18` aceita e grava `-18.0`.
  - `test_recusa_quem_ja_e_motorista`: CPF em `bd_motoristas` (via `motoristas_banco.inserir`) → `CadastroInvalido` com código 409 e mensagem contendo "já é motorista da Fresh Log"; idem com login em `motoristas` e com cadastro `APROVADO`.
  - `test_recusado_nao_bloqueia` e `test_pendente_vira_substituido`: cadastro anterior `RECUSADO` deixa criar; anterior `PENDENTE` (app ou link) vira `SUBSTITUIDO` e o novo nasce.
  - `test_validacoes`: idade < 18 recusa; ano do veículo < 1980 ou > ano atual + 1 recusa; `posicao` fora de FIXO/ESPORADICO recusa; `celular` fora de ANDROID/IPHONE recusa.
  - `test_documentos_link`: aceita `selfie.heic` e `antt.pdf`, recusa `cnh.exe`; `completo` só com os 5.
  - `test_marcar_enviado`: incompleto → `CadastroInvalido`; completo → `status='APROVADO'`, `enviado_em` preenchido; 2ª chamada não levanta e não muda `enviado_em`; chave errada → 404.
  - `test_pendentes_do_app_sem_link`: `listar_pendentes` não traz cadastro de origem link.

- [ ] **Step 2: Rodar e ver falhar**
  Run: `py -3.11 -m unittest nucleo.test_cadastro_link -v`
  Expected: FAIL (`AttributeError: ... criar_cadastro_link`).

- [ ] **Step 3: Implementar** — migração em `nucleo/banco.py` junto da de `dono_veiculo`; funções acima em `nucleo/cadastro_motorista.py` reaproveitando `validar_telefone`, `validar_placa`, `validar_pix`, `_lista`. Duplicidade: `motoristas_banco.por_cpf`, `auth.buscar_motorista`, `SELECT ... status='APROVADO'`. `guardar_documento` aceita `status` PENDENTE (como hoje). `criar_cadastro` do app continua igual (a regra de substituir só vale no link).

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest nucleo.test_cadastro_link nucleo.test_cadastro_motorista -v` → OK (o teste antigo do app continua verde).

- [ ] **Step 5: Commit** — `git add nucleo/banco.py nucleo/cadastro_motorista.py nucleo/test_cadastro_link.py` · `Motoristas: cadastro pelo link no banco (campos novos, 5 documentos, CPF com DV)`

---

### Task 2: Cliente Vuupt de agentes e veículos

**Files:**
- Create: `nucleo/vuupt_agentes.py`
- Test: `nucleo/test_vuupt_agentes.py`

**Interfaces:**
- Produces:
  - `class ErroVuupt(Exception)` com `.status: int | None` e `.corpo: str`.
  - `class VuuptAgentes(token: str, base: str = "https://api.vuupt.com/api/v1", sessao=None)`:
    - `listar_agentes() -> list[dict]` (GET /agents, todas as páginas)
    - `listar_veiculos() -> list[dict]` (GET /vehicles, todas as páginas)
    - `criar_agente(nome: str, email: str, senha: str, telefone: str) -> int` (POST /users; resposta `{"user": {...}}` ou `{"data": {...}}`; devolve o id)
    - `criar_veiculo(placa: str, base_operacional_id: int | None, agent_id: int) -> int` (POST /vehicles com `code=placa`, `agentsAllowed=[agent_id]`)
    - `ligar_veiculo(agent_id: int, vehicle_id: int) -> None` (PUT /users/{id})
    - `apagar_agente(agent_id: int) -> None` e `apagar_veiculo(vehicle_id: int) -> None` (só pra limpar o teste real)
    - Erro HTTP ≥ 400 ou de rede → `ErroVuupt`.
  - `gerar_email(nome: str, ocupados: set[str]) -> str`
  - `telefone_e164(telefone: str) -> str` (`"11998765432"` → `"+5511998765432"`)
  - `mesmo_telefone(a: str, b: str) -> bool` (compara os últimos 11 dígitos sem `55`)
  - `base_operacional_mais_comum(veiculos: list[dict]) -> int | None`

- [ ] **Step 1: Testes que falham** (sessão `requests` falsa com respostas enfileiradas):
  - `test_gerar_email`: `"João Pedro da Silva"` → `jps@freshhub.com.br`; com `{"jps@freshhub.com.br"}` ocupado → `jps2@…`; com `jps@` e `jps2@` → `jps3@…`; `"MARIA  DE   SOUZA e LIMA"` → `msl@…`; `"Ângela Íris"` → `ai@…`.
  - `test_telefone`: `telefone_e164("11998765432") == "+5511998765432"`; `mesmo_telefone("+55 11 99876-5432", "11998765432")` True; `mesmo_telefone("", "")` False.
  - `test_criar_agente_payload`: confere URL `/users`, JSON com `is_driver="Y"`, `is_admin="N"`, `phone_number="+55…"`; resposta `{"user": {"id": 777}}` → 777; `{"data": {"id": 778}}` → 778.
  - `test_erro_vira_ErroVuupt`: 422 com corpo `{"errors":{"email":["taken"]}}` → `ErroVuupt` com `.status == 422` e `"email" in .corpo`.
  - `test_paginacao`: duas páginas (`meta.pagination.total_pages=2`) → lista com as duas.
  - `test_base_operacional`: `[{"operational_base_id": 5}, {"operational_base_id": 5}, {"operational_base_id": 9}, {}]` → 5; lista vazia → None.

- [ ] **Step 2:** `py -3.11 -m unittest nucleo.test_vuupt_agentes -v` → FAIL (módulo não existe).
- [ ] **Step 3: Implementar** `nucleo/vuupt_agentes.py` (timeout 30 s, header `Authorization: Bearer`, JSON; paginação igual a `regras/cadastro_motoristas._listar_tudo_vuupt`). Sem acento: `unicodedata.normalize("NFKD")`.
- [ ] **Step 4:** mesmo comando → OK.
- [ ] **Step 5: Commit** — `Motoristas: cliente da Vuupt pra criar agente e veiculo (POST /users e /vehicles)`

---

### Task 3: Mensagens de WhatsApp do cadastro pelo link

**Files:**
- Modify: `notificar_whatsapp.py`
- Test: `test_notificar_whatsapp_cadastro_link.py` (raiz, mesmo padrão de `test_notificar_whatsapp_cancelamento.py`)

**Interfaces:**
- Produces:
  - `ORIGEM_MOTORISTA = "motorista_boas_vindas"`; `_enviados_hoje` conta essa origem sozinha; `_teto` devolve `cfg.motoristas.teto_diario` (padrão 20) pra ela; o teto interno deixa de contá-la.
  - `texto_cadastro_link(cadastro: dict, link: str, pendencia: str | None, agora=None) -> str` — grupo: `🧑‍✈️ *Motorista novo pelo link* · dd/mm hh:mm`, nome (≤40), tipo + `refrigerado`/`seco`, `FIXO`/`ESPORADICO`, `iPhone` quando for, e `⚠️ <pendencia>` quando houver; link da ficha. ≤ 200.
  - `texto_cadastro_link_concluido(cadastro: dict, agora=None) -> str` — `✅ Cadastro de <nome> concluído na Vuupt (agente <id>)`.
  - `texto_boas_vindas_motorista(primeiro_nome: str, email: str, senha: str, link_app: str, iphone: bool) -> str` — Android: boas-vindas, link do app, e-mail e senha. iPhone: boas-vindas e "a equipe vai te chamar pra explicar como funcionam as entregas" (sem link nem senha).
  - `avisar_motorista_boas_vindas(cadastro_id: int, telefone: str, texto: str, config: dict, modo_teste=False, **kw) -> str` — destino individual `<numero>@c.us`; `cadastro_motorista_link.forcar_destino_whatsapp` presente e não vazio → manda pra esse número com `[teste → +<telefone>]` na 1ª linha; confere `integracao_openwa.numero_existe` (fora do modo teste) como `avisar_cliente_cadastro`; assinatura `cadastro_link:<id>:boas_vindas`. Devolve a situação de `despachar` ou `numero_sem_whatsapp`/`indeterminado`/`desligado`.

- [ ] **Step 1: Testes que falham:** tamanho ≤ 200 dos dois textos de grupo com nome de 60 letras e pendência longa; boas-vindas Android contém e-mail, senha e link, iPhone não contém senha nem link; `avisar_motorista_boas_vindas` com `forcar_destino_whatsapp="5511900000000"` chama `despachar` com `grupo_id="5511900000000@c.us"` e texto começando por `[teste → +5511998765432]`; teto: 20 envios `motorista_boas_vindas` no dia → o 21º volta `nao_enviado` e um aviso de grupo (`cadastro_motorista`) ainda sai.
- [ ] **Step 2:** rodar o teste → FAIL.
- [ ] **Step 3: Implementar** em `notificar_whatsapp.py` (sem acento nos comentários, como o arquivo). Atualizar a docstring do topo com a origem nova e a chave de config.
- [ ] **Step 4:** teste novo + os testes existentes do módulo → OK.
- [ ] **Step 5: Commit** — `WhatsApp: aviso do cadastro pelo link no grupo e boas-vindas ao motorista (piloto)`

---

### Task 4: Coluna POSICAO (fixo/esporádico) e marcação dos fixos

**Files:**
- Modify: `nucleo/banco.py` (`_migrar_colunas(conn, "bd_motoristas", [("POSICAO", "TEXT")])`)
- Modify: `regras/motoristas_banco.py` (`COLUNAS` ganha `"POSICAO"` no fim; CLI `marcar-posicao`)
- Modify: `regras/preferencias_motoristas.py` (`MotoristaPreferencias.posicao: str = "ESPORADICO"`, lida em `_construir_motorista`)
- Modify: `regras/cadastro_motoristas.py` (`_FORMULARIO["posicao"] = ("POSICAO", ...)`, aceita só `FIXO`/`ESPORADICO`/"" → ValueError no resto)
- Modify: `painel_agentes/motoristas.py` + `painel_agentes/templates/motoristas.html` (campo Posição no Editar e coluna na tabela)
- Test: `regras/test_motoristas_banco.py`, `regras/test_cadastro_motoristas.py`, `regras/test_preferencias_motoristas.py`

**Interfaces:**
- Produces: `motoristas_banco.marcar_posicao(conn, fixos: set[int], origem: str) -> int` — `FIXO` pra quem tem `AGENT_ID_VUUPT` em `fixos`, `ESPORADICO` pro resto, pelo `atualizar()` (fica no histórico); devolve quantas linhas mudaram. CLI: `py -3.11 -m regras.motoristas_banco marcar-posicao --fixos 31828,48907,... [--db X]`.
- `MotoristaPreferencias.posicao` ∈ {`"FIXO"`, `"ESPORADICO"`}; vazio/desconhecido → `"ESPORADICO"` (Hugo, 30/10: "todo o resto = esporádico").

- [ ] **Step 1: Testes que falham:** `inserir` com `POSICAO="FIXO"` → espelho xlsx tem a coluna `POSICAO`; `marcar_posicao({31828, 47424})` com 3 linhas (duas do 47424) marca 3 FIXO/ESPORADICO certos e grava histórico; `_construir_motorista` com `POSICAO` vazio → `"ESPORADICO"`, `"fixo"` → `"FIXO"`; `editar_motorista(..., {"posicao": "QUALQUER"})` → ValueError.
- [ ] **Step 2:** `py -3.11 -m unittest regras.test_motoristas_banco regras.test_cadastro_motoristas regras.test_preferencias_motoristas -v` → FAIL.
- [ ] **Step 3: Implementar.** No template, select "Posição" com Fixo/Esporádico no formulário de Editar (o JS já manda só o que mudou).
- [ ] **Step 4:** mesmos testes → OK; `painel_agentes`: `cd painel_agentes && py -3.11 -m unittest test_cadastros_motoristas -v` → OK.
- [ ] **Step 5: Commit** — `Motoristas: coluna POSICAO (fixo/esporadico) na tabela, no Editar e CLI marcar-posicao`

---

### Task 5: Processador idempotente + retentativa

**Files:**
- Create: `nucleo/cadastro_motorista_link.py`
- Create: `infra/stokki-cadastro-motorista-link.service`, `infra/stokki-cadastro-motorista-link.timer`
- Test: `nucleo/test_cadastro_motorista_link.py`

**Interfaces:**
- Consumes: Task 1 (colunas, `buscar_cadastro`), Task 2 (`VuuptAgentes`, `ErroVuupt`, `gerar_email`, `mesmo_telefone`, `base_operacional_mais_comum`), Task 3 (textos e `avisar_motorista_boas_vindas`), Task 4 (`posicao` em `cadastrar_motorista`), `regras.cadastro_motoristas.cadastrar_motorista`.
- Produces:
  - `processar(conn, config: dict, cadastro_id: int, *, vuupt=None, avisar_grupo=None, avisar_motorista=None, agora=None) -> dict` → `{"situacao": "concluido"|"pendente"|"conflito"|"ocupado", "erro": str|None}`.
  - `retentar(conn, config: dict, **mesmos_kw) -> list[dict]` — todos de `origem='link'` com `enviado_em` e sem `concluido_em`, `tentativas < 20`, sem conflito aberto.
  - `main(argv=None) -> int` — `--retentar`, `--modo-teste` (Vuupt e WhatsApp viram duplos que só logam), `--id N`.

**Ordem das etapas em `processar` (cada uma pula se já feita):**
1. Trava: `UPDATE motoristas_cadastros SET processando_em=? WHERE id=? AND (processando_em IS NULL OR processando_em < agora-10min)`; `rowcount == 0` → `"ocupado"`. Sempre limpa `processando_em` no fim (`finally`).
2. Conflito por telefone (só se `agent_id` vazio e `conflito_resolvido = 0`): `mesmo_telefone` contra `listar_agentes()` e contra `TELEFONE_MOTORISTA` da `bd_motoristas`. Bateu → grava `conflito_agent_id` (o da Vuupt, ou o `AGENT_ID_VUUPT` da linha) e vai pro aviso do grupo com pendência "telefone já é do agente <id> — resolver no painel"; devolve `"conflito"`.
3. Agente (se `agent_id` vazio): e-mail = `gerar_email(nome, ocupados)` com `ocupados` = e-mails de `listar_agentes()` + `vuupt_email` de outros cadastros + `EMAIL_MOTORISTA` da tabela; grava `vuupt_email` **antes** do POST. `ErroVuupt` 422 com `email` no corpo → acrescenta o e-mail aos ocupados e tenta o próximo (até 9). 422 com `password` → senha de 8 dígitos. Sucesso → grava `agent_id` e `vuupt_senha_digitos` na hora (commit).
   - Contra duplicata após timeout: antes do POST, se `vuupt_email` já estava gravado de uma tentativa anterior, procurar esse e-mail em `listar_agentes()`; achou → usa o id, não cria.
4. Veículo (se `vehicle_id` vazio): placa em `listar_veiculos()` → reaproveita; senão `criar_veiculo(placa, base_operacional_mais_comum(veiculos), agent_id)`. Grava `vehicle_id`; depois `ligar_veiculo`.
5. Tabela (se `bd_motorista_id` vazio): se já existe linha com esse `AGENT_ID_VUUPT`, usa o id dela; senão `cadastrar_motorista(config, {...}, origem="link")` com `ativo=True, max_rotas_dia=1, aceita_viagens=False, dias_disponiveis, zonas_preferidas, tipo_veiculo, telefone, email=vuupt_email, placa, cpf, vehicle_id, apenas_carga_seca=not refrigerado, sem_app=(celular=="IPHONE"), posicao`. Grava `bd_motorista_id`.
6. Aviso no grupo, uma vez (`aviso_grupo_em` vazio): `texto_cadastro_link` com a pendência da etapa que falhou (ou None). Se ele saiu com pendência e agora concluiu: `texto_cadastro_link_concluido` uma vez (`aviso_concluido_em`).
7. Boas-vindas (se `boas_vindas_situacao` vazio ou em `falhou`/`indeterminado`, e com `agent_id`): `avisar_motorista_boas_vindas`; grava a situação.
8. Concluído quando há `agent_id`, `vehicle_id`, `bd_motorista_id` e boas-vindas não está em `falhou`/`indeterminado` → `concluido_em`.
- Qualquer `ErroVuupt`/rede numa etapa: grava `ultimo_erro`, `tentativas += 1`, segue pro aviso do grupo (passo 6) e devolve `"pendente"`.

- [ ] **Step 1: Testes que falham** (Vuupt falsa em memória que conta chamadas; avisos como `mock`):
  - `test_fluxo_feliz`: cadastro completo enviado → 1 `criar_agente` com `jps@freshhub.com.br` e senha = 6 primeiros dígitos do CPF, 1 `criar_veiculo`, `ligar_veiculo`, linha na `bd_motoristas` com `ATIVO=SIM`, `APENAS_CARGA_SECA=SIM` (não refrigerado), `SEM_APP` vazio/NAO (Android), `POSICAO=FIXO`, `CPF_MOTORISTA` com zero à esquerda (use CPF `01234567890` — gerar um válido com DV no teste); aviso de grupo 1x sem pendência; boas-vindas 1x; `situacao == "concluido"`.
  - `test_duas_chamadas_um_agente`: `processar` 2x → `criar_agente` chamado 1x.
  - `test_trava_ocupado`: `processando_em` = agora − 2 min → `"ocupado"` sem chamar a Vuupt; − 11 min → processa.
  - `test_veiculo_falha_e_retenta`: `criar_veiculo` levanta `ErroVuupt(500)` → `"pendente"`, `agent_id` gravado, aviso do grupo com pendência; `retentar` depois → não recria o agente, cria o veículo, conclui e manda `texto_cadastro_link_concluido` 1x.
  - `test_timeout_depois_do_post`: `criar_agente` levanta erro de rede mas a Vuupt falsa registrou o agente → na retentativa acha pelo `vuupt_email` e não cria outro.
  - `test_email_colisao_na_vuupt`: 1º POST 422 `email` → 2º com `jps2@`.
  - `test_senha_recusada`: 422 `password` → 2º POST com 8 dígitos e `vuupt_senha_digitos = 8`; a boas-vindas leva a senha de 8.
  - `test_conflito_telefone`: agente da Vuupt com o mesmo telefone → nenhum POST, `conflito_agent_id` gravado, `"conflito"`, aviso com pendência; `retentar` ignora; com `conflito_resolvido=1` processa.
  - `test_placa_existente_reaproveita`: placa já em `listar_veiculos` → `criar_veiculo` não é chamado.
  - `test_iphone`: `celular=IPHONE` → `SEM_APP=SIM` e boas-vindas no texto de iPhone.
  - `test_tabela_ja_tem_agente`: linha com o mesmo `AGENT_ID_VUUPT` já existe → não insere outra.
- [ ] **Step 2:** `py -3.11 -m unittest nucleo.test_cadastro_motorista_link -v` → FAIL.
- [ ] **Step 3: Implementar** `nucleo/cadastro_motorista_link.py` (reconfigura `sys.stdout` pra UTF-8 no topo do `main`, como os scripts agendados). Timer: cópia do par `stokki-nucleo-sincronizar-servicos`, `ExecStart=.../venv/bin/python /opt/stokki-eventos/nucleo/cadastro_motorista_link.py --retentar`, `OnCalendar=*-*-* *:02,17,32,47:00`, `OnFailure=stokki-alerta-falha@%n.service`.
- [ ] **Step 4:** teste novo + `nucleo.test_cadastro_link` → OK.
- [ ] **Step 5: Commit** — `Motoristas: processador do cadastro pelo link (agente, veiculo, tabela, avisos) com retentativa a cada 15 min`

---

### Task 6: Fixo antes do esporádico na alocação

**Files:**
- Modify: `roteirizacao/alocacao_motoristas.py` (`selecionar_motorista_equitativo`, chave do `sort` e docstring)
- Test: `roteirizacao/test_alocacao_justa.py`

**Interfaces:**
- Consumes: `MotoristaPreferencias.posicao` (Task 4).
- Chave nova: `(0 if m.posicao == "FIXO" else 1, contagem_alocacoes_dia..., longas..., rotas_7d..., rotas_30d..., agent_id)`. Continua sendo só ordenação (nenhum filtro muda). `listar_motoristas_elegiveis`/`contar_motoristas_elegiveis` não mudam.

- [ ] **Step 1: Testes que falham:** fixo com 10 rotas em 7 d ganha de esporádico com 0; dois fixos → segue a regra de hoje (menos rotas em 7 d); fixo inelegível (zona errada) → esporádico elegível é escolhido; todos `ESPORADICO` → resultado idêntico ao de antes (mesmo caso de um teste já existente).
- [ ] **Step 2:** `py -3.11 -m unittest roteirizacao.test_alocacao_justa -v` → FAIL.
- [ ] **Step 3: Implementar** (comentário curto citando o Hugo, 28/10 e 02/11).
- [ ] **Step 4:** `py -3.11 -m unittest discover -s roteirizacao -p "test_*.py"` → OK (sem misturar com `painel_agentes`).
- [ ] **Step 5: Commit** — `Alocacao: motorista fixo tem preferencia sobre esporadico (Hugo, 28/10)`

---

### Task 7: Página do link e rotas na motorista-api

**Files:**
- Create: `nucleo/templates/cadastro_motorista.html`
- Modify: `nucleo/api_motorista.py`
- Test: `nucleo/test_api_cadastro_link.py`

**Interfaces:**
- Consumes: Task 1 (`criar_cadastro_link`, `guardar_documento`, `marcar_enviado`, `situacao_link`), Task 5 (`processar`).
- Produces (todas públicas, sem login; 503 `{"erro": "Cadastro indisponível no momento."}` com `cadastro_motorista_link.ativo` falso):
  - `GET /cadastro` → HTML (`render_template`, `url_for` com o prefixo do ProxyFix).
  - `POST /api/cadastro-link` (JSON dos passos 1 e 2) → 201 `{"id", "chave_envio"}`; usa `_freio_cadastro` como `/api/cadastro`.
  - `POST /api/cadastro/<id>/documentos` (já existe) aceita os 5 tipos.
  - `GET /api/cadastro/<id>/situacao?chave_envio=` → `situacao_link`.
  - `POST /api/cadastro/<id>/concluir` `{"chave_envio"}` → `marcar_enviado` + `processar` (erro no processar é logado, nunca vira 500: o cadastro já está salvo e o timer retenta) → `{"ok": true, "primeiro_nome"}`.
- A página: 3 passos + tela final, textos e campos da spec (seção "O formulário") mais **"Seu celular é Android ou iPhone?"** no passo 1. Dias com seg–sex marcados; 9 zonas de `cadastro_motorista.ZONAS` e tipos de `regras.tipo_veiculo.TIPOS_VEICULO` passados pelo template. Temperatura só aparece com refrigerado = Sim. Selfie com `accept="image/*" capture="user"`; documentos `accept="image/*,application/pdf"`. Fotos reduzidas no navegador antes de subir (canvas, lado maior 1600 px, JPEG 0,8; mesma ideia de `painel_agentes/templates/_reduzir_imagem.js.html`); PDF e HEIC sobem como estão. `localStorage` guarda os campos digitados e `{id, chave_envio}` (try/catch em toda leitura/escrita); ao abrir com `id` salvo, chama `situacao` e retoma no passo 3 com os documentos já enviados marcados. Botão Enviar desabilita no clique. Marca Fresh Log, sem biblioteca externa, cabe em 4G.

- [ ] **Step 1: Testes que falham** (cliente Flask, `criar_app({...})` como em `nucleo/test_api_motorista.py`, `processar` trocado por `mock`): desligado → 503 em `GET /cadastro` e `POST /api/cadastro-link`; ligado → `GET /cadastro` 200 com `ZONA SUL` no HTML; fluxo criar → 5 documentos → `situacao` mostra os 5 → `concluir` 200 e `processar` chamado 1x; `processar` levantando → `concluir` ainda 200; `concluir` antes dos documentos → 400; 6º `POST /api/cadastro-link` do mesmo IP na hora → 429.
- [ ] **Step 2:** `py -3.11 -m unittest nucleo.test_api_cadastro_link -v` → FAIL.
- [ ] **Step 3: Implementar** rotas e página.
- [ ] **Step 4:** teste novo + `nucleo.test_api_motorista` + `nucleo.test_cadastro_motorista` → OK.
- [ ] **Step 5: Commit** — `Motoristas: pagina do cadastro pelo link (app.freshhub.com.br/motorista/cadastro), atras de cadastro_motorista_link.ativo`

---

### Task 8: Seção "Cadastros pelo link" na tela /motoristas

**Files:**
- Modify: `painel_agentes/motoristas.py` (`cadastros_link() -> list[dict]`: origem link, `enviado_em` nos últimos 30 dias ou sem `concluido_em`, mais novos primeiro)
- Modify: `painel_agentes/painel_agentes.py` (rotas abaixo)
- Modify: `painel_agentes/templates/motoristas.html`
- Modify: `nucleo/cadastro_motorista_link.py` (`resolver_conflito(conn, cadastro_id: int, usar_agent_id: int | None, usuario: str) -> dict`)
- Test: `painel_agentes/test_cadastros_motoristas.py`, `nucleo/test_cadastro_motorista_link.py`

**Interfaces:**
- `resolver_conflito`: `usar_agent_id` informado → grava como `agent_id` (o processador pula a criação do agente) e `conflito_resolvido=1`; `None` → só `conflito_resolvido=1` (cria agente novo). Depois chama `processar`.
- Rotas (nível `total`, `@exige_mesma_origem`): `POST /api/motoristas/cadastros-link/<id>/resolver` `{"usar_agent_id": int|null}`; `POST /api/motoristas/cadastros-link/<id>/tentar` (chama `processar`). Os documentos usam a rota que já existe `api_cadastro_documento` (os 5 tipos já valem pela Task 1).
- Ficha na tela (níveis que veem `/motoristas`; botões e documentos só `pode_aprovar`): nome, CPF, nascimento, telefone, Pix, emite NF, posição, celular, dias, zonas, veículo (tipo, placa, modelo, ano, refrigerado e temperatura, é dele?), e-mail Vuupt, situação de cada etapa (agente, veículo, tabela, aviso, boas-vindas) com `ultimo_erro`, links pros 5 documentos (selfie como miniatura). Conflito → texto "Telefone já é do agente <id> na Vuupt" e botões "É o mesmo: usar agente <id>" / "É outra pessoa: criar agente novo". Pendente sem conflito → "Tentar de novo".

- [ ] **Step 1: Testes que falham:** `resolver_conflito(..., 999)` → `agent_id == 999`, `criar_agente` não chamado; `resolver_conflito(..., None)` → cria agente; rota `resolver` com nível `operador` → 403; tela `/motoristas` com um cadastro de link mostra o nome e "Tentar de novo" pro `total`.
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_cadastros_motoristas -v` e `py -3.11 -m unittest nucleo.test_cadastro_motorista_link -v` → FAIL.
- [ ] **Step 3: Implementar.** Texto da ajuda das pendências do app continua (fluxo do app não muda).
- [ ] **Step 4:** mesmos comandos → OK.
- [ ] **Step 5: Commit** — `Painel: cadastros pelo link na tela /motoristas (ficha, documentos, resolver telefone repetido, tentar de novo)`

---

### Task 9: Teste da tela de ponta a ponta (local) + mapa

**Files:**
- Create (fora do repo): roteiro Playwright no scratchpad da sessão; não é commitado
- Modify: `MAPA_DO_SISTEMA.txt` (página `/motorista/cadastro`, módulos `nucleo/cadastro_motorista_link.py` e `nucleo/vuupt_agentes.py`, timer novo, colunas novas, `POSICAO`, chaves de config)

**Roteiro (é o "testar com a tela" do Hugo):**
- [ ] **Step 1:** subir a motorista-api local em porta de teste com banco temporário, `cadastro_motorista_link.ativo: true`, Vuupt e WhatsApp em `--modo-teste` (duplos que só logam): `py -3.11 -c "..."` com `criar_app(config_de_teste)` na porta **8098** (nunca 8073).
- [ ] **Step 2:** Playwright em viewport de celular (390×844, toque): preencher passo 1 (inclusive iPhone), passo 2 (refrigerado Sim → aparece temperatura, aceita −18), passo 3 com 5 arquivos (uma foto de 8 MB → conferir no log que subiu < 1,5 MB; um PDF); recarregar a página no meio do passo 3 → volta no passo 3 com os já enviados marcados; Enviar 2x rápido → um cadastro, tela final com o primeiro nome. Screenshots de cada passo no scratchpad.
- [ ] **Step 3:** abrir `/motoristas` do painel local na porta **8099** contra o mesmo banco e conferir a ficha (5 documentos abrem, etapas concluídas, linha nova na tabela com FIXO/SEM_APP certos).
- [ ] **Step 4:** caso de conflito: cadastrar com telefone de um motorista da tabela → ficha mostra o conflito; "criar agente novo" conclui.
- [ ] **Step 5:** `py -3.11 -m py_compile` em todos os arquivos tocados; todos os testes das Tasks 1–8 juntos (respeitando a separação `painel_agentes`).
- [ ] **Step 6: Commit** — `git add MAPA_DO_SISTEMA.txt` · `Mapa: cadastro do motorista pelo link`

---

### Depois do OK do Hugo (deploy — não executar sem ele pedir)

1. Commit/push, `deploy-vps` (pull, chown, copiar o par `stokki-cadastro-motorista-link` de `infra/`, `daemon-reload`, `enable --now` do timer, `restart motorista-api painel-agentes`).
2. `sudo -u www-data venv/bin/python -m regras.motoristas_banco marcar-posicao --fixos 31828,48907,47339,42642,45594,29253,45422,47083,47039,47424,46454,47084` e conferir que 13 linhas viraram FIXO (o 47424 tem 2).
3. Config da VPS (o Hugo ou com o OK dele): `cadastro_motorista_link: {ativo: true, forcar_destino_whatsapp: "<número do Hugo>", link_app_vuupt: "https://play.google.com/store/apps/details?id=com.vuupt.vuuptdriver"}` e `whatsapp_notificacoes.motoristas.teto_diario: 20`.
4. Prova real com agente descartável (autorizado em 02/11): cadastro "Teste Cadastro Link" (e-mail gerado `tcl@freshhub.com.br`), CPF válido de teste e placa de teste; conferir agente+veículo na Vuupt, linha na tabela, aviso no grupo, boas-vindas no número do Hugo; depois `apagar_veiculo`, `apagar_agente`, `ATIVO=NAO` na linha de teste (ou apagar a linha) e marcar o cadastro como RECUSADO "teste".
5. Mandar o link ao Hugo pra ele testar no celular dele.
