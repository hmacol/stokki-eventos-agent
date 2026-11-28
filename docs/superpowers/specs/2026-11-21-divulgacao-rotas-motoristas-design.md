# Divulgacao de rotas aos motoristas: recusa ou equipe, nunca por horario

Data: 21/11/2026. Pedido do Hugo. Muda `nucleo/ofertas_rota_enviada.py` (oferta de rota ja enviada).

## Por que

A oferta automatica por prazo (05h30 sem aceite) gerou ofertas indevidas (19/11: rotas atribuidas
direto ao motorista durante o dia viraram oferta em 2 min). O Hugo quer que a rota so fique aberta
por decisao humana: o motorista recusou, ou a equipe quis ofertar. E, aberta, que chegue primeiro a
quem pode pegar mais rapido.

## O que muda

**Quando uma rota fica aberta (so dois casos):**
1. O motorista recusou no app (gatilho RECUSA, ja existe).
2. A equipe de logistica clicou **Ofertar** no card da rota ja enviada (novo, motivo EQUIPE).

O gatilho PRAZO sai por inteiro (e a chave `horario_limite` deixa de ser lida). Nada abre por relogio.

**Como ela e divulgada (cronometro conta a partir da abertura):**

| Minuto | Para quem | Como |
|---|---|---|
| 0 | grupo Rotas FRESHLOG | aviso de hoje (link, entregas, regiao, valor). Qualquer motorista com app ja pode pegar pelo link: quem chegar primeiro. |
| 15 | perto de terminar: rota de hoje com ate 2 paradas pendentes, ou rota de hoje concluida (fixos antes de esporadicos) | mensagem particular no WhatsApp |
| 25 | fixos restantes (`bd_motoristas.POSICAO = FIXO`) | mensagem particular |
| 35 | esporadicos restantes | mensagem particular |

- A rota aparece na aba Ofertas para todos desde o minuto 0 (o link do grupo abre essa aba).
- Alguem pegou (ESCOLHIDA) ou a oferta saiu (RETIRADA): as mensagens param.
- Etapa sem ninguem e pulada na hora (de madrugada ninguem esta em rota: vai direto pros fixos).
- Cada motorista recebe no maximo uma mensagem por oferta.

## Decisoes do Hugo (21/11)

- Nunca por tempo ou horario: so recusa ou equipe.
- Primeiro o grupo Rotas; 15 min depois os motoristas, comecando pelos que estao perto de terminar,
  depois fixos, depois esporadicos.
- No grupo, pelo link, pega quem chegar primeiro.
- "Perto de terminar" = faltam ate 2 paradas (ou ja terminou a de hoje).
- 10 min entre as etapas dos motoristas.
- Ofertar pela equipe = rota ja enviada (botao no card), nao o rascunho.
- Caminho A: etapas sao mensagem particular no WhatsApp (o push do app nao funciona hoje: 0 de 17
  motoristas com token).

## Abordagens consideradas

- **A (escolhida):** rota aberta a todos desde o inicio; etapas = WhatsApp particular em ordem.
  Mantem o "quem chegar primeiro" do grupo e ainda alcanca quem nao viu o grupo.
- **B:** etapas controlam quem ve a rota na aba. Garante a ordem, mas o link do grupo abriria a aba
  vazia para quem ainda nao foi liberado; contradiz "quem chegar primeiro".
- **C:** etapas por notificacao do app. Ideal, mas o push nao funciona (nenhum token registrado);
  exige investigar e provavelmente APK novo.

## Minhas escolhas (o Hugo pode corrigir)

- **Mensagem particular comeca em piloto:** vai so pro numero do Hugo (como as outras mensagens a
  motorista) ate ele ligar o envio real. Teto diario proprio (`rota_sem_aceite.teto_mensagens`,
  padrao 60) pra nao gastar o teto dos outros avisos. Motivo: numero nao-oficial, risco de bloqueio.
- **Quem recebe mensagem:** so motorista com login ativo no app (sem app nao consegue pegar), menos
  o original, menos quem o veiculo barra (Fiorino em rota VAN/HR).
- **Andamento pra "perto de terminar":** rota operada pelo app usa o nucleo (tempo real); rota da
  Vuupt consulta a Vuupt na hora da etapa (so GET), porque o sync do nucleo e de 30 em 30 min. Se a
  consulta falhar, usa o nucleo.
- **Ofertar rota com motorista:** ele continua com a rota ate a equipe trocar na Vuupt (a escolha
  segue aguardando a equipe, como hoje). Ele nao ve a propria rota em Ofertas. Rota enviada sem
  motorista tambem pode ser ofertada.
- **Botao Ofertar:** no card da rota enviada do planejamento, ao lado de "Cancelar e notificar",
  com confirmacao; so pra quem pode editar o planejamento; uma oferta por rota (ofertada uma vez,
  o botao vira "Ofertada" com "Retirar oferta" enquanto ninguem pegou; retirada nao reabre). Rota de hoje ou futura, ainda nao iniciada.
- **Texto da mensagem particular:** curto, com entregas, regiao, saida, valor e o link da pagina da
  oferta (mesmo da pagina de hoje).

## Confirmacao da escolha (Hugo, 21/11)

A troca na Vuupt deixa de ser manual. Quem pegou a rota aparece no card ("Fulano quer esta rota") com
**Confirmar motorista** e **Recusar**. Confirmar: troca motorista e carro na Vuupt (funcao do "editar
rota enviada"), passa a rota pro motorista novo no app na hora, fecha a oferta e refaz o romaneio. Se
a Vuupt recusar, nada muda e o card mostra o motivo. Recusar: a rota volta pra oferta e o recusado nao
a ve mais. O motorista novo ainda da o aceite normal no app. Carro = o do cadastro dele (sem carro:
passa com aviso, como hoje).

## Fora do escopo

- O marketplace de rascunho ("Publicar oferta" do planejamento, com ondas por rodizio) fica como
  esta.
- Consertar o push do app.
- Escolha trocando o motorista sem o OK da equipe (o Confirmar no card e obrigatorio).

## Pro construtor

- Etapas guardadas na oferta (`nucleo_ofertas_rota`: hora de abertura e quem ja recebeu), executadas
  pelo timer de 2 min que ja existe.
- Testes: abrir por recusa e por equipe; nunca por horario; ordem e espera das etapas; parar quando
  alguem pega; etapa vazia pulada; veiculo barrado nao recebe; piloto manda so pro Hugo.
