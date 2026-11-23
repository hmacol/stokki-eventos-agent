# Motoristas: planilha BD_MOTORISTAS vai pro banco — design

Data: 28/10/2026. Conversa com o Hugo nesta data. Projeto 1 de 3 (ordem
combinada): **1) planilha → banco**, 2) cadastro do motorista pelo link
(`2026-10-28-cadastro-motorista-link-design.md`), 3) inativação mensal +
botão Reativar.

## Por que

`dados/BD_MOTORISTAS.xlsx` é a fonte dos motoristas pra alocação, telas e app.
Ela é versionada no git, mas a VPS escreve nela (tela /motoristas, "Meus dados"
do app): hoje a cópia da VPS tem 40 linhas e está modificada desde 26/10 sem
commit, enquanto a do git tem 30. Edições feitas no notebook e levadas por
`git pull` conflitam ou apagam o que a VPS gravou. Os projetos 2 e 3 vão escrever
nos motoristas automaticamente; o Hugo disse que "o ideal é levar essa planilha
para o banco" e escolheu fazer isso primeiro. Também: hoje nenhuma correção é
feita pela tela (só Excel ou sessão do Claude).

## Objetivos

- Os motoristas passam a morar numa tabela do `dados/dados.db`, que vira a fonte
  única. Todo leitor e escritor da planilha passa a usar a tabela.
- Tela /motoristas ganha **Editar** (todos os campos da linha, inclusive ATIVO)
  além do cadastro que já existe (Hugo, 28/10).
- Comportamento da roteirização/alocação idêntico ao de antes (mesmos motoristas,
  mesmos campos lidos com os mesmos valores).

## Não-objetivos

- Fixo/esporádico, cadastro pelo link, inativação mensal (projetos 2 e 3).
- Mexer na tabela `motoristas` do app (login CPF+PIN) além do necessário: ela
  continua sendo o login; a nova tabela é o cadastro operacional.
- Mudar regras de alocação.

## Anti-objetivos

- Motorista some ou muda de valor na migração (zona, dia, tipo, CPF sem zero à
  esquerda, telefone com `.0`, VEHICLE_ID como float).
- O freshhub (sistema novo, `C:\freshhub`) parar de enxergar os motoristas.
- Janela em que a VPS grava na planilha velha e a tabela não vê (ou o contrário).

## Decisões

- **Fonte da migração = a planilha da VPS** (a do git está defasada). A carga é
  idempotente e confere linha a linha (contagem e valores por coluna) contra o que
  `CatalogoMotoristas` lia antes.
- **Colunas** = as 15 de hoje (AGENT_ID_VUUPT, VEHICLE_ID_VUUPT, NOME_MOTORISTA,
  ACEITA_VIAGENS, DIAS_DISPONIVEIS, MAX_ROTAS_DIA, ATIVO, ZONAS_PREFERIDAS,
  TELEFONE_MOTORISTA, EMAIL_MOTORISTA, PLACA, CPF_MOTORISTA, TIPO_VEICULO,
  APENAS_CARGA_SECA, SEM_APP) com tipos certos (texto pra CPF/telefone, inteiro
  pra ids). AGENT_ID_VUUPT continua sendo a chave de fato (há agente repetido de
  propósito, ex. Erick "CARRO 2": não pôr UNIQUE nele sem tratar isso).
- **Leitura**: `regras/preferencias_motoristas.CatalogoMotoristas` passa a carregar
  da tabela mantendo a mesma interface; os ~15 arquivos que abrem o xlsx direto
  (`grep BD_MOTORISTAS`) passam pela tabela.
- **Espelho xlsx só-leitura** (chamada minha): a cada gravação, regerar
  `dados/BD_MOTORISTAS.xlsx` a partir da tabela, com as mesmas colunas, porque o
  freshhub (`freshhub/adaptadores/legado.py`, `infra/legado/copiar_legado.py`,
  carga de cadastros) e o `backup_dados_gcs.py` leem esse arquivo. Some quando o
  freshhub assumir motoristas (Fase 2 dele). Ninguém escreve no espelho.
- **Sair do git**: o arquivo deixa de ser versionado (tirar a exceção do
  `.gitignore` e `git rm --cached`), senão todo pull na VPS conflita. Isso desliga
  de fato a rotina mensal de agosto que commitava nele (o Hugo autorizou desligar;
  desativar a rotina em claude.ai também).
- **Histórico**: registrar quem mudou o quê (painel: login; app: CPF; scripts:
  nome do script), no padrão de `motoristas_alteracoes` (chamada minha).
- **Editar na tela**: mesmos níveis do cadastro de hoje (`total`, `operador`,
  `logistica` — corrigido em 29/10; antes dizia só "total"),
  `@bloqueia_planejamento_passado` não se aplica; rota nova entra em
  `acesso_telas.TELAS` se for rota nova.

## Relação com a tabela `motoristas` (login do app)

A tabela de login guarda cópia de nome, telefone, e-mail, placa, tipo_veiculo,
vehicle_id, agent_id e ativo. Chamadas minhas, depois da revisão:
- A tabela nova manda. O Editar da tela grava nela e **repassa pro login** (mesmo
  CPF) os campos em comum, senão o extrato do app (lê `tipo_veiculo` do login)
  e o do painel (lê o catálogo) pagam o mesmo motorista por veículos diferentes.
- AGENT_ID_VUUPT é só leitura no Editar (amarra rotas, ofertas, extrato e login).
  CPF só pode ser preenchido quando está vazio (12 linhas sem CPF hoje); trocar um
  CPF existente continua fora da tela.
- "Meus dados" do app (telefone/e-mail/placa por CPF): telefone e e-mail seguem
  valendo pra todas as linhas do CPF, como hoje; **placa só é copiada se o CPF tiver
  uma linha só** (motorista com 2 carros, ex. Erick "CARRO 2", não tem a placa do
  outro carro sobrescrita — hoje tem); com 2+ linhas, fica no login e o histórico
  registra que não foi repassada.
- Linhas sem AGENT_ID entram na tabela (o freshhub carrega por CPF), o catálogo
  continua ignorando como hoje. Valores de texto ("S"/"SIM") preservados no espelho.
- **`ATIVO` = NÃO (Hugo, 29/10):** sai da alocação na hora, mas o login no app
  continua funcionando por **1 mês** (pra ver extrato e pagamentos pendentes);
  depois disso o app recusa o login. Guardar a data em que virou inativo; voltar
  a ATIVO = SIM limpa a data e libera o app de novo. Vale também pra inativação
  mensal do projeto 3. O bloqueio é calculado na hora do login/refresh pela data
  (não precisa de rotina pra desligar), e `motoristas.ativo` deixa de ser
  copiado direto do ATIVO da tabela nova.

## Corte (ordem que evita a janela)

1. No deploy, a carga da planilha pra tabela roda **antes** de reiniciar os
   serviços, e os serviços só sobem já lendo a tabela. Os timers que leem motoristas (rodadas de rota 14h10/16h/18h/22h,
   cancelar 15h45, avisar motoristas) não podem rodar no meio: deploy fora desses
   horários.
2. Conferência: a alocação de um dia passado (replay) dá o mesmo resultado lendo
   da tabela e da planilha; `CatalogoMotoristas` devolve os mesmos 40 com os
   mesmos campos.
3. Só então tirar o arquivo do git. Cuidado: puxar na VPS um commit que deixa de
   versionar o arquivo apaga a cópia de lá (ou o pull aborta, porque ela está
   modificada). Guardar a cópia da VPS antes e conferir que ela continua no lugar
   depois do pull.

## Restrições

- Ver CLAUDE.md: `sudo -u www-data` na VPS, testes de `painel_agentes` de dentro da
  pasta, deploy só depois de commit+push, `MAPA_DO_SISTEMA.txt` atualizado.
- `/opt/confirmacao-motoristas` é cópia manual separada e lê motoristas: conferir
  o que ela lê antes do corte.

## Deixado pro construtor

- Nome e esquema da tabela, onde fica o código de acesso (`nucleo/banco.py` ou
  `regras/`), formato do histórico.
- Visual do Editar (seguir o formulário de cadastro que já existe na tela).
- Como os scripts manuais `sincronizar_placas_motoristas.py`,
  `sincronizar_cpf_motoristas.py`, `vincular_veiculos_motoristas.py` passam a
  gravar.
