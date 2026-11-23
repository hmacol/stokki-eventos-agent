# Recebimento em três etapas: receber, conferir recebimento e guardar

Data: 22/11/2026. Dono: Hugo. Prévia das telas: https://claude.ai/artifact/3HGL6Zz48wN71H2MQmDS3N

(O arquivo se chama "duas etapas" porque o desenho começou assim; a etapa Receber entrou depois, a pedido do Hugo.)

## Intenção

Hoje, na tela Receber do `/wms`, cada linha pede lote, validade, quantidade e **posição** de uma vez. Quem confere a carga tem que decidir na hora onde guardar, o sistema não ajuda a escolher o lugar, e não fica registro de como a carga chegou.

O Hugo quer três etapas, que podem ser feitas por pessoas e em momentos diferentes:

1. **Receber**: checklist da chegada do caminhão, com fotos.
2. **Conferir Recebimento**: conferência da carga, lotes, etiquetagem, entrada no sistema.
3. **Guardar**: colocar o produto no melhor local do estoque.

## Objetivos

- Registro da chegada (motorista, placa, fotos, temperatura) visível para o cliente.
- Conferir sem escolher posição. O saldo entra no sistema na conferência e já atende pedido.
- Uma fila "Guardar" com tudo que foi conferido e ainda não tem lugar.
- O sistema sugere a posição e diz o motivo; o operador confirma pelo bipe.
- O cliente continua sendo avisado de falta ao encerrar a conferência, sem esperar a guarda.

## Fora do escopo

- Capacidade por posição (quanto cabe). "Vazia" é o único critério de espaço.
- Mais de uma doca.
- Pallet como unidade (etiqueta de pallet).
- Mudança na réplica para a Stokki e nas regras do encerramento.
- O botão "Entrada" avulso do Início: continua lançando direto na posição.
- Recusa de carga pelo galpão: quem decide é o escritório.

## O que seria um fracasso

- Caixa ou reserva apontando para uma posição onde o produto não está, depois da guarda (a separação tira dali o "onde buscar" e a baixa do saldo).
- Carga com temperatura fora da faixa sendo conferida e entrando no estoque sem o escritório ter decidido.
- E-mail saindo para cliente sem o Hugo ter ligado.
- A mudança entrar no ar para um embarcador sem o Hugo ligar.

## Abordagens consideradas

**A. Saldo entra na conferência, numa posição de espera DOCA; guardar é uma transferência DOCA → posição.** Escolhida. O Hugo pediu que a conferência já "coloque para o sistema". Reaproveita o que existe: entrada, transferência, `mover_caixa`, encerramento, e a checagem de caixas x saldo continua batendo porque a doca é uma posição como as outras.

**B. A conferência só anota as quantidades; o saldo só nasce na guarda.** Descartada: contraria o pedido, e o estoque conferido ficaria invisível até alguém guardar.

**C. Etiquetagem como etapa separada.** Descartada: a etiqueta da caixa leva lote e validade, lidos na conferência, e o bipe dela já é a contagem. Separar faria contar duas vezes.

## Decisões do Hugo (22/11)

1. **A doca atende pedido.** O que está na DOCA pode ser reservado, separado e escolhido como lote. (Primeiro o Hugo pediu a doca bloqueada; voltou atrás no mesmo dia.)
2. **Doca única.**
3. **Ordem dos critérios de sugestão** aprovada como proposta (abaixo).
4. **O recebimento conta como concluído no fim da conferência.** A guarda é pendência interna.
5. **Etiquetagem junto com a conferência.** O botão "Pedir etiquetas de todas as linhas" entra.
6. **Guarda de caixa serializada**: tudo numa posição, um bipe move todas as caixas do lote; em partes, bipa as caixas que vão para a posição lida.
7. **Etapa Receber** com checklist: fotos do recebimento, temperatura se refrigerado, foto do caminhão aberto, nome do motorista, placa. Liberdade para acrescentar itens.
8. **Receber é obrigatório** antes de Conferir Recebimento.
9. **Faixas de temperatura**: refrigerado 0 a 7 °C; congelado -18 a -12 °C; varia conforme o produto.
10. **Fora da faixa**: o escritório é notificado, comunica o cliente e verifica se a carga será recebida. O galpão não recusa pela tela.
11. **O checklist fica disponível para o cliente** no pedido de entrada do portal, e há notificações por e-mail.
12. **Liga por embarcador.**
13. **O portal não esconde** lote que só existe na doca.
14. **E-mail "sua carga chegou"** ao concluir o Receber, com **Relatório de Recebimento em PDF** com o logo, formal, para documentação.
16. **Receber fora da faixa exige liberação formal do cliente**, por link de aprovação. Documento aprovado pelo Hugo em 22/11 com esse acréscimo.
15. **Sem tela nova no portal** nesta entrega; o cliente verá o checklist quando a aba Pedidos de Entrada entrar.

## Como funciona

### Posição DOCA

Uma posição única, de um tipo de área novo ("Doca"). O operador não precisa bipar a doca: a conferência lança nela automaticamente. Na tela aparece como "DOCA". Para reserva, separação e escolha de lote ela vale como qualquer posição; o "onde buscar" da separação mostra DOCA.

*Chamado meu:* o código interno da posição fica com quem implementar (o formato atual é `ÁREA-E<n>-N<n>` ou `ÁREA-P<n>`).

### Etapa 1: Receber (checklist da chegada)

Botão "Receber" no Início, com a lista das cargas esperadas. O operador escolhe a carga e preenche, em duas telas.

Quem chegou:
- Recebimento esperado. *Chamado meu:* pode marcar mais de um, quando o mesmo caminhão traz mais de um recebimento; o checklist vale para todos.
- Nome do motorista e placa do veículo (Hugo).
- Foto do caminhão aberto, antes de descarregar (Hugo). Obrigatória.
- *Acréscimos meus:* transportadora (opcional), foto da placa, foto da nota fiscal, hora de chegada e operador gravados sozinhos.

Como chegou:
- Temperatura, só para carga refrigerada ou congelada (Hugo), com foto do termômetro (*acréscimo meu*).
- Fotos da carga (Hugo). Pelo menos uma.
- *Acréscimos meus:* quantidade de volumes ou pallets; número do lacre, se tiver; "a carga chegou inteira?" (avaria exige foto e observação); observação livre.

**Temperatura.** Faixas padrão no config: refrigerado 0 a 7 °C, congelado -18 a -12 °C. *Chamado meu para o "varia conforme o produto":* o produto pode ter faixa própria (opcional); a carga está fora da faixa se a medição sai da faixa de qualquer produto dela.

**Fora da faixa** (decisão 10, mais a liberação formal do cliente pedida pelo Hugo): a tela avisa o operador, o checklist é concluído normalmente e a carga fica parada, fora de Conferir Recebimento. O escritório recebe um e-mail com a medição e as fotos. Receber mesmo assim exige **liberação formal do cliente, por link de aprovação**:

1. No `/wms` (níveis total e operador), o escritório clica "Pedir liberação ao cliente". O sistema manda ao cliente um e-mail com um link e mostra o link para copiar (para mandar também por WhatsApp).
2. O link abre uma página pública, sem login, com a carga, a temperatura medida, a faixa e as fotos. O cliente informa o nome e escolhe "Autorizo o recebimento" ou "Não autorizo".
3. Fica gravado quem respondeu, quando e o que respondeu. O escritório é avisado por e-mail.
4. Autorizou: a carga é liberada para Conferir Recebimento. Não autorizou: o escritório encerra a carga como "não recebida", com motivo.

*Chamados meus:*
- A autorização do cliente libera a carga sozinha, sem um segundo clique do escritório.
- O link vale para um recebimento, por um token longo e difícil de adivinhar, e deixa de aceitar resposta depois da primeira.
- Saída para quando o cliente responde por outro meio: o escritório pode "Registrar liberação recebida por fora", com texto obrigatório dizendo quem autorizou e por onde. Fica marcado como registrado pelo escritório, não pelo cliente.
- "Não receber" pelo escritório continua existindo sem depender do cliente.
- O Relatório de Recebimento em PDF registra a liberação: quem autorizou, quando e por qual via.
- A página do link fica no serviço do portal do cliente (já é público e fala com o cliente). É a única página nova fora do `/wms`; não é a tela do checklist no portal, que continua fora desta entrega.
- O e-mail do link segue a regra dos envios a cliente (nasce preso em `forcar_destino`). Enquanto estiver preso, o escritório usa o link copiado.

**Avaria** (*chamado meu*): notifica o escritório por e-mail, mas não segura a carga.

Sem checklist concluído (e, se for o caso, liberado pelo escritório), a carga não aparece em Conferir Recebimento.

### Cliente: portal e e-mail

- O checklist completo (dados, fotos, temperatura, horário) fica visível para o cliente no pedido de entrada do portal.
- **Dependência (decidido pelo Hugo):** a aba Pedidos de Entrada do portal não está em produção (ramo `pedidos-entrada`, sem merge; spec v2 esperando aprovação). A exibição no portal entra junto com essa aba; **esta entrega não cria tela nova no portal**. O checklist e o PDF são gravados de forma que o portal só precise ler.
- **E-mail ao cliente (aprovado pelo Hugo):** um e-mail quando o Receber é concluído e liberado ("sua carga chegou"), com o resumo do checklist e as fotos, para os mesmos destinatários do relatório de faltas. O relatório de faltas do encerramento continua como hoje. Carga fora da faixa não dispara esse e-mail antes da decisão do escritório.
- **Relatório de Recebimento em PDF (pedido do Hugo):** documento formal, com o logo da Fresh Log, para documentação. Vai anexado ao e-mail "sua carga chegou" e fica disponível para baixar dentro do recebimento no `/wms`. *Chamados meus:* o conteúdo é o checklist da chegada (carga, embarcador, NF, data e hora, motorista, placa, transportadora, temperatura medida e faixa, volumes, lacre, condição da carga, observação, operador, fotos); quando a carga foi liberada pelo escritório fora da faixa, o PDF registra quem liberou e quando; o resultado da conferência (anunciado x recebido) não entra nele, porque o PDF nasce antes da conferência e as faltas já têm relatório próprio. O layout segue os PDFs que o projeto já gera (comprovante de entrega, romaneio) e fica com quem implementar.
- Como todo envio a cliente no projeto: nasce desligado e com `forcar_destino`; ligar é do Hugo.

### Etapa 2: Conferir Recebimento

A tela de hoje (`TELAS.recebimento` em `wms.html`) sem o campo Posição, com o nome "Conferir Recebimento".

- **Produto sem caixa serializada**: lote, validade, quantidade, "Registrar conferido". Lança ENTRADA na DOCA e contabiliza na linha.
- **Produto com caixa serializada**: lote, validade, nº de caixas, pedir etiquetas, colar, bipar cada caixa. Cada bipe ativa a caixa na DOCA e soma no conferido.
- **"Pedir etiquetas de todas as linhas"**: pede de uma vez as etiquetas de todas as linhas com lote, validade e nº de caixas preenchidos. *Chamado meu:* por baixo é um pedido de impressão por linha, enviados juntos, para o contador de cada linha continuar valendo.
- Sobra além do anunciado continua pedindo confirmação.
- **Encerrar conferência**: mesma tela e mesmas regras de hoje. Só acrescenta a informação de que os itens foram para a fila Guardar.

Os estados do recebimento que já existem (`ESPERADO`, `ENDERECADO`, `DIVERGENCIA`) não mudam de sentido; o texto na tela troca "endereçado" por "conferido". Os estados novos do Receber (aguardando checklist, aguardando escritório, não recebido) ficam com quem implementar. *Chamado meu.*

Um item aparece na fila Guardar assim que é conferido, sem esperar o encerramento.

### Guardar com pedido reservado na doca

Consequência da decisão 1. A reserva do pedido é presa à posição, e hoje o sistema recusa mover caixa reservada (`wms_caixas._recusar_se_reservada`). Sem tratar isso, a guarda travaria justamente no que já tem pedido.

Regra: **ao guardar a partir da DOCA, a reserva acompanha o produto.** Se, depois da guarda, as reservas ativas daquele produto + lote + validade na DOCA passam do que sobrou lá, o excedente é reapontado para a posição de destino, na mesma transação da transferência. Vale só para origem DOCA; mover entre posições normais continua recusando caixa reservada, como hoje.

Continua valendo: caixa com unidade já separada para um pedido não se move até a baixa (ela já está saindo; não precisa ser guardada). Essa caixa some da fila Guardar.

A mesma regra vale para produto sem caixa serializada (transferência por quantidade).

### Etapa 3: Guardar

**Fila** (tela nova, botão "Guardar" no Início com contador): uma linha por produto + lote + validade com saldo na DOCA, juntando todos os recebimentos. Ordem: refrigerado e congelado primeiro, depois mais tempo na doca. Cada linha mostra quantidade, recebimento, embarcador, tempo na doca, a posição sugerida (ou "sem sugestão") e, se houver, "reservado para N pedidos".

**Guardar um item**: abre pelo toque na fila ou bipando uma caixa do item. Mostra a sugestão em destaque com o motivo e até duas alternativas. O operador bipa a posição:

- Sem caixa serializada: informa a quantidade (padrão: tudo) e confirma. Pode repetir para dividir.
- Com caixa serializada, tudo numa posição: confirma e o sistema move todas as caixas daquele produto + lote + validade que estão na DOCA.
- Com caixa serializada, em partes: bipa as caixas que vão para a posição lida e confirma; só as bipadas são movidas. O resto continua na DOCA e na fila. *Chamado meu:* cobre mais de duas posições e o caso de não caber tudo.

Bipar posição diferente da sugerida é aceito sem pergunta. A única trava é temperatura: produto com temperatura conhecida em área de temperatura diferente pede confirmação ("Bipar outra" / "Guardar aqui mesmo"), e o "aqui mesmo" fica registrado com o operador.

### Sugestão de posição

Em ordem, a primeira que achar:

1. Só áreas da temperatura do produto.
2. Posição que já tem o mesmo produto e o mesmo lote.
3. Posição vazia ao lado de onde o produto já está.
4. Posição vazia perto de produtos do mesmo embarcador.
5. Qualquer posição vazia da área certa.

Sem posição vazia na área certa: "sem sugestão", o operador escolhe.

Leitura minha: lote diferente do mesmo produto não é sugerido na mesma posição (cai no critério 3), mas o operador pode bipar lá.

"Ao lado" e "perto" ficam com quem implementar. Sugestão: mesma estante primeiro, depois estante vizinha na mesma área; "perto do embarcador" = área com mais posições ocupadas por ele.

### Temperatura do produto

O cadastro do produto não tem temperatura hoje (só as áreas têm). *Chamado meu:*

- Campo novo no produto (ambiente, refrigerado, congelado) e faixa própria opcional, editáveis na tela do produto por quem já edita "validade obrigatória".
- Sem o campo preenchido, o sistema deduz da área onde o produto já tem saldo.
- Sem nenhum dos dois: não trava a guarda, e a fila mostra "sem temperatura".
- No checklist do Receber vale só o cadastro: a tela pede temperatura quando a carga tem produto cadastrado como refrigerado ou congelado. Produto sem temperatura no cadastro não faz o checklist pedir temperatura, mesmo que já esteja guardado numa câmara. (Ajuste feito ao escrever o plano: a dedução pela área serve para sugerir posição, não para exigir medição.) Por isso, antes de ligar um embarcador de produto frio, é preciso marcar a temperatura dos produtos dele.

### Entrada no ar

- Liga **por embarcador**, no `config.yaml`, seção `wms`, no mesmo molde das listas de piloto que já existem. Embarcador fora da lista recebe exatamente como hoje. Ligar é decisão do Hugo.
- Saldo que já está em posição não muda.
- Tirar um embarcador da lista com saldo na doca: a fila Guardar continua mostrando esse saldo até zerar.

## Fica com quem implementar

- Código da posição DOCA, estados novos e migração.
- Nomes de rotas e funções, formato da resposta da sugestão.
- Onde as fotos são guardadas (o projeto já guarda anexos em `dados/<área>/<id>/` com cópia no GCS).
- Comportamento sem rede no checklist e na guarda (a conferência já tem fila local).
- Testes, seguindo os `test_wms_*.py` existentes, com atenção especial à reserva que acompanha a guarda.
- Atualizar o `MAPA_DO_SISTEMA.txt`.
