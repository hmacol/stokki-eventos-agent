# Revisão de segurança: calculadora liberada pro cliente teste (14/10/2026)

Diff: `a062430..28a3c1b -- portal_cliente/` (commit dcb8047).
Mudança: `portal_cliente.cotacao.cnpjs_liberados` (padrão `["00000000000191"]`)
faz `_visivel()` liberar a calculadora pra esse CNPJ logado, mesmo com
`visivel_cliente: false`.

## Achados

**MÉDIO — login teste vira porta pra calculadora inteira.**
O CNPJ do cliente teste (00.000.000/0001-91) é óbvio e o PIN é 123456 (mesmo
problema do A1 da revisão de 07/10). Quem souber disso pode:
- chamar `/api/cotacao/calcular` em loop (cada chamada consulta a Google Routes
  API, que é cobrada; `max_paradas` 25 limita só o tamanho, não a frequência);
- mandar proposta (`/api/cotacao/<id>/proposta`) com PDF pra e-mails
  arbitrários, com remetente Fresh Log. Hoje contido só se
  `portal_cliente.cotacao.forcar_destino` continuar preenchido na VPS
  (estava `hugo@` desde 11/09; não conferido hoje).
Correção sugerida: trocar o PIN do cliente teste por um forte, ou limitar
taxa nas rotas da cotação.

## Sem achado
- A verificação usa `g.cliente["cnpj"]` vindo da sessão (`requer_cliente` roda
  antes de `exige_visivel`); não há entrada do usuário no gate.
- Cotações continuam filtradas por CNPJ em `ct.buscar(conn, id, cnpj)`.
- Aceite público por token não mudou.
- Botão da Torre é só link (`target=_blank rel=noopener`), sem dado novo.
