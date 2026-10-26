# Aviso de pendências no painel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Faixas insistentes no topo de todas as telas do painel, uma por tipo de pendência do perfil logado, com Verificar e Ignorar (30 min, com confirmação), e uma tela nova que executa as solicitações do portal (cancelar, em espera, reagendar).

**Architecture:** Um módulo `painel_agentes/avisos_pendencias.py` com o catálogo das 18 pendências (texto, níveis, destino, cor), as contagens que faltam e o filtro do "ignorado" (tabela própria no `dados.db`). O endpoint existente `/api/sidebar/contadores` passa a devolver também `avisos`, e o JS do menu (que já busca a cada 2 min) dispara um evento que um partial novo, incluído em `base.html` e `base_mobile.html`, usa para desenhar as faixas. Parte 2: `portal_cliente/solicitacoes_execucao.py` executa cada tipo de solicitação na Vuupt/Stokki, e a tela `/solicitacoes-portal` lista e executa.

**Tech Stack:** Python 3.11, Flask (painel_agentes), sqlite3, unittest, JS puro.

**Spec:** `docs/superpowers/specs/2026-10-24-aviso-pendencias-painel-design.md` (ler junto). Esboço aprovado (opção A): `.superpowers/brainstorm/53141-1792886123/content/aviso-pendencias.html`.

## Global Constraints

- Português em código, comentários e commits (`Área: o que mudou e por quê`). `py -3.11`; unittest; testes do painel rodam **de dentro de `painel_agentes/`**, nunca misturados com outras suítes.
- Trabalhar num worktree novo a partir de `origin/master` (a master local tem edições de outras sessões em `painel_agentes.py`, menu e contadores). `git add` só dos arquivos da tarefa. Commits locais autorizados; push/deploy só quando o Hugo pedir.
- **Nunca deixar página lenta:** nada de contagem na renderização; contagens rodam no `/api/sidebar/contadores` (JS depois do load); fonte cara usa `contadores_menu._caro`. As chamadas periódicas não renovam a sessão (já é assim; não mudar).
- **Um único polling** (o do `_menu_lateral_script.html`, 2 min). O aviso escuta um evento; não cria outro `setInterval` de busca.
- `IGNORAR_MINUTOS = 30`. Até **3** faixas visíveis no desktop, **2** no `base_mobile.html`; resto em "+ N pendências · ver todas".
- Vermelhas (spec 6): chaves `chamados`, `solicitacoes_portal`, `retornos`, `cadastros_embarcador`, `motoristas`. Vermelha ignorada **volta na hora** se a quantidade subir; laranja só ao fim dos 30 min.
- Ordem **fixa** = ordem do catálogo (vermelhas primeiro, depois a ordem da tabela da spec 5).
- Pessoa do "ignorar": login nominal (`acesso_telas.conta(session["usuario"])` existe) → `login:<usuario>`; senão cookie `aviso_aparelho` (uuid4, 1 ano, HttpOnly, SameSite=Lax, Secure como o cookie de sessão) → `aparelho:<uuid>`.
- Nível `leitura` nunca é cobrado. Login personalizado só vê a faixa se a tela de destino estiver liberada pra ele; `cadastros_embarcador` segue a permissão `aprovar_embarcadores`.
- A faixa não aparece quando `location.pathname` (sem o prefixo `BASE_PATH`) é o destino dela (comparar sem query).
- Rotas novas: `requer_auth(niveis=...)`, `url_for()`, entrada em `acesso_telas.TELAS` (ou `FORA_DO_CATALOGO` no teste), menu com `niveis` igual. Atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit.
- Stokki: sempre dentro de `stokki/sessao_uso` (trava), nunca login por cima; chamada manual ao DataTables dá 500. Vuupt: `PUT /cancel` (não DELETE). Testes nunca chamam Stokki/Vuupt reais (injeção).

## Review Focus

1. **Login de nível compartilhado** (ex.: `operador` usado por 3 pessoas): um ignorar não pode silenciar as outras — teste em Task 2 (`test_pessoas_diferentes_nao_compartilham`) e Task 3 (`test_aparelho_sem_cookie_ganha_um`).
2. **Contagem falha ou demora** (Torre/Pedidos Parados sem valor após restart, ou exceção numa fonte): o aviso daquela pendência simplesmente não vem, as outras sim — teste em Task 1 (`test_fonte_que_falha_nao_derruba_as_outras`).
3. **Vermelha cresce durante o ignorar** volta na hora; laranja não — Task 2 (`test_vermelha_volta_se_subir`, `test_laranja_espera_30_min`).
4. **Perfil sem permissão** (leitura, login personalizado sem a tela) não recebe aviso de tela que não abre — Task 1 (`test_leitura_nao_e_cobrado`, `test_login_personalizado_sem_tela`).
5. **Executar solicitação com a rota já andando / Stokki ocupada**: não conclui, mostra o motivo, nada fica meio-feito — Task 7 (`test_cancelar_vuupt_recusa_nao_conclui`, `test_stokki_ocupada_nao_conclui`).

---

## Estrutura de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `painel_agentes/avisos_pendencias.py` | catálogo, contagens novas, montar a lista por perfil, ignorar/filtrar |
| `painel_agentes/test_avisos_pendencias.py` | testes do módulo |
| `painel_agentes/painel_agentes.py` | `/api/sidebar/contadores` devolve `avisos`; `POST /api/avisos/ignorar`; Parte 2: rotas da tela nova |
| `painel_agentes/templates/_aviso_pendencias.html` | CSS + HTML + JS das faixas e do modal |
| `painel_agentes/templates/_menu_lateral_script.html` | dispara `document` event `contadores-atualizados` com a resposta |
| `painel_agentes/templates/base.html`, `base_mobile.html` | `{% include "_aviso_pendencias.html" %}` no topo do conteúdo |
| `painel_agentes/test_aviso_pendencias_tela.py` | testes das rotas |
| `portal_cliente/solicitacoes_execucao.py` (+ teste) | Parte 2: executar cada tipo |
| `painel_agentes/templates/solicitacoes_portal.html` | Parte 2: tela |

---

## Parte 1 — o aviso

### Task 1: Catálogo e contagens

**Files:** Create `painel_agentes/avisos_pendencias.py`, `painel_agentes/test_avisos_pendencias.py`.

**Interfaces — Produces:**
- `IGNORAR_MINUTOS = 30`
- `CATALOGO: list[dict]` na ordem fixa, cada item `{"chave", "texto": "{n} chamados esperando a Fresh Log", "niveis": tuple, "destino": "/atendimento", "endpoint_destino": "atendimento_chamados", "vermelho": bool}` — as 18 linhas da spec 5 com estas chaves: `chamados, solicitacoes_portal, retornos, cadastros_embarcador, motoristas, pedagios, canhotos, canhotos_pendentes, transportadoras, clientes_agenda, pedidos_parados, torre, rascunhos_erro, batimento, vigia_vencidos, rotinas_falhas, wms_pendencias, wms_divergencias`. Níveis: os da spec 5 sem `leitura`; `total` em todos. Texto no singular quando n == 1 (ex.: "1 chamado esperando a Fresh Log").
- `contar(chave: str) -> int | None` para as chaves que **não** estão em `contadores_menu` (`solicitacoes_portal, retornos, cadastros_embarcador, canhotos_pendentes, clientes_agenda, rascunhos_erro, batimento, vigia_vencidos, rotinas_falhas, wms_pendencias, wms_divergencias`). Usar COUNT em SQL quando a função existente só lista (ver spec 10). `retornos`, `vigia_vencidos`, `batimento`, `rotinas_falhas`, `canhotos_pendentes` passam por `contadores_menu._caro(chave, ...)` (podem ser lentas).
- `montar(nivel: str, contadores: dict, telas_liberadas: set | None, pode_aprovar_embarcador: bool) -> list[dict]`: recebe o dict já calculado por `contadores_menu.contadores(...)` (reaproveita `atendimento→chamados`, `pedagios`, `canhotos`, `transportadoras`, `motoristas`, `pedidos_parados`, `torre`), calcula as demais com `contar`, filtra por nível/tela/permissão, descarta `qtd == 0` e devolve `[{"chave","texto","qtd","destino","vermelho"}]` na ordem do catálogo. Exceção ou `None` numa fonte → item ausente.

- [ ] **Step 1: Testes que falham** (`painel_agentes/test_avisos_pendencias.py`, banco temporário + fontes mockadas com `mock.patch.object`):
  - `test_ordem_fixa_vermelhas_primeiro`: com qtd > 0 em `pedagios`, `chamados`, `retornos` → chaves `["chamados","retornos","pedagios"]`.
  - `test_texto_singular_e_plural`: chamados 1 → `"1 chamado esperando a Fresh Log"`; 12 → `"12 chamados esperando a Fresh Log"`.
  - `test_leitura_nao_e_cobrado`: nível `leitura` com tudo > 0 → `[]`.
  - `test_login_personalizado_sem_tela`: nível `operador`, `telas_liberadas={"torre"}` → só `torre`.
  - `test_embarcador_so_com_permissao`: `cadastros_embarcador` some para `operador` sem `pode_aprovar_embarcador`, aparece com ela.
  - `test_fonte_que_falha_nao_derruba_as_outras`: `contar("retornos")` levanta → lista tem as outras, sem `retornos`.
  - `test_zero_nao_vira_aviso`.
  - Um teste de contagem por fonte nova com 2–3 linhas inseridas no banco temporário (ex.: `test_conta_clientes_agenda_pendentes`, `test_conta_solicitacoes_portal_pendentes`, `test_conta_rascunhos_com_erro`).
- [ ] **Step 2:** `cd painel_agentes && py -3.11 -m unittest test_avisos_pendencias` → FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4:** mesmo comando → OK.
- [ ] **Step 5: Commit** `Painel: catálogo das pendências do aviso no topo (18 tipos, por perfil)`.

### Task 2: Ignorar por pessoa

**Files:** Modify `painel_agentes/avisos_pendencias.py`, `painel_agentes/test_avisos_pendencias.py`.

**Interfaces — Produces:**
- Tabela `painel_avisos_ignorados(pessoa TEXT, chave TEXT, ate TEXT, qtd INTEGER, PRIMARY KEY(pessoa, chave))` em `dados/dados.db`, criada por `conectar(caminho=None)` no padrão de `acesso_telas.conectar` (`DB_PATH` mockável).
- `ignorar(conn, pessoa: str, chave: str, qtd: int, agora: datetime) -> None` (upsert, `ate = agora + IGNORAR_MINUTOS`).
- `filtrar_ignorados(conn, pessoa: str, avisos: list[dict], agora: datetime) -> list[dict]`: remove os ignorados ainda válidos; vermelho volta se `qtd_atual > qtd_ignorada`.

- [ ] **Step 1: Testes que falham:** `test_ignorado_some_por_30_min` (some às 10:29, volta às 10:30 quando ignorado às 10:00), `test_vermelha_volta_se_subir` (chamados ignorado com 3, agora 5 → volta), `test_laranja_espera_30_min` (pedágios ignorado com 3, agora 8 às 10:10 → continua escondido), `test_pessoas_diferentes_nao_compartilham`, `test_ignorar_de_novo_renova`.
- [ ] **Step 2–4:** RED → implementar → GREEN (`cd painel_agentes && py -3.11 -m unittest test_avisos_pendencias`).
- [ ] **Step 5: Commit** `Painel: ignorar aviso por 30 min por pessoa (vermelha volta se subir)`.

### Task 3: API

**Files:** Modify `painel_agentes/painel_agentes.py` (`api_sidebar_contadores` e rota nova perto dela), `painel_agentes/acesso_telas.py` (`CAMINHOS_COMUNS` += `/api/avisos/ignorar`). Create `painel_agentes/test_aviso_pendencias_tela.py` (molde `test_inicio_tela.py`).

**Interfaces — Consumes:** Task 1 `montar`, Task 2 `filtrar_ignorados`, `ignorar`. **Produces:**
- `GET /api/sidebar/contadores` → `{"contadores": {...como hoje...}, "avisos": [{"chave","texto","qtd","destino","vermelho"}]}`; `destino` já com `url_for` (prefixo do Caddy). Seta o cookie `aviso_aparelho` se o login não é nominal e o cookie não existe.
- `POST /api/avisos/ignorar` body `{"chave": str, "qtd": int}` → `{"ok": true, "ate": iso}`; `@exige_mesma_origem`; mesmos níveis do endpoint de contadores; chave fora do catálogo → 400.
- Helper `_pessoa_do_aviso() -> str` (Global Constraints).

- [ ] **Step 1: Testes que falham:** `test_contadores_traz_avisos` (montar mockado), `test_aparelho_sem_cookie_ganha_um` (resposta tem `Set-Cookie: aviso_aparelho=`), `test_login_nominal_usa_login`, `test_ignorar_grava_e_some` (POST e depois GET sem o aviso), `test_ignorar_chave_invalida_400`, `test_ignorar_exige_mesma_origem`.
- [ ] **Step 2–4:** `cd painel_agentes && py -3.11 -m unittest test_aviso_pendencias_tela test_acesso_telas` RED → GREEN.
- [ ] **Step 5: Commit** `Painel: API do aviso de pendências (lista por perfil e ignorar)`.

### Task 4: Faixas na tela

**Files:** Create `painel_agentes/templates/_aviso_pendencias.html`. Modify `_menu_lateral_script.html` (no `buscar()`, depois de `pintar`: `document.dispatchEvent(new CustomEvent("contadores-atualizados", {detail: d}))`), `base.html` e `base_mobile.html` (include no topo do `<main>`, antes do `{% block conteudo %}`), `MAPA_DO_SISTEMA.txt`.

O partial: CSS (faixa vermelha `#fdecee`/borda `#e5484d`, laranja `#fff4e5`/borda `#f0a020`, contraste AA, padding ≥ 8px), contêiner `#aviso-pendencias`, modal de confirmação (texto exato: título "Ignorar este aviso?", corpo "Os {N} itens continuam esperando. O aviso volta em 30 min.", botões "Voltar" e "Ignorar 30 min"), JS que escuta `contadores-atualizados`, descarta o aviso cujo `destino` é a página atual, desenha até 3 (2 se `document.body.dataset.mobile` — marcar no `base_mobile.html`), "+ N pendências · ver todas" que expande/recolhe, **Verificar** = `location.href = destino`, **Ignorar** = modal → `POST` (com o mesmo header que o painel usa para `exige_mesma_origem`) → remove a faixa na hora. Texto via `textContent` (sem `innerHTML` com dado). Acessível: faixas em `role="region" aria-label="Pendências"`, botões focáveis, modal com foco no "Voltar" e Esc fecha.

- [ ] **Step 1:** Implementar o partial e os includes.
- [ ] **Step 2: Conferir no navegador** (o worktree não tem o arquivo de segredos: usar uma página harness no scratchpad que inclui o partial renderizado à mão + `BASE_PATH` e um mock de `/api/sidebar/contadores` com 5 avisos, ou o runner de testes do painel com config falsa, como na Task 7 do Galpão 3D). Playwright: 3 faixas + "+ 2 pendências", vermelha primeiro; clicar Ignorar → modal → confirmar → faixa some e o POST foi feito; página `/atendimento` não mostra a faixa de chamados; viewport 390px com `data-mobile` mostra 2. Zero erros no console. Foto.
- [ ] **Step 3: Commit** `Painel: faixas de pendência no topo de todas as telas (Verificar/Ignorar)` com o MAPA.

---

## Parte 2 — tela de solicitações do portal

### Task 5: Sondas (só leitura)

**Files:** Create `portal_cliente/sondar_solicitacoes.py` (scratch commitável, sem efeito).

- [ ] **Step 1:** Na VPS, como www-data, dentro da trava (`adquirir(..., esperar_segundos=300)`): abrir `outbound/show/{id}` de um pedido em "Waiting for Carrier" e extrair o formulário do botão **Pausar** (action, método, campos, `_token`) — sem enviar. Registrar no relatório.
- [ ] **Step 2:** Na documentação/código da Vuupt do repo (memória `reference_vuupt_api_publica_stoplight`, `vuupt_client.py`, `painel_agentes/expedicao.py:396`), descobrir como tirar **um** serviço de uma rota sem cancelar (desatribuir) e se existe na API pública; sem chamada de escrita.
- [ ] **Step 3:** Relatório com o caminho de cada ação; commit só do script (`Portal: sonda do Pausar da Stokki e do desatribuir da Vuupt (só leitura)`).

### Task 6: Executores

**Files:** Create `portal_cliente/solicitacoes_execucao.py`, `portal_cliente/test_solicitacoes_execucao.py`.

**Interfaces — Produces:** `executar(conn, solicitacao_id: int, deps: Dependencias) -> dict` (`{"ok": bool, "mensagem": str}`), onde `Dependencias` é um dataclass com callables injetáveis: `cancelar_vuupt(service_id)` (→ `roteirizacao.cancelar_servico.cancelar_servico_completo`), `cancelar_stokki(id_stokki, motivo)` (→ `stokki.cancelar.cancelar_pedido`), `pausar_stokki(id_stokki, motivo)` (Task 5), `tirar_da_rota_vuupt(service_id)` (Task 5), `reagendar_vuupt(service_id, data, ini, fim)` (→ `painel_agentes.planejamento_rotas.reagendar_pedido`), `buscar_servico(codigo)`. Só depois de **todas** as etapas do tipo darem certo chama `envio_pedidos.concluir_solicitacao(conn, id, resposta)` com texto automático em português; qualquer falha → `ok False` com o motivo e nada concluído. `recusar(conn, solicitacao_id, resposta) -> None` → `concluir_solicitacao(..., recusada=True)`. Funções de produção `dependencias_reais(config)` montam as callables dentro da trava da Stokki (`sessao_uso.adquirir(..., esperar_segundos=60)`; sem vez → `ok False, "Stokki ocupada, tente em instantes"`).

- [ ] **Step 1: Testes que falham** (fakes): `test_cancelar_ok_conclui`, `test_cancelar_vuupt_recusa_nao_conclui`, `test_em_espera_tira_da_rota_e_pausa`, `test_em_espera_falha_no_pausar_nao_conclui`, `test_reagendar_aplica_data_e_conclui`, `test_stokki_ocupada_nao_conclui`, `test_recusar_marca_recusada`, `test_solicitacao_ja_concluida_recusa_executar`.
- [ ] **Step 2–4:** `py -3.11 -m unittest portal_cliente.test_solicitacoes_execucao` RED → GREEN (rodar da raiz; não misturar com testes do painel).
- [ ] **Step 5: Commit** `Portal: executar solicitações do cliente (cancelar, em espera, reagendar)`.

### Task 7: Tela `/solicitacoes-portal`

**Files:** Modify `painel_agentes/painel_agentes.py` (rotas), `painel_agentes/acesso_telas.py` (`TELAS` += `("solicitacoes_portal", "Pedidos do portal", "Acompanhar", [r"^/solicitacoes-portal$", r"^/api/solicitacoes-portal"])`), `_menu_lateral_nav.html` (badge? não — o aviso já cobra), `MAPA_DO_SISTEMA.txt`. Create `templates/solicitacoes_portal.html`, `painel_agentes/test_solicitacoes_portal_tela.py`. Conferir que o destino do aviso `solicitacoes_portal` aponta pra cá.

**Interfaces:** `GET /solicitacoes-portal`, `GET /api/solicitacoes-portal` (lista de `envio_pedidos.listar_solicitacoes_pendentes` + embarcador/PS/tipo/detalhe/criado_em), `POST /api/solicitacoes-portal/<id>/executar`, `POST /api/solicitacoes-portal/<id>/recusar` `{"resposta"}`; níveis `("total","operador","logistica")`; POST com `@exige_mesma_origem`. Executar roda em thread? **Não**: chamada síncrona com timeout de 60 s (é ação pontual de um operador); a tela mostra "Executando…" e o resultado.

- [ ] **Step 1: Testes que falham:** `test_lista`, `test_executar_chama_executor` (mock), `test_recusar_exige_resposta`, `test_nivel_atendimento_bloqueado`, `test_acesso_telas` continua verde.
- [ ] **Step 2–4:** `cd painel_agentes && py -3.11 -m unittest test_solicitacoes_portal_tela test_acesso_telas` RED → GREEN.
- [ ] **Step 5: Commit** `Painel: tela de pedidos do portal (executar ou recusar a solicitação do cliente)`.

### Task 8: Deploy (só quando o Hugo pedir)

- [ ] Skill `deploy-vps`: `vps_ops.py deploy --restart painel-agentes`. Prova: `vps_ler.py saude`, `curl` de `/api/sidebar/contadores` sem sessão → 401; com `venv/bin/python -c` chamar `avisos_pendencias.montar(...)` com os contadores reais e conferir os números contra as telas; journal do painel sem Traceback por 10 min; medir o tempo do `/api/sidebar/contadores` antes/depois (não pode passar de ~1 s).
- [ ] Tela nova: executar **uma** solicitação real só com o Hugo acompanhando.
