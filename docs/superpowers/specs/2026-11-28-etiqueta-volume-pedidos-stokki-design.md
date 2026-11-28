# Etiqueta de volume para todo pedido finalizado na Stokki (etapa 1 de 3)

Data: 28/11/2026. Pedido do Hugo. Estado: desenho para aprovação, nada implementado.

## Por quê

Hoje a caixa sai do galpão sem identificação nossa. O Hugo quer que toda caixa de todo pedido
leve uma etiqueta da Fresh Log com QR próprio, para que depois:

1. **(etapa 2)** o carregador bipe cada caixa no carregamento e o sistema prove que todos os
   pedidos da rota subiram no caminhão;
2. **(etapa 3)** a bipagem completa libere, no app do motorista, o checklist de carregamento
   com a foto do baú (que já existe, desligado: `nucleo/carregamento.py`).

Este documento cobre **só a etapa 1: etiquetar**. As etapas 2 e 3 terão desenho próprio.

## O que o Hugo decidiu

- O pedido continua sendo separado/conferido e finalizado **na Stokki**. A etiqueta é nossa e
  usa o número do pedido da Stokki (PS).
- A contagem de volumes é **nossa**: o operador informa na nossa tela.
- Vale para **todos os pedidos, cross-docking incluído** (sem isso a bipagem da etapa 2 não
  prova a rota inteira).
- Volume é **sempre a caixa**. Não existe palete fechado; não haverá etiqueta de palete.
- Caminho de construção **B**: etiquetagem própria pelo número do pedido (ver Caminhos).

## Objetivos

- Qualquer pedido finalizado na Stokki pode receber etiquetas 10×15, uma por caixa, com
  "n / total" e QR que identifica **aquela caixa**.
- O operador não espera o sistema: digita ou bipa o PS e imprime na hora.
- O escritório enxerga o que está pronto na Stokki e ainda sem etiqueta.
- O que for gravado aqui já serve à bipagem da etapa 2 sem retrabalho.

## Fora desta etapa

- Bipagem no carregamento, qualquer mudança no app do motorista ou no checklist de carregamento.
- Travar rota, romaneio ou expedição por falta de etiqueta (aqui é só cobrança visual).
- Unificar com a aba Volumes do piloto do galpão (continua como está).
- Qualquer escrita na Stokki ou na Vuupt.

## O que não pode acontecer

- Quebrar ou mudar o fluxo do piloto do galpão (`wms_separacao.fechar_volumes`, QR `FV|PS|n|total`).
- A tela chamar a Stokki ao vivo (login concorrente derruba a sessão dos robôs).
- Duas caixas diferentes com QR que a etapa 2 aceite como a mesma, ou etiqueta antiga
  (de um total que mudou) continuar valendo.
- Falta de dado (destinatário, NF) impedir a impressão: campo vazio sai em branco.

## Caminhos considerados

- **A. Abrir a aba Volumes do /wms para todos.** Reaproveita a tela, mas ela exige a nossa
  conferência (`execucao IN ('CONFERIDO','EMBALADO')`) e grava em `wms_volumes`, preso a
  `wms_pedidos` (estoque/reservas do piloto). Abrir pra todos mexe em regra que funciona e
  deixa a etapa 2 longe da rota.
- **B. Etiquetagem própria pelo PS (escolhido).** Tela simples + tabela nova ligada ao código
  do pedido, o mesmo de `nucleo_pedidos` / `nucleo_paradas` (rota e app do motorista).
  Reaproveita o desenho da etiqueta e a fila de impressão. Custo: duas portas para a mesma
  etiqueta enquanto o piloto existir.
- **C. Usar o total de volumes da conferência da Stokki.** Descartado: a contagem é nossa.

## Como funciona

### Fluxo do operador

1. Finaliza o pedido na Stokki.
2. Abre a tela **Etiquetar** e digita ou bipa o PS (aceita `PS-45352`, `#PS-45352`, `45352`;
   normalizar com `nucleo/normalizacao.normalizar_codigo`).
3. A tela mostra embarcador e destinatário para conferência visual e pede a quantidade de caixas.
4. Confirma: as etiquetas entram na fila de impressão (tipo `VOLUME`, 10×15) e saem no
   computador fixo, em `/etiquetas/fila`, como já acontece com as do piloto.

```
┌ Etiquetar pedido ─────────────────────────────┐
│ Pedido: [ PS-45352        ]  (digite ou bipe) │
│                                               │
│ MARIA DOLORES → MERCADO EXEMPLO LTDA          │
│ São Paulo - SP                                │
│                                               │
│ Quantas caixas?  [ 5 ]     [ Imprimir ]       │
│                                               │
│ Já etiquetado: 5 caixas, 28/11 14:02 (João)   │
│ [Reimprimir todas] [Reimprimir a nº _] [Mudar total] │
├───────────────────────────────────────────────┤
│ Prontos na Stokki sem etiqueta (12)           │
│ PS-45360  FABENNE      desde 13:10            │
│ PS-45358  MARCHEF      desde 12:40            │
└───────────────────────────────────────────────┘
```

O desenho da tela acima é rascunho. **Antes de codar a tela, mostrar a prévia ao Hugo**
(regra dele para tela nova).

### Regras

- **Quantidade:** 1 a 300. Acima de 99 a tela pede confirmação ("são mesmo 120 caixas?").
- **Repetir o mesmo total:** não cria nada novo; oferece reimprimir (mesmos QRs).
- **Reimprimir uma ou todas:** mesmos QRs, conta a reimpressão.
- **Mudar o total** (eram 5, são 6): as etiquetas antigas são **anuladas** (ficam gravadas,
  marcadas como anuladas, com quem e quando) e saem N novas com QRs novos. A tela avisa:
  "as 5 etiquetas antigas deixam de valer; troque todas".
- **Pedido que não achamos no nosso banco:** imprime mesmo assim, só com o PS e a contagem,
  e a tela avisa que não reconheceu o pedido (o operador confere o número). Nunca bloqueia.
- **Pedido cancelado** (no nosso cadastro): a tela avisa em destaque e pede confirmação.
- **Reentrega** (`PS-1-R1`): a caixa é a mesma; a etiqueta é do pedido base (`PS-1`).
- **Sem trava:** nada na rota/romaneio/expedição depende da etiqueta nesta etapa.

### De onde vêm os dados da etiqueta

Sempre do nosso banco, nunca da Stokki ao vivo. Ordem:

1. `nucleo_pedidos` pelo código (destinatário, endereço, remetente = embarcador);
2. `wms_pedidos` (pedidos do piloto e os que o WMS já leu);
3. `vigia_stokki_abertos` (ao menos o embarcador);
4. nada: etiqueta só com PS e "n / total".

NF: usar quando já estiver gravada em alguma dessas fontes; sem NF, o campo some.

**Sugestão de quantidade (regra do Hugo, 28/11):** quando o pedido está em "aguardando
transportador" na Stokki e existe o total **conferido** pelo galpão, a tela já traz esse
número no campo, como sugestão; o operador aceita ou troca. Em qualquer outro caso (ainda em
conferência, só o número da NF, nunca lido) **não sugere nada**.

- O total conferido vem da tabela `volumes_conferidos`, que hoje só é lida às 03h50 para as
  rotas do dia. Para a sugestão existir, uma rotina nova lê de hora em hora (08h25 às 17h25,
  dia útil, depois da rodada do pipeline) os pedidos prontos que ainda não têm leitura.
- Consequência: a sugestão só aparece **depois da rodada seguinte** (até cerca de 1h20 após
  o operador finalizar na Stokki). Quem etiqueta logo em seguida digita o número.
- `nucleo_pedidos.caixas` **não serve** (é o volume ponderado, `dimension_3`). A tela nunca
  busca na Stokki.

### O QR e a identidade da caixa

- Conteúdo: `FV|<PS>|<n>|<total>|<id>` — os quatro primeiros campos iguais aos de hoje, mais
  um identificador único e curto da caixa (ex.: 8 caracteres aleatórios, sem caracteres ambíguos).
- O QR antigo do piloto, com quatro campos, continua sendo lido e impresso como hoje.
- Na etapa 2 a leitura valerá pelo `<id>`: existe, pertence àquele PS e não está anulado.

### O que é gravado

Tabela nova (nome sugerido `expedicao_volumes`; **conferir na VPS se o nome está livre**
antes de criar — o banco local está congelado):

| campo | para quê |
|---|---|
| `id_caixa` (único) | o identificador que vai no QR |
| `codigo` | PS normalizado, pedido base |
| `numero`, `total` | "n / total" |
| `impressao_id` | trabalho na fila de impressão |
| `reimpressoes` | contador |
| `criado_em`, `criado_por` | quem etiquetou |
| `anulado_em`, `anulado_por` | preenchidos quando o total muda |

Só existe **um conjunto ativo** por pedido (todos com o mesmo `total`, numerados 1..total).

### Impressão

- Reaproveita `wms_fila_impressao` (tipo `VOLUME`, tamanho `10x15`) e o desenho de
  `wms_etiqueta_volume.desenhar_etiqueta_volume` sem mudar a arte.
- `gerar_pdf_volumes` passa a aceitar também o item com cinco campos e busca os dados pela
  ordem acima; o item de quatro campos segue pelo caminho atual (`wms_separacao.dados_do_volume`).

### Lista "prontos na Stokki sem etiqueta"

- Fonte: `vigia_stokki_abertos` com status "Waiting for Carrier" (o retrato que o pipeline
  grava a cada rodada) e sem conjunto ativo de volumes.
- É atrasada por natureza (até a próxima rodada); serve de cobrança, não de fila de trabalho.
- Mostra o total e os 50 mais novos. No dia em que entrar no ar a lista já nasce grande:
  em 28/11 havia 352 pedidos "prontos" na Stokki, todos sem etiqueta nossa.
- Clicar num item preenche o campo do pedido.

### Onde fica e quem acessa

- Entrada **Etiquetar** no app do galpão (`/wms`), para os mesmos níveis do app: `galpao`,
  `total` e `operador`. O nível `expedicao` **não entra** no app do galpão hoje; ele continua
  só imprimindo pela fila (`/etiquetas/fila`). Abrir a tela para ele exigiria uma página
  fora do app (não feito).
- As rotas ficam sob `/api/wms/`, que a tela `wms` de `acesso_telas.TELAS` já cobre.
- Sem chave de liga/desliga: a tela só imprime etiqueta, não envia nada a ninguém nem
  escreve em Stokki/Vuupt. O controle é o acesso por nível/tela.

## Decisões minhas (o Hugo pode trocar)

- Limite 300 caixas com confirmação acima de 99.
- Mudar o total anula todas as antigas e reimprime tudo (em vez de imprimir só a diferença):
  mais papel, mas nenhuma caixa fica com "2 / 5" num pedido de 6.
- Pedido desconhecido imprime com aviso em vez de bloquear.
- Sem chave de config.
- Tabela separada de `wms_volumes`; o piloto não muda.
- Sugestão de quantidade no cross só com fonte real já gravada.

## Fica para quem for construir

- Nome final da tabela e do módulo (sugestão: `painel_agentes/expedicao_volumes.py`, dados
  puros sem Flask, no estilo de `wms_fila_impressao.py`).
- Formato exato do `<id>` e como garantir unicidade.
- Onde exatamente a entrada aparece no app do galpão (depois da prévia aprovada).
- Existência (ou não) de total real de volumes da NF gravado para o cross.
- Testes: `unittest` de dentro de `painel_agentes/` (criar, repetir, reimprimir, mudar total
  anula, pedido desconhecido, QR de 4 campos intacto, limite e confirmação).
- Atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit.

## O que esta etapa deixa pronto para as próximas

- Cada caixa com identidade única e estado (ativa/anulada), ligada ao código do pedido que
  `nucleo_paradas` e o app do motorista já usam.
- Etapa 2 (bipagem no carregamento) e etapa 3 (liberar o checklist com foto do baú no app)
  ganham desenho próprio; perguntas já anotadas: quem bipa (carregador no nosso app ou
  motorista), o que fazer com caixa faltando, e o que muda no checklist atual (M3, B1, B5
  seguem pendentes lá).
