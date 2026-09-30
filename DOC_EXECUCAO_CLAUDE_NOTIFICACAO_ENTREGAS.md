# Notificação de entrega concluída (e-mail por pedido ao embarcador)

Pedido do Hugo (17/09/2026): "criar e-mails por embarcador e por entrega, que
informa assim que um pedido é concluído, com todas as informações referentes
ao pedido, status de entrega, canhoto se possuir, em caso de falha informar o
motivo da falha."

É o substituto do e-mail de "finalizado" que a Vuupt manda hoje ao embarcador
(`DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md`, linha 43: "precisa de substituto na
virada").

## Decisões do Hugo (17/09)

| Tema | Decisão |
|---|---|
| Formato | **1 e-mail por pedido**, para os e-mails cadastrados do embarcador (`interno.email`). Sem resumo diário. |
| E-mail da Vuupt | Este **substitui** o da Vuupt. O Hugo desliga o "finalizado" lá depois de validar o nosso. |
| Falha | O e-mail novo **só informa** (status + motivo). O e-mail de insucesso com botões de reenvio (`insucesso_entrega/notificar_insucesso_aguardando_resposta.py`) **continua como está**. |
| Canhoto ausente | Sucesso sem canhoto **espera até 30 min**; vencido o prazo, envia sem canhoto. Falha sai na hora. |
| Liga/desliga | **Flag própria** `notificacao_entregas.ativo`; não obedece `notificacoes_automaticas.ativo`. |
| Motorista | **Não aparece** no e-mail. |
| Volume | ~200 entregas/dia no pico; cabe no limite do Gmail (2.000/dia). |

Verificado antes de implementar (pedido do Hugo): não havia e-mail por pedido
concluído no código, no git (todas as branches), no repo de importação nem na
VPS. `entregas_processadas` é tabela sem código que a use.

## Como se encaixa com os outros e-mails ao embarcador (todos de 17/09)

| Quando | E-mail | Onde |
|---|---|---|
| 07h | Notas que saem pra entrega hoje (1 por embarcador) | `notificar_nfs_em_rota.py` (commit `bd459a3`) |
| Ao concluir | **Este**: 1 por pedido, com canhoto ou motivo da falha | `notificacao_entregas/` |
| 20h | Resumo do dia, opt-in (1 por embarcador) | `DOC_EXECUCAO_CLAUDE_RESUMO_DIARIO_EMBARCADOR.md` (outra frente, em andamento) |

Os três juntos substituem os e-mails de início/fim da Vuupt.

**Preferências do portal (botão Notificações, `preferencias_notificacao.py`,
tipo `entrega_concluida`): já integradas.** `aplicar_preferencias()` em
`notificar_entrega_concluida.py` sobrepõe o e-mail de notificações do cliente e
a chave liga/desliga ao cadastro da `interno`. É tolerante de propósito: sem o
módulo (outra frente, pode ser deployado depois) ou com erro nele, vale só a
`interno`, e a rotina de 5 min não cai. O doc do resumo diário lista essa
integração como pendente: **não precisa mais ser feita lá**.

**`interno.notificar_email = 0` não é veto para este e-mail** (2ª decisão do
Hugo em 17/09: "tirar o veto deles e deixar como não marcados"). Em produção
são 4: CLIENTE TESTE, EMPÓRIO QUATRO ESTRELAS, PADRAO PURO e PEDRAMOURA (35 das
97 entregas da janela medida). Para eles os 3 tipos novos **nascem
desmarcados** em `preferencias_notificacao.py`; ligam sozinhos no botão
Notificações do portal, ou o Hugo liga pelo `/equipe`. Nada é gravado em
produção pra isso: a regra é o padrão de quem não tem linha, então não há
ordem de deploy que faça os 4 receberem sem querer. A coluna NÃO foi alterada,
porque `pipeline.py` (confirmação de agendamento e de redespacho, sem
chave-mestra) e a triagem de pedidos parados também a leem, e ali o Hugo quer
que continue bloqueando. Contingência: sem o módulo de preferências no ar, a
coluna volta a bloquear aqui (não há como o cliente optar).

## Arquitetura

Agente novo em `notificacao_entregas/`, timer próprio a cada 5 min (06h–23h),
consultando a Vuupt direto. Medido em 17/09: `status=done` +
`completed_at>=ontem` com `include=checklistAnswers,customer,failedReason`
devolve ~260 serviços em 2 s.

| Arquivo | Papel | Depende de |
|---|---|---|
| `regras_entrega.py` | Funções puras: decidir o que fazer com cada serviço (enviar, esperar canhoto, ignorar), converter data, calcular volumes. Sem rede, sem banco. | nada |
| `montar_email_entrega.py` | Assunto + HTML (puro). | `email_utils` (paleta/envelope), `motivos_falha` |
| `fingerprint_notificacao_entrega.py` | Tabela `notificacoes_entrega` (estado por `service_id`). | sqlite |
| `vuupt_entregas.py` | `buscar_concluidos()` e `baixar_canhoto_pdf()`. **Única peça a trocar na saída da Vuupt** (passa a ler `nucleo_paradas` + `nucleo_comprovantes`). | `vuupt_client`, `http_retry` |
| `notificar_entrega_concluida.py` | Orquestra: config, busca, decide, envia, registra. CLI. | todos acima |
| `infra/stokki-notificar-entregas.{service,timer}` | oneshot como www-data, `*:00/5` das 06h às 23h. | — |

### Estados (`notificacoes_entrega.estado`, chave `service_id`)

Reentrega (`-R2`) é outro serviço na Vuupt, então gera outro e-mail — correto.

| Situação do serviço | Ação | Estado gravado |
|---|---|---|
| Já tem estado final (`ENVIADO`, `IGNORADO`, `SEM_DESTINATARIO`, `ERRO_ENVIO`) | nada | — |
| Flag `ativo=false` | não envia | `IGNORADO` (desligado) → ligar a flag vale só dali pra frente, sem rajada retroativa |
| Código fora do padrão `PS-n`, sem `sender_id`, retirada com `status_done=failed` | não envia | `IGNORADO` (motivo) |
| Retirada no galpão (`[RETIRADA]`) com sucesso | envia na hora o e-mail de **Retirado** (nunca tem checklist: não espera canhoto) | `ENVIADO` |
| Concluído há mais de `max_atraso_horas` (12) | não envia (timer parado/primeiro deploy não dispara e-mail velho) | `IGNORADO` (antigo) |
| Embarcador fora de `embarcadores_piloto` (quando a lista não é vazia) | não envia | `IGNORADO` (fora do piloto) |
| Embarcador sem e-mail, ou com a chave `entrega_concluida` desligada (pelo cliente, ou nascida desmarcada por `notificar_email=0`) | não envia | `SEM_DESTINATARIO` |
| Falha (`status_done=failed`) | envia na hora | `ENVIADO` |
| Sucesso com canhoto (`images_quantity>0`) | baixa PDF, envia com anexo | `ENVIADO` (com_canhoto=1) |
| Sucesso sem canhoto, < 30 min de `completed_at` | espera | `AGUARDANDO_CANHOTO` |
| Sucesso sem canhoto, ≥ 30 min | envia sem anexo, com aviso | `ENVIADO` (com_canhoto=0) |
| SMTP falhou | tenta de novo na próxima rodada; na 3ª falha desiste e avisa internamente | `FALHA_ENVIO` → `ERRO_ENVIO` |

PDF do canhoto maior que 15 MB não é anexado (limite do Gmail); o e-mail sai
com o aviso do portal. Falha ao baixar o PDF conta como "sem canhoto" e segue
a regra dos 30 min.

### Conteúdo do e-mail

- Assunto: `Pedido PS-1234 entregue · <destinatário>` / `Pedido PS-1234 não entregue · <motivo>`.
- Corpo (envelope visual Freshlog, faixa verde no sucesso e vermelha na falha):
  pílula de status; pedido; NF (`documentos_processados`); destinatário;
  endereço (+complemento); concluído em (UTC da Vuupt → São Paulo);
  volumes; observações do pedido (`note`), quando houver.
- **Volumes**: `dimension_3` é ponderado. Mostra `dimension_3 ÷ interno.fator_ponderado`
  só quando o resultado é inteiro; senão a linha é omitida.
- Falha: bloco com o motivo (`MOTIVOS_FALHA`; motivo fora do de-para usa a
  descrição oficial da Vuupt, sem os avisos internos de `texto_do_motivo`).
  Hoje TODO insucesso gera a pergunta de reenvio, mas aquele e-mail obedece
  `notificacoes_automaticas.ativo` (desligada desde `174ca4a`). Por isso a
  frase "você receberá em seguida um e-mail para decidir sobre o reenvio" só
  aparece com a chave-mestra ligada; desligada, o texto é "nossa equipe vai
  tratar a próxima tentativa; se quiser orientar o reenvio, responda este e-mail".
- Canhoto: PDF anexo `Canhoto_PS-1234.pdf`. Sem canhoto: "comprovante
  disponível no portal assim que processado".
- Rodapé: link do portal (`portal_cliente.url_base`), Reply-To `entregas@freshlogbr.com`.
- Fora do e-mail por indisponibilidade na API de listagem: nome de quem
  recebeu e observação do motorista (estão no PDF do canhoto).

### E-mail de "Retirado" (Hugo, 17/09: "vamos colocar um e-mail de Retirado também")

A retirada é o serviço avulso `[RETIRADA] #PS-x` que `retiradas/acompanhar_retiradas.py`
fecha por API quando a Stokki marca "Enviado" (timer de 30 min: o horário é o
de quando detectamos a saída, por isso o rótulo é "Saída registrada em").

- Assunto: `Pedido PS-1234 retirado · <quem retirou>` (ou `· <destinatário final>` se quem retirou for genérico).
- Pílula verde RETIRADO; "foi retirado no nosso galpão por X"; linhas: pedido, NF,
  destinatário final, retirado por, saída registrada em, volumes. Sem bloco de canhoto.
- O `customer` e o `address` desse serviço são o PRÓPRIO galpão e a `note` é
  texto interno ("não roteirizar…"): nada disso vai pro e-mail. Quem retirou e o
  destinatário final são lidos de volta da nota por `retiradas.regras_retirada.dados_da_nota`
  (teste de ida e volta com `montar_payload_retirada`: mudar o formato lá quebra o teste aqui).
- Nomes que são rótulo e não empresa na `BD_TRANSPORTADORAS` (`CLIENTE RETIRA`,
  `Retirada Pessoal`, `COLETA FABRICA`, `TRANSPORTADORA COLETA`, e o fallback
  `cliente`): o e-mail não diz "por quem".
- Mesmas travas, preferência (`entrega_concluida`) e idempotência das entregas.
- Medido 17/09: 4 retiradas concluídas em 4 dias, todas com `sender_id` e nota no formato.

### Config (`config.yaml`, seção opcional — os defaults são os seguros)

```yaml
notificacao_entregas:
  ativo: false                          # default false
  forcar_destino: hugo@freshlogbr.com   # default hugo@; "" = envia ao embarcador de verdade
  embarcadores_piloto: []               # sender_ids; vazio = todos
  espera_canhoto_min: 30
  max_atraso_horas: 12
  cc: []                                # cópia fixa opcional
```

Trava dupla pro envio real: `ativo: true` **e** `forcar_destino: ""`. Com
`forcar_destino` preenchido o e-mail vai só pra ele, com uma faixa "iria para
X", e o estado é gravado normalmente (piloto sem repetição).

### CLI

```
py -3.11 notificacao_entregas/notificar_entrega_concluida.py --modo-teste [--limite 3]
py -3.11 notificacao_entregas/notificar_entrega_concluida.py --modo-teste --pedido PS-1234
py -3.11 notificacao_entregas/notificar_entrega_concluida.py
```

`--modo-teste`: ignora a flag `ativo`, manda pra hugo@ com faixa de teste, no
máximo `--limite` e-mails (default 3, misturando sucesso e falha), **não grava
nada no banco**. `--pedido`: força um pedido específico (só com `--modo-teste`).

### Aviso interno

Teto de 60 e-mails por rodada (o resto entra na seguinte), 1 s entre envios.

Rodando a cada 5 min, **não** manda `notificar_execucao` a cada rodada. Só
quando algum pedido chega a `ERRO_ENVIO` (3 falhas de SMTP). Queda do script é
coberta pelo `OnFailure=stokki-alerta-falha@%n.service`.

## Testes

`py -3.11 -m unittest notificacao_entregas.test_regras_entrega notificacao_entregas.test_montar_email_entrega notificacao_entregas.test_fingerprint_notificacao_entrega notificacao_entregas.test_notificar_entrega_concluida retiradas.test_regras_retirada`

Cobrem: tabela de estados acima linha a linha; espera e vencimento dos 30 min;
idempotência em reprocesso; UTC → São Paulo; volumes ponderados; HTML de
sucesso/falha com escape; `forcar_destino`; 3 falhas de SMTP; modo teste sem
escrita.

Estado em 17/09: 83 testes verdes (inclui `retiradas.test_regras_retirada`); prova real local com
`--modo-teste`: 3 e-mails na caixa do Hugo (2 com canhoto PDF anexo, 1 de falha) e 2 de
retirada (`--pedido PS-38931` e `PS-39187`). Sem commit e sem deploy.

## Rollout

1. Deploy com `ativo: false` (default): o timer roda, só registra `IGNORADO`.
2. `--modo-teste` na VPS: Hugo confere 3 e-mails reais na caixa dele.
3. Piloto: `ativo: true`, `forcar_destino: hugo@`, `embarcadores_piloto: [<1 sender_id>]`.
4. Envio real ao piloto: `forcar_destino: ""`. Hugo decide.
5. Todos: `embarcadores_piloto: []`. Hugo desliga o "finalizado" na Vuupt.

## Fora do escopo (fase 2)

- Fonte app próprio (`nucleo_paradas` + `nucleo_comprovantes`) — trocar `vuupt_entregas.py` na virada.
- E-mail ao destinatário final.
- Quando o link público do canhoto (`/cliente/c/<token>`, frente do resumo
  diário) estiver no ar: usar no e-mail "sem canhoto" no lugar do aviso do
  portal, e trocar o rodapé "responda este e-mail" por "ajuste no portal,
  botão Notificações".
