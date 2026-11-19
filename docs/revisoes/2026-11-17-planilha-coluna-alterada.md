# Revisão de segurança: planilha com coluna de item alterada (17/11/2026)

Ramo `planilha-tolerante`, diff contra `origin/master` d49ed56. Arquivos: `portal_cliente/envio_pedidos.py` (`_pares_de_itens`, `ler_planilha`), `portal_cliente/app.py` (rota de análise do envio), testes.

## O que muda na superfície exposta

A rota de análise (cliente logado) passa a aceitar planilhas que antes recusava: `SkuN` sem `QuantidadeN` usa a coluna à direita; colunas `sku`/`quantidade` do modelo valem como item numa planilha de pares. O texto do cabeçalho do cliente entra em um aviso (devolvido só a quem enviou) e no log do serviço. O motivo de cada rejeição passa a ir pro log.

## Achados

Sem ALTO nem MÉDIO.

- **BAIXO (corrigido antes do commit final): texto do cliente no log sem escape.** Rótulo do pedido, erro e aviso levam valores de célula/cabeçalho; quebra de linha numa célula forjaria linhas no journal. Agora vão com `!r` e truncados (rótulo 80, erro 300).
- **BAIXO (corrigido): cabeçalho sem limite de tamanho no aviso.** Uma célula de cabeçalho com dezenas de milhares de caracteres inflaria a resposta e o log. Nome de coluna no aviso limitado a 60 caracteres.
- **Informativo: dedução pode errar a quantidade.** A coluna à direita do SKU pode não ser quantidade. Mitigações: não usa coluna que já é campo do pedido, outro SKU ou quantidade de outro par; valor não numérico continua rejeitando o pedido; o aviso aparece na prévia e o cliente ainda precisa confirmar. Não há mudança no que é gravado sem confirmação.

## Sem mudança

Autenticação, escopo por empresa do grupo (`emb not in empresas` continua recusando), validação de SKU no catálogo, limites de linhas/itens, gravação e fila pra Stokki. O aviso é exibido na prévia por `esc()` (já existente).

## Testes

3 testes novos em `portal_cliente/test_envio_planilha.py` (as planilhas reais da Marchef de 17/11 reproduzidas, e os casos em que não deve deduzir). Suíte do portal: 683 passam; falhas restantes são do worktree sem `config.yaml`/banco.
