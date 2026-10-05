# Agente Analista de Logística

Especificação de 04/10/2026, a partir do questionário respondido pelo Hugo
nesse dia. Registra o que o sistema já faz, as decisões tomadas e o que o
agente precisa fazer a mais.

Status: **especificado, não implementado**. Depende do batimento de
pedidos (DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md) e do calendário de
feriados, nessa ordem.

## O que o agente é (e o que não é)

O agente acompanha cada pedido aberto, percebe quando ele parou ou falhou,
fala com o embarcador e com o motorista e sugere a ação para uma pessoa
aprovar. Ele é construído **em cima** do que já existe: vigia (`vigia/`),
Torre de Controle, reentrega automática (`expedir_pedidos.py`), triagem com
Claude no portal (`portal_cliente/assistente.py`) e no app
(`nucleo/assistente_motorista.py`). Não é um sistema novo ao lado deles.

Três cuidados de leitura:

1. A Fresh Log é operador logístico. "Cliente" tem dois sentidos: o
   **embarcador** (paga, tem os pedidos na Stokki) e o **destinatário**
   (restaurante, mercado, atacado). **O agente fala só com o embarcador.**
2. Não existe prazo de "7 a 15 dias fora de SP". Fora da Grande SP o pedido
   vai por redespacho e o nosso controle termina na transportadora.
3. Tudo que o agente escreve na Stokki ou na Vuupt passa por aprovação de
   uma pessoa. Ele sugere; não executa sozinho.

## Decisões do Hugo (04/10)

| Pergunta | Decisão |
|---|---|
| Com quem o agente fala | Só com o embarcador, nunca com o destinatário. |
| Canal de aviso | OpenWA (número do Hugo), sempre pedindo que a conversa continue no Sistema de Atendimento da Fresh, com o número/link da central na mensagem. O aviso não é canal de conversa. |
| Canal com o motorista | App próprio (`app_motorista/`). Rota na Vuupt não tem canal do sistema com o motorista; o fluxo de entrega só funciona depois da migração para o app. |
| Mensagens | Templates fixos. A IA só faz triagem e leitura de resposta; nunca promete ação operacional. |
| Problema na entrega | Embarcador tem **15 minutos** para responder o chamado, dentro ou fora do horário de atendimento (fluxo abaixo). |
| Limite de tentativas | 3 (original, -R1, -R2), igual ao teto atual da reentrega automática. |
| Devolução | O embarcador confirma. Com a confirmação, o agente sugere o cancelamento na Stokki e uma pessoa aprova. |
| Fora de SP | Carga para a transportadora o mais rápido possível, respeitando o dia fixo da região. Depois do comprovante assinado + Stokki expedido, a Fresh não faz mais nada. |
| Divergências do batimento | O agente sugere a ação com a evidência; uma pessoa aprova antes de qualquer escrita. |
| Feriados | Calendário entra e vale para tudo (vigia, batimento, rotas fracas, prazos do embarcador, agente). Feriado é dia não útil para todos. Não há rota em feriado, salvo dedicado. |
| Serviço com mais de um pedido | Não existe mais; não tratar. |
| KPIs | Fonte passa a ser `batimento_pedidos`, não `vigia_historico`. |

## O que já existe e o agente reaproveita

- **Captura de eventos:** consulta periódica, sem webhook. Vigia a cada
  15 min, expedição/reentrega a cada 30 min (08h-19h30), importação de hora
  em hora (seg-sex), aviso de entrega a cada 5 min.
- **Prazos do vigia** (`vigia/regras.py`, decididos em 28/09): sem serviço
  4h; no pool 1 dia útil; aguardando embarcador 2 dias úteis; rascunho não
  enviado 19h da véspera; rascunho com erro e rota de dia anterior na hora;
  insucesso sem reentrega 1h após a 1ª rodada da expedição passadas 12h;
  recusado 2 dias úteis. O que vence vira exceção na Torre e aparece em
  `/vigia`.
- **Prazo de entrega:** 3 dias úteis a partir do `created_at` na Vuupt
  (`roteirizacao/rotas_fracas.prazo_final`). Hoje só decide se a rota fraca
  pode segurar o pedido; ninguém mede entrega no prazo.
- **Insucesso:** pergunta ao embarcador (`insucesso_entrega/`); sem resposta
  em 12h, cria a reentrega sozinho (`expedir_pedidos.reentregar_insucessos_sem_resposta`),
  exceto avaria, validade, manutenção, não coletado, não reconheceu e problema
  fiscal, e no máximo até a -R2.
- **Agendamento:** só por e-mail ao embarcador; o Claude lê a resposta
  (`ler_respostas_agendamento.py`) e a data é aplicada às 18h e 22h.
- **Alertas internos:** Torre, `/vigia`, e-mail de falha de job, grupo de
  WhatsApp interno (OpenWA, teto 20/dia, desligado por padrão), grupo ALERTAS
  ATENDIMENTO.
- **Indicadores:** `relatorio_operacional.py` (volume, entregues, insucessos,
  taxa de sucesso, tendência 7 dias), KPIs da Torre, resumo diário 20h,
  quinzenal de dedicados.

## O que o agente faz a mais

### 1. Problema na entrega (regra dos 15 minutos)

Vale com o motorista no destino, antes de registrar insucesso. Exige rota no
app próprio.

1. O motorista informa o problema pelo app e o agente abre um chamado para o
   embarcador.
2. O embarcador tem 15 minutos para responder. O relógio corre igual fora do
   horário de atendimento.
3. Respondeu a tempo: o agente repassa a orientação ao motorista.
4. Passou de 15 minutos: o agente manda o motorista seguir a rota e avisa o
   embarcador que ele seguiu.
5. Se o embarcador responder depois pedindo a entrega, o agente pede ao
   motorista que volte e o motorista confirma se consegue.
   - Consegue: o agente avisa o embarcador.
   - Não consegue: o agente informa que na rota daquele dia não foi possível
     voltar e que o pedido vai para o próximo dia de rota da região, já com a
     data quando a regra de dia fixo (`roteirizacao/regioes_dia_fixo.py`)
     permitir.
6. Se o motorista registrar insucesso, o agente informa o embarcador da falha.
   Daí em diante vale o fluxo de insucesso que já existe.

Chamado sem resposta em 15 minutos **não** vai para uma pessoa: o agente
libera o motorista sozinho.

### 2. Sugestões sobre divergências do batimento

Nenhuma ação é automática. O agente monta a sugestão com a evidência e uma
pessoa aprova (Torre).

| Divergência | Ação que o agente sugere |
|---|---|
| Entregue na Vuupt/app e não expedido na Stokki | Expedir na Stokki, anexando o canhoto |
| Expedido sem canhoto assinado | Cobrar o canhoto do motorista |
| Redespacho sem comprovante da transportadora | Cobrar o comprovante da transportadora |
| Expedido na Stokki sem entrega registrada | Conferir com a operação se a entrega aconteceu |
| Cancelado na Vuupt com pedido aberto na Stokki | Recriar o serviço ou cancelar na Stokki, conforme o embarcador |
| 3ª tentativa falhou ou embarcador recusou o reenvio | Pedir confirmação de devolução ao embarcador; com ela, sugerir o cancelamento na Stokki |

### 3. Avisos ao embarcador pelo WhatsApp

Saem pelo OpenWA (`integracao_openwa.py`, seção `whatsapp_notificacoes.clientes`
do config). Toda mensagem termina pedindo que a conversa continue no Sistema
de Atendimento, com o link. Enquanto `forcar_destino` tiver o número do Hugo,
nenhum cliente recebe.

Riscos aceitos: gateway não oficial no número pessoal do Hugo (bloqueio pela
Meta com volume), teto diário próprio, resposta do cliente no OpenWA cai no
celular do Hugo e não entra no chamado. A central de atendimento segue na
Evolution API, também não oficial.

### 4. Indicadores novos

Calculados a partir de `batimento_pedidos` (não de `vigia_historico`, que só
guarda transições enquanto o pedido está aberto e apaga a linha ao fechar):

- entrega dentro dos 3 dias úteis (Grande SP) e entrega à transportadora
  dentro do dia fixo (fora de SP);
- tempo médio para resolver um insucesso;
- tempo de resposta do embarcador (chamado de 15 min e pergunta de insucesso);
- taxa de reentrega por motivo.

## Quando passa para uma pessoa

- Qualquer escrita na Stokki ou na Vuupt (sempre: o agente só sugere).
- Assistente do portal ou do app não resolveu, ou o cliente pediu atendente.
- Motivo de insucesso fora da reentrega automática.
- 3ª tentativa falhou.
- Embarcador recusou o reenvio.
- Qualquer estado do vigia venceu.

Não há critério por valor do pedido nem por "cliente irritado".

## Ordem de construção

1. **Calendário de feriados**, função única usada por todos. Hoje cada módulo
   faz `weekday() >= 5` por conta própria: `vigia/regras.py`,
   `roteirizacao/rotas_fracas.py`, `criar_rotas_diarias.py`,
   `incrementar_rotas.py`, `pipeline.py`, `relatorio_operacional.py`,
   `painel_agentes/torre_controle.py`, `insucesso_entrega/fingerprint_duplicacao_agendada.py`,
   `coleta_emporio_quatro_estrelas/lancar_coleta.py`, `portal_cliente/chamados.py`.
   Fonte: nacionais + São Paulo, em arquivo ou BrasilAPI.
2. **Batimento de pedidos** (spec própria).
3. **Migração das rotas para o app próprio** (pré-requisito do fluxo de
   15 minutos).
4. **Agente**: fluxo de 15 min, sugestões sobre divergências, avisos OpenWA,
   indicadores.

## Pendências

- "O mais rápido possível" para a transportadora precisa virar um número
  para o vigia alertar. Proposta: a carga entra na primeira rota da região
  depois da entrada do pedido; se não entrou até o fim do dia fixo seguinte,
  alerta. Confirmar com o Hugo.
- Resposta do cliente no OpenWA se perde: avaliar resposta automática
  apontando de novo para a central.
- Chave `operator` do OpenWA precisa estar liberada para chat individual
  (`infra/openwa/LEIAME.md`).
