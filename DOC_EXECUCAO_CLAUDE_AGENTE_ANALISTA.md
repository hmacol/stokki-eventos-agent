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

Os motivos são os da lista fechada do batimento (mesmo nome).

| Motivo no batimento | Ação que o agente sugere | Quem aprova |
|---|---|---|
| ENTREGUE_NAO_EXPEDIDO | Expedir na Stokki anexando o canhoto (`expedir_pedidos.expedir_na_stokki` + `anexar_canhoto`) | Torre |
| EXPEDIDO_SEM_DOCUMENTO | Cobrar o canhoto do motorista (chamado no app) | Torre |
| REDESPACHO_SEM_COMPROVANTE | Cobrar o comprovante da transportadora (e-mail) | Torre |
| RETIRADA_SEM_COMPROVANTE | Cobrar assinatura de quem retirou (`/wms`) | Torre |
| EXPEDIDO_SEM_ENTREGA | Perguntar à operação se a entrega aconteceu; se não, reabrir na Stokki | Torre |
| CANCELADO_VUUPT_STOKKI_ABERTO | Recriar o serviço (`pipeline.py --pedido`) ou cancelar na Stokki, conforme o embarcador | Torre |
| CANCELADO_STOKKI_SERVICO_VIVO | Cancelar o serviço na Vuupt (`cancelar_servico`) ou a parada no app | Torre |
| STATUS_STOKKI_DESCONHECIDO | Nenhuma; só aponta para incluir no de-para | ninguém (é bug) |
| 3ª tentativa falhou ou embarcador recusou o reenvio (vem do vigia, não do batimento) | Pedir confirmação de devolução ao embarcador; com ela, sugerir o cancelamento na Stokki | embarcador confirma, Torre executa |

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

## Arquitetura

O agente não é um processo novo: é um módulo `agente/` que roda dentro dos
ritmos que já existem e grava tudo o que decide numa tabela própria.

```
                 Stokki (Playwright)      Vuupt (REST)       app motorista (API 8073)
                        |                     |                       |
         pipeline.py  --+   sincronizar_*  ---+   nucleo/operacao.py -+
                        v                     v                       v
                  nucleo_pedidos / nucleo_paradas / nucleo_eventos / nucleo_comprovantes
                        |                                             |
                 vigia/vigiar.py (15 min)                 batimento/bater.py (diário)
                        |                                             |
                   vigia_pedidos                               batimento_pedidos
                        \_____________________   _____________________/
                                              v v
                                    agente/analisar.py  (timer, 5 min)
                                    regras.py  -> o que fazer com cada fato
                                    acoes.py   -> grava agente_acoes (PROPOSTA)
                                              |
                 +----------------------------+----------------------------+
                 v                            v                            v
        embarcador                       motorista                       Torre
   e-mail (email_utils)          chamado no app (portal_chamados,   exceção "Agente" com
   OpenWA (notificar_whatsapp,    perfil logistica) + push          botão Aprovar/Recusar
   origem cliente)                                                   -> acoes.executar()
   portal (chamado + assistente)                                      (Stokki/Vuupt)
```

Peças:

- `agente/regras.py` — puro, testado, mesmo padrão de `vigia/regras.py`.
  Entrada: um fato (estado do vigia vencido, divergência do batimento,
  evento de parada, mensagem de chamado). Saída: lista de ações com
  `tipo`, `destinatario`, `template`, `precisa_aprovacao`.
- `agente/acoes.py` — tabela `agente_acoes`: `id, pedido, fato_origem,
  tipo, destinatario, texto, status (PROPOSTA | ENVIADA | APROVADA |
  RECUSADA | EXECUTADA | FALHOU), criado_em, aprovado_por, executado_em,
  resultado`. Append-only: nunca apaga, nunca reescreve texto enviado.
  Idempotência por `(pedido, fato_origem, tipo)`: o mesmo fato não gera a
  mesma ação duas vezes.
- `agente/analisar.py` — job de 5 min (`stokki-agente-analisar`). Só lê o
  banco (como o vigia): nunca chama Stokki nem Vuupt. Executa ações que
  não precisam de aprovação (avisos) e deixa as outras em PROPOSTA.
- `agente/executar.py` — chamado pelo botão da Torre. É o único lugar que
  escreve na Stokki/Vuupt, e só para ações APROVADAS. Pega a trava de
  `stokki/sessao_uso.py`.
- `agente/templates.py` — todos os textos (e-mail, WhatsApp, chamado),
  sem LLM. A IA entra só em `agente/leitura.py`: classificar resposta do
  embarcador num chamado (mesma técnica de `ler_respostas_agendamento.py`).
- Torre: exceção tipo "Agente" por ação em PROPOSTA; tela `/agente` lista
  tudo com filtro por status. Tratativa registrada em `tratativas.py`
  (`origem="agente"`) para aparecer em `/historico-tratativas`.

Regras que não mudam:

1. Toda ação sobre Stokki ou Vuupt nasce PROPOSTA e só vira EXECUTADA com
   `aprovado_por` preenchido.
2. Aviso ao embarcador respeita `preferencias_notificacao` e o teto do
   `notificar_whatsapp` (origem `cliente`). Enquanto `forcar_destino` tiver
   o número do Hugo, nenhum cliente recebe.
3. Hook do agente em código existente (pipeline, expedição, app) é
   best-effort: `try/except` com log, nunca derruba o fluxo atual.

## Estados do pedido (visão do agente)

O agente não cria estado novo: ele lê os do vigia (pedido aberto) e os do
batimento (pedido fechado) e reage. Setas com `*` são as reações dele.

```
 Stokki: pedido criado
   |
   v
 SEM_SERVICO --4h--> * aviso interno (já é exceção do vigia)
   |
   v  pipeline
 NO_POOL ----1 dia útil----> * aviso interno
   |  \__ AGUARDANDO_CLIENTE --2 d.u.--> * cobrar data (e-mail + OpenWA)
   |  \__ AGENDADO (sem prazo)
   v  roteirização + confirmação
 EM_RASCUNHO --19h véspera--> * aviso interno
   |
   v  envio da rota
 EM_ROTA
   |  evento CHEGADA no app
   |  \__ PROBLEMA_NA_ENTREGA (novo, só no agente)
   |        |-- abre chamado p/ embarcador, relógio 15 min
   |        |-- resposta a tempo ---------> * repassa ao motorista
   |        |-- 15 min sem resposta ------> * motorista segue; avisa embarcador
   |        |      \__ resposta tardia ---> * pede retorno; motorista confirma
   |        |              |-- volta ------> * avisa embarcador
   |        |              |-- não volta --> * avisa: próximo dia de rota da região
   |        v
   |  ENTREGUE | PARCIAL | INSUCESSO | REAGENDAR
   |
   |-- ENTREGUE ----> batimento: ENTREGUE (c/ canhoto) ou EXPEDIDO_SEM_DOCUMENTO
   |                  * cobrar canhoto do motorista
   |
   |-- INSUCESSO ---> * avisa embarcador da falha (OpenWA + e-mail de pergunta)
   |                  |-- resposta "reenviar"/data --> -R1 (fluxo existente)
   |                  |-- 12h sem resposta ----------> reentrega automática
   |                  |-- "não reenviar" ------------> RECUSADO
   |                  |-- motivo sem auto ----------> INSUCESSO vencido -> Torre
   |
   |-- -R1 falha ---> mesma coisa -> -R2
   |-- -R2 falha ---> 3ª TENTATIVA ESGOTADA (novo)
   |                  * pede confirmação de devolução ao embarcador
   |                  |-- confirmou --> PROPOSTA: cancelar na Stokki -> Torre aprova
   |                  |-- não respondeu 2 d.u. --> Torre decide
   |
   v
 Fechado na Stokki -> batimento classifica:
   ENTREGUE | REDESPACHADO | RETIRADO | DEDICADO | LALAMOVE | DEVOLVIDO | CANCELADO
   ou DIVERGÊNCIA (tabela da seção 2) -> * PROPOSTA -> Torre aprova
```

Fora da Grande SP a linha EM_ROTA termina em REDESPACHADO: entregue à
transportadora com comprovante + Stokki expedido. Depois disso não há estado.

## Prompts base

Só duas chamadas ao modelo. Tudo o mais é template fixo em
`agente/templates.py`. Modelo: o mesmo `MODELO_PADRAO` dos assistentes
(hoje `claude-opus-5`, chave `anthropic.api_key` do config), saída em JSON,
temperatura baixa.

### 1. Ler a resposta do embarcador num chamado de entrega

Usado no passo 3 do fluxo de 15 min e na resposta tardia. Mesma técnica de
`ler_respostas_agendamento.py`.

```
Você lê a resposta de um embarcador (cliente da Fresh Log, transportadora de
alimentos refrigerados) a um chamado aberto porque o motorista encontrou um
problema na entrega. Classifique a resposta. Não invente nada.

Pedido: {codigo}. Destinatário: {destinatario}. Problema relatado pelo
motorista: {problema}. Hora de agora: {agora}.

Resposta do embarcador:
"""
{texto}
"""

Responda só com JSON:
{"decisao": "ENTREGAR_ASSIM" | "AGUARDAR" | "VOLTAR_DEPOIS" | "CANCELAR_HOJE" | "NAO_ENTENDI",
 "instrucao_motorista": "uma frase curta, em linguagem de rua, ou null",
 "nova_data": "YYYY-MM-DD ou null",
 "confianca": 0.0 a 1.0}

Se a resposta pedir algo que não está nas opções (mudar endereço, trocar
produto, falar com o destinatário), use NAO_ENTENDI.
```

Regra de uso: `confianca < 0.7` ou `NAO_ENTENDI` vai para a fila humana
(`entrar_na_fila`), nunca para o motorista.

### 2. Resumir o caso para a Torre

Usado quando uma ação vira PROPOSTA, para o operador aprovar sem abrir cinco
telas.

```
Resuma para um operador de logística, em no máximo 3 frases, sem markdown,
o que aconteceu com este pedido e o que está sendo proposto. Use só os
fatos abaixo. Não sugira ação além da proposta.

Pedido: {codigo}. Embarcador: {embarcador}. Destinatário: {destinatario}.
Linha do tempo (vigia_historico + tratativas_pedido + nucleo_eventos):
{linha_do_tempo}
Divergência ou estado vencido: {fato}
Ação proposta: {acao}
Evidências: {evidencias}
```

### O que o agente nunca escreve

As mesmas regras dos assistentes atuais, agora para o agente inteiro:
não promete reentrega, cancelamento, reagendamento, devolução, estorno ou
mudança de endereço; não fala com o destinatário; não inventa previsão de
horário; toda mensagem ao embarcador termina com o link da central de
atendimento.

## Mapeamento de APIs e funções

O que o agente lê e chama, por sistema. Nada aqui é novo; a coluna "Falta"
diz o que precisa ser criado.

### Banco próprio (`dados/dados.db`, só leitura no job)

| Dado | Tabela / função |
|---|---|
| Estado e prazo de cada pedido aberto | `vigia_pedidos`, `vigia/consulta.py` |
| Histórico de estados | `vigia_historico` |
| Destino final e divergências | `batimento_pedidos` (a criar, spec do batimento) |
| Rota, paradas, eventos do app | `nucleo_rotas`, `nucleo_paradas`, `nucleo_eventos` (tipos: DESLOCAMENTO, CHEGADA, ENTREGUE, PARCIAL, INSUCESSO, REAGENDAR, OBSERVACAO) |
| Comprovantes do app | `nucleo_comprovantes`, `nucleo/validacao_fotos.py` |
| Chamados (embarcador e motorista) | `portal_chamados`, `portal_chamados_mensagens` (`portal_cliente/chamados.py`) |
| Insucesso: pergunta enviada, resposta, reentrega | `insucesso_entrega/fingerprint_*.py`, `expedir_pedidos._fatos_do_banco` |
| Agendamento pedido/confirmado | `agendamentos_pedido` |
| Dedicados | `pedidos_dedicados` |
| Dia fixo da região | `roteirizacao/regioes_dia_fixo.py` |
| Preferências e WhatsApp do embarcador | `preferencias_notificacao.py` (coluna `whatsapp`) |
| Tratativas | `tratativas.registrar_evento` / `buscar` |
| Exceções tratadas na Torre | `torre_excecoes_tratadas` |

### App do motorista (`nucleo/api_motorista.py`, porta 8073)

| Uso | Endpoint / função | Falta |
|---|---|---|
| Saber que o motorista chegou e relatou problema | `POST /api/paradas/<id>/eventos` tipo CHEGADA + OBSERVACAO, `nucleo/operacao.registrar_evento_parada` | Evento `PROBLEMA` com `motivo_id` (ou reaproveitar OBSERVACAO com flag) que dispare o chamado |
| Abrir chamado para o motorista / mandar instrução | `nucleo/chamados_motorista.py`: `POST /api/atendimento/conversas`, `POST .../mensagens`, `ch.registrar_mensagem_equipe` com autor "Agente" | Push ao motorista quando a instrução chega (`/api/push-token` já guarda o token; falta o envio) |
| Resposta "consigo voltar / não consigo" | `POST /api/atendimento/chamados/<id>/acao` | Duas ações novas: `VOLTAR_SIM`, `VOLTAR_NAO` |
| Registrar insucesso / reagendar | `POST /api/paradas/<id>/eventos` tipo INSUCESSO / REAGENDAR | nada |
| Cobrar canhoto | mensagem no chamado + `POST /api/paradas/<id>/comprovantes` | nada |

Rota na Vuupt: nenhum destes existe. O fluxo de 15 min só vale para rotas
com `provedor=APP`.

### Embarcador

| Uso | Função | Falta |
|---|---|---|
| Abrir chamado do problema na entrega | `ch.criar_chamado(origem="agente", status=AGUARDANDO_FL)` + `ch.adicionar_mensagem(ORIGEM_SISTEMA)` | Origem `agente` nos filtros da fila |
| Aviso WhatsApp | `notificar_whatsapp.despachar(origem="cliente", ...)` -> `integracao_openwa.enviar_texto` | Templates novos: problema na entrega, motorista seguiu, falha de entrega, confirmação de devolução; assinatura por `(pedido, tipo)` |
| E-mail | `email_utils.enviar_email` com `preferencias_notificacao` | Templates novos (mesmos quatro) |
| Ler resposta do embarcador | `portal_chamados_mensagens` (origem cliente) -> prompt 1 | `agente/leitura.py` |
| Pergunta de insucesso e resposta | `insucesso_entrega/notificar_insucesso_aguardando_resposta.py`, `resposta_insucesso/app.py` (`/insucesso/r/<token>`) | Opção "confirmo a devolução" na página de resposta |

### Vuupt (`vuupt_client.VuuptClient`, só após aprovação)

| Ação proposta | Função |
|---|---|
| Cancelar serviço (CANCELADO_STOKKI_SERVICO_VIVO) | `cancelar_servico` |
| Recriar serviço (CANCELADO_VUUPT_STOKKI_ABERTO) | `pipeline.py --pedido PS-X` |
| Reagendar / mover para o próximo dia de rota | `atualizar_servico` com `scheduled_start`, via `insucesso_entrega/aplicar_resposta_insucesso._ajustar_data_por_dia_fixo` |
| Reentrega manual (-R1/-R2) | `expedir_pedidos.duplicar_servico_por_insucesso` |

### Stokki (`stokki/`, Playwright, só após aprovação)

| Ação proposta | Função | Falta |
|---|---|---|
| Expedir + anexar canhoto | `expedir_pedidos.expedir_na_stokki`, `anexar_canhoto` | nada |
| Conferir situação | `expedir_pedidos._situacao_na_stokki` | nada |
| Cancelar pedido (devolução) | — | Não existe função. Hoje é feito à mão na tela. Mapear a tela em `investigacao/` antes de prometer |
| Reabrir pedido expedido por engano | — | Idem |

Sempre dentro de `stokki/sessao_uso.py` (`aguardar_vez_para_login`).

### Torre e painel

| Uso | Onde | Falta |
|---|---|---|
| Exceção "Agente" com botão | `painel_agentes/torre_controle._montar_excecoes` (novo tipo) | Ação `aprovar`/`recusar` chamando `agente/executar.py` |
| Tela `/agente` | novo template + rota em `painel_agentes.py`, níveis total/operador/atendimento | tudo |
| Badge no menu | `contadores_menu.py` | contagem de PROPOSTA |
| Resumo diário do agente | `notificar_execucao_agente.py` | nada |

## Pendências

- `nucleo_eventos` não tem o tipo PROBLEMA: decidir entre evento novo e
  OBSERVACAO com flag. Evento novo é mais limpo e não quebra o app atual
  (tipo desconhecido já é rejeitado pelo servidor).
- Cancelar pedido na Stokki não tem função: mapear a tela antes de prometer
  a sugestão de devolução.
- Push ao motorista: o token é guardado mas nada envia. Sem push, a
  instrução do agente só chega quando o motorista abrir o chat.
- "O mais rápido possível" para a transportadora precisa virar um número
  para o vigia alertar. Proposta: a carga entra na primeira rota da região
  depois da entrada do pedido; se não entrou até o fim do dia fixo seguinte,
  alerta. Confirmar com o Hugo.
- Resposta do cliente no OpenWA se perde: avaliar resposta automática
  apontando de novo para a central.
- Chave `operator` do OpenWA precisa estar liberada para chat individual
  (`infra/openwa/LEIAME.md`).
