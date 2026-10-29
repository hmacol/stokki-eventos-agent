# Checklist da Vuupt preenchido pelas telas do painel (canhoto e nota de retorno)

Data: 29/10/2026 · Pedido do Hugo · Status: design para aprovação

## 1. Intenção

Quando a equipe fecha um pedido como entregue pelo painel (sem o app do motorista), o checklist da Vuupt "Checklist de Entregas com Sucesso" passa a ser preenchido com as fotos que a equipe enviar na tela — do mesmo jeito que o app do motorista faz. Hoje esses pedidos ficam `done` **sem checklist**.

Por quê: o canhoto tem que estar no checklist da Vuupt, que é onde a operação, o financeiro e quem mais olha a Vuupt procuram o comprovante (Hugo: "No check list"). Guardar só do nosso lado não basta.

## 2. Descoberta técnica (provada em 29/10 na conta real)

A API pública da Vuupt não documenta gravação de checklist, mas o app do agente (com.vuupt.vuuptdriver 7.2.3) usa endpoints da mesma API que aceitam o nosso token (usuário 28438). Sequência provada no serviço de teste 54548287 (checklist 37555287):

1. ações do agente até `arrived` (o `concluir_como_agente` de hoje: accept/start/check-in);
2. `POST /checklists` (form): `checklist_form_id=1531`, `checklist_rule_id=1526`, `entity_id=<service_id>`, `trigger=finished_service`, `latitude`, `longitude`, `gps_date_time`, `filled_at`, **`images_quantity` = número exato de fotos** (fixado na criação; PUT não altera) → `{"checklist_answer": {"id": ...}}`;
3. `POST /checklists/{id}` (multipart), um campo por request ou vários: `checklist_form_id`, `fields[N][id]` (15086 Foto do Canhoto Assinado — obrigatório no app; 15349 Foto Nota de Retorno), `fields[N][content_uuid]` (uuid4 nosso), `fields[N][file_source]=gallery` e **o arquivo na parte `fields[N][value]`** (outros nomes de parte são ignorados em silêncio, com 200);
4. `PUT /checklists/{id}/finish` (POST dá 405; 409 se a contagem de fotos não bate);
5. `PUT /services/{id}/check-out` `accomplished=true`.
O checklist aparece no `GET /checklists?include=fields` com `file_url` (a Vuupt recomprime o JPEG). Detalhes: memória `reference_vuupt_api_publica_stoplight`.

**Risco aceito:** endpoints não documentados; a Vuupt pode mudar sem aviso. Por isso a gravação confere o resultado (seção 4.4) e falha alto.

## 3. Decisões do Hugo

- **Onde:** em todas as telas que concluem entrega pelo painel: `/baixa-sem-app` (motorista sem app), "Marcar como entregue" da Torre e do Planejamento, inclusive **em massa**.
- **Duas fotos:** canhoto (campo 15086) e nota de retorno (campo 15349), ambas pela tela.
- **Em massa:** a mesma foto (ou as mesmas duas) vai para todos os pedidos selecionados.
- **Sem foto do canhoto:** só conclui com **justificativa de uma lista fixa** (para análise):
  1. Canhoto chega depois (motorista vai trazer)
  2. Cliente não assinou / recusou assinar
  3. Entrega confirmada por telefone ou WhatsApp com o cliente
  4. Motorista sem celular ou sem bateria na hora da entrega
  5. Foto do canhoto perdida ou ilegível
  6. Entrega sem canhoto por acordo com o embarcador
  7. Outro
- Teste na conta real (feito); não precisa de credencial de agente: o token atual serve.

## 4. O que muda

### 4.1 Tela (as três)
- Pedido marcado "Entregue" mostra: **Foto do canhoto** (`image/*`, `capture` no celular) e **Foto da nota de retorno** (opcional).
- Sem foto do canhoto, aparece a **lista de justificativas** (obrigatória para confirmar); "Outro" pede uma frase curta digitada (decisão minha).
- No "Marcar como entregue" da Torre/Planejamento, o campo de texto livre de hoje ("como a entrega foi confirmada", obrigatório) é **substituído** por foto do canhoto **ou** justificativa da lista (decisão minha: a lista cobre os mesmos casos e é analisável; o texto livre fica só no "Outro").
- Em massa (Planejamento): os campos aparecem uma vez na barra de seleção e valem para todos os selecionados.
- Pedido marcado "voltou"/insucesso (`/baixa-sem-app`) não tem fotos nem checklist (o checklist "com sucesso" é de entrega feita).

### 4.2 Fechamento na Vuupt
`concluir_como_agente` ganha o preenchimento opcional do checklist entre o check-in e o check-out (ordem do app e do teste). Sem foto (com justificativa), fecha como hoje, sem checklist.
- Valores fixos: formulário 1531, regra 1526, gatilho `finished_service`, `file_source=gallery`, `images_quantity` = nº de fotos enviadas; latitude/longitude = coordenadas do endereço do serviço, e sem coordenada as da base (R. Zilda 288) (decisão minha; o app manda o GPS do celular); horários em UTC como a Vuupt guarda.
- O 1531 vale para toda **entrega com sucesso** (conferido em 800 checklists de 20 a 29/10: 589 no 1531; os outros são o 1565 "Foto da fachada" de insucesso e o 1532 de coleta Quatro Estrelas). Serviço que não seja `type=delivery` não recebe checklist e a tela avisa (decisão minha).
- Imagens: JPEG/PNG/HEIC (HEIC convertido, como já faz `canhotos_manuais`), limite 15 MB como hoje; PDF não vai pro checklist (o campo é foto) — se vier PDF, ele vai só pra cópia local e o checklist não é preenchido, com aviso na tela (decisão minha).

### 4.3 Cópia local
A foto do canhoto continua indo para `nucleo/canhotos_manuais` (PDF em `dados/canhotos_manuais`, GCS, tabela) **antes** do fechamento na Vuupt, como a `/baixa-sem-app` já faz — agora nas três telas. Se o checklist ou o check-out falham, a cópia local fica num pedido não concluído, o que é inofensivo: expedição e batimento só olham pedido `done`, e a nova tentativa substitui o arquivo (decisão minha, mantém o fluxo que já existe). Expedição e batimento seguem iguais (a expedição já prefere o checklist da Vuupt e cai pro manual). A nota de retorno vai só para a Vuupt (decisão minha).

### 4.4 Falhas
- Se o checklist não for gravado (erro, ou a releitura mostra a foto sem `file_url`), o pedido **não é concluído** e a tela mostra o erro daquele pedido; dá pra tentar de novo (decisão minha: o objetivo é o canhoto no checklist; concluir sem ele repetiria o problema de hoje).
- Repetir não cria checklist duplicado: se já existe checklist `finished_service` desse serviço, reaproveita/completa (o builder confere como identificar).
- Em massa, falha de um pedido não para os outros (como hoje).
- Pedido que já estava `done` quando a tela confirmou (concluído no app ou por outra pessoa): não mexe na Vuupt nem no checklist, guarda a cópia local do canhoto e a tela diz "já estava concluído; a foto não foi para o checklist" (decisão minha; anexar depois está fora do escopo).
- Retentativa acha o checklist pelo serviço (`entity_id`) mesmo que o status tenha andado (ex.: checklist criado, `finish` falhou).

### 4.5 Registro
Justificativa (código da lista + texto do "Outro"), quem, quando e se o checklist foi gravado (id do checklist) ficam **por pedido** na auditoria que já existe: `torre_entregas_manuais` (colunas novas, criadas pelo próprio código como as outras tabelas do painel) e `baixas_sem_app` (dentro do JSON dos itens). Evento em `tratativas` como hoje. Em massa, a mesma justificativa vale para todos e é gravada em cada um. "Outro": texto de 5 a 200 caracteres (decisão minha).

### 4.6 Contrato da API
`/api/torre/marcar-entregue` passa a receber multipart (fotos + justificativa) no lugar do JSON com `observacao`; só a Torre e o Planejamento chamam, então o contrato antigo sai e os testes são reescritos (decisão minha).

## 5. Fora do escopo / não pode acontecer
- **Fora:** anexar a foto **depois** num pedido já concluído (motivo "canhoto chega depois"); validação automática de nitidez das fotos; checklist de insucesso; mudar o app do motorista; retiradas e Lalamove (usam `concluir_como_agente` e continuam sem checklist).
- **Fracasso mesmo funcionando:** pedido concluído sem canhoto e sem justificativa; foto gravada no pedido errado (em massa); checklist duplicado no mesmo serviço; a tela dizer "ok" com a foto não aparecendo no checklist.

## 6. Consequências aceitas
- O checklist fica no nome do usuário da API (Hugo Maçol Sousa, 28438), não do motorista da rota.
- Endpoints não documentados (seção 2).

## 7. Pendente do teste de 29/10
O serviço de teste `PS-TESTE-2910-0534` (54548287, rota 5402411) ficou `done` com checklist 37555287 sem `finish` (2 fotos para images_quantity 1). Serviço concluído não cancela; fica como o teste de agosto.

## 8. Prova (Hugo autorizou 29/10)
Depois de implementar, provar na conta real com serviços de teste novos do cliente "usuario teste" (14425519) em rota do agente 28438 (script `testar_fluxo_agente_vuupt.py criar`), uns 4–5 casos: um pedido com canhoto + nota de retorno, em massa (mesma foto em 2+ pedidos), sem foto com justificativa, e repetição depois de falha. Eles ficam `done` para sempre (não cancelam). Conferir no `GET /checklists?include=fields` e no painel da Vuupt.

## 9. O que fica com o builder
- Forma exata de reaproveitar checklist existente (4.4).
- Como a tela do Planejamento em massa manda a foto (uma vez e o servidor replica, ou por pedido).
- Textos e layout dos campos, seguindo as telas atuais.
