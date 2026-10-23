# WMS: estoque proprio com reserva por pedido da Stokki

Data: 21/09/2026
Modulo: Galpao (WMS) — fase 2
Piloto: MARIA DOLORES INDUSTRIA E COMERCIO DE ALIMENTOS LTDA

## 1. Problema

A fase 1 do WMS (04/09/2026) colocou no ar a tela `/painel/wms` com catalogo,
posicoes, operadores e etiqueta de posicao. Em 21/09/2026 a producao mostra:

```
areas: 10   posicoes: 165   produtos: 1816 (1498 ativos, todos com perfil)
wms_movimentos: 0   wms_saldos: 0
```

Dezessete dias no ar e **nenhum movimento**. O motivo: a fase 1 nao fecha
nenhum ciclo util sozinha — enderecar produto sem que isso sirva pra alguma
coisa e trabalho extra pro galpao.

O estoque do galpao hoje nao existe em sistema nenhum. A Stokki tem o modulo
`/administrator/operation/inventory/lot` (colunas Endereco, Entrada, Lote,
Fabricacao, Validade, Produto, Cliente, Estoque, Comprometido, Disponivel),
mas esta **zerado** (`iTotalRecords = 0`, verificado em 21/09 com os mesmos
filtros da tela).

## 2. O que este projeto entrega

Um ciclo fechado de estoque para **um** embarcador: a mercadoria entra com
lote e validade, ganha etiqueta com QR, e cada pedido lancado na Stokki
reserva o que vai consumir. Na expedicao, a reserva vira baixa.

No fim, a pergunta "o que a Maria Dolores tem no galpao, de que lote, com que
validade, e quanto ja esta comprometido" tem resposta.

## 3. Decisoes (Hugo, 21/09/2026)

| # | Decisao | Escolha |
|---|---------|---------|
| D1 | Gatilho da baixa | **Reserva** no lancamento do pedido; fica reservado **ate a expedicao**, quando vira baixa |
| D2 | Entrada de mercadoria | Recebimento puxado da Stokki (`inventory/incoming`) + enderecamento nosso |
| D3 | Lote e validade | Do **fabricante**, lidos no recebimento |
| D4 | Fonte de verdade | **Nosso WMS**. Espelhar na Stokki e etapa 2, fora deste escopo |
| D5 | Etiqueta | Produto + lote + validade, **sem serial** |
| D6 | Escolha do lote | **FEFO sugerido**, operador pode trocar bipando outro |
| D7 | Escopo | Piloto com **Maria Dolores** |
| D8 | Falta de saldo | **Avisa, nunca bloqueia** a operacao |
| D9 | Formato do QR | Formato proprio: `FL\|SKU\|LOTE\|AAAA-MM-DD` |
| D10 | Frequencia da reserva | Timer systemd a cada 15 min |
| D11 | Visibilidade | So a equipe interna, no painel. Cliente nao ve no piloto |

Modulo de expedicao proprio: **decidido que existira, mas em outra sessao.**
Aqui a baixa se apoia no que ja detecta expedicao hoje.

## 4. O que ja foi provado na Stokki (21/09/2026)

Tudo abaixo foi verificado ao vivo pela sessao da VPS, so leitura:

- **Itens do pedido sao legiveis** no HTML de
  `/administrator/inventory/outbound/show/{id}`, na aba "Itens do pedido",
  como `Produto | EAN | Quantidade`. Exemplo real (PS-39751):
  `72400017 - COXINHA FESTA ZC 5KG PCT | 724000170000 | 1` e
  `72400017 - COXINHA FESTA ZC 5KG PCT | 430000100000 | 2` — mesmo SKU, dois
  EANs, porque a linha e por **embalagem**.
- A Stokki tem os status `Separating` e `Pack`, e campos de conferencia
  ("Usuario da Estacao da Conferencia", "Inicio", "Finalizacao") hoje vazios.
- `/administrator/inventory/incoming` responde 200 e lista recebimentos com
  `ID do Pedido | Chegada | Tipo | Movimento | Cliente | Origem | unidade |
  Recebimento | Marcador | Situacao`, com datatable em `incoming/table`.
- Escrever na Stokki e rotina nossa: `expedir_pedidos.py` ja faz
  `POST /provider/operation/shipping/store` com CSRF via Playwright. Nao e
  usado neste escopo, mas confirma que a etapa 2 (espelho) e viavel.

## 5. Modelo de dados

Tres tabelas novas em `dados/dados.db`. **Nenhuma alteracao** em
`wms_saldos` ou `wms_movimentos`.

```sql
CREATE TABLE IF NOT EXISTS wms_pedidos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    id_stokki       INTEGER NOT NULL UNIQUE,
    codigo_ps       TEXT NOT NULL,
    embarcador      TEXT NOT NULL DEFAULT '',
    situacao        TEXT NOT NULL DEFAULT '',
    estado_reserva  TEXT NOT NULL DEFAULT 'PENDENTE',
    -- PENDENTE | RESERVADO | PARCIAL | BAIXADO | CANCELADO
    lido_em         TEXT NOT NULL,
    atualizado_em   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wms_pedido_itens (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id      INTEGER NOT NULL REFERENCES wms_pedidos(id),
    linha          INTEGER NOT NULL,
    sku            TEXT NOT NULL DEFAULT '',
    ean_linha      TEXT NOT NULL DEFAULT '',
    descricao      TEXT NOT NULL DEFAULT '',
    qtd_embalagem  REAL NOT NULL,
    qtd_un         REAL,              -- NULL enquanto nao resolve
    produto_id     INTEGER REFERENCES wms_produtos(id),
    motivo_pendencia TEXT NOT NULL DEFAULT '',
    UNIQUE (pedido_id, linha)
);

CREATE TABLE IF NOT EXISTS wms_reservas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id      INTEGER NOT NULL REFERENCES wms_pedidos(id),
    item_id        INTEGER NOT NULL REFERENCES wms_pedido_itens(id),
    produto_id     INTEGER NOT NULL REFERENCES wms_produtos(id),
    posicao        TEXT NOT NULL,
    lote           TEXT NOT NULL DEFAULT '',
    validade       TEXT NOT NULL DEFAULT '',
    quantidade_un  REAL NOT NULL,
    estado         TEXT NOT NULL DEFAULT 'ATIVA',   -- ATIVA | CONSUMIDA | CANCELADA
    origem         TEXT NOT NULL DEFAULT 'FEFO',    -- FEFO | MANUAL
    movimento_uuid TEXT NOT NULL DEFAULT '',        -- preenchido na baixa
    criado_em      TEXT NOT NULL,
    atualizado_em  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wms_reservas_saldo
    ON wms_reservas(produto_id, posicao, lote, validade, estado);
```

**Invariante central:** reserva nao altera saldo fisico. `wms_saldos` continua
sendo exatamente a soma de `wms_movimentos`, e `recalcular_saldos()` continua
valendo sem mudanca. O disponivel e derivado:

```
disponivel(posicao, produto, lote, validade)
    = wms_saldos.quantidade - SUM(wms_reservas.quantidade_un WHERE estado='ATIVA')
```

## 6. Resolucao do item e conversao de unidade

Este e o ponto mais perigoso do projeto. O pedido da Stokki traz quantidade em
**embalagem**; `wms_saldos` guarda **UN**.

Regra, nesta ordem:

1. `ean_linha` bate com o **DUN** do produto (`wms_produtos.dun`)
   → `qtd_un = qtd_embalagem * qtd_por_caixa`.
2. `ean_linha` bate com o **EAN unitario** → `qtd_un = qtd_embalagem`.
3. `ean_linha` nao bate com nenhum, mas o **SKU** identifica um unico produto
   ativo e esse produto tem `qtd_por_caixa` igual a 1 → `qtd_un = qtd_embalagem`.
4. Qualquer outro caso → `qtd_un = NULL`, `motivo_pendencia` preenchido, e o
   item **nao e reservado**.

Um item nao resolvido nunca vira reserva e nunca vira baixa. Aparece na fila de
pendencias. Estoque que mente e pior que estoque nenhum.

## 7. Os cinco fluxos

### 7.1 Entrada (recebimento)

`sincronizar_recebimentos_wms.py` le `inventory/incoming` filtrando o
embarcador do piloto e cria a expectativa de entrada. Na tela do celular o
operador abre o recebimento, bipa o produto, digita **lote**, **validade** e
quantidade, escolhe a posicao, e a gravacao chama o
`registrar_movimento(tipo="ENTRADA", ...)` que ja existe — com o `uuid` da fila
offline, como a fase 1 ja faz.

Validade continua obrigatoria conforme `controla_validade` do produto (regra da
fase 1, inalterada).

### 7.2 Etiqueta com QR

Novo `painel_agentes/wms_etiqueta_produto.py`, espelhando o
`desenhar_etiqueta()` de posicao que ja esta calibrado (Elgin L42 Pro Full,
midia deitada 10x5 cm, config `wms.etiqueta_*` da VPS).

Conteudo do QR: `FL|<SKU>|<LOTE>|<AAAA-MM-DD>`. Validade vazia vira campo
vazio (`FL|72400017|L2291|`). Em texto legivel na etiqueta: descricao, SKU,
lote e validade em fonte grande.

`ler_codigo()` do `wms.py` ganha o reconhecimento desse prefixo e devolve
produto + lote + validade ja preenchidos.

### 7.3 Reserva

`sincronizar_pedidos_wms.py`, timer a cada 15 min:

1. Lista os pedidos do embarcador piloto na Stokki (status em aberto).
2. Para cada pedido novo ou alterado, le o detalhe, extrai os itens com
   `extrair_itens_do_pedido()` e grava `wms_pedidos` + `wms_pedido_itens`.
3. Resolve produto e unidade (secao 6).
4. Aloca **FEFO** sobre o disponivel: ordena por validade crescente, depois por
   entrada mais antiga, e consome ate cobrir `qtd_un`. Pode gerar varias
   reservas por item (lotes diferentes).
5. Faltou saldo → reserva o que da, marca o pedido como `PARCIAL` e registra a
   falta na fila de pendencias. **Nunca recusa nada** (D8).
6. Pedido cancelado ou sumido da Stokki → reservas viram `CANCELADA`.

### 7.4 Separacao

A tela do galpao ganha a lista de separacao por pedido, ja com posicao, lote e
validade sugeridos. O operador bipa o que realmente pegou:

- bipou o lote sugerido → confirma;
- bipou outro lote → a reserva daquele item e refeita pro lote bipado
  (`origem = MANUAL`), desde que haja saldo; senao, avisa e segue.

### 7.5 Baixa na expedicao

Quando o pedido e expedido, cada reserva `ATIVA` vira
`registrar_movimento(tipo="SAIDA", ...)` com **uuid deterministico**
`ps-<id_stokki>-item-<linha>-<n>`, gravado em `wms_reservas.movimento_uuid`.
Como `registrar_movimento` ja e idempotente por uuid, rodar duas vezes nao
baixa duas vezes. A reserva passa a `CONSUMIDA` e o pedido a `BAIXADO`.

O gatilho e a deteccao de expedicao que ja existe hoje; quando o modulo de
expedicao proprio nascer (outra sessao), ele chama a mesma funcao.

## 8. Onde o codigo mora

`painel_agentes/wms.py` ja tem ~1.600 linhas e nao deve crescer mais.

| Arquivo | Responsabilidade |
|---------|------------------|
| `painel_agentes/wms_pedidos.py` (novo) | espelho do pedido, resolucao de item, FEFO, reserva, baixa. Sem Flask, como o `wms.py` |
| `painel_agentes/wms_etiqueta_produto.py` (novo) | etiqueta de produto com QR |
| `stokki/pedidos.py` | so ganha `extrair_itens_do_pedido(html)` |
| `stokki/recebimentos.py` (novo) | leitura de `inventory/incoming` |
| `sincronizar_pedidos_wms.py` (novo) | rotina de lote da reserva + timer |
| `sincronizar_recebimentos_wms.py` (novo) | rotina de lote do recebimento |
| `painel_agentes.py` | rotas `/api/wms/*` novas |
| `painel_agentes/templates/wms.html` | telas de recebimento, separacao e etiqueta |
| `infra/` | `.service` + `.timer` das duas rotinas |

Tela de saldo e reservas no painel interno, visivel a equipe (D11).

## 9. Testes

`unittest`, sem rede:

- `painel_agentes/test_wms_pedidos.py` — resolucao de item (os quatro casos da
  secao 6), FEFO com multiplos lotes, reserva parcial, cancelamento,
  idempotencia da baixa.
- `stokki/test_extrair_itens.py` — parser sobre HTML real do PS-39751 salvo
  como fixture, incluindo o caso do mesmo SKU em duas linhas.
- `painel_agentes/test_wms_etiqueta_produto.py` — conteudo do QR e leitura de
  volta por `ler_codigo()`.

Toda rotina de lote aceita `--modo-teste` (nao grava), como as demais do
projeto.

## 10. Fora de escopo

- Espelhar o estoque na Stokki (etapa 2, D4).
- Modulo de expedicao proprio (outra sessao, decisao do Hugo).
- Inventario / contagem cega.
- Demais embarcadores.
- Cliente ver o proprio estoque no portal (D11).
- Pallets por container e posicoes das camaras — entram so se a Maria Dolores
  ocupar camara.

## 11. Riscos e armadilhas

- **`dados.db` e compartilhado.** Transacao longa derruba o painel com
  "database is locked" (aconteceu em 04/09). A reserva grava por pedido, com
  commit por pedido, nunca num lote so.
- **Sessao da Stokki e unica.** Login concorrente derruba a outra sessao com
  401 — inclusive a do `agente-importacao-stokki`. O timer de 15 min usa a
  trava cooperativa de `stokki/sessao_uso.py`.
- **Quantidade em embalagem, nao em UN** (secao 6). Se a regra errar, o
  estoque erra silenciosamente. Por isso item nao resolvido nao reserva.
- **Um mesmo SKU aparece em varias linhas do pedido** com EANs diferentes.
  Reservar por linha, nunca agrupando por SKU.
- **Saldo comeca zerado.** Ate a Maria Dolores estar enderecada, todo pedido
  dela sera `PARCIAL`. Isso e esperado, nao e bug — e some conforme o galpao
  enderecar.
- Na VPS, rodar sempre `sudo -u www-data venv/bin/python`.

## 12. Como sabemos que funcionou

Um pedido real da Maria Dolores, lancado na Stokki, que:

1. apareca em `wms_pedidos` com os itens resolvidos;
2. gere reservas FEFO nas posicoes certas;
3. seja separado pelo operador no celular;
4. na expedicao, gere `SAIDA` em `wms_movimentos` e derrube o saldo.

Enquanto isso nao acontecer com um pedido de verdade, a fase 2 nao esta pronta
— foi exatamente o que faltou na fase 1.
