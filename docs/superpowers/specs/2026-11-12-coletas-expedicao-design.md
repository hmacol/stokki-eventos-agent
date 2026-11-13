# Coletas na Expedição: folha impressa, foto e expedição na Stokki

Data: 12/11/2026. Pedido do Hugo. Estado: desenho para aprovação, nada construído.

## Por quê

Pedido de retirada (o cliente ou a transportadora dele busca no galpão) não sobe pra Vuupt desde 06/11.
Ele fica em "Aguardando Transportador" na Stokki até alguém marcar a saída lá dentro, à mão. Hoje são
59 pedidos assim (Nonna 30, Laticínios Dourado 13, Padrão Puro 12, outros 4), e na tela /pedidos eles
ficam como "Importado" para sempre. Não existe registro nosso de quem levou a carga.

O Hugo escolheu (12/11): a expedição do galpão confirma a coleta no painel; o sistema gera um documento
para imprimir, o motorista preenche com os dados dele, e a foto desse documento é obrigatória para
confirmar.

## O que se quer

1. A saída de pedido de retirada passa a ser feita no nosso painel, não na Stokki.
2. Toda coleta deixa prova: a foto da folha preenchida e assinada por quem levou.
3. O pedido coletado deixa de aparecer como "Importado" na /pedidos.

Fora do escopo: link para a transportadora confirmar sozinha, confirmação pelo portal do cliente,
redespacho (transportadora tipo TERCEIROS), Lalamove, aviso ao embarcador de que a carga saiu.

Seria falha mesmo funcionando: pedido virar Enviado na Stokki sem foto; pedido que não saiu ser
expedido junto; a mesma carga constar em duas folhas.

## Como funciona, na ordem do dia

### 1. Aba "Coletas" na tela /expedicao

Lista os pedidos em "Aguardando Transportador" cuja transportadora é de retirada, agrupados por
transportadora, com a contagem (J3 Flex: 15, Cliente Retira: 17...). Cada linha: código PS, embarcador,
unidades e itens (o que o espelho da Stokki já tem) e, quando o pedido veio pelo portal, NF,
destinatário e volumes.

"É de retirada" segue a regra que o robô de expedição já usa: nome com "CLIENTE RETIRA" ou tipo
RETIRADA no cadastro de transportadoras. Pedido com transportadora fora dessa regra (ou sem
transportadora) aparece num grupo à parte, "Transportadora sem cadastro de retirada", só com o aviso e
o atalho para /transportadoras: não entra em folha.

"Cliente Retira" junta embarcadores diferentes; dentro desse grupo a lista se divide por embarcador,
porque cada um manda uma pessoa.

### 2. Gerar a folha de coleta

O caminhão chega. O expedidor marca os pedidos que vão sair (ou "todos" do grupo) e clica "Gerar folha".
O sistema cria a coleta com número próprio (COL-1, COL-2...) e abre o PDF para imprimir. Uma página A4
em pé, com:

- cabeçalho: Fresh Log, "Folha de coleta COL-n", data e hora, transportadora, quem gerou;
- tabela dos pedidos: código PS, embarcador, NF e destinatário (quando há), unidades/volumes, e uma
  coluna "Levou" com quadradinho para marcar à caneta;
- total de pedidos e de volumes;
- campos em branco para o motorista: nome completo, CPF ou RG, placa, data e hora, assinatura;
- uma linha de declaração: "Recebi os pedidos marcados acima."

Pedido que está numa folha aberta sai da lista "a coletar" e não entra em outra folha. Folha aberta pode
ser reimpressa e pode ser cancelada (os pedidos voltam para a lista).

### 3. Confirmar a coleta

Com a folha preenchida, o expedidor abre a coleta na tela (a aba mostra as "folhas abertas" no topo) e:

- desmarca o que não saiu (por padrão vem tudo marcado);
- tira ou anexa a **foto da folha preenchida**: obrigatória. Sem foto o botão fica desligado e o
  servidor recusa;
- digita o nome do motorista e a placa: **obrigatórios** (Hugo, 12/11).

Ao confirmar, os pedidos marcados entram na fila de execuções do painel com o mesmo robô do botão
"Expedir na Stokki" da /pedidos (`expedir_pedidos.py --tela-pedidos`), e a tela acompanha o resultado
pedido a pedido. Os desmarcados voltam para a lista "a coletar".

Pedido que o robô não conseguiu expedir fica na coleta como "falhou", com o motivo e um botão "tentar de
novo" (não pede foto outra vez). A coleta só fica "concluída" quando todos os marcados viraram Enviado.

### 4. Depois

- Aba Coletas tem o histórico: número, data, transportadora, quantos pedidos, quem confirmou, e a foto.
- Na /pedidos, pedido que a Stokki mostra como Enviado e que nunca passou pela Vuupt aparece com o
  status novo **"Coletado"** (fila e aba próprias, como o Importado). Quando houver coleta nossa, o
  painel do pedido mostra o número da coleta, quem levou e o link da foto. Pedido marcado direto na
  Stokki, sem folha, também vira Coletado, sem esses dados.

## Decisões do Hugo (12/11)

- A foto obrigatória é **uma: a folha de coleta preenchida e assinada**. Não guardamos foto de documento
  pessoal do motorista.
- Botão "Expedir na Stokki" da /pedidos: para pedido de retirada **anterior à entrada da aba Coletas em
  produção** continua valendo como atalho, sem foto (limpar o que está acumulado). Pedido de retirada
  **criado a partir desse dia** é recusado no botão, com a mensagem "retirada sai pela aba Coletas", e só
  expede por coleta com foto. Para pedido entregue preso na Stokki o botão não muda.
- Os logins fixos dos aparelhos (`expedicao` e `galpao`) geram a folha e confirmam a coleta, além de
  total e operador. O registro guarda o nome do login.

## Decisões de quem escreveu (não foram ditas pelo Hugo)

- A data de corte do botão é configurada (o dia do deploy em produção), comparada com a data em que o
  pedido foi visto pela primeira vez na Stokki. Os pedidos antigos também aparecem na aba Coletas: para
  eles valem os dois caminhos.
- O robô recebe da coleta um sinal de que há foto; sem ele, retirada nova é recusada no próprio robô
  (a trava não fica só na tela).
- Atendimento e leitura veem a aba e o histórico, sem gerar nem confirmar.

- (decidido pelo Hugo) Nome do motorista e placa são digitados e **obrigatórios** na confirmação, junto com a foto.
- Uma folha por transportadora por coleta; dá para ter várias no mesmo dia.
- A foto fica em `dados/coletas/`, servida só para quem tem acesso à tela, como as outras fotos do painel.
- **Fonte da lista (corrige a 1a versão deste desenho).** O coletor do galpão NÃO serve: ele só acompanha
  os 4 estados abertos e para de olhar o pedido quando chega em "Aguardando Transportador" (1.455 linhas
  paradas nesse estado no espelho, 2 em Enviado). Então a aba lê de uma leitura própria da listagem
  "Aguardando Transportador" da Stokki (que traz a transportadora), feita por um job separado a cada
  10 min no horário comercial, gravada em tabela própria. Pedido que some dessa listagem sem estar
  cancelado é tratado como Enviado. O job respeita a trava da sessão Stokki compartilhada.
- "Coletado" na /pedidos vale só para pedido com transportadora de retirada, visto por esse job; o que
  saiu antes de o job existir continua como está hoje.
- No grupo "Cliente Retira" a folha é travada por embarcador (uma folha, um embarcador).
- Retirada antiga que ainda tem serviço [RETIRADA] aberto na Vuupt entra na aba normalmente; o robô
  já fecha o serviço, e na /pedidos ela segue como Entregue, com os dados da coleta no painel do pedido.
- Folha tem no máximo 30 pedidos (limite do robô por execução e o que cabe em uma página): uma foto.
- Estados: folha aberta não expira e qualquer usuário da aba pode cancelar; confirmar com tudo
  desmarcado cancela a folha; pedido que falhou sem conserto (ex.: cancelado na Stokki) pode ser
  retirado da coleta para ela concluir; pedido já Enviado direto na Stokki conta como feito.
- Homologação: a coleta conclui simulada (pedidos saem da lista e viram Coletado só lá), sem Stokki.
- Tabelas com prefixo `expedicao_coletas` (`galpao_coletas` já existe e é outra coisa).
- Status "Coletado" é calculado na tela, como o Importado: não é gravado no espelho da Vuupt.

## Restrições

- Na homologação nada vai pra Stokki: confirmar a coleta só registra.
- Expedir não tem volta simples: a primeira coleta real é acompanhada, num pedido que o Hugo indicar.
- Tirar um pedido de uma Expedição Programada que tem outros pedidos ainda não foi provado de verdade; o
  robô já trata o caso, mas a primeira ocorrência precisa ser conferida.
- Tela nova entra em `acesso_telas.TELAS` e no MAPA_DO_SISTEMA.txt.
- A tela é interna (painel com login): não passa pela revisão de módulo exposto na internet.

## Fica para quem constrói

Nomes de tabelas e rotas, biblioteca do PDF (seguir a do romaneio), tamanho máximo e formato da foto,
layout exato da folha e da aba (computador e celular, já que a foto sai do celular).
