# Agendamento automático no próximo dia útil

Data: 02/11/2026 · Pedido do Hugo · Status: APROVADO pelo Hugo em 02/11
(corte das 14h: opção A; `expedition_date` do embarcador seguindo as regras)

## Por quê

Hoje o pedido sem agendamento de verdade sobe para a Vuupt sem data e entra
em qualquer rodada. Não dá para separar o que "devia sair hoje" do que está
atrasado. O Hugo quer que todo pedido tenha uma data prometida, gravada na
Vuupt (o cliente já vê no portal), para controlar o que sai em cada dia.

Isso reverte em parte a decisão de 29/07 ("só agenda quem exige
agendamento"). O problema de 29/07 era a janela 08h–16h colada em todo
pedido, apertando as rotas. Aqui a data vem com o horário do próprio
cliente, e quem não tem horário fica sem janela (dia todo).

## O que muda (decidido pelo Hugo)

1. **Pedido novo sem agendamento de verdade** ganha `scheduled_start/end` na
   Vuupt:
   - **data**: corte às **14h**. Pedido que subiu até 14h de um dia útil vai
     para o próximo dia útil; depois das 14h, ou em fim de semana/feriado,
     vai para o dia útil seguinte a esse. Ex.: segunda 13h → terça;
     segunda 15h → quarta; sábado → terça.
   - **horário**: o de recebimento do próprio destinatário.
   - **o corte vale de verdade** (Hugo, 02/11, opção A): pedido que sobe
     depois das 14h **não entra** nas rotas do dia seguinte (rascunho das 16h,
     incrementos das 18h e 22h); só sai no dia útil depois. A roteirização já
     faz isso sozinha: data agendada maior que a data da rota fica fora.
     Consequência aceita: os incrementos das 18h e 22h quase não pegam mais
     pedido novo, só sobra de antes das 14h, atrasado e data do cliente.
2. **Destinatário marcado com AGENDA** (BD_CLIENTES /
   `clientes_agendamento`): **nada muda**. Espera o cliente se posicionar
   (fluxo atual: horário da mensagem da Stokki, senão e-mail de solicitação
   e `aguardando_confirmacao`).
3. **Data informada pelo cliente** (portal, planilha, resposta de e-mail,
   `buscar_confirmacao`): **nada muda**, vale como veio.
4. **Pedido que não saiu na data** anda **sozinho para o próximo dia útil**.
5. **Gravar direto na Vuupt** (sem fase "só por dentro").
6. **Só pedidos que subirem a partir de 03/11/2026.** O pool de hoje fica como
   está: nem ganha data, nem anda.

## Não-objetivos

- Não mexe em quem é AGENDA, nem no e-mail de solicitação de agendamento.
- Não mexe em data informada pelo cliente nem em data posta pela equipe no
  Planejamento (origem EQUIPE).
- Não cria aviso novo ao cliente/embarcador quando a data é criada ou anda.
- A roteirização só muda onde a data automática não pode valer como
  agendamento do cliente (SEGURAR das rotas fracas, junção de nível 4). O
  filtro de elegibilidade fica como está (data agendada ≤ data_alvo entra,
  atrasado também entra, data futura fica fora): é ele que faz o corte das
  14h valer.

## Anti-objetivos (se acontecer, deu errado mesmo funcionando)

- **Pedido virar DEDICADO e o embarcador ser cobrado/avisado por causa da
  data automática.** `fora_dia_fixo.py` trata como "data do cliente" toda
  `scheduled_start` sem linha DIA_FIXO/EQUIPE em `agendamentos_origem`; data
  do cliente fora do dia de visita da região vira dedicado com frete da
  calculadora + aviso ao embarcador. A data automática **tem** de ser
  registrada como data do sistema.
- **Rotas apertadas de novo** por janela que o cliente nunca pediu (o erro de
  29/07). Sem horário cadastrado = dia todo (00:00–23:59, que a roteirização
  já ignora como janela: `JANELAS_PADRAO_IGNORADAS`).
- **Mover data de pedido que está em rota/rascunho** (inclusive rota ENVIADA
  sem motorista, que continua `not_assigned` na Vuupt — a fonte é
  `rascunhos_parada` + `rascunhos_rota`).
- **Data andando a cada rodada horária**: o pipeline reprocessa os
  `not_assigned` toda hora; a data só é calculada uma vez.
- Pedido AGENDA ou com data do cliente sendo tocado.

## Restrições e o que já existe

- **Onde entra a regra**: `pipeline.processar_pedido`, no bloco de agendamento
  (hoje `fonte_agendamento = "nao_exigido"` quando não há confirmação e o
  destinatário não é AGENDA). Vira uma fonte nova (ex.: `proximo_dia_util`).
- **Dia útil**: `regras/feriados.py` (`eh_dia_util`, `proximo_dia_util`,
  `somar_dias_uteis`). Sem operação em feriado nem fim de semana.
- **Dia fixo de região**: o pipeline já empurra a data para o dia de visita
  (`regioes_dia_fixo.ajustar_data_por_dia_fixo`, bloco "Regras de dia fixo"
  em `processar_pedido`) e avisa o remetente
  (`notificar_agendamentos_dia_fixo`). A data automática passa por esse mesmo
  ajuste. O `aplicar_regioes_dia_fixo` da roteirização só age em pedido **sem**
  data, então para esses pedidos novos ele deixa de agir — o ajuste do
  pipeline é que vale.
- **Origem da data**: `roteirizacao/registro_dia_fixo.py`, tabela
  `agendamentos_origem` (`ORIGENS_NAO_CLIENTE = (DIA_FIXO, EQUIPE)`). Ver
  anti-objetivo 1.
- **Horário do destinatário**: `regras/complexidade_entrega.horario_efetivo`
  (ajuste manual > planilha BD_CLIENTES > 00:00–23:59). É o mesmo que o portal
  mostra no "Pedir alteração".
- **Data de saída da Stokki** (`expedition_date`): hoje
  `calcular_data_entrega` usa como base quando é ≥ amanhã.
- **Importação**: `stokki-pipeline-horario` roda seg–sex 08h05–16h05 de hora
  em hora; as sequências das 16h, 18h e 22h também importam (`executar_tudo`
  / `pipeline.py`). A listagem da Stokki não traz a hora de criação do pedido.
- **Rodadas de rota** (conferido no código): 16h (`sequencia_tarde`,
  `criar_rotas_diarias --gerar-rascunho`, monta as rotas do próximo dia útil),
  18h e 22h (`incrementar_rotas`, aceita pedido que chegou até 22h,
  `HORA_CORTE_PEDIDO = 22`). Não há rodada de manhã; o pipeline das 08h–16h só
  importa. As rodadas leem o pool (`nucleo/pool.listar_pool_not_assigned`,
  `fonte_pool = freshhub` em produção desde 29/10, com queda pra Vuupt) e tiram
  pausados (`pedidos_pausados_cliente`) antes do dia fixo.
- **Quem mais lê `scheduled_start`** e precisa tratar a data automática como
  "sem agendamento do cliente": `rotas_fracas.motivo_nao_segurar` ("tem
  agendamento" impede o SEGURAR), `roteirizacao_dados._chave_nivel4` (nível 4
  do mesmo endereço só junta com a mesma data), `reagendar_feriado.py` (regrava
  como DIA_FIXO 08h–16h), `registro_dia_fixo.data_nao_e_do_cliente` (SQL com
  `origem IN (?, ?)` fixo). Contagens por `scheduled_start` em
  `dashboard_embarcadores/dashboard_dados.py` e `relatorio_operacional.py`
  mudam de número.
- `--modo-teste` não grava na Vuupt nem no banco.
- Mexe em `pipeline.py` e em rotina de lote: deploy sem restart de serviço;
  se a roteirização ganhar o passo de "andar a data", o painel importa
  `criar_rotas_diarias`? (o builder confere e reinicia `painel-agentes` se
  sim). MAPA_DO_SISTEMA.txt atualizado no mesmo commit.

## Chamadas minhas (Claude), não do Hugo — revisar

- **Hora em que o pedido "subiu"** = momento em que o pipeline cria o serviço
  na Vuupt (a listagem da Stokki não tem hora de criação). Se o detalhe do
  pedido na Stokki trouxer data/hora de criação, usar ela. Como a importação
  é de hora em hora, **a rodada das 14h05 ainda conta como antes do corte**
  (pega o que foi criado até 14h).
- **`expedition_date` (data de saída da Stokki)**: quem preenche é o
  embarcador, **mas obedecendo as regras de rotas e horários** (Hugo, 02/11).
  Leitura adotada: é a data mínima pedida pelo embarcador. Data final =
  a maior entre a do corte das 14h e a `expedition_date`, depois o ajuste de
  dia fixo da região e o horário do destinatário. Fica registrada com a
  origem automática: anda se não sair e nunca vira dedicado por estar fora do
  dia fixo.
- **Quando a data anda**: uma vez por dia, no começo da sequência das 16h,
  antes de montar o rascunho do dia seguinte. Pedido com data automática
  anterior ao próximo dia útil, ainda `not_assigned`, fora de rota/rascunho e
  não pausado, ganha o próximo dia útil (mesmo horário). Às 16h já
  não sai mais nada no dia (não há rodada de manhã), então é o momento em que
  "não saiu na data" fica certo. Se a sequência não rodar num dia (feriado),
  a rodada seguinte acerta tudo de uma vez, sem andar dia a dia.
- **Sem horário cadastrado = dia todo** (00:00–23:59), não 08h–16h.
- **Horário**: o da mensagem da Stokki do próprio pedido, quando houver;
  senão `horario_efetivo` (ajuste manual > planilha > dia todo). Quando a data
  anda, o horário fica o mesmo (no momento de andar não há mais a mensagem da
  Stokki; recalcular perderia o horário dela).
- **A data automática não conta como "tem agendamento"** no SEGURAR das rotas
  fracas nem na junção de nível 4 (mesmo endereço junta com qualquer data
  automática). Sem isso o SEGURAR para de agir e o caso KHAPPY/GPA volta.
- **`reagendar_feriado.py`** preserva a origem automática quando move a data.
- **Data prometida original guardada**: cada data automática gravada (a
  primeira e cada vez que anda) vira uma linha em `agendamentos_origem`
  (linhas nunca são apagadas), então dá para contar quantas vezes um pedido
  escorregou. Mostrar isso em tela (Torre/Planejamento) fica para depois.
- **Freshhub**: depois de gravar/andar a data, ressincronizar o núcleo como o
  dia fixo faz; a cópia do freshhub (:10/:40) leva até ~30 min. Como pedido
  com data vencida continua elegível, o atraso da cópia não tira ninguém de
  rota.
- **Reentregas (-R1/-R2) e duplicações da Torre** nascem fora do pipeline e
  ficam fora nesta versão (sem data automática).
- **Só a data automática anda.** Data do cliente, EQUIPE e DIA_FIXO de antes
  não andam.
- **Sem aviso** ao cliente/embarcador quando a data é criada ou anda; ele vê
  no portal. O aviso de dia fixo que já existe continua.
- **Retirada** já tem caminho próprio (serviço avulso) e fica fora.
  Terceiros/redespacho e Lalamove seguem a regra normal (Lalamove é decidido
  na roteirização, depois).
- **Marcação do que é "a partir de 03/11"**: só pedidos cujo serviço for
  criado na Vuupt a partir dessa data; serviço que já existia continua sem
  data.

## O que fica com o builder

- Nome da origem nova em `agendamentos_origem` (ex.: `AUTOMATICO`) ou reuso de
  DIA_FIXO — desde que entre em `ORIGENS_NAO_CLIENTE` e que "só a data
  automática anda" consiga distinguir.
- Onde exatamente fica o passo de "andar a data" (script/função chamado pela
  sequência das 16h antes do `criar_rotas_diarias --gerar-rascunho`) e como
  saber que o pedido está em rota/rascunho.
- Re-sincronizar o espelho do núcleo depois de gravar a data
  (`ressincronizar_ids`, como o dia fixo faz) para a rodada seguinte não ver
  a data antiga.
- Testes: unittest (`py -3.11`), sem pytest. Casos mínimos: corte 13h59/14h01,
  sexta depois das 14h, véspera de feriado, AGENDA intocado, data do cliente
  intocada, reprocessamento não mexe na data, origem registrada, pedido em
  rascunho não anda, pedido pausado não anda, pedido de depois das 14h fora
  do rascunho do dia seguinte, data andada entra no rascunho do mesmo dia,
  data automática fora do dia fixo não vira dedicado.
