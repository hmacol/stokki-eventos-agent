# Correção de canhoto pelo painel — design

Data: 05/10/2026. Decisões do Hugo nesta conversa: cliente vê a correção (B),
atendimento também corrige sem aprovação (B), a correção não muda o
resultado da entrega (A).

## Objetivo

Dar ao escritório autonomia para completar ou substituir o checklist de um
pedido já entregue quando a foto ficou ruim, o motorista esqueceu ou
houve qualquer ocorrência — como se o escritório preenchesse o checklist
do motorista. O cliente passa a ver a versão corrigida no portal e no link
do e-mail.

Sucesso = um pedido entregue sem canhoto (ou com canhoto ruim) é achado
pela fila, corrigido em menos de 1 minuto, e o cliente abre o comprovante
novo pelo portal ou pelo link do e-mail, sem ninguém mexer na Vuupt.

## Fora do escopo

- Mudar o resultado da entrega (Entregue ↔ Parcial ↔ Não entregue).
- Reanexar canhoto na Stokki (pedido finalizado não aceita anexo).
- Escrever a correção de volta na Vuupt.
- Avisar o cliente de que o canhoto foi corrigido.

## 1. Telas (painel_agentes)

### Ficha do pedido — `/consulta/pedido/<codigo>`

Na parada entregue/parcial/não entregue, dois botões (níveis `total`,
`operador`, `atendimento`; `leitura` não vê os botões):

- **Corrigir comprovante** — abre formulário montado a partir de
  `checklist_modelo` (`fluxo` = resultado já marcado na parada:
  `ENTREGUE`, `PARCIAL` ou `NAO_ENTREGUE`). Campos `FOTO` aceitam imagem
  (jpg/png/webp) ou PDF, até 10 MB por arquivo. Campos
  obrigatórios seguem `obrigatorio` do modelo. Pré-preenche os textos com
  o que o motorista mandou, quando houver (app próprio).
- **Marcar canhoto ruim** — pede um motivo curto (texto livre, obrigatório)
  e põe o pedido na fila.

Depois de enviar, a ficha mostra a correção (fotos + campos + quem/quando)
acima do comprovante original, que continua visível.

### Fila "Canhotos a resolver" — aba nova em `/canhotos`

Janela: últimos 30 dias pela data da rota. Entram:

1. Paradas com resultado ENTREGUE ou PARCIAL **sem canhoto**:
   - origem Vuupt: `nucleo_pedidos.qtd_checklists = 0` (ou nulo após sync);
   - app próprio: nenhum `nucleo_comprovantes` com `tipo = 'CANHOTO'`.
2. Pedidos marcados como canhoto ruim e ainda não resolvidos.
3. Comprovantes `REPROVADO` (comportamento atual, mantido na aba própria).

Sai da fila quando existe correção do escritório para a parada (e, se
estava marcada como ruim, a marcação recebe `resolvido_em`). Cada linha
mostra código, destinatário, embarcador, data, motorista, motivo de estar
na fila e o botão que leva à ficha já com o formulário aberto.

Rota da aba: `/canhotos?resultado=A_RESOLVER`, mesmos níveis de leitura da
tela atual; ações exigem `total`/`operador`/`atendimento`.

## 2. Dados (nucleo/banco.py)

### `nucleo_comprovantes` — colunas novas (via `_migrar_colunas`)

| coluna | tipo | uso |
|---|---|---|
| `origem` | TEXT | `APP` (nulo = APP, linhas antigas) ou `PAINEL` |
| `enviado_por` | TEXT | usuário do painel (`session["usuario"]` ou nível) |
| `correcao_id` | TEXT | agrupa os arquivos de um mesmo envio do formulário |

Cada foto do formulário é uma linha (`tipo` mapeado da chave do campo:
`foto_canhoto` → `CANHOTO`, `foto_nf_devolucao` → `NF_DEVOLUCAO`,
`foto_produto_devolvido` → `PRODUTO`, `foto_ocorrencia` → `OCORRENCIA`).
Campos `DOCUMENTO` também são arquivo (`documento` → `DOCUMENTO`). Os
campos de texto do formulário vão no evento `COMPROVANTE_CORRIGIDO`
(`dados_json.checklist`), não nas linhas de arquivo — assim uma correção
só de texto (ex.: não entregue sem foto) também existe. Arquivos em
`dados/comprovantes/<codigo>/painel_<correcao_id>_<chave>.<ext>`.
`resultado_validacao = 'APROVADO'`, `validado_por = 'HUMANO'` (quem enviou
já conferiu).

Nada é apagado: o comprovante do app e o PDF da Vuupt continuam
guardados. Vale para o cliente o envio `PAINEL` mais recente da parada.

### Evento

Cada envio grava `nucleo_eventos` com `tipo = 'COMPROVANTE_CORRIGIDO'`,
`origem = 'PAINEL'`, `dados_json = {"correcao_id", "enviado_por", "fluxo", "checklist", "arquivos"}`
— aparece na linha do tempo da ficha.

### Tabela nova `canhoto_marcacoes`

```
id, parada_id (FK nucleo_paradas), motivo TEXT NOT NULL,
marcado_por TEXT, marcado_em TEXT, resolvido_em TEXT, resolvido_por TEXT
```

### Funções de domínio (`nucleo/correcao_comprovante.py`, módulo novo — `operacao.py` já é grande)

- `registrar_correcao(conn, parada_id, campos, arquivos, usuario, pasta)`
  — valida contra `checklist_modelo` do fluxo da parada (recusa fluxo
  diferente do resultado marcado), grava arquivos e linhas, evento, e
  resolve marcações abertas. Uma transação curta (arquivos gravados antes,
  removidos se o INSERT falhar).
- `marcar_canhoto_ruim(conn, parada_id, motivo, usuario)`.
- `listar_canhotos_a_resolver(conn, dias=30)`.
- `correcao_vigente(conn, service_id) -> dict | None` — último envio
  `PAINEL` das paradas com aquele `service_id` (campos + arquivos).

A ligação é sempre por parada / `service_id`, nunca só pelo código
(reentrega com o mesmo código sobrescreve linha no núcleo).

## 3. Portal do cliente

`/api/canhoto/<service_id>` (portal_cliente/app.py) e `/c/<token>`
(link_canhoto.py) passam a:

1. Manter a checagem de dono atual (`sender_id` do serviço na Vuupt).
2. Chamar `correcao_vigente(service_id)`. Se houver:
   - um único arquivo PDF e nenhum campo de texto → devolve o PDF;
   - senão → monta PDF com Pillow (mesma técnica de `cotacao_pdf.py`):
     cabeçalho (código, destinatário, data da entrega), campos do
     checklist com os rótulos de `checklist_modelo`, e as imagens, uma por
     página. PDFs enviados junto entram como páginas seguintes.
   - Cache em `dados/canhotos/canhoto_<codigo>_painel_<correcao_id>.pdf`.
3. Sem correção → fluxo atual (PDF do checklist da Vuupt).

A busca de correção fica num helper único usado pelas duas rotas, para
não divergirem. O texto "O motorista ainda não enviou a foto" só aparece
quando não há nem correção nem checklist da Vuupt.

## 4. Erros e limites

- Arquivo acima de 10 MB ou tipo fora de imagem/PDF → 400 com mensagem.
- Parada sem resultado (ainda pendente) → botões não aparecem; API
  recusa com 409.
- Falha ao montar o PDF no portal → cai para o PDF da Vuupt se existir,
  senão a mensagem de indisponível atual (502).
- Endpoints novos com `@bloqueia_planejamento_passado` não se aplicam
  (é correção de passado por definição); registrar `niveis=` no menu.

## 5. Testes (unittest)

- `nucleo/test_correcao_comprovante.py`: grava sem apagar o original;
  recusa fluxo diferente do resultado; campos obrigatórios; resolve
  marcação; evento gravado; `correcao_vigente` pega o mais recente; ligação
  por `service_id` com duas paradas de mesmo código.
- `portal_cliente/test_canhoto_corrigido.py`: portal e link preferem a
  correção; sem correção usam a Vuupt (mock); cliente de outro `sender_id`
  recebe 404; PDF gerado abre (Pillow) e tem N páginas.
- `painel_agentes/test_canhotos_a_resolver.py`: fila mostra sem-canhoto e
  marcados, some depois da correção; `leitura` não corrige (403);
  `atendimento` corrige.

## 6. Arquivos tocados

- `nucleo/banco.py` (colunas + tabela), `nucleo/correcao_comprovante.py`
  (funções, novo).
- `painel_agentes/painel_agentes.py` (rotas), `templates/consulta_pedido.html`,
  `templates/canhotos.html`, `_menu_lateral_nav.html` se a aba virar item.
- `portal_cliente/app.py`, `portal_cliente/link_canhoto.py`, helper novo
  `portal_cliente/canhoto_corrigido.py` (busca + PDF).
- `MAPA_DO_SISTEMA.txt` (tela, tabela, helper).

Deploy: reiniciar `painel-agentes` e `portal-cliente`.
