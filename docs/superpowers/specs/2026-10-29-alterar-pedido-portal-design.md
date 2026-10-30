# Cliente altera endereço, data e horário do pedido pelo portal

Data: 29/10/2026 · Pedido do Hugo · Status: rascunho para aprovação

## 1. Por quê

Hoje a mudança de endereço, data ou horário de entrega chega por e-mail ou
pela própria Stokki, e o planejamento aplica tudo à mão na Vuupt. O Hugo quer
que o cliente (embarcador) faça isso sozinho no portal e que a mudança chegue
na Vuupt sem passar pela equipe.

## 2. O que já existe (não refazer)

- **Na importação pelo portal** (prévia do envio por XML/planilha) o cliente
  já informa, por pedido, o horário de recebimento do destinatário e a
  data/janela de agendamento. O **endereço de entrega** entrou em 27/10
  (`portal_cliente/endereco_entrega.py`, spec
  `2026-10-27-endereco-entrega-portal-design.md`), mesclado na master local,
  sem deploy, chave `portal_cliente.envios.endereco_entrega_ativo` desligada.
  A parte "passo na importação" do pedido do Hugo é essa entrega; este
  documento cobre o que vem **depois** da importação.
- **Reagendar no portal** (`envio_pedidos.aplicar_acao`, tipo `reagendar`):
  só para pedidos que entraram pelo portal (`portal_envios`), só data/janela.
- **Tirar da rota sem cancelar**: `solicitacoes_execucao._tirar_da_rota` /
  `_tirar_da_rota_real` (usado pelo Em espera, provado em 28/10).
- **Gravar endereço no serviço da Vuupt**: `planejamento_rotas._gravar_endereco_pedido`
  (PUT `/services/{id}` com address/lat/long na raiz + contato best-effort +
  cópia do rascunho + ressincroniza núcleo). Geocodificação:
  `geocodificacao.geocodificar`.
- **Horário de recebimento do destinatário**: `envio_pedidos.gravar_destinatario`
  já grava em `portal_destinatarios` (por embarcador) **e** em
  `regras.complexidade_entrega.definir_ajuste_manual` (por CNPJ/CPF, vale pra
  todos), que a roteirização lê por `horario_efetivo`.
- **Regra de quem pode mexer sozinho** (rota iniciada, entregue, insucesso,
  Lalamove → operação): `cancelamento.decidir`, lendo `nucleo_pedidos` /
  `nucleo_rotas`.
- **Data do cliente x dia fixo**: `envio_pedidos.registrar_agendamento_pedido`
  grava como DO CLIENTE; data fora do dia de visita vira dedicado
  (`avisos_fora_dia_fixo` já avisa na importação).

## 3. O que o Hugo decidiu

1. O cliente altera pelo portal **endereço de entrega, data de entrega e
   horário de recebimento**.
2. Vale para **todos os pedidos do cliente**, inclusive os lançados direto na
   Stokki (não só os que entraram pelo portal).
3. **Só na Vuupt.** A Stokki não permite editar pedido de saída; ela fica com
   os dados da NF. (Consequência aceita: etiqueta/romaneio da Stokki saem com
   o endereço antigo; o motorista segue a Vuupt.)
4. **O que decide é o pedido estar numa rota já enviada, não o relógio**
   (29/10, depois da revisão). Pedido fora de rota: a alteração entra sem
   aviso, a qualquer hora. Pedido numa rota enviada e ainda não iniciada: o
   portal avisa que a alteração pode atrasar a entrega em 1 dia; se o
   cliente confirmar, o pedido **sai da rota (sem cancelar) e volta pro
   pool** com os dados novos, entrando na próxima roteirização. As "16h" só
   aparecem no texto do portal, como explicação ("as rotas do dia seguinte
   são fechadas às 16h"). Assim sexta → segunda e véspera de feriado não
   precisam de regra própria.
5. **Endereço novo em outra região**: depois da troca, a regra do dia fixo é
   aplicada de novo. Se a data do pedido ficar fora do dia de visita da
   região nova, o cliente vê o mesmo aviso de dedicado da importação antes
   de confirmar. Faixa de frete por endereço fica fora deste projeto.
6. **Horário = horário de recebimento do destinatário**, que passa a valer
   dali em diante, e **para todos os embarcadores** que entregam naquele
   destinatário (é por CNPJ/CPF do destinatário).
7. Sugestões aceitas pelo Hugo em 29/10: itens 4 e 5 acima.

## 4. Como funciona para o cliente

Na tela de acompanhamento do portal (lista de pedidos do dia, próximos dias e
sem rota, montada por `dados_cliente.py` a partir da Vuupt), cada pedido
alterável ganha o botão **"Alterar entrega"**. Ele abre uma janela com três
blocos, todos opcionais (o cliente muda só o que quiser):

- **Endereço de entrega**: mesmo formulário de CEP + número + complemento da
  prévia do envio (`endereco_entrega.montar_endereco`). Vale só para aquele
  pedido.
- **Data de entrega**: a partir do próximo dia útil de entrega. Se cair fora
  do dia fixo da região, mostra o mesmo aviso de dedicado da importação.
- **Horário de recebimento**: início e fim. O texto deixa claro que vale para
  todos os próximos pedidos desse destinatário.

Antes de gravar, um resumo "antes → depois". Se o pedido estiver numa rota
enviada e a alteração for de endereço ou data, o resumo mostra o aviso:
*"As rotas do dia seguinte são fechadas às 16h e esse pedido já está numa
rota. Para alterar, ele sai da rota e volta para a programação, e a entrega
pode atrasar 1 dia."* com o botão "Alterar mesmo assim". Se a data (ou o
endereço novo) cair fora do dia fixo, aparece também o aviso de dedicado.

Pedido que **não** pode ser alterado mostra o motivo no lugar do botão e
orienta a falar com a Fresh Log (chat do portal):
rota já iniciada (motorista na rua), entregue, insucesso, cancelado, rota
Lalamove, retirada/terceiros (transportadora).

## 5. O que acontece por trás

Ordem de cada alteração confirmada:

1. **Validar dono**: o serviço da Vuupt tem que ser de um dos `sender_ids` do
   login (cuidado com grupo econômico). Mesma permissão do Reagendar
   (`_exige_pode_enviar`).
2. **Decidir** com a regra do `cancelamento.decidir` (adaptada): pode
   sozinho / precisa da operação / precisa sair da rota.
3. **Sair da rota**, se for o caso (endereço ou data mudou e o pedido está
   numa rota enviada, não iniciada): reaproveitar `_tirar_da_rota_real`.
   Falhou → nada mais é aplicado e o cliente vê o erro. Mudança só de
   horário não tira de rota.
4. **Aplicar na Vuupt**:
   - endereço → `_gravar_endereco_pedido` (geocodifica; atualiza rascunho se
     estiver em um) e reaplica a regra do dia fixo com o endereço novo;
   - data → PUT `/services/{id}` com `scheduled_start/end` + registro como
     data DO CLIENTE (`registrar_agendamento_pedido`). A janela do dia é o
     horário de recebimento efetivo do destinatário (`horario_efetivo`), ou
     08:00–18:00 se não houver;
   - horário → `gravar_destinatario` (portal + ajuste manual global, que
     vale para todos os embarcadores) e `operating_hour_start/end` do
     contato na Vuupt. **Não** grava `scheduled_*` em serviço nenhum: criar
     data em pedido que não tinha conta como data do cliente e pode virar
     dedicado sem ninguém pedir. A roteirização já lê o ajuste manual.
5. **Registrar** a alteração (quem, quando, antes/depois) numa tabela nova,
   visível no histórico do pedido no portal e na `/solicitacoes-portal`.
6. **Avisar a equipe** só quando o pedido saiu de uma rota (e-mail de
   atendimento, mesmo padrão de `_avisar_operacao_solicitacao`).

### Proteções obrigatórias

- **A importação de hora em hora não pode desfazer.** O `pipeline.py`
  reenvia o serviço com o endereço da NF quando o conteúdo vindo da Stokki
  muda (`criar_ou_atualizar_servico` + fingerprint). O endereço alterado pelo
  cliente tem que ser guardado por código do pedido e vencer o da NF em
  toda montagem do payload. Mesmo cuidado com a data (já protegida pelo
  registro DO CLIENTE) e com o `operating_hour` do contato, que hoje é
  reescrito pela extração do pipeline (o ajuste manual tem que vencer).
- **Próxima roteirização vê a alteração, seja qual for `planejamento.fonte_pool`.**
  Com `freshhub`, o pool vem da carga do legado no sistema novo (a cada
  ~30 min). Uma alteração às 15h50, ou um pedido recém-tirado da rota, não
  pode entrar na rodada das 16h com os dados antigos: o construtor garante
  isso (forçar a atualização do pedido no freshhub, ou outro meio) e prova
  na seção 7.
- **Endereço fora da área atendida** (raio/bloqueio de área): não aplica;
  o portal diz que a região não é atendida e manda falar com a Fresh Log.
- **Rascunho de rota**: pedido com data alterada sai do rascunho; com
  endereço alterado, a parada do rascunho é atualizada no lugar.

## 6. Fora do escopo

- Qualquer alteração na Stokki.
- Alterar em lote (vários pedidos de uma vez).
- Alterar itens, quantidades, NF ou destinatário.
- Portal do freshhub (1.3): vai precisar da mesma regra antes da virada, em
  outro projeto.

## 7. Ligar com segurança

- Chave nova `portal_cliente.alteracao_entrega_ativo`, desligada por padrão;
  liberar primeiro só para o cliente teste
  (CNPJ 00.000.000/0001-91), depois para todos. Ligar é decisão do Hugo.
- Prova antes de ligar: pedido fictício do cliente teste (código ≤ 7
  dígitos), alterar endereço/data/horário fora de rota e dentro de rota
  enviada e conferir na Vuupt; rodar a importação horária em cima e ver que
  não desfez; conferir que o pedido tirado da rota aparece no pool da
  próxima rodada com os dados novos (na `fonte_pool` ativa na VPS).

## 8. Chamadas minhas (não decididas pelo Hugo, conferir)

- "Rota enviada" = serviço atribuído a uma rota na Vuupt. Parada num
  rascunho local ainda não enviado não conta como rota: endereço atualiza
  a parada; data tira o pedido do rascunho, sem aviso.
- Mudar só o horário de recebimento não tira pedido de rota já montada
  (vale dali em diante).
- Mudar só a data usa como janela o horário de recebimento efetivo do
  destinatário (08:00–18:00 se não houver).
- Horário grava no ajuste manual + contato da Vuupt, nunca em `scheduled_*`.
- Rota já iniciada, entregue, Lalamove e retirada/terceiros **bloqueiam** a
  alteração (vai pra operação), igual ao cancelamento.
- O botão novo substitui o "Reagendar" para pedidos que já estão na Vuupt; o
  "Reagendar" atual continua só para pedido do portal ainda não criado
  (na fila, erro, aguardando liberação).
- Endereço novo vale **só para aquele pedido**; a memória de endereços da
  importação (caso 4 do spec de 27/10) aprende sozinha, porque lê o que valeu
  na Vuupt.
- Equipe é avisada **só** quando o pedido sai de uma rota.
