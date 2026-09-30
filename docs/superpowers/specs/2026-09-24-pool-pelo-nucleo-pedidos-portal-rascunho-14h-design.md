# Pool do planejamento pelo núcleo, pedido do portal nasce no núcleo, rascunho às 14h10

Data: 24/09/2026. Pedido do Hugo.

## Objetivo

O fluxo de hoje dá uma volta: tela do portal → Stokki → (pipeline 18h/22h)
→ Vuupt → pool do planejamento (lido ao vivo da Vuupt) → roteirização → Vuupt.
Um pedido enviado pela nossa tela às 10h só aparece no planejamento depois
das 18h.

O que muda, em três entregas, cada uma provada antes da seguinte:

1. **O pool do planejamento passa a ler do nosso banco (`nucleo_pedidos`)**,
   não da API da Vuupt. Base da etapa 4 do
   `DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md` ("pedido e rota nascem no núcleo").
2. **Pedido enviado pela tela do portal nasce no núcleo assim que a Stokki
   devolve o código PS**, e aparece no pool minutos depois do envio. A Vuupt
   só recebe o serviço quando a rota é enviada (ou quando o pipeline das
   18h/22h passa, o que vier primeiro).
3. **Rascunho automático às 14h10 e incrementar de 30 em 30 min até as 19h**,
   com botão pra rodar na hora.

Os motoristas continuam no app da Vuupt; a Vuupt continua recebendo todas as
rotas (decisão de 12/09: replicar tudo lá até a virada, por causa do
financeiro).

## Decisões do Hugo (24/09)

- Direção: começar a sair da Vuupt por esta ponta (pool + nascimento do
  pedido), não fazer a saída completa agora nem só encurtar a demora.
- "Clientes cadastrados" = os embarcadores que usam a tela de envio do
  portal (`portal_clientes_envio`). Os demais seguem pelo pipeline como hoje.
- O pedido aparece no planejamento **quando a Stokki devolve o código PS**
  (não no instante do envio). Envio com erro ou duplicado não aparece.
- **Aberto na Stokki não é pronto.** Só "Aguardando Transportador" garante
  que o pedido pode sair.
- Regra da operação: pedido lançado/colocado em Aberto **até as 14h** é
  finalizado no mesmo dia. Por isso o **rascunho das 14h10 considera todos os
  pedidos do pool, inclusive os em Aberto**. Depois disso o incrementar só
  encaixa pedido já pronto (Aguardando Transportador).
- Incrementar: timer de 30 em 30 min **e** botão pra rodar o fluxo na hora.
- Botão "Atualizar agora" no planejamento (sincroniza o núcleo com a Vuupt
  antes de recarregar o pool).
- Parada do rascunho identificada pelo **código do pedido**; o id da Vuupt
  vira referência (recomendação minha, aceita na conversa).

- Pedido do portal cujo XML traz transportadora de redespacho passa pela
  mesma regra do pipeline (`regras/endereco.resolver_endereco_entrega`), pra
  que o pool mostre o galpão da transportadora e não o destino final
  (confirmado pelo Hugo em 24/09).

## Fora de escopo

- Torre, badge do menu, portal do cliente, `laboratorio_rotas`,
  `reconciliar_pedidos_retirada` e `dados_cliente` continuam lendo o pool da
  Vuupt. Migram um por vez na etapa 3 do plano de saída.
- Clientes que não usam a tela (entram por e-mail ou direto na Stokki):
  continuam chegando ao núcleo pelo pipeline das 18h/22h e pelo sync de
  15 min. Nada muda pra eles além do chip "Em separação".
- App dos motoristas, canhoto, status de execução, relatórios do financeiro.
- Importação por planilha: usa o mesmo caminho do XML (`portal_envios`), então
  ganha o nascimento de graça, mas não é testada aqui (nunca rodou contra a
  Stokki real).
- Cancelar/reagendar/editar endereço de um pedido que ainda não existe na
  Vuupt: fica bloqueado com aviso até o serviço existir. Fazer essas ações
  no núcleo é etapa 4 completa.

## Como está hoje

- **Entrada pela tela:** `portal_cliente/envio_pedidos.confirmar_envios`
  grava `portal_envios` (`NA_FILA`). O worker `enviar_stokki.py --loop`
  (unit `portal-cliente-envios`, ~20 s) sobe pelo wizard da Stokki e grava
  o PS em `_gravar_codigo` (`:470`, chamado em `:609` com
  `status=CRIADO`). Envio que volta sem código é achado depois por
  `reconciliar_codigos` (`:703`). A geocodificação em
  `classificar_area_envio` (`:1112`) calcula a coordenada e **joga fora**.
- **Stokki → Vuupt:** `pipeline.py` às 18h (`infra/sequencia_tarde.sh`, via
  `executar_tudo.py`) e 22h (`sequencia_noite.sh`), ou pelo botão "Somente
  Importação". Lista "Aguardando Transportador" de todos e **qualquer status
  em aberto** dos embarcadores 98 (Quatro Estrelas), 18 (Dourado) e 79
  (Jersey) (`EMBARCADORES_IMPORTAR_ABERTOS`, `:120`). Monta o payload em
  `montar_payload_vuupt` (`:258`, função pura), geocodifica (`:750`), chama
  `vuupt.criar_ou_atualizar_servico` (upsert pelo `code`, só sobrescreve
  `not_assigned`) e grava no núcleo com `nucleo.pedidos.registrar_importacao`
  (`:779`). O núcleo **não** recebe o status da Stokki.
- **Pool:** `planejamento_rotas.buscar_pool_e_agendados` (`:631`) chama
  `vuupt.listar_servicos(status=not_assigned, include=customer)` (`:696`)
  e monta cada item em `_servico_para_pool` (`:458`). Mesma leitura ao vivo
  em `criar_rotas_diarias.py:645`, `incrementar_rotas.py:388` e
  `roteirizar_selecionados` (`planejamento_rotas.py:988`).
- **Espelho:** `nucleo/sincronizar_servicos_vuupt.py` (timer
  `stokki-nucleo-sincronizar-servicos`, :05/:20/:35/:50) copia os serviços da
  Vuupt pra `nucleo_pedidos` (chave `codigo` normalizado, sem `#`). Já tem
  endereço, lat/lng, `caixas` (= `dimension_3`), `sender_id`, destinatário
  (`customer.name`/`code`), `horario_inicio/fim`, `agendamento_inicio/fim`,
  `status_provedor`, `fluxo`, `excluido_em`, `vuupt_service_id`.
  Lacunas: serviço com vários códigos (`"#PS-1, PS-2"`) é rejeitado pela
  `_RE_CODIGO_PEDIDO` (`:69`); o pipeline grava pedido "pulado" sem
  `vuupt_service_id`.
- **Rascunho:** `rascunhos_parada.service_id INTEGER NOT NULL` +
  `UNIQUE(rascunho_id, service_id)` (`rascunhos_rota.py:89-110`). Todo o
  fluxo identifica a parada por `service_id`: mover, reordenar, fundir,
  otimizar, remover, adicionar, cancelar (`:227-770`, `:1542`), o envio
  (`enviar_rascunho:1268` → `rotas_client.criar_rota_removendo_conflitos`
  → `activities[].service_id`) e o template (36 ocorrências).
- **Incrementar:** `incrementar_rotas.py` encaixa em rotas **já na Vuupt**
  (`POST /routes/{id}/activities`), corte `HORA_CORTE_PEDIDO = 19`; sem
  timer. Rascunho pendente tem o próprio incremento
  (`rascunhos_rota.incrementar_rascunhos_com_selecionados:1125`, botão).

---

## Entrega 1: pool lê do núcleo

### Chave de configuração

`config.yaml`, seção `planejamento`: `fonte_pool: vuupt | nucleo`. Padrão
`vuupt` (ausente = vuupt). Lida a cada requisição/execução, sem restart.
Voltar atrás é trocar a chave.

### `nucleo/pool.py` (módulo novo)

`listar_pool() -> list[dict]`: devolve os pedidos em aberto no **mesmo
formato do serviço da Vuupt** que `_servico_para_pool`, `resolver_janela`,
`regra_dia_fixo_do_servico`, `extrair_volume_caixas` e
`chegou_dentro_do_corte` já leem. Assim nenhum consumidor muda de forma.

| chave do dict | de `nucleo_pedidos` |
|---|---|
| `id` | `vuupt_service_id` (None se ainda não existe na Vuupt) |
| `code` | `codigo` com `#` na frente (o formato que a Vuupt devolve hoje) |
| `title`, `address`, `latitude`, `longitude`, `sender_id`, `note` | `titulo`, `endereco`, `latitude`, `longitude`, `sender_id`, `nota` |
| `dimension_3` | `caixas` |
| `scheduled_start`, `scheduled_end` | `agendamento_inicio/fim` |
| `created_at` | `criado_em_provedor` ou `criado_em` |
| `customer` | `{name: destinatario_nome, code: destinatario_codigo, phone_number, operating_hour_start/end: horario_inicio/fim}` |
| `customer_id`, `status` | `customer_id`, `status_provedor` ou `"not_assigned"` |
| `_origem`, `_status_stokki`, `_pronto_em` | novos, usados pela entrega 2 |

Critério de "em aberto": `status = 'ABERTO'`, `fluxo = 'ENTREGA'` (ou NULL),
`excluido_em IS NULL`, e (`vuupt_service_id IS NOT NULL` **ou**
`origem = 'PORTAL'`). O "ou" entra na entrega 2; nesta, só com id.

**Fuso:** a Vuupt devolve `scheduled_*` em UTC sem fuso e o pool de hoje lê
o valor bruto como hora local (`_hora_de_iso`, `_data_agendada`). No núcleo
está em hora local convertida. `listar_pool` devolve **o que o pool de hoje
enxerga** (mesmo valor que a Vuupt devolveria), pra não mudar comportamento
nesta entrega. O comparador em sombra confirma; corrigir a leitura errada é
tarefa à parte.

### Quem passa a ler dela

Uma função `roteirizacao/roteirizacao_dados.listar_pool_not_assigned(config)`
escolhe pela chave e é chamada em:

- `planejamento_rotas.buscar_pool_e_agendados` (`:696`) e
  `roteirizar_selecionados` (`:988`);
- `criar_rotas_diarias.py:645`;
- `incrementar_rotas.py:388`.

Os quatro veem o mesmo pool, sempre.

### Ajustes no espelho (`sincronizar_servicos_vuupt.py`)

1. Serviço com vários códigos entra no núcleo: a chave vira o texto
   normalizado inteiro (`PS-1, PS-2`), como o rascunho já trata via
   `_codigos_base_lista`.
2. Depois de cada ação do planejamento que escreve na Vuupt (cancelar,
   reagendar, editar endereço, nível, mover pra rota enviada), o endpoint
   ressincroniza **aquele serviço** no núcleo antes de responder
   (`sincronizar_servicos_vuupt.sincronizar_um(service_id)`, função nova
   sobre o que já existe). Sem isso o pool fica até 15 min atrasado.
3. Pedido criado pelo pipeline já entra no núcleo na hora (dual-write
   existente); nada a fazer.

### Botão "Atualizar agora"

Na barra do pool do planejamento. `POST /api/planejamento/atualizar-pool`
roda a sincronização incremental (a mesma do timer, `--incremental`, sem a
reconciliação completa) e devolve o pool. Enquanto roda, o botão fica
desabilitado com "Atualizando…". Medir o tempo em produção; se passar de
~10 s, rodar em thread e o front faz polling como já faz com agentes.

### Sombra e virada

- `nucleo/comparar_pool.py`: lista os dois pools, casa por `code`, compara
  os campos que a tela mostra (id, coordenadas, `dimension_3`, janela
  resolvida, dia fixo, nível, `tipo_area`). Saída: só diferenças, com
  `--salvar` numa tabela `nucleo_reconciliacoes_pool` (dia, hora, total,
  diferenças) e `--email`.
- Timer `stokki-comparar-pool.timer` de hora em hora, 07h–20h.
- Critério de virada: **3 dias úteis seguidos** sem diferença sem
  explicação. A virada (`fonte_pool: nucleo`) é do Hugo.

### Testes

- `nucleo/test_pool.py`: `listar_pool` num banco de exemplo (pedido com
  agendamento, sem agendamento, com vários códigos, retirada fora, excluído
  fora, sem `vuupt_service_id` fora).
- Teste que `_servico_para_pool(listar_pool()[i])` ==
  `_servico_para_pool(servico_vuupt_equivalente)`.
- `comparar_pool.py` rodando de verdade na VPS, com `fonte_pool: vuupt`.

---

## Entrega 2: pedido do portal nasce no núcleo

### Colunas novas em `nucleo_pedidos` (ALTER aditivo em `nucleo/banco.py`)

| coluna | uso |
|---|---|
| `status_stokki TEXT` | último status visto na Stokki (`Open`, `Waiting for Carrier`, …) |
| `pronto_em TEXT` | hora local em que virou `Waiting for Carrier`; NULL = em separação |
| `portal_envio_id INTEGER` | liga ao `portal_envios.id` que deu origem |

O pipeline passa a gravar `status_stokki` (o `status` do laço de listagem em
`pipeline.py:892/912/929`; "Aguardando Transportador" também preenche
`pronto_em`). Assim os pedidos em Aberto dos embarcadores 98/18/79, que já
entram na Vuupt hoje, ganham o chip "Em separação" igual aos do portal.

### Nascimento: `nucleo/pedidos.registrar_envio_portal(conn, envio: dict, codigo: str)`

Chamado por `enviar_stokki.py` logo depois de `_gravar_codigo` com
`status=CRIADO` (`:609`) e em `reconciliar_codigos` quando acha o código de um
envio `CRIADO`. Idempotente (upsert pelo código; campos None não apagam).

| campo do núcleo | de onde |
|---|---|
| `codigo` | PS normalizado |
| `titulo` | `destinatario_nome` (o mesmo `title` que o pipeline monta) |
| `destinatario_nome/codigo/telefone` | `portal_envios.destinatario_*` |
| `endereco` | `resolver_endereco_entrega` com um `detalhe` montado a partir do XML (destinatário + transportadora), como o pipeline. Sem transportadora de redespacho, cai no endereço do destinatário: `logradouro, bairro, município - UF, CEP` |
| `latitude/longitude` | `geocodificacao.geocodificar` (cache `geocache`), reaproveitando a coordenada que `classificar_area_envio` já calcula: ela passa a devolver e gravar `latitude/longitude` em `portal_envios` |
| `caixas` | `max(1, round(volumes × interno.fator_ponderado))`, `interno` buscado por `cnpj_embarcador` |
| `sender_id`, `remetente_nome`, `remetente_codigo` | `interno` por `cnpj_embarcador` |
| `horario_inicio/fim` | `portal_destinatarios` (horário de recebimento) |
| `agendamento_inicio/fim` | `portal_envios.agendamento_*` quando informado |
| `status`, `fluxo`, `origem` | `ABERTO`, `ENTREGA`, `PORTAL` |
| `status_stokki`, `pronto_em` | `Open`, NULL |
| `vuupt_service_id` | NULL |
| `dados_json` | `{"portal_envio": {...campos usados...}}` pra o envio da rota montar o payload sem reler o portal |

`enviar_stokki.py --modo-teste` não grava no núcleo (imprime o que gravaria).

### Prontidão: `roteirizacao/atualizar_prontidao_stokki.py`

- Lista "Aguardando Transportador" na Stokki
  (`stokki.pedidos.iterar_todos_pedidos`, sessão via `StokkiSession`, que já
  espera a trava cooperativa de `stokki/sessao_uso.py`). Uma passada, sem
  filtro por embarcador.
- Pra cada código listado que está no núcleo com `pronto_em IS NULL`: grava
  `status_stokki = 'Waiting for Carrier'`, `pronto_em = agora`.
- Pedido do núcleo `ABERTO` com `pronto_em IS NULL` que **não** está na lista:
  continua em separação (não conclui nada; a Stokki tem 4 outros status em
  aberto e cancelamento vem por outro caminho).
- `--modo-teste` só imprime. Sai com 0 mesmo sem mudanças.
- Roda dentro do timer da entrega 3 e pelo botão. Não tem timer próprio.

### Pool

- `listar_pool` inclui `origem = 'PORTAL'` sem `vuupt_service_id`.
- `_servico_para_pool` ganha `origem` e `pronto` (`pronto_em IS NOT NULL` ou
  `status_stokki` desconhecido = pronto, pra não travar pedido antigo). O
  card mostra chip **"Em separação"** quando `pronto == False` e
  "Portal" quando `origem == 'PORTAL'`. Sem bloqueio de arrastar.
- Filtro rápido "só prontos" no pool (checkbox, guardado em
  `sessionStorage` como os outros filtros).

### Parada identificada pelo código

- `rascunhos_parada`: `service_id` passa a aceitar NULL (recriar a tabela
  com migração dentro do `_criar_tabelas`, copiando os dados); `codigo` vira
  obrigatório e `UNIQUE(rascunho_id, codigo)`.
- `rascunhos_rota.py`: as funções que recebem `service_id` passam a receber
  `codigo` (mover, reordenar, fundir, otimizar, remover, adicionar,
  atualizar endereço/nível, cancelar). `service_id` continua gravado na
  parada quando existe.
- `nucleo/pedidos.service_id_do_pedido(codigo) -> int | None`: único lugar
  que resolve código → id da Vuupt (lê `nucleo_pedidos`).
- Endpoints em `painel_agentes.py` e o template `planejamento_rotas.html`
  trocam a chave `service_id` por `codigo` nas chamadas (`data-codigo`).
  Ações que escrevem na Vuupt (cancelar, reagendar, editar endereço, nível,
  mover pra rota já enviada) chamam `service_id_do_pedido`; se `None`,
  devolvem 409 com "Pedido ainda não está na Vuupt (entra quando a rota for
  enviada)" e o front mostra o aviso.
- `incrementar_rotas.py` e `roteirizar_selecionados` já trabalham com o
  dict do pool; passam a casar por `code` em vez de `id`.

### Envio da rota cria o serviço que falta

Em `enviar_rascunho` (`rascunhos_rota.py:1234`), antes de
`criar_rota_removendo_conflitos`:

1. Pra cada parada com `service_id_do_pedido(codigo) is None`:
   `roteirizacao/servico_vuupt.criar_para_pedido(codigo)` (módulo novo) lê o
   pedido do núcleo (`dados_json.portal_envio`), monta o payload com
   `pipeline.montar_payload_vuupt` (mesmas entradas: `detalhe`,
   `endereco_resolvido`, `skill_ids` via `vuupt.skill_ids_por_nome`,
   `sender_id`, apelido, fator, `scheduled_*`), chama
   `vuupt.criar_ou_atualizar_servico` e grava `vuupt_service_id` no núcleo
   com `registrar_importacao`.
2. Falha em qualquer parada aborta o envio inteiro com a mensagem do erro;
   nada é enviado pela metade (os serviços já criados ficam `not_assigned`
   e o pipeline/próximo envio os reaproveita pelo `code`).
3. Segue como hoje com os `service_id` resolvidos.

**Aviso, não bloqueio:** se alguma parada tem `pronto_em IS NULL`, a resposta
do envio traz a lista e o front mostra "N pedido(s) ainda em separação na
Stokki: …" antes de confirmar.

**Convergência com o pipeline:** o pipeline das 18h/22h acha o serviço pelo
`code`; se já está atribuído a uma rota, `criar_ou_atualizar_servico` não
mexe (`pulado_atribuido`) e `registrar_importacao` só completa campos. Se o
pipeline passar antes do envio, é ele que preenche `vuupt_service_id`, e o
envio não cria nada. Os dois caminhos chegam ao mesmo estado.

### Testes

- `nucleo/test_pedidos_portal.py`: `registrar_envio_portal` a partir de uma
  linha de `portal_envios` de exemplo (XML real anonimizado): campos,
  idempotência, `fator_ponderado`, agendamento, sem `interno` → erro claro.
- `roteirizacao/test_prontidao_stokki.py`: listagem simulada marca
  `pronto_em`; não desmarca; ignora código fora do núcleo.
- `painel_agentes/test_rascunhos_codigo.py`: mover/remover/fundir por
  código; parada sem `service_id`; envio cria serviço (Vuupt simulada),
  aborta em falha, avisa em separação.
- Migração de `rascunhos_parada` num banco de exemplo com rascunhos
  pendentes.
- Prova em produção: um envio real pela tela (cliente de teste do portal ou
  um pedido do Quatro Estrelas acompanhado) → aparece no pool com "Em
  separação" → prontidão marca → entra num rascunho → envio cria o serviço
  na Vuupt → pipeline das 18h não duplica (conferir com
  `verificar_pedidos_duplicados_vuupt.py`).

---

## Entrega 3: rascunho às 14h10 e incrementar de 30 em 30

Só entra depois que a 1 estiver virada (`fonte_pool: nucleo`) e a 2 provada.

### Rascunho das 14h10

- `infra/stokki-rascunho-tarde.timer` (`OnCalendar=Mon..Fri 14:10`) →
  `criar_rotas_diarias.py --gerar-rascunho`. Pega **todo o pool**, inclusive
  em separação (regra das 14h).
- O passo `--gerar-rascunho` sai de `sequencia_tarde.sh` (18h). Se às 18h
  não houver rascunho pendente pra data alvo (feriado, falha), o
  `incrementar` da rodada seguinte gera um, com log.
- Fim de semana e feriado seguem a regra de `data_alvo` que
  `criar_rotas_diarias` já usa.

### Fluxo da tarde: `roteirizacao/fluxo_tarde.py`

Um script, chamado pelo timer e pelo botão:

1. `atualizar_prontidao_stokki` (entrega 2).
2. Se existe rascunho pendente pra data alvo: incrementa o rascunho com os
   pedidos **prontos** fora de rota (`incrementar_rascunhos_com_selecionados`
   com os candidatos = pool ∖ rascunho, filtrado por `pronto`).
   Senão, se as rotas do dia já estão na Vuupt: `incrementar_rotas.main()`
   com o mesmo filtro de prontos (flag `--so-prontos`, ligada aqui).
   Senão (nenhum dos dois): gera o rascunho (caso das 18h sem rascunho).
3. Log no formato dos outros agentes; `--modo-teste` passa pra baixo.

- `infra/stokki-fluxo-tarde.timer`: duas linhas `OnCalendar`,
  `Mon..Fri 14:30:00` e `Mon..Fri 15..19:00,30:00` (14h30, 15h, 15h30 … 19h,
  19h30; a das 19h30 recolhe o que ficou pronto até o corte). O `.service`
  é `Type=oneshot` com `OnFailure=stokki-alerta-falha@%n.service`, como as
  outras unidades.
- Botão **"Fluxo da tarde (prontidão + incrementar)"** em
  `painel_agentes/agentes.py`, na barra do planejamento, com fila como os
  outros.
- Corte: `HORA_CORTE_PEDIDO = 19` continua valendo. Pedido que fica pronto
  depois das 19h vai pro rascunho seguinte.

### Testes

- `test_fluxo_tarde.py`: os três ramos (rascunho pendente / rotas na Vuupt
  / nada), filtro de prontos, modo teste.
- Prova em produção: um dia inteiro acompanhado (14h10 → 30/30 → envio →
  pipeline 18h → 22h), conferindo duplicados e o e-mail de alerta.

---

## Deploy e volta atrás

- Entrega 1: deploy com `fonte_pool: vuupt` (nada muda), timer do
  comparador, painel reiniciado. Virada = editar `config.yaml` na VPS.
  Volta = editar de novo.
- Entrega 2: migração de `rascunhos_parada` roda no primeiro `_conectar()`
  do painel após o deploy; fazer backup do dia antes (`backup_dados_gcs.py`)
  e conferir rascunhos pendentes depois. Worker `portal-cliente-envios` e
  painel reiniciados. Volta = `git revert` + restaurar a tabela do backup
  (a migração é aditiva, não perde parada).
- Entrega 3: dois timers novos; retirar a linha do `sequencia_tarde.sh`.
  Volta = `systemctl disable --now` dos timers e devolver a linha.

## Riscos e armadilhas

- **Sessão da Stokki:** o job de prontidão faz login; sem a trava
  cooperativa derrubaria o worker do portal ou a importação (401). Usar
  `StokkiSession` normal, nunca sessão paralela.
- **Pool defasado após mudança direto no site da Vuupt:** até 15 min (ou
  "Atualizar agora"). Aceito pelo Hugo.
- **Serviço com vários códigos** e **`customer.name` ≠ nome da NF**: o
  comparador em sombra mede; corrigir antes da virada.
- **Fuso do agendamento:** ver entrega 1; não "consertar" de passagem.
- **Trabalho paralelo:** `planejamento_rotas.html` e `rascunhos_rota.py` são
  editados por outras sessões (ramo `wms-fase2` tem 28 arquivos modificados
  não commitados). Conferir `git status` antes de cada edição; commitar só
  os próprios arquivos.
- **Pedido do portal que vira DUPLICADO na Stokki** depois de nascer: não
  tratado aqui; o pipeline/sync cuida do lado Vuupt e o pedido some do pool
  quando for cancelado lá.
