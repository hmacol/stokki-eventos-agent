# Importação de pedidos no fluxo Cross: item XD + troca de NF na Stokki

Data: 24/10/2026. Decisões do Hugo nesta data, em conversa. Nada implementado ainda.

## 1. Intenção

Hoje só o Empório Quatro Estrelas tem o "truque do XD": a regra `emporio_quatro_estrelas`
em `agente_importacao_stokki/etapa2_processamento_xml/processar_xml.py` troca todos os
itens da NF por um item único `XDQUATROESTRELAS`, quantidade 1, e o pedido entra na
Stokki assim. O pedido fica com a NF de um item só: DANFE, XML pra transportadora e
romaneio saem sem os produtos reais.

Queremos generalizar isso pra qualquer embarcador que opere em **Cross** e completar o
que falta: depois de criar o pedido, enviar o XML original (todos os produtos) pela
função "Informar Nota Fiscal" da Stokki (o "Trocar NF"), de modo que o documento fiscal
do pedido seja o verdadeiro e os itens continuem XD.

## 2. Vocabulário (definições do Hugo)

Três fluxos operacionais. Um embarcador pode ter mais de um.

| Fluxo | Operação no galpão | Arquivo de importação |
|---|---|---|
| **Armazenagem** ("Full") | Trabalha com estoque; separação por SKU a partir do que está armazenado | Importação normal (XML como vem, com a regra de ajuste do embarcador quando houver) |
| **Full cross** | Mercadoria chega na quantidade exata dos pedidos e é separada por pedido no galpão; não fica estoque. Só erra se o próprio embarcador errar | Importação normal, **sem** troca de produto (igual à Armazenagem no arquivo) |
| **Cross** | Os pedidos já chegam separados pelo embarcador; o galpão só confere e expede | Importação **com troca de produto pra XD** e, em seguida, **troca da NF** pelo XML original |

Quando um embarcador tem mais de um fluxo, **quem informa o fluxo de cada pedido é o
embarcador**, no envio. O sistema nunca adivinha.

## 3. Objetivos, não objetivos, anti-objetivos

**Objetivos**
- Qualquer embarcador marcado como Cross tem seus XMLs convertidos em item XD e o
  pedido criado na Stokki, pelos dois caminhos que existem hoje (portal do cliente e
  importador por e-mail).
- Logo depois da criação, o XML original entra no pedido pela "Informar Nota Fiscal".
  O sistema confere que deu certo. Se não deu, fica registrado como pendente, tenta de
  novo nas rodadas seguintes e avisa a equipe.
- O fluxo (ou fluxos) de cada embarcador mora no cadastro de embarcadores (tabela
  `interno`), junto com o código do produto XD dele.
- Embarcador com mais de um fluxo escolhe o fluxo pedido a pedido no portal.

**Não objetivos**
- Não usamos o módulo "Cross Docking" da Stokki (`/administrator/crossdocking/*`). O
  pedido Cross continua sendo um pedido de saída comum (Sale), como o Quatro Estrelas
  faz hoje.
- Não criamos o produto XD nem mexemos em estoque na Stokki. O produto XD continua
  sendo cadastrado à mão pela equipe na entrada do cliente, quando o fluxo dele é
  entendido (decisão do Hugo: "vamos permanecer dessa forma").
- Não mudamos as regras de ajuste de XML existentes (`muai`, `maiz`): elas continuam
  valendo pra Armazenagem e Full cross.
- Não mudamos a tela de Embarcadores do painel. Ela só existe no ramo
  `cadastros-painel` (sem merge); quando entrar, o campo de fluxo entra nela.

**Anti-objetivos (o que seria fracasso mesmo funcionando)**
- Criar pedido Cross com os produtos reais porque o fluxo foi deduzido errado. Por isso
  embarcador com dois fluxos é obrigado a informar, sem padrão escondido.
- Converter pra um código XD que não existe no catálogo e deixar a Stokki recusar em
  silêncio. Validar antes e parar com erro claro.
- Pedido criado e NF não trocada sem ninguém saber. A pendência tem que aparecer na
  tela e no relatório.
- Derrubar a sessão da Stokki de outro processo. Toda chamada passa pela trava
  `stokki/sessao_uso.py`.

## 4. Restrições do ambiente

- Stokki não tem API. Criação de pedido é o wizard por Playwright
  (`etapa3_importacao_stokki/importar_stokki.py`); o restante pode ser HTTP com a
  `StokkiSession` (cookies + CSRF), como `stokki/cancelar.py` faz.
- Login concorrente derruba a outra sessão. Trava obrigatória
  (`stokki/sessao_uso.py`), já usada pelo portal e pelo importador por e-mail.
- Dois repositórios: `c:\agente_stokki_eventos` (portal, módulo `stokki/`, cadastro)
  e `c:\agente_importacao_stokki` (importador por e-mail: regras de XML + wizard). O
  portal já importa `processar_xml` e `importar_stokki` do segundo via
  `portal_cliente.caminho_importador`; o importador já importa `stokki.sessao_uso` do
  primeiro (best-effort). Deploy do segundo é por `scp`, não `git pull`.
- Scripts em lote precisam de `--modo-teste` / `--simular` que não tocam a Stokki.
  E-mails a clientes respeitam `forcar_destino`.
- Interpretador local `py -3.11`; na VPS `venv/bin/python` como `www-data`.

## 5. Decisões já tomadas (Hugo, 24/10/2026)

1. Converter os produtos do XML num item só, código XD do embarcador. (Generalização
   da regra do Quatro Estrelas.)
2. **Quantidade do item XD = quantidade de volumes da nota** (soma de
   `<transp><vol><qVol>`). **Se vier em branco ou zero, 1.** Isso muda o Quatro
   Estrelas, que hoje usa 1 fixo: ele passa a sair com os volumes.
3. Segundo passo é o **"Trocar NF"** da Stokki: Pedidos de Saída, botão Opções da
   linha, "Informar Nota Fiscal", janela de importação. Os itens do pedido
   **permanecem os do XML convertido**; só o documento fiscal muda.
4. O fluxo fica no **cadastro de embarcadores**, podendo ser mais de um por
   embarcador. Os três fluxos são Armazenagem, Full cross e Cross (seção 2).
5. Com mais de um fluxo, **o embarcador informa** o fluxo de cada pedido.
6. Produto XD e estoque continuam manuais, cadastrados na entrada do cliente.
7. (25/10) Todos os embarcadores marcados como Cross **já têm o XD cadastrado** na
   Stokki. O sistema não guarda o código à mão: **descobre no catálogo do embarcador o
   produto cujo código começa com `XD`** (ex.: `XDDOURADO`). **Se não existir, informa a
   equipe de atendimento e o cliente.**
8. (25/10) Pedidos Cross que já existem na Stokki na virada e os duplicados que o
   importador reenvia toda rodada **também recebem a troca de NF**, de forma
   idempotente (6.7).
9. Saldo do XD continua manual, sem checagem automática nesta versão (*minha escolha,
   não contestada: entra na lista do piloto como item operacional*).

Marcações feitas pelo Hugo em 24/10 (artifact "Fluxo por embarcador",
https://claude.ai/artifact/BWYxiXrDfVhiD37z3SSbER, coleção `fluxos`, um documento por
CNPJ; ler com ArtifactData antes de implementar, porque ele pode completar a lista):

| Fluxo | Embarcadores (stkkc) |
|---|---|
| Só Cross | Quatro Estrelas (98), Laticínios Dourado (18), Facilita (10), Jatobá / Luciano Gambarini (15), Jersey Vale (79), Dansken (85), De Tommaso / CIAO (87), Morota (77) |
| Armazenagem + Cross | Fruta Fina (94), Fazenda Burin (12) |
| Full cross (escrito em "Outro") | Muai (99), Maíz Food (93), Vidaveg (80), Khappy (97) |
| Só Armazenagem | os outros 24 ativos marcados, mais Gazzino (CNPJ sem zero à esquerda, duplicata do Magazzino 86) |
| Sem marcação | pré-cadastros inativos e cancelados |

Cliente Teste (00000000000191) ficou com tudo marcado; tratar como embarcador de teste
com os três fluxos, útil pra provar a tela.

## 6. Desenho

### 6.1 Cadastro de embarcadores (`interno`)

Uma coluna nova em `interno` (repo principal):

- `fluxos TEXT`: valores separados por vírgula, do conjunto `armazenagem`,
  `full_cross`, `cross`. Vazio ou NULL = `armazenagem` (comportamento de hoje). *Minha
  escolha: texto com vírgula em vez de tabela nova, porque são três valores fixos e o
  painel lê a linha inteira do embarcador.*

O **código XD não é cadastrado**: é descoberto no catálogo sincronizado da Stokki
(`wms_produtos`, filtrando `embarcador_id` = stkkc do embarcador, `ativo`, `sku`
começando com `XD`). Regra de escolha (*minha*): se houver exatamente um, é ele; se
houver mais de um, vale o que casa com `XD` + apelido sem espaços e acentos (ex.
`XDQUATROESTRELAS`), e sem casamento é erro de configuração (equipe decide). Se não
houver nenhum, ou o catálogo do embarcador estiver vazio, é "XD não encontrado" (6.2).

Carga inicial: um script único (`scripts/` ou `gerenciar_clientes.py`) lê a tabela da
seção 5 (ou os documentos do artifact) e grava `fluxos` nos 39 marcados; os dez com
Cross entram como Cross de uma vez, porque o XD de todos já existe (decisão 7). O
script imprime, por embarcador Cross, o XD que encontrou no catálogo, pra conferência
antes da virada. Até a tela de Embarcadores do ramo `cadastros-painel` entrar, a edição
é por esse mesmo comando de linha, com `--cnpj` e `--fluxos`.

Uma função de leitura única, no repo principal (sugestão: `nucleo/fluxo_embarcador.py`
ou dentro de `portal_cliente/envio_pedidos.py`): `fluxos_do_embarcador(conn, cnpj ou
stkkc) -> {"fluxos": [...], "codigo_xd": str|None, "erro_xd": str|None}` (aceita o
stkkc porque o config do importador por e-mail tem `client_id`, não CNPJ). Portal e
importador por e-mail leem daqui; ninguém mais guarda cópia do fluxo.

### 6.2 Regra de XML `cross` (importador, `processar_xml.py`)

Nova transformação parametrizada: `transformar_xml_cross(origem, destino, codigo_xd,
nome_item)`:

- Mantém só o primeiro `<det>`, `nItem="1"`, remove os demais (como a regra do Quatro
  Estrelas).
- `cProd = codigo_xd`; `xProd = nome_item` (*minha escolha: `"Mercadoria <apelido do
  embarcador>"`, no padrão do que já existe*).
- `qCom = qTrib = soma de todos os <vol><qVol>` do XML, inteiro, formatado como
  `"N.0000"`; se não houver `<vol>`, `qVol` vazio, não numérico ou soma zero, usa 1.
- Tudo o mais fica intacto: chave, emitente, destinatário, `<transp>`, valores,
  impostos, `uCom`/`uTrib` do primeiro item (*minha escolha: não mexer na unidade, igual
  à regra atual*).

A regra `emporio_quatro_estrelas` passa a ser `transformar_xml_cross` com
`XDQUATROESTRELAS` / "Mercadoria Quatro Estrelas" (a quantidade muda de 1 pra
volumes, decisão 2). As regras `muai` e `maiz` ficam como estão.

`TRANSFORMACOES` hoje mapeia slug do cliente -> função sem parâmetros. O builder decide
como passar `codigo_xd`/`nome_item` (ex.: `TRANSFORMACOES["cross"]` aceitando kwargs e
`transformar_xml(regra, origem, destino, px, **params)` no portal). O que não pode
acontecer: regra `cross` sem `codigo_xd` cair silenciosamente em outra regra; deve
levantar erro.

**Validação antes de converter ("XD não encontrado"):** a conversão só acontece com
um XD resolvido pela regra de 6.1. Sem XD, nada é enviado à Stokki e **a equipe de
atendimento e o cliente são informados** (decisão 7):

- No portal, isso aparece já na prévia (`/analisar`): o item Cross é recusado com a
  mensagem "Produto XD não encontrado no cadastro do embarcador na Stokki. A Fresh Log
  foi avisada." e o servidor manda um e-mail ao atendimento com embarcador e NF. No
  worker, a mesma checagem é rede de segurança (o XD pode ter sido desativado entre o
  envio e o processamento): linha ERRO e o caminho de aviso que já existe
  (`_avisar_erros`: cliente com atendimento em cópia).
- No importador por e-mail, o XML fica pendente (não vai pra Stokki), o relatório da
  rodada marca ❌ "XD não encontrado" e um e-mail vai ao cliente (e-mail do cadastro)
  com cópia pro atendimento, pelo caminho que respeita `forcar_destino`.

### 6.3 Portal do cliente: fluxo por pedido

- `/api/envios/analisar` passa a devolver `fluxos_disponiveis` do embarcador (via
  6.1) e, por item, `fluxo` pré-preenchido quando há um só. Se Cross está entre os
  fluxos e o XD não foi encontrado, Cross sai da lista com o aviso de 6.2 (e o item é
  recusado quando Cross era a única opção).
- Na prévia (`_envios.html`), item de embarcador com mais de um fluxo mostra uma escolha
  obrigatória por pedido (Armazenagem / Full cross / Cross, só os que ele tem). Com um
  fluxo só, nada aparece. Um controle "aplicar a todos" ajuda em lotes grandes
  (*minha escolha*).
- `confirmar_envios` exige `fluxo` válido por item quando há mais de um disponível,
  preenche sozinho quando há um, e grava em `portal_envios.fluxo`. Pedido de planilha
  (sem NF-e) só aceita `armazenagem`/`full_cross`: Cross exige XML.
- Reenvio de erro (`reenviar-erros`) reaproveita o `fluxo` gravado, sem perguntar.
- A tela da equipe (`/equipe`) usa a mesma prévia e as mesmas regras.

### 6.4 Worker do portal (`enviar_stokki.py`)

Por envio, na preparação do arquivo:
- `fluxo == cross` -> `transformar_xml_cross` com `codigo_xd`/`nome_item` resolvidos
  por `fluxos_do_embarcador` (6.1), depois da validação de 6.2. A `regra_xml` do
  embarcador não se aplica nesse caso.
- `armazenagem` ou `full_cross` -> comportamento de hoje (`regra_xml`).

Depois do wizard, pra cada envio Cross criado:
1. Localiza o pedido recém-criado na Stokki pelo número da NF e pelo cliente
   (`stokki.pedidos.listar_pedidos(status="Open", cliente=stkkc, busca=numero_nf)`,
   que já devolve o id interno e o código PS; se não achar em Open, procura em todos os
   status). Grava `codigo_pedido` e o id interno.
2. Chama a troca de NF (6.5) com o XML **original** (`xml_path`, que já é o original).
3. Confere o resultado (6.5). Sucesso -> `troca_nf_status = FEITA`. Falha ->
   `troca_nf_status = PENDENTE` com `troca_nf_erro`; o ciclo seguinte do worker
   (loop de 20 s) tenta de novo os PENDENTES, até 3 tentativas espaçadas (*minha
   escolha: 3 tentativas, intervalo mínimo de 10 min entre elas*), depois `ERRO` e aviso
   à equipe (e-mail de atendimento, pelo caminho que respeita `forcar_destino`), nunca ao
   cliente.

Colunas novas em `portal_envios`: `fluxo TEXT`, `id_stokki INTEGER`,
`troca_nf_status TEXT` (NULL, `PENDENTE`, `FEITA`, `ERRO`), `troca_nf_em TEXT`,
`troca_nf_erro TEXT`, `troca_nf_tentativas INTEGER`. O status do envio continua
`CRIADO` quando o pedido existe na Stokki; a troca é acompanhada à parte, e a lista do
portal mostra um selo "NF a trocar" / "NF trocada" (*minha escolha: não inventar um
status novo de envio, pra não mexer no que o cliente já conhece*).

`--simular` não toca a Stokki em nenhum dos passos.

### 6.5 Troca de NF na Stokki (módulo novo `stokki/trocar_nf.py`)

Mecanismo sondado em 24/10 na listagem `/pt-br/administrator/inventory/outbound`
(só leitura, HTML salvo em `/tmp/sonda_trocar_nf/outbound_listing.html` na VPS):

- Item do menu Opções: "Informar Nota Fiscal" (classe `modal_action`,
  `data-target="#modal_action_invoice"`, `data-id=<id interno do pedido>`). Aparece em
  Aberto, Aguardando Transportador e Em separação.
- Formulário `#form_invoice_standardized`, POST multipart em
  `/pt-br/administrator/inventory/outbound/nfe/store` com os campos: `_token`,
  `order_id_nfe=<id interno>`, `action=invoice`, `page=index`, `nfe=<chave de acesso,
  44 dígitos>` e `file_nfe=<arquivo .xml>`. O JS da tela preenche `nfe` lendo a chave
  do próprio XML.
- Sucesso: HTTP 200, a tela mostra "Nota fiscal atualizada com sucesso!". Erro: JSON
  com `errors` (lista) ou `message`.

`trocar_nf(sessao, id_stokki, caminho_xml) -> {"ok", "erro", "itens_antes",
"itens_depois"}`, por HTTP com a `StokkiSession`, no padrão de `stokki/cancelar.py`:
usa o `_token` do formulário na página da listagem (ou o CSRF que a `StokkiSession` já
guarda do login; é o mesmo token da sessão), extrai a chave do XML (`Id="NFe..."`),
faz o POST, e **confere**. A conferência parte da própria listagem, área admin, mesma
sessão: a linha do pedido traz o caminho do arquivo da NF-e em
`btn_danfe data-url="users/clients/inventories/outbounds/invoices/<arquivo>.xml"`
(é o que a tela manda pra `/document/danfe`). Depois da troca esse caminho muda; o
módulo relê a linha, baixa o XML pelo caminho novo (`/pt-br/xml/nfe/<base64 do
caminho>`, o formato que `documentos_pedido/stokki_documentos.py` decodifica) e conta os
`<det>`: a troca só é FEITA se o XML do pedido tem a mesma chave e o mesmo número de
itens do original. Chamador segura a trava `sessao_uso`.

**Sonda pendente (vai pro piloto):** a leitura em produção que confirmaria o download
do XML por `/pt-br/xml/nfe/<base64>` na sessão admin foi negada em 25/10. Por isso
`stokki/trocar_nf.py` decide em tempo de execução: se o XML baixa, confere chave e a
lista de códigos dos itens (`cProd`), o que também evita confundir o XML convertido de
um item só com o original de um item só; se não baixa, confere que o `data-url` da
linha mudou depois do POST. O resultado diz qual conferência foi usada. O `_token` vem
do formulário da listagem, com o CSRF guardado no login como reserva. Não precisa de
segunda conta nem de Playwright em nenhum dos casos.

Risco a provar na primeira rodada real: a Stokki aceitar a troca quando a chave do XML
novo é a **mesma** do XML que já está no pedido (é o caso aqui: o convertido mantém a
chave). O Hugo faz isso à mão hoje, então a expectativa é que aceite. Se recusar, o
desenho muda (voltar ao Hugo).

### 6.6 Importador por e-mail (`agente_importacao_stokki/main.py`)

O único cliente Cross por e-mail hoje é o Quatro Estrelas. O importador:
- Resolve `fluxos`/`codigo_xd` do cliente pela função do repo principal (6.1), pelo
  mesmo caminho que já usa pra `stokki.sessao_uso`. Cliente Cross sem repo principal
  acessível = erro de configuração, pipeline para antes da etapa 2 (*minha escolha: não
  cair na regra antiga em silêncio*); XD não encontrado segue 6.2 (XML pendente, aviso
  a cliente e atendimento). Cliente com mais de um fluxo
  **não pode** ser importado por e-mail (não tem onde escolher): erro de configuração
  também.
- Etapa 2 usa `transformar_xml_cross` pra cliente Cross; guarda o original em
  `xmls_extraidos/` (já guarda).
- Depois da etapa 3, pra cada XML criado: localiza o pedido e troca a NF (6.4/6.5),
  ainda com a trava. O relatório/`STATUS.md` ganha a linha "NFs trocadas / pendentes".
- **Pendência não fica no importador.** O timer dele roda só 16:20 a 19:50, seg a sex:
  uma troca que falhasse às 19:50 só voltaria no dia seguinte, e a expedição da manhã
  sairia com a NF de item único (o anti-objetivo). Por isso a pendência vai pra uma
  tabela no `dados.db` do repo principal, `trocas_nf_pendentes` (`id_stokki`,
  `codigo_pedido`, `chave_nfe`, `cnpj_embarcador`, `caminho_xml_original`, `origem`
  = portal | email, `tentativas`, `erro`, `criado_em`, `ultima_tentativa_em`,
  `concluido_em`). Quem retoma é o worker do portal (`portal-cliente-envios`, loop de
  20 s, já com trava), com as mesmas 3 tentativas espaçadas e o mesmo aviso à equipe de
  6.4. O importador só grava a linha; `portal_envios.troca_nf_*` passa a ser um espelho
  dessa tabela pros envios do portal (ou o builder faz os dois lerem só dela). *Minha
  escolha, a partir da revisão do builder.*
- `--modo-teste` não troca nada.

### 6.7 Pedidos já existentes e duplicados (decisão 8)

A troca de NF é **idempotente**: antes de postar, o módulo baixa o XML atual do pedido
(6.5) e compara com o original. Se já tem a mesma chave e o mesmo número de `<det>`,
está FEITA e nada é enviado. Isso permite aplicar a troca em três situações além do
pedido recém-criado:

- **Virada:** script único (`--modo-teste` primeiro) lista, pra cada embarcador Cross,
  os pedidos ainda não expedidos na Stokki (Aberto, Aguardando Transportador, Em
  separação) e, pra cada um que tenha XML original disponível (`portal_envios.xml_path`
  ou `xmls_extraidos/` do importador, casado pela chave), enfileira em
  `trocas_nf_pendentes`. O worker faz o resto. Pedido sem original disponível entra no
  relatório pra troca manual.
- **Duplicado no portal:** envio Cross que a Stokki recusa como "chave já utilizada"
  localiza o pedido existente pela NF e enfileira a troca (que não faz nada se já foi
  feita).
- **Duplicado no importador por e-mail:** idem, grava a linha em `trocas_nf_pendentes`.
  Como o importador reenvia os XMLs dos últimos 4 e-mails toda rodada, a maioria das
  linhas vai terminar em "já estava FEITA" com um download por pedido; é o custo
  aceito pra garantir a cobertura.

### 6.8 O que acontece depois da troca

Com o XML original no pedido, o link `/xml/nfe/` passa a servir a NF completa. DANFE
(`documentos_pedido`), XML enviado a transportadoras (`baixar_xml_nfe`) e romaneio
passam a refletir os produtos reais sem alteração de código: a chave é real (não cai no
tratamento de placeholder `99...`). Volumes e peso continuam vindo do `<transp>`, igual
antes e depois.

## 7. Prova e colocação em produção

1. Testes unitários: `transformar_xml_cross` (soma de volumes, fallback 1, vários
   `<vol>`, XML sem `<transp>`), leitura/escrita de `fluxos`, exigência de `fluxo` em
   `confirmar_envios`, `trocar_nf` com sessão falsa (sucesso, erro JSON, conferência de
   itens). `py -3.11 -m unittest` nos módulos de cada repo, sem misturar `portal_cliente`
   com `painel_agentes`.
2. Portal local em porta alternativa (`--simular`) com o Cliente Teste (três fluxos)
   pra ver a escolha por pedido e os selos.
3. Antes do piloto, a equipe confere saldo do XD dos dez embarcadores Cross na Stokki
   (decisão 9) e o script de carga mostra o XD encontrado pra cada um.
4. Piloto real, com o Hugo acompanhando: **um** XML do Quatro Estrelas pela `/equipe`,
   conferir na Stokki o item XD com quantidade = volumes e, depois da troca, a DANFE com
   os produtos reais. Só então rodar a virada (6.7) e trocar a regra do importador por
   e-mail.
5. Deploy: repo principal por `git pull` + restart de `portal-cliente` e
   `portal-cliente-envios`; importador por `scp` dos arquivos alterados.

## 8. Fica pro builder

- Primeiro passo do plano: a sonda só leitura de 6.5 (download do XML por HTTP e
  `_token` na página admin), com o Hugo autorizando a leitura em produção.
- Forma exata de passar parâmetros pela `TRANSFORMACOES` e de o portal chamar a regra
  parametrizada.
- Onde fica a função de leitura do cadastro (6.1) e o nome do comando de carga/edição.
- Regex e seletores pra achar `_token` e o link `/xml/nfe/` na página do pedido por
  HTTP (há exemplos em `stokki/cancelar.py` e `documentos_pedido/stokki_documentos.py`).
- Texto dos selos e do e-mail de pendência. Layout da escolha de fluxo na prévia,
  seguindo o estilo da tela atual.
- Ordem das migrações de banco (`_garantir_colunas` já existe em `envio_pedidos.py`).

## 9. Fora deste desenho

- Tela de Embarcadores no painel (ramo `cadastros-painel`): quando entrar, ganha os
  campos `fluxos` e `codigo_xd`.
- Qualquer mudança nas regras de Full cross e Armazenagem.
- Módulo Cross Docking da Stokki.
