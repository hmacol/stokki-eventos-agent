# Revisão de segurança: limite da consulta de CEP do portal (06/11/2026)

Diff: ramo `limite-cep` sobre origin/master 7c51d5b. Arquivos: `portal_cliente/app.py`,
`portal_cliente/cotacao.py`, `templates/_envios.html`, `templates/acompanhamento.html`, testes.

Fecha o item 2 (MÉDIO) da revisão de 30/10 (`2026-10-30-endereco-entrega-seguranca.md`),
partes (2) limite por cliente e (3) teto do `_CACHE_CEP`. A parte (4), timeout menor, não foi feita.

## Mudanças

1. `GET /api/envios/cep/<cep>`: teto de 30 consultas por minuto por CNPJ da sessão, em memória
   com trava (`threading.Lock`). Acima do teto, 429 sem chamar ViaCEP ou BrasilAPI.
2. A mesma rota abre quando `alteracao_entrega` está ativa pra alguma empresa do login
   (a janela "Pedir alteração" do acompanhamento já chamava essa rota e recebia 404).
3. `cotacao._CACHE_CEP` é limpo quando chega a 5000 entradas.
4. As duas telas mostram a mensagem do 429 em vez de "CEP não encontrado".

## Achados

| # | Nível | Onde | Achado |
|---|-------|------|--------|
| 1 | BAIXO | `app.py` `_cep_acima_do_limite` | O limite é por processo e zera no restart. Com um processo só do waitress, isso é aceitável. Se um dia rodar com vários workers, o teto real multiplica. |
| 2 | BAIXO | `app.py` | Equipe entrando como cliente usa a mesma cota do CNPJ do cliente. Não abre acesso, só pode fazer o cliente ver 429 antes. |
| 3 | info | `cotacao.py` | Limpar tudo ao chegar no teto (em vez de LRU) faz rajadas de miss depois da limpeza. Com o limite por cliente, o efeito é pequeno. |
| 4 | info | rota de CEP | A abertura nova vale só pra login com `alteracao_entrega` ativa (hoje, só o piloto do cliente teste). Continua com `@requer_cliente` + `_exige_pode_enviar`. É GET sem efeito colateral e sem CORS. |

Sem XSS: a mensagem do 429 é constante do servidor e vai por `esc()` (`_envios.html`) e
por `textContent` (`acompanhamento.html`). Sem IDOR: a chave do limite e a checagem da
alteração usam só `g.cliente` da sessão. Nenhum ALTO ou MÉDIO.

## Testes

`portal_cliente.test_endereco_entrega_api` e `test_endereco_entrega`: 27 OK (5 novos).
Suíte do portal: 538 passaram. As 2 falhas (`test_cotacao::test_oculta_do_cliente_equipe_continua_vendo`
e `test_grupos::...test_cache_e_invalidacao...`) também falham no origin/master. Os 15 erros são por falta de `config.yaml` na worktree.
