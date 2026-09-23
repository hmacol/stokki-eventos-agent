# Bloqueio de área não atendida no portal + pedidos dedicados

Data: 23/09/2026. Pedido do Hugo.

## Objetivo

1. Pedido enviado pelo portal do cliente (XML ou planilha) para cidade que a
   Fresh Log não atende **não entra na Stokki**. Fica na aba Envios, em
   vermelho, "Aguardando liberação", e o cliente é avisado por chat e e-mail.
   A equipe libera pelo `/atendimento` do painel.
2. Existe a marcação **"Dedicado"** em qualquer pedido (vindo do bloqueio ou
   marcado no Planejamento), com o valor da cotação. A roteirização
   automática deixa o pedido dedicado fora das rotas compartilhadas.
3. Dia 1 e dia 16, e-mail para o financeiro com os dedicados da quinzena
   anterior: remetente, pedido, valor.

Decisões do Hugo (23/09):

- Regra de "não atendida" = a mesma da roteirização
  (`roteirizacao/notificar_area_nao_atendida.classificar_pedido`): cidade em
  região de dia fixo → atendida; UF ≠ SP → não atendida; SP a mais de 35 km
  do centro → não atendida; sem dado/geocodificação falhou → atendida.
- Liberar acontece no chamado, na tela `/atendimento` (não tela nova).
- Pedido dedicado segue normalmente para Stokki e Vuupt; o transporte à
  parte é combinado por fora.
- A quinzena é pela data da **marcação como dedicado** (`marcado_em`).
- Todo e-mail novo começa redirecionado para hugo@ (`forcar_destino`).

## Fora de escopo

- Cotação automática (a `cotacao.py` já existe; aqui o valor é digitado).
- Alterar o pedido na Stokki ou na Vuupt (tag, observação).
- Tela nova no painel para envios do portal.
- Tocar a Torre ou a Expedição.

## Como está hoje

- Portal → `POST /api/envios/confirmar` → `envio_pedidos.confirmar_envios`
  grava `portal_envios` com `status=NA_FILA`. O worker
  `portal_cliente/enviar_stokki.py` (loop de 20 s) só pega `NA_FILA`.
- Cidade/UF/CEP já estão em `portal_envios.destinatario_municipio/uf/cep`
  (XML: `dest/enderDest`; planilha: colunas Cidade/UF/CEP).
- `criar_rotas_diarias.py:671` e `incrementar_rotas.py:465` identificam
  área não atendida sobre os serviços Vuupt, tiram da rota e mandam e-mail
  (hoje `forcar_destino=EMAIL_TESTE`). `planejamento_rotas.py:710` só
  rotula `tipo_area` no pool.
- Chat: `portal_cliente/chamados.py` (`criar_chamado`, `mensagem_sistema`,
  `registrar_mensagem_equipe`, `email_resposta_cliente`); precedente de
  chamado aberto pelo sistema em `cotacao.py:781`. Equipe atende em
  `painel_agentes/atendimento_chamados.py` (`/atendimento`).

## Parte 1: dados e regra

### Tabela nova `pedidos_dedicados`

Módulo novo `pedidos_dedicados.py` na raiz (sem Flask), criado com
`CREATE TABLE IF NOT EXISTS` no `conectar()` dele, como os outros.

| coluna | tipo | obs |
|---|---|---|
| id | INTEGER PK | |
| codigo_pedido | TEXT | `PS-xxxxx` normalizado (`normalizar_order_number`); NULL enquanto o envio do portal não foi reconciliado |
| service_id | INTEGER | id do serviço Vuupt, quando conhecido (marcação pelo Planejamento) |
| envio_id | INTEGER | `portal_envios.id`, quando veio do bloqueio |
| sender_id | INTEGER | remetente Vuupt |
| remetente_nome | TEXT | |
| numero_nf | TEXT | |
| valor | REAL | parte deste pedido, 2 casas |
| grupo_id | TEXT | uuid; iguala os pedidos que dividiram a mesma cotação |
| valor_total_grupo | REAL | |
| marcado_em | TEXT | ISO local |
| marcado_por | TEXT | usuário do painel |
| removido_em | TEXT | NULL = ativo |
| removido_por | TEXT | |

Índice único parcial em `codigo_pedido` com `removido_em IS NULL` não
funciona com NULL de codigo; a regra de unicidade fica na função
`marcar()`: se já existe ativo com o mesmo `codigo_pedido` (ou o mesmo
`envio_id`), atualiza o valor em vez de duplicar.

API do módulo:

- `marcar(conn, pedidos, valor_total, por)` → `grupo_id`. `pedidos` é lista
  de dicts com o que se souber (`codigo_pedido`, `service_id`, `envio_id`,
  `sender_id`, `remetente_nome`, `numero_nf`). Divide o valor.
- `remover(conn, codigo_pedido=None, envio_id=None, por=...)`.
- `dividir_valor(total, n) -> list[float]`: centavos que sobram vão para o
  primeiro; a soma bate com o total. Ex.: 100/3 → 33.34, 33.33, 33.33.
- `ativos_por_codigo(conn) -> dict[codigo_normalizado, linha]` para a
  roteirização e o pool.
- `listar_quinzena(conn, data_ref) -> (inicio, fim, linhas)`.
- `vincular_codigo(conn, envio_id, codigo_pedido)`: chamado pela
  reconciliação do worker quando descobre o PS.

### Status novo em `portal_envios`

`AGUARDANDO_LIBERACAO` ("Aguardando liberação"). Coluna nova
`bloqueio_motivo` (TEXT: `fora_sp` | `sp_nao_atendido`) e
`bloqueio_chamado_id` (INTEGER).

Transições: `AGUARDANDO_LIBERACAO → NA_FILA` (liberar) ou `→ CANCELADO`.
`STATUS_ABERTOS` passa a incluir o novo status (aparece na sub-aba "fila";
em `_linha`, `pode_cancelar=True`, `pode_reagendar=False`).

### Onde a checagem roda

Em `confirmar_envios`, para cada item, depois de validar e antes de gravar:

```python
servico = {"address": f"{endereco}, {municipio} - {uf}, {cep}"}
tipo = classificar_pedido(servico, gmaps_key)   # None | fora_sp | sp_nao_atendido
```

`gmaps_key` vem do `config` que `confirmar_envios` já recebe. Exceção na
classificação → loga e deixa passar. Itens com `tipo` gravam
`status=AGUARDANDO_LIBERACAO` + `bloqueio_motivo`.

O retorno de `confirmar_envios` traz os ids bloqueados; a rota
`/api/envios/confirmar` chama `abrir_bloqueio(...)` (Parte 2) com eles.

O worker não muda: já filtra `status='NA_FILA'`.

### Efeito da marca na roteirização

- `criar_rotas_diarias.py` e `incrementar_rotas.py`: antes do
  `identificar_area_nao_atendida`, carregar `ativos_por_codigo` e retirar
  dos `servicos` os que têm código dedicado (`normalizar_order_number` do
  `order_number` do serviço). Logar "N pedido(s) dedicado(s) fora da rota
  compartilhada". Como saem antes, não geram e-mail de área não atendida.
- `planejamento_rotas.py`: item do pool e parada em rascunho ganham
  `dedicado: {"valor": float, "grupo_id": str} | None` pelo mesmo mapa.

## Parte 2: telas e avisos

### Portal (aba Envios, `_envios.html`)

- Badge `.b-AGUARDANDO_LIBERACAO` com `--erro-bg/--st-falha`.
- Abaixo do badge: "Não atendemos a região de **Cidade/UF**."
- Botões da linha:
  - **Solicitar cotação de envio dedicado** → abre o widget no chamado
    `bloqueio_chamado_id` e envia, como mensagem do cliente, "Solicito
    cotação de envio dedicado para NF 12345 → Cidade/UF".
  - **Falar com a equipe Fresh Log** → abre o widget no mesmo chamado.
  - **Cancelar envio** → `POST /api/envios/<id>/acao` com `cancelar`
    (caminho existente; para este status cancela na hora, sem solicitação).
- Linha com dedicado ativo (join por `envio_id`) mostra chip "Envio
  dedicado" ao lado do status, também no acompanhamento se for barato;
  senão só na aba Envios.

Rota nova no portal: `POST /api/envios/<id>/solicitar-cotacao` que grava a
mensagem no chamado via `chamados.adicionar_mensagem` (origem cliente) e
devolve `chamado_id` para o widget abrir.

### Aviso na hora do bloqueio (`envio_pedidos.abrir_bloqueio`)

Uma chamada por confirmação do cliente, com todos os ids bloqueados:

1. Cria **um** chamado: `criar_chamado(origem=sistema, status=AGUARDANDO_FL,
   assunto="Envio para região não atendida", area="envios",
   pedido_ref=<NFs separadas por vírgula>)`. Grava `bloqueio_chamado_id`
   nas linhas.
2. `mensagem_sistema` com o texto: "Não atendemos a região de Cidade/UF. As
   NFs 123, 456 estão aguardando liberação. Você pode solicitar cotação de
   envio dedicado, falar com a equipe ou cancelar o envio."
3. E-mail ao cliente (`email_utils.enviar_email` + `envelope_html`) com o
   mesmo texto e link para o portal. Destino: e-mails do embarcador, mas
   com `portal_cliente.envios.forcar_destino` (config; começa `hugo@`).
4. E-mail interno para `email.email_atendimento` (também sob o mesmo
   `forcar_destino`).

O chamado em `AGUARDANDO_FL` já conta no badge do menu do painel.

### Painel `/atendimento`

Quando o chamado tem `area="envios"` e existe `portal_envios` com
`bloqueio_chamado_id = chamado.id`, o painel da conversa mostra o quadro
**"NFs aguardando liberação"**:

- Lista com checkbox por NF (número, cidade/UF, status atual); NFs já
  liberadas/canceladas aparecem riscadas, sem checkbox.
- Campo **Valor da cotação (R$)**.
- Botões: **Liberar como dedicado** (exige valor > 0 e ≥1 NF), **Liberar
  sem dedicado**, **Cancelar envio**.

Rotas novas em `atendimento_chamados.py`:

- `GET /api/atendimento/chamados/<id>/envios-bloqueados`
- `POST /api/atendimento/chamados/<id>/liberar` com
  `{ids: [...], modo: "dedicado"|"simples"|"cancelar", valor?: float}`.
  - dedicado: `pedidos_dedicados.marcar(...)` com `envio_id`, `sender_id`,
    `remetente_nome`, `numero_nf`; depois `status=NA_FILA`.
  - simples: `status=NA_FILA`.
  - cancelar: `status=CANCELADO`.
  - Em todos: `mensagem_sistema` no chamado ("NF 123 liberada como envio
    dedicado (R$ 33,34)" / "liberada" / "cancelada"). Não resolve o chamado
    sozinho; o atendente resolve.

`enviar_stokki.reconciliar_codigos`: ao descobrir o `codigo_pedido` de um
envio, chama `pedidos_dedicados.vincular_codigo(conn, envio_id, codigo)`.

### Planejamento

- Menu de botão direito (`abrirMenuContexto`, `planejamento_rotas.html:5518`),
  só com `PODE_EDITAR`: **"Marcar como dedicado…"** ou **"Remover dedicado"**
  conforme o card. Se o card clicado está entre os selecionados, a ação vale
  para todos os selecionados.
- Barra de seleção: botão **"Dedicado"** (mesmo comportamento com os
  selecionados).
- Modal novo `modal-overlay-dedicado`: lista dos pedidos, campo valor total,
  texto "R$ X será dividido entre N pedidos (R$ Y cada)". Confirmar →
  `POST /api/planejamento/dedicado` `{service_ids: [...], valor: float}`;
  remover → `DELETE` no mesmo caminho com `service_ids`.
- O endpoint resolve `codigo_pedido`, `sender_id`, `remetente_nome`,
  `numero_nf` a partir dos serviços já carregados no pool.
- Card mostra chip **"Dedicado · R$ 33,34"** (classe própria, diferente do
  chip de área não atendida). Tooltip com valor total e quantos dividiram.

### E-mail do financeiro

Script novo na raiz `notificar_dedicados_financeiro.py`:

- `--modo-teste` e `--data-ref YYYY-MM-DD` (default hoje).
- Quinzena: dia 16 → dias 1 a 15 do mês; dia 1 → dia 16 ao último do mês
  anterior. Qualquer outro dia: só com `--data-ref` (calcula a quinzena
  anterior fechada) e loga que rodou fora do dia.
- Seleção: `pedidos_dedicados` com `removido_em IS NULL` e `marcado_em` na
  quinzena.
- E-mail HTML (`envelope_html`): tabela remetente · pedido (PS + NF) ·
  marcado em · valor; subtotal por remetente; total geral. Sem pedidos →
  manda mesmo assim, "nenhum pedido dedicado na quinzena".
- Destino `financeiro@freshlogbr.com` (config `financeiro.email`), sob
  `financeiro.forcar_destino` (começa `hugo@`).
- Timer `infra/stokki-dedicados-financeiro.timer` `OnCalendar=*-*-01,16 08:00`,
  service no padrão dos outros (`sudo -u www-data venv/bin/python`).

## Config novo (`config.yaml`, VPS e local)

```yaml
portal_cliente:
  envios:
    bloqueio_area_ativo: true
    forcar_destino: hugo@freshlogbr.com
financeiro:
  email: financeiro@freshlogbr.com
  forcar_destino: hugo@freshlogbr.com
```

`bloqueio_area_ativo: false` desliga a checagem sem deploy.

## Erros e casos de borda

- Geocodificação falha ou sem chave: passa (loga WARNING).
- Cliente cancela um envio bloqueado: mensagem de sistema no chamado.
- Liberar NF já liberada/cancelada: 409, sem efeito.
- Dedicado removido no Planejamento para pedido que veio do portal: some da
  quinzena; a linha no portal perde o chip.
- Pedido dedicado que a Vuupt já colocou em rota manual: nada muda; a marca
  só afeta a roteirização automática.
- Mesmo pedido marcado duas vezes: atualiza valor e grupo, não duplica.

## Testes

- `test_pedidos_dedicados.py`: `dividir_valor` (100/3, 0.01/2, 1 pedido),
  `marcar` idempotente, `listar_quinzena` nas duas viradas de mês.
- `portal_cliente/test_bloqueio_area.py`: `confirmar_envios` com
  `classificar_pedido` mockado → status certo; falha de geocodificação
  passa; `abrir_bloqueio` cria 1 chamado para N NFs; `_linha` do novo
  status.
- `painel_agentes/test_atendimento_liberar.py`: os três modos, 409 no
  repetido, divisão gravada.
- `roteirizacao/test_dedicado_fora_rota.py`: serviço com código dedicado sai
  da lista antes de `identificar_area_nao_atendida`.
- `test_notificar_dedicados_financeiro.py`: recorte da quinzena e corpo com
  subtotais, em `--modo-teste`.
- Prova manual: portal local em porta alternativa, XML de teste com
  destino fora de SP (cliente teste do portal), ver linha vermelha, abrir
  chat, liberar no painel em porta 8099, ver chip no Planejamento.

## Deploy

Commit + push, pull na VPS, chown, copiar timer/service novos,
`daemon-reload`, `enable --now`, restart de `portal-cliente`,
`portal-cliente-envios` e `painel-agentes`. Provar: enviar 1 XML de teste
pelo cliente de teste e liberar.

## Divergências na implementação (23/09)

- Código canônico é `PS-NNNNN` via `pedidos_dedicados.normalizar_codigo`,
  não `normalizar_order_number` (essa devolve só dígitos e não trata sufixo
  `-R1` nem lista com vírgula). Serviço Vuupt com dois códigos
  (`#PS-1, PS-2`) sai inteiro da rota automática se qualquer um for dedicado.
- "Solicitar cotação" não tem rota própria: o botão abre o widget
  (`window.atdAbrirChamado(id, texto)`) e usa o envio de mensagem que já
  existe no chat.
- A regra da roteirização entra via
  `identificar_area_nao_atendida([servico_sintetico], api_key)`, que já
  fecha `classificar_pedido` com as dependências.
- Quinzena fora dos dias 1/16 = última quinzena fechada antes de `--data-ref`.
- `"envios"` entrou em `chamados.AREAS` ("Envios do portal"), então o
  cliente também vê essa área ao abrir um chat novo.
- `atendimento_mobile.html` não recebeu o quadro de liberação (desktop só).
- O mapa do pool (`dedicados_por_servico`) mora em `roteirizacao/dedicados.py`,
  junto de `separar_dedicados`, não inline em `planejamento_rotas.py`.
