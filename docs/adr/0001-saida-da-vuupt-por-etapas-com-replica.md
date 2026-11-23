# ADR-0001: Sair da Vuupt por etapas reversíveis, replicando tudo nela até a virada

## Status
Aceito

## Data
2026-09-12 (complementada em 2026-09-15)

## Contexto
A Vuupt é dona do pool de pedidos, da execução das rotas, do status que a Torre, a expedição na Stokki e o portal leem, do canhoto e do ciclo do pedido (reentrega, cancelamento, reagendamento). Em 12/09 só existiam na Vuupt 7.844 rotas, 62.527 serviços e 59.496 checklists; o núcleo (`nucleo_*`) cobria de 15/07 em diante e tinha defeitos medidos em produção (status voltando a ABERTO, paradas fantasmas, códigos com e sem `#`, horários UTC misturados com hora local).

O financeiro da Fresh Log fecha o mês com os relatórios exportados da Vuupt. O Hugo foi explícito: "tem uma questão de relatórios do meu financeiro... precisamos replicar tudo o que fizermos no Vuupt, até a virada de chave real".

Decisão do Hugo, registrada em `DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md`.

## Decisão
1. Abandonar o banco da Vuupt **por etapas reversíveis**, nunca big bang. Cada etapa roda em sombra, tem critério objetivo de passagem e volta atrás por configuração (`fonte: vuupt | nucleo`).
2. **Até a virada real, tudo o que o sistema próprio faz é replicado na Vuupt.** A Vuupt continua completa enquanto o financeiro depender dela.
3. Ordem das etapas: blindar (backup, alerta, trava de acoplamento) → exportar o histórico → espelho fiel com comparador diário → leituras passam ao núcleo uma a uma (expedição na Stokki por último) → pedido e rota nascem no núcleo → motoristas no app, rota a rota → Vuupt só leitura e corte.
4. A trava `nucleo/test_acoplamento_vuupt.py` congela a lista de arquivos que falam com a Vuupt: arquivo novo chamando a Vuupt reprova no teste.

## Alternativas consideradas

### Corte direto (big bang) para o núcleo e o app próprio
- Prós: acaba com a dupla escrita de uma vez.
- Contras: espelho com defeitos medidos; 0 fotos de canhoto no servidor próprio em 12/09; financeiro sem relatório substituto.
- Rejeitada: risco operacional sem volta atrás.

### Ficar na Vuupt e só espelhar para consulta
- Prós: zero risco de execução.
- Contras: não resolve custo, dependência de API sem contrato de versão e limites já medidos (fechar serviço pela API não fecha a rota; km real só pelo app da Vuupt).
- Rejeitada: a saída é objetivo do Hugo.

## Consequências
- Toda escrita nova no fluxo de pedido ou rota precisa de dois destinos (núcleo + Vuupt) ou de um sincronizador que cubra o caso. Sincronizadores: `nucleo/sincronizar_vuupt.py` (rotas) e `nucleo/sincronizar_servicos_vuupt.py` (pedidos fora de rota).
- Comparadores (`nucleo/comparar_vuupt.py`, `nucleo/comparar_pool.py`) são o placar de cada etapa. Divergência explicada (ex.: duplicado na Vuupt) não suja o placar.
- A primeira leitura que passou ao núcleo foi o pool do planejamento (`planejamento.fonte_pool`, 24/09); a virada da chave na VPS é decisão do Hugo.
- Pendências abertas com o Hugo: fim de contrato e exportação oficial da Vuupt, backup contínuo (Litestream/Postgres) antes do piloto real, motoristas do piloto, fechamento de rota Lalamove/terceiros no núcleo.
