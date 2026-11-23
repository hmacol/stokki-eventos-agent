# Revisão de segurança: devolução de estoque no cancelamento (PR #23)

Data: 16/11/2026. Escopo: `git diff origin/master...origin/devolucao-estoque-cancelamento` nos módulos
expostos (`portal_cliente/cancelamento.py`, `portal_cliente/solicitacoes_execucao.py`,
`portal_cliente/enviar_stokki.py`) e no gancho de `painel_agentes/acoes_pedidos.py`.

## Resultado

Nenhum achado ALTO ou MÉDIO.

## O que foi conferido

- **Superfície nova:** nenhuma rota, formulário ou parâmetro novo. Os três ganchos só chamam
  `devolucao_estoque.registrar` depois de um cancelamento que já passou pelos controles de acesso existentes.
- **De onde vem o id do pedido:** do registro do envio no banco (portal), da solicitação (equipe) ou do
  pedido resolvido pelo painel. Nada vem direto do navegador para a fila.
- **Um cliente mexer no estoque de outro:** o plano usa o `cliente_id` lido do próprio pedido na Stokki e só
  move o saldo que o Bloqueio daquele pedido prendeu. O cliente só cancela pedido dele (regra anterior ao PR).
- **SQL:** consultas novas com parâmetros (`stkkc_id = ?`, `id_stokki = ?`). O único `f-string` em SQL
  (`gravar`) monta nomes de coluna fixos do código.
- **HTML do e-mail à operação:** todo valor passa por `html.escape`.
- **Falha do gancho:** exceção vira log e o cancelamento segue; a fila nunca devolve erro ao cliente.
- **Chave desligada:** `registrar` e `processar_devolucoes_do_ciclo` saem antes de tocar banco ou Stokki.

## BAIXO

1. Nome e SKU do produto (texto cadastrado pelo embarcador na Stokki) vão para a mensagem do grupo de
   WhatsApp do galpão e para o log sem filtro de caracteres de controle. Efeito: texto estranho numa mensagem
   interna. Sem correção agora.
2. `stokki/amostras_devolucao/enderecos_2278.html` é a página inteira da Stokki e traz nome e e-mail do Hugo
   (repositório privado). Reduzir a amostra numa próxima passada.
