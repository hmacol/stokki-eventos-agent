# Galpão F0 + F1: investigação na Stokki e caixas com serial — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dar à Maria Dolores um estoque controlado caixa a caixa:
- etiqueta com serial e fila de impressão;
- inventário de abertura e recebimento ativando caixas;
- baixa da expedição consumindo caixas;
- checagem diária caixas × saldo.

Em paralelo, levantar na Stokki (só leitura) o que o robô das fases seguintes vai precisar.

**Architecture:**
- A regra de negócio fica em módulos de dados puros, sem Flask (`painel_agentes/wms_caixas.py`, `painel_agentes/wms_fila_impressao.py`), no estilo de `wms.py` e `wms_pedidos.py`. O HTTP fica em `painel_agentes.py`.
- As caixas são uma camada sobre o estoque atual (D1). Toda mudança de estoque continua passando por `wms.registrar_movimento`, e a caixa só guarda situação, posição e histórico.
- A F2 (separação), a F3 (conferência), a F4 (volumes e robô) e a F5 terão planos próprios, depois do resultado da F0.

**Tech Stack:** Python 3.11, Flask (painel), SQLite (`dados/dados.db`), PIL + qrcode (etiquetas), `unittest`, PWA em HTML/JS puro (`templates/wms.html`).

**Spec:** `docs/superpowers/specs/2026-11-01-galpao-separacao-conferencia-etiquetagem-design.md`

## Global Constraints

- Python sempre `py -3.11`. Na VPS, `/opt/stokki-eventos/venv/bin/python` como `www-data`.
- `config.yaml` nunca é commitado nem sobrescrito, e nenhum valor dele vai para o chat.
- Piloto: `config.yaml → wms.embarcador_piloto_id` (padrão `48`), comparado com `wms_produtos.embarcador_id`.
- Serial da caixa: `C` + 7 dígitos (`C0000001`), sequencial e nunca reutilizado (C1).
- Código interno: `FLP` + 6 dígitos (`FLP000001`), só para produto sem SKU e sem EAN (C2, H7).
- QR da caixa: `FL|CÓDIGO|LOTE|AAAA-MM-DD|SERIAL`. A etiqueta antiga, com 3 campos, continua sendo lida (H6).
- Etiqueta de caixa: 10×5 cm, calibração `wms.etiqueta_*` do config, sem mudar a calibração (C4).
- Máximo de 200 caixas por pedido de etiquetas, o mesmo teto de `copias` hoje.
- `dados.db` é compartilhado: transação curta, `BEGIN IMMEDIATE` e commit por caixa ou por bipe. Nunca uma transação que atravesse um laço de N caixas, exceto a criação das etiquetas (no máximo 200 linhas).
- Cada arquivo segue o próprio estilo: comentários sem acento em `wms_pedidos.py` e `wms_etiqueta_produto.py`; com acento em `wms.py` e `painel_agentes.py`.
- Testes de módulo de dados rodam da raiz (`py -3.11 -m unittest painel_agentes.test_X -v`). Testes que importam `painel_agentes.py` rodam de dentro de `painel_agentes/` (`py -3.11 -m unittest test_X -v`).
- Painel local de teste só na porta 8099. Nunca encerrar processo que já existia.
- Nunca fazer login na Stokki pela máquina local. A Stokki só é lida na VPS, com as funções de `stokki/`.
- Trabalhar num ramo próprio (`galpao-caixas`, num worktree). `git add` só dos arquivos da tarefa. Commit e push só com autorização do Hugo. Deploy só quando o Hugo disser "deploy".
- Arquivo, rotina ou tela nova entra no `MAPA_DO_SISTEMA.txt` no mesmo commit.

## Review Focus

1. **Ativação repetida** (resposta perdida, fila offline reenviando): a mesma caixa ativada duas vezes não pode somar duas vezes no saldo. Teste: Task 4, `test_ativar_duas_vezes_soma_uma`.
2. **Caixa já ativada bipada em outra posição:** erro claro ("use Mover"), nunca mudar de lugar em silêncio. Teste: Task 4, `test_ativar_em_outra_posicao_recusa`.
3. **Produto da Maria Dolores entrando pelo caminho antigo** (`/api/wms/movimentos`): recusado com mensagem que mande usar as caixas. Senão, o saldo cresce sem caixa. Teste: Task 8, `test_movimento_avulso_de_produto_do_piloto_recusa`.
4. **Baixa que não fecha caixa inteira** (reserva de 4 UN, caixa de 6): a caixa vira ABERTA com 2 e a checagem continua batendo. Teste: Task 5, `test_baixa_parcial_deixa_caixa_aberta_e_checagem_zerada`.
5. **Etiqueta antiga (3 campos) bipada no Inventário:** a ativação é recusada ("etiqueta sem serial, peça etiqueta nova"), mas o Consultar continua lendo. Teste: Task 3, `test_qr_sem_serial_nao_vira_caixa`.

---

### Task 0: F0, investigação na Stokki (só leitura, sem código)

Pode correr em paralelo às Tasks 1 a 10, porque nenhuma depende dela. **É obrigatória antes do plano da F2.**

**Files:**
- Create: `docs/superpowers/specs/2026-11-01-galpao-f0-stokki-notas.md`

**Pré-requisito:** a leitura na VPS foi bloqueada pelo controle de permissões em 01/11. Peça ao Hugo para liberar a leitura de produção antes de começar. Nada nesta task escreve na Stokki, no banco ou nos arquivos da VPS.

- [ ] **Step 1: Volume da Maria Dolores.** No `dados.db` da VPS, em modo só leitura (`file:...?mode=ro`), levante dos últimos 30 dias:
  - pedidos por dia (média e máximo);
  - linhas e UN por pedido;
  - SKUs distintos por dia;
  - pedidos dela por rota;
  - estado atual das tabelas `wms_*` dela (saldos, movimentos, recebimentos).

  Registre cada número com a query usada.
- [ ] **Step 2: Itens no status Open.** Com `stokki/pedidos.py`, na VPS e com a sessão dela (skill `operar-stokki`), leia 3 pedidos dela em Open e 3 em Separating. Responda: a tabela Produto|EAN|Quantidade existe em Open? Se não existe, que outra fonte tem os itens (por exemplo o XML da NF)?
- [ ] **Step 3: Como a Stokki registra cada etapa.** Pela página `show` e pelos formulários da estação de separação e de conferência, levante a URL, o método, os campos e o CSRF de:
  - (a) início da separação;
  - (b) itens separados / EANs lidos;
  - (c) conferência (usuário, início, fim);
  - (d) "quantidade de volumes informada na conferência";
  - (e) a passagem para Waiting for Carrier.

  Diga também se dá para informar o operador real ou se a Stokki grava o usuário logado. Não envie nenhum formulário.
- [ ] **Step 4: Dados para a etiqueta de volume e para a onda.** Descubra de onde vêm a data de entrega, o destinatário (nome, bairro, cidade) e o número da NF de um pedido. Pode ser campo já lido pelo sync, pelo núcleo ou pelo `show`.
- [ ] **Step 5: Escrever a nota.** Em `2026-11-01-galpao-f0-stokki-notas.md`, registre as respostas dos Steps 1 a 4 com evidência e um roteiro proposto para o robô R1/R2. Marque o que não foi possível verificar.
- [ ] **Step 6: Parar e mostrar ao Hugo.** O plano da F2 sai daqui.

---

### Task 1: fila de impressão (dados)

**Files:**
- Create: `painel_agentes/wms_fila_impressao.py`
- Test: `painel_agentes/test_wms_fila_impressao.py`

**Interfaces:**
- Produces:
  - `TIPOS = ("CAIXA",)`, `TAMANHOS = ("10x5", "10x15")`, `SITUACOES = ("PENDENTE", "IMPRESSO", "CANCELADO")`
  - `garantir_tabela(conn) -> None`: DDL da `wms_fila_impressao` (id, tipo, tamanho, titulo, itens_json, situacao, pedido_por, criado_em, impresso_em, impresso_por).
  - `enfileirar(conn, *, tipo: str, tamanho: str, itens: list[str], titulo: str, pedido_por: str = "") -> int`. **Não faz commit**: roda dentro da transação de quem chama.
  - `obter(conn, impressao_id: int) -> dict | None`, com `itens` já como lista.
  - `listar(conn, situacao: str = "PENDENTE", limite: int = 50) -> list[dict]`, das mais antigas para as mais novas.
  - `marcar_impresso(conn, impressao_id: int, por: str) -> dict`. Faz commit e é idempotente: marcar de novo não muda o `impresso_em`.

- [ ] **Step 1: Write the failing tests**

```python
class TestFila(unittest.TestCase):  # banco temporario, garantir_tabela no setUp
    def test_enfileirar_e_listar_pendente(self):
        i = fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x5", itens=["C0000001", "C0000002"],
                            titulo="COXINHA · 2 caixas", pedido_por="Kleber"); self.conn.commit()
        [t] = fila.listar(self.conn)
        self.assertEqual((t["id"], t["itens"], t["situacao"]), (i, ["C0000001", "C0000002"], "PENDENTE"))
    def test_tipo_ou_tamanho_invalido_recusa(self):
        with self.assertRaises(wms.ErroWMS): fila.enfileirar(self.conn, tipo="X", tamanho="10x5", itens=["C1"], titulo="t")
        with self.assertRaises(wms.ErroWMS): fila.enfileirar(self.conn, tipo="CAIXA", tamanho="9x9", itens=["C1"], titulo="t")
    def test_sem_itens_recusa(self):
        with self.assertRaises(wms.ErroWMS): fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x5", itens=[], titulo="t")
    def test_marcar_impresso_tira_da_fila_e_e_idempotente(self):
        i = fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x5", itens=["C0000001"], titulo="t"); self.conn.commit()
        a = fila.marcar_impresso(self.conn, i, "Ana"); b = fila.marcar_impresso(self.conn, i, "Bia")
        self.assertEqual(fila.listar(self.conn), [])
        self.assertEqual((b["impresso_por"], b["impresso_em"]), ("Ana", a["impresso_em"]))
```

- [ ] **Step 2: Rodar e ver falhar.** `py -3.11 -m unittest painel_agentes.test_wms_fila_impressao -v`. Esperado: `ModuleNotFoundError`.
- [ ] **Step 3: Implementar `wms_fila_impressao.py`** com as interfaces acima, sem acento nos comentários. Erros são `wms.ErroWMS`.
- [ ] **Step 4: Rodar e ver passar.** Mesmo comando. Esperado: OK.
- [ ] **Step 5: Commit** (com autorização): `git add painel_agentes/wms_fila_impressao.py painel_agentes/test_wms_fila_impressao.py`; mensagem `Galpao: fila de impressao de etiquetas (dados)`.

---

### Task 2: caixas e código interno (dados)

**Files:**
- Modify: `painel_agentes/wms.py`. Em `conectar` (:164), adicionar a migração aditiva da coluna `wms_produtos.codigo_interno TEXT` e o índice único parcial `WHERE codigo_interno IS NOT NULL`.
- Create: `painel_agentes/wms_caixas.py`
- Test: `painel_agentes/test_wms_caixas.py`

**Interfaces:**
- Consumes: `wms_fila_impressao.enfileirar`, `wms_fila_impressao.garantir_tabela`, `wms_pedidos.conectar`, `wms._validar_validade`, `wms.obter_produto`.
- Produces:
  - `SITUACOES_CAIXA = ("IMPRESSA", "EM_ESTOQUE", "ABERTA", "EXPEDIDA", "CANCELADA")`
  - `conectar(caminho: Path | None = None) -> sqlite3.Connection`: chama `wms_pedidos.conectar` e cria `wms_caixas`, `wms_caixa_eventos`, `wms_contadores` e a fila.
  - `formatar_serial(n: int) -> str`
  - `reservar_numeros(conn, contador: str, quantidade: int) -> int`: devolve o primeiro número. Roda dentro da transação de quem chama.
  - `garantir_codigo_interno(conn, produto_id: int) -> str`: devolve o código interno existente ou cria um. Faz commit.
  - `criar_caixas(conn, *, produto_id: int, lote: str, validade: str | None, qtd_un: float | None, quantidade: int, operador: dict | None = None) -> dict`: devolve `{"seriais": [...], "impressao_id": int}`.
  - `obter_caixa(conn, serial: str) -> dict | None`: inclui a chave `produto`.
  - `produto_usa_caixas(produto: dict, piloto_id: int) -> bool`: `produto["embarcador_id"] == piloto_id`.
- Tabelas: `wms_caixas` (serial PK, produto_id, lote, validade, qtd_un, posicao, situacao, serial_origem, impressao_id, criado_em, atualizado_em) e `wms_caixa_eventos` (id, uuid UNIQUE, serial, de, para, posicao, movimento_uuid, operador_id, operador_nome, observacao, criado_em). Índice em `wms_caixa_eventos(movimento_uuid)`.

- [ ] **Step 1: Write the failing tests** (produto 1 = Maria Dolores, `embarcador_id=48`, `qtd_por_caixa=6`, `controla_validade=1`; produto 2 sem SKU e sem EAN)

```python
def test_criar_tres_caixas_gera_seriais_em_sequencia_e_um_trabalho_na_fila(self):
    r = cx.criar_caixas(self.conn, produto_id=1, lote="l1", validade="15/12/2026", qtd_un=None, quantidade=3)
    self.assertEqual(r["seriais"], ["C0000001", "C0000002", "C0000003"])
    c = cx.obter_caixa(self.conn, "C0000002")
    self.assertEqual((c["situacao"], c["lote"], c["validade"], c["qtd_un"]), ("IMPRESSA", "L1", "2026-12-15", 6))
    self.assertEqual(fila.obter(self.conn, r["impressao_id"])["itens"], r["seriais"])
def test_serial_continua_de_onde_parou(self):
    cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=None, quantidade=2)
    r = cx.criar_caixas(self.conn, produto_id=1, lote="L2", validade="2026-12-20", qtd_un=None, quantidade=1)
    self.assertEqual(r["seriais"], ["C0000003"])
def test_quantidade_fora_de_1_a_200_recusa(self): ...  # 0 e 201 -> ErroWMS
def test_validade_obrigatoria_quando_produto_controla(self): ...  # validade=None -> ErroWMS
def test_codigo_interno_e_unico_e_estavel(self):
    a = cx.garantir_codigo_interno(self.conn, 2); b = cx.garantir_codigo_interno(self.conn, 2)
    self.assertEqual((a, b), ("FLP000001", "FLP000001"))
def test_criar_caixas_nao_cria_nada_se_falhar(self): ...  # produto inexistente -> ErroWMS e 0 linhas em wms_caixas e na fila
```

- [ ] **Step 2: Rodar e ver falhar.** `py -3.11 -m unittest painel_agentes.test_wms_caixas -v`
- [ ] **Step 3: Implementar.**
  - `criar_caixas` roda tudo num só `BEGIN IMMEDIATE ... commit`: contador, N linhas, N eventos `None → IMPRESSA` e o trabalho da fila (título `"<descrição> · <N> caixas"`).
  - `qtd_un=None` usa `qtd_por_caixa` (ou 1 quando for nulo).
  - O lote é normalizado como em `registrar_movimento`.
- [ ] **Step 4: Rodar e ver passar**, e rodar também `painel_agentes.test_wms_pedidos` e `painel_agentes.test_wms_etiqueta_produto` (a migração não pode quebrá-los).
- [ ] **Step 5: Commit**: `Galpao: caixas com serial e codigo interno de produto (dados)`.

---

### Task 3: etiqueta nova e leitura de código

**Files:**
- Modify: `painel_agentes/wms_etiqueta_produto.py` (`conteudo_qr` :19, `identificador_do_produto` :24, `parse_qr`, `desenhar_etiqueta_produto`, `gerar_etiquetas_produto_pdf` :110-116)
- Modify: `painel_agentes/wms.py` (`buscar_por_codigo` :543, `ler_codigo` :817)
- Test: `painel_agentes/test_wms_etiqueta_produto.py`, `painel_agentes/test_wms_caixas.py`

**Interfaces:**
- Consumes: `wms_caixas.obter_caixa`, `wms_caixas.garantir_codigo_interno`.
- Produces:
  - `conteudo_qr(sku: str, lote: str, validade: str | None, serial: str = "") -> str`.
  - `parse_qr(texto) -> dict | None`, agora com a chave `serial` (vazia na etiqueta antiga).
  - `identificador_do_produto(produto: dict) -> str`: SKU, senão EAN, senão `codigo_interno`.
  - `gerar_pdf_caixas(conn, seriais: list[str], orientacao: str = "paisagem", margem_mm: float = 0, desloc_x_mm: float = 0, desloc_y_mm: float = 0) -> bytes`: uma página por serial.
  - `wms.ler_codigo` ganha `{"tipo": "caixa", "caixa": {...}}` e `{"tipo": "caixa_inexistente", "serial": ...}`.
  - `wms.buscar_por_codigo("FLP000001")` acha pelo `codigo_interno`, com `lido_como="INTERNO"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_qr_com_serial(self):
    self.assertEqual(etq.conteudo_qr("72400017", "L-A", "2026-10-15", "C0000001"), "FL|72400017|L-A|2026-10-15|C0000001")
    self.assertEqual(etq.parse_qr("FL|72400017|L-A|2026-10-15|C0000001")["serial"], "C0000001")
def test_parse_da_etiqueta_antiga_tem_serial_vazio(self):  # substitui test_parse_devolve_os_tres_campos
    self.assertEqual(etq.parse_qr("FL|72400017|L-A|2026-10-15"),
                     {"sku": "72400017", "lote": "L-A", "validade": "2026-10-15", "serial": ""})
def test_produto_sem_sku_e_sem_ean_ganha_codigo_interno_em_vez_de_recusar(self):
    # substitui o teste que esperava ErroWMS; o PDF sai e o QR usa FLP000001
def test_pdf_de_caixas_tem_uma_pagina_por_serial(self):  # 3 seriais -> 3 paginas (pypdf ou contar b"/Type /Page")
def test_pdf_recusa_serial_cancelado_ou_inexistente(self): ...
def test_ler_codigo_qr_com_serial_vira_caixa(self):
    r = wms.ler_codigo(self.conn, "FL|72400017|L1|2026-12-15|C0000001")
    self.assertEqual((r["tipo"], r["caixa"]["serial"]), ("caixa", "C0000001"))
def test_qr_sem_serial_nao_vira_caixa(self):  # Review Focus 5
    self.assertEqual(wms.ler_codigo(self.conn, "FL|72400017|L1|2026-12-15")["tipo"], "produto_lote")
def test_buscar_pelo_codigo_interno(self): ...  # lido_como == "INTERNO"
```

- [ ] **Step 2: Rodar e ver falhar.** `py -3.11 -m unittest painel_agentes.test_wms_etiqueta_produto painel_agentes.test_wms_caixas -v`
- [ ] **Step 3: Implementar.**
  - Layout aprovado (H8, mockup `etiquetas_exemplo.png`), de cima para baixo:
    - descrição (até 50 px, negrito condensado);
    - `SKU <identificador>` (58 px, negrito condensado);
    - `LOTE` e `VAL` (até 72 px, condensado);
    - faixa preta de 62 px com `CAIXA <serial>` em branco (46 px), só quando houver serial;
    - rodapé `<embarcador cortado em 34> · Freshlog` (28 px).
  - A etiqueta sem serial mantém tudo menos a faixa.
  - A rotação e a margem usam o mesmo caminho de `gerar_etiquetas_produto_pdf`.
- [ ] **Step 4: Rodar e ver passar.** Gerar um PDF de 1 caixa num script descartável no scratchpad e abrir para conferir a olho contra o mockup.
- [ ] **Step 5: Commit**: `Galpao: etiqueta de caixa com serial e SKU em destaque; leitura do QR com serial`.

---

### Task 4: ativar, mover e cancelar caixa

**Files:**
- Modify: `painel_agentes/wms_caixas.py`
- Test: `painel_agentes/test_wms_caixas.py`

**Interfaces:**
- Consumes: `wms.registrar_movimento`, `wms_pedidos.contabilizar_enderecamento`.
- Produces:
  - `ativar_caixa(conn, serial: str, posicao: str, *, operador: dict | None = None, recebimento_id: int | None = None, item_id: int | None = None, dispositivo: str = "", criado_em: str | None = None) -> dict` → `{"caixa": dict, "duplicado": bool, "movimento_uuid": str}`
  - `mover_caixa(conn, serial: str, destino: str, *, uuid: str, operador: dict | None = None) -> dict`
  - `cancelar_caixa(conn, serial: str, motivo: str, *, operador: dict | None = None) -> dict`

**Regras:**
- **Ativação:**
  - O uuid do movimento é `cx-<serial>-ativar`.
  - Sem recebimento, gera AJUSTE para mais (só destino), com `observacao="Inventario de abertura - caixa <serial>"`. Nunca INVENTARIO, porque ele grava a contagem absoluta da posição.
  - Com recebimento, gera ENTRADA e depois `contabilizar_enderecamento(..., qtd_un, uuid=cx-<serial>-ativar)`. O produto do item precisa ser o produto da caixa.
  - Ordem: primeiro o movimento (que faz commit), depois UPDATE da caixa e o evento, com commit. Uma nova chamada depois de uma queda no meio completa o que faltou sem somar de novo.
  - Caixa EM_ESTOQUE ou ABERTA na mesma posição → `duplicado=True`. Em outra posição → `ErroWMS("Caixa <serial> ja esta em <pos>. Use Mover.")`. EXPEDIDA ou CANCELADA → `ErroWMS`.
- **Mover:** TRANSFERENCIA com o uuid do aparelho. Só para EM_ESTOQUE ou ABERTA.
- **Cancelar:**
  - IMPRESSA → CANCELADA, sem movimento.
  - EM_ESTOQUE ou ABERTA → AJUSTE para menos de `qtd_un` com o motivo, e depois CANCELADA.
  - O motivo é obrigatório.

- [ ] **Step 1: Write the failing tests**

```python
def test_ativar_no_inventario_soma_no_saldo_e_vira_em_estoque(self):
    s = cx.criar_caixas(..., quantidade=2)["seriais"]
    cx.ativar_caixa(self.conn, s[0], "C9-E1-N1"); cx.ativar_caixa(self.conn, s[1], "C9-E1-N1")
    self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N1", 1, "L1", "2026-12-15"), 12)  # AJUSTE soma; INVENTARIO daria 6
def test_ativar_duas_vezes_soma_uma(self):  # Review Focus 1
    cx.ativar_caixa(self.conn, s[0], "C9-E1-N1"); r = cx.ativar_caixa(self.conn, s[0], "C9-E1-N1")
    self.assertTrue(r["duplicado"]); self.assertEqual(saldo, 6)
def test_ativar_em_outra_posicao_recusa(self):  # Review Focus 2
    cx.ativar_caixa(self.conn, s[0], "C9-E1-N1")
    with self.assertRaisesRegex(wms.ErroWMS, "Use Mover"): cx.ativar_caixa(self.conn, s[0], "C9-E1-N2")
def test_ativar_pelo_recebimento_anda_a_barra_uma_vez(self):  # contabilizar com o mesmo uuid; chamar 2x -> qtd_enderecada 6
def test_ativar_recebimento_de_outro_produto_recusa(self): ...
def test_queda_entre_movimento_e_caixa_completa_sem_somar(self):
    # registra o movimento cx-<serial>-ativar na mao (simula a queda) e chama ativar: caixa EM_ESTOQUE, saldo 6
def test_mover_caixa_leva_o_saldo_junto(self): ...
def test_cancelar_impressa_sem_movimento_e_em_estoque_com_ajuste(self): ...
```

- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar** conforme as regras acima.
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit**: `Galpao: ativar, mover e cancelar caixa (inventario por AJUSTE, recebimento por ENTRADA)`.

---

### Task 5: baixa consome caixas, checagem e zerar saldo antigo

**Files:**
- Modify: `painel_agentes/wms_caixas.py`
- Modify: `painel_agentes/wms_pedidos.py`, em `baixar_por_expedicao` (:480). Depois do UPDATE da reserva e antes do `conn.commit()` do laço, `import wms_caixas` lazy e chamar `consumir_caixas`.
- Test: `painel_agentes/test_wms_caixas.py`

**Interfaces:**
- Produces:
  - `consumir_caixas(conn, *, posicao: str, produto_id: int, lote: str, validade: str, qtd_un: float, movimento_uuid: str) -> dict` → `{"expedidas": [serial], "aberta": serial | None, "faltou_un": float, "ja_consumido": bool}`. **Não faz commit.**
    - Não faz nada se já houver evento com aquele `movimento_uuid`.
    - Ordem de consumo: ABERTA primeiro, depois EM_ESTOQUE por serial crescente.
    - Caixa consumida inteira vira EXPEDIDA. A parcial fica ABERTA com o `qtd_un` reduzido.
  - `divergencias(conn, embarcador_id: int) -> list[dict]` → `[{"posicao", "produto_id", "sku", "lote", "validade", "saldo", "caixas"}]`. Compara `wms_saldos` com a soma de `qtd_un` das caixas EM_ESTOQUE e ABERTA, só para produtos daquele embarcador, com tolerância de 0,001. Aparecem os dois lados: saldo sem caixa e caixa sem saldo.
  - `plano_zerar_saldo(conn, embarcador_id: int) -> list[dict]` e `zerar_saldo(conn, embarcador_id: int, motivo: str, operador: dict | None = None) -> int`. Uma AJUSTE por linha de saldo: para menos quando o saldo é maior que zero, para mais quando é menor (saldo negativo de baixa antiga). O uuid é `zerar-<emb>-<posicao>-<produto>-<lote>-<validade>-<AAAAMMDD>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_baixa_inteira_marca_caixa_expedida(self): ...  # reserva 6 UN, 1 caixa de 6 -> EXPEDIDA; divergencias == []
def test_baixa_parcial_deixa_caixa_aberta_e_checagem_zerada(self):  # Review Focus 4
    # 1 caixa de 6, reserva de 4 -> baixar_por_expedicao -> caixa ABERTA qtd_un 2; saldo 2; divergencias == []
def test_baixa_rodada_duas_vezes_nao_consome_duas_vezes(self): ...
def test_produto_sem_caixa_nao_quebra_a_baixa(self): ...  # outro embarcador, baixa igual a hoje, faltou_un ignorado
def test_divergencia_aparece_dos_dois_lados(self): ...  # saldo sem caixa e caixa sem saldo
def test_zerar_saldo_positivo_e_negativo(self): ...  # depois: plano_zerar_saldo == [] e wms_saldos sem linha do embarcador
```

- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.** No `baixar_por_expedicao`, o retorno de `consumir_caixas` não muda o estado do pedido: falta de caixa aparece na checagem, não na baixa.
- [ ] **Step 4: Rodar e ver passar**, mais a suíte inteira `painel_agentes.test_wms_pedidos` (67 testes) e `test_sincronizar_pedidos_wms` (da raiz). Todos verdes.
- [ ] **Step 5: Commit**: `Galpao: baixa da expedicao consome caixas; checagem caixas x saldo; zerar saldo antigo`.

---

### Task 6: tela da fila de impressão (computador da equipe)

**Files:**
- Modify: `painel_agentes/painel_agentes.py`, rotas novas perto de `/wms/etiqueta-produto.pdf` (:4737)
- Create: `painel_agentes/templates/fila_etiquetas.html`
- Modify: `painel_agentes/acesso_telas.py`, entrada nova depois de `wms_estoque` (:69)
- Modify: o menu do painel, como foi feito para a tela `/retornos`
- Test: `painel_agentes/test_fila_etiquetas_tela.py`

**Interfaces:**
- Consumes: `wms_fila_impressao.listar/obter/marcar_impresso`, `wms_etiqueta_produto.gerar_pdf_caixas`, `wms_caixas.conectar`.
- Produces (níveis `("total", "operador", "expedicao")`):
  - `GET /etiquetas/fila` → `fila_etiquetas.html`
  - `GET /api/etiquetas/fila?situacao=PENDENTE` → `{"trabalhos": [...]}`
  - `GET /etiquetas/fila/<id>.pdf` → PDF com a calibração `wms.etiqueta_*` (a mesma leitura de config de `wms_etiqueta_produto_pdf`)
  - `POST /api/etiquetas/fila/<id>/impresso` → `{"trabalho": {...}}`
  - TELAS: `("fila_etiquetas", "Fila de etiquetas", "Operar", [r"^/etiquetas/fila", r"^/api/etiquetas/fila"])`

- [ ] **Step 1: Write the failing tests** (padrão de `test_retornos_tela.py`: carregar `painel_agentes.py` por `importlib`, `mock.patch.object(wms, "DB_PATH", ...)`, `session_transaction`)

```python
def test_galpao_nao_ve_a_fila(self): self.logar("galpao"); self.assertIn(self.get("/etiquetas/fila").status_code, (302, 403))
def test_expedicao_lista_e_baixa_o_pdf(self):
    self.logar("expedicao"); j = self.get("/api/etiquetas/fila").get_json()
    self.assertEqual(len(j["trabalhos"]), 1)
    r = self.get(f"/etiquetas/fila/{j['trabalhos'][0]['id']}.pdf"); self.assertEqual(r.mimetype, "application/pdf")
def test_marcar_impresso_tira_da_lista(self): ...
def test_tela_fila_esta_em_acesso_telas(self): ...  # chave "fila_etiquetas" casa /etiquetas/fila e nao casa /wms
```

- [ ] **Step 2: Rodar e ver falhar.** De dentro de `painel_agentes/`: `py -3.11 -m unittest test_fila_etiquetas_tela -v`
- [ ] **Step 3: Implementar** as rotas e a tela: lista de pendentes com título, quantidade, quem pediu e quando; botões "Abrir PDF" (nova aba) e "Marcar impresso"; atualização a cada 30 s. A tela segue o `base.html` das outras telas do escritório.
- [ ] **Step 4: Rodar e ver passar**, mais `test_acesso_telas`.
- [ ] **Step 5: Commit**: `Galpao: tela da fila de etiquetas pro computador da impressao`.

---

### Task 7: APIs de caixa para a PWA e trava do caminho antigo

**Files:**
- Modify: `painel_agentes/painel_agentes.py`, junto das rotas `/api/wms/*` (:4233-4734)
- Test: `painel_agentes/test_wms_caixas_tela.py`

**Interfaces:**
- Consumes: as interfaces de `wms_caixas` das Tasks 2, 4 e 5.
- Produces (níveis `_NIVEIS_WMS`; toda escrita exige operador por `_wms_exige_operador()`):
  - `_wms_piloto_id() -> int`: `config wms.embarcador_piloto_id`, padrão 48.
  - `POST /api/wms/caixas` `{produto_id, lote, validade, qtd_un?, quantidade}` → `{"seriais", "impressao_id"}`
  - `GET /api/wms/caixas/<serial>` → `{"caixa"}` (404 se não existir)
  - `POST /api/wms/caixas/<serial>/ativar` `{posicao, recebimento_id?, item_id?, criado_em?}` → `{"caixa", "duplicado"}`
  - `POST /api/wms/caixas/<serial>/mover` `{destino, uuid}` e `POST /api/wms/caixas/<serial>/cancelar` `{motivo}`
  - `GET /api/wms/recebimentos/<id>`: cada item ganha `usa_caixas: bool`.
  - `_wms_bootstrap` ganha `"piloto_id"`.
  - **Trava:** `POST /api/wms/movimentos` e `/api/wms/movimentos/lote` recusam com 400, para produto em que `produto_usa_caixas` é verdadeiro, com a mensagem `"Produto controlado por caixa: use Inventario, Receber ou Mover caixa."`.

- [ ] **Step 1: Write the failing tests**

```python
def test_movimento_avulso_de_produto_do_piloto_recusa(self):  # Review Focus 3
    r = self.post("/api/wms/movimentos", {"tipo": "ENTRADA", "produto_id": 1, "quantidade": 6, "destino": "C9-E1-N1", ...})
    self.assertEqual(r.status_code, 400); self.assertIn("controlado por caixa", r.get_json()["erro"])
def test_movimento_de_outro_embarcador_continua_igual(self): ...
def test_fluxo_criar_e_ativar(self):  # POST /caixas -> 2 seriais; ativar os 2 -> saldo 12
def test_sem_operador_da_409(self): ...
def test_recebimento_marca_usa_caixas(self): ...
```

- [ ] **Step 2: Rodar e ver falhar.** De dentro de `painel_agentes/`: `py -3.11 -m unittest test_wms_caixas_tela -v`
- [ ] **Step 3: Implementar.** As rotas usam `wms_caixas.conectar()`, e `ErroWMS` vira 400 por `_wms_json_erro`.
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit**: `Galpao: APIs de caixa no /wms e trava do movimento avulso pro piloto`.

---

### Task 8: PWA, com Inventário, Receber por caixa e caixa no Consultar

**Files:**
- Modify: `painel_agentes/templates/wms.html`: aba nova perto de :167-172; `TELAS.recebimento` (:680) e `confirmarRecebimentoItem` (:777); `lerCodigo` (:924); fila offline (:1011-1031)
- Test: `painel_agentes/test_wms_telas.py`

**Interfaces:**
- Consumes: as APIs da Task 7.

**Comportamento:**
- **Aba "Inventário"** (`data-aba="inventario"`), em 3 passos numa tela só:
  1. bipar a posição (`POS:`);
  2. bipar ou buscar o produto, informar lote, validade, UN por caixa (pré-preenchido com `qtd_por_caixa`) e nº de caixas, e tocar "Pedir etiquetas" (POST `/caixas`), que mostra "Etiquetas na fila #<id>, retire no computador da impressão";
  3. "Bipe cada caixa", com o contador `ativadas / total` daquele pedido de etiquetas. Bipar uma caixa de outro pedido de etiquetas também ativa.
- **Receber:** quando o item tem `usa_caixas`, os botões "Imprimir etiqueta" e "Registrar entrada" são substituídos por "Pedir etiquetas" (lote, validade e nº de caixas) e por um campo "posição + bipe das caixas". Cada caixa chama `/caixas/<serial>/ativar` com `recebimento_id` e `item_id`.
- **Consultar:** `lerCodigo` com `tipo === 'caixa'` mostra um cartão com serial, situação, posição, produto, lote e validade, mais os botões "Mover" (bipar o destino) e "Cancelar" (motivo). `caixa_inexistente` vira toast. QR antigo continua como hoje.
- **Fila offline:** a ativação sem rede entra em `wms_fila` como `{tipo: 'ativar_caixa', serial, posicao, recebimento_id, item_id, criado_em}`. `reenviarFila` manda as ativações para `/caixas/<serial>/ativar`, uma por vez, e os movimentos como hoje.

- [ ] **Step 1: Write the failing tests** (guardas de ligação, no padrão do arquivo)

```python
class TestCaixasNaTela(unittest.TestCase):
    def test_aba_inventario_existe(self): self.assertIn('data-aba="inventario"', _WMS)
    def test_ativacao_chama_a_rota_da_caixa(self): self.assertIn("'/caixas/' + ", _WMS); self.assertIn("/ativar'", _WMS)
    def test_receber_respeita_usa_caixas(self): self.assertIn("it.usa_caixas", _WMS)
    def test_fila_offline_reenvia_ativacao(self): self.assertIn("ativar_caixa", _WMS)
```

- [ ] **Step 2: Rodar e ver falhar.** `py -3.11 -m unittest painel_agentes.test_wms_telas -v`
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar**, e testar na mão com o painel local **na porta 8099**, num banco de teste (Playwright, ver a memória `feedback_testar_painel_playwright_local`):
  - inventário de 3 caixas: pedir, ver na fila, ativar as 3, saldo 18;
  - repetir o bipe: sem soma;
  - bipar a caixa em outra posição: mensagem de "Mover";
  - recebimento de produto do piloto por caixas;
  - Consultar a caixa e movê-la.
- [ ] **Step 5: Commit**: `Galpao: aba Inventario, recebimento por caixa e caixa no Consultar (PWA)`.

---

### Task 9: checagem diária e ferramenta de zerar saldo

**Files:**
- Create: `checar_caixas_wms.py` (raiz, no estilo de `sincronizar_pedidos_wms.py`: UTF-8 no stdout, log, `--modo-teste`)
- Modify: `painel_agentes/wms_caixas.py`, com `main(argv)` e os subcomandos `divergencias --embarcador-id N` e `zerar-saldo --embarcador-id N --motivo "..." [--aplicar]` (sem `--aplicar`, só mostra o plano)
- Create: `infra/stokki-wms-checar-caixas.service` e `.timer` (copiar `stokki-wms-reservar-pedidos.*`; `OnCalendar=*-*-* 06:40:00`; `OnFailure=stokki-alerta-falha@%n.service`)
- Test: `test_checar_caixas_wms.py` (raiz)

**Interfaces:**
- Consumes: `wms_caixas.divergencias`, `email_utils.enviar_email(destinatarios, assunto, corpo_html, config_email)`.
- Produces: `checar(conn, config: dict, modo_teste: bool = False) -> dict` → `{"divergencias": int, "email": "enviado" | "teste" | "nada"}`. Destinatários: `config wms.alertas_email` (lista) ou, se faltar, `[config email.remetente]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_sem_divergencia_nao_manda_email(self): ...           # email == "nada", enviar_email nao chamado
def test_com_divergencia_manda_um_email_com_as_linhas(self): ...  # assunto com "1 divergencia"; corpo com posicao e SKU
def test_modo_teste_nao_manda(self): ...                       # email == "teste"
```

- [ ] **Step 2: Rodar e ver falhar.** `py -3.11 -m unittest test_checar_caixas_wms -v`
- [ ] **Step 3: Implementar** `checar_caixas_wms.py`, os subcomandos e os dois units.
- [ ] **Step 4: Rodar e ver passar.** `py -3.11 checar_caixas_wms.py --modo-teste`, num banco de teste, imprime as divergências e não manda e-mail.
- [ ] **Step 5: Commit**: `Galpao: checagem diaria caixas x saldo (timer 06h40) e CLI de zerar saldo`.

---

### Task 10: mapa, spec, suíte inteira e revisão

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt`:
  - linha 109 (WMS) passa a citar `wms_caixas.py` e `wms_fila_impressao.py`;
  - o bloco de timers (:374-376) ganha `06h40 wms-checar-caixas checar_caixas_wms.py`;
  - as rotas do painel (:1074) ganham `/etiquetas/fila`;
  - as tabelas (:1392) ganham `wms_caixas`, `wms_caixa_eventos`, `wms_contadores`, `wms_fila_impressao` e `wms_produtos.codigo_interno`.
- A spec já foi corrigida em 01/11 (ativação por AJUSTE/ENTRADA; baixa consumindo caixas na F1). Só atualize se a implementação mudar alguma decisão.

- [ ] **Step 1: `py -3.11 -m py_compile`** em todos os `.py` alterados.
- [ ] **Step 2: Suítes.**
  - Da raiz: `py -3.11 -m unittest painel_agentes.test_wms_pedidos painel_agentes.test_wms_etiqueta_produto painel_agentes.test_wms_faltas_recebimento painel_agentes.test_wms_telas painel_agentes.test_wms_caixas painel_agentes.test_wms_fila_impressao test_sincronizar_pedidos_wms test_sincronizar_recebimentos_wms test_checar_caixas_wms -v`
  - De dentro de `painel_agentes/`: `py -3.11 -m unittest test_fila_etiquetas_tela test_wms_caixas_tela test_acesso_telas -v`
  - Esperado: tudo OK.
- [ ] **Step 3: Revisão do ramo inteiro** (superpowers:requesting-code-review). Corrigir o que for ALTO ou MÉDIO.
- [ ] **Step 4: Commit** do mapa: `Galpao: mapa atualizado com as caixas (F1)`.
- [ ] **Step 5: Parar.** Deploy só quando o Hugo pedir (skill `deploy-vps`). Na VPS:
  - reiniciar `painel-agentes`;
  - instalar e ligar o timer `stokki-wms-checar-caixas`;
  - provar com `curl` na 8071 e `checar_caixas_wms.py --modo-teste` como `www-data`.
- [ ] **Step 6: Roteiro do 1º inventário**, executado com o Hugo e a equipe, não pelo agente sozinho:
  1. `wms_caixas.py zerar-saldo --embarcador-id 48 --motivo "..."` sem `--aplicar` e mostrar o plano;
  2. com o OK do Hugo, rodar com `--aplicar`;
  3. mutirão numa janela sem chegada nem saída da Maria Dolores;
  4. no dia seguinte, a checagem das 06h40 tem que dar 0 divergência. Essa é a prova da F1.
