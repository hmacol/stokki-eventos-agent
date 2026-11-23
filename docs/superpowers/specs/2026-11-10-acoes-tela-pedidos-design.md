# Ações na tela /pedidos — design

Data: 10/11/2026. Pedido do Hugo: "quero permitir que a gente crie reenvios, cancelamentos, alterações
de endereços e agendamentos nessa página" + "incluir também canhoto/comprovante de entrega".

## Por quê

A tela /pedidos (deployada 09/11, `painel_agentes/painel_agentes.py` rota `pedidos_tabela`, template
`pedidos_tabela.html`, dados de `nucleo/tabela_pedidos.py`) mostra todos os pedidos, mas para agir
sobre um deles a equipe precisa ir à Torre (reenvio, baixa com canhoto) ou ao Planejamento
(cancelar, endereço, agendar), e lá só acha pedido do dia/pool. A /pedidos passa a ser o lugar onde
se acha o pedido e já se resolve.

## Objetivos

1. Na tabela, cada linha ganha um botão **Ações** com: Reenvio, **Cancelar Vuupt**, **Cancelar
   Stokki**, Alterar endereço, Agendar/reagendar, Canhoto (Ver/baixar · Dar baixa com canhoto ·
   Trocar canhoto), **Nota fiscal** (Imprimir DANFE · Baixar DANFE · Baixar XML).
2. Caixinhas de seleção por linha + barra de seleção com ações **em lote**: Reenvio, Cancelar Vuupt,
   Cancelar Stokki, Agendar, Alterar endereço, Dar baixa com canhoto, **Imprimir DANFEs** (um PDF só
   com todos, na ordem da tabela) e **Baixar NF/XML** (ZIP). (Ver e Trocar canhoto são só por linha.)
   [escolha minha: formato do lote]
3. Quem pode agir: `total`, `operador`, `logistica`, `atendimento`. `leitura` continua vendo a tela
   sem os botões (nem caixinhas); `galpao` segue sem acesso à tela. Exceção: **Nota fiscal** só lê,
   então `leitura` também imprime/baixa DANFE e XML. [escolha minha]

## Não-objetivos

- Não mexer nas telas do Planejamento e da Torre nem nas rotas `/api/planejamento/*` e `/api/torre/*`
  (níveis e `@bloqueia_planejamento_passado` delas ficam como estão).
- Nenhum botão cancela nos dois lugares de uma vez: Vuupt e Stokki são ações separadas (decisão do
  Hugo, 10/11).
- Não soltar o estoque que fica no Virtual quando a Stokki cancela pedido já separado (isso segue
  com a equipe / skill ajustar-estoque-stokki).
- Alterar endereço e agendar não tocam a Stokki (igual ao Planejamento).
- Não há envio a cliente/motorista nas ações (nenhum e-mail ou WhatsApp novo).

## Anti-objetivos (seria fracasso mesmo funcionando)

- Dois serviços vivos na Vuupt para o mesmo pedido (reenvio de pedido ainda aberto).
- Uma segunda implementação das regras que já existem (geocodificação do endereço, fuso do
  agendamento, regra de lote da foto, trava de duplicação). As ações **chamam as funções que já
  rodam**; a /pedidos só monta a chamada.
- Lote que para no primeiro erro ou que devolve 500 cru pro `fetch()`.

## Decisões do Hugo (10/11)

| Tema | Decisão |
|---|---|
| Canhoto | as três: ver/baixar, dar baixa com canhoto, trocar canhoto de pedido já entregue |
| Reenvio | qualquer status… |
| Reenvio de pedido aberto (pool ou rota) | …menos aberto: **bloquear**. Só entregue, insucesso ou cancelado |
| Onde | menu na linha + seleção em lote |
| Acesso | total, operador, logistica **e atendimento** |
| Trocar canhoto | **trazer o ramo `correcao-canhoto`** (correção fica do nosso lado; cliente vê no portal e no link do e-mail; Vuupt e Stokki não mudam) |
| Cancelar | dois botões separados: **Cancelar Vuupt** e **Cancelar Stokki** (pedido de saída) |
| Cancelar Vuupt com motorista a caminho/no local | **bloquear** (como o portal): "motorista a caminho/no local, fale com ele antes" |
| Reenvio de pedido cancelado na Stokki | **bloquear**: consulta a Stokki antes de criar |
| Quem cancela na Stokki | total, operador, logistica, atendimento (mesmos das outras ações) |
| Nota fiscal | imprimir e baixar DANFE e XML, por linha e em lote |

## Visão: uma linha por pedido (Hugo, 10/11)

"A visão da nossa tela tem que ser parecida com a do portal de clientes, ou seja, não repetir
diversas vezes por pedido e sim 1 pedido e a atualização mais correta com um dropdown."

Hoje a /pedidos tem uma linha por linha de `nucleo_pedidos`, ou seja, por serviço da Vuupt: o
original e cada reentrega (`PS-1`, `PS-1-R1`, `PS-1-R2`) aparecem separados. Em produção, nos
últimos 30 dias: 2.582 linhas, 263 delas reentregas. Rota e motorista já saem só da última parada.

Regra (a mesma do portal, `portal_cliente/dados_cliente.unir_tentativas`, 05/11):
- **Grupo** = embarcador (`sender_id`) + códigos base (`codigos_base_lista`: tira o `#` e o `-R<n>`;
  código combinado `PS-1, PS-2` vira a tupla dos dois). A coluna Código mostra o código base.
- **Linha principal** = a tentativa "mais correta": **entregue vence sempre** (tentativa depois da
  entrega é serviço duplicado); senão, a mais recente por (data, peso), com o mesmo peso do portal
  (programado 0 < insucesso 1 < aguardando saída 2 < em rota 3 < agendado 4 < entregue 5).
  Todas as colunas (status, rota, motorista, agendamento, canhoto, `service_id`…) vêm dessa
  tentativa.
- **Selo "N tentativas ▾"** ao lado do status quando há mais de uma. Ele abre uma linha embaixo com
  a lista em ordem de data. Cada item mostra: data, código do serviço (`PS-1-R1`), situação,
  motivo do insucesso, rota, motorista, hora da conclusão e link do canhoto quando houver. Entram as
  tentativas que terminaram (entregue/insucesso) e a atual. Parada cancelada por remanejo de rota
  não conta (o PS-35067 tem 7 dessas). O que fica aberto continua aberto depois de filtrar ou paginar.
- **Diferença interna** [escolha minha]: o portal esconde "não coletado" (5744) do cliente; aqui ele
  aparece no histórico como tentativa. Continua sem decidir a linha principal, igual ao portal.

Efeitos no resto da tela [escolhas minhas]:
- **Total, paginação e caixas** contam pedidos (grupos), não serviços. Caixas = as da linha
  principal.
- **Filtros** valem sobre a linha principal (status "Insucesso" traz os pedidos cuja última situação
  é insucesso). A **busca livre** casa com qualquer código do grupo: buscar `PS-1-R1` acha o pedido
  `PS-1`.
- **Período**: pela data da linha principal.
- **CSV**: uma linha por pedido, com uma coluna nova "Tentativas" no formato do xlsx do portal
  ("03/11 Insucesso (Ausente); 04/11 Entregue").
- **Ações** agem sobre o serviço da linha principal, como no portal. Reenvio e cancelar já olham o
  grupo todo (regra do "serviço vivo com o mesmo código base").

Implementação: agrupar no SQL de `nucleo/tabela_pedidos.py`, com o código base calculado no
`_SQL_BASE` e `ROW_NUMBER() OVER (PARTITION BY sender_id, base ORDER BY <regra>)`, pra paginação e
ordenação continuarem no banco. O histórico do grupo é uma segunda consulta só dos grupos da página.
Testes iguais aos do portal (`portal_cliente/test_tentativas.py`): entregue vence, mais recente
vence, busca por `-R1` acha o grupo, empresas diferentes com o mesmo código não se misturam.

Isto vira a **Fase 0**: muda a tela que já está no ar e é a base das ações, que agem sobre a linha
principal.

## Como cada ação funciona (o que já existe e deve ser reusado)

Todas recebem o `vuupt_service_id` da linha (coluna `service_id` de `tabela_pedidos.COLUNAS`). Linha
sem `vuupt_service_id`: ações desabilitadas com dica "pedido sem serviço na Vuupt".

| Ação | Função existente | Observação |
|---|---|---|
| Reenvio | `torre_controle.duplicar_pedido_manual(service_id, codigo, motorista, rota)` → `insucesso_entrega/expedir_pedidos.duplicar_servico_por_insucesso` | cria `#PS-N-R{k}` com `recreated_order_origin_id`; trava `fingerprint_duplicacao_insucesso` (mesmo serviço não duplica 2x) |
| Cancelar Vuupt | `roteirizacao/cancelar_servico.cancelar_servico_completo(token, service_id)` (a do portal) | PUT `/cancel`; acha o rascunho sozinho e tira a parada (também de rota enviada); recusa motorista a caminho/no local |
| Cancelar Stokki | `stokki/cancelar.cancelar_pedido(sessao, id_stokki, motivo)` com a trava `stokki.sessao_uso` e `StokkiSession(config, conta=...)`, igual a `portal_cliente/solicitacoes_execucao.py` (~l.400–432); `id_stokki` por `portal_cliente/cancelamento.id_stokki_do_codigo(codigo)` | ver "Cancelar Stokki" abaixo |
| Endereço | `planejamento_rotas.editar_endereco_pedido` / `editar_endereco_pedidos` (lote, mesmo endereço) | geocodifica e grava serviço + contato + cópia do rascunho |
| Agendar | `planejamento_rotas.reagendar_pedido` / `reagendar_pedidos` (lote, mesma janela) + `checar_reagendamento_dia_fixo` antes de confirmar | mesmo aviso de dia fixo do Planejamento |
| Ver/baixar canhoto | rota existente `canhoto_foto(comprovante_id)` (a mesma da /consulta/pedido) | se houver mais de um comprovante, lista todos; linha com `canhoto = Não` mostra "sem canhoto" |
| Dar baixa com canhoto | `torre_controle.marcar_entregue_manual(...)` + `/api/torre/validar-lote-foto` para lote | mesmo modal da Torre: foto do canhoto + nota de retorno opcional, ou justificativa da lista fixa; regras do lote (mesmo embarcador e mesmo destinatário/local) valem igual |
| Trocar canhoto | `nucleo/correcao_comprovante.py` do ramo `correcao-canhoto` (`registrar_correcao` por `parada_id`) | ver "Trocar canhoto" abaixo |

### Regras por status (checadas no servidor, não só escondidas na tela)

O status do núcleo (`nucleo_pedidos.status`) pode estar atrasado em relação à Vuupt. A decisão de
permitir vale pelo **status do serviço na Vuupt lido na hora da ação** (as funções já buscam o
serviço; onde não buscam, buscar antes). A tela usa o status do núcleo só para habilitar/desabilitar
os itens do menu.

| Ação | Permitido quando o serviço na Vuupt está |
|---|---|
| Reenvio | `done` (entregue) ou `failed` (insucesso) ou `canceled`. Recusa `not_assigned`/em rota com "pedido ainda aberto: já existe um serviço vivo". |
| Cancelar Vuupt | pool, rascunho ou rota enviada com o motorista ainda sem sair para o pedido. Recusa `done`, `canceled` e motorista a caminho/no local (`on_route`/`arrived`) — regra de `roteirizacao/cancelar_servico.cancelar_servico_completo`, que é a função a usar |
| Reenvio (Stokki) | além da Vuupt: recusa se o pedido base estiver **Cancelado na Stokki** (`stokki/cancelar.situacao_e_token`, conta admin pela trava). Stokki ocupada = erro "não deu pra conferir a Stokki, tente de novo" (não cria sem conferir) |
| Cancelar Stokki | não depende da Vuupt; vale a situação na Stokki lida na hora (a função já recusa expedido e trata "já cancelado" como ok) |
| Endereço / Agendar | qualquer um que não seja `done` nem `canceled` |
| Dar baixa com canhoto | como hoje na Torre (recusa `not_assigned`, `canceled`, `failed`; `done` = ok sem mexer) |
| Trocar canhoto | parada `ENTREGUE`, `PARCIAL` ou `INSUCESSO` (regras do ramo) |

**Reenvio de entregue/cancelado** é novidade: hoje a função só é chamada para insucesso. Ela copia
endereço/contato/remetente e não depende do motivo, então serve; o título fica com o sufixo
"(reentrega Rk)" como hoje. O reenvio pede um **motivo curto obrigatório** (texto, 5–200 caracteres)
gravado na tratativa `REENVIO_MANUAL` (texto "Reenvio pela tela Pedidos → PS-N-Rk: <motivo>").
[escolha minha]

**Busca pelo serviço certo:** `duplicar_pedido_manual` hoje busca o original por `code`. Como
reentrega reaproveita o código base, a /pedidos deve garantir que o serviço duplicado é o da linha
(`service_id`), não outro com código parecido. [escolha minha: buscar por id]

### Cancelar Stokki

- Login concorrente na Stokki derruba a outra sessão: sempre `sessao_uso.adquirir(dono, ...,
  alternativa=True)` antes e `liberar(dono)` no `finally`. Stokki ocupada além da espera = erro
  claro ("Stokki ocupada por <dono>, tente em alguns minutos"), sem tentar.
- Motivo obrigatório, **mínimo 15 caracteres** (regra da Stokki). Hoje `montar_motivo` prefixa
  "Portal Fresh Hub:", o que faria parecer que o cliente cancelou; a /pedidos deve gravar
  "Fresh Hub Pedidos (<usuario>): <motivo>" (aceitar prefixo como parâmetro sem mudar o padrão do
  portal). [escolha minha]
- A chamada pode levar até ~30 s (poll de 15×2 s) mais a espera da trava. Lote = um pedido por vez
  com progresso na tela; o servidor não pode estourar o timeout do waitress/Caddy num pedido só.
- Avisos no modal antes de confirmar:
  - "Se o pedido já foi separado, o estoque fica preso no Virtual até a equipe fazer o Desbloqueio."
  - Linha de reentrega (`PS-N-R1`): o pedido da Stokki é o mesmo do original (`PS-N`); cancelar
    cancela o pedido inteiro na Stokki.
  - Se o serviço na Vuupt ainda está vivo: "A Vuupt continua com o pedido; use Cancelar Vuupt se
    for o caso." (só aviso, não bloqueia)
- Registro: tratativa com origem `PEDIDOS` (CANCELADO_STOKKI) com usuário, motivo e situação
  antes/depois.
- Testes com a sessão Stokki mockada; prova real só com pedido do cliente teste e com o Hugo
  indicando qual (cancelar na Stokki não se desfaz).

### Nota fiscal (DANFE e XML) — pedido do Hugo 10/11

O que já existe:
- **XML**: o timer noturno `coletar-xml-nfe` (`documentos_pedido/coletar_xml_nfe.py`, 23:30–03:40)
  grava `dados/xml_stokki/PS-<id>.xml` e o estado em `xml_stokki_coleta` (OK / PLACEHOLDER / sem XML /
  erro). Só pega o que a coleta já visitou; pedido do dia ainda não tem.
- **DANFE**: não guardamos. Quem gera é a Stokki: `POST /pt-br/document/danfe` (`_token` + `file_url`
  = caminho do XML do `btn_danfe`), hoje feito pelo Playwright da conta provider em
  `documentos_pedido/stokki_documentos._gerar_danfe_por_file_url` / `gerar_danfes`.
- **Placeholder**: chave começando com 99… ou valor R$ 0,00 não é NF (`stokki_documentos.xml_placeholder`).
  Nunca entregar placeholder como nota.

Comportamento:
1. **Baixar XML**: serve o arquivo de `dados/xml_stokki/PS-<base>.xml` se existir e não for
   placeholder. Se não existir, busca na Stokki na hora (sob a trava `sessao_uso`), grava na mesma
   pasta e atualiza `xml_stokki_coleta` como OK (a coleta noturna não repete). Nome do download:
   `PS-<id>_NF<numero>.xml`.
2. **DANFE (baixar e imprimir)**: pede à Stokki o PDF a partir do XML, guarda em
   `dados/danfe_stokki/PS-<base>[_n].pdf` (mesmo esquema de pasta do XML) e serve o arquivo; da
   segunda vez em diante serve do disco. **Imprimir** = abre o PDF numa aba nova e chama a janela de
   impressão do navegador; **Baixar** = `Content-Disposition: attachment`. [escolha minha: DANFE da
   própria Stokki, sem gerar DANFE nosso, para ficar igual ao que o cliente e o galpão veem]
3. Pedido com **mais de uma NF** (a Stokki lista vários `btn_danfe`): devolve todas — DANFEs juntadas
   num PDF, XMLs num ZIP.
4. Reentrega (`PS-N-R1`) usa a nota do pedido base `PS-N`.
5. Sem NF (embarcador sem nota, `EMBARCADORES_SEM_NF`, pedido sem XML ou só placeholder): item do menu
   desabilitado com o motivo; no lote, o resumo lista quem ficou sem nota.
6. Lote: Imprimir = um PDF só com as DANFEs na ordem da tabela; Baixar = ZIP com DANFE + XML de cada
   pedido. Teto de 50 pedidos por lote (cada busca na Stokki leva segundos). [escolha minha]

**Conta da Stokki (achado da revisão):** a conta **provider** é das rotinas do dia (expedição,
Estação de Impressão); a coleta noturna só a usa 23:00–03:40 e o login provider pode esperar até 2 h
na fila (`stokki_documentos._credenciais_provider`, `sessao_uso.vez_da_provider_para_login`). A
/pedidos **nunca usa a conta provider**. Usa a conta admin pela trava normal
(`sessao_uso.adquirir(..., alternativa=True)`, principal ou reserva) com `StokkiSession` (requests),
esperando no máximo ~60 s; ocupada = erro claro.

Fontes, nesta ordem, sem tocar a Stokki quando der:
1. XML do portal (envio que entrou pelo portal: `portal_cliente/envio_pedidos.caminho_xml(envio)`;
   a coleta noturna pula esses pedidos, `com_portal`).
2. `dados/xml_stokki/` da coleta noturna.
3. Stokki ao vivo pela conta admin: o `data-url` do `btn_danfe` (listagem/show do admin, como
   `stokki/trocar_nf.caminho_nf_do_pedido`) + `trocar_nf.baixar_xml_por_caminho`. O cache guarda
   **todas** as NFs do pedido (`PS-<id>.xml`, `PS-<id>_2.xml`…), não só a primeira (hoje
   `baixar_xml_nfe` guarda uma só).

DANFE — **spike primeiro, só leitura, com o cliente teste ou um pedido que o Hugo indicar**:
- (a) `POST /pt-br/document/danfe` pela `StokkiSession` admin (sem Playwright). Se der PDF, é o
  caminho.
- (b) Se a Stokki recusar fora da área provider: gerar o DANFE nós mesmos a partir do XML com uma
  biblioteca Python de DANFE (dependência nova no venv da VPS; avisar o Hugo antes de instalar).
- Em nenhum caso usar Playwright nem conta provider de dia.

**Lote sem estourar timeout:** o navegador pede um pedido por vez (`POST /pedidos/<codigo>/nf/preparar`,
que só garante XML e DANFE no disco e responde rápido quando já há cache), com barra de progresso;
no fim, `GET /pedidos/nf/lote.pdf?codigos=...` / `lote.zip` só lê do disco e junta (pypdf, já no
projeto) ou zipa. Nenhuma requisição faz mais de um pedido na Stokki.

Rotas: `GET /pedidos/<codigo>/nf.pdf`, `GET /pedidos/<codigo>/nf.xml`, `POST
/pedidos/<codigo>/nf/preparar`, `GET /pedidos/nf/lote.pdf|lote.zip`. Níveis
total/operador/logistica/atendimento/leitura. Código validado pelo padrão `PS-\d+(-[RC]\d+)?`
antes de virar nome de arquivo; nunca montar caminho com texto do usuário sem essa validação.
Entrar em `acesso_telas` junto com a /pedidos.

### Achados da revisão de construção (10/11) — o que muda em relação às tabelas acima

- **Reenvio**: `torre_controle.duplicar_pedido_manual` chama o `expedir_pedidos.py` da RAIZ (não a
  cópia de `insucesso_entrega/`) e está amarrado ao insucesso (origem `TORRE`, texto "pela Torre",
  marca `insucesso:<codigo>` como tratado). Parametrizar (origem, texto, e só marcar a exceção de
  insucesso quando o serviço for `failed`) em vez de copiar. Ele também cria o próximo `-R{k}` mesmo
  com um `-R` anterior ainda aberto: a regra "bloquear aberto" vale para **qualquer serviço vivo com
  o mesmo código base** (original ou qualquer `-R`/`-C`), consultado na Vuupt na hora.
- **Ver/baixar canhoto**: `canhoto_foto` só cobre rotas do nosso app (`nucleo_comprovantes`). Mostrar
  tudo o que existir, nesta ordem: checklist da Vuupt (`/checklists/{id}/print`, como
  `notificacao_entregas/vuupt_entregas.py` e o download com cache do portal), canhoto manual
  (`nucleo/canhotos_manuais`), comprovantes do app (`canhoto_foto`). "Sem canhoto" = nenhuma das três.
  [escolha minha]
- **Acessos**: `acesso_telas.py` (entrada da /pedidos é `^/pedidos(/|$)`) precisa cobrir
  `/api/pedidos/...` e `/pedidos/<codigo>/nf...`; senão quem tem lista personalizada leva 403.
- **Lote de foto**: `/api/torre/validar-lote-foto` não aceita `atendimento`; a /pedidos ganha a sua
  rota que chama a mesma validação.
- **Rascunho**: `cancelar_pedido` precisa do rascunho também quando a rota JÁ foi enviada (tira a
  parada da rota real antes). `roteirizacao/cancelar_servico.cancelar_servico_completo` já descobre o
  rascunho sozinho (`rascunho_do_servico`) e lê o status vivo; usar ele (é o que o portal usa).
- **Lalamove**: cancelar na Vuupt não cancela na Lalamove; serviço com Lalamove mostra aviso.
- **Quem fez**: `tratativas.registrar_evento` não tem coluna de usuário; o usuário vai no texto
  ("<usuario>: ...") de toda tratativa da /pedidos.
- **Linha sem `vuupt_service_id`**: Cancelar Stokki e Nota fiscal continuam habilitados (não
  dependem da Vuupt).
- **Serviço com vários pedidos** (`PS-1, PS-2`): Cancelar Stokki lista todos os PS na confirmação e
  cancela cada um. Nota fiscal traz as notas de todos. [escolha minha]

### Rascunho

`cancelar_pedido` e `editar_endereco_pedido` recebem `rascunho_id` para limpar a cópia local do
rascunho não enviado. A /pedidos não conhece rascunho: o servidor descobre o rascunho aberto que
contém aquele `service_id` (o que o Planejamento sabe pela tela) e passa adiante. Sem rascunho,
`None`.

## Trocar canhoto (ramo `correcao-canhoto`)

O ramo local `correcao-canhoto` (commit 7fe446c + merge c39f3c8, parado desde 17/10, 412 commits
atrás da master) tem a feature completa: correção pela ficha do pedido (/consulta/pedido), fila
"A resolver" em /canhotos, portal e link do e-mail mostram a versão corrigida. Spec:
`docs/superpowers/specs/2026-10-05-correcao-canhoto-painel-design.md`.

Trabalho:
1. Atualizar o ramo com a master (merge, sem rebase), rodar os 62 testes dele + suítes do painel e do
   portal.
2. Toca `portal_cliente/` → **revisão de segurança antes do deploy** (skill deploy-vps, item 4).
3. Na /pedidos, "Trocar canhoto" abre o mesmo formulário de correção do ramo para a **última parada**
   do pedido (`parada_id` — hoje o `_SQL_BASE` de `tabela_pedidos` não expõe o id da parada; passar
   a expor).
4. Níveis: o ramo usa `_NIVEIS_CORRIGE_CANHOTO` = total/operador/atendimento. Na /pedidos vale a
   decisão de 10/11 (inclui `logistica`); alinhar o ramo para total/operador/logistica/atendimento.
   [escolha minha, alinhando com a decisão de acesso]
5. Pendências já anotadas do ramo seguem com o Hugo e não bloqueiam: PARCIAL exige 3 fotos;
   `qtd_checklists` nulo fora da fila; Lalamove/terceiros podem encher a fila.

## Rotas novas

Prefixo `/api/pedidos/acao/...`, todas `POST`, `@requer_auth(niveis=("total","operador","logistica",
"atendimento"))` + `@exige_mesma_origem`, resposta sempre JSON (try/except devolvendo
`{"erro": ...}` em vez de 500 HTML). Lote = o front manda um POST por pedido (padrão do "Marcar
entregue" em massa do Planejamento) com barra de progresso, ou usa as funções de lote que já existem
(`reagendar_pedidos`, `editar_endereco_pedidos`); em qualquer caso, falha de um não para os outros e
o resumo final lista as falhas por código. Rota nova de tela não há (a /pedidos já está em
`acesso_telas.TELAS`).

Toda ação registra quem fez (`session["usuario"]`) onde a função existente já registra (tratativas,
`torre_entregas_manuais`, eventos do núcleo). Ações que hoje não registram autor (cancelar,
endereço, agendar) passam a gravar uma tratativa com origem `PEDIDOS` (CANCELADO_MANUAL,
ENDERECO_ALTERADO, REAGENDADO) com usuário e valor novo. [escolha minha: hoje esses três não deixam
rastro de quem fez; a /pedidos abre para mais gente (atendimento)]

## Tela

- Coluna fixa à esquerda com caixinha + botão "Ações" (⋯) por linha; só para níveis que agem.
- Itens do menu desabilitados (com dica do motivo) conforme status do núcleo; o servidor re-checa.
- Modais reaproveitam o visual e, quando der, o JS dos modais do Planejamento/Torre (agendar com dia
  fixo, endereço, marcar entregue com foto reduzida no navegador ≤2000 px).
- Confirmação explícita em Cancelar e Reenvio (mostra código e destinatário; lote mostra a contagem).
- Depois da ação a linha mostra um selo do resultado ("Cancelado agora", "Reenvio → PS-N-R1",
  "Agendado 12/11 08:00–12:00"). O status da tabela só muda na próxima sincronização do núcleo com a
  Vuupt — o selo deixa isso claro. [escolha minha: não forçar ressincronização]
- Filtros, página e ordenação atuais são mantidos após a ação (sem recarregar a página toda).

## Restrições

- Envio a cliente segue desligado: nenhuma ação manda mensagem.
- Testes: unittest (sem pytest); testes do painel rodam de dentro de `painel_agentes/`, com banco
  temporário isolando `banco.DB_PATH`, `acesso_telas.DB_PATH` e `contadores_menu` (como
  `test_pedidos_tabela.py`). Vuupt sempre mockada nos testes.
- Prova real só com o cliente teste (CNPJ 00.000.000/0001-91, sender 22812827), como nas provas do
  checklist de 30/10.
- `MAPA_DO_SISTEMA.txt` atualizado no mesmo commit.

## Ordem sugerida de entrega

0. **Fase 0:** uma linha por pedido com o selo de tentativas (muda a tela que já está no ar).
1. **Fase 1:** ações na linha e em lote para Reenvio, Cancelar Vuupt, Cancelar Stokki, Endereço,
   Agendar, Ver/baixar e Dar baixa com canhoto (só painel-agentes; sem portal).
2. **Fase 2:** Nota fiscal — spike do DANFE pela conta admin, depois XML/DANFE por linha e em lote.
3. **Fase 3:** trazer o ramo `correcao-canhoto` (merge + revisão de segurança + deploy de
   painel-agentes e portal-cliente) e ligar "Trocar canhoto" na /pedidos.

As fases não dependem umas das outras. Item de fase ainda não entregue aparece desabilitado
("em breve"). [escolha minha]

## Fica para quem constrói

- Layout exato do menu e da barra de lote, textos de dica e de selo.
- Se o lote de reenvio/cancelar usa um POST por pedido ou uma rota de lote própria.
- Como descobrir o rascunho aberto de um `service_id`.
- Nome das tratativas novas, desde que fique quem, quando, o quê e o valor novo.
