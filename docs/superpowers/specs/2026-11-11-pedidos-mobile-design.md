# /pedidos no celular — desenho (11/11)

Escolha do Hugo (11/11), entre as 3 opções do canvas "Pedidos no celular":
lista compacta + tela do pedido (opção B) com abas por status, seleção em lote
e filtros em tela cheia (opção C).

## Por quê
A /pedidos (uma linha por pedido, ações por linha e em lote, deploy 10/11) é
uma tabela larga com filtro por coluna: no celular não dá pra ler nem agir.
A equipe (atendimento, logística) precisa achar um pedido, entender o que
houve e agir (reenvio, agendar, endereço, cancelar, canhoto) fora do computador.

## O que muda
- **Mesmo endereço `/pedidos`.** Tela estreita (≤ 760 px, o mesmo corte em que
  o base.html vira gaveta ☰) ou `?modo=mobile` põe `html.modo-mobile`;
  `?modo=desktop` força a tabela. Mesmo padrão do Planejamento. (Decisão do
  Claude: reaproveita modais, travas e chamadas das ações que estão em
  produção, sem um segundo código pra manter.)
- **Lista compacta**: duas linhas por pedido (código, destinatário, data /
  status + motivo ou rota + motorista, selo "N×" quando há mais de 1
  tentativa). Marcador de status muda cor E forma (redondo x quadrado), não
  só a cor. Agrupada por dia quando a ordem é por data.
- **Abas por status** no topo (Todos, Abertos, Em rota, Entregues, Insucesso,
  Cancelados), com contagem do período/filtros atuais. Tocar = filtro de
  status. A contagem vem de `GET /api/pedidos/contagem` (mesmos filtros, sem
  o de status), chamada só no modo celular — a tabela do computador não
  paga a consulta a mais.
- **Tela do pedido** (toque na linha): sobrepõe a lista, fecha no voltar do
  Android (hash na URL). Destinatário com Ligar (tel:) e Abrir no mapa,
  dados da entrega, tentativas em linha do tempo, botão principal conforme o
  status (Insucesso/Cancelado/Entregue → Reenvio; Em rota → Dar baixa;
  Aberto → Agendar) e "Todas as ações" (o mesmo menu, como folha de baixo,
  com os mesmos bloqueios e dicas). Leitura vê tudo, sem botões de ação.
  Dados vêm da própria página (JSON embutido), sem chamada nova.
- **Lote**: botão "Selecionar" mostra as caixas; barra fixa embaixo com
  Agendar, Endereço e "Mais" (Reenvio, Cancelar Vuupt, Cancelar Stokki, Dar
  baixa). Mesmo fluxo e mesmas regras do lote do computador (endereço em lote
  só mesmo embarcador + destinatário).
- **Filtros em tela cheia**: Status (11/11, pedido do Hugo; mesmo filtro das
  abas, que ficam marcadas junto), Canhoto (11/11: Todos / Com / Sem, também
  na barra do computador; critério da /canhotos-pendentes, "Sem" só entregues), atalhos Hoje / Ontem / 7 dias / Este mês,
  De, Até, Tipo de data, Embarcador, Motorista, Zona, Fluxo; botão "Aplicar".
  Busca pela lupa no topo (mesmo campo `q`).
- Paginação continua de 100 em 100 (Anterior / Próxima).
- Link "Versão completa" leva para `?modo=desktop`.

## Fora
- Filtro por coluna, escolha de colunas e CSV no celular (ficam no computador).
- Nota fiscal e Trocar canhoto continuam "em breve" (fases 2 e 3).
- Nenhuma regra de ação muda; o servidor continua decidindo.

## Contas e acesso
Mesmos níveis da /pedidos. `/api/pedidos/contagem` entra no prefixo
`^/api/pedidos/` que a tela já libera em acesso_telas.

## Prova
- unittest: contagem por status (respeita filtros, ignora o de status) e rota.
- Testes atuais da /pedidos e das ações seguem verdes (computador intocado).
- Playwright a 390 px com `/api/pedidos/acao/**` interceptado: lista, abas,
  tela do pedido, voltar, lote, filtros; e a 1280 px a tabela igual a antes.
