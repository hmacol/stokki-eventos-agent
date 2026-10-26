# Tela Notificações no painel — design

Data: 24/10/2026. Pedido do Hugo, conversa de 23–24/10.

## Por que

Hoje cada notificação para cliente (embarcador, transportadora) é ligada ou
desligada editando chaves no `config.yaml` da VPS (`notificacoes_automaticas.ativo`,
`<secao>.ativo`, `<secao>.forcar_destino`) por SSH. O Hugo quer soltar os
envios **um a um**, desligar um às pressas se algo der errado, disparar à mão
quando precisar e atender pedidos de cliente ("menos e-mails", "tira esse
endereço") sem terminal e com segurança.

A tela substitui a edição do config para essas chaves. O `config.yaml` continua
sendo o arquivo de segredos e nunca é escrito pelo painel.

## Metas

- Uma tela `/notificacoes` no painel, só nível `total`, no layout escolhido
  pelo Hugo (esboço A): interruptor geral no topo, uma linha por tipo de envio
  com estado e exceções, detalhe por tipo num painel lateral.
- Quatro camadas de controle: **geral**, **por tipo**, **por embarcador**,
  **por transportadora**.
- Estado de cada tipo: **Desligado / Piloto (só Hugo) / Ligado (cliente recebe)**.
- Botão **"Disparar agora"** por tipo, que **pergunta para quem enviar**.
- E-mails de notificação de cada embarcador editáveis na tela.
- Mais adiante: horário de cada envio configurável na tela e frequência
  reduzida por cliente, com **resumo agrupado**.
- Toda mudança registrada (quem, quando, de → para) e reversível.

## Não metas

- Não editar o `config.yaml` pelo painel, nem dar ao processo web permissão de
  escrita nele ou no systemd.
- Não mudar o conteúdo dos e-mails existentes (fora a versão "resumo" da peça 3).
- Não mexer nos alertas internos (falha de job, grupo ALERTAS FRESH,
  `notificacao_execucao`), que seguem pelo config.
- Não substituir a escolha do cliente no portal (botão Notificações).

## Anti-metas (falharia mesmo "funcionando")

- Um clique errado manda e-mail real para todos os clientes sem confirmação.
- A tela some com uma trava que hoje existe (ex.: transportadoras voltarem a
  receber porque a mestra ligou — ver commit 16b5501).
- Duas fontes de verdade para os e-mails do embarcador (tela × portal).
- Tela no ar com banco indisponível passando a enviar por padrão: falha de
  leitura do controle tem de cair no comportamento de hoje (config), nunca em
  "ligado".

## Restrições e fatos do sistema

- Painel Flask em `painel_agentes/`, nível `total`; rota nova entra em
  `acesso_telas.TELAS`, no menu lateral com `niveis=` batendo com o
  `@requer_auth`, `@exige_mesma_origem` nos POST; templates com `url_for()`.
- Banco de produção `dados/dados.db` (SQLite, VPS). Transação curta (WAL; já
  houve trava por transação longa).
- Preferência do cliente: `preferencias_notificacao.py` (tabela
  `preferencias_notificacao`, PK `cnpj_embarcador`; colunas `emails`,
  `whatsapp` e uma por tipo: `nfs_em_rota`, `entrega_concluida`,
  `faltas_recebimento`, `resumo_diario`, `insucesso`, `pedidos_em_espera`,
  `agendamento`, `chamado_sem_resposta`). 44 embarcadores com linha hoje.
  Editada pelo cliente no portal e pela equipe em `/equipe`.
- Deduplicação por rotina: `notificacoes_enviadas (tipo, chave, enviado_em)`,
  `notificacoes_entrega`, `notificacoes_transportadora_enviadas`,
  `avisos_fora_dia_fixo`, `notificacoes_whatsapp`, `pedidos_area_notificada`.
- O portal (`portal_cliente/app.py`) carrega o config só na subida
  (`_CONFIG`); o controle novo é lido a cada envio, então mudar na tela vale
  na hora, sem restart.
- Transportadoras: o cadastro já está no banco (tabela `transportadoras`,
  tela `/transportadoras`, `CatalogoTransportadoras.carregar_do_banco`,
  commit bf6a667). "NÃO ENVIAR" vale **por ponto de redespacho** (linha),
  não pela transportadora inteira (e4c13f7). **Decisão minha:** a camada
  "por transportadora" da tela de notificações **é o mesmo dado** — a tela
  mostra o "NÃO ENVIAR" do cadastro (por ponto) e leva ao `/transportadoras`
  para mudá-lo (ajuste do plano, 24/10: um só lugar que grava); não existe
  segunda trava nem "liberado" que fure o NÃO ENVIAR. A exceção "liberado" (que fura
  o Piloto) vale para pontos sem NÃO ENVIAR.

### Envios cobertos e de onde cada um decide hoje

| Tipo (chave) | Rotina | Quando sai | Trava atual no config |
|---|---|---|---|
| nfs_em_rota | `notificar_nfs_em_rota.py` | timer 07h | mestra + `notificacao_nfs_em_rota.forcar_destino` |
| entrega_concluida | `notificacao_entregas/notificar_entrega_concluida.py` | timer frequente | `notificacao_entregas.{ativo,forcar_destino,embarcadores_piloto}` + mestra |
| resumo_diario | `resumo_diario_embarcador.py` | timer 20h | `resumo_diario_embarcador.{ativo,forcar_destino}` (ignora mestra) |
| insucesso | `expedir_pedidos.py` (pergunta de reenvio) | dentro da expedição | mestra |
| pedidos_em_espera | `notificar_pedidos_em_espera.py` | timer 08:20/15:20 | `notificacao_pedidos_em_espera.ativo` |
| agendamento (pendente, dia fixo) | `criar_rotas_diarias.py`, `incrementar_rotas.py`, `pipeline.py` | dentro das sequências | mestra |
| fora_dia_fixo | `roteirizacao/notificar_fora_dia_fixo.py` | dentro das sequências | `fora_dia_fixo.forcar_destino` + mestra |
| faltas_recebimento | `painel_agentes/wms_faltas_recebimento.py` | ao encerrar recebimento | `notificacao_faltas_recebimento.forcar_destino` + mestra |
| whatsapp_sem_resposta | `avisar_cliente_sem_resposta.py` | timer 2 min | `whatsapp_notificacoes.clientes.{ativo,forcar_destino}` |
| chamados | `portal_cliente/chamados.py` | na hora do evento | `portal_cliente.chamados.forcar_destino` |
| area_nao_atendida | `portal_cliente/bloqueio_area.py` | na hora do envio do XML | `portal_cliente.envios.forcar_destino` |
| ~~financeiro_dedicados~~ | `notificar_dedicados_financeiro.py` | dias 1 e 16 | `financeiro.forcar_destino` — **fora da tela** (só interno) |
| transportadoras | `notificacao_transportadoras/notificar_transportadoras.py` | timer 04h10 | mestra + `notificacao_transportadoras.ativo` |
| convite_embarcador | `embarcadores_stokki.py` | timer 07h10 | `cadastro_embarcador.forcar_destino_{email,whatsapp}` |

O builder confirma esta lista no código antes de planejar (pode haver envio a
mais; `notificar_area_nao_atendida` da roteirização hoje manda fixo pro Hugo
por código e fica fora da tela, salvo achado em contrário).

## Como funciona (decidido)

### Regra de decisão de um envio

Para cada envio a um destinatário, nesta ordem:

1. **Geral** desligado → não envia.
2. **Estado do tipo**: Desligado → não envia; Piloto → envia **só** para o
   destino de piloto (e-mail/WhatsApp do Hugo), com a faixa "iria para X" que
   as rotinas já usam; Ligado → segue.
3. **Exceção do destinatário** (embarcador ou transportadora) na tela:
   bloqueado → não envia; liberado → envia mesmo com o tipo em Piloto (é
   assim que se testa um tipo com poucos clientes antes de abrir).
4. **Preferência do cliente no portal** (`preferencias_notificacao`): se ele
   desmarcou, não envia. A tela é uma trava **por cima** da escolha do
   cliente, nunca a força.

Sem linha do tipo no controle novo → vale o config como hoje (migração sem
susto). Erro ao ler o controle → vale o config e loga aviso.

**O que não saiu, some (Hugo, 24/10).** Evento que caiu com o tipo Desligado,
o Geral desligado ou o destinatário Bloqueado é **consumido**: passar para
Ligado vale dali pra frente, nunca solta acumulado (mesmo comportamento de
hoje da entrega concluída com `ativo: false`, que grava IGNORADO). O Disparar
agora também não ressuscita o período desligado; ele trabalha sobre o que a
rotina enxerga no momento.

**Cópia de Piloto não conta como enviada ao cliente (Hugo, 24/10).** O envio
que vai para o Hugo no Piloto é registrado à parte; quando o destinatário é
liberado, ele recebe normalmente, e o "já recebeu hoje" do disparo manual só
mostra o que chegou ao cliente de verdade. O builder ajusta a deduplicação de
cada rotina para separar "enviado ao cliente" de "cópia de piloto".

**Escopo: só o que vai para fora (Hugo, 24/10).** Geral, estados e exceções
controlam apenas envios a **destinatários externos**: embarcador,
transportadora e futuro cliente (convite). E-mails à equipe interna, ao
motorista e ao financeiro **nunca** passam pela trava e continuam saindo
sempre, inclusive com o Geral desligado. Consequências:
- `chamados`: só o e-mail ao cliente é controlado; aviso à equipe e ao
  motorista seguem.
- `area_nao_atendida`: só o e-mail ao cliente; a cópia ao financeiro segue.
- `financeiro_dedicados` (só interno) **sai da tela** e fica no config.
- A função central recebe o tipo de destinatário ou só é chamada no ramo
  do destinatário externo.

### Tela (esboço A)

- Topo: interruptor geral (vermelho), com confirmação para ligar.
- Lista: uma linha por tipo — rótulo, estado (Desligado/Piloto/Ligado),
  resumo das exceções ("2 bloqueados", "liberado p/ 3"), horário ou "acompanha
  a rotina X", último envio.
- Clicar no tipo abre painel lateral com: estado; lista de embarcadores (ou
  transportadoras, no tipo transportadoras) com busca e liberar/bloquear;
  botão Disparar agora; (peça 2) horário.
- Detalhe do embarcador (a partir da lista ou de uma busca): e-mails de
  notificação (mesma lista que o cliente edita no portal), WhatsApp, situação
  de cada tipo para ele; (peça 3) frequência por tipo.
- Passar para **Ligado** pede confirmação mostrando quantos destinatários
  vão receber.
- Histórico de mudanças com "desfazer" (volta ao valor anterior).

### Disparar agora

- Abre janela perguntando **para quem**: só para mim (teste) · todos os
  liberados naquele tipo · um embarcador/transportadora escolhido.
- Mostra quem **já recebeu hoje** ("enviado hoje às 07:02") e deixa marcar
  para reenviar; por padrão quem já recebeu fica desmarcado.
- Roda a rotina daquele tipo com esse alvo, em segundo plano; a tela mostra
  o resultado (enviados, pulados, falhas).
- Só tipos que têm rotina própria disparável (ver tabela). Para os que saem
  dentro de outra rotina ou no evento (insucesso, agendamento, chamados,
  área), o disparo manual não se aplica ou reaproveita o botão manual que já
  existe (ex.: insucesso na Torre) — decisão do builder, registrada no plano.

## Peças e ordem

### Peça 1 — tela e travas (construir primeiro)

Geral, estado por tipo, exceções por embarcador e por transportadora, regra
de decisão plugada em todas as rotinas da tabela por **uma função central**
(ex.: `controle_notificacoes.decidir(tipo, destinatario) -> envia/piloto/nao`),
disparo manual, edição de e-mails do embarcador, histórico. Ao terminar, o
Hugo liga os envios pela tela, sem SSH.

### Peça 2 — horários na tela

Um relógio único (timer systemd que roda a cada poucos minutos) dispara cada
envio quando chega o horário salvo na tela, substituindo os horários fixos
dos timers de notificação. O painel nunca mexe em systemd. Envios que saem
dentro de outra rotina ou no evento mostram "acompanha a rotina X" sem campo.

### Peça 3 — frequência por cliente com resumo agrupado

Por embarcador e por tipo: Normal · no máximo X vezes por dia nos horários
escolhidos · só em alguns dias da semana. Para envios **um por pedido**
(entrega concluída, faltas…) a frequência reduzida vira **resumo
agrupado** nos horários do cliente: o que sair fora do horário entra numa fila
e vai junto no próximo resumo. Cada tipo afetado ganha uma versão "resumo".

**Decisão minha, a confirmar com o Hugo ao planejar a peça 3:** insucesso e
agendamento pedem decisão do cliente com horário de corte nas rotinas do dia,
então ficam **fora** da frequência reduzida (sempre saem na hora). Se a
frequência vale para WhatsApp também fica para essa conversa.

**Plano agora cobre só a peça 1.** Peças 2 e 3 ganham plano próprio depois;
a peça 1 não precisa preparar a fila da peça 3.

## Deixado para o builder

- Nomes e esquema das tabelas (controle por tipo, exceções, histórico, fila
  da peça 3), respeitando transação curta.
- Como cada rotina chama a função central (adaptar sem mudar o conteúdo dos
  e-mails); destino de piloto (sugestão: hoje é hugo@ / número do Hugo nos
  defaults; pode virar campo na tela).
- Como o disparo manual roda (thread no painel × unit oneshot existente com
  argumento), garantindo que não trava o painel e não roda duas vezes junto.
- Testes: a função central (cada combinação da regra), cada rotina plugada
  (desligado não envia, piloto vai pro Hugo, liberado passa no piloto,
  bloqueado não passa, preferência do cliente respeitada), fallback pro config
  sem linha e com banco com erro.

## Calls que fiz (o Hugo confere)

- Estado de três níveis (Desligado/Piloto/Ligado) em vez de liga/desliga.
- "Liberado" fura o Piloto para aquele destinatário (é o piloto com poucos
  clientes).
- A tela fica por cima da preferência do cliente; nunca liga o que o cliente
  desligou no portal.
- Sem linha no controle novo → config de hoje (migração gradual).
- Relógio único na peça 2 em vez de o painel editar timers.
- Bloqueio por transportadora = o mesmo "NÃO ENVIAR" do cadastro, por ponto.
- Insucesso e agendamento fora da frequência reduzida (peça 3, a confirmar).
- Plano agora só da peça 1.

Decididos pelo Hugo em 24/10: o que não saiu some (sem acumulado); cópia de
piloto não conta como enviada ao cliente; a tela só controla destinatários
externos (equipe, motorista e financeiro sempre recebem).
