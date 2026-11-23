# Revisão: alteração de entrega aplicada sozinha pelo portal (12/11)

Diff revisado: `origin/master` (d555dc6) .. ramo `alteracao-automatica`, arquivos de `portal_cliente/`.
Rota afetada: `POST /cliente/api/pedidos/<id>/alteracao` (volta a gravar na Vuupt; até aqui abria chamado).

## Conferido

- Dono do pedido: `_servico_alteravel` devolve 404 se o `sender_id` do serviço não é do login ou se a chave está desligada para a empresa.
- CSRF: `@exige_mesma_origem` no POST. Equipe só altera nos níveis que enviam (`_exige_pode_enviar`).
- A avaliação (cancelado, entregue, rota iniciada, retirada, Lalamove) é refeita no POST; nunca confia no GET.
- Entrada passa por `alterar_entrega.validar` antes de qualquer chamada externa; avisos exigem `confirmar` explícito.
- Tela: todo texto vindo do servidor passa por `esc()`.
- WhatsApp ao grupo: texto montado só com código do pedido, nome da empresa e rótulos fixos, cortado em 200 caracteres.

## Achados

- ALTO: nenhum.
- MÉDIO (antigo, aceito pelo Hugo em 12/11 ao escolher o escopo "endereço, data e horário"): o horário de recebimento vale para o destinatário em todos os embarcadores. Um cliente muda a janela de um destinatário que outro cliente também atende. A tela avisa isso no campo.
- BAIXO: a checagem de área falha aberta (erro na consulta deixa o endereço passar), mesma regra do envio de pedidos.
- BAIXO: sem limite de quantas alterações o mesmo login faz por pedido; cada uma gera chamada à Vuupt e uma mensagem no grupo (o teto diário do WhatsApp segura o grupo).
