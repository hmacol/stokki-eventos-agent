# Homologação do legado — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Subir painel e portal numa bancada `homolog.freshhub.com.br` com código do ramo em teste, banco copiado uma vez e uma guarda central que impede qualquer coisa de sair (e registra o que teria saído).

**Architecture:** Pasta nova `homologacao/` no repo, inerte sem `FRESH_AMBIENTE=homolog`. Um `sitecustomize` (via `PYTHONPATH` do unit) instala a guarda antes de qualquer import e é herdado pelos subprocessos. A guarda troca `smtplib`, `imaplib`, o adapter do `requests`, o transporte do `httpx`, o Playwright e o `socket`, e grava cada bloqueio na tabela `homolog_envios`. Na VPS: checkout `/opt/stokki-homolog` do usuário `homolog`, units 8171/8174, Caddy com `basic_auth`, `nft` por usuário e scripts `criar/subir/ok/recopiar`.

**Tech Stack:** Python 3.11, Flask + waitress, `requests` 2.34, `httpx` (dependência do `anthropic` 1.4), Playwright 1.44, SQLite, systemd, Caddy, nftables, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-02-homologacao-design.md` (APROVADO 10/11). Ler as seções 5 (D1-D9) e 6 antes de cada task.

## Global Constraints

- Variável de ambiente: `FRESH_AMBIENTE=homolog`. Sem ela, **nada** do que este plano cria pode mudar comportamento (todo teste de "produção não muda" roda sem a variável).
- Raízes fixas: produção `/opt/stokki-eventos`, homologação `/opt/stokki-homolog`. Usuário Linux `homolog`. Portas `8171` (painel) e `8174` (portal). Endereço `https://homolog.freshhub.com.br/painel/` e `/cliente/`.
- Liberado pela guarda (spec D4, P2/P3): `api.vuupt.com` GET/HEAD com ≤ 1 req/s por processo; `maps.googleapis.com` GET; `storage.googleapis.com` GET/HEAD; `oauth2.googleapis.com` só `POST /token`; loopback `:8100` só GET; `viacep.com.br` e `brasilapi.com.br` GET; `api.anthropic.com` qualquer método. OpenWA (loopback `:2785`) = **resposta falsa registrada**. Todo o resto = registra e bloqueia.
- Conteúdo registrado: até 64 KB por envio (`conteudo[:65536]`).
- CNPJ permitido no portal por padrão: `00000000000191` (cliente teste). Config: `homologacao.portal_cnpjs_permitidos`.
- Idioma: português nos nomes, comentários e mensagens; strings de log sem acento (padrão do repo). Mensagem ao usuário: **"indisponível na homologação"** / **"desligado na homologação"**.
- Ramo `homologacao`, criado de `origin/master` num worktree (a master local está suja e atrás). **Commits locais por task sim; push, merge, deploy e qualquer ação na VPS só quando o Hugo pedir.** `git add` só dos arquivos da task.
- Nunca ler, imprimir nem copiar valores do arquivo de config (local ou VPS), nem no código de teste. O `gerar_config.py` imprime só nomes de chaves.
- Python local: `py -3.11`. Testes: `unittest`. Testes do painel rodam de dentro de `painel_agentes/`. Nunca misture `nucleo`/`roteirizacao` com `painel_agentes` no mesmo comando.
- `.claude/` é gitignored: `vps_ops.py`, `vps_ler.py` e as skills são editados no checkout principal `c:\agente_stokki_eventos`, não no worktree, e não entram em commit.

## Review Focus

1. **Import já ligado antes da guarda** (`from smtplib import SMTP`, `from playwright.sync_api import sync_playwright` feitos depois do `instalar()`): o nome importado precisa ser a versão falsa. → teste em Task 2 e Task 3.
2. **Redirecionamento** de um GET liberado para host proibido (Vuupt/GCS respondendo 302 para outro domínio): o salto precisa ser bloqueado, não seguido. Por isso a guarda fica no `HTTPAdapter.send` (vê cada salto), não no `Session.request`. → teste em Task 2.
3. **Variantes de host e porta** (`API.VUUPT.COM`, `api.vuupt.com:443`, `localhost:2785`, `127.0.0.2:2785`, `[::1]:2785`, `http://127.0.0.1:8100` com POST): todas caem na regra certa. → teste em Task 2.
4. **Banco travado** (transação longa do WMS) na hora de registrar: o envio continua bloqueado, a requisição não vira 500 e o registro cai no `dados/homolog_envios.jsonl`. → teste em Task 1.
5. **Subprocesso disparado por caminho que não é o executor** (thread, `subprocess.run` solto): o filho nasce com a guarda e o SMTP dele é registrado. → teste em Task 3.
6. **Ramo sem a pasta `homologacao/`** (criado antes deste trabalho): o serviço não pode subir sem guarda. → `ExecStartPre` dos units e checagem do `subir.sh` (Task 6), provados na Task 8, Step 3 (subir um commit antigo de propósito: o unit fica `failed` e nada responde).

---

### Task 1: Ambiente e registro de envios

**Files:**
- Create: `homologacao/__init__.py` (vazio), `homologacao/ambiente.py`, `homologacao/registro.py`
- Test: `homologacao/test_ambiente.py`, `homologacao/test_registro.py`

**Interfaces:**
- Produces (`homologacao/ambiente.py`):
  - `RAIZ_PRODUCAO = Path("/opt/stokki-eventos")`, `RAIZ_HOMOLOG = Path("/opt/stokki-homolog")`, `VARIAVEL = "FRESH_AMBIENTE"`
  - `class DesligadoNaHomologacao(RuntimeError)`
  - `homolog_ativo() -> bool` (lê `os.environ` a cada chamada)
  - `exigir_coerencia(raiz: Path) -> None`: levanta `SystemExit` com mensagem em 3 casos: (a) ativo e `raiz.resolve() == RAIZ_PRODUCAO`; (b) ativo e `sys.modules.get("homologacao.guarda")` ausente ou com `INSTALADA` falso; (c) `raiz.resolve() == RAIZ_HOMOLOG` e não ativo.
  - `recusar_na_homologacao(o_que: str) -> None`: se ativo, levanta `DesligadoNaHomologacao(f"{o_que} desligado na homologação")`.
  - `cnpjs_portal_permitidos() -> set[str]`: lê `homologacao.portal_cnpjs_permitidos` do arquivo de config da raiz do repo (cache por processo); padrão `{"00000000000191"}`; normaliza só dígitos.
  - `informacao_versao(raiz: Path) -> dict` com `ramo`, `commit`, `master`, `quando` (de `dados/HOMOLOG_VERSAO`, JSON) e `copia` (de `dados/HOMOLOG_COPIA`, texto); valores `"?"` quando o arquivo falta.
- Produces (`homologacao/registro.py`):
  - `CAMINHO_BANCO: Path` = `os.environ["HOMOLOG_REGISTRO_BANCO"]` quando existe (só para testes, inclusive os de subprocesso da Task 3), senão `<raiz>/dados/dados.db` (raiz = pai de `homologacao/`). `registrar_envio` lê o atributo do módulo a cada chamada. O `.jsonl` de reserva fica na mesma pasta do banco.
  - `registrar_envio(canal: str, metodo: str | None, destino: str | None, resumo: str | None, conteudo: str | bytes | None, origem: str | None = None) -> int | None`. Nunca levanta. Grava em `CAMINHO_BANCO`, `timeout=5`, tabela criada na hora:
    `homolog_envios(id INTEGER PRIMARY KEY AUTOINCREMENT, quando TEXT NOT NULL, canal TEXT NOT NULL, metodo TEXT, destino TEXT, resumo TEXT, conteudo TEXT, origem TEXT, pid INTEGER)`.
    `conteudo` truncado em 65536 caracteres (bytes decodificados com `errors="replace"`). `origem` padrão = primeiro frame fora de `homologacao/` como `modulo.py:linha`. Em `sqlite3.Error`, escreve uma linha JSON em `<raiz>/dados/homolog_envios.jsonl` e devolve `None`.
  - `listar_envios(canal: str | None = None, limite: int = 200) -> list[dict]` (mais novos primeiro).
  - `buscar_envio(envio_id: int) -> dict | None`.

- [ ] **Step 1: Write the failing tests**

`test_ambiente.py` (`mock.patch.dict(os.environ, ...)` e `mock.patch.dict(sys.modules, ...)`):
`test_inativo_sem_variavel` (`homolog_ativo() is False`); `test_coerencia_aborta_homolog_na_raiz_de_producao` (com `Path.resolve` patcheado para `RAIZ_PRODUCAO`, `assertRaises(SystemExit)`); `test_coerencia_aborta_sem_guarda`; `test_coerencia_aborta_raiz_homolog_sem_variavel`; `test_coerencia_ok_em_producao_sem_variavel` (raiz qualquer, sem variável, não levanta); `test_recusar_levanta_so_quando_ativo` (mensagem exata `"Agentes desligado na homologação"`); `test_cnpjs_padrao_e_cliente_teste` (config sem a seção → `{"00000000000191"}`); `test_informacao_versao_sem_arquivos_devolve_interrogacao`.

`test_registro.py` (banco temporário via `registro.CAMINHO_BANCO`):
`test_registra_e_lista` (grava `email`, lista devolve o item com `canal == "email"` e `pid == os.getpid()`); `test_trunca_conteudo_em_64kb` (`len(conteudo) == 65536`); `test_bytes_viram_texto`; `test_origem_aponta_para_quem_chamou` (termina com `test_registro.py:<n>`); `test_banco_travado_cai_no_jsonl` (outra conexão segura `BEGIN EXCLUSIVE`, com `timeout` do registro patcheado para `0.1`; retorno `None`, o `.jsonl` ganha 1 linha com `"canal": "email"` e nada levanta).

- [ ] **Step 2: Run to verify they fail**

Run: `py -3.11 -m unittest homologacao.test_ambiente homologacao.test_registro -v` (da raiz do worktree)
Expected: FAIL com `ModuleNotFoundError: No module named 'homologacao.ambiente'`

- [ ] **Step 3: Implement `homologacao/ambiente.py` and `homologacao/registro.py`** with the interfaces above.

- [ ] **Step 4: Run to verify they pass**

Run: o mesmo comando. Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add homologacao/__init__.py homologacao/ambiente.py homologacao/registro.py homologacao/test_ambiente.py homologacao/test_registro.py
git commit -m "Homologacao: ambiente (coerencia, recusa) e registro de envios bloqueados"
```

---

### Task 2: Guarda de rede (SMTP, IMAP, requests)

**Files:**
- Create: `homologacao/guarda.py`
- Test: `homologacao/test_guarda_rede.py`

**Interfaces:**
- Consumes: `registro.registrar_envio` (Task 1).
- Produces (`homologacao/guarda.py`):
  - `class EnvioBloqueadoHomolog(requests.exceptions.ConnectionError)` (mensagem começa com `"indisponível na homologação:"`)
  - `INSTALADA: bool`, `CAMADAS: list[str]` (cada camada instalada acrescenta o nome: `"smtp"`, `"imap"`, `"requests"`, depois `"httpx"`, `"playwright"`, `"socket"` na Task 3)
  - `decidir(metodo: str, url: str) -> str` → `"passa"`, `"falso_openwa"` ou `"bloqueia"`. Função pura com as regras de Global Constraints. Normaliza o host em minúsculas, tira a porta padrão (80/443) e trata `localhost`, `127.0.0.0/8` e `::1` como loopback.
  - `canal_de(url: str) -> str` → `stokki` (`freshlog.stokki.com.br`), `vuupt`, `whatsapp` (loopback 2785), `lalamove` (`*.lalamove.com`), `gcs`, `freshhub` (loopback 8100/8101), `supabase` (`*.supabase.co`, `freshhub.com.br`), `anthropic`, `google`, `outro`
  - `instalar() -> None` (idempotente) e `desinstalar() -> None` (restaura os originais; só para testes)
  - Interno, mas fixado aqui: `SMTPFalso` substitui `smtplib.SMTP` e `smtplib.SMTP_SSL`. Aceita `(host="", port=0, *a, **k)` e context manager. `starttls/ehlo/login/quit/close/set_debuglevel` não fazem nada. `send_message(msg, from_addr=None, to_addrs=None)` e `sendmail(from_addr, to_addrs, msg)` registram `canal="email"`, `destino=", ".join(to_addrs ou cabeçalhos To/Cc)`, `resumo=Subject`, `conteudo=msg.as_string()` e devolvem `{}`.
  - `imaplib.IMAP4` / `IMAP4_SSL` → registram `canal="imap"` e levantam `EnvioBloqueadoHomolog`.
  - `requests.adapters.HTTPAdapter.send(self, request, **kw)` embrulhado. `"passa"` chama o original (para `vuupt`, depois de `_esperar_vez_vuupt()`: lock + intervalo mínimo de 1,0 s por processo). `"falso_openwa"` registra (`canal="whatsapp"`, destino = `chatId` do corpo JSON, `resumo` = primeira linha de `text`) e devolve um `requests.Response` com status 201, `url`, `request` e corpo `{"messageId": "homolog-<id do registro>", "exists": true}`. `"bloqueia"` registra (`canal=canal_de(url)`, `metodo`, `destino=url`, `conteudo=request.body`) e levanta `EnvioBloqueadoHomolog`.

- [ ] **Step 1: Write the failing tests** (cada teste faz `guarda.instalar()` no `setUp` e `guarda.desinstalar()` no `tearDown`; `registro.CAMINHO_BANCO` num temporário; rede real nunca é tocada: o adapter original é trocado por um `mock` que devolve 200)

- `test_decidir_regras`: tabela com, no mínimo:
  `("GET","https://api.vuupt.com/api/v1/services")→passa`, `("PUT","https://api.vuupt.com/api/v1/services/1")→bloqueia`, `("GET","https://API.VUUPT.COM:443/x")→passa`, `("GET","https://freshlog.stokki.com.br/pt-br/x")→bloqueia`, `("POST","http://127.0.0.1:2785/sessions/s/messages/send-text")→falso_openwa`, `("GET","http://localhost:2785/sessions/s/contacts/check/1")→falso_openwa`, `("POST","http://127.0.0.2:2785/x")→falso_openwa`, `("POST","http://[::1]:2785/x")→falso_openwa`, `("GET","http://127.0.0.1:8100/api/pool")→passa`, `("POST","http://127.0.0.1:8100/api/eventos")→bloqueia`, `("GET","http://127.0.0.1:8101/api/pool")→bloqueia`, `("POST","https://oauth2.googleapis.com/token")→passa`, `("POST","https://oauth2.googleapis.com/outra")→bloqueia`, `("GET","https://storage.googleapis.com/b/o")→passa`, `("POST","https://storage.googleapis.com/upload/storage/v1/b/x/o")→bloqueia`, `("GET","https://maps.googleapis.com/maps/api/geocode/json")→passa`, `("POST","https://rest.lalamove.com/v3/orders")→bloqueia`, `("GET","https://viacep.com.br/ws/01001000/json/")→passa`, `("get","https://api.vuupt.com/x")→passa`.
- `test_smtp_registra_e_finge_sucesso`: `email_utils.enviar_email(["a@b.com"], "Assunto X", "<p>oi</p>", {"remetente": "x@y.com"})` devolve `True`; `registro.listar_envios("email")[0]` tem `destino == "a@b.com"`, `resumo == "Assunto X"` e `"<p>oi</p>"` no conteúdo.
- `test_smtp_importado_por_nome_depois_de_instalar`: `from smtplib import SMTP` (feito no teste, depois do `instalar`) `is guarda.SMTPFalso`.
- `test_imap_bloqueia`: `imaplib.IMAP4_SSL("imap.gmail.com", 993)` levanta `EnvioBloqueadoHomolog`; 1 registro `canal == "imap"`.
- `test_whatsapp_resposta_falsa`: `integracao_openwa.enviar_texto({"base_url": "http://127.0.0.1:2785", "api_key": "HOMOLOG-SEM-CHAVE", "sessao": "s"}, "5511999999999@c.us", "linha1\nlinha2")` devolve `(True, "homolog-<id>")`; registro `canal == "whatsapp"`, `destino == "5511999999999@c.us"`, `resumo == "linha1"`. `integracao_openwa.numero_existe(...)` devolve `True`.
- `test_stokki_bloqueia_qualquer_metodo`: `requests.get("https://freshlog.stokki.com.br/pt-br/administrator")` levanta `EnvioBloqueadoHomolog`, que é instância de `requests.ConnectionError`; registro `canal == "stokki"`.
- `test_vuupt_escrita_bloqueia_e_leitura_passa`: `requests.put(...api.vuupt.com...)` levanta erro e registra `canal == "vuupt"`, `metodo == "PUT"`; `requests.get(...)` chega no adapter original (mock chamado 1 vez).
- `test_vuupt_ritmo_um_por_segundo`: 3 GETs seguidos com `time.monotonic`/`time.sleep` patcheados → `sleep` chamado com somas ≥ 2,0 s.
- `test_redirecionamento_para_host_proibido_bloqueia`: o adapter original (mock) devolve 302 `Location: https://evil.example/x` para o primeiro GET liberado; `requests.get(...)` termina em `EnvioBloqueadoHomolog` e o registro tem `destino` começando por `https://evil.example`.
- `test_desinstalar_restaura`: depois de `desinstalar()`, `smtplib.SMTP` é a classe original e `guarda.INSTALADA is False`.

- [ ] **Step 2: Run to verify they fail**

Run: `py -3.11 -m unittest homologacao.test_guarda_rede -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'homologacao.guarda'`

- [ ] **Step 3: Implement the SMTP/IMAP/requests layers in `homologacao/guarda.py`**

- [ ] **Step 4: Run to verify they pass**

Run: `py -3.11 -m unittest homologacao.test_guarda_rede homologacao.test_registro homologacao.test_ambiente -v`. Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add homologacao/guarda.py homologacao/test_guarda_rede.py
git commit -m "Homologacao: guarda de SMTP, IMAP e requests (so leitura na Vuupt, WhatsApp falso registrado)"
```

---

### Task 3: Guarda de processo (httpx, Playwright, socket) e sitecustomize

**Files:**
- Modify: `homologacao/guarda.py`
- Create: `homologacao/site/sitecustomize.py`
- Test: `homologacao/test_guarda_processo.py`

**Interfaces:**
- Consumes: `guarda.instalar`, `decidir`, `canal_de`, `EnvioBloqueadoHomolog` (Task 2); `ambiente.homolog_ativo` (Task 1).
- Produces:
  - `instalar()` passa a trocar também:
    - `httpx.HTTPTransport.handle_request` e `httpx.AsyncHTTPTransport.handle_async_request`: liberado só `api.anthropic.com`; o resto registra e levanta `httpx.ConnectError("indisponível na homologação: <url>")`.
    - `playwright.sync_api.sync_playwright` e `playwright.async_api.async_playwright`: registram `canal="playwright"` e levantam `EnvioBloqueadoHomolog`.
    - `socket.getaddrinfo`: nome fora de `NOMES_PERMITIDOS = {"api.vuupt.com", "maps.googleapis.com", "storage.googleapis.com", "oauth2.googleapis.com", "api.anthropic.com", "viacep.com.br", "brasilapi.com.br", "localhost"}` registra `canal="socket"` e levanta `socket.gaierror`. Os IPs resolvidos vão para `_IPS_RESOLVIDOS`.
    - `socket.socket.connect` / `connect_ex`: `AF_UNIX` passa. Loopback (v4 ou v6) só na porta 8100. Qualquer outro IP só se estiver em `_IPS_RESOLVIDOS`. Fora disso, registra e levanta `ConnectionRefusedError`.
  - `homologacao/site/sitecustomize.py`: se `ambiente.homolog_ativo()`, põe a raiz do repo (avô do arquivo) no `sys.path` e chama `guarda.instalar()`. Sem a variável, não faz nada. Qualquer exceção na instalação **aborta o processo** (`raise SystemExit`): falha fechada.

- [ ] **Step 1: Write the failing tests**

- `test_httpx_bloqueia_fora_da_anthropic`: `httpx.get("https://example.com")` levanta `httpx.ConnectError`; registro `canal == "outro"`. Com `handle_request` original mockado, `httpx.Client().post("https://api.anthropic.com/v1/messages")` chega no mock.
- `test_playwright_importado_depois_bloqueia`: `from playwright.sync_api import sync_playwright` (depois do `instalar`); chamar levanta `EnvioBloqueadoHomolog`; registro `canal == "playwright"`.
- `test_socket_nome_fora_da_lista`: `socket.getaddrinfo("freshlog.stokki.com.br", 443)` levanta `socket.gaierror`.
- `test_socket_loopback_so_8100`: `socket.create_connection(("127.0.0.1", 2785))` levanta `ConnectionRefusedError`; o mesmo para `("::1", 2785)`; `("127.0.0.1", 8100)` chega no `connect` original (mockado).
- `test_socket_ip_literal_nao_resolvido`: `connect(("8.8.8.8", 443))` levanta `ConnectionRefusedError`.
- `test_subprocesso_herda_guarda`: `subprocess.run([sys.executable, "-c", "import smtplib; s=smtplib.SMTP('smtp.gmail.com',587); s.sendmail('a@x','b@y','Subject: filho\n\nx')"], env={**os.environ, "FRESH_AMBIENTE": "homolog", "PYTHONPATH": "<worktree>/homologacao/site"}, ...)` sai com código 0 e o banco temporário (passado por variável `HOMOLOG_REGISTRO_BANCO`, lida por `registro` se existir) ganha 1 registro `canal == "email"`, `resumo == "filho"`.
- `test_sitecustomize_sem_variavel_nao_instala`: subprocesso com `PYTHONPATH` do site e **sem** `FRESH_AMBIENTE` imprime `smtplib.SMTP.__name__ == "SMTP"`.
- [ ] **Step 2: Run to verify they fail**

Run: `py -3.11 -m unittest homologacao.test_guarda_processo -v`. Expected: FAIL (httpx/playwright/socket não trocados; `sitecustomize` não existe)

- [ ] **Step 3: Implement the three layers and `homologacao/site/sitecustomize.py`**

- [ ] **Step 4: Run all homologacao tests**

Run: `py -3.11 -m unittest discover -s homologacao -p "test_*.py" -t . -v`. Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add homologacao/guarda.py homologacao/site/sitecustomize.py homologacao/test_guarda_processo.py
git commit -m "Homologacao: guarda de httpx, Playwright e socket + sitecustomize herdado pelos subprocessos"
```

---

### Task 4: Flask da homologação (faixa, erros, saúde, tela de envios) e ligação no painel e no portal

**Files:**
- Create: `homologacao/flask_homolog.py`, `homologacao/templates/envios.html`
- Modify: `painel_agentes/painel_agentes.py` (depois dos `sys.path.insert` das linhas 29-31: coerência; depois da definição de `requer_auth`, linha 251: `instalar`)
- Modify: `portal_cliente/app.py` (depois do `sys.path.insert` das linhas ~37-38: coerência; depois do `app.config.update(...)`: `instalar`)
- Test: `homologacao/test_flask_homolog.py`, `painel_agentes/test_homolog_painel.py`

**Interfaces:**
- Consumes: `ambiente.*`, `registro.listar_envios/buscar_envio`, `guarda.INSTALADA/CAMADAS/EnvioBloqueadoHomolog`.
- Produces: `flask_homolog.instalar(app: Flask, *, servico: str, requer_auth=None) -> None`, com `servico` em `"painel"` ou `"portal"`:
  - `after_request`: em resposta `text/html` sem `direct_passthrough`, injeta depois da primeira tag `<body…>` uma `<div id="faixa-homologacao">` (fundo `#f97316`, texto branco, `position:sticky;top:0;z-index:2147483647`) com o texto `HOMOLOGAÇÃO · {ramo} @ {commit[:7]} · dados de {copia}`, e troca `<title>` por `<title>[HOMOLOG] `. Ajusta `Content-Length`.
  - `errorhandler(DesligadoNaHomologacao)` → 409 e `errorhandler(EnvioBloqueadoHomolog)` → 503. Devolve JSON `{"erro": "<mensagem>"}` quando o caminho contém `/api/` ou o `Accept` pede JSON; senão, HTML simples com a mensagem.
  - `GET /homologacao/saude` (sem login): JSON `{"ambiente": "homolog", "guarda": "ativa"|"inativa", "camadas": CAMADAS, "ramo", "commit", "master", "copia"}`.
  - Só no painel: `GET /homologacao/envios` (`requer_auth()` padrão = só total), com filtro `?canal=` e lista dos últimos 200; e `GET /homologacao/envios/<int:envio_id>`, que mostra o detalhe e, para e-mail, o HTML dentro de `<iframe sandbox srcdoc>`.
- Ligação (as duas apps): `from homologacao import ambiente as _ambiente` + `_ambiente.exigir_coerencia(_RAIZ)` no topo; `if _ambiente.homolog_ativo(): from homologacao import flask_homolog; flask_homolog.instalar(app, servico=..., requer_auth=...)`.
- As rotas novas entram em `FORA_DO_CATALOGO` de `painel_agentes/test_acesso_telas.py` (são do total e só existem na homologação).

- [ ] **Step 1: Write the failing tests**

`test_flask_homolog.py` (app Flask mínima com uma rota HTML, outra JSON e outras que levantam os dois erros; `guarda` com `INSTALADA=True` patcheado):
`test_faixa_entra_no_html` (corpo contém `faixa-homologacao` e `<title>[HOMOLOG] `); `test_faixa_nao_entra_no_json`; `test_desligado_vira_409_json_na_api`; `test_bloqueado_vira_503_html_fora_da_api` (corpo contém `indisponível na homologação`); `test_saude_responde_sem_login` (`json["guarda"] == "ativa"`); `test_envios_exige_login_no_painel` (com `requer_auth` falso que devolve 401 → 401); `test_envios_lista_registro` (1 registro gravado aparece com o `resumo`).

`painel_agentes/test_homolog_painel.py`: `test_producao_sem_variavel_nao_tem_rotas_nem_faixa` (importa o painel como `test_acesso_telas` faz, sem a variável; `/homologacao/saude` → 404 e o login HTML não contém `faixa-homologacao`).

- [ ] **Step 2: Run to verify they fail**

Run: `py -3.11 -m unittest homologacao.test_flask_homolog -v` e, de dentro de `painel_agentes/`, `py -3.11 -m unittest test_homolog_painel test_acesso_telas -v`
Expected: FAIL (`flask_homolog` não existe)

- [ ] **Step 3: Implement `flask_homolog.py`, the template and the wiring in both apps**

- [ ] **Step 4: Run to verify they pass**

Mesmos comandos, mais `py -3.11 -m unittest portal_cliente.test_grupos -v` (o portal continua importando). Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add homologacao/flask_homolog.py homologacao/templates/envios.html homologacao/test_flask_homolog.py painel_agentes/painel_agentes.py painel_agentes/test_homolog_painel.py painel_agentes/test_acesso_telas.py portal_cliente/app.py
git commit -m "Homologacao: faixa, erros amigaveis, /homologacao/saude e tela de envios bloqueados no painel e no portal"
```

---

### Task 5: Recusas no executor, no disparo e no login do portal, e as telas que liam a Stokki

**Files:**
- Modify: `painel_agentes/executor.py` (`iniciar_execucao` :183, `iniciar_sequencia` :210, `iniciar_execucao_com_fila` :288)
- Modify: `painel_agentes/notificacoes_tela.py` (`iniciar_disparo` :396)
- Modify: `portal_cliente/auth_cliente.py` (`autenticar` :337)
- Modify (só se o teste mostrar 500): `painel_agentes/torre_controle.py` (`contar_sem_rota`), `painel_agentes/consulta_expedicao.py` (`consultar`), `painel_agentes/pedidos_parados_triagem.py` (`verificar_na_stokki` :1357), `portal_cliente/solicitacoes_execucao.py` (`executar` :239)
- Test: `painel_agentes/test_homolog_recusas.py`, `portal_cliente/test_homolog_portal.py`

**Interfaces:**
- Consumes: `ambiente.recusar_na_homologacao`, `ambiente.cnpjs_portal_permitidos`, `guarda.EnvioBloqueadoHomolog`, `flask_homolog.instalar`.
- Produces: primeira linha dos três do executor: `ambiente.recusar_na_homologacao("Agentes")`; de `iniciar_disparo`: `ambiente.recusar_na_homologacao("Disparo de notificação")` (a rota já transforma `RuntimeError` em 409, `painel_agentes.py:2178`); de `autenticar`: se ativo e `normalizar_cnpj(cnpj)` fora de `cnpjs_portal_permitidos()`, levanta `AutenticacaoInvalida("Este CNPJ não tem acesso à homologação.", 403)` **antes** de mexer em tentativas.

- [ ] **Step 1: Write the failing tests** (com `FRESH_AMBIENTE=homolog` por `mock.patch.dict`, guarda não instalada e `ambiente.exigir_coerencia` patcheado para não abortar)

`test_homolog_recusas.py` (de dentro de `painel_agentes/`): `test_executor_recusa_os_tres` (`assertRaises(DesligadoNaHomologacao)` em cada um, sem `Popen` chamado); `test_disparo_recusa` (`iniciar_disparo` com conexão temporária levanta e não cria linha de disparo); `test_telas_stokki_nao_dao_500`: app do painel com `flask_homolog.instalar(...)`, login total simulado como os testes existentes fazem, `stokki.auth.StokkiSession` patcheado para levantar `EnvioBloqueadoHomolog`. As rotas de `painel_agentes.py:1021` (consulta), `:2383` (verificar na Stokki), `:2774` (contar sem rota) e `:845` (executar solicitação) devolvem status ≠ 500, e o corpo contém `indisponível na homologação` ou a mensagem de "Stokki fora" que a tela já tem.

`portal_cliente/test_homolog_portal.py`: `test_cnpj_fora_da_lista_recusado` (403, mensagem exata, `tentativas_pin` não muda); `test_cliente_teste_entra` (CNPJ `00000000000191` com PIN certo autentica); `test_sem_variavel_qualquer_cnpj` (produção inalterada).

- [ ] **Step 2: Run to verify they fail**

Run: de `painel_agentes/`: `py -3.11 -m unittest test_homolog_recusas -v`; da raiz: `py -3.11 -m unittest portal_cliente.test_homolog_portal -v`. Expected: FAIL

- [ ] **Step 3: Implement the refusals; for any Stokki screen still returning 500, catch `EnvioBloqueadoHomolog` (or `requests.ConnectionError`) where the screen already handles "Stokki fora"**

- [ ] **Step 4: Run to verify they pass**, mais a suíte existente de quem foi tocado: `test_acesso_telas`, `test_solicitacoes_portal_tela` (de `painel_agentes/`) e `portal_cliente.test_grupos`. Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add painel_agentes/executor.py painel_agentes/notificacoes_tela.py portal_cliente/auth_cliente.py painel_agentes/test_homolog_recusas.py portal_cliente/test_homolog_portal.py
# + os arquivos de tela que o Step 3 precisou tocar
git commit -m "Homologacao: agentes e disparo desligados, portal so do cliente teste, telas da Stokki sem 500"
```

---

### Task 6: Config gerado, cópia de dados e infraestrutura da VPS (arquivos no repo)

**Files:**
- Create: `homologacao/gerar_config.py`, `homologacao/test_gerar_config.py`
- Create: `homologacao/vps/criar.sh`, `copiar_dados.sh`, `subir.sh`, `ok.sh`, `recopiar.sh`, `trocar_senha.sh`
- Create: `infra/homolog/homolog-painel.service`, `infra/homolog/homolog-portal.service`, `infra/homolog/legado-homolog.caddy`, `infra/homolog/homolog.nft`
- Modify: `MAPA_DO_SISTEMA.txt` (seções 1, 3, 16 e 17), `CLAUDE.md` (bloco "Homologação": comando, regra "deploy = produção", "nada sai")

**Interfaces:**
- Produces:
  - `gerar_config.gerar(prod: dict, *, url_base_painel: str, url_base_portal: str, caminho_gcs: str) -> tuple[dict, list[str], list[str]]` → `(config_homolog, chaves_removidas, chaves_mantidas)`. Remove, em qualquer nível, as chaves cujo nome casa com `re.compile(r"senha|token|secret|api_key|api_secret|password|credenciais", re.I)`. Depois, reinsere da produção **só** a lista fixa `SEGREDOS_LIBERADOS = ["vuupt_api.token", "google_maps.api_key", "anthropic.api_key", "freshhub_api.token", "gcs.credenciais_json"]` (`gcs.credenciais_json` recebe `caminho_gcs`). Gera `painel_agentes.secret_key` e `portal_cliente.secret_key` novos (`secrets.token_hex(32)`). Põe `whatsapp_notificacoes.api_key = "HOMOLOG-SEM-CHAVE"` quando a seção existe (sem isso, `integracao_openwa.configurado()` desiste antes da guarda e nada fica registrado). Troca toda `url_base` que comece com `https://app.freshhub.com.br/` pelo mesmo caminho em `https://homolog.freshhub.com.br/`. Acrescenta `homologacao: {portal_cnpjs_permitidos: ["00000000000191"]}`.
  - CLI `python -m homologacao.gerar_config --origem <arq> --destino <arq> --caminho-gcs <arq>`: grava o destino com `0600` e imprime **só** `removidas:` / `mantidas:` com nomes pontuados. Nunca imprime valores.
  - Scripts `homologacao/vps/*.sh` (bash, `set -euo pipefail`, rodam como root, cada passo dá `echo "== <passo>"`):
    - `copiar_dados.sh`: para os 2 units; faz o backup SQLite como `www-data` (`sudo -u www-data /opt/stokki-eventos/venv/bin/python -c "sqlite3.connect('file:…/dados/dados.db?mode=ro', uri=True).backup(sqlite3.connect(TMP))"`, com `TMP` em `mktemp` 600); `rsync -a` de `dados/` e `roteirizacao/dados/` com `--exclude` para `dados.db*`, `*.log`, `logs/`, `sessao_*.json`, `*service*account*`, `*.bak*` e `eventos_freshhub_pendentes.jsonl`; move, faz `chown -R homolog:homolog`, grava `dados/HOMOLOG_COPIA` (`date '+%d/%m/%Y %H:%M'`) e sobe os units.
    - `criar.sh` (idempotente): `useradd --system --home /opt/stokki-homolog homolog`; clona o repositório com a mesma origem da produção; `python3.11 -m venv` + `pip install -r requirements.txt` + `playwright` **sem** `install` de browser; roda `gerar_config` (copia o arquivo da conta de serviço com 600); `copiar_dados.sh`; copia os units e faz `daemon-reload` + `enable --now`; backup `Caddyfile.bak-<dd-mm>-antes-homolog`, copia o `.caddy`, acrescenta `import /etc/caddy/legado-homolog.caddy` se faltar, `caddy validate` e `systemctl reload caddy`; gera a senha (`openssl rand -base64 15`) em `/root/homolog-senha-caddy.txt` (600) e o hash via `caddy hash-password` no `.caddy`; instala `/etc/nftables.d/homolog.nft` e `nft -f`. **Recusa se `getent hosts homolog.freshhub.com.br` não devolver 187.127.52.197.**
    - `subir.sh <ramo> <quem>`: se `HOMOLOG_VERSAO` tem outro ramo, imprime qual, desde quando e quem subiu, e sai com código 3 (o `vps_ops` pede confirmação e chama de novo com `--trocar`). Faz `git -c safe.directory=/opt/stokki-homolog fetch origin` como root, `checkout --detach --force origin/<ramo>` e `-c user.name=homolog -c user.email=homolog@localhost merge --no-edit origin/master` (conflito → `merge --abort`, sai com 4). `pip install` se o sha256 de `requirements.txt` mudou (guardado em `dados/HOMOLOG_REQ_SHA`). Esvazia `dados/eventos_freshhub_pendentes.jsonl`, faz `chown`, grava `HOMOLOG_VERSAO` (JSON: `ramo`, `commit` = ponta do ramo, `master`, `quando`, `quem`), acrescenta `SUBIU <ramo> <commit> <quando> <quem>` em `HOMOLOG_HISTORICO`, reinicia os 2 units, roda `curl -fsS http://127.0.0.1:8171/homologacao/saude` e `http://127.0.0.1:8174/homologacao/saude` (direto no waitress não há prefixo; o `handle_path` do Caddy o tira), exige `"guarda": "ativa"` nas duas e imprime os 2 links públicos.
    - `ok.sh`: lê `HOMOLOG_VERSAO` e acrescenta `OK <ramo> <commit> <quando>`.
    - `recopiar.sh --confirmo-recopia`: sem a flag, sai com 2. Copia `dados/dados.db` para `dados/dados.db.anterior` e chama `copiar_dados.sh`.
    - `trocar_senha.sh`: gera senha nova, troca o hash no `.caddy`, `caddy validate` e `reload`.
  - Units: como os de produção, mais `User=homolog`, `Group=homolog`, `UMask=0027`, `Environment=FRESH_AMBIENTE=homolog`, `Environment=PYTHONPATH=/opt/stokki-homolog/homologacao/site`, `Environment=PYTHONIOENCODING=utf-8`, `MemoryMax=600M`, `WorkingDirectory` e `ExecStart` em `/opt/stokki-homolog` com as portas 8171/8174. **Trava que não depende do código do ramo:** `ExecStartPre=/opt/stokki-homolog/venv/bin/python -c "import smtplib, socket, sys; sys.exit(0 if smtplib.SMTP.__name__ == 'SMTPFalso' else 1)"` (mesmo ambiente do unit). Se o checkout não tiver a guarda (ramo antigo, `sitecustomize` quebrado), o serviço **não sobe**. O `subir.sh` também confere `test -f homologacao/site/sitecustomize.py` depois da mescla e **antes** do restart (sai com 5 se faltar).
  - `legado-homolog.caddy`: bloco `homolog.freshhub.com.br` com `basic_auth { homolog <HASH> }`, os `redir /painel /painel/` e `/cliente /cliente/`, `handle_path /painel/*` → `127.0.0.1:8171` (`header_up X-Forwarded-Prefix /painel`), `/cliente/*` → `:8174` e `header X-Robots-Tag "noindex"`.
  - `homolog.nft`: `table inet homolog { chain saida { type filter hook output priority 0; meta skuid != "homolog" accept; ct state established,related accept; udp dport 53 accept; tcp dport 53 accept; ip daddr 127.0.0.1 tcp dport 8100 accept; ip daddr 127.0.0.0/8 drop; ip6 daddr ::1 drop; tcp dport { 25, 143, 465, 587, 993 } drop; } }`

- [ ] **Step 1: Write the failing tests** (`test_gerar_config.py`, só dicionários inventados, nunca o arquivo real)

`test_remove_segredos_em_qualquer_nivel` (`{"email": {"senha_app": "x"}, "lalamove": {"api_secret": "y"}}` → nenhuma das duas no resultado e ambas em `removidas`); `test_reinsere_so_os_liberados` (`vuupt_api.token` e `anthropic.api_key` voltam; `stokki.senha` não); `test_secret_keys_novos_e_diferentes_da_producao`; `test_openwa_ganha_chave_falsa`; `test_url_base_vira_homolog` (`https://app.freshhub.com.br/cliente` → `https://homolog.freshhub.com.br/cliente`); `test_secao_homologacao_com_cliente_teste`; `test_cli_nao_imprime_valores` (roda o `main` com origem temporária contendo o valor `"VALOR-SECRETO-123"` e `assertNotIn` na saída capturada); `test_cli_grava_0600` (só fora do Windows: `skipIf(os.name == "nt")`).

- [ ] **Step 2: Run to verify they fail**

Run: `py -3.11 -m unittest homologacao.test_gerar_config -v`. Expected: FAIL

- [ ] **Step 3: Implement `gerar_config.py`, os scripts, units, Caddy e nft; atualizar MAPA e CLAUDE.md**

- [ ] **Step 4: Verify**

Run: `py -3.11 -m unittest homologacao.test_gerar_config -v` → `OK`; `bash -n homologacao/vps/*.sh` (Git Bash) → sem saída; `py -3.11 -m py_compile homologacao/*.py`. A validação do `.caddy` e do `.nft` só acontece no `criar.sh`, na VPS.

- [ ] **Step 5: Commit**

```bash
git add homologacao/gerar_config.py homologacao/test_gerar_config.py homologacao/vps infra/homolog MAPA_DO_SISTEMA.txt CLAUDE.md
git commit -m "Homologacao: config sem segredos, copia do banco e scripts/units/Caddy/nft da VPS"
```

---

### Task 7: Comandos locais e skills (fora do git)

**Files (no checkout principal `c:\agente_stokki_eventos`, gitignored; sem commit):**
- Modify: `.claude/scripts/vps_ops.py` (subcomando `homolog`), `.claude/scripts/vps_ler.py` (subcomando `homolog-status`)
- Create: `.claude/skills/homologar/SKILL.md`
- Modify: `.claude/skills/deploy-vps/SKILL.md`

**Interfaces:**
- `vps_ops.py homolog criar | subir <ramo> | ok | recopiar --confirmo-recopia | trocar-senha`. Cada um pede a confirmação que o `vps_ops` já pede e roda `bash /opt/stokki-homolog/homologacao/vps/<script>.sh` (o `criar` roda a partir de uma cópia `scp` do script, porque o checkout ainda não existe). `subir` checa localmente, antes, que `git rev-parse origin/<ramo>` existe e é igual a `git rev-parse <ramo>`. Saída 3 do script → mostra quem ocupa e pergunta "trocar?" → chama de novo com `--trocar`.
- `vps_ler.py homolog-status`: `cat` de `HOMOLOG_VERSAO`, `tail -5` do `HOMOLOG_HISTORICO`, `systemctl is-active homolog-painel homolog-portal`, `curl` nas duas `/homologacao/saude`.
- Skill `homologar` (gatilhos: "sobe pra homologação", "homolog", "manda pra bancada", "ok na homologação"): `subir` exige ramo pushado (o push é decisão do Hugo); "ok"/"aprovado" depois de testar → `homolog ok`; **nunca** propõe `recopiar` por conta própria; senha do Caddy nunca vai para o chat.
- Passo novo no `deploy-vps`, antes do pull: para os commits de `origin/master` que ainda não estão na VPS e que tocam `painel_agentes/`, `portal_cliente/` ou um arquivo do fecho de imports (o script usado no levantamento fica em `.claude/scripts/fecho_imports.py`), conferir se cada um é ancestral (`git merge-base --is-ancestor`) de algum commit com linha `OK` no `HOMOLOG_HISTORICO`. Se faltar, listar e pedir confirmação. **Não trava.**

- [ ] **Step 1:** `py -3.11 .claude/scripts/vps_ops.py homolog subir --help` e `py -3.11 .claude/scripts/vps_ler.py homolog-status --help` → sem erro de parse.
- [ ] **Step 2:** `py -3.11 .claude/scripts/vps_ops.py homolog subir ramo-que-nao-existe --dry-run` → recusa local, sem SSH.
- [ ] **Step 3:** Ler as duas skills de volta e conferir os gatilhos (sem commit; `.claude/` é gitignored).

---

### Task 8: Criação na VPS e prova (só com o "pode criar" do Hugo e o registro A no ar)

Antes de começar: o Hugo confirma o registro A **e** pede explicitamente a criação. O ramo `homologacao` precisa estar mergeado em `origin/master` e deployado em produção (as mudanças de `painel_agentes.py`, `portal_cliente/app.py`, `executor.py` e `auth_cliente.py` não fazem nada sem a variável, mas precisam estar no código que a homologação clona). Seguir a skill `deploy-vps` para isso.

- [ ] **Step 1:** `py -3.11 .claude/scripts/vps_ops.py homolog criar`. Expected: todos os `== passo` sem erro, `caddy validate` ok e `nft list table inet homolog` mostrando a cadeia.
- [ ] **Step 2:** `py -3.11 .claude/scripts/vps_ops.py homolog subir master`. Expected: `"guarda": "ativa"` nas duas saúdes e os 2 links impressos.
- [ ] **Step 3: Prova "nada sai"** (cada item tem que aparecer em `/homologacao/envios` e **não** aparecer do lado de fora):
  - e-mail: um chamado aberto pelo portal (cliente teste) → registro `email`; caixa do destinatário vazia.
  - WhatsApp: a ação que avisa o grupo de atendimento → registro `whatsapp`; grupo sem mensagem.
  - Vuupt: "marcar como entregue" num serviço de teste → registro `vuupt` `PUT`; o serviço continua igual (`vps_ler` / GET).
  - GCS: anexo numa demanda → registro `gcs` `POST`; o objeto não existe no bucket.
  - freshhub: enviar um rascunho → registro `freshhub` `POST`; o evento não chega no 8100.
  - Stokki: Torre (contagem), Consulta na Expedição, Pedidos Parados → "indisponível na homologação"; registros `stokki`/`playwright`; **`journalctl` dos timers da produção sem 401 no horário da prova**.
  - Agentes: botão de qualquer agente → 409 "Agentes desligado na homologação".
  - Trava do unit: `ExecStartPre` com o `PYTHONPATH` apontando para uma pasta vazia (teste manual com `systemd-run --uid=homolog`, mesmo ambiente) → sai com 1; com a pasta certa → 0.
  - Firewall: `sudo -u homolog /opt/stokki-homolog/venv/bin/python -c "import socket; socket.create_connection(('127.0.0.1',2785),3)"` **sem** `PYTHONPATH` → timeout/refused (é o nft agindo, não a guarda).
  - Portal: CNPJ de um cliente real → "Este CNPJ não tem acesso à homologação."; cliente teste entra.
  - Celular: abrir `/painel/` pelo 4G, digitar a senha do Caddy, abrir uma tela com câmera (retornos ou baixa sem app) e tirar a foto.
- [ ] **Step 4:** Atualizar a memória `project_homologacao_legado.md` com o resultado e o commit; avisar o Hugo onde está a senha (`/root/homolog-senha-caddy.txt`), sem mostrá-la.
