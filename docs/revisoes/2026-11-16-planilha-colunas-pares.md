# Revisão de segurança: planilha do cliente (indicar colunas + pares Sku N / Quantidade N)

- Data: 16/11/2026
- Diff: `origin/master...mapear-colunas-planilha` em `portal_cliente/` (commits 964c7a5, 73e7e30 e o da correção abaixo)
- Produção antes do deploy: c6b2a80
- Arquivos: `app.py` (rota `api_envios_analisar`), `envio_pedidos.py` (`ler_planilha`, `_pares_de_itens`, `_colunas_pelos_nomes`, mapa por login), `templates/_envios.html` (janela de colunas)
- Risco do módulo: MÉDIO (rota autenticada, entrada nova do cliente, estado novo por login; não toca login, PIN nem sessão)

## Resultado

Nenhum achado ALTO ou MÉDIO. Dois BAIXOS corrigidos antes do deploy, um BAIXO aceito.

## O que foi conferido

| Ponto | Conclusão |
|---|---|
| Autenticação | A rota já exigia login; nenhuma rota nova. |
| Separação entre clientes | O mapa de colunas é lido e gravado só pelo CNPJ do próprio login (`g.cliente["cnpj"]`), nunca por valor vindo do formulário. A checagem "empresa faz parte do acesso" do login de grupo não mudou e vale igual na planilha de pares (teste `test_pares_com_problema...`). |
| Entrada nova `colunas` (JSON) | Só aceita dicionário; linha do cabeçalho entre 0 e 14; índice de coluna dentro do cabeçalho; chave fora da lista de campos é ignorada; qualquer outra coisa vira "colunas indicadas inválidas" (400 tratado, sem 500). |
| Entrada nova `indicar` | Só liga a janela; não muda o que é gravado. |
| Saída na tela | Nome do arquivo, nomes de coluna e exemplos passam por `esc()`; `indice`, `linha` e `pares` são inteiros gerados no servidor. Sem `innerHTML` de texto do cliente sem escape. |
| SQL | `portal_mapa_planilha` só por parâmetros. |
| Arquivos | Nenhum caminho novo em disco; temporários iguais aos de antes. |

## Achados

### BAIXO 1 (corrigido): planilha de pares podia gerar milhões de itens
`envio_pedidos.py`, `ler_planilha`. Antes eram no máximo 2.000 itens (uma linha por item). Com pares, um arquivo com milhares de colunas `SkuN`/`QuantidadeN` em 2.000 linhas chegaria a milhões de itens em memória e no JSON temporário. Só cliente logado alcança.
Correção: `MAX_ITENS_PLANILHA = 10000`; acima disso a planilha é recusada com mensagem. Teste: `test_pares_tem_teto_de_itens...`.

### BAIXO 2 (corrigido): número enorme no cabeçalho derrubava a leitura
`envio_pedidos.py`, `_RE_PAR_SKU` / `_RE_PAR_QTD`. Cabeçalho `Sku` seguido de mais de 4.300 dígitos fazia `int()` levantar `ValueError` (limite do Python 3.11) e a rota respondia 500.
Correção: o padrão só aceita de 1 a 3 dígitos. Mesmo teste acima.

### BAIXO 3 (aceito): tamanho do mapa guardado
O mapa guarda o nome normalizado de cada coluna indicada, sem cortar. São no máximo 22 campos por login e o cliente só afeta a própria linha; o tamanho do upload já é limitado pela rota.

## Mudança de comportamento (não é achado)

`_colunas_pelos_nomes`: campo guardado cujo nome não existe na planilha volta a ser procurado pelos apelidos conhecidos (antes ficava "fechado" e a janela reabria). Campo guardado como "não tem" continua sem busca.

## Cobertura e limites

- Testes: suíte `portal_cliente` com 669 passando; 2 falhas de ambiente do worktree (sem banco local), iguais às de antes da mudança.
- A variação de pares não foi clicada em navegador (a tela só ganhou uma frase); a janela em si foi clicada em Chromium no commit 964c7a5.
- Não passou pela homologação com a parte de pares: decisão do Hugo ("subir direto em prod").
- `enviar_stokki.executar_wizard_excel` (criação do pedido por planilha na Stokki) continua sem nunca ter rodado em produção; fora deste diff.
