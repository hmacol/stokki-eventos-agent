# Coletas na Expedição: plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a expedição do galpão dá saída em pedido de retirada pelo painel, com folha impressa, nome, placa e foto obrigatórios, e o pedido passa a aparecer como "Coletado" na /pedidos.

**Architecture:** um job próprio lê a listagem "Aguardando Transportador" da Stokki a cada 10 min e mantém uma fila local (com a transportadora). A tela `/expedicao/coletas` monta folhas a partir dessa fila, gera o PDF, recebe a confirmação e põe o robô `expedir_pedidos.py` na fila de execuções do painel com um sinal `--coleta N`. A /pedidos calcula o status COLETADO a partir da fila, como já faz com o IMPORTADO.

**Tech Stack:** Python 3.11, Flask (painel_agentes), SQLite (`dados/dados.db`), unittest, PIL/pypdf (mesma do romaneio), systemd timer.

**Spec:** `docs/superpowers/specs/2026-11-12-coletas-expedicao-design.md`

## Global Constraints

- Idioma português; comentários e logs sem acento onde o arquivo já segue isso; arquivos CRLF continuam CRLF.
- Testes: `py -3.11 -m unittest <modulo>`; os de `painel_agentes` rodam de dentro de `painel_agentes/`, nunca junto com `nucleo`/`roteirizacao`.
- Nunca ler nem alterar `config.yaml`. Nenhuma chave nova de config neste plano.
- Nada de login na Stokki a partir do notebook: tudo que fala com a Stokki é testado com sessão falsa.
- Stokki só por `stokki/` (`iterar_todos_pedidos`, `extrair_transportadora`, `extrair_id_da_linha`) e sempre com a trava `stokki/sessao_uso`.
- Regra de retirada é uma só: nome contém "CLIENTE RETIRA" ou `CatalogoTransportadoras.resolver(nome).tipo == "RETIRADA"`.
- Sem foto, nome e placa não existe confirmação: validado no servidor, não só na tela.
- Folha: no máximo 30 pedidos, uma transportadora; no grupo Cliente Retira, um embarcador.
- Na homologação nada vai pra Stokki (`_EnvioBloqueadoHomolog`): a coleta conclui simulada.
- Rota nova entra em `acesso_telas.TELAS`; `MAPA_DO_SISTEMA.txt` atualizado no mesmo commit.
- Commit, push e deploy só quando o Hugo pedir. Os passos "Commit" abaixo ficam pendentes até lá.

## Review Focus

- Pedido some da listagem porque foi **cancelado**, não enviado: não pode virar Coletado. O job só marca saída quando a leitura foi completa; cancelamento vem de `galpao_pedidos.situacao = 'Cancelado'` ou do resultado do robô.
- Leitura **parcial** da Stokki (cedeu a vez, erro no meio): ninguém "some". Teste no Task 2.
- Dois expedidores geram folha com o **mesmo pedido** ao mesmo tempo: a segunda é recusada (índice único parcial). Teste no Task 3.
- Confirmação reenviada (duplo clique): não cria segunda execução nem pede foto de novo. Teste no Task 3.
- Retirada nova chamada pelo botão da /pedidos ou por `--pedido --tela-pedidos` direto: recusada no robô. Teste no Task 5.

---

### Task 1: Banco das coletas

**Files:**
- Create: `coletas/__init__.py`, `coletas/banco.py`
- Test: `coletas/test_banco.py`

**Interfaces — Produces:**
- `garantir_esquema(conn)`: cria
  - `expedicao_coletas_fila(id_stokki INTEGER PRIMARY KEY, codigo TEXT, embarcador TEXT, transportadora TEXT, retirada INTEGER, carga_inicial INTEGER, visto_primeiro_em TEXT, visto_ultimo_em TEXT, saiu_em TEXT)`
  - `expedicao_coletas(id INTEGER PRIMARY KEY AUTOINCREMENT, transportadora TEXT, embarcador TEXT, status TEXT, criada_em TEXT, criada_por TEXT, confirmada_em TEXT, confirmada_por TEXT, motorista_nome TEXT, placa TEXT, foto_arquivo TEXT, execucao_id INTEGER, token TEXT, simulada INTEGER DEFAULT 0)`; status `ABERTA | CONFIRMADA | CONCLUIDA | CANCELADA`
  - `expedicao_coletas_pedidos(coleta_id INTEGER, id_stokki INTEGER, codigo TEXT, embarcador TEXT, unidades INTEGER, nf TEXT, destinatario TEXT, estado TEXT, mensagem TEXT, PRIMARY KEY (coleta_id, id_stokki))`; estado `NA_FOLHA | NAO_SAIU | EXPEDINDO | EXPEDIDO | FALHOU | RETIRADO`
  - índice único parcial em `expedicao_coletas_pedidos(id_stokki) WHERE estado IN ('NA_FOLHA','EXPEDINDO','FALHOU')`
- `FMT = "%Y-%m-%d %H:%M:%S"`; `numero(coleta_id) -> "COL-<id>"`.

- [ ] Teste: `garantir_esquema` duas vezes não falha; inserir o mesmo `id_stokki` em duas coletas com estado `NA_FOLHA` levanta `sqlite3.IntegrityError`; com a primeira em `NAO_SAIU` passa.
- [ ] Rodar e ver falhar; implementar; rodar e ver passar (`py -3.11 -m unittest coletas.test_banco`).
- [ ] Commit.

### Task 2: Job que lê a listagem da Stokki

**Files:**
- Create: `coletas/ler_stokki.py`, `infra/expedicao-coletas-ler.service`, `infra/expedicao-coletas-ler.timer` (07h–20h, a cada 10 min, seg–sáb; copiar o formato de `infra/galpao3d-coletar.*`)
- Test: `coletas/test_ler_stokki.py`

**Interfaces:**
- Consumes: `stokki.pedidos.iterar_todos_pedidos(sessao, status=STATUS_AGUARDANDO_TRANSPORTADOR)`, `extrair_transportadora`, `extrair_id_da_linha`; `stokki.sessao_uso.adquirir/liberar` (como em `galpao3d/coletar.py:119`); `regras.transportadoras.CatalogoTransportadoras.carregar_do_banco`.
- Produces: `eh_retirada(nome: str, catalogo) -> bool` (a regra única, usada pelos Tasks 3 e 5); `ler(conn, config, sessao_fabrica=None, agora=None) -> str` devolvendo `"ok" | "ocupada" | "erro"`; `main(argv)` com `--modo-teste` (lê e mostra, não grava).

Comportamento de `ler` numa leitura completa:
- linha nova: insere com `visto_primeiro_em = agora`; `carga_inicial = 1` só se a tabela estava vazia antes desta leitura;
- linha conhecida: atualiza `transportadora`, `retirada`, `visto_ultimo_em`; se tinha `saiu_em`, limpa (voltou);
- linha conhecida que não veio: `saiu_em = agora`, se ainda estava nula.
Erro ou trava ocupada: nada é gravado.

- [ ] Testes com sessão falsa (lista de dicts `{"id": ..., "client": ..., "carrier": "<span>J3 FLEX</span>"}`; conferir o formato real em `stokki/fixtures` e em `extrair_id_da_linha`):
  - `test_primeira_leitura_marca_carga_inicial` (2 pedidos, ambos `carga_inicial == 1`; um terceiro numa segunda leitura fica `0`);
  - `test_quem_sumiu_ganha_saiu_em_e_quem_voltou_perde`;
  - `test_erro_no_meio_nao_grava_nada` (iterador levanta na 2a linha: tabela igual a antes, retorno `"erro"`);
  - `test_trava_ocupada_devolve_ocupada`;
  - `test_eh_retirada` ("CLIENTE RETIRA 15.060..." → True; nome tipo RETIRADA no catálogo → True; tipo ENTREGA, vazio e desconhecido → False).
- [ ] Ver falhar; implementar; ver passar.
- [ ] Commit.

### Task 3: Regras da coleta (lista, folha, confirmação)

**Files:**
- Create: `coletas/regras.py`
- Test: `coletas/test_regras.py`

**Interfaces — Produces** (todas recebem `conn`; erro de regra levanta `ErroColeta(str)`):
- `a_coletar(conn) -> {"grupos": [{"transportadora", "embarcador" | None, "pedidos": [{id_stokki, codigo, embarcador, unidades, nf, destinatario, carga_inicial}]}], "sem_cadastro": [...]}`: fila com `saiu_em IS NULL`, fora de folha viva. `retirada = 1` vai para `grupos` (Cliente Retira dividido por embarcador), o resto para `sem_cadastro`. `unidades` de `galpao_pedidos`; `nf`/`destinatario`/volumes do último `portal_envios` do código quando as tabelas existem (mesmo cuidado de `nucleo/tabela_pedidos._norm`).
- `gerar_folha(conn, ids_stokki: list[int], usuario: str, agora=None) -> int` (id da coleta, status ABERTA, pedidos `NA_FOLHA`).
- `cancelar_folha(conn, coleta_id, usuario)`: só ABERTA; pedidos viram `NAO_SAIU`.
- `confirmar(conn, coleta_id, ids_que_sairam: list[int], motorista_nome: str, placa: str, foto_arquivo: str, usuario: str, agora=None) -> list[str]` (códigos a expedir): status CONFIRMADA, marcados `EXPEDINDO`, demais `NAO_SAIU`. Lista vazia = cancela a folha e devolve `[]`.
- `registrar_execucao(conn, coleta_id, execucao_id, token)`.
- `aplicar_resultados(conn, coleta_id, resultados: dict[str, {"ok": bool, "mensagem": str}], simulada=False)`: `EXPEDIDO`/`FALHOU`; sem pendência → CONCLUIDA; EXPEDIDO grava `saiu_em` na fila se nulo.
- `tentar_de_novo(conn, coleta_id) -> list[str]` (os FALHOU voltam a `EXPEDINDO`); `retirar_pedido(conn, coleta_id, id_stokki, usuario)` (FALHOU → RETIRADO, reavalia conclusão).
- `coleta(conn, coleta_id) -> dict`, `abertas(conn) -> list`, `historico(conn, limite=50) -> list`, `coleta_do_pedido(conn, codigo) -> dict | None` (a última com o pedido EXPEDIDO: número, motorista, placa, confirmada_em).
- `tem_foto(conn, coleta_id, codigos: list[str]) -> bool`: coleta CONFIRMADA com `foto_arquivo` e todos os códigos em `EXPEDINDO` (o robô usa no Task 5).

- [ ] Testes (um por regra): mais de 30 pedidos; transportadoras misturadas; Cliente Retira com dois embarcadores; pedido fora da fila ou com `saiu_em`; pedido já em folha viva (`ErroColeta`, e a corrida via `IntegrityError` vira `ErroColeta`); `confirmar` sem foto, sem nome, sem placa (cada um recusa); `confirmar` duas vezes (a segunda levanta `ErroColeta("coleta já confirmada")`); tudo desmarcado cancela; resultados parciais deixam CONFIRMADA e todos ok concluem; `retirar_pedido` conclui; `tem_foto` falso para coleta ABERTA e para código fora dela; `a_coletar` não mostra pedido em folha ABERTA e volta a mostrar depois de `cancelar_folha`.
- [ ] Ver falhar; implementar; ver passar.
- [ ] Commit.

### Task 4: PDF da folha de coleta

**Files:**
- Create: `coletas/folha_pdf.py`
- Test: `coletas/test_folha_pdf.py`

**Interfaces:** Consumes `regras.coleta(conn, id)`. Produces `gerar(coleta: dict) -> bytes` (PDF A4 em pé, 1 página).

Conteúdo, nesta ordem: "Fresh Log — Folha de coleta COL-n"; data/hora, transportadora (e embarcador, se Cliente Retira), quem gerou; tabela `Levou [ ] | Pedido | Embarcador | NF | Destinatário | Unid.`; "Total: N pedidos, M unidades"; "Recebi os pedidos marcados acima."; linhas em branco rotuladas Nome completo, CPF ou RG, Placa, Data e hora, Assinatura. Desenhar como o romaneio faz (PIL → PDF, ver `roteirizacao/gerar_pdf_romaneios.py`), 30 linhas têm de caber.

- [ ] Teste: `gerar` com 1 e com 30 pedidos devolve bytes que começam com `%PDF` e `pypdf.PdfReader` conta 1 página.
- [ ] Ver falhar; implementar; ver passar; abrir o PDF de 30 linhas e conferir a olho.
- [ ] Commit.

### Task 5: Robô — sinal da coleta e trava da retirada nova

**Files:**
- Modify: `expedir_pedidos.py` (`_retiradas_pela_transportadora` ~1278, bloco `if faltando and tela_pedidos` ~1407, `main` ~1344, argparse ~1812)
- Test: `test_expedir_coletas.py` (raiz, ao lado dos testes existentes do robô)

**Interfaces:**
- Consumes: `coletas.regras.tem_foto`, `coletas.ler_stokki.eh_retirada`, tabela `expedicao_coletas_fila`.
- Produces: argumento `--coleta N` (só com `--pedido ... --tela-pedidos`); `main(..., coleta: int | None = None)`.

Regra para pedido sem entrega na Vuupt cuja transportadora é de retirada:
- com `--coleta N`: expede se `tem_foto(conn, N, codigos)`; senão recusa todos com "coleta sem foto confirmada";
- sem `--coleta`: expede só se o pedido está na fila com `carga_inicial = 1`; senão recusa com "retirada sai pela aba Coletas (Expedição)". Pedido que nem está na fila também recusa, com "ainda não lido pela aba Coletas; tente em 10 min por lá".
`_retiradas_pela_transportadora` passa a usar `eh_retirada` (uma regra só). Pedido entregue na Vuupt não muda.

- [ ] Testes, isolando a decisão numa função pura `_decidir_retirada(conn, codigo, nome_transportadora, catalogo, coleta) -> (bool, str)`: antigo sem coleta passa; novo sem coleta recusa; fora da fila recusa; novo com coleta com foto passa; com coleta sem foto recusa; transportadora que não é retirada recusa com a mensagem de hoje.
- [ ] Ver falhar; implementar; ver passar; rodar os testes existentes do robô e de `painel_agentes/test_acoes_pedidos.py`.
- [ ] Commit.

### Task 6: Tela /expedicao/coletas

**Files:**
- Create: `painel_agentes/templates/coletas_expedicao.html`, `painel_agentes/test_coletas_expedicao.py`
- Modify: `painel_agentes/painel_agentes.py` (rotas, ao lado de `/expedicao/consulta` ~1024), `painel_agentes/acesso_telas.py:68` (entrada `("expedicao_coletas", "Coletas", "Operar", [r"^/expedicao/coletas", r"^/api/expedicao/coletas"])`), `painel_agentes/templates/expedicao.html` (link "Coletas" no topo), `painel_agentes/acoes_pedidos.py` (`expedir_stokki` ganha `coleta: int | None = None`, que acrescenta `--coleta N` aos args)

**Rotas** (`NIVEIS_COLETA_VER = total, operador, leitura, atendimento, logistica, expedicao, galpao`; `NIVEIS_COLETA_AGIR = total, operador, expedicao, galpao`; POST com `@exige_mesma_origem`):
- `GET /expedicao/coletas` (VER): a coletar, folhas abertas, histórico, hora da última leitura da Stokki (aviso se passou de 30 min).
- `POST /api/expedicao/coletas/folha` (AGIR) `{ids}` → `{coleta_id, numero}`.
- `GET /expedicao/coletas/<id>/folha.pdf` (VER).
- `POST /api/expedicao/coletas/<id>/cancelar` (AGIR).
- `POST /api/expedicao/coletas/<id>/confirmar` (AGIR), multipart: `ids`, `motorista_nome`, `placa`, `foto`. Foto: JPEG/PNG/WebP validada abrindo com PIL, até 12 MB, regravada como JPEG em `dados/coletas/COL-<id>.jpg`. Depois `regras.confirmar` → `acoes_pedidos.expedir_stokki(..., coleta=id)` → `registrar_execucao`. `_EnvioBloqueadoHomolog`: `aplicar_resultados(todos ok, simulada=True)` e responde `{simulada: true}`.
- `GET /api/expedicao/coletas/<id>/andamento` (VER): usa `andamento_expedicao`; no `fim`, chama `aplicar_resultados` uma vez e devolve o estado por pedido.
- `POST /api/expedicao/coletas/<id>/tentar-de-novo` e `.../retirar` (AGIR).
- `GET /expedicao/coletas/<id>/foto` (VER): `send_file`, como `/retornos/arquivo` (~1579).

Tela: funciona no celular (a foto sai dele: `<input type="file" accept="image/*" capture="environment">`); botão Confirmar desligado até ter foto, nome e placa; `url_for()` em tudo; seguir o visual de `consulta_pedido_expedicao.html`.

- [ ] Testes (test_client, padrão de `painel_agentes/test_pedidos_tabela.py`): tela abre para `galpao` e `expedicao`; `leitura` vê e leva 403 no POST; gerar folha → PDF 200 `application/pdf`; confirmar sem foto 400, sem placa 400, com arquivo que não é imagem 400; confirmar ok chama `iniciar` com `--coleta <id>` nos args (iniciar falso); segundo confirmar 400 sem nova execução; andamento `fim` com um ok e um falho deixa a coleta CONFIRMADA e mostra o motivo; foto exige login.
- [ ] Ver falhar; implementar; ver passar; rodar `test_acesso_telas`.
- [ ] Abrir em 8099+ com Playwright (computador e celular): gerar folha, confirmar com foto, ver o andamento com execução falsa. Guardar prints.
- [ ] Commit.

### Task 7: /pedidos — status Coletado e botão antigo

**Files:**
- Modify: `nucleo/tabela_pedidos.py` (`STATUS`, `_norm`), `painel_agentes/painel_agentes.py` (`_pedido_celular`), `painel_agentes/templates/pedidos_tabela.html` (aba, fila, cor, grade do painel, `bloqueio("expedir_stokki")`)
- Test: `nucleo/test_tabela_pedidos.py`, `painel_agentes/test_pedidos_tabela.py`

**Regras:**
- `COLETADO = "COLETADO"`, depois de IMPORTADO em `STATUS`; rótulo "Coletado".
- Em `_norm`, se `expedicao_coletas_fila` existe: linha do galpão (fora do núcleo, não cancelada) cujo `id_stokki` está na fila com `retirada = 1` e `saiu_em` preenchido sai como COLETADO, com `situacao_stokki = 'Enviado'`. Sem a tabela, nada muda.
- Painel do pedido: quando `coleta_do_pedido` acha, mostra "Coleta COL-n", motorista, placa, data e link da foto.
- `bloqueio("expedir_stokki")` na tela: pedido IMPORTADO com `retirada_nova` (campo novo da linha: está na fila, `retirada = 1`, `carga_inicial = 0`) → "Retirada sai pela aba Coletas (Expedição)". A trava de verdade é a do Task 5.

- [ ] Testes: pedido na fila com `saiu_em` vira COLETADO e conta na fila "Coletados"; mesmo pedido sem `saiu_em` segue IMPORTADO; cancelado no galpão segue CANCELADO; com pedido no núcleo o núcleo vence; banco sem a tabela dá o resultado de hoje (os 30 testes atuais passam sem mudança, fora a contagem que ganha `"COLETADO": 0`).
- [ ] Ver falhar; implementar; ver passar; rodar a bateria do painel (213 testes) e o Playwright mobile (81 checagens).
- [ ] Commit.

### Task 8: Mapa, bateria completa e prova

- [ ] `MAPA_DO_SISTEMA.txt`: pasta `coletas/`, tela, timer `expedicao-coletas-ler`, tabelas `expedicao_coletas*`, pasta `dados/coletas/`, status COLETADO e a regra do botão.
- [ ] `py -3.11 -m py_compile` em tudo que mudou; `py -3.11 -m unittest coletas.test_banco coletas.test_ler_stokki coletas.test_regras coletas.test_folha_pdf nucleo.test_tabela_pedidos`; bateria do painel.
- [ ] Prova só leitura com o banco de produção (`file:dados/dados.db?mode=ro`): `/pedidos` devolve as mesmas linhas de hoje enquanto a tabela da fila não existe.
- [ ] Commit.

## Depois do plano (só com pedido do Hugo)

Homologação → OK do Hugo → PR → deploy com `--units --timers expedicao-coletas-ler.timer` e restart do `painel-agentes` → conferir a primeira leitura do job (skill conferir-rodada) → primeira coleta real acompanhada, num pedido indicado pelo Hugo.
