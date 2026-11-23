# Aviso de pendências no topo das telas do painel

Data: 24/10/2026 · Pedido do Hugo · Status: design para aprovação
Escopo: **painel interno** (`painel_agentes/`). O aviso equivalente do **portal do cliente** é um projeto separado, depois deste.

## 1. Intenção

Pendências ficam esquecidas porque só aparecem como um número no menu ou dentro de uma tela específica. O Hugo quer que **cada pessoa seja cobrada em qualquer tela que estiver usando**: uma faixa insistente no topo, para cada tipo de pendência que cabe ao perfil dela, com a opção de ir resolver (**Verificar**) ou adiar conscientemente (**Ignorar**, com confirmação).

## 2. Objetivos

1. Uma faixa por tipo de pendência, no topo de **todas** as telas do painel que usam `base.html` ou `base_mobile.html`.
2. Cada perfil vê só as pendências que ele resolve (operador, logística, atendimento, galpão, expedição; o Hugo, nível `total`, vê todas).
3. **Verificar** leva à tela que resolve aquela pendência.
4. **Ignorar** pergunta "Ignorar este aviso? Os N itens continuam esperando. O aviso volta em 30 min." e, confirmado, esconde **aquela** faixa por **30 minutos para aquela pessoa**. Depois volta se a pendência continuar.
5. A faixa some sozinha quando a pendência zera.
6. Atualiza junto com os contadores do menu (a cada 2 min).

## 3. Fora do escopo / não pode acontecer

- **Fora:** o aviso do portal do cliente (projeto seguinte); notificação push no celular; e-mail/WhatsApp.
- **Fracasso mesmo funcionando:**
  - deixar a tela lenta ou travar enquanto conta pendências (Torre e Pedidos Parados dependem da Vuupt e levam até ~1 min);
  - cobrar uma pessoa por algo que ela não pode resolver (perfil errado);
  - o aviso cobrir a tela a ponto de atrapalhar o trabalho (por isso o limite de 3 faixas);
  - manter a sessão viva para sempre (a busca periódica não pode renovar o tempo de inatividade de 30 min).

## 4. Aparência (decidido pelo Hugo: opção A)

- Uma faixa por tipo: ícone de alerta, texto com a quantidade em negrito ("**12 chamados** esperando a Fresh Log"), botões **Verificar** (azul) e **Ignorar** (contorno).
- Até **3 faixas visíveis**; o restante recolhido numa linha "+ N pendências · ver todas" que expande a lista.
- Ordem por urgência (seção 6); a mais urgente em vermelho, as demais em laranja.
- Esboço aprovado: `.superpowers/brainstorm/53141-1792886123/content/aviso-pendencias.html` (opção A).
- Alternativas guardadas a pedido do Hugo, caso a A fique pesada: **B** (faixa única com uma etiqueta por tipo) e **C** (uma pendência por vez, com setas).

## 5. Pendências que entram (decidido pelo Hugo: todas)

Levantamento de 24/10. "Quem" segue os níveis do painel; o nível `total` vê todas.

| # | Pendência (texto da faixa) | Quem | Fonte (já existe) | Verificar → |
|---|---|---|---|---|
| 1 | N chamados esperando a Fresh Log | atendimento, operador, logística | `contadores_menu._contar_atendimento` / `chamados.contagens_fila` (inclui motoristas e envios bloqueados) | `/atendimento` |
| 2 | N pedidos do portal com cancelamento/reagendamento/reenvio pendente | operador | `envio_pedidos.listar_solicitacoes_pendentes` | **tela nova** (seção 7) |
| 3 | N pedágios para aprovar | operador, logística | `_contar_pedagios` | `/financeiro/pedagios` |
| 4 | N canhotos reprovados pela IA para revisar | operador, logística | `_contar_canhotos` | `/canhotos` |
| 5 | N entregas sem canhoto | operador, logística | `nucleo/canhotos_manuais.pendentes(...)["total"]` (60 dias, como a tela) | `/canhotos-pendentes` |
| 6 | N transportadoras desconhecidas | operador, logística | `transportadoras_tela.contar_pendentes` | `/transportadoras` |
| 7 | N Clientes AGENDA aguardando decisão | logística | `marcar_clientes_agenda.listar(situacao='PENDENTE')` | `/clientes-agenda` |
| 8 | N pedidos parados sem classificação | operador, logística, atendimento | `_contar_pedidos_parados` (cache, Vuupt) | `/pedidos-parados` |
| 9 | N ocorrências na fila da Torre | operador, logística, atendimento | `_contar_torre` (cache, Vuupt) | `/torre` |
| 10 | N retornos esperados não recebidos | galpão, expedição, operador, logística | `retornos.listar_pendentes` (somar itens) | `/retornos` |
| 11 | N rascunhos de rota com erro de envio | operador, logística | `rascunhos_rota` status `ERRO_ENVIO` | `/planejamento` |
| 12 | N divergências do batimento sem tratar | operador | `batimento/consulta.fechamento()["total_abertas"]` | `/vigia?aba=fechamento` |
| 13 | N pedidos com prazo vencido (Vigia) | operador | `vigia/consulta.listar()["vencidos"]` | `/vigia` |
| 14 | N cadastros de embarcador para aprovar | total (ou login com `aprovar_embarcadores`) | `cadastro_embarcador` situação `PREENCHIDO` | `/inicio` |
| 15 | N motoristas novos para aprovar | total | `cadastro_motorista.contar_pendentes(so_completos=True)` | `/motoristas` |
| 16 | N rotinas falharam nas últimas 24 h | total | `pagina_inicial.montar_rotinas` | `/historico` |
| 17 | N itens do WMS com pendência/falta | galpão, operador | `wms_pedidos.pendencias` | `/wms/estoque` |
| 18 | N recebimentos com divergência sem aviso | galpão | `wms_faltas_recebimento.pendentes_de_envio` | `/wms` |

Itens do levantamento que já estão contidos em outros (chamados de motorista e envios bloqueados por área, dentro do #1) não ganham faixa própria.

## 6. Ordem e cor (decisão minha — o Hugo pode mudar)

Vermelho e no topo: **alguém de fora está esperando** — #1 chamados, #2 solicitações do portal, #10 retornos (motorista esperando acerto), #14/#15 cadastros. Depois, laranja, na ordem da tabela. A ordem é **fixa** (lista única no código, fácil de mudar) — não reordena pela quantidade, para as faixas não pularem de lugar a cada atualização.

## 7. Tela nova: solicitações do portal, executando a ação (decidido pelo Hugo: opção B)

Hoje o cliente pede no portal e, quando o pedido já está na Stokki, a solicitação fica `portal_solicitacoes.status='PENDENTE'` e só é fechada por linha de comando (`portal_cliente/gerenciar_clientes.py` → `envio_pedidos.concluir_solicitacao`, que **só marca**). O Hugo decidiu que o botão da tela **executa a ação de verdade**.

Tela `/solicitacoes-portal` (operador, logística, total): lista das pendentes (embarcador, pedido PS, tipo, quando, detalhe/motivo, situação atual na Stokki/Vuupt) com, por linha, **Executar** e **Recusar** (Recusar pede uma resposta curta pro cliente e chama `concluir_solicitacao(..., recusada=True)`).

O que **Executar** faz, por tipo (`envio_pedidos.TIPOS_SOLICITACAO`):

| Tipo | Quando vira pendência | Executar |
|---|---|---|
| `cancelar` | o worker do portal devolveu pra operação (ex.: motorista já saiu) | tenta de novo o cancelamento completo pelo mesmo caminho do worker (Vuupt `PUT /cancel` + Stokki `outbound/cancel`, já provados); se a Vuupt ainda recusar (rota em andamento), mostra o motivo e não conclui |
| `em_espera` | pedido já criado na Stokki | tira o pedido da rota na Vuupt (desatribuir, como o "Excluir da rota" da Expedição — não cancela) **e** coloca o pedido "Em espera" na Stokki (botão "Pausar" da página do pedido; endpoint a sondar só leitura antes, como foi feito com o cancelamento) |
| `reagendar` | pedido já criado na Stokki | aplica a nova data/janela no serviço da Vuupt pelo caminho que já existe para agendamento (`adicionar_agendamento` / reagendamento de feriado) |
| `reenviar` | não gera pendência hoje (resolve no portal) | — |

Depois de executar com sucesso: `concluir_solicitacao(..., resposta=<texto automático>)`, que o cliente vê no portal. Cada execução respeita a trava da sessão da Stokki (`sessao_uso`) e roda com `--modo-teste` equivalente nos testes. Sem envio de e-mail/WhatsApp novo nesta entrega.

Essa tela é a maior parte do trabalho e mexe em Stokki e Vuupt; o plano deve entregá-la **depois** do aviso, em tarefas próprias, começando pela sonda do "Pausar".

## 8. Restrições

- Seguir `CLAUDE.md` e `MAPA_DO_SISTEMA.txt` (atualizar no mesmo commit). Português.
- Um partial incluído em `base.html` e `base_mobile.html` (padrão do menu). `login.html` e `wms.html` não estendem base: `wms.html` (PWA do galpão) fica de fora nesta versão.
- Aproveitar `GET /api/sidebar/contadores` e o polling de 2 min do `_menu_lateral_script.html`; não criar um segundo polling. As chamadas periódicas **não** renovam a sessão (já é assim hoje).
- Pendências caras (Torre, Pedidos Parados) usam o último valor do cache existente; nunca bloquear a requisição.
- Logins personalizados (`/acessos`): o perfil vê a faixa só se tiver acesso à tela de destino (mesma regra dos contadores, `_niveis_por_contador`).
- Rotas novas entram em `acesso_telas.TELAS` (ou `FORA_DO_CATALOGO`) e no menu com `niveis=` igual ao da rota.

## 9. Decisões minhas (o Hugo pode mudar)

- **"Ignorado" guardado no servidor, por pessoa e por tipo de pendência**, com hora de expiração (tabela pequena no `dados.db`). Pessoa = login nominal (`usuarios_extras`), valendo em qualquer computador/celular dela. Em login compartilhado por nível (sem nome), a chave é o **aparelho** (cookie próprio, longo, sem dado pessoal) — um colega ignorar não silencia a faixa para os outros.
- **Mais de 30 min:** valor numa constante (`IGNORAR_MINUTOS = 30`), sem tela de configuração.
- **Quando a pendência cresce durante o ignorar:** as **vermelhas** (seção 6) **voltam na hora** se a quantidade subir (decidido pelo Hugo); as laranjas continuam escondidas até os 30 min. Para isso o ignorar guarda a quantidade do momento em que foi ignorado.
- **Faixa não aparece** na própria tela que resolve aquela pendência: some quando o caminho atual é o destino do Verificar (sem a query). Na `/vigia` somem #12 e #13; no `/inicio` some #14; nas demais telas todas aparecem.
- **Quem é cobrado:** os níveis da tabela da seção 5 mandam (acrescentar `galpao` e `expedicao` onde a tabela lista). O nível `leitura` **não** é cobrado (não resolve nada). Login personalizado (`/acessos`) só vê a faixa se também tiver acesso à tela de destino; a #14 segue a permissão `aprovar_embarcadores`.
- **Contadores caros (#8, #9):** usam o cache existente; logo depois de reiniciar o painel podem levar um ciclo (2–5 min) para aparecer — aceitável, sem aquecimento. Outras contagens que se mostrarem lentas entram no mesmo esquema de cache.
- **Celular (`base_mobile.html`):** mesmas faixas, empilhadas, botões menores; limite de 2 visíveis.

## 10. O que fica com o builder

- Formato exato da resposta de `/api/sidebar/contadores` (estender o que existe ou endpoint irmão), desde que continue um único polling.
- Como contar #5, #7, #10, #11, #12, #13, #17, #18 de forma barata (SQL de contagem em vez de listar tudo).
- Estilo visual dentro do padrão do painel (cores e fontes já usadas no menu).
