# Embarcadores novos na Stokki: pré-cadastro automático e convite

Data: 08/10/2026. Pedido do Hugo em 07/10/2026: "criar uma rotina de verificação de
embarcadores na Stokki. Se entrar algum novo embarcador já seguir com o pré-cadastro.
Caso falte alguma informação, mandar msg no WhatsApp pedindo para finalizar o
cadastro, enviando o link direto."

## Problema

A tabela `interno` (um embarcador por linha, chave de tudo: tipo de carga, e-mails,
sender_id da Vuupt, stkkc_id da Stokki, conta do portal) é mantida à mão. Quando um
cliente novo entra na Stokki e ninguém cadastra:

- o pipeline manda o pedido pra Vuupt sem `sender_id` e sem tipo de carga; a Vuupt
  cria um remetente novo a cada pedido (NONNA, stkkc_id 100, já tem 16 senders);
- o planejamento mostra "Remetente não identificado";
- o cliente não recebe e-mail nenhum e não tem conta no portal.

Em 07/10 a Stokki tinha 82 clientes e `interno` 45. Dois com pedido aberto estavam
fora: NONNA DISTRIBUIÇÃO (apelido FABENNE, id 100, celular de mentira
"(11) 999 999 999", e-mail da Mariana) e RIC INDÚSTRIA (apelido TERRA NUTRI, id 56).

## Decisões do Hugo (07/10)

1. O cliente preenche só o que ele sabe: tipo de carga, e-mails pra notificação e
   WhatsApp de contato. Fator ponderado e sender_id ficam internos. Tipo de carga só
   vale no roteirizador depois que o Hugo aprova.
2. O link do convite é o link de PIN do portal (24 h, uso único). Depois do PIN o
   cliente cai na tela "Complete seu cadastro".
3. Celular da Stokki vazio ou inválido: o convite vai por e-mail pro endereço da
   Stokki e o grupo ALERTAS é avisado. Sem e-mail também: só o aviso no grupo.
4. Primeira rodada: convite só pra quem tem pedido nos últimos 30 dias. Os outros
   entram em `interno` como inativos, sem convite, até aparecer pedido.

## Fatos levantados (07/10, em produção)

- `GET /pt-br/administrator/inventory/outbound/select` (já em
  `stokki/pedidos.py::listar_embarcadores`) devolve `{"success", "clients", "units"}`;
  `clients` é lista de `{id, name, select_label}`, com `select_label` no formato
  `APELIDO (RAZÃO SOCIAL - 00.000.000/0000-00)`. Cliente sem apelido repete a razão
  social no começo do rótulo.
- `GET /pt-br/administrator/client/edit/{id}` é HTML com inputs `name` (apelido),
  `last_name`, `cellphone`, `phone`, `email`, `company_company_name` (razão social),
  `company_cnpj`, `company_email[]`, `city`, `state`.
- Na Vuupt o remetente é um customer: `GET /customers/{sender_id}` (app.vuupt.com)
  devolve `{"customer": {id, name, code, ...}}`. Não existe `/senders` nem `/contacts`.
  A criação por `POST /customers` com `name` e `code` deve ser confirmada na
  implementação (primeira tarefa do plano); se não existir, cai no reaproveitamento
  do sender do primeiro pedido (ver abaixo).
- `StokkiSession` entra sozinha na fila por conta (`sessao_uso.py`); rotina
  recorrente espera sem limite.
- WhatsApp a cliente já existe (`avisar_cliente_sem_resposta.py` +
  `notificar_whatsapp.despachar` com `grupo_id` = telefone), seção
  `whatsapp_notificacoes.clientes` {ativo, teto_diario, forcar_destino}. O OpenWA
  responde 201 até pra número sem WhatsApp: validar antes com `contacts/check`.
- Link de PIN: `auth_cliente.gerar_token_definir_pin` (itsdangerous, 24 h) e o
  comando `enviar-link` de `portal_cliente/gerenciar_clientes.py`.
- Conta do portal exige `interno.sender_id` preenchido (`auth_cliente.buscar_embarcador`).
- O pipeline lê `interno` por `stkkc_id` (`pipeline._buscar_dados_embarcador_banco`)
  e usa `habilidade` capitalizada em {Seco, Refrigerado, Congelado}; vazio cai no
  padrão com log "sem 'habilidade' válida".

## Arquitetura

Três peças, uma por responsabilidade:

1. **`embarcadores_stokki.py`** (raiz): rotina de lote. Detecta, pré-cadastra,
   cria o sender, convida, reenvia, avisa o grupo. Timer
   `infra/stokki-embarcadores-novos.{service,timer}`.
2. **`portal_cliente/cadastro_embarcador.py`** + template `_completar_cadastro.html`:
   tela "Complete seu cadastro" e gravação do que o cliente informou.
3. **Aprovação**: linha nas pendências do `/inicio` do painel com botão "Aprovar"
   (`painel_agentes`) e subcomando `cadastro` em `gerenciar_clientes.py`.

Estado compartilhado na tabela nova `cadastro_embarcador` (em `dados/dados.db`):

```
cadastro_embarcador
  cnpj_embarcador      TEXT PRIMARY KEY   -- mesmo formato de interno (só dígitos)
  stkkc_id             INTEGER NOT NULL
  celular_stokki       TEXT NOT NULL DEFAULT ''
  email_stokki         TEXT NOT NULL DEFAULT ''
  situacao             TEXT NOT NULL      -- INATIVO | CONVIDADO | REENVIADO | SEM_CONTATO | PREENCHIDO | APROVADO
  canal_convite        TEXT               -- whatsapp | email | nenhum
  convite_enviado_em   TEXT
  reenvio_em           TEXT
  habilidade_informada TEXT               -- Seco | Refrigerado | Congelado (o que o cliente marcou)
  preenchido_em        TEXT
  aprovado_em          TEXT
  aprovado_por         TEXT
  criado_em            TEXT NOT NULL
  atualizado_em        TEXT NOT NULL
```

Criada com `CREATE TABLE IF NOT EXISTS` pelo módulo que a lê primeiro (mesmo padrão
de `preferencias_notificacao._garantir_tabela`).

## Parte 1: detecção e pré-cadastro (`embarcadores_stokki.py`)

Fluxo de uma rodada:

1. `StokkiSession(config)` (fila, recorrente). `listar_embarcadores` → lista de
   `{id, name, select_label}`. Função pura `interpretar_rotulo(select_label, name)`
   devolve `(apelido, razao_social, cnpj_digitos)`; sem apelido, `apelido = None`.
2. `SELECT stkkc_id, cnpj_embarcador FROM interno`. Candidatos = ids da Stokki
   ausentes de `interno.stkkc_id`.
3. CNPJ do candidato já existe em `interno` com outro `stkkc_id` (ou NULL): não
   cria linha; se `stkkc_id` for NULL, preenche; senão avisa o grupo
   ("CNPJ X já está em interno com stkkc_id Y, conferir") e segue.
4. Pra cada candidato, `GET client/edit/{id}` e `extrair_contato(html)` →
   `{celular, telefone, email, cidade}` (regex nos inputs; função pura, testável
   com HTML gravado).
5. `INSERT INTO interno`: `cnpj_embarcador`, `stkkc_id`, `nome_remetente` = razão
   social, `apelido`, `email` = e-mail da Stokki, `fator_ponderado` = 1.0,
   `habilidade` = NULL, `notificar_email` = 1 se ativo / 0 se inativo,
   `observacoes` = "Pré-cadastro automático dd/mm/aaaa (Stokki #stkkc-N)",
   `criado_em`/`atualizado_em` = agora.
6. Ativo ou inativo: ativo = tem pedido em `nucleo_pedidos` com
   `remetente_codigo` igual ao CNPJ e `criado_em` nos últimos 30 dias. A regra vale
   em toda rodada, não só na primeira: inativo que ganhar pedido depois vira ativo
   e recebe convite na rodada seguinte (situação `INATIVO` → `CONVIDADO`).
7. Sender da Vuupt, só pra ativos:
   - existe pedido desse CNPJ em `nucleo_pedidos` com `sender_id`: usa o
     `sender_id` do pedido mais antigo;
   - senão `POST /customers` com `{"name": razão social, "code": CNPJ formatado}`
     e usa o `id` devolvido;
   - falha: linha fica com `sender_id` NULL, aviso no grupo, tenta de novo na
     próxima rodada (`interno.sender_id IS NULL AND cadastro.situacao != 'INATIVO'`).
   Inativo fica sem sender até ter pedido.
8. Grava `cadastro_embarcador` com situação `INATIVO` ou segue pro convite.

Linhas de `interno` criadas pela rotina são idênticas às manuais: nada no resto
do sistema precisa saber que foi automático.

## Parte 2: convite e cobrança

Só pra ativo com `interno.sender_id` preenchido (exigência da conta do portal).

1. Link: `gerenciar_clientes._link` (token de `gerar_token_definir_pin` +
   `portal_cliente.url_base` do config), o mesmo do `enviar-link`.
2. Canal, nesta ordem:
   - celular da Stokki normalizado por `preferencias_notificacao.normalizar_whatsapp`;
     rejeita vazio, menos de 10 dígitos e sequências repetidas (`999999999`,
     `000000000`); passa por `integracao_openwa.numero_existe` (já existe, usa o
     endpoint `contacts/check`).
     Válido → WhatsApp pelo canal de clientes (`notificar_whatsapp.despachar` com
     origem `cadastro_embarcador`, teto `clientes.teto_diario`, `forcar_destino`
     respeitado). Texto, até 200 caracteres:
     `Olá, {apelido}. Sua conta na Fresh Log está quase pronta. Complete o cadastro aqui: {link}`
   - senão, e-mail pro endereço da Stokki, reaproveitando o corpo do `enviar-link`
     (mesmo `forcar_destino` do portal);
   - senão, só o aviso no grupo.
   Grava `canal_convite`, `convite_enviado_em`, situação `CONVIDADO` ou `SEM_CONTATO`.
   O número da Stokki, se válido, também entra em `preferencias_notificacao.whatsapp`
   pra ser o padrão na tela de complemento.
3. Reenvio: `CONVIDADO` há mais de 3 dias sem conta criada (`clientes_portal` sem
   `pin_hash`) → reenvia uma vez pelo mesmo canal, situação `REENVIADO`, aviso no
   grupo. Depois disso a rotina não manda mais nada; a pendência continua no
   `/inicio` até alguém resolver à mão.
4. Aviso no grupo ALERTAS (`notificar_whatsapp.despachar`, origem
   `embarcadores_stokki`, uma mensagem por rodada com novidade, até 200 caracteres):
   `Embarcador novo na Stokki: FABENNE (NONNA DISTRIBUICAO). Convite por WhatsApp.`
   Variações: "sem WhatsApp válido, convite por e-mail", "sem contato, cadastrar à
   mão", "reenviado o convite", "já em interno com outro stkkc_id".
5. `--modo-teste`: tudo calculado e logado, nada gravado em `interno`,
   `cadastro_embarcador`, Vuupt, WhatsApp ou e-mail.

## Parte 3: tela do portal e aprovação

1. Após login (e após a troca obrigatória de PIN, se houver), se o CNPJ tem linha
   em `cadastro_embarcador` com situação em {CONVIDADO, REENVIADO, SEM_CONTATO} ou
   `interno.habilidade` vazia, o portal redireciona pra `/cliente/completar-cadastro`
   antes de qualquer outra tela. Grupo econômico: a tela vale pro CNPJ do login.
2. Formulário: tipo de carga (rádio: Seco, Refrigerado, Congelado, uma linha
   explicando cada um), e-mails pra notificação (um por linha, validados por
   `preferencias_notificacao._validar_emails`), WhatsApp de contato (pré-preenchido
   com o da Stokki quando válido).
3. Ao salvar: e-mails e WhatsApp vão por `preferencias_notificacao.salvar` (mesmo
   caminho do botão Notificações); `habilidade_informada`, `preenchido_em`,
   situação `PREENCHIDO` em `cadastro_embarcador`. `interno.habilidade` NÃO muda.
   Aviso no grupo: `FABENNE completou o cadastro: Refrigerado. Aguardando aprovação.`
4. Aprovação:
   - `/inicio` do painel: bloco "Cadastros de embarcador" no nível do Hugo, uma
     linha por CNPJ com situação PREENCHIDO (apelido, tipo informado, data) e
     botão "Aprovar" (`POST /inicio/cadastro-embarcador/<cnpj>/aprovar`), que grava
     `interno.habilidade` = tipo informado, `cadastro_embarcador.aprovado_em/por`,
     situação `APROVADO`. Linhas CONVIDADO/REENVIADO/SEM_CONTATO aparecem no mesmo
     bloco só como informação ("convidado em dd/mm, sem resposta").
   - CLI: `gerenciar_clientes.py cadastro` lista tudo que não está APROVADO;
     `cadastro aprovar <cnpj>` faz o mesmo que o botão; `cadastro habilidade <cnpj>
     <tipo>` aprova com outro tipo, se o cliente marcou errado.
5. Pendência some do `/inicio` quando APROVADO.

## Parte 4: operação, erros e testes

- Timer `stokki-embarcadores-novos.timer`: `OnCalendar=*-*-* 07..19:10:00`,
  `Persistent=false`. Service `Type=oneshot`, `User=www-data`,
  `ExecStart=/opt/stokki-eventos/venv/bin/python embarcadores_stokki.py`.
  Resumo da execução por `notificar_whatsapp.avisar_execucao` (só em erro).
- Stokki fora ou fila ocupada: a rodada termina com aviso no log e sem alerta;
  a próxima hora tenta de novo.
- Falha no `POST /customers`: `interno` fica sem sender_id, aviso no grupo, tenta
  na próxima rodada.
- Cliente removido da Stokki: nada acontece com `interno` (fora do escopo).
- Teto diário de WhatsApp a clientes atingido: convite fica pra próxima rodada,
  situação não muda.
- Envio real a cliente continua preso em `forcar_destino` (WhatsApp e e-mail) até
  o Hugo liberar, como nas outras notificações.
- Primeira execução real: provar na VPS com `--modo-teste`, conferir que lista
  NONNA e RIC como ativos e uns 35 inativos, depois rodar de verdade com
  `forcar_destino` no Hugo.

Testes (unittest, banco em memória, Stokki/Vuupt/OpenWA/e-mail falsos):

- `test_embarcadores_stokki.py`: `interpretar_rotulo` (com e sem apelido, CNPJ
  com pontuação), `extrair_contato` (HTML gravado da tela de edição), detecção
  (ausente, presente, CNPJ com outro stkkc_id), ativo/inativo pelos 30 dias,
  reaproveitamento do sender mais antigo vs. `POST /customers`, falha no POST,
  escolha do canal (celular válido, de mentira, vazio; com e sem e-mail),
  reenvio único após 3 dias, teto diário, `--modo-teste` não grava.
- `portal_cliente/test_cadastro_embarcador.py`: redirecionamento após login,
  salvar grava preferências e `cadastro_embarcador` sem tocar `interno.habilidade`,
  validação de e-mails e tipo.
- `painel_agentes/test_inicio_cadastro_embarcador.py`: bloco aparece só pro nível
  certo, aprovar grava `interno.habilidade` e some da lista.
- `portal_cliente/test_gerenciar_cadastro.py`: `cadastro`, `cadastro aprovar`,
  `cadastro habilidade`.

## Fora do escopo

- Limpar os 16 senders duplicados da NONNA na Vuupt (os pedidos antigos continuam
  apontando pra eles; só os novos usam o sender de `interno`).
- Cadastro na Stokki (o cliente já entra lá pelo time comercial).
- Endereço de coleta, horário e fator ponderado (ficam à mão).
- Mudar o critério "pedido nos últimos 30 dias" por cliente.

## Ajustes do plano (10/10)

O plano `docs/superpowers/plans/2026-10-10-embarcadores-novos-stokki.md` muda
estes pontos, e eles valem sobre o texto acima:

1. O estado fica num módulo da raiz, `cadastro_embarcador.py`, lido pela rotina,
   pelo portal, pelo painel e pelo CLI. Não existe `portal_cliente/cadastro_embarcador.py`.
2. Situação nova `PENDENTE`: ativo que ainda não tem sender ou convite.
   O portal também pede o cadastro nessa situação.
3. O portal só desvia quem tem linha aberta em `cadastro_embarcador`. Cliente
   antigo com `interno.habilidade` vazia não é desviado.
4. O e-mail do convite tem trava própria: `cadastro_embarcador.forcar_destino_email`.
   Ausente, vai pro Hugo. Envio real exige `forcar_destino_email: ""`.
5. Texto do WhatsApp mais curto, pra caber com o link:
   `Fresh Log: complete o cadastro da sua empresa: {link}`.
6. `VuuptClient.criar_customer` já existe; `vuupt_client.py` não muda. Como todo
   ativo tem pedido no núcleo, o `POST /customers` é só reserva.
7. Avisos no grupo saem uma vez por evento (assinatura em `notificacoes_whatsapp`),
   inclusive "sem sender". Inativos de uma rodada saem numa mensagem só.
8. A tela do portal exige pelo menos um e-mail.

## Arquivos

Novos: `embarcadores_stokki.py`, `test_embarcadores_stokki.py`,
`infra/stokki-embarcadores-novos.service`, `infra/stokki-embarcadores-novos.timer`,
`portal_cliente/cadastro_embarcador.py`, `portal_cliente/templates/_completar_cadastro.html`,
`portal_cliente/test_cadastro_embarcador.py`, `portal_cliente/test_gerenciar_cadastro.py`,
`painel_agentes/test_inicio_cadastro_embarcador.py`.

Alterados: `vuupt_client.py` (`criar_customer`), `portal_cliente/app.py` (rota e
redirecionamento),
`portal_cliente/gerenciar_clientes.py` (subcomando `cadastro`),
`painel_agentes/painel_agentes.py` e template do `/inicio` (bloco e rota de
aprovação), `MAPA_DO_SISTEMA.txt`. Nenhuma chave nova no `config.yaml`:
reaproveita `whatsapp_notificacoes.clientes` e `portal_cliente.url_base`.
