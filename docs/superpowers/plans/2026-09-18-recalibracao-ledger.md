# SDD ledger — plan: docs/superpowers/plans/2026-09-18-recalibracao-roteirizacao.md

Spec: docs/superpowers/specs/2026-09-18-recalibracao-roteirizacao-design.md (lida)
Working tree: master, direto (sem worktree — decisão do Hugo, 18/09: config.yaml e dados/ são gitignored e não existem em worktree)
Ruling: `.superpowers/` adicionado ao .gitignore — sem isso os artefatos do SDD entrariam no repo. Custo se errado: uma linha a remover do .gitignore.
Global constraint deste projeto: NÃO commitar em nenhuma task (CLAUDE.md: commit só quando o Hugo pedir). Cada task termina em checkpoint (py_compile + testes verdes), sem `git commit`. Consequência operacional: `review-package` por range de commits não se aplica — as review packages são geradas de `git diff` do working tree, escopadas aos arquivos da task.

## Pre-flight scan

| Par / Task | Produz -> Consome | Achado |
|---|---|---|
| T1 -> T8 | `metricas_plano`, `plano_de_sublotes`, `formatar_metricas` | OK, nomes e chaves batem |
| T2 -> T8 | `coordenada_embutida` | OK (T2 define; T8 usa em `_coords`) |
| T2 -> T3/T6 | `obter_coordenadas` preferindo lat/lng | OK, `coords_do_servico` vira alias |
| T3 -> T1 | `calcular_km_estimado` sem volta x `_km_rota` sem volta | OK, mesma convenção |
| T3 -> T6 | `ordenar_2opt` | OK |
| T4 -> T7 | `escolher_melhor_modelo(registrar_historico=)` | OK |
| T5 -> T7/T8 | `SEPARAR_POR_TIPO_CARGA`, `PARTICAO_GERAL`, `rotulo_carga` | OK |
| T5 x T7 | ambos editam `criar_rotas_diarias.py` | Ordem importa: T5 troca `"particao": label` (linhas 453 e 747) ANTES de T7 reestruturar `roteirizar_para_rascunhos`/`main`. O plano declara isso na T7 Step 3(d). Sem conflito se executadas em ordem. |
| T3 x T6 | ambos editam `otimizacao_rotas.py` | Trechos distintos (docstring x alias `limite_distancia`). OK |
| T6 -> T7 | `polir_entre_rotas(...)` | Assinatura do T6 bate com a chamada do T7 `_polir_particao` |
| T7 -> T8 | `planejar_sublotes(...)` | Assinatura bate com a chamada de `rodar_dia` |
| T1 (interno) | testes x código | OK — conferi à mão o caso entrelaçado (6/6 cruzadas, 1 par) e o separado (0/0) |
| T3 (interno) | testes x código | OK — conferi à mão a semente NN da reta ([5,1,3,4,2]) e o caso "primeira parada pode mudar" (ganho ~5,6 km, acima do limiar 4,0 do teste) |
| T6 (interno) | testes x código | OK depois das correções de 18/09 (ver rulings abaixo) |
| T8 (interno) | replay x pipeline | `definir_hora_saida_base` e `carregar_tipos_carga_por_sender` existem e estão acessíveis. OK |

Ruling (pre-flight, T6): o teste `test_realoca_parada_perdida_pra_rota_vizinha` original deixava a rota de origem com 2 paradas, o que a acionaria também no passo de esvaziamento e tornaria o assert ambíguo. Corrigido no plano para 3 paradas restantes. Custo se errado: o teste passaria por outro caminho que não o que ele quer provar.
Ruling (pre-flight, T6): o esvaziamento passa a exigir ganho de km no conjunto das rotas envolvidas (era "de graça"). O objetivo aprovado é compacidade, não contagem de rotas — esvaziar sem ganho contradiz o critério da T4. Spec 3.4 atualizada. Custo se errado: algumas rotas de 1-2 paradas sobrevivem que poderiam ter sido absorvidas.
Ruling (pre-flight, T6): `macro_regiao_predominante_do_sublote` é calculada uma vez sobre a rota original, não a cada consulta — a rota muda de conteúdo durante o polimento e recalcular tornaria a vizinhança instável (e o resultado dependente da ordem). Custo se errado: uma rota que migrasse de macro-região no meio do polimento manteria a vizinhança antiga.
Ruling (pre-flight, T6): `resumo["esvaziadas"]` conta rotas políveis que terminaram vazias por qualquer caminho (realocação ou esvaziamento), não só pelo passo 3. Custo se errado: número no log subestimado.
Observação (não é conflito): `calcular_km_estimado` sem a volta à base muda o km exibido nos rascunhos do painel (`_recalcular_km_silencioso`) — cai quase pela metade em rotas urbanas. É consequência aceita da decisão 3 da spec; avisar o Hugo no relatório final.

## Tasks

### Task 1
Task 1: review 1 — spec OK; 2 Important (ambos plan-mandated, vieram do codigo do brief): (a) rota vazia some silenciosamente de `rotas`/`paradas`; (b) `horas` nao e filtrada junto com `rotas`, entao `rotas_acima_teto` pode desalinhar.
Task 1: Ruling: aceito os dois achados contra o texto do plano. O plano manda transcrever, mas a spec (§3.5) exige que o replay MEÇA fielmente — metrica que esconde rota invisivel mente sobre o que mudou, e foi exatamente esse tipo de silencio que motivou a recalibracao. Fix minimo: filtrar `rotas` e `horas` pelos MESMOS indices, e expor `rotas_sem_coordenada`/`paradas_sem_coordenada` no dict e no `formatar_metricas` (so quando > 0). Contrato com a Task 8 cresce, nao quebra. Custo se errado: duas chaves a mais no dict que a Task 8 pode ignorar.
Task 1: minor (deferred): `diametro_mediano_km` pega o elemento superior do meio em lista par, nao a media dos dois centrais — nome impreciso, sem impacto na comparacao (mesma regra dos dois lados).
Task 1: minor (deferred): nenhum teste assere `diametro_mediano_km` nem `raio_medio_km` diretamente.
Task 1: fix round 1/5 (2 adereçados, 1 novo Important aberto: `paradas_sem_coordenada` e sempre 0 — `sum(len(r) for r in rotas if not r)` soma len de listas vazias)
Task 1: Ruling: `paradas_sem_coordenada` SAI do dict em vez de ganhar uma contagem inventada. `metricas_plano` recebe rotas ja filtradas por `plano_de_sublotes` e nao tem como saber quantas paradas a rota vazia tinha — a informacao so existe em quem filtrou. Fazer `plano_de_sublotes` devolver a contagem mudaria a assinatura que a Task 8 consome direto como argumento. `rotas_sem_coordenada` fica (essa e real e mensuravel). Custo se errado: o replay nao mostra quantas PARADAS individuais ficaram sem coordenada — o log de geocodificacao ja cobre isso.
Task 1: fix round 2/5 (1 adereçado, 0 abertos — `paradas_sem_coordenada` removido, 15 chaves no dict, 10 testes OK)
Task 1: complete (sem commit — working tree; roteirizacao/metricas_plano.py + roteirizacao/test_metricas_plano.py; review clean)

### Sessao reiniciada (20/09)
Ruling: a sessao anterior caiu com 2 agentes em voo (revisao da Task 2 e implementacao da Task 4). Verifiquei o estado real pelo working tree em vez de re-dispatchar: Task 2 e Task 4 estao implementadas, 20 testes verdes (`test_metricas_plano` 10 + `test_coordenadas_embutidas` 5 + `test_selecao_modelo` 5). A Task 4 NAO tem relatorio de implementador (caiu antes de escrever) — a revisao dela vai com evidencia de teste coletada por mim e aviso explicito de que o codigo e a unica fonte. Custo se errado: a revisao da Task 4 pode pedir esclarecimento que ninguem sabe responder; nesse caso o fix vai pra um implementador novo.
Ruling: corrigido no plano um bug de contrato que eu mesmo achei entre Task 1 e Task 8 — `_somar` do replay nao incluia `rotas_sem_coordenada`, e `formatar_metricas` le essa chave no dict do TOTAL: levantaria KeyError na ultima linha do relatorio de 30 dias. Chave adicionada a lista somavel e a linha de Interfaces da Task 1 atualizada pra 15 chaves. Brief da Task 8 reextraido. Custo se errado: nenhum — sem a correcao o replay quebrava no fim.

### Task 2 e Task 4
Task 2: review 1 — spec OK, qualidade Aprovado. Important (ambos FORA do pacote/cobertura, nao entram no fix loop): (a) comentario em `painel_agentes/rascunhos_rota.py:417-420` caducou (justificava nao reusar `calcular_km_estimado` dizendo que `obter_coordenadas` ignora lat/lng embutida — deixou de ser verdade); (b) sem teste dedicado pra `coords_do_servico` com (0,0). Minor: 3 dos 5 testes passariam sem a mudanca; so `test_usa_lat_lng_do_dict_sem_geocodificar` e rede de seguranca real.
Task 2: complete (sem commit; roteirizacao/roteirizacao_dados.py + roteirizacao/test_coordenadas_embutidas.py; review clean)
Task 4: review 1 — spec OK, qualidade Aprovado, 0 Critical/Important. Minor: (a) texto novo sem acento num arquivo que usa acentos (CLAUDE.md manda seguir o padrao DO ARQUIVO); (b) `test_empate_em_km_arredondado` tambem passaria com o criterio antigo; (c) comentario `criar_rotas_diarias.py:662-664` ainda diz "menos rotas primeiro".
Task 4: Ruling: (a) e (c) viram itens da Task 9 (limpeza de texto), nao fix loop — (c) some sozinho porque a Task 7 reescreve aquele bloco. Custo se errado: comentario desatualizado sobrevive mais alguns dias.
Task 4: complete (sem commit; roteirizacao/selecao_modelo.py + roteirizacao/test_selecao_modelo.py; review clean)

### Task 3
Task 3: implementador DONE_WITH_CONCERNS. Concern 1 VERIFICADO por mim e REAL: `criar_rotas_diarias.py:98` faz `sys.path.insert(0, .../painel_agentes)`, entao num processo que ja importou `criar_rotas_diarias` o `import painel_agentes` resolve pro ARQUIVO `painel_agentes/painel_agentes.py` em vez do pacote — rodar `painel_agentes.test_*` no MESMO comando unittest que qualquer modulo de `roteirizacao` da AttributeError. Reproduzido no HEAD puro: e pre-existente, nao foi esta task.
Task 3: Ruling: nao corrijo o `sys.path.insert` — e pre-existente, esta fora do escopo da spec e mexer na resolucao de import do pipeline das 22h por causa de ergonomia de teste e risco desproporcional. Em vez disso a Task 9 passa a rodar a suite em DOIS comandos (roteirizacao/*, depois painel_agentes/*). Provado agora: 88 testes de roteirizacao OK + 13 do painel OK, 101 no total. Custo se errado: quem rodar tudo num comando so leva um erro confuso; fica registrado no doc.
Task 3: review 1 — spec OK (7 itens do Step 3 + Step 4 todos feitos), qualidade Aprovado com ressalvas. 2 Important: (1) a docstring nova de `ordenar_com_janelas` promete "reversoes que envolvam parada sem coordenada sao puladas", e isso e FALSO no ramo COM janela — `_busca_local_com_janela` nunca chama `_tem_coords`; o reviewer reproduziu o or-opt movendo um item sem coordenada pro meio da rota pra absorver espera (custo caiu 7,5 sem custo de km). Comportamento pre-existente, promessa nova. (2) comentario em `roteirizacao_dados.py:1665` ainda descreve a ordem final como "farthest-first+2opt".
Task 3: Ruling: corrijo a DOCSTRING pra dizer a verdade (e nomear o vies), nao o algoritmo. Ancorar item sem coordenada no ramo com janela e mudanca de comportamento fora da spec, num ramo pre-existente, e o caso e de borda: desde a Task 2 quase todo servico chega com coordenada embutida ou geocodificada, e pedido sem coordenada nenhuma ja fica fora das travas de distancia. Mentir na docstring e pior que o vies, porque quem ler depois vai confiar numa invariante inexistente. Custo se errado: numa rota que junte pedido sem coordenada E janela apertada, o sequenciador pode preferir uma ordem que "absorve" espera de mentira — fica registrado no doc da Task 9 como candidato a proxima fase.
Task 3: Ruling: o comentario `criar_rotas_diarias.py:705` NAO entra no fix — a Task 7 reescreve aquele bloco inteiro (`_rotear_particao`). O de :286 (docstring da fusao entre macro-regioes) entra, porque sobrevive a Task 7. Custo se errado: nenhum, e so ordem de trabalho.
Task 3: Ruling: a docstring de `otimizacao_rotas.ordenar_2opt` ficou COM acento, divergindo do texto do brief. Mantenho: o arquivo ja usava acentuacao plena antes, e o CLAUDE.md manda seguir o padrao DO ARQUIVO. O brief e que estava errado ao copiar sem acento. Custo se errado: inconsistencia cosmetica.
Task 3: fix round 1/5 (2 adereçados, 0 abertos — docstring de `ordenar_com_janelas` agora separa os dois ramos e nomeia o vies; comentario de `dividir_em_sublotes` corrigido; 43 testes OK; nenhuma logica tocada)
Task 3: complete (sem commit; roteirizacao_dados.py + otimizacao_rotas.py + criar_rotas_diarias.py + test_janelas_horario.py + test_sequencia_livre.py; review clean)
Task 3: minor (deferred -> Task 9): comentarios residuais "farthest-first" em criar_rotas_diarias.py:286 e :705 (o :705 morre na Task 7; o :286 esta no item 2 do Step 1 da Task 9)

### Preparo da Task 8 (replay)
Copia consistente do banco de producao baixada por `sqlite3.backup` como www-data: `dados/dados_replay.db` (74 MB, coberto pelo `dados/*` do .gitignore, conferido). Temporario removido da VPS.
Base disponivel: 32 dias com rota ENVIADA (12/08 a 21/09), 340 rotas, 3010 paradas, ZERO parada sem coordenada, 403 paradas com janela de horario.
Ruling: o replay vai rodar de 12/08 a 19/09 (nao 17/09 como o plano dizia) — ha dados ate 21/09 e os dois dias extras sao operacao real. 21/09 fica de fora por ser futuro/incompleto (1 rota, 2 paradas). Custo se errado: nenhum, so mais amostra.
Observacao: com zero parada sem coordenada nesses 32 dias, o vies do item sem coordenada no ramo com janela (documentado na Task 3) nao aparece no replay — confirma que e caso de borda.

### Baseline medido (dados reais, 31 dias: 12/08 a 18/09)
TOTAL enviado: 339 rotas | 3008 paradas | media 8,9 paradas/rota | 116 rotas com <=6 paradas | diametro mediano dos dias 10,6 km | 18.731 km | 658 paradas (22%) com a vizinha mais proxima em OUTRA rota | 658 pares de rotas com areas se cruzando | 0 rotas acima de 9h.
Pior dia de entrelacamento: 25/08 (40%), 01/09 (36%), 26/08 (33%), 18/09 (39%). Melhor: 14/08 (3%).
Este baseline valida o modulo da Task 1 contra dado real e e o lado "enviado" da comparacao da Task 8.

ACHADOS OPERACIONAIS pro Hugo (nao sao bug da spec, sao o que o dado mostrou):
1. 20/08, rota VIAGEM de 5 paradas com diametro de 849 km: uma parada e em PORTO ALEGRE (RS) -- PS-37057, Rua Quintino Bocaiuva -- junto com paradas em Sao Paulo. Pedido de outro estado que entrou em rota propria em vez de ir pra redespacho.
2. 25/08, rota marcada GRANDE_SP com 204 km de diametro: Campinas (PS-37709) + Ribeirao Preto (PS-37576). O rotulo esta errado (deveria ser VIAGEM) e a trava de 20 km par-a-par da Grande SP foi contornada -- consistente com rota montada ou editada a mao no painel, onde nao ha trava.
Ruling: nao corrijo nem investigo mais fundo nesta spec (esta fora do escopo aprovado, e mexer em validacao de rota manual e decisao do Hugo). Vai no relatorio final como achado. Custo se errado: os dois casos continuam possiveis ate o Hugo decidir.

### Task 5
Task 5: review 1 — spec OK, qualidade Aprovado, 0 Critical/Important. Reviewer varreu o repo por usos de `particao` e confirmou que nenhum filtro/agrupamento/validacao assume so 2 valores: os rotulos novos ("Misto (Seco+Refrigerado)", "Geral") nao quebram consumidor nenhum. `rotulo_carga` usa o `TIPOS_CARGA_FRIA` compartilhado, nao lista nova. As 2 trocas de `"particao": label` confirmadas por grep (zero restantes).
Task 5: minor (deferred -> Task 9): (a) tooltip do botao Roteirizar em `painel_agentes/templates/planejamento_rotas.html:2218` ainda diz "Seco separado de Refrigerado/Congelado"; (b) comentario em `criar_rotas_diarias.py:645-654` descreve a particao por carga como vigente (a Task 7 reescreve esse bloco); (c) `test_religar_volta_a_particao_tripla` nao exercita o ramo de fusao "Misto" (codigo de fusao nao foi alterado — lacuna de cobertura, nao regressao).
Task 5: complete (sem commit; criar_rotas_diarias.py + laboratorio_rotas.py + laboratorio_rotas.html + painel_agentes.py 1 linha + test_particao_carga.py + test_laboratorio_todos.py; review clean)

### Medicao do polimento antes da Task 7 (feita por mim, controlador)
Volume real (18 rotas, 171 paradas — o maior dia medido, 16/09), rotas ENTRELACADAS de proposito (99% das paradas com vizinha em outra rota, que e o padrao "petala" do Clarke-Wright):
| teto | tempo | movimentos | rotas | km | entrelacamento |
|---|---|---|---|---|---|
| 3 s | 3,0 s (estourou) | 81 realoc + 44 trocas | 18 -> 14 | -75% | 99% -> 87% |
| 10 s | 10,0 s (estourou) | 148 + 116 | 18 -> 13 | -83% | 99% -> 61% |
| 30 s | 30,1 s (estourou) | 169 + 154 | 18 -> 13 | -84% | 99% -> 46% |
Cobertura de servicos preservada nos tres casos, sem duplicata. Num cenario ja bem agrupado (300 paradas, rotas compactas) ele bate o teto sem achar quase nada a fazer, o que e o comportamento correto.
Ruling: `POLIMENTO_TEMPO_MAXIMO_S` entra na Task 7 com **15.0**, nao 3.0 como o plano dizia. Motivos: (a) desde a Task 5 ha UMA particao por dia, entao o teto e pago uma vez, nao tres; (b) o job das 22h roda por timer e ja leva minutos — 15 s nao muda nada operacionalmente; (c) a medicao mostra que entre 3 s e 15 s o ganho esta justamente no ENTRELACAMENTO (87% -> ~61%), que e o sintoma que o Hugo relatou, e o km ja satura em -83%; (d) de 15 s pra 30 s o ganho de km e ~1%, entao 15 s e onde a curva vira. Custo se errado: 12 segundos a mais no job noturno; baixar e trocar uma constante.

### Task 6
Task 6: review 1 — spec OK, qualidade REPROVADO. 1 CRITICAL reproduzido pelo reviewer: DUPLICACAO de servico no esvaziamento. `envolvidas`/`backup` sao calculados uma vez, mas o laco de colocacao recalcula `_vizinhas(i)` a cada parada — e `_vizinhas` filtra por centroide, que se DESLOCA quando uma parada sai de `i`. Uma rota j2 fora do backup pode entrar no alcance, receber a parada, e nao ser revertida quando o cheque agregado falha: entrada [1,2,11,21] saiu [1,2,2,11,21]. O resumo mentiu junto (0 movimentos, km_depois > km_antes). Nenhum dos 10 testes usa 3+ rotas, e o unico de esvaziamento e caso de sucesso com 2 rotas — foi por ai que passou.
Task 6: Ruling: o defeito e do MEU plano (o codigo do brief foi copiado verbatim, o reviewer confirmou). Fix obrigatorio em 3 partes: (1) congelar a lista de vizinhas no inicio do esvaziamento e o backup cobrir exatamente `[i] + vizinhas congeladas` — perder uma vizinha que so entraria no alcance depois e preco justo por integridade e determinismo; (2) checar o teto de tempo TAMBEM dentro dos lacos de parada e de troca (o laco de troca e O(paradas^4) por par de rotas, sem ponto de checagem); (3) rede de seguranca final: se o km de saida for pior que o de entrada, devolver a ENTRADA e marcar no resumo — nenhum movimento aceito pode piorar o km, entao km pior significa bug, e o modulo nao deve entregar isso pro pipeline. Mais testes com 3+ rotas e com esvaziamento revertido. Custo se errado: sem (1) um pedido pode ser duplicado numa rota real; e o pior tipo de erro desta spec.
Task 6: CRITICAL confirmado independentemente por mim (rodei o repro do reviewer): entrada [1,2,11,21] -> saida [1,2,2,11,21], o pedido 2 fica em DUAS rotas ([1,2] e [2,21]), com resumo reportando 0 movimentos e km 152,7 -> 160,8 (piorou). Minhas proprias varreduras de geometria (3 rotas, varias distancias) NAO reproduziram — o bug depende de geometria bem especifica, o que o torna mais perigoso, nao menos: passaria despercebido em teste casual e apareceria num dia de operacao qualquer.
Task 6: fix round 1 verificado por mim ANTES da re-revisao: rodei de novo o repro do reviewer e a mesma geometria que duplicava agora devolve [1,2,11,21] intacto, km 152,7 -> 152,7, e o resumo traz `descartado_por_piora: False`. Rede de seguranca presente no modulo.
Task 6: fuzz de integridade feito por mim: 400 geometrias aleatorias x 3 valores de `ganho_minimo_km` (0,05 / 5 / 999999) x teto de 0,5 s = 1200 casos. ZERO perda, ZERO duplicata, ZERO caso de km piorando. O modulo esta solido depois do fix.

### Task 7
Task 7: implementador DONE. 70 testes OK. PROVA REAL contra os pedidos not_assigned de producao (--modo-teste, nada escrito na Vuupt), log de 20/09 21:23:
  - particao unica "Geral" funcionando (antes eram 3);
  - competicao dos 5 modelos: Atual 16 rotas/535,3 km | Sweep 18/594,3 | Clarke-Wright 13/513,8 | CEP 15/569,2 | K-means 16/554,6 -> venceu Clarke-Wright;
  - polimento: 6 realocacoes + 3 trocas, 13 -> 13 rotas, km 513,8 -> 478,9 (**-6,8%**) em 15,4 s (bateu o teto).
Esse e o primeiro numero de ganho medido no pipeline inteiro com dado real, nao sintetico.
Ruling (Task 9): incluo no escopo da Task 9 a correcao de 3 TEXTOS em `roteirizacao/incrementar_rotas.py` (linhas 845, 847, 866), arquivo que a spec mandava nao tocar. Motivo: ele chama `ordenar_2opt`, que mudou de comportamento, e a linha 866 ESCREVE NO LOG "reordenada (mais longe -> mais perto da base)" — um log que mente sobre o que o sistema fez, justamente no arquivo que alguem vai ler pra diagnosticar rota estranha. So texto, nenhuma linha de logica. Custo se errado: 3 linhas de comentario/log num arquivo que a spec preferia intocado.
Brief da Task 9 anexado com a lista exata dos 7 pontos de texto, cada um com arquivo e linha, mais o que e residual legitimo e nao deve ser tocado (mencao historica em criar_rotas_diarias.py:19 e "farthest-point" das sementes de k-means em otimizacao_rotas.py:396,440).
Task 7: review 1 — spec OK, qualidade Aprovado. O reviewer leu as duas funcoes inteiras (nao so o diff) e confirmou preservado: fluxo de reserva sem base, ordenacao por escassez de motorista antes de alocar, contagem por motorista, indice global #N, modo teste, modo rascunho, try/except por rota; nenhuma chamada duplicada; nenhum codigo morto; comentarios desatualizados removidos e nao reintroduzidos. 1 Important: falta teste garantindo a INVARIANTE DE ORDEM (fusao antes do polimento) e que cada etapa roda uma vez — hoje trocar as duas linhas nao quebraria teste nenhum.
Task 7: Ruling: aceito o Important e mando fix. A ordem nao e detalhe de estilo: se o polimento rodasse antes da fusao, a fusao de sublotes pequenos desfaria o trabalho dele, e o sintoma (rotas pequenas voltando) seria dificil de rastrear meses depois. Vale um teste que prenda isso. Custo se errado: a invariante fica so no comentario.
Task 7: duvida do reviewer sobre o log ESCLARECIDA por mim: as linhas com rotulo "[teste]" e os pares repetidos as 21:29 sao dos TESTES UNITARIOS (o logging do modulo escreve no `criar_rotas_diarias.log` local quando o modulo e importado). A prova real e a das 21:24:09, que tem "Particao 'Geral': modelo Clarke-Wright, 13 rota(s)" e a linha do polimento com 6 realocacoes + 3 trocas e km 513,8 -> 478,9. Nao ha chamada dupla em producao.
Task 7: minor (deferred): os `mock.patch.object(rd/ot, "obter_coordenadas", ...)` em test_planejar_sublotes sao redundantes — `polimento_rotas` e `otimizacao_rotas` importaram o nome direto, entao o mock nao os alcanca; o isolamento real vem das coordenadas embutidas nos dicts de teste.
Task 6: fix round 1/5 (1 CRITICAL + 2 Important adereçados; implementador confirmou que `test_esvaziamento_revertido_nao_duplica` FALHAVA no codigo antigo com "Lists differ: [401, 402, 402, 403, 404] != [401, 402, 403, 404]"; 14 testes no modulo, 47 no conjunto). Re-revisao escopada dispatchada.
Task 7: fix round 1/5 (1 Important adereçado: 2 testes novos de invariante de ordem, arquivo foi de 4 pra 6 testes, 23 OK; `criar_rotas_diarias.py` intocado no round). Re-revisao escopada dispatchada.
Evidencia colateral verificada por mim: o `selecao_modelo_historico.txt` LOCAL ganhou exatamente UMA linha nova ("2026-09-21 | Geral | vencedor: Clarke-Wright"), que e a prova de fumaça real do job em modo teste — legitima. Os testes nao sujaram (usam tempfile + mock) e o replay tambem nao, o que confirma na pratica que o `registrar_historico=False` funciona como projetado.
Task 6: re-revisao do fix APROVADA. As 3 partes verificadas por leitura + reproducao independente: vizinhanca congelada (nenhuma chamada a `_vizinhas` sobrou dentro do bloco 3), teto fino nos 3 lacos com reversao do backup ao estourar, rede de seguranca devolvendo os objetos ORIGINAIS. O reviewer forcou estouro de tempo no meio do esvaziamento contra o modulo real e a saida saiu integra. Confirmou tambem que o polimento continua util (4 realocacoes, 2 esvaziadas, km 51,4 -> 27,1 num cenario de 4 rotas).
Task 6: minor (deferred, backlog): os testes de duplicacao so pegam uma regressao da parte 1 se a rede de seguranca (parte 3) tambem estiver desligada — com as 3 partes no lugar, uma regressao isolada da vizinhanca congelada seria absorvida pela rede e passaria verde. Isso e defesa em profundidade funcionando (a saida fica correta), mas falta um teste que isole a parte 1 com a rede neutralizada. Nao bloqueia: o comportamento de producao esta correto nos dois cenarios.
Task 6: complete (sem commit; roteirizacao/polimento_rotas.py + test_polimento_rotas.py (14 testes) + 1 linha de alias em otimizacao_rotas.py; review clean apos 1 fix round)
Task 7: re-revisao do fix APROVADA. `test_ordem_selecao_fusao_polimento_uma_vez_cada` compara a lista de chamadas com igualdade exata, entao pega tanto a troca de ordem quanto a duplicacao de chamada. O mock e feito em `polir_entre_rotas` (funcao publica) e nao no wrapper interno, o que mantem a checagem real da chave de ligado/desligado sendo exercitada. 6 testes no arquivo, 23 no conjunto.
Task 7: complete (sem commit; roteirizacao/criar_rotas_diarias.py + test_planejar_sublotes.py; review clean apos 1 fix round)

### Task 8 (replay) — assumida por mim
O subagente da Task 8 escreveu `roteirizacao/replay_rotas.py` + teste (2 testes OK) e ENCERROU no meio, tendo lancado a rodada 1 em background sem esperar. Assumi a execucao. O script funciona.

CURVA RAPIDA, 3 dias (15 a 17/09, 401 paradas). Enviado de verdade: 42 rotas | 9,5 paradas/rota | 10 pequenas | diam mediano 8,4 km | 1.833 km | 102 cruzadas (25%) | 6 rotas acima de 9h.
| distancia | rotas | paradas/rota | pequenas | diam med | km | cruzadas | acima 9h |
|---|---|---|---|---|---|---|---|
| 20 km | 46 | 8,7 | 15 | 10,3 | 1.778 (-3%) | 90 (22%) | 0 |
| 15 km | 48 | 8,4 | 19 | 7,8 | 1.781 (-2,8%) | 85 (21%) | 0 |
| 12 km | 50 | 8,0 | 21 | 7,2 | 1.811 (-1,2%) | 75 (19%) | 0 |
| 10 km | 56 | 7,2 | 28 | 6,1 | 1.840 (+0,4%) | 82 (20%) | 0 |
| controle (regras antigas de agrupamento + sequencia nova) | 48 | 8,4 | 17 | 10,5 | 1.859 (+1,4%) | 125 (31%) | 0 |

Leituras:
1. O CONTROLE e o pior de todos (31% de cruzadas, diametro 10,5, km acima do enviado). Isso PROVA que quem melhora e o pacote de agrupamento (particao unica + criterio de km + polimento), nao a sequencia livre sozinha.
2. 20 km PIORA o diametro mediano (8,4 -> 10,3): com o teto largo o agrupador estica a rota. Nao serve.
3. 15 km e 12 km sao as candidatas: diametro cai abaixo do enviado, km cai ou fica estavel, cruzadas caem de 25% pra 21% / 19%.
4. 10 km explode: 56 rotas (+33%) e km acima do enviado. Fragmenta demais.
5. Rotas pequenas SOBEM em toda configuracao (10 -> 15..28). E o custo direto de apertar a trava.
6. Rotas acima de 9h: o ENVIADO tem 6 em 3 dias; toda configuracao nova da ZERO. Ganho grande e nao esperado, vem do orcamento de horas sendo respeitado na ordem final.
7. NENHUMA configuracao atinge o criterio de aceite da spec (§5.1, "cruzadas cai pelo menos pela metade"). O melhor e 19%, contra 25% do enviado. Motivo estrutural: nem o criterio de selecao nem o polimento otimizam ENTRELACAMENTO — os dois otimizam KM, e duas rotas paralelas na mesma avenida podem ter km baixo e entrelacamento alto. Isso vai honestamente pro relatorio do Hugo como limite do desenho aprovado, com a proposta de fase seguinte (penalizar sobreposicao explicitamente no objetivo).
Rodadas completas de 31 dias (20, 15, 12 e controle) lancadas em paralelo.

### Replay completo — rodada de CONTROLE (31 dias, 12/08 a 19/09, 32 dias processados, 0 erros)
Controle = regras ANTIGAS de agrupamento (particao por carga LIGADA, polimento DESLIGADO, distancia 20 km), so com a sequencia livre e o criterio de km novos.
| | rotas | paradas/rota | pequenas | diam med | km | cruzadas | acima de 9h |
|---|---|---|---|---|---|---|---|
| enviado de verdade | 339 | 8,9 | 116 | 10,6 km | 18.731 | 658 (22%) | **71** |
| controle | 363 | 8,3 | 145 | 10,6 km | **15.637 (-16,5%)** | 724 (24%) | **0** |

DOIS ACHADOS GRANDES:
1. **71 das 339 rotas enviadas (21%) estouram o orcamento de 9 horas.** Uma em cada cinco rotas que foram pros motoristas nao cabia no dia. O pipeline novo zera isso, porque o orcamento e conferido na ORDEM FINAL (pos-sequenciamento), nao so na ordem de formacao. Esse numero nao estava no diagnostico inicial e e material pro Hugo.
2. **A sequencia livre sozinha vale -16,5% de km** (18.731 -> 15.637), medida com a mesma regra dos dois lados (trajeto ate a ultima parada, sem volta a base). Bate com o diagnostico inicial, que estimava 12 a 21% reordenando cada rota do zero.
Contrapartida do controle: cruzadas SOBE (22% -> 24%) e rotas pequenas sobem (116 -> 145) — confirma que sem particao unica e sem polimento o entrelacamento piora.

### REPLAY COMPLETO — 31 dias (12/08 a 19/09), 32 dias processados, 3008 paradas, 0 erros
| configuracao | rotas | paradas/rota | pequenas | diam med | km | cruzadas | pares | acima de 9h |
|---|---|---|---|---|---|---|---|---|
| **enviado de verdade** | 339 | 8,9 | 116 | 10,6 km | 18.731 | 658 (22%) | 658 | **71** |
| controle (regras antigas + seq nova) | 363 | 8,3 | 145 | 10,6 km | 15.637 (-16,5%) | 724 (24%) | 554 | 0 |
| **20 km** | 350 | 8,6 | 133 | 11,1 km | **15.012 (-19,9%)** | 631 (21%) | 577 | 0 |
| **15 km** | 372 | 8,1 | 158 | **10,1 km** | 15.234 (-18,7%) | 573 (19%) | 499 | 0 |
| **12 km** | 406 | 7,4 | 213 | **8,7 km** | 15.551 (-17,0%) | 548 (18%) | **398** | 0 |

Leitura:
- km cai de 17% a 20% em TODAS as configuracoes. A de 20 km e a melhor em km puro.
- 20 km PIORA o diametro mediano (10,6 -> 11,1): nao resolve a queixa numero 1 do Hugo ("rotas espalhadas demais").
- 15 km e o unico ponto em que TODO indicador melhora ou fica estavel: km -18,7%, diametro 10,1 (melhor que o enviado), entrelacamento 19% (era 22%), pares -24%, zero rota acima de 9h. Custo: 33 rotas a mais em 31 dias, ~1 por dia.
- 12 km entrega a melhor compacidade (diametro 8,7, pares -39%) mas custa 67 rotas (+20%, ~2 por dia) e 213 rotas pequenas.
- CRITERIO DE ACEITE DA SPEC (§5.1) NAO ATINGIDO no entrelacamento: queria cair pela metade (22% -> 11%), o melhor foi 18%. Motivo estrutural ja identificado: nem o criterio de selecao nem o polimento otimizam entrelacamento, os dois otimizam km. Vai honesto pro Hugo, com proposta de fase seguinte.
- RESSALVA IMPORTANTE A FAVOR DO NOVO: o baseline "enviado" tem 71 rotas acima de 9h, ou seja 339 nao e um numero viavel — se aquelas rotas fossem quebradas pra caber no dia, o enviado teria MAIS que 339 rotas. Comparar 339 com 372 e conservador contra o pipeline novo.
Recomendacao que vou levar ao Hugo: 15 km.

### DECISAO DO HUGO (20/09)
1. `DISTANCIA_MAXIMA_ROTA_KM` = **15** (era 20). Aplicado por mim nos DOIS lugares: `roteirizacao/criar_rotas_diarias.py` (com o comentario explicando a calibracao e o que 20 e 12 fariam) e `painel_agentes/planejamento_rotas.py` (copia do badge). Conferido em runtime: as duas batem em 15.
2. Entrelacamento: atacar numa FASE SEGUINTE, depois desta ir pro ar. O Hugo quer ver em producao antes. Proposta a fazer depois: penalizar sobreposicao explicitamente no objetivo (hoje criterio de selecao e polimento so olham km).

### Task 8 e Task 9
Task 8: script + teste entregues pelo subagente (2 testes OK); as 4 rodadas e a analise foram executadas por mim. Relatorio escrito por mim em task-8-report.md. Revisao dispatchada.
Task 9: DONE. Os 7 pontos de texto corrigidos + 1 achado extra do proprio implementador (`painel_agentes/templates/mapa_rotas.html:235`, comentario de JS que dizia "mais longe -> mais perto da base" pro optimizeWaypoints). Adendo §9 escrito no DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md com os numeros REAIS do replay (o relatorio da Task 8 ficou pronto no meio do trabalho dele). 150 testes OK (134 + 16). Varredura final deixou so 4 residuos, todos legitimos: 2 mencoes historicas ("substituiu o 'mais longe primeiro' de 03/08") e 2 "farthest-point" das sementes de k-means. Revisao dispatchada.

### Tempo do job (criterio §5.4 da spec: nao passar de 2x)
Producao hoje (VPS, 4 execucoes reais): 42,9s | 62,7s | 55,7s | 50,5s -> media ~53s.
Pipeline novo (local, modo teste, 20/09): 115,0s. Razao: **2,17x** — ligeiramente ACIMA do criterio de 2x que eu mesmo escrevi na spec.
Ruling: aceito e reporto honestamente, sem mexer na constante. Motivos: (a) o numero ABSOLUTO e +62 segundos num job que roda as 22h por timer, sem ninguem esperando — operacionalmente irrelevante; (b) 15,4s dos 115s sao o teto do polimento, e a medicao de 20/09 mostrou que e justamente entre 3s e 15s que o entrelacamento melhora, que e o sintoma relatado pelo Hugo; (c) a comparacao nem e homogenea (notebook local x VPS, volumes de pedido diferentes). Se o Hugo quiser ficar dentro dos 2x, baixar o teto de 15s pra 10s resolve e custa um pouco de compacidade. Vai pro relatorio final como item de decisao dele. Custo se errado: o job noturno leva 2 minutos em vez de 1.
CORRECAO do tempo (medido DEPOIS de aplicar os 15 km, 20/09 22:50): o job completo em modo teste levou **105,3s** contra os ~53s da media de producao = **1,99x**, ou seja DENTRO do criterio de 2x da spec. A medida anterior (115,0s / 2,17x) era com a trava ainda em 20 km. A trava mais apertada deixa o problema mais facil pros agrupadores. Nesta execucao: Clarke-Wright venceu com 13 rotas / 509,3 km (com 20 km dava 513,8), e o polimento levou pra 497,8 km com 5 realocacoes + 1 troca em 15,1s. Nao ha mais item de decisao pendente sobre o teto do polimento.
Task 9: review 1 — spec OK (os 7 pontos conferidos um a um + o achado extra do mapa julgado razoavel), qualidade Aprovado, 0 Critical. O reviewer separou manualmente os hunks nos arquivos compartilhados por varias tasks e confirmou que NENHUMA linha de logica e atribuivel a Task 9. Conferiu o adendo contra o task-8-report E contra o log real de producao: nenhum numero inventado.
Task 9: 1 Important, e a falha e MINHA: o pacote de revisao que montei omitiu o diff de `criar_rotas_diarias.py`, um dos arquivos do item 3. O reviewer foi buscar no repo por conta propria e confirmou a conformidade. Nao afeta o codigo; fica como licao sobre montagem de pacote.
Task 9: minor esclarecido por mim: a docstring de `_orcamento_inviavel_por_distancia` que o reviewer viu ja corrigida foi obra da Task 3 (item (e) do Step 3 daquele brief), nao um achado extra nao declarado da Task 9.
Task 9: complete (sem commit; roteirizacao_dados.py + selecao_modelo.py + criar_rotas_diarias.py + incrementar_rotas.py + rascunhos_rota.py + planejamento_rotas.html + mapa_rotas.html + DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md; review clean)
Task 8: review 1 — spec OK, qualidade Aprovado. O reviewer verificou de forma independente: banco principal genuinamente somente-leitura (tentativa de escrita levanta erro), `registrar_historico=False` nao grava nada, mapeamento de caixas/nivel/tipo de carga/janelas correto, soma do TOTAL cobrindo as 8 chaves somaveis (incluindo `rotas_sem_coordenada`, que era a armadilha que eu tinha corrigido no plano), as 3 opcoes de linha de comando mutando as constantes certas, zero parada sem coordenada e zero service_id duplicado no periodo.
Task 8: 3 Important, todos tratados:
  (a) `scheduled_start` ausente na reconstrucao -> afeta `_chave_nivel4` (dois nivel-4 do mesmo endereco/embarcador agendados pra dias diferentes poderiam ser fundidos). VERIFIQUEI A FONTE: `rascunhos_parada` NAO tem coluna de agendamento — e limitacao do DADO, nao do script. Ruling: documentar honestamente em vez de simular precisao. Escala: 31 paradas nivel 4 em 3008 (1%), em 10 dos 31 dias, e o efeito e IDENTICO nas 5 configuracoes (a separacao por nivel 4 nao depende da trava de distancia), entao nao muda qual configuracao vence nem invalida a escolha dos 15 km. Documentado no docstring do script e no relatorio.
  (b) segunda conexao ao banco (via `carregar_tipos_carga_por_sender`, em `regras/`) sem `mode=ro`. So faz SELECT. Ruling: nao mexo num modulo compartilhado do projeto por isso; fica documentado como ressalva no script.
  (c) rodadas em paralelo x instrucao de rodar uma a uma, e o arquivo de log compartilhado em modo append pode ter escritas intercaladas. Ruling: os numeros da analise vieram do STDOUT separado de cada processo, nao do arquivo compartilhado — corrigi o relatorio pra deixar isso explicito.
Task 8: minor corrigido por mim no relatorio: eu escrevi "32 dias processados" mas sao 31 dias COM ROTA ENVIADA na janela (o 32 vinha da minha contagem ate 21/09, fora do periodo). A soma das rotas desses 31 dias da 339, batendo com o baseline — nenhum dia foi descartado.
Task 8: complete (sem commit; roteirizacao/replay_rotas.py + test_replay_rotas.py; review clean apos documentar as limitacoes)

### REVISAO FINAL DO CONJUNTO (modelo mais capaz)
Veredito: PRONTO PARA ENTREGA, 0 Critical, 6 Important, 9 Minor. O reviewer provou as 3 chaves de retorno rapido rodando o pipeline real com 24 pedidos sinteticos (POLIMENTO_ATIVO=False, SEPARAR_POR_TIPO_CARGA=True, DISTANCIA_MAXIMA_ROTA_KM=20 e o fluxo de reserva sem base) — nenhuma esta morta, cobertura 24/24 em todos. Confirmou coerencia da convencao "sem volta a base" nos 3 lugares, nivel 4 e veiculo grande intocados, e que o teto de 100 caixas do polimento e metade do minimo de qualquer veiculo grande (150), entao o polimento nao consegue criar um por acidente.
Entram numa unica onda de fix (o guia SDD manda UMA, nao um agente por achado): I1 (polimento nao confere `KM_ACUMULADO_MAXIMO_ROTA_KM`, que todas as outras etapas aplicam), I2 (`_polir_particao` sem try/except derruba o job noturno inteiro por uma etapa que e 100% opcional), I3 (nenhuma assercao de cobertura DEPOIS da fusao e do polimento — a de `selecao_modelo._validar` roda antes, e o polimento ja produziu um bug de duplicacao uma vez), I4 (spec §3.6 pedia teste de rejeicao por horas e por janela; o arquivo nao tem nenhum dos dois, e sao as duas travas avaliadas via estado global), I5 (o teto de 15 s vale tambem pro botao sincrono da tela, com uma pessoa esperando), M1 (excecao do "pedido gigante sozinho" ausente em `_rota_valida`, divergindo das outras 2 copias da trava), M3 (passar `coords_base` explicito em vez de depender do global), M5 (filtro de centroide condicionado so a `distancia_maxima_km`), M9 (§1.1 do doc ainda descreve o sequenciamento antigo).
Ficam registrados sem acao: M2 (km dos rascunhos pela metade -> ja rastreado: `marcar_enviado` sobrescreve com km rodoviario antes de espelhar no nucleo; residuo so se esse recalculo best-effort falhar), M4 (texto da spec x moda da macro-regiao), M6 (`--base` sem validacao de aridade; base do replay ~0,5 km do CD do resto do repo — simetrico nos dois lados), M7 (replay fixa a hora de saida em vez de ler o config), M8 (religar a particao triplicaria o orcamento do polimento).
I6 e comunicacao minha, nao codigo: DOIS dos quatro criterios de aceite da spec §5.1 falharam, nao um. Alem do entrelacamento (22% -> 19%, meta 11%), as ROTAS PEQUENAS PIORARAM (116 -> 158, +36%) e paradas/rota caiu de 8,9 pra 8,1 — e "poucas paradas por rota" era uma das quatro queixas originais do Hugo. Vou dizer isso a ele com todas as letras.
Onda de fix final: 9 itens (C1..C9) aplicados e RE-REVISADOS. Todos adereçados, quebra nova: nenhuma. O re-reviewer conferiu que os parametros novos sao keyword-only com default None (comportamento antigo preservado byte a byte), que a funcao de km acumulado foi REAPROVEITADA e nao reimplementada, que a conferencia de cobertura e log e nao excecao (um plano com falha ainda vale mais que nenhum plano as 22h), e refez a aritmetica dos 3 testes novos pra confirmar que cada um e rejeitado SO pela trava sob teste. 137 + 16 = 153 testes.

### FIM — todas as 9 tasks completas, revisao final do conjunto feita, onda de fix aplicada e re-revisada.
