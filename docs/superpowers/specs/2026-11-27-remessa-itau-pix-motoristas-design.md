# Remessa Itaú (Pix) dos pagamentos dos motoristas — design

Data: 27/11/2026. Origem: pedido do Hugo ("conectar esses pagamentos com o Itaú para lançamentos automáticos com aprovação"). Decidido na conversa: **caminho A (arquivo de remessa CNAB 240, sem API)** e **opção 2 (gerar a remessa e ler o retorno)**, em duas etapas.

## Intenção e porquê

Hoje o financeiro lança cada pagamento de motorista, um a um, no Itaú. A tela `/financeiro/esporadicos` já calcula o que cada motorista tem a receber e já "marca como pago" (tabela `pagamentos_esporadicos`), mas o dinheiro ainda é movido à mão. Queremos que o sistema gere o arquivo de pagamentos por Pix, o financeiro o suba no Itaú Empresas, quem tem alçada aprove **no banco**, e o sistema leia o arquivo de retorno para só então considerar "pago" o que o banco efetuou, e mostrar o motivo do que foi rejeitado.

## Objetivos

1. Gerar o arquivo de remessa Pix (Sispag CNAB 240, layout de arquivo 080 / lote 040, outubro/2020) com os motoristas escolhidos na tela.
2. Mostrar por que um motorista não pode entrar na remessa (sem chave Pix, chave inválida, sem CPF, valor zero) em vez de pular em silêncio.
3. Três estados claros por pagamento: **a pagar** (pendente) → **enviado ao banco** (em remessa) → **pago**; mais **rejeitado** (volta a pendente, com o motivo).
4. Etapa 2: ler o arquivo de retorno do Itaú (upload na tela): ocorrência `00` = pago; rejeições voltam a pendente com o motivo escrito.
5. Rastreabilidade: quem gerou, quando, qual arquivo (nome + SHA-256), quais pagamentos, qual retorno.

## Não-objetivos

- API do Itaú / credencial ou certificado do banco no servidor. O sistema só **gera e lê arquivos**; quem sobe e aprova é o financeiro, no Itaú Empresas.
- Outras formas de pagamento (TED, boleto, DOC, QR Code) e os "pagamentos pontuais" (`pagamentos_pontuais.py`, tela `/financeiro/pagamentos`).
- Teleprocessamento/VAN/SFTP com o Itaú.
- Conciliação com o extrato bancário.

## Anti-objetivos (seria falha mesmo funcionando)

- Marcar como **pago** o que o banco não efetuou (por isso o estado intermediário "enviado").
- Uma rota entrar em duas remessas ativas (pagar duas vezes).
- Gerar arquivo com chave inválida e deixar o banco descobrir sozinho.
- Segredo ou dado bancário no repositório, no chat ou em log.

## Restrições

- Layout oficial `download.itau.com.br/bankline/sispag_cnab.pdf` (outubro/2020): registros de **240 colunas**, texto ASCII em maiúsculas, sem acento; Pix **em arquivo separado** de outras formas; estrutura header de arquivo (0), header de lote (1, forma de pagamento **45 = PIX Transferência**, tipo de operação `C`), por pagamento um segmento **A** + um segmento **B** (obrigatório para Pix por chave), trailer de lote (5), trailer de arquivo (9). No segmento A: câmara `009`, identificação da transferência (pos. 113-114) = `04` (Chave Pix); no B: tipo de chave (`01` telefone, `02` e-mail, `03` CPF/CNPJ, `04` aleatória) e a chave (pos. 128-227) no formato `+55...`, e-mail, CPF/CNPJ só dígitos, ou UUID.
- O Itaú valida CPF/CNPJ do favorecido contra a chave (`CD`/`BI` no retorno): chave de terceiro será rejeitada pelo banco e deve aparecer na tela.
- Dados da empresa pagadora (CNPJ, agência, conta, DAC, nome) ficam no `config.yaml` da **VPS** (seção `itau_remessa`), nunca no chat nem no repo. Sem a seção, nada disso age.
- Banco de produção é o da VPS; arquivos gerados em `dados/remessas_itau/` (a pasta `dados/*` já é ignorada pelo git).
- Convenções do repositório: português, `MAPA_DO_SISTEMA.txt` atualizado, commit por worktree limpo, restart do serviço conferido.

## Abordagens consideradas

1. **Só gerar o arquivo:** o financeiro marca "pago" à mão. Simples, mas não pega rejeição e deixa o "pago" depender de memória.
2. **Gerar + ler o retorno (escolhida pelo Hugo):** fecha o ciclo e dá ao financeiro o que bater. Construída em duas etapas (remessa, depois retorno) sobre o mesmo desenho.
3. **Gerar e já marcar como pago:** mais rápido, porém o "pago" pode ser falso (aprovador nega, chave errada). Descartada pelo anti-objetivo acima.

## Desenho

### Estados e dados (reaproveita `pagamentos_esporadicos`)

- `pagamentos_esporadicos` ganha: `situacao` (`PAGO` para todos os existentes e para "marcar como pago" manual; `ENVIADO` quando entra em remessa; `REJEITADO`), `remessa_id`, `ocorrencia` (código do retorno), `motivo` (texto), `pago_em` passa a ser a data efetiva do retorno quando vier de lá.
- Nova `remessas_itau`: `id`, `gerada_em`, `gerada_por`, `data_pagamento`, `qtd`, `valor_total`, `arquivo_nome`, `arquivo_sha256`, `retorno_em`, `retorno_por`, `retorno_sha256`.
- Rota continua presa por `pagamentos_esporadicos_rotas.rota_id UNIQUE`: enquanto o pagamento está `ENVIADO` ou `PAGO` a rota não volta a pendente. `REJEITADO` e "cancelar remessa" soltam as rotas (apaga as linhas de rotas, mantém o registro com o motivo), como o Desfazer de hoje.
- O botão **Marcar como pago** atual continua, renomeado "Marcar como pago (fora do Itaú)", para pagamentos feitos por outro meio.
- A tela mostra o estado de cada rota/pagamento; o filtro "Mostrar" ganha **Enviados** e **Rejeitados**; "Pendentes" não inclui enviados.

### Tela (em `/financeiro/esporadicos`)

- Seleção de motoristas pendentes (caixas de marcação) e botão **Gerar remessa Itaú**, com a **data de pagamento** (padrão: próximo dia útil via `regras/feriados.proximo_dia_util`; nunca anterior a hoje) e confirmação com quantidade e total.
- Motoristas não elegíveis aparecem com o motivo ao lado (sem Pix, Pix com formato inválido, sem CPF, só rota sem valor).
- Lista de remessas (data, quem, qtd, total, situação) com **baixar arquivo** de novo (mesmo conteúdo, conferido pelo SHA-256) e, na etapa 2, **enviar retorno**.
- Em remessa `ENVIADO`: **Confirmar pago** (manual, se o retorno não vier) e **Cancelar remessa** (solta as rotas).

### Geração do arquivo (módulo novo `painel_agentes/remessa_itau.py`)

- Funções puras: classificar chave Pix (tipo + valor normalizado), montar cada registro de 240 colunas por posição do manual, montar o arquivo, validar (todo registro com 240 caracteres, totais, contagens).
- `Seu Número` (pos. 74-93 do segmento A) = id do pagamento, para casar o retorno.
- Nome do favorecido: maiúsculo, sem acento, 30 caracteres; valor em centavos, 15 posições.
- Tudo numa transação: cria remessa + pagamentos `ENVIADO` + trava as rotas, grava o arquivo em disco e o SHA-256; falha no meio não deixa meia-remessa.

### Retorno (etapa 2)

- Upload do arquivo de retorno; lê os registros A pelo `Seu Número`; ocorrência `00` → `PAGO` (data efetiva); `BD` (agendado) mantém `ENVIADO`; qualquer outra → `REJEITADO` com o texto do manual (`NA` falta de autorização, `SS` saldo/limite, `BI`/`CD` CPF, `CN` conta, etc.). Arquivo reenviado é idempotente (mesmo SHA = nada muda).

### Acesso e segurança

- Gerar remessa, baixar arquivo e enviar retorno movem dinheiro: **somente nível `total`** (proposta; ver pendências). Rotas POST com `exige_mesma_origem` como as de hoje. Chave Pix e CPF não vão para log (só ids).
- Sinalizador `itau_remessa.ativo` (padrão `false`): desligado, a tela não mostra nada de remessa.

### Testes e implantação

- Testes unitários do gerador com asserções por posição (240 colunas, campos fixos, totais) e do analisador de chave Pix; teste do retorno com arquivo sintético (**não temos retorno real do Itaú**).
- Antes de usar em produção: validar um arquivo de teste com o Itaú (o manual prevê fase de testes) e fazer a primeira remessa com **um único pagamento de valor mínimo**.
- Deploy pelo fluxo do projeto; restart do `painel-agentes` conferido.

## Decisões minhas (a conferir)

- Reaproveitar `pagamentos_esporadicos` em vez de criar tabela paralela (um só "pago" no sistema).
- Data de pagamento padrão = próximo dia útil; arquivo gravado em `dados/remessas_itau/`.
- Nível `total` apenas para gerar e subir retorno.
- Sem retorno real para testar, o analisador segue estritamente o manual e falha de forma visível (linha desconhecida não muda estado).

## Decisões minhas, adicionadas após a checagem do desenho (a conferir)

- **Layout:** o texto do manual (PDF de outubro/2020) já foi lido e extraído; as posições de cada campo dos registros 0/1/A/B/5/9 e a tabela de ocorrências saem dele na hora de construir. Nenhum campo é inventado: o gerador cita a posição do manual em comentário e o teste confere por posição.
- **Um pagamento por motorista por remessa**, com o período da tela no momento de gerar (a remessa não junta períodos). `Seu Número` = id do pagamento.
- **Chave Pix: classificação.** CPF/CNPJ só se a chave tem 11/14 dígitos com dígito verificador válido; telefone só no formato `+55` + DDD + celular (ou 10/11 dígitos que comecem com DDD válido e 9, quando não forem o CPF cadastrado); e-mail se tem `@`; aleatória se é UUID. Chave de 11 dígitos que seja o CPF do motorista = CPF; que seja outro CPF válido = **chave de terceiro** (não elegível, aparece o motivo); ambígua = não elegível ("confirmar o tipo da chave"). Chave **CNPJ** (MEI) entra, com o CNPJ da própria chave como documento do favorecido. Antes de fechar a regra, olho os valores reais de `chave_pix` na produção (somente leitura) para ver os formatos que existem.
- **Cancelar remessa:** só enquanto o arquivo **nunca foi baixado**. Depois do download, o único caminho é o retorno do banco (ou "Confirmar pago" / "Marcar como rejeitado" manuais, com confirmação explícita dizendo para conferir no Itaú). Isso evita soltar a rota enquanto alguém ainda pode aprovar o arquivo no banco e pagar duas vezes.
- **Fixos:** entram na remessa (decisão do Hugo, 27/11). Fica o aviso na tela de que o valor deles vem da tarifa por km, e que o acerto real pode ser outro: o financeiro confere antes de subir o arquivo.
- **Migração:** pagamentos existentes viram `situacao = PAGO`. `pago_em` (hoje obrigatório) recebe a data de geração enquanto `ENVIADO` e passa a ser a data efetiva no retorno. `REJEITADO` **não** usa `desfeito_em`: é uma situação própria, com as rotas liberadas, e fica fora de "Pagos". Desfazer um pagamento `ENVIADO` não existe (usa Cancelar remessa).

## Decisões do Hugo (27/11/2026)

1. **Tipo de pagamento do lote:** `20 Fornecedores` (campo do header de lote; configurável em `itau_remessa.tipo_pagamento`).
2. **Fixos entram na remessa:** sim.
3. **Quem gera remessa / sobe retorno:** somente nível `total`.
4. **Dados da conta pagadora:** o Hugo coloca no `config.yaml` da VPS, fora do chat (seção `itau_remessa`).
5. **Layout e contrato:** o Hugo confirma com o gerente do Itaú se o layout de outubro/2020 é o vigente e se a conta tem Sispag com Pix por remessa. A remessa só é ligada (`itau_remessa.ativo`) depois do arquivo de teste validado pelo banco.

## O que fica para quem constrói

Nomes de colunas/arquivos, textos e disposição dos botões, mensagens de erro, ordem das migrações (`ALTER TABLE` idempotente como no resto do projeto).
