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

⏳ **Fase 1 — Painel web (`painel_agentes`)**
Candidato natural pra ir primeiro: já roda via `waitress` (produção-ready), já é acessado remotamente em conceito (equipe via navegador), menor acoplamento com o Agendador. Critério de aceite: equipe acessa o painel pela VPS, dado bate 1:1 com o que aparecia local.

⏳ **Fase 2 — `dados.db` + planilhas como fonte única na VPS**
Ponto de corte real (ver risco #5). A partir daqui o banco "mora" na VPS; qualquer job que ainda roda local precisa ler/escrever via rede em vez de arquivo direto, OU migrar junto nesta mesma fase — provavelmente mais simples puxar todos os jobs que tocam `dados.db` numa fase só, pra nunca ter dois donos do mesmo arquivo ao mesmo tempo.

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
