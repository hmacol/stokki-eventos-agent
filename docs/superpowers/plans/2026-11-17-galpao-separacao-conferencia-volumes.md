# Galpão: separação em onda, conferência e volumes (teste de 23/11) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Na segunda 23/11 o galpão endereça, separa em onda por embarcador, confere às cegas e etiqueta volumes da MOMBAK no `/wms`; na terça/quarta o mesmo fluxo roda com a Maria Dolores.

**Architecture:** Camada nova sobre o que já existe (caixas com serial, reservas FEFO, fila de impressão). As regras ficam num módulo de dados puro, `painel_agentes/wms_separacao.py` (sem Flask), e as telas são abas novas da PWA `wms.html` mais uma tela de escritório `/ondas`. Nada escreve na Stokki: a equipe termina o pedido lá à mão, como hoje, e a baixa continua vindo da expedição.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db`), `unittest`, PIL + qrcode (etiquetas), Playwright (ensaio de tela).

**Spec:** `docs/superpowers/specs/2026-11-01-galpao-separacao-conferencia-etiquetagem-design.md` (fases F2 a F4) + `docs/superpowers/specs/2026-11-01-galpao-f0-stokki-notas.md`. Este plano cobre F2, F3 e F4 **sem o robô** (R1/R2).

## Decisões do Hugo em 17/11 (valem sobre a spec)

- Teste de segunda 23/11 com a **MOMBAK** (`#stkkc-22`, 4 produtos, todos com EAN). Maria Dolores (`#stkkc-48`) entra terça ou quarta.
- A MOMBAK fica etiquetada e endereçada **antes** de segunda. A etiquetagem da Maria Dolores (marca Nuu) acontece de segunda a quarta.
- **Robô da Stokki fora.** A equipe repete o pedido na Stokki à mão.
- Onda é montada **por embarcador** (uma onda nunca mistura embarcadores).

## Recorte proposto pelo Claude (o Hugo pode trocar)

- Sem etiqueta avulsa de fracionamento (C8 da spec). Pedido que pede menos que a caixa: o separador bipa a caixa e digita quanto tirou; a caixa fica no lugar com o resto. Na conferência o conferente bipa o EAN do produto e digita a quantidade solta.
- Sem filtro por data de entrega na onda (H11): `wms_pedidos` não guarda a data. Filtro só por embarcador; o escritório marca os pedidos.
- Acompanhamento = uma lista por pedido (etapa, quem, quando) na tela `/ondas`. Sem comparação com a Stokki.

## Global Constraints

- Nada escreve na Stokki. Nenhum login na Stokki pela máquina local.
- `dados.db` é compartilhado: transação curta, **commit por bipe**. Nunca lote grande num commit.
- Todo bipe carrega `uuid` do aparelho e é idempotente: repetir a chamada devolve o mesmo resultado, nunca conta duas vezes.
- Separar **não** gera movimento (C6): a caixa continua no saldo da posição até a baixa da expedição.
- Telas do escritório ficam **fora** de `/wms` (a regex da tela `wms` em `acesso_telas.TELAS` daria acesso ao login do galpão). Níveis `total` e `operador`.
- Rota nova entra em `acesso_telas.TELAS` (o `test_acesso_telas` cobra) e, se tiver página, no menu (área Galpão de `_menu_lateral_nav.html`).
- Testes: `py -3.11 -m unittest <modulo>` de dentro de `painel_agentes/`, banco temporário, no padrão de `test_wms_caixas.py`. Painel local só na porta 8099.
- Código, comentários e commits em português; logs sem acento. Commits `Galpao: ...`.
- `config.yaml` da VPS só o Hugo edita. O plano diz o que ele precisa gravar, sem valores secretos.
- Migração só aditiva (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN`).

## Review Focus

1. **Bipe repetido** (leitor HID manda duas vezes, ou a rede cai e o app reenvia): conta uma vez só. Testes em T4, T5 e T6.
2. **Caixa errada**: de outro embarcador, de produto que a onda não pede, já separada em outra onda, ou em situação `IMPRESSA`/`EXPEDIDA`/`CANCELADA`: recusa com mensagem que diz o motivo. T4.
3. **Conferente que também separou ou distribuiu** caixa do pedido: recusa ao iniciar a conferência. T6.
4. **Pedido mudou na Stokki depois da onda montada** (item removido, quantidade alterada, pedido cancelado pelo sync de 15 min): a separação já feita não some; o pedido sai da onda se cancelado e as caixas ficam `DEVOLVIDA`; fechar conferência compara com o pedido **atual**. T6 e T8.
5. **Dois celulares na mesma onda**: dois separadores bipando a mesma caixa ao mesmo tempo, só um vence (`BEGIN IMMEDIATE`, como em `_mudar_caixa`). T4.

## Mapa de arquivos

| Arquivo | Papel |
|---|---|
| `painel_agentes/wms_pilotos.py` (novo) | Lê `wms.pilotos` do config: quem é lido da Stokki e quem opera por caixa |
| `painel_agentes/wms_separacao.py` (novo) | Ondas, separar, distribuir, conferir, falta, volumes, andamento |
| `painel_agentes/wms_etiqueta_volume.py` (novo) | Arte e PDF da etiqueta de volume 10×15 |
| `painel_agentes/wms_caixas.py` | Situações novas da caixa; baixa consome primeiro as caixas do pedido |
| `painel_agentes/wms_fila_impressao.py` | Tipo `VOLUME` |
| `stokki/pedidos.py` | Leitor dos itens em Open (`table#example1`) |
| `sincronizar_pedidos_wms.py`, `sincronizar_recebimentos_wms.py`, `checar_caixas_wms.py`, `painel_agentes/wms_faltas_recebimento.py` | Rodam para cada piloto |
| `painel_agentes/painel_agentes.py` | Rotas `/api/wms/separacao/*`, `/ondas`, `/api/ondas/*` |
| `painel_agentes/templates/wms.html` | Abas Separar (nova), Distribuir, Conferir, Volumes |
| `painel_agentes/templates/ondas.html` (novo) | Escritório: montar onda, faltas, andamento |
| `painel_agentes/acesso_telas.py`, `templates/_menu_lateral_nav.html`, `MAPA_DO_SISTEMA.txt` | Acesso, menu e mapa |

## Calendário

| Dia | Entrega |
|---|---|
| Qua 18/11 | T1 (deploy no mesmo dia: libera a etiquetagem da MOMBAK) e T2 |
| Qui 19/11 | T3, T4, T5 |
| Sex 20/11 | T6, T7, T8 |
| Sáb 21/11 | T9, T10 |
| Dom 22/11 | T11: ensaio ponta a ponta, bancada com o Hugo, deploy |
| Seg 23/11 | Teste com a MOMBAK, acompanhado |

---

### Task 1: Vários pilotos, caixa por embarcador

Hoje o código assume um embarcador (`wms.embarcador_piloto_id`, padrão 48) e uma chave global (`wms.caixas_ativo`). Ligar a chave global travaria a Maria Dolores antes da etiquetagem dela.

**Files:**
- Create: `painel_agentes/wms_pilotos.py`, `painel_agentes/test_wms_pilotos.py`
- Modify: `painel_agentes/painel_agentes.py` (`_wms_piloto_id`, `_wms_caixas_ativo` e os usos em ~5606-5624, 5803, 6113, 6126), `painel_agentes/wms_caixas.py:192` (`produto_usa_caixas`), `sincronizar_pedidos_wms.py` (`main`), `sincronizar_recebimentos_wms.py` (`main`), `checar_caixas_wms.py`, `painel_agentes/wms_faltas_recebimento.py:132`
- Test: `painel_agentes/test_wms_pilotos.py`, ajustes em `test_wms_caixas_tela.py`

**Interfaces:**
- Produces:
  - `wms_pilotos.pilotos(config: dict) -> list[dict]`: cada item `{"id": int, "nome": str, "caixas": bool}`. Com `wms.pilotos` no config, usa a lista. Sem ela, devolve um item a partir das chaves antigas (`embarcador_piloto_id` padrão 48, `embarcador_piloto` padrão `MARIA DOLORES`, `caixas` = `caixas_ativo`).
  - `wms_pilotos.ids_com_caixas(config: dict) -> set[int]`
  - `wms_caixas.produto_usa_caixas(produto: dict, ids_com_caixas) -> bool` (aceita `int` ou conjunto; mantém compatibilidade com quem passa um id só)
- Config que o Hugo grava na VPS:

```yaml
wms:
  pilotos:
    - {id: 48, nome: "MARIA DOLORES", caixas: false}
    - {id: 22, nome: "MOMBAK", caixas: true}
```

- [ ] **Step 1:** testes de `pilotos()`: lista do config; fallback das chaves antigas; `id` como texto vira `int`; item sem `id` é ignorado; `ids_com_caixas` só traz `caixas: true`.
- [ ] **Step 2:** rodar e ver falhar.
- [ ] **Step 3:** implementar `wms_pilotos.py`.
- [ ] **Step 4:** trocar os seis pontos de uso. Os syncs e a checagem das 06h40 passam a rodar um laço por piloto (a checagem só nos que têm `caixas`); um piloto que falha não impede o seguinte e a saída do processo é != 0 se algum falhou. `/api/wms/bootstrap` devolve `caixas_ativo: true` se algum piloto usa caixas e a lista `caixas_embarcadores`; a trava do movimento avulso e o "criar caixa" valem por produto (embarcador do produto).
- [ ] **Step 5:** `test_wms_caixas`, `test_wms_caixas_tela`, `test_wms_pedidos`, `test_wms_telas` verdes.
- [ ] **Step 6:** commit. Deploy (com pedido do Hugo): restart `painel-agentes`; `sincronizar_pedidos_wms.py --modo-teste` na VPS tem que listar pedidos dos dois pilotos.

Depois do deploy e do config: conferir `0` reservas ativas e `0` saldo da MOMBAK (hoje o WMS inteiro está zerado), e a equipe já pode etiquetar e endereçar a MOMBAK pela aba Inventário.

### Task 2: Ler pedidos em Open

A separação precisa começar no nosso sistema antes de a Stokki mexer no pedido. O sync só lê `Waiting for Carrier`, `Separating` e `Ready to Pack`, e `extrair_itens_do_pedido` não acha itens em Open (notas da F0, Step 2).

**Files:**
- Modify: `stokki/pedidos.py`, `sincronizar_pedidos_wms.py:72` e o trecho de `rodar` que lê os itens
- Test: `stokki/test_*` do módulo de pedidos (seguir o existente), fixture nova com a `table#example1` copiada das notas da F0

**Interfaces:**
- Produces: `stokki.pedidos.extrair_itens_tabela_pedido(html: str) -> list[dict]`, mesmo formato de `extrair_itens_do_pedido` (chaves que `wms_pedidos.registrar_pedido` consome), com `ean` vazio e a quantidade da coluna "Quantidade".

- [ ] **Step 1:** teste com a fixture: 2 linhas, SKUs `CXPTD1FD` e `MNCXDCHF300`, quantidades 38 e 18.
- [ ] **Step 2:** implementar o leitor.
- [ ] **Step 3:** `STATUS_INTERESSANTES` ganha `"Open"` na frente; em `rodar`, se `extrair_itens_do_pedido` vier vazio, tenta `extrair_itens_tabela_pedido`.
- [ ] **Step 4:** teste do sync com Stokki falsa: pedido Open é registrado e reservado; o mesmo pedido relido em `Separating` não duplica itens.
- [ ] **Step 5:** commit. Prova na VPS: `--modo-teste` mostra os Open dos pilotos.

### Task 3: Modelo de dados da separação

**Files:**
- Create: `painel_agentes/wms_separacao.py`, `painel_agentes/test_wms_separacao.py`
- Modify: `painel_agentes/wms_caixas.py:24` (`SITUACOES_CAIXA` ganha `SEPARADA`, `DISTRIBUIDA`, `CONFERIDA`, `DEVOLVIDA`), `painel_agentes/wms_pedidos.py` (`_COLUNAS_NOVAS`: `execucao TEXT NOT NULL DEFAULT ''`, `onda_id INTEGER`)

**Interfaces:**
- Produces: `wms_separacao.garantir_tabelas(conn) -> None` criando:
  - `wms_ondas(id, embarcador_id, embarcador, situacao, criada_por, criada_em, iniciada_em, concluida_em)`; situação `ABERTA | EM_SEPARACAO | CONCLUIDA | CANCELADA`
  - `wms_onda_pedidos(onda_id, pedido_id, PRIMARY KEY(onda_id, pedido_id))`
  - `wms_separacao_itens(id, uuid UNIQUE, onda_id, pedido_id, serial, produto_id, lote, validade, qtd_un, caixa_inteira, etapa, separado_por_id, separado_por_nome, distribuido_por_id, distribuido_por_nome, criado_em, atualizado_em)`; etapa `SEPARADA | DISTRIBUIDA | CONFERIDA | DEVOLVIDA`
  - `wms_conferencias(id, pedido_id, operador_id, operador_nome, iniciada_em, fechada_em, resultado, detalhe)`; resultado `'' | OK | FALTA`
  - `wms_conferencia_bipes(id, uuid UNIQUE, conferencia_id, serial, produto_id, qtd_un, criado_em)`
  - `wms_decisoes_falta(id, pedido_id, conferencia_id, decisao, motivo, por, em)`; decisão `VOLTAR | PARCIAL`
  - `wms_volumes(pedido_id, numero, total, impressao_id, reimpressoes, criado_em, PRIMARY KEY(pedido_id, numero))`
- `wms_pedidos.execucao`: `'' | NA_ONDA | SEPARANDO | SEPARADO | CONFERINDO | AGUARDANDO_DECISAO | CONFERIDO | EMBALADO`

- [ ] **Step 1:** teste: `garantir_tabelas` duas vezes num banco temporário não falha e as colunas novas existem.
- [ ] **Step 2:** implementar; chamar `garantir_tabelas` onde `wms_caixas.garantir_tabelas` já é chamado no painel.
- [ ] **Step 3:** commit.

### Task 4: Onda e separação

**Files:** `painel_agentes/wms_separacao.py`, `painel_agentes/test_wms_separacao.py`

**Interfaces:**
- Consumes: `wms_caixas.obter_caixa`, `wms_caixas._mudar_caixa`, `wms_pedidos.trocar_lote_reserva`, `wms_pedidos.reservar_pedido`.
- Produces:
  - `pedidos_para_onda(conn, embarcador_id: int) -> list[dict]`: pedidos do embarcador com `execucao = ''`, não baixados nem cancelados, com `estado_reserva` e a falta de cada um.
  - `montar_onda(conn, embarcador_id: int, pedido_ids: list[int], criada_por: str) -> int`: recusa pedido de outro embarcador, pedido já em onda viva, lista vazia.
  - `onda_de_um_pedido(conn, pedido_id: int, operador: dict) -> int` (C9: o separador começa sozinho).
  - `iniciar_onda(conn, onda_id: int, operador: dict) -> None`: `ABERTA -> EM_SEPARACAO`, pedidos `SEPARANDO`.
  - `lista_caminhada(conn, onda_id: int) -> list[dict]`: reservas `ATIVA` dos pedidos da onda somadas por (posição, produto, lote, validade), em ordem de código de posição, cada linha com `pedido_un`, `separado_un`.
  - `separar_caixa(conn, onda_id: int, serial: str, *, qtd_un: float | None, uuid: str, operador: dict) -> dict`: `qtd_un=None` leva a caixa inteira (caixa vira `SEPARADA`); menor que a caixa registra a parte e a caixa fica onde está. Devolve a linha da caminhada atualizada e, em onda de um pedido, o `codigo_ps` (já distribui).
  - `cancelar_onda(conn, onda_id: int, por: str) -> None`: só sem nada separado.

- [ ] **Step 1:** testes do caminho bom: onda com 2 pedidos da MOMBAK, caminhada soma os dois, separar caixa inteira e parte de caixa, onda de um pedido já distribui.
- [ ] **Step 2:** testes do Review Focus 1, 2 e 5: mesmo `uuid` duas vezes devolve o mesmo e conta uma; caixa de outro embarcador, de produto fora da onda, `IMPRESSA`, já `SEPARADA`; quantidade acima do que a onda ainda pede daquele produto; duas conexões separando a mesma caixa (só uma vence).
- [ ] **Step 3:** teste de lote trocado: caixa do mesmo produto com lote diferente do reservado é aceita e a reserva é trocada (D6); se não houver reserva pendente daquele produto, recusa.
- [ ] **Step 4:** implementar. `BEGIN IMMEDIATE` por bipe, commit ao fim.
- [ ] **Step 5:** commit.

### Task 5: Distribuição

**Files:** os mesmos de T4.

**Interfaces:**
- Produces: `distribuir_caixa(conn, onda_id: int, serial: str, *, uuid: str, operador: dict) -> dict` com `{"codigo_ps", "pedido_id", "restam_do_pedido"}`. Escolhe o pedido da onda que ainda precisa daquele produto/lote (menor `codigo_ps` primeiro); item vira `DISTRIBUIDA`, caixa inteira vira `DISTRIBUIDA`. Parte de caixa é distribuída pelo EAN + quantidade (`serial` aceita `EAN:<ean>` com `qtd_un` no corpo; a assinatura ganha `qtd_un: float | None = None`).
  - `fechar_separacao(conn, onda_id: int, operador: dict) -> dict`: pedidos com tudo distribuído viram `SEPARADO`; onda `CONCLUIDA` quando todos fecharam; devolve o que ficou faltando por pedido (não bloqueia: falta aparece na conferência).

- [ ] **Step 1:** testes: 2 pedidos pedindo o mesmo produto, três caixas distribuídas vão 2 e 1; caixa não separada nesta onda é recusada; `uuid` repetido não distribui duas vezes.
- [ ] **Step 2:** implementar. **Step 3:** commit.

### Task 6: Conferência cega e decisão de falta

**Files:** `painel_agentes/wms_separacao.py`, `painel_agentes/test_wms_separacao.py`

**Interfaces:**
- Produces:
  - `pedidos_para_conferir(conn, embarcador_id: int | None = None) -> list[dict]`: `execucao = SEPARADO`, só `codigo_ps`, destinatário e embarcador (sem itens, sem quantidades).
  - `iniciar_conferencia(conn, pedido_id: int, operador: dict) -> int`: recusa operador que separou ou distribuiu item do pedido; pedido vira `CONFERINDO`.
  - `conferir_bipe(conn, conferencia_id: int, codigo: str, *, qtd_un: float | None, uuid: str) -> dict` com `{"caixas_bipadas": int}` e nada sobre o esperado. Aceita serial de caixa (tem que ser item `DISTRIBUIDA` deste pedido) ou EAN + quantidade para parte de caixa.
  - `fechar_conferencia(conn, conferencia_id: int) -> dict` com `{"resultado": "OK" | "FALTA", "faltas": [...]}`: compara, por produto, o bipado com o que o pedido pede **agora** (`wms_pedido_itens` sem `removido_em`). OK: pedido `CONFERIDO`, itens e caixas `CONFERIDA`. FALTA: `AGUARDANDO_DECISAO`.
  - `faltas_pendentes(conn) -> list[dict]`
  - `decidir_falta(conn, pedido_id: int, decisao: str, motivo: str, por: str) -> None`: `VOLTAR` reabre a separação do que faltou (pedido `SEPARANDO`, entra numa onda de um pedido); `PARCIAL` segue como `CONFERIDO` só com o conferido. Motivo obrigatório.

- [ ] **Step 1:** testes do caminho bom (bateu) e da falta (faltou uma caixa, decisão `VOLTAR` e decisão `PARCIAL`).
- [ ] **Step 2:** testes do Review Focus 1, 3 e 4: `uuid` repetido; caixa repetida com `uuid` novo é recusada; caixa de outro pedido; conferente = separador; item removido do pedido depois da separação (sobra aparece no fechamento como "a mais", não trava); pedido cancelado no meio (conferência não fecha, mensagem clara).
- [ ] **Step 3:** implementar. **Step 4:** commit.

### Task 7: Volumes e etiqueta 10×15

**Files:**
- Create: `painel_agentes/wms_etiqueta_volume.py`, `painel_agentes/test_wms_etiqueta_volume.py`
- Modify: `painel_agentes/wms_fila_impressao.py:17` (`TIPOS = ("CAIXA", "VOLUME")`), a rota `/etiquetas/fila/<id>.pdf` em `painel_agentes.py` (escolhe o gerador pelo tipo), `painel_agentes/wms_separacao.py`

**Interfaces:**
- Produces:
  - `wms_etiqueta_volume.conteudo_qr(codigo_ps: str, numero: int, total: int) -> str` = `FV|<codigo_ps>|<n>|<total>` (C3)
  - `wms_etiqueta_volume.gerar_pdf_volumes(dados: list[dict]) -> bytes`: 10×15 cm, 300 dpi, faixa preta `PS-xxxxx  n / total`, destinatário (nome, bairro, cidade), NF, `embarcador · Freshlog`, QR (H9).
  - `wms_separacao.fechar_volumes(conn, pedido_id: int, total: int, operador: dict) -> int` (id na fila): só pedido `CONFERIDO`; `1 <= total <= 99`; cria os volumes, enfileira tipo `VOLUME` tamanho `10x15`, pedido `EMBALADO`. Chamar de novo com o mesmo total devolve a mesma impressão.
  - `wms_separacao.reimprimir_volume(conn, pedido_id: int, numero: int, por: str) -> int`
- Fonte de destinatário e NF: `stokki.pedidos._parsear_pagina_detalhe` já roda no sync; gravar em `wms_pedidos` as colunas novas `destinatario`, `bairro`, `cidade`, `nf` no `registrar_pedido` (seguir o que as notas da F0 dizem sobre os campos). Campo vazio sai em branco na etiqueta, nunca impede imprimir.
- `ler_codigo` (`wms.py`) passa a reconhecer `FV|` e devolver o volume.

- [ ] **Step 1:** testes: QR; PDF com N páginas para N volumes; `fechar_volumes` idempotente; recusa pedido não conferido; reimpressão soma `reimpressoes`.
- [ ] **Step 2:** implementar. **Step 3:** conferir o PDF no olho (abrir o PNG de uma página). **Step 4:** commit.

### Task 8: Baixa, checagem e cancelamento com as situações novas

**Files:** `painel_agentes/wms_caixas.py` (`consumir_caixas`, `divergencias`, `mover_caixa`), `painel_agentes/wms_pedidos.py` (`baixar_por_expedicao`, `cancelar_reservas`), testes em `test_wms_caixas.py` e `test_wms_pedidos.py`

**Interfaces:**
- `consumir_caixas(..., pedido_id: int | None = None)`: com `pedido_id`, consome primeiro as caixas `SEPARADA`/`DISTRIBUIDA`/`CONFERIDA` daquele pedido naquela chave; o que faltar segue a ordem de hoje. Sem `pedido_id`, igual a hoje, e **nunca** consome caixa presa a outro pedido.
- `divergencias`: caixas `SEPARADA`, `DISTRIBUIDA`, `CONFERIDA` e `DEVOLVIDA` contam como estoque da posição (C6).
- `cancelar_reservas`: itens de separação do pedido viram `DEVOLVIDA` e as caixas inteiras também (C13); `execucao` volta a `''`.
- `mover_caixa`: aceita `DEVOLVIDA` (volta a `EM_ESTOQUE` na posição bipada) e recusa `SEPARADA`/`DISTRIBUIDA`/`CONFERIDA`.

- [ ] **Step 1:** testes: baixa de pedido separado marca `EXPEDIDA` exatamente as caixas dele; baixa de pedido sem separação não toca caixa separada de outro; checagem dá 0 com caixas separadas; cancelamento devolve; guardar devolvida.
- [ ] **Step 2:** implementar. **Step 3:** suíte WMS inteira verde. **Step 4:** commit.

### Task 9: Rotas e abas da PWA

**Files:** `painel_agentes/painel_agentes.py`, `painel_agentes/templates/wms.html`, `painel_agentes/test_wms_separacao_tela.py` (novo), `painel_agentes/acesso_telas.py` (as rotas `/api/wms/separacao/` já caem na tela `wms`; conferir com `test_acesso_telas`)

**Interfaces (todas `POST` com `uuid`, operador da sessão do PIN, níveis de `/api/wms/*`):**
- `GET /api/wms/separacao/ondas` (ondas vivas dos embarcadores com caixas + pedidos soltos para começar sozinho)
- `POST /api/wms/separacao/ondas/pedido/<pedido_id>` (onda de um pedido)
- `POST /api/wms/separacao/ondas/<onda_id>/iniciar`
- `GET /api/wms/separacao/ondas/<onda_id>` (caminhada)
- `POST /api/wms/separacao/ondas/<onda_id>/separar`, `/distribuir`, `/fechar`
- `GET /api/wms/separacao/conferir`, `POST /api/wms/separacao/conferir/<pedido_id>/iniciar`, `POST /api/wms/separacao/conferencias/<id>/bipe`, `/fechar`
- `POST /api/wms/separacao/pedidos/<pedido_id>/volumes`, `/volumes/<numero>/reimprimir`

Abas: **Separar** (substitui a atual quando o embarcador usa caixas; a antiga segue para os outros), **Distribuir** (PS em letra grande), **Conferir** (PS, destinatário, contador de bipes, sem o esperado, botão Fechar), **Volumes**. Erro do servidor aparece em tela cheia vermelha com bip de erro; acerto em verde. Essas abas exigem rede (C7): sem rede, mensagem "sem conexão", nada entra em fila.

- [ ] **Step 1:** testes de rota com `test_client` (padrão de `test_wms_caixas_tela.py`): cada rota responde, erro de regra vira 400 com `erro`, sem operador logado vira 401.
- [ ] **Step 2:** implementar rotas. **Step 3:** implementar abas. **Step 4:** ensaio Playwright na 8099 simulando o leitor HID (digitar + Enter) num pedido de teste inteiro. **Step 5:** commit.

### Task 10: Tela do escritório `/ondas`

**Files:** `painel_agentes/templates/ondas.html` (novo), `painel_agentes/painel_agentes.py`, `painel_agentes/acesso_telas.py` (tela `ondas`, grupo `Galpão`), `painel_agentes/templates/_menu_lateral_nav.html` (área Galpão, sub `Saída`, primeiro item), `MAPA_DO_SISTEMA.txt`

**Interfaces:**
- `GET /ondas` (`requer_auth(niveis=("total", "operador"))`)
- `GET /api/ondas/pedidos?embarcador_id=`, `POST /api/ondas` (`{embarcador_id, pedido_ids}`), `POST /api/ondas/<id>/cancelar`
- `GET /api/ondas/andamento?embarcador_id=`, `GET /api/ondas/faltas`, `POST /api/ondas/faltas/<pedido_id>/decidir`

Três blocos: **Montar onda** (escolhe o embarcador, marca pedidos, vê quem está com falta de estoque, botão "Montar onda"), **Faltas esperando decisão** (o que faltou, quem conferiu, "Voltar para separação" / "Liberar parcial" com motivo), **Andamento** (por pedido: etapa, onda, quem separou, quem conferiu, volumes, horários).

- [ ] **Step 1:** testes de rota e de acesso (nível `galpao` leva 403 em `/ondas` e `/api/ondas/*`). **Step 2:** implementar. **Step 3:** ensaio na 8099. **Step 4:** commit.

### Task 11: Ensaio ponta a ponta e entrada em produção

- [ ] **Step 1:** ensaio local com banco temporário: etiquetar e ativar caixas de 2 produtos, registrar 3 pedidos, montar onda, separar com um operador, distribuir, conferir com outro, uma falta decidida, fechar volumes, baixar um pedido, checagem com 0 divergência.
- [ ] **Step 2:** revisão do ramo inteiro por um revisor novo (skill `code-review` ou `superpowers:requesting-code-review`).
- [ ] **Step 3:** subir o ramo na bancada (`homolog subir galpao-separacao`) para o Hugo passar o fluxo no celular.
- [ ] **Step 4:** com o "deploy" do Hugo: PR, merge, deploy com restart do `painel-agentes`, prova por `test_client` na VPS de cada rota nova.
- [ ] **Step 5:** escrever o roteiro de uma página do teste de segunda para a equipe (quem faz o quê, e que o pedido ainda é terminado na Stokki à mão).

## Fora deste plano

Robô R1/R2 na Stokki; etiqueta avulsa de fracionamento; filtro por data de entrega; comparação com a Stokki no acompanhamento; migração para o freshhub.
