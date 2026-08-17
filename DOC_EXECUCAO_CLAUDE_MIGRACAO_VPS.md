# Plano de Migração: Estrutura Local → VPS

**Status (16/08): PLANEJAMENTO — nada migrado ainda.** Este documento é o
roteiro combinado com o Hugo pra tirar o sistema (agentes, painel, banco de
dados) da máquina local (Windows, LAN, sem entrada de internet) e colocar na
VPS Hostinger que já hospeda `confirmacao_motoristas` em produção.

---

## 1. Decisões já tomadas com o Hugo (16/08)

| Pergunta | Decisão |
|---|---|
| Estação de impressão física migra também? | **Sim** — precisa de um "print-agent" local enxuto (ver seção 4). |
| Reaproveita a VPS da confirmação ou usa outra? | **Mesma VPS** Hostinger (`187.127.52.197`) que já roda `confirmacao-motoristas`. |
| Estratégia de corte do Agendador do Windows? | **Por módulo** — migra um agente, valida, avança pro próximo (não é big bang nem tudo em paralelo de uma vez). |
| `c:\agente_importacao_stokki` (2º projeto, e-mail→XML→Stokki) entra? | **Sim**, migra junto — hoje já é referenciado pelo `config.yaml` principal (`pasta_xmls_processados`). |

---

## 2. Situação Atual (levantamento)

### 2.1. Dois projetos, hoje separados

- **`c:\agente_stokki_eventos`** (este repo, `github.com/hmacol/stokki-eventos-agent`) — pipeline principal, roteirização, painel web (`painel_agentes`, servido por `waitress` na porta 8070), confirmação de rotas (já migrada pra VPS), validação de checklists, notificação de transportadoras, romaneios/PDF.
- **`c:\agente_importacao_stokki`** — download de e-mail → processamento de XML → importação na Stokki. **Não está sob controle de versão** (sem `git init`). Referenciado pelo `config.yaml` principal via `importacao_email.pasta_xmls_processados`.

### 2.2. Orquestração atual: Agendador do Windows

Tarefas disparam scripts `.ps1`, que chamam `py -3.11 script.py` em sequência,
**esperando cada processo terminar de verdade** antes do próximo (correção
feita em 10/08 depois de dois agentes colidirem escrevendo no mesmo SQLite ao
mesmo tempo — ver comentário em `rodar_sequencial.ps1`).

**Inventário confirmado em 16/08** via `Get-ScheduledTask` (a lista abaixo é
a fonte da verdade agora, não mais os `.ps1` sozinhos — apareceram tarefas
que nenhum `.ps1` revelava).

**Ativas (`Ready`/`Running`):**

| Tarefa | Frequência | Ação |
|---|---|---|
| `StokkiEventos_SequenciaTarde` | Diário, 18:00 | `rodar_sequencial.ps1` → ExecutarTudo → VerificarDuplicadosVuupt → CriarRotasDiarias `--gerar-rascunho` → Notificador → ProcessarDocumentos |
| `StokkiEventos_SequenciaNoite` | Diário, 22:00 | `rodar_sequencial_noite.ps1` → ExecutarTudo → VerificarDuplicadosVuupt → IncrementarRotas |
| `StokkiEventos_ExpedicaoFrequente` | Diário, a cada 30min das 08:00 às 19:35 | `expedir_pedidos.py` |
| `StokkiEventos_RomaneiosManha` | Diário, 04:00 | `rodar_romaneios_manha.ps1` |
| `StokkiEventos_NotificarTransportadoras` | Diário, 04:10 | `rodar_notificar_transportadoras.ps1` |
| `AgenteImportacaoStokki_EmporioQuatroEstrelas` | Seg–Sex, 4x/dia (16:35/17:35/18:35/19:35) | `main.py "Empório Quatro Estrelas"` — **2º projeto, roda em Python 3.13** (`...\Python313\python.exe`), não 3.11 como o resto |
| *(sem tarefa agendada)* `painel_agentes` porta 8070 | Sempre ativo | Confirmado rodando agora via Python 3.11, mas iniciado **por fora do Agendador** — a tarefa `StokkiEventos_PainelAgentesApp` (gatilho de boot) está `Disabled`. Como ele realmente sobe hoje (script manual? outra automação?) é uma pergunta em aberto pro Hugo. |

**Desativadas, mas ainda registradas** (existem no Agendador, `Disabled` —
confirmar com o Hugo se algo aqui ainda precisa entrar no escopo antes de
ignorar de vez):

- `StokkiEventos_ExecutarTudo` (standalone, 10:00/13:00) e `StokkiEventos_Notificador` (standalone, 07:00/16:00) — parecem superseded pelos mesmos passos já embutidos nas sequências de tarde/noite.
- `StokkiEventos_AtualizarAgendamentos` (12:55), `StokkiEventos_RelatorioDiario` (18:07), `StokkiEventos_RelatorioSemanal` (semanal).
- `StokkiEventos_DashboardApp` (boot, porta 8060), `StokkiEventos_DashboardDados` (06:00), `StokkiEventos_DashboardEmailSemanal` (semanal) — todas do `dashboard_embarcadores/`; porta 8060 **não está escutando agora**, parece fora de uso.
- `AgenteExpedicaoStokki_SEG/TER/QUA/QUI/SEX` — apontam pra `C:\agente_relatorio\...\executar_agente_expedicao.bat`, um **projeto/pasta totalmente separado** (existe `agente_relatorio_old`, `agente_relatorio_backup` e um `.zip` do lado) nunca mencionado antes deste levantamento. Parece legado, substituído por `StokkiEventos_ExpedicaoFrequente` — mas não vou tirar do escopo sem o Hugo confirmar.

Confirmação de rotas (aviso diário + sync a cada 30min) — **ainda NÃO
ativadas**, decisão pendente do Hugo (ver
[[project_confirmacao_rotas_motoristas]]), independente deste plano.

### 2.3. Dados: não é só Excel — tem um SQLite de verdade

`dados/dados.db` é o banco operacional real, com 45+ tabelas (`clientes`,
`rotas`, `motoristas`, `pedidos_historico`, `rascunhos_rota`,
`checklist_respostas`, `documentos_pedido`, etc.). As planilhas
(`BD_MOTORISTAS.xlsx`, `BD_CLIENTES.xlsx`, `BD_TRANSPORTADORAS.xlsx`) são
dado mestre lido/gravado por cima disso, não o banco inteiro. Além do
`dados/` raiz (≈738MB, inclui logs e imagens de canhoto), existem bancos/caches
próprios em `painel_agentes/dados/` (≈26MB) e `roteirizacao/dados/`
(≈72MB).

**Importante: praticamente tudo em `dados/*` está no `.gitignore`** (só
`BD_MOTORISTAS.xlsx` e `centro_expandido_sp.json` escapam disso). Ou seja, o
dado de produção NUNCA foi versionado — migrar pra VPS é uma transferência de
arquivo explícita (rsync/scp), não um `git pull`.

### 2.4. Segredos e caminhos

Um único `config.yaml` (gitignored) guarda tudo em texto puro: credenciais
Stokki, token da API Vuupt, chave do Google Maps, caminho da service account
do GCS, chave da API Anthropic, senha de app do Gmail, logins do painel
(admin + leitura), segredos da confirmação de rotas — e caminhos absolutos
do Windows (`C:\agente_stokki_eventos\...`, `C:\agente_importacao_stokki\...`)
misturados com o resto. Isso é as duas coisas ao mesmo tempo: a lista do que
precisa existir na VPS (só que como variável de ambiente/arquivo com
permissão restrita, nunca subir pro GitHub) e a lista de caminhos que
precisam virar caminho Linux.

### 2.5. Sem `requirements.txt`

Não existe `requirements.txt` na raiz do projeto principal — as dependências
só existem implicitamente no ambiente Python 3.11 local (`py -3.11`). Isso
precisa ser resolvido (`pip freeze`) antes de montar qualquer ambiente na
VPS, senão a instalação lá vira tentativa-e-erro.

### 2.6. Estação de impressão

`stokki/estacao_impressao.py` / `somente_impressao.py` imprimem romaneios
direto numa impressora física do escritório — isso não "migra" como código
puro, precisa de uma ponte local (seção 4).

### 2.7. VPS de destino

Hostinger, Ubuntu 24.04.4, IP `187.127.52.197`, já com ufw (22/80/443),
Caddy (HTTPS automático via Let's Encrypt), systemd, chave SSH
`~/.ssh/freshlog_confirmacao_vps`. Hoje roda só `confirmacao-motoristas`
(Flask + SQLite, processo leve, porta interna 8090).

**Baseline medido em 16/08 (Fase 0):** 4 vCPU (AMD EPYC 9354P), 15GB RAM
(482MB em uso, 14GB livre), 192GB de disco (1,7GB usado, 192GB livre), load
average ~0.00. Único processo de app rodando é o `confirmacao-motoristas`
(waitress, ~34MB RSS) + Caddy (~49MB RSS). **Bem mais fôlego do que a
suposição inicial deste plano** ("VPS pequena/leve") — o plano do Hostinger
já contratado tem margem real pra receber o resto do sistema. Ainda assim,
mantém-se a prática de medir de novo depois de cada fase (banco de 738MB +
geração de PDF + jobs em lote é outra ordem de I/O, não só de RAM).

---

## 3. Riscos Principais

1. **Perda de dado real.** `dados.db` e as planilhas nunca foram
   versionados; qualquer erro na transferência é histórico operacional
   perdido de verdade. Mitigação: backup completo antes de CADA fase,
   transferência com checksum, nunca apagar o original local até validar do
   outro lado.
2. **Concorrência no SQLite.** O sistema já quebrou uma vez por dois
   processos escrevendo ao mesmo tempo no mesmo banco (por isso
   `rodar_sequencial.ps1` existe). Rodar local e VPS em paralelo apontando
   pro MESMO arquivo é a mesma armadilha de novo — durante validação, os
   dois lados precisam ter cópias independentes, sincronizadas
   manualmente, nunca o mesmo arquivo sendo escrito por dois hosts.
3. **VPS compartilhada com um serviço já em produção validada.** Se o
   sistema novo consumir CPU/RAM/disco demais, pode derrubar
   `confirmacao-motoristas`, que já funciona. Medir consumo antes de cada
   fase, monitorar depois, ter plano de upgrade do plano Hostinger pronto.
4. **Impressão remota é problema de rede/hardware, não só de código.**
   Trocar mensagem "sim, migra também" por uma solução concreta é o item
   de maior incerteza deste plano (seção 4).
5. **Acoplamento pelo banco compartilhado.** Vários agentes leem e
   escrevem o MESMO `dados.db`/planilhas. "Migrar um módulo por vez" só
   funciona enquanto o módulo migrado e os módulos ainda locais concordam
   sobre ONDE o dado mora. Na prática, a Fase 2 (mover o banco em si) é o
   verdadeiro ponto de corte — depois dela, "por módulo" passa a significar
   "por grupo de jobs que tocam o banco", não mais um agente isolado.
6. **Python 3.11 explícito.** Ubuntu 24.04 traz 3.12 por padrão; será
   preciso instalar 3.11 (deadsnakes PPA) ou validar se o sistema roda sem
   regressão em 3.12.
7. **Sem `requirements.txt` fixado** (ver 2.5) — risco de "funciona só na
   minha máquina" ao montar o ambiente novo.

---

## 4. Arquitetura-Alvo

**Domínio único (pedido do Hugo, 16/08):** serviços novos entram sob
`app.freshhub.com.br/<serviço>` (ex: `/painel`), não um subdomínio por
serviço. `confirmacao_motoristas` é a exceção por ora (fica em
`confirmacao.freshhub.com.br` até migrar, ver Fase 1). Cada serviço Flask
precisa de `ProxyFix(x_prefix=1)` + Caddy `handle_path /<serviço>/*` com
`header_up X-Forwarded-Prefix /<serviço>` pra `url_for()`/links internos
funcionarem sob o prefixo — ver Fase 1 pro padrão completo já validado.

- **VPS (mesma atual) recebe tudo, exceto o print-agent.** Deploy via
  `git clone` do repo já existente no GitHub (não scp manual como foi feito
  pra `confirmacao_motoristas` — esse projeto já tem remoto, aproveitar).
  `painel_agentes` vira serviço systemd de longa duração (`waitress`, igual
  já roda hoje, funciona sem alteração no Linux). As sequências em lote
  (tarde/noite/avulsas) viram scripts chamados por **systemd timer** (ou
  cron) — a lógica de "espera terminar antes do próximo" continua sendo um
  script sequencial único, só troca o agendador por trás.
- **`agente_importacao_stokki`** — primeiro entra sob controle de versão
  (Fase 0), depois ganha deploy e timer próprios (18:30) na mesma VPS.
- **`dados.db` + planilhas migram pra VPS como fonte única** de dado
  operacional. Backup automático pra fora da VPS (o sistema já usa GCS —
  reaproveitar) via cron + rsync/tar, testado de verdade (restaurar, não só
  confirmar que o arquivo existe).
- **Segredos saem do `config.yaml` copiado manualmente** e passam a viver
  só na VPS, com permissão restrita (mesmo padrão que
  `confirmacao_motoristas/infra/.env` já usa) — nunca sobem pro GitHub.
- **Print-agent físico** — o único pedaço que continua rodando perto da
  impressora do escritório (Windows atual ou um mini-PC dedicado, a
  decidir). Fica deliberadamente "burro": só pergunta pra VPS (HTTP
  autenticado, mesmo padrão de polling que `confirmacao_motoristas` já
  usa) "tem romaneio novo pra imprimir?" e manda pra impressora local. Toda
  a lógica (o que imprimir, formatação do romaneio) fica na VPS.

---

## 5. Fases (módulo por módulo)

✅ **Fase 0 — Preparação** (reduz risco de todas as fases seguintes, não migra nada) — **concluída 16/08** (só falta a decisão do print-agent, que não bloqueia o resto)
- [x] Inventário definitivo dos jobs agendados via `Get-ScheduledTask` — ver tabela na seção 2.2 (achou tarefas que os `.ps1` sozinhos não revelavam: `dashboard_embarcadores` desativado, projeto legado `C:\agente_relatorio\` desativado, painel_agentes rodando fora do Agendador).
- [x] `requirements.txt` gerado (`pip freeze` do `py -3.11.7`, 57 pacotes) — ver achado sobre `playwright` precisar de `playwright install` à parte (binário de navegador não vem no `pip install`).
- [x] Baseline de CPU/RAM/disco da VPS medido — ver números reais na seção 2.7 (bem mais folga do que a suposição inicial).
- [x] Backup automático de `dados.db` (snapshot consistente via `VACUUM INTO`, testado com a `SequenciaTarde` rodando ao vivo, sem lock) + as 3 planilhas mestras pro GCS, retenção 30 dias — `backup_dados_gcs.py` (script) + `criar_tarefa_backup_gcs.ps1` (agenda diária 03:00). Testado ponta a ponta com upload real (`gs://freshlog-documentos-pedidos/backups/`) e tarefa `StokkiEventos_BackupGCS` registrada e `Ready` no Agendador.
- [x] `agente_importacao_stokki` sob git — `.gitignore` criado (segredos e dados de runtime excluídos), commit inicial feito (`07bbffb`).
- [ ] Decidir o desenho do print-agent (Windows local vs. mini-PC dedicado) — decisão do Hugo, fica pra quando a migração chegar perto da Fase 7.

**Achado extra (16/08):** `config.yaml` tem duas seções `gcs:` (uma com `bucket`/`service_account_json`, outra com `bucket_name`/`credenciais_json`) — em YAML, chave duplicada faz a segunda sobrescrever a primeira, então só `bucket_name`/`credenciais_json` (a que `storage_gcs.py` e `backup_dados_gcs.py` realmente leem) tem efeito; a primeira seção é morta. Não mexi no `config.yaml` (é o arquivo de segredos do Hugo) — só registrando pra limpar quando ele quiser.

✅ **Fase 1 — Painel web (`painel_agentes`)** — **concluída 16/08**, no ar em `https://app.freshhub.com.br/painel`

**Mudança de arquitetura durante a fase (pedido do Hugo, 16/08):** em vez de cada serviço ganhar seu próprio subdomínio (como `confirmacao.freshhub.com.br`), tudo passa a morar sob um domínio guarda-chuva único, `app.freshhub.com.br`, com um caminho por serviço (`/painel`, e assim por diante conforme mais fases entrarem). `confirmacao_motoristas` **não** migrou pra esse esquema ainda — decisão do Hugo (16/08) de não mexer nela às vésperas da 1ª rodada real com motoristas (17/08); fica em `confirmacao.freshhub.com.br` por enquanto, migra depois.

**Implementação:**
- Deploy em `/opt/stokki-eventos` na VPS (mesma, `187.127.52.197`) via `git clone` com deploy key própria (só leitura) — não mais `scp` manual como o `confirmacao_motoristas`. Python 3.12 (não tem 3.11 disponível no Ubuntu 24.04 e não foi necessário instalar via PPA -- `pip install -r requirements.txt` e o import completo do `painel_agentes` funcionaram sem ajuste).
- **Modo somente leitura, reforçado em duas camadas independentes:**
  1. `config.yaml` da VPS (fora do git, só na VPS) tem `painel_agentes.usuario`/`senha` preenchidos com uma string aleatória descartável, nunca divulgada — o código exige essas duas chaves não-vazias pra qualquer rota funcionar, mas como ninguém sabe o valor, a camada "total" (ações de escrita) fica inacessível de fato. Só `usuario_leitura`/`senha_leitura` (mesmas credenciais já usadas localmente, `equipe`/...) são reais.
  2. Confirmado nos testes: rota de escrita (`POST /api/torre/rodar`) devolve `403` mesmo autenticado como leitura.
  3. Segredos de escrita (`stokki`, `email`, `gcs`, `clientes_agendamento`, `confirmacao_rotas`, `anthropic`) foram **excluídos** do `config.yaml` da VPS -- não são usados por nenhuma rota de leitura (levantamento completo por sub-agente, ver seção 2.2 do histórico da sessão). Único trade-off: o widget "Funil Stokki" da Torre fica sem dado nessa VPS (login Stokki de propósito fora).
- **Réplica de dados, não fonte da verdade:** `dados/dados.db` (snapshot consistente via `VACUUM INTO`) + `BD_MOTORISTAS.xlsx` + `BD_CLIENTES.xlsx` sincronizados da máquina local pra VPS a cada 15 min (`sincronizar_painel_vps.py` + tarefa `StokkiEventos_SincronizarPainelVPS`, one-way, só EMPURRA). A cópia local continua sendo a única escrita pelos jobs do Agendador. **Nota:** tentei também travar o arquivo `dados.db` como read-only (chmod 444) na VPS como camada extra de defesa, mas isso quebra uma rotina legítima de startup (`limpar_execucoes_travadas()`, grava no banco assim que o processo sobe) -- reduzido pra 644 (gravável), já que a proteção real é a camada 1 acima e essa cópia é descartável/sobrescrita a cada sync de qualquer jeito.
- **Prefixo `/painel` via Caddy `handle_path` + Flask `ProxyFix`:** Caddy tira o prefixo antes de repassar pro backend (`header_up X-Forwarded-Prefix /painel`); `painel_agentes.py` usa `werkzeug.middleware.proxy_fix.ProxyFix(x_prefix=1, ...)` pra que `url_for()`/`redirect()` gerem link já com `/painel` na frente. Sem proxy na frente (uso local direto, LAN, porta 8070), os cabeçalhos `X-Forwarded-*` não existem e isso é um no-op -- **zero mudança de comportamento no painel local em produção**, validado com teste antes/depois do cabeçalho.
- **15 ocorrências de caminho absoluto fixo** (`fetch("/api/...")`, `href="/execucao/..."` etc., em `execucao.html`, `planejamento_rotas.html`, `torre_controle.html`) não se beneficiam do `url_for()`/`ProxyFix` automaticamente -- corrigidas manualmente pra usar um `BASE_PATH` JS injetado em `base.html` (`{{ request.script_root }}`, vazio quando não há prefixo).
- Testado ponta a ponta via HTTPS real: certificado emitido, login leitura funciona (`200`), sem login bloqueia (`401`), ação de escrita bloqueada (`403`), navegação interna com link `/painel/...` correto, `confirmacao.freshhub.com.br` sem regressão.

**Bug encontrado e corrigido depois do ar (16/08, teste real do Hugo -- botão "Imprimir rota"):** o romaneio em PDF gerava, mas quebrado de duas formas:
1. `roteirizacao/gerar_pdf_romaneios.py::_fonte()` só tentava fontes do Windows (`C:/Windows/Fonts/arial.ttf` etc.) -- na VPS caía pro bitmap padrão do PIL, que não tem os acentos do português. Corrigido: instalado `fonts-liberation` na VPS (Liberation Sans, métrica compatível com Arial) e adicionado como candidato antes do fallback genérico.
2. Os PDFs físicos de NF/boleto vivem em pastas locais (`documentos_pedido/dados/{downloads_stokki_temp,boletos_separados,nfs_separadas,anexos_temp}/`, NUNCA apagadas depois do upload pro GCS -- ver `documentos_pedido/localizar_arquivos.py`) que eu não tinha sincronizado pra VPS -- todo pedido saía com NF/boleto "não localizado". Corrigido: `sincronizar_painel_vps.py` agora também sincroniza essas 4 pastas (delta só dos arquivos novos, comparando por nome via `find` remoto + tar -- sem rsync, que não está disponível neste Windows; ~318MB no bootstrap inicial, incremental depois).

**Lembrete pra próxima vez:** depois de fazer scp de um `.py` novo pra VPS, sempre `systemctl restart painel-agentes` -- o processo já tinha o módulo velho importado em memória (`sys.modules`), copiar o arquivo no disco sozinho não basta.

**2º bug encontrado depois do primeiro "corrigido" (16/08, Hugo continuou vendo tela branca):** a varredura original dos 15 caminhos absolutos fixos só procurou por `fetch(` e `href=` literais -- passou batido em `postJSON(url, ...)` e `postJSONAgentesPlanejamento(url, ...)`, dois helpers que `planejamento_rotas.html` usa pra TODAS as ~24 chamadas de ação (rodar agente, reordenar, criar rota, alocar motorista etc.), incluindo o próprio botão "Imprimir rota". Corrigido na raiz: os dois helpers agora fazem `fetch(BASE_PATH + url, ...)` em vez de `fetch(url, ...)` -- um ajuste só resolve as ~24 chamadas de uma vez, em vez de editar cada uma. Mais 3 ocorrências pontuais também tinham escapado (`torre_controle.html`: link pro mapa e pro planejamento; `planejamento_rotas.html`: URL do romaneio no botão de imprimir). **Lição:** ao caçar caminho absoluto fixo em JS, procurar por QUALQUER string começando com `/` entre aspas (`grep -oE "[\"'\`][/][a-zA-Z][^\"'\`]*"`), não só pelos nomes de função óbvios (`fetch`/`href`) -- helpers customizados escondem o padrão.

**Validado pelo Hugo em produção (16/08):** botão "Imprimir rota" testado de verdade em `app.freshhub.com.br/painel/planejamento` -- confirmado funcionando.

**Pendente:** Hugo commitar as mudanças (`painel_agentes.py`, 5 templates, `painel_agentes/infra/`, `roteirizacao/gerar_pdf_romaneios.py`, `sincronizar_painel_vps.py`, `criar_tarefa_sincronizar_painel_vps.ps1`) -- por ora só estão no working tree local + copiadas direto pra VPS via scp (não passaram pelo git ainda).

🔶 **Fase 2 — `dados.db` + planilhas como fonte única na VPS** — **iniciada 16/08**
Ponto de corte real (ver risco #5). A partir daqui o banco "mora" na VPS; qualquer job que ainda roda local precisa ler/escrever via rede em vez de arquivo direto, OU migrar junto nesta mesma fase — confirmado no levantamento abaixo: **precisa mesmo migrar os jobs junto**, não dá pra só mover o banco sozinho.

**Levantamento de dependências dos 10 jobs em lote (16/08, sub-agente):**

| Script | Playwright | Config necessário | Risco/criticidade |
|---|---|---|---|
| `validacao_checklists/validar_checklists.py` | Não | `vuupt_api`, `anthropic`, `email` | Baixo -- **melhor piloto**: zero Playwright, e a tarefa nem existe hoje no Agendador (nada em produção pra quebrar) |
| `verificar_pedidos_duplicados_vuupt.py` | Não | `vuupt_api`, `email` | Baixo -- 2º piloto natural, já roda em produção 2x/dia (dá pra comparar local x VPS) |
| `roteirizacao/criar_rotas_diarias.py` | Não | `vuupt_api`, `google_maps`, `motoristas`, `complexidade_entrega`, `clientes_agendamento`, `email` | Alto (desenha as rotas do dia seguinte) |
| `roteirizacao/incrementar_rotas.py` | Não | `vuupt_api`, `google_maps`, `motoristas`, `clientes_agendamento`, `email` | Alto (roda de hora em hora à noite) |
| `roteirizacao/gerar_pdf_romaneios.py` | Não | `vuupt_api`, `motoristas` | Médio-alto -- **já validado rodando na VPS** (Fase 1, botão Imprimir), só falta o timer das 4h |
| `expedir_pedidos.py` | **Sim, direto** | `vuupt_api`, `stokki`, `email` | **Muito alto** -- mais frequente (30/30min) e já causou incidente real (5 dias parado, ~570 pedidos represados) |
| `documentos_pedido/processar_documentos.py` | **Sim, direto** | `vuupt_api`, `gcs`, `email` | Médio |
| `notificacao_transportadoras/notificar_transportadoras.py` | **Sim, direto** | `vuupt_api`, `email`, `stokki` | Médio |
| `executar_tudo.py` | **Sim, indireto** (via `stokki/auth.py` fallback + `stokki/estacao_impressao.py`) | `stokki`, `email`, `vuupt_api`, `anthropic`, `google_maps`, `clientes_agendamento`, `complexidade_entrega`, `importacao_email`, `notificacao_execucao` | **Alto** -- espinha dorsal do pipeline, 2x/dia |
| `notificar_pedidos_em_espera.py` | **Sim, indireto** (mesma cadeia) | `email`, `stokki` | Médio-alto |

**Achado importante: nenhum dos 10 scripts mexe com impressora física.** "Estação de Impressão" é só uma tela do Stokki que muda status de pedido (`/provider/operation/order/printing`), não hardware -- a impressão física do romaneio já é manual hoje (alguém imprime o PDF que `gerar_pdf_romaneios.py` gera). **Ou seja, a Fase 2/5 não precisa esperar o desenho do print-agent (Fase 7)** -- são questões independentes.

**Achados extras a confirmar com o Hugo (não bloqueiam, só registrar):**
- `StokkiEventos_ValidacaoChecklists` não existe no Agendador hoje (nem `Ready` nem `Disabled`) -- o `.ps1` e o script existem e já rodou manualmente em produção (13/08), mas a tarefa em si nunca foi registrada ou foi removida. Perguntar se é intencional.
- `insucesso_entrega/expedir_pedidos.py` é uma cópia desatualizada (12/08) do `expedir_pedidos.py` real da raiz (15/08) -- como `executar_tudo.py` insere `insucesso_entrega/` NO INÍCIO do `sys.path`, um import feito de dentro de `insucesso_entrega/ler_respostas_insucesso.py` pode estar resolvendo pro arquivo antigo por engano. Não investigado a fundo (fora do escopo do levantamento) -- candidato a limpeza antes de comparar resultado local x VPS.

**Pré-requisito antes de qualquer script com Playwright ir pra VPS:** `playwright install` (baixa o Chromium, ~300MB) -- só a lib Python foi instalada até agora (`pip install -r requirements.txt`), sem os binários de navegador.

**Ordem de migração (piloto → maior risco):** validar_checklists → verificar_pedidos_duplicados_vuupt → criar_rotas_diarias/incrementar_rotas → gerar_pdf_romaneios (timer) → [instalar Playwright+Chromium] → expedir_pedidos → processar_documentos/notificar_transportadoras → executar_tudo/notificar_pedidos_em_espera.

**Pilotos 1 e 2 validados na VPS (16/08), rodando de verdade (não só import):**
- `verificar_pedidos_duplicados_vuupt.py --modo-teste` -- consultou VUUPT de verdade (184 serviços, 2 dias), achou 2 grupos de duplicados reais, simulou cancelamento corretamente, notificação por e-mail de execução enviada com sucesso.
- `validacao_checklists/validar_checklists.py --modo-teste --limite 5` -- baixou 5 PDFs de canhoto reais da VUUPT, extraiu foto em alta resolução, classificou via Claude (5/5 aprovados com justificativa coerente) -- pipeline completo (VUUPT + Anthropic + processamento de PDF/imagem) validado rodando nativamente em Python 3.12/Linux.
- `config.yaml` da VPS ganhou `email` (SMTP) e `anthropic` (API) pra viabilizar isso -- `stokki` e `gcs` continuam de fora (só entram com os scripts que dependem de Playwright).
- **Conclusão prática:** ambiente Linux/VPS está validado pra qualquer script SEM Playwright.

**Pilotos 3 e 4 validados (16/08) -- o motor de roteirização inteiro:**
- `roteirizacao/criar_rotas_diarias.py --modo-teste` -- rodou o algoritmo completo: 170 serviços `not_assigned` reais da VUUPT, consolidação de região, os 5 modelos de roteirização comparados por partição (Atual/Sweep/Clarke-Wright/CEP/K-means) com o vencedor escolhido por km, alocação de motorista respeitando rodízio de SP e zona (inclusive identificando corretamente rotas sem motorista elegível) -- 13 rotas simuladas em 65s.
- `roteirizacao/incrementar_rotas.py --modo-teste` -- comportamento correto: **falhou de propósito** (mesma `RuntimeError` intencional que rodaria local) porque não havia rota confirmada de verdade na VUUPT pra 17/08 (os rascunhos do teste anterior foram só simulados, não criados) -- validou que a lógica de fail-safe funciona igual na VPS.
- `clientes_agendamento` adicionado ao `config.yaml` da VPS pra viabilizar isso.
- **Conclusão:** motor de roteirização (geocoding via cache, alocação, seleção de modelo) roda corretamente em Python 3.12/Linux.

**Playwright + Chromium instalados na VPS (16/08):**
- Achado: Ubuntu 24.04 renomeou `libasound2` -> `libasound2t64`, e o instalador do Playwright (`playwright install --with-deps`) ainda referencia o nome antigo -- `apt-get install libasound2` falha (pacote virtual, sem candidato direto). Corrigido criando um pacote dummy via `equivs` (`libasound2` que só depende de `libasound2t64`) -- instalado, aí sim `--with-deps` completou.
- Binário do Chromium baixado tanto pro cache do `root` (sessão SSH) quanto pro de `www-data` (`HOME=/var/www`, mesmo usuário que os serviços systemd usam) -- sem isso o processo real não acharia o navegador.
- Smoke test rodando como `www-data`: Chromium headless abre e navega normalmente.
**Login Stokki testado de verdade (16/08):**
- Achado no código: existem DOIS logins Stokki diferentes -- `stokki.usuario/senha` (nível administrador, usado por `stokki/auth.py::StokkiSession`, a maioria dos scripts) e `stokki.provider.usuario/senha` (nível provider, só `stokki/estacao_impressao.py`).
- Hugo criou um login PROVIDER dedicado pra VPS (`cc@freshlog.com.br`) -- testado e confirmado nível provider (acessa `/provider`, é barrado em `/administrator/...`). Resolve a colisão de sessão pro provider.
- Pro nível administrador, decisão do Hugo: **continuar usando o mesmo `hugo@freshlogbr.com`** (não criar conta dedicada) -- aceita o risco de colisão de sessão nesse nível.
- Testado de verdade com as funções reais do projeto (`stokki.pedidos.contar_pedidos`/`listar_transportadoras`, via `StokkiSession`): login funcionou, retornou dado real (84 pedidos em revisão, 19 abertos, 323 aguardando transportadora etc.) -- **sem incidente de colisão nesse teste**.
- Achado à parte (não é bug, é limitação documentada da API do Stokki): o endpoint `/administrator/inventory/outbound/table` exige o conjunto COMPLETO de ~40 parâmetros que o DataTables do navegador gera (comentário em `stokki/pedidos.py`: "qualquer subconjunto resulta em 500") -- um teste ad-hoc simplificado bateu nisso e pareceu (incorretamente) um problema de sessão/CSRF. Usar sempre as funções prontas do módulo (`contar_pedidos`, `listar_transportadoras` etc.), nunca montar a chamada à mão.
- O aviso "CSRF token não encontrado" no login continua aparecendo (a conta administrador não tem acesso a `/provider`, então o código não consegue capturar o token nessa etapa) -- inofensivo pra chamadas GET, não testado ainda se afeta POSTs (ex: `expedir_pedidos.py` anexando canhoto).

**Pilotos 5, 6 e 7 (16/08) -- grupo Playwright:**
- `expedir_pedidos.py --modo-teste --limite 3` -- consultou VUUPT real (20 entregues com canhoto, 2 insucessos), notificação de pendência enviada. Nenhum pedido validado na janela pra exercitar o anexo real no Stokki, mas o resto do fluxo validou certo.
- `notificacao_transportadoras/notificar_transportadoras.py --modo-teste --data hoje` -- reaproveitou sessão Stokki salva em cookies (não precisou logar de novo), 0 rotas pra hoje, concluiu limpo. Precisou copiar `BD_TRANSPORTADORAS.xlsx` pra VPS (esquecido antes) -- `sincronizar_painel_vps.py` corrigido pra incluir essa planilha daqui pra frente.
- `documentos_pedido/processar_documentos.py --modo-teste` -- precisou do `gcs.bucket_name`/`credenciais_json` (copiado pro VPS, antes só existia pro painel read-only). Rodando de verdade: 6 e-mails novos via IMAP, login provider com `cc@freshlog.com.br` confirmado, paginação de 328 pedidos reais no Stokki, classificação de documento e simulação de envio pro GCS (`[TESTE] Enviaria pro GCS: ...`) tudo funcionando. **Nota operacional:** a conexão SSH caiu no meio de um teste anterior (~4min) -- o processo remoto SOBREVIVEU (não estava preso a pty/sessão), continuou rodando e completando sozinho -- confirma que processos disparados via `ssh host "comando"` sem `-t` não morrem se a conexão cair.

**Piloto 8 -- `executar_tudo.py --modo-teste` (a espinha dorsal do pipeline):** rodou completo em 10,8 minutos, 341 pedido(s) processado(s) (0 erro, 31 em revisão manual -- classificação de negócio normal, não falha), gerou os arquivos de histórico (`STATUS_IMPORTACAO.md`, CSV de importação) e notificação de execução enviada.

**Piloto 9 -- `notificar_pedidos_em_espera.py --modo-teste`:** 28 pedido(s) "On hold" reais encontrados, 8 e-mail(s) de teste enviados (redirecionados pra `hugo@freshlogbr.com`, nunca pro destinatário real -- proteção do próprio `modo_teste`), 0 falhas.

**✅ TODOS OS 10 SCRIPTS EM LOTE VALIDADOS RODANDO DE VERDADE NA VPS (16/08).** `roteirizacao/gerar_pdf_romaneios.py` já tinha sido validado na Fase 1 (botão Imprimir). Ambiente Python 3.12/Linux + Playwright/Chromium + os dois níveis de login Stokki + GCS + Anthropic + Google Maps + SMTP/IMAP -- tudo funcionando.

## ✅ CORTE EXECUTADO (madrugada 17/08) -- VPS é a fonte real a partir daqui

Decisão do Hugo: corte imediato (janela de madrugada, antes do RomaneiosManha 04:00), com acesso total liberado no painel da VPS na mesma janela.

**Sequência executada:**
1. `painel_agentes.usuario/senha` da VPS trocado do valor aleatório descartável pro real (`hugom`/mesma senha do painel local) -- acesso total liberado, testado (`HTTP 200` em `/torre` com as credenciais reais).
2. As 6 tarefas locais relevantes desativadas no Agendador do Windows (`StokkiEventos_SequenciaTarde`, `SequenciaNoite`, `ExpedicaoFrequente`, `RomaneiosManha`, `NotificarTransportadoras`, `SincronizarPainelVPS`) -- confirmado `Disabled` via `Get-ScheduledTask`. `AgenteImportacaoStokki_*` (2º projeto, banco separado) e `StokkiEventos_BackupGCS` (continua útil) ficaram de fora, fora do escopo deste corte.
3. Sync final local->VPS rodado manualmente (`sincronizar_painel_vps.py`) -- última foto do `dados.db`/planilhas/documentos antes do banco local congelar de vez.
4. **Timers systemd criados e ativados** (`infra/sequencia_tarde.sh`, `infra/sequencia_noite.sh` -- shell scripts sequenciais equivalentes aos `.ps1`, com `set -e`; + 5 pares `.service`/`.timer`): `stokki-sequencia-tarde` (18:00), `stokki-sequencia-noite` (22:00), `stokki-expedicao-frequente` (a cada 30min, 08:00-19:30), `stokki-romaneios-manha` (04:00), `stokki-notificar-transportadoras` (04:10).
5. **Achado crítico, corrigido na hora:** a VPS estava com timezone `Etc/UTC`, não Brasília -- os timers, calculados sobre `OnCalendar` (que usa o timezone do sistema), iam disparar 3h mais cedo do que o pretendido (`04:00` rodaria às 01:00 de verdade). Corrigido com `timedatectl set-timezone America/Sao_Paulo` **antes** de qualquer timer disparar errado -- `stokki-sequencia-noite` estava a 13 min de disparar no momento do fix. Timers recalculados corretamente pra `-03` depois disso.
6. Painel local (porta 8070, processo Python direto) encerrado -- painel da VPS (`app.freshhub.com.br/painel`, agora com acesso total) é o único painel em uso a partir daqui.

**✅ Primeira execução real (não-teste) validada: `stokki-sequencia-noite`, 22:00-22:14 (17/08), sucesso.** `ExecutarTudo` completo, `VerificarDuplicadosVuupt` achou 37 grupos (0 cancelados -- só cancela de verdade com `--cancelar-duplicados`, nunca usado nem localmente, comportamento correto/inalterado), `IncrementarRotas` falhou de propósito (rascunho de 17/08 não confirmado em `/planejamento` -- 143 pedidos ficaram sem alocar, aguardando confirmação manual do Hugo). Notificações de execução enviadas em cada etapa. Primeiro ciclo real do Agendador rodando 100% na VPS, sem intervenção manual.

**Pendente de validação:** os demais timers no primeiro ciclo completo (~24h): `stokki-romaneios-manha` (04:00), `stokki-notificar-transportadoras` (04:10), `stokki-expedicao-frequente` (08:00-19:30), `stokki-sequencia-tarde` (18:00). Rollback NÃO é simples depois de execuções reais (não-teste) na VPS -- se algo sair errado depois de escrita real, o caminho é corrigir pra frente na VPS, não reverter pro local (o `dados.db` local já ficou pra trás e voltar a usá-lo perderia o que a VPS já processou).

**Login de verdade + botão de sair (17/08, pedido do Hugo):** o painel usava HTTP Basic Auth (senha guardada pelo navegador, sem jeito confiável de "deslogar"). Trocado por sessão de verdade: `POST /login` (usuário/senha do `config.yaml`, mesmos dois níveis de sempre) grava `session["nivel_acesso"]` num cookie assinado (`painel_agentes.secret_key`, novo no config, local e VPS -- **nunca trocar sem motivo, invalida toda sessão ativa**), `POST /logout` limpa a sessão. Sessão dura 14 dias (uso operacional diário, não devia pedir login toda hora). Rotas `/api/*` sem sessão devolvem `401` JSON (pra não quebrar `fetch()`/`postJSON()`); rotas de página redirecionam pra `/login?proximo=<onde estava>`, com validação de que `proximo` é um caminho deste próprio painel (proteção contra open redirect). Testado local (porta de teste) e na VPS via HTTPS real -- login, sessão, nível leitura x total, 401 de API sem sessão, logout, tudo validado.

**Follow-ups resolvidos (17/08, madrugada):**
- ✅ Redirect do Caddy corrigido: era `/painel` -> `/painel/torre` (escondia a tela "Agentes" até pro admin) -- agora só normaliza a barra (`/painel` -> `/painel/`) e deixa o Flask decidir quem vê o quê. Testado: `/painel/` com sessão admin agora abre a tela "Agentes" (200).
- ✅ Backup GCS movido pra rodar na própria VPS: `backup_dados_gcs.py` deployado + `stokki-backup-gcs.timer` (systemd, 03:00, mesmo script/horário de antes) -- agora faz backup do banco de VERDADE, não mais do `dados.db` local congelado. Testado com upload real (`gs://freshlog-documentos-pedidos/backups/...`).

**Follow-ups ainda pendentes:**
- A tarefa local `StokkiEventos_BackupGCS` (backup do `dados.db` local, agora redundante) continua `Ready` no Agendador do Windows -- não desativei ainda (precisa do Hugo rodar como admin; baixa urgência, só desperdiça um backup inútil às 3h se ninguém desativar antes).
- Nenhuma tarefa equivalente à `StokkiEventos_ValidacaoChecklists` foi criada na VPS (ela já não existia no Agendador local -- ver achado da Fase 2, seção acima).
- Muita coisa de hoje (Fase 2 inteira + login/logout + scripts de corte) ainda não foi commitada no git.

⏳ **Fase 3 — Sequência da tarde** (ExecutarTudo → VerificarDuplicados → CriarRotasDiarias → Notificador → ProcessarDocumentos), systemd timer 18:00.

⏳ **Fase 4 — Sequência da noite** (ExecutarTudo → VerificarDuplicados → IncrementarRotas), systemd timer 22:00.

⏳ **Fase 5 — Tarefas avulsas** (expedição frequente, romaneios de manhã, notificar transportadoras, validação de checklists).

⏳ **Fase 6 — `agente_importacao_stokki`** (e-mail → XML → Stokki), 18:30.

⏳ **Fase 7 — Print-agent** (impressão remota) + desligamento definitivo do Agendador do Windows local.

⏳ **Fase 8 — Ativar as 2 tarefas de confirmação de rotas** hoje manuais (aviso diário + sync 30min) — só depois do Hugo validar a 1ª rodada real (17/08), como já combinado em [[project_confirmacao_rotas_motoristas]].

Cada fase: roda em paralelo (local ainda ativo, VPS já ativo mas só um lado é "fonte da verdade" por vez) por alguns dias, compara resultado, só então desliga a tarefa local equivalente no Agendador do Windows.

---

## 6. Critérios de Aceitação Gerais

- [ ] Todo job do Agendador do Windows tem equivalente rodando via systemd timer/cron na VPS, mesmo horário e mesma ordem/dependência.
- [ ] `painel_agentes` acessível via HTTPS na VPS pra equipe, com os 2 níveis de acesso (admin/leitura) preservados.
- [ ] `dados.db` e planilhas na VPS com backup automático **restaurável de verdade** (testado, não só "existe arquivo").
- [ ] Nenhum segredo commitado no GitHub em nenhum momento da migração.
- [ ] Impressão de romaneio funcionando a partir da lógica gerada na VPS.
- [ ] Máquina local do Hugo pode ficar desligada (exceto o print-agent, se for essa a escolha) sem nenhum agente parar de rodar.
- [ ] Rollback claro em cada fase: se algo quebrar, sabe-se exatamente como voltar a rodar 100% local naquele módulo.

---

## 7. Próximos Passos Imediatos

1. Hugo revisa este plano e ajusta o que fizer sentido (ordem das fases, desenho do print-agent).
2. Iniciar a Fase 0 (levantamento completo dos jobs + `requirements.txt` + backup automático pro GCS) — pode começar já, não depende de mais nenhuma decisão.
3. Decidir o desenho do print-agent antes de a migração chegar na Fase 7.
