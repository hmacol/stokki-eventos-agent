# Revisão de segurança: BAIXO 3 e 5 do endereço de entrega (12/11/2026)

Diff: ramo `endereco-baixos` sobre origin/master 25debd4. Arquivos: `portal_cliente/endereco_entrega.py`
e o teste. Fecha os itens 3 e 5 de `2026-10-30-endereco-entrega-seguranca.md`.

## Mudanças

1. `montar_endereco` trata `campos` que não é dict como vazio. O resultado é "CEP não encontrado"
   (400) em vez de `AttributeError` (500).
2. Logradouro, número, complemento e bairro digitados pelo cliente passam por `_texto`, que tira
   `\x00-\x08`, `\x0b`, `\x0c`, `\x0e-\x1f` e surrogate solto (`\ud800-\udfff`) antes do `strip`
   e do teto de 60 caracteres.

As duas rotas que chamam a função ganham a correção: o confirmar do envio
(`envio_pedidos._resolver_entrega` e `avisos_fora_dia_fixo`) e o "Pedir alteração"
(`alterar_entrega.validar`).

## Achados

| # | Nível | Onde | Achado |
|---|-------|------|--------|
| 1 | info | `_texto` | U+FFFE e U+FFFF também não valem em XML 1.0 e não entram no filtro. Só derrubam o pedido do próprio cliente, como o item 3 original. |
| 2 | info | `montar_endereco` | Rua e bairro vindos do ViaCEP (quando o cliente deixa em branco) não passam pelo filtro. A fonte é externa, mas é a mesma que já preenche a cotação. |
| 3 | info | `_texto` | Tab e quebra de linha continuam (são XML válido). Ficam no texto do endereço. |

Nenhum ALTO, MÉDIO ou BAIXO novo. Não houve mudança de rota, sessão, permissão nem template.

## Testes

3 testes novos em `test_endereco_entrega` (controle/surrogate somem e o XML gerado é lido pelo
ElementTree; campo só com controle pede o campo; valor não-dict vira erro de CEP).
Suíte do portal: 570 passaram. As 2 falhas antigas (`test_cotacao::test_oculta...` e
`test_grupos::...cache_e_invalidacao...`) também falham no origin. Os 15 erros são por falta de `config.yaml` na worktree.
