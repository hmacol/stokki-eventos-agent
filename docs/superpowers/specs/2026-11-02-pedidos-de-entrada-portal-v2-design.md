# Portal: Pedidos de Entrada v2 (o lançamento nasce no portal e cria o #PE na Stokki)

Data: 02/11/2026. Pedido do Hugo.
Base: `docs/superpowers/specs/2026-09-24-pedidos-de-entrada-portal-design.md` (v1)
e o ramo `pedidos-entrada` (origin `afab17b`, implementado e revisado em
26/09, nunca mergeado nem deployado). **Este documento só registra o que
muda ou se confirma em relação à v1.** O que ele não menciona vale como está
na v1.

## 1. Por quê

Hoje o recebimento nasce na Stokki: alguém da Fresh Log digita o `#PE` a
partir do e-mail do cliente, e `sincronizar_recebimentos_wms.py` copia de lá
pro `/wms` a cada 30 min. O Hugo quer inverter: o lançamento nasce no nosso
portal, "igual ao lançamento de pedidos" (aba Envio de pedidos), e é isso que
dispara a criação na Stokki. A Stokki deixa de ser origem e vira destino.

Sucesso = uma nota real da Maria Dolores lançada no portal vira `#PE` na
Stokki sem ninguém digitar, aparece no Receber do `/wms`, o galpão recebe por
caixa, encerra pelo botão, e o lançamento no portal mostra o resultado
(encerrado ou com divergência).

## 2. Decisões do Hugo (02/11/2026)

| # | Decisão |
|---|---|
| V1 | **Quem lança:** o cliente (CNPJ + PIN) **e** a equipe Fresh Log pelo `/equipe` do portal, atuando pelo cliente. Uma tela só para os dois. |
| V2 | **De que se lança:** XML da nota de remessa (vários ou .zip) **e** planilha. Mantém o desenho da v1 (7.1 e 7.2). |
| V3 | **Tela:** o esboço aprovado em 02/11 (três blocos: Anunciar, Conferência antes de enviar, Minhas entradas), mesma estrutura da aba Envio de pedidos. |
| V4 | **Produto não cadastrado trava o lançamento.** O botão "Confirmar e enviar pra Stokki" fica desabilitado com o aviso "N produto(s) não existe(m) no cadastro. Peça o cadastro à Fresh Log". Muda a v1, em que catálogo não sincronizado era só aviso: agora sem catálogo também trava (não há como conferir). |
| V5 | **Lote e validade não vêm do cliente.** Quem registra é o galpão, na caixa, no recebimento (como já é no `/wms`). A conferência não mostra nem pede lote/validade. Confirma a v1 (seção 13). |
| V6 | **Confirmadas da v1:** data prevista obrigatória (D4); cancelar só enquanto o galpão não começou a receber (D5); só Maria Dolores, ligada por cliente com `entradas_ativo` (D3); módulo próprio (D6); nota de remessa emitida pelo cliente (D1); Stokki cria o `#PE` pelo wizard (D2). |
| V7 | **Fase 2 (fora deste projeto):** (a) cadastro de produtos feito no nosso portal; (b) a planilha de escolha de lotes da NUU (Maria Dolores) recebida pelo portal. Ver seção 7. |

## 3. O que muda na tela em relação à v1

Referência visual: esboço de 02/11 (`.superpowers/brainstorm/7715-1793656559/content/tela-entradas.html`
no repo principal, não versionado). Diferenças, sem lote/validade (V5):

- **Faixa da equipe.** Quem entrou pelo `/equipe` vê no topo da aba
  "Lançando como [cliente ▾]" usando o seletor de empresa que a aba Envio já
  tem (`empresas_envio`). O lançamento grava `enviado_por` com o login
  nominal (mesmo carimbo de `portal_envios.enviado_por`). Na lista, o cliente
  vê "lançado pela equipe Fresh Log" nas entradas que a equipe lançou. A
  trava de nível é a de Envio (`_exige_pode_enviar`: equipe só total/operador).
- **Conferência produto por produto** antes de confirmar: SKU, produto,
  quantidade em unidades, **caixas** (quantidade ÷ `wms_produtos.qtd_por_caixa`;
  sem `qtd_por_caixa` mostra "—") e coluna **Cadastro** (OK / Não cadastrado).
  Na v1 a prévia era uma linha por nota; agora cada nota abre os itens.
- **Situação na lista**, em português de operação (substitui os chips de
  status da v1, mesmos estados por baixo):

  | Rótulo na tela | Estado por baixo |
  |---|---|
  | Enviando pra Stokki | `ANUNCIADO` + `stokki_status` `NA_FILA`/`ENVIANDO` |
  | Criado na Stokki · #PE-xxxx | `ANUNCIADO` + `CRIADO` |
  | Stokki recusou (ver motivo) | `stokki_status = ERRO`, com `stokki_erro` |
  | Recebendo · X de Y itens | `CHEGOU` (X = linhas do recebimento com `qtd_enderecada > 0`) |
  | Encerrado | `ENDERECADO` |
  | Com divergência · falta N un | `DIVERGENCIA` |
  | Cancelado | `CANCELADO` |

  "Atrasada" continua como na v1 (chip vermelho quando `ANUNCIADO` e data
  prevista passou).
- **Cancelar** aparece só em `ANUNCIADO` (nenhuma caixa recebida), igual v1.
- **Ver divergência** mostra a mesma tabela do botão "Encerrar recebimento"
  do `/wms` (anunciado × recebido por linha, FALTA e SOBRA), lida de
  `wms_pedidos.resumo_encerramento`. O e-mail de faltas ao cliente continua
  igual.

## 4. O que mudou no sistema desde 26/09 e afeta a v1

- **Encerramento só pelo botão (02/11, ramo `galpao-caixas`, commit
  `62ab032`, ainda não pushado).** O recebimento não fecha mais sozinho
  quando tudo foi endereçado: fica `ESPERADO` até o galpão apertar
  "Encerrar recebimento". Sem falta vira `ENDERECADO`, com falta vira
  `DIVERGENCIA` (+ e-mail). Sobra pede confirmação na hora do bipe.
  Consequências:
  - o gatilho de `sincronizar_status` da v1 na rota
    `/api/wms/recebimentos/<id>/encerrar-divergencia` passa a ser a rota
    `/encerrar` (a antiga continua como apelido, as duas chamam
    `encerrar_recebimento`). O gatilho tem que disparar nos dois desfechos,
    não só na divergência;
  - `CHEGOU` ("Recebendo") pode durar mais, até o botão.
- **Recebimento por caixa com serial (F1, `wms.caixas_ativo`, deployado
  desligado).** Não muda nada aqui: o `#PE` criado pelo portal entra no
  Receber como qualquer outro, e o galpão recebe por caixa ou pelo fluxo
  antigo conforme a chave.
- **Master andou 593 commits** desde `afab17b`. O ramo é atualizado por
  **merge** de `origin/master` (o ramo já foi publicado; sem rebase). Arquivos
  com conflito provável: `portal_cliente/app.py`,
  `portal_cliente/templates/acompanhamento.html`,
  `portal_cliente/envio_pedidos.py`, `painel_agentes/wms_pedidos.py`,
  `painel_agentes/painel_agentes.py`, `sincronizar_recebimentos_wms.py`,
  `MAPA_DO_SISTEMA.txt`. O `galpao-caixas` (com `62ab032` e `062341c`) entra
  no master **antes** desta atualização, pra o merge já pegar o encerramento
  pelo botão.

## 5. O que continua igual (dito explicitamente pra não haver dúvida)

- `#PE` criado direto na Stokki por alguém (sem passar pelo portal) continua
  chegando no `/wms` pela sincronização de 30 min, como hoje. O portal não é
  o único caminho; é o caminho novo. Se a equipe digitar na Stokki uma nota
  que o cliente já lançou no portal, a amarração pela chave (v1 8.3) junta os
  dois.
- Estoque só entra quando o galpão recebe/endereça. Nada no portal escreve
  em `wms_saldos` nem `wms_movimentos`.
- Worker só na VPS, dentro de `sessao_uso` (sessão Stokki compartilhada com
  o `agente_importacao_stokki`; login concorrente derruba a outra com 401).
- Cancelar na Stokki continua por chamado à equipe (v1 seção 6), nunca
  automático.

## 6. Prova (substitui a v1 seção 12, passos 3 a 5)

Na ordem, cada passo provado antes do próximo:

1. Ramo atualizado com o master, testes do ramo + `painel_agentes/test_wms_*`
   verdes.
2. Tela no ar com o worker parado: o Hugo lança pelo `/equipe` uma nota real
   da Maria Dolores combinada com ele; aparece "Enviando pra Stokki" e a
   conferência travou certo um SKU fora do cadastro (testar com uma planilha
   de teste com SKU inexistente, sem confirmar).
3. Worker `--uma-vez` real com **essa uma** nota: vira `#PE` na Stokki, a
   lista mostra "Criado na Stokki · #PE-xxxx".
4. Próxima rodada do timer: o `#PE` aparece no Receber do `/wms` com a data
   prevista; o galpão recebe e encerra pelo botão; o portal mostra
   "Recebendo" e depois "Encerrado" ou "Com divergência" com a tabela.
5. Planilha: o mesmo ciclo com uma planilha real (a v1 deixou a planilha
   sem prova na Stokki; se o wizard Excel exigir `origin_id` sem cadastro,
   a planilha fica "Stokki recusou" com o motivo e isso volta pro Hugo).

## 7. Fase 2 (registrado, não construído)

- **Cadastro de produtos no portal.** Hoje produto novo é cadastrado na
  Stokki e o catálogo chega por sincronização; a trava V4 manda o cliente
  pedir à Fresh Log. Na fase 2 o cadastro nasce no portal (e vai pra Stokki,
  no mesmo sentido deste projeto). A conferência ganha "Cadastrar" no
  produto travado.
- **Planilha de escolha de lotes da NUU.** Hoje a NUU (Maria Dolores) manda
  por e-mail, junto com os outros documentos, uma planilha que diz quais
  lotes a operação deve usar nas **saídas**. Na fase 2 ela é enviada pelo
  portal, no mesmo fluxo de lançamento. Antes de desenhar: ver como ela é
  lida hoje (`documentos_pedido/`, `executar_tudo.py` passo 1a, que hoje lê
  a `ENTREGAS.xlsx` da NUU pra agenda/endereço; confirmar com o Hugo se a de
  lotes é outra planilha) e quem aplica o lote na separação.

## 8. Escolhas minhas (Claude) que o Hugo pode revisar

- **O que é "cadastrado" (V4):** a mesma consulta de `validar_skus` de
  Envio (catálogo do embarcador sincronizado da Stokki). "Sem catálogo" =
  nenhum produto do embarcador no catálogo; não há limite de idade.
- **`qtd_por_caixa` vazio** só mostra "—" na coluna Caixas e não trava.
- **"Lançado pela equipe"** vem de uma coluna nova
  `portal_entradas.lancado_por_equipe` (1 quando a sessão é `/equipe`),
  não da leitura do `enviado_por`. A equipe pode lançar por qualquer cliente
  com `entradas_ativo` (hoje só a Maria Dolores).
- **Recebendo com caixas ligadas:** o plano confere no código do
  `galpao-caixas` que ativar caixa soma `qtd_enderecada` na linha (é o que
  a checagem de sobra usa). Se não somar, é bug deste projeto e se corrige
  aqui, pra o portal não ficar em "Criado na Stokki" com o galpão já
  recebendo.
- **Esboço x texto:** onde o esboço de 02/11 divergir deste documento, vale
  o texto (o esboço ainda mostra lote/validade, que saíram por V5).
- **Teste que quebrar depois do merge** em arquivo que este projeto toca se
  corrige aqui. Em arquivo alheio, se reporta ao Hugo e não se mexe.

## 9. Fora de escopo (além da v1 seção 13)

- Fase 2 inteira (seção 7).
- Outros embarcadores além da Maria Dolores.
- Notificar o cliente por e-mail/WhatsApp quando o `#PE` for criado ou a
  mercadoria chegar.
- Painel interno com tela própria de lançamento: a equipe lança pelo
  `/equipe` do portal (V1).
