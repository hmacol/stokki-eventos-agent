# Galpão F0: notas da investigação na Stokki (só leitura)

Data: 01/11/2026 (leitura feita entre 23h e 00h, domingo). Cliente: Maria Dolores (`#stkkc-48`).
Autorização: Hugo liberou a leitura de produção para esta task. Nada foi escrito: nem formulário
enviado, nem escrita no banco, nem arquivo criado na VPS.

Como foi feito:
- Banco: `sqlite3` com `file:/opt/stokki-eventos/dados/dados.db?mode=ro`, como `www-data`.
- Stokki, área admin: `StokkiSession` (conta principal, a mesma dos jobs) + `stokki.pedidos.listar_pedidos`
  e GET em `/pt-br/administrator/inventory/outbound/show/{id}`. Total: 3 listagens + 8 GETs de `show`.
- Stokki, área de operador (`/pt-br/provider/`): `stokki/sondar_area_operador.py --scripts --acoes`
  (sonda de leitura que já existia) e um script igual, de leitura, só para ler o HTML/JS inline das telas
  de separação e conferência. Só navegação (GET), dentro da trava da conta provider
  (`na_vez_da_provider`). A conta provider logou duas vezes (aparece o aviso "conta já conectada em
  outro dispositivo": é o comportamento normal dessa conta, que só a Estação de Impressão usa à noite).
  Nenhum botão clicado.

---

## Step 1. Volume da Maria Dolores (últimos 30 dias)

Fonte: `nucleo_pedidos` (remetente `MARIA DOLORES INDUSTRIA E COMERCIO DE ALIMENTOS LTDA - NUU`,
`sender_id=16362183`, 976 pedidos no total) junto com `nucleo_rotas` (data da rota). Só pedidos
originais (sem `-R`), `excluido_em IS NULL`. Data do pedido = `nucleo_rotas.data_rota`; sem rota, o
dia de `criado_em_provedor`.

```sql
SELECT date(coalesce(r.data_rota, substr(p.criado_em_provedor,1,10))) d, count(*), count(distinct p.vuupt_route_id)
FROM nucleo_pedidos p LEFT JOIN nucleo_rotas r ON r.vuupt_route_id = p.vuupt_route_id
WHERE p.remetente_nome LIKE '%DOLORES%' AND p.excluido_em IS NULL AND p.codigo NOT LIKE '%-R%'
  AND d >= date('now','-30 day') GROUP BY d;
```

| Medida | Valor |
|---|---|
| Dias com pedido (04/10 a 30/10) | 20 |
| Total de pedidos | 286 |
| Pedidos por dia, média | 14,3 |
| Pedidos por dia, máximo | 25 (14/10) |
| Pedidos por dia, mínimo | 2 (04/10); dias típicos 7 a 25 |
| Pedidos por rota (só os da Maria Dolores) | média 2,19; máx. 13; mín. 1 (124 rotas) |
| Pedidos sem rota | 15 |
| Estado dos 286 | 233 ENTREGUE, 27 INSUCESSO, 12 ABERTO, 10 EM_ROTA, 4 CANCELADO |
| Caixas por pedido (`nucleo_pedidos.caixas`) | média 20,7; máx. 414; total 5.910 |

Por dia (pedidos, rotas): 10-04 (2,1) 10-05 (9,6) 10-06 (19,5) 10-07 (19,10) 10-08 (22,11) 10-09 (7,5)
10-13 (17,4) 10-14 (25,8) 10-15 (10,7) 10-16 (7,4) 10-19 (9,8) 10-20 (24,6) 10-21 (9,5) 10-22 (17,8)
10-23 (18,5) 10-26 (22,9) 10-27 (7,4) 10-28 (14,8) 10-29 (17,7) 10-30 (12,3).

Linhas, UN e SKUs. A fonte é `wms_pedido_itens` (itens que o sync de 15 min leu, só pedidos que
passaram por Waiting for Carrier/Separating). O sync cobre 305 dos 286+ pedidos desde 02/10 (não é 100%
do universo; o sync parou de gravar pedido novo em 30/10 13:49, ver "Não verificado").

```sql
SELECT avg(n),max(n),avg(un),max(un),avg(emb) FROM (SELECT pedido_id,count(*) n,sum(qtd_un) un,sum(qtd_embalagem) emb
  FROM wms_pedido_itens GROUP BY pedido_id);
-- 382 pedidos, 1.178 linhas: (3,08 linhas, máx 11; 13,7 UN, máx 276; 13,7 embalagens, máx 276)
-- desde 02/10: 305 pedidos, 3,23 linhas/pedido (máx 11), 13,4 UN/pedido (máx 276)
SELECT date(p.lido_em), count(distinct p.id), count(distinct i.sku) FROM wms_pedidos p
  JOIN wms_pedido_itens i ON i.pedido_id=p.id WHERE p.lido_em>='2026-10-02' GROUP BY 1;
```

- Linhas por pedido: média 3,2; máx. 11.
- UN por pedido: média 13,4; máx. 276 (a maioria dos pedidos é pequena, poucos grandes).
- SKUs distintos por dia (nos pedidos lidos): de 4 a 20, típico 12 a 15.
  Exemplos: 10-02: 32 pedidos / 20 SKUs; 10-06: 26 / 15; 10-21: 11 / 20; 10-29: 5 / 4.
- Cuidado: a mesma `qtd_embalagem` é "CX" na Stokki (caixa), e `qtd_un` o nosso cálculo em unidades.

Estado atual das tabelas `wms_*` da Maria Dolores (produção, 01/11):

| Tabela | Linhas | Observação |
|---|---|---|
| `wms_produtos` | 1.986 (83 da Maria Dolores) | desses 83: 73 com EAN, 46 com DUN, 46 com `qtd_por_caixa` |
| `wms_posicoes` | 165 | |
| `wms_areas` | 10 | |
| `wms_operadores` | 4 | Kleber, Eduardo, Anderson, Hugo (criados 04/09) |
| `wms_movimentos` | 0 | WMS vazio, como na memória |
| `wms_saldos` | 0 | |
| `wms_reservas` | 0 | |
| `wms_pedidos` | 382 | 380 `Waiting for Carrier` + 2 `Separating`, todos `estado_reserva=PARCIAL`; lidos de 23/09 a 30/10 |
| `wms_pedido_itens` | 1.178 | todos com `removido_em=''` (nenhum removido) |
| `wms_recebimentos` | 14 | todos `ESPERADO`/`Recebido`; último `lido_em` 22/10, atualizado 01/11 23:09 |
| `wms_recebimento_itens` | 158 | |
| `wms_recebimento_enderecamentos` | 0 | nenhum endereçamento feito |

Motivo de pendência mais comum nos itens: `EAN 004 esta em 3 produtos ativos` (23), `EAN 1 ...` (8):
EAN curto/lixo no cadastro da Stokki.

---

## Step 2. Itens no status Open

Resposta: **sim, os itens existem em Open, mas não na tabela que `extrair_itens_do_pedido` lê.**

Pedidos lidos (GET `show`):
- Open: não havia pedido Open da Maria Dolores no momento (listagem `state=Open, client=48`: 0 linhas;
  domingo à noite). Usei dois Open de outro embarcador (Laticínios Dourado, PS-43061 e PS-43060) só
  para provar a estrutura. **Não verificado com pedido Open da Maria Dolores.**
- Separating: 1 só da Maria Dolores (PS-42906, `#TO-24059`).
- Waiting for Carrier (para comparar): PS-42967.

A página tem outra tabela, `<table id="example1">` (aba "Itens do pedido"), com colunas
`NR. | ID | SKU | Nome | Quantidade | unidade | Valor Unitário | Quantidade confirmada | Peso informado | Obs`.
Ela existe em Open e Separating, com a quantidade pedida:

```html
<!-- PS-43061, situação "Aberto" -->
<tr><td>1</td><td>#ITM-1022</td><td>XDDOURADO</td><td>Mercadoria Dourado</td>
<td><h6><span class="badge badge-secondary">3</span></h6></td><td>CX</td><td>R$ 367,2767</td>
<td><h6><span class="badge badge-danger"></span></h6></td><td>-</td>...</tr>
<!-- PS-42906, "Em separação" (Maria Dolores): 2 linhas -->
CXPTD1FD  MINI CX PALITINHO DE TAPIOCA TRAD - 1KG FOOD  38 CX  R$ 165,3750
MNCXDCHF300  MINI CX DADINHO DO CHEF S/ MOLHO - 300G    18 CX  R$ 169,2000
```

- A coluna "Quantidade confirmada" fica vazia em Open/Separating e preenchida em Waiting
  (`<span class="badge badge-success">4</span>` + "Confirmado"). Serve como marca de "conferido".
- **Não há coluna EAN nessa tabela.** O EAN vem de `wms_produtos` pelo SKU (73 de 83 SKUs da Maria
  Dolores têm EAN).
- A tabela `Produto | EAN | Quantidade` (a que `extrair_itens_do_pedido` usa) **só aparece depois
  da conferência** (Waiting for Carrier): no PS-42967 foi a segunda tabela: "EANS lidos na separação".
  Em Open e em Separating `extrair_itens_do_pedido` devolve `[]`: confirmado nos 3 pedidos. Isso também
  explica o aviso "sem itens legiveis" do sync para pedidos em Separating.
- Em Separating (PS-42906) aparece também a tabela `Produto | Lote | Entrada | Fabricação | Validade |
  Quantidade` (aba "Detalhes de itens"): lotes que o sistema da Stokki alocou, com a quantidade por lote
  (ex.: `CXPTD1FD ... lote 1806, validade 23/02/2027, 20`; `... lote 07/10/2027, 17`; `... 1`). Em Open essa
  aba não existe (as abas são só: Itens do pedido, Documentos, Mensagens, Detalhes do transporte,
  Documento referenciado, Detalhes da operação, Despesas extras, Histórico, Anotações).
- Unidade: a quantidade é em **CX** (embalagem). A conversão para UN fica no nosso lado
  (`wms_produtos.qtd_por_caixa`).

Consequência para a F2: um parser novo (por exemplo `extrair_itens_pedido_aberto(html)`) deve ler
`table#example1` (cabeçalho com `SKU` e `Quantidade confirmada`), e não precisa de XML da NF. O XML da NF
fica como plano B (itens fiscais, sem a unidade CX da Stokki).

---

## Step 3. Como a Stokki registra cada etapa

### Onde fica

A página `show` da área admin (`/pt-br/administrator/inventory/outbound/show/{id}`) **não tem
formulário de separação nem de conferência**: só leitura. Os formulários dela (`form_post`,
`form_annotation`, `form_expense`, `form_danfe`...) são de mensagem, despesa, DANFE e carta de
correção. A separação e a conferência acontecem na **área de operador** `/pt-br/provider/...`, que
exige a conta *provider* (a mesma da Estação de Impressão e da expedição), com a trava
`na_vez_da_provider`.

Aba "Detalhes da operação" do `show` (já lida pelo Galpão 3D) mostra o resultado de cada etapa:

```text
PS-42967 (Waiting): Tarefa da Operação: #TO-24107  Operador da Tarefa: Carla  Código de separação: Q000000639
 Estação de conferência: Estação de Conferência 1  Usuário da Estação da Conferência: Caio
 Inicio da Conferência: 30/10/2026 13:56  Finalização da Conferência: 30/10/2026 14:03  Tempo: 00:06:50
Histórico do pedido: 30/10 14:03 Caio Aguardando Transportador | 13:36 Carla Em Conferência |
 13:34 Carla Em separação | 12:13 Hugo Maçol Aberto
```

### (a) Início da separação

Há dois caminhos, ambos na área de operador (ainda não exercitamos nenhum, só lemos):

1. **Seleção de Pedidos por Lista** `GET /pt-br/provider/operation/picking/list`. Fluxo do JS:
   `form_order` (campos `id, date, type, option, client_id, carrier_cnpj, b2b`) escolhe o pedido,
   rotas `picking/list/order/table`, `.../order/filters`, `.../order/dates`; depois o passo
   "Selecione os operadores" (`select#select_operator`, `name="user_id[]"`, múltiplo) e
   `form_finish`: **POST `https://freshlog.stokki.com.br/pt-br/provider/operation/picking/list/finish`**
   (multipart/FormData; campos `_token`, `provider_outbound_id`, `user_id[]`). A resposta é assíncrona
   ("finishV3 processa na fila"): consulta `GET .../picking/list/finish/status/{id}`; erros mostram
   "Falha ao criar tarefa". Resultado: cria a `#TO-xxxxx` ("Tarefa da Operação") e o pedido passa a "Em separação"
   com o "Código de separação" (`Q...`/`W...`).
2. **Lista de Pedidos** `/pt-br/provider/operation/order`, menu de ações com `data-action="separation"`
   e `"confirm_separation"` (atalho usado pelo `stokki/avancar_cross.py` para Cross): modal `#modal_action`,
   form `#form_action`, **POST `/pt-br/provider/inventory/outbound/action`** (multipart) com
   `outbound_id, action, page, select_situation, select_motion, select_client, select_unit,
   request_invoice, request_cte, request_tag, volume, hour, reason, _token`. Ações vistas no `switch`:
   `carrier, confirm_separation, correction_letter, download_documents, hold, invoice, sent, separation, tag`.
   `avancar_cross.py` só chegou a abrir o modal de "iniciar separação" e a escolher operador por nome;
   **nunca foi confirmado** com envio real na Stokki (o histórico git para na sonda). Ou seja, não
   sabemos ainda se `separation` + `confirm_separation` levam de Aberto a Aguardando Transportador.

### (b) Itens separados / EANs lidos

Coletor/estação "Seleção de Pedidos" v2: `GET /pt-br/provider/operation/order/v2/selection`. Formulários e
rotas (todas POST/GET por JS, com `_token`):

| Form | Campos | Rota |
|---|---|---|
| `form_task` | `task_type, zone` | `.../v2/selection/task/store` |
| `form_product` | `task_id, task_location_id, ean, save` | `.../v2/selection/product/store` |
| `form_absence` | `task_id, task_location_id` | `.../v2/selection/product/absence` (falta) |
| `form_serial_number` | `task_id, task_location_id, code` | `.../v2/selection/serial/number` |
| `form_box` | `task_id, box_id, box_code, provider_outbound_id` | `.../v2/selection/box/store`, `.../box/store/order` |
| `form_pending` / `form_concluded` | `task_id` | `.../v2/selection/task/pending`, `.../task/concluded` |

Ou seja: a Stokki espera **o EAN bipado por posição (`task_location_id`)**, e depois o código de caixa
(`box_code`). É isso que vira "EANS lidos na separação" e "Quantidade total separada". Existe também
`picking/batch` e `puttowall/*` (colmeia), que não usamos.
**Não verificado**: se `product/store` aceita quantidade (`save`), ou uma chamada por EAN lido; e se
`task/concluded` exige ter lido tudo (a UI diz "Separar"/"Próximo pedido").

### (c) Conferência (usuário, início, fim)

Estação de Conferência: `GET /pt-br/provider/operation/order/packaging`.
- `form_search` (`puttowall, packing_station, search`) → **POST `.../packaging/search`** (busca o pedido
  pela NF ou código; `packing_station` = `select#select_packing_station`, ex. "Estação de Conferência 1").
  Também `form_search_multiple` (`task_id, action, search`) → `.../packaging/multiple/search`.
- Fecha com `form_packaging` (`shipment_automatic, action=billed, printed_pages, provider_outbound_id,
  volume`) → **POST `https://freshlog.stokki.com.br/pt-br/provider/operation/order/packaging/store`**
  (multipart); sucesso: toast "Pedido embalado" e redireciona para a própria estação.
- Rotas auxiliares do JS: `packaging/station/`, `packaging/cache/`, `packaging/count/`,
  `packaging/create/`, `packaging/danfe/`, `packaging/tag/`, `packaging/volume/control/`,
  `packaging/correctionletter/`.
- Início e fim: a Stokki mede sozinha (do `search` ao `store`): "Inicio da Conferência" /
  "Finalização" / "Tempo de Conclusão". Não há campo para enviarmos horário.

### (d) "Quantidade de volumes informada na conferência"

Vai no mesmo `packaging/store`, campo `volume` (hidden, valor inicial `0`). A UI conta `count_volume`
e há `packaging/volume/control/` (controle de volume por etiqueta, "Imprimir Controle de Volume"). No
`show`, a aba "Detalhes do transporte" mostra `Quantidade de volumes mencionado na NF-e: 18` e
`Quantidade de volumes informado na conferência: 18`, seguido de uma linha `Peso verificado (kg) /
Embalagem: Padrao` por volume (PS-42967). O parser `quantidade_volumes` já usa essa linha.

### (e) Passagem para Waiting for Carrier

É o efeito do `packaging/store` (conferência concluída): o histórico do pedido registra
"Aguardando Transportador" em nome do conferente. Não há botão separado.

### CSRF

Todas as telas trazem `<input name="_token" type="hidden">` em cada formulário e
`<meta name="csrf-token">` no `<head>` (o parser `_parsear_pagina_detalhe` já extrai esta última).
Todos os POST são multipart (`FormData`). Precisa de cookies da sessão da conta **provider**.
O arquivo de cookies da provider é separado do da conta principal (`stokki/auth.caminho_cookies`).

### Operador real ou usuário logado?

- **Separação: dá para informar o operador real.** O passo de operadores (`user_id[]`) lista (lido em
  01/11): `Carla (9)`, `Recebimentofrio01 (10)`, `Thiago Meira (13)`, `Werlly Oliveira (14)`, `Carla (17)`,
  `Kleber (102)`. Só quem existe nessa lista como usuário de operador da Stokki pode ser escolhido. Ao
  usar o robô, ele escolhe pelo nome; um operador nosso que não esteja ali (por exemplo "Eduardo",
  "Anderson", "Hugo") não pode ser gravado. Resolve-se cadastrando-o na Stokki ou mapeando o nome.
  Obs.: `avancar_cross.py` já usava `Recebimentofrio01` por padrão; `Carla` aparece 2 vezes (ids 9 e 17).
- **Conferência: a Stokki grava o usuário logado.** O formulário `packaging/store` não tem campo de
  usuário. Hoje o "Usuário da Estação da Conferência" é `Caio`, `Fael` ou `Felipe Gabriel` porque cada um
  entra na estação com o seu login (tabela `galpao_conferencias`: Felipe Gabriel 241, Fael 170, Caio 162).
  O robô só teria a conta provider única (`stokki.provider`, "wms@"), então a conferência ficaria no nome
  do robô. Para manter o nome real, só com logins por pessoa (não temos). **Não verificado** se a
  estação aceita trocar de usuário sem novo login. Efeito: o Galpão 3D e os indicadores de conferência
  por pessoa perderiam o nome; mitigar guardando o conferente real do nosso lado (já temos o fluxo próprio).

---

## Step 4. Dados para a etiqueta de volume e para a onda

Fonte já disponível, sem pedir mais nada à Stokki:

| Dado | Onde | Observação |
|---|---|---|
| Número da NF | **Título do serviço na Vuupt**: `#PS-42967 - 42038 / NUU / NUTRICAR...` (em `nucleo_pedidos.titulo` e `dados_json.payload.title`). Também a chave `NF-e` no `show` (44 dígitos; nº = dígitos 26 a 34, ex. `...0000420381...` → 42038), e `documentos_pedido.numero_nf` / `chave_acesso` quando há documento | A Stokki não mostra o número separado, só a chave |
| Destinatário (nome, cidade, UF) | `nucleo_pedidos.destinatario_nome`, `endereco` (texto único `rua número - compl, BAIRRO, Cidade - UF, CEP, Brasil`) e `zona` | O bairro é o segmento antes da cidade (ex. `TAMBORE, Barueri - SP`); precisa de parser |
| | `show` Stokki, bloco "Destino:" (`stokki.pedidos._extrair_blocos_endereco`): `nome, documento, endereco, cidade, uf, cep, telefone, email` | Vem separado, mas o bairro só no `endereco` |
| Data de entrega | `nucleo_pedidos.agendamento_inicio` (ex. `2026-11-03 10:00:00`) + `horario_inicio/fim`; ou `nucleo_rotas.data_rota` quando já roteirizado | Em UTC sem fuso (Vuupt) quando vem da Vuupt; confira a regra de fuso do projeto |
| Data de saída (expedição) | `show`: "Data de saída:" (ex. 31/10/2026) e "Data do Pedido" | **Não é a data de entrega**: é a data de expedição planejada |
| Caixas | `nucleo_pedidos.caixas` (campo `dimension_3` do payload, ex. 27) | Volume ponderado |
| Volumes (NF e conferência) | `show` → "Quantidade de volumes mencionado na NF-e" / "informado na conferência" (`pedidos._parsear_pagina_detalhe`) | |

Não há campo de bairro isolado; se a etiqueta exigir bairro limpo, usar o `endereco` do núcleo.

---

## Step 5. Roteiro proposto para os robôs R1 e R2

Premissa: o robô roda na VPS, como `www-data`, dentro da trava da conta provider
(`sessao_uso.na_vez_da_provider`) e só escreve depois de `--modo-teste` e de aprovado pelo Hugo.

### R1 (nosso app terminou a separação → Stokki fica "Em separação" e depois "Em conferência")

1. Pré-checagem (GET, conta principal): `show` do pedido; exige situação `Aberto` e itens iguais
   aos nossos (parser novo de `table#example1`); aborta se a situação for outra (idempotência
   por pedido+marco em `wms_replicacao_stokki`).
2. Playwright (provider): `picking/list` → filtrar pelo pedido → "Selecione os operadores" =
   o operador real pelo nome (mapa nome nosso → nome Stokki) → confirma → `POST picking/list/finish`
   (`provider_outbound_id`, `user_id[]`); esperar `finish/status/{id}`.
3. Registro dos itens, uma de duas opções, a decidir na primeira prova real:
   - **R1-A (completa)**: replicar o coletor v2: `task/store`, `product/store` por EAN lido
     (`task_location_id`), `box/store`, `task/concluded`. Fiel, mas é um passo por bipe.
   - **R1-B (mínima)**: só a etapa (a) e deixar o resto para a conferência (como Cross faz).
4. Conferir pós-ação (GET `show`): situação `Em separação`, `Operador da Tarefa` = operador real.
   Se falhar: tentar de novo e alertar a equipe, que termina à mão (spec §10).

### R2 (conferência no nosso app → Stokki: Aguardando Transportador)

1. Pré-checagem: situação `Em separação` ou `Em Conferência`, código de separação preenchido.
2. Playwright (provider): `order/packaging` → escolher `packing_station` → `POST packaging/search`
   com NF ou código → `POST packaging/store` com `provider_outbound_id`, `volume` = nossa contagem de
   volumes, `action=billed`, `printed_pages` (ver abaixo), `shipment_automatic=0`.
3. Conferir pós-ação (GET `show`): situação `Aguardando Transportador`, "Quantidade de volumes
   informado na conferência" = nosso número, `Usuário da Estação da Conferência` (será o robô).
4. A prontidão da rota (`nucleo/prontidao_stokki.py`) já libera pedido quando a Stokki sai de
   separação/conferência, então não muda nada do lado da roteirização.

### Riscos a tratar no plano da F2

- Conferência no nome do robô (item acima). Decisão do Hugo: aceitar e guardar o conferente real do nosso lado.
- `printed_pages` e as impressões de DANFE/etiqueta na estação: o `store` pode exigir ter impresso a
  DANFE (a UI tem `btn_danfe_modal`, `outbound_packaging_print_*`). Testar com `--modo-teste`/pedido de teste.
- Sessão Stokki compartilhada: o login da provider derruba a sessão provider de outro. Trava obrigatória.
- Expedição Programada (OE) pendente trava o robô de expedição, não o desta etapa.
- Operadores só os 6 acima; novos nomes dependem de cadastro na Stokki.

---

## O que NÃO foi possível verificar

1. Pedido **Open** da Maria Dolores: não havia nenhum (domingo à noite). A estrutura de `table#example1`
   foi provada em Open de outro embarcador (PS-43061, PS-43060) e em Separating/Waiting da Maria Dolores.
   Só 1 Separating da Maria Dolores (pedia 3).
2. Nenhum formulário foi enviado: **não sabemos** se `separation`/`confirm_separation` (Lista de
   Pedidos), `picking/list/finish`, `product/store` e `packaging/store` realmente aceitam o que o robô
   mandaria, nem a regra exata de `printed_pages`/`volume` e se `task/concluded` exige ler todos os EANs.
3. O corpo das respostas dos endpoints (JSON de `picking/list/finish`, `.../status/{id}`) e os campos
   do modal "iniciar separação" não foram lidos (precisam de clique).
4. Se a estação de conferência permite informar usuário diferente do logado: indício forte de que não.
5. Estado do sync `sincronizar_pedidos_wms`: o último `lido_em` em `wms_pedidos` é 30/10 13:49:52 e há 12
   ABERTOS + 10 EM_ROTA no núcleo; não investiguei se o timer parou ou se não há pedido novo em
   Waiting/Separating/Ready to Pack.
6. Fuso das datas: `agendamento_inicio` vem da Vuupt (UTC sem fuso, exceção conhecida do projeto).

## Arquivos e endpoints citados

- Código: `stokki/pedidos.py` (`extrair_itens_do_pedido`, `_parsear_pagina_detalhe`),
  `stokki/avancar_cross.py`, `stokki/sondar_area_operador.py`, `sincronizar_pedidos_wms.py`,
  `galpao3d/fixtures/show_*.html`.
- Tabelas: `nucleo_pedidos`, `nucleo_rotas`, `wms_*`, `galpao_conferencias`, `documentos_pedido`.
