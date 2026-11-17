# Motoristas: padrão único de cadastro, pendências na tela e ações (16/11/2026)

## Por quê

Hoje há dois tipos de motorista no sistema. Quem entrou pelo link ou pelo app
(12 pessoas) tem ficha completa: dados pessoais, veículo, PIX e 5 documentos
(CNH, CRLV, selfie, ANTT, comprovante de residência). Os antigos (a maioria
dos 54 da tabela `bd_motoristas`, 44 ativos) têm só a linha operacional; 14
estão sem CPF e 25 sem telefone. O Hugo quer todos no mesmo padrão, ver na
tela quem está devendo o quê, e ter na tela as ações que hoje dependem de
linha de comando ou não existem.

## Objetivos

1. Todo motorista da `bd_motoristas` pode ter a ficha do padrão novo, preenchida
   por ele mesmo num link pessoal.
2. A tela /motoristas mostra, por motorista, o que falta (selo por documento e
   selo de ficha incompleta) e deixa filtrar só os pendentes.
3. O link vai sozinho para o grupo de WhatsApp do motorista ("FRESHLOG + NOME -
   veículo"), começando pelos mais ativos.
4. Botões na tela: Editar (já existe), Inativar/Reativar, Excluir, Resetar PIN.
5. PIN resetado ou criado pela operação é sempre provisório: o app obriga a
   troca no primeiro acesso (mecanismo `motoristas.trocar_pin`, já existente).

## Fora desta entrega

- A equipe subir documento pelo painel no lugar do motorista.
- Envio por timer, sem clique. Lembrete automático para quem não respondeu.
- Inativação mensal automática de quem não roda (projeto 3 antigo).
- Qualquer mudança no app Expo (nenhuma é necessária).
- Senha da Vuupt. "Senha" aqui é o PIN do app próprio.

## O que não pode acontecer

- Mensagem real sair sem o Hugo ligar. Tudo nasce desligado.
- O link de atualização criar ou alterar agente/veículo na Vuupt.
- Alguém com o link de outro trocar o PIX dele sem barreira.
- Excluir motorista que tem rota no histórico (quebra financeiro e consulta).
- Zona, dias, posição (Fixo/Esporádico), tipo de veículo e "só carga seca"
  mudarem pelo link: são decisões da operação e mexem em alocação e tarifa.

## Caminhos considerados

- **A. Link pessoal por motorista (escolhido pelo Hugo).** Endereço único por
  motorista, abre a página de cadastro já preenchida, só atualiza a ficha.
  Serve para todos (iPhone, sem CPF na tabela) e reaproveita página, validação
  e guarda de documentos.
- **B. Link único identificando pelo CPF.** Descartado: 14 sem CPF na tabela e
  qualquer um com o CPF de outro abriria a ficha dele.
- **C. Dentro do app, em "Meus dados".** Descartado: deixa de fora iPhone e
  quem não tem o app, e depende de publicar versão.

## Desenho

### 1. Ficha de atualização (dados)

- Reusa a tabela `motoristas_cadastros` com `origem = 'atualizacao'` e
  `bd_motorista_id` preenchido. O processador do link novo
  (`cadastro_motorista_link._ignorar`) e a fila de aprovação do app
  (`listar_pendentes`) já filtram por origem e não enxergam essas linhas;
  conferir com teste que continuam sem enxergar.
- **Ficha vigente** de um motorista = linha mais recente de
  `motoristas_cadastros` com aquele `bd_motorista_id` e status APROVADO
  (qualquer origem). Atualização enviada vira APROVADO na hora (não há
  aprovação: o motorista já é da casa); rascunho não enviado fica PENDENTE.
- **Vínculo das fichas antigas.** Só o cadastro pelo link grava
  `bd_motorista_id`; a aprovação do app não grava
  (`cadastro_motorista.aprovar`). Migração única e idempotente: para ficha
  APROVADA sem vínculo, preencher `bd_motorista_id` **primeiro pelo CPF da
  ficha**; pelo `agent_id` só quando nenhuma linha tem aquele CPF **e** o
  agente pertence a uma única pessoa (e essa pessoa não tem outro CPF: a ficha
  de Y nunca vai para X). Ficha do app tem só CNH e CRLV e não tem
  nascimento, modelo nem ano: esses motoristas aparecem como pendentes do
  que falta, como qualquer outro.
- **Pessoa x linha.** A tabela tem a mesma pessoa em mais de uma linha (mesmo
  CPF com dois carros; mesmo `AGENT_ID` repetido, caso "CARRO 2"). Ficha,
  link, grupo e pendência são **por pessoa**: linhas com o mesmo CPF formam
  uma pessoa. Linha **sem CPF** entra na pessoa com CPF que tem o mesmo
  `AGENT_ID` quando existe exatamente uma pessoa com CPF naquele agente; com
  duas ou mais (dupla no mesmo agente) a linha sem CPF fica sozinha (não
  adivinhar); sem nenhuma pessoa com CPF no agente, as linhas sem CPF do
  mesmo `AGENT_ID` formam uma pessoa. As duas tabelas novas apontam para a
  linha de menor `id` da pessoa; como essa linha principal pode mudar (linha
  que entra ou sai da pessoa, exclusão), link, estado do link e vínculo de
  grupo são procurados por **qualquer id da pessoa**. Excluir só recusa e
  apaga os arquivos das fichas com o CPF da pessoa removida (pessoa sem CPF:
  fichas cujo CPF não é de outra linha); ficha de outro CPF nunca é tocada.
  A pessoa recebe um link só; os selos
  aparecem iguais em todas as linhas dela; "Rotas 30 d" soma cada `agent_id`
  uma vez. Os repasses (telefone, e-mail, CPF) valem para todas as linhas da
  pessoa; a placa só é repassada quando a pessoa tem uma linha só (mesma
  regra do `_SO_COM_UMA_LINHA` de `regras/cadastro_motoristas.py`).
- **PIX vigente = o do login do app** (`motoristas.chave_pix`): o motorista
  troca em "Meus dados" e isso não passa pela ficha. A ficha vigente é só o
  reserva (sem login). Nessa ordem: pré-preenchimento do link, pendência e
  "Ver ficha" no painel. Assim um "Enviar" sem mexer no PIX nunca grava de
  volta uma chave abandonada.
- Tabela nova `motoristas_links_atualizacao`: `id, bd_motorista_id,
  token_hash, criado_em, criado_por, expira_em, aberto_em, concluido_em,
  cancelado_em, tentativas_cpf, cadastro_id, envio_destino, envio_situacao,
  enviado_em, piloto`. O token só existe em claro no endereço; no banco fica o hash.
  Validade de 15 dias. Gerar outro para o mesmo motorista cancela o anterior.
- Tabela nova `motoristas_grupos_whatsapp`: `bd_motorista_id (único),
  grupo_id, grupo_nome, como (telefone | nome | manual), confirmado_por,
  confirmado_em`. Fica fora da `bd_motoristas` para não mexer no espelho
  `BD_MOTORISTAS.xlsx`, que o freshhub lê.

### 2. Página pública do link

- `GET /motorista/atualizar/<token>` na API do motorista (porta 8073), mesmo
  template `cadastro_motorista.html` em modo "atualizar".
- Primeiro passo: digitar o CPF. Se a tabela já tem CPF, precisa bater; 5 erros
  cancelam o link. Se não tem, o CPF digitado (válido e que não seja de outro
  motorista) passa a ser o dele. Como CPF gravado não muda mais
  (`motoristas_banco.atualizar`), **motorista sem CPF na tabela não entra no
  envio pelo grupo**: a equipe preenche o CPF no Editar antes, ou usa o
  "Copiar link" e manda direto para ele. A linha mostra o selo "falta CPF
  para enviar no grupo".
- **Envio parcial é aceito.** Para enviar bastam os dados pessoais, o veículo,
  o PIX, a CNH, o CRLV e a selfie. ANTT e comprovante de residência podem
  ficar para depois: a ficha é gravada, o selo do documento que falta continua
  na tela e o motorista volta a ser elegível para um link novo. (O link de
  cadastro novo continua exigindo os 5, sem mudança.)
- Depois abre a ficha preenchida com o que já temos (linha da tabela + ficha
  vigente, se houver). Documento já enviado antes aparece como "já recebido",
  com opção de trocar; não precisa mandar de novo.
- Não aparecem: zonas, dias, Fixo/Esporádico. Tipo de veículo, modelo, ano,
  refrigerado e temperatura aparecem e ficam na ficha, mas **não** alteram a
  linha operacional; se o tipo informado for diferente do da tabela, a tela do
  painel mostra o selo "veículo diferente" para a equipe decidir.
- Ao enviar: grava a ficha; repassa para a `bd_motoristas` (com histórico,
  origem `link_atualizacao`) CPF se estava vazio, telefone, e-mail e placa.
- **Troca de PIX espera a equipe (Hugo, 17/11).** O link vai no grupo e só
  pede o CPF, então trocar um PIX que já existia não vale na hora: a chave
  nova fica guardada (`motoristas_links_atualizacao.pix_pendente`), a linha
  ganha o selo "PIX novo aguardando aprovação" e, em "Ver ficha", o nível
  total aprova (grava no login do app e na ficha, com histórico) ou recusa.
  Até lá vale a chave anterior, e o motorista lê isso na tela final. Só o
  primeiro PIX (não havia nenhum) entra direto.
- Respeita o mesmo freio por IP e os mesmos limites de arquivo do link atual.
- Chave própria `atualizacao_motoristas.ativo` (padrão desligado → 503),
  independente de `cadastro_motorista_link.ativo`.

### 3. Pendências na tela /motoristas

Calculadas a cada carga da tela, só para motorista ativo:

- Um selo vermelho por documento faltando: CNH, CRLV, Selfie, ANTT, Residência.
- Selo "Ficha incompleta" se faltar algum de: CPF, telefone, PIX, nascimento,
  placa, modelo e ano do veículo. Passar o mouse lista o que falta.
- Selo cinza do estado do link: "link enviado dd/mm", "abriu, não concluiu",
  "link vencido".
- Selo "veículo diferente" (item 2).
- No topo: contador "N motoristas com pendência" e filtro "só pendentes".
- Botão "Ver ficha" abre os dados e os documentos (mesma rota de documento que
  a ficha do link já usa, com o mesmo nível de acesso).

### 4. Grupos do WhatsApp e envio do link

- `integracao_openwa.py` ganha leitura: `listar_grupos` e `grupo` (com
  participantes). Só GET. Conferido na produção em 16/11:
  `GET /sessions/{sessao}/groups` devolve `id, name` (136 grupos, cerca de 60
  no padrão) e `GET /sessions/{sessao}/groups/{id}` devolve `participants`
  com `id, number, isAdmin`; `number` vem como telefone.
  `/groups/{id}/participants` não existe (404).
- Botão "Identificar grupos" (nível total): lê os grupos cujo nome começa com
  "FRESHLOG +" ou "FRESH +" e sugere um por motorista. Sugestão forte quando o
  telefone do motorista está entre os participantes; sugestão fraca por nome.
  Nada é gravado sem a equipe confirmar (lista com seletor por motorista).
  Motivo: há nomes repetidos (dois Rodrigo, Renato e Renato Forte), grupos em
  dobro (Clayson) e grupos de dupla (Jessé/Weslei, Fabio + Marcos).
- **Mais ativos** = mais rotas em `nucleo_rotas` nos últimos 30 dias, por
  `agent_id`. A tela ganha a coluna "Rotas 30 d".
- Botão "Enviar links de atualização" (nível total): mostra os motoristas
  ativos com pendência, com grupo confirmado e sem link válido em aberto,
  ordenados por rotas; a equipe marca quem vai (padrão: os 10 primeiros) e
  confirma. Para cada um: gera o link e manda a mensagem no grupo dele.
- Mensagem (texto simples): "Olá, {primeiro nome}! Estamos atualizando o
  cadastro dos motoristas da Fresh Log. Preencha pelo link (leva 5 minutos,
  tenha CNH e documento do veículo em mãos): {link}. Vale até {data}."
- Proteções de envio, no padrão do `notificar_whatsapp.py`: intervalo mínimo
  entre mensagens, teto diário próprio `atualizacao_motoristas.teto_diario`
  (padrão 15), uma mensagem por link (assinatura `link_atualizacao:<id>`),
  registro em `notificacoes_whatsapp`.
- Piloto: `atualizacao_motoristas.forcar_destino_whatsapp`. Ausente = não
  envia. Com número = manda para ele com "[teste → grupo X]" na primeira
  linha. Presente e vazio = envio real. Link gerado em piloto nasce com
  `piloto = 1`: não conta como enviado, não gasta o teto e não tira o
  motorista do próximo lote; é cancelado quando o link de verdade for gerado.
- Sem grupo confirmado ou fora do lote: botão "Copiar link" na linha (gera o
  link e copia; a equipe manda por onde quiser). Só nível total (Hugo,
  17/11): quem tem o link e sabe o CPF preenche no lugar do motorista.
- Grupo de dupla: cada motorista recebe a sua mensagem, com o nome dele.

### 4b. Renomear o grupo com o nome completo (pedido do Hugo, 16/11)

- Depois que a equipe confirma o grupo de um motorista, o grupo pode ser
  renomeado para o padrão `FRESHLOG + {NOME COMPLETO} - {veículo}`: troca só o
  trecho do nome; o que vem depois do " - " (veículo/temperatura) fica como
  está no nome atual. O nome completo é o `NOME_MOTORISTA` da tabela.
- Não é automático na confirmação: a lista de grupos mostra "nome atual →
  nome novo" e o botão "Renomear grupos confirmados" (nível total) aplica em
  lote, com a prévia na tela. Renomear aparece para todos no grupo, então
  quem solta é a equipe.
- Gateway: `PUT /sessions/{sessao}/groups/{id}/subject` (existe no OpenWA da
  VPS, `group.controller.ts`). Só funciona se o número conectado for
  administrador ou se o grupo deixar qualquer membro editar; a leitura do
  grupo traz `locked` e `isAdmin`. Com o número conectado conhecido, a prévia
  marca "sem permissão" e pula o grupo trancado em que ele não é admin. Sem
  ele (hoje), o grupo trancado entra na lista como "renomear" com a
  observação "grupo trancado: só funciona se o número conectado for
  administrador"; se não for, o gateway responde 403 e o resultado sai "sem
  permissão".
- Grupo de dupla (dois motoristas confirmados no mesmo grupo) não é
  renomeado. Nome já no padrão é pulado.
- Mesmas proteções do envio: intervalo entre chamadas, chave própria
  `atualizacao_motoristas.renomear_grupos` (ausente = botão desabilitado),
  `--modo-teste`/piloto só mostra a prévia. Guarda o nome antigo em
  `motoristas_grupos_whatsapp.nome_anterior` para poder voltar (o da
  primeira renomeação; uma segunda não sobrescreve).
- **Só renomeia de verdade no modo "real"**: `renomear_grupos` ligado **e** o
  canal do WhatsApp ligado (`gateway_clientes_ok`) **e** envio real
  (`forcar_destino_whatsapp` presente e vazio). Com número de teste =
  "piloto": todos saem com situação "piloto" e nada é chamado no gateway.
  Canal desligado ou chave ausente = "desligado". A prévia devolve
  `"modo"` (desligado | piloto | real) e o botão de aplicar só habilita em
  "real"; a tela mostra "Piloto: a renomeação só mostra a prévia." ou, em
  vermelho, "RENOMEAÇÃO REAL: os grupos mudam de nome para todos os
  participantes."

### 5. Ações na tela

- **Editar:** como está.
- **Inativar / Reativar** (níveis do Editar): atalho do campo Ativo, com
  confirmação. Vale a regra atual: inativo ainda entra no app por 30 dias.
  Não mexe na Vuupt. Inativar cancela link de atualização em aberto.
- **Excluir** (só nível total): recusado se o `agent_id` tem qualquer rota em
  `nucleo_rotas` ou `rascunhos_rota` (e em `rotas`, tabela antiga que existe
  no banco de produção; checar só se a tabela existir) ou baixa em
  `baixas_sem_app` ("tem N rotas: inative"). Oferta, confirmação e
  disponibilidade não barram. Se pode: apaga a linha da `bd_motoristas`
  (deixando registro no histórico), apaga o login do app se o CPF não for de
  outra linha, cancela links e o vínculo de grupo, marca as fichas como
  RECUSADO com motivo "excluído no painel" e apaga os arquivos de documento.
  **Não mexe na Vuupt.** A exceção já existe e continua onde está: cadastro
  vindo do link tem o "Desfazer", que apaga o agente e o veículo que ele
  mesmo criou. Agente de motorista antigo sai da Vuupt à mão.
- **Resetar PIN** (só nível total, Hugo 17/11: o PIN aparece na tela e quem
  vê entra no app como o motorista): sorteia PIN de 6 dígitos, grava com
  `trocar_pin = 1`, zera tentativas e bloqueio, mostra o PIN uma única vez na
  tela para a equipe passar ao motorista. Sessões antigas caem sozinhas (o
  hash muda). Motorista sem login e com CPF: o mesmo botão vira "Criar acesso
  ao app". Sem CPF: botão desabilitado com a dica "falta o CPF".
- Toda ação grava quem fez (login do painel) no histórico.

### 6. Chaves de configuração (todas ausentes = desligado)

```yaml
atualizacao_motoristas:
  ativo: false                  # página pública do link
  # forcar_destino_whatsapp: "5511999999999"   # ver abaixo; deixe comentada (ausente = não envia)
  teto_diario: 15
  validade_dias: 15
  renomear_grupos: false        # libera o botão de renomear grupos
```

`forcar_destino_whatsapp` tem três valores: **ausente** (linha comentada,
como acima) = nada sai, envio e renomeação desligados; **com número** =
piloto (a mensagem vai só para esse número; a renomeação só mostra a
prévia); **presente e vazio (`""`)** = ENVIO REAL nos grupos e renomeação
real. Não copie `""` para "desligar": é o valor que liga o envio real.

## Decisões minhas (o Hugo pode trocar)

1. Validade do link em 15 dias (eram 7 antes do envio por grupo entrar).
2. O link pede o CPF para abrir, porque vai num grupo com mais gente.
3. Tipo de veículo informado pelo motorista não muda a linha operacional; vira
   selo para a equipe decidir.
4. Envio por lote com clique, não por timer. O sistema manda sozinho, mas é a
   equipe que solta cada lote.
5. Teto de 15 mensagens por dia (o número é o do Hugo; risco de banimento).
6. Excluir apaga os arquivos de documento do motorista.
7. Resetar PIN e "Copiar link" só no nível total; troca de PIX pelo link
   espera aprovação do nível total (revisto em 17/11, depois da revisão de
   segurança; antes eram os níveis do Editar e o PIX valia na hora).
8. ANTT e comprovante de residência contam como pendência para todos, igual
   ao cadastro pelo link, mas não impedem o motorista de enviar a ficha.
9. Motorista sem CPF na tabela não recebe o link no grupo (só por "Copiar
   link" ou depois de a equipe preencher o CPF).
10. Tudo é por pessoa, não por linha: quem tem dois carros recebe um link só.
11. Excluir não apaga nada na Vuupt (o rascunho anterior tinha uma caixa
    para isso; saiu porque motorista antigo não tem a marca de "agente criado
    por nós" e apagar agente errado custa licença e histórico).
12. Link gerado em piloto não conta como enviado.
13. Nome novo do grupo mantém o prefixo "FRESHLOG + " e o veículo no fim; só
    o nome do motorista passa a ser o completo. Grupo de dupla não muda.
14. Renomear é em lote com clique e prévia, não no ato de confirmar o grupo.

## Verificação

- Testes `unittest` novos: ficha de atualização (token, CPF, repasses,
  o que não repassa), pendências, casamento de grupos, lote de envio (teto,
  piloto, assinatura), excluir (recusa com rota), resetar PIN.
- Tela conferida no navegador local (Playwright, banco temporário, gateway
  falso), em outra porta.
- Bancada de homologação antes de produção (lá nada sai).
- Produção sobe desligada. Ligar a página e soltar o primeiro lote são do Hugo.

## Fica com quem implementa

Nomes de funções e rotas, layout fino dos selos e do modal, texto exato dos
erros, e onde cada trecho novo mora dentro dos módulos existentes
(`nucleo/cadastro_motorista.py`, `regras/cadastro_motoristas.py`,
`painel_agentes/motoristas.py`, `notificar_whatsapp.py`). Atualizar o
`MAPA_DO_SISTEMA.txt` e registrar rota nova em `acesso_telas.TELAS` se houver.
