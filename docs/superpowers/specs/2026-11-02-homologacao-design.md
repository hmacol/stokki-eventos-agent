# Homologação do legado (painel interno + portal do cliente)

Data: 02/11/2026 (perguntas P1-P4 respondidas em 10/11). Status: **APROVADO pelo Hugo em 10/11/2026**.
Levantamento feito em `origin/master` @ c218520 e na VPS (somente leitura) em 02/11.

## 1. Intenção

Hoje uma mudança só é testada na máquina do Hugo (porta 8099). O celular não abre a câmera ali,
porque não há HTTPS confiável, e por isso as telas de galpão, canhoto e foto só são provadas de verdade
depois do deploy. A homologação é a **bancada de testes do Hugo**: uma cópia do painel e do portal
com HTTPS de verdade, que abre no celular de qualquer lugar, com os dados de produção e o código do ramo
em teste. **Ela não consegue fazer nada sair para o mundo.**

Fluxo novo: ramo → homologação → OK do Hugo → merge → produção.

## 2. Decisões do Hugo (02/11, não reabrir)

- **Para quê:** é a bancada de testes dele. Toda mudança passa primeiro pela homologação e só vai para produção depois do OK dele.
  "deploy" continua significando produção. A homologação ganha comando próprio.
- **Escopo:** painel interno (`painel_agentes`) e portal do cliente (`portal_cliente`). **Ficam de fora** o app dos
  motoristas (`motorista-api`) e as rotinas automáticas (timers).
- **Regra de ouro: NADA SAI da homologação.**
  - E-mail e WhatsApp nunca são enviados, só registrados (tela ou log).
  - Nada é escrito na Stokki nem na Vuupt.
  - **Nunca login na Stokki, nem para leitura.** As telas que leem a Stokki ao vivo mostram só o que está no banco copiado.
  - A Vuupt pode ser lida pela API. Qualquer escrita é bloqueada.
- **Dados:** o banco de produção é copiado UMA vez, na criação. Trocar de versão muda só o código, os dados ficam.
  Recópia só quando o Hugo pedir explicitamente.
- **Logins:** os mesmos de produção (vêm junto na cópia do banco).
- **Acesso:** pelo celular, de qualquer lugar, com HTTPS de verdade, na mesma VPS, com código e banco próprios e
  uma faixa "HOMOLOGAÇÃO" em todas as telas.

Respondidas pelo Hugo em 10/11 (perguntas P1-P4):
- **P1 Endereço:** subdomínio próprio, `homolog.freshhub.com.br/painel` e `/cliente`. O Hugo cria o registro A.
- **P2 Vuupt:** a homologação usa o **token de produção**. A guarda deixa passar só leitura.
- **P3 Serviços com chave:** ligar os quatro: **Google Maps**, **Anthropic**, **GCS só leitura** e **pool do freshhub**
  (leitura da API nova de produção).
- **P4 Acesso:** **senha extra no Caddy** para o subdomínio inteiro, e o **portal restrito** ao cliente teste e aos
  logins de equipe.

## 3. Metas, não-metas, antimetas

**Metas**
1. Abrir painel e portal da homologação no celular, por HTTPS, com câmera funcionando.
2. Subir qualquer ramo publicado com um comando e ver a tela daquele ramo em menos de 2 min.
3. Registrar cada envio bloqueado (e-mail, WhatsApp, Stokki, Vuupt, outros) numa tela onde dá para ler o que
   *teria* saído (destinatário, assunto, texto).
4. Faixa "HOMOLOGAÇÃO" em toda tela, com ramo, commit e data da cópia dos dados.

**Não-metas**
- App dos motoristas (`motorista-api`), página de insucesso (`resposta-insucesso`), atendimento (8095), confirmação (8090).
- Timers e daemons. Os daemons do portal (`portal-cliente-envios` = fila de pedidos para a Stokki;
  `portal-cliente-chamados` = leitor IMAP) **não sobem**. O que o portal enfileira fica parado no banco da homologação.
- Dados "vivos": o banco não acompanha a produção. A Vuupt lida ao vivo vai divergir do banco congelado, e isso é esperado.
- Várias homologações ao mesmo tempo (uma por ramo). É uma bancada só, um ramo por vez.
- Anonimizar dados (é a mesma máquina e são os mesmos logins).

**Antimetas (fracasso mesmo que "funcione")**
- Qualquer e-mail, WhatsApp, escrita na Stokki/Vuupt/Lalamove/GCS ou evento no freshhub sair da homologação, mesmo por
  subprocesso, thread em segundo plano ou biblioteca nova.
- Qualquer login ou requisição na Stokki.
- Entrar na homologação derrubar a sessão de produção do Hugo (ou o contrário), ou uma tela da homologação conseguir
  agir na produção.
- A homologação ler ou escrever o banco, o config ou os arquivos de produção depois da cópia.
- O código de produção mudar de comportamento por causa da homologação (tudo o que é novo só age com o ambiente marcado).
- Leituras da homologação na Vuupt provocarem 429 que atrapalhem as rotinas de produção.

## 4. O que o levantamento achou (base das decisões)

| # | Achado | Evidência |
|---|--------|-----------|
| A1 | Folga na VPS: 15 GB de RAM, **11 GB disponíveis**, sem swap; disco com **165 GB livres**. O painel usa cerca de 128 MB de RSS e o portal cerca de 212 MB. `dados/` = 2,4 GB, `dados.db` = 149 MB, `roteirizacao/dados` = 969 MB. | `vps_ler.py disco`, `ps -o rss` |
| A2 | Portas em uso: 2785, 3000, 5432, 5434, 8071-8074, 8080, 8090, 8095, 8100, 8101. **8171 e 8174 livres.** | `vps_ler.py portas` |
| A3 | O Caddy tem um bloco `app.freshhub.com.br` com `handle_path /painel/*`, `/insucesso/*`, `/motorista/*`, `/cliente/*` e importa `freshhub-app.caddy`. O sistema novo já tem homologação própria num subdomínio (`hml.freshhub.com.br`, arquivo `/etc/caddy/freshhub-hml.caddy`, importado no fim). | `/etc/caddy/Caddyfile` |
| A4 | Config e banco são achados por `Path(__file__)` (raiz do checkout): 116 arquivos citam `dados.db`. Não há variável de ambiente para trocar o caminho. **Um checkout próprio já dá à homologação config e banco próprios, sem mudar código.** | `painel_agentes.py:29,107`, `portal_cliente/app.py:33,65` |
| A5 | O cookie de sessão do painel se chama `session` (padrão do Flask), com path `/`. O do portal se chama `portal_cliente`, também com path `/`. A trava CSRF (`exige_mesma_origem`, `painel_agentes.py:340`) compara só o host. No mesmo host, a homologação e a produção **trocam cookies entre si**, e uma tela da homologação que chame `/painel/api/...` passa na trava e age na produção. Hoje nenhum template usa caminho absoluto, mas é justamente código novo que vai para a homologação. | grep em templates e `.py` |
| A6 | O painel dispara subprocessos: `executor.py:136` (agentes de `agentes.py`, entre eles `pipeline.py`, `expedir_pedidos.py` com SMTP próprio e **a importação do outro repo em `/opt/agente-importacao-stokki`**, que tem config próprio e faz login na Stokki) e `notificacoes_tela.py:374` (disparo de notificação). | |
| A7 | Threads em segundo plano que saem para a rede: `demandas.py:961` e `painel_agentes.py:1527` (GCS), `painel_agentes.py:1448` (baixa sem app → Vuupt), `planejamento_rotas.py:286` (ressincroniza da Vuupt), `chamados.py:194` (e-mails), `stokki/sessao_uso.py:162` (renova a sessão da Stokki). | |
| A8 | Bibliotecas de saída nos 140 módulos que o painel e o portal importam: `requests` (Vuupt, OpenWA, Lalamove, Supabase, Google Maps, freshhub, ViaCEP, BrasilAPI e GCS via google-auth), `smtplib` (`email_utils.py:143`), `imaplib` (`chamados.py:1250`), `httpx` e `httpx2` (**SDK da Anthropic**; o 1.x usa o `httpx2`, achado no 1º subir, 10/11) e Playwright/Chromium (`stokki/auth.py:283`, `lovable/auth.py:223`). | `fecho_imports.py` |
| A9 | O token da Vuupt (`vuupt_api.token`) existe só no config de produção e **também escreve**. | |
| A10 | Arquivo de config de produção: 600 (www-data); `dados/`: 750 com ACL. **Mas 8 backups antigos do config na raiz** (`*.bak-22-08` … `*.bak-29-08-lalamove*`) estão **644, legíveis por qualquer usuário da VPS.** Fora do escopo, mas enfraquece o isolamento por usuário (D3). | `vps_ler.py ls` |

## 5. Desenho

### D1. Endereço *(decisão do Hugo, P1)*

Subdomínio `homolog.freshhub.com.br`, com os mesmos caminhos da produção:
`homolog.freshhub.com.br/painel/` e `homolog.freshhub.com.br/cliente/`. Origem diferente da produção significa
cookies, `localStorage`, service worker (`/wms/sw.js`) e trava CSRF separados pelo navegador, sem depender de código.
Exige um registro A no DNS, que o Hugo cria como fez com `hml.` e `novo.`. **O registro precisa existir antes do
`homolog criar`**, senão o Caddy fica tentando emitir o certificado. Segue o padrão que o freshhub já usa: arquivo
`/etc/caddy/legado-homolog.caddy`, importado no fim do Caddyfile. Os cookies continuam com os nomes de produção (não
há colisão, porque são origens diferentes).

### D2. Checkout, venv e usuário próprios *(decisão do Claude)*

- `/opt/stokki-homolog`: clone do mesmo repositório do GitHub, em HEAD destacado no commit do ramo em teste.
- `/opt/stokki-homolog/venv`: venv próprio (ramo com dependência nova não pode instalar no venv de produção).
- Usuário Linux **`homolog`** (não `www-data`), dono de `/opt/stokki-homolog` (750). Como `dados/` e o config de
  produção são 750/600 do `www-data`, um caminho errado no código da homologação dá *permission denied* em vez de
  tocar a produção. (Ver A10: o Hugo deveria fechar os 8 backups 644, fora deste trabalho.)

### D3. Serviços *(decisão do Claude)*

Dois units novos em `infra/homolog/`, cópias dos de produção com:
- `homolog-painel.service`: `waitress-serve --host=127.0.0.1 --port=8171 painel_agentes:app`, `WorkingDirectory=/opt/stokki-homolog/painel_agentes`
- `homolog-portal.service`: porta 8174, `WorkingDirectory=/opt/stokki-homolog/portal_cliente`
- Ambos: `User=homolog`, `UMask=0027`, `Environment=FRESH_AMBIENTE=homolog`,
  `Environment=PYTHONPATH=/opt/stokki-homolog/homologacao/site` (carrega o `sitecustomize` da guarda, D4),
  `Environment=HOMOLOG_ESTADO=/var/lib/stokki-homolog` (de onde o painel lê ramo/commit/cópia),
  `MemoryMax=600M` (proteção da produção), `Restart=on-failure`.
- Nenhum timer e nenhum daemon (`*-envios`, `*-chamados`).

### D4. A guarda central *(decisão do Claude, o coração do desenho)*

Pasta nova `homologacao/` no repo. Fica inerte em produção: só age com `FRESH_AMBIENTE=homolog`.

**Como liga:** `homologacao/site/sitecustomize.py` (via `PYTHONPATH` do unit) chama `guarda.instalar()` antes de qualquer
import do projeto. Além dele, o `infra/homolog/homolog-guarda.pth` (do root, 644, instalado pelo `criar.sh`/`subir.sh`
no site-packages do venv da homologação) chama `homologacao/pth_guarda.py:ativar()` em **todo** processo do venv,
inclusive script rodado à mão sem o ambiente dos units e com `-E`/`-I`: força `FRESH_AMBIENTE=homolog`, instala a guarda
e, se falhar, derruba o processo (`os._exit(1)`). Só `-S` (sem o módulo `site`) pula o `.pth`; aí resta o nft. O pip
do root passa com `HOMOLOG_PIP_SEM_GUARDA=1`, que só vale para euid 0. Como os processos filhos herdam o ambiente, todo `python` disparado pelo painel também nasce com a
guarda. Além disso, `homologacao/ambiente.py:exigir_coerencia()` roda no topo de `painel_agentes.py` e de
`portal_cliente/app.py` e **aborta a subida** quando:
- `FRESH_AMBIENTE=homolog`, mas a raiz do código é `/opt/stokki-eventos`; ou
- a raiz é `/opt/stokki-homolog`, mas a guarda não está instalada (`PYTHONPATH` esquecido).

**O que a guarda troca (no processo, todos os pontos de uma vez):**

| Camada | Troca | Política |
|---|---|---|
| `smtplib.SMTP` / `SMTP_SSL` | classe falsa | `sendmail`/`send_message` **registram a mensagem inteira e devolvem sucesso**. O fluxo segue como se tivesse enviado. Cobre `email_utils` e qualquer SMTP solto (`expedir_pedidos.py` etc.). |
| `imaplib.IMAP4` / `IMAP4_SSL` | recusa | registra e levanta erro. |
| `requests.Session.request` (pega `requests.get/post`, `chamar_com_retry`, google-auth) | lista do que passa, por host e método | **Passa (só leitura):** `api.vuupt.com` e `app.vuupt.com` (o `vuupt_client.py` lê deste; achado na prova da VPS, 10/11) GET/HEAD, com limite próprio de ≤ 1 req/s para não gerar 429 na conta usada pela produção (P2); `maps.googleapis.com` GET (P3); `storage.googleapis.com` GET/HEAD (download e metadados; upload é POST/PUT e fica bloqueado) e `oauth2.googleapis.com` **só** `POST /token` (troca da conta de serviço por token de acesso; sem isso a leitura do GCS não funciona) (P3); `127.0.0.1:8100` (API do freshhub de produção) **só `GET /api/pool`** (query livre; os POST de `eventos_freshhub` e
qualquer outro GET ficam bloqueados: `GET /api/replicacao/pendentes` marca os itens como enviados e consumiria a fila
de réplica da produção) (P3); `viacep.com.br` e `brasilapi.com.br` GET (públicos, sem chave). **OpenWA (`127.0.0.1:2785`, `POST .../sendText`): registra destino e texto e devolve uma resposta 200 falsa**, no formato que `integracao_openwa.py:44` espera. **Todo o resto** (Stokki, escrita na Vuupt, Lalamove, upload no GCS, Sheets, Supabase, POST no freshhub, freshhub hml 8101 etc.): **registra e levanta `EnvioBloqueadoHomolog`** (subclasse de `requests.ConnectionError`, para cair no tratamento de "serviço fora" que já existe). |
| `httpx.Client.send` / `AsyncClient.send` | mesma lista | hoje é só o SDK da Anthropic: **`api.anthropic.com` passa** (P3). É uma chamada ao modelo, não um envio a terceiros. As *ações* que o assistente do portal dispara continuam passando pelas outras camadas, e a validação de foto continua com a própria chave de liga/desliga copiada do config (hoje desligada). Qualquer outro host via httpx fica bloqueado. |
| `playwright.sync_api.sync_playwright` / `async_api.async_playwright` | recusa | registra e levanta erro. Nenhum Chromium sobe. |
| `socket` (rede de segurança) | `getaddrinfo` + `connect` | só resolve nomes da lista acima; conexão com IP literal só se veio dessa resolução; **loopback proibido, com uma exceção: `127.0.0.1:8100`** (freshhub, P3). OpenWA, Evolution, freshhub hml e Postgres ficam inalcançáveis mesmo por código que fuja das camadas de cima. |

**Executor de agentes e disparo de notificação:** `executor.py` e `notificacoes_tela.py` checam o ambiente e recusam
com "desligado na homologação" (as rotinas estão fora do escopo; ver A6). A guarda herdada nos filhos é a segunda
barreira, para quando um agente for disparado por outro caminho.

**Registro dos envios:** tabela `homolog_envios` no `dados.db` **da homologação**: `quando`, `canal`
(`email|whatsapp|stokki|vuupt|lalamove|gcs|freshhub|anthropic|playwright|imap|subprocesso|outro`), `metodo`, `destino`
(e-mail, telefone ou URL), `resumo` (assunto ou primeira linha), `conteudo` (até 64 KB: HTML do e-mail, texto do
WhatsApp, corpo da requisição), `origem` (módulo:linha que chamou). Tela **"Envios bloqueados"** no painel
(`/homologacao/envios`, só nível total, existe só na homologação): lista com filtro por canal e visualização do e-mail
renderizado.

**Segunda camada: segredos ausentes** (D6). Mesmo que algo escape da guarda, o config da homologação não tem senha de
e-mail, chave do OpenWA, credencial da Stokki nem chave da Lalamove. As quatro chaves liberadas em P2/P3 (Vuupt,
Google Maps, Anthropic, conta de serviço do GCS) e o token do freshhub **podem escrever**: para elas, a única trava
é a primeira camada (só leitura). Por isso a prova da guarda (seção 7) cobre cada uma com uma tentativa de escrita
real que precisa aparecer em "Envios bloqueados".

**Terceira camada: firewall por usuário.** Regra `nftables` numa tabela própria (`inet homolog`), que vale só para o
usuário `homolog`: aceita `ct state established,related` (as respostas ao Caddy), DNS **só para o resolvedor local
`127.0.0.53`** e `127.0.0.1:8100` (freshhub); **descarta a saída TCP para as portas 25/465/587/143/993 (e-mail), para
qualquer outra porta em 127.0.0.0/8, para qualquer endereço da própria máquina (`fib daddr type local`: IP público,
gateways do Docker), para redes privadas e link-local (10/8, 172.16/12, 192.168/16, 169.254/16, fc00::/7, fe80::/10)
e o DNS para qualquer outro destino**. O `homolog-nft.service` roda depois do `nftables.service`. Cobre
subprocesso, Chromium e qualquer coisa fora do Python. Persistida em `/etc/nftables.d/homolog.nft`. *(Decisão do Claude. Mexe no firewall da VPS, então
o OK deste desenho cobre isso. A tabela é própria e não toca nas cadeias do Docker.)*

**Ajustes da revisão final (10/11, decisões do Claude):**
- Estado da bancada (`HOMOLOG_VERSAO`, `HOMOLOG_HISTORICO`, `HOMOLOG_COPIA`, `HOMOLOG_REQ_SHA`, `EM_TROCA`) em
  `/var/lib/stokki-homolog`, só do root, gravado atomicamente; o painel lê pela variável `HOMOLOG_ESTADO` (D3).
- `homolog-guarda.pth` no venv (ver "Como liga"): a guarda vale também para script rodado à mão; ainda assim, a regra
  é nunca rodar script à mão em `/opt/stokki-homolog`.
- No 8100 só `GET /api/pool` (tabela acima).
- Playwright presente com submódulo que não importa: a instalação da guarda falha (processo não sobe). O `subir.sh`
  exige na saúde as 6 camadas (`smtp`, `imap`, `requests`, `httpx`, `playwright`, `socket`).
- `criar.sh` recusa com ramo no ar (`HOMOLOG_VERSAO`), salvo `--refazer`, que grava `EM_TROCA` antes de tocar no
  checkout.

**Impressão de etiquetas (galpão):** a fila `wms_fila_impressao` da homologação nunca é lida por estação nenhuma. As
etiquetas ficam visíveis na tela da fila, e nada imprime. Pela regra "nada sai", não há exceção. *(Decisão do Claude.)*

**Telas que liam a Stokki ao vivo** (passam a mostrar o que está no banco ou "indisponível na homologação", nunca erro 500):
- Torre: contagem da Stokki (`torre_controle.py:1931`, `contar_pedidos`)
- Consulta na Expedição (`consulta_expedicao.py:264`, `_SessaoSemLogin` reusa a sessão salva; o arquivo de sessão não é copiado)
- Pedidos Parados, triagem (`pedidos_parados_triagem.py:1304,1370`)
- Solicitações do portal → Executar cancelamento (`solicitacoes_execucao.py:403-431`)

### D5. Faixa "HOMOLOGAÇÃO" *(decisão do Claude)*

Um `after_request` em cada app (painel e portal), ativo só no ambiente de homologação, injeta logo depois do `<body>`
de toda resposta `text/html` uma faixa fixa no topo (laranja, por cima de tudo, sem cobrir botões; empurra o conteúdo).
O texto é `HOMOLOGAÇÃO · <ramo> @ <commit curto> · dados de <dd/mm hh:mm>`, e o `<title>` ganha o prefixo
`[HOMOLOG]`. Nenhum template é editado. Ramo e commit vêm de `HOMOLOG_VERSAO` (D8); a data, de
`HOMOLOG_COPIA` (D7), ambos na pasta de estado `/var/lib/stokki-homolog` (variável `HOMOLOG_ESTADO`).

### D6. Config da homologação *(decisão do Claude)*

Gerado **na VPS** por `homologacao/gerar_config.py`, rodando como root, **sem nunca imprimir valores**:
1. Lê o config de produção.
2. Remove toda chave cujo nome case com segredo (`senha*`, `*token*`, `*secret*`, `api_key`, `api_secret`, `password`,
   `senha_app`, `sync_secret`, caminhos de conta de serviço) em qualquer nível.
3. Gera `secret_key` novos para `painel_agentes` e `portal_cliente` (a sessão da homologação não vale na produção e vice-versa).
4. Mantém `freshhub_api` (url + token) e `planejamento.fonte_pool` como na produção (P3). Os eventos que o legado manda
   para o freshhub (`nucleo/eventos_freshhub.py`, POST) são barrados pela guarda e acumulam em
   `dados/eventos_freshhub_pendentes.jsonl` **da homologação**. Isso é inofensivo; o arquivo é esvaziado a cada subida.
5. Ajusta `portal_cliente.url_base` (e as outras `url_base`) para `https://homolog.freshhub.com.br/...`.
6. Reinsere **só** os segredos liberados em P2/P3, por lista fixa no script: `vuupt_api.token`, a chave do Google Maps,
   `anthropic.api_key`, o token do `freshhub_api` e o caminho da conta de serviço do GCS. O próprio arquivo da conta de
   serviço é copiado para `/opt/stokki-homolog/` com 600, dono `homolog`.
7. Acrescenta a seção `homologacao:` com `portal_cnpjs_permitidos: ["00000000000191"]` (cliente teste; P4).
8. Grava `/opt/stokki-homolog/config.yaml` (600, dono `homolog`) e imprime **só os nomes** das chaves removidas e mantidas.

Consequência: o par usuário/senha do painel que está no config **não** vale na homologação. Entra-se com os
**logins nominais** (`painel_usuarios`: 1 total, 9 operador, 1 logística, 1 atendimento, 1 galpão, todos ativos). A
equipe do portal com par fixo do config também não entra.

**Portal restrito (P4):** com o ambiente de homologação, `auth_cliente` só aceita login por CNPJ + PIN de CNPJ que esteja
em `homologacao.portal_cnpjs_permitidos` (padrão: só o cliente teste `00.000.000/0001-91`). Os demais recebem
"este CNPJ não tem acesso à homologação". A entrada de equipe (`/equipe`, login nominal com "Portal do cliente") continua
valendo, e é por ela que o Hugo vê o portal de qualquer cliente.

### D7. Cópia inicial e recópia do banco *(decisão do Claude)*

**Cópia** (`homologacao/copiar_dados.sh`, chamado pelo comando de criação e pelo de recópia):
1. Para `homolog-painel` e `homolog-portal`.
2. `dados.db` via **API de backup do SQLite** (`sqlite3.connect(prod).backup(dest)`), rodando como `www-data`
   para um arquivo temporário 600. Assim a cópia sai consistente com o WAL, sem `cp` de arquivo vivo e sem travar a
   produção (o backup é incremental, em páginas).
3. `rsync` do resto de `dados/` e de `roteirizacao/dados/`, **excluindo** `dados.db*`, `*.log`, `logs/`,
   `sessao_*.json`, contas de serviço, `*.bak*` e `eventos_freshhub_pendentes.jsonl`.
4. Monta `dados/` e `roteirizacao/dados/` numa pasta nova, entrega ao `homolog` e troca cada uma inteira (o root nunca
   grava dentro de uma pasta do `homolog`), grava `/var/lib/stokki-homolog/HOMOLOG_COPIA` (data e hora) e sobe os serviços.

**Recópia:** mesmo script, mas antes guarda o banco atual da homologação em `dados/dados.db.anterior` (uma geração só).
**Só roda com pedido explícito do Hugo.** O subcomando exige `--confirmo-recopia`, e a skill diz que Claude nunca o
propõe por conta própria.

Mudança de esquema: um ramo que cria tabela ou coluna altera a cópia. Voltar para outro ramo não desfaz. Isso é
aceito (é a mesma regra da produção: os dados ficam).

### D8. Comando: subir um ramo para a homologação *(decisão do Claude)*

Subcomando novo no `vps_ops.py` local (gitignored; pede confirmação, como o deploy):

```
py -3.11 .claude/scripts/vps_ops.py homolog subir <ramo>
py -3.11 .claude/scripts/vps_ops.py homolog status            # (também em vps_ler.py, só leitura)
py -3.11 .claude/scripts/vps_ops.py homolog recopiar --confirmo-recopia
py -3.11 .claude/scripts/vps_ops.py homolog criar             # 1 vez: usuário, clone, venv, config, cópia, units, Caddy, nft
```

`subir <ramo>`:
1. **Local:** recusa se `origin/<ramo>` não existe ou difere do ramo local (precisa estar pushado; push continua sendo
   decisão do Hugo).
2. **Bancada ocupada:** se já há outro ramo na homologação, mostra qual, desde quando e quem subiu, e pede
   confirmação para trocar (há sessões em paralelo). *(Decisão do Claude.)*
3. **VPS, como `homolog`:** `git fetch origin` → `git checkout --detach --force origin/<ramo>` →
   **`git merge --no-edit origin/master`** só no checkout da homologação (nunca é pushado). Assim o que se testa é o
   ramo como ele vai ficar depois do merge. Se der conflito, aborta e avisa que o ramo precisa ser atualizado.
   Depois, `pip install -r requirements.txt` se ele mudou desde a última subida. *(Decisão do Claude.)*
4. Grava `/var/lib/stokki-homolog/HOMOLOG_VERSAO` (ramo, commit da ponta do ramo, commit do master usado, quando) e
   acrescenta uma linha em `/var/lib/stokki-homolog/HOMOLOG_HISTORICO`. A pasta de estado é só do root, gravada
   atomicamente; o painel a lê pela variável `HOMOLOG_ESTADO` dos units (sem ela, cai em `dados/`).
4. `systemctl restart homolog-painel homolog-portal`.
5. **Prova:** `curl` em `127.0.0.1:8171/homologacao/saude` e `8174/homologacao/saude` → JSON com
   `{ambiente: homolog, guarda: ativa, ramo, commit, camadas: [...]}`. A rota existe só na homologação. Depois um
   `curl` HTTPS no endereço público. Imprime os dois links para abrir no celular.

Skill local nova **`homologar`** (`.claude/skills/homologar`), disparada por "sobe pra homologação", "homolog",
"manda pra bancada". Ela chama o comando acima.

**Registro do OK:** `vps_ops.py homolog ok` (rodado quando o Hugo diz "ok", "aprovado" ou "pode ir") acrescenta em
`HOMOLOG_HISTORICO` a linha `OK <ramo> <commit da ponta> <quando>`. Se o ramo ganhar commit novo depois do OK, o OK
deixa de valer (é preciso subir e aprovar de novo).

A skill **`deploy-vps`** ganha um passo. Antes do deploy, para cada commit novo que vai para produção e que mexe em
arquivo do painel, do portal ou dos módulos que eles importam (lista calculada pelo fecho de imports, como no
levantamento), ela verifica se esse commit é ancestral de algum `<commit da ponta>` com OK no histórico. A comparação é
por ancestralidade, não por SHA do merge. Se faltar OK, **avisa, lista os commits e pede confirmação; não trava**
(correção urgente continua possível). Deploy que só mexe em timer, `motorista-api` ou pipeline não gera aviso.
*(Decisão do Claude.)*

### D9. Caddy *(endereço e senha: decisão do Hugo, P1/P4; o resto: decisão do Claude)*

Arquivo novo `infra/homolog/legado-homolog.caddy` (cópia em `/etc/caddy/legado-homolog.caddy`, importado no fim do
Caddyfile, como o `freshhub-hml`), bloco `homolog.freshhub.com.br` com:
- **`basic_auth` no host inteiro** (P4): usuário `homolog`, senha só com hash bcrypt no arquivo. A senha é gerada pelo
  `homolog criar` (aleatória, 20 caracteres) e gravada em `/root/homolog-senha-caddy.txt` (600). **Nunca aparece no
  chat:** o Hugo lê por SSH e salva no celular (o navegador lembra por aparelho). Trocar = rodar `homolog trocar-senha`.
- `handle_path /painel/*` → 8171 e `/cliente/*` → 8174, `X-Forwarded-Prefix`, os mesmos `redir` de barra final e
  `header X-Robots-Tag "noindex"`.
- Backup do Caddyfile antes de mexer (padrão `Caddyfile.bak-<data>-antes-homolog`), `caddy validate` antes do `reload`.

Os links públicos com token (canhoto, cotação) que a homologação gerar também passam pela senha do Caddy. É aceito:
quem testa é o Hugo.

## 6. Pontos de bloqueio (levantamento 1c): onde cada saída está e o que a segura

Ordenados por canal. "Guarda" = D4. "Sem segredo" = D6. "nft" = terceira camada.

| Canal | Onde sai (módulo:linha) | Quem chama, a partir do painel/portal | Fica bloqueado por |
|---|---|---|---|
| E-mail | `email_utils.py:143` (`smtplib.SMTP`) | chamados, cancelamento, alterar entrega, notificações, cadastro de embarcador, avisos de execução, preferências etc. (todos via `enviar_email`) | Guarda (SMTP falso registra), sem senha, nft 587/465 |
| E-mail | `expedir_pedidos.py:426,839,902`, `insucesso_entrega/expedir_pedidos.py`, `relatorio_operacional.py:512`, `validacao_checklists` | só como agente (subprocesso) | executor desligado, guarda herdada, nft |
| IMAP | `portal_cliente/chamados.py:1250` | só o daemon `chamados.py ler --loop` (não sobe) | não sobe, guarda, nft 993 |
| WhatsApp | `integracao_openwa.py:44` (POST `127.0.0.1:2785`) | `notificar_whatsapp.despachar` (chamados, cliente sem resposta, alterar entrega, cancelamento pendente, clientes AGENDA, alertas) | Guarda (resposta falsa registrada), sem `api_key`, socket loopback, nft loopback |
| Stokki (login) | `stokki/auth.py:283` (Playwright) e `:112/:350` (requests) | Torre (`torre_controle.py:1931`), Consulta Expedição (`consulta_expedicao.py:33,264`), Parados (`pedidos_parados_triagem.py:1304,1370`), Executar cancelamento (`solicitacoes_execucao.py:418-431`) | Guarda (Playwright + host `freshlog.stokki.com.br`), sem credencial, sem `sessao_stokki.json` |
| Stokki (sessão) | `stokki/sessao_uso.py:162` (thread de renovação) | `solicitacoes_execucao.py:403,412` | Guarda |
| Stokki (envio XML) | `portal_cliente/envio_pedidos.py:1112,1117` → daemon `enviar_stokki --loop` | envio de pedido no portal | daemon não sobe (fila fica parada) + guarda |
| Vuupt (escrita) | `vuupt_client.py:87` (Session), `vuupt_checklist.py`, `roteirizacao/cancelar_servico.py` | Planejamento/rascunhos (enviar rota), Torre (marcar entregue, cancelar), baixa sem app (`painel_agentes.py:1448`), checklist, portal (cancelar, alterar entrega, retomar) | Guarda (só GET/HEAD na Vuupt) |
| Vuupt (leitura) | `vuupt_client.py`, `portal_cliente/dados_cliente.py:743,779`, `regras/cadastro_motoristas.py:43`, `planejamento_rotas.py:286` | Torre, Planejamento, portal (canhoto, status) | **Permitido** (GET, ≤ 1 req/s), token de produção (P2) |
| Lalamove | `lalamove_client.py:73` | Torre/Planejamento (`lalamove_integracao`) | Guarda, sem chave |
| GCS (upload) | `documentos_pedido/storage_gcs.py:82` | Demandas (`demandas.py:961`), Retornos (`painel_agentes.py:1527`), canhotos manuais | Guarda (POST/PUT no `storage.googleapis.com`). O arquivo fica só no disco da homologação. |
| GCS (leitura) | `documentos_pedido/storage_gcs.py` (download, URL assinada) | canhoto/foto já na nuvem | **Permitido** (GET + `POST oauth2/token`), conta de serviço copiada (P3) |
| freshhub API (escrita) | `nucleo/freshhub_client.py:49` (8100), POST de `nucleo/eventos_freshhub.py` | eventos `roteirizado`/`nao_aprontou`/`rota_enviada` | Guarda (só `GET /api/pool` no 8100); acumulam no `.jsonl` da homologação |
| freshhub API (leitura) | `nucleo/freshhub_client.py` (`GET /api/pool`) | pool do planejamento (`nucleo/pool.py`) | **Permitido** (só `GET /api/pool`), token copiado (P3); exceção de loopback no socket e no nft. A fila de réplica (`GET /api/replicacao/pendentes`, que marca itens como enviados) fica bloqueada |
| Fresh Hub SPA (Supabase) | `lovable/auth.py:177,223` | importado por `pedidos_parados_triagem` (hoje só `normalizar_order_number`) | Guarda |
| Anthropic | `portal_cliente/assistente.py:60`, `nucleo/assistente_motorista.py:94`, `nucleo/validacao_fotos.py` (httpx) | assistente do portal, validação de foto | **Permitido** `api.anthropic.com` (P3); as ações do assistente passam pela guarda; validação de foto segue a chave de liga/desliga do config |
| Google Maps | `geocodificacao.py:136` | endereço/geocodificação no portal e painel | **Permitido** GET (P3), chave copiada |
| ViaCEP / BrasilAPI | `portal_cliente/cotacao.py:258` | cotação | **Permitido** (GET público, sem chave) |
| Subprocesso | `painel_agentes/executor.py:136`, `notificacoes_tela.py:374` | botões de agentes, disparo de notificação | recusa no código + guarda herdada + nft |

Não estão no fecho de imports do painel/portal (conferido): Evolution (`integracao_evolution.py`), Chatwoot, push do Expo.

## 7. O que fica para o construtor

- Formato exato da resposta falsa do OpenWA (ler `integracao_openwa.py:44-60`) e o tipo do erro levantado para cada
  biblioteca, de modo que cada tela da lista de D4 mostre "indisponível na homologação" em vez de 500. Provar tela a tela.
- Como a guarda registra no banco a partir de threads e subprocessos (conexão própria, `timeout`, nunca derrubar a
  requisição por falha do registro; se o banco falhar, cai para `dados/homolog_envios.jsonl`).
- Detalhes da regra `nft` (ordem com as cadeias do Docker, persistência no boot) e prova de que o Caddy continua
  alcançando 8171/8174.
- Testes (`unittest`, padrão do repo): guarda instalada bloqueia SMTP, requests não-GET na Vuupt, Stokki em qualquer
  método, upload no GCS (e deixa passar só `POST oauth2/token`), POST no 8100, httpx para host que não seja
  `api.anthropic.com`, Playwright, socket loopback fora do 8100, subprocesso com ambiente herdado; `exigir_coerencia`
  aborta nos dois casos; portal recusa CNPJ fora da lista; produção (sem a variável) não muda nada.
- **Prova na VPS, depois do `criar`:** uma tentativa real de escrita por chave copiada (Vuupt PUT, upload GCS,
  POST freshhub) e um e-mail e um WhatsApp disparados por tela. Cada um precisa aparecer em "Envios bloqueados", e nada
  pode aparecer do lado de fora (Vuupt sem a alteração, bucket sem o arquivo, freshhub sem o evento).
- Entrada da tela `/homologacao/envios` e da rota `/homologacao/saude` no catálogo `acesso_telas.TELAS` (o
  `test_acesso_telas` exige), registradas só com o ambiente de homologação.
- Atualizar `MAPA_DO_SISTEMA.txt` (seções 3, 16 e 17) e o `CLAUDE.md` do projeto (comando novo; "deploy" continua sendo produção).
- Script `homolog criar` idempotente (pode rodar de novo sem estragar), com cada passo mostrando o que fez.

## 8. Ordem sugerida de construção

1. `homologacao/` (guarda, ambiente, faixa, registro, rota de saúde) + testes. Nada muda em produção.
2. Ajustes no executor e no disparo de notificação, e nas 4 telas que leem a Stokki (mensagem amigável).
3. `gerar_config.py`, `copiar_dados.sh`, units, Caddy e nft em `infra/homolog/`.
4. Subcomandos `homolog` no `vps_ops.py` / `vps_ler.py`, e skills `homologar` + passo na `deploy-vps`.
5. Com o OK do Hugo: `homolog criar` na VPS, depois provar cada linha da tabela da seção 6 com uma ação real na
   homologação, conferindo a tela "Envios bloqueados".

## 9. Riscos aceitos

- A Vuupt ao vivo diverge do banco congelado (rotas e serviços que o banco não conhece). É esperado.
- As leituras da homologação gastam a cota da mesma conta da Vuupt (mitigado pelo limite de 1 req/s).
- Um ramo com migração de esquema deixa marca no banco da homologação até a próxima recópia.

## 10. Calls do Claude, para o Hugo conferir ao ler

D2 (usuário `homolog` e venv próprio), D3 (portas 8171/8174, `MemoryMax`, daemons não sobem), D4 inteiro
(e-mail e WhatsApp "fingem sucesso"; o resto falha como "serviço fora"; ViaCEP e BrasilAPI liberados; executor de
agentes desligado), D5 (faixa por `after_request`), D6 (par de login do config não vale; só logins nominais),
D7 (cópia de `dados/` inteiro menos sessões e logs; recópia guarda uma geração), D8 (subir exige ramo pushado e
testa o ramo **já mesclado** com o master; OK registrado por `homolog ok`; deploy só **avisa** quando falta OK e só para
commits que mexem no painel/portal; trocar de ramo pede confirmação), D4 (firewall `nft` por usuário; etiquetas
nunca imprimem; GCS lido com `POST oauth2/token` liberado; Anthropic liberado inteiro, inclusive POST), D6 (eventos do
freshhub acumulam no `.jsonl` da homologação; lista de CNPJs do portal no config), D9 (senha do Caddy gerada pelo
`criar`, guardada só em `/root`, nunca no chat).

## 11. Perguntas para o Hugo

Respondidas em 10/11 (ver seção 2). Ação do Hugo antes do `homolog criar`: **criar o registro A de
`homolog.freshhub.com.br` apontando para 187.127.52.197**.
