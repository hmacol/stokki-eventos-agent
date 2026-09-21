# Recalibração da roteirização: concentração, seleção e sequência

Data: 18/09/2026. Decisões tomadas com o Hugo em 17/09 e 18/09.
Módulo: `roteirizacao/` (criar_rotas_diarias, selecao_modelo,
otimizacao_rotas, roteirizacao_dados) + `painel_agentes/laboratorio_rotas.py`.
Doc-mãe: `DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md` (ganha adendo ao final).

## 1. Problema

O Hugo reportou (17/09) que a ordenação e a concentração das rotas estão
descalibradas: rotas espalhadas demais, rotas que se sobrepõem, sequência
de paradas ruim e poucas paradas por rota.

Medição nas rotas ENVIADAS de produção (rascunhos_rota/rascunhos_parada
da VPS, 11, 15, 16 e 17/09; haversine; base aproximada em Casa Verde
Alta):

| Dia   | Rotas | Paradas | Paradas/rota | Diâmetro mediano | Paradas cuja vizinha mais próxima está em OUTRA rota | Pares de rotas com "bolhas" que se cruzam |
|-------|------:|--------:|-------------:|-----------------:|-----------------------------------------------------:|------------------------------------------:|
| 11/09 | 14    | 105     | 7,5          | 6,4 km           | 23 % (24, todas na mesma partição de carga)          | 19 |
| 15/09 | 11    | 103     | 9,4          | 8,2 km           | 19 % (15 mesma partição + 5 partição diferente)      | 21 |
| 16/09 | 18    | 171     | 9,5          | 11,8 km          | 25 % (22 + 21)                                       | 45 |
| 17/09 | 13    | 127     | 9,8          | 8,4 km           | 31 % (14 + 25)                                       | 32 |

Reordenar cada rota do zero (vizinho mais próximo + 2-opt, sem volta à
base) daria 12 a 21 % menos km em linha reta que a sequência enviada.

Causas no código, uma por sintoma:

- **Sobreposição**: metade ou mais das paradas "cruzadas" vem da
  partição Seco / Refrigerado-Congelado / Misto
  (`criar_rotas_diarias._particionar_carga_com_fusao`), que cria rotas
  paralelas na mesma região. O restante são pétalas do Clarke-Wright se
  entrelaçando: nenhum modelo olha uma rota em relação às outras.
- **Espalhamento**: a trava é 20 km entre qualquer par de paradas
  (`DISTANCIA_MAXIMA_ROTA_KM`) e o critério de vitória da competição
  diária é "menos rotas, km só desempata"
  (`selecao_modelo.escolher_melhor_modelo`, linha 297). O Clarke-Wright
  vence quase todo dia desde agosto porque estica cada rota até o teto.
- **Poucas paradas por rota**: cada partição (tipo de carga x
  macro-região) deixa sobras que viram rota pequena (3 a 6 rotas por
  dia com 6 paradas ou menos).
- **Sequência**: regra "parada mais longe da base primeiro, e ela
  nunca se move" (Hugo, 03/08) como semente do 2-opt; objetivo do 2-opt
  inclui uma volta à base que a rota real não faz; tudo por haversine.

## 2. Decisões de negócio (Hugo, 18/09)

1. **Seco e Refrigerado podem sempre ir no mesmo carro.** Toda a frota
   tem compartimento térmico. O tipo de carga deixa de separar rotas.
2. **Objetivo: rota compacta, mesmo que custe 1 rota a mais no dia.**
   O critério deixa de ser contagem de rotas.
3. **Sequência livre.** A regra "sai pro ponto mais longe e volta
   esvaziando" deixa de valer, como trava e como preferência. Vale só
   menor km/tempo, respeitando janelas de horário.
4. Abordagem escolhida: **B** (recalibrar + polimento entre rotas +
   replay como ferramenta fixa). Tempo real de via (Google Routes) fica
   fora, como possível fase seguinte.

## 3. Desenho

### 3.1 Partição e rótulo de carga

- `_particionar_carga_com_fusao` passa a devolver **uma partição por
  dia** quando `SEPARAR_POR_TIPO_CARGA = False` (novo, padrão False em
  `criar_rotas_diarias.py`, ao lado das outras constantes). Com True o
  comportamento de hoje volta inteiro (retorno rápido).
- O rótulo gravado em `rascunhos_rota.particao` passa a ser derivado do
  conteúdo de cada rota, por uma função nova
  `rotulo_carga(sublote) -> "Seco" | "Refrigerado/Congelado" | "Misto (Seco+Refrigerado)"`
  em `criar_rotas_diarias.py`. Tela de planejamento, histórico de
  seleção e nomes de rota não mudam de formato.
- O histórico `selecao_modelo_historico.txt` passa a ter uma linha por
  dia com label `Geral` (ou `Geral (seleção manual)`), em vez de uma por
  tipo de carga.
- `painel_agentes/laboratorio_rotas.py`: `PARTICOES_VALIDAS` ganha
  `"Todos"` (novo padrão da tela); "Seco" e "Refrigerado/Congelado"
  continuam existindo pra investigação.
- `main()` de `criar_rotas_diarias.py` e `roteirizar_para_rascunhos`
  passam pelo mesmo caminho (ambos chamam `_particionar_carga_com_fusao`).

### 3.2 Critério de seleção e travas

- `escolher_melhor_modelo`: vencedor = **menor km total estimado**,
  desempate por menos rotas:
  `min(avaliacoes, key=lambda n: (round(km, 1), rotas))`.
  O km total usa `calcular_km_estimado` alterado (ver 3.3: sem a perna
  de volta à base). Cada rota extra paga a perna base -> 1ª parada,
  então rota a mais só vence quando compensa em km.
- `DISTANCIA_MAXIMA_ROTA_KM` (Grande SP) cai de 20 km pra um valor
  escolhido pelo Hugo a partir da curva do replay (candidatos 10, 12 e
  15 km; ver 3.5). A cópia em `painel_agentes/planejamento_rotas.py`
  (badges de trava) acompanha.
- Não muda: `TAMANHO_MAXIMO_ROTA`, `VOLUME_MAXIMO_ROTA`, km acumulado,
  orçamento de 9h, velocidades, `FATOR_ESTRADA`, níveis, veículo grande,
  macro-regiões, fusão pós-hoc de sublotes pequenos.

### 3.3 Sequência (`roteirizacao_dados.ordenar_com_janelas`)

- Semente: **vizinho mais próximo** partindo da base (em vez de
  farthest-first). Serviço sem coordenada vai pro final, como hoje.
- Objetivo: km do trajeto `base -> p1 -> ... -> pN` (**sem** a perna
  `pN -> base`), mais as penalidades de janela já existentes
  (`PESO_ATRASO_JANELA_KM = 60`, `PESO_ESPERA_JANELA_KM = 30`,
  `TOLERANCIA_JANELA_HORAS = 0.25`). `_km_total`/`_delta_km` internos
  passam a tratar a posição N+1 como "nada" em vez de base.
- 2-opt e or-opt podem mover **qualquer** posição, inclusive a primeira.
  A "2ª passada que libera a 1ª parada" some porque deixa de ter razão
  de existir.
- `calcular_km_estimado` deixa de somar a volta à base (fica coerente
  com o objetivo, com `estimar_tempo_rota` e com o km mostrado nos
  rascunhos). `selecao_modelo._km_total` herda.
- `ordenar_por_distancia_base` (farthest-first) fica no módulo, sem
  chamador em produção, com docstring dizendo que a regra foi revogada
  em 18/09. Comentários que citam farthest-first
  (`_orcamento_inviavel_por_distancia`, `ordenar_2opt`, `reparar_sublotes_por_horas`)
  são atualizados; a lógica deles continua válida com sequência livre.
- Beneficiados sem mudança de código: botão "Otimizar sequência" dos
  rascunhos (`rascunhos_rota.otimizar_sequencia`) e o resequenciamento
  do `incrementar_rotas`.

### 3.4 Polimento entre rotas (novo `roteirizacao/polimento_rotas.py`)

Entrada: lista de sublotes já sequenciados de **uma** partição do dia
(depois de `escolher_melhor_modelo` + `_fundir_sublotes_entre_macrorregioes`),
coordenadas da base, `api_key`, travas (`tamanho_maximo`, `volume_maximo`,
`distancia_maxima_km`, `distancia_maxima_viagem_km`, `eh_viagem_fn`).
Saída: lista de sublotes com a mesma cobertura de serviços.

Função pública: `polir_entre_rotas(sublotes, base_lat, base_lng, api_key, *, tamanho_maximo, volume_maximo, distancia_maxima_km, distancia_maxima_viagem_km, eh_viagem_fn, tempo_maximo_s=3.0) -> tuple[list[list[dict]], dict]` (o dict é o resumo: movimentos aceitos, km antes/depois, rotas esvaziadas).

Regras:

- Só participam rotas "políveis" (`_rota_polivel`): sem pedido de
  nível 4 (`NIVEL_ROTA_EXCLUSIVA`), não classificada como veículo grande
  (`classificar_tipo_veiculo` é None) e sem pedido inviável só por
  distância (`_orcamento_inviavel_por_distancia`). Mesmos três critérios
  de `exige_orcamento_horas`, **sem** a exclusão de rota de 1 parada:
  rota de 1 parada pode ser origem (esvaziar) e destino (receber). As
  demais passam intactas.
- Só rotas da **mesma macro-região** trocam paradas
  (`macro_regiao_do_servico` da 1ª parada de cada rota).
- Movimentos, avaliados em laço até não haver melhoria ou estourar
  `tempo_maximo_s`:
  1. **Realocar**: tirar uma parada da rota A e inserir na rota B na
     posição de menor acréscimo de km.
  2. **Trocar**: permutar uma parada de A com uma de B, cada uma na
     melhor posição da rota de destino.
  3. **Esvaziar**: rota com 1 ou 2 paradas políveis tenta realocar
     todas nas vizinhas. É um movimento composto: aceito se todas
     couberem **e** o km total das rotas envolvidas cair (tirar a rota
     inteira elimina a perna da base, ganho que a realocação parada a
     parada não enxerga). Não esvazia "de graça": o objetivo é
     compacidade, não contagem de rotas.
- Aceite: soma `km(A') + km(B') < km(A) + km(B) - 0.05` (50 m) **e** A'
  e B' válidas por `_rota_valida`: tamanho, caixas, distância par-a-par
  (`otimizacao_rotas._limite_distancia`), `estimar_tempo_rota <= ROTA_TEMPO_MAXIMO_HORAS`
  (ou `_orcamento_inviavel_por_distancia`), `janela_respeitada`. Depois
  de aceitar, A' e B' são resequenciadas por `ordenar_com_janelas` e
  revalidadas; se a revalidação falhar, o movimento é desfeito.
- Vizinhança: pra cada rota A, só rotas B cujo centroide esteja a menos
  de `2 * distancia_maxima_km` do centroide de A (evita O(n²) inútil).
- Determinístico: laço em ordem fixa de índice, sem aleatoriedade.
- Chamado em `criar_rotas_diarias.roteirizar_para_rascunhos` e em
  `main()` logo após `_fundir_sublotes_entre_macrorregioes`, com log do
  resumo. **Não** é chamado pelo `incrementar_rotas` (rotas já enviadas
  à Vuupt) nem pelo botão "Otimizar sequência" (que é por rota).

### 3.5 Replay e métricas

- `roteirizacao/metricas_plano.py` (novo, puro, sem I/O): dado um plano
  (lista de rotas, cada rota lista de paradas com lat/lng/caixas/nível)
  e a base, devolve: rotas, paradas, paradas/rota (média, mín, máx),
  rotas com <= 6 paradas, diâmetro (mediano e máximo), raio médio do
  centroide, km total (sem volta), % de paradas cuja vizinha mais
  próxima está em outra rota, pares de rotas com bolhas que se cruzam
  (centroides mais próximos que 0,8 x soma dos raios), rotas acima de
  `ROTA_TEMPO_MAXIMO_HORAS`.
- `roteirizacao/replay_rotas.py --de AAAA-MM-DD --ate AAAA-MM-DD [--banco caminho] [--modelo X] [--distancia-maxima N] [--sem-polimento] [--separar-carga]`:
  1. Lê `rascunhos_rota` (status ENVIADO) + `rascunhos_parada` de cada
     dia do período: coordenadas, caixas, nível, `sender_id`
     (-> `classificar_tipo_carga`) e a janela efetiva **já gravada** na
     parada (`janela_inicio`/`janela_fim`, a mesma que valeu no dia).
     Monta dicts no formato de serviço da Vuupt que o pipeline já
     aceita, com `latitude`/`longitude` embutidas (sem geocodificar).
     Pra isso `roteirizacao_dados.obter_coordenadas` passa a preferir a
     coordenada embutida no dict antes de geocodificar o endereço
     (mesma preferência que `coords_do_servico` e `_distancia_da_base`
     já tinham; unifica agrupadores, sequenciador e replay).
  2. Roda o miolo de produção sem Vuupt, sem motorista, sem gravar nada
     e sem escrever no histórico: função extraída
     `planejar_sublotes(servicos, coords_base, gmaps_key, data_alvo, ...)`
     em `criar_rotas_diarias` (partição -> seleção -> fusão ->
     polimento), que `main()` e `roteirizar_para_rascunhos` passam a
     usar, com `registrar_historico=False` e os parâmetros da linha de
     comando sobrescrevendo as constantes do módulo.
  3. Imprime, por dia e total, as métricas de 3.5 lado a lado:
     "enviado de verdade" x "novo". Grava em
     `roteirizacao/dados/replay_resultado.txt`.
- Base local pro replay: cópia somente-leitura do `dados.db` da VPS
  (scp pra `dados/dados_replay.db`, gitignored), período 12/08 a 17/09
  (30 dias, ~3 mil paradas).
- Calibração de `DISTANCIA_MAXIMA_ROTA_KM`: rodar o replay com 10, 12,
  15 e 20 km e apresentar ao Hugo a tabela rotas x diâmetro x
  entrelaçamento x km. O Hugo escolhe; o valor entra na constante.
- Nota de limite: o replay roteiriza o conjunto de pedidos que **foi
  enviado** naquele dia, não o pool inteiro que o pipeline viu às 22h
  (pedidos removidos/adiados manualmente ficam de fora). A comparação
  é justa porque os dois lados usam o mesmo conjunto.

### 3.6 Testes, rollout e documentação

- Testes novos (unittest, `py -3.11 -m unittest`):
  - `roteirizacao/test_sequencia_livre.py`: semente vizinho mais
    próximo; objetivo sem volta; 1ª parada pode mudar; janela continua
    respeitada (casos de `test_janelas_horario.py` que assumem
    farthest-first são reescritos, não apagados).
  - `roteirizacao/test_polimento_rotas.py`: realocação reduz km;
    movimento que estoura caixas/tamanho/distância/horas/janela é
    rejeitado; rota de nível 4 e veículo grande intocadas; cobertura de
    serviços preservada; esvaziamento de rota pequena; determinismo;
    respeito ao teto de tempo.
  - `roteirizacao/test_metricas_plano.py`: métricas em planos sintéticos
    (2 rotas separadas -> 0 % cruzado; 2 rotas entrelaçadas -> alto).
  - `roteirizacao/test_selecao_modelo.py`: critério km-primeiro; empate
    por rotas; `registrar_historico=False` não escreve.
  - `roteirizacao/test_particao_carga.py`: partição única com
    `SEPARAR_POR_TIPO_CARGA=False` e tripla com True; `rotulo_carga`.
  - `roteirizacao/test_coordenadas_embutidas.py`: `obter_coordenadas`
    prefere lat/lng do dict; (0, 0) e valor inválido caem na
    geocodificação.
  - `roteirizacao/test_planejar_sublotes.py`: cobertura, partição
    única, chave do polimento, fluxo de reserva sem base.
  - `roteirizacao/test_replay_rotas.py`: leitura do banco -> dicts.
  - `painel_agentes/test_laboratorio_todos.py`: partição "Todos".
- Testes existentes que precisam rodar verdes: `test_orcamento_horas`,
  `test_janelas_horario`, `test_nivel4_veiculo_grande`,
  `test_incrementar_rotas`, `painel_agentes/test_incrementar_rascunhos`.
- Rollout: deploy padrão (pull + chown), sem unit/timer novo, restart do
  `painel-agentes` (importa os módulos). Na primeira noite: comparar a
  linha nova do `selecao_modelo_historico.txt` e rodar
  `metricas_plano` nos rascunhos gerados. Retorno: `SEPARAR_POR_TIPO_CARGA=True`
  + `DISTANCIA_MAXIMA_ROTA_KM=20` + desligar polimento
  (`POLIMENTO_ATIVO=False`, constante em `criar_rotas_diarias.py`). A
  sequência livre não tem chave de retorno (é a decisão 3).
- Documentação: adendo "§9 Recalibração 18/09" em
  `DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md` com as decisões, constantes
  novas e como rodar o replay. Corrigir de passagem o docstring de
  `roteirizacao_dados.py:50` (diz 70 km, é 35).

## 4. Fora do escopo

- Tempo real de via (Google Routes / OSRM) na sequência.
- Orçamento de horas, velocidades médias, `FATOR_ESTRADA`.
- `incrementar_rotas` (corte 19h, escolha de rota por centroide).
- Tela de planejamento além da opção "Todos" no Laboratório.
- Tabela de parâmetros no banco ou em `config.yaml` (continuam
  constantes Python).

## 5. Critérios de aceite

1. Replay dos 30 dias com as regras novas, contra o enviado:
   entrelaçamento (% vizinha em outra rota) cai pelo menos pela metade;
   diâmetro mediano não sobe; rotas com <= 6 paradas caem; km total não
   sobe mais que 5 % mesmo com eventual rota a mais; 0 rotas acima de
   9h fora das exceções já previstas.
2. Todos os testes listados em 3.6 verdes com `py -3.11`.
3. Primeira noite em produção: histórico registra vencedor com o
   critério novo, rascunhos com rótulo de carga correto, painel abre e
   "Otimizar sequência" funciona.
4. Tempo total de `criar_rotas_diarias` na VPS não passa de 2x o de
   hoje (polimento tem teto de 3 s por partição).
