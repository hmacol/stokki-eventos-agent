# "Em espera" a pedido do cliente — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pedido que o cliente pediu "em espera" fica fora de toda roteirização automática até o próprio cliente retomar pelo portal com Agendamento ou Próxima rota. Nada na Stokki.

**Architecture:** Uma tabela nossa `pedidos_pausados_cliente` (módulo raiz `pedidos_pausados_cliente.py`, no molde de `roteirizacao/pedidos_segurados.py`) é a fonte da verdade. O executor da tela `/solicitacoes-portal` tira da rota na Vuupt e abre a linha; os três leitores do pool pulam esses pedidos; o portal ganha os estados `EM_ESPERA_CLIENTE`/`RETOMANDO` e a ação "Retomar"; o worker do portal (`enviar_stokki.py --loop`) aplica a data na Vuupt e fecha a linha.

**Tech Stack:** Python 3.11, sqlite3 (`dados/dados.db`), Flask (portal e painel), unittest.

**Spec:** `docs/superpowers/specs/2026-10-25-em-espera-a-pedido-do-cliente-design.md` (ler junto).

## Global Constraints

- Português em código, comentários e commits (`Área: o que mudou e por quê`); `py -3.11`; unittest; testes do painel de dentro de `painel_agentes/` (runner de config falsa no worktree), nunca misturados com outras suítes.
- Worktree novo a partir de `origin/master`; `git add` só os arquivos da tarefa; commits locais ok; push/deploy só quando o Hugo pedir.
- **Nada na Stokki ligado à pausa** (decisão do Hugo 27/10): nem Pausar, nem Reabrir; Estação, cobrança e vigia intocados.
- **Pausado = linha aberta (`retomado_em IS NULL`) em `pedidos_pausados_cliente`.** Nenhuma outra regra.
- **Falha ao ler a tabela nunca bloqueia nada:** leitura devolve vazio com log (como `pedidos_segurados._ativos_ou_vazio`).
- Códigos normalizados `PS-12345` (`pedidos_dedicados.normalizar_codigo` / `codigos_do_servico`); reentrega (`-R1`) conta pelo código base (`nucleo/prontidao_stokki._codigo_base`).
- Vuupt sempre `PUT`; testes nunca chamam Vuupt real.
- **Filtro da roteirização logo depois de `listar_pool_not_assigned`**, antes de `aplicar_regioes_dia_fixo` (senão o dia fixo grava data e avisa o remetente de um pedido pausado). A tela de planejamento **marca** (não esconde) os pausados.
- Sem e-mail/WhatsApp novo ao cliente. Aviso à operação só pelo `avisar_operacao` que o worker já usa.
- Atualizar `MAPA_DO_SISTEMA.txt` no commit que cria arquivo/rotina/tela.

## Review Focus

1. **Tabela indisponível** (erro de banco) não bloqueia a roteirização — Task 1 (`test_erro_de_banco_devolve_vazio`) e Task 2 (`test_pool_segue_sem_tabela`).
2. **Reentrega** (`PS-1-R1`) de um pedido pausado também é pulada — Task 2 (`test_reentrega_do_pausado_fica_fora`).
3. **Falha na Vuupt ao retomar** deixa o pedido pausado, avisa a operação e pode ser repetida sem duplicar — Task 5 (`test_vuupt_falha_volta_em_espera_e_avisa`, `test_retomar_de_novo_e_idempotente`).
4. **Cliente cancela um pedido em espera**: o cancelamento normal roda e a linha fecha — Task 4 (`test_cancelar_em_espera_vira_cancelando`) e Task 5 (`test_cancelamento_concluido_fecha_pausa`).
5. **Worker morre no meio da retomada**: `RETOMANDO` órfão volta a `EM_ESPERA_CLIENTE` — Task 5 (`test_orfao_retomando_volta`).

---

## Estrutura de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `pedidos_pausados_cliente.py` (+ `test_pedidos_pausados_cliente.py`) | tabela, abrir/fechar, leitura, `separar_pausados`, marcar serviços |
| `roteirizacao/criar_rotas_diarias.py`, `roteirizacao/incrementar_rotas.py`, `painel_agentes/planejamento_rotas.py` | filtro/marcação na roteirização |
| `portal_cliente/solicitacoes_execucao.py` | ligar em_espera sem Stokki, abrir a pausa |
| `portal_cliente/envio_pedidos.py`, `portal_cliente/app.py`, `portal_cliente/templates/_envios.html` | estados e ação "Retomar" |
| `portal_cliente/retomada.py` (+ teste), `portal_cliente/enviar_stokki.py`, `portal_cliente/cancelamento.py` | worker da retomada; cancelamento fecha a pausa |
| `stokki/pausar.py`, `stokki/test_pausar.py` | **apagar** (só o executor usava) |

---

### Task 1: Tabela e leitura

**Files:** Create `pedidos_pausados_cliente.py`, `test_pedidos_pausados_cliente.py`; Modify `MAPA_DO_SISTEMA.txt`.

**Interfaces — Produces:**
- `DB_PATH = <raiz>/dados/dados.db`; `conectar(db_path=None) -> sqlite3.Connection` (cria `pedidos_pausados_cliente(id INTEGER PK, codigo TEXT NOT NULL, id_stokki INTEGER, envio_id INTEGER, service_ids TEXT, pausado_em TEXT NOT NULL, retomado_em TEXT, fim TEXT)` + índice em `codigo`).
- `abrir(conn, codigo, id_stokki, envio_id, service_ids: list[int], agora) -> int` (não duplica se já houver aberta para o código; devolve o id).
- `fechar(conn, codigo, fim: str, agora) -> bool` (`fim` ∈ `"retomado"`, `"cancelado"`).
- `codigos_pausados(db_path=None) -> set[str]` (erro → vazio + log).
- `separar_pausados(servicos: list[dict], db_path=None) -> tuple[list, list]` (descarta o serviço se qualquer código dele, pelo código base, está pausado).
- `pausados_por_servico(servicos, db_path=None) -> set[int]` (ids de serviço pausados, para a tela marcar).

- [ ] **Step 1: Testes que falham:** `test_abrir_e_fechar`, `test_abrir_duas_vezes_nao_duplica`, `test_separar_pausados_pelo_codigo_base` (serviço `code="#PS-10-R1"` com PS-10 pausado sai), `test_servico_com_varios_codigos`, `test_erro_de_banco_devolve_vazio` (caminho inválido → `set()` e `([...todos], [])`), `test_fechado_nao_conta`.
- [ ] **Step 2–4:** `py -3.11 -m unittest test_pedidos_pausados_cliente` RED → GREEN.
- [ ] **Step 5: Commit** `Em espera: tabela de pedidos pausados a pedido do cliente`.

### Task 2: Roteirização respeita a pausa

**Files:** Modify `roteirizacao/criar_rotas_diarias.py` (~:870), `roteirizacao/incrementar_rotas.py` (~:460), `painel_agentes/planejamento_rotas.py` (~:906 marcar `p["pausado_cliente"]` e ~:1224 filtrar no "roteirizar selecionados"); tests nos `test_*.py` existentes desses módulos (ou novos).

**Interfaces — Consumes:** Task 1 `separar_pausados`, `pausados_por_servico`.

- [ ] **Step 1: Testes que falham:** nos dois robôs, `test_pausado_fica_fora_do_pool_antes_do_dia_fixo` (com `aplicar_regioes_dia_fixo` mockado: não é chamado com o serviço pausado), `test_reentrega_do_pausado_fica_fora`, `test_pool_segue_sem_tabela`; no planejamento, `test_pool_marca_pausado` e `test_roteirizar_selecionados_pula_pausado` (mensagem diz quantos foram pulados).
- [ ] **Step 2–4:** RED → GREEN (rodar `roteirizacao` e `painel_agentes` em comandos separados).
- [ ] **Step 5: Commit** `Roteirização: pedido em espera a pedido do cliente não entra em rota`.

### Task 3: Executor liga o "em espera" (sem Stokki)

**Files:** Modify `portal_cliente/solicitacoes_execucao.py`, `portal_cliente/test_solicitacoes_execucao.py`, `portal_cliente/envio_pedidos.py` (constantes `STATUS_EM_ESPERA_CLIENTE = "EM_ESPERA_CLIENTE"`, `STATUS_RETOMANDO = "RETOMANDO"`, rótulos "Em espera (a seu pedido)" e "Retomando"); Delete `stokki/pausar.py`, `stokki/test_pausar.py`; Modify `MAPA_DO_SISTEMA.txt` (tirar o pausar).

- `EM_ESPERA_HABILITADO = True` (apagar `MSG_EM_ESPERA_DESLIGADO` se ficar sem uso); `_em_espera` só tira da rota (sai `deps.pausar_stokki`, o campo `pausar_stokki` de `Dependencias` e a função em `dependencias_reais`); `"em_espera"` sai de `_USAM_STOKKI` (não pega mais a trava da Stokki); docstring do módulo atualizada; `AVISO_REROTEIRIZACAO` vira "Pedido segurado até o cliente retomar pelo portal."; resposta ao cliente "Pedido retirado da rota e mantido em espera até você retomar pelo portal."; depois do sucesso (antes de `concluir_solicitacao`): `pedidos_pausados_cliente.abrir(...)` e `UPDATE portal_envios SET status = EM_ESPERA_CLIENTE`.
- [ ] **Step 1: Testes que falham:** `test_em_espera_abre_pausa_e_muda_status`, `test_em_espera_nao_toca_na_stokki` (abrir_stokki nunca chamado), `test_em_espera_falhou_nao_abre_pausa`; ajustar o teste do "desligado".
- [ ] **Step 2–4:** `py -3.11 -m unittest portal_cliente.test_solicitacoes_execucao` RED → GREEN; `git grep -n "stokki.pausar\|pausar_pedido"` vazio.
- [ ] **Step 5: Commit** `Portal: executar em espera só tira da rota e grava a pausa (nada na Stokki)`.

### Task 4: Portal — ação Retomar

**Files:** Modify `portal_cliente/envio_pedidos.py` (`_linha` flags ~:1401-1437; `aplicar_acao` tipo novo `"retomar"`), `portal_cliente/app.py` (`api_envios_acao` aceita retomar), `portal_cliente/templates/_envios.html` (botão + modal), testes do portal do envio.

**Interfaces — Produces:**
- `_linha`: `pode_retomar = status == EM_ESPERA_CLIENTE`; em `EM_ESPERA_CLIENTE`: `pode_cancelar = True`, `pode_reagendar = False`, `pode_em_espera = False`.
- `aplicar_acao(..., "retomar", dados={"modo": "agendamento"|"proxima_rota", "data", "hora_inicio", "hora_fim"})`: valida como o reagendar quando `modo == "agendamento"`; grava `status = RETOMANDO` e colunas novas `retomar_modo`, `retomar_data`, `retomar_hora_inicio`, `retomar_hora_fim` (via `_garantir_colunas`); devolve `{"aplicado": False, "em_andamento": True, "mensagem": "Retomando… você verá aqui em instantes."}`.
- Cancelar em `EM_ESPERA_CLIENTE` segue o caminho do pedido já criado (→ `CANCELANDO`).
- Modal: "Próxima rota da sua região" / "Agendar data e horário" (mesmos campos do reagendar). Textos via `textContent`.
- [ ] **Step 1: Testes que falham:** `test_linha_em_espera_oferece_retomar_e_cancelar`, `test_retomar_proxima_rota_vira_retomando`, `test_retomar_agendamento_valida_data`, `test_cancelar_em_espera_vira_cancelando`, `test_retomar_fora_de_em_espera_recusa`.
- [ ] **Step 2–4:** RED → GREEN.
- [ ] **Step 5: Commit** `Portal: cliente retoma pedido em espera (próxima rota ou agendamento)`.

### Task 5: Worker da retomada

**Files:** Create `portal_cliente/retomada.py`, `portal_cliente/test_retomada.py`; Modify `portal_cliente/enviar_stokki.py` (`ciclo` chama `processar_retomadas_do_ciclo` ao lado dos cancelamentos; `resetar_orfaos` devolve `RETOMANDO` > 30 min para `EM_ESPERA_CLIENTE` e avisa), `portal_cliente/cancelamento.py` (cancelamento concluído → `pedidos_pausados_cliente.fechar(..., "cancelado")`).

**Interfaces — Produces:** `processar_retomadas(conn, config, aplicar_data_vuupt, proxima_data, avisar, agora=None) -> {"retomados","falhas"}` (injetáveis), por envio `RETOMANDO`:
1. `agendamento` → `aplicar_data_vuupt(service_id, data, ini, fim)` pelo caminho do cliente (o mesmo do executor `_reagendar`: PUT + `registrar_agendamento_pedido`); `proxima_rota` → `proxima_data(servico)` (dia fixo da região a partir de amanhã; `None` = não mexe na Vuupt) e aplica se houver.
2. `fechar(..., "retomado")`, envio → `CRIADO`.
Falha no 1 → volta `EM_ESPERA_CLIENTE` + `avisar`; repetir é seguro.
`processar_retomadas_do_ciclo(conn, config, simular=False)` em `enviar_stokki.py` — sem trava da Stokki (não usa Stokki).
- [ ] **Step 1: Testes que falham:** `test_retomar_proxima_rota_aplica_dia_fixo_e_fecha`, `test_retomar_agendamento_aplica_data_e_fecha`, `test_regiao_sem_dia_fixo_nao_mexe_na_vuupt`, `test_vuupt_falha_volta_em_espera_e_avisa`, `test_retomar_de_novo_e_idempotente`, `test_orfao_retomando_volta`, `test_cancelamento_concluido_fecha_pausa`.
- [ ] **Step 2–4:** RED → GREEN (`portal_cliente.test_retomada portal_cliente.test_cancelamento`).
- [ ] **Step 5: Commit** `Portal: worker retoma o pedido em espera (data na Vuupt) e fecha a pausa` (+ MAPA).

### Task 6: Deploy e prova com o Hugo (só quando ele pedir)

- [ ] Deploy (`vps_ops.py deploy --restart painel-agentes portal-cliente portal-cliente-envios`; lote pega sozinho). Módulo público (`portal_cliente/`): review de segurança antes.
- [ ] Prova acompanhada num pedido de teste: executar "em espera" na tela → rodada de roteirização em `--modo-teste` mostra o pedido pulado → cliente retoma (agendamento) pelo portal de teste → data na Vuupt, linha fechada, envio `CRIADO`.
