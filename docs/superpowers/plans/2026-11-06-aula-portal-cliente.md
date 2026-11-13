# Aula do portal do cliente — plano de construção

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Página pública `cliente/aula` com 11 vídeos curtos do portal, gerados por um robô Playwright com legenda, destaque de clique e narração da Thalita, sem tocar Stokki nem Vuupt.

**Architecture:** Duas partes independentes. (1) No portal: rota `/aula` que lê um manifesto `aula.json` e serve os mp4/jpg de `dados/aula/`. (2) Fora do portal, em `aula_portal/`: um gerador que monta uma cópia descartável do repo (worktree) com config e banco fictícios, sobe o portal ali, roda o roteiro de cada vídeo no Playwright interceptando o que dependeria de Stokki/Vuupt, gera a narração por passo e junta tudo no ffmpeg.

**Tech Stack:** Python 3.11 (`py -3.11`), Flask (portal existente), Playwright sync API, edge-tts (`pt-BR-ThalitaMultilingualNeural`), ffmpeg via `imageio_ffmpeg.get_ffmpeg_exe()`, unittest.

**Spec:** `docs/superpowers/specs/2026-11-06-aula-portal-cliente-design.md`

## Global Constraints

- Trabalhar num ramo `aula-portal` criado de `origin/master`, em worktree próprio (a master local está atrás e suja com edições de outras sessões). Nunca `git add -A`; commit/push só quando o Hugo pedir.
- Python sempre `py -3.11`. Testes com `py -3.11 -m unittest`. Testes de `portal_cliente` rodam como `portal_cliente.test_x` a partir da raiz do worktree; nunca no mesmo comando que `painel_agentes`.
- Voz: `pt-BR-ThalitaMultilingualNeural`. Viewport da gravação: 1280x720.
- Cliente fictício: nome **"Mercado Exemplo"**, CNPJ `47821930000170` (era o 00000000000191 do cliente teste; trocado na revisão final de 12/11, ele entra em produção com o PIN padrão). Destinatários, endereços, NFs, motoristas: todos inventados.
- O PIN padrão de produção (`123456`) nunca aparece em roteiro, legenda, fala, dado de teste ou vídeo. PIN da gravação: `482915`, só no banco fictício, digitado em campo mascarado.
- Nada da gravação pode alcançar Stokki, Vuupt, SMTP, WhatsApp, Claude ou `app.freshhub.com.br`.
- Templates com `url_for()`, nunca caminho absoluto (ProxyFix com prefixo `/cliente`).
- Vídeos 3 (alteração da entrega) e 10 (notificações) ficam com `publicar=False` até o Hugo confirmar que a alteração está ligada para todos e os 8 avisos chegam aos clientes.
- O vídeo 3 diz explicitamente que o cliente **pede** a alteração e a equipe da Fresh trata. Nunca "altera".
- Mensagens de commit: `Área: o que mudou e por quê`, em português.
- Criou arquivo/rotina/tela: atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit.

## Review Focus

1. Vídeo pedido com `Range` (iPhone/Safari só toca mp4 com resposta 206): `/aula/arquivo/<nome>` precisa responder 206 com `Content-Range`.
2. Nome de arquivo fora do manifesto ou com `..`/barra em `/aula/arquivo/`: 404, nunca lê fora de `dados/aula/`.
3. Manifesto ausente ou JSON quebrado na VPS: `/aula` abre 200 com "Os vídeos estão sendo preparados", sem 500.
4. Cliente logado com PIN padrão ainda não trocado (`trocar_pin=1`) abrindo `/aula`: abre a aula, não redireciona para `/trocar-pin`.
5. Fala mais longa que a ação do passo (ou ação mais longa que a fala): a próxima fala nunca começa antes de a anterior terminar, e a tela não avança antes de a fala do passo acabar.

---

### Task 1: Página `/aula` no portal

**Files:**
- Create: `portal_cliente/aula.py`, `portal_cliente/templates/aula.html`, `portal_cliente/test_aula.py`
- Modify: `portal_cliente/app.py` (rotas + `_LIVRES_ANTES_DA_TROCA`), `portal_cliente/templates/base_cliente.html` (botão "Aula" ao lado de `#btn-regioes`), `portal_cliente/templates/login.html` (link "Aprenda a usar o portal" junto dos `.links`), `MAPA_DO_SISTEMA.txt` (seção 11 e lista de telas)

**Interfaces:**
- Produces (contrato do manifesto, usado pela Task 3): arquivo `aula.json` em `PASTA_AULA`:
  `{"gerado_em": "AAAA-MM-DDTHH:MM", "videos": [{"numero": int, "slug": str, "titulo": str, "resumo": str, "duracao_s": int, "video": "<slug>.mp4", "capa": "<slug>.jpg"}]}`. Só entram vídeos publicados, em ordem de `numero`.
- Produces: `aula.PASTA_AULA: Path` (= `_RAIZ / "dados" / "aula"`), `aula.carregar_manifesto(pasta: Path = PASTA_AULA) -> list[dict]` (lista vazia se ausente/inválido), `aula.arquivo_permitido(nome: str, videos: list[dict]) -> bool` (só nomes exatos de `video`/`capa` do manifesto).
- Rotas em `app.py`: `@app.route("/aula")` endpoint `aula`; `@app.route("/aula/arquivo/<nome>")` endpoint `aula_arquivo` (`send_from_directory(..., conditional=True)`, `Cache-Control: public, max-age=3600`). Ambas sem `requer_cliente`, e `"aula", "aula_arquivo"` entram em `_LIVRES_ANTES_DA_TROCA`.

- [ ] **Step 0: Worktree** — `git worktree add ../agente_stokki_eventos-aula -b aula-portal origin/master` e trabalhar lá. Conferir `git status --short` limpo.

- [ ] **Step 1: Testes que falham** em `portal_cliente/test_aula.py` (carregar o app como `test_alterar_entrega_api.py` faz, com `open` falso para `config.yaml`; `mock.patch.object(aula, "PASTA_AULA", tmp)`):
  - `test_aula_abre_sem_login`: GET `/aula` com manifesto de 2 vídeos → 200, os dois títulos no HTML, na ordem de `numero`.
  - `test_aula_sem_manifesto`: pasta vazia → 200 e o texto "Os vídeos estão sendo preparados".
  - `test_manifesto_quebrado`: `aula.json` com `{` → 200, mesmo texto.
  - `test_arquivo_do_manifesto_serve_com_range`: GET `/aula/arquivo/acompanhar-o-dia.mp4` com `Range: bytes=0-9` → 206, 10 bytes, `Content-Range` presente.
  - `test_arquivo_fora_do_manifesto_404`: `outro.mp4` existente na pasta mas fora do manifesto → 404; `..%2Fconfig.yaml` → 404.
  - `test_cliente_com_troca_pendente_abre_aula`: sessão de cliente com `trocar_pin=1` (montar como os testes de troca de PIN fazem) → `/aula` 200, não 302.
  - `test_login_tem_link_da_aula`: GET `/login` contém `href` para `url_for("aula")`.

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest portal_cliente.test_aula` → FAIL (`No module named 'aula'` / 404).

- [ ] **Step 3: Implementar** `aula.py`, as duas rotas e `aula.html`. Página independente (não estende `base_cliente.html`, como `regioes.html`): logo (`url_for("regioes_logo")`), título "Aprenda a usar o portal", subtítulo de uma linha, player grande no topo tocando o vídeo escolhido (`<video controls preload="metadata" poster=...>`), lista com capa, número, título, resumo e duração (`m:ss`); clicar troca o vídeo do player e o `#n` da URL; ao terminar um vídeo, oferece o próximo. Botão "Entrar no portal" (`url_for("login")`). Paleta pelo partial `_paleta_cores.html` (claro/escuro), como o resto do portal. No celular (≤ 600px), lista em uma coluna sem rolagem lateral. Botão no cabeçalho do portal: mesma classe `botao-cab`, rótulo "Aula", ícone de play, `href="{{ url_for('aula') }}"`, `target="_blank"`.

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest portal_cliente.test_aula` → OK. Depois toda a suíte do portal: `py -3.11 -m unittest discover -s portal_cliente -p "test_*.py"` (a partir da raiz do worktree) → sem falhas novas em relação ao `origin/master`.

- [ ] **Step 5: Conferir no navegador** — portal do worktree na porta 8099 (`PORTAL_CLIENTE_DEV=1`, com o config fictício da Task 2 ou um mínimo só com `secret_key`), manifesto de exemplo em `dados/aula/` com um mp4 qualquer; screenshot Playwright em 1280x720 e 390x844; os dois sem rolagem lateral e com o player tocando.

- [ ] **Step 6: MAPA + commit** (só se o Hugo autorizou) — `git add portal_cliente/aula.py portal_cliente/templates/aula.html portal_cliente/test_aula.py portal_cliente/app.py portal_cliente/templates/base_cliente.html portal_cliente/templates/login.html MAPA_DO_SISTEMA.txt`; `Portal: pagina publica /aula com os videos de treinamento do cliente`.

---

### Task 2: Ambiente de gravação (config, banco fictício e portal isolado)

**Files:**
- Create: `aula_portal/__init__.py`, `aula_portal/dados_teste.py`, `aula_portal/ambiente.py`, `aula_portal/fixtures/` (XML de NF-e fictícios, `canhoto.png`), `aula_portal/test_ambiente.py`
- Modify: `.gitignore` (`aula_portal/saida/`)

**Interfaces:**
- Produces `dados_teste`: `CNPJ = "47821930000170"`, `NOME = "Mercado Exemplo"`, `SENDER_ID: int` (fictício), `PIN_GRAVACAO = "482915"`; `dia(hoje: date) -> dict` no formato de `dados_cliente.montar_dia` (chaves `data, data_rotulo, hoje, atualizado_em, kpis, pedidos, atencao, aguardando_saida, proximos_dias, agendados_futuros, historico`, mais `empresas` e `alteracao_entrega: True` como `api_dia` devolve), com pedidos nas situações `entregue`, `em_rota`, `programado`, `aguardando_saida` e `insucesso` (este com item em `atencao`); `alteracao(service_id: int) -> dict` (formato do GET `/api/pedidos/<id>/alteracao`, conferido em `alterar_entrega.py`); `atendimento(etapa: str) -> dict` (respostas roteirizadas de `/api/atendimento/*` para o vídeo 9); `xmls_nfe() -> list[Path]` (2 NF-e fictícias válidas para a prévia e 1 com erro); `exportacao_xlsx() -> bytes`.
- Produces `ambiente`: `ESTADOS = ("primeiro_acesso", "padrao")`; `preparar_worktree(destino: Path, ref: str = "HEAD") -> Path` (worktree `--detach`; recria se existir); `escrever_config(raiz: Path) -> Path` (config.yaml mínimo: `portal_cliente.secret_key` aleatório e `portal_cliente.alteracao_entrega` ligada para `CNPJ`; **sem** `email`, `vuupt_api`, `stokki*`, `freshhub_api`, `anthropic`/Claude, WhatsApp); `semear_banco(raiz: Path, estado: str) -> Path` (apaga e recria `raiz/dados/dados.db`, cria as tabelas chamando o `conectar()` de cada módulo do portal com `DB_PATH` apontado para lá, e insere: cliente com `PIN_GRAVACAO`; em `primeiro_acesso`, `trocar_pin=1` e pré-cadastro aberto; em `padrao`, cadastro completo, `interno` com `stkkc_id` fictício, envios em `CRIADO`, `ERRO`, `EM_ESPERA_CLIENTE` e um cancelável, preferências de notificação); `subir_portal(raiz: Path, porta: int) -> subprocess.Popen` (sobe `py -3.11 -c "import app; app.app.run(host='127.0.0.1', port=porta)"` de dentro de `raiz/portal_cliente`, env com `PORTAL_CLIENTE_DEV=1`, `HTTP_PROXY`/`HTTPS_PROXY=http://127.0.0.1:9`, `NO_PROXY=127.0.0.1,localhost`; espera `/saude` responder; nunca reaproveita processo já existente); `PORTA_PADRAO = 8097`.

- [ ] **Step 1: Testes que falham** em `aula_portal/test_ambiente.py`:
  - `test_config_sem_credenciais`: `yaml.safe_load` do config escrito não tem nenhuma das chaves `email, vuupt_api, stokki, stokki_api, freshhub_api, anthropic, claude, whatsapp, openwa, lalamove`; tem `portal_cliente.secret_key` com ≥ 32 caracteres.
  - `test_banco_padrao_tem_so_o_cliente_ficticio`: após `semear_banco(tmp, "padrao")`, `clientes_portal` tem uma linha só, com `cnpj == CNPJ`; `auth.verificar` (ou a função de login de `auth_cliente`) aceita `PIN_GRAVACAO` e recusa `"123456"`.
  - `test_banco_primeiro_acesso`: `trocar_pin == 1` e `cadastro_embarcador.precisa_completar(conn, CNPJ)` verdadeiro.
  - `test_dia_tem_todas_as_situacoes`: `{p["situacao"] for p in dia(date(2026,11,6))["pedidos"]} >= {"entregue","em_rota","programado","aguardando_saida","insucesso"}` e todo `insucesso` tem código em algum `atencao[i]["codigos"]`.
  - `test_dados_sem_pin_padrao`: `"123456"` não aparece em `json.dumps` de `dia`, `alteracao`, `atendimento` de todas as etapas, nem no texto dos XMLs.
  - `test_xmls_passam_na_previa`: as 2 NF-e boas passam no parser do `envio_pedidos` (função usada por `/api/envios/analisar`) sem erro; a terceira volta com erro.

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest aula_portal.test_ambiente` → FAIL.

- [ ] **Step 3: Implementar** `dados_teste.py` e `ambiente.py` pelas interfaces acima. Formatos copiados do código real (`dados_cliente.montar_dia`, `alterar_entrega`, `chamados_web`), não inventados.

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest aula_portal.test_ambiente` → OK.

- [ ] **Step 5: Prova manual** — `preparar_worktree` num diretório do scratchpad, `escrever_config`, `semear_banco(..., "padrao")`, `subir_portal(..., 8097)`; entrar com CNPJ + `PIN_GRAVACAO` num Playwright comum, abrir a aba "Enviar pedidos" (lista de envios aparece) e a janela de Notificações. Derrubar só o processo que você subiu.

- [ ] **Step 6: Commit** (só se autorizado) — `Aula: ambiente de gravacao com config e banco ficticios, sem Stokki/Vuupt`.

---

### Task 3: Motor de gravação (narração, robô, montagem, manifesto)

**Files:**
- Create: `aula_portal/roteiro.py`, `aula_portal/narracao.py`, `aula_portal/overlay.js`, `aula_portal/gravar.py`, `aula_portal/test_motor.py`

**Interfaces:**
- Consumes: Task 2 (`ambiente.*`, `dados_teste.*`); contrato do manifesto da Task 1.
- Produces `roteiro`: `@dataclass Passo(fala: str, acoes: list[tuple], legenda: str | None = None)` (legenda padrão = fala); `@dataclass Video(numero: int, slug: str, titulo: str, resumo: str, estado: str, passos: list[Passo], intercepta: dict[str, Callable] = {}, publicar: bool = True)`; `VIDEOS: list[Video]` (preenchida nas Tasks 4–6). Vocabulário de ações: `("ir", caminho)`, `("clicar", seletor)`, `("digitar", seletor, texto)`, `("arquivo", seletor, [Path])`, `("esperar", seletor)`, `("rolar", seletor)`, `("cartao", titulo, subtitulo)`, `("pausa", segundos)`.
- Produces `narracao`: `VOZ = "pt-BR-ThalitaMultilingualNeural"`; `gerar(texto: str, pasta_cache: Path) -> Path` (mp3 com cache por hash de voz+texto); `duracao(mp3: Path) -> float` (lida do `Duration:` do ffmpeg).
- Produces `gravar`: `classificar_url(url: str, porta: int) -> str` (`"local"` para `127.0.0.1:<porta>`/`localhost:<porta>`, `data:`, `blob:`; `"permitido"` para `fonts.googleapis.com`, `fonts.gstatic.com`, `cdnjs.cloudflare.com`, `cdn.jsdelivr.net`; senão `"proibido"`); `linha_do_tempo(inicios: list[float], duracoes: list[float]) -> list[float]` (normaliza para que cada fala comece ≥ fim da anterior); `comando_ffmpeg(webm: Path, falas: list[tuple[Path, float]], saida: Path) -> list[str]` (`adelay` por fala + `amix` + H.264/AAC + `-movflags +faststart`); `gravar_video(video: Video, raiz: Path, porta: int, saida: Path) -> dict` (entrada do manifesto); `escrever_manifesto(videos: list[Video], saida: Path) -> Path` (só `publicar=True` com mp4 existente); CLI `py -3.11 -m aula_portal.gravar --video N | --todos | --manifesto`, saída em `aula_portal/saida/`.
- Comportamento fixado de `gravar_video`: semeia o banco com `video.estado` e sobe o portal (um processo por vídeo, derrubado no fim); contexto Playwright 1280x720 com `record_video_dir`; `context.route("**/*")`: URLs `proibido` são abortadas e registradas, `video.intercepta` (padrão de URL → handler que faz `route.fulfill`) responde as chamadas montadas; `overlay.js` via `add_init_script` (funções `window.aula.legenda(texto)`, `window.aula.destacar(seletor)` com círculo pulsando + cursor movendo até o alvo, `window.aula.cartao(titulo, sub)` em tela cheia); antes de cada `clicar` em link com `target="_blank"`, remover o `target` para tudo ficar na mesma aba; digitação com `delay` de ~70 ms por tecla; para cada passo: marcar o início, mostrar a legenda, executar as ações, esperar até `inicio + duracao(fala) + 0,4 s`. Login é feito por passo de roteiro (vídeo 1) ou por ações invisíveis antes do primeiro cartão (demais vídeos), cortadas pelo `-ss` do ffmpeg. **Se houve qualquer URL `proibido`, a gravação falha** e lista as URLs. Capa = quadro do primeiro cartão.

- [ ] **Step 1: Testes que falham** em `aula_portal/test_motor.py`:
  - `test_classificar_url`: `http://127.0.0.1:8097/api/dia` → local; `https://fonts.gstatic.com/x.woff2` → permitido; `https://api.vuupt.com/...`, `https://freshlog.stokki.com.br/...`, `https://app.freshhub.com.br/cliente/` → proibido; `http://127.0.0.1:8100/x` → proibido (porta errada).
  - `test_linha_do_tempo_sem_sobreposicao`: `linha_do_tempo([0, 2, 3], [4, 1, 1]) == [0, 4, 5]`; `linha_do_tempo([0, 10], [2, 2]) == [0, 10]`.
  - `test_comando_ffmpeg`: contém `adelay=4000|4000` para fala em 4,0 s, `amix=inputs=2`, `libx264`, `aac`, `+faststart`.
  - `test_manifesto_so_publicados`: 3 vídeos (um `publicar=False`, um sem mp4) → manifesto com 1, no formato da Task 1.
  - `test_roteiros_sem_pin_padrao`: nenhum `fala`/`legenda`/texto de `digitar` em `VIDEOS` contém `123456`.
  - `test_roteiros_numeracao`: `VIDEOS` tem números únicos e slugs únicos; quando completo (fim da Task 6), números `1..11`.
  - `test_video3_pede_nao_altera`: o vídeo 3 contém "pedido" ou "pedir" na fala e não contém "você altera"/"alterar sozinho".
  - `test_narracao_cache` (sem rede): com `edge_tts` mockado, gerar o mesmo texto duas vezes chama a síntese uma vez.

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest aula_portal.test_motor` → FAIL.

- [ ] **Step 3: Implementar** `roteiro.py`, `narracao.py`, `overlay.js`, `gravar.py` pelas interfaces. Legenda: faixa escura semitransparente embaixo, fonte ≥ 22px, máx. 2 linhas. Cartão: fundo na cor da marca do portal, título grande, subtítulo "Portal do cliente Fresh Log".

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest aula_portal.test_motor` → OK.

- [ ] **Step 5: Vídeo de prova** — escrever em `VIDEOS` o vídeo 11 (`regioes-e-dias`, estado `padrao`, sem intercepta): cartão → login invisível → clicar "Dias de atendimento" → rolar a página de regiões → trocar de mês → cartão final "Pronto! Esse foi o último vídeo". `py -3.11 -m aula_portal.gravar --video 11` → mp4 + jpg em `aula_portal/saida/`, sem URL proibida. Extrair 4 quadros (`ffmpeg -ss <t> -frames:v 1`) e olhar: legenda legível, destaque no lugar certo. Ouvir o início: fala começa junto com o cartão.

- [ ] **Step 6: Commit** (só se autorizado) — `Aula: gerador de videos do portal (Playwright + narracao + ffmpeg)`.

---

### Tasks 4–6: Roteiros dos vídeos

Comum às três: cada vídeo é um `Video` em `aula_portal/roteiro.py`, de 1 a 3 min, começando com `("cartao", titulo, ...)` e terminando com cartão "No próximo vídeo: <título do próximo>" (o 11 fecha a aula). Seletores tirados dos templates reais do worktree. Falas em português simples, frases curtas, tratando o cliente por "você". Cada task termina com: `py -3.11 -m unittest aula_portal.test_motor` OK; `--video N` de cada vídeo sem URL proibida; 4 quadros extraídos e olhados por vídeo (nenhum erro na tela, nada com `123456`, nenhum nome que não seja fictício); duração conferida em 60–180 s; commit (só se autorizado) `Aula: roteiros dos videos N, M...`.

### Task 4: Vídeos 1, 2, 4 (acesso, acompanhar o dia, canhoto e planilha)

**Files:** Modify `aula_portal/roteiro.py`.

- [ ] **Vídeo 1 `primeiro-acesso`** (estado `primeiro_acesso`, sem login invisível): login com CNPJ (`#cnpj`) e o PIN em `#pin` ("digite o PIN que você recebeu da Fresh"), tela de troca do PIN, completar cadastro (tipo de carga, e-mails, WhatsApp), chegada no acompanhamento. Última fala: "Se esquecer o PIN, use o link Primeiro acesso / esqueci o PIN na tela de entrada." Sem clicar no link (ele mandaria e-mail).
- [ ] **Vídeo 2 `acompanhar-o-dia`** (intercepta `**/api/dia*` → `dados_teste.dia(hoje)`): números do topo, abas (em rota, entregues, pendentes, insucesso), linha de um pedido com histórico de tentativas, escolher colunas, trocar o dia.
- [ ] **Vídeo 4 `canhoto-e-planilha`** (intercepta `/api/dia*`, `**/api/canhoto/*` → `fixtures/canhoto.png`, `**/exportar.xlsx*` → `exportacao_xlsx()`): clicar "Canhoto" num entregue (mesma aba, voltar), clicar exportar; legenda/cartão curto "Planilha baixada" (download não aparece no vídeo).
- [ ] Verificação da task (comum acima).

### Task 5: Vídeos 5, 7, 8 (enviar, cancelar, retomar)

**Files:** Modify `aula_portal/roteiro.py`.

Estes rodam de verdade contra o banco fictício (nenhum worker ligado, então nada vai à Stokki). Interceptar só `**/api/envios/cep/*` com o CEP do fixture.

- [ ] **Vídeo 5 `enviar-pedidos`**: aba "Enviar pedidos", subir os 2 XMLs bons + o com erro (`("arquivo", ...)`), ler a prévia, apontar o erro e o que fazer, confirmar; pedidos aparecem "Na fila"; mencionar a planilha-modelo (`/api/envios/modelo-planilha`) sem baixar; "Reenviar com erro" num envio em `ERRO`.
- [ ] **Vídeo 7 `cancelar-pedido`**: abrir um envio cancelável, "Cancelar", ler a confirmação, confirmar; explicar que pedido ainda não criado cancela na hora e o já criado a Fresh cancela e ele acompanha ali.
- [ ] **Vídeo 8 `retomar-em-espera`**: aba "Pendências", envio `EM_ESPERA_CLIENTE`, "Retomar entrega", escolher a opção, confirmar; mostrar o "Tirar da rota" em outro envio só apontando, sem clicar.
- [ ] Verificação da task (comum acima), mais: depois de cada gravação, `portal_envios` do banco fictício mudou como esperado (prova de que passou pelo portal real).

### Task 6: Vídeos 3, 6, 9, 10 (alteração, insucesso, chat, notificações)

**Files:** Modify `aula_portal/roteiro.py`.

- [ ] **Vídeo 3 `pedir-alteracao`** (`publicar=False`; intercepta `/api/dia*`, GET e POST `**/api/pedidos/*/alteracao` → `dados_teste.alteracao(sid)` e `{"ok": true, "chamado": {...}}`): "Pedir alteração" num programado, abrir endereço/data/horário, preencher a data, enviar. Fala obrigatória: "Você não muda a entrega sozinho: o pedido vai para a nossa equipe, que confirma com você pelo chat."
- [ ] **Vídeo 6 `responder-insucesso`** (intercepta `/api/dia*` e `**/api/responder` → `{"ok": true, ...}` no formato que `acompanhamento.html` espera): cartão de atenção, as três opções (reentregar, outra data, não reenviar), escolher "Escolher outra data" e confirmar.
- [ ] **Vídeo 9 `chat-e-chamados`** (intercepta `**/api/atendimento/**` com `dados_teste.atendimento(etapa)`, avançando etapa a cada chamada): abrir o widget, assistente pergunta a área, cliente digita a dúvida, "Deixar chamado", resposta da equipe chegando (no próximo poll do estado).
- [ ] **Vídeo 10 `notificacoes`** (`publicar=False`; roda contra o banco fictício): abrir a janela de Notificações, passar pelos 8 tipos de `preferencias_notificacao.TIPOS` com uma frase cada, ligar/desligar um, trocar o e-mail, salvar.
- [ ] Verificação da task (comum acima), mais: `test_roteiros_numeracao` agora exige `1..11`.

---

### Task 7: Gravar tudo, revisar e entregar ao Hugo

**Files:** nenhum novo.

- [ ] **Step 1:** `py -3.11 -m aula_portal.gravar --todos` → 11 mp4 + 11 jpg, nenhuma URL proibida, `aula.json` com 9 vídeos (sem 3 e 10).
- [ ] **Step 2:** Revisão por quadros de todos os vídeos (4 por vídeo) e conferência de duração. Listar ao Hugo os caminhos dos 11 mp4 para ele assistir.
- [ ] **Step 3:** Revisão do ramo (`superpowers:requesting-code-review`), com atenção à Task 1 (rota pública nova).
- [ ] **Step 4 (só quando o Hugo pedir deploy):** commit + push do ramo e merge, deploy pela skill `deploy-vps` (`systemctl restart portal-cliente`); `scp` de `aula_portal/saida/*.mp4 *.jpg aula.json` para `/opt/stokki-eventos/dados/aula/`; `chown www-data:www-data` e `chmod 640`; prova: `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8074/aula` → 200 e `curl -s -r 0-9 -o /dev/null -w "%{http_code}" http://127.0.0.1:8074/aula/arquivo/<slug>.mp4` → 206.
- [ ] **Step 5 (quando o Hugo liberar):** vídeos 3 e 10 → `publicar=True`, `--manifesto`, `scp` só do `aula.json`.
