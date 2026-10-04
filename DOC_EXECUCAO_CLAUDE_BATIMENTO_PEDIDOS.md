# Batimento de pedidos (destino final)

Especificação de 04/10/2026, a pedido do Hugo: "um batimento que garanta que
todos os pedidos lançados têm um destino bem definido, ou seja, que saibamos
exatamente o que aconteceu com cada um".

Status: **especificado, não implementado**. Decisões abaixo são do Hugo
(04/10). Ver também a spec do Agente Analista de Logística (sessão paralela),
que consome esta camada e não a implementa.

## Por que existe

O vigia (`vigia/`, DOC_EXECUCAO_CLAUDE_VIGIA.md) acompanha pedido ABERTO:
estado, idade, prazo. Ele resolve "pedido esquecido". Não resolve "o que
aconteceu com o pedido": quando o pedido sai da lista de abertos, o vigia
apaga a linha (`vigia/vigiar.py`, `DELETE FROM vigia_pedidos`) e ninguém
confere se as duas pontas (Stokki e Vuupt/app) contam a mesma história.

Três buracos concretos:

1. Nenhuma tabela registra o destino final do pedido com evidência.
2. O vigia só enxerga o que o pipeline lista ("Aguardando Transportador" de
   todos + todos os status dos 3 prioritários + Estação de Impressão). Pedido
   cancelado na Stokki, "On hold" de outro embarcador, ou criado e expedido
   sem passar pelo pipeline, não aparece.
3. O único cruzamento das duas pontas é `verificar_entregues_nao_expedidos.py`
   (entregue na Vuupt e não expedido na Stokki). Os outros sentidos não
   existem: expedido sem entrega, cancelado na Stokki com serviço vivo,
   cancelado na Vuupt com Stokki aberto (hoje só sai como
   `pulado_cancelado_vuupt` no relatório da importação).

Sem isso, os KPIs que o agente analista promete (entrega em 3 dias úteis,
reentrega por motivo, tempo de insucesso) não podem ser calculados:
`vigia_historico` guarda transições enquanto aberto, não o fechamento.

## A equação (regra de ouro)

Todo dia, para os pedidos a partir da data de corte:

```
lançados na Stokki
  = destino final confirmado nas duas pontas
  + em andamento dentro do prazo (vigia)
  + divergência com motivo
```

Se a soma não fecha, o próprio batimento está quebrado e isso é alerta.
Uma lista de problemas não garante nada; a equação garante.

## Decisões do Hugo (04/10)

| Pergunta | Decisão |
|---|---|
| Data de corte | Última semana: pedidos criados na Stokki a partir de **28/09/2026**. Nada antes entra. |
| Destinos finais | A tabela abaixo, com Dedicado e Lalamove separados. |
| Evidência | **Sempre** precisa de documento assinado confirmando recebimento. Sem documento não é destino final, é divergência. |
| Quem trata divergência | Pode ser o agente analista, caso a caso, conforme a spec dele definir. O que ele não tratar vai pra uma pessoa (Torre). |
| Serviço com mais de um pedido | Não existe mais. Não tratar. |
| Feriados | Em aberto, decisão única para batimento e agente. |

## Destinos finais e evidência exigida

| Destino | Ponta operacional | Ponta Stokki | Documento assinado |
|---|---|---|---|
| ENTREGUE | serviço `done` + `status_done=success` na Vuupt, ou parada ENTREGUE no app (`nucleo_paradas`) | expedido | canhoto: foto na Vuupt (`tem_canhoto`) ou `nucleo_comprovantes` tipo CANHOTO/ASSINATURA |
| REDESPACHADO | transportadora tipo TERCEIROS (`regras/transportadoras.py`) e e-mail às 04h10 enviado (`notificacoes_transportadora_enviadas`) | expedido | comprovante de recebimento da transportadora (**não existe captura hoje**, ver pendências) |
| RETIRADO | serviço de retirada concluído (`retiradas/`) | expedido | comprovante assinado da retirada (**não existe captura hoje**) |
| DEDICADO | linha em `pedidos_dedicados` sem `removido_em` e serviço `done` | expedido | canhoto (mesma regra de ENTREGUE) |
| LALAMOVE | pedido Lalamove com `pod_status` concluído (`lalamove_client.resumir_pedido`) | expedido | `pod_image` da Lalamove guardada |
| DEVOLVIDO | embarcador respondeu "não reenviar" (`insucesso_entrega/`) e nenhum serviço vivo | cancelado ou devolvido | não se aplica (não houve recebimento) |
| CANCELADO | nenhum serviço vivo na Vuupt (`nucleo_pedidos.status` CANCELADO ou sem serviço) | cancelado | não se aplica |

Pedido fechado na Stokki que não se encaixa em nenhuma linha é
**DIVERGÊNCIA**, com um destes motivos (lista fechada, cresce só com decisão):

| Motivo | O que é |
|---|---|
| EXPEDIDO_SEM_ENTREGA | Stokki expedido, mas nenhuma ponta operacional confirma |
| EXPEDIDO_SEM_DOCUMENTO | entregue e expedido, mas sem canhoto/comprovante |
| ENTREGUE_NAO_EXPEDIDO | o caso do `verificar_entregues_nao_expedidos.py` |
| CANCELADO_STOKKI_SERVICO_VIVO | Stokki cancelado, serviço ainda ativo na Vuupt |
| CANCELADO_VUUPT_STOKKI_ABERTO | o `pulado_cancelado_vuupt` do pipeline |
| REDESPACHO_SEM_COMPROVANTE | transportadora recebeu, sem documento |
| RETIRADA_SEM_COMPROVANTE | retirada fechada, sem documento |
| STATUS_STOKKI_DESCONHECIDO | status da Stokki fora do de-para conhecido |

Pedido ABERTO na Stokki não é classificado aqui: é do vigia. O batimento só
registra que ele está "em andamento" e se o vigia o considera no prazo.

## Peças (a implementar)

- `batimento/regras.py` — puro, testado: dado o retrato de um pedido (status
  Stokki, status núcleo, comprovantes, transportadora, dedicado, Lalamove,
  resposta de insucesso), devolve `(destino | DIVERGENCIA, motivo, evidencias)`.
  Mesmo padrão de `vigia/regras.py`.
- `batimento/banco.py` — tabela `batimento_pedidos`, uma linha por
  pedido-base, **nunca apagada**:
  `codigo, criado_stokki_em, embarcador, status_stokki, status_nucleo,
  destino, motivo, evidencias_json, fechado_em, visto_em, tratado_em,
  tratado_por`. Mais `batimento_rodadas` (quando rodou, totais da equação,
  fechou ou não).
- `batimento/listar_stokki.py` — lista TODOS os status da Stokki a partir da
  data de corte, incremental por id (ordenar coluna 1 desc), pelas funções de
  `stokki/pedidos.py`. Confere o total contra `contar_pedidos` por status; se
  não bate, a rodada é "incompleta" e não fecha ninguém (mesma regra do
  retrato do vigia). Pega a trava de `stokki/sessao_uso.py`.
- `batimento/bater.py` — job diário: junta a listagem com `nucleo_pedidos`,
  `nucleo_paradas`, `nucleo_comprovantes`, `expedicoes_processadas`
  (`canhoto_anexado`), `pedidos_dedicados`, Lalamove, `vigia_pedidos`;
  aplica as regras; grava; confere a equação. `--resumo` só imprime.
- Saída: aba "Fechamento" em `/vigia`, exceção na Torre quando a equação não
  fecha ou há divergência vencida, e-mail interno. O
  `verificar_entregues_nao_expedidos.py` vira caso particular e pode ser
  absorvido depois (não no primeiro commit).

Pedido-base: agrupar `-R1`, `-R2`, `-C` com `nucleo/normalizacao.py`. A
reentrega herda o pedido-base; o destino é um só por pedido.

## Horário e sessão Stokki

Rodar uma vez por dia num horário sem outra sessão Stokki aberta. Candidato:
**07h25**, depois do `verificar-entregues-nao-expedidos` (07h15) e antes do
`comparar-vuupt` (07h40) e da expedição (08h). Listar todos os status é mais
pesado que o pipeline; medir o tempo na primeira rodada antes de fixar.
Login concorrente derruba a outra sessão (inclusive a do
`agente-importacao-stokki`): obrigatório `aguardar_vez_para_login`.

## Primeiro passo (antes de qualquer timer)

Script de medição, só leitura, rodado uma vez na VPS como www-data:
lista a Stokki desde 28/09, cruza com o banco, gera xlsx em
`dados/batimento_medicao_<data>.xlsx` com a classificação de cada pedido.
Mostra o tamanho real do buraco e quais divergências aparecem de fato. O
banco local está congelado desde 17/08: a medição só vale na VPS.

## Pendências e riscos

- **Comprovante de redespacho e de retirada não existe no sistema.** A regra
  "sempre documento assinado" faz TODO redespacho e TODA retirada nascerem
  como divergência até existir captura (foto do protocolo da transportadora;
  assinatura de quem retirou no galpão, via `/wms` ou app). Decidir com o
  Hugo: capturar antes de ligar o batimento, ou aceitar a divergência como
  fila de trabalho desde o dia 1.
- Nomes dos status da Stokki para cancelado/devolvido não estão no código
  (conhecidos: Waiting for Carrier, Open, Separating, Ready to Pack, On hold,
  Sent). Rodar `contar_pedidos(STATUS_TODOS)` na VPS antes de escrever o
  de-para; status fora do de-para vira `STATUS_STOKKI_DESCONHECIDO`.
- Lalamove: `pod_image` vem como URL temporária; guardar a imagem no GCS
  (`pedidos/{PS}/canhoto/`) no `sincronizar_lalamove.py`, senão a evidência
  some.
- Canhoto na Vuupt "com foto, sem validar" conta como documento? Hoje a
  expedição aceita (expede e anexa). Proposta: conta, e `validado` fica como
  coluna à parte. Confirmar com o Hugo.
- Feriados (decisão em aberto, compartilhada com o agente).
