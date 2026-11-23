# Clientes com boleto e aviso de boleto faltando ao embarcador — Design

Data: 06/10/2026. Pedido do Hugo: "marcar os clientes que têm boleto para podermos informar o embarcador", por e-mail, WhatsApp e notificação no atendimento.

## Objetivo

Saber, por par **embarcador + cliente (destinatário)**, se aquele cliente paga com boleto. Com isso, avisar o embarcador quando um pedido desse cliente vai sair (ou saiu) sem boleto, para ele mandar a tempo.

Sucesso:
- O embarcador recebe o aviso na véspera da entrega só de boleto que realmente falta. Falso alarme é o pior erro, porque o aviso é externo.
- O time decide os casos duvidosos numa tela, sem planilha.
- Romaneio e vigia usam a mesma régua.

## Decisões do Hugo (06/10)

| Tema | Decisão |
|---|---|
| Objetivo do aviso | Antes da entrega E resumo periódico (C) |
| Canais | E-mail + WhatsApp + chamado no atendimento |
| Como marcar | Sistema sugere pela NF, time confirma no painel (C) |
| Limiar da sugestão | Basta 1 DANFE com duplicata |
| Quando avisar antes da entrega | Fim da tarde (~18h30), pedidos roteirizados para amanhã (A) |
| Resumo | Seção nova no resumo diário das 20h que já existe (A) |
| Atendimento | Chamado, um aberto por embarcador, área nova "Boleto" (A) |
| Frescor dos dados | Atualizar os documentos dos candidatos antes de avisar (B) |
| Chamado no piloto | Abre de verdade já no piloto (o embarcador vê no portal) |

## Partes

### 1. Marcação (`clientes_boleto`)

**Tabela `clientes_boleto`** (dados.db), chave `(sender_id, documento)`. O documento é o CNPJ/CPF do destinatário só com dígitos. Colunas:

- `nome`
- `situacao`: PENDENTE, TEM_BOLETO ou NAO_TEM_BOLETO
- `nfs_com_duplicata` e `nfs_sem_duplicata` (contagem dos últimos 90 dias)
- `boletos_recebidos`
- `ultimo_pedido`
- `sugerido_em`, `decidido_em`, `decidido_por`

**Sugestão.** Módulo novo `regras/clientes_boleto.py` (regras + banco) mais o script `marcar_clientes_boleto.py`.

- Para cada NF ENVIADA dos últimos 90 dias, liga a NF ao par:
  - `codigo_pedido` leva a `nucleo_pedidos.sender_id` e a `destinatario_codigo`;
  - `cobranca` (1 / 0 / -1) vira a evidência.
- O par vira **PENDENTE** quando tem ≥1 DANFE com `cobranca=1` (ou ≥1 boleto ENVIADO) e ainda não existe na tabela.
- Par já decidido só tem a evidência atualizada; a situação não muda.
- Nunca sugere NAO_TEM_BOLETO.
- Roda na sequência das 18h, antes do aviso. A primeira carga cobre os 90 dias.

**Tela `/painel/clientes-boleto`.** Mesmo modelo de `/painel/clientes-agenda`:
- pendentes no topo, com a evidência e os botões "Tem boleto" e "Não tem";
- os decididos embaixo, com busca e opção de trocar a decisão;
- endpoint de decisão com `@exige_mesma_origem`;
- nível de acesso igual ao da AGENDA;
- item no menu lateral.

Pendentes novos geram um aviso no WhatsApp interno com o link, no padrão de `avisar_clientes_agenda`.

### 2. Regra do boleto esperado

`regras_documentos.boleto_esperado` ganha o parâmetro `situacao_cliente`, que vale None, `TEM_BOLETO` ou `NAO_TEM_BOLETO`:

- `NAO_TEM_BOLETO`: False, sempre.
- `TEM_BOLETO`: True, sempre.
- `None` (sem decisão ou PENDENTE): a regra atual, ou seja, duplicata na NF ou a lista `SENDERS_BOLETO_SEM_FATURA`.

`gerar_pdf_romaneios.avaliar_documentos_pedido` busca a situação do par. Para isso precisa do CNPJ do destinatário: recebe `documento_destinatario` opcional, e os chamadores passam o que têm (`nucleo_pedidos.destinatario_codigo`, ou o code do customer do serviço Vuupt).

Capa do romaneio e vigia das 06:43 continuam usando essa função; nenhuma régua paralela.

### 3. Aviso das 18h30 (`aviso_boleto_faltando.py`)

**Quando:** último passo de `infra/sequencia_incremento.sh` (18h), depois de `marcar_clientes_boleto.py`. Sai entre 18h20 e 18h40, com as rotas de amanhã já montadas.

**Passos:**
1. **Candidatos.** Pedidos de amanhã, com rota no espelho do núcleo para a data OU em rascunho da data, de pares **TEM_BOLETO**, sem boleto ENVIADO. Pedidos já avisados para essa data ficam fora.
2. **Atualizar.** `processar_documentos.main(pedidos_stokki=candidatos, com_email_embarcadores=True)`, um modo escopado que roda também a etapa de e-mail dos embarcadores. Usa a fila da Stokki.
   - Falha ou fila esgotada: segue com o que tem, e o resumo de execução marca a etapa como erro.
3. **Reavaliar.** Fica só quem continua sem boleto.
4. **Avisar**, agrupando por embarcador (sender_id leva a `interno.cnpj_embarcador`):
   - **E-mail:**
     - tabela com pedido, cliente, NF e data de entrega, mais a instrução "responda este e-mail com o boleto ou anexe no pedido da Stokki";
     - mailto e resposta para entregas@ (padrão do projeto);
     - destinatários: e-mails de `preferencias_notificacao` ou `interno.email`;
     - `aviso_boleto_faltando.forcar_destino` começa em hugo@.
   - **WhatsApp:**
     - texto de até 200 caracteres, por exemplo "Fresh Log: 3 pedidos de amanhã sem boleto (PS-1, PS-2 +1). Detalhes no e-mail e no portal.";
     - número vem de `preferencias_notificacao.whatsapp` com a opção nova `boleto_faltando` na lista `TIPOS`;
     - usa o caminho de clientes (`clientes_ligado`, `clientes.forcar_destino`, teto de clientes, `numero_existe`).
   - **Chamado:**
     - procura chamado do embarcador com `area='boleto'` e status diferente de RESOLVIDO; se achar, `mensagem_sistema` com os pedidos novos;
     - se não achar, `criar_chamado(..., ORIGEM_SISTEMA, STATUS_AGUARDANDO_FL, assunto="Boleto faltando", area="boleto")` mais `mensagem_sistema`;
     - padrão de `bloqueio_area.abrir_bloqueio`;
     - abre de verdade já no piloto; os e-mails do chamado seguem `portal_cliente.chamados.forcar_destino`.
5. **Registrar** em `notificacoes_enviadas`, chave `boleto_faltando|<PS>|<data_entrega>`, só se ao menos um canal funcionou.

`AREAS` em `portal_cliente/chamados.py` ganha a área `boleto` ("Boleto"). Não é um `tipo` novo: os filtros do portal usam `tipo='CLIENTE'`.

**CLI:**
- `--modo-teste`: não grava nada, não abre chamado e manda e-mail e WhatsApp só para `forcar_destino`.
- `--data`: padrão é amanhã.
- `--sem-atualizar`: pula o passo 2.

### 4. Resumo das 20h

`resumo_diario_embarcador.py` ganha o bloco **"Entregues hoje sem boleto"**: pedidos entregues hoje de pares TEM_BOLETO sem boleto ENVIADO. O CNPJ do cliente vem de `nucleo_pedidos` pelo código, porque a linha do resumo não o carrega.

Fica atrás de `resumo_diario_embarcador.secao_boleto` (padrão False), porque o resumo já tem envio real ligado.

Quando a seção tem pedidos, e só com `secao_boleto` ligada:
- uma linha de WhatsApp com a contagem, pelo mesmo caminho e com as mesmas travas da parte 3;
- uma mensagem no chamado de boleto do embarcador, que é aberto se não existir.

### 5. Configuração

```yaml
aviso_boleto_faltando:
  ativo: true
  forcar_destino: hugo@freshlogbr.com   # "" = envio real ao embarcador
resumo_diario_embarcador:
  secao_boleto: false
```

O WhatsApp usa `whatsapp_notificacoes.clientes.*` (já existe; hoje `forcar_destino` = Hugo). Ligar o envio real é decisão do Hugo.

## Falhas

- Cada canal é independente: se um falha, os outros saem.
- Pedido sem nenhum canal bem-sucedido não é marcado como avisado e entra de novo na rodada seguinte da mesma data, se houver.
- Stokki ocupada além da espera: avisa com os dados atuais e registra isso no resumo de execução.
- Notificação de execução (`notificar_execucao`): e-mail sempre; WhatsApp interno só com erro.

## Testes

- `regras/test_clientes_boleto.py`:
  - sugestão: 1 DANFE com duplicata vira PENDENTE;
  - par decidido não volta a PENDENTE;
  - evidência atualizada;
  - CPF e CNPJ normalizados.
- `documentos_pedido/test_regras_documentos.py`: precedência da situação do cliente.
- `test_aviso_boleto_faltando.py`:
  - seleção: rota e rascunho, só TEM_BOLETO, sem boleto, não repetidos;
  - agrupamento por embarcador;
  - WhatsApp com 200 caracteres ou menos;
  - chamado reaproveitado ou aberto;
  - registro só com canal ok;
  - `--modo-teste` não grava nada.
- Resumo: bloco aparece só com `secao_boleto`, e só com pares TEM_BOLETO.
- Painel: tela e decisão testadas em porta separada (8099), com o padrão de `test_clientes_agenda_tela.py`.
- VPS: tudo em `--modo-teste` antes de ligar; primeira carga de sugestões medida, para saber quantos pendentes aparecem.

## Fora do escopo

- Desmarcar automaticamente pela resposta do embarcador no chamado: o time decide na tela.
- Sugerir NAO_TEM_BOLETO automaticamente.
- O embarcador marcar os clientes pelo portal.
- Muai: sem sender_id na `interno`. Entra quando tiver.
