# Documento de Especificação e Execução: Tipos de Veículo Grande na Roteirização

Este documento especifica as alterações feitas para que a roteirização e a alocação de motoristas considerem 4 categorias de veículo maior que a última milha (VAN/HR, VUC, 3/4, Truck), cada uma com peso máximo, caixas máximas, número máximo de endereços diferentes atendidos por rota e um piso mínimo de caixas que justifica usar aquele porte.

---

## 1. Contexto e Objetivos

Até 15/08, toda rota era tratada do mesmo jeito: no máximo `TAMANHO_MAXIMO_ROTA` (16) entregas e `VOLUME_MAXIMO_ROTA` (100) caixas, sem nenhum conceito de tipo de veículo. Não havia campo de veículo no cadastro de motorista (`dados/BD_MOTORISTAS.xlsx`) além de `PLACA` (usada só pra rodízio de SP).

Pedido do Hugo, 15/08: passar a considerar 4 categorias de veículo maior:

| Tipo | Peso máx | Caixas máx | Endereços diferentes máx | Caixas mín |
|---|---|---|---|---|
| VAN/HR | 1300kg | 400cx | 4 | 150cx |
| VUC | 2000kg | 600cx | 4 | 300cx |
| 3/4 | 6000kg | 1200cx | 4 | 500cx |
| Truck | 10000kg | 2500cx | 2 | 1500cx |

Confirmado com o Hugo: é uma faixa **nova** -- entra só quando um grupo de pedidos (até 4 endereços diferentes, ou até 2 no caso do Truck) **juntos** já justificam um veículo maior do que o de última milha. A roteirização pequena (16 paradas/100 caixas) continua intocada. Mesmo endereço nunca conta mais de 1 vez contra o limite de endereços (pode ter qualquer quantidade de pedidos).

**Peso (kg) não é aplicado como trava na criação de rota** -- fica documentado em `regras/tipo_veiculo.py` pra uso futuro, mas não é hoje um dado disponível no momento da roteirização (só é extraído da DANFE em `gerar_pdf_romaneios.py`, depois que a rota já foi montada).

**Tipo de veículo do motorista** (`TIPO_VEICULO`) é uma coluna nova, 100% manual, em `dados/BD_MOTORISTAS.xlsx` -- a Vuupt não expõe esse dado de forma confiável (mesmo problema já visto com `PLACA`).

**Classificação automática**: o sistema calcula o volume/nº de endereços do lote e escolhe a MENOR categoria que comporta, sem intervenção manual.

---

## 2. Regras de Negócio e Algoritmo

### 2.1. `regras/tipo_veiculo.py` (novo módulo)

Define as 4 categorias (`TIPOS_VEICULO`, ordenadas por capacidade crescente) e:

- `classificar_tipo_veiculo(caixas, enderecos_distintos)` -- menor tipo cujo `max_enderecos_distintos` comporta `enderecos_distintos` E cujo `[volume_minimo_cx, volume_maximo_cx]` contém `caixas`. `None` quando não cabe em nenhum tipo (fica fora da faixa, segue roteirização comum).
- `teto_caixas_para_enderecos(qtd_enderecos)` -- maior `volume_maximo_cx` entre os tipos cujo `max_enderecos_distintos` ainda comporta aquele tanto de endereços (usado no empacotamento pra saber até quanto vale a pena crescer um cluster).
- `veiculo_comporta(tipo_motorista, tipo_necessario)` -- motorista com veículo de capacidade MAIOR OU IGUAL comporta (ex: motorista de Truck também serve rota classificada VUC). `tipo_necessario=None` (rota comum) sempre `True`; `tipo_motorista=None` só serve rota sem `tipo_necessario`.
- `tipo_por_codigo(codigo)` -- fail-safe, com apelidos pro preenchimento manual (ex: "3/4" normaliza pra "3_4", mapeado pra `TRES_QUARTOS`).

### 2.2. Empacotamento: `roteirizacao_dados.py::separar_pedidos_exclusivos`

Novo estágio (`_extrair_grupos_veiculo_grande`), rodando sobre o pool "demais" (nível 1/2/3, não-gigante) -- a MESMA função já compartilhada pelos 5 esquemas de roteirização (Atual, Sweep, Clarke-Wright, CEP, K-means via `otimizacao_rotas.py`), então o comportamento novo vale pra todos automaticamente:

1. Agrupa os candidatos por endereço (`servico.get("address")`) -- cada grupo é 1 "unidade" (mesmo endereço nunca conta mais de 1 vez).
2. Guloso: parte do endereço com mais caixas ainda não usado (semente) e vai anexando o endereço geograficamente mais próximo ainda não usado, enquanto o total ainda couber em ALGUM tipo -- teto que ENCOLHE conforme mais endereços entram (com 3+ endereços, Truck deixa de ser alcançável, o teto cai de 2500 pra 1200) -- e a distância pro cluster continuar dentro da mesma trava de sempre (Grande SP x Viagem).
3. Guarda o ÚLTIMO estado em que o cluster classificou em algum tipo (`melhor`) -- crescer mais um endereço pode "estourar" o cluster pra fora de qualquer faixa válida (ex: 3 endereços/1200cx cabe em 3/4, mas o 4º endereço empurra pra 1250cx, que não cabe em nada com 4 endereços); nesse caso o cluster extraído é o último válido, não o final.
4. Extrai o cluster se ele classificou em algum momento -- inclusive com 1 endereço só (vários pedidos pro MESMO endereço somando volume de veículo grande, nenhum "gigante" individualmente). Endereços que nunca entram em nenhum cluster válido voltam pro pool comum.

Pedido "gigante" (1 endereço com mais caixas que `volume_maximo`) continua isolado pelo caminho já existente -- como a classificação final é recalculada a partir do sublote pronto (não durante o empacotamento), ele já sai automaticamente tagueado com o tipo certo, sem tratamento especial.

### 2.3. Classificação e tagueação

`caixas_e_enderecos(sublote)` (novo, em `roteirizacao_dados.py`) devolve `(soma de caixas, nº de endereços distintos)` de um sublote -- usado por `criar_rotas_diarias.py` (tag `tipo_veiculo` no rascunho), `alocacao_motoristas.py` (trava de elegibilidade) e `laboratorio_rotas.py` (rótulo da rota) pra chamar `classificar_tipo_veiculo` sempre a partir do sublote final, nunca de estado carregado do empacotamento.

### 2.4. Alocação de motorista: `alocacao_motoristas.py::selecionar_motorista_equitativo`

Nova trava, ao lado de Viagem/Zona/Rodízio: `tipo_veiculo_necessario = classificar_tipo_veiculo(*caixas_e_enderecos(sublote))`; motorista só é elegível se `veiculo_comporta(motorista.tipo_veiculo, tipo_veiculo_necessario)`. Rota fora da faixa de veículo grande (classificação `None`) não é afetada -- comportamento idêntico a antes.

### 2.5. Cadastro de motorista: `regras/preferencias_motoristas.py`

Novo campo `tipo_veiculo` no dataclass `MotoristaPreferencias`, populado pela coluna `TIPO_VEICULO` da planilha (preenchimento manual, mesmo padrão de `PLACA`). Valor ausente ou não reconhecido vira `None` -- motorista continua elegível pra rota comum, mas nunca pra rota de veículo grande.

### 2.6. Incremento de rota existente: `incrementar_rotas.py`

Rota já classificada como veículo grande (`tipo_veiculo` recalculado a partir dos pedidos já nela) usa o teto de caixas/endereços do PRÓPRIO tipo ao decidir se um pedido novo cabe (`_cabe_na_rota`), em vez do teto genérico de 100 caixas/16 paradas -- evita que o incremento horário estoure a capacidade real do veículo já designado, ou rejeite pedidos que caberiam perfeitamente numa rota de Truck só porque "qtd > 16".

### 2.7. Persistência e UI (`painel_agentes/`)

- `rascunhos_rota.py`: coluna `tipo_veiculo TEXT` em `rascunhos_rota` (+ migração pra banco já existente), propagada em `criar_lote_rascunhos` e `duplicar_rascunho`.
- `planejamento_rotas.py`: `_badges_trava` troca as travas de paradas/caixas de última milha pelas do PRÓPRIO tipo quando o rascunho está classificado (nível 3/4 não se aplica -- só entra pedido nível 1/2/3 nesses grupos). Payload `travas.tipos_veiculo` exposto pro front. **Correção**: `alocar_motoristas_rascunhos` reconstruía o sublote sem o campo `dimension_3`, o que faria `classificar_tipo_veiculo` tratar toda parada como 1 caixa -- corrigido incluindo `p["volume_caixas"]`.
- `planejamento_rotas.html`: badge com o nome do tipo no cabeçalho do card; barra de capacidade troca "paradas" por "endereços" e usa o teto do tipo quando classificado.
- `laboratorio_rotas.py`: rótulo `[veículo: X]` no nome de cada rota do laboratório; novo cenário sintético (4 endereços/170 caixas) valida a consolidação.

---

## 3. Arquivos Alterados

- `regras/tipo_veiculo.py` **[NOVO]**
- `regras/preferencias_motoristas.py`
- `roteirizacao/roteirizacao_dados.py`
- `roteirizacao/alocacao_motoristas.py`
- `roteirizacao/criar_rotas_diarias.py`
- `roteirizacao/incrementar_rotas.py`
- `painel_agentes/rascunhos_rota.py`
- `painel_agentes/planejamento_rotas.py`
- `painel_agentes/templates/planejamento_rotas.html`
- `painel_agentes/laboratorio_rotas.py`

**Fora do escopo**: `roteirizacao/roteirizar.py` (agente legado, `TAMANHO_MAXIMO_ROTA` já desatualizado e 1 único veículo fixo -- não é usado pelo fluxo atual).

---

## 4. Plano de Validação e Testes

1. **Empacotamento isolado** (`separar_pedidos_exclusivos`): 4 endereços de ~40-50cx cada (170cx total) devem virar 1 sublote só, classificado VAN/HR; 2 endereços de ~1000-700cx devem virar Truck (1700cx); um 4º endereço que "estoura" um cluster de 3/4 válido não deve derrubar o cluster inteiro (fica com o último estado válido).
2. **Alocação**: motorista com `TIPO_VEICULO=TRUCK` deve ser elegível pra rota classificada `VUC`/`3/4`/`VAN_HR`; motorista com `VAN_HR` NÃO deve ser elegível pra rota `TRUCK`; motorista sem `TIPO_VEICULO` nunca é elegível pra rota de veículo grande, mas continua elegível pra rota comum.
3. **Laboratório**: `py -3.11` rodando `buscar_dados_laboratorio(..., usar_teste=True)` -- conferir se o cenário novo (Vila Prudente, 4 endereços) aparece como 1 rota só com `[veículo: VAN/HR]`, e se o pedido "gigante" de Pinheiros (150cx) também sai tagueado.
4. **Painel de Planejamento**: gerar rascunhos com `--gerar-rascunho`, abrir a tela e conferir o badge/barra de capacidade do card de uma rota classificada; clicar "Alocar motoristas" e conferir que o motorista sugerido tem o veículo compatível.
5. `py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste` -- conferir no log se alguma rota sai com `[veículo: X]` quando o lote do dia tiver grupos que justifiquem.

---

## 5. Critérios de Aceitação

- [x] 4 categorias de veículo (VAN/HR, VUC, 3/4, Truck) com peso/caixas/endereços/mínimo definidos em `regras/tipo_veiculo.py`.
- [x] Grupo de até 4 (ou 2, pra Truck) endereços diferentes cujo volume combinado cruza o piso de alguma categoria sai como rota exclusiva, em vez de espalhado entre rotas comuns.
- [x] Rota de última milha (fora da faixa de veículo grande) continua com os limites de sempre (16 paradas/100 caixas), sem regressão.
- [x] Alocação de motorista respeita o tipo de veículo necessário, com motorista de capacidade maior cobrindo demanda menor.
- [x] Motorista sem `TIPO_VEICULO` cadastrado nunca é alocado numa rota de veículo grande.
- [x] Peso (kg) documentado mas não aplicado como trava (dado não disponível na criação de rota).
- [ ] Coluna `TIPO_VEICULO` preenchida em `dados/BD_MOTORISTAS.xlsx` pros motoristas reais (passo manual do Hugo, fora do escopo de código).
