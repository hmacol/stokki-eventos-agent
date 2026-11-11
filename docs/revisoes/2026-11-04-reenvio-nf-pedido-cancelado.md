# Revisao de seguranca: reenvio da NF de pedido cancelado fora do portal (04/11)

Escopo: portal_cliente/envio_pedidos.py (validar_item + _pedido_cancelado), teste novo.

- Entrada: nenhuma nova. `_pedido_cancelado` recebe `codigo_pedido` lido de portal_envios
  (gravado pelo worker a partir da Stokki), consulta parametrizada (sem SQL dinamico).
- Autorizacao: a checagem emitente == empresa logada continua antes e independe da regra
  nova; quem nao e o emitente da NF segue com erro. Nao abre reenvio de NF de terceiro.
- Duplicidade: liberacao so quando pedido + reentregas (-R/-C) estao CANCELADO no nucleo;
  se a Stokki ainda tiver o pedido ativo ela recusa a chave ("ja utilizada") -> DUPLICADO.
- Falha da consulta (tabela ausente) -> bloqueia como antes (fail closed).

Achados: nenhum ALTO/MEDIO. BAIXO: aviso "sera reenviado" pode nao se cumprir se o
pedido estiver cancelado so na Vuupt (Stokki devolve DUPLICADO; sem efeito colateral).
