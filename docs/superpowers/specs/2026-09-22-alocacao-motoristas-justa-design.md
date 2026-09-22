# Alocacao automatica de motoristas: rotacao justa, rotas longas e FIORINO

Data: 22/09/2026
Modulo: Roteirizacao (`roteirizacao/`, `regras/`)
Substitui: `2026-09-21-escala-motoristas-design.md` (escala manual por numero, descartada)

## 1. Problema

Duas coisas, que se cruzam no mesmo ponto do codigo.

**A escolha do motorista é miope.** `selecionar_motorista_equitativo`
(`roteirizacao/alocacao_motoristas.py:47`) ordena os elegiveis por
`(alocacoes_hoje, agent_id)` (`:121`). Com `MAX_ROTAS_DIA = 1` em 28 dos 30
motoristas, `alocacoes_hoje` é quase sempre 0 para todo mundo — na pratica **a
escolha é por `agent_id` crescente**. Quem tem id baixo roda quase todo dia;
quem tem id alto pode passar semanas parado, e o sistema nao percebe. Nenhum
historico entra na conta. Uma rota de 9h e uma de 2h valem a mesma coisa.

**O gatilho de veiculo grande nao cobre o caso real.** Hoje
`_extrair_grupos_veiculo_grande` (`roteirizacao_dados.py:1520`) agrupa por
endereco e depois **cresce** anexando ate 4 enderecos vizinhos, aceitando o
grupo so quando ele alcanca o `volume_minimo_cx` do tipo (VAN/HR exige 150 cx,
`regras/tipo_veiculo.py:41`). Consequencia: **dois pedidos de 60 caixas para o
mesmo endereco vao para rotas diferentes** — 120 estoura o teto de 100 da rota
comum (`VOLUME_MAXIMO_ROTA`, `criar_rotas_diarias.py:127`) e nao alcanca o
minimo de 150 da VAN/HR. O caminhao certo existe e nao é usado.

**E o catalogo nao tem o carro da frota.** `TIPOS_VEICULO` comeca na VAN/HR.
Fiorino, que é o carro da maioria, nao existe — 27 dos 30 motoristas estao com
`TIPO_VEICULO` vazio na `dados/BD_MOTORISTAS.xlsx`.

## 2. O que este projeto entrega

1. FIORINO no catalogo de veiculos e na planilha.
2. Regra nova de veiculo grande: **1 endereco acima de 100 caixas**.
3. Alocacao automatica que roda os motoristas de forma justa, com rodizio
   proprio para as rotas longas.

Nao ha tela nova. O Hugo nao digita nada — o numero de rotas do dia ja diz
quantos motoristas sao necessarios.

## 3. Decisoes (Hugo, 21-22/09/2026)

| # | Decisao | Escolha |
|---|---------|---------|
| D1 | FIORINO | Tipo novo no catalogo, **100 caixas**, default de quem esta sem `TIPO_VEICULO` |
| D2 | Gatilho de veiculo grande | **So 1 endereco**: 1 pedido acima de 100 cx, ou a soma dos pedidos do mesmo endereco acima de 100 cx. O crescimento por enderecos vizinhos **sai** |
| D3 | Justica | Rotacao por numero de rotas, **7 dias primeiro, 30 dias desempata** |
| D4 | Rotas longas | Rodizio proprio: quem pegou rota longa na semana nao pega a proxima. **Longa = acima de 7h estimadas** |
| D5 | Fonte das horas | Duracao **estimada**, persistida por rota. A real (`iniciada_em`/`concluida_em`) nao é usada |
| D6 | Area | **Trava rigida, como hoje.** A justica opera dentro de cada zona |
| D7 | Escopo | So a alocacao automatica. A tela de escala por numero foi descartada |
| D8 | Planilha | FIORINO nos 27 vazios, menos LALAMOVE (virtual) e Hugo Macol (TESTE) |
| D9 | O que conta como "rodou" | Rota nao cancelada em `nucleo_rotas` + rascunho em `RASCUNHO/OFERTADA/ENVIADO` (contagem que o marketplace ja usa) |

## 4. FIORINO no catalogo

### 4.1 Faixas contiguas

Com D2, o `volume_minimo_cx` perde a funcao que tinha (barrar grupos pequenos
demais) e vira simplesmente o teto do tipo anterior. As faixas ficam
**contiguas**, sem buraco:

| Tipo | Caixas | Observacao |
|---|---|---|
| FIORINO | ate 100 | é a rota comum de ultima milha (`VOLUME_MAXIMO_ROTA = 100`) |
| VAN/HR | 101 a 400 | hoje comeca em 150 |
| VUC | 401 a 600 | hoje comeca em 300 |
| 3/4 | 601 a 1200 | hoje comeca em 500 |
| Truck | 1201 a 2500 | hoje comeca em 1500 |

O buraco de hoje (101-149 caixas num endereco nao tem veiculo) é exatamente o
problema da secao 1, e some.

```python
TipoVeiculo("FIORINO", "Fiorino", peso_maximo_kg=650, volume_maximo_cx=100,
            volume_minimo_cx=0, max_enderecos_distintos=4,
            gera_rota_exclusiva=False),
```

`gera_rota_exclusiva` é campo novo no dataclass (default `True`; FIORINO é o
unico `False`). Duas listas derivadas:

| Lista | Conteudo | Quem usa |
|---|---|---|
| `TIPOS_VEICULO` | os 5, capacidade crescente | `ordem_capacidade` → `veiculo_comporta`; `tipo_por_codigo` |
| `TIPOS_VEICULO_EXCLUSIVOS` | os 4 maiores | `classificar_tipo_veiculo`, `teto_caixas_para_enderecos`, `VOLUME_MAXIMO_GERAL_CX` |

FIORINO **nunca** pode sair de `classificar_tipo_veiculo`: esse retorno é o
gatilho de "vira rota exclusiva de veiculo grande" em ~12 pontos do pipeline
(`roteirizacao_dados.py:547`, `:878`, `:1482`, `:1561`, `:1580`,
`criar_rotas_diarias.py:579`, `:812`, `otimizacao_rotas.py:82`, `:517`,
`polimento_rotas.py:60`, `selecao_modelo.py:79`, `benchmark_modelos.py:134`).
Se ele vazar, **toda** rota comum vira exclusiva e o plano do dia muda inteiro.

`_ORDEM_CODIGO` (hoje privado) ganha um acessor publico, para o resto do
projeto nao cutucar o privado:

```python
def ordem_capacidade(codigo: str | None) -> int | None:
    """Posicao na ordem de capacidade crescente (FIORINO=0 ... TRUCK=4),
    None se vazio/nao reconhecido."""
```

### 4.2 Acima do Truck

Um endereco com mais de 2500 caixas nao cabe em nenhum tipo. Corrigindo o que
esta escrito antes: isso **nao** cai no caminho do "pedido gigante"
(`roteirizacao_dados.py:1603`), que é por pedido INDIVIDUAL acima de 100 cx. Um
endereco somando 3000 caixas em pedidos de 80 cai hoje no pool comum e vira ~30
rotas pequenas para o mesmo portao.

Por isso o gatilho da secao 5.1 é o **teto da rota comum**, nao a existencia de
tipo: todo endereco acima de `volume_maximo` (100) vira sublote exclusivo. Se
`classificar_tipo_veiculo` nao devolver tipo (acima de 2500), o sublote é
extraido mesmo assim, com `[ALERTA_ALOCACAO]` no log dizendo que excede a maior
capacidade do catalogo — melhor 1 rota sinalizada que 30 rotas silenciosas.
Dividir em varias rotas de Truck é outro projeto.

### 4.3 Efeito em `veiculo_comporta`

Ordem passa a ser `FIORINO < VAN_HR < VUC < TRES_QUARTOS < TRUCK`:

- motorista FIORINO em rota de veiculo grande → `False`
- motorista de qualquer tipo em rota comum (`tipo_necessario=None`) → `True`

Isso **nao afrouxa nada**: hoje `veiculo_comporta(None, X)` ja devolve `False`
para todo `X` grande, e `veiculo_comporta("FIORINO", X)` devolve `False` pelos
mesmos casos. A mudanca é de rotulo.

### 4.4 Planilha e leitura

- `regras/preferencias_motoristas.py:227-238`: `TIPO_VEICULO` vazio ou nao
  reconhecido passa a ser lido como `FIORINO`.
- `dados/BD_MOTORISTAS.xlsx` (versionada, excecao no `.gitignore:24`): preencher
  `FIORINO` nos 27 vazios, **menos** `LALAMOVE (virtual)` e
  `Hugo Macol Sousa (TESTE)`, que nao sao carros reais e ficam vazios.
- `regras/cadastro_motoristas.py` e a tela `/motoristas` passam a oferecer
  FIORINO entre as opcoes.
- Apelidos aceitos apos a normalizacao existente: `FIORINO`, `FIO`.

**Nao entra neste projeto** (achado, nao pedido): `Rafael Batista Ribeiro`
(VAN_HR) e `LALAMOVE (virtual)` estao com `ZONAS_PREFERIDAS` vazio, o que pela
trava de zona os torna nunca elegiveis a rota com zona reconhecida. Relatado ao
Hugo, sem alteracao.

## 5. Regra nova de veiculo grande (D2)

### 5.1 O que substitui o que

`_extrair_grupos_veiculo_grande` (`roteirizacao_dados.py:1520-1589`) é
**substituida** por uma versao sem crescimento:

```
para cada endereco distinto em `demais`:
    caixas = soma das caixas dos pedidos daquele endereco
    se caixas <= volume_maximo (100):
        volta pro pool comum
    senao:
        aquele endereco inteiro vira 1 sublote exclusivo
        se classificar_tipo_veiculo(caixas, 1) é None:  # acima de 2500
            [ALERTA_ALOCACAO] no log, mas extrai do mesmo jeito (4.2)
```

Sai junto: `_cabe_na_distancia` aplicado ao crescimento (`:1572`), o laco guloso
de anexar vizinhos (`:1564-1581`) e o uso de `teto_caixas_para_enderecos` nesse
laco (`:1565`). `_agrupar_por_endereco` (`:1496`) continua, e é o unico
agrupamento que sobra.

O caso "1 pedido acima de 100 cx" nao precisa de codigo novo: um pedido sozinho
num endereco ja cai na mesma regra. O isolamento de "pedido gigante"
(`:1595-1606`) continua para o que excede o Truck (4.2).

`classificar_tipo_veiculo` passa a ser chamada **sempre com 1 endereco**, o que
torna `max_enderecos_distintos` inerte na pratica (inclusive o limite de 2 do
Truck). O campo fica, documentado como inerte.

Nivel 4 (`_empacotar_grupo_nivel4`, `:1457`) ja trabalha por chave de endereco e
ja classifica com 1 endereco (`:1482`) — **nao muda**.

### 5.2 O que isso muda no plano de rotas

Mais rotas exclusivas (o caso 101-149 cx passa a existir) e menos rotas grandes
multi-endereco. O saldo em numero de rotas, km e horas **tem que ser medido
antes do deploy** — ver secao 8.

## 6. Alocacao: a ordem nova

### 6.1 O que trava (sem mudanca)

`_elegibilidade_sublote` (`alocacao_motoristas.py:125-170`) fica **igual**:
ativo, disponivel no dia, `MAX_ROTAS_DIA`, viagem, **zona (trava rigida, D6)**,
rodizio de placa e `veiculo_comporta`. Nenhum criterio novo de exclusao.

### 6.2 O que ordena

```python
elegiveis.sort(key=lambda m: (
    contagem_alocacoes_dia.get(m.agent_id, 0),           # 1. carga do proprio dia
    longas_7d.get(m.agent_id, 0) if rota_longa else 0,   # 2. so em rota longa
    rotas_7d.get(m.agent_id, 0),                         # 3. rodou menos na semana
    rotas_30d.get(m.agent_id, 0),                        # 4. desempate no mes
    m.agent_id,                                          # 5. desempate estavel
))
```

1. **Carga do dia** continua em primeiro: preserva o comportamento atual e
   impede dar a 2a rota a alguem enquanto outro esta com zero.
2. **Rotas longas em 7 dias** so pesa quando a rota sendo alocada é longa
   (D4). Numa rota curta o termo é 0 para todos e nao interfere. É isto que
   realiza "quem pegou uma longa hoje nao pega outra amanha".
3 e 4. **Justica geral** (D3), as mesmas janelas do marketplace.
5. `agent_id` deixa de ser o criterio efetivo e vira o ultimo desempate.

`rota_longa` = `estimar_tempo_rota(sublote) > roteirizacao.rota_longa_horas`
(config, padrao **7.0**). O sublote chega aqui ja sequenciado e com
`COORDS_BASE` definida (`criar_rotas_diarias.py:559`), entao a estimativa é
direta e barata.

### 6.3 Assinatura

```python
def selecionar_motorista_equitativo(
    sublote, data_rota, motoristas, contagem_alocacoes_dia,
    api_key=None, ajustes_disponibilidade=None,
    rotas_7d: dict[int, int] | None = None,
    rotas_30d: dict[int, int] | None = None,
    longas_7d: dict[int, int] | None = None,
    rota_longa: bool = False,
): ...
```

`rota_longa` é **booleano ja decidido por quem chama**, nao o limiar: assim
`alocacao_motoristas.py` nao precisa importar `estimar_tempo_rota` nem estimar
tempo, e o teste da ordenacao nao depende de coordenadas. Quem chama faz
`estimar_tempo_rota(sublote) > roteirizacao.rota_longa_horas`.

**Compatibilidade:** com os parametros novos ausentes, a chave vira
`(carga, 0, 0, 0, agent_id)`, que ordena identico a `(carga, agent_id)` de hoje.
Chamadores que nao passarem nada mantem o comportamento atual — inclusive
`incrementar_rotas.py:366` e `:735`.

### 6.4 Quem carrega as contagens

Sao caras demais para chamar por rota. Cada chamador carrega **uma vez por
execucao** e repassa:

| Chamador | Onde |
|---|---|
| Job noturno / "Criar rotas" | `criar_rotas_diarias.py:580` e `:815` (antes do laco, em `_rotear_particao`) |
| Botao "Alocar motoristas" | `painel_agentes/planejamento_rotas.py:1293` |

### 6.5 Ordem de processamento dos sublotes

Mantida a ordenacao por escassez (`contar_motoristas_elegiveis`,
`criar_rotas_diarias.py:790-796`), que existe para corrigir um problema real de
20/08 (rota de zona escassa ficando sem ninguem). **Desempate novo: duracao
decrescente** — entre dois sublotes com a mesma escassez, a rota longa é
alocada primeiro, para nao gastar em rotas curtas quem esta "fresco" de longas.

## 7. Persistir as horas estimadas

O rodizio de rotas longas precisa saber quantas longas cada motorista pegou na
semana. Hoje a duracao estimada **nao é gravada em lugar nenhum**:
`rascunhos_rota` e `nucleo_rotas` so guardam `km_estimado`.

Coluna nova nas duas tabelas, via `ALTER TABLE ... ADD COLUMN` idempotente
(mesmo padrao de migracao ja usado no projeto):

```sql
ALTER TABLE rascunhos_rota ADD COLUMN horas_estimadas REAL;
ALTER TABLE nucleo_rotas   ADD COLUMN horas_estimadas REAL;
```

Gravada no momento em que a rota é criada, junto de `km_estimado`. A real
(`iniciada_em`/`concluida_em`) **nao é usada** (D5): o timestamp é distorcido
quando o motorista confirma varias paradas em lote na Vuupt — problema ja
conhecido e documentado em `nucleo/tempos.py:16-18`.

Funcao nova em `regras/prioridade_ofertas.py`, irma de `contar_rotas_recentes`
(`:146`) e com o mesmo dedup por `rascunho_id`/`vuupt_route_id`:

```python
def contar_rotas_longas_recentes(data_alvo, dias, limiar_horas, conn=None) -> dict[int, int]:
    """{agent_id: quantas rotas acima de `limiar_horas` na janela}.
    Rota sem horas_estimadas gravada nao conta."""
```

**Entrada em vigor gradual:** nos primeiros 7 dias apos o deploy quase nenhuma
rota tem `horas_estimadas`, todas as contagens sao 0 e o criterio nao pesa. Isso
é aceito e esperado — nao é bug. Backfill do historico nao é possivel: a
estimativa depende da sequencia final do sublote, que nao foi guardada.

## 8. Replay obrigatorio antes do deploy

A secao 5 muda o plano de rotas. **Nao vai para producao sem medicao.**

`roteirizacao/replay_rotas.py` ja existe e foi usado na recalibracao de 20/09.
Rodar 30 dias, antes e depois, e comparar:

- numero de rotas (total, e quantas sao exclusivas de veiculo grande)
- km total
- rotas acima de 9h (tem que continuar em 0)
- quantas rotas de veiculo grande por tipo — em especial quantas VAN/HR novas
  aparecem na faixa 101-149 cx, que é o ganho esperado
- rotas sem motorista alocado (nao pode aumentar)

O resultado vai para o Hugo **antes** de qualquer deploy. Se o numero de rotas
subir muito, a decisao de seguir é dele.

## 9. Testes

Sem pytest: `py -3.11 -m unittest <modulo>`.

**Novos**

- `regras/test_tipo_veiculo_fiorino.py`
  - `classificar_tipo_veiculo` **nunca** devolve FIORINO, em tabela que cobre as
    faixas e os limites (0/1/100/101/400/401/600/601/1200/1201/2500/2501 cx,
    1 a 5 enderecos)
  - faixas contiguas: 101→VAN/HR, 400→VAN/HR, 401→VUC, 1201→Truck, 2501→None
  - `veiculo_comporta("FIORINO", None)` True; `("FIORINO","VAN_HR")` False;
    `("TRUCK","FIORINO")` True
  - `ordem_capacidade` e `tipo_por_codigo("FIO"/"fiorino")`
- `regras/test_preferencias_motoristas.py` (acrescentar): `TIPO_VEICULO` vazio
  ou invalido vira `FIORINO`
- `roteirizacao/test_veiculo_grande_um_endereco.py`
  - 2 pedidos de 60 cx no mesmo endereco → 1 sublote exclusivo VAN/HR (o caso
    que hoje falha)
  - 2 pedidos de 60 cx em enderecos **diferentes** e vizinhos → **nao** forma
    veiculo grande (o crescimento acabou)
  - 1 pedido de 120 cx → VAN/HR; 1 pedido de 90 cx → pool comum
  - endereco com 3000 cx → sublote isolado + alerta, nao quebra
  - nivel 4 continua igual
- `roteirizacao/test_alocacao_justa.py`
  - com todos empatados em carga do dia, ganha quem tem menos rotas em 7d, nao
    o menor `agent_id`
  - 7d empatado → decide 30d → decide `agent_id`
  - rota longa: quem tem menos rotas longas em 7d ganha, mesmo tendo mais rotas
    no total
  - rota curta: o termo de longas nao interfere
  - zona continua travando: quem nao atende a area nao entra nem sendo o mais
    justo
  - **sem** os parametros novos, a escolha é identica a de hoje
- `regras/test_prioridade_ofertas.py` (acrescentar):
  `contar_rotas_longas_recentes` conta so acima do limiar, ignora cancelada,
  ignora rota sem `horas_estimadas`, e nao conta duas vezes a mesma rota

**Nao-regressao (obrigatoria)**

```
py -3.11 -m unittest discover -s roteirizacao -p "test_*.py"
py -3.11 -m unittest discover -s regras      -p "test_*.py"
py -3.11 -m unittest discover -s painel_agentes -p "test_*.py"
```

Os testes de `regras/` e de alocacao devem passar sem alteracao. Os de
roteirizacao que verificam formacao de veiculo grande
(`test_particao_carga.py`, `test_nivel4_veiculo_grande.py`) **vao precisar
mudar** — é a regra de negocio mudando de proposito, nao regressao. Cada
alteracao nesses arquivos precisa estar justificada por D2 na mensagem de
commit. Qualquer OUTRO teste que quebre é sinal de que FIORINO vazou para
`classificar_tipo_veiculo`.

## 10. Fases

Fases independentes, cada uma entregavel e verificavel sozinha:

| Fase | Entrega | Prova |
|---|---|---|
| 1 | FIORINO no catalogo + faixas contiguas + `ordem_capacidade` | `test_tipo_veiculo_fiorino.py`; suite inteira verde |
| 2 | Planilha preenchida + leitura com default FIORINO + `/motoristas` | `test_preferencias_motoristas.py`; a tela lista os tipos |
| 3 | Regra de 1 endereco na formacao | `test_veiculo_grande_um_endereco.py` |
| 4 | **Replay de 30 dias** e numeros para o Hugo | relatorio; decisao de seguir é dele |
| 5 | `horas_estimadas` persistida + `contar_rotas_longas_recentes` | teste da contagem; conferir coluna preenchida numa rodada real |
| 6 | Ordem nova na alocacao | `test_alocacao_justa.py` |

Fases 1-3 nao alteram a alocacao. A fase 6 nao altera a formacao de rotas. O
replay da fase 4 é o portao: sem o aval do Hugo, 5 e 6 nao vao pro ar.

## 11. Fora de escopo

- **Tela de escala por numero.** Descartada (D7).
- **Justica em horas acumuladas.** O criterio é rodizio de rotas longas (D4),
  nao soma de horas.
- **Duracao real como metrica.** Timestamp distorcido (D5).
- **Zona como preferencia.** Continua trava rigida (D6).
- **Corrigir as zonas vazias de Rafael e LALAMOVE.** Relatado, nao alterado.
- **Dividir endereco acima de 2500 cx em varias rotas de Truck.** Fica como
  sublote isolado com alerta (4.2).
- **Jornada por motorista.** Nao existe coluna de jornada e nao é criada aqui.

## 12. Riscos

| Risco | Mitigacao |
|---|---|
| FIORINO vazar para `classificar_tipo_veiculo` e transformar toda rota comum em exclusiva | Duas listas separadas (4.1) + teste de tabela que prova que FIORINO nunca sai de la (9) |
| A regra de 1 endereco explodir o numero de rotas | Replay de 30 dias antes do deploy, com aval do Hugo (8) |
| Rodizio de longas sem efeito no comeco | Esperado e documentado: entra em vigor conforme `horas_estimadas` acumula (7) |
| Contagens caras chamadas por rota deixarem a alocacao lenta | Carregadas uma vez por execucao e repassadas (6.4) |
| Sobrescrever `BD_MOTORISTAS.xlsx`, que é versionada e editada pelo Hugo | Conferir `git status` antes; alterar so a coluna `TIPO_VEICULO` das 27 linhas vazias, preservando as demais; diff conferido antes de qualquer commit |
| Rota ficar sem motorista por causa da ordem nova | Impossivel por construcao: a mudanca é so de ordenacao, o filtro de elegibilidade nao muda (6.1) |
| Editar arquivos co-editados por outra sessao | `git status --short` antes de editar e antes de commitar; `git add` so dos arquivos desta tarefa |
