# Editar rota enviada (motorista, carro, pedidos) — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** no card da rota enviada do Planejamento, trocar motorista/carro, adicionar e tirar pedido (inclusive em rota iniciada), com motorista sempre ligado ao carro e pedido tirado de rota iniciada marcado "Retornando" até a /retornos receber.

**Architecture:** um módulo novo `roteirizacao/edicao_rota.py` concentra a regra (status ao vivo da Vuupt, "pode sair", "rota iniciada") e as chamadas novas do `rotas_client`. Planejamento, Torre e "Cancelar pedido" passam a usá-lo. Um módulo novo `roteirizacao/pedidos_retornando.py`, no molde de `pedidos_segurados.py`, guarda os retornando. O pool, o rascunho automático e o incremento pulam esses pedidos, e a /retornos os lista.

**Tech Stack:** Python 3.11, Flask (painel_agentes), SQLite `dados/dados.db`, JS puro em `planejamento_rotas.html`, API Vuupt (`api.vuupt.com/api/v1`).

**Spec:** `docs/superpowers/specs/2026-11-10-editar-rota-enviada-design.md` (leia antes; a tabela "O que a Vuupt aceita" é a base das travas).

## Global Constraints

- Trabalhar num worktree a partir de `origin/master` (a master local está 30 commits atrás e tem arquivos de outras sessões): skill superpowers:using-git-worktrees, ramo `editar-rota-enviada`. `git add` só dos arquivos da tarefa.
- Python sempre `py -3.11`. Testes com `py -3.11 -m unittest`; testes de `painel_agentes` rodam **de dentro de `painel_agentes/`**, os de `roteirizacao` da raiz. Nunca misture os dois num comando só.
- Nenhum teste chama a Vuupt de verdade nem o `dados/dados.db` real: duble do cliente + banco em `tempfile`.
- Pode sair da rota: status Vuupt `not_assigned`, `assigned`, `accepted`. Não pode: `on_route`, `arrived`, `done`, `canceled`.
- Rota iniciada: algum serviço `on_route`/`arrived`/`done`, ou status da rota `started`/`finished`.
- Veículo genérico = `license_plate` que começa com `"."`. Não conta como carro.
- Agentes virtuais fora das travas de carro: Lalamove 50258, Retirada 50259 (e TESTE 28438 só em teste).
- Texto da trava de envio: `Motorista {nome} sem carro cadastrado: escolha o carro no card ou cadastre o veículo dele.`
- Texto do selo do pool: `Retornando — no carro de {motorista}`.
- Texto da trava de motorista: `Rota já começou: só o carro pode mudar.`
- Grupo de alerta: "Alertas" do /atendimento (`retornos.postar_alerta`).
- Endpoints novos: `@requer_auth(niveis=("total", "operador", "logistica"))`, `@exige_mesma_origem`, `@bloqueia_planejamento_passado`; rota nova entra em `painel_agentes/acesso_telas.TELAS` se for caminho novo.
- Comentários e logs sem acento nos arquivos que já seguem esse padrão. Commits `Área: o que mudou e por quê`, com a linha `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Commit e push só quando o Hugo pedir. Os "Commit" dos passos são commits locais no ramo do worktree.

## Review Focus

1. **Card velho:** a tela mostra um pedido "assigned", mas na Vuupt ele já está `on_route`. A remoção precisa recusar pelo status **ao vivo**, nunca pelo do card (Task 2, `test_recusa_pelo_status_ao_vivo`).
2. **Último pedido pendente numa rota com entregue:** tirar o último pendente de uma rota que tem `done` não pode cancelar a rota, porque a Vuupt cancelaria em cima do entregue (Task 2, `test_ultimo_pendente_com_entregue_nao_cancela_rota`).
3. **Pedido combinado** (`code` = `"#PS-1, #PS-2"`): Retornando e o filtro do pool têm que pegar pelo `service_id`, não pelo código (Task 3, `test_separa_por_service_id_em_codigo_combinado`).
4. **Corrida de dois cliques:** dois usuários tiram o mesmo pedido, e o segundo DELETE volta 200 com o pedido já fora. Não pode duplicar o Retornando (Task 3, `test_marcar_duas_vezes_mantem_um_ativo`).
5. **Rota da Lalamove com corrida lançada:** adicionar, tirar e trocar têm que recusar (Task 6, `test_lalamove_com_corrida_recusa_edicao`).

---

### Task 1: Chamadas novas no cliente de rotas

**Files:**
- Modify: `roteirizacao/rotas_client.py` (junto de `adicionar_atividades`)
- Test: `roteirizacao/test_rotas_client_edicao.py` (novo)

**Interfaces:**
- Produces: `remover_servico_da_rota(token: str, route_id: int, service_id: int) -> dict` (`DELETE {API_BASE}/routes/{route_id}/services/{service_id}`, devolve o JSON); `atualizar_motorista_veiculo(token: str, route_id: int, agent_id: int | None, vehicle_id: int | None) -> dict` (`PUT {API_BASE}/routes/{route_id}` com `{"agent_id": ..., "vehicle_id": ...}` e **sem** `activities`; devolve `resp.json()["route"]`). Ambas via `chamar_com_retry` + `_verificar_resposta`.

- [ ] **Step 1: Testes que falham** (patch de `rotas_client.chamar_com_retry` com um duble que grava método, url e json):
  - `test_remover_servico_usa_delete_no_caminho_certo`: url termina em `/routes/5449893/services/54894926`, método `requests.delete`.
  - `test_atualizar_motorista_veiculo_nao_manda_activities`: json == `{"agent_id": 28438, "vehicle_id": 74274}`; `"activities" not in json`.
  - `test_atualizar_motorista_veiculo_aceita_none`: `agent_id=None` vai como `null` (chave presente).
- [ ] **Step 2:** `py -3.11 -m unittest roteirizacao.test_rotas_client_edicao -v` → FAIL (AttributeError).
- [ ] **Step 3:** implementar as duas funções.
- [ ] **Step 4:** o mesmo comando → PASS.
- [ ] **Step 5: Commit** `Roteirizacao: rotas_client tira servico da rota (DELETE) e troca motorista/carro sem mexer nas paradas`.

---

### Task 2: Regra de edição de rota ao vivo

**Files:**
- Create: `roteirizacao/edicao_rota.py`
- Test: `roteirizacao/test_edicao_rota.py`

**Interfaces:**
- Consumes: Task 1; `rotas_client.buscar_rota(token, id, include=["services"])`, `adicionar_atividades`, `cancelar_rota`; `nucleo.sincronizar_vuupt._rota_do_corpo` (ou o helper equivalente de `expedicao`).
- Produces:
  - `STATUS_PODE_SAIR = frozenset({"not_assigned", "assigned", "accepted"})`; `STATUS_COMECADO = frozenset({"on_route", "arrived", "done"})`.
  - `ler_rota(token, route_id) -> dict` → `{"id", "status", "agent_id", "vehicle_id", "servicos": [{"id", "code", "status", "dimension_3"}]}`. Levanta `ErroEdicao("Rota #X não existe mais na Vuupt.")` no 404.
  - `rota_iniciada(rota: dict) -> bool` (definição dos Global Constraints).
  - `tirar_pedido(token, route_id, service_id) -> dict` → `{"ok": True, "rota_cancelada": bool, "rota_iniciada": bool, "servico": {...}}` ou `{"ok": False, "erro": str}`.
  - `trocar_motorista_veiculo(token, route_id, agent_id, vehicle_id) -> dict` → `{"ok": True, "rota": {...}}` / `{"ok": False, "erro": str}`.
  - `adicionar_pedidos(token, route_id, service_ids: list[int], position="end") -> dict` → `{"ok": True, "falharam": [ids]}` (lê `data.info.failed`) / `{"ok": False, "erro": str}`.
  - `class ErroEdicao(Exception)`.

- [ ] **Step 1: Testes que falham** (duble de `rotas_client` com rota em memória):
  - `test_tira_pendente_usa_delete`: serviço `assigned` → `ok`, DELETE chamado, `rota_cancelada False`.
  - `test_recusa_pelo_status_ao_vivo`: o duble devolve `on_route` → `ok False`, erro contém `"já começou"`, nenhum DELETE.
  - `test_recusa_entregue_e_insucesso`: `done` → `ok False`, erro contém `"concluído"`.
  - `test_ultimo_pedido_sem_nada_comecado_cancela_rota`: rota com 1 serviço `assigned` → `cancelar_rota(..., services_action="unassign")`, `rota_cancelada True`.
  - `test_ultimo_pendente_com_entregue_nao_cancela_rota`: rota [`done`, `assigned`] → DELETE, sem `cancelar_rota`, `rota_iniciada True`.
  - `test_pedido_fora_da_rota`: service_id ausente → `ok False`, erro `"Pedido ... não está mais nessa rota."`.
  - `test_rota_encerrada_recusa`: status `finished`/`canceled` → `ok False`.
  - `test_troca_motorista_recusada_em_rota_iniciada`: rota com `on_route` e `agent_id` diferente do atual → `ok False`, erro == `"Rota já começou: só o carro pode mudar."`, sem PUT.
  - `test_troca_so_carro_em_rota_iniciada`: mesmo `agent_id`, veículo novo → PUT chamado.
  - `test_troca_motorista_em_rota_nao_iniciada`: `assigned`/`accepted` → PUT com o par novo.
  - `test_adicionar_devolve_falharam`: `info.failed=[{"id": 9}]` → `falharam == [9]`.
- [ ] **Step 2:** `py -3.11 -m unittest roteirizacao.test_edicao_rota -v` → FAIL.
- [ ] **Step 3:** implementar. `tirar_pedido` sempre chama `ler_rota` antes. A rota é cancelada só quando ela fica sem nenhum serviço e nenhum outro começou.
- [ ] **Step 4:** PASS.
- [ ] **Step 5: Commit** `Roteirizacao: edicao_rota - regra unica de tirar/adicionar pedido e trocar motorista/carro em rota enviada`.

---

### Task 3: Tabela de pedidos Retornando

**Files:**
- Create: `roteirizacao/pedidos_retornando.py` (molde: `roteirizacao/pedidos_segurados.py`)
- Test: `roteirizacao/test_pedidos_retornando.py`

**Interfaces:**
- Produces:
  - Tabela `pedidos_retornando(id INTEGER PK, service_id INTEGER NOT NULL, codigo TEXT, vuupt_route_id INTEGER, parada_id INTEGER, agent_id INTEGER, motorista_nome TEXT, motivo TEXT, observacao TEXT, usuario TEXT, criado_em TEXT NOT NULL, recebido_em TEXT, descarga_id INTEGER)`, com índice único parcial `ON pedidos_retornando(service_id) WHERE recebido_em IS NULL`.
  - `conectar(db_path=DB_PATH) -> sqlite3.Connection` (cria a tabela); `garantir_tabela(conn) -> None` (para quem já tem conexão, ex. retornos).
  - `marcar(conn, *, service_id, codigo, vuupt_route_id, agent_id, motorista_nome, motivo, observacao, usuario) -> int | None` (resolve `parada_id` com `resolver_parada_id`; `INSERT OR IGNORE`, então já ativo → None).
  - `resolver_parada_id(conn, vuupt_route_id, service_id) -> int | None` (`nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id WHERE r.vuupt_route_id = ? AND p.service_id = ?`, a mais recente).
  - `ativos(conn) -> dict[int, dict]` (por service_id, `recebido_em IS NULL`).
  - `marcar_recebidos(conn, parada_ids: list[int], descarga_id: int) -> None`.
  - `separar_retornando(servicos: list[dict], db_path=DB_PATH) -> tuple[list[dict], list[dict]]` (por `servico["id"]`; erro de banco → loga e devolve tudo em "ficam", como `separar_segurados`).
  - `retornando_por_servico(servicos, db_path=DB_PATH) -> dict[int, dict]`.

- [ ] **Step 1: Testes que falham:**
  - `test_marcar_e_listar_ativo`.
  - `test_marcar_duas_vezes_mantem_um_ativo`: segundo `marcar` → None, `len(ativos) == 1`.
  - `test_recebido_sai_dos_ativos`: `marcar_recebidos([parada_id], 7)` → não está em `ativos`; um `marcar` novo depois disso vale (o pedido pode voltar a ser retirado outro dia).
  - `test_separa_por_service_id_em_codigo_combinado`: serviço `{"id": 5, "code": "#PS-1, #PS-2"}` marcado → vai para "retornando".
  - `test_resolve_parada_id_pelo_nucleo`: banco com `nucleo_rotas(vuupt_route_id=99)` e `nucleo_paradas(service_id=5)` → `parada_id` gravado.
  - `test_banco_com_erro_nao_derruba_rodada`: `db_path` inválido → `separar_retornando` devolve tudo em "ficam".
- [ ] **Step 2:** `py -3.11 -m unittest roteirizacao.test_pedidos_retornando -v` → FAIL.
- [ ] **Step 3:** implementar.
- [ ] **Step 4:** PASS.
- [ ] **Step 5: Commit** `Roteirizacao: pedidos_retornando - pedido tirado de rota iniciada fica retido ate o galpao receber`.

---

### Task 4: Retornando na /retornos (e na cobrança das 07h)

**Files:**
- Modify: `painel_agentes/retornos.py` (`conectar`, `_SQL_RETORNOS`, `_pedido`, `_retornos_pendentes`, `_paradas_por_id`, `registrar_descarga`)
- Modify: `painel_agentes/templates/retornos.html` (rótulo do tipo)
- Test: `painel_agentes/test_retornos.py` (casos novos)

**Interfaces:**
- Consumes: Task 3 (`garantir_tabela`, `marcar_recebidos`, tabela `pedidos_retornando`).
- Produces: item de pedido com `"tipo": "RETIRADO"` (os de hoje: `"INSUCESSO"`/`"PARCIAL"`), `motivo = "Retirado da rota: {motivo}"`, `caixas_esperadas = volume_caixas`, `dia_insucesso = criado_em[:10]`.

- [ ] **Step 1: Testes que falham** (banco temporário com `nucleo_rotas`/`nucleo_paradas` como os testes atuais montam):
  - `test_retirado_aparece_nos_pendentes`: parada `CANCELADA` + `pedidos_retornando` ativo → aparece no grupo do motorista com `tipo == "RETIRADO"`.
  - `test_retirado_recebido_sai_e_baixa_retornando`: `registrar_descarga` com o `parada_id` → sai dos pendentes e `pedidos_retornando.recebido_em` preenchido.
  - `test_retirado_entra_na_cobranca_atrasada`: criado ontem, `pendentes_atrasados(hoje)` inclui.
  - `test_cancelada_sem_retornando_continua_fora`: parada `CANCELADA` sem linha em `pedidos_retornando` → não aparece.
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_retornos -v` → os novos FAIL.
- [ ] **Step 3:** implementar. `_SQL_RETORNOS` passa a pegar `p.situacao IN ('INSUCESSO','PARCIAL') OR p.id IN (SELECT parada_id FROM pedidos_retornando WHERE recebido_em IS NULL)`. Para esse tipo não se aplica o filtro de "Não coletado". `registrar_descarga` chama `marcar_recebidos` na mesma transação, antes do commit. Na tela, o rótulo "Retirado da rota" aparece onde hoje mostra "Parcial".
- [ ] **Step 4:** `cd painel_agentes && py -3.11 -m unittest test_retornos test_retornos_tela -v` → PASS.
- [ ] **Step 5: Commit** `Retornos: pedido tirado de rota iniciada entra na lista e na cobranca das 07h`.

---

### Task 5: Pool, rascunho automático e incremento pulam Retornando

**Files:**
- Modify: `roteirizacao/criar_rotas_diarias.py` (logo depois de `separar_segurados`, ~l.950)
- Modify: `roteirizacao/incrementar_rotas.py` (logo depois de `separar_segurados`, ~l.558)
- Modify: `painel_agentes/planejamento_rotas.py` (pool: junto de `segurados_por_servico`, ~l.960; `incrementar_rascunhos_com_selecionados`; roteirizar-selecionados)
- Modify: `painel_agentes/rascunhos_rota.py` (`adicionar_parada`, `mover_parada(s)` para rascunho)
- Test: `roteirizacao/test_incrementar_rotas.py` (caso novo), `painel_agentes/test_incrementar_rascunhos.py` (caso novo)

**Interfaces:**
- Consumes: Task 3 (`separar_retornando`, `retornando_por_servico`).
- Produces: item do pool com `retornando: {"motorista_nome", "motivo", "criado_em"} | None`; `rascunhos_rota.adicionar_parada` levanta `ValueError("Pedido retornando ao galpão: só entra em rota depois do recebimento em /retornos.")`.

- [ ] **Step 1: Testes que falham:**
  - `test_incremento_pula_retornando` (roteirizacao): serviço retornando não é encaixado.
  - `test_incrementar_selecionados_pula_retornando` (painel): o pedido volta em "não colocados" com o motivo.
  - `test_adicionar_parada_recusa_retornando` (painel): `ValueError` com o texto acima.
- [ ] **Step 2:** rodar os dois arquivos de teste (cada um no seu diretório) → FAIL.
- [ ] **Step 3:** implementar. No pool, o pedido continua aparecendo com o selo, e o front (Task 8) bloqueia o arraste.
- [ ] **Step 4:** PASS, mais `py -3.11 -m unittest roteirizacao.test_pedidos_segurados roteirizacao.test_incrementar_rotas_prontidao -v` sem regressão.
- [ ] **Step 5: Commit** `Roteirizacao: pedido retornando fica fora do rascunho, do incremento e da rota manual`.

---

### Task 6: Backend do card da rota enviada (Planejamento) + Cancelar pedido

**Files:**
- Modify: `painel_agentes/rascunhos_rota.py`
- Modify: `painel_agentes/painel_agentes.py` (`api_trocar_motorista`, `api_adicionar_parada`, `api_remover_parada`, `api_confirmar_envio`)
- Modify: `painel_agentes/planejamento_rotas.py` (`cancelar_pedido`, `buscar_dados_planejamento`)
- Modify: `painel_agentes/expedicao.py` (expor `registrar_exclusao` público, que chama o `_registrar_exclusao` atual)
- Test: `painel_agentes/test_rota_enviada_edicao.py` (novo)

**Interfaces:**
- Consumes: Tasks 2, 3, 5.
- Produces (em `rascunhos_rota`):
  - `trocar_motorista(rascunho_id, agent_id, vehicle_id, motorista_nome, token: str | None = None)`: em rascunho ENVIADO chama `edicao_rota.trocar_motorista_veiculo` e só grava no banco local depois do ok. Erro → `ValueError(erro)`. Em RASCUNHO segue como hoje.
  - `adicionar_em_rota_enviada(rascunho_id, parada: dict, token, usuario) -> dict`: recusa retornando, pedido em espera na Stokki (`prontidao_stokki.situacao_nao_pronta` com o retrato atual; só "Em espera" barra) e Lalamove com `lalamove_order_id`. Depois do ok: `_inserir_parada`, `marcar_alocado`, `nucleo.pedidos.marcar_em_rota_por_service_ids`, `documentacao_rota.agendar(route_id, data_alvo, motivo="pedido incluido na rota")` e `eventos_freshhub.rota_enviada` só dessa parada (via `_avisar_freshhub`).
  - `remover_de_rota_enviada(rascunho_id, service_id, motivo, observacao, token, usuario) -> dict`: `motivo` tem que estar em `expedicao.MOTIVOS_EXCLUSAO`; chama `edicao_rota.tirar_pedido`; depois do ok faz `expedicao.registrar_exclusao(...)`; se `rota_iniciada`, também `pedidos_retornando.marcar(...)`; `remover_parada` local; romaneio refeito, ou descartado se a rota foi cancelada (nesse caso `reverter_para_rascunho` como hoje).
  - `rascunho_por_vuupt_route_id(vuupt_route_id) -> dict | None` (só ENVIADO).
- Endpoints: os de hoje aceitam ENVIADO e repassam o token. `remover-parada` passa a exigir `motivo` quando a rota está ENVIADA. `api_confirmar_envio` recusa motorista real sem carro (veja a Task 7).
- `preparar_cancelamento_de_parada`: em ENVIADO troca a trava de "rota não iniciada" e o PUT por `edicao_rota.tirar_pedido`. Pedido cancelado **não** vira retornando.
- `buscar_dados_planejamento`: em rascunho ENVIADO, cada parada ganha `status_vuupt` (do espelho `nucleo_paradas`, atualizado a cada 15 min, só para exibir) e o rascunho ganha `rota_iniciada: bool`. A badge de confirmação ignora a linha de `confirmacoes_rota` cujo `agent_id` difere do `agent_id` atual do rascunho.

- [ ] **Step 1: Testes que falham** (rascunho ENVIADO em banco temporário, `edicao_rota` com duble):
  - `test_troca_motorista_enviada_chama_vuupt_e_grava`.
  - `test_troca_motorista_enviada_erro_nao_grava`.
  - `test_adicionar_em_enviada_grava_parada_e_agenda_romaneio`.
  - `test_adicionar_recusa_em_espera_stokki`.
  - `test_remover_em_rota_iniciada_marca_retornando`.
  - `test_remover_em_rota_nao_iniciada_nao_marca_retornando`.
  - `test_remover_sem_motivo_valido_recusa`.
  - `test_cancelar_pedido_em_rota_iniciada_usa_regra_nova` (pendente sai; `on_route` recusa).
  - `test_lalamove_com_corrida_recusa_edicao` (trocar, adicionar e remover).
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_rota_enviada_edicao -v` → FAIL.
- [ ] **Step 3:** implementar.
- [ ] **Step 4:** PASS, mais `cd painel_agentes && py -3.11 -m unittest test_incrementar_rascunhos test_torre_marcar_entregue -v` sem regressão.
- [ ] **Step 5: Commit** `Planejamento: rota enviada aceita trocar motorista/carro, incluir e tirar pedido (com Retornando)`.

---

### Task 7: Motorista sempre com carro

**Files:**
- Create: `regras/veiculos.py`
- Modify: `painel_agentes/painel_agentes.py` (`api_confirmar_envio`; GET novo `/api/planejamento/veiculos`)
- Modify: `roteirizacao/enviar_rascunhos_pendentes.py` (alerta depois do envio automático)
- Test: `regras/test_veiculos.py`, `roteirizacao/test_enviar_rascunhos_pendentes.py` (caso novo; crie o arquivo se não existir)

**Interfaces:**
- Produces (`regras/veiculos.py`):
  - `AGENTES_SEM_CARRO = frozenset({50258, 50259})`.
  - `eh_generico(placa: str | None) -> bool` (`(placa or "").strip().startswith(".")`).
  - `falta_carro(agent_id, vehicle_id, placa_do_veiculo: str | None) -> bool` (False sem agente ou com agente virtual; True sem `vehicle_id` ou com genérico).
  - `listar_para_seletor(veiculos_vuupt: list[dict], motoristas: list[dict]) -> list[dict]` → `[{"vehicle_id", "placa", "dono": nome|None}]`: sem `deleted_at`, sem genérico, primeiro os que têm dono (pelo `vehicle_id` do catálogo), depois os demais por placa.
- `/api/planejamento/veiculos?data=YYYY-MM-DD`: `nucleo.vuupt_agentes` `listar_veiculos()` com cache em memória de 10 min, devolvido por `listar_para_seletor`. Cada item ganha `rodizio_no_dia: bool`, calculado pela função de placa e dia que já existe em `roteirizacao/rodizio_sp.py` (é a usada pela trava da alocação). Teste: `test_seletor_marca_rodizio_do_dia`.
- `api_confirmar_envio`: para cada rascunho com `falta_carro`, não envia e devolve esse item com `{"ok": False, "erro": <texto dos Global Constraints>}`; os outros seguem.
- `enviar_rascunhos_pendentes`: envia mesmo assim; para cada enviado com `falta_carro`, posta em Alertas `Rota {nome} saiu sem carro: motorista {motorista} sem carro cadastrado.` Em `--modo-teste` não posta, só loga.

- [ ] **Step 1: Testes que falham:** `test_generico_por_ponto`, `test_virtual_nao_falta_carro`, `test_sem_vehicle_falta`, `test_seletor_tira_generico_e_excluido_e_poe_dono_primeiro`, `test_envio_automatico_sem_carro_posta_alerta`, `test_envio_automatico_modo_teste_nao_posta`.
- [ ] **Step 2:** `py -3.11 -m unittest regras.test_veiculos roteirizacao.test_enviar_rascunhos_pendentes -v` → FAIL.
- [ ] **Step 3:** implementar.
- [ ] **Step 4:** PASS.
- [ ] **Step 5: Commit** `Planejamento: motorista sempre com carro - envio manual barra, automatico avisa no grupo Alertas`.

---

### Task 8: Tela Planejamento (card enviado + pool)

**Files:**
- Modify: `painel_agentes/templates/planejamento_rotas.html` (`criarCardRota`, `montarSelectMotorista`, handler de `.select-motorista` ~l.7674, `criarLiParada`, drag/drop para o card, render do pool)

**Interfaces:**
- Consumes: os endpoints da Task 6, `/api/planejamento/veiculos` (Task 7), os campos `status_vuupt`, `rota_iniciada`, `retornando` e o selo `sem carro`.

Comportamento (a tela segue o estilo do que já existe no card):
- `bloqueada` passa a ser só `ofertada` para motorista, carro, X e drop. Ações de rascunho (Otimizar, Inverter, Descartar, Fundir, Publicar) continuam escondidas na ENVIADO.
- O seletor de motorista fica desligado em ENVIADO com `rota_iniciada`, com o title da trava. Um `<select class="select-veiculo">` novo fica ao lado: o valor é o `vehicle_id` do rascunho, e trocar o motorista põe o carro do motorista. Os dois chamam `/api/planejamento/trocar-motorista`.
- Rodízio: ao trocar o carro, se a placa está no rodízio do dia e a rota tem parada no Centro Expandido, faz `confirm()` antes de gravar. A informação vem do backend junto da lista de veículos (`rodizio_no_dia`, Task 7); o card já sabe se a rota tem parada no Centro Expandido pelas travas atuais.
- Selo `sem carro` no card quando `falta_carro`.
- Parada de rota ENVIADO: mostra `status_vuupt`. O X só aparece se o status está em `STATUS_PODE_SAIR`. Clicar no X abre um diálogo com select de motivo (`MOTIVOS_EXCLUSAO`, que o backend passa na página) e observação.
- Drop do pool num card ENVIADO chama o endpoint de adicionar e, no erro, mostra `alert` com a mensagem.
- No pool, o pedido com `retornando` mostra o selo do texto dos Global Constraints, sem checkbox e sem arraste.

- [ ] **Step 1:** implementar.
- [ ] **Step 2: Verificação no navegador** (memória `feedback_testar_painel_playwright_local`: painel em outra porta, `/login?proximo=`, bloquear `/api/torre/**`): com o banco local, um rascunho ENVIADO de teste (as chamadas da Vuupt com duble ou com o agente TESTE), conferir que o seletor de carro grava, que o X some em parada `on_route`, que o diálogo de motivo aparece e que o selo Retornando aparece no pool. Guardar screenshot no scratchpad.
- [ ] **Step 3: Commit** `Planejamento: card da rota enviada com seletor de carro, X com motivo e selo Retornando no pool`.

---

### Task 9: Torre usa a mesma regra

**Files:**
- Modify: `painel_agentes/expedicao.py` (`excluir_pedido_da_rota`)
- Test: `painel_agentes/test_expedicao_excluir.py` (novo)

**Interfaces:**
- Consumes: Tasks 2, 3, 6 (`rascunho_por_vuupt_route_id`, `remover_parada`).

- [ ] **Step 1: Testes que falham:**
  - `test_torre_recusa_pedido_a_caminho`.
  - `test_torre_recusa_entregue`.
  - `test_torre_tira_pendente_com_delete_e_atualiza_rascunho`.
  - `test_torre_em_rota_iniciada_marca_retornando`.
  - `test_expedicao_mantem_trava_de_rota_nao_iniciada` (`permitir_rota_em_andamento=False` com rota `started` → recusa como hoje).
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_expedicao_excluir -v` → FAIL.
- [ ] **Step 3:** implementar. Com `permitir_rota_em_andamento=True`, a função delega a `edicao_rota.tirar_pedido` e mantém `_registrar_exclusao`, o romaneio e a data. O caminho da Expedição também passa a usar o DELETE.
- [ ] **Step 4:** PASS, mais `cd painel_agentes && py -3.11 -m unittest test_torre_acumulada test_torre_tratar -v` sem regressão.
- [ ] **Step 5: Commit** `Torre: Excluir da Rota recusa pedido comecado ou entregue, usa DELETE e atualiza o card do Planejamento`.

---

### Task 10: Mapa, prova real com o agente TESTE, entrega

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (módulos `edicao_rota.py`, `pedidos_retornando.py`, `regras/veiculos.py`, endpoint `/api/planejamento/veiculos`, tabela `pedidos_retornando`, a /retornos com o tipo "Retirado da rota")

- [ ] **Step 1:** atualizar o mapa.
- [ ] **Step 2:** `py -3.11 -m py_compile` em todos os `.py` alterados → sem saída.
- [ ] **Step 3: Prova real local contra a Vuupt** (agente TESTE 28438, cliente 14425519, veículos 74274/103399, como nas sondas de 10/11 no scratchpad): chamar `edicao_rota` e `rascunhos_rota` diretamente com um rascunho ENVIADO de teste no banco local para: trocar carro, trocar motorista (TESTE ↔ sem motorista), adicionar, tirar pendente, tentar tirar `done` (tem que recusar sem chamar DELETE). Depois, rota em rota iniciada → Retornando criado. Limpar tudo no fim (cancelar rota e serviços).
- [ ] **Step 4: Commit** `Mapa: edicao de rota enviada, pedidos retornando e veiculos`.
- [ ] **Step 5:** relatar ao Hugo. Deploy só quando ele pedir (skill deploy-vps: reiniciar `painel-agentes`; timers pegam sozinhos). Lembrar que a trava de envio sem carro só deve ir ao ar depois de a lista de motoristas sem carro estar acertada.
