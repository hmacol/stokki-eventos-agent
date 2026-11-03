# Cadastro do motorista pelo link (sem aprovação) — design

Data: 28/10/2026. Conversa com o Hugo nesta data.

## Por que

Motoristas novos chegam pelo WhatsApp (indicação ou grupos onde o Hugo divulga
vagas). O cadastro é "a pior coisa do processo": vai-e-volta pedindo dado e foto
de documento, e depois alguém digita tudo na Vuupt (agente criado à mão no site)
e na BD_MOTORISTAS. O Hugo quer mandar um link, o motorista preenche sozinho no
celular, e ao terminar ele já está pronto pra receber rota — sem ninguém
aprovar. A equipe só é avisada.

## Objetivos

- Link público que abre no **navegador do celular** (sem instalar app).
- O motorista preenche tudo (inclusive zonas, dias e tipo de veículo).
- Ao enviar, automaticamente: agente + veículo na Vuupt, linha na
  BD_MOTORISTAS (elegível pra alocação), aviso no grupo ALERTAS FRESH e
  WhatsApp de boas-vindas pro motorista com o login da Vuupt.
- Documentos e selfie guardados e visíveis pela equipe no painel.

## Não-objetivos

- Aprovação/trava antes de operar (Hugo: "pode notificar mas não precisa travar").
- Login no nosso app agora: o motorista começa no **app da Vuupt**; a migração
  pro app próprio é outro momento (os dados ficam guardados pra ela).
- Validação automática de documentos/selfie (a selfie é "bom ter", sem uso definido).
- Inativar motoristas parados há 1 mês: pedido do Hugo, mas é projeto separado
  (próxima conversa).
- Editar motorista já cadastrado pelo link.

## Anti-objetivos (falhou mesmo "funcionando")

- Motorista termina o cadastro e não aparece pro roteirizador (zona/dia vazio,
  linha descartada, agent_id faltando).
- Agente duplicado na Vuupt pro mesmo CPF (reenvio, duplo clique, retentativa).
- Perder o cadastro porque a Vuupt estava fora do ar.

## O formulário (rascunho aprovado em conversa)

Três passos + tela final; pode parar e voltar pelo mesmo aparelho (decisão minha,
ver "Chamadas minhas"). Campos marcados (novo) não existem no auto-cadastro atual.

**Passo 1 — Seus dados:** nome completo, CPF, data de nascimento (novo),
telefone/WhatsApp, chave Pix, emite nota fiscal Sim/Não (novo, logo abaixo do Pix),
dias que roda (padrão seg–sex marcados), regiões que atende (as 9 zonas de
`nucleo/cadastro_motorista.ZONAS`).

Também no passo 1: **posição desejada — Fixo ou Esporádico** (novo; pedido do
Hugo em 28/10). Ver "Fixo x esporádico" abaixo.

**Passo 2 — Veículo:** tipo (`TIPOS_VEICULO`: Fiorino, Van/HR, VUC, 3/4, Truck),
placa, modelo (novo), ano (novo), refrigerado Sim/Não (novo), menor temperatura
de programação em °C (novo; só aparece e só é obrigatória se refrigerado = Sim;
aceita negativo), "o veículo é seu?" (já existe; alimenta `ve_financeiro`).

**Passo 3 — Documentos:** selfie (novo; câmera frontal na hora, sem galeria —
`<input type=file accept=image/* capture=user>`; no navegador isso é um pedido,
não garantia), CNH, CRLV, ANTT (novo), comprovante de residência (novo).
Documentos aceitam foto ou PDF. Todos obrigatórios.

Sem e-mail no formulário (o Hugo não pediu; o e-mail da Vuupt é gerado).

**Tela final:** "Cadastro feito, <primeiro nome>! Você já está liberado pra
receber rotas. Em instantes chega no seu WhatsApp o que fazer a seguir."

## O que acontece no Enviar

1. **Validação.** CPF com dígito verificador. Recusa com "você já é motorista da
   Fresh Log, fale com a gente" (nada é criado) quando: CPF já está na
   BD_MOTORISTAS, tem login no app ou tem cadastro APROVADO. Como só parte da
   planilha tem CPF e a Vuupt tem agentes que já saíram da planilha, também casar
   pelo **telefone** (planilha e `GET /agents`): se bater, não cria agente, fica
   pendente e o aviso do grupo pede pra equipe resolver (chamada minha). Cadastro
   RECUSADO não bloqueia (pode tentar de novo); cadastro PENDENTE do app é
   assumido pelo link (o antigo vira substituído) (chamada minha).
2. **Vuupt — agente.** `POST /users` (já usado em `retiradas/criar_agente_retirada.py`):
   `is_driver=Y`, demais `is_*=N`, nome, telefone se o endpoint aceitar.
   - **E-mail (decisão do Hugo):** primeira letra de cada nome e sobrenome,
     minúsculas, sem acento, ignorando partículas (da, de, do, das, dos, e)
     `@freshhub.com.br`. Ex.: "João Pedro da Silva" → `jps@freshhub.com.br`.
     Colisão (já existe na Vuupt ou em outro cadastro) → `jps2@`, `jps3@`...
     É só login: o domínio não precisa receber e-mail.
   - **Senha (decisão do Hugo, ciente de que é adivinhável):** os 6 primeiros
     dígitos do CPF. Se a Vuupt recusar por tamanho, os 8 primeiros (Hugo, 28/10).
3. **Vuupt — veículo.** Criar o veículo com a placa (ou reaproveitar se a placa já
   existir em `GET /vehicles`) e ligar ao agente (`PUT /users/{id}
   {"vehicle_id": ...}`, provado em `vincular_veiculos_motoristas.py`).
4. **BD_MOTORISTAS.** Via `regras.cadastro_motoristas.cadastrar_motorista`: ATIVO
   SIM, AGENT_ID e VEHICLE_ID da Vuupt, dias, zonas, tipo de veículo, MAX 1 rota/dia,
   ACEITA_VIAGENS NÃO (o formulário não pergunta), telefone, placa, CPF (como texto,
   com zero à esquerda), e-mail gerado, posição (fixo/esporádico). Conferido pelo
   Hugo ("tá ok"). **Refrigerado = Não → APENAS_CARGA_SECA = SIM** (Hugo, 28/10,
   ciente de que embarcador sem tipo de carga cadastrado não conta como seco e
   esse motorista vai pegar poucas rotas). Login no nosso app não é criado: os
   campos que só existem lá (ve_financeiro, Pix) ficam em `motoristas_cadastros`
   pra migração (chamada minha).
5. **Aviso interno** no grupo ALERTAS FRESH (`notificar_whatsapp`, origem
   `cadastro_motorista`): nome, veículo (tipo, refrigerado), regiões e link pra
   ficha na tela /motoristas. Se algum passo da Vuupt ficou pendente, o aviso diz.
6. **WhatsApp pro motorista** (destino individual, como o dos clientes):
   boas-vindas, link pra baixar o app da Vuupt, e-mail e senha.
7. **Vuupt fora do ar / erro:** o cadastro já está salvo no banco antes de
   qualquer chamada externa; o que falhou é retentado sozinho (ex.: no próximo
   ciclo de um timer existente ou um novo) e cada passo é idempotente (guardar
   agent_id/vehicle_id assim que obtidos; não recriar). Os passos 4–6 só rodam
   depois de ter agent_id.

## Fixo x esporádico

O motorista escolhe a posição no formulário. **Fixos têm preferência na seleção
de motoristas** (Hugo). Entendimento (a confirmar no plano): na alocação
(`roteirizacao/alocacao_motoristas.py`, hoje equitativa), o esporádico só recebe
rota quando nenhum fixo elegível (zona, dia, porte, travas) estiver livre; entre
os do mesmo grupo segue a regra equitativa de hoje. Os motoristas atuais: o Hugo
vai passar a lista de quem é fixo (pendente); até lá não mudar a alocação.

## Painel

Na tela /motoristas, a equipe vê os cadastros feitos pelo link (ficha com todos
os campos, selfie e documentos, situação de cada passo automático). O botão
"aprovar" de hoje deixa de ser necessário pra esse fluxo.

## Restrições do projeto

- Base existente: `nucleo/cadastro_motorista.py` (tabela `motoristas_cadastros`,
  pasta `dados/cadastros_motoristas/<id>/`, chave_envio, freio por IP em
  `nucleo/api_motorista.py`). Estender, não duplicar. A tela do app
  (`app_motorista/app/cadastro.tsx`) e o fluxo de aprovação continuam existindo.
- Página web servida pela `motorista-api` (público, já atrás do Caddy em
  `app.freshhub.com.br/motorista`); seguir `url_for`/prefixo (convenção das telas).
- **Fonte da planilha (Hugo, 28/10):** a partir de agora a BD_MOTORISTAS só muda
  na VPS (tela /motoristas, link, sessão lá); acabam as edições no notebook levadas
  por git, e a rotina mensal de agosto (sincroniza formulário do Google no dia 1)
  é desligada. O ideal, segundo o Hugo, é levar a planilha pro banco — ver ordem
  dos projetos. Enquanto for xlsx: reescrever com `dtype` CPF como str e
  VEHICLE_ID Int64 (armadilhas conhecidas).
- Envio real a motorista é desligado por padrão no projeto: o WhatsApp de
  boas-vindas nasce atrás de chave própria com `forcar_destino` = número do Hugo;
  esvaziar é decisão dele. Teto diário próprio.
- Agente novo pode contar como licença paga na Vuupt e o link não tem trava:
  **risco aceito pelo Hugo** ("vamos trabalhar com esse risco"). O freio por IP/dia
  que já existe continua valendo.
- Documentos são dados pessoais: ficam em `dados/` (fechado pra outros usuários
  da VPS), servidos só pelo painel com login.
- `--modo-teste`/teste: nunca criar agente real sem querer; testar a criação com
  um agente descartável e inativá-lo/apagá-lo em seguida.

## Chamadas minhas (o Hugo pode revisar)

- 3 passos com progresso salvo no próprio aparelho.
- Documentos aceitam PDF além de foto; selfie só câmera.
- Partículas ignoradas no e-mail; sufixo numérico em colisão.
- Não pedir e-mail; ACEITA_VIAGENS = NÃO; MAX 1 rota/dia (Hugo confirmou o caminho).
- WhatsApp do motorista começa em piloto (convenção do projeto).

## Deixado pro construtor

- Campos exatos de `POST /users` e de criação de veículo na Vuupt (confirmar na doc
  ou com um agente teste); onde guardar os campos novos (colunas na
  `motoristas_cadastros`).
- Mecanismo de retentativa (timer novo ou carona num existente).
- Visual da página (seguir o rascunho; marca Fresh Log, simples, rápida em 4G).
- Texto exato das mensagens (curtas, linguagem simples; ≤200 caracteres no grupo).
