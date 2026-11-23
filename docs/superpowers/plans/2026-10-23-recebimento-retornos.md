# Recebimento de retornos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tela mobile `/retornos` no painel onde o recebedor confere os insucessos/parciais que voltam ao galpão (caixas, avaria com foto, motivo, foto da carga, assinatura do motorista), com alerta de divergência/parcial e de retorno atrasado (07h do próximo dia útil) no grupo "Alertas" do /atendimento, e selo "No galpão" na Torre e na Consulta.

**Architecture:** Módulo de domínio `painel_agentes/retornos.py` (tabelas próprias em `dados/dados.db`, lê `nucleo_paradas`/`nucleo_rotas`, posta no grupo via `grupos_equipe.postar`). Rotas finas em `painel_agentes.py`; template `retornos.html` estendendo `base_mobile.html`. Script de lote na raiz `alertar_retornos_atrasados.py` com timer systemd 07h00 seg-sex (pula feriado em código).

**Tech Stack:** Python 3.11, Flask, SQLite, Jinja, JS puro (canvas pra assinatura, `<input capture>` pra foto), unittest.

**Spec:** `docs/superpowers/specs/2026-10-23-recebimento-retornos-design.md`

## Global Constraints

- Rodar Python sempre com `py -3.11`; testes `unittest` de dentro de `painel_agentes/` (`cd painel_agentes && py -3.11 -m unittest test_retornos -v`). Nunca misturar `nucleo`/`roteirizacao` com `painel_agentes` no mesmo comando de teste.
- **Trabalhar num worktree a partir de `origin/master`** (`superpowers:using-git-worktrees`): `painel_agentes.py`, `_menu_lateral_nav.html` e `contadores_menu.py` têm modificações não commitadas de outras sessões na master local. Nunca `git add -A`; só os arquivos da tarefa.
- Português em código, comentários e logs; comentários sem acento onde o arquivo já segue esse padrão (`painel_agentes.py` usa acento; siga o arquivo).
- Templates com `url_for()`, nunca caminho absoluto (painel vive em `app.freshhub.com.br/painel`).
- Entram: parada com `situacao IN ('INSUCESSO','PARCIAL')`. Sai "Não coletado": `failed_reason_id = 5744`, ou `motivo_id` cujo `motivos_ocorrencia.vuupt_failed_reason_id = 5744`, ou `motivo_texto` normalizado (sem acento, minúsculo) igual a `"nao coletado"`.
- Janela da lista de pendentes: insucessos dos últimos **7 dias** (`date(completed_at)` local; se nulo, `data_rota`). `completed_at` do núcleo já é horário local (`vuupt_para_local` no sync).
- Chave do recebimento = `nucleo_paradas.id` (UNIQUE). Nunca chavear só por código (reentrega reaproveita código).
- Divergência por pedido: `caixas_recebidas != caixas_esperadas` (só quando esperado é conhecido) **ou** `avaria` **ou** motivo corrigido. Observação sozinha não é divergência. Parcial recebida **sempre** vai pro alerta.
- Parcial: caixas esperadas = desconhecidas (`None`); recebedor informa as caixas, campo obrigatório sem pré-preenchimento.
- Alerta no grupo de nome exato **"Alertas"** (criado se não existir), autor **"Recebimento de retornos"**, usuario `"sistema"`. Linha de parcial: `PARCIAL: avisar o cliente e solicitar a entrada no estoque`.
- Alerta de atraso: só em dia útil (`regras.feriados.eh_dia_util`), pega retornos com dia do insucesso **< hoje**, não recebidos e não alertados; uma mensagem só, agrupada por motorista; cada retorno alertado uma única vez; nada a alertar = nenhuma mensagem. `--modo-teste` imprime e não posta nem grava.
- Confirmar: níveis `("total", "operador", "logistica", "galpao", "expedicao")`. Ver: esses + `("atendimento", "leitura")`. POST com `@exige_mesma_origem`.
- Obrigatórios na confirmação: ≥1 foto da carga, assinatura, nome do recebedor, ≥1 pedido marcado; foto de avaria obrigatória em pedido com avaria; caixas obrigatórias em parcial.
- Fotos/assinatura: disco em `dados/retornos/<descarga_id>/` + GCS best-effort por `documentos_pedido.storage_gcs.enviar_documento(config, caminho, f"RETORNO-{descarga_id}", "Retorno")` (falha de GCS só loga). Servir só por rota autenticada.
- Atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit em que tela, tabelas e timer nascem.
- Commit e push só quando o Hugo pedir. Mensagem: `Painel: ...` / `Retornos: ...`, terminando com `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. Duas pessoas confirmando a mesma descarga ao mesmo tempo (ou duplo toque no botão) → o segundo recebe erro claro ("já recebido"), nada duplicado, um alerta só. → teste `test_receber_duas_vezes_mesma_parada_recusa` (Task 1).
2. Insucesso que vira entregue depois (motorista corrige na Vuupt) → some da lista de pendentes e do alerta de atraso. → coberto pelo filtro `situacao IN (...)` em `test_lista_ignora_entregue` (Task 1).
3. Upload grande/4G lento: foto de 8 MB do celular → reduzida no navegador antes de enviar; servidor recusa > 10 MB por arquivo com mensagem. → teste `test_descarga_recusa_arquivo_grande` (Task 2).
4. Sexta 18h insucesso → não alerta no sábado/domingo, alerta segunda 07h; segunda feriado → terça. → `test_atraso_fim_de_semana_e_feriado` (Task 3).
5. Usuário do nível `galpao` (cai em /wms) precisa achar a tela → link "Retornos" no app do galpão e no menu; `galpao` em `requer_auth` e em `acesso_telas.TELAS`. → `test_galpao_abre_retornos` (Task 2) + `test_acesso_telas` existente (toda rota precisa estar numa tela).

---

### Task 1: Domínio `retornos.py` (lista, registro, divergência, texto do alerta)

**Files:**
- Create: `painel_agentes/retornos.py`
- Test: `painel_agentes/test_retornos.py`

**Interfaces:**
- Consumes: `grupos_equipe.conectar`, `grupos_equipe.criar_grupo`, `grupos_equipe.postar` (assinaturas em `painel_agentes/grupos_equipe.py`); `insucesso_entrega.motivos_falha.MOTIVOS_FALHA`; esquema `nucleo_paradas`/`nucleo_rotas`/`motivos_ocorrencia` (`nucleo/banco.py`).
- Produces:
  - `DB_PATH: Path`, `NOME_GRUPO = "Alertas"`, `AUTOR_ALERTA = "Recebimento de retornos"`, `JANELA_DIAS = 7`
  - `class ErroRetorno(Exception)` (vira 400 na API)
  - `conectar(caminho=None) -> sqlite3.Connection` — cria `retornos_descargas`, `retornos_recebidos` (UNIQUE `parada_id`), `retornos_alertas_atraso` (PK `parada_id`).
  - `eh_nao_coletado(failed_reason_id, motivo_id, motivo_texto, ids_nao_coletado: set[int]) -> bool`
  - `listar_pendentes(conn, hoje: date, dias: int = JANELA_DIAS) -> list[dict]` — um dict por motorista: `{"motorista_nome", "motorista_cpf", "chave", "pedidos": [...], "total_caixas", "mais_antigo": "YYYY-MM-DD", "desde_ontem": bool}`; cada pedido: `{"parada_id", "rota_id", "service_id", "codigo", "destinatario", "embarcador", "caixas_esperadas": int|None, "parcial": bool, "motivo", "baixa_em", "dia_insucesso", "rota_nome"}`. `chave` = cpf ou nome (agrupa).
  - `registrar_descarga(conn, *, itens: list[dict], recebedor: str, usuario: str, fotos_carga: list[str], assinatura: str) -> dict` — `itens`: `{"parada_id", "caixas_recebidas", "avaria": bool, "fotos_avaria": list[str], "motivo": str, "observacao": str}`. Relê as paradas do núcleo (não confia no cliente pra esperado/motivo original/motorista). Valida obrigatórios, grava tudo numa transação, devolve `{"descarga_id", "divergentes": [...], "parciais": [...], "motorista_nome"}`. Parada já recebida → `ErroRetorno("PS-… já foi recebido")` sem gravar nada.
  - `reservar_descarga(conn) -> int` — insere `retornos_descargas` vazia (pra ter id antes de salvar as fotos); `registrar_descarga` recebe `descarga_id: int` como kwarg extra e completa essa linha.
  - `texto_alerta_descarga(resultado: dict, recebedor: str) -> str | None` — `None` quando não há divergência nem parcial.
  - `postar_alerta(conn_grupos, texto: str) -> None` — acha/cria o grupo "Alertas" e posta com `usuario="sistema"`, `autor=AUTOR_ALERTA`.
  - `status_por_service_ids(conn, ids: list[int]) -> dict[int, dict]` — `{service_id: {"recebido_em", "recebedor", "divergente": bool, "descarga_id"}}`.
  - `status_por_codigo(conn, codigo: str) -> list[dict]` — recebimentos do código (consulta).
  - `listar_descargas(conn, limite: int = 50) -> list[dict]` e `detalhar_descarga(conn, descarga_id: int) -> dict | None` (histórico).

- [ ] **Step 1: Escrever os testes que falham** em `test_retornos.py` com banco temporário (`tempfile`), criando `nucleo_rotas`/`nucleo_paradas` via `nucleo.banco.conectar(caminho)` e semeando paradas. Testes e asserções:
  - `test_lista_insucesso_e_parcial_agrupa_por_motorista`: 2 insucessos + 1 parcial de João, 1 de Maria → 2 grupos; João com 3 pedidos, `total_caixas` soma só os esperados conhecidos; parcial com `caixas_esperadas is None` e `parcial True`.
  - `test_lista_exclui_nao_coletado`: parada com `failed_reason_id=5744` e outra (app) com `motivo_id` ligado a `motivos_ocorrencia.vuupt_failed_reason_id=5744` e outra com `motivo_texto="Não Coletado"` → nenhuma aparece.
  - `test_lista_ignora_entregue`: parada `situacao='ENTREGUE'` → não aparece.
  - `test_lista_janela_7_dias`: insucesso de 8 dias atrás não aparece; de 7 aparece.
  - `test_lista_tira_recebidos`: depois de `registrar_descarga` com a parada, ela some.
  - `test_desde_ontem`: insucesso de ontem → `desde_ontem True`; de hoje → `False`.
  - `test_registrar_sem_divergencia`: caixas iguais, sem avaria, motivo igual → `divergentes == []`, `texto_alerta_descarga(...) is None`.
  - `test_registrar_divergencias`: caixas 3 de 5 → divergente; avaria com foto → divergente; motivo trocado → divergente com `"de → para"` no texto; só observação → não divergente.
  - `test_parcial_sempre_no_alerta_e_exige_caixas`: parcial sem `caixas_recebidas` → `ErroRetorno`; com caixas → texto contém `"PARCIAL: avisar o cliente e solicitar a entrada no estoque"`.
  - `test_obrigatorios`: sem foto da carga / sem assinatura / sem recebedor / sem itens / avaria sem foto → `ErroRetorno` cada, e nenhuma linha em `retornos_recebidos`.
  - `test_receber_duas_vezes_mesma_parada_recusa`: segunda `registrar_descarga` da mesma parada → `ErroRetorno` com o código, contagem de `retornos_recebidos` continua 1.
  - `test_registrar_usa_dados_do_nucleo`: item com `caixas_esperadas` forjado no dict é ignorado; esperado gravado é o `volume_caixas` da parada.
  - `test_postar_alerta_cria_grupo_uma_vez`: dois `postar_alerta` → 1 linha em `equipe_grupos` com nome "Alertas", 2 mensagens com autor `"Recebimento de retornos"`.
  - `test_status_por_service_ids`: devolve recebido_em/recebedor/divergente pro service_id recebido; ausente pros outros.

- [ ] **Step 2: Rodar e ver falhar**
  Run: `cd painel_agentes && py -3.11 -m unittest test_retornos -v`
  Expected: `ModuleNotFoundError: No module named 'retornos'`

- [ ] **Step 3: Implementar `painel_agentes/retornos.py`** com as interfaces acima. Docstring no topo no estilo de `grupos_equipe.py` (pedido do Hugo 23/10, decisões da spec seção 5). `listar_pendentes` faz um SELECT com `LEFT JOIN retornos_recebidos r ON r.parada_id = p.id WHERE r.id IS NULL` e filtra não coletado em Python com `eh_nao_coletado` (ids de `motivos_ocorrencia` carregados uma vez; tabela ausente → conjunto vazio). Lista de motivos válidos para correção = textos de `MOTIVOS_FALHA` (exceto "Não coletado"); `registrar_descarga` recusa motivo fora dela.

- [ ] **Step 4: Rodar e ver passar**
  Run: `cd painel_agentes && py -3.11 -m unittest test_retornos -v`
  Expected: todos `ok`.

- [ ] **Step 5: Commit**
  ```bash
  git add painel_agentes/retornos.py painel_agentes/test_retornos.py
  git commit -m "Retornos: dominio do recebimento de retornos (lista, descarga, divergencia, alerta)"
  ```

### Task 2: Tela `/retornos` + APIs + arquivos + menu/acesso

**Files:**
- Modify: `painel_agentes/painel_agentes.py` (rotas novas perto de `/baixa-sem-app`; `import retornos` junto dos outros imports locais)
- Create: `painel_agentes/templates/retornos.html` (estende `base_mobile.html`)
- Modify: `painel_agentes/templates/_menu_lateral_nav.html` (item no grupo `Operar`)
- Modify: `painel_agentes/acesso_telas.py` (`TELAS`)
- Modify: `painel_agentes/templates/wms.html` (link "Retornos" no topo do app do galpão)
- Test: `painel_agentes/test_retornos_tela.py`

**Interfaces:**
- Consumes: tudo de `retornos` (Task 1); `requer_auth`, `exige_mesma_origem`, `_carregar_config` de `painel_agentes.py`.
- Produces (rotas):
  - `GET /retornos` endpoint `retornos_tela` → HTML; passa `pode_confirmar = g.nivel_acesso in NIVEIS_RETORNOS_CONFIRMAR` (ou tela liberada em /acessos).
  - `GET /api/retornos` → `{"motoristas": listar_pendentes(...), "motivos": [...]}`.
  - `POST /api/retornos/descarga` (multipart): campo `dados` (JSON: `recebedor`, `itens`), arquivos `foto_carga` (1+), `assinatura` (PNG do canvas), `foto_avaria_<parada_id>` (1+ por pedido com avaria) → `{"ok": true, "descarga_id": n}` ou 400 `{"erro": "..."}`. Fluxo: `reservar_descarga` → salvar arquivos em `dados/retornos/<id>/` (+GCS best-effort) → `registrar_descarga` → se `texto_alerta_descarga` não for `None`, `postar_alerta` (falha do alerta só loga, não desfaz). Em `ErroRetorno` apaga a pasta e a descarga reservada.
  - `GET /api/retornos/descargas` → histórico; `GET /retornos/descarga/<int:descarga_id>` → JSON do detalhe.
  - `GET /retornos/arquivo/<int:descarga_id>/<nome>` → `send_file` só de dentro de `dados/retornos/<id>/` (`secure_filename`, recusa `..`).
  - Constantes: `NIVEIS_RETORNOS_CONFIRMAR = ("total", "operador", "logistica", "galpao", "expedicao")`, `NIVEIS_RETORNOS_VER = NIVEIS_RETORNOS_CONFIRMAR + ("atendimento", "leitura")`.
  - `acesso_telas.TELAS`: `("retornos_tela", "Retornos", "Operar", [r"^/retornos(/|$)", r"^/api/retornos(/|$)"])`.

- [ ] **Step 1: Escrever os testes que falham** em `test_retornos_tela.py` usando `app.test_client()` com sessão forjada (ver padrão em `test_baixa_sem_app_tela.py` / `test_canhotos_pendentes_tela.py`), `retornos.DB_PATH` e a pasta de arquivos apontando pra temporário, `postar_alerta` e `enviar_documento` substituídos por dublês:
  - `test_galpao_abre_retornos`: nível `galpao` → `GET /retornos` 200; `atendimento` → 200 sem botão de confirmar (`pode_confirmar` False no HTML); nível sem acesso (ex.: nenhum) → redireciona pro login.
  - `test_atendimento_nao_confirma`: `POST /api/retornos/descarga` como `atendimento` → 403.
  - `test_descarga_ok_grava_e_alerta`: POST com 1 parada divergente, foto da carga, assinatura → 200, arquivo existe em `dados/retornos/<id>/`, dublê de alerta chamado 1 vez com texto contendo o código.
  - `test_descarga_sem_divergencia_nao_alerta`: dublê não chamado.
  - `test_descarga_sem_foto_carga_400`: 400 com `erro`, nenhuma linha gravada, pasta removida.
  - `test_descarga_recusa_arquivo_grande`: arquivo > 10 MB → 400 `"Foto muito grande"`.
  - `test_arquivo_nao_sai_da_pasta`: `GET /retornos/arquivo/1/..%2Fdados.db` → 404.
  - `test_falha_gcs_nao_impede`: dublê do GCS levanta exceção → 200 mesmo assim.

- [ ] **Step 2: Rodar e ver falhar**
  Run: `cd painel_agentes && py -3.11 -m unittest test_retornos_tela -v`
  Expected: 404 nas rotas / `AttributeError`.

- [ ] **Step 3: Implementar rotas** em `painel_agentes.py` conforme Interfaces, com `MAX_FOTO_BYTES = 10 * 1024 * 1024`.

- [ ] **Step 4: Implementar `templates/retornos.html`** (mobile first, uma coluna, botões ≥ 44 px, estilo das telas `atendimento_mobile.html`/`wms.html`):
  - Segmento "A receber" | "Recebidos".
  - A receber: cartões por motorista (nome, nº pedidos, caixas, selo "desde ontem" se `desde_ontem`). Toque abre a conferência.
  - Conferência: por pedido — checkbox "veio" (marcado), código, destinatário, embarcador, motivo; caixas com −/+ (pré-preenchido com esperado; vazio e obrigatório se parcial, com selo "Parcial"); "Avaria" sim/não → aparece botão de foto (obrigatório); select de motivo (pré-selecionado o do motorista); observação.
  - Foto da carga (≥1), nome do recebedor (lembrado em `localStorage` dentro de try/catch), canvas de assinatura com "Limpar" (pointer events, funciona com o dedo), botão "Confirmar recebimento" desabilitado durante o envio.
  - Fotos com `<input type="file" accept="image/*" capture="environment">`, reduzidas no navegador (lado maior 1600 px, JPEG 0.8) antes do `FormData`.
  - Recebidos: lista de `/api/retornos/descargas`; toque abre detalhe com fotos e assinatura via `url_for('retornos_arquivo', ...)`.
  - Sem `pode_confirmar`: conferência só leitura.
  - Todas as URLs via `url_for()` injetadas no template.

- [ ] **Step 5: Menu, acessos e link do galpão**: item `{'rota': 'retornos_tela', 'rotulo': 'Retornos', 'titulo': 'Recebimento de retornos (insucessos que voltam ao galpão)', 'niveis': ('total', 'operador', 'logistica', 'galpao', 'expedicao', 'atendimento', 'leitura'), 'badge': none, 'icone': <svg de caixa com seta de volta>}` no grupo `Operar` de `_menu_lateral_nav.html`; linha em `acesso_telas.TELAS`; link "Retornos" no topo de `wms.html` (`url_for('retornos_tela')`).

- [ ] **Step 6: Rodar testes da tela e os de acesso**
  Run: `cd painel_agentes && py -3.11 -m unittest test_retornos test_retornos_tela test_acesso_telas test_nivel_logistica -v`
  Expected: todos `ok` (o `test_acesso_telas` confere que toda rota nova está numa tela).

- [ ] **Step 7: Prova manual local** em outra porta: `cd painel_agentes && py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"`; abrir `/retornos` com viewport de celular (Playwright, ver memória `feedback_testar_painel_playwright_local`), conferir lista (banco local congelado pode ter poucos insucessos; tudo bem estar vazio), desenhar assinatura, tirar screenshot. Não confirmar descarga contra o banco local real sem apontar `retornos.DB_PATH` pra cópia.

- [ ] **Step 8: Commit**
  ```bash
  git add painel_agentes/painel_agentes.py painel_agentes/templates/retornos.html painel_agentes/templates/_menu_lateral_nav.html painel_agentes/acesso_telas.py painel_agentes/templates/wms.html painel_agentes/test_retornos_tela.py
  git commit -m "Painel: tela Retornos (recebimento de insucessos no galpao, mobile, fotos e assinatura)"
  ```

### Task 3: Alerta de retorno atrasado (07h do próximo dia útil)

**Files:**
- Create: `alertar_retornos_atrasados.py` (raiz)
- Create: `infra/stokki-retornos-atrasados.service`, `infra/stokki-retornos-atrasados.timer`
- Modify: `notificar_whatsapp.py` (`NOMES_DAS_TAREFAS`: `"retornos-atrasados": "Retornos não recebidos"`)
- Modify: `painel_agentes/retornos.py` (funções abaixo)
- Test: `painel_agentes/test_retornos.py` (casos novos)

**Interfaces:**
- Consumes: `retornos.conectar`, `retornos.postar_alerta`, `regras.feriados.eh_dia_util`.
- Produces em `retornos.py`:
  - `pendentes_atrasados(conn, hoje: date, dias: int = JANELA_DIAS) -> list[dict]` — mesmos grupos de `listar_pendentes`, só pedidos com `dia_insucesso < hoje` e sem linha em `retornos_alertas_atraso`.
  - `texto_alerta_atraso(grupos: list[dict]) -> str` — cabeçalho `"Retornos não recebidos até 07h"` + por motorista: nome e linhas `código · embarcador · N cx (ou "parcial") · motivo`.
  - `marcar_alertados(conn, parada_ids: list[int]) -> None`.
- `alertar_retornos_atrasados.py`: `main(argv=None) -> int`, flags `--modo-teste` e `--data YYYY-MM-DD` (padrão hoje). Reconfigura `sys.stdout` para UTF-8 no topo. Se não for dia útil → loga e sai 0. Sem pendentes → sai 0 sem postar. Senão posta e marca (modo teste: só imprime).
- Timer: `OnCalendar=Mon..Fri *-*-* 07:00:00`, `Persistent=false`, `AccuracySec=1min`; service `Type=oneshot`, `User=www-data`, `WorkingDirectory=/opt/stokki-eventos`, `ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/alertar_retornos_atrasados.py`, `OnFailure=stokki-alerta-falha@%n.service` (mesmo molde de `infra/stokki-vigia-fila-impressao.*`).

- [ ] **Step 1: Testes que falham** em `test_retornos.py`:
  - `test_atraso_pega_so_dias_anteriores`: insucesso de hoje não entra; de ontem entra.
  - `test_atraso_uma_vez_so`: depois de `marcar_alertados`, `pendentes_atrasados` vem vazio.
  - `test_atraso_ignora_recebido`.
  - `test_atraso_fim_de_semana_e_feriado`: `main(["--data", "2026-10-24", "--modo-teste"])` (sábado) retorna 0 sem chamar postar; insucesso sexta 23/10 aparece em `main(["--data", "2026-10-26", ...])` (segunda); com `--data 2026-11-02` (feriado de Finados, segunda) não roda e em `2026-11-03` roda. (Confirmar Finados em `regras.feriados.feriados_do_ano(2026)`; se não estiver, usar outro feriado da lista.)
  - `test_texto_atraso_agrupa_motorista`: 2 motoristas → nomes na ordem, parcial aparece como "parcial".
  - `test_unit_tem_nome`: rodar o teste existente de `NOMES_DAS_TAREFAS` (procurar em `test_notificar_whatsapp*.py`) e confirmar que passa com a unit nova.

- [ ] **Step 2: Rodar e ver falhar**
  Run: `cd painel_agentes && py -3.11 -m unittest test_retornos -v`
  Expected: `AttributeError: module 'retornos' has no attribute 'pendentes_atrasados'`

- [ ] **Step 3: Implementar** as funções, o script e as units.

- [ ] **Step 4: Rodar e ver passar**
  Run: `cd painel_agentes && py -3.11 -m unittest test_retornos -v` e, da raiz, o teste de `NOMES_DAS_TAREFAS` (ex.: `py -3.11 -m unittest test_notificar_whatsapp -v`).
  Expected: `ok`.

- [ ] **Step 5: Prova em modo teste local**: `py -3.11 alertar_retornos_atrasados.py --modo-teste --data 2026-10-26` → imprime a mensagem (ou "nada a alertar"), nenhuma linha nova em `retornos_alertas_atraso`.

- [ ] **Step 6: Commit**
  ```bash
  git add alertar_retornos_atrasados.py infra/stokki-retornos-atrasados.service infra/stokki-retornos-atrasados.timer notificar_whatsapp.py painel_agentes/retornos.py painel_agentes/test_retornos.py
  git commit -m "Retornos: alerta 07h de dia util dos retornos nao recebidos no grupo Alertas"
  ```

### Task 4: Selo "No galpão" na Torre e na Consulta + mapa do sistema

**Files:**
- Modify: `painel_agentes/torre_controle.py` (`_badges_insucessos`, ~linha 489)
- Modify: `painel_agentes/painel_agentes.py` (`consulta_pedido`, ~linha 2511)
- Modify: `painel_agentes/templates/consulta_pedido.html`
- Modify: `MAPA_DO_SISTEMA.txt`
- Test: `painel_agentes/test_retornos_tela.py` (casos novos)

**Interfaces:**
- Consumes: `retornos.status_por_service_ids`, `retornos.status_por_codigo`.
- Produces: badge na Torre no mesmo formato dos existentes em `_badges_insucessos` (ler o formato do dict de badge no código), texto `"No galpão dd/mm HH:MM"` ou `"No galpão · divergência"`; na Consulta, `d["retornos"] = retornos.status_por_codigo(conn, codigo)` mostrado como bloco "No galpão" (data, recebedor, caixas, divergência, link pro detalhe da descarga). Falha de leitura não derruba Torre nem Consulta (try/except + log, como os outros badges).

- [ ] **Step 1: Testes que falham**:
  - `test_torre_badge_no_galpao`: `_badges_insucessos([{"service_id": X}])` com X recebido → badge com `"No galpão"`; com divergência → `"divergência"` no texto; sem tabela de retornos → nenhum erro.
  - `test_consulta_mostra_no_galpao`: `GET /consulta/pedido/PS-1` (dublê de `nucleo_consulta.detalhar_pedido`) com recebimento → HTML contém `"No galpão"`.

- [ ] **Step 2: Rodar e ver falhar**
  Run: `cd painel_agentes && py -3.11 -m unittest test_retornos_tela -v`
  Expected: FAIL nos dois testes novos.

- [ ] **Step 3: Implementar** badge e bloco da consulta.

- [ ] **Step 4: `MAPA_DO_SISTEMA.txt`**: tela `/retornos` (seção de telas do painel), módulo `painel_agentes/retornos.py`, tabelas `retornos_descargas`/`retornos_recebidos`/`retornos_alertas_atraso`, pasta `dados/retornos/`, timer `stokki-retornos-atrasados` (07h seg-sex, pula feriado) na tabela de horários, e armadilha "Alerta vai pro grupo Alertas do /atendimento, não WhatsApp".

- [ ] **Step 5: Rodar toda a suíte do painel tocada**
  Run: `cd painel_agentes && py -3.11 -m unittest test_retornos test_retornos_tela test_acesso_telas test_torre_tratar test_torre_marcar_entregue -v` e `py -3.11 -m py_compile` em todos os arquivos editados.
  Expected: todos `ok`.

- [ ] **Step 6: Commit**
  ```bash
  git add painel_agentes/torre_controle.py painel_agentes/painel_agentes.py painel_agentes/templates/consulta_pedido.html MAPA_DO_SISTEMA.txt painel_agentes/test_retornos_tela.py
  git commit -m "Torre/Consulta: selo No galpao para retorno recebido; mapa do sistema"
  ```

## Depois do plano (não executar sem o Hugo pedir)

Deploy pelo skill `deploy-vps`: pull, chown, copiar `infra/stokki-retornos-atrasados.*` para `/etc/systemd/system/`, `daemon-reload`, `enable --now stokki-retornos-atrasados.timer`, `restart painel-agentes`; provar com `curl` em `/painel/retornos` (login real) e `sudo -u www-data venv/bin/python alertar_retornos_atrasados.py --modo-teste`. Conferir que `dados/retornos/` nasce com dono `www-data`.
