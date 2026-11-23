# "Em espera" a pedido do cliente: pausa de verdade e retomada pelo portal

Data: 25/10/2026 (revisado 27/10: nada na Stokki) · Pedido do Hugo · Status: design para aprovação
Continua: `2026-10-24-aviso-pendencias-painel-design.md` (seção 7) — hoje o tipo `em_espera` da tela `/solicitacoes-portal` está **desligado** (`EM_ESPERA_HABILITADO = False` em `portal_cliente/solicitacoes_execucao.py`).

## 1. Intenção

O cliente pede no portal para tirar um pedido da rota e segurar ("em espera"). A operação executa pela tela `/solicitacoes-portal`: o sistema tira o pedido da rota na Vuupt e anota numa tabela nossa que ele está segurado. O pedido **fica parado de verdade** — nenhuma roteirização automática o recoloca em rota — até **o próprio cliente retomar pelo portal**, escolhendo **Agendamento** (data e janela) ou **Próxima rota** (regra normal do dia fixo da região).

Por que estava desligado: o executor apertava "Pausar" na Stokki, e na Stokki "Em espera" é também a fila de entrada da Fresh Log (a Estação de Impressão soltaria o pedido em ~1 h). Com a decisão de 27/10 (nada na Stokki), esse conflito some.

## 2. Decisões do Hugo

- **(a) Pausa de verdade**, respeitada pelas rotinas (não uma marca paralela).
- **Quem solta: o cliente, pelo portal.**
- **Ao retomar:** o cliente escolhe **Agendamento** ou **Próxima rota**.
- **27/10: nada na Stokki relacionado a pausas.** Nem Pausar, nem Reabrir. A Stokki segue o fluxo normal dela; o "segurado" vive só na nossa tabela e na Vuupt (fora de rota).

## 3. Como saber que um pedido está pausado

Fonte da verdade: tabela nossa no `dados.db`, `pedidos_pausados_cliente` (código PS, id Stokki, envio_id, service_ids, pausado_em, retomado_em, motivo do fim). Pausado = linha aberta (sem `retomado_em`).

- **Entra** quando a operação executa o "em espera" com sucesso (tirou da rota na Vuupt).
- **Sai** quando o cliente retoma com sucesso ou cancela o pedido.

## 4. O que muda

1. **Executar "em espera"** na tela `/solicitacoes-portal`: ligar `EM_ESPERA_HABILITADO`; o executor só tira da rota na Vuupt (`expedicao.excluir_pedido_da_rota`) e abre a linha da tabela. Sai o passo de pausar na Stokki (e o módulo `stokki/pausar.py`, que só ele usava). A mensagem ao operador passa a "Pedido segurado até o cliente retomar pelo portal". O envio no portal vai para o estado novo `EM_ESPERA_CLIENTE`.
2. **Roteirização não usa pedido pausado:** os três pontos que leem o pool (`nucleo.pool.listar_pool_not_assigned`): `criar_rotas_diarias` (rascunho das 16h), `incrementar_rotas` (18h/22h) e o "roteirizar selecionados" do Planejamento deixam de fora serviços com linha aberta. A tela de Planejamento continua mostrando o pedido, marcado "em espera (cliente)".
3. **Portal — retomar:** o envio em `EM_ESPERA_CLIENTE` aparece como "Em espera, a seu pedido" com botão **Retomar entrega**:
   - **Próxima rota:** se a região tem dia fixo, aplica na Vuupt a próxima data válida a partir de amanhã (`regioes_dia_fixo.proxima_data_valida`); senão não mexe na Vuupt (o pool trata pela regra normal). Não se manda data vazia pra Vuupt (nunca testado).
   - **Agendamento:** data e janela, mesmas regras do reagendamento do portal, aplicada pelo caminho do cliente (o mesmo do executor de reagendar).
   O pedido vira `RETOMANDO`; o worker do portal aplica a data na Vuupt (se houver) e fecha a linha → envio volta a `CRIADO`. Falha na Vuupt → nada mudou, volta a `EM_ESPERA_CLIENTE` e avisa a operação (e-mail interno que o worker já usa); o cliente pode tentar de novo.
4. **Aviso de pendências:** nenhum aviso novo.

## 5. Fora do escopo / não pode acontecer

- **Fora:** qualquer ação na Stokki ligada à pausa; Estação de Impressão, cobrança de pedidos em espera e vigia da fila seguem exatamente como hoje (tratam a Stokki, que não muda); prazo máximo de pausa ou lembrete ao cliente; retomar pela operação.
- **Fracasso mesmo funcionando:**
  - um pedido pausado entrar numa rota automática;
  - o cliente retomar e a data não ser aplicada sem ninguém saber.

## 6. Ainda não verificado ao vivo

- PUT de data na Vuupt em serviço `not_assigned` recém-desatribuído.
Prova com o Hugo num pedido de teste: executar em espera → rodada de roteirização em `--modo-teste` mostra o pedido pulado → cliente retoma (agendamento) → data na Vuupt, linha fechada, envio `CRIADO`.

## 7. Decisões minhas (o Hugo pode mudar)

- Fonte da verdade = tabela `pedidos_pausados_cliente`.
- Como a Stokki não muda, a Estação pode imprimir/avançar o pedido e a cobrança pode cobrar NF de um pedido segurado que ainda esteja na fila de entrada: aceito, porque são o fluxo normal da Stokki e não colocam o pedido em rota.
- **Quais pedidos podem ser pausados:** os mesmos limites do executor — rota em andamento ou Lalamove é recusado com o motivo; a operação pode recusar.
- **Em `EM_ESPERA_CLIENTE` o cliente ainda pode cancelar** (worker de cancelamento atual; a linha é fechada). O reagendar comum some nesse estado; quem reagenda é o "Retomar → Agendamento".
- Nenhuma mensagem nova ao cliente por e-mail/WhatsApp (só o portal).
