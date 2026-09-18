# Resumo diário por embarcador + preferências de notificação no portal

Spec aprovado pelo Hugo em 17/09. Três etapas independentes, nesta ordem.
**Estado em 17/09: as 3 etapas IMPLEMENTADAS e provadas localmente, SEM commit e SEM deploy**
(ver "Estado e pendências" no fim). Commit e deploy só quando o Hugo pedir.

## Objetivo

1. Um e-mail por embarcador, todo dia às 20:00, com o status de todas as
   notas que estavam em rota no dia: entregues (com link do canhoto), com
   falha (motivo + tratativa) e não concluídas.
2. No portal do cliente (`/cliente`), um botão **Notificações** onde o
   próprio cliente liga/desliga 6 tipos de e-mail e informa o e-mail em
   que quer recebê-los.

## Decisões do Hugo (17/09)

| Tema | Decisão |
|---|---|
| Escopo do dia | Tudo que estava em rota na data (entregue, falha, em aberto). Sem rota não entra. |
| Canhoto | Link por nota, sem login (token assinado). Sem anexo. |
| Tratativa no e-mail | Só rótulos padronizados. Texto livre vira "Em tratativa com nossa equipe". |
| Quem recebe o resumo | Opt-in por embarcador, ligado pelo cliente no portal (ou pelo Hugo via `/equipe`). |
| Tipos no botão | **6 chaves.** Os 3 novos de 17/09: Notas em rota (07h, `notificar_nfs_em_rota.py`), Entrega concluída (por pedido, `notificacao_entregas/`), Resumo diário (20h, este doc). E os 3 antigos: Insucesso aguardando retorno, Pedidos em espera (XML), Agendamento (pendente + dia fixo). |
| Padrão de quem nunca abriu o modal | Tudo ligado, menos o Resumo diário (opt-in). |
| E-mail editável | Campo separado só de notificações. `interno.email` continua sendo do cadastro (PIN, Cc de transportadora) e só o Hugo altera. |
| Horário | 20:00 do mesmo dia. |

Decisões do Claude, a vetar se o Hugo discordar:
- Insucesso desligado só tira o e-mail: a pendência segue no bloco de
  atenção do portal e na Torre; o log diz "desligado pelo cliente".
- O botão "notificar" manual da Torre ignora a chave (ação deliberada),
  mas usa o e-mail de notificações.
- A confirmação enviada depois que o embarcador responde um insucesso
  (`aplicar_resposta_insucesso._email_do_remetente`) usa o e-mail de
  notificações e ignora a chave (é resposta a um clique dele).
- Tentativa com falha e reentrega `-R1` no mesmo dia aparecem como duas
  linhas, sem consolidar.

## Etapa 1: módulo de preferências + rotinas existentes

### Tabela `preferencias_notificacao` (criada sob demanda, `CREATE TABLE IF NOT EXISTS`)

| Coluna | Tipo | Default |
|---|---|---|
| `cnpj_embarcador` | TEXT PK | (mesma chave da `interno`) |
| `emails` | TEXT | `''` (vazio = usa `interno.email`) |
| `nfs_em_rota` | INTEGER | 1 |
| `entrega_concluida` | INTEGER | 1 |
| `resumo_diario` | INTEGER | 0 |
| `insucesso` | INTEGER | 1 |
| `pedidos_em_espera` | INTEGER | 1 |
| `agendamento` | INTEGER | 1 |
| `atualizado_em` | TEXT | |
| `atualizado_por` | TEXT | `cliente` ou `equipe` |

Embarcador sem linha = defaults (comportamento de hoje, resumo desligado).
Fica fora da `interno` de propósito: não depende de quem reescreve aquela tabela.

### `preferencias_notificacao.py` (raiz)

- `TIPOS`: `nfs_em_rota`, `entrega_concluida`, `resumo_diario`, `insucesso`,
  `pedidos_em_espera`, `agendamento` (cada um com rótulo, descrição e default,
  que a tela do portal lê daqui).
- `interno.notificar_email = 0` **não é mais veto** (2ª decisão do Hugo em
  17/09, na frente de `notificacao_entregas/`: "tirar o veto deles e deixar
  como não marcados"). Para os 3 tipos novos (`TIPOS_OPT_IN_SE_NOTIFICAR_EMAIL_0`)
  a coluna só muda o PADRÃO de quem nunca salvou nada: nascem desmarcados
  (tela e rotinas), e o embarcador liga sozinho no portal, ou o Hugo pelo
  `/equipe`. Linha gravada sempre vence. Em produção são 4: CLIENTE TESTE,
  EMPÓRIO QUATRO ESTRELAS, PADRAO PURO, PEDRAMOURA. Os 3 tipos antigos nunca
  olharam essa coluna; `pipeline.py` (confirmação de agendamento/redespacho) e
  a triagem de pedidos parados continuam bloqueados por ela, de propósito.
- `carregar_embarcadores(tipo, chave="sender_id", db_path=None) -> dict`
  devolve `{chave: {"nome", "emails", "cnpj", "desligado"}}`,
  mesmo formato que as rotinas já usam (`nome`, `emails`) mais `desligado`.
  `emails` = campo de notificações, ou `interno.email` se vazio.
  `chave` aceita `sender_id` ou `stkkc_id`.
- `ler(conn, cnpj) -> dict` com os 6 flags, `emails` (lista), `emails_cadastro` (lista, só leitura).
- `salvar(conn, cnpj, emails, flags, por) -> dict` valida e grava (upsert).
  Validação: no máximo 5 e-mails, formato `x@y.z`, sem espaço/quebra de
  linha/`<>`, até 254 caracteres cada; flags viram 0/1; tipo desconhecido
  é erro; CNPJ precisa existir na `interno`. Levanta `ValueError` com
  mensagem pronta pra tela.

### Rotinas alteradas (só o carregamento e o laço de envio)

| Arquivo | Tipo | Mudança |
|---|---|---|
| `insucesso_entrega/notificar_insucesso_aguardando_resposta.py` | `insucesso` | loader usa o módulo; grupo desligado é pulado e contado em `desligados`; novo parâmetro `ignorar_preferencia=False` |
| `painel_agentes/torre_controle.py` (`notificar_ocorrencia_manual`) | `insucesso` | passa `ignorar_preferencia=True` |
| `insucesso_entrega/aplicar_resposta_insucesso.py` (`_email_do_remetente`) | `insucesso` | usa o e-mail de notificações, ignora a chave |
| `notificar_pedidos_em_espera.py` | `pedidos_em_espera` | loader com `chave="stkkc_id"`; desligado é pulado |
| `roteirizacao/notificar_agendamento_pendente.py` | `agendamento` | idem, `sender_id` |
| `roteirizacao/notificar_agendamento_dia_fixo.py` | `agendamento` | idem |
| `notificar_nfs_em_rota.py` | `nfs_em_rota` | loader usa o módulo (commitado em `bd459a3` por outra sessão: conferir `git status` antes) |
| `notificacao_entregas/` | `entrega_concluida` | **feito** pela frente de `notificacao_entregas/` (17/09): `aplicar_preferencias()` em `notificar_entrega_concluida.py`, tolerante a módulo ausente. Inclui o e-mail de "Retirado". |

Pedido de embarcador desligado **não** é marcado como notificado nem
registra `AVISO_ENVIADO`. O retorno ganha a chave `desligados`; as chaves
`enviados/falhas/sem_email` não mudam.

## Etapa 2: tela no portal

- `GET /api/notificacoes` e `POST /api/notificacoes` em `portal_cliente/app.py`,
  com `@requer_cliente`; o POST também com `@exige_mesma_origem`. Sempre
  opera no CNPJ da sessão (`g.cliente["cnpj"]`), nunca em CNPJ vindo do corpo.
  No modo `/equipe`, grava `atualizado_por="equipe:<usuario>"`; nível leitura vê mas não altera (403).
- Botão **Notificações** no cabeçalho (`base_cliente.html`, vale pra todas as telas do portal) abre um
  modal (`templates/_notificacoes.html`, `<dialog>` nativo): 6 chaves em 2 grupos ("Acompanhamento das
  entregas" e "Avisos que pedem uma ação sua") com uma linha de explicação cada, campo de e-mails (um por
  linha ou separados por vírgula), texto "se vazio, usamos o e-mail do
  cadastro: …". Erro de validação aparece no próprio modal.
- Passo novo no tour (`_tour_portal.html`) apontando pro botão.
- URLs por `url_for()` (portal roda atrás de `/cliente`).

## Etapa 3: resumo diário

### `resumo_diario_embarcador.py` (raiz)

`py -3.11 resumo_diario_embarcador.py [--data AAAA-MM-DD] [--modo-teste] [--sender-id N]`

1. Carrega embarcadores com `carregar_embarcadores("resumo_diario")`;
   processa só quem tem `desligado=False` e e-mail. `--sender-id` em
   `--modo-teste` processa o embarcador mesmo desligado (pra prova).
2. Mesmo molde de `notificar_nfs_em_rota.py` (a irmã das 07h): `dados_cliente.buscar_rotas_do_dia`
   UMA vez pra todos e `dados_cliente.linhas_das_rotas(rotas, sender_id, ...)` por embarcador
   (não usa `montar_dia`, que faria 1 busca por embarcador). Separa em `entregues`, `falhas`,
   `abertos`. Zero linhas = não manda e-mail.
3. Para cada falha, `tratativa_para_cliente(...)`.
4. Monta o HTML com `email_utils.envelope_html` e envia com `enviar_email`.
5. Dedup na tabela que a irmã das 07h já usa: `notificacoes_enviadas (tipo, chave)`, tipo
   `resumo_diario`, chave `sender|data` (com sufixo `|redirecionado` no piloto, pra ligar o
   envio real no mesmo dia ainda mandar ao cliente). `--modo-teste` não grava nem consulta.
6. Fecha com `notificar_execucao` como as outras rotinas.

### Tratativa pro cliente (função pura)

Ordem de decisão:
1. `torre_excecoes_tratadas.motivo` (id `insucesso:<code>`) que casa com um
   dos 11 rótulos de `RESPOSTAS_TRATATIVA` (menos "Outro"), depois de
   normalizar caixa, acento e pontuação. Texto composto antigo
   ("Local Fechado / Reagendado") casa pelo trecho depois da última `/`.
2. Evento `REENVIO_AUTOMATICO`, `REENVIO_AGENDADO` ou `REENVIO_MANUAL` em
   `tratativas_pedido`: "Reentrega programada".
3. `insucessos_aguardando_resposta` PENDENTE: "Aguardando seu retorno".
4. Evento `RESPOSTA_RECEBIDA`: "Retorno recebido, em andamento".
5. Senão: "Em tratativa com nossa equipe".

### E-mail

Assunto: `[Freshlog] Entregas de 17/09: 12 entregues, 1 com falha, 2 em aberto`
(partes com zero somem). Corpo: saudação, 3 números no topo, e uma tabela por bloco não vazio,
na ordem falhas, em aberto, entregues. **Sempre 3 colunas** (e-mail não pode contar com media
query; com 5 colunas estourava o celular):
- **Com falha**: NF (pedido embaixo) · destinatário (endereço embaixo) · motivo + "Tratativa: X".
- **Não concluídas até o momento**: NF · destinatário · situação.
- **Entregues**: NF · destinatário · horário + "Ver canhoto".
O motivo passa por `motivo_para_cliente` (tira os avisos internos de `texto_do_motivo`).

NF vazia aparece como "—". Todo texto vindo de fora passa por `html.escape`.
Rodapé explica que as preferências são ajustadas no portal, botão Notificações.

### Link público do canhoto

- `portal_cliente/link_canhoto.py`: `gerar(service_id, sender_id, segredo)` e
  `ler(token, segredo, validade_dias=90)`, com `itsdangerous.URLSafeTimedSerializer`,
  salt `canhoto-email`. Segredo = o mesmo do portal (sem chave nova no config).
- Rota `GET /c/<token>` no portal (pública: `/cliente/c/<token>`), sem login.
  Token inválido ou vencido = 404 com mensagem. Confere
  `servico["sender_id"] == token.sender_id` antes de servir. A rota mora no próprio
  `link_canhoto.py` (`registrar(app, ...)`, padrão de `cotacao_web.py`). O `api_canhoto` logado
  NÃO foi refatorado pra compartilhar o miolo: em 17/09 outra sessão editava o `app.py`
  (login de grupo + revisão de segurança).
- O link vai em toda entrega com sucesso; a rotina não verifica canhoto
  por pedido (evita 1 request por nota). Canhoto que chega depois passa a abrir.
- URL base e segredo: os do portal que já existem no config (`portal_cliente.url_base`,
  `portal_cliente.secret_key`). Sem segredo, o e-mail sai sem link ("Canhoto no portal").

### Travas

- Respeita `email_utils.notificacoes_automaticas_ativas(config)`; `--modo-teste` ignora.
- `resumo_diario_embarcador.forcar_destino`: **default no código = hugo@freshlogbr.com**
  (chave ausente = piloto). Envio real = `forcar_destino: ""` no config da VPS,
  decisão do Hugo. Com `forcar_destino`, o log mostra o destinatário real.
- `resumo_diario_embarcador.ativo` (default true) desliga a rotina inteira.

### Agendamento

`infra/stokki-resumo-diario-embarcador.service` + `.timer`, `OnCalendar=*-*-* 20:00:00`,
`User=www-data`, `venv/bin/python`. Não usa a Stokki (só Vuupt + SQLite).

## Testes

- `test_preferencias_notificacao.py`: defaults sem linha, fallback de e-mail,
  tipo desligado, chave `stkkc_id`, validação (e-mail ruim, mais de 5, CNPJ
  inexistente, tipo desconhecido), upsert.
- Um teste por rotina alterada: embarcador desligado não chama `enviar_email`
  e conta em `desligados`; `ignorar_preferencia=True` envia.
- `portal_cliente/test_notificacoes_portal.py`: sem sessão = 401/redirect,
  origem errada = 403, CNPJ do corpo é ignorado, modo equipe grava `equipe`.
- `test_resumo_diario_embarcador.py`: tratativa (com os textos livres reais),
  separação em blocos, assunto, HTML escapado, dedup, `forcar_destino` default.
- `portal_cliente/test_link_canhoto.py`: ida e volta, vencido, adulterado,
  sender trocado = 404.
- Prova real: `--modo-teste --data <ontem>` (e-mail pro Hugo) e modal no
  navegador em porta alternativa.

## Fora do escopo

Anexar PDFs; campo "mensagem pro cliente" na Torre; tela no painel interno
pra gerir preferências (o Hugo usa `/equipe`); consolidar tentativa + reentrega.

## Armadilhas já mapeadas

- Vuupt devolve datas em UTC sem fuso: usar só `montar_dia`, que já trata.
- A NF mora no código base (`codigo_base("#PS-1-R2") == "PS-1"`); pedido pode ter N notas.
- Filtro `sender_id` da API Vuupt falha às vezes: `montar_dia` reconfere localmente.
- `notificar_email` da `interno` já está em 1 pra 40 de 44: não serve de opt-in.
- Tratativas anteriores a 16/09 são texto livre.
- Rodar na VPS sempre como `www-data` (WAL do SQLite).

## Estado e pendências (17/09)

Provas feitas localmente: 115 testes (`test_preferencias_notificacao`, `test_preferencias_nas_rotinas`,
`test_notificar_nfs_em_rota`, `test_resumo_diario_embarcador`, `portal_cliente.test_notificacoes_portal`,
`portal_cliente.test_link_canhoto`); modal no navegador (Playwright, porta 8174, banco temporário:
login real, validação, salvar, reabrir, celular, tema escuro); `--modo-teste --data ontem
--sender-id 21785428` enviou e-mail real ao Hugo (42 entregues, 4 falhas); 3 links `/c/<token>`
daquele e-mail devolveram o PDF real do canhoto sem sessão, e token adulterado deu 404.

Pendências:
1. **`notificacao_entregas/` ainda não lê a chave `entrega_concluida`** (pasta de outra sessão, sem
   commit em 17/09). Integrar no carregamento do embarcador que alimenta `regras_entrega.decidir`.
   Até lá a chave aparece no portal e é gravada, mas não tem efeito. Fazer antes de ligar o envio real dela.
2. **Login de grupo** (outra sessão, 17/09): as preferências valem pro CNPJ que fez login. As
   empresas-membro do grupo têm preferências próprias, que hoje só o Hugo ajusta entrando via
   `/equipe` em cada CNPJ.
3. Commit: `portal_cliente/app.py` e `painel_agentes/torre_controle.py` têm alterações de OUTRAS
   sessões no mesmo arquivo. Commitar só os trechos desta feature (`git apply --cached` de um patch
   filtrado), ou depois que as outras sessões commitarem.
4. Deploy: pull + chown; copiar `infra/stokki-resumo-diario-embarcador.{service,timer}`,
   `daemon-reload`, `enable --now` do timer; restart de `portal-cliente` (rotas novas) e
   `painel-agentes` (Torre importa o módulo de insucesso). Provar com `--modo-teste` como www-data
   e abrir um `/cliente/c/<token>` de verdade.
5. Ligar pra valer: o cliente (ou o Hugo via `/equipe`) liga o Resumo no portal; o Hugo põe
   `resumo_diario_embarcador.forcar_destino: ""` no config da VPS.
6. `pytest` não está instalado na máquina local: `portal_cliente/test_cotacao.py` e
   `test_envio_planilha.py` (estilo pytest) não foram rodados nesta sessão.
