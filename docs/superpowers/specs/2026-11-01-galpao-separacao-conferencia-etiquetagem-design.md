# Galpão: etiqueta com serial, separação, conferência e volumes

Data: 01/11/2026 · Piloto: MARIA DOLORES (`#stkkc-48`, `wms.embarcador_piloto_id`)
Continua: `2026-09-21-wms-estoque-reserva-pedidos-design.md` (fases 1 e 2 do WMS, em produção)

## 1. Objetivo

Hoje o app do galpão (`/wms`) recebe, endereça, reserva (FEFO) e dá baixa na
expedição. Não existe separação de verdade, conferência nem volume: a aba
"Separar" só confirma ou troca o lote da reserva. Quem separa e confere é a
Stokki.

Este projeto faz o galpão **executar no nosso sistema**, para todos os pedidos
da Maria Dolores:

1. etiqueta de produto com **número único por caixa** (serial) e SKU em destaque;
2. **separação** pedido a pedido e em onda, com distribuição por pedido;
3. **conferência cega** caixa a caixa, por outra pessoa;
4. **fechamento de volumes** com etiqueta de volume.

Um **robô replica essa execução na Stokki**, do jeito da Stokki: ela não
trabalha com lote nem validade, então recebe só o que usa (itens, quantidades,
conferência, volumes). A Stokki continua sendo onde estão a NF, o cliente e a
expedição. A equipe opera um sistema só.

Seria falha, mesmo que "funcione":
- estoque do WMS que não bate com a prateleira;
- um bipe repetido contado como duas caixas;
- pedido parado na Stokki porque o robô falhou e ninguém percebeu, deixando a
  rota sem sair;
- a equipe abandonar o app porque ele atrapalha o turno.

## 2. Fora de escopo

- Lote e validade na Stokki (ela não usa).
- Outros embarcadores (continuam como hoje no `/wms`).
- Rota e ordem de entrega na etiqueta (ficam no romaneio).
- Embalagem térmica, peso, foto do volume.
- Ligar caixa a volume (o volume é só "X de Y" do pedido).
- Migrar para o freshhub (a seção 13 diz só como não atrapalhar).

## 3. O que já existe (levantamento de 01/11, código local)

- Tabelas `wms_*`: posições, produtos (sku, ean, dun, qtd_por_caixa), operadores
  (PIN PBKDF2), `wms_movimentos` (só INSERT, uuid UNIQUE, tipos
  ENTRADA/SAIDA/TRANSFERENCIA/AJUSTE/INVENTARIO), `wms_saldos` por
  (posição, produto, lote, validade) derivado dos movimentos
  (`painel_agentes/wms.py:72-157`, `recalcular_saldos` :874).
- `wms_pedidos`, `wms_pedido_itens`, `wms_reservas` (linha × posição × lote ×
  validade, ATIVA/CONSUMIDA/CANCELADA), recebimentos
  (`painel_agentes/wms_pedidos.py:34-121`).
- Os pedidos chegam pelo timer de 15 min (`sincronizar_pedidos_wms.py:398-475`), só
  do piloto, e **só nos status Waiting for Carrier, Separating e Ready to Pack**
  (:72). Pedido em Open não é lido.
- Baixa: `baixar_por_expedicao` (`wms_pedidos.py:480`), chamada por
  `expedir_pedidos.py:1587-1604` (cópia da RAIZ) e pela varredura do sync.
- Leitura de código: `ler_codigo` (`wms.py:817-851`) separa `FL|`, `POS:` e
  EAN/DUN/SKU (`buscar_por_codigo` :543).
- Etiqueta de produto: `wms_etiqueta_produto.py`, PIL + qrcode, 300 dpi, 10×5 cm,
  calibração `wms.etiqueta_*` no config da VPS. Sai como PDF no navegador
  (`/wms/etiqueta-produto.pdf`). Recusa produto sem SKU e sem EAN (:112).
- PWA `templates/wms.html`: leitura por câmera (BarcodeDetector + ZXing do unpkg) e
  leitor HID (buffer até Enter, :971-981); fila offline só para movimentos
  (:1010-1031); operador + PIN, sessão de 2 h.
- Escrever na Stokki já é rotina: `expedir_pedidos.py` faz
  `POST /provider/operation/shipping/store` com CSRF via Playwright;
  `stokki/` tem `StokkiSession`, cancelamento por HTTP e a skill `operar-stokki`.
- A Stokki separa em ondas e registra, por pedido: quem separou ("Operador da
  Tarefa"), quem conferiu, tempos, lotes, "Quantidade total separada", "EANS lidos
  na separação", "volumes da NF-e" e "volumes informados na conferência", além de
  "Imprimir Controle de Volume" (fixtures em `galpao3d/fixtures/show_*.html`;
  `stokki/pedidos.py:328-353` já extrai `quantidade_volumes`).
- O WMS está vazio em produção desde 24/09 (memória do projeto; não reconferido
  em 01/11 porque a leitura da VPS foi bloqueada).

## 4. Decisões

### Do Hugo, 01/11/2026

| # | Tema | Decisão |
|---|------|---------|
| H1 | Onde | Legado `/wms` (Flask + SQLite). Migra para o freshhub depois |
| H2 | Stokki | **O galpão opera no nosso sistema e um robô replica a execução na Stokki, do jeito da Stokki** (sem lote/validade). Substitui o "paralelo só leitura" do início da conversa |
| H3 | Piloto | Maria Dolores, **todos os pedidos** dela |
| H4 | Saldo inicial | Inventário de abertura etiquetando cada caixa com serial e endereçando na mesma passada |
| H5 | Serial | Cada caixa tem serial único; **substitui a D5 de 21/09 ("sem serial")** |
| H6 | QR | Um QR só por caixa: `FL\|CÓDIGO\|LOTE\|VALIDADE\|SERIAL`. Sem código de barras extra |
| H7 | Produto sem EAN | Código interno gerado no backend, vai dentro do QR no lugar do SKU/EAN |
| H8 | Layout da etiqueta de produto | Descrição, **SKU em destaque**, LOTE, VAL, faixa `CAIXA <serial>`, rodapé embarcador · Freshlog (mockup aprovado) |
| H9 | Etiqueta de volume | Impressa no fechamento, sem rota: PS, NF, embarcador, destinatário (nome, bairro, cidade), "X / Y", QR do volume. Sem temperatura |
| H10 | Separação | Pedido a pedido **e** em onda |
| H11 | Montar onda | Botão "montar onda", filtro por data de entrega |
| H12 | Distribuir a onda | O separador bipa cada caixa e a tela diz o pedido; depois outra pessoa confere |
| H13 | Conferência | Cega, caixa a caixa pelo serial, por pessoa diferente de quem separou, travando lote/validade diferente do reservado |
| H14 | Falta na conferência | Pedido trava; o escritório decide (voltar para separação ou liberar parcial), fica registrado |
| H15 | Quem é o "líder" | Só o escritório (níveis total e operador do painel) |
| H16 | Embalagem | Operador define quantos volumes e quantas etiquetas, e etiqueta |
| H17 | Aparelho | Celular + leitor HID (o mesmo da PWA) |
| H18 | Quando a equipe larga a Stokki de vez | Sem prazo fixo: o Hugo decide olhando o acompanhamento (seção 10) |
| H19 | Desenho | **D1** (caixas como camada sobre o estoque atual) + **O1** (pedido a pedido = onda de um pedido) |
| H20 | Impressão | A equipe de impressão usa um **computador fixo ligado às térmicas**. Caixa: etiqueta **10×5**. Identificação do pedido/volume: **10×15**. Os tamanhos podem mudar |

### Mantidas de 21/09

- FEFO sugerido, e o separador pode trocar o lote bipando outra caixa (D6).
- Falta de saldo avisa e nunca bloqueia a reserva (D8).
- Reserva no lançamento e baixa na expedição (D1).
- Só a D5 (sem serial) cai.

### Decisões do Claude (o Hugo pode trocar)

- C1. O serial é `C` + 7 dígitos, sequencial, tirado de um contador no banco.
  Nunca é reutilizado.
- C2. O código interno do produto sem EAN é `FLP` + 6 dígitos. Tem letras de
  propósito: nunca se confunde com EAN/DUN em `buscar_por_codigo`.
- C3. O QR do volume é `FV|<codigo_ps>|<n>|<total>`.
- C4. Etiqueta de volume em **10×15** na impressora de pedidos (H20). Etiqueta
  de caixa em 10×5 com a calibração atual da Elgin L42.
- C5. Acompanhamento (seção 10) no painel, por dia e por pedido, para os níveis
  total e operador.
- C6. Separar **não** gera movimento. A caixa continua contando no saldo da
  posição até a baixa da expedição, como a reserva hoje. Só a situação da
  caixa muda.
- C7. Separação, distribuição e conferência exigem rede, porque o servidor
  recusa bipe repetido ou de outro pedido. Só a ativação de caixa (inventário
  e recebimento) entra na fila offline, com uuid determinístico por serial.
- C8. **Caixa fracionada** (o pedido pede menos que a caixa): como não há
  impressora no chão, o separador carrega um rolo de **etiquetas de serial
  avulso**, impressas antes no computador. Elas trazem só o QR `FC|<serial>` e
  `CAIXA <serial>`. Ele bipa a caixa de origem, informa quanto tirou e bipa uma
  etiqueta avulsa, que passa a representar a parte retirada. A caixa de origem
  fica ABERTA com o restante.
- C9. A "onda de um pedido" (pedido a pedido) o separador inicia sozinho no
  celular. Só onda com 2 ou mais pedidos exige o escritório.
- C10. **Impressão por fila.** O celular nunca imprime. Pedido de etiqueta
  (inventário, recebimento, volumes, rolo avulso) vai para uma fila de
  impressão, e o computador da equipe de impressão imprime por uma tela de
  fila (aperta "imprimir") ou automaticamente. O caminho fica com quem
  constrói; `print_agent/` é a referência que já existe.
- C11. **O robô replica em dois marcos:**
  - R1, quando a separação começa no nosso sistema: o pedido vai para
    "Em separação" na Stokki;
  - R2, quando os volumes são fechados: a Stokki recebe a separação (itens e
    quantidades), a conferência e a quantidade de volumes, e o pedido sai
    liberado como hoje.

  Assim o pool, a prontidão (`nucleo/prontidao_stokki.py`) e o Galpão 3D
  continuam lendo a Stokki sem mudança.
- C12. **Baixa sem o nosso fluxo** (caso raro, já que são todos os pedidos):
  - a baixa continua pela reserva;
  - as caixas são escolhidas pelo FEFO, marcadas EXPEDIDA, e o pedido aparece
    como "baixa sem conferência" no acompanhamento;
  - no parcial liberado, baixa só o conferido, e o resto da reserva é
    cancelado.
- C13. A caixa devolvida (pedido cancelado ou "voltar para separação") fica
  DEVOLVIDA até alguém bipar a posição onde guardou. Nada volta sozinho para
  a prateleira.
- C14. Antes do inventário, qualquer saldo antigo da Maria Dolores é zerado
  com AJUSTE registrado. O inventário roda numa janela sem chegada nem saída
  dela.

## 5. Fluxo de ponta a ponta

1. **Etiqueta.** Pedir N etiquetas de um produto/lote/validade cria N caixas na
   situação IMPRESSA (serial reservado, ainda sem estoque) e manda o trabalho
   para a fila de impressão (C10).
2. **Inventário de abertura (uma vez) e recebimento (sempre).**
   - O operador bipa a posição, cola as etiquetas e bipa cada caixa.
   - A caixa passa a EM_ESTOQUE naquela posição e gera um movimento:
     AJUSTE para mais no inventário (o tipo INVENTARIO grava a contagem
     total da posição e apagaria a caixa anterior); ENTRADA amarrada ao item
     do recebimento, reusando `contabilizar_enderecamento`.
   - Caixa IMPRESSA que nunca foi bipada não é estoque.
3. **Entrada do pedido.** O sync passa a ler também os pedidos em **Open**
   (hoje não lê), para a separação começar no nosso sistema. A reserva
   continua igual (FEFO por linha × posição × lote).
4. **Separação.**
   - Pedido a pedido: o separador escolhe o PS no celular, e isso vira uma onda
     de um pedido (C9).
   - Onda: o escritório monta no painel, com filtro por data de entrega.
   - O início dispara o R1.
   - A lista soma as reservas ATIVAS por posição e lote, em ordem de caminhada.
   - O separador bipa a caixa na posição. Ela precisa bater com o produto de
     uma reserva. Lote diferente pode ser trocado (D6), e a reserva é
     atualizada. A caixa fica SEPARADA.
   - Fracionamento conforme C8.
5. **Distribuição (onda com 2 ou mais pedidos).** O separador bipa cada caixa,
   a tela mostra "PS-xxxx", e a caixa fica DISTRIBUÍDA. Numa onda de um pedido,
   separar já distribui.
6. **Conferência cega.**
   - A tela mostra o PS e o destinatário, sem itens nem quantidades.
   - O conferente precisa ser diferente de todo operador que separou ou
     distribuiu caixa do pedido.
   - Ele bipa caixa a caixa. O sistema recusa caixa repetida, caixa de outro
     pedido e lote/validade diferente da reserva vigente.
   - Ao "fechar", o sistema compara as caixas bipadas com o que o pedido pede:
     - bateu: CONFERIDO;
     - faltou: o pedido trava em AGUARDANDO_DECISAO.
7. **Decisão de falta (painel, escritório).**
   - "Voltar para separação" reabre o pedido.
   - "Liberar parcial" segue com o que foi conferido.
   - As duas ficam registradas com quem decidiu e o motivo.
8. **Volumes.**
   - O operador informa quantos volumes o pedido tem.
   - O sistema cria os volumes 1..N e manda N etiquetas 10×15 para a fila. Dá
     para reimprimir um volume.
   - O pedido fica EMBALADO, e isso dispara o R2.
9. **Expedição.** A baixa continua vindo de `expedir_pedidos.py` e da varredura
   do sync. Ao baixar, as caixas do pedido passam a EXPEDIDA.

**Situações da caixa:** IMPRESSA → EM_ESTOQUE → (ABERTA) → SEPARADA → DISTRIBUÍDA →
CONFERIDA → EXPEDIDA. Também CANCELADA (etiqueta inutilizada) e DEVOLVIDA (C13).

## 6. Modelo de dados (D1, migração aditiva)

Nada muda em `wms_saldos`, `wms_movimentos` nem `wms_reservas`. Tabelas novas
(nomes e colunas são o esboço; os detalhes ficam com quem constrói):

- `wms_caixas`: serial (UNIQUE), produto_id, lote, validade, qtd_un,
  posicao_codigo, situacao, pedido_id, onda_id, serial_origem (fracionamento),
  criado_em, atualizado_em.
- `wms_caixa_eventos`: só INSERT. Serial, de → para, operador, pedido, onda,
  uuid UNIQUE, criado_em. É o histórico e o rastreio.
- `wms_ondas` (id, criada_por, filtro, situacao, criada_em) e `wms_onda_pedidos`.
- `wms_conferencias` (pedido, operador, início, fim, resultado OK/FALTA,
  detalhe) e `wms_decisoes_falta` (quem, quando, decisão, motivo).
- `wms_volumes`: pedido_id, numero, total, impresso_em, reimpressoes.
- `wms_fila_impressao`: tipo (caixa, avulso, volume), tamanho (10×5 / 10×15),
  referências, situação, criado_em, impresso_em.
- `wms_replicacao_stokki`: pedido, marco (R1/R2), situação
  (PENDENTE/OK/FALHOU), tentativas, último erro, quando.
- `wms_produtos`: coluna nova para o código interno (C2).
- `wms_pedidos`: situação de execução em coluna própria (sem misturar com
  `estado_reserva`), mais a data de entrega, se o filtro da onda precisar (F0).

**Checagem diária caixas × saldo:** para cada (posição, produto, lote,
validade), a soma de `qtd_un` das caixas não expedidas e não canceladas tem
que ser igual a `wms_saldos`. Se divergir, o aviso sai no acompanhamento e
por e-mail interno.

## 7. Leitura de códigos

`ler_codigo` ganha:
- `FL|COD|LOTE|VAL|SERIAL`: devolve a caixa. A etiqueta antiga, de 3 campos,
  continua sendo lida como hoje (produto + lote, sem caixa) e não vale para
  conferência.
- `FC|SERIAL`: etiqueta avulsa (C8).
- `FV|PS|n|total`: volume.
- `FLP…`: `buscar_por_codigo` acha pelo código interno.

## 8. Etiquetas

- **Caixa, 10×5 (H8):** mockup aprovado em 01/11, com descrição, SKU grande,
  LOTE, VAL, faixa preta `CAIXA C0001234` e rodapé. Reusa
  `desenhar_etiqueta_produto` e a calibração da VPS. N cópias geram N seriais
  diferentes.
- **Avulsa, 10×5 (C8):** só o QR `FC|…` e `CAIXA <serial>`, impressa em rolo
  antes do turno.
- **Volume, 10×15 (H9, C4):** faixa preta com `PS-xxxxx  n / total`,
  destinatário (nome, bairro, cidade), NF, embarcador · Freshlog e QR `FV|…`. A
  fonte do destinatário e da NF sai da F0.
- **Mockup:** `etiquetas_exemplo.png`, gerado pelo script descartável
  `mock_etiquetas.py` na pasta temporária da sessão de 01/11. O volume está
  desenhado em 10×5 e precisa ser redesenhado em 10×15.

## 9. Telas

**PWA** (`/wms`, nível galpao + operador/PIN):
- **Inventário:** bipa a posição, escolhe o produto, informa lote e validade,
  pede as etiquetas e bipa para ativar.
- **Receber:** igual a hoje, mais o pedido de etiquetas e a ativação por caixa.
- **Separar:** pedidos e ondas liberadas, lista de caminhada, bipe por caixa,
  fracionamento.
- **Distribuir:** bipe da caixa, com o PS em letra grande.
- **Conferir:** PS e destinatário, contador de caixas bipadas (sem o
  esperado), botão "fechar".
- **Volumes:** número de volumes, mandar imprimir, reimprimir um.

**Painel** (escritório, níveis total/operador):
- **Ondas:** pedidos da Maria Dolores prontos para separar, filtro por data
  de entrega, "montar onda".
- **Faltas:** pedidos em AGUARDANDO_DECISAO, com o que faltou e os botões de
  decisão.
- **Acompanhamento:** seção 10.
- **Fila de impressão:** a tela que o computador da equipe de impressão deixa
  aberta (C10).

**Acesso:** as telas do escritório **não** podem cair na regex da tela "wms"
de `acesso_telas.TELAS` (`^/wms(?!/estoque$)`), senão o login do galpão as
vê. Use rota fora de `/wms`, ou exclua na regex como já é feito com
`wms_estoque`, e crie entradas próprias em TELAS com
`requer_auth(niveis=_ADMIN)`.

## 10. Robô na Stokki e acompanhamento

**Robô:**
- Roda na VPS, com a sessão Stokki compartilhada e a trava que as rotinas já
  usam. É idempotente por pedido e marco (`wms_replicacao_stokki`).
- Tem `--modo-teste`, que não escreve.
- Falha não some: tenta de novo, e se o pedido ficar parado ele aparece no
  acompanhamento e gera alerta para a equipe, que pode terminar à mão na
  Stokki como hoje.

**Acompanhamento** (painel, por dia e por pedido):
- em que etapa cada pedido está no nosso sistema e na Stokki;
- R1 e R2: OK, pendente ou falhou;
- diferenças entre o que conferimos e o que a Stokki ficou registrando
  (itens, quantidades, volumes);
- tempos de separação e conferência;
- "baixa sem conferência" (C12);
- a checagem caixas × saldo.

O Hugo decide quando o fluxo está provado (H18).

## 11. Riscos e armadilhas

- **Robô falhou = rota não sai.** A prontidão só libera pedido que a Stokki
  liberou. Alerta e reprocesso são obrigatórios desde o primeiro dia, e o
  plano manual (terminar na Stokki) precisa estar escrito para a equipe.
- **Como escrever separação e conferência na Stokki é desconhecido.** Nunca
  fizemos isso. Pode exigir bipar EAN por EAN numa estação deles. Descobrir
  na F0, só lendo.
- **Itens do pedido em Open.** `stokki/pedidos.py:521-573` lê a tabela
  "Produto|EAN|Quantidade", que nas fixtures só aparece em "EANS lidos na
  separação" (conferência e aguardando), não em "Em separação". Se não houver
  itens legíveis em Open, é preciso outra fonte (por exemplo o XML da NF).
  Provar na F0.
- **"Quem separou" na Stokki pode virar o robô.** O Galpão 3D e os
  indicadores por pessoa perdem sentido, a menos que a Stokki aceite o nome
  real do operador. Ver na F0.
- **Sessão Stokki compartilhada:** o importador (outro projeto) usa a mesma
  sessão, e login concorrente derruba a outra com 401. Use as funções prontas
  de `stokki/`, nunca login próprio. Desde 28/10 existe a tela
  `/login/active-session`.
- **Expedição Programada (OE) não finalizada trava o robô** ("situação
  inválida"), como já acontece no `expedir_pedidos`.
- **dados.db é compartilhado:** transação curta e commit por caixa ou por bipe.
  Lote grande num commit só já derrubou o painel ("database is locked", 04/09).
- Templates só recarregam com restart do waitress.
- Teste local sempre na porta 8099.
- Nunca fazer login na Stokki pela máquina local.
- O ZXing vem do unpkg: sem internet, só o leitor HID funciona.
- **Inventário de abertura é mutirão:** a fila de impressão precisa aguentar
  centenas de etiquetas, e o banco recebe commits pequenos.
- **Ordem de rollout das caixas (F1).** A chave `wms.caixas_ativo` (padrão
  desligada) liga a trava do movimento avulso, o `usa_caixas` do Receber, o
  criar caixa, a aba Inventário e a checagem das 06h40. Ler, ativar, mover e
  cancelar caixa funcionam com ela desligada.
  1. deploy com `wms.caixas_ativo` desligado;
  2. conferir que não há reservas ATIVAS da Maria Dolores;
  3. `zerar-saldo` (plano, depois `--aplicar`); ele recusa com reserva ATIVA
     ou com caixa ativa;
  4. ligar `wms.caixas_ativo: true` logo antes do mutirão (criar caixa recusa
     com a chave desligada), numa janela sem chegada nem saída da Maria
     Dolores, e fazer o inventário;
  5. a checagem das 06h40 do dia seguinte precisa dar 0 divergência.
  Divergência se conserta com `wms_caixas.py acertar-caixa` (caixa que já não
  existe, sem mexer no saldo) ou `acertar-saldo` (saldo igual às caixas),
  ambos com `--motivo`.
- **Serial conta, não rastreia (C12).** Na F1 o serial vale para a contagem.
  A caixa marcada EXPEDIDA pela baixa é escolhida por FEFO/serial e pode não
  ser a caixa física que saiu. O rastro por serial só fica exato quando a
  separação gravar o serial (F2).

## 12. Fases (cada uma com uma prova real)

- **F0. Investigação na Stokki (só leitura, VPS).**
  - Em qual status os itens do pedido aparecem.
  - Como a Stokki registra o início da separação, os itens separados, a
    conferência e os volumes: telas, formulários, endpoints, CSRF, e se dá
    para informar o operador real.
  - De onde vêm a data de entrega, o destinatário (bairro e cidade) e a NF.
  - Resultado: nota anexada a esta spec, com o roteiro do robô. Sem código e
    sem escrita.
- **F1. Caixas, etiquetas e fila de impressão.** Contador de serial,
  `wms_caixas` e eventos, etiqueta de caixa, código interno, fila de
  impressão no computador, ativação no inventário e no recebimento, checagem
  caixas × saldo. Também entra aqui a parte da C12 em que a baixa da
  expedição consome as caixas, com a situação ABERTA: sem isso, a baixa
  diária já faria a checagem divergir. A etiqueta avulsa (C8) fica para a
  F2, junto com o fracionamento. Prova: inventário de abertura da Maria
  Dolores feito, com a checagem zerada.
- **F2. Separação e distribuição (O1)**, mais a tela de Ondas e o robô R1 (em
  `--modo-teste` primeiro). Prova: pedidos de teste e alguns reais separados
  no nosso app. Até o R2 existir, a equipe termina esses pedidos na Stokki,
  num piloto acompanhado.
- **F3. Conferência cega e decisão de falta.** Prova: pedidos conferidos por
  outra pessoa; uma falta decidida no painel.
- **F4. Volumes, etiqueta 10×15 e robô R2.** Prova: um pedido real percorre
  tudo só no nosso app e sai liberado na Stokki pelo robô.
- **F5. Todos os pedidos da Maria Dolores** no nosso fluxo, com o
  acompanhamento completo, até o Hugo decidir (H18).

Testes:
- `unittest` com banco temporário e Stokki falsa, no padrão de
  `painel_agentes/test_wms_pedidos.py`;
- `test_wms_telas.py` para a ligação da tela;
- Playwright local na 8099 para os fluxos de bipe;
- robô sempre em `--modo-teste` antes da primeira escrita real.

## 13. Como isso migra para o freshhub (Fase 3)

- As regras ficam em módulos de dados puros (sem Flask), como `wms.py` e
  `wms_pedidos.py`, para portar para FastAPI.
- O serial é um texto global e estável, que vira chave natural no Postgres
  sem renumerar. As etiquetas coladas continuam valendo depois da migração.
- O histórico só cresce (`wms_caixa_eventos`, `wms_movimentos`), o que dá uma
  carga direta para o banco novo.
- O robô fica isolado num módulo próprio. Quando a Stokki sair (Fase 3), ele é
  desligado sem mexer no fluxo do galpão.
- Nada de recurso exclusivo do SQLite nas regras.

## 14. Fica com quem constrói

- Nomes de tabelas, colunas e rotas.
- Ordem de caminhada na lista de separação.
- Textos e cores das telas.
- Formato do e-mail da checagem.
- Como guardar o contador de serial.
- Como o computador de impressão consome a fila (tela ou agente).
- Fonte exata do destinatário e da NF, e o roteiro do robô (depois da F0).
