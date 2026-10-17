# Cadastro do motorista pelo app: meus dados + auto-cadastro com aprovação

Pedido do Hugo (08/10/2026): "Quero que os motoristas tenham acesso aos seus
cadastros para mudar telefone, PIX, e-mail, placa do carro. Que eles mesmos
possam se cadastrar também, com aprovação de um administrador (eu mesmo)."

Decisões do Hugo (08/10):
- Mudança de telefone/e-mail/PIX/placa **vale na hora**; ele recebe aviso no
  WhatsApp com o que mudou; PIX e placa ficam no histórico.
- Auto-cadastro exige, além de nome/CPF/telefone/e-mail/PIX/placa/tipo de
  veículo: **foto da CNH, foto do CRLV, zonas e dias que atende**.
- Aprovação no painel **escolhendo o agente Vuupt** (criado à mão antes, como
  na tela /motoristas de hoje). Aprovar = linha na BD_MOTORISTAS + login no
  app com PIN provisório.
- Avisos no **WhatsApp ALERTAS FRESH** (grupo padrão de notificar_whatsapp).

## Fontes de verdade (sem mudar)
- `dados/BD_MOTORISTAS.xlsx` continua mandando na roteirização (zonas, dias,
  veículo, placa, AGENT_ID). O app só espelha telefone/e-mail/placa nela.
- `motoristas` (SQLite) é o login do app; ganha `chave_pix` e `placa`.
- Agente na Vuupt continua manual.

## Dados
- `motoristas` += `chave_pix TEXT`, `placa TEXT` (migração aditiva).
- `motoristas_alteracoes` (histórico): id, cpf, campo, valor_antigo,
  valor_novo, alterado_em.
- `motoristas_cadastros` (auto-cadastro): id, cpf, nome, telefone, email,
  chave_pix, placa, tipo_veiculo, zonas (csv), dias (csv), aceita_viagens,
  chave_envio (segredo pra subir os documentos), cnh_arquivo, crlv_arquivo,
  status PENDENTE|APROVADO|RECUSADO, motivo, agent_id, criado_em,
  revisado_em, revisado_por, avisado_em.
- Documentos em `dados/cadastros_motoristas/<id>/cnh.jpg|crlv.jpg`.

## API (nucleo/api_motorista.py)
- `GET /api/eu` passa a devolver `chave_pix` e `placa`.
- `PUT /api/eu` {telefone, email, chave_pix, placa} (token; vale o gate do
  PIN provisório). Validação: telefone 10-11 dígitos, e-mail com @, placa
  7 caracteres (ABC1234 ou ABC1D23), PIX até 120 caracteres. Grava, registra
  histórico só do que mudou, espelha telefone/e-mail/placa na planilha
  (best-effort, pelo CPF) e avisa no WhatsApp.
- `POST /api/cadastro` (sem token, JSON): cria PENDENTE e devolve
  {id, chave_envio}. Recusa CPF já com login ou já pendente.
- `POST /api/cadastro/<id>/documentos` (sem token, multipart `arquivo` +
  `tipo`=cnh|crlv + `chave_envio`): grava a foto. Quando CNH e CRLV estão
  presentes, manda o aviso no WhatsApp (uma vez) com o link do painel.

## Painel (painel_agentes)
- `/motoristas` ganha a seção "Cadastros pendentes" (só nível total):
  dados, fotos, campos editáveis (zonas/dias/tipo/aceita viagens vindos do
  cadastro), select do agente Vuupt (mesma lista de hoje). Botões Aprovar e
  Recusar (motivo).
- `POST /api/motoristas/cadastros/<id>/aprovar` {agent_id, zonas, dias,
  tipo_veiculo, aceita_viagens}: `cadastrar_motorista` (planilha) + login
  (PIN provisório SORTEADO por motorista, trocar_pin=1) + status APROVADO.
  Devolve o PIN uma única vez pra o Hugo mandar no WhatsApp do motorista
  (revisão automática 08/10: não repetir o 123456 do lote).
- `POST /api/motoristas/cadastros/<id>/recusar` {motivo}.
- `GET /api/motoristas/cadastros/<id>/documento/<cnh|crlv>` (nível total).
- Badge no menu e cartão "Cadastros de motorista" em /inicio (nível total).

## App (app_motorista)
- Perfil mostra PIX e placa e ganha "Meus dados" → `app/meus-dados.tsx`
  (telefone, e-mail, PIX, placa; Salvar → PUT /api/eu).
- Login ganha "Ainda não sou cadastrado" → `app/cadastro.tsx`: dados, tipo
  de veículo, zonas e dias (chips), aceita viagens, foto da CNH e do CRLV
  (câmera), Enviar. Tela final: "Recebemos seu cadastro. A Fresh Log avalia
  e manda o PIN no seu WhatsApp."

## WhatsApp (notificar_whatsapp.py)
- `texto_cadastro_motorista(nome, tipo_veiculo, link)` e
  `texto_alteracao_cadastro(nome, mudancas)`; `despachar(..., origem=
  "cadastro_motorista")`. Respeita `whatsapp_notificacoes.ativo`.

## Fora de escopo
- Criar agente na Vuupt pela API (não existe endpoint).
- Editar zonas/dias/veículo pelo app depois de aprovado (continua no painel).
- Aprovação sem agente Vuupt.
