# Alterar entrega pelo portal — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** o cliente altera endereço de entrega, data de entrega e horário de recebimento de qualquer pedido dele pelo portal, e a alteração vai sozinha pra Vuupt.

**Architecture:** registro e consultas das alterações num módulo sem dependência do portal (`regras/alteracoes_entrega.py`), lido também pela importação horária (`pipeline.py`) e pela escolha da fonte do pool (`nucleo/pool.py`). Regra e orquestração em `portal_cliente/alterar_entrega.py`, com tudo que fala com fora injetado em `Dependencias` (mesmo padrão de `solicitacoes_execucao.py`), reaproveitando `_tirar_da_rota_real`, `reagendar_servico_vuupt` e `planejamento_rotas._gravar_endereco_pedido`. Duas rotas novas no portal e um modal na tela de acompanhamento.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db`), unittest, JS puro no template.

**Spec:** `docs/superpowers/specs/2026-10-29-alterar-pedido-portal-design.md`

## Global Constraints

- Nada na Stokki. Só Vuupt + tabelas locais.
- Chave `portal_cliente.alteracao_entrega.ativo` (padrão `false`) + `portal_cliente.alteracao_entrega.piloto_cnpjs` (lista; padrão vazia). Desligado = o botão não aparece e as rotas devolvem 404.
- "Em rota" = o serviço tem `route_id` na Vuupt (rota enviada). Parada em rascunho local não é "em rota".
- Aviso de atraso só quando muda **endereço ou data** de pedido em rota. Texto exato: "As rotas do dia seguinte são fechadas às 16h e esse pedido já está numa rota. Para alterar, ele sai da rota e volta para a programação, e a entrega pode atrasar 1 dia."
- Mudança só de horário nunca tira de rota.
- Bloqueia (vai pra operação): serviço `canceled`, `on_route`, `arrived`, `done`; núcleo com `ENTREGUE`/`INSUCESSO`; rota iniciada; rota Lalamove; título `[RETIRADA]`. Mensagem: motivo + "Fale com a Fresh Log pelo chat do portal."
- Endereço fora da área (`envio_pedidos._identificar_area` devolve tipo) → recusa: "Esse endereço fica fora da área atendida. Fale com a Fresh Log pelo chat do portal."
- Data: maior que hoje (São Paulo). Horário: `HH:MM`, fim > início. Janela da data = horário de recebimento efetivo do destinatário (`horario_efetivo`), ou 08:00–18:00 quando for o padrão `00:00`–`23:59`.
- Horário grava em `portal_destinatarios` + ajuste manual global + contato da Vuupt; **nunca** em `scheduled_*`.
- Equipe recebe e-mail (destino `email.email_atendimento` ou `email_responsavel`) só quando o pedido saiu de uma rota.
- Código de teste ≤ 7 dígitos (`normalizar_codigo` e o resto do sistema cortam acima disso).
- Português nos textos, commits `Portal: ...`, `git add` só dos arquivos da tarefa, sem push (o Hugo pede).
- Rota nova do portal não precisa de `acesso_telas.TELAS` (isso é do painel).

## Diferenças em relação ao spec (decididas no plano, o Hugo confere)

- Só `[RETIRADA]` é bloqueado. Pedido de terceiros/redespacho não tem marca no serviço da Vuupt; o spec pedia bloqueio também.
- O registro das alterações fica na tabela `alteracoes_entrega_portal`. Mostrar esse histórico no portal e na `/solicitacoes-portal` fica para depois.

## Review Focus

1. **Duas abas / clique duplo:** o POST reavalia o serviço ao vivo na Vuupt; se ele já saiu da rota, não tenta tirar de novo e não mostra o aviso de atraso. Teste na Task 3 (`test_aplicar_servico_ja_fora_da_rota_nao_tira_de_novo`).
2. **Login de grupo:** serviço de outra empresa do mesmo grupo é aceito; serviço de fora do grupo dá 404 (igual ao `/api/canhoto`). Teste na Task 4.
3. **Reentrega de pedido com endereço trocado:** `#PS-1234-R1` acha o endereço alterado do `PS-1234`. Teste na Task 1.
4. **Só endereço, data veio do dia fixo, região nova visita outro dia:** a data passa pra `proxima_data_valida` da região nova, origem `DIA_FIXO`, sem aviso de dedicado. Teste na Task 2.
5. **Falha no meio:** saiu da rota mas o PUT do endereço falhou → registro `FALHOU` com o que já foi feito, mensagem diz que o pedido está fora da rota, aguardando a programação. Teste na Task 3.

---

### Task 0: Ramo e base

**Files:** nenhum arquivo do projeto muda.

- [ ] **Step 1:** `git worktree add .claude/worktrees/alterar-entrega-portal -b alterar-entrega-portal master` (a master local tem os 9 commits do endereço de entrega, ainda sem push).
- [ ] **Step 2:** dentro da worktree, `git fetch origin && git merge --no-edit origin/master`. Conflito esperado só em `portal_cliente/envio_pedidos.py`, no `_garantir_colunas(conn, "portal_envios", ...)`: manter as três colunas do endereço (`endereco_fiscal`, `endereco_entrega_origem`, `endereco_entrega_json`) **e** `**COLUNAS_RETOMAR,`. `git add portal_cliente/envio_pedidos.py && git commit --no-edit`.
- [ ] **Step 3:** copiar o `config.yaml` da raiz pra worktree (não commitar). Rodar `py -3.11 -m unittest discover -s portal_cliente -p "test_*.py"` e `py -3.11 -m unittest discover -s nucleo -p "test_*.py"` (em comandos separados). Esperado: tudo verde; anote falhas pré-existentes antes de seguir.

---

### Task 1: Registro das alterações

**Files:**
- Create: `regras/alteracoes_entrega.py`
- Test: `regras/test_alteracoes_entrega.py`
- Modify: `MAPA_DO_SISTEMA.txt` (tabela nova na lista de tabelas; módulo na seção `regras/`)

**Interfaces:**
- Produces:
  - `DB_PATH` (mesmo `dados/dados.db` dos outros módulos de `regras/`)
  - `conectar(db_path=DB_PATH) -> sqlite3.Connection`: cria a tabela `alteracoes_entrega_portal` se não existir; `row_factory = sqlite3.Row`.
  - `codigo_base(codigo: str) -> str | None`: `'#PS-1234-R1'` → `'PS-1234'`; usa `nucleo.normalizacao.normalizar_codigo` e corta o sufixo `-R\d+`/`-C\d+`.
  - `registrar(conn, *, cnpj_embarcador: str, sender_id: int, service_id: int, codigo: str, por: str, antes: dict, depois: dict, endereco: dict | None, latitude: float | None, longitude: float | None, rota_id_tirada: int | None, status: str, erro: str = "") -> int`. `status` ∈ `APLICADA`, `FALHOU`. Grava `codigo_base` junto.
  - `endereco_vigente(conn, codigo: str) -> dict | None`: última linha `APLICADA` com `endereco` não nulo do `codigo_base(codigo)` → `{"endereco": dict, "latitude": float | None, "longitude": float | None}`.
  - `houve_alteracao_desde(conn, minutos: int, agora: datetime | None = None) -> bool`: alguma `APLICADA` com `criado_em >= agora - minutos`.

Colunas: `id INTEGER PK`, `criado_em TEXT` (local, `%Y-%m-%d %H:%M:%S`), `cnpj_embarcador`, `sender_id INTEGER`, `service_id INTEGER`, `codigo`, `codigo_base`, `por`, `antes_json`, `depois_json`, `endereco_json`, `latitude REAL`, `longitude REAL`, `rota_id_tirada INTEGER`, `status`, `erro`. Índice em `codigo_base`.

- [ ] **Step 1: testes que falham**, em banco temporário:
  - `test_codigo_base_tira_hash_e_reentrega`: `codigo_base("#PS-1234-R1") == "PS-1234"`, `codigo_base("ps-1234") == "PS-1234"`, `codigo_base("") is None`.
  - `test_endereco_vigente_pega_a_ultima_aplicada`: duas `APLICADA` com endereços A e B, depois uma `FALHOU` com C → devolve B.
  - `test_endereco_vigente_vale_pra_reentrega`: registra em `#PS-1234` e consulta `#PS-1234-R1` → acha (Review Focus 3).
  - `test_endereco_vigente_ignora_alteracao_so_de_data`: linha `APLICADA` com `endereco=None` → `None`.
  - `test_houve_alteracao_desde`: linha às 15:10; `agora=15:50` com 60 min → `True`; `agora=16:20` com 60 min → `False`.
- [ ] **Step 2:** `py -3.11 -m unittest regras/test_alteracoes_entrega.py -v` → FAIL (módulo não existe).
- [ ] **Step 3:** implementar o módulo.
- [ ] **Step 4:** mesmo comando → PASS.
- [ ] **Step 5:** atualizar o MAPA e commit `Regras: registro das alteracoes de entrega feitas pelo cliente no portal`.

---

### Task 2: Avaliação e plano da alteração (regra pura)

**Files:**
- Create: `portal_cliente/alterar_entrega.py`
- Test: `portal_cliente/test_alterar_entrega.py`

**Interfaces:**
- Consumes: `endereco_entrega.montar_endereco(campos, consultar)` / `ErroEndereco`; `regioes_dia_fixo.regra_dia_fixo_do_servico`, `data_valida_na_regiao`, `proxima_data_valida`, `descricao_dias`; `registro_dia_fixo.data_nao_e_do_cliente(conn, servico, data)`; `fora_dia_fixo.data_agendada(servico)`; `retiradas.regras_retirada.eh_servico_retirada`.
- Produces:
  - `class ErroAlteracao(ValueError)`: mensagem pronta pro cliente.
  - `ativa_para(config: dict | None, cnpj: str) -> bool`
  - `@dataclass Avaliacao: pode: bool; motivo: str; em_rota: bool; rota_id: int | None`
  - `avaliar(servico: dict, rotas_nucleo: dict[int, dict], status_nucleo: str | None, agent_lalamove: int) -> Avaliacao`. `servico` é o GET da Vuupt; `rotas_nucleo`/`status_nucleo` vêm de `cancelamento.servicos_do_pedido`. Regras de bloqueio: as de Global Constraints, nessa ordem: cancelado, retirada, status Vuupt, núcleo, rota iniciada, Lalamove.
  - `validar(corpo: dict, hoje: date, consultar_cep=None) -> dict` → `{"endereco": dict | None, "data": date | None, "horario": tuple[str, str] | None}`. Corpo vazio (nada a mudar) → `ErroAlteracao("Nada foi alterado.")`.
  - `endereco_vuupt(end: dict) -> str`: `"{logradouro}, {numero}, {complemento}, {bairro}, {municipio} - {uf}, {cep}, Brasil"`, pulando partes vazias (mesmo formato que `pipeline.montar_payload_vuupt` monta e que `regioes_dia_fixo.extrair_cidade` lê).
  - `@dataclass Plano: tirar_da_rota: bool; avisos: list[dict]; data_final: date | None; origem_data: str | None; endereco_str: str | None`. Cada aviso: `{"codigo": "atraso" | "dedicado", "texto": str}`. `origem_data` ∈ `"CLIENTE"`, `"DIA_FIXO"`, `None`.
  - `planejar(servico: dict, validado: dict, avaliacao: Avaliacao, conn_registro, hoje: date) -> Plano`.

Regras do `planejar`:
- `tirar_da_rota = bool(validado["data"]) or (avaliacao.em_rota and bool(validado["endereco"]))`. Data nova sempre passa pelo `_tirar_da_rota_real`, porque ele também tira a parada de um rascunho local; sem rota e sem rascunho ele não faz nada. Aviso `atraso` só quando `avaliacao.em_rota` e mudou endereço ou data, com o texto exato de Global Constraints.
- Endereço considerado = o novo, senão o atual do serviço. Data considerada = a nova (origem `CLIENTE`), senão a atual (`data_agendada`).
- Data nova fora do dia fixo da região considerada → aviso `dedicado`: `"Essa data não é dia de entrega na região ({descricao_dias(regra)}). A entrega vira dedicada e é cobrada à parte."`.
- Só endereço, data atual existe, `data_nao_e_do_cliente` verdadeiro e a data não vale na região nova → `data_final = proxima_data_valida(regra, hoje + 1 dia)`, `origem_data = "DIA_FIXO"`, sem aviso.
- Só endereço, data atual do cliente e inválida na região nova → aviso `dedicado`, `data_final = None`.

- [ ] **Step 1: testes que falham** (servicos sintéticos; endereços de cidades com regra conhecida em `regioes_dia_fixo`; `consultar_cep` falso):
  - `test_avaliar_bloqueia_cada_caso`: `canceled`, `[RETIRADA] #PS-1`, `on_route`, `done`, núcleo `INSUCESSO`, rota com `iniciada_em`, rota com `agent_id == agent_lalamove` → `pode False` e motivo terminando em "Fale com a Fresh Log pelo chat do portal."
  - `test_avaliar_pool_e_rota_nao_iniciada`: sem `route_id` → `pode True, em_rota False`; `route_id=55`, rota não iniciada → `pode True, em_rota True, rota_id 55`.
  - `test_validar_recusa`: data de hoje; `"25:00"`; fim ≤ início; CEP inexistente (`ErroEndereco` vira `ErroAlteracao` com a mesma mensagem); corpo vazio.
  - `test_planejar_atraso_so_com_endereco_ou_data`: em rota + só horário → `tirar_da_rota False`, sem avisos; em rota + data → `tirar_da_rota True` e aviso `atraso` com o texto exato; fora de rota + data → `tirar_da_rota True` e sem aviso; fora de rota + só endereço → `tirar_da_rota False`.
  - `test_planejar_data_fora_do_dia_fixo_avisa_dedicado`.
  - `test_planejar_so_endereco_data_do_dia_fixo_recalcula` (Review Focus 4): `data_final == proxima_data_valida(...)`, `origem_data == "DIA_FIXO"`, sem aviso.
  - `test_ativa_para`: `ativo False` + CNPJ no piloto → True; fora do piloto → False; `ativo True` → True.
- [ ] **Step 2:** `py -3.11 -m unittest portal_cliente/test_alterar_entrega.py -v` → FAIL.
- [ ] **Step 3:** implementar.
- [ ] **Step 4:** mesmo comando → PASS.
- [ ] **Step 5:** commit `Portal: regra de quem pode alterar a entrega e que avisos mostrar`.

---

### Task 3: Aplicar a alteração

**Files:**
- Modify: `portal_cliente/alterar_entrega.py`
- Modify: `portal_cliente/envio_pedidos.py` (`registrar_agendamento_pedido`: sem `envio["id"]`, pula o `UPDATE portal_envios`)
- Test: `portal_cliente/test_alterar_entrega.py`

**Interfaces:**
- Consumes: Task 1 (`registrar`), Task 2 (`Plano`, `validar`, `endereco_vuupt`); `envio_pedidos.registrar_agendamento_pedido`, `envio_pedidos.gravar_destinatario`; `registro_dia_fixo.registrar_origem(conn, servico, data, ORIGEM_DIA_FIXO, ...)`; `complexidade_entrega.horario_efetivo`.
- Produces:
  - `@dataclass Dependencias`: `tirar_da_rota: Callable[[int], dict]`, `gravar_endereco: Callable[[int, str, dict], dict]` (service_id, texto, `{"address", "latitude"?, "longitude"?}`), `reagendar: Callable[[int, str, str, str], dict]` (service_id, `YYYY-MM-DD`, ini, fim), `atualizar_horario_contato: Callable[[int, str, str], dict]` (customer_id, ini, fim), `geocodificar: Callable[[str], tuple[float, float] | None]`, `avisar_equipe: Callable[[dict], None]`. Toda chamada devolve `{"ok": bool, "erro": str, ...}`.
  - `aplicar(conn, servico: dict, validado: dict, plano: Plano, contexto: dict, deps: Dependencias) -> dict` → `{"ok": bool, "mensagem": str, "feito": list[str]}`. `contexto`: `cnpj_embarcador`, `sender_id`, `por`, `destinatario_doc`, `destinatario_nome`.
  - `dependencias_reais(config: dict, usuario: str) -> Dependencias`: `tirar_da_rota` = `solicitacoes_execucao._tirar_da_rota_real(token, sid, agent_lalamove, usuario)`; `reagendar` = `solicitacoes_execucao.reagendar_servico_vuupt`; `gravar_endereco` = `planejamento_rotas._gravar_endereco_pedido(vuupt, sid, texto, dados, rascunho_id)` com `rascunho_id` de `roteirizacao.cancelar_servico.rascunho_do_servico` (importar pelo `solicitacoes_execucao._caminhos_painel()`); `atualizar_horario_contato` = `VuuptClient.atualizar_customer(cid, {"operating_hour_start", "operating_hour_end"})`; `geocodificar` = `geocodificacao.geocodificar(texto, google_maps.api_key)`; `avisar_equipe` = e-mail no padrão de `app._avisar_operacao_solicitacao`, assunto `[Portal] Pedido saiu da rota · {codigo} · {empresa}`.

Ordem do `aplicar` (para no primeiro erro, registra `FALHOU` com `erro` e o que já foi feito):
1. `plano.tirar_da_rota` → `deps.tirar_da_rota`.
2. endereço → `deps.geocodificar(endereco_str)` e `deps.gravar_endereco`.
3. data (`validado["data"]` ou `plano.data_final`) → janela pelo `horario_efetivo` do destinatário (08:00–18:00 quando vier `00:00`–`23:59`) → `deps.reagendar`. Origem `CLIENTE`: `registrar_agendamento_pedido(conn, {"codigo_pedido": codigo, "agendamento_data", "agendamento_hora_inicio", "agendamento_hora_fim", "destinatario_doc", "destinatario_nome", "cnpj_embarcador", "numero_nf": None}, contexto["por"])` e `UPDATE agendamentos_pedido SET aplicado_vuupt_em` (como `_reagendar`). Origem `DIA_FIXO`: `registrar_origem(..., ORIGEM_DIA_FIXO)`.
4. horário → `gravar_destinatario` mantendo o `requer_agendamento` que já está em `portal_destinatarios` (sem linha = `False`), depois `deps.atualizar_horario_contato(servico["customer_id"], ini, fim)` (falha aqui vira aviso no log; não derruba).
5. `registrar(... status="APLICADA")`; se tirou da rota, `deps.avisar_equipe`.

Mensagens ao cliente: sucesso `"Alteração feita. {resumo}"` (+ `" O pedido saiu da rota e volta na próxima programação."` quando tirou); falha depois de tirar da rota: `"Não conseguimos concluir a alteração ({erro}). O pedido já saiu da rota e aguarda a programação; a Fresh Log foi avisada."` (e chama `avisar_equipe`).

- [ ] **Step 1: testes que falham** (Dependencias falsas que gravam as chamadas; banco temporário com `agendamentos_pedido`, `portal_destinatarios`, `agendamentos_origem`, `ajustes_complexidade_cliente`):
  - `test_aplicar_ordem_completa`: em rota + endereço + data → chamadas na ordem tirar → geocodificar → gravar_endereco → reagendar; registro `APLICADA` com `rota_id_tirada`; `avisar_equipe` chamado uma vez.
  - `test_aplicar_servico_ja_fora_da_rota_nao_tira_de_novo` (Review Focus 1): `Plano(tirar_da_rota=False)` → `tirar_da_rota` não é chamado; `avisar_equipe` não é chamado.
  - `test_aplicar_falha_depois_de_tirar` (Review Focus 5): `gravar_endereco` devolve `{"ok": False, "erro": "x"}` → `ok False`, registro `FALHOU`, mensagem contém "já saiu da rota", `reagendar` não chamado, `avisar_equipe` chamado.
  - `test_aplicar_data_usa_horario_do_destinatario`: ajuste manual 13:00–17:00 → `reagendar(sid, "2026-11-03", "13:00", "17:00")`; sem ajuste → `"08:00", "18:00"`; linha em `agendamentos_pedido` com `aplicado_vuupt_em` preenchido.
  - `test_aplicar_horario_nao_mexe_em_scheduled`: só horário → `reagendar` não chamado; `portal_destinatarios` e `ajustes_complexidade_cliente` com 14:00–18:00; `requer_agendamento` anterior (1) preservado.
  - `test_registrar_agendamento_sem_envio_id`: `registrar_agendamento_pedido` com dict sem `id` grava em `agendamentos_pedido` e não quebra.
- [ ] **Step 2:** `py -3.11 -m unittest portal_cliente/test_alterar_entrega.py -v` → FAIL nos novos.
- [ ] **Step 3:** implementar `aplicar`, `Dependencias`, `dependencias_reais` e o ajuste em `registrar_agendamento_pedido`.
- [ ] **Step 4:** mesmo comando + `py -3.11 -m unittest portal_cliente/test_solicitacoes_execucao.py portal_cliente/test_retomada.py -v` → PASS.
- [ ] **Step 5:** commit `Portal: aplicar alteracao de entrega na Vuupt (sai da rota, endereco, data, horario do destinatario)`.

---

### Task 4: Rotas do portal

**Files:**
- Modify: `portal_cliente/app.py`
- Test: `portal_cliente/test_alterar_entrega_api.py`
- Modify: `MAPA_DO_SISTEMA.txt` (seção do portal: "Alteração de entrega pelo cliente")

**Interfaces:**
- Consumes: Tasks 2 e 3; `dados.buscar_servico(token, sid)`; `cancelamento.servicos_do_pedido(conn, codigo_base)`; `dados.invalidar_cache(sender_id)`; `envio_pedidos._identificar_area`.
- Produces:
  - `GET /api/pedidos/<int:service_id>/alteracao` (`@requer_cliente`) → `{"pode", "motivo", "em_rota", "destinatario", "atual": {"endereco", "data", "horario": {"inicio", "fim"}}}`.
  - `POST /api/pedidos/<int:service_id>/alteracao` (`@requer_cliente`, `@exige_mesma_origem`, `_exige_pode_enviar()`), corpo `{"endereco"?: {cep, numero, complemento, logradouro, bairro}, "data"?: "YYYY-MM-DD", "horario"?: {"inicio", "fim"}, "confirmar": bool}`. Respostas: 200 `{"ok": True, "mensagem"}`; 200 `{"ok": False, "precisa_confirmar": True, "avisos": [...]}` quando há aviso e `confirmar` falso; 400 `{"erro"}` em `ErroAlteracao`; 409 `{"erro"}` quando `avaliar` bloqueia; 502 `{"erro"}` em falha do `aplicar`.
  - `/api/dia` passa a devolver `"alteracao_entrega": ativa_para(_CONFIG, _empresa_envio()["cnpj"])`.

Regras: chave desligada → 404 nas duas rotas. Serviço fora de `g.cliente["sender_ids"]` → 404. O POST sempre busca o serviço de novo na Vuupt (nunca confia no GET). Endereço novo → `_identificar_area` com `endereco_vuupt(...)`; tipo devolvido → 400 com a mensagem de fora de área de Global Constraints. Depois de aplicar, `dados.invalidar_cache(servico["sender_id"])`. Log `alteracao cnpj=... sid=... por=... -> ok/erro`.

- [ ] **Step 1: testes que falham** (cliente de teste do Flask com sessão de cliente falsa, `dados.buscar_servico`, `dependencias_reais` e `_identificar_area` trocados por falsos):
  - `test_chave_desligada_404`.
  - `test_servico_de_outro_cliente_404` e `test_servico_de_outra_empresa_do_grupo_ok` (Review Focus 2).
  - `test_get_bloqueado_mostra_motivo` (serviço `on_route`).
  - `test_post_em_rota_pede_confirmacao_e_depois_aplica`: 1º POST sem `confirmar` → `precisa_confirmar` com aviso `atraso`; 2º com `confirmar: true` → 200 e `aplicar` chamado.
  - `test_post_endereco_fora_da_area_400`.
  - `test_post_equipe_so_leitura_403`.
  - `test_api_dia_informa_chave`.
- [ ] **Step 2:** `py -3.11 -m unittest portal_cliente/test_alterar_entrega_api.py -v` → FAIL.
- [ ] **Step 3:** implementar.
- [ ] **Step 4:** mesmo comando + `py -3.11 -m unittest discover -s portal_cliente -p "test_*.py"` → PASS (salvo as falhas pré-existentes anotadas na Task 0).
- [ ] **Step 5:** MAPA + commit `Portal: rotas para o cliente alterar endereco, data e horario de um pedido`.

---

### Task 5: Botão e janela na tela de acompanhamento

**Files:**
- Modify: `portal_cliente/templates/acompanhamento.html`

**Interfaces:**
- Consumes: rotas da Task 4; `GET /api/envios/cep/<cep>` (preenche rua/bairro/cidade).

Comportamento:
- Com `dados.alteracao_entrega` verdadeiro, a coluna de ação das linhas `programado`, `aguardando_saida` e `agendado` ganha o link **"Alterar entrega"** (mesmo estilo do link "Canhoto").
- Clique → GET; se `pode` falso, a janela só mostra o motivo. Se verdadeiro, três blocos opcionais: endereço (CEP, número, complemento; rua e bairro preenchidos pelo CEP e editáveis), data (`<input type=date>`, mínimo amanhã), horário de recebimento (início/fim, com a frase "Vale para todos os próximos pedidos desse destinatário, de qualquer embarcador."). Cada bloco começa recolhido mostrando o valor atual, com um botão "Alterar".
- "Salvar" → POST; com `precisa_confirmar`, mostra os avisos e troca o botão por "Alterar mesmo assim" (reenvia com `confirmar: true`). Sucesso → mensagem e recarrega a lista (`?atualizar=1`). Erro → mensagem na própria janela.
- Usa `BASE` / `url_for` como o resto do template (nada de caminho absoluto) e o cabeçalho `X-Portal-Empresa` como as chamadas de envio.

- [ ] **Step 1:** implementar.
- [ ] **Step 2:** testar no navegador com o portal local numa porta livre (config falso, banco temporário, serviço falso da Vuupt), seguindo `feedback_testar_painel_playwright_local`: pedido no pool (sem aviso), pedido em rota (aviso, confirmar), pedido `on_route` (motivo), celular (largura 390 px, sem rolagem horizontal). Tirar print de cada caso.
- [ ] **Step 3:** commit `Portal: botao Alterar entrega na tela de acompanhamento`.

---

### Task 6: Importação horária não desfaz

**Files:**
- Modify: `pipeline.py` (depois do `resolver_endereco_entrega` e antes do `montar_payload_vuupt`; e no bloco 3b de geocodificação)
- Test: `test_pipeline_alteracao_portal.py` (raiz)

**Interfaces:**
- Consumes: `regras.alteracoes_entrega.conectar`, `endereco_vigente`; `regras.complexidade_entrega.carregar_ajustes_manuais`.
- Produces: `aplicar_alteracoes_do_cliente(codigo_ps: str, documento_dest: str, endereco_resolvido: dict, horario_entrega: dict | None, conn=None, ajustes=None) -> tuple[dict, dict | None, tuple[float, float] | None]` em `pipeline.py`: devolve (endereço, horário, coordenadas fixas ou `None`).

Regras: endereço vigente do cliente substitui `logradouro` (= `"{logradouro}, {numero}"`), `complemento`, `bairro`, `cidade` (= `municipio`), `uf`, `cep`; com coordenadas gravadas, o bloco 3b usa elas e não geocodifica de novo. Ajuste manual do documento do destinatário substitui `horario_entrega` (`{"inicio", "fim"}`) — isso passa a valer também pros ajustes feitos pelo Planejamento (registrar no MAPA, seção de armadilhas). Falha ao ler as tabelas → log `aviso` e segue com o da NF (nunca derruba a importação).

- [ ] **Step 1: testes que falham:**
  - `test_endereco_do_cliente_vence_o_da_nf`.
  - `test_sem_alteracao_mantem_nf`.
  - `test_ajuste_manual_vence_horario_extraido`.
  - `test_tabela_ausente_nao_derruba`.
- [ ] **Step 2:** `py -3.11 -m unittest test_pipeline_alteracao_portal.py -v` → FAIL.
- [ ] **Step 3:** implementar e ligar nos dois pontos do `processar_pedido`.
- [ ] **Step 4:** mesmo comando + os testes do pipeline que já existem (`py -3.11 -m unittest discover -s . -p "test_pipeline*.py"`) → PASS.
- [ ] **Step 5:** `py -3.11 pipeline.py --modo-teste` local sem erro novo no log. Commit `Pipeline: endereco e horario alterados pelo cliente no portal vencem os da NF`.

---

### Task 7: Pool do freshhub não perde a alteração

**Files:**
- Modify: `nucleo/pool.py` (`listar_pool_not_assigned`)
- Test: `nucleo/test_pool_freshhub.py`

**Interfaces:**
- Consumes: `regras.alteracoes_entrega.conectar`, `houve_alteracao_desde`.
- Produces: constante `JANELA_ALTERACAO_PORTAL_MIN = 60` em `nucleo/pool.py`.

Regra: com `fonte_pool = freshhub`, se `houve_alteracao_desde(conn, 60)`, a rodada lê pela Vuupt (mesmo caminho da queda), loga `pool.freshhub_alteracao_portal` (nível info) e **não** manda o e-mail de indisponível. O freshhub copia o legado a :10/:40 e carrega a :25/:55 à tarde, então uma alteração pode levar até ~45 min pra aparecer lá. Erro ao ler a tabela → segue pelo freshhub.

- [ ] **Step 1: testes que falham:** `test_freshhub_com_alteracao_recente_le_vuupt`, `test_freshhub_sem_alteracao_le_freshhub`, `test_fonte_nucleo_ignora_alteracoes`.
- [ ] **Step 2:** `py -3.11 -m unittest nucleo/test_pool_freshhub.py -v` → FAIL.
- [ ] **Step 3:** implementar (sem importar `vuupt_client`: `nucleo/test_acoplamento_vuupt` precisa continuar verde).
- [ ] **Step 4:** `py -3.11 -m unittest discover -s nucleo -p "test_*.py"` → PASS.
- [ ] **Step 5:** commit `Pool: rodada logo depois de alteracao do cliente le a Vuupt em vez do freshhub`.

---

### Task 8: Revisão e prova (deploy só quando o Hugo pedir)

- [ ] **Step 1:** `py -3.11 -m py_compile` em todos os arquivos mexidos; suítes do portal, do núcleo e da raiz verdes.
- [ ] **Step 2:** revisão de segurança do diff (rotas novas no portal mexem em pedido de cliente): `docs/revisoes/2026-10-29-alterar-entrega-portal.md`.
- [ ] **Step 3:** com o deploy autorizado (`portal-cliente` reinicia; pipeline e pool pegam sozinhos), ligar só `piloto_cnpjs: ["00000000000191"]` no config da VPS (o Hugo edita) e provar com pedido fictício do cliente teste (código ≤ 7 dígitos): pool sem aviso; em rota com aviso (sai da rota, aparece no pool da rodada seguinte com o endereço novo); só horário; importação horária por cima não desfaz. Cancelar e limpar tudo depois.
- [ ] **Step 4:** atualizar a memória do projeto e avisar o Hugo do que ficou pra ele ligar.
