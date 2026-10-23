# Transportadoras no banco, tela de cadastro e consulta automática ao embarcador

Data: 23/10/2026. Pedido do Hugo: "transformar essa planilha em uma tabela do
banco", "criar a tela e criar um cadastro automático com notificação para o
cliente perguntando se seria uma retirada ou redespacho, quando redespacho
solicitar o endereço correto para redespacho".

## Por que

O Hugo não tem uma dor específica com a planilha. A meta é **deixar tudo mais
profissional**: o cadastro de transportadoras sai de um xlsx editado à mão na
VPS e passa a ter tabela, tela e um fluxo que se resolve sozinho com o cliente.

Hoje (`regras/transportadoras.py`, `regras/endereco.py`, `pipeline.py`):

- A fonte é `dados/BD_TRANSPORTADORAS.xlsx` (129 linhas na VPS em 23/10), lida
  por `CatalogoTransportadoras.carregar(xlsx)` em ~12 módulos.
- Transportadora **desconhecida** vira entrega comum: o pedido vai pra Vuupt
  com o endereço do destinatário e só fica um aviso no log.
- Exceção: Local de Entrega fora da área sem redespacho conhecido
  (`fonte="aguardando_redespacho"`) segura o pedido e manda e-mail ao
  embarcador (`redespacho_confirmacao.py`); a resposta é lida e lançada na
  planilha à mão.
- Caso real de 23/10: a Mandaê estava cadastrada com a grafia
  "Consultoria Logítica" e sem CNPJ, então o nome vindo da Stokki não casava.

## Objetivos

1. Tabela `transportadoras` no `dados/dados.db` substitui a planilha como fonte
   única. Ninguém mais lê o xlsx.
2. Tela no painel para listar, criar, editar e inativar transportadoras, e para
   responder pendências no lugar do cliente.
3. Transportadora nova chegando num pedido: o cadastro é criado sozinho como
   PENDENTE, o pedido **fica parado** (não vai pra Vuupt) e o embarcador recebe,
   por e-mail **e** WhatsApp, um link pra dizer se é **retirada** no galpão ou
   **redespacho**. No redespacho ele informa o endereço da transportadora.
4. A resposta do cliente **entra direto no cadastro**, sem conferência da
   equipe, e libera todos os pedidos parados com essa transportadora (de
   qualquer embarcador) na rodada seguinte do pipeline.
5. Sem resposta: lembrete diário às 08h ao embarcador e, depois de 2 dias úteis,
   alerta no grupo ALERTAS FRESH para alguém da equipe ligar.

## Fora do objetivo

- Mudar a regra de casamento. Prioridade 0 (nome inequívoco de RETIRADA vence
  CNPJ), prioridade 1 (CNPJ) e prioridade 2 (nome normalizado), conflito e
  `resolver_por_endereco` ficam exatamente como estão.
- O caso "fora da área com transportadora **conhecida** que não é TERCEIROS"
  continua no fluxo atual de `redespacho_confirmacao.py`.
- Pedido **sem** transportadora no bloco da Stokki: continua entrega comum.
- A Central de Cadastros completa (`2026-10-17-cadastros-painel-embarcadores-design.md`,
  Etapa 0 ainda não implementada). Esta tela é a "Etapa 3" dela feita antes;
  quando a Etapa 0 sair, o item muda de grupo no menu.
- Leitura automática de resposta por e-mail.

## Anti-objetivos (seria falha mesmo funcionando)

- Algum pedido que hoje resolve (retirada, redespacho, entrega) passar a
  resolver diferente depois da migração.
- Um embarcador receber vários avisos no mesmo dia pela mesma transportadora
  (ex.: 5 pedidos da mesma transportadora nova = 1 aviso).
- Pedido parado sem ninguém saber: todo pedido segurado aparece na tela e,
  passado o prazo, no grupo de alertas.
- Falha de notificação derrubar o pipeline (regra de `notificar_whatsapp.py`).

## Decisões do Hugo (23/10)

1. Planilha vira tabela do banco.
2. Tela no painel para cadastrar e editar.
3. Transportadora desconhecida gera cadastro automático PENDENTE e pergunta ao
   embarcador: retirada ou redespacho; se redespacho, pede o endereço.
4. Pedido fica **parado até a resposta**.
5. Resposta por **link sem login** (modelo de `/cotacao/aceite/<token>`).
6. Aviso por **e-mail e WhatsApp**.
7. Resposta do cliente **entra direto no cadastro** e libera o pedido.
8. **Envio real aos embarcadores desde o início** (sem prender em hugo@).
9. Lembrete diário 08h; alerta à equipe após 2 dias úteis; equipe pode
   responder pela tela no lugar do cliente.
10. Só para quando o pedido iria para o endereço do destinatário por falta de
    informação (`fonte="destino"`) ou quando o Local de Entrega está fora da
    área. Pedido com Local de Entrega dentro da área ou endereço lido da
    mensagem segue como hoje, mesmo com transportadora desconhecida.
11. Terceira resposta: **"Entregar no endereço do pedido"** (tipo ENTREGA). O
    nome vira entrega comum e nunca mais gera consulta. Isso resolve também os
    nomes que não são transportadora ("Próprio", "A combinar", grafias da
    Fresh Log), sem lista de ignorados.
12. Virada: antes de ligar, as transportadoras desconhecidas dos últimos 60
    dias são classificadas pela tela. A trava vale só para pedido ainda não
    importado na Vuupt; o que já está lá segue, nada é cancelado.

## Desenho

### 1. Tabela `transportadoras`

Uma linha por grafia, como na planilha (várias grafias da mesma empresa
continuam sendo linhas separadas; o casamento por nome normalizado depende
disso).

| coluna | conteúdo |
|---|---|
| id | INTEGER PK |
| nome | grafia original (coluna A) |
| tipo | ENTREGA, RETIRADA, TERCEIROS; NULL enquanto PENDENTE |
| situacao | ATIVA, PENDENTE, INATIVA |
| uf, municipio, bairro, logradouro, numero, complemento, cep | endereço de redespacho (C–I) |
| cidades_atendidas, ufs_atendidas | colunas Q e R da planilha, copiadas como texto |
| cnpj | 14 dígitos, texto (com o mesmo `zfill` de 13 dígitos do loader atual) |
| email | coluna T, mesmo formato (vírgulas, "NÃO ENVIAR") |
| origem | PLANILHA, AUTOMATICO, TELA |
| criado_em, atualizado_em, atualizado_por | auditoria |

Mudanças ficam registradas em `transportadoras_historico` (id da
transportadora, quando, quem, campo, antes, depois), para responder "quem mudou
o endereço da X".

`CREATE TABLE IF NOT EXISTS` no módulo novo (padrão do projeto).

Já existe no `dados.db` uma tabela `transportadoras` antiga (junho/2026,
`nome` PK, `classificacao`, `ignorar`; nenhum `.py` lê ou grava). A migração
renomeia essa tabela para `transportadoras_antiga_jun26` antes de criar a nova,
sem apagar. Conferir na VPS se ela existe lá e se algum serviço fora deste repo
(`/opt/agente-importacao-stokki`, `/opt/confirmacao-motoristas`, coletor do
freshhub) lê esse nome; se ler, parar e avisar o Hugo. *Minha escolha.*

### 2. Importação única

Script `migrar_transportadoras_planilha.py` (idempotente, `--modo-teste` só
mostra): lê o xlsx com o loader atual e grava cada entrada com
`origem=PLANILHA`, `situacao=ATIVA`. Linhas ignoradas pelo loader atual
(tipo inválido, nome vazio) são listadas na saída e não entram.

Prova obrigatória antes de virar a chave: para cada nome+CNPJ distinto visto
nos pedidos dos últimos 60 dias (`vigia`/núcleo na VPS) **e** para cada linha
da planilha, `resolver()` sobre o catálogo do xlsx e sobre o do banco devolve
o mesmo tipo, endereço e e-mail. Zero diferenças.

O xlsx fica no disco como histórico (e no backup GCS, que já inclui o
`dados.db`).

### 3. Leitura

`CatalogoTransportadoras.carregar_do_banco(db_path)` monta as mesmas
`_Entrada` a partir das linhas **ATIVA**. PENDENTE e INATIVA não entram no
catálogo; PENDENTE é consultada à parte (abaixo). Todos os leitores trocam
`carregar(xlsx)` por essa chamada:

`pipeline.py`, `reconciliar_pedidos_retirada.py`, `inventario.py`,
`batimento/medir.py`, `notificacao_transportadoras/notificar_transportadoras.py`,
`roteirizacao/gerar_pdf_romaneios.py`, `portal_cliente/envio_pedidos.py`,
`painel_agentes/planejamento_rotas.py`, `painel_agentes/pedidos_parados_triagem.py`,
`debug/*.py`, e quem mais citar a planilha ou o catálogo numa busca
(`retiradas/regras_retirada.py`, `roteirizacao/regioes_dia_fixo.py`,
`telefone_origem.py`, `batimento/regras.py`,
`notificacao_entregas/montar_email_entrega.py`, `redespacho_confirmacao.py`).
Mensagens de erro e docstrings que citam "BD_TRANSPORTADORAS.xlsx"
passam a citar a tela. `carregar(xlsx)` fica só para o script de migração.

### 4. Cadastro automático e pedido parado

No pipeline, depois de `resolver_endereco`, quando a transportadora **formal**
do pedido (bloco Transportadora da Stokki) veio preenchida, `resolver()` deu
**desconhecida**, o serviço **ainda não existe na Vuupt**, e o endereço final
é `fonte="destino"` ou `fonte="aguardando_redespacho"` (decisão 10):

1. Procura uma PENDENTE com o mesmo CNPJ ou o mesmo nome normalizado. Se não
   houver, cria (`nome` e `cnpj` do bloco Transportadora da Stokki,
   `origem=AUTOMATICO`).
2. Registra o pedido em `transportadora_pedidos_parados` (pedido, transportadora
   id, embarcador cnpj/stkkc_id, desde).
3. Abre ou reusa a consulta daquele **par transportadora + embarcador** e
   notifica (seção 5), no máximo 1 aviso por par por dia.
4. Não importa na Vuupt. Ação nova `aguardando_transportadora`, que aparece no
   resumo da rodada, no vigia e nos Pedidos Parados como os demais "segurados".

Isso vale também quando o Local de Entrega está fora da área e a transportadora
é desconhecida: o fluxo novo substitui o e-mail antigo de redespacho nesse caso.

Quando a PENDENTE vira ATIVA, a rodada seguinte do pipeline resolve o pedido
normalmente: RETIRADA vira serviço avulso de retirada; TERCEIROS vai com o
endereço de redespacho; ENTREGA segue para o endereço do pedido. As linhas de `transportadora_pedidos_parados` daquela
transportadora são fechadas (`liberado_em`).

O mesmo ponto de verificação vale para `--pedido` e `--modo-teste`: em modo
teste cria/consulta nada no banco de produção e só loga o que faria.

### 5. Consulta ao embarcador

Tabela `transportadora_consultas`: transportadora id, embarcador cnpj,
`token` (assinado, como o da cotação), `enviado_em`, `ultimo_lembrete_em`,
`alerta_equipe_em`, `respondido_em`, `respondido_por`, `canal_resposta`
(link, tela).

Destinos:
- E-mail: `interno.email` do embarcador (mesma fonte do redespacho atual),
  respeitando `notificar_email`.
- WhatsApp: `preferencias_notificacao.whatsapp_do_embarcador(conn, cnpj)` via
  função nova `notificar_whatsapp.avisar_cliente_transportadora(...)`, no molde
  de `avisar_cliente_cadastro` (consulta número antes, assinatura
  `transportadora:<id>:<cnpj>:<data>`, texto ≤200 caracteres com o link).

Envio real: chave própria `consulta_transportadora` no `config.yaml` da VPS,
com `ativo: true` e `forcar_destino_email: ""` / `forcar_destino_whatsapp: ""`
(vazio = envio real). Ela vale independente de
`notificacoes_automaticas.ativo` e de `whatsapp_notificacoes.clientes.forcar_destino`,
porque o Hugo liberou esta notificação especificamente. O WhatsApp ainda
depende de `whatsapp_notificacoes.ativo` e do teto diário de clientes.

Conteúdo (e-mail no `envelope_html` padrão): transportadora (nome e CNPJ),
pedidos parados daquele embarcador com essa transportadora (código, destinatário,
cidade), a pergunta e o botão para o link. Explica que os pedidos ficam parados
até a resposta.

### 6. Página do link (portal do cliente, sem login)

Rota `GET/POST /cliente/transportadora/<token>`, no `portal_cliente` (mesmo
`secret` e o mesmo padrão de `validar_token_aceite`):

- Mostra a transportadora e os pedidos parados daquele embarcador.
- Três botões: **"A transportadora vem retirar no galpão"**,
  **"Levar até a transportadora (redespacho)"** e **"Entregar no endereço do
  pedido"** (não é transportadora; grava ENTREGA).
- Redespacho abre o formulário: CEP (preenche rua, bairro, cidade, UF com
  `cotacao.consultar_cep`), número, complemento (obrigatórios: CEP, rua,
  número, cidade, UF) e e-mail da transportadora (opcional). Nome de quem
  responde, obrigatório.
- Ao enviar: grava tipo (RETIRADA, TERCEIROS ou ENTREGA) e endereço, `situacao=ATIVA`,
  `atualizado_por = "<nome> (<embarcador>) pelo link"`, histórico, fecha
  todas as consultas abertas dessa transportadora e mostra a confirmação com
  "os pedidos seguem na próxima rodada".
- Token de consulta já respondida (por outro embarcador ou pela equipe):
  mostra o que foi definido, sem formulário. Token inválido ou vencido
  (validade: 30 dias): mensagem pedindo contato com a Fresh Log.

### 7. Lembrete e alerta

Script `lembrar_consultas_transportadora.py` com timer novo
`consulta-transportadora.timer` às 08h00, dias úteis (`regras/feriados.py`):

- Para cada consulta aberta sem lembrete hoje: reenvia e-mail + WhatsApp
  (texto de lembrete).
- Consulta aberta há 2 dias úteis ou mais e sem `alerta_equipe_em`: uma
  mensagem no grupo ALERTAS FRESH com transportadora, embarcador, quantidade
  de pedidos parados e link da tela; grava `alerta_equipe_em`.
- Embarcador sem e-mail e sem WhatsApp: o alerta à equipe sai já na primeira
  rodada, sem esperar os 2 dias.

### 8. Tela no painel

Rota `/transportadoras` (endpoint `transportadoras`), arquivo de lógica
`painel_agentes/transportadoras_cadastro.py`, template `transportadoras.html`.

- Lista com busca por nome/CNPJ, filtro por tipo e situação; abas
  **Pendentes** (com dias parado, embarcadores consultados e nº de pedidos
  parados) e **Todas**.
- Ficha: editar nome, tipo, endereço (com CEP), CNPJ, e-mail, cidades/UFs
  atendidas; inativar e reativar. Histórico de alterações embaixo.
- Nova transportadora: formulário em branco (`origem=TELA`).
- Em uma PENDENTE, a equipe pode responder no lugar do cliente (mesmos dois
  três botões do link). Isso fecha as consultas abertas e libera os pedidos.
- Validação na gravação: CNPJ com 14 dígitos ou vazio; TERCEIROS exige
  endereço completo; criar ou editar uma linha que deixaria o mesmo CNPJ ou
  nome normalizado com tipos diferentes é recusado com a mensagem do conflito
  (hoje o pipeline só loga conflito depois).
- Badge no menu com o número de PENDENTES (`contadores_menu`).
- Entra em `acesso_telas.TELAS` e ganha guia em `_tour_transportadoras.html`.

### 9. Mapa e documentação

`MAPA_DO_SISTEMA.txt`: seção de `regras/` (fonte agora é a tabela), tabelas
novas, timer novo, tela nova, script de migração; armadilha "planilha
BD_TRANSPORTADORAS.xlsx é histórico, não editar". Docstring de
`redespacho_confirmacao.py` e `regras/transportadoras.py` atualizadas.

## Escolhas do builder (não decididas pelo Hugo)

- **Níveis da tela:** total, operador e logística editam e respondem
  pendências (mesmo conjunto que hoje usa "Transportadora (redespacho)" no
  Planejamento); leitura e atendimento só veem. *Minha escolha.*
- **Lugar no menu:** grupo "Sistema", ao lado de Clientes AGENDA, até a Central
  de Cadastros existir. *Minha escolha.*
- **Validade do link:** 30 dias. *Minha escolha.*
- **Primeira resposta vence:** se dois embarcadores forem consultados sobre a
  mesma transportadora, vale a primeira resposta; a segunda pessoa vê o que já
  foi definido. Mudar depois, só pela tela. *Minha escolha.*
- **Uma linha por grafia** em vez de uma empresa com apelidos, para não mexer
  na regra de casamento. *Minha escolha.*
- Detalhes de layout, nomes de funções e estrutura dos testes ficam com quem
  implementa.

## Virada (ordem do deploy)

1. Deploy com a consulta automática **desligada** (`consulta_transportadora.ativo: false`):
   tabela, migração, leitura pelo banco e tela no ar.
2. Prova de equivalência (seção 2) com zero diferenças.
3. Script lista os nomes+CNPJ desconhecidos vistos nos pedidos dos últimos 60
   dias e os grava como PENDENTE sem consulta; a equipe classifica pela tela.
4. Com a aba Pendentes vazia (ou só com o que o Hugo aceitar deixar), ligar
   `ativo: true`. A partir daí vale a trava, só para pedido não importado.

## Verificação

- Testes unitários: `carregar_do_banco` equivalente a `carregar(xlsx)`; ciclo
  PENDENTE → resposta → ATIVA; dedupe de aviso por par/dia; token
  (válido, respondido, vencido); validação de conflito na tela.
- Prova de equivalência da seção 2 na VPS, antes do deploy, com zero diferenças.
- `pipeline.py --modo-teste` na VPS antes e depois, comparando as ações por
  pedido (fora `aguardando_transportadora`, nada muda).
- Tela e link testados com Playwright em porta de teste (painel 8099, portal em
  porta própria), desktop e celular.
- Primeira consulta real: conferir e-mail, WhatsApp, resposta pelo link e
  liberação do pedido na rodada seguinte.
