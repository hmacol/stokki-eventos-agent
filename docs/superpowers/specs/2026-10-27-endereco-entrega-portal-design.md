# Endereço de entrega no envio do portal: o cliente confere antes de ir pra Stokki

Data: 27/10/2026 · Pedido do Hugo · Status: design para aprovação
Prévia visual aprovada na conversa (casos 1 a 4, abaixo).
Continua: decisão do Hugo de 19/10 ("registrar toda alteração de endereço e, no próximo pedido do mesmo CNPJ, pedir confirmação"), que tinha ficado para a Fase 1 do freshhub; o Hugo pediu agora a versão no portal atual. E o trabalho de 24/09 (`<entrega>` do XML no portal), que nunca foi commitado.

## 1. Intenção

Hoje, quando o cliente sobe o XML no portal, o pedido vai pra Stokki com o endereço **fiscal** da nota (`enderDest`), a não ser que a nota traga o bloco `<entrega>`. Quando a entrega é em outro lugar (filial, depósito, doca, loja), a equipe descobre tarde e corrige na mão na Vuupt, ou o motorista vai ao lugar errado.

A tela de envio já pede, antes do "Confirmar envio", o horário de recebimento do destinatário e a data de agendamento quando ele exige. O pedido é que o **endereço de entrega** entre do mesmo jeito: o cliente vê pra onde o pedido vai e pode trocar ali mesmo. O endereço escolhido entra no XML como bloco `<entrega>`, para que Stokki, Vuupt e todo o resto já nasçam com o endereço certo.

Sucesso: nenhum pedido enviado pelo portal com a entrega em outro lugar chega à Vuupt com o endereço fiscal.

## 2. O que o cliente vê (prévia do envio, só pedidos por XML)

A coluna "Endereço" da prévia vira **Endereço de entrega** e mostra um de quatro casos:

1. **Nota sem `<entrega>` e destinatário sem alteração anterior:** endereço fiscal, o aviso *"Se a entrega for em outro lugar, informe abaixo. Sem alteração, o pedido vai pro endereço fiscal da nota."* e o botão **Alterar endereço de entrega**. O botão abre um formulário na própria linha: CEP (preenche rua, bairro, cidade e UF), número, complemento/referência, com os botões "Usar este endereço" e "Cancelar". O botão é **opcional**: sem mexer, segue o fiscal.
2. **Cliente alterou:** mostra o endereço novo com o selo "alterado por você" e o fiscal riscado embaixo, com os botões "Editar" e "Voltar pro endereço fiscal".
3. **Nota já traz `<entrega>`:** não tem conferência nem botão. Mostra o endereço da nota com o selo "informado na nota" e o fiscal embaixo, só como informação (decisão do Hugo de 24/09, mantida).
4. **Nota sem `<entrega>` e a última entrega desse destinatário foi em outro endereço** (seção 3): aparece o fiscal e um quadro *"No último pedido pra este destinatário (dd/mm) a entrega foi em X. Onde entregar este?"* com três botões: **entregar em X de novo**, **endereço fiscal** e **outro endereço** (abre o formulário do caso 1). **Escolha obrigatória:** o "Confirmar envio" não libera enquanto houver linha no caso 4 sem escolha, e a mensagem diz qual NF falta (no mesmo padrão do "informe o horário de recebimento").

A lista de envios, depois de confirmado, mostra o endereço de entrega efetivo e, quando ele difere do fiscal, o fiscal como nota (o que o trabalho de 24/09 já faz).

Planilha fica de fora: lá o endereço digitado já é o de entrega e não existe "fiscal" (decisão minha, confirmada pelo Hugo).

**NF com transportadora de redespacho (TERCEIROS) ou retirada** (decisão minha): sem formulário e sem caso 4. A importação troca o endereço pelo galpão da transportadora ou pelo nosso (`regras/endereco.py`), e um `<entrega>` inserido por nós faria o pipeline entregar no cliente em vez de no galpão (o Local de Entrega dentro da área tem prioridade sobre o redespacho), quebrando o redespacho sem ninguém ver. A linha mostra "Redespacho: entregue à transportadora X" ou "Retirada", como informação.

## 3. "A última vez" (caso 4): todas as alterações contam

Decisão do Hugo: contam **todas** as alterações, do cliente e da equipe.

- **Chave:** embarcador + documento do destinatário. A memória **não atravessa embarcadores**: o cliente A não vê o endereço que o cliente B usou para o mesmo destinatário (decisão minha, confirmada pelo Hugo).
- **Quais pedidos contam** (decisão minha): os desse par (embarcador, destinatário), de qualquer origem (portal, importação, Stokki direto), que já tiveram **tentativa de entrega concluída** (entregue ou insucesso), mais os enviados pelo portal com escolha explícita de endereço (`cliente`, `ultima_entrega`, `nota`) mesmo que ainda abertos, porque o cliente já disse onde é. Ficam de fora: cancelados, pedidos abertos sem escolha explícita (um pedido importado hoje no fiscal e ainda não roteirizado não apaga a lembrança de ontem) e pedidos de **redespacho (TERCEIROS) e retirada**, cujo endereço na Vuupt é o galpão da transportadora ou o nosso. Reentrega (`-R1`, `-R2`) conta como tentativa do mesmo pedido.
- **Ordem:** pela data da tentativa concluída (`nucleo_paradas`/rota do dia) ou, para os abertos com escolha no portal, pela data do envio. É essa data que aparece no "dd/mm" do quadro. Nunca usar o `created_at` da Vuupt sem tratar o fuso (vem em UTC sem fuso).
- **O endereço que valeu de fato**, pra cada pedido: **o da Vuupt** (`nucleo_pedidos.endereco`, onde aparece a correção que a equipe faz na Vuupt ou pelo painel) **sempre que existir**, porque é o que o motorista usou. O portal e o `<entrega>` servem pra dar os campos estruturados quando o CEP bate com o da Vuupt. Quando o CEP da Vuupt diverge do que o cliente escolheu, vale a Vuupt (a equipe corrigiu depois). Sem serviço na Vuupt (pedido aberto com escolha no portal), vale a escolha do portal. É a mesma leitura da carga do freshhub (`carregar_alteracoes`: portal e `<entrega>` são o ponto de partida, o núcleo é o que valeu).
- Endereços que a equipe aplicou a partir de mensagem do cliente (`fonte="mensagem_llm"` em `regras/endereco.py`) também contam, como qualquer correção. No pior caso aparece a pergunta do caso 4, e quem decide é o cliente.
- **Quando vira caso 4:** o endereço que valeu na última entrega é diferente do endereço fiscal **da nota que está sendo enviada agora**. Se a última entrega foi no próprio fiscal, não aparece nada (caso 1).
- **Comparação:** pelo **CEP** quando a fonte é a Vuupt (o texto do núcleo não separa o número com segurança; é a mesma lição da carga do freshhub, `carregar_alteracoes`). Quando as duas pontas são estruturadas (portal ou `<entrega>`), compara CEP + número. Consequência aceita: correção da equipe que só troca o número na mesma rua não vira caso 4.
- **"Entregar em X de novo" com X vindo da Vuupt:** o texto não dá os campos que o `<entrega>` precisa, então o botão abre o formulário **já preenchido** (CEP consultado, número extraído quando der) pra o cliente só conferir (decisão minha).

## 4. Depois do "Confirmar envio"

- O endereço escolhido (ou nenhum, quando segue o fiscal) fica gravado no envio (`portal_envios`) junto com a origem: `fiscal`, `nota`, `cliente`, `ultima_entrega`. Isso alimenta o caso 4 dos próximos pedidos e a carga do freshhub (que já procura alterações de endereço).
- **O XML que vai pra Stokki ganha o bloco `<entrega>`** com o endereço escolhido, montado no worker `enviar_stokki.py`, no mesmo ponto em que a `regra_xml` do embarcador já transforma o XML antes do envio. O arquivo original do cliente fica intocado no disco (o fiscal não se perde). Quando a nota já tem `<entrega>` (caso 3), nada é mexido.
- **Checagem de área atendida e aviso de fora do dia fixo** passam a usar o endereço de entrega, não o fiscal: um pedido entregue em área atendida não pode ser barrado pelo endereço fiscal (decisão minha, confirmada pelo Hugo). A exceção de hoje continua: transportadora TERCEIROS/RETIRADA passa.
- Daí em diante nada muda: a Stokki e a Vuupt já usam o `<entrega>` (o pipeline e a importação já leem esse bloco).

## 5. Antes de construir / ligar

1. **Resgatar o trabalho de 24/09:** a worktree `.claude/worktrees/endereco-entrega` (ramo `endereco-entrega-xml`) tem, sem commit, `ler_nfe` lendo `<entrega>`, a coluna `endereco_fiscal` e o aviso na tela, mais `portal_cliente/test_endereco_entrega_xml.py` (8 testes). Foi montada sobre e5cd18e, bem atrás da master atual: levar pra cima da master e commitar como primeiro passo.
2. **Prova na Stokki (com o Hugo, antes de ligar):** subir um XML com `<entrega>` **inserido por nós** e ver se a Stokki aceita. Inserir o bloco invalida a assinatura digital da NF-e. Se a Stokki recusar, o plano B é mandar o XML original e aplicar o endereço de entrega depois (no pedido da Stokki ou no serviço da Vuupt). Isso muda a seção 4, então volta pro Hugo antes de seguir.
3. O `<entrega>` do layout da NF-e pede o código IBGE do município (`cMun`). Hoje `portal_cliente.cotacao.consultar_cep` não devolve esse código; o ViaCEP devolve (`ibge`).
4. O `<entrega>` também pede CNPJ ou CPF. O formulário não pede documento: usar o do destinatário (decisão minha). A prova do item 2 confere se a Stokki usa esse documento pra identificar o cliente. Se usar, a entrega numa empresa com outro CNPJ (outro CD) precisa de um campo de documento, e isso volta pro Hugo.

## 6. Não-objetivos

- Pedidos que não passam pelo portal (importação por e-mail, cliente lançando direto na Stokki): seguem como hoje. Eles só **alimentam** a memória do caso 4.
- Trocar o endereço de entrega **depois** de confirmado o envio pelo portal (continua por chamado e pela equipe).
- Pendência de liberação para a equipe no painel: no portal, quem confirma é o próprio cliente, na hora do envio. A pendência para a equipe continua prevista na Fase 1 do freshhub.
- Memória de horário e data: o horário de recebimento já é lembrado por destinatário (`portal_destinatarios`), e a data de agendamento é por pedido. Não muda nada aqui.

## 7. Fracasso mesmo funcionando

- Um pedido ir pro endereço da última vez **sem o cliente ter escolhido** (o caso 4 nunca decide sozinho).
- O endereço fiscal se perder: o XML original e o `endereco_fiscal` ficam guardados.
- Um embarcador ver endereço usado por outro embarcador.
- A Stokki aceitar o XML mas gravar o endereço fiscal (a prova da seção 5 precisa conferir o **endereço gravado na Stokki e no serviço da Vuupt**, não só "o pedido foi criado").
- O caso 4 aparecer por diferença de texto no mesmo endereço (correção de geocodificação da equipe com o mesmo CEP não pode virar pergunta).

## 8. Liga e desliga

Atrás de uma chave no config (`portal_cliente.envios.endereco_entrega_ativo`, padrão desligado), para o deploy poder sair antes da prova da seção 5 (decisão minha). Com a chave desligada, a tela é a de hoje mais o trabalho de 24/09 (que só informa).

## 9. Fica para quem constrói

- Como achar "o pedido mais recente do par" no núcleo (o `nucleo_pedidos.destinatario_codigo` vem formatado, então é preciso normalizar dígitos; o embarcador sai de `sender_id`/`remetente_codigo`) e o desempenho dessa consulta na prévia.
- O formato das colunas novas em `portal_envios`.
- O layout fino do formulário (seguir `_envios.html`: `.dest`, `.ag`, `.chip`, os mesmos tokens de cor e o tema escuro).
- Testes: caracterizar `ler_nfe` e a montagem do `<entrega>` com XML sintético (nunca com dado de cliente no repo).

## 10. Freshhub

O portal novo (Fase 1.3, ramo `fase1-3-entrada` em `C:\freshhub`) vai substituir este envio. A mesma regra (casos 1 a 4, memória de todas as alterações) precisa estar lá antes da virada dos embarcadores. Não entra nesta entrega, mas fica registrado pra não se perder na virada.
