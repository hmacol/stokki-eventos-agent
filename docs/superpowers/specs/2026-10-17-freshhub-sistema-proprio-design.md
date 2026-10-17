# Fresh Hub: sistema próprio, sem Stokki, Vuupt e Lovable

Data: 17/10/2026
Tipo: plano mestre (arquitetural). Cada fase ganha spec e plano próprios.
Decisões do Hugo em 17/10/2026, nas seções 0 e 10.

## 0. Brief

**Pedido:** um plano passo a passo pra chegar num sistema 100% da Fresh Log,
excluindo Stokki (WMS), Vuupt (execução de rotas) e Fresh Hub do Lovable
(Supabase) das dependências, cobrindo telas, banco de dados, segurança e
infra.

**Decisões que moldam tudo (Hugo, 17/10):**

| # | Decisão |
|---|---------|
| 1 | **Robustez acima de velocidade.** O corte de cada fornecedor só acontece com certeza absoluta; não há pressa pra tirar os fornecedores. |
| 2 | Time: **Hugo + Claude**, como hoje. Sem dev contratado. |
| 3 | O Fresh Hub do **Lovable sai junto**. |
| 4 | Fiscal: **só receber o XML da NF-e** do embarcador. Nenhuma emissão (CT-e, MDF-e) nossa. |
| 5 | Infra: **aplicação na VPS + Postgres gerenciado**. |
| 6 | O portal do embarcador cobre **tudo** que ele faz na Stokki hoje: lançar pedido (XML ou planilha), estoque com lote e validade, status e canhoto, relatórios e financeiro. |
| 7 | Caminho: **fundação primeiro, depois uma trilha por fornecedor** (seção 6). |
| 8 | Mesmo repositório, backend em Python, front em TypeScript (seção 1). |
| 9 | Modelo de dados **novo, com IDs próprios**; IDs externos viram referência (seção 2). |
| 10 | **Logins nominais pra todo mundo**, inclusive galpão (seção 3). |
| 11 | Homologação com banco próprio; **deploy só por Action**, nunca à mão (seção 5). |
| 12 | Ordem das fases: Pedido → TMS (Vuupt) → WMS (Stokki); Lovable sai na Fase 1. |
| 13 | Nome do pacote novo: `freshhub`. |

**O que já existe e este plano aproveita, não reabre** (ver ADR-0001, 0008,
0009 e `DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md`):

- Saída da Vuupt por etapas reversíveis, replicando nela até o financeiro
  fechar um mês com relatório nosso. Etapas 0–2 prontas: backup horário,
  exportação do histórico pro GCS, espelho `nucleo_*` com comparador diário
  em 0 divergências, relatórios do financeiro reproduzidos (98–99% das
  células).
- Roteirizador próprio; a Vuupt só executa. Pool do planejamento já lê do
  núcleo em sombra (`planejamento.fonte_pool`), virada pendente.
- App Android dos motoristas no ar (APK 1.1.0, 17 logins), sem rota real.
- WMS próprio fases 1 e 2 no ar (posições, recebimento, reserva), vazio na
  operação.
- Km e tarifa do motorista: cálculo próprio, Vuupt ignorada.

**Fora de escopo, de propósito:** emissão fiscal; iOS (decisão de 26/08: só
Android); Central de WhatsApp Evolution (parada por decisão do Hugo);
integração com fornecedor novo. Lalamove continua: é transportadora, não
dependência de sistema.

## 1. Arquitetura alvo

### 1.1 Um repositório, um pacote novo

O sistema novo nasce neste repositório, no pacote `freshhub/`. A pasta
atual `freshhub/` (scraping do Lovable: `auth.py`, `tasks.py`,
`pedidos_parados.py`) é renomeada pra `lovable/` na primeira tarefa da
Fase 0, com os imports ajustados.

Por quê no mesmo repo: a lógica a migrar já está aqui com histórico git; CI
e deploy ficam um só; o legado importa do pacote novo durante a transição.
Repo separado obrigaria a copiar código sem histórico e manter dois
pipelines pra uma pessoa.

### 1.2 Linguagens

- **Backend em Python.** As regras de negócio já são Python e o time é uma
  pessoa: trocar de linguagem no backend é o maior risco evitável.
- **Front em TypeScript.** O app dos motoristas já é TypeScript (Expo); o
  front web compartilha tipos e cliente de API com ele.

### 1.3 Camadas

```
freshhub/
  dominio/      regras puras, sem banco e sem rede (migradas de regras/,
                roteirizacao/, nucleo/)
  modelos/      tabelas (SQLAlchemy 2) + migrações (Alembic)
  servicos/     casos de uso: criar pedido, planejar rota, expedir,
                fechar extrato
  adaptadores/  Stokki, Vuupt, Lalamove, Google, e-mail, WhatsApp,
                atrás de interfaces
  api/          FastAPI: rotas tipadas, OpenAPI gerado, autenticação única
  jobs/         entradas de linha de comando dos timers (mesmo systemd)
web/            React + TypeScript + Vite, PWA, uma aplicação com 3 áreas:
                painel, portal, galpão
app_motorista/  continua Expo; passa a usar o cliente de API gerado do
                OpenAPI
```

Regras de dependência: `dominio` não importa nada das outras camadas;
`servicos` usa `dominio`, `modelos` e as interfaces de `adaptadores`;
`api` e `jobs` só chamam `servicos`. Um teste de arquitetura (import-linter
ou equivalente) reprova violação.

### 1.4 Módulos de domínio

Um dono por tabela, como no ADR-0005:

| Módulo | Dono de |
|---|---|
| Identidade e acesso | usuários, papéis, permissões, sessões, auditoria |
| Cadastros | embarcador, destinatário e endereço de entrega, motorista, veículo, transportadora, tarifa |
| Pedidos | pedido, eventos do pedido, NF/XML, documentos, ocorrências, agendamento |
| Armazém | produto, lote, posição, saldo, movimento, recebimento, separação, expedição, inventário |
| Transporte | rascunho, rota, parada, execução, comprovante, marketplace, confirmação |
| Financeiro | lançamento, extrato do motorista, cobrança do embarcador, cotação |
| Comunicação | e-mail, WhatsApp, chamados, notificações e preferências |

### 1.5 Integrações como adaptadores

Duas interfaces centrais:

- `ExecutorDeRota`: implementações `Vuupt`, `AppProprio`, `Lalamove`.
- `ArmazemExterno`: implementações `Stokki`, `Proprio`.

Cortar um fornecedor é trocar a implementação no config, que é o que o
ADR-0001 pede (`fonte: vuupt | nucleo` generalizado). O legado (Flask,
scripts soltos) continua rodando e é esvaziado módulo a módulo conforme
cada tela e job passa pro pacote novo.

### 1.6 ADRs novos

Três decisões contradizem ADRs vigentes e ganham ADR próprio na Fase 0:

- ORM + migrações (ADR-0005 rejeitou "enquanto for SQLite").
- Front com build step (ADR-0006 e `PRODUCT.md` assumem Jinja sem build).
- Postgres gerenciado como banco único (ADR-0005 adiou).

## 2. Banco de dados e migração

### 2.1 Postgres gerenciado, um banco, esquemas por módulo

Um banco `freshhub` com schemas `identidade`, `cadastros`, `pedidos`,
`armazem`, `transporte`, `financeiro`, `comunicacao` e `legado` (tabelas
espelhadas do SQLite durante a convivência). Esquema por módulo mantém a
regra "um dono por tabela" e deixa visível quando uma consulta cruza
fronteira.

**Provedor recomendado: Google Cloud SQL for PostgreSQL**, região
`southamerica-east1` (São Paulo).

- Por quê: a Fresh Log já tem projeto, billing e bucket no Google (backup
  em GCS, Maps, DANFE); PITR e backup automático são nativos; TLS e
  usuário por papel nativos; exportação pro GCS por comando. Instância
  pequena (1 vCPU, 3,75 GB, 20 GB SSD) fica na faixa de R$ 150 a 300/mês.
- Alternativa: Supabase Pro em `sa-east-1` (Postgres puro também; PITR é
  complemento pago, custo sobe). Fica como reserva se o Cloud SQL
  decepcionar na prova da Fase 0.
- Critério que decide, independente do provedor: **restaurar pra um
  instante qualquer dos últimos 7 dias em menos de 30 minutos, ensaiado.**

Conexão da VPS por IP autorizado + TLS obrigatório; usuário de aplicação
sem DDL; migrações com usuário próprio.

### 2.2 Modelo novo, não cópia do velho

As 111 tabelas de hoje cresceram por acreção (`pedidos_parados_*` em 5
versões, `rotas` morta, `nucleo_*` chaveado por ID da Vuupt). O modelo novo
tem **IDs próprios** e os IDs externos viram colunas de referência
(`vuupt_service_id`, `vuupt_agent_id`, `stokki_pedido_id`,
`stokki_sender_id`, `lalamove_order_id`), nunca chave.

Entidades centrais:

- **Pedido**: um por código (`PS-41705`; reentrega `PS-41705-R1` é pedido
  próprio ligado ao original). Estado atual + tabela `pedido_evento`
  append-only (criado, importado, roteirizado, saiu, entregue, insucesso,
  cancelado, reagendado, endereço alterado...). O estado é derivado dos
  eventos e cacheado.
- **Rota** e **Parada** (ordem, janela, previsão, chegada, saída, resultado).
- **Embarcador** (cadastro, usuários, preferências, dias fixos, bloqueios).
- **Destinatário** com **endereço de entrega versionado** (histórico de
  mudanças; o pedido aponta pra versão usada).
- **Motorista** e **Veículo** (placa, tipo, rodízio, disponibilidade).
- **Produto**, **Lote** (validade, fabricação), **Posição**, **Saldo** por
  produto × lote × posição, **Movimento de estoque** append-only.
- **Comprovante** (foto, assinatura, GPS, hora; arquivo no bucket).
- **Lançamento financeiro** append-only (tarifa, km, pedágio, ajuste),
  com extrato e cobrança derivados.
- **Auditoria** (quem, quê, quando, de onde) append-only.

### 2.3 Dois princípios

1. **Histórico é append-only.** Eventos do pedido, movimentos de estoque,
   lançamentos e auditoria nunca sofrem UPDATE. O estado atual é derivado
   ou cacheado, nunca a única verdade. Elimina a classe de bugs "status
   voltou pra ABERTO" medida em setembro.
2. **Toda data tem fuso.** `timestamptz`, gravada em UTC, exibida em
   `America/Sao_Paulo`. Acaba a mistura UTC × local na mesma coluna.

### 2.4 Migração em três movimentos

1. **Carga inicial** (Fase 0): script idempotente lê `dados/dados.db` e o
   arquivo da Vuupt no GCS (7.844 rotas, 62 mil serviços, 59 mil
   checklists, 11 mil destinatários) e popula o modelo novo. Roda quantas
   vezes precisar numa cópia; divergências viram relatório por categoria.
   Normalização de código, fuso e `#PS` aplicada na carga.
2. **Convivência** (Fases 1–3): enquanto um módulo roda no legado, o SQLite
   continua sendo escrito por ele e um sincronizador **traduz pro modelo
   novo** (mesma tradução da carga inicial, incremental), guardando a
   linha bruta no schema `legado` só pra auditoria e comparação. O módulo
   novo lê só do modelo novo; nada lê de `legado` em tela.
3. **Virada por módulo**: quando as telas e jobs do módulo passam pro
   pacote novo, o Postgres vira a única escrita e a tabela SQLite congela.
   Um comparador por módulo é o portão, com dias limpos como placar.

### 2.5 Migrações versionadas

Alembic desde a primeira tabela; nenhum `CREATE TABLE IF NOT EXISTS` no
pacote novo. Toda mudança de esquema roda em homologação antes da
produção, pela Action de deploy.

### 2.6 Fica fora

`dados/atendimento.db` (central parada por decisão do Hugo). O
`confirmacoes.db` vira parte do módulo Transporte quando o marketplace
migrar (Fase 2).

## 3. Segurança

Situação: cinco jeitos de entrar hoje (painel por nível, portal por
CNPJ+PIN, motorista por CPF+PIN, galpão por aparelho compartilhado,
confirmação por link). A revisão de 07/10 achou PIN padrão `123456` abrível
só com o CNPJ (ALTO) e POSTs autenticados sem proteção de origem. O sistema
novo resolve na fundação, não tela a tela.

### 3.1 Uma identidade, vários perfis

Tabela única `usuario` com tipo (`interno`, `embarcador`, `motorista`,
`galpao`) e vínculo (usuário de embarcador → embarcador; motorista →
cadastro de motorista). **Logins nominais sempre**: acaba o usuário
"operador" compartilhado e a senha fixa do aparelho do galpão. O galpão
mantém operador + PIN de 4 dígitos, por cima de uma sessão de aparelho
registrada (aparelho cadastrado, revogável).

### 3.2 Autenticação

- Senha com Argon2id. PIN só onde o uso justifica (motorista no celular,
  galpão), com bloqueio progressivo após erros.
- Troca obrigatória no primeiro acesso (já existe no portal e no app; vira
  regra geral).
- Web: cookie de sessão `HttpOnly`, `Secure`, `SameSite=Lax`, rotacionado
  no login. App: token de curta duração + renovação, revogável por
  aparelho.
- Segundo fator (TOTP) opcional pra perfis internos de nível alto.
- Recuperação por link de uso único por e-mail ou WhatsApp. PIN nunca é
  reenviado em texto.

### 3.3 Autorização em um lugar só

Permissões por papel e por recurso, declaradas no backend e aplicadas na
camada de serviço; o front só esconde o que a API já nega. Embarcador vê
exclusivamente dados do próprio embarcador: filtro imposto no serviço, não
na tela, coberto por teste que tenta vazar (seção 7). Os níveis internos de
hoje (total, operador, expedição, galpão, leitura, atendimento) viram
papéis com o mesmo nome.

### 3.4 API

CSRF por token de origem em toda escrita; limite de taxa por IP e por
conta no login e nos endpoints públicos (insucesso, confirmação de rota);
validação de entrada por schema (Pydantic, campos extras proibidos);
cabeçalhos de segurança (CSP, HSTS) no Caddy.

### 3.5 Segredos e dados

- Segredos saem do `config.yaml` em texto: arquivo de ambiente do systemd
  com permissão `0600` em produção, secrets do GitHub no CI. Rotação
  documentada por segredo.
- Banco com TLS obrigatório; usuário de aplicação sem DDL.
- Fotos de canhoto, XML e PDFs em bucket privado, URL assinada de curta
  duração, nunca caminho público. Backups cifrados.

### 3.6 Auditoria

Quem fez o quê, quando, de onde, em toda escrita relevante (cancelar
pedido, expedir, mudar endereço, fechar extrato, alterar permissão).
Append-only, consultável no painel por pedido, por usuário e por período.

### 3.7 LGPD

CPF, telefone e endereço de destinatário com acesso restrito por papel e
mascarados em listas. Retenção definida por tipo (canhoto 5 anos, logs 1
ano, sessões 30 dias). Exportação e exclusão dos dados de um titular como
função do sistema, não consulta à mão.

### 3.8 Rotina

Dependências varridas no CI (`pip-audit`, `npm audit`); revisão de
segurança obrigatória antes de deploy que toque módulo exposto (skill
`differential-review`/`security-review` que já existe); teste de
restauração e teste "embarcador A lê pedido do B" rodando em todo CI.

## 4. Telas

### 4.1 Uma aplicação, três áreas, um sistema de design

React + TypeScript + Vite, servida pelo Caddy em `app.freshhub.com.br` com
rotas `/painel`, `/portal`, `/galpao`. Tailwind + shadcn (mesma base do
Fresh Hub do Lovable). Paleta atual: navy `#141428`, verde `#0EA575` (claro)
/ `#2FD9AE` (escuro), vermelho `#E1483D`, âmbar `#E8A33D`; tema claro e
escuro. Tokens de design num arquivo compartilhado com o app Expo.

**Contrato primeiro:** o OpenAPI gerado pelo FastAPI gera o cliente
TypeScript; nenhuma tela chama URL na mão. Mudança de API que quebra tela
aparece na compilação.

### 4.2 Painel interno (substitui o Flask, tela a tela)

- *Acompanhar*: Início (pendências por papel), Torre de Controle (desktop e
  celular com o mesmo código), Pedidos Parados, Batimento, Consulta de
  pedido, Saúde do sistema.
- *Planejar*: Pool e Planejamento de Rotas (arrastar, mapa, Lalamove),
  Motoristas e disponibilidade, Marketplace de rotas, Calendário de
  feriados e regiões.
- *Operar*: Expedição, Documentos e impressão, Romaneios, Atendimento e
  chamados.
- *Cadastros e Sistema*: embarcadores, destinatários, motoristas, veículos,
  transportadoras, tarifas, usuários e papéis, auditoria, agentes e timers
  com log.

### 4.3 Portal do embarcador (substitui a Stokki pra quem é de fora)

- Pedidos: lançar por XML ou planilha; acompanhar status com linha do
  tempo e canhoto; cancelar, reagendar, pedir reentrega; ocorrências e
  resposta de insucesso.
- Estoque: saldo por produto, lote e validade; entradas e saídas; pedidos
  de entrada (chegada de mercadoria); inventário.
- Documentos: NF, romaneio, comprovantes, com download.
- Financeiro: extrato de serviços do período, cotação de frete, boletos
  quando existirem.
- Cadastro: destinatários com endereço de entrega e janelas; usuários do
  próprio embarcador.
- Chamados com a Fresh Log.

É a área com mais telas e a que decide se a Stokki pode sair.

### 4.4 Galpão (PWA no celular ou coletor; câmera e leitor HID)

Recebimento e conferência, endereçamento, separação por pedido com FEFO
sugerido, conferência de saída, expedição por rota, inventário cego,
etiquetas (Elgin L42 Pro, 10 × 5 cm, já calibrada).

### 4.5 App do motorista (continua Expo)

Ganha o que falta pra executar rota sem a Vuupt: aceitar e iniciar rota,
navegação, checklist por parada, foto, assinatura, GPS, insucesso com
motivo, fila offline, extrato, confirmação de rota (absorve o
`confirmacao_motoristas`).

### 4.6 Regras de tela

Celular em primeiro lugar nas áreas de campo (galpão, motorista, Torre);
listas densas e estado legível no card (`PRODUCT.md`); nada de recarregar
a página inteira a cada ação; tempo real onde a operação precisa (Torre,
galpão) via SSE; toda tela com estado vazio, erro e carregando desenhados;
acessível por teclado.

**Ordem:** nenhuma tela é feita "porque existe hoje"; cada uma entra quando
a fase dela a exige e substitui a Flask correspondente no mesmo deploy.

## 5. Infra, ambientes e entrega

### 5.1 Dois ambientes

- **Homologação** `hml.freshhub.com.br`: banco próprio no mesmo provedor
  (cópia anonimizada da produção, refeita toda semana por job), bucket
  próprio, adaptadores externos em modo sandbox (Lalamove) ou gravação
  (Stokki, Vuupt). Tudo que vai pra produção passa antes por aqui,
  inclusive migração de esquema.
- **Produção**: VPS atual (Ubuntu 24.04, Caddy, systemd) serve a
  aplicação; Postgres gerenciado fora dela. Se a VPS cair, o dado está
  intacto e uma VPS nova sobe a partir do código e do bucket.

### 5.2 Processos

API FastAPI com uvicorn atrás do Caddy; front estático pelo Caddy; jobs
como timers systemd (provados, com `OnFailure=` e alerta) chamando
`python -m freshhub.jobs.<nome>`. Os 5 serviços web de hoje viram 1 API +
1 front, mais o legado enquanto durar. Sem Kubernetes nem orquestrador.

### 5.3 Arquivos

Fotos, XML, PDFs e exportações saem do disco da VPS pro bucket GCS (já
existe pro backup), com URL assinada e ciclo de vida (retenção
automática). O disco da VPS passa a ser descartável.

### 5.4 Entrega automatizada (GitHub Actions)

1. A cada push: lint (ruff, eslint), tipos (mypy, tsc), testes de domínio e
   de API com Postgres em container, build do front, varredura de
   dependências. Vermelho não entra.
2. Merge em `master`: deploy automático em homologação + testes de ponta a
   ponta (Playwright) contra ela.
3. Produção: deploy por aprovação manual do Hugo na Action: pull, migração,
   reinício e **prova de fumaça** (chama rotas principais e compara com o
   esperado). Falhou, volta sozinho pro commit anterior.

Acaba `ssh` + `git pull` + `chown` à mão (fonte do "arquivo com dono
errado").

### 5.5 Observabilidade

Logs estruturados (JSON) de API e jobs num lugar só (Grafana Cloud no
plano gratuito, ou equivalente), com correlação por pedido e por rota.
Métricas: latência e erros da API, duração e atraso de cada job, pedidos
por status, fila offline do app. Alertas: job não rodou na janela, erro
acima do normal, fila do app crescendo, certificado vencendo, disco e
banco perto do limite. Tela "Saúde do sistema" no painel lê daqui, e os
comparadores de cada módulo publicam o placar nela.

### 5.6 Backup e recuperação

Banco: PITR do provedor + snapshot diário exportado pro GCS (não confiar
só no provedor). Arquivos: versionamento do bucket. **Ensaio de
restauração mensal completo**: banco + bucket + VPS limpa, cronometrado e
registrado. Meta: operação de volta em menos de 1 hora.

### 5.7 Segredos

Arquivo de ambiente do systemd em produção e homologação; secrets do
GitHub no CI. `config.yaml` deixa de existir no sistema novo (o legado
continua lendo o dele até ser desligado).

### 5.8 Custo esperado (ordem de grandeza)

Postgres gerenciado R$ 150 a 300/mês (duas instâncias pequenas, prod e
hml, ou uma com dois bancos); VPS atual; GCS poucos reais; observabilidade
e CI nos planos gratuitos. Alerta de orçamento no provedor.

## 6. Fases e critérios de corte

Cada fase termina num **portão** com critério medível; só se avança com o
portão verde. Durações são ordem de grandeza pra Hugo + Claude, com ~30% do
tempo reservado pra operação e manutenção do legado. O que manda é o
portão, não o calendário.

### Fase 0 · Fundação (2 a 3 meses)

Entregas:

- Renomear `freshhub/` (Lovable) → `lovable/`; criar o pacote `freshhub/`.
- Postgres gerenciado (prod e hml), Alembic, modelos de Identidade,
  Cadastros e Pedidos.
- Identidade e papéis; API; front com as 3 áreas e o shell (login, menu,
  tema).
- Homologação, CI/CD, observabilidade, bucket, secrets.
- Carga inicial do histórico (SQLite + arquivo da Vuupt) com relatório de
  divergências.
- Sincronizador SQLite → Postgres pra pedidos e rotas (convivência).
- ADRs novos (ORM, front com build, Postgres).
- **Fatia vertical de prova:** cadastro de embarcador e usuário → login no
  portal novo → Consulta de pedido lendo do Postgres → Torre em modo
  leitura ao lado da Flask.

Portão:

- Restauração completa (banco + bucket + VPS limpa) ensaiada em < 1 h.
- Deploy por Action em hml e prod, com prova de fumaça e volta automática
  testada de propósito.
- Comparador SQLite × Postgres em 0 pra pedidos e rotas por 10 dias.
- Revisão de segurança da fundação sem achado ALTO.

### Fase 1 · Pedido e cadastros nascem no sistema novo (2 meses)

Entregas:

- Pedido com ciclo de vida por eventos; entrada por XML e planilha no
  portal novo; destinatários e endereços versionados.
- Pool do planejamento lendo do Postgres (termina o que `fonte_pool`
  começou).
- Pipeline Stokki → Vuupt vira adaptador do pedido novo (o pedido nasce no
  nosso sistema e é replicado nos dois fornecedores).
- Painel novo: Início, Consulta, Pedidos Parados (o Lovable sai aqui),
  Cadastros. Portal novo: lançar e acompanhar pedido.
- Notificações ao embarcador (resumo 20h, entrega) migradas pra
  Comunicação.

Portão:

- 100% dos pedidos de um mês nascem no sistema novo (ainda replicados em
  Stokki e Vuupt).
- Lovable desligado; `lovable/` apagado.
- Zero divergência de pool por 10 dias úteis.

### Fase 2 · TMS: execução de rota no app próprio (3 a 4 meses)

Entregas:

- Planejamento, marketplace e confirmação de rota no painel novo
  (`confirmacao_motoristas` absorvido).
- App do motorista completo (seção 4.5).
- Torre em tempo real; comprovante chega ao portal e à expedição.
- Financeiro do motorista com os relatórios já reproduzidos; réplica
  núcleo → Vuupt pra manter o financeiro.
- Expedição no painel novo (ainda gravando na Stokki pelo adaptador).
- Piloto rota a rota: Hugo → 1 motorista → 3 → metade → todos. Botão
  "devolver pra Vuupt" enquanto ela existir; plano papel documentado.

Portão do corte da Vuupt:

- 100% das rotas de 2 meses executadas no app com foto e resultado.
- Extrato do motorista e relatório do financeiro batendo com o da Vuupt
  num mês fechado.
- Nenhuma reclamação de embarcador atribuível ao app.
- Vuupt em só-leitura por 30 a 60 dias com alerta se algo gravar.
- Depois: exportação final conferida contra o manifesto, fim de contrato.

### Fase 3 · WMS e portal completo (4 a 5 meses)

Entregas:

- Catálogo próprio (importado da Stokki, mantido pelo embarcador no
  portal); estoque com lote, validade, FEFO.
- Recebimento e pedido de entrada; separação, conferência, expedição por
  rota no galpão; etiquetas; inventário cego.
- Estoque, documentos, relatórios e financeiro do embarcador no portal.
- Expedição na Stokki vira espelho (a Stokki recebe o que o nosso sistema
  decidiu) até o corte.
- Piloto por embarcador: Maria Dolores → 3 → todos, cada um com inventário
  de abertura conferido junto. Comunicação e treinamento são tarefas da
  fase.

Portão do corte da Stokki:

- Todos os embarcadores lançando só pelo portal por 30 dias.
- Saldo do nosso WMS = contagem física nos últimos 2 inventários.
- Nenhuma NF perdida num mês.
- Stokki em só-leitura 30 a 60 dias.
- Depois: exportação, fim de contrato, convites da Stokki encerrados.

### Fase 4 · Desligar o legado (1 mês)

Flask, scripts soltos, `dados/dados.db` e `confirmacoes.db` saem; timers só
do pacote novo; MAPA e ADRs refeitos; ensaio de restauração final;
`agente_importacao_stokki` aposentado.

**Soma:** 12 a 15 meses. Primeiro fornecedor (Vuupt) por volta do mês 8,
segundo (Stokki) por volta do 13. Em qualquer ponto dá pra parar e o que já
migrou continua funcionando: cada fase deixa a convivência estável, sem
metade de módulo no ar.

## 7. Testes e qualidade

Hoje: ~170 testes de unidade, nenhum de ponta a ponta, prova real à mão
na VPS. O sistema novo tem quatro camadas com dono claro.

### 7.1 Domínio (rápidos, milhares)

Toda regra pura tem teste sem banco e sem rede. A migração de `regras/`,
`roteirizacao/` e `nucleo/` usa **os testes existentes como contrato**: o
teste velho roda contra o código novo antes de o velho ser apagado. Onde
não há teste (ex.: `pipeline.processar_pedido`, 15 parâmetros, 0 testes),
escreve-se primeiro um teste de caracterização do comportamento atual,
inclusive o que parece errado, e só então se migra. Regra que muda de
propósito vira teste novo e ADR.

### 7.2 Serviços e API (contra Postgres real)

Cada caso de uso tem teste com Postgres em container no CI (nunca SQLite
fingindo), cobrindo caminho feliz, erros de negócio e **fronteira de
acesso**: pra cada endpoint do portal, o teste "embarcador A pede o recurso
do B e recebe 404". Adaptadores externos testados com respostas gravadas
(HTML e JSON reais salvos) e um **teste de contrato diário** por adaptador
contra o serviço real em homologação, que avisa se o fornecedor mudou a
página.

### 7.3 Ponta a ponta (Playwright, contra homologação)

Um roteiro por fluxo que paga a conta: embarcador lança XML → pedido no
pool → rota → motorista executa (API simulando o celular) → canhoto no
portal → expedição → extrato. Mais galpão (receber, endereçar, separar,
expedir) e segurança (login, bloqueio por tentativas, troca de PIN). Rodam
a cada merge; vermelho segura o deploy em produção.

### 7.4 Sombra e comparador em produção

O comparador com placar de dias limpos vira padrão de toda virada: cada
módulo tem o seu, o resultado vai pra tela de Saúde, e o portão da fase lê
de lá. Divergência explicada tem categoria própria e não suja o placar.

### 7.5 Disciplina

- Nada entra sem teste na camada certa; o CI mede cobertura do domínio e
  dos serviços e não deixa cair.
- Revisão de código em toda mudança (skill de review, mais a de segurança
  em módulo exposto), antes do merge.
- Dado real só anonimizado em homologação; nenhum teste toca produção.
- Falha em produção vira primeiro teste que a reproduz, depois correção.
- Documentação viva: MAPA do sistema novo gerado em parte do código (rotas
  da API, tabelas, jobs), ADR pra toda decisão, manual do operador por
  tela no repo.

## 8. Riscos

| Risco | Por que é real | Tratamento |
|---|---|---|
| Uma pessoa só, projeto de um ano | Doença, férias ou a operação param o projeto; o legado continua precisando de cuidado | Cada fase deixa o sistema estável se parar no meio; documentação e ADRs desde o dia 1; ~30% do tempo reservado pra operação e legado |
| Embarcadores resistem a sair da Stokki | Gente de fora, hábito, confiança no estoque; único corte que depende de terceiros | Portal pronto e provado antes de qualquer convite; piloto Maria Dolores; migração um a um com inventário de abertura; Stokki só-leitura 30–60 dias; comunicação e treinamento como tarefa |
| Financeiro sem relatório | Requisito explícito do Hugo; já derrubou um plano | Réplica na Vuupt até mês fechado bater; relatórios já a 98–99%; portão exige mês fechado com relatório nosso |
| Regra de negócio perdida na migração | 15 meses de casos de borda sem teste em vários módulos | Caracterização com teste antes de migrar; teste velho contra código novo; comparador em sombra por módulo |
| Fornecedor muda tela/API no meio | Stokki trocou a tabela de produtos em 22/09 e quebrou o sync por 36 h | Teste de contrato diário por adaptador; adaptadores isolados; alerta imediato |
| Migração corrompe histórico | 7 anos de dados, formatos mistos (UTC × local, `#PS` × `PS`) | Carga idempotente numa cópia, relatório por categoria, 10 dias de comparador em 0; arquivo da Vuupt no GCS como fonte imutável |
| Postgres gerenciado fora do ar ou caro | Dependência nova, de outro tipo | Snapshot diário no GCS além do PITR; ensaio mensal numa VPS limpa prova troca de provedor em < 1 h |
| App do motorista não funciona na rua | Sinal, bateria, aparelho antigo; 0 rotas reais até hoje | Fila offline em teste de ponta a ponta; piloto Hugo → 1 → 3 → metade; "devolver pra Vuupt"; plano papel |
| Escopo cresce | Tela pedida "porque existe hoje" atrasa o corte | Tela só entra quando a fase exige; pedido novo vira item de backlog com fase definida |
| Custo escondido | Observabilidade, CI e banco podem sair do gratuito | Orçamento por fase; alerta de custo no provedor |

## 9. Próximos passos

1. Hugo revisa esta spec.
2. Plano de execução da **Fase 0** (skill `writing-plans`): tarefas
   pequenas e verificáveis, na ordem, com o primeiro commit sendo a
   renomeação de `freshhub/` → `lovable/` e o esqueleto do pacote.
3. Cada fase seguinte ganha spec curta (só o que muda) e plano próprio,
   escritos quando a anterior passar no portão.

## 10. Pendências com o Hugo

| # | Pendência | Estado em 17/10 |
|---|---|---|
| 1 | Provedor do Postgres | Recomendação: Google Cloud SQL `southamerica-east1`; Supabase como reserva. Hugo pediu recomendação; confirmar ao abrir a Fase 0 |
| 2 | Fim de contrato e exportação oficial da Vuupt e da Stokki | Sem data exata. O portão de cada corte prevê 30–60 dias de só-leitura antes do fim |
| 3 | Motoristas do piloto e aparelhos | Sem definição. Decidir na abertura da Fase 2 |
| 4 | Grafia do pacote | Hugo escreveu "Freshub"; assumido `freshhub` (dois h, como o domínio) |

## 11. Referências

- `DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md` (etapas 0–6 da saída da Vuupt, decisões de 12 e 15/09)
- `DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md` (app, fases A–E)
- `docs/adr/0001` (saída por etapas com réplica), `0002` (Stokki sem API), `0005` (SQLite), `0006` (telas por prefixo), `0008` (km e tarifa), `0009` (roteirizador próprio)
- `docs/superpowers/specs/2026-09-21-wms-estoque-reserva-pedidos-design.md` (WMS fase 2, decisões D1–D11)
- `docs/superpowers/specs/2026-09-24-pool-pelo-nucleo-pedidos-portal-rascunho-14h-design.md` (pool pelo núcleo)
- `PRODUCT.md` (princípios de tela), `MAPA_DO_SISTEMA.txt`
- Plano "Fresh Hub v2" de 25/08 (artifact), substituído por este documento
