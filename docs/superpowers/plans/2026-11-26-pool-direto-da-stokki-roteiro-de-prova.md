# Pool direto da Stokki: roteiro de prova e portao

Ramo `pool-stokki` (so commits locais ate o Hugo decidir). Chave: `planejamento.fonte_pool: stokki`.
Spec: `docs/superpowers/specs/2026-11-26-pool-direto-da-stokki-design.md`. Mapa: secao Pool em `MAPA_DO_SISTEMA.txt`.

## 1. Pre-requisitos (decisao do Hugo)

1. Push do ramo `pool-stokki` (nada foi pushado ate aqui).
2. `py -3.11 .claude/scripts/vps_ops.py homolog subir pool-stokki` (skill `homologar`; "deploy" continua sendo producao).
3. No config da homologacao, `planejamento.fonte_pool: stokki`. Nunca rodar script a mao em `/opt/stokki-homolog`.
4. **O que cada ambiente prova (e o que NAO prova).** Resumo honesto: nenhum ambiente fora da producao cria um
   pedido sem servico de verdade nem materializa um servico. A 1a materializacao REAL acontece em producao
   (secao 6a, "primeiro dia supervisionado").
   - **Testes (mock, banco temporario)**: o caminho novo inteiro. `nucleo.test_materializar`, `nucleo.test_pool`,
     `nucleo.test_adocao_servico`, `test_pipeline_sem_servico`, `painel_agentes/test_rascunhos_rota` (Vuupt falsa:
     criacao, adocao, recusa de regra, falha tecnica/429, heartbeat, trava assumida, envio concorrente).
   - **`--modo-teste` do pipeline NAO exercita o caminho novo**: em `pipeline.processar_pedido` o `if modo_teste:`
     (acao `simulado`) vem ANTES do `elif sem_servico:` que grava no nucleo; com a chave ligada e `--modo-teste` nada
     chega ao nucleo, nenhum pedido sem servico nasce. Serve so pra ver o payload montado e os logs. (E `--modo-teste`
     pode mandar e-mail real: `forcar_destino`.)
   - **Homologacao** (Vuupt so GET, Stokki nunca, nenhum script a mao em `/opt/stokki-homolog`, bancada so
     painel/portal): ninguem roda o pipeline la, entao **nenhum pedido nasce sem servico** (`nasceu_sem_servico`), e a
     linha legada sem servico da copia da producao fica fora do pool (correcao FIX 2). Prova-se: com a chave ligada as
     telas (pool, /planejamento, portal, Torre, /pedidos) leem o nucleo sem quebrar e os pedidos COM servico aparecem
     como antes; "Confirmar e enviar" de rascunho com ids reais bate na guarda (POST da Vuupt bloqueado) e o rascunho
     vai pra ERRO_ENVIO sem rota. NAO se prova la: pedido sem servico no pool, edicao local, cancelar sem servico,
     materializacao, adocao do id, tempo do envio.
   - **Sombra `comparar_pool.py --fonte stokki` com a chave desligada**: o pipeline continua criando o servico na
     importacao, entao o nucleo nao tem pedido nascido sem servico; a sombra stokki = sombra `nucleo` (o espelho do
     nucleo bate com a Vuupt) e o balde "sem servico" fica vazio. Prova o espelho, NAO o caminho novo.
5. **Decisao pendente do Hugo: confirmar a excecao da fila freshhub.** Com a chave ligada, a fila de replica do
   freshhub (`pipeline.py --fila-freshhub`, `_materializar_pela_fila` em `atender_fila_freshhub`) cria o servico na
   Vuupt NA IMPORTACAO (a fila exige o id da Vuupt), excecao ao "materializa so no envio" (o spec, item 8, permite).

## 1a. Deploy (producao; vale tambem com a chave desligada)

O ramo acrescenta `id_pool`, `edicao_local_json` e `nasceu_sem_servico` em `nucleo_pedidos` (tabela quente), a
coluna `codigo` em `pedidos_area_notificada` e a tabela `rascunho_envio_trava`. Os dois ALTER toleram a corrida
"duplicate column name" entre processos.

1. `git pull --no-rebase` + `chown -R www-data:www-data /opt/stokki-eventos`.
2. ANTES de reiniciar qualquer coisa, migrar uma vez como www-data:

       sudo -u www-data venv/bin/python -c "from nucleo import banco; banco.conectar().close()"

   (o ALTER tolera a corrida "duplicate column name" entre dois processos, mas migrar antes evita processo velho
   gravando no meio.)
3. Reiniciar JUNTOS, logo depois da migracao, todos os processos de longa duracao que gravam `nucleo_pedidos`:
   `painel-agentes`, `portal-cliente`, `motorista-api` (o `nucleo/operacao.py` chama `upsert_pedido`: codigo velho
   grava linha com `id_pool` NULL, que o pool pula com WARNING `pool.sem_id_pool`) e `portal-cliente-entradas`
   (`--loop`; importa o nucleo sob demanda pelo `enviar_stokki`). Conferido no codigo: `atendimento-central`
   (`atendimento/`) so cita o nucleo em comentario e a copia do repo de `confirmacao_motoristas/` nao importa o
   nucleo; a copia manual em `/opt/confirmacao-motoristas` nao da pra conferir daqui (conferir la antes:
   `grep -rn nucleo /opt/confirmacao-motoristas/*.py`).
4. Os timers (pipeline, sync, comparar_pool...) pegam o codigo novo sozinhos.
5. Verificacao pos-deploy (somente leitura), tem que dar 0:

       SELECT COUNT(*) FROM nucleo_pedidos WHERE id_pool IS NULL;

   Se der > 0 algum processo velho ainda grava: achar e reiniciar. Enquanto isso o `conectar()` nao cai (revisao de
   seguranca M2): com o banco travado no `BEGIN IMMEDIATE` do backfill ele loga 1 WARNING por processo ("backfill
   do id_pool adiado, banco travado") e segue; o `upsert_pedido` preenche o id_pool linha a linha (se a 1a
   migracao foi a adiada e o contador `nucleo_contadores` ainda nao existe, grava o pedido SEM id_pool: o pool pula
   essa linha com WARNING `pool.sem_id_pool` ate a proxima conexao sem lock completar) e a proxima conexao tenta de
   novo.

## 2. Sombra (portao, parte 1)

Na VPS, como `www-data`, com a chave ainda em `nucleo`/`vuupt`:

    sudo -u www-data venv/bin/python nucleo/comparar_pool.py --salvar --email --fonte stokki   # 1x por hora, como as outras fontes
    sudo -u www-data venv/bin/python nucleo/comparar_pool.py --historico 10 --fonte stokki      # placar por dia

- Pedido do nucleo sem servico na Vuupt aparece como "sem servico" (informativo, fora do placar).
- Divergencia = pedido que a Vuupt tem e o nucleo nao, pedido so no nucleo COM servico, ou campo diferente
  (endereco, coordenadas, caixas, janela, data, dia fixo).
- Timer: o timer existente e `infra/stokki-comparar-pool.*` (a unit existente roda nucleo e freshhub, nao inclui a fonte stokki); a fonte stokki NAO ganhou timer neste ramo:
  rodar a mao ou criar um par com `--fonte stokki` se o Hugo aprovar.
- Pedido sem servico que JA existe na Vuupt com o mesmo codigo segue a comparacao normal (id difere = divergencia), por desenho.

## 3. Passos de prova (producao supervisionada; em homologacao so o que a secao 1 item 4 diz)

| # | Passo | Esperado |
|---|-------|----------|
| 1 | Com a chave ligada, deixar rodar uma rodada REAL do pipeline (timer ou `pipeline.py` sem `--modo-teste`, como www-data) e abrir o pool (`/planejamento`). **Nao vale `--modo-teste`**: ele para antes de gravar no nucleo (secao 1 item 4) | Pedido novo nasce so no nucleo (`nasceu_sem_servico = 1`, sem servico na Vuupt) e aparece no pool como os outros (a tela nao tem selo proprio pra ele); o id negativo e o `_sem_servico_vuupt` aparecem so no JSON do pool. Linha legada ABERTA sem servico NAO aparece e o pipeline pede revisao dela |
| 2 | Montar rascunho com um pedido sem servico | Parada entra com id negativo; nada chamado na Vuupt |
| 3 | Editar endereco/janela do pedido sem servico | Grava em `edicao_local_json`; o pipeline seguinte nao regrava o campo (atencao: mudanca legitima da Stokki no mesmo campo tambem e ignorada ate materializar; hoje viraria PUT) |
| 4 | Cancelar pedido sem servico (e reagendar) | Fecha/atualiza so no nucleo; sai do pool e do rascunho |
| 5 | Confirmar e enviar o rascunho | **Producao**: servico criado na Vuupt (so leitura confirma `not_assigned`/atribuido), id real adotado (`adotar_servico`), rota enviada. **Homologacao**: nada sai |
| 5a | Clicar "Confirmar e enviar" de novo enquanto o 1o envio roda | 2o clique volta "envio deste rascunho ja em andamento" e nao mexe em nada (`rascunho_envio_trava`, renovada pelo heartbeat) |
| 5b | Vuupt falha no meio (timeout, 5xx, 429) | Rascunho em ERRO_ENVIO "falha tecnica ao criar servico na Vuupt (...): tente de novo -- nada enviado, nenhuma parada descartada"; nenhuma rota criada; os pedidos que ja materializaram ficam no id real; o reenvio segue dali |
| 6 | Pedido agrupado / reentrega -R / pedido nao ABERTO / linha que ja teve servico | Recusado com motivo na tela (a parada sai do rascunho), sem servico duplicado |
| 7 | Incremento (`incrementar_rotas`) | Pedido sem servico e materializado antes de entrar |
| 8 | Rota ja enviada + adicionar parada | Materializa e adiciona |
| 9 | Portal do cliente (pedido sem servico) | Listado e tratado pelo nucleo (cancelar, alterar entrega) |
| 10 | `/pedidos` | Acoes usam o id sintetico (nao service_id nulo) |
| 11 | Torre, feriado, laboratorio, vigia, area nao atendida | Leem o nucleo; area nao atendida avisa pelo codigo, sem repetir aviso |

## 4. Consultas de verificacao (somente leitura)

    SELECT codigo, id_pool, vuupt_service_id, status, nasceu_sem_servico FROM nucleo_pedidos WHERE vuupt_service_id IS NULL AND status='ABERTO';
    SELECT * FROM nucleo_materializacao;                 -- travas presas (devem sumir)
    SELECT nome, valor FROM nucleo_contadores;
    SELECT substr(rodado_em,1,10) dia, MAX(divergencias_proprias) FROM nucleo_reconciliacoes_pool WHERE fonte='stokki' GROUP BY dia;
    SELECT codigo FROM pedidos_area_notificada WHERE codigo IS NOT NULL ORDER BY rowid DESC LIMIT 20;

    SELECT * FROM rascunho_envio_trava;                  -- envio em andamento (deve sumir no fim de cada envio)

Na Vuupt (GET): o servico do codigo existe uma unica vez (`buscar_servico_por_code`), sem duplicado.

**Antes de ligar a chave:** revisar a 1a consulta. Desde a correcao FIX 2 so entra no pool (e so materializa) a linha
com `nasceu_sem_servico = 1` (nasceu pelo caminho sem servico do pipeline e nunca teve servico). Linha legada ABERTA
sem servico (pedido "pulado" pelo pipeline antigo, servico renomeado/apagado na Vuupt, agrupado) fica FORA do pool
(log `pool.sem_servico_legado`), e recusada na materializacao ("nao nasceu sem servico") e, se o pedido ainda esta
aberto na Stokki, o pipeline grava e pede revisao a cada rodada. Essas linhas viram trabalho manual: conferir na
Vuupt e criar/vincular o servico (o sync liga o id e o pedido volta ao pool pelo caminho normal) ou fechar. Quanto
menos houver ao ligar, melhor. Codigo com servico em `nucleo_servicos` tambem e recusado (conservador).

## 5. Criterios de aceite

- Todos os testes novos do ramo verdes; testes antigos de `nucleo`, `portal_cliente`, `roteirizacao` e `painel_agentes`
  (cada pacote em comando separado) sem regressao.
- Nenhum id negativo chega a Vuupt (log e `nucleo_materializacao` sem travas presas).
- Pedido nunca vira servico duplicado; pedido recusado mostra o motivo.
- Pool da tela = pool da sombra (so "sem servico" a mais).

## 6. PORTAO para ligar em producao (decisao do Hugo)

1. **3 dias uteis seguidos em 0** divergencias proprias na sombra `--fonte stokki` (`dias_limpos_seguidos`). Lembrar:
   com a chave desligada essa sombra prova o espelho do nucleo, nao o caminho novo (secao 1 item 4); e
2. os testes do ramo verdes (o caminho novo so e exercitado la) + homologacao para as telas com a chave ligada; e
3. o 1o dia de producao supervisionado (secao 6a): e ali que acontece a 1a materializacao de verdade.

## 6a. Primeiro dia supervisionado (producao)

A chave e GLOBAL: `planejamento.fonte_pool` e um valor so no config.yaml, lido pelo pipeline, painel, portal,
incremento e criacao de rotas. **Nao existe ligar para poucos pedidos**: ligada, todo pedido NOVO da rodada seguinte
do pipeline nasce sem servico. O mais perto de "poucos pedidos" e ligar logo antes de uma rodada de pouco volume
(escolher com o Hugo) e acompanhar ate as rotas daquela data sairem.

Antes de ligar:
- [ ] Deploy feito (secao 1a), migracao rodada, servicos reiniciados; `SELECT * FROM rascunho_envio_trava` e
      `SELECT * FROM nucleo_materializacao` vazias.
- [ ] Copia do `dados.db` do dia (backup GCS da noite ou copia manual).
- [ ] 1a consulta da secao 4: linhas legadas ABERTAS sem servico revisadas (ficam fora do pool e pedem revisao).
- [ ] Plano de volta (secao 8) lido, com o checklist pre-volta pronto pra rodar.
- [ ] Alguem olhando o painel e a Vuupt durante a 1a rodada e o 1o envio.

Durante:
- [ ] Depois da 1a rodada do pipeline: `SELECT COUNT(*) FROM nucleo_pedidos WHERE nasceu_sem_servico = 1 AND status='ABERTO'`
      bate com os pedidos novos da rodada; nenhum servico novo criado na Vuupt pela importacao (exceto a fila freshhub).
- [ ] 1o envio com um rascunho PEQUENO (1 a 3 pedidos sem servico): para cada pedido, conferir na Vuupt (GET,
      `buscar_servico_por_code`) que o servico existe UMA vez, com endereco/janela/data/caixas certos; conferir a rota
      (paradas, ordem, motorista); no nucleo o pedido ficou com o id real e EM_ROTA.
- [ ] Medir o tempo do envio (log "N de M pedido(s) sem servico ganharam servico na Vuupt em X s").
- [ ] Log sem "falha tecnica" em sequencia (429 = parar e espaçar os envios) e sem trava presa.
- [ ] Sem "possivel problema sistemico" (todos os pedidos recusados com o mesmo status: parar e chamar o Hugo antes
      de reenviar) e sem `[ALERTA_ROTA_NAO_CRIADA]` no e-mail da criacao de rotas (rota adiada por falha tecnica).
- [ ] Incremento e "adicionar parada em rota enviada" com um pedido sem servico, conferindo na Vuupt.

Se algo der errado: nao reenviar em massa; voltar a chave (secao 8) depois do checklist pre-volta.

## 7. Risco: timeout do envio

A materializacao cria o servico pedido a pedido, **~5-7 chamadas a Vuupt por pedido**, de forma sincrona no
"Confirmar e enviar" (sem barra de progresso). Rascunho com dezenas de pedidos pode estourar o timeout do waitress/Caddy.
Medir no 1o dia; se estourar: limite de pedidos por clique ou execucao em segundo plano (nao feito).

- Duplo clique: `enviar_rascunho` reserva o rascunho em `rascunho_envio_trava` (INSERT OR IGNORE, solto no fim, velho
  apos 15 min SEM renovacao). O 2o envio concorrente do mesmo rascunho volta "envio deste rascunho ja em andamento" sem
  mexer em nada. No "Confirmar e enviar (todas)" uma excecao num rascunho vira `ok: False` com a mensagem e os outros
  seguem; o log traz o tempo de cada rascunho.
- Heartbeat (correcao FIX 3): o envio vivo renova `iniciado_em` da sua trava (UPDATE com o dono conferido, transacao
  curta, nunca atravessando chamada HTTP) antes de cada pedido materializado, antes de cada passo na Vuupt e antes de
  criar a rota; a trava por pedido (`nucleo_materializacao`) tambem e renovada antes de buscar e de criar o servico.
  Se a renovacao acha a trava assumida por outro, o envio para com "envio assumido por outro clique/processo ...:
  este envio parou sem criar a rota" (status e paradas intactos). O status do rascunho e reconferido antes de criar a
  rota e depois dela; `marcar_erro_envio` so muda RASCUNHO/ERRO_ENVIO (nunca rebaixa ENVIADO/DESCARTADO). Limite que
  sobra: um UNICO passo (uma chamada a Vuupt com backoff, ou o `criar_rota_removendo_conflitos` inteiro) com mais de
  15 min ainda perde a trava.
- Falha tecnica (correcao FIX 1 + fix round 1): timeout, 408/425/429/5xx (depois do backoff do `http_retry`),
  401/403 (mensagem propria "credencial/permissao da Vuupt ...: avise o Hugo"), banco ocupado, modo teste, resposta
  da Vuupt sem id ou erro inesperado NAO tira a parada: vai em `ResultadoMaterializacao.transitorios` e o envio do
  rascunho para em ERRO_ENVIO ("falha tecnica ao criar servico na Vuupt (...): tente de novo -- nada enviado, ..."),
  sem rota parcial; na resposta do envio essas paradas vem em `codigos_pendentes_tecnico` (ficaram no rascunho). So
  recusa de REGRA (nao ABERTO, agrupado, -R, pulado_atribuido, done/canceled, ja teve servico, id sintetico
  inexistente) ou erro PERMANENTE da Vuupt (`VuuptAPIError` 400/404/409/410/422: "a Vuupt recusou o pedido
  (Status NNN): ...") tira a parada (`codigos_sem_servico`, volta ao pool) e a rota sai com o resto. O status vem do
  texto "Status NNN" (o client nao tem atributo). O incremento pula o pedido so naquela rodada (WARNING no
  transitorio e na trava ocupada, ERROR na recusa; ali nao ha rota nova, so adicao a rota existente); a fila do
  freshhub nao devolve erro no transitorio (o item volta a pendente em 1 h) e devolve o erro na recusa. Os pedidos do
  lote que materializaram antes da falha ficam no id real (servico `not_assigned` na Vuupt).
- **Rota automatica nunca sai parcial por falha tecnica** (Hugo, onda 3): na criacao direta
  (`criar_rotas_diarias.py` sem `--gerar-rascunho`, `_com_servico`) falha tecnica ou trava ocupada em QUALQUER pedido
  do lote levanta `RotaAdiada`: aquela rota NAO e criada na rodada (nenhuma chamada a `criar_rota_removendo_conflitos`),
  todos os pedidos seguem no pool (quem materializou antes fica com o servico `not_assigned`), log ERROR com a rota e o
  motivo, e o resumo da execucao (e-mail `notificar_execucao`) sai com a etapa "Criacao de rotas" em erro e
  `[ALERTA_ROTA_NAO_CRIADA]` com as rotas. A rodada seguinte (ou o planejamento) cria a rota completa. Recusa
  PERMANENTE (regra ou 400/404/409/410/422) continua tirando so aquele pedido e a rota sai com o resto (decisao de
  borda pro Hugo confirmar). O envio de rascunho ja seguia o mesmo principio (aborta em ERRO_ENVIO).
- **Erro sistemico nao remove paradas** (Hugo, onda 3): se TODOS os pedidos sem servico do lote (2 ou mais) sao
  recusados pela Vuupt com o MESMO status permanente e nenhum deu certo, o lote inteiro vira transitorio ("a Vuupt
  recusou todos os pedidos com o mesmo erro (Status NNN: ...): possivel problema sistemico, nada foi removido, avise
  o Hugo"): rascunho em ERRO_ENVIO com tudo dentro; criacao direta adia a rota. Com 1 pedido so (incremento, fila,
  adicionar parada) segue a regra normal; status diferentes no lote, recusa de regra no meio ou 1 sucesso tambem.
  O mesmo vale pro erro LOCAL de dado (onda 4 round 1): todos os pedidos do lote (>= 2) com o mesmo tipo de erro ao
  montar o servico (ValueError/TypeError, ex. bug de formato no payload) = "possivel problema sistemico: <TipoErro>;
  nada foi removido, avise o Hugo"; um malformado so, tipos misturados ou algum sucesso = so o malformado sai.
- Trava velha por pedido: se o processo morre no meio do envio (restart do painel), a trava do pedido em
  `nucleo_materializacao` fica ate **10 minutos**; nesse tempo cada nova tentativa espera ate **30 s por pedido**
  travado e devolve "tente de novo" (nada e descartado). A mensagem ao operador e "tente de novo"; depois de 10 min a
  trava expira sozinha.

## 8. Plano de volta

- Trocar `planejamento.fonte_pool` para o valor anterior e reiniciar `painel-agentes` e `portal-cliente`.
- Pedido que ja esta sem servico: a materializacao decide por **id negativo, nao pela chave**; rascunho com id negativo
  continua enviando (cria o servico) em vez de mandar id sintetico a Vuupt. Com a chave desligada nao nascem novos
  pedidos sem servico: o pipeline volta a criar o servico na importacao e o `adotar_servico` troca o id.
- Dedupe da area nao atendida por codigo continua valendo (mudanca deliberada).
- **Edicoes do Planejamento podem se perder na volta.** Com a chave desligada o pipeline antigo cria o servico a partir
  do payload da Stokki: o que foi editado so no nucleo (endereco, datas de equipe/feriado guardadas so nas colunas do
  nucleo, marcas de `edicao_local_json`) nao vai junto, e o dia fixo e reaplicado na roteirizacao. Conferir os pedidos
  editados antes de desligar.
- **Efeitos colaterais na 1a rodada do pipeline depois da volta** (conferido em `pipeline.processar_pedido`): o pedido
  ainda ABERTO sem servico e visto como NOVO (`servico_existente = vuupt.buscar_servico_por_code(...)` = None). Por isso:
  1. **data automatica nova**: `agendamento_automatico(None, ...)` calcula a data pela hora DESTA rodada (pode ser
     outro dia que a gravada no nucleo, ex. rodada depois do corte das 14h15) em vez de repetir a do nucleo;
  2. **aviso de dia fixo reenviado ao embarcador**: a checagem "a Vuupt ja tem essa data" compara com
     `servico_existente` (None), entao `ajuste_dia_fixo` e montado de novo e o `main()` manda o aviso ao remetente
     outra vez (com a chave ligada ele ja tinha recebido);
  3. o servico nasce na Vuupt pelo caminho antigo, com o payload da Stokki (edicoes so do nucleo ficam de fora, item
     acima); o rascunho que ainda tem a parada com id negativo continua materializando por id no envio (nao pela chave).
- **Checklist pre-volta** (antes de trocar a chave):
  - [ ] `SELECT codigo, id_pool, agendamento_inicio, agendamento_fim, edicao_local_json FROM nucleo_pedidos
        WHERE vuupt_service_id IS NULL AND status='ABERTO' AND nasceu_sem_servico = 1;` -- salvar a lista.
  - [ ] Se possivel, ENVIAR antes os rascunhos com esses pedidos (a materializacao usa as colunas do nucleo: data e
        edicao vao certas) e so depois desligar; quanto menos pedidos sem servico, menos efeito colateral.
  - [ ] Para os que sobrarem: anotar data/endereco editados pra reaplicar na Vuupt depois da 1a rodada; avisar a equipe
        que o aviso de dia fixo desses pedidos vai de novo ao embarcador (sem chave propria pra suprimir).
  - [ ] Depois da 1a rodada do pipeline com a chave desligada: cada codigo da lista tem UM servico na Vuupt
        (`buscar_servico_por_code`), com a data/endereco certos; reaplicar o anotado.

## 9. Itens adiados e avisos (de `.superpowers/sdd/.../progress.md`)

AVISO AO HUGO
- Dedupe da area nao atendida por codigo vale tambem com a chave desligada: servico recriado com o mesmo codigo nao reavisa.

Ruling
- Subir em homologacao exige push do ramo: nao feito; depende do Hugo.

Revisao de seguranca do PR #92 (onda 4)
- Guarda unica (M1/L1): `nucleo.pedidos.eh_sem_servico_legitimo(conn, codigo_ou_linha)` = sem `vuupt_service_id`,
  `nasceu_sem_servico = 1` e fora de servico agrupado vivo (`servicos_que_agrupam`). Vale em tudo que age LOCAL
  pelo id sintetico: `cancelar_sem_servico` (False), `editar_sem_servico`/`gravar_agendamento_sem_servico`
  (ValueError "tratar pela operacao"), `cancelar_sem_servico_local`, portal (`com_id_sintetico`,
  `servico_sem_servico`), `/pedidos` (`tabela_pedidos.listar(id_sintetico)`), `cancelar_cancelados_stokki` e o
  fechamento do pipeline (pede revisao). Linha orfa (servico agrupado/renomeado/apagado na Vuupt) segue o fluxo de
  sempre: o cancelamento do cliente vai pra OPERACAO. Vale tambem com a chave desligada (as acoes do painel por id
  sintetico nao leem a chave).
- L3: id fora da faixa do INTEGER do SQLite (ex. `/api/pedidos/-99999999999999999999/alteracao`) = 404, nao 500.
- L4: dado do nucleo malformado ao montar o payload (ValueError/TypeError) e recusa do pedido ("dado invalido no
  nucleo"): sai so aquela parada; banco/rede seguem transitorios.
- M2: ver secao 1a item 5.
- ACEITO (L2): o id sintetico (-id_pool) e um contador global; um cliente que ve os proprios ids no portal pode
  estimar o volume total de pedidos da plataforma. Informacao de baixo valor, sem acesso a dado de outro cliente.
- ACEITO (L5): a materializacao roda sincrona dentro do request do painel ("enviar todas"); com 429 pode segurar
  threads do waitress por minutos. As travas (rascunho 15 min, pedido 10 min com heartbeat) impedem duplicar.
- LACUNA CONHECIDA (apos o merge com o master, commit 6d6d1fd): ao SALVAR os dias que o cliente recebe no modal
  (`realocar_pedidos_do_cliente`, `painel_agentes/planejamento_rotas.py` ~:2650-2662), os pedidos desse cliente
  que ainda NAO tem servico na Vuupt nao sao realocados na hora (so as rodadas seguintes fazem). Se alguem
  enviar um rascunho a mao antes da proxima rodada, o servico nasce para um dia que o cliente nao recebe.
  Correcao futura: listar tambem os pedidos sem servico legitimos como `-id_pool` nessa funcao. Decisao do Hugo.

Limitacao conhecida (fora desta onda)
- Base do payload: a materializacao parte de `dados_json["payload"]`, que o `upsert_pedido` mescla por `json_patch`
  a cada rodada; as colunas do nucleo vao por cima (`montar_payload`), mas campo do payload que nao tem coluna
  correspondente fica com o valor da ultima rodada do pipeline, e chave que a Stokki deixou de mandar pode sobrar do
  payload anterior (json_patch nao apaga o que nao veio). Spec item 9; nao tratado.

Minor (deferred)
- `fonte_pool` desconhecida devolve a string em vez de cair para `vuupt`; `eh_sem_servico(None)` sem teste.
- Import lazy de `adotar_servico` em `pedidos.py`; dedupe do plano_original ignora lote_id; pedido com id real X recebendo real Y via upsert nao passa por `adotar`.
- E-mail de `cancelar_cancelados` mostra id negativo e assunto "cancelado(s) na Vuupt"; `ROTULOS["gravado_nucleo"]` sem uso; `conferir_pool` Enviado sem prova so avisa.
- Materializacao: espera de ate 30 s por trava velha (ver secao 7); motivos duplicados no card; sem barra de progresso; se o DELETE da trava falha a conexao pode ficar em transacao. (Janela de duplo-envio e excecao que encerrava o "enviar todas": corrigidas na revisao final, I2.)
- `adicionar_em_rota_enviada` usa o 1o `res.ok` (lote de 1).
- Cancelar com id negativo deixa a parada em rascunho OFERTADA ate o envio; falha parcial de `tirar_dos_rascunhos` pula `remover_parada`/tratativa; `agendamentos_origem` guarda id negativo; se re-salvar o fingerprint base falha (best-effort) a regressao volta para o pedido; edicao de data sem rodada do pipeline antes do envio causa 1 PUT.
- Regra unica de marca de edicao local (feriado/confirmados x dia fixo/data automatica) a triar; `_sem_servico_do_nucleo` le o pool inteiro por pagina; `_conferir_agrupamento` faz 1 query por pedido.
- Fase 4 do spec (desligar o sync de descoberta da Vuupt) nao feita.
