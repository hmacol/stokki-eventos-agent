# Checklist de carregamento — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Etapa obrigatória no app dos motoristas, entre "Aceitar" e "Iniciar rota": nome do carregador, foto do baú inteiro (avaliada pelo Haiku, com % de ocupação) e lista de pedidos em que desmarcar tira a parada da rota e devolve o pedido ao pool com selo.

**Architecture:** Novo módulo `nucleo/carregamento.py` (config, tabelas, regra de confirmar) chamado por um endpoint novo em `nucleo/api_motorista.py`; foto reaproveita `nucleo/validacao_fotos.py` com um tipo novo; remoção da Vuupt reaproveita `roteirizacao/edicao_rota.tirar_pedido`; o app ganha um componente de tela no lugar do deslizar "Iniciar rota"; o pool do painel lê os desmarcados recentes e mostra o selo. Tudo atrás da chave `api_motorista.checklist_carregamento.ativo`, desligada por padrão.

**Tech Stack:** Python 3.11 (Flask, SQLite, unittest), Expo/React Native + TypeScript (expo-router), Haiku 5.5 via `validacao_fotos`.

**Spec:** `docs/superpowers/specs/2026-11-21-checklist-carregamento-design.md`

## Global Constraints

- Python sempre `py -3.11`; testes: `py -3.11 -m unittest nucleo.test_carregamento -v` (nunca misturar `nucleo`/`roteirizacao` com `painel_agentes` no mesmo comando; testes do painel rodam de dentro de `painel_agentes/`).
- Chave `api_motorista.checklist_carregamento.ativo` **desligada por padrão**; desligada, app e API ficam exatamente como hoje. Opcional `pilotos: [agent_id]` (lista vazia = todos, quando ativo). Ligar é decisão do Hugo; **não deployar sem pedido**.
- Desmarcar **nunca cancela pedido**: só tira da rota. Vale pela **parada inteira** (um `service_id` da Vuupt).
- Trava até sair: o carregamento só é gravado e o "Iniciar rota" só libera quando **todos** os desmarcados saíram; falha parcial é idempotente (quem já saiu fica fora, a nova tentativa repete só os que faltam). Sem fila de remoção.
- Foto: reprovada trava até `max_tentativas`, depois libera com `revisao_humana`; `NAO_VERIFICADO` (Haiku fora, chave ausente, validação desligada) **nunca** trava. Sem a exceção `exige_outra_foto` da fachada.
- Motivos de desmarcar: `"Não coube"`, `"Não Encontrado"`, `"Outros"` (este exige observação). Todos já estão em `expedicao.MOTIVOS_EXCLUSAO`.
- Ocupação do baú em % arredondada de 10 em 10 (0–100).
- Rota `provedor = APP` (réplica de teste): tira só a parada local e **não** mexe no pedido real (`operacao.eh_rota_replica`).
- Português em código, comentários e commits (`Área: o que mudou e por quê`); comentários/logs sem acento nos arquivos que já seguem esse padrão. Nunca `git add -A`; só os arquivos da tarefa. Trabalhar em worktree/ramo `checklist-carregamento` (a árvore principal tem edições de outras sessões).
- Atualizar `MAPA_DO_SISTEMA.txt` no commit que cria arquivo/tabela/tela (Task 6).

## Review Focus

- Duplo toque em "Confirmar" / reenvio do mesmo `uuid` → um só registro, sem remover duas vezes (Task 3, 4).
- Desmarcar **todos** os pedidos → rota cancelada (Vuupt e núcleo), nada de "Iniciar rota" (Task 3).
- Parada já iniciada/entregue não pode ser desmarcada (a Vuupt recusa via `STATUS_PODE_SAIR`): erro claro, nada gravado (Task 3).
- Rota já `EM_ROTA` (iniciada por outro caminho) ou de outro motorista → recusa 409/404 (Task 4).
- Haiku fora do ar / `NAO_VERIFICADO` / foto sem chave → nunca trava (Task 1).
- Falha parcial na Vuupt: 2 de 3 saem, 1 falha → resposta diz qual falhou, nova tentativa repete só ela (Task 3).
- Veículo sem placa/tipo cadastrado → grava `NULL`, não quebra (Task 3).

---

### Task 1: Tipo de foto CARREGAMENTO na validação com Haiku

**Files:**
- Modify: `nucleo/validacao_fotos.py` (constantes ~linha 82; `validar_foto` ~linha 471; novo schema/prompt perto de `_SCHEMA_PEDAGIO`; `aplicar_tentativas` ~linha 615)
- Test: `nucleo/test_validacao_fotos.py` (nova classe `TestCarregamento`)

**Interfaces:**
- Produces: `TIPO_CARREGAMENTO = "CARREGAMENTO"`; `validar_foto(cfg, "CARREGAMENTO", conteudo)` devolve o mesmo dict de sempre (`resultado`, `motivo`, `problemas`, `leitura`, ...) com `leitura["bau_inteiro"]: bool`, `leitura["eh_carga"]: bool`, `leitura["ocupacao_pct"]: int | None` e, no topo do dict, `ocupacao_pct` já arredondado de 10 em 10.

- [ ] **Step 1: Write the failing tests** (modelo mockado via `mock.patch.object(vf, "_chamar_modelo", ...)`, igual às classes existentes)
  - `test_aprova_bau_inteiro`: leitura `{legivel True, eh_carga True, bau_inteiro True, ocupacao_pct 73, problemas [], motivo "ok"}` → `resultado == APROVADO` e `ocupacao_pct == 70`.
  - `test_reprova_bau_cortado`: `bau_inteiro False` → `REPROVADO`, motivo pede "baú inteiro", `ocupacao_pct` ainda devolvido.
  - `test_reprova_nao_e_carga`: `eh_carga False` → `REPROVADO`.
  - `test_haiku_fora_nao_trava`: `_chamar_modelo` levanta `RuntimeError` → `NAO_VERIFICADO` e `aplicar_tentativas(cfg, r, 1)["pode_seguir"] is True`.
  - `test_libera_na_ultima_tentativa_sem_excecao_da_fachada`: reprovado + `tentativa == cfg["max_tentativas"]` → `pode_seguir True` e `revisao_humana True`.
- [ ] **Step 2: Run** `py -3.11 -m unittest nucleo.test_validacao_fotos.TestCarregamento -v` → FAIL (tipo sem validação).
- [ ] **Step 3: Implement.** `_SCHEMA_CARREGAMENTO`/`_PROMPT_CARREGAMENTO` no estilo de `_SCHEMA_PEDAGIO` (campos acima + `problemas` enum `DESFOCADA|ESCURA|CORTADA|PORTA_FECHADA`). Prompt em português: foto do baú/carroceria já carregado, **porta aberta e baú inteiro visível**; estimar ocupação 0–100. `validar_foto` aceita o tipo novo (nitidez e fluxo de chamada iguais) e delega a `_decidir_carregamento(base, leitura)` → `REPROVADO` se `not eh_carga` ou `not bau_inteiro` ou `not legivel`, senão `APROVADO`; arredonda `ocupacao_pct` para o múltiplo de 10 mais próximo e põe no topo do dict.
- [ ] **Step 4: Run** a classe → PASS; rodar também `py -3.11 -m unittest nucleo.test_validacao_fotos` inteiro (sem regressão).
- [ ] **Step 5: Commit** — `Motorista: validacao de fotos ganha o tipo CARREGAMENTO (bau inteiro + ocupacao)`.

---

### Task 2: Config, tabelas e módulo `carregamento` (esqueleto + leitura)

**Files:**
- Create: `nucleo/carregamento.py`
- Modify: `nucleo/banco.py` (SCHEMA: duas tabelas novas, `CREATE TABLE IF NOT EXISTS`)
- Test: `nucleo/test_carregamento.py`

**Interfaces:**
- Produces:
  - `config_carregamento(config: dict | None = None) -> {"ativo": bool, "pilotos": set[int]}` (lê `api_motorista.checklist_carregamento`, mesmo estilo de `ponte_vuupt.config_ponte`).
  - `exigido(rota: sqlite3.Row, cfg: dict) -> bool`: `cfg["ativo"]`, rota editável (`operacao.rota_editavel`), status `PLANEJADA`/`ACEITA`, e `agent_id` em `pilotos` (ou `pilotos` vazio).
  - `carregamento_da_rota(conn, rota_id) -> dict | None`.
  - Tabela `nucleo_carregamentos`: `id, rota_id UNIQUE, agent_id, uuid UNIQUE, placa, tipo_veiculo, carregador, foto_caminho, foto_sha256, resultado_foto, ocupacao_pct, revisao_humana, qtd_carregados, qtd_desmarcados, criado_em`.
  - Tabela `nucleo_carregamento_itens`: `id, carregamento_id, rota_id, service_id, codigo, motivo, observacao, removido_em, criado_em` (um item por parada desmarcada; `removido_em` NULL = ainda não saiu da Vuupt).

- [ ] **Step 1: Write the failing tests:** `test_config_desligada_por_padrao` (config vazia → `ativo False`); `test_config_pilotos`; `test_exigido_so_com_chave_ligada_e_rota_aceita` (usa banco temporário como em `test_validacao_fotos`); `test_tabelas_existem` (`banco.garantir_schema`/equivalente cria as duas).
- [ ] **Step 2: Run** → FAIL (módulo inexistente).
- [ ] **Step 3: Implement** o módulo e as tabelas conforme Interfaces. Placa/tipo do veículo vêm de `motoristas` (campos `placa`, `tipo_veiculo`) pelo `agent_id`; `NULL` se ausente.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `Nucleo: tabelas e config do checklist de carregamento (chave desligada)`.

---

### Task 3: Regra de confirmar o carregamento (tirar da rota + gravar)

**Files:**
- Modify: `nucleo/carregamento.py`
- Test: `nucleo/test_carregamento.py`

**Interfaces:**
- Consumes: `carregamento.exigido`, tabelas da Task 2; `roteirizacao.edicao_rota.tirar_pedido(token, route_id, service_id) -> {"ok", "rota_cancelada", "rota_iniciada", "servico"} | {"ok": False, "erro", "fora_da_rota"?}`; `nucleo.pedidos.marcar_aberto_por_service_ids(ids)`.
- Produces: `confirmar(conn, rota, agent_id, *, carregador: str, uuid: str, desmarcados: list[dict], foto: dict, tirar=None) -> dict`
  - `desmarcados`: `[{"parada_id": int, "motivo": str, "observacao": str | None}]`.
  - `foto`: `{"caminho": str, "sha256": str, "resultado": str, "ocupacao_pct": int | None, "revisao_humana": bool}`.
  - `tirar`: função injetável `(rota, parada, motivo, observacao) -> dict` (padrão `_tirar_da_vuupt_ou_local`), para teste sem rede.
  - Devolve `{"ok": True, "carregamento": {...}, "rota_cancelada": bool}` ou, se algo falhou, `{"ok": False, "falhas": [{"parada_id", "codigo", "erro"}], "removidos": [parada_id, ...]}` **sem gravar** `nucleo_carregamentos`, mas mantendo gravados os itens já removidos (idempotência).
  - Erros de entrada levantam `operacao.OperacaoInvalida` (409/400): carregador vazio, motivo fora de `MOTIVOS_DESMARCAR = ("Não coube", "Não Encontrado", "Outros")`, "Outros" sem observação, parada de outra rota, rota já `EM_ROTA`, rota não exigida.
  - Reenvio do mesmo `uuid` devolve o carregamento já gravado.

- [ ] **Step 1: Write the failing tests** (com `tirar` falso):
  - `test_confirma_sem_desmarcados_grava_e_conta`: 3 paradas, nenhuma desmarcada → grava com `qtd_carregados 3`, `qtd_desmarcados 0`.
  - `test_desmarcar_tira_parada_e_devolve_pedido_ao_pool`: parada fica `CANCELADA` no núcleo, pedido `ABERTO` (`marcar_aberto_por_service_ids`), item gravado com `removido_em`.
  - `test_desmarcar_todos_cancela_a_rota` (`tirar` devolve `rota_cancelada True` na última): `nucleo_rotas.status == CANCELADA`, resposta `rota_cancelada True`.
  - `test_falha_parcial_nao_grava_e_repete_so_os_que_faltam`: 3 desmarcados, o 2º falha → `ok False`, `falhas` com o 2º, `removidos` com 1º e 3º; segunda chamada (já sem falha) só chama `tirar` para o 2º (contador do falso) e grava.
  - `test_uuid_repetido_devolve_o_mesmo`: segunda chamada igual não chama `tirar`.
  - `test_parada_ja_comecada_erro_claro`: `tirar` devolve erro de status → `falhas` traz a mensagem, nada gravado.
  - `test_outros_exige_observacao`, `test_carregador_vazio`, `test_rota_replica_nao_mexe_no_pedido_real` (rota `provedor APP` com `replica_de`: parada local sai, `marcar_aberto...` **não** é chamado).
  - `test_veiculo_sem_placa_grava_null`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `_tirar_da_vuupt_ou_local` : rota Vuupt → importar `edicao_rota` (adicionar `roteirizacao/` ao `sys.path` dentro da função), token de `config["vuupt_api"]["token"]`, chamar `tirar_pedido`; `fora_da_rota` conta como sucesso. Efeitos locais após sucesso: parada `CANCELADA`, `marcar_aberto_por_service_ids`, contadores (`operacao._recalcular_contadores`), rota `CANCELADA` se cancelada. Efeitos no card do painel (`rascunhos_rota.aplicar_efeitos_pos_remocao`: tira parada do rascunho, `reverter_para_rascunho` se a rota foi cancelada, auditoria `expedicao.registrar_exclusao`) em `try/except` best-effort com import lazy e log de aviso — localizar o rascunho pelo `vuupt_route_id` (ver `rascunhos_rota._rascunho_enviado`); se não for viável importar de dentro do `motorista-api`, registrar a limitação no `docs/revisoes` e seguir só com os efeitos do núcleo. Registrar evento `CARREGAMENTO` em `nucleo_eventos` via `operacao.registrar_evento`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `Nucleo: confirmar carregamento tira os desmarcados da rota e grava o checklist`.

---

### Task 4: Endpoints da API + trava no iniciar

**Files:**
- Modify: `nucleo/api_motorista.py` (validar-foto aceita `tipo=CARREGAMENTO` com `rota_id`; novo `POST /api/rotas/<id>/carregamento`; `checklist()` devolve a chave); `nucleo/operacao.py` (`montar_rota` ganha `carregamento: {"exigido": bool, "feito": bool}`; `iniciar_rota` recusa).
- Test: `nucleo/test_carregamento.py` (classe `TestApiCarregamento`, mesmo padrão de `test_validacao_fotos`: app Flask de teste, login por CPF/PIN, `_chamar_modelo` mockado)

**Interfaces:**
- Consumes: `carregamento.confirmar`, `carregamento.exigido`, `_guardar_foto`, `_validar_conteudo` (existentes no `api_motorista`).
- Produces: `POST /api/rotas/<id>/carregamento` multipart: `arquivo`, `carregador`, `uuid`, `desmarcados` (JSON string), `tentativa`; respostas: 200 `{ok True, carregamento, rota}`; 409 `{ok False, falhas, removidos}` (mensagem legível); 409 "Faça o checklist de carregamento antes de iniciar a rota." em `iniciar` quando `exigido and not feito`. `GET /api/checklist` ganha `"carregamento": {"ativo": bool}`.

- [ ] **Step 1: Write the failing tests:** foto aprovada + sem desmarcados → 200 e rota com `carregamento.feito True`; `iniciar` antes do checklist → 409 (chave ligada); `iniciar` com chave desligada → 200 como hoje; foto reprovada na `tentativa 1` → 409 com `pode_seguir False` e nada gravado; reprovada na última tentativa → 200 com `revisao_humana True`; `NAO_VERIFICADO` → 200; rota de outro motorista → 404; `uuid` repetido → mesmo corpo, sem segunda foto em disco; falha da Vuupt (falso) → 409 com `falhas`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** os pontos listados. O endpoint valida a foto com `_validar_conteudo("CARREGAMENTO", ..., rota_id=..., no_ato=True)` (reaproveita o cache por sha256 do `/fotos/validar`) + `validacao_fotos.aplicar_tentativas`, guarda com `_guardar_foto(conteudo, f"rota-{id}", "CARREGAMENTO", uuid, ext)` **só quando a confirmação segue**, e chama `carregamento.confirmar`. `TIPOS_COMPROVANTE` não muda (a foto não é de parada).
- [ ] **Step 4: Run** `py -3.11 -m unittest nucleo.test_carregamento nucleo.test_validacao_fotos -v` → PASS.
- [ ] **Step 5: Commit** — `Motorista: API do checklist de carregamento e trava no Iniciar rota (chave desligada)`.

---

### Task 5: Tela no app dos motoristas

**Files:**
- Create: `app_motorista/src/carregamentoRota.tsx` (componente da etapa)
- Modify: `app_motorista/src/tipos.ts` (campo `carregamento` em `Rota`; tipos `Desmarcado`, resposta do endpoint); `app_motorista/src/api.ts` (`confirmarCarregamento(rotaId, uri, carregador, uuid, desmarcados, tentativa)` com `enviarArquivo`; `validarFoto` aceita `tipo 'CARREGAMENTO'` com `rota_id`); `app_motorista/app/rota/[id].tsx` (quando `rota.status === 'ACEITA' && rota.editavel && rota.carregamento?.exigido && !rota.carregamento.feito`, renderiza o componente **no lugar** do deslizar "Iniciar rota", linha ~392)

**Interfaces:**
- Consumes: `POST /api/fotos/validar` (`tipo=CARREGAMENTO`, `rota_id`), `POST /api/rotas/<id>/carregamento` (Task 4); `rota.paradas` (`id`, `codigo`, `destinatario_nome`, `situacao`).
- Produces: `<CarregamentoRota rota={Rota} aoConfirmar={(rotaAtualizada) => void} />`.

- [ ] **Step 1: Implement tipos e api** (compila com `npm run typecheck`).
- [ ] **Step 2: Implement `CarregamentoRota`** seguindo o rascunho do design: campo "Quem carregou?" (texto livre + últimos 5 nomes em `AsyncStorage`, chave `carregadores`); botão de foto (mesmo seletor/câmera e fluxo de tentativas do canhoto na `app/parada/[id].tsx`: valida na hora, mostra veredito, "baú inteiro · ocupação ~N%", tentativa N de M); lista de paradas com caixa de seleção (todas marcadas), card mostra os pedidos juntos; ao desmarcar abre seletor de motivo (`Não coube`, `Não Encontrado`, `Outros` + texto); desmarcar todas mostra aviso "a rota será cancelada" + confirmação; "Confirmar carregamento" desabilitado até haver nome + foto aprovada/liberada; `uuid` gerado uma vez por tela (mesmo gerador do `fila.ts`); resposta `ok False` mostra qual pedido não saiu com botão "Tentar de novo"; sem rede, mensagem clara (não usa a fila offline: tirar da Vuupt é síncrono).
- [ ] **Step 3: Integrar em `rota/[id].tsx`** e, ao confirmar, atualizar o estado da rota (o deslizar "Iniciar rota" passa a aparecer; rota cancelada volta à lista).
- [ ] **Step 4: Verify** `cd app_motorista && npm run typecheck` → sem erros. Teste manual contra o painel local em outra porta (`api_motorista` de teste com chave ligada e Haiku mockado) com uma rota `ACEITA`: fluxo feliz, desmarcar um, desmarcar todos, foto reprovada.
- [ ] **Step 5: Commit** — `App motorista: etapa de carregamento antes de iniciar a rota`. Publicação (OTA) só com pedido do Hugo (`publicar-ota`); sem módulo nativo novo.

---

### Task 6: Selo "Não carregado" no pool + mapa do sistema

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py` (perto de `retornando_por_id`, ~linha 1015: `nao_carregado_por_servico(servicos_brutos)` e `p["nao_carregado"] = {...}`); `painel_agentes/templates/planejamento_rotas.html` (badge ao lado do `badge-retornando`, ~linha 3036); `nucleo/carregamento.py` (função de leitura); `MAPA_DO_SISTEMA.txt`
- Test: `nucleo/test_carregamento.py` (leitura) e, de dentro de `painel_agentes/`, teste do pool com selo (seguir `test_rota_enviada_edicao.py`/testes de pool existentes)

**Interfaces:**
- Produces: `carregamento.nao_carregados_recentes(conn, service_ids: list[int], dias: int = 2) -> dict[int, {"motivo", "observacao", "criado_em"}]`: item mais recente por `service_id`, `removido_em` não nulo, dos últimos `dias` dias; pedido que voltou a rota deixa de estar no pool e some sozinho.

- [ ] **Step 1: Write the failing tests:** leitura devolve só quem saiu da Vuupt e é recente; pool anota `nao_carregado` e não quebra com tabela ausente (banco antigo) — try/except com `logger.warning`, como o do `retornando`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** leitura, anotação e badge: "↺ Não carregado: <motivo>" com `title` da observação; **não** bloqueia roterização (diferente do Retornando). Atualizar `MAPA_DO_SISTEMA.txt`: `nucleo/carregamento.py`, as duas tabelas, endpoint novo, `src/carregamentoRota.tsx`, a chave de config e a armadilha "desmarcar = tirar parada inteira; Vuupt síncrona".
- [ ] **Step 4: Run** os testes do `nucleo` e, em comando separado, os do `painel_agentes` → PASS; `py -3.11 -m py_compile` nos arquivos editados.
- [ ] **Step 5: Commit** — `Painel: selo "Nao carregado" no pool e mapa do sistema atualizado`.

---

## Fechamento (fora das tasks)

- Prova real em **homologação** só se o Hugo pedir (`homolog subir checklist-carregamento`); deploy em produção só com "deploy", chave **desligada**; ligar é do Hugo.
- Registrar o 1º uso real (carregador, foto, ocupação) e a lista de fotos com `revisao_humana` para a tela de lotação futura (não-meta desta entrega).
