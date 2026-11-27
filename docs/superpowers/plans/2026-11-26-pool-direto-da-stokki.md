# Pool direto da Stokki Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O pedido nasce no núcleo direto da Stokki e entra no pool sem serviço na Vuupt; o serviço só é criado na Vuupt ao enviar a rota.

**Architecture:** `nucleo_pedidos` ganha `id_pool` estável; o pedido sem serviço entra no pool com id sintético `-id_pool`, que atravessa rascunho/planejamento sem mudar schema. `nucleo/materializar.py` cria o serviço na Vuupt no envio e `adotar_servico` troca o id negativo pelo real nas tabelas do rascunho. Tudo atrás de `planejamento.fonte_pool: stokki` (voltar atrás = trocar a chave).

**Tech Stack:** Python 3.11 (`py -3.11`), SQLite (`dados/dados.db`), Flask (painel), `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-26-pool-direto-da-stokki-design.md` (inclui a revisão do Fable com os itens 1-18 e as decisões do Hugo: materializar **só ao enviar**; portal e ações do pool **entram no mesmo corte**).

## Global Constraints

- Testes: `py -3.11 -m unittest <modulo>`; nunca misturar `roteirizacao`/`nucleo` com `painel_agentes` no mesmo comando (testes de `painel_agentes` rodam de dentro de `painel_agentes/`).
- Ramo/worktree próprio, sem commit de arquivos alheios (`git add` só dos arquivos da tarefa, nunca `-A`); commit e push só quando o Hugo pedir.
- Chave desligada (`fonte_pool` ≠ `stokki`) = comportamento idêntico ao de hoje em TODOS os módulos tocados.
- Comentários e logs sem acento nos arquivos que já seguem esse padrão. Mensagens de rejeição sempre com o motivo real.
- Pedido nunca vai a rota com id negativo; nunca dois serviços na Vuupt para o mesmo código.
- Envio real a clientes/motoristas continua desligado por padrão; testar com `--modo-teste` e em homologação (`homologar`), nunca script à mão em `/opt/stokki-homolog`.
- Atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit de cada arquivo/rotina novo.

## Review Focus

1. Clique duplo / dois processos enviando o mesmo rascunho: um único serviço por código na Vuupt (Task 5).
2. Falha no meio do lote (materializou 40 de 50): rota não sai com negativos, pedidos que falharam saem do rascunho com motivo (Task 5).
3. Reentrega `-R` e serviço mais recente `done`/`canceled` (`pulado_atribuido` sem id): materialização aborta com motivo, não cria duplicado (Task 5).
4. Sync da Vuupt grava `vuupt_service_id` com o rascunho ainda negativo: sem pedido duplicado no pool nem no rascunho (Task 3).
5. Pedido cancelado/entregue sem serviço: some do pool e nunca é materializado (Task 4).

---

### Task 1: Coluna `id_pool` e id sintético

**Files:**
- Modify: `nucleo/banco.py` (lista `_COLUNAS_PEDIDOS_NOVAS` ~:492 + migração/backfill + índice único)
- Modify: `nucleo/pedidos.py` (`upsert_pedido`: pedido novo recebe `id_pool`)
- Test: `nucleo/test_id_pool.py`

**Interfaces:**
- Produces: coluna `nucleo_pedidos.id_pool INTEGER` única e nunca reutilizada (contador em tabela `nucleo_contadores` ou `MAX+1` dentro da transação); `pedidos.id_sintetico(pedido) -> int` (= `-id_pool`); `pedidos.codigo_por_id_sintetico(conn, id_negativo) -> str | None`.

- [ ] **Step 1: Teste** `test_backfill_dá_id_pool_a_todos_e_é_único`, `test_pedido_novo_ganha_id_pool_crescente`, `test_id_pool_sobrevive_a_vacuum_into` (copiar com `VACUUM INTO` e comparar `codigo`→`id_pool`), `test_id_sintetico_ida_e_volta`.
- [ ] **Step 2:** Rodar `py -3.11 -m unittest nucleo.test_id_pool -v` → FAIL.
- [ ] **Step 3:** Implementar a migração aditiva (padrão de `_COLUNAS_PEDIDOS_NOVAS`) com backfill ordenado por `criado_em, codigo` e `CREATE UNIQUE INDEX`; `upsert_pedido` atribui `id_pool` só no INSERT.
- [ ] **Step 4:** Rodar → PASS; rodar `py -3.11 -m unittest nucleo.test_espelho_fiel nucleo.test_pool` (sem regressão).
- [ ] **Step 5:** Commit `Nucleo: id_pool estavel em nucleo_pedidos (base do pool sem servico na Vuupt)`.

### Task 2: Chave `fonte_pool: stokki` e pool com pedido sem serviço

**Files:**
- Modify: `nucleo/pool.py` (`FONTE_STOKKI`, `listar_pool`, `pedido_para_servico`, `listar_pool_not_assigned`, `fonte_pool`)
- Test: `nucleo/test_pool.py` (casos novos)

**Interfaces:**
- Consumes: `pedidos.id_sintetico` (Task 1).
- Produces: com fonte `stokki`, `listar_pool_not_assigned(config, vuupt)` nunca chama `vuupt`; item sem serviço traz `id = -id_pool`, `_sem_servico_vuupt = True`; item com serviço traz o id real. Valor de `fonte_pool` desconhecido é logado em WARNING (um por processo) em vez de cair calado na Vuupt.

- [ ] **Step 1: Teste** `test_stokki_inclui_pedido_sem_servico_com_id_negativo`, `test_stokki_nao_chama_vuupt` (mock do client com `assert_not_called`), `test_pedido_com_servico_mantem_id_real`, `test_fonte_desconhecida_loga_warning`, `test_vuupt_e_nucleo_seguem_iguais`.
- [ ] **Step 2:** `py -3.11 -m unittest nucleo.test_pool -v` → FAIL.
- [ ] **Step 3:** Remover o filtro `vuupt_service_id IS NOT NULL` só quando fonte = `stokki` (`listar_pool(conn, incluir_sem_servico: bool = False)`); `pedido_para_servico` usa o id sintético quando `vuupt_service_id` é nulo.
- [ ] **Step 4:** → PASS. **Step 5:** Commit `Pool: fonte stokki lista pedido sem servico com id sintetico`.

### Task 3: `adotar_servico` (troca de identidade em um lugar só)

**Files:**
- Create: `nucleo/adocao_servico.py`
- Modify: `nucleo/sincronizar_servicos_vuupt.py` (`:196`, `:253`), `pipeline.py` (fila freshhub, `registrar_importacao` com id novo), `nucleo/pedidos.py` (`registrar_importacao`)
- Test: `nucleo/test_adocao_servico.py`

**Interfaces:**
- Consumes: `id_pool` (Task 1), tabelas `rascunhos_parada`, `rascunhos_plano_original` (PK/UNIQUE `(rascunho_id, service_id)`, `painel_agentes/rascunhos_rota.py:95,116,125`).
- Produces: `adotar_servico(conn, codigo: str, service_id: int) -> int` (devolve quantas linhas de rascunho foram reescritas). Mesma transação: grava `vuupt_service_id` no núcleo e troca `-id_pool` → `service_id` nas duas tabelas de rascunho; se já existe a linha real no mesmo rascunho, remove a duplicada em vez de estourar o UNIQUE. Todo caminho que grava `vuupt_service_id` num pedido que não tinha chama esta função.

- [ ] **Step 1: Teste** `test_reescreve_rascunho_e_plano_original`, `test_unique_nao_estoura_se_id_real_ja_no_rascunho`, `test_idempotente`, `test_sync_que_traz_servico_nao_duplica_no_pool`, `test_pedido_sem_rascunho_so_grava_id`.
- [ ] **Step 2:** rodar → FAIL. **Step 3:** implementar e religar os chamadores. **Step 4:** PASS + `py -3.11 -m unittest nucleo.test_sincronizar_servicos`.
- [ ] **Step 5:** Commit `Nucleo: adotar_servico troca o id sintetico pelo real em um lugar so`.

### Task 4: Pipeline grava no núcleo sem criar serviço; fechar pedido sem serviço

**Files:**
- Modify: `pipeline.py` (`processar_pedido` ~:624-1111; `servico_existente` :692; `atender_fila_freshhub` :1177-1200; retorno de cancelado/retirada :715-783)
- Modify: `roteirizacao/cancelar_cancelados_stokki.py` (`:97-99`), `roteirizacao/conferir_pool.py` (`:126-216`)
- Modify: `roteirizacao/datas_automaticas.py` e `roteirizacao/regioes_dia_fixo.py` (gravar no núcleo quando não há serviço)
- Test: `test_pipeline_sem_servico.py` (raiz, junto dos testes do pipeline existentes), `roteirizacao/test_conferir_pool.py`

**Interfaces:**
- Consumes: `pedido_para_servico` (Task 2), `adotar_servico` (Task 3).
- Produces: com a chave ligada, `processar_pedido` monta o mesmo payload, **não** chama `criar_ou_atualizar_servico`, grava `registrar_importacao(payload, None, "nucleo_sem_servico")` e devolve `vuupt_service_id=None` sem erro; `servico_existente` vem da linha do núcleo no formato de `pedido_para_servico`; data automática e aviso de dia fixo ficam gravados no núcleo (não repetem na rodada seguinte). `fila-freshhub`: sem serviço, materializa (Task 5) em vez de devolver erro.

- [ ] **Step 1: Teste** `test_chave_ligada_nao_chama_vuupt`, `test_chave_desligada_igual_a_hoje`, `test_segunda_rodada_nao_recalcula_data_automatica`, `test_aviso_dia_fixo_nao_repete`, `test_cancelado_na_stokki_fecha_pedido_sem_servico`, `test_retirada_sem_servico_fecha_no_nucleo`, `test_conferir_pool_entregue_sem_servico_cancela_local_e_nunca_materializa`.
- [ ] **Step 2:** rodar → FAIL. **Step 3:** implementar por trás de `pool.fonte_pool(config) == "stokki"`. **Step 4:** PASS + testes existentes do pipeline e `roteirizacao.test_*` relacionados.
- [ ] **Step 5:** Commit `Pipeline: com fonte stokki grava so no nucleo; fecha pedido sem servico`.

### Task 5: Materialização no envio, com trava e idempotência

**Files:**
- Create: `nucleo/materializar.py`
- Modify: `painel_agentes/rascunhos_rota.py` (`enviar_rascunho` :1378-1476, adição de parada em rota enviada :1948-1956), `roteirizacao/incrementar_rotas.py` (:637, :971, :1018), `roteirizacao/criar_rotas_diarias.py` (:1179, :1223), `painel_agentes/planejamento_rotas.py` (laço "enviar todas" :305-313)
- Modify: `nucleo/banco.py` (tabela `nucleo_materializacao(codigo PRIMARY KEY, iniciado_em, processo)` para a trava)
- Test: `nucleo/test_materializar.py`, `painel_agentes/test_rascunhos_rota.py` (casos novos)

**Interfaces:**
- Consumes: `vuupt_client.criar_ou_atualizar_servico`, resolução de `customer_id` (`vuupt_client.py:513-570`), `adotar_servico` (Task 3), payload a partir das colunas do núcleo (fonte única; não ler `dados_json`).
- Produces: `materializar_servicos(itens: list[dict], vuupt, config) -> ResultadoMaterializacao` com `ok: dict[codigo, service_id]` e `falhas: dict[codigo, str]` (motivo legível). Regras: trava por código no banco (quem perde a corrida espera e reaproveita o id); busca por `code` antes de criar; `pulado_atribuido`/`done`/`canceled`/reentrega `-R` sem id → falha com motivo, sem criar; rodar `registrar_data_automatica` e dia fixo quando for o caso; chamado no início de `enviar_rascunho`, `incrementar_rotas`, adição de parada em rota enviada e criação direta de rota. Pedido com falha sai do rascunho com motivo (`remover_parada`) e a rota segue só com os que têm serviço; nenhuma rota com id negativo.

- [ ] **Step 1: Teste** `test_cria_servico_e_adota_id`, `test_clique_duplo_cria_um_servico_so` (duas threads), `test_falha_no_meio_envia_so_os_materializados`, `test_pulado_atribuido_falha_com_motivo`, `test_reentrega_R_sem_id_falha_com_motivo`, `test_enviar_rascunho_nunca_chama_criar_rota_com_id_negativo`, `test_rascunho_com_ids_reais_nao_chama_materializar` (compat).
- [ ] **Step 2:** rodar → FAIL. **Step 3:** implementar; o laço "enviar todas" mostra progresso/erro por rascunho (a resposta já devolve `codigos_nao_prontos`; acrescentar `codigos_sem_servico: {codigo: motivo}`). **Step 4:** PASS (`nucleo.test_materializar`; depois, de dentro de `painel_agentes/`, `py -3.11 -m unittest test_rascunhos_rota`).
- [ ] **Step 5:** Commit `Envio: materializa servico na Vuupt so ao enviar (trava por codigo, motivo por pedido)`.

### Task 6: Ações do Planejamento em pedido sem serviço

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py` (`:2043, 2081, 2132, 2201, 2249, 2309-2315, 2420`), `painel_agentes/acoes_pedidos.py` (`:103, 114, 144, 212`), `roteirizacao/cancelar_servico.py` (`:56`), `painel_agentes/rascunhos_rota.py` (`:1948`)
- Test: `painel_agentes/test_planejamento_sem_servico.py`

**Interfaces:**
- Consumes: `pool.eh_sem_servico(service_id: int) -> bool` (`service_id < 0`, criar em Task 2), `codigo_por_id_sintetico` (Task 1).
- Produces: para id negativo, cancelar = marcar CANCELADO no núcleo; reagendar/alterar endereço/janela = gravar nas colunas do núcleo (valem na materialização e **não** são sobrescritas pelo pipeline: usar a proteção de `pedidos.py:229-231`); mover/reordenar no rascunho não toca a Vuupt. Nenhum botão do pool devolve erro por falta de serviço.

- [ ] **Step 1: Teste** um por ação: `test_cancelar_sem_servico_marca_cancelado_local`, `test_reagendar_sem_servico_grava_no_nucleo`, `test_alterar_endereco_sem_servico_grava_no_nucleo`, `test_pipeline_nao_sobrescreve_edicao_local`, `test_acao_com_servico_continua_chamando_vuupt`.
- [ ] **Step 2-4:** FAIL → implementar → PASS. **Step 5:** Commit `Planejamento: acoes do pool funcionam em pedido sem servico`.

### Task 7: Portal e consumidores diretos da Vuupt

**Files:**
- Modify: `portal_cliente/dados_cliente.py` (`:416`), `portal_cliente/cancelamento.py` (`:154`), `portal_cliente/retomada.py` (`:97`), `portal_cliente/solicitacoes_execucao.py` (`:132-214`), `roteirizacao/reagendar_feriado.py` (`:144`), `roteirizacao/torre_controle.py` (`:879`), `roteirizacao/notificar_area_nao_atendida.py` (`:200`, chave por `codigo`, não por service_id), `nucleo/sincronizar_servicos_vuupt.py` (não marcar `PEDIDO_SUMIU_DA_VUUPT` quem nunca teve serviço; `vincular_sem_service_id` com teto de chamadas por rodada)
- Test: `portal_cliente/test_*.py` (casos novos), `roteirizacao/test_*.py` (casos novos)

**Interfaces:**
- Consumes: `pool.listar_pool(conn, incluir_sem_servico=True)`.
- Produces: com a chave ligada o portal lista o pedido sem serviço em "aguardando"; cancelamento/retomada/solicitação de execução tratam sem serviço como núcleo-local; o aviso de área não atendida não repete quando o id troca.

- [ ] **Step 1: Teste** `test_portal_lista_pedido_sem_servico`, `test_cancelamento_portal_sem_servico_local`, `test_aviso_area_nao_repete_ao_adotar_servico`, `test_sync_nao_marca_sumiu_pedido_sem_servico`.
- [ ] **Step 2-4:** FAIL → implementar → PASS (rodar cada pacote em comando separado). **Step 5:** Commit `Portal e rotinas: pedido sem servico na Vuupt tratado pelo nucleo`.

### Task 8: Sombra, homologação, portão e docs

**Files:**
- Modify: `nucleo/comparar_pool.py` (`--fonte stokki`, coluna `fonte` já existe), `MAPA_DO_SISTEMA.txt` (seção Pool + novos arquivos), `infra/*.timer` só se criar timer
- Test: `nucleo/test_comparar_pool.py` (caso `stokki`)

**Interfaces:**
- Consumes: Tasks 2-7.
- Produces: comparação pool-stokki × Vuupt registrada em `nucleo_reconciliacoes_pool`; roteiro de prova.

- [ ] **Step 1: Teste** `test_comparar_fonte_stokki_marca_so_diferencas_reais` (pedido sem serviço ainda não é divergência).
- [ ] **Step 2-4:** implementar; subir o ramo com `homologar` (chave `fonte_pool: stokki` só lá) e provar de ponta a ponta com `--modo-teste` e pedidos de teste: pool → rascunho → enviar → serviço criado na Vuupt (só leitura confirma) → rota → baixa. Portão para ligar em produção (decisão do Hugo): 3 dias úteis em 0 na sombra **e** a prova de homologação acima.
- [ ] **Step 5:** Commit `Pool direto da Stokki: sombra, mapa e roteiro de prova`. Deploy só quando o Hugo pedir (skill `deploy-vps`; reiniciar `painel-agentes` e `portal-cliente`, pois `nucleo/pool.py` é importado pelos dois).
