# Devolução automática de estoque no cancelamento (Stokki)

Especificação de 12/11/2026, a partir da conversa com o Hugo. Ainda não implementada.

## Por quê

Pedido de saída cancelado **depois da separação** deixa o saldo preso no endereço
**Virtual** da Stokki. O cancelamento não solta sozinho: hoje alguém da equipe faz o
"Desbloqueio | Interno" à mão. Enquanto isso o embarcador não consegue usar o saldo.

O Hugo quer que a devolução aconteça **no momento do cancelamento**, porque muitas vezes
o cliente cancela justamente para lançar outro pedido com o mesmo produto.

## O que o Hugo decidiu

1. Cancelou, o produto volta sozinho para um endereço físico com saldo disponível.
2. Destino: **de onde saiu**, ou **a posição onde há mais produto do embarcador**.
3. É **na hora do cancelamento**, não em lote num horário combinado.
4. O login na conta wms@ **entra na fila de execução normal** (`stokki/sessao_uso.py`):
   espera a vez, nunca derruba a Estação de Impressão, o Cross ou a equipe.
5. Vale para os **três caminhos** que cancelam na Stokki:
   - cliente cancela no portal (`portal_cliente/enviar_stokki.processar_cancelamentos_do_ciclo`);
   - equipe conclui uma solicitação de cancelamento (`portal_cliente/solicitacoes_execucao.py`);
   - botão "Cancelar na Stokki" da tela /pedidos (`painel_agentes/acoes_pedidos.cancelar_stokki`).

## Objetivo

Depois de um cancelamento confirmado na Stokki por qualquer um dos três caminhos, a
quantidade que aquele pedido bloqueou sai do Virtual e fica **disponível** num endereço
físico, sem ação humana, em minutos.

## Fora do escopo

- Pedido cancelado direto na tela da Stokki (o robô não fica sabendo).
- Varrer cancelados antigos que já estão presos no Virtual.
- Tela nova. O resultado aparece em log, na tabela da fila, no aviso ao galpão e no
  aviso de falha.
- Recriar o pedido novo do cliente. Só o saldo é liberado.

## O que seria um fracasso mesmo "funcionando"

- Mover do Virtual saldo que é de **outro pedido** (o Virtual é compartilhado).
- Devolver duas vezes o mesmo pedido.
- Derrubar a sessão de alguém na Stokki pra ganhar tempo.
- Desfazer ou travar o cancelamento porque a devolução falhou.
- Usar Ajuste Manual (saída + entrada) pra mover: distorce o histórico do embarcador.
  O caminho é a Movimentação de Estoque Manual, que gera "Desbloqueio | Interno".

## Regras

### Quanto devolver

Só o que **este pedido** bloqueou, por produto. A fonte é a movimentação do produto
(`GET /administrator/inventory/v2/moviment/table`): linha `Bloqueio | Interno` com
referência `#PS-<id>`. Quantidade em unidade base (3 cx de 10 kg = 30), que é a mesma
unidade da movimentação de devolução. Bloqueio com referência `#PE-` é recebimento e
não conta.

Pedido sem linha de Bloqueio (cancelado antes da separação) não tem nada preso: a
devolução termina como "nada a devolver", sem login na wms@.

### Para onde

Por produto, nesta ordem:

1. **Endereço de origem** da separação. Fonte provável: "movimentos entre endereçamentos"
   (`/administrator/inventory/moviment/addressing/moviment/<product_id>/1`, dados em
   `.../addressing/table/moviment/table`), colunas Localização inicial e Localização
   destino. **Provado em 12/11** (amostras em `stokki/amostras_devolucao/`, ver `LEIA.txt`): a separação
   aparece ali como linha `origem -> Virtual`. PS-43101: produto 2278 = `#A-110830 | 03/11/2026 11:44 |
   Carla | FABENNE-1 -> Virtual | 4` (produto 2282 = `#A-110831`, mesmo minuto). A hora é a de "Em
   Conferência" no histórico do pedido (11:44), e o Bloqueio de 12:12 na tela de movimentos é o
   cancelamento. A linha não traz o número do pedido: casar por produto + hora + quantidade +
   usuário. Se a separação tirou de mais de um endereço,
   cada parte volta para o seu.
2. Sem origem conhecida: **endereço físico com mais estoque daquele produto**
   (`/administrator/inventory/moviment/addressing/<product_id>/1`: Código de
   Localização | Estoque | Bloqueado | Disponível).
3. Produto sem saldo em endereço físico nenhum: **endereço físico com mais estoque do
   embarcador** (`stokki/ajuste_estoque.saldo_por_endereco`).
4. Nada disso: falha desse item, com aviso.

Endereços de sistema (Virtual, Falta, Sobra, Ajuste) nunca são destino. Se a Stokki
recusar o destino escolhido, tenta a regra seguinte.

Se a etapa 1 provar que a origem não é legível, a regra passa a começar na 2 e isso
deve ser dito ao Hugo na entrega.

### Como mover

Movimentação de Estoque Manual da área provider (conta wms@, Playwright, como
`stokki/estacao_impressao.py` e `stokki/avancar_cross.py`):
`POST /provider/operation/inventory/adjust/store/process` (client_id,
origin_location_id = Virtual, product_id, quantity_available, quantity,
destination_location_id, damaged=0, reason) → `adjust_id`; poll
`adjust/processed/:id` (já levou ~20 s). `adjust/location/search/<cliente>/<código>`
dá o id do endereço; `adjust/product/search/<cliente>/<id do Virtual>/<sku>` dá
`product.quantity`, o quanto a Stokki deixa tirar do Virtual.

Provado à mão em 03/11 no PS-43101 (#110832, #110833). O script daquela vez não está
no repositório. Detalhes dos endpoints: memória `reference_stokki_inventario_ajuste_endpoints`
e `reference_stokki_cancelado_apos_separacao`.

Motivo gravado na Stokki: `Retorno ao estoque do PS-<id> cancelado`.

### Não mexer no saldo dos outros

- Nunca mover mais que o Bloqueio do pedido, nem mais que o `product.quantity` que a
  tela oferece. Se a tela oferecer menos, move o que der e trata como parcial (aviso).
- O Desbloqueio feito à mão **não traz referência do pedido**, e o recebimento também gera
  `Desbloqueio | Interno` sem referência: por isso a prova de "já devolvido" NÃO é a tabela
  de movimentações. Prova = linha `Virtual -> endereço físico` do produto em "movimentos
  entre endereçamentos", criada a partir do cancelamento (mesmo minuto conta) até agora.
  Quantidade igual ou soma das linhas igual ao Bloqueio = já devolvido (não move);
  linha que existe mas não fecha, ou não concluída = dúvida (avisa). Devoluções feitas pelo
  próprio robô para OUTROS pedidos são reconhecidas pelo id do movimento (`#A-<n>` = `adjust_id`)
  e não contam como prova. *(decisão minha, não do
  Hugo; ajustado em 13/11 na revisão: recebimento também gera Desbloqueio sem referência)*
- Na dúvida, **não mover e avisar**: movimento `Virtual -> endereço físico` depois do
  cancelamento que não fecha com a quantidade do pedido, ou ainda não concluído, ou
  qualquer leitura que não feche (data ilegível, coluna faltando, paginação que não
  fecha), vira aviso à operação em vez de movimentação. Saldo preso com aviso é
  melhor que saldo de outro pedido movido. *(decisão minha)*
- "Já estava cancelado" entra na fila, mas só é devolvido se o cancelamento for
  recente: ler a data no "Histórico do pedido" do show; cancelado há mais de 24 h
  não é movido (registra "cancelamento antigo", sem aviso). Cancelados antigos
  seguem fora do escopo. *(decisão minha)*
- Pedido do fluxo Cross não tem tratamento próprio: sem Bloqueio com a referência
  do PS, nada a devolver; com Bloqueio, vale a mesma regra. *(decisão minha)*
- Um pedido só entra na fila uma vez (chave única pelo id da Stokki). Cada item
  movido grava o `adjust_id` antes de seguir para o próximo, para uma nova tentativa
  não repetir o que já foi.

### Quando e por quem

*(desenho meu; o Hugo decidiu "na hora" e "pela fila normal")*

- Os três caminhos, logo depois do `cancelar_pedido` devolver ok (inclusive "já
  estava cancelado"), só **registram o pedido numa fila** no `dados/dados.db`. Não
  fazem a devolução ali: dois deles rodam em thread do painel, onde a fila da Stokki
  não espera (falha na hora com "Stokki em uso").
- Quem executa é o **worker do portal** (`portal_cliente/enviar_stokki.py`, ciclo de
  20 s, thread principal, já usa Playwright e a trava). A cada ciclo, se houver
  pendente, pega a vez nas contas e faz a devolução. Com a conta ocupada, não fica
  bloqueando o envio de pedidos dos clientes: espera pouco e tenta de novo no ciclo
  seguinte.
- Consequência aceita pelo Hugo: com o pipeline horário ou a Estação de Impressão
  usando a Stokki, a devolução atrasa (em 12/11 uma leitura esperou mais de 30 min
  atrás do pipeline). Fora disso, minutos.

### Aviso ao galpão (decisão do Hugo, 12/11)

O robô só move o saldo no sistema; a caixa continua na área de expedição. A cada
devolução feita (inteira ou parcial), mandar mensagem no grupo de WhatsApp
**❌PEDIDOS CANCELADOS❌**, uma por pedido, dizendo o PS, o embarcador e, por item:
produto (SKU e nome), quantidade e **endereço onde guardar**.

- Envio por `notificar_whatsapp.py` (OpenWA), no padrão dos outros grupos, com chave
  nova `whatsapp_notificacoes.grupo_cancelados_id`. O grupo ainda não está no
  código: o número do OpenWA precisa estar nele e o id entra no config da VPS (tarefa
  do Hugo). Sem o id, a devolução acontece igual e o log registra que o aviso não saiu.
- "Nada a devolver", "já devolvido" e "cancelamento antigo" não geram mensagem.
- O Hugo aprovou a regra da dúvida (não mover e avisar a operação).

### Se der errado

- O cancelamento **continua valendo** em qualquer caso.
- Erro técnico: tenta de novo nos ciclos seguintes, até 3 vezes.
- Esgotou, ficou parcial ou sem destino: aviso à operação pelo mesmo canal dos avisos
  de cancelamento (`portal_cliente/cancelamento.avisar_operacao`), dizendo o PS, o
  produto, a quantidade que ficou no Virtual e o erro.

### Chave

`stokki.devolucao_cancelamento.ativo` no `config.yaml`, **desligada por padrão**.
Desligada: nada entra na fila e nada roda. Ligar em produção é decisão do Hugo, depois
da prova. `--simular` do worker e `--modo-teste` só registram o plano (pedido, itens,
quantidade, destino escolhido), sem mover. Na homologação fica desligada (lá nada fala
com a Stokki).

## Restrições do projeto

- Stokki só pelas sessões prontas (`StokkiSession`, login Playwright da provider) e
  sempre sob `sessao_uso`. Não chutar URL: redireciona pro login e derruba sessões.
- Operações de inventário ficam na área `/provider/` (conta wms@); as leituras acima
  são da área `/administrator` (conta principal). Se a área provider tiver leitura
  equivalente, preferir uma conta só.
- Conferido no código em 12/11: a wms@ não entra em `/administrator`
  (`stokki/contas.py`), então as leituras usam a conta principal/reserva (o worker
  já pega com `DONO_TRAVA`) e a movimentação usa a provider, que tem trava própria.
  Para "esperar pouco e tentar no próximo ciclo" use
  `sessao_uso.adquirir(..., conta=contas.PROVIDER, esperar_segundos=<curto>)`; o
  decorador `na_vez_da_provider` espera até 2 h e travaria o worker.
- Ganchos de registro na fila: `painel_agentes/acoes_pedidos.cancelar_stokki` (após
  `r.get("ok")`), o `cancelar_stokki` de `solicitacoes_execucao.dependencias_reais`
  e `portal_cliente/cancelamento._processar_um` (após `res.get("ok")`). Painel e
  worker compartilham o `dados/dados.db`. No worker, a devolução é mais um bloco
  isolado em `try/except` dentro de `enviar_stokki.ciclo`.
- Roda na VPS como www-data. Nada disso roda no notebook.
- Testes em `unittest`, com sessão falsa, no padrão de `stokki/test_cancelar.py` e
  `portal_cliente/test_cancelamento.py`. A escolha de destino e a decisão "já
  devolvido / quanto mover" são funções puras, testadas sem Stokki.
- Atualizar `MAPA_DO_SISTEMA.txt` (arquivo novo, tabela nova) e a skill
  `operar-stokki` (sai de "Não sabemos fazer" o "soltar a reserva de pedido cancelado").

## Fica com quem constrói

- Nome e colunas da tabela da fila; nome do módulo (sugestão: `stokki/devolucao_estoque.py`).
- Parâmetros do DataTables de `addressing/table/moviment/table`.
- Como achar o `product_id` de cada item do pedido (a aba "Itens do pedido" do show
  tem coluna ID e SKU).
- Em que meses procurar o Bloqueio (mês do pedido até o mês atual).
- Tempo de espera pela conta em cada ciclo do worker.
- Registrar ou não um evento na tratativa do pedido na /pedidos.

## Prova antes de ligar

1. Leitura: origem do PS-43101 confirmada (ou descartada) na Stokki.
2. Simulação na VPS com a chave desligada: plano coerente para um cancelado real.
3. Um caso real acompanhado: pedido do cliente teste separado e cancelado, saldo
   disponível no destino certo, "Desbloqueio | Interno" na movimentação, Virtual dos
   outros pedidos intacto.
4. Só então o Hugo liga a chave.
