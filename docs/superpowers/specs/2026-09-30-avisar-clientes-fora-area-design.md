# Avisar clientes em massa sobre pedidos fora da área de atendimento

Data: 30/09/2026. Pedido do Hugo: "botão para avisar em massa por WhatsApp e
e-mail para cliente sobre os pedidos fora da área de atendimento".

## Contexto

- A roteirização (`roteirizacao/criar_rotas_diarias.py` e `incrementar_rotas.py`)
  classifica pedidos fora da área com `identificar_area_nao_atendida` e manda o
  e-mail de `notificar_area_nao_atendida.notificar_remetentes`. Desde 20/08 esse
  e-mail vai **só para hugo@** (`forcar_destino=EMAIL_TESTE`) e marca o pedido em
  `pedidos_area_notificada`, que também é o que tira o pedido da rota automática.
  O cliente nunca recebe nada; o Hugo repassa à mão.
- O planejamento (`painel_agentes/planejamento_rotas.py` + template) já separa
  esses pedidos no bloco "Fora da área de atendimento" (`#secao-pool-fora-area`),
  com botão "Selecionar" e `tipo_area` em cada card.
- WhatsApp interno: `notificar_whatsapp.despachar(config, origem, tipo, texto,
  assinatura, grupo_id=...)` sobre o gateway OpenWA (`integracao_openwa.py`), com o
  número do Hugo. Hoje só manda para os grupos internos; a chave `operator` da VPS
  está restrita à sessão e ao grupo ALERTAS FRESH.
- Cadastro do embarcador: tabela `interno` (44 linhas: `cnpj_embarcador`,
  `sender_id`, `nome_remetente`, `apelido`, `email`, `notificar_email`...), sem
  telefone e sem tela de edição.
- O OpenWA expõe `GET /api/sessions/{sessao}/groups` (lista os grupos do número).

## Decisões do Hugo (30/09)

1. WhatsApp vai para o **grupo que já existe com cada cliente**, pelo número dele.
2. O grupo é cadastrado numa **coluna nova em `interno`** por uma **tela no painel**.
3. A prévia mostra quem já foi avisado pelo botão (data e usuário) e vem
   **desmarcado**; a marca antiga de `pedidos_area_notificada` (e-mail só pro Hugo)
   não conta como "cliente avisado".
4. **Envio real desde o primeiro clique**, com os destinos na prévia. Chave
   `forcar_destino` no config nasce vazia e serve só para redirecionar o e-mail em
   teste de produção.

## Fora de escopo

- Mudar o e-mail automático da roteirização (continua só pro Hugo) ou a marca em
  `pedidos_area_notificada`.
- Resposta do cliente (o fluxo segue por fora, como hoje).
- Mensagem privada para número de pessoa; só grupo.
- Cadastro de e-mail do embarcador (a tela nova só cuida do grupo).

## Seção 1 — Botão "Avisar clientes" no planejamento

**Onde.** Cabeçalho do bloco "Fora da área de atendimento", ao lado de
"Selecionar". Age sobre os pedidos **selecionados** desse bloco; sem seleção, pega
todos os visíveis do bloco (mesma regra de "Selecionar visíveis"). Pedido
selecionado de outro bloco é ignorado e aparece na prévia como "ignorado: não
está fora da área". Visível só para os níveis `total` e `operador` (o botão
"Dedicado" já segue essa regra).

**Prévia.** `POST /api/planejamento/avisar-fora-area/previa` com
`{service_ids: [...]}`. O servidor:

1. Reclassifica os pedidos com `identificar_area_nao_atendida` sobre os serviços
   brutos do pool (não confia no `tipo_area` do cliente) e descarta o que não está
   fora da área.
2. Agrupa por `(sender_id, tipo)`. Um embarcador com pedidos dos dois tipos vira
   dois blocos, porque o texto é diferente (mesma regra do e-mail atual).
3. Para cada bloco devolve: `sender_id`, `nome` (apelido ou nome_remetente),
   `tipo`, `pedidos: [{service_id, codigo, cidade, uf, avisado_em, avisado_por}]`,
   `emails` (lista de `interno.email`), `whatsapp_grupo_id`, `whatsapp_grupo_nome`
   (se o gateway responder), `ultimo_aviso: {em, por}` (o mais recente entre os
   pedidos do bloco), `whatsapp_disponivel` (config ligado + gateway configurado).

O modal (mesmo padrão visual do modal "Dedicado") mostra um bloco por embarcador:
nome, tipo de aviso, pedidos (código · cidade/UF), e-mail(s) e grupo, checkbox do
bloco e checkboxes dos canais (e-mail / WhatsApp), ambos marcados por padrão.

- Sem e-mail ou sem grupo: rótulo vermelho "sem e-mail" / "sem grupo" e o canal
  que falta vem desmarcado e travado. Sem os dois: o bloco vem desmarcado e travado.
- WhatsApp indisponível (desligado no config ou gateway sem configuração): rótulo
  "WhatsApp indisponível", canal desmarcado e travado em todos os blocos.
- `ultimo_aviso` preenchido: rótulo "avisado em DD/MM HH:MM por X" e o bloco vem
  **desmarcado** (o usuário marca se quiser cobrar de novo).
- Rodapé: "Enviar para N clientes", que conta só os blocos marcados.

**Envio.** `POST /api/planejamento/avisar-fora-area` com
`{itens: [{sender_id, tipo, service_ids, canais: ["email", "whatsapp"]}]}`, níveis
`total` e `operador`, `@exige_mesma_origem`. O servidor reclassifica de novo os
`service_ids` (pula o que saiu da área entre a prévia e o clique e lista em
`ignorados`) e, bloco a bloco:

- **E-mail:** assunto e corpo iguais aos de hoje (`_montar_conteudo` e o assunto de
  `notificar_remetentes`, extraídos para uma função reutilizável em
  `notificar_area_nao_atendida.py`), remetente = `email.remetente` do config,
  destinos = `interno.email`; se `avisos_fora_area.forcar_destino` estiver
  preenchido no config, vai só para lá (o registro guarda o destino real usado).
- **WhatsApp:** `notificar_whatsapp.despachar(config, "avisar_fora_area",
  "fora_area_cliente", texto, assinatura=None, grupo_id=<grupo do embarcador>,
  contar_no_teto=False)`. Parâmetro novo `contar_no_teto`: quando `False`, o envio
  não entra no teto diário nem na janela de repetição (é manual, com prévia), mas
  respeita o intervalo mínimo e o disjuntor de falhas e é registrado em
  `notificacoes_whatsapp` como as outras origens.
- Falha em um canal ou embarcador não interrompe os demais.

Texto do WhatsApp (uma linha por pedido, até 10; depois "e mais N"; linhas cortadas
com `_uma_linha`; sem limite de 200 caracteres, como o aviso de insucesso):

```
⚠️ *Fresh Log · pedidos fora da área de atendimento*
PS-40316 · Curitiba/PR
PS-40320 · Blumenau/SC
Esses destinos ficam fora do estado de SP. Haverá redespacho por transportadora? Se sim, nos envie o endereço completo com CEP e o nome da transportadora. Detalhes no e-mail.
```

Tipo SP fora do raio, última linha: "Esses destinos ficam fora da nossa área de
atendimento padrão. Se quiser, fazemos uma cotação de entrega dedicada. Detalhes
no e-mail." Quando o e-mail não foi enviado para aquele bloco (canal desmarcado
ou sem e-mail), a frase "Detalhes no e-mail." sai.

**Resposta.** `{resultados: [{sender_id, tipo, email: "enviado"|"falhou"|"pulado",
whatsapp: "enviado"|"falhou"|"pulado"|"nao_enviado"|"desligado", detalhe}],
ignorados: [...]}`. O modal mostra o resultado por embarcador e não recarrega a
página: os cards do bloco ganham o chip "avisado DD/MM" na hora.

**Chip nos cards.** `montar_pool` passa a devolver `avisado_em` (último `enviado`
em `avisos_fora_area`) ao lado de `tipo_area`; o front mostra
`<span class="badge-avisado">✉ avisado 30/09</span>` só nos cards com `tipo_area`.

## Seção 2 — Cadastro do grupo por embarcador

**Banco.** Coluna `whatsapp_grupo_id TEXT` em `interno`. Migração idempotente em
`avisar_fora_area.garantir_coluna_grupo(conn)` (`PRAGMA table_info` + `ALTER TABLE`),
chamada por quem lê ou grava a coluna.

**Tela.** `/painel/embarcadores/whatsapp` (Blueprint em
`painel_agentes/embarcadores_whatsapp.py`, template
`embarcadores_whatsapp.html`), item "WhatsApp dos clientes" no grupo do
atendimento do menu lateral, níveis `total` e `operador`. Conteúdo: tabela com os
embarcadores de `interno` ordenados por apelido (apelido/nome, e-mail, grupo
atual) e em cada linha um `<select>` com os grupos do número (nome + id), com
campo de busca por texto acima da tabela que filtra as opções (o número tem ~113
grupos). Opção "— nenhum —" para tirar. Botão "Salvar" por linha.

- `GET /api/embarcadores/whatsapp/grupos`: chama
  `integracao_openwa.listar_grupos(cfg)` (função nova, `GET
  /sessions/{sessao}/groups`, nunca levanta exceção, devolve `[{id, nome}]`).
  Se o gateway não responder, devolve `{grupos: [], indisponivel: true}` e a tela
  troca o `<select>` por um campo de texto para colar o `...@g.us` à mão, com
  aviso "gateway do WhatsApp fora do ar".
- `POST /api/embarcadores/<cnpj>/whatsapp-grupo` com `{grupo_id}`: valida o
  formato (`^\d+(-\d+)?@g\.us$` ou vazio; o `-<timestamp>` é o JID dos grupos
  criados antes de 2022), grava em `interno`, registra em log quem mudou
  (usuário da sessão).

**Chave do OpenWA.** A chave `operator` da VPS é restrita ao grupo ALERTAS FRESH.
No deploy o Hugo (ou o Claude com o ok dele) cria uma chave `operator` da mesma
sessão sem restrição de chat e a coloca em `whatsapp_notificacoes.api_key` no
`config.yaml` da VPS (backup antes). Sem isso, o envio ao cliente falha e a
resposta mostra `whatsapp: falhou`.

## Seção 3 — Registro

Tabela `avisos_fora_area` (criada por `avisar_fora_area.py`, `CREATE TABLE IF NOT
EXISTS`):

| coluna | conteúdo |
|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT |
| service_id | id do serviço na Vuupt |
| codigo | código do pedido (`PS-NNNNN`, normalizado) |
| sender_id | embarcador |
| tipo | `sp_nao_atendido` / `fora_sp` |
| canal | `email` / `whatsapp` |
| situacao | `enviado` / `falhou` |
| destino | e-mail(s) ou id do grupo usado |
| por | usuário do painel |
| criado_em | ISO, segundos |

Uma linha por pedido e canal, a cada clique. `pedidos_area_notificada` não é lida
nem escrita pelo botão.

**Módulos.**
- `avisar_fora_area.py` (raiz): `garantir_tabelas`, `garantir_coluna_grupo`,
  `montar_previa(servicos, tipos_area, config, conn)`, `texto_whatsapp(tipo,
  pedidos, com_email)`, `enviar(itens, servicos, tipos_area, config, por, conn)`,
  `avisados_por_service_id(conn)`.
- `notificar_area_nao_atendida.py`: `assunto_e_conteudo(nome, tipo, pedidos)`
  extraído do que hoje está dentro de `notificar_remetentes` (sem mudar o
  comportamento da roteirização).
- `notificar_whatsapp.py`: parâmetro `contar_no_teto` em `despachar`/`_despachar`
  e `_motivo_para_nao_enviar`.
- `integracao_openwa.py`: `listar_grupos(cfg)`.
- `painel_agentes/painel_agentes.py`: as duas rotas do planejamento; registro do
  Blueprint novo; item no menu.
- `painel_agentes/planejamento_rotas.py`: `avisado_em` no item do pool.
- `painel_agentes/templates/planejamento_rotas.html`: botão, modal, chip.
- `painel_agentes/embarcadores_whatsapp.py` + `templates/embarcadores_whatsapp.html`.
- `MAPA_DO_SISTEMA.txt`: módulo, tela, tabela e coluna novos.

**Config (`config.yaml`, opcional, defaults no código):**

```yaml
avisos_fora_area:
  forcar_destino: ""    # preenchido = e-mail vai só pra esse endereço
```

WhatsApp usa a seção `whatsapp_notificacoes` existente (`ativo`, `base_url`,
`api_key`, `sessao`, `intervalo_min_seg`, `falhas_para_alerta`, `pausa_canal_min`).

## Seção 4 — Erros e ambiente local

- Falha de e-mail ou WhatsApp em um bloco é registrada (`falhou`) e devolvida;
  os outros blocos seguem.
- WhatsApp com `ativo: false` ou sem `base_url`/`api_key`/`sessao`: a prévia marca
  `whatsapp_disponivel: false`; o envio devolve `desligado` para o canal.
- Gateway fora do ar no envio: `falhou`, e o disjuntor de `notificar_whatsapp`
  pausa o canal como já faz hoje (e-mail de alerta na N-ésima falha).
- Pedido que saiu da área entre a prévia e o clique: pulado, listado em `ignorados`.
- Bloco sem canal utilizável no envio (por exemplo, e-mail apagado entre a prévia
  e o clique): `pulado` nos dois canais, com `detalhe`.
- Ambiente local: não há detecção automática de "VPS x local". O `config.yaml`
  local tem `whatsapp_notificacoes.ativo: false` (nada sai) e, para testar o
  e-mail localmente, o Hugo preenche `avisos_fora_area.forcar_destino` no config
  local. A rota nunca envia sem um clique explícito.

## Testes

`unittest`, como o resto do projeto.

- `test_avisar_fora_area.py` (raiz), com SQLite em memória e `enviar_email`/
  `despachar` substituídos:
  - agrupamento por `(sender_id, tipo)`; embarcador com dois tipos vira dois blocos;
  - prévia: `ultimo_aviso` preenchido pelo último `enviado`; sem e-mail e sem
    grupo → `enviavel: false`; WhatsApp desligado → `whatsapp_disponivel: false`;
  - `texto_whatsapp`: até 10 pedidos + "e mais N"; frase final por tipo; sem
    "Detalhes no e-mail." quando `com_email=False`;
  - `enviar`: registra `enviado`/`falhou` por pedido e canal; falha em um bloco não
    para o próximo; `forcar_destino` redireciona só o e-mail e o registro guarda o
    destino usado; pedido fora dos `tipos_area` cai em `ignorados`;
  - `garantir_coluna_grupo` roda duas vezes sem erro.
- `test_notificar_whatsapp.py`: `contar_no_teto=False` ignora teto e janela de
  repetição, mas respeita intervalo e disjuntor; os testes existentes seguem.
- `painel_agentes/test_embarcadores_whatsapp.py` (Flask test client): salvar e
  remover grupo; formato inválido → 400; gateway fora → `indisponivel: true`;
  nível `leitura` → 403.
- Prova na VPS após o deploy: cadastrar um grupo de teste do Hugo num embarcador de
  teste, clicar no botão com 1 pedido, receber e-mail e WhatsApp, conferir a linha
  em `avisos_fora_area` e a linha em `notificacoes_whatsapp`.

## Deploy

1. Commit + push; na VPS `git pull --no-rebase` + `chown -R www-data:www-data`.
2. Chave nova do OpenWA (sem restrição de chat) em `whatsapp_notificacoes.api_key`
   do `config.yaml` da VPS, com backup do arquivo.
3. `systemctl restart painel-agentes`.
4. Prova real descrita em Testes.
