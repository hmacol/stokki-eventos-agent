# Dias fixos v2: frequência por região, data fora do dia vira dedicado

Data: 03/10/2026. Pedido do Hugo. Estado: especificação para revisão, nada implementado.

## 1. Problema

Medição de 60 dias (04/08 a 02/10/2026, rotas executadas):

| Região | Dia fixo | Pedidos/semana | Rotas em 9 semanas | Rotas fracas | Dias com rota |
|---|---|---|---|---|---|
| Campinas | Qua | 9,2 | 33 | 91% | todos, sábado inclusive |
| Sorocaba | Ter | 2,9 | 9 | 78% | Ter a Sáb |
| Barueri | Ter/Qui | 14,4 | 39 | 72% | todos |
| Vale do Paraíba | Seg | 7,9 | 15 | 60% | todos |
| ABCD | Seg/Qua/Sex | 27,8 | 54 | 59% | todos os úteis |
| Baixada Santista | Ter | 5,4 | 11 | 36% | Seg a Qua |
| Piracicaba | Qua | 4,4 | 17 | 35% | Ter a Sáb |

- O dia fixo não é cumprido: a data informada pelo embarcador vence o dia fixo (regra do Hugo de 28/09) e cada data fora do dia abre uma rota quase vazia.
- A espera real nessas regiões já é de 4 a 7 dias (90% dos pedidos), acima dos 3 dias úteis.
- Transfrios tem dia fixo Seg/Qua, mas a maioria das entregas cai em Ter/Qui.
- Endereço com cidade truncada ("SAO JOSE DOS CA - SP") escapa do dia fixo.
- O sistema não registra se o agendamento de um pedido veio do dia fixo ou do embarcador.

## 2. Decisões do Hugo (03/10)

| Tema | Decisão |
|---|---|
| Vale do Paraíba (Suzano, Mogi, Biritiba, Jacareí, São José dos Campos) | Semanal, segunda, como hoje. Alto Tietê fica no Vale mesmo com rota mais longa |
| ABCD | 2x por semana: segunda e quinta (hoje Seg/Qua/Sex) |
| Barueri | Terça e quinta (como hoje) |
| Campinas, Baixada Santista, Piracicaba | Semanal (Qua, Ter, Qua), como hoje |
| Sorocaba | Quinzenal, terça. Primeira visita na terça seguinte ao deploy |
| Prazo de entrega | 3 dias úteis na diária e nas internas (Barueri, ABCD); até 7 dias corridos na semanal; até 15 dias corridos na quinzenal |
| Comunicação | Informativo aos embarcadores, em PDF |
| Data do cliente fora do dia da região | A data do cliente manda. O pedido é marcado como dedicado, sai da rota compartilhada, valor pela calculadora de frete dedicado, e o embarcador é avisado |
| Canais do aviso | E-mail, portal do cliente, aviso no envio do pedido pelo portal, WhatsApp |

## 3. Regras de região

### 3.1 Configuração (`roteirizacao/regioes_dia_fixo.py`)

- ABCD: `dias = [SEGUNDA, QUINTA]`.
- Transfrios (regra por endereço): `dias = [TERCA, QUINTA]`.
- Sorocaba: campo novo `frequencia = "quinzenal"` e `ancora = "AAAA-MM-DD"` (primeira terça de visita, gravada no deploy). Regiões sem o campo são semanais (comportamento atual).
- Campo novo `prazo_dias` derivado do nível, para mensagens e para o vigia: interna/diária = 3 dias úteis; semanal = 7 corridos; quinzenal = 15 corridos.

### 3.2 Data de visita

`proxima_data_dias_semana` passa a respeitar a frequência: para região quinzenal, só valem as datas no dia da semana cujo número de semanas desde a âncora é par. Uma função `data_valida_na_regiao(regra, data) -> bool` decide se uma data é dia de visita (dia da semana certo e, se quinzenal, semana certa). Usada pelo agendamento automático, pela detecção de data fora do dia e pelo aviso do Planejamento (`_aviso_dia_fixo`).

### 3.3 Cidade truncada

`extrair_cidade`/índice de cidades passa a reconhecer nome truncado: se o nome lido do endereço tem pelo menos 10 letras e é prefixo de exatamente uma cidade cadastrada, vale essa cidade. Teste com "SAO JOSE DOS CA".

## 4. Origem do agendamento

Tabela nova `agendamentos_dia_fixo` (código base, service_id, data gravada, gravado_em): `aplicar_regioes_dia_fixo` registra cada scheduled_start que ele mesmo grava. Agendamento de um pedido cuja data não está nessa tabela é considerado informado pelo embarcador.

## 5. Data do embarcador fora do dia da região

### 5.1 Detecção

Roda onde o dia fixo já é aplicado: `criar_rotas_diarias.main` (18h) e `incrementar_rotas.main`, antes de separar dedicados. Para cada pedido do pool com scheduled_start informado pelo embarcador (seção 4), região de dia fixo (`regra_dia_fixo_do_servico`) e data que não é dia de visita (`data_valida_na_regiao`):

1. Marca como dedicado em `pedidos_dedicados` (`marcar`, `por = "automatico: fora do dia fixo"`), com o valor calculado pela calculadora de frete dedicado (`portal_cliente/cotacao.calcular`, km da base ao destino com retorno, caixas do pedido, tipo de carga do embarcador). Se o cálculo falhar (sem coordenada, sem tabela), marca com valor 0 e o motivo "valor pendente" para o financeiro corrigir; o pedido sai da rota do mesmo jeito.
2. Pedido já dedicado ativo: não remarca (não altera valor nem data de marcação).
3. A partir daí o fluxo de dedicado que já existe vale: sai da rota compartilhada, chip "Dedicado · R$ X" no pool, chip "Envio dedicado" no portal, entra no e-mail quinzenal do financeiro.

Reentrega e pedidos com nível 4/veículo grande seguem a mesma regra (a data do cliente é o critério).

### 5.2 Avisos ao embarcador (um por pedido)

Tabela de controle `avisos_fora_dia_fixo` (código, enviado_em, canais) garante um aviso por pedido.

- E-mail: texto curto — data escolhida, dias de visita da região, que o pedido será tratado como envio dedicado com o valor estimado, e como pedir outra data. Segue as chaves já usadas pelos avisos de dia fixo (`notificacoes_automaticas`, `forcar_destino`); enquanto o redirecionamento estiver ligado, vai para hugo@.
- WhatsApp: mesma mensagem em até 200 caracteres pelo canal de WhatsApp ao cliente que já existe (respeita `forcar_destino` e o teto diário dos clientes).
- Portal: o chip "Envio dedicado" já existe; acrescentar o texto "data fora do dia de visita da região" no detalhe do pedido.
- Envio do pedido pelo portal: quando o embarcador escolhe uma data (máscara de envio XML e planilha) e o destino é de região de dia fixo fora do dia de visita, aviso antes de confirmar, com os dias de visita e a observação de dedicado. O embarcador pode trocar a data ou confirmar assim mesmo.

### 5.3 Planejamento

O aviso atual "Americana: só Quartas" do botão Roteirizar continua. Pedido marcado por esta regra mostra no chip de dedicado o motivo "fora do dia fixo".

### 5.4 Reagendamento pela equipe fora do dia

Decisão do Hugo (03/10): data posta pela EQUIPE não vira dedicado sozinha; só a do cliente (5.1). A origem de cada data gravada pelo sistema fica registrada (dia fixo ou equipe); edição direta na tela da Vuupt não dá pra distinguir e conta como do cliente.

Quando um usuário interno reagenda no Planejamento (um pedido, `reagendar_pedido`, ou lote, `reagendar_pedidos`) para uma data que não é dia de visita da região do pedido (`data_valida_na_regiao`), a tela abre uma pergunta ANTES de salvar: "Este pedido é de <região>, com visita só às <dias>. Marcar como dedicado?"

- **Sim:** abre o modal de dedicado que já existe, com os pedidos e o valor da calculadora de frete dedicado (mesma conta da 5.1, editável; sem valor calculado, o campo abre vazio). A data é salva e o pedido é marcado como dedicado (sai da rota compartilhada).
- **Não:** a data é salva normalmente, registrada como da equipe (não do cliente), e o pedido segue na roteirização.
- **Lote:** uma pergunta só, listando apenas os pedidos fora do dia; "sim" vale para eles e os demais são reagendados normalmente.
- Não vale para o botão Roteirizar nem para adicionar à rota (o aviso "Americana: só Quartas" continua).

## 6. Prazo por nível

- `vigia/regras.py`: estado AGENDADO continua sem prazo. NO_POOL de pedido de região semanal/quinzenal sem agendamento usa o prazo do nível (7 ou 15 dias corridos), não 1 dia útil.
- Rotas fracas: o segurar continua só na Grande SP (pedido de dia fixo nunca é segurado; regra atual).

## 7. Informativo aos embarcadores (PDF)

Documento de 1 a 2 páginas, gerado pelo sistema a partir da configuração de regiões (para não desatualizar), com:
- tabela de regiões, cidades, dias de visita, frequência e prazo;
- como funciona a data escolhida pelo embarcador e o tratamento como dedicado;
- contato.
Script `gerar_informativo_regioes.py` grava o PDF em `dados/informativos/`. O envio aos embarcadores é decisão e ação do Hugo (nada é enviado automaticamente nesta entrega).

## 8. Testes

- `data_valida_na_regiao` para semanal, 2x/semana e quinzenal (âncora, semana par/ímpar, virada de mês).
- Agendamento automático de Sorocaba cai só em terça de semana válida.
- Cidade truncada reconhecida; prefixo ambíguo não reconhecido.
- Detecção: data do embarcador fora do dia marca dedicado com valor da calculadora; data no dia não marca; agendamento do próprio dia fixo não marca; já dedicado não remarca; falha da calculadora marca com valor 0 e motivo.
- Aviso: um por pedido, com `forcar_destino` respeitado; WhatsApp até 200 caracteres.
- Portal: aviso no envio quando a data escolhida está fora do dia.
- Reagendamento pela equipe: data da equipe não marca dedicado sozinha; a checagem devolve só os pedidos fora do dia, com o valor da calculadora (nulo quando falha); só quem pode reagendar consegue consultar.
- Vigia: prazo de 7/15 dias para regiões semanal/quinzenal.
- PDF gerado contém todas as regiões.

## 9. Entrada em produção

1. Replay de 31 dias com a nova configuração (ABCD 2x, Transfrios, Sorocaba quinzenal) para medir rotas e rotas fracas. Limitação: o replay usa os pedidos enviados, não simula a marcação como dedicado.
2. Deploy com a marcação de dedicado e os avisos ligados, avisos externos redirecionados para hugo@ (`forcar_destino`) até o Hugo liberar.
3. Gerar o PDF e entregar ao Hugo.

## 10. Riscos

- Mais pedidos dedicados: cada data fora do dia vira transporte dedicado a organizar. Medição: hoje Campinas teve rotas em todos os dias da semana; parte disso passa a ser dedicado.
- Valor da calculadora pode não refletir o custo real de encaixar o pedido numa rota próxima; o financeiro corrige.
- ABCD em 2 dias aumenta o volume por dia (~14 pedidos), mais perto do teto de 9h por rota.

## 11. Fora do escopo

- Envio automático do PDF aos embarcadores.
- Novas regiões para cidades de viagem espalhadas (demanda pulverizada, 24 cidades e 42 pedidos em 60 dias).
- Cobrança efetiva do dedicado (segue o fluxo financeiro atual).
