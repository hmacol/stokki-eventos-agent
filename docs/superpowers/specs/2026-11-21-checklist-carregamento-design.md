# Checklist de carregamento no app dos motoristas — design

Data: 21/11/2026 · Pedido do Hugo · Caminho escolhido: **A** (etapa obrigatória no app entre "Aceitar" e "Iniciar rota").

## Por quê

Duas coisas, nas palavras do Hugo: **controle de lotação dos carros** e **confirmação de carregamento**.
Hoje a rota sai do galpão sem registro de quem carregou, do que foi pro carro e de quanto do baú
foi usado. Pedido que "não entrou" só aparece como problema na rua.

## O que o Hugo pediu (decidido)

1. **Nome do carregador.**
2. **Foto do carro carregado**, avaliada pelo **Haiku**; a foto precisa mostrar **o baú completo**.
3. **Pedidos não carregados**: o motorista **desmarca** o pedido, e isso funciona como
   **"Tirar da rota" antes do início**. Pedido desmarcado **volta pro pool pra ser verificado**.
4. Tudo no app dos motoristas, antes de "Iniciar rota" (caminho A).

## Metas, não-metas, anti-metas

- Meta: nenhuma rota do app começa sem carregador, foto aprovada (ou liberada pra revisão) e lista confirmada.
- Meta: dado de lotação por rota/veículo guardado e consultável depois.
- Meta: pedido desmarcado sai da rota de verdade (Vuupt e núcleo) e reaparece no pool marcado.
- Não-meta: tela/relatório de lotação no painel nesta entrega (o dado fica gravado; tela vem depois).
- Não-meta: rotas feitas no app da Vuupt (não passam pelo nosso app, não veem a etapa).
- Não-meta: checklist pelo galpão/painel (caminhos B/C descartados).
- **Anti-meta:** prender o motorista no galpão. Foto reprovada trava, mas depois das tentativas libera com
  revisão humana (mesma regra do canhoto, `validacao_fotos.aplicar_tentativas`). Haiku fora do ar /
  NAO_VERIFICADO **nunca** trava.
- **Anti-meta:** cancelar pedido. Desmarcar **nunca** cancela: só tira da rota.

## Abordagens consideradas

- **A. Etapa obrigatória no app (ESCOLHIDA).** Motorista preenche, servidor tira os desmarcados e só então
  libera "Iniciar rota". Prova na hora, sem aparelho novo; depende da honestidade do motorista.
- B. Galpão preenche pelo painel; app só mostra. Quem carrega confirma, mas exige aparelho e passo da equipe.
- C. Como A, mas desmarcar só avisa e a logística tira à mão. Nada sai sozinho, mas o pedido fica "fantasma".

## Como funciona hoje (pontos de apoio)

- Fluxo da rota no app: `app_motorista/app/rota/[id].tsx` — status `ACEITA` + `editavel` mostra o
  deslizar "Iniciar rota" → `POST /api/rotas/<id>/iniciar` (`nucleo/api_motorista.py`) →
  `operacao.iniciar_rota` (`nucleo/operacao.py`).
- Rotas que o app opera: `provedor = APP` (só núcleo) **ou** rota da Vuupt operada pela ponte
  (`ponte_vuupt.rota_operavel`). Ver `operacao.rota_editavel`.
- Validação de foto com IA: `nucleo/validacao_fotos.py` + `POST /api/validar-foto` (multipart com `tipo`,
  `rota_id`, `tentativa`). Já tem cache por sha256, tentativas, `pode_seguir`, `revisao_humana`.
  Ligado pra todos desde 21/11 com Haiku 5.5.
- Tirar pedido de rota enviada não iniciada: `painel_agentes/rascunhos_rota.py::remover_de_rota_enviada`
  → `edicao_rota.tirar_pedido` (Vuupt DELETE) + `aplicar_efeitos_pos_remocao` (card, auditoria
  `expedicao.registrar_exclusao`, `nucleo.pedidos.marcar_aberto_por_service_ids` = volta ao pool).
  Motivos válidos: `expedicao.MOTIVOS_EXCLUSAO` (`"Não Encontrado"`, `"Não coube"`, ...).
  Último pedido de rota não iniciada → rota cancelada e rascunho volta a RASCUNHO.
- Fila offline do app: `app_motorista/src/fila.ts` (idempotente por uuid).

## Desenho

### No app (tela "Carregamento")

Aparece quando a rota está `ACEITA` e editável, no lugar do deslizar "Iniciar rota" até o checklist ser enviado.

```
┌ Carregamento ─────────────────────────┐
│ Quem carregou?  [ Jonas         ▾ ]   │  texto livre, sugere os últimos nomes
│                                       │
│ Foto do baú     [ 📷 Tirar foto ]     │  Haiku confere; mostra veredito
│   ✓ Baú inteiro · ocupação ~70%       │
│                                       │
│ Pedidos (12)                          │
│ ☑ PS-44510  Padaria X                 │
│ ☐ PS-44511  Mercado Y   [Não coube ▾] │  desmarcar pede o motivo
│ ☑ ...                                 │
│                                       │
│ [ Confirmar carregamento ]            │
└───────────────────────────────────────┘
        → depois: deslizar "Iniciar rota"
```

- Desmarcar pede motivo (subconjunto de `MOTIVOS_EXCLUSAO`: **Não coube**, **Não Encontrado**, **Outros**;
  "Outros" pede texto). *Chamada minha.*
- Desmarcar todos: aviso forte ("a rota vai ser cancelada") e confirmação. *Chamada minha.*
- Confirmar exige rede (tirar pedido da Vuupt é síncrono); sem rede ou com a Vuupt recusando, mensagem clara
  com o pedido que falhou e botão de tentar de novo (ver Decisões do Hugo, item 1). *Chamada minha:*
  a foto segue o padrão da fila.
- Desmarcar vale pela parada inteira: o card mostra os pedidos juntos (Decisões do Hugo, item 2).

### Foto e Haiku

- Novo `tipo = CARREGAMENTO` em `validacao_fotos` com `rota_id`.
- O modelo devolve: baú inteiro visível (sim/não), é foto de baú/carga (sim/não), **ocupação estimada em %**
  (faixa 0–100, arredondada de 10 em 10) e um motivo curto. *Ocupação em % é chamada minha: é o "controle de lotação".*
- Reprova: não é baú/carga, ou baú cortado/porta fechada/escuro. Mesma regra de tentativas do canhoto,
  **sem** a exceção `exige_outra_foto` da fachada: depois das tentativas sempre libera (erro do Haiku não
  pode prender o motorista no galpão). *Chamada minha.*
- A foto é guardada (mesmo esquema de `_guardar_foto`, pasta da rota).

### No servidor

- Endpoint novo `POST /api/rotas/<id>/carregamento` com `{carregador, foto (ref/sha256), desmarcados:[{parada_id, motivo, observacao}], uuid}`.
  Idempotente por uuid.
- Para cada desmarcado: tira da rota.
  - Rota Vuupt (ponte): mesmo caminho de `remover_de_rota_enviada` (Vuupt + efeitos + auditoria), motivo
    vindo do app, usuário `app:<motorista>`.
  - Rota `provedor = APP`: hoje só existe como réplica de teste (`eh_rota_replica`); tira só a parada
    local e **não** mexe no pedido real. *Chamada minha.*
- Grava tabela nova `nucleo_carregamentos` (rota_id, agent_id, veiculo/placa, tipo_veiculo, carregador,
  foto, veredito, ocupacao_pct, revisao_humana, qtd_carregados, qtd_desmarcados, criado_em).
- `operacao.iniciar_rota` passa a recusar (409 "Faça o checklist de carregamento") se a chave estiver
  ligada e não houver carregamento da rota.

### Pool: "Não carregado — verificar"

- Pedido desmarcado volta ao pool com selo **"Não carregado: <motivo>"** (mesmo jeito do selo
  "Em separação na Stokki"), até entrar em outra rota. *Chamada minha: selo sim, aviso no WhatsApp não.*

### Chave

- `checklist_carregamento.ativo` (config), **desligada por padrão**. Desligada: app e API seguem exatamente
  como hoje. Ligar é decisão do Hugo. Opcional `pilotos: [agent_id]` para começar com poucos.

## Decisões do Hugo (21/11, resposta ao revisor)

1. **Falha da Vuupt ao tirar pedido: trava até sair.** O app mostra qual pedido não saiu e deixa tentar de novo;
   o carregamento só é gravado e o "Iniciar rota" só libera quando todos os desmarcados saíram. Falha parcial:
   os que já saíram ficam fora (idempotente), a nova tentativa só repete os que faltam. Sem fila de remoção
   e sem "fantasma".
2. **Parada com vários pedidos: desmarcar vale pra parada inteira** (todos os pedidos dela voltam ao pool).
   Não divide serviço na Vuupt.
3. **Foto liberada pra revisão: só grava** (`revisao_humana` na `nucleo_carregamentos`). Sem lista no painel
   nesta entrega; fica junto da tela de lotação, que vem depois.

## Fica a cargo de quem construir

- Onde encaixar o selo no pool e a consulta que o alimenta.
- Como chamar o caminho de remoção da Vuupt a partir do `motorista-api` (o código está em `painel_agentes/`;
  importar ou extrair pra `nucleo/`), sem quebrar o painel.
- Texto exato das mensagens do app e do prompt do Haiku.
- Publicação: mudança só de JS → OTA; se precisar de módulo nativo novo, build novo.
- Testes: unittest em `nucleo/` (validação CARREGAMENTO, endpoint, trava do iniciar) e no
  `painel_agentes/` se mexer na remoção.
