# Pool do Planejamento direto da Stokki (a Vuupt só recebe a rota enviada)

Data: 2026-11-26. Status: rascunho para aprovação do Hugo.

## Intenção e por quê

O Hugo quer a **saída definitiva da Vuupt**. Este é o passo de leitura do plano de saída
(`DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md`, Etapa 4): o Planejamento deixa de depender da Vuupt para saber
**quais pedidos existem**. Hoje o pedido só entra no pool depois que o pipeline cria o serviço na Vuupt
(`pipeline.py:1079`) e o núcleo o espelha. O pedido passa a nascer no núcleo direto da Stokki; a Vuupt só
ganha o serviço **ao confirmar e enviar a rota** (decisão do Hugo em 26/11, para o financeiro e os
motoristas do app dela seguirem iguais até o corte).

## Metas

- Pedido liberado pela Stokki aparece no pool sem existir na Vuupt.
- Enviar a rota (rascunho, incremento, edição de rota enviada) cria o serviço na Vuupt na hora e segue
  como hoje, com o mesmo resultado na Vuupt (financeiro idêntico).
- Voltar atrás é trocar uma chave do config (`planejamento.fonte_pool`).

## Não metas / anti-metas

- Não desligar a Vuupt, não mexer no app dos motoristas nem na baixa (execução continua na Vuupt).
- Não ler a Stokki ao vivo a cada "Atualizar" (sessão única derruba o projeto de importação).
- Não pode haver pedido perdido ou duplicado na Vuupt (um serviço por código) nem rota que sai sem
  o serviço criado.
- Não mexer na sombra do freshhub (`fonte_pool: freshhub`); ela continua independente.

## Restrições encontradas (fatos do código)

- `rascunhos_parada.service_id` e `rascunhos_plano_original.service_id` são NOT NULL e todo o
  `rascunhos_rota.py` (~2.000 linhas: mover, reordenar, remover, enviar, cancelar) e
  `planejamento_rotas.py` indexam por esse id. Os helpers do pool (dedicados, segurados, retornando)
  indexam por `s["id"]`.
- `nucleo/pool.py:101` só devolve pedido com `vuupt_service_id`.
- `enviar_rascunho` (`rascunhos_rota.py:1378-1474`) cria a rota com `POST /routes` com os service_id;
  `incrementar_rotas` usa `adicionar_atividades` (`incrementar_rotas.py:971`).
- A criação do serviço na Vuupt também resolve `customer_id` (GET/PUT/POST em /customers,
  `vuupt_client.py:513-570`) e grava dia fixo/agendamento automático depois (`registrar_data_automatica`,
  `pipeline.py:1094`). Geocodificação e janela são locais. **Dia fixo e data automática NÃO são só
  locais** (corrigido na revisão): gravam na Vuupt pelo id do pool (`regioes_dia_fixo.py:462`,
  `datas_automaticas.py:128`, chamados em `criar_rotas_diarias.py:895,915` e `incrementar_rotas.py:526`).
- Consumidores que chamam `not_assigned` direto na Vuupt (fora do pool): `reagendar_feriado`,
  `reconciliar_pedidos_retirada`, `torre_controle:879`, `portal_cliente/dados_cliente:416`,
  `conferir_pool`. Cancelar, reagendar e o portal pedem `vuupt_service_id`
  (`cancelar_servico.py:56`, `acoes_pedidos.py:103`, `portal_cliente/cancelamento.py:154`).

## Abordagens consideradas

1. **Id sintético + materialização no envio (escolhida).** O pedido sem serviço entra no pool com id
   **negativo** (`-nucleo_pedidos.id`), que passa pelo rascunho sem mudar o schema. Um passo único
   `materializar_servicos(ids)` roda no início de `enviar_rascunho`, `incrementar_rotas` e na adição de
   parada em rota enviada: cria o serviço na Vuupt (mesmo payload/`customer_id` de hoje), grava o
   `vuupt_service_id` no núcleo e **reescreve o id negativo nas tabelas do rascunho pelo real** antes de
   montar a rota. Troca: pouca mudança nas 2.000 linhas do rascunho; custo é o passo de materialização e
   tratar bem a falha no meio.
2. **Trocar a chave do rascunho para o id do núcleo.** Mais limpo a longo prazo (é o que o app próprio
   vai precisar), mas reescreve rascunho, pool, incremento e edição de rota: alto risco num sistema
   usado todo dia. Fica para a Etapa 5.
3. **Pipeline continua criando o serviço na Vuupt e só o pool lê do núcleo (`fonte_pool: nucleo`).**
   Rápido, mas não tira a dependência: pedido só aparece depois de existir na Vuupt.

Decisão: 1, por ser reversível por chave e confinada.

## Desenho (abordagem 1)

- **Chave nova** `planejamento.fonte_pool: stokki` (ou flag própria `pool.sem_servico_vuupt`; decisão do
  construtor). Com a chave desligada tudo é igual a hoje.
- **Pipeline:** com a chave ligada, `processar_pedido` monta o payload como hoje, mas em vez de
  `criar_ou_atualizar_servico` só grava em `nucleo_pedidos` (origem PIPELINE, `vuupt_service_id` nulo),
  guardando o payload pronto em `dados_json` para a materialização usar. Pedido que **já tem** serviço
  na Vuupt (criado antes da virada ou reentrega) continua como está e atualiza o serviço existente.
- **Pool:** `listar_pool` inclui pedidos ABERTOS sem `vuupt_service_id` com id negativo; `pedido_para_servico`
  devolve o id sintético. Pedidos antigos com serviço seguem com o id real.
- **Materialização:** idempotente (busca por code antes de criar, como `criar_ou_atualizar_servico`
  já faz), por pedido; falha de um pedido tira só ele da rota com motivo na tela e no log
  (regra "rejeição sempre com o motivo"), nunca envia rota com id negativo. Lote em ordem estável.
  Depois de criar, roda `registrar_data_automatica` quando for o caso (hoje só na ação "criado").
- **Ações em pedido sem serviço** (cancelar, reagendar, alterar endereço, portal): primeiro corte só
  **bloqueia com mensagem clara** ("pedido ainda não tem serviço na Vuupt") ou faz a versão local
  (cancelar = marcar cancelado no núcleo; reagendar = atualizar `agendamento_*` do núcleo, que a
  materialização usa). Lista exata é fase 2.
- **Consumidores que leem `not_assigned` direto da Vuupt** passam a ler o núcleo quando a chave estiver
  ligada, um a um, na ordem: `conferir_pool`, `reagendar_feriado`, `torre_controle`, `portal dados_cliente`.
- **Sync Vuupt→núcleo** (`sincronizar_servicos_vuupt`) continua, mas não pode marcar como
  `PEDIDO_SUMIU_DA_VUUPT` um pedido que nunca esteve lá.

## Fases (cada uma reversível)

1. **Sombra:** o pipeline grava também o pedido "sem serviço" e `comparar_pool` mede núcleo-novo x Vuupt
   (portão: 3 dias úteis em 0).
2. **Corte do pool + envio:** chave ligada; materialização no `enviar_rascunho` e `incrementar_rotas`.
   Prova em homologação e em modo teste antes da produção.
3. **Ações e consumidores diretos** (lista acima).
4. Desligar o sync de descoberta da Vuupt (fica só o de status de rota).

## O que fica para o construtor

Nome final da chave; formato exato do payload guardado em `dados_json`; como o tempo de criação do
serviço (dezenas de pedidos no mesmo clique) entra no "Confirmar e enviar" (barra de progresso ou
assíncrono); testes de `rascunhos_rota` com id negativo.

## Revisão independente (Fable, 26/11): ajustes obrigatórios no desenho

Veredito: aprovar com ajustes. A abordagem 1 se mantém; o texto acima estava incompleto nos pontos abaixo.

**ALTO (entram no desenho):**
1. **Pipeline sem serviço perde a memória do pedido** (`pipeline.py:692`, `538-545`, `973`, `1035-1045`):
   passar a linha do núcleo como `servico_existente` (mesmo formato de `pedido_para_servico`). Sem isso a
   data automática recalcula a cada rodada (pedido da manhã vira D+2) e o aviso de dia fixo repete.
2. **Dia fixo / data automática** precisam gravar no núcleo quando o pedido nasce, não "depois de criar na
   Vuupt"; senão `marcar_datas_automaticas` trata a data como do cliente e o pedido vira dedicado.
3. **`conferir_pool.py:216`** cancela entregue por `cancelar_servico_oficial(s["id"])`: com id negativo falha e
   a materialização criaria serviço de pedido já entregue. Passa a cancelar local quando não há serviço.
4. **Fechar pedido sem serviço** (cancelado na Stokki, virou retirada): hoje `pipeline.py:715-783` e
   `cancelar_cancelados_stokki.py:97-99` exigem `vuupt_service_id`. Precisam fechar no núcleo também.
5. **`nucleo_pedidos` não tem `id`** (PK é `codigo`, `banco.py:94`). Usar rowid é instável (backup é
   `VACUUM INTO`). Criar coluna própria (`id_pool` inteiro estável) e o id sintético é `-id_pool`.
6. **Troca de identidade fora do envio:** o sync (`sincronizar_servicos_vuupt.py:196,253`) ou a fila do
   freshhub podem gravar `vuupt_service_id` com o rascunho ainda no id negativo. Uma função única
   `adotar_servico(codigo, service_id)`, na mesma transação, reescreve o rascunho
   (cuidado com `UNIQUE(rascunho_id, service_id)`, `rascunhos_rota.py:125`) e é chamada por todo caminho
   que grava o id.
7. **Trava no banco contra envio duplo:** a Vuupt aceita `code` repetido (`vuupt_client.py:196-203`) e a
   materialização passa de um POST para ~5 chamadas por pedido; "enviar todas" é laço síncrono
   (`planejamento_rotas.py:305-313`). Trava por código/rascunho no SQLite, pois o incremento é outro processo.

**MÉDIO (entram nas fases 2/3, não deixam botão quebrado no corte):**
8. Fila do freshhub (`pipeline.py:1177-1200`) exige o id: com a chave ligada precisa usar o caminho antigo ou
   materializar.
9. Fonte única do payload: a materialização lê colunas do núcleo e não `dados_json` mesclado (`pedidos.py:116-120`);
   reagendar/editar endereço local escrevem nas colunas e valem na próxima rodada (proteção de `pedidos.py:229-231`).
10. Ações do pool que chamam a Vuupt com o id (`planejamento_rotas.py:2043, 2081, 2132, 2201, 2249,
    2309-2315, 2420`; `rascunhos_rota.py:1948`) entram na fase 2, não na 3.
11. **Portal entra junto com o corte** (`dados_cliente.py:416` lê not_assigned da Vuupt; `cancelamento.py:154`):
    senão o pedido some da lista do cliente.
12. Idempotência por código: serviço mais recente `done`/`canceled` devolve `pulado_atribuido` sem id
    (`vuupt_client.py:614-621`); definir o que a materialização faz (reentrega -R, abortar com motivo).
13. A sombra com o pipeline ainda criando serviço não exercita o caminho novo: o portão da fase 1 precisa
    também de rodadas em homologação/modo teste com a chave ligada.

**BAIXO:** valor novo de `fonte_pool` cai calado na Vuupt em processo sem restart (`pool.py:134`: logar);
`vincular_sem_service_id` com risco de 429; aviso de área não atendida repete quando o id troca
(`notificar_area_nao_atendida.py:200`); `criar_rotas_diarias.py:1179,1223` (criação direta de rota) também
é ponto de materialização.

**Alternativa levantada, decisão do Hugo:** materializar em segundo plano ao gravar o rascunho (16h), deixando
o envio só com as sobras. Tira o lote de chamadas do clique, mas o serviço passa a existir na Vuupt antes
do envio (contraria "só cria ao enviar").

## Decisões do Hugo (26/11)

- **Materializar só ao enviar** (opção a): o serviço na Vuupt é criado no clique de enviar/incrementar/adicionar
  parada em rota enviada. Nada de criar às 16h. Consequência: o envio precisa de trava no banco (item 7) e de
  progresso/tempo tratado no "Confirmar e enviar".
- **Portal e ações do pool entram na mesma entrega do corte** (itens 10 e 11 sobem para a fase 2). A chave só
  liga em produção com as duas prontas e provadas em homologação.

## Riscos a vigiar

- Envio parcial: rota criada na Vuupt com parte dos serviços (materializou 40 de 50 e caiu).
- Duplicar serviço (clique duplo no envio): a materialização precisa ser idempotente e travar por código.
- Dois "donos" do pedido na janela da virada (pedido com e sem serviço): comparar na fase 1.
- Cliente altera o pedido no portal depois de entrar no pool: precisa escrever no núcleo, não só na Vuupt.
