# Ciência obrigatória do dedicado fora do dia fixo (desenho)

Data: 17/11/2026. Pedido do Hugo. **Aprovado pelo Hugo em 17/11.** Ramo `ciencia-dedicado` (worktree `.claude/worktrees/ciencia-dedicado`).

## Por quê

Embarcadores estão contestando a eficiência dos nossos avisos de pedido agendado que virou dedicado ("não fui avisado"). Hoje o aviso sai uma vez, por e-mail e WhatsApp, sem resposta, e está em piloto. O centro deste trabalho é a **prova**: todo envio registrado e a resposta do embarcador gravada, consultável pela equipe na hora da contestação.

## O que o Hugo pediu (palavras dele)

- Notificação recorrente para todos os embarcadores, de entregas agendadas para localidades com agenda certa (região de dia fixo), em data que não é dia de visita.
- Três canais: chat do portal, e-mail e WhatsApp.
- Clique obrigatório num link: ciência do carro dedicado **ou** reagendamento.
- Sem resposta, lembrete a cada 4 horas.
- Enquanto não responde, o pedido **segue como hoje**: vira dedicado na hora e sai na data pedida (opção 1, decidida em 17/11).

## Objetivos, não objetivos, o que seria fracasso

Objetivos: registro completo de avisos e respostas; embarcador resolve com um clique; equipe enxerga tudo numa tela.

Não objetivos: mudar a regra que marca o dedicado; mudar o cálculo do valor; motor genérico de confirmação para outros avisos; segurar pedido por falta de ciência; reagendar sozinho.

Fracasso mesmo funcionando: atrasar entrega por falta de clique; lembrete continuar depois da resposta ou depois da entrega; aviso real sair para cliente antes de o Hugo ligar; não conseguir mostrar, num pedido contestado, quando e por onde avisamos.

## Caminhos considerados

1. **Fazer o aviso atual crescer (escolhido, aprovado pelo Hugo).** A detecção de `roteirizacao/fora_dia_fixo.py` fica igual; o aviso único vira aviso com ciência, lembrete por timer e página de resposta. Reaproveita chat do portal, link assinado e alteração de entrega.
2. Motor genérico de "notificação com confirmação" para qualquer tipo de aviso. Mais obra, ninguém pediu para os outros avisos.
3. Ciência por resposta de texto no e-mail/WhatsApp lida por IA. Prova fraca; resposta de WhatsApp cai no celular do Hugo, fora do sistema.

## Como funciona hoje (o que se reaproveita)

- `roteirizacao/fora_dia_fixo.py`: `marcar_fora_dia_fixo` marca dedicado (`pedidos_dedicados`, `marcado_por` começando com `POR_FORA_DIA_FIXO`), `pendentes_de_aviso` e `tratar_fora_dia_fixo` (chamado por `criar_rotas_diarias` e `incrementar_rotas`, com a lista fresca de serviços da Vuupt).
- `roteirizacao/notificar_fora_dia_fixo.py`: e-mail por embarcador + WhatsApp por pedido; `registro_dia_fixo.avisos_fora_dia_fixo` guarda um aviso por pedido.
- `controle_notificacoes.decidir("fora_dia_fixo", cnpj, canal=...)`: tela Notificações (SIM / NAO / PILOTO).
- Chat do portal: `portal_cliente/chamados.py` (`criar_chamado`, `mensagem_sistema`), como em `painel_agentes/aviso_rota_cancelada.py`.
- Link sem login: padrão de `portal_cliente/link_canhoto.py` (itsdangerous, salt próprio).
- Reagendar: `portal_cliente/alterar_entrega.py` (`avaliar`, `planejar`, `aplicar` com `dependencias_reais`).
- WhatsApp ao cliente: `notificar_whatsapp.avisar_cliente_fora_dia_fixo`, teto `whatsapp_notificacoes.clientes.teto_diario`.
- Timer de referência: `infra/stokki-avisar-cliente-sem-resposta.{service,timer}`.

## Desenho

### 1. Registro (duas tabelas novas em `dados/dados.db`)

`ciencias_dedicado`: uma linha por pedido marcado por esta regra.
Campos: código do pedido (normalizado, chave), service_id, sender_id, cnpj, região, data da entrega, valor, valor pendente (0/1), criado_em, situação (`PENDENTE`, `CIENTE`, `REAGENDADO`, `ENCERRADO`), motivo do encerramento, respondido_em, respondido_por (nome digitado), respondido_ip, data nova (quando reagendou), proximo_lembrete_em, alerta_equipe_em.

`ciencias_dedicado_envios`: uma linha por tentativa de envio, por canal.
Campos: código do pedido, canal (`chat`, `email`, `whatsapp`), quando, destino (e-mail, número ou id do chamado), resultado (`enviado`, `piloto`, `nao_enviado`), motivo (ex.: `teto`, `sem_whatsapp`, `falha`, `desligado`), número da rodada (0 = primeiro aviso).

Nada é apagado: é o histórico que responde à contestação.

### 2. Quem entra

Todo pedido que `marcar_fora_dia_fixo` marcar depois do deploy. Pedido marcado antes do deploy não entra (minha escolha, dita ao Hugo). O aviso único antigo deixa de ser enviado; `avisos_fora_dia_fixo` fica como está, só deixa de receber linhas.

**Marcar de novo depois de reagendar (decisão do Hugo, 17/11, opção 1):** hoje `marcar_fora_dia_fixo` pula qualquer pedido que já teve dedicado, ativo ou removido. Exceção nova: dedicado removido **por esta página** (`removido_por` = "ciência: reagendado pelo embarcador") não bloqueia. Se o embarcador reagendar e depois voltar a data para fora do dia de visita, o pedido é marcado de novo e o ciclo de aviso recomeça (linha nova de ciência, histórico antigo preservado). Remoção feita pela equipe continua valendo para sempre. É o único ajuste na regra de marcação.

Dedicado marcado à mão pela equipe (Planejamento "Marcar como dedicado?", atendimento) **não** entra: ali a equipe já falou com o cliente. (Minha escolha; perguntar ao Hugo se quiser incluir.)

### 3. Envio e lembretes

Rotina nova `notificar_ciencia_dedicado.py` (raiz), timer `stokki-ciencia-dedicado` a cada 15 minutos:

- Seleciona pedidos `PENDENTE` com `proximo_lembrete_em` vencido.
- Só envia entre 08h e 20h (America/Sao_Paulo), todos os dias. Fora da janela, o lembrete fica para as 08h.
- Agrupa por embarcador: **uma** mensagem por canal listando todos os pedidos pendentes dele que venceram nessa rodada; cada pedido registra o seu envio.
- Depois de enviar (ou tentar), `proximo_lembrete_em = agora + 4h`.
- O primeiro aviso sai na própria rodada que marcou o dedicado (chamada direta de `tratar_fora_dia_fixo`), respeitando a mesma janela de horário.

Canais, em cada rodada:
- **E-mail:** destinatários das preferências do portal (tipo `agendamento`), **ignorando** a opção "desligado" do embarcador, por ser aviso de cobrança. Um e-mail por embarcador, tabela pedido / destino / data pedida / dias de visita da região / valor, e um botão por pedido para a página de resposta.
- **Chat do portal:** primeiro aviso abre uma conversa (`ORIGEM_SISTEMA`, assunto "Entrega dedicada: confirme a ciência", `pedido_ref` = primeiro código). Lembretes entram como mensagem do sistema na mesma conversa, sem abrir outra.
- **WhatsApp:** número cadastrado nas preferências do embarcador, **mesmo com o aviso de WhatsApp desligado** por ele (`preferencias_notificacao.whatsapp_do_embarcador` só devolve o número com a preferência ligada: aqui lê o número direto). Uma mensagem por embarcador com os pedidos e o link. Conta no teto diário de cliente; teto estourado registra `nao_enviado: teto` e os outros canais saem.

Regras do chat (escolhas minhas):
- Só conta como `enviado` quando o embarcador tem conta no portal. Sem conta, registra `nao_enviado: sem_conta_portal` e não abre conversa (a maioria dos embarcadores ainda não tem conta; a tela da equipe mostra isso).
- A decisão da tela Notificações para o **e-mail** vale também para o chat (a tela só conhece e-mail e WhatsApp).
- A conversa nasce aguardando o cliente, fora da fila de trabalho da equipe. Se a equipe resolver a conversa e houver lembrete depois, abre conversa nova.

Falha de um canal não impede os outros. Embarcador sem canal nenhum gera envio `nao_enviado: sem_canal` e alerta para a equipe na primeira rodada.

### 4. Página de resposta (portal do cliente)

Rota pública `/ciencia/<token>` no `portal_cliente` (sem login, token assinado com salt próprio, carrega sender_id; validade 30 dias). O token é por embarcador: a página lista todos os pedidos dele em `PENDENTE`, e mostra como respondidos os já resolvidos.

Revisão de segurança (19/11): o token passou a carregar também as ciências que cobre (as da mensagem em que saiu) e a origem (`email`, `whatsapp`, `chat`, `equipe` ou `painel:<usuário>`). A página só mostra e só aceita resposta dessas ciências; pedido aberto depois ganha link novo. A resposta grava a origem e o navegador; o "Copiar link" do painel gera link só daquele pedido, marcado com quem copiou. Ciente e reagendar travam a ciência (`processando_desde`, 10 min) antes de responder, então duas respostas ao mesmo tempo não divergem da Vuupt.

Para cada pedido: código, destinatário, cidade, data pedida, dias de visita da região, valor do dedicado (ou "valor a confirmar pela Fresh Log" quando pendente). Duas ações:

- **Estou ciente do carro dedicado:** exige o nome de quem responde. Grava `CIENTE`, nome, data/hora, IP. Encerra os lembretes do pedido.
- **Reagendar para o dia de visita:** mostra as próximas 4 datas de visita da região (a partir de amanhã, feriado tratado por `regioes_dia_fixo`). Ao escolher, exige o nome, aplica a data pelo caminho de `alterar_entrega` (só data), remove o dedicado (`pedidos_dedicados.remover`, por "ciência: reagendado pelo embarcador"), grava `REAGENDADO`. Se a alteração não for permitida (pedido já em rota que começou, etc.), mostra a mensagem do `alterar_entrega` e orienta o chat; o pedido continua `PENDENTE`.

A resposta também entra como mensagem do sistema na conversa do chat ("Fulano confirmou ciência em 17/11 10:32"). A página funciona mesmo com `portal_cliente.alteracao_entrega` desligada para o CNPJ: tem chave própria.

No piloto os botões são **simulados**: a página mostra a faixa "modo piloto", valida tudo e mostra a tela de sucesso, mas não muda data na Vuupt, não remove dedicado e não fecha o pedido. A exceção é o cliente teste do portal, onde as ações são reais, para provar o caminho inteiro. (Escolha minha: o clique de teste do Hugo não pode mexer em pedido de cliente real.)

### 5. Quando os lembretes param

- Resposta (`CIENTE` ou `REAGENDADO`).
- `ENCERRADO` com motivo, verificado pela rotina do timer **antes de cada envio**, pelo espelho local do núcleo (`nucleo_*`, sincronizado com a Vuupt): pedido cancelado; pedido entregue ou com insucesso; rota do pedido já iniciada (saiu para entrega, não há mais o que decidir); dedicado removido (`pedidos_dedicados.removido_em`); data da entrega já passou.
- Sumir da lista de `tratar_fora_dia_fixo` **não** encerra: essa lista é só o pool sem rota, e o dedicado sai dela quando ganha rota.
- Data mudou (núcleo): se agora cai em dia de visita, `ENCERRADO: data em dia de visita`; se continua fora, atualiza a data na linha e segue `PENDENTE`.
- Pedido que o núcleo não conhece: segue `PENDENTE` e só encerra por data passada ou dedicado removido.

### 6. Equipe

- Tela nova no painel `/ciencias-dedicado` (entra em `acesso_telas.TELAS` e no menu): lista com filtro por situação e embarcador; cada pedido abre o histórico de envios por canal e a resposta. Botão "Copiar link" da página do embarcador, para a equipe mandar à mão.
- Alerta no grupo de WhatsApp de Alertas, uma vez por pedido: às 16h da véspera da entrega (ou no primeiro aviso, se já for depois disso), se ainda `PENDENTE`. Texto com pedido, embarcador, quantos avisos saíram.

### 7. Chaves e lançamento

`config.yaml`, seção `ciencia_dedicado`: `ativo`, `intervalo_horas` (4), `janela` (`08:00`–`20:00`), `forcar_destino` (ausente = e-mail do Hugo).

Duas chaves com papéis separados:
- `ciencia_dedicado.ativo` escolhe o **fluxo**: ausente ou falso = aviso único antigo, nada deste desenho roda; verdadeiro = fluxo novo, e o aviso antigo deixa de sair. A tela não muda isso.
- Tela Notificações, tipo novo `ciencia_dedicado`, escolhe **para quem vai** (ENVIAR / PILOTO / NÃO) dentro do fluxo novo. Sem linha na tela = piloto.

Piloto: e-mail e WhatsApp vão para o Hugo; o chat não abre conversa para o cliente (prévia registrada como envio `piloto`). Ligar para os clientes é decisão do Hugo, pela tela.

Virada de piloto para envio real: pedidos ainda `PENDENTE` com entrega futura recebem um primeiro aviso real (a contagem de rodadas recomeça; os envios de piloto ficam no histórico marcados como `piloto`).

`--modo-teste` na rotina: só loga o que enviaria, não grava envio nem mexe em `proximo_lembrete_em`.

### 8. Testes

`unittest`, com banco temporário e canais falsos: seleção por vencimento e janela; agrupamento por embarcador; um canal falhando não bloqueia os outros; teto do WhatsApp; ciência grava e para o lembrete; reagendar aplica, remove dedicado e para; alteração recusada mantém `PENDENTE`; encerramentos (removido, data passada, cancelado, data nova válida); piloto não abre conversa; token inválido/expirado dá página de erro sem vazar dado; token de um embarcador não mostra pedido de outro.

## Limites conhecidos

- WhatsApp sai do número do Hugo (OpenWA), com teto diário de cliente. Com vários embarcadores pendentes o teto estoura e o WhatsApp deixa de sair nesse dia; fica registrado. Aumentar o teto é decisão do Hugo.
- Resposta do cliente por texto no WhatsApp continua fora do sistema; só o clique vale como ciência.
- O valor continua em linha reta (calculadora sem a Google Routes); a página mostra o valor gravado no dedicado.

## Deixado para quem constrói

Nomes de funções, layout dos templates (seguir o visual das páginas públicas do portal e das telas do painel), texto final das mensagens (português simples, com o link em destaque), organização dos módulos. Atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit.
