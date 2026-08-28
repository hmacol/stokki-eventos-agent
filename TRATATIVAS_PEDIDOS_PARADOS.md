# Tratativas de Pedidos Parados

Criado em 24/08/2026 a pedido do Hugo: documentar as tratativas dos
"pedidos parados" (https://freshhub.com.br/pedidos-parados) pra que, toda
vez que a lista for revisada, dê pra saber o que já foi tratado, o que
precisa de prioridade e o que precisa ser encaminhado pra operação.

## O que é essa tela

**Fresh Hub** (`freshhub.com.br`, login `usuario@freshlog.local` + PIN) é
um **sistema interno próprio da Freshlog** de gestão de operação —
Pedidos de Saída/Entrada, Separação, Conferência, Recebimentos, Estoque, e
uma seção "Rotinas operacionais" com Demandas, Devoluções e **Pedidos
parados**. Não é o Stokki (`freshlog.stokki.com.br`, usado por
`stokki/pedidos.py`), não é o "On hold" que `notificar_pedidos_em_espera.py`
já trata, e não é nenhum dos apps que construímos sob `*.freshhub.com.br`
(`app.`, `atendimento.`, `confirmacao.`) — é um domínio próprio deles, com
subpáginas (`/pedidos-parados`, `/demandas`), sem API mapeada no projeto
ainda.

`freshhub.com.br/pedidos-parados` é a tela de **registro manual de
incidentes** de pedidos parados propriamente dita.

A tela tem duas abas:
- **Registrar** — cadastra um pedido que ficou parado.
- **Histórico** — lista os já registrados.

Colunas do Histórico:

| Coluna | Significado |
|---|---|
| Data | Data/hora em que o pedido foi **registrado como parado** nesta tela (não é a data de criação do pedido no Stokki) |
| Pedido | Número do pedido |
| Volumes | Quantidade de volumes do pedido |
| Recebedor | Quem **registrou** o pedido como parado (não é o destinatário da entrega) |
| Status | Classificação do pedido parado (ver tabela abaixo) |
| Foto | Foto anexada ao registro, quando houver |

Filtros disponíveis: data, pedido, recebedor, status. Botões "Colunas"
(customiza colunas visíveis) e "Exportar".

## Classificação (Status) e tratativa

Todo pedido nasce como **Pendente** (ainda não classificado). A primeira
ação de quem revisa a lista é classificar cada pendente num dos status
abaixo — essa lista tende a crescer (Hugo, 24/08: "talvez eu queira
cadastrar mais opções mais adiante").

| Status | Duplicar (reenviar à Vuupt)? | Tratativa |
|---|---|---|
| **Pendente** | — | Ainda não classificado — classificar primeiro, antes de qualquer outra ação |
| **Cancelados** | **Nunca** | Criar card em Demandas (`freshhub.com.br/demandas`) para a equipe de operação |
| **Devolução Parcial** | **Nunca** | Criar card em Demandas para a equipe de operação |
| **Reenvio** | **Sim** | Duplicar o pedido e reenviar à Vuupt o quanto antes, pra voltar pra rota |
| **Agendado** | Não | Verificar e manter o agendamento já existente (não é caso de operação nem de duplicação) |
| **Descartar** | **Nunca** | **Mesmo comportamento de "Cancelados"** (corrigido 24/08, pedido do Hugo: "precisamos comunicar a equipe") — cria card em Demandas com o mesmo molde de Cancelados (categoria "Cancelamento de Pedido e Retorno ao Estoque", área "Operações") |
| **Verificar com Cliente** | **Nunca** | Cria card em Demandas pra área **Atendimento** (categoria "Verificar Pedido com Cliente", cadastrada pelo Hugo em 24/08) — antes de decidir Cancelados/Devolução Parcial/Reenvio, alguém do atendimento ou comercial confirma com o cliente o que fazer com o pedido |
| **Em Rota** | Não | **Mesmo comportamento de "Agendado"** (pedido do Hugo, 25/08) — só marca como revisado, sem duplicar nem criar Demanda; pedido já está em rota, não precisa de mais nada |
| **Cliente Retira** | **Nunca** | **Não cria Demanda** (confirmado com o Hugo, 25/08) — botão manual "Notificar embarcador" dispara um e-mail (mesmo template visual/design dos outros notificadores automáticos do projeto, `email_utils.envelope_html`) avisando o embarcador que o pedido ainda não foi coletado. E-mail resolvido via `pedidos_historico.cliente_cnpj` → `interno.cnpj_embarcador` (cruza por CNPJ, não por nome); se o pedido não estiver em `pedidos_historico`, o embarcador não tiver e-mail cadastrado, ou tiver `notificar_email` desligado, a tela mostra erro em vez de enviar |
| **Entregue** | Não | **Mesmo comportamento de "Agendado"** (pedido do Hugo, 25/08) — só marca como revisado, sem notificação, sem Demanda e sem duplicar; o pedido já foi entregue, o registro de "parado" ficou pra trás |
| *(novo status futuro)* | **Indefinido até ser documentado aqui** | Tratar como Pendente — nunca duplicar nem presumir tratativa por conta própria; decidir manualmente e atualizar esta tabela |

**Classificação automática via Stokki (pedido do Hugo, 28/08)**: o botão
"Consultar Stokki (cancelados / cliente retira)" na barra da tela abre o
detalhe de cada pedido em tela na Stokki (1 GET por pedido, ~1s cada) e
classifica sozinho: situação **Cancelado** → "Cancelados"; transportadora
com nome literal **CLIENTE RETIRA** → "Cliente Retira" (botão "Notificar
embarcador" disponível, manual); transportadora com tipo **RETIRADA na
BD_TRANSPORTADORAS** (terceira que coleta no galpão, ex.: ACEVILLE) →
também "Cliente Retira", mas **sem notificação ao cliente** (Hugo, 28/08):
a tratativa já nasce concluída, sem botão. Cancelado vence quando os dois
batem. A tratativa dos demais (Encaminhar / Notificar) continua manual. Pedido que já
tem OUTRA classificação com tratativa concluída não é sobrescrito (vira
"divergente" no resumo). Pedidos cujo `order_number` é a NF do cliente e
não foi resolvido pra id Stokki aparecem como "não encontrados". A
consulta é recusada enquanto houver agente rodando no painel (login
concorrente derruba a sessão Stokki do agente). O status/transportadora
vistos ficam gravados (`pedidos_parados_stokki`) e aparecem embaixo do
número do pedido.

**Classificação automática via Vuupt (pedido do Hugo, 28/08)**: o botão
"Consultar Vuupt (em rota / agendado)" resolve cada pedido em tela na
Vuupt (segue a cadeia de reentregas até o serviço mais recente) e, se o
serviço está **não atribuído** (`not_assigned`): sem agendamento → "Em
Rota"; agendado pra **hoje** → "Em Rota"; agendado pra **depois de hoje**
→ "Agendado". Agendamento vencido (antes de hoje) e qualquer outro
status (atribuído/em rota/concluído/cancelado) NÃO classificam, só
aparecem em "sem ação" no resumo. Só preenche classificação vazia ou
troca Em Rota ↔ Agendado — Cancelados/Cliente Retira/Reenvio etc. já
marcados não são sobrescritos (viram "divergente"). Erro da Vuupt (ex.:
429) interrompe a rodada em vez de insistir. O status/agendamento vistos
ficam em `pedidos_parados_vuupt` e aparecem embaixo do número.

**Importante (confirmado com o Hugo, 24/08): a classificação acontece no
NOSSO painel, não na tela do Fresh Hub.** O Fresh Hub continua sendo só a
fonte do registro bruto (pedido, volumes, data, foto); o Status/tratativa
e o histórico de ações vivem do nosso lado (ver plano de automação
abaixo).

## Fluxo de revisão (toda vez que a lista for checada)

1. Abrir o Histórico e olhar os pedidos com Status = **Pendente** —
   esses ainda não têm tratativa definida.
2. Para cada pedido pendente, classificar num dos status da tabela acima
   (usando o motivo real do problema).
3. Aplicar a tratativa correspondente ao status:
   - **Reenvio** → duplicar o pedido (nova entrega na Vuupt).
   - **Cancelados** / **Devolução Parcial** → encaminhar para a operação, não duplicar.
   - **Agendado** → só confirmar que o agendamento existente segue de pé.
4. Também revisar os já classificados que ainda não tiveram a ação
   concluída (ex.: classificado como Reenvio mas ainda não duplicado) —
   "verificar se já foi tratado" antes de agir de novo, pra não duplicar
   duas vezes o mesmo pedido.
5. **Prioridade**: qualquer pedido (Pendente ou já classificado, mas sem
   ação concluída) com a coluna **Data** há **mais de 2 dias corridos**
   deve ser tratado primeiro — ficou tempo demais parado sem resolução.

## O que esta tela NÃO faz

Duplicar/reenviar aqui significa criar um **novo pedido/entrega na Vuupt**
a partir do original — a tela de pedidos parados em si é só o registro do
incidente, não dispara nada automaticamente hoje.

## Demandas (`freshhub.com.br/demandas`) — destino do encaminhamento à operação

Kanban do próprio Fresh Hub, já usado pela operação hoje. Colunas:
**Aguardando prazo** → **Aguardando aprovação** → **Fila de execução** →
**Concluída**. Cada card tem: título (ex. "Entregas — MARCHEF - AOS
AREIA"), área/categoria (ex. "Entregas"), prioridade (badge, ex. "ALTA"),
tags (ex. "Logística", nome do cliente), responsável (ex. "Quecia Costa da
Silva") e, quando concluído, data de conclusão. Botão "Nova demanda";
filtros por área e "Todos/Meus".

Decisão do Hugo, 24/08: pedidos parados classificados como **Cancelados**
ou **Devolução Parcial** viram card automático aqui, em vez de e-mail/
Chatwoot. **A forma exata de trabalhar esse fluxo ainda precisa ser
definida** — ver perguntas abaixo (título padrão, área/categoria,
prioridade, se atribui a alguém).

---

## Plano de automação (próxima etapa, ainda não implementada)

Peças que já existem no projeto e podem ser reaproveitadas, em vez de
construir algo do zero:

- **`tratativas.py`** já é um log de eventos por pedido (tabela
  `tratativas_pedido`, usada pela Torre de Controle — eventos como
  `REENVIO_MANUAL`, `EXCECAO_TRATADA`). Dá pra registrar os eventos de
  pedido parado aqui também (ex.: `PEDIDO_PARADO_CLASSIFICADO`,
  `PEDIDO_PARADO_DUPLICADO`, `PEDIDO_PARADO_ENCAMINHADO_OPERACAO`), em vez
  de criar uma tabela nova — mantém tudo pesquisável num só lugar.
- **Duplicação** já tem função pronta: `expedir_pedidos.duplicar_servico_por_insucesso`,
  hoje acionada manualmente pelo botão "Duplicar pedido" da Torre
  (`painel_agentes/torre_controle.py::duplicar_pedido_manual` →
  `/api/torre/duplicar`). O caso "Reenvio" de pedidos parados é
  conceitualmente o mesmo fluxo.
- **Encaminhar para operação** pode reaproveitar os canais já usados no
  projeto (`email_utils.py` e/ou `integracao_chatwoot.py`), no mesmo
  padrão dos outros notificadores.
- **Evitar reprocessar** o que já foi tratado: mesmo padrão de
  `tratativas.ja_registrado()` / fingerprint (`fingerprint_duplicacao_insucesso.py`
  etc.) já usado em outros fluxos do projeto.

### Resolvido com o Hugo, 24/08

- **Classificação**: feita no nosso painel (não na tela do Fresh Hub).
- **Mapeamento pedido → Vuupt**: o número da coluna "Pedido" pode ser
  **o ID numérico do Stokki (a parte numeral de PS-XXXXX)** ou **a NF do
  cliente** (frequentemente usada como referência) — não é fixo, varia
  por registro. A checagem precisa tentar os dois: primeiro como ID
  Stokki direto; se não resolver, buscar por `numero_nfe` em
  `pedidos_historico` (mesmo campo que `tratativas.buscar()` já
  usa no LEFT JOIN).
- **Encaminhamento à operação**: vira card automático em
  `freshhub.com.br/demandas` (ver seção acima), não e-mail/Chatwoot.
- **Prioridade do card**: sempre **ALTA** (todo Cancelados/Devolução
  Parcial de pedido parado entra como urgente pra operação, sem depender
  da regra dos 2 dias).

### Descoberta técnica (via HAR + testes diretos, 24/08)

O Fresh Hub é um SPA (React/Vite) que fala **direto com um projeto
Supabase** (Postgres + PostgREST) — não tem backend próprio nosso pra
mapear. Toda leitura/escrita é uma chamada REST em
`https://qhujqhzkwvbfsauxgepf.supabase.co/rest/v1/<tabela>`, com o
header `apikey` (JWT "anon" do Supabase, o mesmo que vem embutido no
bundle JS público do site — não é segredo de verdade, mas ainda assim
deve ficar em `config.yaml`, não hardcoded em script).

**Teste direto confirmou que a `apikey` sozinha NÃO basta**: fiz uma
leitura em `stalled_orders` só com ela (sem sessão de usuário) e voltou
`200 OK` com lista **vazia** — ou seja, o Fresh Hub tem uma regra de
segurança (RLS) no Supabase que só libera os dados pra uma sessão de
usuário autenticado de verdade. Uma automação sem login real veria
sempre "0 pedidos parados", errado e silencioso (sem erro visível).
Não há cookies em nenhuma chamada do HAR (`"cookies": []` em tudo) —
sinal de que a sessão do Supabase fica no `localStorage` do navegador,
não em cookie (padrão comum do supabase-js). Isso aponta pro mesmo
caminho já usado pro Stokki (`stokki/auth.py`): login de verdade via
Playwright (preencher CPF+PIN no formulário real), depois ler o token de
sessão do `localStorage` (chave no padrão `sb-<project-ref>-auth-token`)
e reusar esse token nas chamadas REST seguintes — sem precisar reproduzir
manualmente o mecanismo exato de autenticação do Fresh Hub.

**Credenciais já estão em `config.yaml`** (seção `freshhub: cpf / pin`,
mesmo padrão do Stokki/Vuupt) — não precisam ser coladas em lugar
nenhum, e não vão aparecer neste documento.

**Tabela `stalled_orders`** = fonte real da tela Pedidos Parados/Histórico
(`GET /rest/v1/stalled_orders?select=*&order=created_at.desc&limit=200`).
Schema confirmado (Hugo colou uma amostra real da Response):

```
id             uuid    -- chave interna do Fresh Hub
order_number   text    -- "Pedido" na UI (ex.: "175904", "37368")
volumes        int
photo_url      text|null -- "Foto" na UI
recebedor_name text    -- e-mail de quem registrou (ex.: "werlly@freshlog.local")
recebedor_id   uuid    -- FK pra profiles
resolved_at    timestamptz|null -- null em TODOS os registros vistos até agora
created_at     timestamptz -- "Data" na UI
```

**Achado importante: não existe coluna de status/classificação nesta
tabela.** Isso bate com o que o Hugo já tinha confirmado ("classificação
acontece no nosso painel, não no Fresh Hub") — o "Status: Pendente" que
aparece na tela do Fresh Hub não é um dado persistido com várias opções,
é só o estado default (provavelmente calculado a partir de `resolved_at
IS NULL`). A classificação (Cancelados/Devolução Parcial/Reenvio/
Agendado) precisa mesmo ser um dado NOSSO, ligado a `stalled_orders.id`
ou `order_number` — não tem onde gravar isso no lado do Fresh Hub hoje.

**Outro achado, olhando a amostra**: vários `order_number` aparecem
repetidos (ex.: "175906", "37368", "31156", "174965" — registrados de
novo dias depois, em 21/08 e de novo em 24/08). Confirma que o mesmo
pedido pode ser registrado como parado mais de uma vez — reforça a
necessidade de "verificar se já foi tratado" por `order_number`, não só
por `id`, antes de agir.

**Tabela `tasks`** = fonte real de Demandas — essa eu consegui mapear
completa, porque o Hugo demonstrou o ciclo de vida inteiro de uma demanda
de teste (criar → aprovar → concluir) e o HAR capturou os corpos das
requisições de escrita:

- **Criar** (`POST /rest/v1/tasks`): `title`, `description` (pode ser
  null), `client_name`, `category`, `priority`, `area`, `requested_by`
  (uuid do usuário, vem de `profiles.id`), `status` inicial
  `"aguardando_prazo"`.
- **Fluxo de status** (a coluna visual do Kanban é derivada do `status`
  raw, não é 1 pra 1 óbvio):
  `aguardando_prazo` → **Aguardando prazo**
  `aguardando_aprovacao` → **Aguardando aprovação** (PATCH manda também
  `proposed_deadline` e `assigned_to`)
  `aprovada` → **Fila de execução** (PATCH manda `approved_deadline` e
  `scheduled_date`)
  `concluida` → **Concluída** (PATCH manda `completed_at` e mantém
  `scheduled_date`)
- **Tabelas de apoio** (todas com `id`/`label` ou `id`/`name`, filtradas
  por `active=eq.true`): `task_areas` (5 áreas cadastradas), `task_categories`
  (11 categorias cadastradas), `clients` (43 clientes cadastrados) — usadas
  pra preencher os dropdowns de área/categoria/cliente na hora de criar.
- `profiles` (id, full_name, email, area) e `user_roles` (user_id) — pra
  resolver quem é o usuário logado/responsável.
- `task_comments` e `task_attachments` (FK `task_id`) — comentários e
  anexos da demanda, vazios no exemplo do Hugo.

**Exemplo real que o Hugo criou** (isso praticamente já é o molde da
tratativa "Cancelados"):
```
title:      "Cancelamento de Pedido e Retorno ao Estoque — GOURMAR PESCADOS INDUSTRIA E COMERCIO LTDA"
client_name: "GOURMAR PESCADOS INDUSTRIA E COMERCIO LTDA"
category:   "Cancelamento de Pedido e Retorno ao Estoque"
area:       "Operações"
priority:   "alta"
status:     "aguardando_prazo"
```

### Implementado, 24/08 (leitura funcionando de ponta a ponta)

- `freshhub/auth.py` — `FreshHubSession`: login via Playwright no
  formulário real (`#identifier` = CPF/celular, `#pin-in` = PIN,
  seletores confirmados inspecionando a página de verdade), lê o token
  de sessão do Supabase no `localStorage`
  (`sb-qhujqhzkwvbfsauxgepf-auth-token`), persiste em
  `freshhub/sessao_freshhub.json` e renova via `refresh_token` (sem
  reabrir browser) antes de recorrer a um login completo de novo.
- `freshhub/pedidos_parados.py` — `listar_pedidos_parados()`, lê
  `stalled_orders` com a sessão autenticada.
- `investigacao/testar_freshhub_pedidos_parados.py` — script só de
  leitura (não cria Demanda, não duplica nada) que valida o login e
  imprime um resumo. **Rodado com sucesso em 24/08**:
  - **200 pedidos parados encontrados** (limite da consulta é 200 —
    pode haver mais no total; ajustar/paginar quando formos além do
    teste), **todos ainda sem `resolved_at`**.
  - **68 `order_number` aparecem mais de uma vez** (2 ou 3x, em datas
    diferentes) — confirma a necessidade de checar por `order_number`
    antes de agir de novo num pedido.
  - **Categorias reais cadastradas em `task_categories`**: Cadastrar EAN
    - Novo Cadastro / **Cancelamento de Pedido e Retorno ao Estoque**
    (a de "Cancelados") / Entregas / Inventário Parcial / Inventário
    Total Qualitativo / Inventário Total Quantitativo / Pedido para a
    Freshlog - Verificar e Dividir com o time ou Descartar / Prioridade
    de Entrada / Reembolso Motorista - Descarga / Verificação de
    Validade / Verificação Física de Pedido.
  - **Áreas reais cadastradas em `task_areas`**: Atendimento, Cadastro,
    Financeiro, Logística, **Operações** (a de "Cancelados").

### Resolvido, 24/08 — todas as decisões de negócio fechadas

- **Categoria pra "Devolução Parcial"**: o Hugo cadastrou a categoria
  **"Devolução Parcial"** em `task_categories` (confirmei rodando o
  script de novo — ela já aparece na lista, entre "Cancelamento..." e
  "Entregas").
- **Área pra "Devolução Parcial"**: **"Operações"** — mesma de
  "Cancelados".
- **`requested_by`**: o usuário de serviço do Fresh Hub (a conta logada
  via `config.yaml`, hoje é a do Hugo) — não o humano que classificou no
  nosso painel.
- **Título do card**: mesmo formato do exemplo, **sem** número do
  pedido: `"{categoria} — {client_name}"`.

Molde final das duas tratativas que viram card em Demandas:

```
Cancelados:
  title:      "Cancelamento de Pedido e Retorno ao Estoque — {client_name}"
  category:   "Cancelamento de Pedido e Retorno ao Estoque"
  area:       "Operações"
  priority:   "alta"
  requested_by: <uuid do usuário de serviço, resolvido da própria sessão>
  status:     "aguardando_prazo"

Devolução Parcial:
  title:      "Devolução Parcial — {client_name}"
  category:   "Devolução Parcial"
  area:       "Operações"
  priority:   "alta"
  requested_by: <uuid do usuário de serviço, resolvido da própria sessão>
  status:     "aguardando_prazo"
```

`client_name` ainda depende de resolver o pedido no Stokki/Vuupt (nome
do embarcador ou destinatário) — não vem de `stalled_orders` (que só tem
`order_number`, não o nome do cliente).

### Implementado, 24/08 — módulo de escrita

`freshhub/tasks.py` — `criar_demanda()` (genérica) e
`criar_demanda_para_tratativa(sessao, tratativa, client_name)` (atalho
pras duas tratativas que viram Demanda, usando o molde acima).

**Testado ponta a ponta, 24/08 (combinado com o Hugo)**: criei uma
Demanda de teste de verdade — `criar_demanda_para_tratativa(sessao,
"Devolução Parcial", "TESTE - ignorar")` → título "Devolução Parcial —
TESTE - ignorar", `id` retornado `a256728e-20e0-4725-a4af-0edc0f97cad8`,
apareceu no Kanban real do Fresh Hub. Login, leitura de pedidos parados
e escrita de Demandas estão todos confirmados funcionando.

### Implementado, 24/08 — tela de triagem no painel_agentes

Fecha o fluxo de ponta a ponta dentro do próprio painel (menu lateral
"Pedidos Parados", rota `/pedidos-parados`), no mesmo padrão da Torre de
Controle (`torre_controle.py`/`historico_tratativas.py`: página sobe só
a casca, dados chegam por `/api/pedidos-parados/*` via JS):

- `painel_agentes/pedidos_parados_triagem.py` — lógica de negócio:
  - `listar_com_classificacao()` — une a leitura do Fresh Hub
    (`freshhub.pedidos_parados.listar_pedidos_parados`) com a
    classificação local. **Ajustado, 24/08 (pedido do Hugo)**: a lista
    exibida só mostra pedidos **registrados de novo no dia mais
    recente** presente nos dados (o mesmo pedido é re-registrado todo
    dia enquanto continua parado — se não aparece de novo hoje, ou já
    foi resolvido, some da lista de qualquer forma), e **tira quem já
    foi entregue** por fora da triagem (`_codes_entregues_recentes()`:
    busca em lote os serviços com `completed_at` preenchido e SEM
    `failed_reason_id` nos últimos 3 dias na Vuupt — insucesso não
    conta como "entregue", continua na lista, já tem tratativa própria
    na Torre). `dias_parado` usa o **histórico completo** disponível
    (busca até 2000 registros brutos, não só os 200 default — o mesmo
    pedido pode aparecer dezenas de vezes), calculado como hoje menos a
    primeira vez que aquele pedido apareceu como parado — é assim que a
    prioridade (≥2 dias) continua funcionando mesmo só mostrando o dia
    de hoje. **Achado real ao testar**: tem pedido parado há **35 dias**
    (registrado 19-20 vezes!). Testado: 76 pedidos no dia mais recente,
    1 já entregue (removido), 75 exibidos.
  - `classificar()` — grava a classificação numa tabela nova,
    `pedidos_parados_classificacao` (`dados/dados.db`, raiz do
    projeto), **chaveada por `order_number`** (não por `freshhub_id`)
    — é assim que "verificar se já foi tratado" funciona mesmo quando o
    Fresh Hub tem várias linhas pro mesmo pedido. Registra também um
    evento `PEDIDO_PARADO_CLASSIFICADO` em `tratativas.py` (aparece no
    Histórico de Tratativas).
  - `duplicar()` — tratativa "Reenvio": resolve o pedido na Vuupt
    (`_resolver_pedido()`, tenta como ID Stokki direto e, se não achar,
    como NF via `pedidos_historico`) e reaproveita a MESMA função da
    Torre (`expedir_pedidos.duplicar_servico_por_insucesso`), com o
    mesmo fingerprint de idempotência (`fingerprint_duplicacao_insucesso`)
    — não duplica duas vezes o mesmo pedido. Evento `REENVIO_MANUAL`
    (origem `PEDIDOS_PARADOS`, pra diferenciar da Torre no histórico).
  - `encaminhar_operacao()` — tratativas "Cancelados"/"Devolução
    Parcial": resolve o nome do cliente na Vuupt (com *fallback* pra um
    título genérico se não achar — nunca trava a criação do card por
    causa disso) e chama `freshhub.tasks.criar_demanda_para_tratativa()`.
    Evento `PEDIDO_PARADO_ENCAMINHADO_OPERACAO`.
  - **Achado ao testar `_resolver_pedido()` com um pedido real
    ("175904")**: não resolveu nem como ID Stokki nem como NF local —
    plausível que ainda não tenha sido importado da Stokki pra Vuupt
    (pode estar "parado" bem cedo no funil, antes até de virar um
    serviço na Vuupt). Comportamento correto: `duplicar()` levanta erro
    claro nesse caso; `encaminhar_operacao()` segue em frente com nome
    genérico (não é bloqueante pra Cancelados/Devolução Parcial).
- `painel_agentes/templates/pedidos_parados_triagem.html` — tabela com
  select de classificação por linha, botão de ação (Duplicar/
  Encaminhar p/ operação) que muda conforme a classificação, badge de
  prioridade e de "Nx registrado", filtro "Mostrar só não tratados".
- Rotas em `painel_agentes.py`: GET `/pedidos-parados` (níveis total/
  operador/leitura) + `/api/pedidos-parados/dados` (GET) +
  `/classificar`, `/duplicar`, `/encaminhar` (POST, níveis total/
  operador + `exige_mesma_origem`, mesmo padrão CSRF da Torre).
- Link novo no menu lateral (`templates/base.html`).
- Novos eventos `PEDIDO_PARADO_CLASSIFICADO` e
  `PEDIDO_PARADO_ENCAMINHADO_OPERACAO` adicionados a
  `tratativas.EVENTOS_POR_PEDIDO` (o `REENVIO_MANUAL` já existia, só
  reaproveitado).

**Ainda não testado**: a tela em si no navegador (só a lógica de
negócio foi validada por script) — falta abrir `/pedidos-parados` de
verdade e clicar nos botões antes de considerar pronto pra produção.
Também não fiz deploy nem reiniciei o serviço na VPS.