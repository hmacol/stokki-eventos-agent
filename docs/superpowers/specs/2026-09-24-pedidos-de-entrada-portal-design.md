# Portal: aba Pedidos de Entrada (mercadoria que vai chegar no galpão)

Data: 24/09/2026. Pedido do Hugo.
Módulo: portal do cliente + WMS (fase 2) + Stokki (incoming).
Piloto: Maria Dolores (`wms.embarcador_piloto_id` no `config.yaml`).

## 1. Problema

O portal só conhece pedidos de **saída** (aba Envios: NF-e ou planilha viram
pedido na Stokki). Mercadoria que o cliente manda **pro galpão** entra hoje
por fora: o cliente avisa por e-mail, alguém da Fresh Log cria o recebimento
na Stokki à mão (`#PE-xxxx`), o timer `stokki-wms-recebimentos` lê de lá e o
galpão endereça no celular. O cliente não vê nada disso e a equipe digita o
que o cliente já tinha em arquivo.

## 2. O que este projeto entrega

Uma aba **Pedidos de Entrada** no portal, onde o cliente anuncia a
mercadoria que vai chegar (XML da NF-e de remessa ou planilha), acompanha
quando chegou, quando foi endereçada e o que faltou, e pode cancelar
enquanto não chegou. A entrada anunciada vira o recebimento (`#PE`) na
Stokki sem ninguém digitar, e daí a fila do galpão no WMS, como já acontece
hoje com os `#PE` criados à mão.

## 3. Decisões do Hugo (24/09/2026)

| # | Decisão | Escolha |
|---|---------|---------|
| D1 | Que nota o cliente sobe | NF-e de **remessa para armazenagem emitida pelo próprio cliente**, Fresh Log destinatária. Validação: emitente = CNPJ do cliente (igual Envios) |
| D2 | Stokki | **Sondar primeiro**. Sondagem feita em 24/09 (seção 8.1): a Stokki **tem** wizard de criação de recebimento por XML, então o portal cria o `#PE` sozinho. Hugo pediu esforço máximo nesse caminho; o caminho sem Stokki foi descartado |
| D3 | Alcance | **Só Maria Dolores** (piloto), como o WMS |
| D4 | Data prevista de chegada | **Obrigatória** no anúncio |
| D5 | Ação do cliente | **Cancelar**, só enquanto ANUNCIADO. Revisão de divergência fica pelo e-mail de faltas que já existe |
| D6 | Abordagem | **Módulo próprio** (`portal_cliente/entradas.py`, tabela `portal_entradas`), sem reaproveitar `portal_envios` |

Suposição minha, aceita na conversa: destinatário do XML diferente da
Fresh Log gera **aviso** na prévia, não erro.

## 4. O que já existe e é reaproveitado

- `portal_cliente/envio_pedidos.py`: `expandir_upload` (zip), `ler_nfe`,
  `guardar_temporario` + `limpar_temporarios`, `validar_skus`,
  `_abrir_planilha`, `_achar_cabecalho`, `formatar_documento`. O módulo novo
  **importa** essas funções; nada é copiado.
- `painel_agentes/wms_pedidos.py`: `wms_recebimentos`,
  `wms_recebimento_itens`, `registrar_recebimento`, `resolver_item`,
  `contabilizar_enderecamento`, `encerrar_com_divergencia`,
  `faltas_congeladas`.
- `sincronizar_recebimentos_wms.py`: timer de 30 min que lê
  `inventory/incoming` do piloto.
- `painel_agentes/wms_faltas_recebimento.py`: e-mail de faltas ao cliente
  (desligado por `forcar_destino`, decisão do Hugo).
- `stokki/recebimentos.py`: leitura de `incoming/table` e `incoming/show/{id}`.
  Só leitura foi provada; criação nunca foi sondada.
- Padrão de chamado no atendimento (bloqueio de área, 23/09): usado quando o
  cancelamento precisa de mão humana na Stokki.

## 5. Modelo de dados

Duas tabelas novas em `dados/dados.db`, criadas em `entradas.conectar()` com
`CREATE TABLE IF NOT EXISTS`, como `envio_pedidos.py` faz.

```sql
CREATE TABLE IF NOT EXISTS portal_entradas (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_embarcador    TEXT NOT NULL,
    origem             TEXT NOT NULL,            -- 'xml' | 'planilha'
    chave_nfe          TEXT NOT NULL UNIQUE,     -- real no XML; sintética na planilha
    numero_nf          TEXT,
    serie              TEXT,
    emitida_em         TEXT,
    referencia         TEXT,                     -- nº do pedido de compra/remessa
    data_prevista      TEXT NOT NULL,            -- AAAA-MM-DD (D4)
    volumes            INTEGER,
    peso_kg            REAL,
    valor_nf           REAL,
    arquivo_path       TEXT NOT NULL,
    status             TEXT NOT NULL,            -- ANUNCIADO | CHEGOU | ENDERECADO | DIVERGENCIA | CANCELADO
    stokki_status      TEXT NOT NULL DEFAULT 'NA_FILA',
                                                 -- NA_FILA | ENVIANDO | CRIADO | ERRO
    stokki_id          INTEGER,                  -- id do #PE (o do href /incoming/show/{id})
    stokki_codigo      TEXT,                     -- '#PE-2478'
    stokki_erro        TEXT,
    stokki_tentativas  INTEGER NOT NULL DEFAULT 0,
    wms_recebimento_id INTEGER,                  -- wms_recebimentos.id
    observacoes        TEXT,
    enviado_por        TEXT,
    criado_em          TEXT NOT NULL,
    atualizado_em      TEXT NOT NULL,
    cancelado_em       TEXT,
    cancelado_por      TEXT,
    chamado_id         INTEGER                   -- chamado aberto pra cancelar na Stokki
);
CREATE INDEX IF NOT EXISTS idx_portal_entradas_emb ON portal_entradas (cnpj_embarcador, criado_em);
CREATE INDEX IF NOT EXISTS idx_portal_entradas_status ON portal_entradas (status);

CREATE TABLE IF NOT EXISTS portal_entrada_itens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entrada_id  INTEGER NOT NULL REFERENCES portal_entradas(id),
    linha       INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    ean         TEXT NOT NULL DEFAULT '',
    descricao   TEXT NOT NULL DEFAULT '',
    quantidade  REAL NOT NULL,
    unidade     TEXT NOT NULL DEFAULT '',
    UNIQUE (entrada_id, linha)
);
```

Chave sintética da planilha: `PLANILHA-ENTRADA-<cnpj>-<ref>-<sha1[:10]>`,
mesmo desenho de `chave_planilha` de Envios, com prefixo próprio pra nunca
colidir com uma chave de saída.

Mudanças em tabelas existentes, todas por `ALTER TABLE` guardado
(`_garantir_colunas` / `_COLUNAS_NOVAS`), nunca recriando tabela:

- `portal_clientes_envio.entradas_ativo INTEGER NOT NULL DEFAULT 0`. Ligado
  só pra Maria Dolores (D3), por `gerenciar_clientes.py`.
- `wms_recebimentos.portal_entrada_id INTEGER` (ligação de volta).
- `wms_recebimentos.data_prevista TEXT NOT NULL DEFAULT ''` (vem do portal;
  vazia nos que vieram só da Stokki).
- `wms_recebimentos.estado` ganha o valor `CANCELADO` (hoje: ESPERADO,
  ENDERECADO, DIVERGENCIA). Sem mudança de esquema.

## 6. Ciclo de vida

| Estado | Quem coloca | Quando |
|---|---|---|
| ANUNCIADO | o cliente, em `confirmar` | ao gravar |
| CHEGOU | `entradas.sincronizar_status` | o `wms_recebimentos` ligado tem situação "Recebido" na Stokki, **ou** já tem algum endereçamento (`qtd_enderecada > 0` em alguma linha) |
| ENDERECADO | `sincronizar_status` | `wms_recebimentos.estado = 'ENDERECADO'` |
| DIVERGENCIA | `sincronizar_status` | `wms_recebimentos.estado = 'DIVERGENCIA'` |
| CANCELADO | o cliente, em `cancelar` | só a partir de ANUNCIADO |

Regras:

- `sincronizar_status(conn)` é uma função só, em `entradas.py`, chamada no
  fim de `sincronizar_recebimentos_wms.rodar()` e no fim da rota
  `/api/wms/recebimentos/<id>/encerrar-divergencia` do painel. Só anda pra
  frente (ANUNCIADO → CHEGOU → ENDERECADO | DIVERGENCIA) e **nunca** mexe em
  CANCELADO.
- "Atrasado" não é estado: `status = ANUNCIADO` e `data_prevista < hoje`.
  Chip vermelho na tela e tile próprio.
- Cancelar:
  1. `portal_entradas.status = CANCELADO`, `cancelado_em/por`.
  2. Se há `wms_recebimento_id` e o recebimento está ESPERADO sem nenhum
     endereçamento: `wms_recebimentos.estado = 'CANCELADO'`. Com
     endereçamento a entrada já seria CHEGOU e o botão não existe.
  3. Se há `stokki_id` (o `#PE` já foi criado): abre chamado
     no atendimento com `chamados.criar_chamado(conn, cliente,
     ORIGEM_SISTEMA, STATUS_AGUARDANDO_FL, assunto="Cancelar recebimento
     #PE-x na Stokki")`, o mesmo que `bloqueio_area.py` usa, e grava
     `chamado_id`. Nunca tenta cancelar na Stokki sozinho.
  4. Com `stokki_status = NA_FILA`: vira CANCELADO antes do worker pegar; o
     worker só consome `NA_FILA` com `status = ANUNCIADO`.
- O timer de recebimentos e o app do galpão ignoram `wms_recebimentos` com
  estado CANCELADO (entra na mesma lista de "fechados" de `rodar()` e some
  da aba Receber, que já filtra por ESPERADO).

## 7. Entrada de dados

### 7.1 XML de NF-e (D1)

`entradas.ler_nfe_entrada(conteudo, nome)` chama `envio_pedidos.ler_nfe`
com um parâmetro novo `permitir_entrada=True` (padrão `False`, pra Envios
continuar recusando `tpNF=0`) e acrescenta os itens:

```
det/prod: cProd -> sku, cEAN -> ean ('SEM GTIN' vira ''), xProd -> descricao,
          qCom -> quantidade, uCom -> unidade
```

Validação, nesta ordem, em `entradas.validar_item` (devolve `{ok, erros[],
avisos[], existente}` como a de Envios):

1. NF-e válida com chave de 44 dígitos (já em `ler_nfe`).
2. Emitente = CNPJ do cliente logado; se for de outra empresa do grupo,
   mesma mensagem "troque a empresa no seletor".
3. `chave_nfe` já anunciada → erro "já anunciada em DD/MM, status X",
   exceto se a anterior for CANCELADO: aí vira aviso "já esteve anunciada,
   será reaberta" e `confirmar` **regrava a mesma linha por UPDATE**
   (status volta a ANUNCIADO, data prevista nova, itens apagados e
   regravados), exatamente como Envios faz com ERRO/CANCELADO.
4. Destinatário ≠ CNPJ da Fresh Log → **aviso**. O CNPJ vem de uma chave
   **nova** no `config.yaml`, `portal_entradas.cnpj_freshlog` (hoje não há
   CNPJ da Fresh Log em lugar nenhum do código). Sem a chave, o aviso não é
   emitido.
5. SKU fora do catálogo do embarcador → **erro**, pela mesma
   `validar_skus` de Envios (sem catálogo sincronizado: só aviso).
6. `data_prevista` obrigatória, hoje ou futura. Não vem do XML: o cliente
   preenche na prévia (uma data pro lote, ajustável por nota).

### 7.2 Planilha de entrada

Modelo próprio, baixável em `/api/entradas/modelo-planilha`, uma linha por
item, agrupado por referência (mesma mecânica de `ler_planilha`, com
`COLUNAS_PLANILHA_ENTRADA` própria):

| Chave | Rótulo | Obrigatória |
|---|---|---|
| referencia | Referência (nº do pedido de compra/remessa) | sim |
| data_prevista | Data prevista de chegada (DD/MM/AAAA) | sim |
| sku | SKU do produto | sim |
| quantidade | Quantidade | sim |
| unidade | Unidade | não (padrão: a do catálogo) |
| numero_nf | Nº da NF | não |
| volumes | Volumes | não |
| peso_kg | Peso (kg) | não |
| observacoes | Observações | não |

Sem endereço, destinatário ou valor: o destino é sempre o galpão. Linhas
do mesmo pedido com `data_prevista` diferente → erro no grupo.

### 7.3 Prévia e confirmação

Igual Envios: `analisar` guarda temporários e devolve a lista com erros e
avisos por nota; `confirmar` recebe os tokens + `data_prevista` +
`observacoes` por item, regrava as validações (o catálogo pode ter mudado),
grava `portal_entradas` + `portal_entrada_itens` como ANUNCIADO e
`stokki_status = NA_FILA`, e move o arquivo pra
`dados/portal_entradas/<cnpj>/`. Commit por entrada, nunca em lote
(`dados.db` compartilhado).

## 8. Stokki, WMS e conciliação

### 8.1 O que a sondagem provou (24/09/2026, só GET, na VPS)

Feita em duas rodadas como `www-data`, dentro de
`sessao_uso.adquirir("sondagem-incoming")`. HTML bruto guardado em
`dados/sondagem/` na VPS (pasta ignorada pelo git). Nada foi escrito na
Stokki.

**A Stokki cria recebimento por três caminhos**, todos no menu de
`/pt-br/administrator/inventory/incoming` ("Pedidos de Entrada"):

| Tela | Como cria | POST |
|---|---|---|
| `inventory/incoming/xml/multiple/create` | solta 1..N XMLs de NF-e; o JS da página lê a NF-e no navegador e monta um form por arquivo | `inventory/incoming/xml/multiple/store` (multipart) |
| `inventory/incoming/create/excel/incoming` | solta 1..N planilhas no modelo da Stokki | `inventory/incoming/excel/store` |
| `inventory/incoming/create/invoice` e `create/manual` | formulário único (`form_order`) | `inventory/incoming/store` |

Detalhes do wizard XML (extraídos do JS da página salva):

- Passo 1: `select client_id` (`#stkc-<id>`). Ao escolher, GET
  `xml/multiple/create/client/<id>` devolve JSON com `cnpj`,
  `client.regime` e `warehouses[]`. Pro piloto (48): `warehouse_id = 1`
  (São Paulo, SP), regime `general_filial`, CNPJ 22.135.070/0001-90.
- Passo 2: `type_transport` (valores `Fractional (LTL)`, `Capacity (FTL)`,
  `Express Shipping`), `packaging` (`Loose Cargo (Boxes)` ou `Palletized`),
  `arrival_date` (dd/mm/aaaa, mínimo hoje), `same_day_receipt`,
  `check_warehouse` + `provider_invoice` (só quando a origem é o próprio
  armazém; não usamos).
- Passo 3 ("Próximo" → aba Arquivos): um `form_incoming_<n>` por XML. O JS
  valida **só** que a NF-e tem número; a checagem de CNPJ do emitente e de
  CFOP (5949/6949/5905/6905/5663/6663) só vale pra regime
  `general_warehouse`, que não é o do piloto.
- O POST leva, por arquivo: `client_id`, `destination_id` (= warehouse),
  `packaging`, `type_transport`, `same_day_receipt`, `arrival_date`,
  `po` (= número da NF), `invoice` (= chave de 44 dígitos),
  `value_invoice`, `species`, `volumes`, `freight_value`, os campos
  `*_origin` do emitente, `*_carrier` do transportador quando há, e
  `sku[]`/`quantity[]`/`unitary_value[]` (linhas do mesmo `cProd`
  **somadas**), mais `file_invoice[]` com o próprio XML.
- Sucesso é HTTP 2xx; erro é 4xx com `responseJSON.errors`. O JS não usa o
  corpo da resposta de sucesso.

O modelo Excel de entrada é público
(`prod-app-stokki-files.s3.sa-east-1.amazonaws.com/models/modelo_de_pedido_de_entrada.xlsx`)
e tem as **mesmas três colunas** do de saída: `SKU | Quantidade | Valor
Unitário`. O POST leva `motion=incoming`, `client_id`, `destination_id`,
`po`, `origin_id`, `same_day_receipt`, `arrival_date`, `carrier_id` e
`file_excel[]`.

**O detalhe de um `#PE` mostra a chave da NF-e.** Nos três recebimentos
reais da Maria Dolores lidos (`#PE-2458`, `2478`, `2497`): "Ref. do Pedido"
= número da NF (41003, 41099, 41221) e "NF-e" = chave de 44 dígitos. Os
três foram criados a partir da NF de remessa da própria Maria Dolores
(emitente = CNPJ dela), o que confirma D1 com dado real.

Armadilha corrigida: o segundo número no campo `id` da listagem
(`#PE-2478 41099`) **é o número da NF** ("Ref. do Pedido"), não um id
interno como o docstring de `stokki/recebimentos.py` diz hoje. Continua
não servindo pra abrir `show/{id}`, mas serve pra conciliar.

Custo da sondagem: a primeira rodada chutou URLs que não existem, e URL
inexistente redireciona pro `/login`, que o `auth.py` trata como sessão
caída e refaz o login via Playwright. Foram uns 14 logins em 2 minutos.
Nenhuma rotina falhou (o timer seguinte logou de novo e seguiu), mas fica
a regra: **nunca sondar URL por adivinhação**; extrair do menu primeiro.

### 8.2 Worker: portal cria o `#PE` na Stokki

`portal_cliente/enviar_entradas_stokki.py`, mesmo desenho de
`enviar_stokki.py`: trava cooperativa (`DONO_TRAVA = "portal-entradas"`),
`--loop`, `--uma-vez`, `--simular`, `--visivel`, 3 tentativas técnicas
antes de ERRO, recusa da Stokki vira ERRO na hora com o texto na aba do
cliente. Consome `stokki_status = NA_FILA AND status = ANUNCIADO`, agrupado
por embarcador. Serviço systemd `portal-cliente-entradas` em `infra/`.

Como cria, por origem:

- **XML**: Playwright abre `xml/multiple/create`, escolhe o cliente
  (`client_id` = `stkkc_id` do embarcador), `warehouse_id`, tipo de
  transporte e embalagem de `portal_clientes_envio` (mesmos campos que
  Envios já usa: `tipo_transporte`, `embalagem`), `arrival_date` =
  `data_prevista`, marca `same_day_receipt` se for hoje, solta o XML em
  `#input_drop_file`, clica "Próximo" e depois "Criar pedido". A resposta do
  `xml/multiple/store` é capturada por hook em `XMLHttpRequest`, **a mesma
  técnica de `importar_stokki.py`** (que hoje captura `/sale/xml/store`).
  4xx com `errors` → ERRO com a mensagem; 2xx → CRIADO.
- **Planilha**: `create/excel/incoming` com o xlsx gerado por
  `envio_pedidos.xlsx_pedido_stokki(itens)` (o modelo é o mesmo), `po` =
  `referencia`, `arrival_date` = `data_prevista`. `origin_id` e
  `carrier_id`: o plano sonda (GET) `create/excel/client/<id>` pra ver o que
  a Stokki devolve; se a origem for obrigatória e não houver cadastro do
  embarcador como origem, esse caso vira ERRO com mensagem clara em vez de
  chute.

Depois do CRIADO, ainda na mesma rodada, o worker descobre o `#PE`:
`listar_recebimentos(cliente=stkkc, busca=<numero da NF ou referencia>)`,
lê `show/{id}` do candidato e confere **a chave NF-e** (XML) ou o
"Ref. do Pedido" (planilha). Bateu: grava `stokki_id` e `stokki_codigo`.
Não bateu: fica CRIADO sem id e o timer de recebimentos tenta de novo na
próxima rodada (8.3).

Antes de subir, o worker confere a fila da Stokki pela chave: se
`listar_recebimentos(busca=<numero>)` já mostra um `#PE` com essa chave, a
entrada vira CRIADO direto (DUPLICADO na prática), sem criar de novo. É a
proteção contra rodar duas vezes depois de uma resposta perdida.

### 8.3 Amarração com o WMS

`sincronizar_recebimentos_wms.rodar()` continua igual e ganha um passo ao
gravar cada `wms_recebimentos`: procura `portal_entradas` do mesmo
embarcador por `stokki_id`; se não tiver, pela **chave NF-e** que o
`show/{id}` mostra (XML) ou pelo "Ref. do Pedido" = `referencia`
(planilha). Achou: amarra os dois lados (`wms_recebimento_id` num,
`portal_entrada_id` + `data_prevista` no outro) e completa
`stokki_id`/`stokki_codigo` se estavam vazios. Depois chama
`sincronizar_status`.

Isso também cobre o `#PE` que a equipe criar à mão a partir de uma nota
que o cliente já anunciou no portal: a chave é a mesma, então amarra.

### 8.4 O que não muda

- Estoque: **nenhuma** escrita em `wms_movimentos` ou `wms_saldos` a partir
  do portal. O estoque entra quando o operador endereça, como hoje.
- `stokki/recebimentos.py` continua sendo a única leitura de `incoming`;
  ganha `extrair_chave_nfe(html)` e `extrair_ref_pedido(html)` pro
  `show/{id}`, achando a célula pelo cabeçalho como o resto do módulo.
- E-mail de faltas continua saindo da rota de divergência do painel, sem
  mudança. A aba mostra a divergência com as faltas congeladas
  (`faltas_congeladas`).

## 9. A tela

- Aba **Pedidos de Entrada** em `acompanhamento.html`, parcial novo
  `_entradas.html`, visível só quando `entradas_ativo = 1` na empresa
  selecionada. `?aba=entradas` entra no `aba_inicial` (hoje aceita
  `acompanhamento` e `envios`). Mesma trava de nível de equipe de Envios
  (`_exige_pode_enviar`).
- Topo: "Anunciar mercadoria" (XML, ZIP ou planilha) e "Baixar modelo".
- Tiles: Anunciados, Atrasados, Chegaram hoje, Com divergência; clicar
  filtra a lista.
- Lista: NF ou referência, data prevista (chip vermelho se atrasada), itens
  e volumes, status, `#PE` quando houver. Expandir mostra os itens com a
  quantidade anunciada e, em ENDERECADO/DIVERGENCIA, recebida e falta
  (lidas de `wms_recebimento_itens` pelo `wms_recebimento_id`).
- Ações por linha: Cancelar (só ANUNCIADO), baixar o arquivo original.
- Prévia de upload: modal como a de Envios, com data prevista e observação
  por nota e o botão Confirmar.
- Tour (`_tour_portal.html`) ganha o passo da aba.
- Painel interno não ganha tela nova. Em `/painel/wms` a aba Receber passa a
  mostrar a origem "portal" e a data prevista na linha do recebimento.

Rotas em `portal_cliente/app.py`, espelhando as de envios:

```
GET  /api/entradas                       lista (janela de 30 dias + tudo que não está fechado)
POST /api/entradas/analisar
POST /api/entradas/confirmar
POST /api/entradas/<id>/cancelar
GET  /api/entradas/<id>/arquivo
GET  /api/entradas/modelo-planilha
```

## 10. Onde o código mora

| Arquivo | Responsabilidade |
|---|---|
| `portal_cliente/entradas.py` (novo) | tabelas, leitura XML/planilha de entrada, validação, gravação, cancelar, `sincronizar_status`, conciliação. Sem Flask |
| `portal_cliente/envio_pedidos.py` | só ganha `permitir_entrada` em `ler_nfe` e a extração de itens |
| `portal_cliente/enviar_entradas_stokki.py` (novo) | worker da fila: wizard XML múltiplo e wizard Excel, hook no store, descoberta do `#PE` |
| `stokki/recebimentos.py` | `extrair_chave_nfe`, `extrair_ref_pedido`; docstring corrigido (segundo número = nº da NF) |
| `portal_cliente/app.py` | rotas `/api/entradas/*`, `aba_inicial`, flag na tela |
| `portal_cliente/templates/_entradas.html` (novo) | a aba |
| `portal_cliente/templates/acompanhamento.html`, `_tour_portal.html` | inclusão da aba e passo do tour |
| `portal_cliente/gerenciar_clientes.py` | `--entradas` liga/desliga `entradas_ativo` |
| `painel_agentes/wms_pedidos.py` | colunas novas, estado CANCELADO, `registrar_recebimento` aceita `data_prevista` e `portal_entrada_id` |
| `sincronizar_recebimentos_wms.py` | amarração por `stokki_id` ou chave NF-e + `sincronizar_status` |
| `painel_agentes/painel_agentes.py` | rota de divergência chama `sincronizar_status`; listagem filtra CANCELADO |
| `infra/` | `portal-cliente-entradas.service` |

## 11. Testes

`unittest`, sem rede, banco SQLite temporário como em `portal_cliente/test_*.py`:

- `portal_cliente/test_entradas.py`: NF-e de entrada com itens (fixture
  sintética, `tpNF=1` e `tpNF=0` aceitos), as 6 validações da 7.1, planilha
  de entrada com agrupamento e data divergente no grupo, ciclo completo de
  `sincronizar_status`, cancelar em cada estado (só ANUNCIADO passa),
  cancelar com e sem `stokki_id` (chamado só com), chave duplicada com
  anterior CANCELADO (regrava a mesma linha, itens antigos somem),
  destinatário ≠ Fresh Log com e sem a chave no config.
- `test_sincronizar_recebimentos_wms.py`: amarração por `stokki_id`, por
  chave NF-e e por referência; sem par; recebimento CANCELADO não é relido.
- `stokki/test_recebimentos.py`: `extrair_chave_nfe` e `extrair_ref_pedido`
  sobre o HTML real do `#PE-2497` salvo como fixture (com a chave e o CNPJ
  do piloto trocados por valores sintéticos).
- `portal_cliente/test_enviar_entradas_stokki.py`: wizard simulado (XML e
  Excel), 4xx com `errors` vira ERRO com a mensagem, 3 falhas técnicas,
  entrada CANCELADO na fila é pulada, chave já existente na Stokki vira
  CRIADO sem criar, descoberta do `#PE` pela chave.

## 12. Ordem de entrega e prova

Cada etapa é commitada, deployada e provada antes da seguinte.

1. **Sondagem**: feita (8.1).
2. **Núcleo**: `entradas.py` + testes. Prova: testes passando.
3. **Tela**: aba, rotas, prévia, cancelar, flag da Maria Dolores. Prova: uma
   NF de teste anunciada no portal de produção aparece em `portal_entradas`
   como ANUNCIADO / NA_FILA (worker ainda desligado).
4. **Worker da Stokki**: primeiro com `--simular` e `--visivel` contra a
   Stokki real, com um XML de verdade da Maria Dolores; depois `--uma-vez`
   real com **uma** nota combinada com o Hugo. Prova: a entrada vira `#PE`
   na Stokki sem ninguém digitar e `stokki_id` fica preenchido.
5. **Ligação com o WMS**: amarração no timer, `sincronizar_status` nos dois
   gatilhos, CANCELADO. Prova: o `#PE` do passo 4 aparece na aba Receber do
   galpão com a data prevista, e o cliente vê CHEGOU e depois ENDERECADO
   (ou DIVERGENCIA com as faltas).

Enquanto uma entrada real da Maria Dolores não percorrer o ciclo inteiro em
produção, a aba não está pronta.

## 13. Fora de escopo

- Outros embarcadores (D3).
- Cancelar na Stokki automaticamente.
- E-mail ao cliente quando a mercadoria chega (decisão do Hugo depois).
- Filtro por período e exportar xlsx na aba.
- Pedir revisão de divergência pela tela (fica pelo e-mail).
- Amarração manual de entrada ↔ `#PE` pela tela do painel.
- Anexar documentos extras ou etiqueta ao `#PE` (o wizard permite; não
  usamos).
- Lote e validade no anúncio: continuam vindo da caixa física, na mão do
  operador (decisão de 23/09).

## 14. Riscos e armadilhas

- **Sessão única da Stokki**: sondagem e worker só na VPS, sempre dentro
  de `sessao_uso`. Daqui derruba a produção com 401.
- **`dados.db` compartilhado**: commit por entrada; `sincronizar_status`
  faz um UPDATE por entrada, nunca transação longa.
- **Sondar URL por adivinhação derruba a sessão** (8.1): o worker e o
  timer só usam URLs que a sondagem viu no menu. Sondagem nova, se
  precisar, extrai links da página antes de seguir qualquer um.
- **O wizard soma linhas do mesmo SKU** (`sku[]` agregado por `cProd`). O
  `#PE` na Stokki terá menos linhas que `portal_entrada_itens`; o WMS lê
  do `#PE`, então é o agregado que o galpão endereça. A aba do cliente
  mostra as linhas como vieram na nota; a comparação anunciado × recebido
  é feita por SKU, não por linha.
- **Resposta perdida do store**: o `#PE` pode ter sido criado sem o worker
  saber. Por isso a checagem pela chave antes de subir (8.2), e o timer
  amarra pela chave de qualquer jeito (8.3).
- **`origin_id` da planilha**: o wizard Excel pede origem; se a Stokki não
  aceitar sem cadastro, planilha fica ERRO com mensagem e a nota XML
  continua funcionando. Não bloqueia a entrega.
- **`ler_nfe` é compartilhada com Envios**: o parâmetro `permitir_entrada`
  nasce `False`; teste de Envios continua garantindo que `tpNF=0` é
  recusado lá.
- **Timer só lê o piloto**: entrada anunciada por outro embarcador (se
  alguém ligar a flag sem querer) ficaria ANUNCIADO pra sempre.
  `gerenciar_clientes.py` avisa ao ligar `entradas_ativo` pra CNPJ que não
  é o piloto.
- Na VPS, sempre `sudo -u www-data venv/bin/python`.
