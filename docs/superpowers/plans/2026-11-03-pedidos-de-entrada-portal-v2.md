# Pedidos de Entrada v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trazer o ramo `pedidos-entrada` (aba Pedidos de Entrada do portal, v1 de 26/09) para o master atual e aplicar o que mudou na v2: conferência produto por produto que trava produto fora do cadastro, situação em linguagem de operação, encerramento pelo botão do `/wms` refletido no portal e marcação do que a equipe lançou.

**Architecture:** O lançamento nasce no portal (`portal_cliente/entradas.py`, tabela `portal_entradas`). O worker `enviar_entradas_stokki.py` cria o `#PE` na Stokki pelo wizard, e o timer `sincronizar_recebimentos_wms.py` traz o `#PE` pro `/wms` e amarra os dois lados. `entradas.sincronizar_status` anda o status do portal lendo `wms_recebimentos`. A v2 não cria módulo novo: ajusta `entradas.py`, `envio_pedidos.py` (um helper), `_entradas.html` e a rota de encerramento do painel.

**Tech Stack:** Python 3.11 (`py -3.11`), Flask, SQLite (`dados/dados.db`), Jinja + JS puro, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-02-pedidos-de-entrada-portal-v2-design.md` (delta). Ela vale junto com a v1, `docs/superpowers/specs/2026-09-24-pedidos-de-entrada-portal-design.md`. Plano da v1: `docs/superpowers/plans/2026-09-25-pedidos-de-entrada-portal.md`; a Task 9 dele, de deploy e prova, é substituída pela seção 6 da spec v2.

## Global Constraints

- Worktree: `C:\agente_stokki_eventos\.claude\worktrees\pedidos-entrada`, ramo `pedidos-entrada` (publicado: **sem rebase, sem `--force`**). Atualizar só por `git merge --no-edit origin/master`.
- Python sempre `py -3.11`. `py -3.11 -m py_compile <arquivo>` em todo `.py` editado.
- Testes: `py -3.11 -m unittest ...`. Os de `painel_agentes` rodam **de dentro de** `painel_agentes/`. Nunca misturar `painel_agentes` com `roteirizacao`/`nucleo` no mesmo comando.
- `config.yaml` não existe no worktree e **não pode ser copiado pra lá** (o hook bloqueia). Testes que dependem dele (`test_nivel_logistica`, `test_acesso_telas`) ficam de fora. Nunca ler nem colar o conteúdo do config.
- Nada fala com a Stokki, a Vuupt ou a VPS durante a implementação. Worker só com `--simular` nos testes. Nunca logar na Stokki desta máquina: a sessão é compartilhada e o login derruba produção com 401.
- Commit por tarefa, só com os arquivos da tarefa (`git add <arquivos>`, nunca `-A`). Mensagem `Área: o que mudou e por quê`, em português, terminando com `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. **Push e deploy só com pedido do Hugo.**
- Textos da tela (exatos, da spec V4 e seção 3):
  - trava: `"{n} produto(s) não existe(m) no cadastro: {skus}. Peça o cadastro à Fresh Log."`;
  - sem catálogo: `"Nenhum produto seu está no cadastro da Fresh Log ainda. Peça o cadastro à Fresh Log antes de anunciar."`;
  - situações: `Enviando pra Stokki`, `Criado na Stokki · {#PE}`, `Stokki recusou`, `Recebendo · {X} de {Y} itens`, `Encerrado`, `Com divergência · falta {N} un`, `Cancelado`;
  - marca da equipe: `lançado pela equipe Fresh Log`.
- Lote e validade **não** aparecem nem são pedidos no lançamento (V5).
- Só a Maria Dolores tem a aba ligada (`entradas_ativo`); nada nesta entrega liga outra empresa.

## Review Focus

1. **Nota com um produto fora do cadastro num lote de várias notas:** só essa nota fica travada (vai pra `rejeitados` com a tabela de produtos); as outras continuam confirmáveis. Teste na Task 2 (`test_conferencia_trava_so_a_nota_com_produto_fora`).
2. **Encerrar sem falta no `/wms`:** o portal tem que virar `Encerrado`, não só na divergência. Teste na Task 1 (`test_encerrar_sem_falta_atualiza_portal`).
3. **Caixas com a unidade da nota em CX:** caixas = quantidade da nota; unidades = quantidade × `qtd_por_caixa`. Sem `qtd_por_caixa`, unidades fica `None`. Teste na Task 2 (`test_conferencia_converte_cx_e_un`).
4. **SKU que bate pelo EAN ou DUN, não pelo SKU:** conta como cadastrado, como no Envio. Teste na Task 2 (`test_conferencia_aceita_ean_e_dun`).
5. **Sobra num SKU e falta em outro no mesmo recebimento:** a conferência do portal mostra as duas, e a situação da lista soma só a falta. Teste na Task 3 (`test_linha_com_sobra_e_falta`).

---

### Task 1: Atualizar o ramo com o master e ligar o portal aos dois desfechos do encerramento

**Files:**
- Merge: o ramo inteiro.
- Conflitos conhecidos, vistos em merge de teste em 03/11 com `origin/master` `aca1062`:
  - `docs/superpowers/plans/2026-09-25-pedidos-de-entrada-portal.md` (add/add): ficar com a versão do ramo (`--ours`). É a mais nova, com os passos 7b/7c.
  - `portal_cliente/app.py` (imports): manter os dois lados, `import entradas as entradas_mod` e os quatro imports do master.
  - `portal_cliente/envio_pedidos.py`: manter `_endereco_linha` do master **e** a assinatura do ramo `def ler_nfe(conteudo: bytes, nome_arquivo: str = "", permitir_entrada: bool = False) -> dict:`. Conferir que o corpo de `ler_nfe` ficou com as mudanças dos dois lados (endereço `<entrega>` do master + `permitir_entrada`/itens do ramo).
  - `portal_cliente/gerenciar_clientes.py` (2 blocos: docstring e subparsers): manter os dois lados (`entradas` + `cadastro` + `fluxos`).
  - `painel_agentes/painel_agentes.py`, rota `api_wms_recebimento_encerrar_divergencia`: ficar com o bloco do master (`envio = {"enviado": False, "motivo": "sem_falta"}` + e-mail só quando `estado == "DIVERGENCIA"`). **Depois dele, fora do `if`**, vem o `try` do ramo que importa `entradas` e chama `portal_entradas.sincronizar_status(conn)`, com o mesmo `except` que só loga.
- Test: `painel_agentes/test_wms_faltas_recebimento.py`
- Modify: `MAPA_DO_SISTEMA.txt` (só se o merge deixar conflito ou duplicata na seção do portal/WMS)

**Interfaces:**
- Consumes: `entradas.sincronizar_status(conn) -> int`, `entradas.garantir_tabelas(conn)`, rota `/api/wms/recebimentos/<id>/encerrar` (master: mesma função registrada em `/encerrar` e `/encerrar-divergencia`).
- Produces: ramo `pedidos-entrada` contendo `origin/master`; a rota de encerrar atualiza `portal_entradas` nos dois desfechos.

- [ ] **Step 1:** `git status --short` e `git log --oneline -3` no worktree. O esperado é só a spec v2 e este plano não rastreados. Commitar os dois antes do merge: `git add docs/superpowers/specs/2026-11-02-pedidos-de-entrada-portal-v2-design.md docs/superpowers/plans/2026-11-03-pedidos-de-entrada-portal-v2.md`, mensagem `Docs: spec e plano da v2 dos Pedidos de Entrada (lançamento nasce no portal)`.
- [ ] **Step 2:** `git fetch origin && git merge --no-edit origin/master`. Resolver os conflitos como listado acima. Se aparecer conflito **fora** dessa lista, resolver mantendo os dois comportamentos e registrar no relatório o que fez.
- [ ] **Step 3:** `py -3.11 -m py_compile` em todos os `.py` que tiveram conflito, e `py -3.11 -c "import jinja2; jinja2.Environment().parse(open('portal_cliente/templates/_entradas.html', encoding='utf-8').read())"`.
- [ ] **Step 4: Escrever o teste que falha** em `painel_agentes/test_wms_faltas_recebimento.py`, na classe que já testa a rota de encerrar com `app.test_client()`. O teste `test_encerrar_sem_falta_atualiza_portal`:
  - cria `portal_entradas` com `entradas.garantir_tabelas(conn)` no mesmo banco temporário, mais uma linha `status='CHEGOU'` e `wms_recebimento_id` = o recebimento do teste;
  - endereça tudo, faz `POST /api/wms/recebimentos/<id>/encerrar` e espera 200;
  - `assert` que `portal_entradas.status == 'ENDERECADO'`.

  Antes de mexer na rota, comentar a chamada a `sincronizar_status` e confirmar que o teste falha. Depois, um segundo teste, `test_encerrar_com_falta_atualiza_portal` → `'DIVERGENCIA'`.
- [ ] **Step 5:** Rodar, de dentro de `painel_agentes/`: `py -3.11 -m unittest $(ls test_wms*.py | sed 's/\.py$//')`. O esperado é OK. Hoje são 260 testes, mais os 2 novos.
- [ ] **Step 6:** Rodar, da raiz do worktree: `py -3.11 -m unittest portal_cliente.test_entradas portal_cliente.test_enviar_entradas_stokki portal_cliente.test_envio_pedidos stokki.test_recebimentos test_sincronizar_recebimentos_wms`. O esperado é OK. Se `portal_cliente.test_envio_pedidos` não existir com esse nome, rodar o `test_*envio*` que existir em `portal_cliente/`. Falha em arquivo que esta entrega toca se corrige aqui; falha em arquivo alheio se reporta sem mexer (spec v2, seção 8).
- [ ] **Step 7: Commit.** O merge commit leva a resolução dos conflitos, inclusive a chamada fora do `if` em `painel_agentes.py`. Os 2 testes novos vão num commit separado, só com `painel_agentes/test_wms_faltas_recebimento.py`. Mensagem: `Portal: entrada do portal acompanha o encerramento pelo botao do /wms (com e sem falta)`.

Nota (spec v2 seção 8, "Recebendo com caixas"): já conferido em 03/11. `wms_caixas.ativar_caixa` chama `wms_pedidos.contabilizar_enderecamento`, que soma `qtd_enderecada`, então o `CHEGOU` do portal dispara também com caixas ligadas. Não precisa de código.

### Task 2: Conferência produto por produto e trava de produto fora do cadastro

**Files:**
- Modify: `portal_cliente/envio_pedidos.py` (extrair o filtro do catálogo de `validar_skus` para um helper; comportamento de `validar_skus` idêntico)
- Modify: `portal_cliente/entradas.py` (`conferir_itens`, `validar_entrada`)
- Modify: `portal_cliente/app.py` (`api_entradas_analisar`: `conferencia` em `itens` e em `rejeitados`)
- Test: `portal_cliente/test_entradas.py`; os testes de Envio continuam verdes sem mudança

**Interfaces:**
- Produces:
  - `envio_pedidos.produtos_do_embarcador(conn, cfg: dict) -> list[sqlite3.Row] | None`: `None` quando a tabela `wms_produtos` não existe ou `cfg` não tem `client_id` nem `nome`; senão a lista de linhas ativas (`ativo = 1`) do embarcador pelo mesmo filtro de hoje (`embarcador_id = ? OR LOWER(embarcador) = LOWER(?)`), com as colunas `sku, ean, dun, descricao, unidade, qtd_por_caixa`. `validar_skus` passa a usar esse helper e devolve exatamente as mesmas mensagens de hoje.
  - `entradas.conferir_itens(conn, item: dict, cfg: dict) -> list[dict] | None`: `None` = sem catálogo (helper devolveu `None` ou lista vazia). Senão, um dict por linha de `item["itens_lista"]`: `{linha, sku, descricao, quantidade, unidade, cadastrado: bool, qtd_por_caixa: float | None, caixas: float | None, unidades: float | None}`. Casa pelo SKU, EAN ou DUN em maiúsculas, como `validar_skus`. A descrição vem do catálogo quando cadastrado, senão a da nota.
  - Regra de caixas/unidades: unidade da nota normalizada (`strip().upper()`) começando com `"CX"` ou igual a `"CAIXA"`: `caixas = quantidade`, `unidades = quantidade * qtd_por_caixa` (ou `None` sem `qtd_por_caixa`). Qualquer outra unidade: `unidades = quantidade`, `caixas = quantidade / qtd_por_caixa` arredondado a 2 casas (ou `None`). Ruling: a quantidade da Stokki é em embalagem (CX) e a conversão pra UN é nossa (notas F0); a spec diz só "quantidade ÷ qtd_por_caixa".
  - `entradas.validar_entrada(...)` devolve também `"conferencia": list[dict] | None`. Deixa de usar as mensagens de SKU de `ep.validar_skus` e passa a dar erro com os textos exatos das Global Constraints: produto não cadastrado → trava; `conferir_itens` `None` → erro "Nenhum produto seu…". Isso muda a v1, em que sem catálogo era só aviso. Sem `cfg` (erro de `config_stokki_cliente`) continua como hoje.
  - `api_entradas_analisar`: cada dict de `itens` ganha `"conferencia"`. Cada rejeitado **por validação** (o ramo `if not v["ok"]`) ganha `"conferencia": v["conferencia"]` além de `erro`.

- [ ] **Step 1: Testes que falham** em `portal_cliente/test_entradas.py`, com a fixture de catálogo que os testes de SKU já usam:
  - `test_conferencia_converte_cx_e_un`: item `unidade="CX"`, `quantidade=20`, produto com `qtd_por_caixa=12` → `caixas == 20`, `unidades == 240`. Item `unidade="UN"`, `quantidade=96`, `qtd_por_caixa=12` → `caixas == 8`, `unidades == 96`. Produto sem `qtd_por_caixa` → `caixas is None` no UN, `unidades is None` no CX.
  - `test_conferencia_aceita_ean_e_dun`: SKU da nota igual ao EAN de um produto → `cadastrado is True`; igual ao DUN → `True`.
  - `test_produto_fora_do_cadastro_trava_com_texto`: um SKU fora → `ok is False` e `erros == ["1 produto(s) não existe(m) no cadastro: X9. Peça o cadastro à Fresh Log."]`. A `conferencia` marca `cadastrado False` só nessa linha.
  - `test_sem_catalogo_trava`: embarcador sem nenhum produto → `ok is False`, erro igual ao texto "Nenhum produto seu…", `conferencia is None`. Ajustar o teste da v1 que esperava aviso nesse caso, citando a V4 no comentário.
  - `test_conferencia_trava_so_a_nota_com_produto_fora` (rota): com `app.test_client()` e uma sessão de cliente de teste, como os testes de rota do portal já fazem; se não houver padrão de sessão, chamar diretamente a função interna que monta `itens/rejeitados`. Duas NF-e sintéticas, uma com SKU fora → `len(itens) == 1`, `len(rejeitados) == 1`, `rejeitados[0]["conferencia"]` não vazio.
- [ ] **Step 2:** Rodar `py -3.11 -m unittest portal_cliente.test_entradas`. O esperado é FAIL nos novos.
- [ ] **Step 3:** Implementar `produtos_do_embarcador`, `conferir_itens`, a mudança em `validar_entrada` e a rota.
- [ ] **Step 4:** Rodar `py -3.11 -m unittest portal_cliente.test_entradas` e os testes de Envio do `portal_cliente/`. O esperado é OK, com Envio sem mudança de mensagem.
- [ ] **Step 5: Commit** `portal_cliente/envio_pedidos.py portal_cliente/entradas.py portal_cliente/app.py portal_cliente/test_entradas.py`. Mensagem: `Portal: entrada confere produto por produto e trava SKU fora do cadastro (sem catalogo tambem trava)`.

### Task 3: Situação da lista, contagem de itens, divergência com sobra e marca da equipe

**Files:**
- Modify: `portal_cliente/entradas.py` (`_conferencia_do_galpao`, `_linha`)
- Test: `portal_cliente/test_entradas.py`

**Interfaces:**
- Consumes: `wms_recebimento_itens.qtd_un/qtd_enderecada/falta_un`, `wms_recebimentos.estado`, `portal_entradas.enviado_por`. Vale `_quem_envia()` do portal: `"equipe:<usuario>"` ou `"cliente"`.
- Produces: cada dict de `listar_entradas` ganha
  - `situacao_rotulo: str`: os textos exatos da tabela da spec seção 3, nesta ordem de decisão:
    1. `CANCELADO` → `Cancelado`;
    2. `stokki_status == "ERRO"` → `Stokki recusou`;
    3. `DIVERGENCIA` → `Com divergência · falta {N} un`, com N = soma de `falta` da conferência, formatada sem casas quando inteira;
    4. `ENDERECADO` → `Encerrado`;
    5. `CHEGOU` → `Recebendo · {X} de {Y} itens`;
    6. `ANUNCIADO` + `CRIADO` → `Criado na Stokki · {stokki_codigo}`;
    7. `ANUNCIADO` + `NA_FILA`/`ENVIANDO` → `Enviando pra Stokki`.
  - `itens_recebidos: int` (X = SKUs da conferência com `recebido > 0`) e `itens_total: int` (Y = SKUs da conferência). Os dois são 0 sem recebimento ligado.
  - `lancado_pela_equipe: bool` = `enviado_por` começa com `"equipe:"`. Ruling: isso substitui a coluna nova `lancado_por_equipe` da spec seção 8, porque o carimbo `equipe:<usuario>` já é determinístico. O texto da spec é corrigido no Step 5.
  - Cada linha de `conferencia` ganha `sobra: float` (`max(recebido - anunciado, 0)`) e `situacao: "OK" | "FALTA" | "SOBRA"`, por SKU:
    - FALTA quando `falta > 0`, ou quando `recebido < anunciado` com o recebimento já ENDERECADO/DIVERGENCIA;
    - SOBRA quando `recebido > anunciado`;
    - OK nos demais.

    Ruling: a spec pede "a mesma tabela de `resumo_encerramento`". O portal não importa `wms_pedidos`, que faz `import wms` fora de pacote. Por isso a conta continua por SKU em `entradas.py`, com os mesmos critérios (anunciado × recebido, falta congelada). É por SKU porque o wizard soma linhas do mesmo SKU (risco da v1).

- [ ] **Step 1: Testes que falham:**
  - `test_situacao_rotulo_em_cada_estado`: um caso por linha da ordem acima, com `assertEqual` no texto exato. Ex.: `"Criado na Stokki · #PE-1188"`, `"Recebendo · 1 de 2 itens"`, `"Com divergência · falta 12 un"`.
  - `test_linha_com_sobra_e_falta`: recebimento DIVERGENCIA. SKU A tem `qtd_un=10`, `qtd_enderecada=12`; SKU B tem `qtd_un=10`, `qtd_enderecada=0`, `falta_un=10`. Esperado: A `situacao "SOBRA"`, `sobra 2`; B `"FALTA"`; `situacao_rotulo == "Com divergência · falta 10 un"`.
  - `test_lancado_pela_equipe`: `enviado_por="equipe:hugo"` → `True`; `"cliente"` → `False`.
- [ ] **Step 2:** Rodar `py -3.11 -m unittest portal_cliente.test_entradas`. O esperado é FAIL nos novos.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** Rodar `py -3.11 -m unittest portal_cliente.test_entradas`. O esperado é OK.
- [ ] **Step 5:** Na spec v2 seção 8, trocar o item "Lançado pela equipe" por: `vem do carimbo enviado_por = "equipe:<usuario>" que o portal já grava (sem coluna nova)`.
- [ ] **Step 6: Commit** `portal_cliente/entradas.py portal_cliente/test_entradas.py docs/superpowers/specs/2026-11-02-pedidos-de-entrada-portal-v2-design.md`. Mensagem: `Portal: entrada mostra a situacao em linguagem de operacao, itens recebidos e sobra na conferencia`.

### Task 4: A tela (`_entradas.html`) no desenho aprovado

**Files:**
- Modify: `portal_cliente/templates/_entradas.html`
- Modify: `MAPA_DO_SISTEMA.txt` (linha da aba Pedidos de Entrada: v2, trava de cadastro, equipe pelo `/equipe`)

**Interfaces:**
- Consumes: `conferencia` em `itens`/`rejeitados` (Task 2); `situacao_rotulo`, `itens_recebidos`, `itens_total`, `lancado_pela_equipe` e `conferencia[].situacao/sobra` (Task 3); variável de template `equipe` (context processor do portal) e `cliente.nome`.

Referência visual: `C:\agente_stokki_eventos\.superpowers\brainstorm\7715-1793656559\content\tela-entradas.html`. **Sem** as colunas lote/validade (V5). Onde o esboço e a spec divergirem, vale a spec.

- [ ] **Step 1: Faixa da equipe.** `{% if equipe %}` no topo do card "Anunciar":
  `Lançando como <b>{{ cliente.nome }}</b> · equipe Fresh Log ({{ equipe.usuario }})`, seguido do link "trocar" para `url_for('equipe_cliente')`.
- [ ] **Step 2: Prévia por nota com a tabela de produtos.**
  - Cada remessa válida mostra, abaixo da linha atual, a tabela com as colunas SKU, Produto, Qtd (un), Caixas e Cadastro. `None` aparece como `—`. Cadastro: `OK` em verde ou `Não cadastrado` em vermelho.
  - Cada rejeitado com `conferencia` mostra a mesma tabela dentro do bloco de rejeitados, com o texto do erro em destaque.
  - Rejeitado sem `conferencia` continua como hoje.
  - O botão Confirmar cobre só as válidas e fica desabilitado sem nenhuma válida (já é assim).
- [ ] **Step 3: Lista.**
  - A coluna "Situação" passa a mostrar `situacao_rotulo`, com a cor por estado (classes `b-*` existentes; `Stokki recusou` em vermelho, com o `stokki_erro` no `title` e numa linha abaixo).
  - A coluna "Stokki" sai, porque o `#PE` já está no rótulo. Atualizar o `colspan` do detalhe.
  - O chip `equipe` passa a usar `x.lancado_pela_equipe`, com o texto `lançado pela equipe Fresh Log`.
  - No detalhe, a tabela "Conferência do galpão" ganha a coluna `Sobrou` e a linha SOBRA destacada. O botão de expandir de entradas DIVERGENCIA passa a dizer `ver divergência`.
- [ ] **Step 4: Verificar:**
  - jinja parse (comando da Task 1, Step 3);
  - subir o portal num banco temporário em `127.0.0.1:8099` (nunca a porta do serviço), com 3 entradas sintéticas: CRIADO, CHEGOU e DIVERGENCIA com sobra;
  - abrir `?aba=entradas` com Playwright (skill `webapp-testing`), com sessão de cliente e de equipe;
  - conferir os três rótulos, a faixa só na sessão de equipe e, numa prévia com SKU fora do cadastro, a tabela com `Não cadastrado` e o botão desabilitado;
  - screenshot em 390 px e em 1280 px no scratchpad.
- [ ] **Step 5: Commit** `portal_cliente/templates/_entradas.html MAPA_DO_SISTEMA.txt`. Mensagem: `Portal: aba Pedidos de Entrada no desenho de 02/11 (conferencia por produto, situacao, faixa da equipe)`.

### Fim: revisão e entrega

- [ ] Revisão final do ramo inteiro (`git diff origin/master...HEAD`). A review de segurança é obrigatória (`differential-review`), porque `portal_cliente/` é exposto na internet (skill deploy-vps, seção 1.4). O relatório vai em `docs/revisoes/2026-11-03-pedidos-entrada-v2.md`, sem commit.
- [ ] **Parar aqui.** Push, deploy, ligar `portal-cliente-entradas` e a prova da spec v2, seção 6 (uma nota real combinada), só com o Hugo.
