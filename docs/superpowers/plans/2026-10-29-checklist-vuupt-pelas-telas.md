# Checklist da Vuupt pelas telas — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** As três telas que concluem entrega pelo painel (`/baixa-sem-app`, "Marcar como entregue" da Torre e do Planejamento, inclusive em massa) preenchem o checklist "Entregas com Sucesso" da Vuupt com as fotos de canhoto e nota de retorno enviadas; sem foto do canhoto, só concluem com justificativa de lista fixa.

**Architecture:** Módulo novo na raiz `vuupt_checklist.py` fala com os endpoints não documentados de checklist (criar cabeçalho, mandar foto por campo, finalizar, conferir). `VuuptClient.concluir_como_agente` ganha fotos opcionais e chama o módulo entre o check-in e o check-out. A lista de justificativas fica em `nucleo/justificativas_sem_canhoto.py`. Torre/Planejamento passam a mandar multipart para `/api/torre/marcar-entregue`; a `/baixa-sem-app` guarda as fotos do lote em disco e a thread do lote as usa.

**Tech Stack:** Python 3.11, requests, Pillow (+ pillow-heif se instalado), Flask, sqlite3, unittest.

**Spec:** `docs/superpowers/specs/2026-10-29-checklist-vuupt-pelas-telas-design.md` (está untracked na raiz `C:\agente_stokki_eventos\docs\...`; copiar para o worktree no commit da Task 1). Memória com os detalhes do protocolo: `reference_vuupt_api_publica_stoplight`.

## Global Constraints

- Português em código, comentários e commits (`Área: o que mudou e por quê`); arquivos que já são sem acento continuam sem acento. `py -3.11`; unittest; testes do painel de dentro de `painel_agentes/` com o runner de config falsa (`C:\Users\HUGOMA~1\AppData\Local\Temp\claude\c--agente-stokki-eventos\faed8f78-8be3-4d0b-a20e-afd6429fbca2\scratchpad\roda_painel.py`), nunca misturados com outras suítes. Nunca ler `config.yaml`. Saída de unittest em arquivo (pipe pra `tail` trava o shell).
- Worktree `C:\agente_stokki_eventos\.claude\worktrees\checklist-vuupt` (ramo `checklist-vuupt`); `git add` só dos arquivos da tarefa; commits locais; push/deploy só quando o Hugo pedir.
- Testes nunca chamam a Vuupt real (sessão requests falsa / mocks) nem `dados/dados.db`.
- Valores fixos (spec §2 e §4.2): `FORM_ENTREGA = 1531`, `REGRA_ENTREGA = 1526`, `GATILHO = "finished_service"`, `CAMPO_CANHOTO = 15086`, `CAMPO_NOTA_RETORNO = 15349`, `file_source = "gallery"`, `images_quantity` = nº exato de fotos, arquivo na parte multipart `fields[0][value]`, `PUT /checklists/{id}/finish`. Base de requisição = a mesma do `VuuptClient` (`API_BASE_URL`) com o header `Content-Type: None` por requisição (mesmo truque de `_acao_servico`).
- Coordenadas do checklist: `latitude`/`longitude` do serviço; sem elas, as da base `BASE_LAT = -23.497015`, `BASE_LNG = -46.660775`. `gps_date_time` e `filled_at` em UTC `"%Y-%m-%d %H:%M:%S"`.
- Só serviço `type == "delivery"` recebe checklist; outro tipo com foto → recusa com mensagem.
- Justificativas (códigos → rótulos, nessa ordem): `canhoto_depois` "Canhoto chega depois (motorista vai trazer)", `nao_assinou` "Cliente não assinou / recusou assinar", `confirmado_contato` "Entrega confirmada por telefone ou WhatsApp com o cliente", `sem_bateria` "Motorista sem celular ou sem bateria na hora da entrega", `foto_perdida` "Foto do canhoto perdida ou ilegível", `acordo_embarcador` "Entrega sem canhoto por acordo com o embarcador", `outro` "Outro". "Outro" exige texto de 5 a 200 caracteres.
- Regra: com foto do canhoto → justificativa ignorada; sem foto do canhoto → justificativa obrigatória. Nota de retorno sempre opcional (pode ir sozinha, com justificativa).
- Falha do checklist = pedido **não** concluído (check-out não roda) e erro visível por pedido. Cópia local do canhoto continua antes do fechamento (spec §4.3).
- Pedido já `done`: não mexe na Vuupt; grava cópia local do canhoto se veio; resposta avisa "já estava concluído; a foto não foi para o checklist".
- Atualizar `MAPA_DO_SISTEMA.txt` no commit que cria arquivo.

## Review Focus

1. **Foto que "sobe" com 200 mas não grava** (nome de parte errado, Vuupt mudou) → tem que virar erro e não concluir — Task 1 (`test_foto_sem_file_url_na_releitura_falha`).
2. **Retentativa depois de falha no meio** (cabeçalho criado, foto ou finish falhou) não cria segundo checklist e completa o existente — Task 1 (`test_reaproveita_checklist_existente_do_servico`).
3. **Em massa: a mesma foto em N pedidos** cria um checklist por serviço, cada um no `entity_id` certo — Task 4 (`test_lote_mesma_foto_um_checklist_por_pedido` no front: um POST por pedido com os mesmos arquivos) e Task 3 (`test_checklist_no_servico_do_request`).
4. **Sem foto e sem justificativa** nunca conclui (nem pela API direta) — Task 3 (`test_sem_foto_sem_justificativa_recusa`) e Task 5 (`test_item_entregue_sem_canhoto_sem_justificativa_400`).
5. **HEIC/PNG do celular** viram JPEG antes de subir; PDF não vai pro checklist (só cópia local, com aviso) — Task 1 (`test_para_jpeg_png_e_heic`) e Task 3 (`test_pdf_so_copia_local_e_avisa`).

---

## Estrutura de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `vuupt_checklist.py` (+ `test_vuupt_checklist.py`), raiz | protocolo do checklist de entrega |
| `vuupt_client.py` | `concluir_como_agente(..., fotos_checklist=None, servico=None)` |
| `nucleo/justificativas_sem_canhoto.py` (+ teste) | lista fixa e validação |
| `painel_agentes/torre_controle.py`, `painel_agentes/painel_agentes.py` (`/api/torre/marcar-entregue`) | regra e API Torre/Planejamento |
| `painel_agentes/templates/torre_controle.html`, `painel_agentes/templates/planejamento_rotas.html` | modais com fotos e justificativa |
| `nucleo/baixa_sem_app.py`, `painel_agentes/painel_agentes.py` (`/api/baixa-sem-app`), `painel_agentes/templates/baixa_sem_app.html` | baixa sem app |

---

### Task 1: Protocolo do checklist + `concluir_como_agente` com fotos

**Files:** Create `vuupt_checklist.py`, `test_vuupt_checklist.py`; Modify `vuupt_client.py` (`concluir_como_agente`), `MAPA_DO_SISTEMA.txt`; add the spec file to the worktree (`docs/superpowers/specs/2026-10-29-checklist-vuupt-pelas-telas-design.md`, copied from the root checkout) and this plan.

**Interfaces — Produces:**
- Constantes da seção Global Constraints; `class ChecklistNaoGravado(Exception)`.
- `para_jpeg(conteudo: bytes) -> bytes` — JPEG/PNG/HEIC → JPEG (HEIC via `pillow_heif` se instalado, como `nucleo/canhotos_manuais.para_pdf`); PDF ou não-imagem → `ValueError("Arquivo não é foto")`.
- `checklist_entrega_do_servico(vuupt, service_id: int) -> dict | None` — `GET /checklists` com `filter[0][field]=entity_id`, `filter[0][operator]=eq`, `filter[0][value]=service_id`, `include=fields`; devolve o de `checklist_form_id == 1531` (o mais recente) ou None.
- `preencher_checklist_entrega(vuupt, servico: dict, fotos: dict[int, bytes]) -> int` — `fotos` = {campo_id: jpeg}. Reaproveita o existente (se `images_quantity` dele ≠ len(fotos), cria novo); senão `POST /checklists` (form, valores fixos). Para cada campo: `POST /checklists/{id}` multipart com `checklist_form_id`, `fields[0][id]`, `fields[0][content_uuid]` (uuid4), `fields[0][file_source]`, arquivo em `fields[0][value]`. `PUT /checklists/{id}/finish` (409 por contagem = `ChecklistNaoGravado`). Relê com `checklist_entrega_do_servico` e exige `file_url` em cada campo enviado; senão `ChecklistNaoGravado`. Devolve o id.
- `VuuptClient.concluir_como_agente(service_id, sucesso=True, failed_reason_id=None, status_atual="", fotos_checklist: dict[int, bytes] | None = None, servico: dict | None = None) -> int | None` — se `fotos_checklist`, chama `preencher_checklist_entrega` **depois do check-in e antes do check-out** (busca o serviço se `servico` for None); devolve o id do checklist ou None. Exceção do checklist propaga (check-out não roda). Retiradas/Lalamove seguem sem fotos.

- [ ] **Step 1: Testes que falham** (sessão requests falsa que registra chamadas e devolve respostas montadas): `test_cria_cabecalho_com_valores_fixos_e_utc`, `test_foto_vai_na_parte_fields_value`, `test_finish_e_put`, `test_foto_sem_file_url_na_releitura_falha`, `test_reaproveita_checklist_existente_do_servico`, `test_sem_coordenada_usa_base`, `test_para_jpeg_png_e_heic` (HEIC só se `pillow_heif` importar; senão skip), `test_pdf_nao_e_foto`, `test_concluir_com_fotos_checklist_entre_checkin_e_checkout` (ordem das chamadas), `test_concluir_falha_no_checklist_nao_faz_checkout`, `test_concluir_sem_fotos_igual_a_hoje`.
- [ ] **Step 2–4:** `py -3.11 -m unittest test_vuupt_checklist test_vuupt_client_buscar_servico_por_id` RED → GREEN.
- [ ] **Step 5: Commit** `Vuupt: checklist de entrega com foto por API (canhoto e nota de retorno) no concluir_como_agente` (+ spec, plano, MAPA).

### Task 2: Lista de justificativas

**Files:** Create `nucleo/justificativas_sem_canhoto.py`, `nucleo/test_justificativas_sem_canhoto.py`.

**Interfaces — Produces:** `JUSTIFICATIVAS: dict[str, str]` (ordem e textos da Global Constraints); `validar(codigo: str | None, texto_outro: str | None) -> tuple[str, str]` (devolve `(codigo, texto)` com texto `""` fora do "outro"; `ValueError` com mensagem pra tela se código inválido/vazio ou "outro" fora de 5–200); `rotulo(codigo) -> str`.

- [ ] **Step 1: Testes:** `test_lista_na_ordem`, `test_codigo_invalido`, `test_outro_exige_texto`, `test_outro_limites_5_200`, `test_texto_ignorado_fora_do_outro`.
- [ ] **Step 2–4:** `py -3.11 -m unittest nucleo.test_justificativas_sem_canhoto` RED → GREEN.
- [ ] **Step 5: Commit** `Painel: lista fixa de justificativas de entrega sem canhoto`.

### Task 3: Torre/Planejamento — regra e API

**Files:** Modify `painel_agentes/torre_controle.py` (`marcar_entregue_manual`, `_registrar_entrega_manual`, tabela `torre_entregas_manuais`), `painel_agentes/painel_agentes.py` (`api_torre_marcar_entregue`), `painel_agentes/test_torre_marcar_entregue.py` (reescrever os casos de `observacao`).

**Interfaces — Consumes:** Task 1 (`para_jpeg`, `concluir_como_agente(... fotos_checklist, servico)`, constantes de campo), Task 2 (`validar`, `rotulo`), `nucleo.canhotos_manuais.para_pdf/salvar`.
**Produces:**
- `marcar_entregue_manual(service_id: int, usuario: str, foto_canhoto: bytes | None, foto_nota_retorno: bytes | None, justificativa: str | None, texto_outro: str | None) -> dict` → `{"ok", "codigo", "ja_estava"?, "checklist_id"?, "aviso"?, "erro"?}`. Ordem: valida (sem canhoto → `validar`; erro = `ok False`); lê o serviço; recusa canceled/failed/sem motorista como hoje; foto e `type != delivery` → erro; se veio canhoto: `canhotos_manuais.salvar(... para_pdf(...) ...)` antes de tudo; `done` → `ja_estava` + aviso "já estava concluído; a foto não foi para o checklist" (sem tocar na Vuupt); canhoto em PDF → só cópia local, sem checklist, `aviso`; senão `concluir_como_agente` com `fotos_checklist` {15086: jpeg, 15349: jpeg} dos que vieram. Falha → `ok False` com "Checklist não gravado na VUUPT: ..." e nada concluído.
- `torre_entregas_manuais`: colunas novas `justificativa TEXT`, `texto_outro TEXT`, `checklist_id INTEGER`, `fotos INTEGER` (criadas por `ALTER TABLE` idempotente no mesmo lugar que cria a tabela); `observacao` passa a guardar o rótulo da justificativa (ou "com foto do canhoto").
- API: multipart com `service_id`, `foto_canhoto`, `foto_nota_retorno` (arquivos opcionais, 15 MB cada, recusa 413 como a baixa), `justificativa`, `texto_outro`. JSON antigo sai.
- [ ] **Step 1: Testes** (Vuupt e canhotos_manuais dublados): `test_com_canhoto_preenche_checklist_e_conclui`, `test_com_canhoto_e_nota_duas_fotos`, `test_sem_foto_sem_justificativa_recusa`, `test_sem_foto_com_justificativa_conclui_sem_checklist`, `test_outro_sem_texto_recusa`, `test_checklist_falha_nao_conclui_e_mostra_erro`, `test_ja_done_nao_toca_vuupt_e_avisa`, `test_pdf_so_copia_local_e_avisa`, `test_tipo_nao_delivery_com_foto_recusa`, `test_checklist_no_servico_do_request`, `test_registro_grava_justificativa_e_checklist_id`; API: `test_api_multipart_ok`, `test_api_arquivo_grande_413`, `test_api_exige_mesma_origem` (como os testes atuais).
- [ ] **Step 2–4:** de dentro de `painel_agentes/`: `py -3.11 <runner> test_torre_marcar_entregue` RED → GREEN.
- [ ] **Step 5: Commit** `Torre: marcar entregue com foto no checklist da VUUPT ou justificativa da lista`.

### Task 4: Torre/Planejamento — telas

**Files:** Modify `painel_agentes/templates/torre_controle.html` (modal `#modal-entregue` ~:644 e JS ~:1270-1300), `painel_agentes/templates/planejamento_rotas.html` (modal ~:2065 e `abrirModalEntregueLote`/`confirmarEntregue` ~:6112-6160), `painel_agentes/test_torre_marcar_entregue_tela.py`.

- Modal: "Foto do canhoto" (`input type=file accept="image/*,application/pdf" capture="environment"`), "Foto da nota de retorno (opcional)" (`accept="image/*"`), `select` de justificativa (opções de `JUSTIFICATIVAS` passadas pelo servidor ao template, não duplicadas no HTML) visível/obrigatório só sem foto do canhoto, campo "Descreva" visível só em "Outro". Subtítulo novo: "Fecha na VUUPT agora. Com foto, ela vai para o checklist da VUUPT."
- Envio: `FormData` por pedido (`service_id`, os arquivos escolhidos, `justificativa`, `texto_outro`) para a URL da API via `url_for` (Torre já usa `URL_TORRE_MARCAR_ENTREGUE`; no Planejamento trocar o caminho fixo `/api/torre/marcar-entregue` por `url_for`). Em massa: mesmos arquivos e justificativa em todos os POSTs, um por pedido, falha de um não para os outros; resumo final como hoje + linha de avisos (`dados.aviso`).
- Textos dinâmicos via `textContent`.
- [ ] **Step 1: Testes de tela** (render): `test_modal_tem_fotos_e_justificativas` (Torre e Planejamento: inputs, as 7 opções na ordem), `test_planejamento_usa_url_for`, `test_lote_mesma_foto_um_checklist_por_pedido` (JS monta um FormData por item com os mesmos arquivos — conferir no HTML/JS renderizado o laço por item com `append` dos arquivos).
- [ ] **Step 2–4:** RED → GREEN (runner do painel).
- [ ] **Step 5: Playwright local** (porta 8099, API dublada como em `feedback_testar_painel_playwright_local`): abrir o modal, escolher foto, trocar pra justificativa, "Outro"; screenshot desktop e 390px.
- [ ] **Step 6: Commit** `Planejamento e Torre: marcar entregue pede foto do canhoto ou justificativa (em massa, mesma foto)`.

### Task 5: Baixa sem app

**Files:** Modify `nucleo/baixa_sem_app.py` (`concluir_servico`, `executar_lote`, `criar_lote`), `painel_agentes/painel_agentes.py` (`api_baixa_sem_app` ~:1381), `painel_agentes/templates/baixa_sem_app.html`, `nucleo/test_baixa_sem_app.py` e os testes de tela da baixa.

**Interfaces — Consumes:** Tasks 1–2.
- Tela: por pedido "Entregue", além do canhoto (já existe, `canhoto_<sid>`), `nota_<sid>` (imagem, opcional) e `select` de justificativa (+ "Descreva" no "Outro"), obrigatório sem canhoto. JSON `dados` ganha por item `justificativa`, `texto_outro`.
- API: valida cada item entregue (canhoto ou `validar`); 400 com o código do pedido. Converte as imagens com `para_jpeg` (PDF de canhoto: só cópia local, sem checklist, item recebe `aviso`) e grava em `dados/checklist_fotos/lote_<id>/<sid>_<campo>.jpg` **antes** de disparar a thread; item guarda `fotos: {"15086": caminho, "15349": caminho}`, `justificativa`, `texto_outro`. Cópia local do canhoto como hoje.
- `concluir_servico(vuupt, service_id, sucesso, failed_reason_id, agent_id, fotos: dict[int, bytes] | None = None) -> str` passa as fotos a `concluir_como_agente` (insucesso nunca leva fotos); item registra `checklist_id`. Falha do checklist = item `erro` (pedido não concluído). Ao terminar o lote, apaga a pasta `lote_<id>` só se nenhum item ficou em `erro`.
- [ ] **Step 1: Testes:** `test_item_entregue_sem_canhoto_sem_justificativa_400`, `test_item_com_justificativa_sem_foto_ok`, `test_fotos_gravadas_antes_da_thread`, `test_executar_lote_passa_fotos_e_guarda_checklist_id`, `test_checklist_falha_item_erro_sem_concluir`, `test_insucesso_sem_fotos`, `test_pasta_apagada_so_sem_erro`, render `test_tela_tem_nota_e_justificativa`.
- [ ] **Step 2–4:** `py -3.11 -m unittest nucleo.test_baixa_sem_app` e (de `painel_agentes/`) os testes de tela, RED → GREEN.
- [ ] **Step 5: Commit** `Baixa sem app: canhoto e nota de retorno no checklist da VUUPT, justificativa sem canhoto` (+ MAPA: pasta `dados/checklist_fotos`).

### Task 6: Deploy e prova (só quando o Hugo pedir)

- [ ] Review de segurança (painel com upload de arquivo) antes do deploy; deploy com restart de `painel-agentes`.
- [ ] Prova na conta real (Hugo autorizou, spec §8): `testar_fluxo_agente_vuupt.py criar` para cada caso — (a) Torre com canhoto + nota; (b) Planejamento em massa, 2 serviços, mesma foto; (c) sem foto com justificativa "Outro"; (d) repetição depois de falha (forçar falha no finish com contagem errada não é possível pela tela — simular reaproveitamento chamando `preencher_checklist_entrega` duas vezes no mesmo serviço). Conferir `GET /checklists?include=fields` com `file_url` por serviço e `torre_entregas_manuais`.
