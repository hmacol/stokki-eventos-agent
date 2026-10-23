# Rotas sem "km pra matar tempo" + rota da tarde (design, 23/10/2026)

## 1. Por que

Caso real: `Planejamento - 23/10/2026 - #13` (rascunho 1736, ZONA SUL, 15 paradas, 77,9 km estimados).
A sequência vai de Vila Prudente/Ipiranga a Moema, volta a leste (Gama Lobo, 7,3 km) e volta a
Moema (Tuim, 7,8 km). A mesma cliente (Deyse Saito, Rua Gama Lobo 1810: PS-42216-R1 e PS-42216)
é visitada duas vezes, com outra parada no meio.

Causa, reproduzida com o código de produção (`ordenar_com_janelas`, paradas da #13):

- O objetivo do sequenciador é `km + 60·atraso_h + 30·espera_h`
  (`roteirizacao_dados.py`, `PESO_ATRASO_JANELA_KM` / `PESO_ESPERA_JANELA_KM`).
- Um pedido da rota (wilian, PS-42147-R1) tem janela 15:00–23:59 (fonte `atendimento`), e a rota
  acabaria por volta das 13h. Esperar 2h "custa" 60 km, então rodar ~30 km a mais em zigue-zague
  sai mais barato. O sequenciador também separa as duas entregas da Deyse pra "gastar tempo".
- Resultado medido: sequenciador atual ~71 km e 0 h de espera; o mesmo conjunto sem a janela do
  wilian ~37 km.

Volume do problema em outubro (19 dias de rota): pedidos com janela abrindo a partir das 13h são
de **0 a 4 por dia**, quase sempre os mesmos clientes: bares e restaurantes do Centro (Varal,
Vanz & Arias, Manuel Gastronomia, The Punch Bar, Zé Bolim, Pizza da Mooca), o wilian e a Talita
(18:30–19:30, agendado).

## 2. Objetivo

1. Pedidos no mesmo endereço sempre seguidos na sequência.
2. A rota da manhã nunca roda km a mais só pra chegar mais tarde num cliente.
3. Pedido que não cabe no horário de nenhuma rota da manhã vai pra uma **rota da tarde** própria.
   A equipe decide no Planejamento se ela sai por Lalamove ou por um motorista numa segunda saída
   (Hugo, 23/10: "eventualmente pode ser sim um lalamove pelo custo, ou uma segunda saída de motorista").

Sucesso = no replay de outubro, menos km total, sem aumentar as rotas acima de 9h nem o atraso
evitável, e a rota da tarde só aparece nos dias que têm pedido de janela tardia.

## 3. Fora do escopo

- Os ~600 pedidos com janela "agendada" começando às 11:00. A suspeita é horário da Vuupt lido em
  UTC (08:00 BRT = 11:00Z) que escapa de `JANELAS_PADRAO_IGNORADAS`. Vai ser investigado em
  sessão própria. **Essa investigação pode mudar os números do replay deste projeto**: se ela
  corrigir antes, refazer o replay de base.
- Corrigir a janela de atendimento de cadastro de clientes específicos (ex.: wilian 15:00–23:59).
- OSRM / OR-Tools (projeto próprio, em sessão separada).
- Escolha automática entre Lalamove e motorista, ou comparação de preço.
- Mudar a regra de rota fraca para as rotas da manhã.

## 4. Decisões

Do Hugo:
- **(b)** Quando um cliente só recebe à tarde e a rota acabaria antes, o pedido **sai dessa rota**
  e vai pra uma rota que passe lá no horário. Não fica esperando, nem é entregue antes da janela.
- Essa "rota que passa lá no horário" pode ser uma **rota da tarde** criada pra isso, feita por
  Lalamove ou por segunda saída de motorista.

- **Corte das 12h (Hugo, 23/10):** só vai pra rota da tarde pedido cuja janela **abre às 12:00
  ou depois** (constante nova, ex.: `HORA_CORTE_TARDE = "12:00"`). A trava de espera da seção 5.3
  conta **só a espera antes dessas janelas**. A espera antes de janela da manhã continua como hoje
  (entra só no custo do sequenciador). As janelas "agendadas 11:00" (suspeita de fuso) ficam
  como estão até a investigação própria concluir.

Do Claude (o Hugo pode mudar; marcar no código como constante com comentário):
- `PESO_ESPERA_JANELA_KM`: 30 → **10**. Precisa ser menor que `VELOCIDADE_MEDIA_KMH` (15). Assim
  rodar pra matar 1h de espera nunca compensa: rodar essa hora também gasta tempo e ainda soma km.
  O replay confirma ou ajusta o valor.
- Trava de espera: rota da manhã com mais de **45 min de espera somada**, na ordem final, é
  reprovada (constante nova, ex.: `ESPERA_MAXIMA_ROTA_HORAS = 0.75`). É mais folgada que os 15 min
  de tolerância do atraso porque esperar o cliente abrir às 7h/8h na primeira parada comercial é
  comum, e não queremos quebrar essas rotas. O replay calibra.
- A rota da tarde é **um rascunho comum** do lote, com nome próprio e `start_at` próprio. Ela não
  passa pela junção/adiamento de rota fraca e leva um selo próprio no card do Planejamento.
- Saída da rota da tarde = primeira chegada necessária (início da janela mais cedo entre os
  pedidos dela) menos o tempo de ida desde a base (`_tempo_perna_horas`), arredondado pra baixo em
  15 min. Nunca antes de `HORA_INICIO_ROTA`.
- **Rota da tarde vem antes do adiamento de rota fraca.** Os pedidos que vão pra tarde são
  separados antes de `absorver_rotas_fracas`/`_aplicar_segurar`, e assim nunca são segurados
  (senão o wilian seria adiado todo dia).
- **Dentro da rota da tarde a trava de espera também vale**, contada a partir da saída dela
  (`simular_horarios(..., hora_saida=...)`). Ex.: bar às 13h + Talita às 18h30 = duas rotas da
  tarde. Cada uma custa uma saída/Lalamove, mas esperar 4h com o carro também custa.
- **Escopo da trava e da rota da tarde:** só rota comum de última milha da Grande SP, o mesmo
  recorte de `reparar_sublotes_por_horas` (`exige_orcamento_horas`). Viagem/interior, veículo
  grande, nível 4 e rota inviável só por distância ficam como hoje.
- **Rota da tarde sai sem motorista sugerido** e não conta no aviso de "rota sem motorista".
  Fica fora da contagem do `selecionar_motorista_equitativo`. A equipe escolhe motorista
  (pode ser quem já tem rota de manhã) ou lança a Lalamove.

## 5. Comportamento desejado

### 5.1 Mesmo endereço seguido

Depois de `ordenar_com_janelas` (os dois caminhos, com e sem janela), paradas com a mesma
coordenada (lat/lng iguais com 5 casas) ficam juntas, na posição da primeira ocorrência e na ordem
original entre si. Vale também pro "Otimizar sequência" da tela e pro incremento, que já passam
por `ordenar_2opt` → `ordenar_com_janelas`.

Teste: na #13, PS-42216-R1 e PS-42216 saem adjacentes.

### 5.2 Sem km pra matar tempo

Com o peso novo, o sequenciador passa a preferir esperar a rodar. Por si só isso gera espera
nas rotas. Quem impede a espera longa é a trava de 5.3, não o peso.

### 5.3 Trava de espera

`janela_respeitada` passa a reprovar rota cuja espera **antes de janelas que abrem às
`HORA_CORTE_TARDE` ou depois** (na ordem dada, pela linha do tempo de `simular_horarios`, que
precisa passar a expor a espera por parada ou essa soma) passe de `ESPERA_MAXIMA_ROTA_HORAS`. Como `janela_viavel` usa `janela_respeitada`, a trava vale
automaticamente na formação, na fusão, no reparo pós-sequenciamento, no polimento
(`_rota_valida`) e no incremento.

Exceção, paralela ao `_atraso_intrinseco`: a trava não reprova rota de **um pedido só** (senão o
reparo não tem onde parar). Esse pedido sozinho é exatamente o que vira rota da tarde (5.4).

Cuidado: as rotas fracas usam `_rota_valida` na receptora. Pedido de janela tardia não deve ser
"juntado" de volta numa rota da manhã que passaria a esperar.

### 5.4 Rota da tarde

No fim da roteirização do dia (depois do polimento, **antes** da junção/adiamento de rotas
fracas, antes de gravar o lote):

1. Pedidos com janela abrindo às `HORA_CORTE_TARDE` ou depois que ficaram numa rota cuja espera
   (seção 5.3) ainda passa da trava, ou sozinhos numa rota de 1 parada, são separados. Pedido de
   janela tardia que coube numa rota da manhã sem estourar a trava fica onde está.
2. Esses pedidos formam a(s) rota(s) da tarde: mesma partição e mesmas travas de caixas, 9h e
   distância da manhã, sequenciadas por `ordenar_com_janelas`, saída calculada como na seção 4.
   Se não couberem numa só, as travas existentes quebram em mais de uma.
3. Nome no padrão de sempre (`Planejamento - DD/MM/AAAA - #N`). A marca de tarde é a coluna
   nova `rascunhos_rota.turno = 'TARDE'`. Um sufixo no nome quebraria
   `planejamento_rotas._PADRAO_INDICE_ROTA` (`#(\d+)\s*$`, numeração do botão Roteirizar) e o
   "Rota N" do portal (`dados_cliente._PADRAO_ROTA_PLANEJAMENTO`).
4. No Planejamento, o card da rota da tarde mostra o selo "Tarde · sai HH:MM". A equipe atribui
   motorista ou lança Lalamove pelo fluxo que já existe (`lancar_lalamove`, `lalamove_lancar_em`).
5. Dia sem pedido nessa situação: nenhuma rota da tarde.

### 5.5 Incremento (16h/18h/22h e "Incrementar" do rascunho)

Pedido novo que não cabe em nenhuma rota da manhã por causa da trava de espera entra na rota da
tarde do dia, se houver e se couber. Se não houver, o comportamento é o de hoje (fica fora e
avisa). *(Escolha do Claude: não criar rota da tarde nova pelo incremento.)*

## 6. Onde fica

- Base de código: o ramo `otimizacao-rapida` (worktree `.claude/worktrees/otimizacao-rapida`)
  tem ajustes de 17/10 sem commit nos mesmos arquivos (`roteirizacao_dados.py`, `polimento_rotas.py`,
  `incrementar_rotas.py`, `criar_rotas_diarias.py`, `rascunhos_rota.py`, `planejamento_rotas.py`).
  Antes de começar: com OK do Hugo, commitar esses ajustes no próprio ramo (commit local, sem push)
  e abrir um ramo novo a partir dele (ex.: `espera-rota-tarde`). Outra sessão (OSRM/OR-Tools)
  também parte desse ramo, então trabalhar em worktree próprio.
- Decisões de roteirização do Hugo: memória `project_roteirizacao_regras_hugo` e
  `DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md` seção 8. Atualizar a seção 8.2 com a trava e a rota
  da tarde, e o `MAPA_DO_SISTEMA.txt` se surgir arquivo ou função pública nova.

## 7. Medir antes de subir

`roteirizacao/replay_rotas.py` sobre 01/10–22/10, com o banco de replay atualizado da VPS.
Rodar com e sem as mudanças, e também uma variante com o peso antigo, pra separar o efeito do
peso do efeito da trava. Tabela por variante:

- km total
- rotas e paradas
- rotas acima de 9h
- atraso evitável (h)
- espera total (h)
- pedidos na rota da tarde por dia
- quantas rotas da manhã a trava de espera quebrou

Critério pra levar ao Hugo: km total menor, rotas > 9h e atraso evitável iguais ou menores, e
rota da tarde com 0–4 pedidos/dia (bate com o levantamento). Se a trava quebrar muitas rotas da
manhã, subir `ESPERA_MAXIMA_ROTA_HORAS` e reportar. O deploy é decisão do Hugo, com os números
na mão.

## 8. Testes

- Unitários em `roteirizacao/` (padrão `test_*.py`, `py -3.11 -m unittest`):
  - mesmo endereço adjacente
  - peso novo não gera zigue-zague: fixture com as 14 paradas da #13 (coordenadas e janelas
    deste documento) → km bem abaixo de 71 e sem atraso
  - trava de espera reprova rota com 1h de espera e aceita rota de 1 pedido
  - rota da tarde formada com a saída correta
  - dia sem janela tardia não gera rota da tarde
- Rodar os testes existentes de roteirização (`test_janelas_horario`, `test_orcamento_horas`,
  `test_polimento_rotas`, rotas fracas). Alguns esperam o peso 30 e vão precisar de ajuste
  consciente; listar quais mudaram e por quê.
- Tela: abrir o Planejamento em porta de teste (8099) com um lote que tenha rota da tarde e
  conferir o selo.

## 9. Dados da #13 (fixture)

Base aproximada (-23.4930, -46.6640). Saída 06:00. Nível 1 = 15 min, nível 3 = 1h15.

| Pedido | lat | lng | nível | janela |
|---|---|---|---|---|
| PS-42344 | -23.57999 | -46.58311 | 1 | — |
| PS-42361 | -23.58870 | -46.60809 | 1 | — |
| PS-42342 | -23.59994 | -46.64014 | 1 | 08:00–18:00 |
| PS-42296 | -23.59355 | -46.63622 | 3 | 08:00–16:00 |
| PS-42191-R1 | -23.59608 | -46.66801 | 2 | 08:00–17:00 |
| PS-42301 | -23.60070 | -46.66681 | 2 | 07:00–15:00 |
| PS-42216-R1 | -23.59992 | -46.61204 | 1 | 08:00–18:00 |
| PS-42348 | -23.59200 | -46.62702 | 1 | — |
| PS-42216 | -23.59992 | -46.61204 | 1 | 08:00–18:00 |
| PS-42334 | -23.60258 | -46.67050 | 1 | — |
| PS-42331 | -23.60158 | -46.67061 | 1 | — |
| PS-42357 | -23.60472 | -46.66137 | 2 | — |
| PS-42147-R1 | -23.58734 | -46.64417 | 2 | 15:00–23:59 |
| PS-42337 | -23.57573 | -46.64508 | 1 | — |

(PS-42031-R1, VIRTUS, foi encaixado à mão depois e fica de fora.)
