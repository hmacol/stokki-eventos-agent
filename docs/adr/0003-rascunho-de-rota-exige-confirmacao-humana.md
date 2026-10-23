# ADR-0003: Rascunho de rota só vira rota com confirmação humana

## Status
Aceito

## Data
2026-08-14 (rede de segurança cancelada); reforçada em 2026-09-09 (sequência das 22h deixou de roteirizar)

## Contexto
Às 18h `roteirizacao/criar_rotas_diarias.py --gerar-rascunho` grava RASCUNHOS (`rascunhos_rota`, `rascunhos_parada`). O operador revisa em `/planejamento` e clica "Confirmar e Enviar", que cria a rota na Vuupt (`rotas_client.criar_rota`) e registra no núcleo.

Antes existia uma rede automática: a sequência das 22h roteirizava de novo, `enviar_rascunhos_pendentes.py` mandava rascunho esquecido e `incrementar_rotas.py` encaixava pedido novo sozinho. Isso gerou rotas duplicadas e rotas que ninguém revisou.

Decisão do Hugo em 14/08/2026, reforçada em 09/09/2026.

## Decisão
- Nenhum processo automático envia rascunho nem cria rota sem clique humano. A sequência das 22h **não roteiriza**.
- `enviar_rascunhos_pendentes.py` não é chamado por ninguém (sem timer). `incrementar_rotas.py` é só botão do painel.
- Rascunho não confirmado até as 22h = pedido perde o dia. `incrementar_rotas.py` falha explícito (e-mail de erro) em vez de criar rota duplicada.
- O job automático das 18h roda sempre no esquema Automático; o botão "Roteirizar" deixa o operador escolher o esquema (risco aceito pelo Hugo).

## Alternativas consideradas

### Enviar rascunho esquecido automaticamente às 22h
- Rejeitada: foi o que existia; produziu rota duplicada e rota não revisada.

### Encaixar pedido novo sozinho nas rotas (incrementar automático)
- Rejeitada em 09/09. Reaberta parcialmente na spec de 24/09 (incrementar em timer de 30/30 das 14h30 às 19h30), ainda **não implementada**; se for ao ar, substitui parte deste ADR.

## Consequências
- A operação tem obrigação diária: confirmar o rascunho em `/planejamento` até as 22h.
- Qualquer automação nova que crie ou altere rota precisa de decisão explícita do Hugo e de ADR novo.
- O vigia e a Torre tratam "rascunho não confirmado" como exceção, não como estado normal.
