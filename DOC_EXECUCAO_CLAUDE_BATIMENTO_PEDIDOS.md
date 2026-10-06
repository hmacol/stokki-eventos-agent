# Batimento de pedidos (destino final)

Especificação de 04/10/2026, a pedido do Hugo: "um batimento que garanta que
todos os pedidos lançados têm um destino bem definido, ou seja, que saibamos
exatamente o que aconteceu com cada um".

Status (06/10): **job diário no ar desde 05/10 (`batimento/bater.py`,
timer 07h25)**. Saída (aba Fechamento, Torre, e-mail) conforme
`docs/superpowers/specs/2026-10-05-batimento-saida-design.md`. Decisões abaixo são do Hugo (04/10 e 05/10). Ver também a spec do Agente Analista de Logística (sessão paralela),
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
| Quem trata divergência | O agente analista monta a ação com a evidência como **sugestão aprovada**: uma pessoa aprova antes de qualquer escrita na Stokki ou na Vuupt; nada automático (confirmado pelo Hugo, 04/10). O que o agente não cobrir vai pra Torre. |
| Redespacho/retirada sem comprovante | **Opção A** (Hugo, 04/10): entram como divergência desde o dia 1; a fila é a lista de "cobrar comprovante". Captura (foto do protocolo, assinatura no galpão) fica pra depois da medição. |
| Canhoto sem validação | **Vale** como documento (Hugo, 04/10). Validado fica como coluna à parte. |
| Serviço com mais de um pedido | Não existe mais. Não tratar. |
| Feriados | Em aberto, decisão única para batimento e agente. |
| Id da faixa fora de toda listagem | (05/10) Situação "Importação" no `/show` **não conta** como lançado; `/show` com HTTP 500 = **inexistente**, não conta. Os dois ficam registrados na rodada (`cobertura`). |

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
| LALAMOVE_SEM_COMPROVANTE | Lalamove entregou, `pod_image` não guardada |
| EXPEDIDO_COM_INSUCESSO_ABERTO | Stokki expedido, último serviço fechou em insucesso |
| STATUS_STOKKI_DESCONHECIDO | status da Stokki fora do de-para conhecido |

Pedido ABERTO na Stokki não é classificado aqui: é do vigia. O batimento só
registra que ele está "em andamento" e se o vigia o considera no prazo.

## Peças

Feitas (04/10 e 05/10):

- `batimento/regras.py` — puro, testado (`test_regras.py`): dado o retrato
  de um pedido (status Stokki, status núcleo, comprovantes, transportadora,
  dedicado, Lalamove, resposta de insucesso), devolve
  `(DESTINO | EM_ANDAMENTO | DIVERGENCIA, rótulo, evidências)`. Mesmo padrão
  de `vigia/regras.py`. `situacao_stokki()` resume o texto da coluna
  "state" em ABERTO/EXPEDIDO/CANCELADO por "contém" (inglês e português).
- `batimento/medir.py` — a leitura (`coletar()`), usada pela medição manual
  (xlsx) e pelo job. Lista a Stokki em ordem de id decrescente até o corte
  nos filtros `""` (só abertos; `all` volta sem linhas), `Sent` e
  `Canceled`; todo id da faixa que nenhum filtro trouxe é aberto no `/show`
  (`cobrir_faixa`). Nome da transportadora vem com CNPJ colado: separado
  antes do `resolver`. 429 da Vuupt espera e tenta de novo.
- `batimento/banco.py` — `batimento_pedidos` (uma linha por pedido-base,
  **nunca apagada**: caixa, rótulo, evidências, `desde`, `fechado_em`,
  `visto_em`, `tratado_em/por`) e `batimento_rodadas` (totais, fecha ou não,
  resumo com a cobertura da faixa). Pedido que chegou a DESTINO fica
  **congelado**: a Vuupt é lida só nos últimos 30 dias, e reclassificar
  faria entrega antiga virar divergência.
- `batimento/bater.py` — job diário (`infra/stokki-batimento-pedidos.*`,
  07h25). Equação que não fecha sai com código 1 (alerta de falha do
  systemd). `--resumo`/`--modo-teste` só imprime. No fim da rodada manda
  e-mail interno (`notificacao_execucao.destinatario`) só se houver
  divergência nova ou a conta não fechar.
- `batimento/consulta.py` — aba `/vigia?aba=fechamento` (conta da última
  rodada, divergências por motivo, histórico de 14 rodadas, botão Tratar
  para total/operador via `POST /api/batimento/tratar`; a tratativa some
  quando o pedido troca de motivo) e `excecoes_torre`: na Torre entra só
  divergência **real** sem tratativa há mais de 1 dia útil (seg-sex), um
  item por pedido, e um crítico se a última rodada não fechou. As "sem
  comprovante" ficam só na aba e no e-mail (Hugo, 05/10). "Tratar" e
  "Desfazer" na Torre (unitário e lote) também gravam/limpam a tratativa em
  `batimento_pedidos` (obs "[Torre] <motivo>"; Hugo, 06/10).
- Oscilação (06/10): `div_rotulo`/`div_desde` guardam o motivo de
  divergência e desde quando; só mudam com motivo NOVO. Prazo da Torre,
  e-mail de "nova" e a coluna "Desde" da aba usam `div_desde`; a tratativa
  só some com motivo novo. Rodada com mais de 26h = item crítico "sem
  rodada" na Torre e selo "rodada atrasada" na aba.

A implementar depois:
- O `verificar_entregues_nao_expedidos.py` vira caso particular e pode ser
  absorvido.

Pedido-base: agrupar `-R1`, `-R2`, `-C` com `nucleo/normalizacao.py`. A
reentrega herda o pedido-base; o destino é um só por pedido.

## Horário e sessão Stokki

Rodar uma vez por dia num horário sem outra sessão Stokki aberta. Candidato:
**07h25**, depois do `verificar-entregues-nao-expedidos` (07h15) e antes do
`comparar-vuupt` (07h40) e da expedição (08h). Medido em 05/10: ~10 s de
listagem, ~90 s de `/show` (101 ids fora das listagens) e ~45 s de Vuupt.
Login concorrente derruba a outra sessão (inclusive a do
`agente-importacao-stokki`): obrigatório `aguardar_vez_para_login`.

## Primeiro passo (antes de qualquer timer): `batimento/medir.py`

Só leitura, rodado uma vez na VPS como www-data, num horário sem outra
sessão Stokki (ex.: depois das 07h15 e antes das 08h, ou à noite):

```
cd /opt/stokki-eventos
sudo -u www-data venv/bin/python -m batimento.medir
sudo -u www-data venv/bin/python -m batimento.medir --data-corte 2026-09-28 --dias-vuupt 30
```

O que faz: acha o id da Stokki de corte (maior PS que o núcleo viu nascer
ANTES da data de corte, mais um: o menor PS criado desde o corte não serve,
pedido antigo reimportado puxava pra PS-31190; `--id-minimo N` força; 28/09
= PS-40267), pega a trava `stokki/sessao_uso.py`, lista a Stokki (ver
Peças) do mais novo pro mais velho até o corte, lê os serviços `done` da Vuupt (com
`checklistAnswers`), cruza com `nucleo_pedidos`, `nucleo_paradas/rotas`,
`nucleo_comprovantes`, `expedicoes_processadas/falhas`, `pedidos_dedicados`,
`insucessos_duplicados`, `insucessos_aguardando_resposta`, `vigia_pedidos`
e BD_TRANSPORTADORAS, classifica e grava
`dados/batimento_medicao_<hoje>.xlsx` (abas Resumo, Pedidos, Status Stokki).
A aba "Status Stokki" mostra os nomes de status que a Stokki devolve: é dela
que sai o de-para definitivo. Rodado na VPS em 04/10 (574 lançados = 255
destino + 280 em andamento + 39 divergência) e 05/10 (636 = 335 + 256 + 45;
faixa 40267..41003 = 636 listados + 81 Importação + 20 inexistentes).

Evidência de canhoto, nesta ordem: `nucleo_comprovantes` (app) >
`images_quantity` do checklist da Vuupt > `expedicoes_processadas.canhoto_anexado`.
Comprovante de redespacho, retirada e Lalamove é sempre "não" (opção A).

## Pendências e riscos

- **Comprovante de redespacho e de retirada não existe no sistema.** A regra
  "sempre documento assinado" faz TODO redespacho e TODA retirada nascerem
  como divergência até existir captura (foto do protocolo da transportadora;
  assinatura de quem retirou no galpão, via `/wms` ou app). Decidido (opção
  A): a divergência é a fila de trabalho desde o dia 1.
- De-para da Stokki visto na VPS (05/10): abertos = Aguardando Transportador,
  Aberto, Em Conferência, Em espera, Em separação; expedido = Enviado
  (`Sent`); cancelado = Cancelado (`Canceled`); "Importação" só no `/show`.
  Status novo fora do de-para vira `STATUS_STOKKI_DESCONHECIDO`.
- Os ids em "Importação" (80 JERSEY VALE 40769..40848, 1 BURIN 40605) são
  abertos no `/show` toda rodada enquanto não saírem desse estado.
- Lalamove: `pod_image` vem como URL temporária; guardar a imagem no GCS
  (`pedidos/{PS}/canhoto/`) no `sincronizar_lalamove.py`, senão a evidência
  some.
- Feriados (decisão em aberto, compartilhada com o agente).
