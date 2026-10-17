# ADR-0007: Agendamento informado vence o dia fixo da região

## Status
Aceito

## Data
2026-09-28 (estendida ao reagendamento pós-insucesso em 2026-09-29)

## Contexto
Algumas regiões têm dia fixo de entrega (`roteirizacao/regioes_dia_fixo.py`): pedido sem data cai na próxima ocorrência do dia da região. Em paralelo, embarcadores informam data de agendamento por e-mail, planilha (NUU) ou portal.

Caso PS-40316 (NUU → Vero Pane, Santo André): o cliente pediu 01/10 pelo portal e o pedido ficou para 30/09 na Vuupt. Três causas: o pipeline só usava a confirmação se o destinatário tivesse "AGENDA" na BD_CLIENTES; `atualizar_agendamentos_confirmados.py` não tinha timer (última aplicação 04/08); com o serviço sem data, o dia fixo preencheu a próxima ocorrência.

Regra dada pelo Hugo em 28/09/2026: "Se em algum lugar tiver indicação de agendamento, considerar sempre essa data e ignorar o dia fixo."

## Decisão
- Data de agendamento informada (portal, planilha, e-mail, resposta de insucesso) **sempre vence** o dia fixo da região. Dia fixo só vale para pedido **sem** agendamento informado.
- Quem grava data passa por um só helper: `regioes_dia_fixo.ajustar_data_por_dia_fixo(servico, data) -> (data_final, regra|None)`, e notifica o remetente quando ajusta.
- `atualizar_agendamentos_confirmados.py` roda nas sequências das 18h e 22h; só mexe em serviço `not_assigned` com data >= hoje, e tira o pedido de rota/rascunho ativo de outra data antes de aplicar.
- Reagendamento após insucesso também respeita a data escolhida (não empurra mais para o dia fixo).

## Alternativas consideradas

### Dia fixo vence o agendamento (comportamento anterior)
- Rejeitada pelo Hugo: o cliente combinou uma data; a região não pode desfazer.

### Ajustar também datas já gravadas em dia errado (caso PS-39752)
- Adiada: o Hugo escolheu só avisar no botão "Roteirizar" e em "Criar rota" (`confirm()`), sem corrigir dado antigo.

## Consequências
- Fonte `mensagem_stokki` traz só horário (a data é calculada por nós); ali o dia fixo continua valendo.
- Planilha NUU lida às 18h antes do pipeline criar o serviço não casa; a retentativa só na leitura seguinte.
- O botão "Roteirizar" ignora o dia fixo de propósito (a seleção do operador vence) e só avisa.
- Esta é uma regra de negócio registrada como ADR porque atravessa pipeline, roteirização, insucesso e portal; mudar exige decisão do Hugo.
