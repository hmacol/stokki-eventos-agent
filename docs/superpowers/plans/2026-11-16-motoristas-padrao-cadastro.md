# Motoristas: padrão único de cadastro — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Todo motorista com a ficha do padrão novo (preenchida por ele num link pessoal), pendências visíveis na /motoristas, grupos de WhatsApp identificados/renomeados, envio do link em lote e ações Inativar, Excluir e Resetar PIN.

**Architecture:** Três módulos novos no `nucleo/` (ficha+link, grupos+envio, ações), que reusam `motoristas_cadastros`, a página pública do cadastro e o `notificar_whatsapp.despachar`. O painel só chama esses módulos por rotas finas em `painel_agentes.py`. Tudo atrás de `atualizacao_motoristas.*`, ausente = desligado.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db` via `nucleo/banco.py`), `unittest`, gateway OpenWA por `integracao_openwa.py`.

**Spec:** `docs/superpowers/specs/2026-11-16-motoristas-padrao-cadastro-design.md`

## Global Constraints

- Idioma: português em código, comentários e mensagens. Seguir o padrão de acento do arquivo que está sendo editado.
- Rodar com `py -3.11`. Testes: `py -3.11 -m unittest <modulo> -v`; os de `painel_agentes` rodam de dentro de `painel_agentes/` e nunca no mesmo comando que `nucleo`/`regras`.
- **Sem commit**: o Hugo pede commit explicitamente. Cada tarefa termina com os testes verdes, não com commit.
- Nada sai de verdade: `atualizacao_motoristas.ativo` ausente → página 503; `forcar_destino_whatsapp` ausente → não envia; `renomear_grupos` ausente → não renomeia.
- O link de atualização nunca chama a Vuupt. Excluir nunca chama a Vuupt.
- Não repassar pelo link: zonas, dias, posição, tipo de veículo, apenas carga seca.
- Validade do link 15 dias (`validade_dias`), teto diário 15 (`teto_diario`), 5 erros de CPF cancelam o link.
- Documentos: `("cnh", "crlv", "selfie", "antt", "residencia")`. Para enviar a ficha de atualização bastam `cnh`, `crlv`, `selfie`; `antt` e `residencia` não travam e continuam como pendência.
- Não tocar em arquivos fora da tarefa; rota nova de tela entra em `acesso_telas.TELAS`; atualizar `MAPA_DO_SISTEMA.txt` (Tarefa 8).
- Ler o config só por `nucleo.api_motorista._carregar_config()` / `painel_agentes.motoristas._carregar_config()`; nunca imprimir valores dele.

## Review Focus

1. Mesma pessoa em duas linhas (mesmo CPF, ou mesmo `AGENT_ID` sem CPF): recebe um link só e os selos saem iguais nas duas linhas. Teste na Tarefa 1.
2. Link aberto duas vezes / em dois aparelhos, ou enviado duas vezes: o segundo envio não duplica ficha nem repasse. Teste na Tarefa 2.
3. CPF digitado que já é de outro motorista (linha sem CPF): recusa sem gravar e conta como erro. Teste na Tarefa 2.
4. Gateway fora do ar ou devolvendo formato inesperado ao listar grupos / renomear: a tela mostra o erro e nada é gravado. Teste na Tarefa 5.
5. Lote de envio interrompido no meio (teto atingido, falha no 3º): os já enviados ficam marcados, os demais continuam elegíveis, nenhum link fica "em aberto" sem mensagem. Teste na Tarefa 6.

---

### Task 1: Banco, pessoas, ficha vigente e pendências

**Files:**
- Modify: `nucleo/banco.py` (junto de `_COLUNAS_CADASTRO_LINK`: duas tabelas novas no mesmo esquema de migração aditiva do arquivo)
- Create: `nucleo/atualizacao_motorista.py`
- Test: `nucleo/test_atualizacao_motorista.py` (mesma montagem de `nucleo/test_cadastro_link.py`: SQLite temporário, `banco.conectar`)

**Interfaces:**
- Produces:
  - Tabela `motoristas_links_atualizacao(id PK, bd_motorista_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE, criado_em, criado_por, expira_em, aberto_em, concluido_em, cancelado_em, tentativas_cpf INTEGER NOT NULL DEFAULT 0, cadastro_id INTEGER, envio_destino TEXT, envio_situacao TEXT, enviado_em, piloto INTEGER NOT NULL DEFAULT 0)`
  - Tabela `motoristas_grupos_whatsapp(bd_motorista_id INTEGER PRIMARY KEY, grupo_id TEXT NOT NULL, grupo_nome TEXT, nome_anterior TEXT, como TEXT NOT NULL, confirmado_por TEXT, confirmado_em TEXT, renomeado_em TEXT)`
  - `ORIGEM_ATUALIZACAO = "atualizacao"`
  - `DOCUMENTOS_MINIMOS = ("cnh", "crlv", "selfie")`
  - `pessoas(conn) -> list[dict]`: cada item `{"principal_id": int, "ids": [int], "agent_ids": [int], "cpf": str|None, "nome": str, "ativo": bool, "linhas": [dict]}`. Linhas da `bd_motoristas` com mesmo CPF formam uma pessoa; sem CPF, as de mesmo `AGENT_ID_VUUPT`. `principal_id` = menor `id`. `ativo` = alguma linha ativa.
  - `vincular_fichas(conn) -> int`: preenche `bd_motorista_id` em `motoristas_cadastros` APROVADO sem vínculo, por `agent_id`, senão por CPF; aponta para o `principal_id`. Idempotente; devolve quantas vinculou.
  - `ficha_vigente(conn, principal_id: int) -> dict | None`: `cadastro_motorista._montar` da linha mais recente (maior `id`) com `bd_motorista_id` em `ids` da pessoa e status APROVADO.
  - `pendencias(conn, pessoa: dict) -> dict`: `{"documentos": [str], "campos": [str], "veiculo_diferente": bool, "falta_cpf": bool}`. `documentos` = tipos de `DOCUMENTOS` sem arquivo na ficha vigente (todos os 5 se não há ficha). `campos` ⊆ `["CPF", "telefone", "PIX", "nascimento", "placa", "modelo do veículo", "ano do veículo"]`: CPF/telefone/placa valem da linha ou da ficha; PIX da ficha ou de `motoristas.chave_pix`. `veiculo_diferente` = ficha tem `tipo_veiculo` e ele difere do `TIPO_VEICULO` não vazio da linha principal (comparar pelo código canônico de `regras.tipos_veiculo.tipo_por_codigo`; conferir o nome do módulo no import de `nucleo/cadastro_motorista.py`).

- [ ] **Step 1: testes que falham** em `nucleo/test_atualizacao_motorista.py`:
  - `test_pessoas_junta_mesmo_cpf_e_mesmo_agent_sem_cpf`: 4 linhas (A cpf X agent 1; B cpf X agent 2; C sem cpf agent 3; D sem cpf agent 3) → 2 pessoas, `principal_id` = menor id de cada, `agent_ids` `[1, 2]` e `[3]`.
  - `test_vincular_fichas_do_app_por_agent_id_e_idempotente`: ficha APROVADA origem `app` com `agent_id` de uma linha e `bd_motorista_id` NULL → 1; segunda chamada → 0.
  - `test_ficha_vigente_e_a_aprovada_mais_recente`: duas APROVADAS e uma PENDENTE mais nova → devolve a APROVADA de maior id.
  - `test_pendencias_sem_ficha_lista_os_cinco_documentos`.
  - `test_pendencias_ficha_do_app_falta_selfie_antt_residencia_e_nascimento`.
  - `test_pendencias_iguais_nas_duas_linhas_da_pessoa` (Review Focus 1): `pendencias` recebe a pessoa, e o resultado não muda conforme a linha.
  - `test_pix_do_login_conta` e `test_veiculo_diferente`.
- [ ] **Step 2:** `py -3.11 -m unittest nucleo.test_atualizacao_motorista -v` → falha por módulo inexistente.
- [ ] **Step 3:** criar as tabelas em `nucleo/banco.py` e implementar as funções acima.
- [ ] **Step 4:** mesmo comando → OK. Rodar também `py -3.11 -m unittest nucleo.test_cadastro_link nucleo.test_cadastro_motorista nucleo.test_cadastro_motorista_link -v` → OK (nada quebrou).

### Task 2: Link pessoal — gerar, abrir com CPF, gravar, concluir, repassar

**Files:**
- Modify: `nucleo/atualizacao_motorista.py`
- Modify: `nucleo/cadastro_motorista.py` (só extrair de `criar_cadastro_link` a validação dos campos comuns para uma função `_validar_dados_pessoa_veiculo(dados) -> dict`, usada pelas duas; sem mudar comportamento do link novo)
- Test: `nucleo/test_atualizacao_motorista.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `class LinkInvalido(Exception)` com `.mensagem` e `.codigo` (404 não existe/cancelado, 410 vencido ou concluído, 403 CPF errado, 409 CPF de outro, 429 bloqueado).
  - `gerar_link(conn, principal_id: int, usuario: str, *, validade_dias: int = 15, piloto: bool = False, agora=None) -> dict` → `{"id": int, "token": str, "expira_em": str}`. Token `secrets.token_urlsafe(32)`; grava só `sha256` hex. Cancela links anteriores não concluídos da mesma pessoa.
  - `link_em_aberto(conn, principal_id: int, agora=None) -> dict | None` (não cancelado, não concluído, não vencido, `piloto = 0`).
  - `abrir(conn, token: str, cpf: str, agora=None) -> dict` → `{"cadastro_id": int, "chave_envio": str, "dados": dict, "documentos": {tipo: bool}}`. Confere o CPF (regra do spec, seção 2); cria (ou reaproveita) a linha rascunho em `motoristas_cadastros` com `origem = 'atualizacao'`, status PENDENTE, `bd_motorista_id = principal_id`, pré-preenchida com linha + ficha vigente; copia para a pasta do rascunho os arquivos de documento da ficha vigente. Marca `aberto_em`. Segunda chamada devolve o mesmo rascunho.
  - `gravar(conn, token: str, chave_envio: str, dados: dict) -> dict`: valida com `_validar_dados_pessoa_veiculo` (sem zonas/dias/posição/celular) e atualiza o rascunho.
  - `concluir(conn, config: dict, token: str, chave_envio: str, agora=None) -> dict` → `{"ok": True, "pendentes": [tipos de documento ainda faltando]}`. Exige `DOCUMENTOS_MINIMOS`; rascunho vira APROVADO com `enviado_em`/`concluido_em`; link ganha `concluido_em`; repasses: CPF (só se vazio), telefone e e-mail em todas as linhas da pessoa via `motoristas_banco.atualizar(..., origem="link_atualizacao")`, placa só se a pessoa tem uma linha, PIX em `motoristas.chave_pix` se o login existe; regenera o espelho como `regras/cadastro_motoristas._espelhar` faz. Idempotente: concluir de novo devolve o mesmo resultado sem repetir repasse.
  - `estado_link(conn, principal_id: int, agora=None) -> str | None`: um de `"enviado"`, `"aberto"`, `"vencido"`, `"concluido"`, `None`.

- [ ] **Step 1: testes que falham:**
  - `test_gerar_guarda_so_o_hash_e_cancela_o_anterior`.
  - `test_abrir_cpf_errado_cinco_vezes_cancela` (5ª tentativa → `LinkInvalido` 429 e `cancelado_em` preenchido).
  - `test_abrir_linha_sem_cpf_aceita_cpf_valido_e_recusa_o_de_outro_motorista` (Review Focus 3: recusa com 409, nada gravado, `tentativas_cpf` +1).
  - `test_abrir_duas_vezes_devolve_o_mesmo_rascunho` e `test_abrir_traz_documentos_da_ficha_vigente`.
  - `test_abrir_vencido_410`.
  - `test_concluir_sem_selfie_recusa`; `test_concluir_sem_antt_e_residencia_aceita_e_lista_pendentes` → `pendentes == ["antt", "residencia"]`.
  - `test_concluir_repassa_telefone_email_cpf_e_pix_mas_nao_tipo_veiculo_nem_zonas`.
  - `test_concluir_pessoa_com_duas_linhas_nao_repassa_placa`.
  - `test_concluir_duas_vezes_nao_duplica` (Review Focus 2: uma ficha APROVADA, histórico em `bd_motoristas_alteracoes` com as mesmas linhas da 1ª vez).
  - `test_processador_do_link_e_fila_do_app_ignoram_atualizacao`: `cadastro_motorista_link._ignorar(ficha)` é True e `cadastro_motorista.listar_pendentes` não traz o rascunho.
- [ ] **Step 2:** rodar → falham.
- [ ] **Step 3:** implementar.
- [ ] **Step 4:** `py -3.11 -m unittest nucleo.test_atualizacao_motorista nucleo.test_cadastro_link -v` → OK.

### Task 3: Página pública do link

**Files:**
- Modify: `nucleo/api_motorista.py` (bloco do cadastro pelo link, ~linhas 320-383)
- Modify: `nucleo/templates/cadastro_motorista.html`
- Test: `nucleo/test_api_atualizacao_motorista.py` (cliente de teste do Flask, no padrão dos testes de API que já existem em `nucleo/`)

**Interfaces:**
- Consumes: Task 2.
- Produces (todas respondem 503 com mensagem amigável se `atualizacao_motoristas.ativo` não for verdadeiro; não dependem de `cadastro_motorista_link.ativo`):
  - `GET /atualizar/<token>` → template em modo atualizar (`modo="atualizar"`, sem zonas/dias/posição/celular; primeiro passo pede o CPF). Token inexistente → página "link inválido ou vencido" (não vazar o motivo).
  - `POST /api/atualizacao/<token>/abrir` corpo `{"cpf"}` → retorno de `abrir`.
  - `POST /api/atualizacao/<token>/dados` corpo `{"chave_envio", ...campos}` → `gravar`.
  - Documentos: a rota existente `POST /api/cadastro/<id>/documentos` com a `chave_envio` do rascunho (já aceita qualquer cadastro PENDENTE); não avisa o grupo para origem `atualizacao`.
  - `POST /api/atualizacao/<token>/concluir` corpo `{"chave_envio"}` → `concluir`.
  - `LinkInvalido` tratado por `errorhandler` igual ao de `CadastroInvalido`. O token não vai para o log/journal (mesmo cuidado da linha ~137 com `chave_envio`).
  - Freio: `_freio("atualizacao")` nas três rotas POST.

- [ ] **Step 1: testes que falham:** `test_desligado_503`; `test_fluxo_completo` (abrir → dados → 3 documentos → concluir → 200 e ficha APROVADA); `test_token_invalido_404_sem_detalhe`; `test_documento_de_atualizacao_nao_avisa_grupo` (mock em `_avisar_whatsapp` não chamado); `test_link_novo_continua_503_independente` (com `atualizacao_motoristas.ativo` ligado e `cadastro_motorista_link` ausente, `GET /cadastro` segue 503).
- [ ] **Step 2:** rodar → falham.
- [ ] **Step 3:** implementar rotas e o modo do template. No modo atualizar: documento já recebido mostra "já recebido" com botão "trocar"; tela final lista o que ficou pendente ("Faltou: ANTT. Você pode mandar depois, peça um link novo à Fresh Log.").
- [ ] **Step 4:** testes do passo 1 + `py -3.11 -m unittest nucleo.test_cadastro_link -v` → OK.

### Task 4: Ações — Inativar/Reativar, Excluir, Resetar PIN

**Files:**
- Create: `nucleo/acoes_motorista.py`
- Modify: `regras/motoristas_banco.py` (função `apagar`)
- Modify: `painel_agentes/painel_agentes.py` (junto de `api_editar_motorista`, ~linha 4636)
- Test: `nucleo/test_acoes_motorista.py`, `regras/test_motoristas_banco.py`

**Interfaces:**
- Consumes: Task 1 (`pessoas`), Task 2 (cancelar links).
- Produces:
  - `motoristas_banco.apagar(conn, motorista_id: int, origem: str) -> dict`: apaga a linha e grava em `bd_motoristas_alteracoes` um registro `campo="EXCLUIDO"`, `valor_antigo` = nome; devolve a linha apagada.
  - `acoes_motorista.rotas_do_agente(conn, agent_id: int) -> int`: soma de linhas em `nucleo_rotas`, `rascunhos_rota`, `baixas_sem_app` e `rotas` com aquele agente; tabela que não existe conta 0 (conferir o nome da coluna do agente em cada uma antes de escrever a consulta).
  - `class AcaoRecusada(Exception)` com `.mensagem`, `.codigo`.
  - `definir_ativo(conn, config, motorista_id: int, ativo: bool, usuario: str) -> dict`: usa `regras.cadastro_motoristas.editar_motorista` com `{"ativo": ...}`; ao inativar, cancela link em aberto da pessoa se ela ficou sem linha ativa.
  - `excluir(conn, config, motorista_id: int, usuario: str) -> dict` → `{"ok": True, "login_apagado": bool, "fichas": int}`. Recusa (409, "tem N rotas no histórico: inative em vez de excluir") se `rotas_do_agente > 0`. Apaga a linha; se era a última linha da pessoa: apaga o login em `motoristas` (CPF), cancela links, apaga o vínculo de grupo, marca fichas como RECUSADO motivo `"excluído no painel"` e apaga `dados/cadastros_motoristas/<id>/`. Regenera o espelho.
  - `resetar_pin(conn, motorista_id: int, usuario: str) -> dict` → `{"cpf": str, "pin": str, "criado": bool}`. Sem CPF → `AcaoRecusada(400, "falta o CPF")`. PIN de `cadastro_motorista.sortear_pin()`; grava com `auth.criar_ou_atualizar_motorista(..., pin=pin, trocar_pin=True)` preservando perfil, `agent_id` e `ve_financeiro` do login existente; registra em `bd_motoristas_alteracoes` `campo="PIN_RESETADO"` sem o valor do PIN.
  - Rotas (todas `@exige_mesma_origem`): `POST /api/motoristas/<int:id>/ativo` `{"ativo": bool}` e `POST /api/motoristas/<int:id>/resetar-pin` com `niveis=("total", "operador", "logistica")`; `DELETE /api/motoristas/<int:id>` com `niveis=("total",)`.

- [ ] **Step 1: testes que falham:** `test_excluir_com_rota_recusa_409_e_nao_apaga`; `test_excluir_sem_rota_apaga_linha_login_fichas_e_arquivos`; `test_excluir_uma_de_duas_linhas_mantem_login_e_ficha`; `test_excluir_nao_chama_vuupt` (mock em `requests` não chamado); `test_resetar_pin_marca_trocar_pin_e_derruba_sessao` (token emitido antes deixa de validar em `auth.motorista_do_token_acesso`); `test_resetar_pin_sem_login_cria` (`criado` True); `test_resetar_pin_sem_cpf_recusa`; `test_resetar_pin_nao_grava_pin_no_historico`; `test_inativar_cancela_link_em_aberto`; `test_rotas_do_agente_tabela_ausente_conta_zero`.
- [ ] **Step 2:** rodar → falham.
- [ ] **Step 3:** implementar módulo e rotas.
- [ ] **Step 4:** `py -3.11 -m unittest nucleo.test_acoes_motorista regras.test_motoristas_banco regras.test_cadastro_motoristas -v` → OK.

### Task 5: Grupos do WhatsApp — ler, sugerir, confirmar, renomear

**Files:**
- Modify: `integracao_openwa.py`
- Create: `nucleo/grupos_motoristas.py`
- Test: `test_integracao_openwa.py` (acrescentar), `nucleo/test_grupos_motoristas.py`

**Interfaces:**
- Consumes: Task 1 (`pessoas`).
- Produces:
  - `integracao_openwa.listar_grupos(cfg) -> list[dict] | None` (`GET /sessions/{sessao}/groups`, itens `{"id", "name"}`; aceita lista pura ou `{"data": [...]}`; falha → `None`).
  - `integracao_openwa.grupo(cfg, grupo_id) -> dict | None` (`GET /sessions/{sessao}/groups/{id}`: `name`, `locked`, `participants[{id, number, isAdmin}]`).
  - `integracao_openwa.renomear_grupo(cfg, grupo_id, nome) -> tuple[bool, str | None]` (`PUT /sessions/{sessao}/groups/{id}/subject`, corpo `{"subject": nome}`; antes de implementar, ler `src/modules/group/group.controller.ts:382` e o DTO no `/opt/openwa` da VPS para confirmar o nome do campo).
  - `grupos_motoristas.PREFIXOS = ("FRESHLOG +", "FRESH +")`
  - `grupos_motoristas.sugerir(conn, cfg_openwa, *, grupo=integracao_openwa.grupo, listar=integracao_openwa.listar_grupos) -> dict` → `{"erro": str|None, "pessoas": [{"principal_id", "nome", "confirmado": {...}|None, "sugestoes": [{"grupo_id", "grupo_nome", "como": "telefone"|"nome"}]}], "grupos": [{"grupo_id", "grupo_nome"}]}`. Só grupos cujo nome (sem espaços e caracteres invisíveis à esquerda, maiúsculo) começa com um dos `PREFIXOS`. "telefone": últimos 11 dígitos do `TELEFONE_MOTORISTA` (ou 10) batem com `number` de um participante. "nome": o primeiro nome do motorista (sem acento, maiúsculo) aparece como palavra no trecho do nome do grupo entre o prefixo e o " - ". Sugestões por telefone vêm antes.
  - `grupos_motoristas.confirmar(conn, principal_id: int, grupo_id: str, grupo_nome: str, como: str, usuario: str) -> None` (upsert; `grupo_id` vazio remove o vínculo).
  - `grupos_motoristas.nome_novo(nome_atual: str, nome_completo: str) -> str`: `"FRESHLOG + " + nome_completo.upper()` + (`" - " + sufixo` se o nome atual tem " - "; sufixo = tudo depois do primeiro " - " que vem após o prefixo). Máximo 100 caracteres (corta o sufixo, nunca o nome).
  - `grupos_motoristas.previa_renomear(conn, cfg_openwa, numero_proprio: str | None = None, *, grupo=...) -> list[dict]` → por vínculo confirmado: `{"principal_id", "grupo_id", "atual", "novo", "situacao"}` com `situacao` ∈ `"renomear"`, `"ja_no_padrao"`, `"dupla"` (mesmo `grupo_id` confirmado para 2+ pessoas), `"sem_permissao"` (`locked` e o participante do número conectado não é admin; se não der para saber qual participante é o número conectado, tratar `locked` como `sem_permissao`), `"erro"`.
  - `grupos_motoristas.renomear(conn, config, usuario: str, principal_ids: list[int], *, modo_teste=False, dormir=time.sleep, ...) -> list[dict]`: recusa tudo (`"desligado"`) se `atualizacao_motoristas.renomear_grupos` não for verdadeiro; só os de situação `"renomear"`; 3 s entre chamadas; grava `nome_anterior`, `grupo_nome`, `renomeado_em`. `modo_teste` devolve a prévia sem chamar o gateway.

- [ ] **Step 1: testes que falham:**
  - openwa: `test_listar_grupos_aceita_lista_e_envelope_data`; `test_listar_grupos_falha_devolve_none`; `test_renomear_grupo_put_subject`.
  - grupos: `test_sugere_por_telefone_antes_de_nome`; `test_dois_rodrigos_viram_sugestao_fraca_para_os_dois_grupos`; `test_ignora_grupo_fora_do_padrao` ("Vida Veg & Freshlog", "Rotas Freshlog"); `test_prefixo_com_caractere_invisivel_e_minusculas`; `test_gateway_fora_devolve_erro_e_nao_grava` (Review Focus 4: `listar` devolve `None` → `erro` preenchido, tabela intacta; e `grupo` devolvendo `{}` não derruba); `test_nome_novo`: `("FRESHLOG + EWERTON - REFRIGERADO", "Ewerton de Paula Abreu")` → `"FRESHLOG + EWERTON DE PAULA ABREU - REFRIGERADO"`, `("FRESH + SAMUEL - FIO -10", "Samuel Souza")` → `"FRESHLOG + SAMUEL SOUZA - FIO -10"`, nome sem " - " → sem sufixo; `test_previa_marca_dupla_e_ja_no_padrao`; `test_renomear_desligado_nao_chama_gateway`; `test_renomear_guarda_nome_anterior`; `test_renomear_falha_no_meio_mantem_os_feitos`.
- [ ] **Step 2:** rodar → falham.
- [ ] **Step 3:** implementar.
- [ ] **Step 4:** `py -3.11 -m unittest test_integracao_openwa nucleo.test_grupos_motoristas -v` → OK.

### Task 6: Envio do link em lote

**Files:**
- Modify: `notificar_whatsapp.py` (junto de `avisar_motorista_boas_vindas`, ~linha 1188, e de `_teto`, ~linha 629)
- Modify: `nucleo/grupos_motoristas.py`
- Test: `test_notificar_whatsapp_atualizacao.py` (raiz, no padrão de `test_notificar_whatsapp_cadastro_link.py`), `nucleo/test_grupos_motoristas.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 5.
- Produces:
  - `notificar_whatsapp.ORIGEM_MOTORISTA_ATUALIZACAO = "motorista_atualizacao"`; `_teto` devolve `atualizacao_motoristas.teto_diario` (padrão 15) para essa origem.
  - `notificar_whatsapp.texto_link_atualizacao(primeiro_nome: str, link: str, expira: date) -> str` — texto fixo do spec: `"Olá, {primeiro_nome}! Estamos atualizando o cadastro dos motoristas da Fresh Log. Preencha pelo link (leva 5 minutos, tenha CNH e documento do veículo em mãos): {link}. Vale até {dd/mm}."`
  - `notificar_whatsapp.avisar_motorista_link_atualizacao(link_id: int, grupo_id: str, grupo_nome: str, texto: str, config: dict, modo_teste: bool = False, **kw) -> tuple[str, bool]` → `(situacao, piloto)`. `forcar_destino_whatsapp` ausente em `atualizacao_motoristas` → `("desligado", False)`; com número → manda para `<numero>@c.us` com `"[teste → grupo {grupo_nome}]\n"` na frente, `piloto=True`; presente e vazio → manda no `grupo_id`. Assinatura `link_atualizacao:<link_id>`. Depende de `gateway_clientes_ok(config)`.
  - `grupos_motoristas.rotas_30d(conn, hoje=None) -> dict[int, int]`: `agent_id` → rotas em `nucleo_rotas` com `data_rota` nos últimos 30 dias.
  - `grupos_motoristas.candidatos_envio(conn, hoje=None) -> list[dict]`: pessoas ativas, com alguma pendência (`documentos` ou `campos`), com CPF, com grupo confirmado, sem `link_em_aberto`; cada item `{"principal_id", "nome", "rotas_30d", "grupo_nome", "pendencias": dict}`; ordem: mais rotas primeiro, desempate por nome.
  - `grupos_motoristas.enviar_lote(conn, config, principal_ids: list[int], usuario: str, url_base: str, *, modo_teste=False, avisar=notificar_whatsapp.avisar_motorista_link_atualizacao) -> list[dict]` → por pessoa `{"principal_id", "situacao"}`. Para cada um, na ordem: gera o link (com `piloto` conforme o config), envia, grava `envio_destino`/`envio_situacao`/`enviado_em`. Se a situação não for `"enviado"` (nem `"simulado"` em modo teste), cancela o link recém-gerado. Situação `"teto"` (ou equivalente devolvido por `despachar`; conferir o texto exato em `_motivo_para_nao_enviar`) interrompe o lote: os restantes saem como `"nao_tentado"`. Quem não é candidato sai `"nao_elegivel"` sem gerar link.
  - `url_base` + `/atualizar/<token>`; a base pública do app do motorista vem de onde `nucleo/cadastro_motorista_link._url_ficha`/`api_motorista._url_painel` tiram a deles (conferir e reusar; não inventar chave nova de config).

- [ ] **Step 1: testes que falham:** `test_sem_chave_forcar_destino_desligado`; `test_piloto_manda_para_o_numero_com_cabecalho_e_marca_piloto`; `test_real_manda_no_grupo`; `test_assinatura_impede_segunda_mensagem_do_mesmo_link`; `test_teto_proprio_15`; `test_candidatos_ordenados_por_rotas_e_sem_cpf_fica_fora`; `test_candidato_com_link_em_aberto_fica_fora_mas_link_piloto_nao_conta`; `test_lote_interrompido_no_teto` (Review Focus 5: 3 pessoas, `avisar` devolve enviado, teto, — → situações `enviado`, `teto`, `nao_tentado`; só a 1ª tem `link_em_aberto`); `test_lote_falha_cancela_o_link_gerado`; `test_dupla_no_mesmo_grupo_recebe_duas_mensagens`.
- [ ] **Step 2:** rodar → falham.
- [ ] **Step 3:** implementar.
- [ ] **Step 4:** `py -3.11 -m unittest test_notificar_whatsapp_atualizacao test_notificar_whatsapp test_notificar_whatsapp_cadastro_link nucleo.test_grupos_motoristas -v` → OK.

### Task 7: Tela /motoristas

**Files:**
- Modify: `painel_agentes/motoristas.py` (`dados_pagina_motoristas`)
- Modify: `painel_agentes/painel_agentes.py` (rotas novas, junto das de motoristas)
- Modify: `painel_agentes/templates/motoristas.html`
- Test: `painel_agentes/test_motoristas_padrao.py` (rodar de dentro de `painel_agentes/`; seguir a montagem de um teste de rota existente dessa pasta, com config falso)

**Interfaces:**
- Consumes: Tasks 1, 2, 4, 5, 6.
- Produces:
  - Cada linha de `dados_pagina_motoristas()["motoristas"]` ganha: `principal_id`, `pendencias` (dict da Task 1; vazio para inativo), `tem_pendencia: bool`, `estado_link`, `rotas_30d`, `grupo_nome`, `tem_login: bool`, `tem_ficha: bool`. Topo: `total_pendentes` (pessoas, não linhas). A carga da página chama `atualizacao_motorista.vincular_fichas(conn)` antes de calcular (idempotente; é a migração do vínculo das fichas antigas). Falha em qualquer consulta nova não derruba a tela (mesmo padrão `try/except` + `logger.warning` das funções vizinhas).
  - Rotas (`@exige_mesma_origem`):
    - `GET /api/motoristas/<int:id>/ficha` (`total`, `operador`, `logistica`) → ficha vigente + pendências; documentos pela rota existente `/api/motoristas/cadastros/<cadastro_id>/documento/<tipo>`.
    - `POST /api/motoristas/<int:id>/link-atualizacao` (mesmos níveis) → `{"url": ...}` de `gerar_link` (botão Copiar link). O link só aparece nessa resposta.
    - `GET /api/motoristas/grupos/sugestoes`, `POST /api/motoristas/grupos/confirmar` `{"itens": [{"principal_id", "grupo_id", "grupo_nome", "como"}]}`, `GET /api/motoristas/grupos/previa-renomear`, `POST /api/motoristas/grupos/renomear` `{"principal_ids": [...]}` — todas `niveis=("total",)`.
    - `GET /api/motoristas/envio-links/candidatos`, `POST /api/motoristas/envio-links` `{"principal_ids": [...]}` — `niveis=("total",)`.
  - Tela:
    - Coluna "Pendências" com selos: `badge badge-erro` por documento ("CNH", "CRLV", "Selfie", "ANTT", "Residência"), "Ficha incompleta" (com `title` listando os campos), "veículo diferente", "falta CPF para enviar no grupo", e selo cinza do link ("link enviado dd/mm", "abriu, não concluiu", "link vencido").
    - Coluna "Rotas 30 d". Contador "N motoristas com pendência" e caixa "só pendentes" (filtro no navegador; lembrar a escolha em `localStorage` dentro de `try/catch`).
    - Por linha (com `pode_editar`): Editar (já existe), Ver ficha, Copiar link, Inativar/Reativar (com `confirm`), Resetar PIN (vira "Criar acesso ao app" sem login; desabilitado com `title="falta o CPF"`), Excluir (só nível total; `confirm` que pede digitar o nome).
    - Resetar PIN mostra o PIN uma vez, em modal com botão copiar; texto: "PIN provisório. O app pede a troca no primeiro acesso."
    - Bloco "WhatsApp" (só nível total): "Identificar grupos" (lista com seletor por pessoa, sugestão por telefone pré-marcada, por nome só sugerida; botão Salvar), "Renomear grupos confirmados" (prévia "atual → novo" e situação; desabilitado com aviso se a chave estiver desligada), "Enviar links de atualização" (candidatos com caixa de seleção, os 10 primeiros marcados, aviso do modo: desligado / piloto para o número de teste / envio real).
    - Sem caminho absoluto no template: `url_for()`.

- [ ] **Step 1: testes que falham:** `test_pagina_traz_pendencias_e_total_por_pessoa`; `test_nivel_leitura_nao_ve_botoes` (HTML sem "Resetar PIN"/"Excluir"); `test_operador_nao_exclui_403`; `test_resetar_pin_devolve_pin_uma_vez`; `test_link_atualizacao_devolve_url`; `test_envio_links_so_total`; `test_falha_no_calculo_de_pendencias_nao_derruba_a_tela` (mock levantando exceção → 200).
- [ ] **Step 2:** de dentro de `painel_agentes/`: `py -3.11 -m unittest test_motoristas_padrao -v` → falham.
- [ ] **Step 3:** implementar backend da página, rotas e template.
- [ ] **Step 4:** mesmo comando → OK; rodar também os demais testes da pasta que tocam motoristas.

### Task 8: Prova no navegador, mapa e fechamento

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (seções do nucleo, painel, tabelas, config e avisos de envio)
- Modify: `painel_agentes/acesso_telas.py` só se alguma rota nova for tela (as desta entrega são APIs sob `/api/motoristas`; conferir como o arquivo trata APIs)

- [ ] **Step 1:** rodar todos os módulos de teste citados nas tarefas 1 a 7 (em comandos separados para `painel_agentes`) → tudo OK. Anotar os que já falhavam antes em `origin/master`, se houver.
- [ ] **Step 2:** subir o painel em `127.0.0.1:8099` e a API do motorista em outra porta livre, com banco temporário, config falso e gateway falso (ver memória "Testar painel com Playwright local": `/login?proximo=`, bloquear `/api/torre/**`, cookie Secure desligado). Roteiro: abrir /motoristas → ver selos e filtro → Copiar link → abrir o link → CPF → completar → enviar 3 documentos → concluir → voltar e ver os selos de ANTT e Residência → Resetar PIN → Inativar → Excluir um sem rota → Identificar grupos → prévia de renomear → envio em modo piloto. Guardar capturas no scratchpad.
- [ ] **Step 3:** atualizar o `MAPA_DO_SISTEMA.txt`.
- [ ] **Step 4:** `py -3.11 -m py_compile` em todos os `.py` tocados; `git status --short` só com arquivos desta tarefa.
