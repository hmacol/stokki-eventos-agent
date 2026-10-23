# Espera de janela + rota da tarde: plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a rota da manhã para de rodar km à toa pra matar tempo; pedido cuja janela abre às 12h ou depois e que não cabe no horário de nenhuma rota da manhã vai pra uma rota da tarde própria (rascunho sem motorista, saída calculada).

**Architecture:**
- O sequenciador e a trava de janela (`roteirizacao_dados.py`) ganham:
  - peso de espera 10;
  - "mesmo endereço seguido";
  - trava de espera antes de janela tardia;
  - hora de saída por rota.
- Um módulo novo, `roteirizacao/rota_tarde.py`, separa os pedidos tardios depois do polimento e monta as rotas da tarde.
- `planejar_sublotes` devolve essas rotas num campo novo do plano, e os dois emissores (job e botão Roteirizar) gravam esses rascunhos com `turno='TARDE'`.

**Tech Stack:** Python 3.11, unittest, SQLite, Flask/Jinja (painel).

**Spec:** `docs/superpowers/specs/2026-10-23-espera-janela-rota-tarde-design.md`

## Global Constraints

- Sempre `py -3.11`. Testes com `py -3.11 -m unittest ...`. Testes de `painel_agentes` rodam de dentro de `painel_agentes/` e nunca no mesmo comando de `roteirizacao`.
- Constantes novas em `roteirizacao/roteirizacao_dados.py`, cada uma com comentário de quem decidiu:
  - `PESO_ESPERA_JANELA_KM = 10.0` (era 30; Claude, 23/10)
  - `HORA_CORTE_TARDE = "12:00"` (Hugo, 23/10)
  - `ESPERA_MAXIMA_ROTA_HORAS = 0.75` (Claude, 23/10)
- A rota da tarde nunca sai antes de `HORA_INICIO_ROTA` ("06:00"). A saída é arredondada pra baixo em múltiplos de 15 min.
- **Nome da rota inalterado** (`Planejamento - DD/MM/AAAA - #N`). A marca de tarde é a coluna nova `rascunhos_rota.turno` (`'TARDE'` ou NULL).
  - Motivo: `planejamento_rotas._PADRAO_INDICE_ROTA = r"#(\d+)\s*$"` e `portal_cliente/dados_cliente._PADRAO_ROTA_PLANEJAMENTO` quebrariam com sufixo no nome.
  - **É mudança de schema** (ALTER TABLE idempotente, no padrão de `rota_fraca_motivo`). O Hugo aprova junto com este plano.
- A trava de espera e a rota da tarde valem só onde `exige_orcamento_horas(sublote)` é True. Viagem, veículo grande, nível 4 e rota inviável só por distância ficam como estão.
- Rota da tarde: sem motorista sugerido; não conta em `rotas_sem_motorista` nem em `contagem_alocacoes_dia`; não passa por `absorver_rotas_fracas` nem `_aplicar_segurar`.
- Nada de push, deploy, escrita na Vuupt/Stokki ou no banco de produção. Commits só no ramo `espera-rota-tarde`.
- Commits no padrão `Roteirizacao: ...` / `Planejamento: ...`, em português, terminando com:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`

## Review Focus

1. **Pedido de janela tardia sem coordenada:** não pode quebrar a separação. Ele fica onde está (não vai pra tarde). → teste na Task 4.
2. **Saída calculada antes das 06:00** (pedido de janela 12:00 muito longe da base): a rota sai às 06:00, nunca antes. → teste na Task 4.
3. **Dois pedidos tardios no mesmo endereço** (reentrega + novo): vão juntos pra mesma rota da tarde, seguidos. → teste na Task 4.
4. **Pedido tardio com nível 4, ou numa rota de viagem:** fica intocado na rota dele. → teste na Task 4.
5. **"Roteirizar" da tela num dia que já tem rota da tarde:** a numeração `#N` continua sem colidir, e a tela abre sem erro com rascunho `turno='TARDE'`. → teste na Task 6.

---

### Task 0: Worktree e base atualizada

**Files:** nenhum arquivo de código.

- [ ] **Step 1:** Na pasta principal, rode `git worktree add .claude/worktrees/espera-rota-tarde -b espera-rota-tarde otimizacao-rapida` e trabalhe só nesse worktree daqui em diante.
- [ ] **Step 2:** `git fetch origin` e `git merge --no-edit origin/master`. Se der conflito, resolva mantendo as duas mudanças. Os conflitos esperados são em `painel_agentes/planejamento_rotas.py`, `painel_agentes/rascunhos_rota.py` e `MAPA_DO_SISTEMA.txt`. Se um conflito exigir escolher entre comportamentos, **pare e pergunte ao Hugo**.
- [ ] **Step 3:** Copie `config.yaml` e `dados/dados_replay.db` do worktree `otimizacao-rapida` (gitignored). Nunca commite nenhum dos dois.
- [ ] **Step 4:** Rode os testes de base:
  - `py -3.11 -m unittest roteirizacao.test_janelas_horario roteirizacao.test_orcamento_horas roteirizacao.test_polimento_rotas roteirizacao.test_custo_janela_rapido roteirizacao.test_pedido_cabe_no_trajeto`
  - de dentro de `painel_agentes/`: `py -3.11 -m unittest test_plano_original`
  - Esperado: tudo OK. Se algo falhar já na base, anote e avise antes de seguir.

### Task 1: Mesmo endereço seguido na sequência

**Files:**
- Modify: `roteirizacao/roteirizacao_dados.py` (`ordenar_com_janelas` e uma função nova logo acima dela)
- Test: `roteirizacao/test_espera_rota_tarde.py` (novo; as Tasks 2–4 acrescentam testes nele)

**Interfaces:**
- Produces: `_juntar_mesmo_endereco(ordem: list[dict], resolver) -> list[dict]`. Paradas com a mesma coordenada (lat e lng arredondadas em 5 casas) vão pra logo depois da primeira ocorrência, mantendo a ordem relativa entre si. Parada sem coordenada não se move. É aplicada no retorno dos dois caminhos de `ordenar_com_janelas`, inclusive no retorno antecipado de `n <= 2`.

- [ ] **Step 1: Teste que falha.** Monte uma fixture com as 14 paradas da seção 9 da spec, como dicts `{"code", "latitude", "longitude", "_nivel_dificuldade", "_janela_inicio", "_janela_fim"}`, e base `(-23.4930, -46.6640)` via `definir_coords_base`.
  - `test_mesmo_endereco_fica_seguido`: em `ordenar_com_janelas(paradas, *BASE)`, os índices de `#PS-42216-R1` e `#PS-42216` diferem em 1.
  - `test_juntar_mesmo_endereco_mantem_resto`: `_juntar_mesmo_endereco([A, B, A2, C])`, com A e A2 na mesma coordenada, devolve `[A, A2, B, C]`.
- [ ] **Step 2:** `py -3.11 -m unittest roteirizacao.test_espera_rota_tarde -v`. Esperado: FAIL (função inexistente / índices separados).
- [ ] **Step 3:** Implemente `_juntar_mesmo_endereco` e aplique-a nos retornos de `ordenar_com_janelas`.
- [ ] **Step 4:** Rode o mesmo comando, mais `roteirizacao.test_janelas_horario roteirizacao.test_custo_janela_rapido`. Esperado: PASS.
- [ ] **Step 5:** Commit `Roteirizacao: pedidos no mesmo endereco saem seguidos na sequencia`.

### Task 2: Peso de espera 10, hora de saída por rota e espera antes de janela tardia

**Files:**
- Modify: `roteirizacao/roteirizacao_dados.py` (constantes perto da linha 956; `simular_horarios`, `ordenar_com_janelas`, `janela_respeitada`, `janela_viavel`; helpers novos perto de `start_at_rota`)
- Test: `roteirizacao/test_espera_rota_tarde.py`

**Interfaces:**
- Produces:
  - `PESO_ESPERA_JANELA_KM = 10.0`, `HORA_CORTE_TARDE = "12:00"`, `ESPERA_MAXIMA_ROTA_HORAS = 0.75`.
  - `eh_janela_tardia(servico: dict) -> bool`: True se `extrair_janela(s)` existe e o início é ≥ `HORA_CORTE_TARDE`.
  - `simular_horarios(...)` ganha a chave `"espera_tardia_h"` no retorno: a soma das esperas só nas paradas com `eh_janela_tardia`. As chaves atuais não mudam.
  - `ordenar_com_janelas(..., hora_saida: float | None = None)`, `janela_respeitada(..., hora_saida: float | None = None)` e `janela_viavel(..., hora_saida: float | None = None)`, repassando pra `simular_horarios`. O default `None` mantém o comportamento de hoje (`HORA_SAIDA_BASE`).
  - `start_at_na_hora(data_alvo: date, hora_brt: float) -> str` (UTC "YYYY-MM-DDTHH:MM:SSZ", BRT+3, mesma conta de `start_at_rota`). `start_at_rota` passa a chamá-la com `HORA_INICIO_ROTA`.
  - `hora_saida_de_start_at(start_at: str | None) -> float | None`: o inverso, aceitando "…Z" e "…-03:00". None se não parsear.

- [ ] **Step 1: Testes que falham.**
  - `test_peso_espera_nao_gera_ziguezague`: km de `ordenar_com_janelas(fixture_13)` (haversine × 1,3 entre pontos consecutivos, saindo da base) < 50. Hoje dá ~71.
  - `test_espera_tardia_conta_so_janela_tardia`: rota `[parada janela 08:00–18:00 perto da base, parada janela 15:00–23:59]` simulada às 06:00 → `espera_tardia_h` igual à espera da segunda parada e menor que `espera_h`.
  - `test_hora_saida_desloca_simulacao`: `simular_horarios([p], hora_saida=13.0)["chegadas"][0] > 13.0`.
  - `test_start_at_ida_e_volta`: `start_at_na_hora(date(2026,10,23), 13.25) == "2026-10-23T16:15:00Z"`, `hora_saida_de_start_at("2026-10-23T16:15:00Z") == 13.25` e `start_at_rota(date(2026,10,23)) == "2026-10-23T09:00:00Z"`.
- [ ] **Step 2:** Rode `roteirizacao.test_espera_rota_tarde`. Esperado: FAIL.
- [ ] **Step 3:** Implemente conforme o bloco Interfaces. Atualize também o comentário das linhas 957–960: ele hoje justifica 30 km ("esperar parado é pior que rodar"). Troque pela regra nova: peso menor que `VELOCIDADE_MEDIA_KMH`, pra rodar nunca compensar a espera.
- [ ] **Step 4:** Rode `roteirizacao.test_espera_rota_tarde roteirizacao.test_janelas_horario roteirizacao.test_orcamento_horas roteirizacao.test_custo_janela_rapido roteirizacao.test_polimento_rotas`. Testes antigos que fixavam o peso 30 ou uma ordem específica podem falhar. Ajuste cada um conscientemente e **liste no commit quais mudaram e por quê**.
- [ ] **Step 5:** Commit `Roteirizacao: peso de espera 10 e hora de saida por rota no simulador`.

### Task 3: Trava de espera antes de janela tardia

**Files:**
- Modify: `roteirizacao/roteirizacao_dados.py` (`janela_respeitada`)
- Test: `roteirizacao/test_espera_rota_tarde.py`

**Interfaces:**
- Consumes: `eh_janela_tardia`, `"espera_tardia_h"` e `hora_saida` (Task 2); `exige_orcamento_horas` (já existe).
- Produces: `janela_respeitada` devolve False quando as três condições valem:
  - `exige_orcamento_horas(sublote, api_key, coords_base)`;
  - `len(sublote) >= 2`;
  - `sim["espera_tardia_h"] > ESPERA_MAXIMA_ROTA_HORAS`.

  As regras atuais não mudam. Isso já alcança formação, fusão, reparo, polimento e incremento, porque todos passam por `janela_respeitada`/`janela_viavel`.

- [ ] **Step 1: Testes que falham.**
  - `test_trava_reprova_espera_tardia`: as 14 paradas da #13 na ordem da tabela, saída 06:00 → `janela_respeitada(...) is False`.
  - `test_trava_aceita_um_pedido`: `[wilian]` sozinho → True.
  - `test_trava_ignora_espera_da_manha`: duas paradas de janela 08:00–18:00 e 09:00–17:00 perto da base, saída 06:00 (espera > 45 min) → True.
  - `test_trava_respeita_hora_saida`: `[bar 13:00–18:00, wilian 15:00–23:59]` com `hora_saida=12.5` → True; o mesmo par com saída 06:00 → False.
- [ ] **Step 2:** Rode `roteirizacao.test_espera_rota_tarde`. Esperado: FAIL nos três que dependem da trava.
- [ ] **Step 3:** Implemente a condição em `janela_respeitada`.
- [ ] **Step 4:** Rode a mesma bateria da Task 2, Step 4, mais `roteirizacao.test_rotas_fracas` (se existir; senão `grep -l rotas_fracas roteirizacao/test_*.py`). Esperado: PASS.
- [ ] **Step 5:** Commit `Roteirizacao: trava de espera antes de janela que abre as 12h ou depois`.

### Task 4: Separar e montar as rotas da tarde

**Files:**
- Create: `roteirizacao/rota_tarde.py`
- Modify: `roteirizacao/criar_rotas_diarias.py` (`planejar_sublotes`, perto das linhas 516–539)
- Test: `roteirizacao/test_espera_rota_tarde.py`

**Interfaces:**
- Consumes: Tasks 1–3; `otimizacao_rotas.ordenar_2opt`; `reparar_sublotes_por_horas`; `_tempo_perna_horas`, `_distancia_km`, `obter_coordenadas`.
- Produces:
  - `separar_pedidos_tarde(sublotes: list[list[dict]], coords_base, api_key) -> tuple[list[list[dict]], list[dict]]`. Pra cada sublote com `exige_orcamento_horas` (ou com 1 parada) que reprova em `janela_respeitada`, ou que é uma rota de 1 parada com `eh_janela_tardia`:
    1. tira, um por vez, o pedido tardio que mais espera;
    2. resequencia o resto com `ordenar_2opt`;
    3. para quando a rota passa ou não sobra pedido tardio.

    Pedido sem coordenada, nível 4 ou de rota de viagem nunca sai. Devolve (sublotes da manhã sem os vazios, pedidos tardios).
  - `hora_saida_tarde(sublote: list[dict], coords_base, api_key) -> float`: `min(início da janela)` dos tardios menos `_tempo_perna_horas(distância da base à 1ª parada tardia na ordem)`, arredondado pra baixo em múltiplos de 0,25 e com piso em `HORA_SAIDA_BASE`.
  - `montar_rotas_tarde(pedidos: list[dict], coords_base, api_key) -> list[tuple[list[dict], float]]`:
    1. sequencia todos com `ordenar_com_janelas(..., hora_saida=hora_saida_tarde(...))`;
    2. quebra com as travas de sempre: reparo por horas e janela com a `hora_saida` da própria rota; caixas e paradas pelos tetos de `criar_rotas_diarias`;
    3. devolve (sublote, hora_saida) por rota.

    Lista vazia → `[]`.
  - `planejar_sublotes` passa a fazer `sublotes, tardios = separar_pedidos_tarde(...)` logo depois de `_polir_particao` e **antes** de `_absorver_fracas_particao`. O plano ganha `"tarde": montar_rotas_tarde(tardios, ...)`, com `[]` quando não há. A conferência de cobertura (linhas 531–538) passa a somar os pedidos de `plano["tarde"]`.

- [ ] **Step 1: Testes que falham.**
  - `test_13_wilian_vai_pra_tarde`: `separar_pedidos_tarde([fixture_13], BASE, None)` → wilian em `tardios`, e o resto passa em `janela_respeitada`.
  - `test_hora_saida_tarde`: wilian sozinho → saída igual a `floor4(15,0 − perna)`, em múltiplos de 0,25.
  - `test_hora_saida_tem_piso`: pedido com janela 12:00 a ~80 km da base → 6,0.
  - `test_tarde_mesmo_endereco_seguidos`: dois tardios na mesma coordenada → mesma rota, índices consecutivos.
  - `test_tarde_bar_e_talita_quebra`: bar 13:00–18:00 no Centro e Talita 18:30–19:30 na Zona Oeste → 2 rotas.
  - `test_tardio_sem_coordenada_fica`: tardio sem lat/lng fica no sublote de origem.
  - `test_tardio_nivel4_e_viagem_ficam`: tardio com `_nivel_dificuldade=4`, e outro numa rota que `exige_orcamento_horas` recusa → nenhum sai.
  - `test_dia_sem_tardio_sem_tarde`: `montar_rotas_tarde([], BASE, None) == []`.
- [ ] **Step 2:** Rode `roteirizacao.test_espera_rota_tarde`. Esperado: FAIL.
- [ ] **Step 3:** Implemente `rota_tarde.py` e a ligação em `planejar_sublotes`, no estilo do arquivo: docstring com data e quem decidiu; constantes importadas de `roteirizacao_dados`.
- [ ] **Step 4:** Rode a bateria da Task 3, Step 4. Esperado: PASS.
- [ ] **Step 5:** Commit `Roteirizacao: rota da tarde para pedido de janela tardia que nao cabe na manha`.

### Task 5: Gravar a rota da tarde (job e botão Roteirizar)

**Files:**
- Modify:
  - `painel_agentes/rascunhos_rota.py`: migração perto das linhas 190–198; INSERT em `criar_lote_rascunhos`, perto das linhas 311–343
  - `roteirizacao/criar_rotas_diarias.py`: `roteirizar_para_rascunhos`, linhas 721–779; `main` / `_rotear_particao`, linhas 975–1121
- Test: `roteirizacao/test_espera_rota_tarde.py` (emissão) e `painel_agentes/test_rota_tarde_tela.py` (novo; gravação)

**Interfaces:**
- Consumes: `plano["tarde"]` (Task 4); `start_at_na_hora` (Task 2).
- Produces:
  - Coluna `rascunhos_rota.turno TEXT`, nullable, criada por `ALTER TABLE` idempotente.
  - O dict de rascunho aceita a chave `"turno"`, gravada no INSERT.
  - Pra cada `(sublote, hora)` de `plano["tarde"]`:
    - nome no padrão de sempre, com o `#N` seguinte;
    - `"turno": "TARDE"`;
    - `start_at = start_at_na_hora(data_alvo, hora)`;
    - motorista/agent/vehicle None;
    - `rota_fraca_motivo` None;
    - não incrementa `contagem` nem `rotas_sem_motorista`.

    No caminho sem rascunho de `main` (cria direto na Vuupt), mesma regra, com `start_at` próprio e sem motorista.
  - Log: `[TARDE] '<nome>' sai HH:MM com N pedido(s): [...]`.
  - O resumo da etapa "Criação de rotas" ganha ` Rota(s) da tarde: N (P pedido(s)).` quando N > 0.

- [ ] **Step 1: Testes que falham.**
  - `test_roteirizar_para_rascunhos_emite_tarde`: com `planejar_sublotes` substituído por mock devolvendo um plano com `"tarde": [([wilian], 13.25)]`, o rascunho correspondente tem `turno == "TARDE"`, `start_at == "…T16:15:00Z"` e `agent_id is None`.
  - Em `painel_agentes/test_rota_tarde_tela.py`: `test_lote_grava_turno`. `criar_lote_rascunhos` num banco temporário (mesmo padrão de `test_enviar_rascunho_erro_envio.py`) grava e lê `turno='TARDE'`.
- [ ] **Step 2:** Rode os dois (em comandos separados). Esperado: FAIL.
- [ ] **Step 3:** Implemente.
- [ ] **Step 4:**
  - Rode os dois testes novos, mais `test_plano_original` e `test_enviar_rascunho_erro_envio` dentro de `painel_agentes/`. Esperado: PASS.
  - Depois, rode o job em modo teste: `py -3.11 roteirizacao/criar_rotas_diarias.py --modo-teste` (só se o banco local e o config permitirem; senão anote que pulou). Ele não escreve na Vuupt. Confira no log se aparece `[TARDE]` ou nenhuma rota da tarde, sem Traceback.
- [ ] **Step 5:** Commit `Roteirizacao: grava a rota da tarde como rascunho sem motorista (coluna turno)`.

### Task 6: Tela do Planejamento

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py` (`_simular_rascunho` na linha 321 e quem a chama nas linhas 415 e 1068; `_badges_trava` na linha 370)
- Test: `painel_agentes/test_rota_tarde_tela.py`

**Interfaces:**
- Consumes: `hora_saida_de_start_at` (Task 2); coluna `turno` (Task 5).
- Produces:
  - `_simular_rascunho(paradas, hora_saida: float | None = None)`. Quem chama passa `hora_saida_de_start_at(rascunho["start_at"])`.
  - `_badges_trava` acrescenta `"Tarde · sai HH:MM"` (via `_horas_para_hhmm`) quando `rascunho.get("turno") == "TARDE"`.
  - O rascunho da tarde não recebe o badge de "rota fraca" nem o de "sem motorista", se houver.

- [ ] **Step 1: Testes que falham.**
  - `test_badge_tarde`: rascunho com `turno='TARDE'` e `start_at="2026-10-23T16:15:00Z"` → um badge igual a `"Tarde · sai 13:15"`.
  - `test_simulacao_usa_saida_da_rota`: a mesma rota → primeira chegada > 13,25.
  - `test_roteirizar_continua_numeracao_com_tarde`: com um rascunho `#18` `turno='TARDE'` na data, `rascunhos_rota._proximo_numero_ordem` devolve 19.
- [ ] **Step 2:** Rode dentro de `painel_agentes/`: `py -3.11 -m unittest test_rota_tarde_tela -v`. Esperado: FAIL.
- [ ] **Step 3:** Implemente.
- [ ] **Step 4:**
  - Testes PASS.
  - Depois suba o painel em porta de teste: `py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"`, de dentro de `painel_agentes/` do worktree.
  - Com um lote local que tenha rascunho `turno='TARDE'` (crie à mão no banco **local** do worktree), abra `/planejamento` e confira o selo e as chegadas a partir da saída. Tire screenshot. Pare o processo que você mesmo subiu.
- [ ] **Step 5:** Commit `Planejamento: selo "Tarde · sai HH:MM" e linha do tempo a partir da saida da rota`.

### Task 7: Incremento respeita a rota da tarde

**Files:**
- Modify: `roteirizacao/incrementar_rotas.py` (`_cabe_na_janela`, perto da linha 741, e onde `rota_info` é montada a partir de `rota.get("start_at")`, perto da linha 245)
- Test: `roteirizacao/test_espera_rota_tarde.py`

**Interfaces:**
- Consumes: `janela_viavel(..., hora_saida=...)`, `hora_saida_de_start_at` (Task 2).
- Produces: `rota_info` ganha `"hora_saida": hora_saida_de_start_at(start_at)`, e `_cabe_na_janela` passa `hora_saida=rota_info.get("hora_saida")`. Nenhuma rota da tarde nova nasce no incremento. O órfão continua com o comportamento de hoje.

- [ ] **Step 1: Teste que falha.** `test_incremento_tardio_cabe_na_tarde`: `janela_viavel([wilian, outro tardio perto], hora_saida=13.0)` é True e, com `hora_saida=None`, False. Se o código de `_cabe_na_janela` puder ser exercitado isolado, teste por ele; senão, por `janela_viavel`, e anote.
- [ ] **Step 2:** FAIL. **Step 3:** Implemente. **Step 4:** PASS, mais `roteirizacao.test_pedido_cabe_no_trajeto`.
- [ ] **Step 5:** Commit `Roteirizacao: incremento avalia a janela pela saida real da rota`.

### Task 8: Replay de outubro e documentação

**Files:**
- Modify: `roteirizacao/replay_rotas.py` (linhas 190–200 e argparse a partir da linha 223), `DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md` (seção 8.2), `MAPA_DO_SISTEMA.txt`
- Output: tabela no relatório final pro Hugo (não commitar dados)

**Interfaces:**
- Produces:
  - Flag `--peso-espera <float>`: sobrescreve `PESO_ESPERA_JANELA_KM`.
  - Flag `--sem-rota-tarde`: desliga a trava e a separação via uma chave de módulo `rota_tarde.ATIVO = True`, que o replay troca pra False.
  - O replay soma as rotas de `plano["tarde"]` nas métricas do plano novo e imprime à parte: rotas da tarde, pedidos na tarde, maior dia.

- [ ] **Step 1:** Atualize `dados/dados_replay.db` do worktree com uma cópia de leitura da VPS. Siga o mesmo procedimento de 16/10 descrito no cabeçalho de `replay_rotas.py`. Se isso exigir algo além de leitura, **pergunte ao Hugo**.
- [ ] **Step 2:** Implemente as flags e a soma. Teste rápido: `py -3.11 roteirizacao/replay_rotas.py --de 2026-10-21 --ate 2026-10-23` roda sem Traceback.
- [ ] **Step 3:** Rode `--de 2026-10-01 --ate 2026-10-22` em três variantes:
  - (a) `--sem-rota-tarde --peso-espera 30`, que é o comportamento de hoje;
  - (b) `--sem-rota-tarde`, só o peso novo;
  - (c) o padrão, com tudo ligado.

  Monte a tabela com:
  - km total
  - rotas
  - rotas acima de 9h
  - atraso evitável (h)
  - espera total (h)
  - rotas e pedidos da tarde por dia (média e máximo)
  - rotas da manhã quebradas pela trava
- [ ] **Step 4:** Atualize a seção 8.2 do DOC (trava de espera, peso 10, rota da tarde, coluna `turno`) e o `MAPA_DO_SISTEMA.txt` (arquivo `rota_tarde.py`, coluna `turno`).
- [ ] **Step 5:** Commit `Roteirizacao: replay compara peso de espera e rota da tarde; doc e mapa`.
- [ ] **Step 6: Pare e entregue ao Hugo:**
  - a tabela;
  - a recomendação. Se (c) não tiver km menor que (a), ou se as rotas acima de 9h / o atraso subirem, recomende ajustar `ESPERA_MAXIMA_ROTA_HORAS` ou o peso, e não subir;
  - a lista de testes antigos que mudaram.

  Sem push nem deploy.
