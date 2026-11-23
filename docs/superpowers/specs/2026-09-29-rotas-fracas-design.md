# Rotas fracas: juntar, segurar ou avisar

Data: 29/09/2026. Pedido do Hugo. Estado: especificação para revisão, nada implementado.

## 1. Problema

Rota com poucos pedidos e pouca carga ocupa um motorista e um carro para pouca entrega. O roteirizador já tenta evitar isso (mínimo de 10 pedidos por região, fusão de rotas pequenas, esvaziamento de rotas de 1 ou 2 paradas no polimento), mas:

- nenhuma regra olha **caixas** como mínimo, só como teto (100 por rota);
- quando a rota pequena não cabe em nenhuma vizinha pelos limites normais, ela sai do mesmo jeito, sem aviso e sem alternativa.

### Medição (produção, 30/08 a 29/09/2026)

Fonte: `nucleo_rotas` + `nucleo_paradas`, rotas CONCLUIDA/EM_ROTA, paradas não canceladas.

| Recorte | Rotas |
|---|---|
| Rotas executadas | 281 |
| Rotas de planejamento (sem coletas e sem Lalamove) | 246 |
| Planejamento com até 5 pedidos | 58 |
| Dessas, com mais de 40 caixas (carga cheia, não é rota fraca) | 30 |
| Até 7 pedidos e até 40 caixas (corte escolhido) | 40 (1,8 por dia de operação) |

Pouco pedido não é pouca carga: houve rota de 1 pedido com 294 caixas. Por isso o corte exige as duas condições juntas.

Limites da medição: conta rotas como foram executadas (depois de edição manual e de pedidos incluídos mais tarde); 7 das 58 pequenas têm pedido nível 4; região e tipo de veículo estão vazios na maioria das rotas pequenas.

## 2. Decisões do Hugo (29/09)

| Tema | Decisão |
|---|---|
| O que fazer com rota fraca | Juntar com folga, senão segurar para o dia seguinte, senão avisar |
| Corte de rota fraca | Até 7 pedidos **e** até 40 caixas |
| Folga da junção | Distância 15 para 20 km, km acumulado 60 para 75 km, paradas 16 para 18 |
| Limites que não cedem | 100 caixas, 9h, janela de horário, mesma macro-região |
| Prazo máximo de entrega | 3 dias úteis a partir da entrada do pedido, contando a data da entrega |
| Adiamentos por pedido | No máximo 1 |
| Aviso ao embarcador quando segura | Não |
| Aviso interno | Etiqueta no Planejamento com o motivo + resumo no WhatsApp (ALERTAS FRESH) |

## 3. Fluxo

Roda depois do polimento, sobre as rotas de uma partição do dia. Para cada rota fraca:

1. **Juntar.** Distribuir os pedidos entre as rotas vizinhas da mesma macro-região, cada pedido na posição de menor acréscimo de km. A folga vale só para a rota que recebe. A rota fraca pode ser repartida entre duas ou mais vizinhas. A junção só é aceita se **todos** os pedidos couberem; caso contrário nada muda. Diferente do polimento, não exige queda de km.
2. **Segurar** (só na rodada automática). Se não coube e todos os pedidos da rota puderem esperar (seção 5), a rota não vira rascunho e os pedidos ficam no pool.
3. **Avisar.** Se não pôde segurar, a rota sai com a etiqueta "Rota fraca" e o motivo.

Ficam fora da regra, nem como fraca nem como receptora: rota com nível 4, rota de veículo grande, rota com destino inviável por distância (mesmo critério de `polimento_rotas._rota_polivel`). Coletas, Lalamove e dedicados não passam pela roteirização, então não são afetados.

Rota de Viagem pode ser juntada a outra da mesma macro-região (a distância par a par de Viagem já é sem teto; o km acumulado de Viagem continua 300 km, sem folga), mas nunca é segurada.

## 4. Onde cada parte roda

| Parte | Rodada das 18h (`main`) | Botão Roteirizar | Replay |
|---|---|---|---|
| Juntar com folga | Sim | Sim | Sim |
| Segurar | Sim | Não | Não |
| Etiqueta | Sim | Sim | Não se aplica |
| Resumo no WhatsApp | Sim | Não | Não |

No botão não se segura nada: ali o Hugo escolheu os pedidos na mão.

## 5. Regra de segurar

A rota fraca só é segurada se **todos** os pedidos dela puderem ser segurados. Um pedido não pode ser segurado quando:

| Situação | Como identificar |
|---|---|
| Tem agendamento ou data informada | `scheduled_start` preenchido |
| É de região de dia fixo | `regioes_dia_fixo.regra_dia_fixo_do_servico(servico)` devolve regra |
| É Viagem | `roteirizacao_dados.macro_regiao_do_servico(servico)` diferente de `MACRO_GRANDE_SP` |
| É reentrega | `recreated_order_origin_id` preenchido ou código com sufixo `-R<n>` |
| Já foi segurado uma vez | Código base presente em `pedidos_segurados` |
| Estouraria o prazo | Nova data de entrega maior que o prazo final |
| Sem data de entrada conhecida | `created_at` ausente (na dúvida, não segura) |

### Conta do prazo

- Entrada = `created_at` do serviço, convertido para hora de Brasília (vem em UTC sem fuso).
- Prazo final = data da entrada + 3 dias úteis (sábado e domingo não contam; entrada no fim de semana conta a partir da segunda). Sem calendário de feriados, igual ao resto do sistema.
- Nova data = próximo dia útil depois da data alvo da rodada.
- Pode segurar se nova data <= prazo final.

Exemplo: pedido entra segunda, prazo final quinta. Rodada de segunda às 18h monta a rota de terça. Segurado, vai para quarta. Não pode ser segurado de novo.

## 6. Componentes

### 6.1 `roteirizacao/rotas_fracas.py` (novo)

Regras puras, sem banco e sem rede além da geocodificação já usada pelo módulo de roteirização.

Constantes no topo:

```python
ROTAS_FRACAS_ATIVO = True
SEGURAR_ATIVO = False            # liga depois da primeira semana (secao 9)
PARADAS_ROTA_FRACA = 7
CAIXAS_ROTA_FRACA = 40
FOLGA_DISTANCIA_KM = 20
FOLGA_KM_ACUMULADO_KM = 75
FOLGA_PARADAS_EXTRA = 2          # 16 + 2 = 18; acompanha o teto escolhido no botao Roteirizar
PRAZO_ENTREGA_DIAS_UTEIS = 3
```

A folga de paradas é relativa ao teto da rodada (teto + 2), e não um 18 fixo: no botão Roteirizar o Hugo pode escolher um teto menor, e a junção não deve passar muito dele.

Funções:

- `eh_rota_fraca(sublote) -> bool`
- `absorver_rotas_fracas(sublotes, base_lat, base_lng, api_key, *, tamanho_maximo, volume_maximo, distancia_maxima_km, distancia_maxima_viagem_km, km_acumulado_maximo, km_acumulado_maximo_viagem, eh_viagem_fn) -> (sublotes, relatorio)`. O relatório traz `juntadas` (quantas rotas fracas sumiram) e `motivos` (por rota que sobrou fraca, o motivo: "vizinha mais próxima a 27 km", "sem rota vizinha na mesma região" ou "não coube nas vizinhas"). Valida cada receptora com `polimento_rotas._rota_valida` usando os limites com folga, e resequencia com `ordenar_2opt`.
- `data_entrada(servico) -> date | None` e `prazo_final(entrada: date) -> date`
- `motivo_nao_segurar(servico, data_alvo, ja_segurados: set[str], api_key) -> str | None`. `None` = pode segurar.

Garantia: o multiconjunto de ids de pedidos na saída de `absorver_rotas_fracas` é igual ao da entrada.

### 6.2 `roteirizacao/criar_rotas_diarias.py`

- `planejar_sublotes`: chama `absorver_rotas_fracas` depois de `_polir_particao`, quando `ROTAS_FRACAS_ATIVO`. Cada entrada do retorno ganha a chave `rotas_fracas` (motivo por sublote que sobrou fraco). A conferência de cobertura continua valendo sem mudança, porque a junção não tira pedido.
- `main`: tira da rodada os pedidos segurados para uma data posterior à data alvo (mesmo filtro do `incrementar_rotas`). Sem isso, rodar o job de novo na mesma noite recriaria a rota fraca, já sem poder segurar.
- `main`: depois de `planejar_sublotes` e **antes** de alocar motorista, aplica o segurar nas rotas fracas que sobraram, quando `SEGURAR_ATIVO`. Grava em `pedidos_segurados`, tira o sublote da lista e registra no resumo. Em `--modo-teste` só registra no log, não grava.
- `main` e `roteirizar_para_rascunhos`: passam `rota_fraca_motivo` no dict do rascunho.
- O resumo da execução ganha uma linha: juntadas, seguradas, sobraram.

### 6.3 `pedidos_segurados` (tabela nova, módulo `roteirizacao/pedidos_segurados.py`)

| Coluna | Conteúdo |
|---|---|
| `codigo` | Código base do pedido (normalizado), chave primária |
| `service_id` | Id do serviço |
| `segurado_em` | Quando foi segurado |
| `data_alvo_original` | Data da rota em que entraria |
| `data_nova` | Data para a qual foi adiado |
| `prazo_final` | Prazo de 3 dias úteis |
| `motivo` | Resumo da rota fraca ("3 pedidos, 17 caixas") |

Uma linha por pedido garante o máximo de 1 adiamento. Funções: `marcar`, `codigos_segurados`, `segurados_ativos(data)` (os com `data_nova` maior que a data informada).

### 6.4 `painel_agentes/rascunhos_rota.py`

Coluna nova `rota_fraca_motivo` em `rascunhos_rota`, pela migração padrão do módulo (`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`). `criar_lote_rascunhos` passa a gravar essa chave.

### 6.5 `painel_agentes/planejamento_rotas.py` e template

- Constantes espelhadas `PARADAS_ROTA_FRACA` e `CAIXAS_ROTA_FRACA` (mesmo padrão das outras travas, sem import cruzado).
- `_badges_trava`: acrescenta o aviso `rota fraca: N pedido(s), M caixa(s). <motivo>` quando o rascunho **ainda** está dentro do corte. Se o Hugo editou a rota e ela deixou de ser fraca, o aviso some. Aparece no mesmo lugar dos avisos atuais e entra no resumo de rotas com alerta.
- Pool: pedido presente em `pedidos_segurados` com `data_nova` futura ganha o chip "Segurado para consolidar (prazo dd/mm)". Continua selecionável para rota manual.

### 6.6 `vigia/`

- `vigiar.coletar_fatos`: lê `pedidos_segurados` e preenche `motivo_pool` com "segurado para consolidar (prazo dd/mm)".
- `regras.prazo`: para pedido segurado em NO_POOL, o prazo passa a ser 19h do último dia útil antes do prazo final, em vez de 1 dia útil.

### 6.7 `roteirizacao/incrementar_rotas.py`

Pula os pedidos de `segurados_ativos(data_alvo)`, para não desfazer a decisão da rodada.

### 6.8 `notificar_whatsapp.py`

Função nova `avisar_rotas_fracas(config, data_alvo, juntadas, seguradas, sobraram, modo_teste)`. Uma mensagem por rodada, só quando houve rota fraca, até 200 caracteres, deduplicada por data alvo. Usa `despachar` e respeita `whatsapp_notificacoes.ativo`.

Exemplo: `Rotas de 30/09: 2 fracas juntadas em vizinhas, 1 segurada para 01/10 (3 pedidos), 1 sobrou fraca (2 pedidos, 9 caixas). Veja no Planejamento.`

As mudanças de outra sessão nesse arquivo (aviso de chamado, linguagem simples) já foram commitadas em 29/09 (`3a35d3e`, `34322fc`). A função nova segue o padrão delas: mensagem inteira dentro de `MAX_MENSAGEM`.

### 6.9 `roteirizacao/replay_rotas.py`

Opção `--sem-rotas-fracas` para comparar com e sem a junção. `metricas_plano` ganha a contagem `rotas_fracas` (até 7 paradas e até 40 caixas).

### 6.10 `MAPA_DO_SISTEMA.txt`

Registrar o módulo novo, a tabela nova e a regra, no mesmo commit.

## 7. Casos de borda

| Caso | Comportamento |
|---|---|
| Só existe uma rota no dia e ela é fraca | Não há vizinha; vai para segurar ou avisar |
| Duas rotas fracas vizinhas | Uma pode receber a outra; o resultado é reavaliado e pode deixar de ser fraco |
| Pedido sem coordenada na rota fraca | Segue o padrão do módulo: não bloqueia por distância |
| Rota fraca segurada em parte | Não existe: ou todos os pedidos, ou nenhum |
| Pedido segurado e o Hugo põe em rota na mão | Permitido; a linha em `pedidos_segurados` fica, e o pedido não pode ser segurado de novo |
| Pedido segurado que vira rota fraca de novo no dia seguinte | Não pode segurar; sai com a etiqueta |
| Falha ao gravar `pedidos_segurados` | Não segura: a rota sai com etiqueta e o erro vai para o log |

## 8. Testes

`roteirizacao/test_rotas_fracas.py` (unittest):

- corte: 7 pedidos e 40 caixas é fraca; 8 pedidos ou 41 caixas não é;
- junção: cabe com 20 km e não cabia com 15; não cede em 100 caixas, 9h, janela, macro-região;
- junção repartida entre duas vizinhas; tudo ou nada quando um pedido não cabe;
- nível 4 e veículo grande nunca participam;
- multiconjunto de ids preservado em todos os testes de junção;
- cada motivo de não segurar, um teste;
- prazo: entrada segunda, sexta e sábado;
- rota com um pedido que não pode esperar não é segurada.

Mais: teste do aviso em `_badges_trava`, do prazo do vigia para pedido segurado, do filtro no `incrementar_rotas` e do texto do WhatsApp (até 200 caracteres).

Rodar `roteirizacao` e `painel_agentes` em comandos separados (misturar os dois pacotes dá AttributeError).

## 9. Entrada em produção

1. Replay de 31 dias com e sem a junção. Apresentar ao Hugo: rotas, rotas fracas, km, rotas acima de 9h, diâmetro. Ajustar a folga se algum indicador piorar além do aceitável.
2. Deploy com `ROTAS_FRACAS_ATIVO = True` e `SEGURAR_ATIVO = False`. Junção, etiqueta e WhatsApp ligados.
3. Uma semana de observação.
4. Ligar `SEGURAR_ATIVO` por decisão do Hugo.

Retorno rápido: `ROTAS_FRACAS_ATIVO = False` volta ao comportamento de hoje.

## 10. Limitações conhecidas

- O replay não simula o segurar: o histórico dele não guarda data de entrada nem agendamento. O efeito do adiamento só será medido em produção.
- Segurar aposta que o volume do dia seguinte ajuda a consolidar. Se não ajudar, o pedido sai no dia seguinte em rota fraca com etiqueta, um dia mais tarde.
- Sem calendário de feriados: véspera de feriado conta como dia útil normal.
- A junção com folga pode aumentar o km da rota que recebe. O ganho é a rota a menos, não o km.

## 11. Fora do escopo

- Mandar rota fraca por Lalamove ou transportadora automaticamente.
- Avisar o embarcador sobre pedido segurado.
- Botão novo no Planejamento para resolver rota fraca.
- Mais de 1 adiamento por pedido.
