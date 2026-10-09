# Cancelamento de pedido pelo portal do cliente

Especificação de 06/10/2026, a pedido do Hugo, depois do caso PS-39959
(Maria Dolores): o embarcador cancelou no portal em 23/09, o pedido ficou
duas semanas vivo na Stokki e na Vuupt, e o portal continuou cobrando
agendamento.

Status: **implementado em 07/10/2026, sem deploy** (ramo
`cancelar-agendamento`; plano em
`docs/superpowers/plans/2026-10-06-cancelamento-pedido-portal.md`).
Decisões abaixo são do Hugo (06/10).

## Por que existe

Hoje "Cancelar" num envio já criado na Stokki (`portal_envios.status` em
CRIADO/DUPLICADO) só grava uma linha PENDENTE em `portal_solicitacoes` e manda
um e-mail pro atendimento (`portal_cliente/app.py::_avisar_operacao_solicitacao`).
Ninguém executa: não há tela no painel, só a CLI
`gerenciar_clientes.py solicitacoes`. O envio segue CRIADO, o pipeline segue
criando serviço na Vuupt, e o portal segue mostrando o pedido como vivo.

Correção de 06/10 (ed92629, deployada): com cancelamento PENDENTE o portal
para de cobrar agendamento. Esta spec resolve o resto: cancelar de verdade.

## Decisões do Hugo (06/10)

| Pergunta | Decisão |
|---|---|
| Cancelar na Stokki é automático? | **Sim.** Spike de 06/10 provou o endpoint (memória `reference_stokki_cancelar_pedido_endpoint`): `POST .../inventory/outbound/cancel` + poll `.../cancel/status/{id}`. PS-39959 foi cancelado assim. |
| Até onde o cliente cancela sozinho? | **Enquanto o motorista não saiu com a mercadoria**: pool, agendado, rascunho ou rota não iniciada. Em rota ou entregue, vira solicitação pra operação com aviso no WhatsApp. |
| Planejamento usa a mesma função? | **Não agora.** Escopo é só o cancelamento vindo do portal. O "Cancelar pedido" do Planejamento continua cancelando só na Vuupt. |
| Onde roda | No worker existente `portal_cliente/enviar_stokki.py --loop` (serviço `portal-cliente-envios`), que já pega a trava da Stokki a cada ciclo. |

## Fluxo

```
cliente clica Cancelar (envio CRIADO ou DUPLICADO)
  -> portal_envios.status = CANCELANDO
  -> portal_solicitacoes: tipo=cancelar, status=PENDENTE, detalhes=motivo
  -> resposta: "Cancelamento em andamento -- você verá aqui em instantes."
     (sem e-mail nesse momento)

worker (a cada 20 s, antes dos envios NA_FILA), para cada CANCELANDO:
  decide pela regra "pode cancelar sozinho" (abaixo)
  SOZINHO:
     cancela cada serviço vivo na Vuupt (base e reentregas)
     cancela o pedido na Stokki
     -> status = CANCELADO; solicitação CONCLUIDA
        resposta = "cancelado automaticamente em DD/MM HH:MM"
        (o portal mostra o badge "Cancelado"; nenhum outro aviso)
  PRECISA DA OPERAÇÃO (em rota / entregue / insucesso em tratamento):
     -> status volta pra CRIADO; solicitação segue PENDENTE
     -> e-mail ao atendimento (o atual) com o motivo de não ser automático
     -> WhatsApp no grupo do atendimento (texto curto, ver Avisos)
     -> portal mostra "Cancelamento · aguardando Fresh Log", sem cobrar agendamento
  FALHA TÉCNICA (Stokki fora, 4xx/5xx inesperado, Vuupt 409...):
     -> mesmo tratamento de PRECISA DA OPERAÇÃO, com o erro no e-mail
     -> não fica tentando de novo sozinho

CANCELANDO órfão há mais de 30 min (worker caiu) volta pra CRIADO,
mesma regra do ENVIANDO.
```

Cancelar envio NA_FILA / ERRO / AGUARDANDO_LIBERACAO continua como hoje
(resolve no portal, nunca foi pra Stokki). ENVIANDO continua recusando.

## Regra "pode cancelar sozinho"

Lida do núcleo (`nucleo_pedidos`, `nucleo_rotas`), sem chamar a Vuupt pra
decidir. Dado o `codigo_pedido` do envio:

1. Serviços do pedido-base: linhas de `nucleo_pedidos` cujo código normalizado
   (`nucleo/normalizacao.py`) é o base ou uma reentrega (`-R1`, `-R2`...),
   sem `excluido_em` e com `status != CANCELADO`.
2. **Precisa da operação** se qualquer serviço vivo está `ENTREGUE` ou
   `INSUCESSO`, ou se a rota dele (`nucleo_rotas` por `vuupt_route_id`)
   já começou: `status = EM_ROTA`, `status_provedor = started` ou
   `iniciada_em` preenchido. Rota do agente virtual LALAMOVE
   (`agent_id = lalamove.agent_id_vuupt`) conta como começada.
   Atenção: `nucleo_pedidos.status = EM_ROTA` significa só "atribuído a
   uma rota" (`status_provedor = assigned`, 109 pedidos em 06/10); não
   decide sozinho.
3. **Sozinho** nos outros casos: `ABERTO` no pool, agendado, em rascunho,
   atribuído a rota `PLANEJADA`/não iniciada, ou sem serviço nenhum no
   núcleo (pedido ainda só na Stokki, ex. "Em espera").
4. Já cancelado numa ponta (serviço `canceled`, Stokki "Cancelado"): conta
   como sucesso naquela ponta e segue na outra. Já expedido na Stokki
   ("Enviado"): precisa da operação.

Função pura, testada: `portal_cliente/cancelamento.py::decidir(servicos, rotas)`
devolve `("SOZINHO" | "OPERACAO", motivo_texto)`.

## Peças

- `stokki/cancelar.py` — `cancelar_pedido(sessao, id_stokki, motivo) -> dict`:
  GET da página do pedido (lê a "Situação" e o `_token` do `form_cancel`);
  se já "Cancelado" devolve `{"ok": True, "ja_estava": True}`; se "Enviado"
  devolve `{"ok": False, "motivo": "expedido"}`; senão POST multipart
  (`_token`, `reason`, `provider_outbound_id`, `page=show`, header
  `X-Requested-With: XMLHttpRequest`), poll de `cancel/status/{id}` a cada
  2 s até `Canceled` (máx. 30 s), reconfere o detalhe. Motivo enviado:
  `"Portal Fresh Hub: <motivo do cliente>"`; sem motivo, `"Portal Fresh Hub: cancelado pelo embarcador"`
  (a Stokki exige 15+ caracteres). Testado com o HTML real gravado da página.
- `roteirizacao/cancelar_servico.py` — `cancelar_servico_completo(token, service_id) -> dict`:
  os mesmos passos de `planejamento_rotas.cancelar_pedido` (achar o
  rascunho do serviço, `preparar_cancelamento_de_parada` se a rota já foi
  enviada, `cancelar_servico_oficial` = `PUT /cancel`, `remover_parada` do
  rascunho, ressincronizar o núcleo), chamável fora do painel.
  `planejamento_rotas.cancelar_pedido` **não é alterado** (escopo B):
  duplicação consciente de ~15 linhas, a unificar quando o Planejamento
  entrar no escopo.
- `portal_cliente/cancelamento.py` — a regra `decidir()` e
  `processar_cancelamentos(conn, config, sessao_stokki)`: lê os CANCELANDO,
  decide, executa, grava os três fins, dispara avisos. Chamado por
  `enviar_stokki.ciclo()` antes dos lotes NA_FILA, dentro da trava já
  adquirida (se não há CANCELANDO nem NA_FILA, o ciclo não abre sessão).
- `portal_cliente/envio_pedidos.py` — `STATUS_CANCELANDO` ("Cancelando"),
  `aplicar_acao` grava CANCELANDO em CRIADO/DUPLICADO, `_linha`: CANCELANDO
  não pode cancelar/reagendar/tirar da rota, `concluir_solicitacao` de
  `cancelar` também marca o envio CANCELADO (pra CLI da operação).
- `portal_cliente/templates/_envios.html` — badge `b-CANCELANDO`, texto
  "Cancelando…" na linha, chip "Cancelamento · aguardando Fresh Log" já
  existe.
- `notificar_whatsapp.py` — `avisar_cancelamento_pendente(config, envio, motivo)`
  no grupo do atendimento (`grupo_atendimento_id`), assinatura
  `cancelamento:<envio_id>` (não repete), mesmo teto diário e modo teste.
- `MAPA_DO_SISTEMA.txt` — arquivos novos, status CANCELANDO, fluxo.

## Avisos

| Fim | E-mail atendimento | WhatsApp grupo atendimento | Portal |
|---|---|---|---|
| Cancelado sozinho | não | não | badge "Cancelado" (e a solicitação some das pendências) |
| Precisa da operação | sim, com o motivo (ex. "motorista em rota") | sim: `Cancelamento pedido pelo cliente: NF 9959 · PS-39959 · Maria Dolores · motorista em rota, precisa cancelar à mão` | "Cancelamento · aguardando Fresh Log" |
| Falha técnica | sim, com o erro | sim, mesmo texto com "falhou: <erro curto>" | idem |

Operação conclui pela CLI (`gerenciar_clientes.py concluir <id> --resposta ...`),
que passa a marcar o envio CANCELADO quando a solicitação é de cancelar.
Tela no painel fica fora desta spec.

## Acerto dos dados atuais (na VPS, como www-data, pela CLI)

- Envio 50 / PS-39959: `CANCELADO`; solicitação 3 CONCLUIDA com
  "cancelado em 06/10 (Vuupt pela operação em 05/10, Stokki pelo spike)".
- Solicitações 4 (PS-39958) e 22 (PS-40146), reagendamento: concluir com
  "resolvido por outro caminho (data definida no portal)".

## Testes

- `portal_cliente/test_cancelamento.py`: `decidir()` com núcleo em memória
  (pool, rascunho, rota não iniciada, rota iniciada, EM_ROTA, ENTREGUE,
  reentrega viva, sem serviço, já cancelado); `processar_cancelamentos` com
  Vuupt e Stokki falsos cobrindo os três fins e o órfão de 30 min;
  `aplicar_acao` gravando CANCELANDO; `concluir_solicitacao` marcando CANCELADO.
- `stokki/test_cancelar.py`: `cancelar_pedido` com sessão falsa e o HTML
  real gravado (já cancelado, expedido, sucesso com poll, erro 422).
- `roteirizacao/test_cancelar_servico.py`: `cancelar_servico_completo` com
  cliente falso (pool, rascunho, rota enviada); e o teste existente do
  Planejamento continua verde.
- Prova na VPS: cancelar pelo portal um pedido do cliente de teste
  (CNPJ 00.000.000/0001-91) criado pra isso; ver `CANCELADO` no portal,
  `canceled` na Vuupt e "Cancelado" na Stokki; depois um caso "precisa da
  operação" simulado (`--modo-teste` do WhatsApp) pra ver e-mail e texto.

## Riscos e armadilhas

- Login concorrente derruba a sessão Stokki: o cancelamento roda só dentro
  da trava do worker, nunca no processo web.
- `DELETE /services` faz o pipeline recriar o pedido aberto na Stokki
  (memória `project_cancelar_pedido_delete_recria`): usar sempre `PUT /cancel`.
  Como aqui a Stokki também é cancelada, o pipeline não encontra mais o
  pedido.
- Reentrega com o mesmo código sobrescreve a linha do núcleo (memória
  `project_nucleo_perde_falha_reentrega_mesmo_codigo`): a decisão olha
  todas as linhas do pedido-base que encontrar; se faltar alguma, o
  cancelamento na Vuupt do que sobrou não acontece — a operação enxerga
  pela Torre/vigia como hoje.
- Pedido dedicado: `aplicar_acao` já remove de `pedidos_dedicados` ao
  cancelar; manter.
- Lalamove: pedido com rota do agente virtual LALAMOVE conta como "em rota"
  (precisa da operação), porque cancelar na Vuupt não cancela na Lalamove.
