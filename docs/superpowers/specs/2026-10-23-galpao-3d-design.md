# Galpão 3D: a operação inteira como um jogo

Data: 23/10/2026 · Pedido do Hugo · Status: design para aprovação

## 1. Intenção

O Hugo quer **olhar a operação inteira de uma vez e decidir pela visão**. A tela
mostra o galpão em 3D, com cara de The Sims e bonecos Lego. Ali se vê o que cada
pessoa está fazendo, em que etapa está cada pedido, o que está pronto para a
etapa seguinte e onde está o gargalo. Do lado de fora do galpão fica a rua: os
carros que saíram, as entregas acontecendo e o que volta.

O lúdico faz parte do pedido. Uma tabela com os mesmos números não atende.
O jogo serve para decidir: o desenho tem de deixar claro, num olhar, onde está
acumulando e quem está parado.

**Quem usa:** por enquanto só o Hugo.

## 2. Objetivos

1. Uma cena 3D do galpão com as áreas, as esteiras e a equipe reais (seção 4),
   em que os bonecos **encenam o que os dados registram**.
2. Atualizar **de 1 em 1 minuto** (Stokki e Vuupt).
3. Mostrar **gargalo**: caixas acumulando na esteira da etapa que está atrasada, e
   pedidos esperando há muito tempo entre uma etapa e outra.
4. Indicadores por pessoa e por etapa (seção 6).
5. **Sugerir remanejamento**: quando uma etapa acumula e outra está folgada, a
   tela sugere mover alguém. A tela só sugere, quem decide é o Hugo.
6. A rua: quantos carros na rua, fluxo até os clientes, entregas concluídas e
   retornos (insucesso) voltando para o galpão.

## 3. Fora do escopo e o que não pode acontecer

**Fora do escopo:**
- Posição física real das pessoas. Não há rastreio no galpão, e o Hugo
  confirmou que não precisa.
- Tempo médio **por pedido** na separação. A Stokki separa em ondas (um operador
  separa vários pedidos ao mesmo tempo), então esse tempo não é confiável. Fica
  para depois, se um dia a Stokki expuser a onda com início e fim.
- Acesso para a equipe ou para clientes.
- Executar o remanejamento. A tela só sugere.

**Seria fracasso mesmo funcionando:**
- Uma animação bonita que não bate com os dados. Se o placar diz 9 na
  conferência, a cena tem de mostrar isso.
- Derrubar a sessão da Stokki. A sessão é compartilhada com a importação, o
  processamento de documentos e os demais jobs. Um login concorrente derruba a
  outra sessão com 401.
- Ficar tão pesada no notebook do Hugo que ele pare de abrir.

## 4. O galpão (decidido pelo Hugo)

Layout validado no protótipo
`.superpowers/brainstorm/1591-1792717525/content/galpao-3d-esteiras.html`
(Three.js, descartável, dados inventados):

| Área | Pessoas | Observação |
|---|---|---|
| Recebimento + Expedição | **3, o mesmo time** | Uma doca só. Recebimento numa ponta, expedição na outra, lado a lado. Hoje não há troca de função por horário. |
| Estoque (câmaras frias) | **1** | O estoque fica nas câmaras frias. |
| Separação | **3** | Uma bancada para cada um. |
| Conferência | **3** | A Stokki tem "Estação de Conferência N". Uma mesa por estação. |

**Horários (decidido pelo Hugo):** o galpão funciona das 4h às 18h.
- Recebimento + Expedição: das 4h às 14h, aproximadamente.
- Separação e Conferência: das 8h às 18h.

**Esteiras (decidido pelo Hugo):** Recebimento → Estoque, Estoque → Separação,
Separação → Conferência, Conferência → Expedição. As de Recebimento → Estoque
e Separação → Conferência são as mais longas.

**Linguagem visual (validada no protótipo):**
- Boneco Lego com o "diamante" do The Sims em cima: verde = ok, amarelo =
  lento, vermelho = parado.
- Balão com a tarefa atual, por exemplo "PS-42355 · conferindo".
- Cor da caixa pela fase: produto = kraft, pedido montado = azul,
  conferido = verde.
- Placa de fila na esteira a partir de 3 caixas: laranja até 4, vermelha a
  partir de 5.
- Placar no topo, histórico de acontecimentos no canto e câmera livre
  (girar, zoom).

## 5. De onde vêm os dados

### Stokki: o histórico do pedido (confirmado em 22/10 nos PS-42355 e PS-42356)

A página `/administrator/inventory/outbound/show/{id}` traz:

- **Linha do tempo**, com situação, usuário e horário:
  Aberto (cliente) → Em separação → Em Conferência → Aguardando Transportador
  → Enviado (também existem Cancelado e Em Espera).
- **Separação:** "Operador da Tarefa" (nome), "Tarefa da Operação" (#TO-…)
  e "Código de separação".
- **Conferência:** "Estação de conferência", "Usuário da Estação da
  Conferência", **Início**, **Finalização** e **Tempo de Conclusão** exatos,
  além de "Problema Reportado".
- **Itens:** SKU, quantidade, unidade, quantidade confirmada, EANs lidos na
  separação, volumes da NF e volumes informados na conferência.

A página tem cerca de 200 KB. Abrir a de todos os pedidos a cada minuto não
cabe na sessão compartilhada.

**Estratégia de atualização:**
- A cada minuto, ler só a **lista de situações** dos pedidos, que é leve.
- Abrir a **página completa** apenas quando um pedido **muda de situação**,
  para guardar quem fez e os horários.
- Sempre dentro da trava `stokki/sessao_uso.py`, entrando na fila e nunca
  logando por cima.
- Se a vez não vier no minuto, a tela mostra o último retrato e a idade dele,
  por exemplo "dados de 3 min atrás". A tela não fica vazia.

### Vuupt (decidido pelo Hugo)

Rotas, motoristas, paradas, entregas e insucessos, a cada minuto. Usar o que o
núcleo (`nucleo/`) já espelha antes de criar outra leitura. Atenção às
armadilhas da Vuupt no CLAUDE.md: datas em UTC sem fuso e códigos de pedido
que precisam de `normalizar_order_number`.

### Recebimento e estoque

O WMS próprio (`painel_agentes/wms*.py`) só cobre o embarcador piloto. Para os
demais, a fonte de "recebimento chegando" e "guardado no estoque" fica a
definir pelo builder (Stokki: recebimentos e inventário). Se não houver dado
confiável, a área aparece com contadores e sem encenação detalhada.

## 6. Indicadores (decidido pelo Hugo)

- **Conferência: tempo médio por pedido**, geral e **por conferente**,
  **ponderado pelo tamanho do pedido**. A fonte é o Início/Finalização da
  Stokki.
- **Separação: produtividade por operador**: itens (ou unidades) por hora,
  contados quando os pedidos dele passam para "Em Conferência". Não depende de
  saber quando cada pedido começou, então continua certo com as ondas.
- **Espera entre etapas, por pedido**: por exemplo, saiu da separação e
  esperou até começar a conferência (nos PS-42355/56 foram quase 8 h). Essa é
  a medida de gargalo que alimenta a sugestão de remanejamento.

## 7. Sugestão de remanejamento (decidido pelo Hugo: só sugerir)

Quando uma etapa acumula e a vizinha está folgada, a tela mostra um aviso do
tipo "mover 1 da Conferência para a Separação: 14 pedidos esperando
conferência há mais de 2 h, separação sem fila". A regra exata fica a cargo do
builder (seção 9). O Hugo valida depois de ver funcionando.

## 8. Restrições do projeto

- Código, comentários e textos em português. Seguir `CLAUDE.md` e
  `MAPA_DO_SISTEMA.txt` e atualizar o mapa no mesmo commit.
- Tela atrás do login do painel, nível que só o Hugo tem (`total`), seguindo o
  padrão das telas do painel (`requer_auth`, `url_for`, menu lateral com
  `niveis=` igual ao da rota).
- Bibliotecas JS por CDN com versão fixa (o protótipo usa
  `three@0.160.0`).
- Leitura da Stokki em lote na VPS, como `www-data`, por timer systemd, nunca
  dentro da requisição da tela.
- Antes de rodar contra a Stokki, usar `--modo-teste` ou um modo só leitura.

## 9. Decisões minhas (o Hugo pode mudar)

- **Onde fica:** no painel atual (`painel_agentes/`), numa rota nova, porque
  os dados (vigia, núcleo, Vuupt) moram aqui hoje. O sistema próprio
  (`C:\freshhub`) está em construção. Levar a tela para lá depois é decisão
  do Hugo.
- **Tamanho do pedido = unidades** (soma da quantidade dos itens). Itens
  (linhas) e volumes ficam guardados para trocar se ele preferir.
- **Quem é quem:** os bonecos levam o nome do usuário da Stokki. A cena começa
  com as vagas da seção 4. Quem aparecer nos dados e não couber vira boneco
  extra na área onde agiu.
- **Status do diamante:** "lento" e "parado" pelo tempo desde o último
  acontecimento da pessoa, comparado com a média dela. Os limites ficam em
  config e são calibrados com dados reais.
- **Encenação:** quando um pedido muda de etapa entre duas leituras, o boneco
  responsável faz o trajeto daquela mudança. Se muitas mudanças chegam de uma
  vez, a cena acelera para não ficar para trás.
- **Relógio de turno:** a espera entre etapas conta só o horário de trabalho
  da etapa que vai receber o pedido. Exemplo: um pedido que sai da separação
  às 17h50 e começa a conferência às 8h10 esperou 20 min, não 14 h20. Fim de
  semana e feriado seguem o calendário de feriados que já existe; o builder
  confirma com o Hugo se há turno no sábado. O placar e as médias do dia zeram
  às 4h. Fora do turno, os bonecos da área ficam "fora do turno" (sentados ou
  apagados), sem diamante vermelho. A coleta da Stokki e da Vuupt roda das
  3h50 às 18h30 e, fora disso, de 15 em 15 min, para pegar o que entrar à
  noite.
- **Prioridade mínima na Stokki:** a página `/administrator` só abre com a
  conta principal (a wms@ não entra e a reserva espera um 3º usuário admin).
  Então a coleta do Galpão 3D usa a mesma fila do pipeline, da expedição, dos
  documentos e da importação. Ela só pega a vez se a conta estiver livre
  naquele instante (`esperar_segundos=0`) e desiste na hora se não estiver.
  Nunca entra na fila para esperar. Consequência: em horário de pico, o
  retrato da Stokki pode ter alguns minutos, e a tela mostra a idade dele.
  Quando a conta reserva existir, a coleta passa a usar a reserva.
- **Vuupt a cada minuto por leitura própria e leve:** as rotas do dia com
  paradas, só leitura. O espelho do núcleo roda de 15 em 15 min e não serve
  para isso. O núcleo continua sendo a fonte das médias históricas. Ler a API
  não é cobrado; só a otimização de rota é.

## 10. O que fica com o builder

- O formato exato da "lista de situações" leve da Stokki (usar as funções
  prontas de `stokki/`, porque uma chamada manual ao DataTables dá 500).
- As tabelas novas para guardar a linha do tempo e a conferência por pedido.
- A regra numérica da sugestão de remanejamento, com proposta para o Hugo
  validar.
- Desempenho da cena: quantas caixas desenhar quando a fila tem 200 pedidos
  (por exemplo, empilhar e mostrar o número).
- Como a rua aparece (mapa de verdade ou estrada estilizada como no protótipo),
  desde que mostre carros na rua, entregas e retornos.
