# WMS fase 2 — estoque proprio com reserva por pedido — Plano de implementacao

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fechar o ciclo de estoque de um embarcador (Maria Dolores): mercadoria entra com lote e validade, ganha etiqueta com QR, cada pedido lancado na Stokki reserva o que vai consumir (FEFO) e a expedicao converte a reserva em baixa.

**Architecture:** Um modulo de dados novo (`painel_agentes/wms_pedidos.py`, sem Flask, no mesmo estilo do `wms.py`) guarda o espelho do pedido, os itens e as reservas. Reserva **nunca** altera `wms_saldos`: o disponivel e derivado (`saldo - reservas ativas`). A baixa reusa `wms.registrar_movimento(tipo="SAIDA")`, que ja e idempotente por uuid. Duas rotinas de lote com timer systemd alimentam recebimento e reserva.

**Tech Stack:** Python 3.11, SQLite (`dados/dados.db`), requests + BeautifulSoup (Stokki), Flask/waitress (painel), Pillow + qrcode (etiqueta), unittest.

**Spec:** `docs/superpowers/specs/2026-09-21-wms-estoque-reserva-pedidos-design.md`

## Global Constraints

- Python local: **sempre `py -3.11`**. Na VPS: **`/opt/stokki-eventos/venv/bin/python`**, rodando como `sudo -u www-data`.
- Idioma: portugues. Comentarios e logs **sem acento** nos arquivos que ja seguem esse padrao (`expedir_pedidos.py`, `infra/*`); `wms.py` e o painel usam acento normalmente.
- `dados.db` e compartilhado com todos os agentes: **commit por pedido**, nunca um lote inteiro numa transacao. Leitura HTTP sempre FORA da transacao.
- Sessao da Stokki e unica: login concorrente derruba a outra sessao com 401, inclusive a do `agente-importacao-stokki`.
- Reserva **nunca bloqueia** a operacao (D8). Falta de saldo vira pendencia.
- Item que nao resolve produto/unidade **nao gera reserva** e **nao gera baixa**.
- Quantidade em `wms_*` e sempre **UN**; o pedido da Stokki vem em **embalagem**.
- Piloto: so o embarcador de `config.yaml → wms.embarcador_piloto` (`"MARIA DOLORES"`, prefixo).
- Commit e push **so quando o Hugo pedir** (CLAUDE.md). As tarefas trazem o comando pronto, mas nao rode sem o ok dele.
- Nao incluir no commit arquivos modificados por outras sessoes (`git status --short` antes).
- Nao mexer em `wms_saldos`, `wms_movimentos` nem em `recalcular_saldos()`.

---

### Task 1: Tabelas e conexao do modulo de pedidos

**Files:**
- Create: `painel_agentes/wms_pedidos.py`
- Test: `painel_agentes/test_wms_pedidos.py`

**Interfaces:**
- Consumes: `wms.conectar(caminho)`, `wms.agora()`, `wms.ErroWMS` de `painel_agentes/wms.py`.
- Produces: `conectar(caminho=None) -> sqlite3.Connection` (as tabelas `wms_pedidos`, `wms_pedido_itens`, `wms_reservas` existem depois da chamada), `ESTADOS_PEDIDO`, `ESTADOS_RESERVA`.

- [ ] **Step 1: Escrever o teste que falha**

Criar `painel_agentes/test_wms_pedidos.py`:

```python
# -*- coding: utf-8 -*-
"""
test_wms_pedidos.py

Testes do estoque com reserva por pedido (WMS fase 2, Hugo 21/09/2026).
Nenhum teste toca na Stokki nem no dados.db real: o banco vai pra uma
pasta temporaria.
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_wms_pedidos -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_pedidos  # noqa: E402


class BaseWMS(unittest.TestCase):
    """Banco temporario com uma area, posicoes e produtos de teste."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = wms_pedidos.conectar(self.db)
        wms.criar_area(self.conn, "C9", "CONTAINER", "Container 9")
        wms.gerar_posicoes(self.conn, "C9", estantes=2, niveis=2)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestTabelas(BaseWMS):
    def test_conectar_cria_as_tres_tabelas(self):
        nomes = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'wms_%'")}
        self.assertIn("wms_pedidos", nomes)
        self.assertIn("wms_pedido_itens", nomes)
        self.assertIn("wms_reservas", nomes)

    def test_conectar_tambem_cria_as_tabelas_da_fase_1(self):
        nomes = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'wms_%'")}
        self.assertIn("wms_saldos", nomes)
        self.assertIn("wms_movimentos", nomes)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar o teste e ver falhar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: FAIL com `ModuleNotFoundError: No module named 'wms_pedidos'`

- [ ] **Step 3: Escrever a implementacao minima**

Criar `painel_agentes/wms_pedidos.py`:

```python
# -*- coding: utf-8 -*-
"""
painel_agentes/wms_pedidos.py

Estoque com reserva por pedido (WMS fase 2, Hugo 21/09/2026).
Modulo de dados puro (sem Flask), no mesmo estilo do wms.py.

Regras que este modulo garante:
- Reserva NAO altera saldo fisico. wms_saldos continua sendo exatamente a
  soma de wms_movimentos; o disponivel e derivado
  (saldo - reservas ATIVAS).
- Item que nao resolve produto ou unidade nao vira reserva nem baixa:
  vira pendencia. Estoque que mente e pior que estoque nenhum.
- Falta de saldo nunca bloqueia a operacao: reserva o que da e registra a
  falta (decisao do Hugo, 21/09).
- A baixa reusa wms.registrar_movimento(tipo="SAIDA") com uuid
  deterministico, entao rodar duas vezes nao baixa duas vezes.
"""
import sqlite3
from pathlib import Path

import wms

ESTADOS_PEDIDO = ("PENDENTE", "RESERVADO", "PARCIAL", "BAIXADO", "CANCELADO")
ESTADOS_RESERVA = ("ATIVA", "CONSUMIDA", "CANCELADA")

_DDL = """
CREATE TABLE IF NOT EXISTS wms_pedidos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    id_stokki       INTEGER NOT NULL UNIQUE,
    codigo_ps       TEXT NOT NULL,
    embarcador      TEXT NOT NULL DEFAULT '',
    situacao        TEXT NOT NULL DEFAULT '',
    estado_reserva  TEXT NOT NULL DEFAULT 'PENDENTE',
    lido_em         TEXT NOT NULL,
    atualizado_em   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wms_pedidos_estado ON wms_pedidos(estado_reserva);
CREATE INDEX IF NOT EXISTS idx_wms_pedidos_ps ON wms_pedidos(codigo_ps);
CREATE TABLE IF NOT EXISTS wms_pedido_itens (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id        INTEGER NOT NULL REFERENCES wms_pedidos(id),
    linha            INTEGER NOT NULL,
    sku              TEXT NOT NULL DEFAULT '',
    ean_linha        TEXT NOT NULL DEFAULT '',
    descricao        TEXT NOT NULL DEFAULT '',
    qtd_embalagem    REAL NOT NULL,
    qtd_un           REAL,
    produto_id       INTEGER REFERENCES wms_produtos(id),
    motivo_pendencia TEXT NOT NULL DEFAULT '',
    UNIQUE (pedido_id, linha)
);
CREATE TABLE IF NOT EXISTS wms_reservas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id      INTEGER NOT NULL REFERENCES wms_pedidos(id),
    item_id        INTEGER NOT NULL REFERENCES wms_pedido_itens(id),
    produto_id     INTEGER NOT NULL REFERENCES wms_produtos(id),
    posicao        TEXT NOT NULL,
    lote           TEXT NOT NULL DEFAULT '',
    validade       TEXT NOT NULL DEFAULT '',
    quantidade_un  REAL NOT NULL,
    estado         TEXT NOT NULL DEFAULT 'ATIVA',
    origem         TEXT NOT NULL DEFAULT 'FEFO',
    movimento_uuid TEXT NOT NULL DEFAULT '',
    criado_em      TEXT NOT NULL,
    atualizado_em  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wms_reservas_saldo
    ON wms_reservas(produto_id, posicao, lote, validade, estado);
CREATE INDEX IF NOT EXISTS idx_wms_reservas_pedido ON wms_reservas(pedido_id, estado);
"""


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    """Conexao com as tabelas da fase 1 (wms.conectar) mais as da fase 2."""
    conn = wms.conectar(caminho)
    conn.executescript(_DDL)
    return conn
```

- [ ] **Step 4: Rodar o teste e ver passar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: PASS (2 testes)

- [ ] **Step 5: Commit**

```bash
git add painel_agentes/wms_pedidos.py painel_agentes/test_wms_pedidos.py
git commit -m "WMS: tabelas de pedido, item e reserva (fase 2)"
```

---

### Task 2: Ler os itens do pedido na Stokki

**Files:**
- Modify: `stokki/pedidos.py` (adicionar funcao no fim do bloco de parsing, perto de `_extrair_mensagens`)
- Create: `stokki/test_extrair_itens.py`
- Create: `stokki/fixtures/pedido_39751_itens.html`

**Interfaces:**
- Consumes: `BeautifulSoup` (ja importado em `stokki/pedidos.py`).
- Produces: `extrair_itens_do_pedido(html: str) -> list[dict]`, cada dict com as chaves `linha` (int, 1-based), `sku` (str), `ean_linha` (str), `descricao` (str), `qtd_embalagem` (float).

- [ ] **Step 1: Criar a fixture**

Criar `stokki/fixtures/pedido_39751_itens.html` com a estrutura real observada na aba "Itens do pedido" do PS-39751 (21/09/2026). Linhas repetem o mesmo SKU com EANs diferentes de proposito — e assim na Stokki:

```html
<div class="tab-pane" id="items">
  <table class="table">
    <thead>
      <tr><th>Produto</th><th>EAN</th><th>Quantidade</th></tr>
    </thead>
    <tbody>
      <tr>
        <td>72400005 - BOLINHA DE QUEIJO FESTA ZC 5KG PCT</td>
        <td>430000200000</td>
        <td>2</td>
      </tr>
      <tr>
        <td>72400017 - COXINHA FESTA ZC 5KG PCT</td>
        <td>724000170000</td>
        <td>1</td>
      </tr>
      <tr>
        <td>72400017 - COXINHA FESTA ZC 5KG PCT</td>
        <td>430000100000</td>
        <td>2</td>
      </tr>
      <tr>
        <td>72400044 - PASTELZINHO DE PRESUNTO/QUEIJO FES</td>
        <td>724000444</td>
        <td>2,00</td>
      </tr>
    </tbody>
  </table>
</div>
```

- [ ] **Step 2: Escrever o teste que falha**

Criar `stokki/test_extrair_itens.py`:

```python
# -*- coding: utf-8 -*-
"""
test_extrair_itens.py

Parser da aba "Itens do pedido" da Stokki, sobre HTML real do PS-39751.
Rodar (da raiz):
    py -3.11 -m unittest stokki.test_extrair_itens -v
"""
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from stokki.pedidos import extrair_itens_do_pedido  # noqa: E402

_FIXTURE = Path(__file__).parent / "fixtures" / "pedido_39751_itens.html"


class TestExtrairItens(unittest.TestCase):
    def setUp(self):
        self.html = _FIXTURE.read_text(encoding="utf-8")
        self.itens = extrair_itens_do_pedido(self.html)

    def test_le_todas_as_linhas(self):
        self.assertEqual(len(self.itens), 4)

    def test_separa_sku_da_descricao(self):
        self.assertEqual(self.itens[0]["sku"], "72400005")
        self.assertEqual(self.itens[0]["descricao"], "BOLINHA DE QUEIJO FESTA ZC 5KG PCT")

    def test_mesmo_sku_em_duas_linhas_nao_e_agrupado(self):
        skus = [i["sku"] for i in self.itens]
        self.assertEqual(skus.count("72400017"), 2)
        eans = [i["ean_linha"] for i in self.itens if i["sku"] == "72400017"]
        self.assertEqual(eans, ["724000170000", "430000100000"])

    def test_linha_e_1_based_e_estavel(self):
        self.assertEqual([i["linha"] for i in self.itens], [1, 2, 3, 4])

    def test_quantidade_com_virgula_vira_float(self):
        self.assertEqual(self.itens[3]["qtd_embalagem"], 2.0)

    def test_html_sem_aba_de_itens_devolve_lista_vazia(self):
        self.assertEqual(extrair_itens_do_pedido("<html><body>nada</body></html>"), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Rodar o teste e ver falhar**

Rodar: `py -3.11 -m unittest stokki.test_extrair_itens -v`
Esperado: FAIL com `ImportError: cannot import name 'extrair_itens_do_pedido'`

- [ ] **Step 4: Escrever a implementacao**

Em `stokki/pedidos.py`, logo depois de `_extrair_mensagens`:

```python
RE_SKU_DESCRICAO = re.compile(r"^\s*(\S+)\s+-\s+(.*)$")


def extrair_itens_do_pedido(html: str) -> list[dict]:
    """
    Itens da aba "Itens do pedido" do detalhe (Produto | EAN | Quantidade).

    O mesmo SKU pode aparecer em mais de uma linha, com EANs diferentes --
    a linha e por embalagem, nao por produto. Por isso a lista devolvida
    preserva as linhas como estao, sem agrupar, e cada uma leva o numero
    da linha (1-based), que e a chave usada pela reserva.

    A quantidade aqui esta em EMBALAGEM, nao em UN.
    """
    soup = BeautifulSoup(html, "html.parser")
    tabela = None
    for t in soup.find_all("table"):
        cabecalhos = [th.get_text(strip=True).upper() for th in t.find_all("th")]
        if "PRODUTO" in cabecalhos and "EAN" in cabecalhos and "QUANTIDADE" in cabecalhos:
            tabela = t
            break
    if tabela is None:
        return []

    itens = []
    for tr in tabela.find_all("tr"):
        celulas = tr.find_all("td")
        if len(celulas) < 3:
            continue  # cabecalho ou linha de rodape
        produto = celulas[0].get_text(" ", strip=True)
        ean = celulas[1].get_text(" ", strip=True)
        bruta = celulas[2].get_text(" ", strip=True).replace(".", "").replace(",", ".")
        try:
            quantidade = float(bruta)
        except ValueError:
            continue  # linha de total ou celula vazia
        m = RE_SKU_DESCRICAO.match(produto)
        sku, descricao = (m.group(1), m.group(2).strip()) if m else ("", produto)
        itens.append({
            "linha": len(itens) + 1,
            "sku": sku,
            "ean_linha": re.sub(r"\D", "", ean),
            "descricao": descricao,
            "qtd_embalagem": quantidade,
        })
    return itens
```

- [ ] **Step 5: Rodar o teste e ver passar**

Rodar: `py -3.11 -m unittest stokki.test_extrair_itens -v`
Esperado: PASS (6 testes)

- [ ] **Step 6: Conferir contra a Stokki de verdade**

Na VPS, so leitura, confirmando que o parser le a pagina real (nao so a fixture):

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python -c \"
import yaml, pathlib
from stokki.auth import StokkiSession, BASE_URL
from stokki.pedidos import extrair_itens_do_pedido
cfg = yaml.safe_load(pathlib.Path('config.yaml').read_text(encoding='utf-8'))
s = StokkiSession(cfg)
h = s.get(f'{BASE_URL}/pt-br/administrator/inventory/outbound/show/39751').text
for i in extrair_itens_do_pedido(h): print(i)
\""
```

Esperado: as 9 linhas do PS-39751, com SKU separado da descricao. Se vier vazio, a fixture nao representa a pagina real — corrigir a fixture com o HTML de verdade antes de seguir.

- [ ] **Step 7: Commit**

```bash
git add stokki/pedidos.py stokki/test_extrair_itens.py stokki/fixtures/pedido_39751_itens.html
git commit -m "Stokki: ler os itens do pedido (produto, EAN e quantidade)"
```

---

### Task 3: Resolver o produto e converter embalagem em UN

**Files:**
- Modify: `painel_agentes/wms_pedidos.py`
- Test: `painel_agentes/test_wms_pedidos.py`

**Interfaces:**
- Consumes: `wms.conectar`, tabela `wms_produtos` (colunas `sku`, `ean`, `dun`, `qtd_por_caixa`, `ativo`).
- Produces: `resolver_item(conn, item: dict) -> dict` com as chaves `produto_id` (int|None), `qtd_un` (float|None), `motivo_pendencia` (str, vazio quando resolveu).

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `painel_agentes/test_wms_pedidos.py`:

```python
class TestResolverItem(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (1, 900, '72400017', 'COXINHA FESTA ZC 5KG PCT', "
            "'MARIA DOLORES', '724000170000', '430000100000', 6, 'UN', '2026-09-21 10:00:00')")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (2, 901, '72400099', 'QUIBE AVULSO', 'MARIA DOLORES', "
            "'111111111111', NULL, 1, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()

    def test_ean_da_linha_igual_ao_dun_multiplica_pela_caixa(self):
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400017", "ean_linha": "430000100000", "qtd_embalagem": 2})
        self.assertEqual(r["produto_id"], 1)
        self.assertEqual(r["qtd_un"], 12)
        self.assertEqual(r["motivo_pendencia"], "")

    def test_ean_da_linha_igual_ao_ean_unitario_e_um_pra_um(self):
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400017", "ean_linha": "724000170000", "qtd_embalagem": 3})
        self.assertEqual(r["produto_id"], 1)
        self.assertEqual(r["qtd_un"], 3)

    def test_sku_unico_com_caixa_de_um_resolve_sem_ean(self):
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400099", "ean_linha": "999999999999", "qtd_embalagem": 4})
        self.assertEqual(r["produto_id"], 2)
        self.assertEqual(r["qtd_un"], 4)

    def test_produto_desconhecido_vira_pendencia_sem_quantidade(self):
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "NAO-EXISTE", "ean_linha": "123", "qtd_embalagem": 1})
        self.assertIsNone(r["produto_id"])
        self.assertIsNone(r["qtd_un"])
        self.assertIn("nao encontrado", r["motivo_pendencia"].lower())

    def test_ean_que_nao_bate_com_caixa_maior_que_um_vira_pendencia(self):
        # SKU conhecido, mas o EAN da linha nao e nem o unitario nem o DUN:
        # nao da pra saber se sao 2 unidades ou 2 caixas de 6. Nao inventa.
        r = wms_pedidos.resolver_item(self.conn, {
            "sku": "72400017", "ean_linha": "555555555555", "qtd_embalagem": 2})
        self.assertIsNone(r["qtd_un"])
        self.assertIn("unidade", r["motivo_pendencia"].lower())
```

- [ ] **Step 2: Rodar e ver falhar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: FAIL com `AttributeError: module 'wms_pedidos' has no attribute 'resolver_item'`

- [ ] **Step 3: Implementar**

Em `painel_agentes/wms_pedidos.py`:

```python
def _so_digitos(valor) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def resolver_item(conn, item: dict) -> dict:
    """
    Descobre a que produto do catalogo a linha do pedido se refere e converte
    a quantidade de EMBALAGEM pra UN.

    A quantidade da Stokki e por embalagem e o EAN da linha diz QUAL
    embalagem. Ordem das regras (spec secao 6):
      1. EAN da linha == DUN do produto  -> qtd * qtd_por_caixa
      2. EAN da linha == EAN unitario    -> qtd
      3. SKU identifica um unico produto ativo com qtd_por_caixa == 1 -> qtd
      4. qualquer outro caso -> pendencia, sem quantidade

    Nunca chuta: um item que cai na regra 4 nao vira reserva nem baixa.
    """
    ean = _so_digitos(item.get("ean_linha"))
    sku = str(item.get("sku") or "").strip()
    qtd = float(item.get("qtd_embalagem") or 0)

    if ean:
        for coluna, fator_caixa in (("dun", True), ("ean", False)):
            rows = conn.execute(
                f"SELECT * FROM wms_produtos WHERE ativo = 1 AND {coluna} = ?", (ean,)).fetchall()
            if len(rows) == 1:
                p = rows[0]
                por_caixa = float(p["qtd_por_caixa"] or 1)
                return {"produto_id": p["id"],
                        "qtd_un": round(qtd * por_caixa, 3) if fator_caixa else qtd,
                        "motivo_pendencia": ""}
            if len(rows) > 1:
                return {"produto_id": None, "qtd_un": None,
                        "motivo_pendencia": f"EAN {ean} esta em {len(rows)} produtos ativos"}

    if sku:
        rows = conn.execute(
            "SELECT * FROM wms_produtos WHERE ativo = 1 AND sku = ? COLLATE NOCASE", (sku,)).fetchall()
        if len(rows) == 1:
            p = rows[0]
            por_caixa = float(p["qtd_por_caixa"] or 1)
            if por_caixa == 1:
                return {"produto_id": p["id"], "qtd_un": qtd, "motivo_pendencia": ""}
            return {"produto_id": p["id"], "qtd_un": None,
                    "motivo_pendencia": (f"EAN {ean or '(vazio)'} nao e o unitario nem o DUN do SKU {sku}; "
                                         f"sem saber a unidade, nao da pra converter")}
        if len(rows) > 1:
            return {"produto_id": None, "qtd_un": None,
                    "motivo_pendencia": f"SKU {sku} esta em {len(rows)} produtos ativos"}

    return {"produto_id": None, "qtd_un": None,
            "motivo_pendencia": f"Produto nao encontrado no catalogo (SKU {sku or '-'}, EAN {ean or '-'})"}
```

- [ ] **Step 4: Rodar e ver passar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: PASS (7 testes)

- [ ] **Step 5: Commit**

```bash
git add painel_agentes/wms_pedidos.py painel_agentes/test_wms_pedidos.py
git commit -m "WMS: resolver o produto do item do pedido e converter caixa em UN"
```

---

### Task 4: Disponivel e alocacao FEFO

**Files:**
- Modify: `painel_agentes/wms_pedidos.py`
- Test: `painel_agentes/test_wms_pedidos.py`

**Interfaces:**
- Consumes: `wms_saldos` (fase 1), `wms_reservas` (Task 1), `resolver_item` (Task 3).
- Produces:
  - `disponivel_por_lote(conn, produto_id: int) -> list[dict]` — cada dict com `posicao`, `lote`, `validade`, `saldo`, `reservado`, `disponivel`, ordenado por FEFO (validade crescente; sem validade por ultimo).
  - `alocar_fefo(conn, produto_id: int, qtd_un: float) -> tuple[list[dict], float]` — lista de alocacoes (`posicao`, `lote`, `validade`, `quantidade_un`) e o quanto faltou (0.0 quando cobriu tudo).

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `painel_agentes/test_wms_pedidos.py`:

```python
class TestFEFO(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, qtd_por_caixa, "
            "atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', 'UN', 1, "
            "'2026-09-21 10:00:00')")
        self.conn.commit()
        # tres lotes do mesmo produto, validades fora de ordem de propósito
        for posicao, lote, validade, qtd in [
            ("C9-E1-N1", "L-B", "2026-12-31", 10),
            ("C9-E1-N2", "L-A", "2026-10-15", 6),
            ("C9-E2-N1", "L-C", "2027-05-01", 20),
        ]:
            wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=qtd,
                                    lote=lote, validade=validade, destino=posicao)

    def test_disponivel_vem_em_ordem_de_validade(self):
        linhas = wms_pedidos.disponivel_por_lote(self.conn, 1)
        self.assertEqual([l["lote"] for l in linhas], ["L-A", "L-B", "L-C"])
        self.assertEqual(linhas[0]["disponivel"], 6)

    def test_fefo_consome_o_que_vence_primeiro(self):
        alocacoes, faltou = wms_pedidos.alocar_fefo(self.conn, 1, 4)
        self.assertEqual(faltou, 0)
        self.assertEqual(len(alocacoes), 1)
        self.assertEqual(alocacoes[0]["lote"], "L-A")
        self.assertEqual(alocacoes[0]["quantidade_un"], 4)

    def test_fefo_quebra_em_varios_lotes_quando_precisa(self):
        alocacoes, faltou = wms_pedidos.alocar_fefo(self.conn, 1, 14)
        self.assertEqual(faltou, 0)
        self.assertEqual([(a["lote"], a["quantidade_un"]) for a in alocacoes],
                         [("L-A", 6), ("L-B", 8)])

    def test_fefo_sem_saldo_suficiente_aloca_o_que_da_e_informa_a_falta(self):
        alocacoes, faltou = wms_pedidos.alocar_fefo(self.conn, 1, 50)
        self.assertEqual(faltou, 14)  # 36 em estoque
        self.assertEqual(sum(a["quantidade_un"] for a in alocacoes), 36)

    def _reserva_crua(self, estado):
        """Insere uma reserva direto na tabela. Precisa de pedido e item de
        verdade: wms.conectar liga PRAGMA foreign_keys = ON."""
        agora = wms.agora()
        self.conn.execute(
            "INSERT INTO wms_pedidos (id, id_stokki, codigo_ps, embarcador, situacao, estado_reserva, "
            "lido_em, atualizado_em) VALUES (1, 39751, 'PS-39751', 'MARIA DOLORES', 'Separating', "
            "'PENDENTE', ?, ?)", (agora, agora))
        self.conn.execute(
            "INSERT INTO wms_pedido_itens (id, pedido_id, linha, sku, descricao, qtd_embalagem, qtd_un, "
            "produto_id) VALUES (1, 1, 1, 'SKU1', 'PRODUTO 1', 5, 5, 1)")
        self.conn.execute(
            "INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade, "
            "quantidade_un, estado, origem, criado_em, atualizado_em) "
            f"VALUES (1, 1, 1, 'C9-E1-N2', 'L-A', '2026-10-15', 5, '{estado}', 'FEFO', ?, ?)",
            (agora, agora))
        self.conn.commit()

    def test_reserva_ativa_derruba_o_disponivel_mas_nao_o_saldo(self):
        self._reserva_crua("ATIVA")
        linhas = wms_pedidos.disponivel_por_lote(self.conn, 1)
        lote_a = [l for l in linhas if l["lote"] == "L-A"][0]
        self.assertEqual(lote_a["saldo"], 6)
        self.assertEqual(lote_a["reservado"], 5)
        self.assertEqual(lote_a["disponivel"], 1)
        # o saldo fisico da fase 1 nao mudou
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N2", 1, "L-A", "2026-10-15"), 6)

    def test_reserva_cancelada_nao_conta(self):
        self._reserva_crua("CANCELADA")
        lote_a = [l for l in wms_pedidos.disponivel_por_lote(self.conn, 1) if l["lote"] == "L-A"][0]
        self.assertEqual(lote_a["disponivel"], 6)
```

- [ ] **Step 2: Rodar e ver falhar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: FAIL com `AttributeError: module 'wms_pedidos' has no attribute 'disponivel_por_lote'`

- [ ] **Step 3: Implementar**

Em `painel_agentes/wms_pedidos.py`:

```python
def disponivel_por_lote(conn, produto_id: int) -> list[dict]:
    """
    Saldo de um produto por (posicao, lote, validade), ja descontando as
    reservas ATIVAS. Ordem FEFO: quem vence primeiro vem primeiro; sem
    validade vai pro fim.

    O disponivel e sempre derivado -- wms_saldos nunca e tocado pela reserva.
    """
    rows = conn.execute("""
        SELECT s.posicao, s.lote, s.validade, s.quantidade AS saldo,
               COALESCE((SELECT SUM(r.quantidade_un) FROM wms_reservas r
                          WHERE r.produto_id = s.produto_id AND r.posicao = s.posicao
                            AND r.lote = s.lote AND r.validade = s.validade
                            AND r.estado = 'ATIVA'), 0) AS reservado
          FROM wms_saldos s
          JOIN wms_posicoes p ON p.codigo = s.posicao AND p.ativo = 1
         WHERE s.produto_id = ? AND s.quantidade > 0
         ORDER BY s.validade = '', s.validade, s.posicao""", (int(produto_id),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["saldo"] = round(float(d["saldo"]), 3)
        d["reservado"] = round(float(d["reservado"]), 3)
        d["disponivel"] = round(d["saldo"] - d["reservado"], 3)
        out.append(d)
    return out


def alocar_fefo(conn, produto_id: int, qtd_un: float) -> tuple[list[dict], float]:
    """
    Distribui qtd_un pelos lotes disponiveis, do que vence primeiro pro que
    vence depois. Devolve (alocacoes, faltante).

    Faltar saldo nao e erro (decisao do Hugo, 21/09): aloca o que da e diz
    quanto faltou, pra virar pendencia.
    """
    restante = round(float(qtd_un), 3)
    alocacoes = []
    for linha in disponivel_por_lote(conn, produto_id):
        if restante <= 0:
            break
        se_pega = min(linha["disponivel"], restante)
        if se_pega <= 0:
            continue
        alocacoes.append({"posicao": linha["posicao"], "lote": linha["lote"],
                          "validade": linha["validade"], "quantidade_un": round(se_pega, 3)})
        restante = round(restante - se_pega, 3)
    return alocacoes, max(restante, 0.0)
```

- [ ] **Step 4: Rodar e ver passar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: PASS (13 testes)

- [ ] **Step 5: Commit**

```bash
git add painel_agentes/wms_pedidos.py painel_agentes/test_wms_pedidos.py
git commit -m "WMS: disponivel por lote descontando reserva e alocacao FEFO"
```

---

### Task 5: Gravar o pedido e reservar

**Files:**
- Modify: `painel_agentes/wms_pedidos.py`
- Test: `painel_agentes/test_wms_pedidos.py`

**Interfaces:**
- Consumes: `resolver_item` (Task 3), `alocar_fefo` (Task 4).
- Produces:
  - `registrar_pedido(conn, pedido: dict, itens: list[dict]) -> int` — `pedido` tem `id_stokki`, `codigo_ps`, `embarcador`, `situacao`; devolve o `id` local. Reexecutar com os mesmos dados atualiza situacao e itens sem duplicar.
  - `reservar_pedido(conn, pedido_id: int) -> dict` — devolve `{"estado": str, "reservas": int, "pendencias": list[str]}`.
  - `cancelar_reservas(conn, pedido_id: int, motivo: str = "") -> int`.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `painel_agentes/test_wms_pedidos.py`:

```python
class TestReservarPedido(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'111111111111', '222222222222', 6, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        self.itens = [
            {"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
             "descricao": "PRODUTO 1", "qtd_embalagem": 4},
        ]
        self.pedido = {"id_stokki": 39751, "codigo_ps": "PS-39751",
                       "embarcador": "MARIA DOLORES", "situacao": "Waiting for Carrier"}

    def test_registrar_pedido_grava_itens_resolvidos(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        item = self.conn.execute("SELECT * FROM wms_pedido_itens WHERE pedido_id = ?", (pid,)).fetchone()
        self.assertEqual(item["produto_id"], 1)
        self.assertEqual(item["qtd_un"], 4)

    def test_registrar_duas_vezes_nao_duplica(self):
        pid1 = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        pid2 = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        self.assertEqual(pid1, pid2)
        n = self.conn.execute("SELECT COUNT(*) n FROM wms_pedido_itens").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_reserva_completa_marca_pedido_como_reservado(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        r = wms_pedidos.reservar_pedido(self.conn, pid)
        self.assertEqual(r["estado"], "RESERVADO")
        self.assertEqual(r["reservas"], 1)
        reserva = self.conn.execute("SELECT * FROM wms_reservas").fetchone()
        self.assertEqual(reserva["lote"], "L-A")
        self.assertEqual(reserva["quantidade_un"], 4)
        self.assertEqual(reserva["estado"], "ATIVA")

    def test_reservar_duas_vezes_nao_duplica_reserva(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        wms_pedidos.reservar_pedido(self.conn, pid)
        wms_pedidos.reservar_pedido(self.conn, pid)
        n = self.conn.execute("SELECT COUNT(*) n FROM wms_reservas WHERE estado='ATIVA'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_sem_saldo_suficiente_fica_parcial_e_nunca_falha(self):
        itens = [dict(self.itens[0], qtd_embalagem=99)]
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, itens)
        r = wms_pedidos.reservar_pedido(self.conn, pid)
        self.assertEqual(r["estado"], "PARCIAL")
        self.assertTrue(any("falt" in p.lower() for p in r["pendencias"]))
        self.assertEqual(self.conn.execute(
            "SELECT SUM(quantidade_un) s FROM wms_reservas").fetchone()["s"], 10)

    def test_item_nao_resolvido_nao_vira_reserva(self):
        itens = [{"linha": 1, "sku": "FANTASMA", "ean_linha": "000",
                  "descricao": "NAO EXISTE", "qtd_embalagem": 1}]
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, itens)
        r = wms_pedidos.reservar_pedido(self.conn, pid)
        self.assertEqual(r["reservas"], 0)
        self.assertEqual(r["estado"], "PARCIAL")
        self.assertTrue(r["pendencias"])

    def test_cancelar_reservas_libera_o_disponivel(self):
        pid = wms_pedidos.registrar_pedido(self.conn, self.pedido, self.itens)
        wms_pedidos.reservar_pedido(self.conn, pid)
        wms_pedidos.cancelar_reservas(self.conn, pid, "pedido cancelado na Stokki")
        linha = wms_pedidos.disponivel_por_lote(self.conn, 1)[0]
        self.assertEqual(linha["disponivel"], 10)
        estado = self.conn.execute("SELECT estado_reserva FROM wms_pedidos WHERE id = ?",
                                   (pid,)).fetchone()["estado_reserva"]
        self.assertEqual(estado, "CANCELADO")
```

- [ ] **Step 2: Rodar e ver falhar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: FAIL com `AttributeError: module 'wms_pedidos' has no attribute 'registrar_pedido'`

- [ ] **Step 3: Implementar**

Em `painel_agentes/wms_pedidos.py`:

```python
def registrar_pedido(conn, pedido: dict, itens: list[dict]) -> int:
    """
    Cria ou atualiza o espelho do pedido e as linhas de item, ja resolvendo
    produto e unidade. Idempotente por id_stokki + linha.
    """
    id_stokki = int(pedido["id_stokki"])
    agora = wms.agora()
    row = conn.execute("SELECT id FROM wms_pedidos WHERE id_stokki = ?", (id_stokki,)).fetchone()
    if row:
        pedido_id = row["id"]
        conn.execute("UPDATE wms_pedidos SET situacao = ?, embarcador = ?, atualizado_em = ? WHERE id = ?",
                     (pedido.get("situacao", ""), pedido.get("embarcador", ""), agora, pedido_id))
    else:
        cur = conn.execute("""
            INSERT INTO wms_pedidos (id_stokki, codigo_ps, embarcador, situacao, estado_reserva, lido_em, atualizado_em)
            VALUES (?,?,?,?,'PENDENTE',?,?)""",
            (id_stokki, pedido.get("codigo_ps", ""), pedido.get("embarcador", ""),
             pedido.get("situacao", ""), agora, agora))
        pedido_id = cur.lastrowid

    for item in itens:
        r = resolver_item(conn, item)
        conn.execute("""
            INSERT INTO wms_pedido_itens (pedido_id, linha, sku, ean_linha, descricao, qtd_embalagem,
                                          qtd_un, produto_id, motivo_pendencia)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(pedido_id, linha) DO UPDATE SET
                sku = excluded.sku, ean_linha = excluded.ean_linha, descricao = excluded.descricao,
                qtd_embalagem = excluded.qtd_embalagem, qtd_un = excluded.qtd_un,
                produto_id = excluded.produto_id, motivo_pendencia = excluded.motivo_pendencia""",
            (pedido_id, int(item["linha"]), item.get("sku", ""), item.get("ean_linha", ""),
             item.get("descricao", ""), float(item["qtd_embalagem"]),
             r["qtd_un"], r["produto_id"], r["motivo_pendencia"]))
    return pedido_id


def reservar_pedido(conn, pedido_id: int) -> dict:
    """
    Reserva FEFO o que cada item do pedido precisa. Nao mexe em saldo fisico.

    Idempotente: itens que ja tem reserva ATIVA sao pulados. Falta de saldo
    ou item nao resolvido nao levanta erro -- vira pendencia e o pedido fica
    PARCIAL (decisao do Hugo, 21/09: avisar, nunca bloquear).
    """
    agora = wms.agora()
    pendencias = []
    criadas = 0
    itens = conn.execute("SELECT * FROM wms_pedido_itens WHERE pedido_id = ? ORDER BY linha",
                         (pedido_id,)).fetchall()
    for item in itens:
        ja = conn.execute("SELECT COUNT(*) n FROM wms_reservas WHERE item_id = ? AND estado = 'ATIVA'",
                          (item["id"],)).fetchone()["n"]
        if ja:
            continue
        if not item["produto_id"] or item["qtd_un"] is None:
            pendencias.append(f"linha {item['linha']}: {item['motivo_pendencia']}")
            continue
        alocacoes, faltou = alocar_fefo(conn, item["produto_id"], item["qtd_un"])
        for a in alocacoes:
            conn.execute("""
                INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade,
                                          quantidade_un, estado, origem, criado_em, atualizado_em)
                VALUES (?,?,?,?,?,?,?,'ATIVA','FEFO',?,?)""",
                (pedido_id, item["id"], item["produto_id"], a["posicao"], a["lote"], a["validade"],
                 a["quantidade_un"], agora, agora))
            criadas += 1
        if faltou > 0:
            pendencias.append(
                f"linha {item['linha']} ({item['descricao']}): faltaram {faltou:g} UN em estoque")

    total_itens = len(itens)
    resolvidos = conn.execute("""
        SELECT COUNT(*) n FROM wms_pedido_itens i
         WHERE i.pedido_id = ? AND i.qtd_un IS NOT NULL
           AND i.qtd_un <= COALESCE((SELECT SUM(r.quantidade_un) FROM wms_reservas r
                                      WHERE r.item_id = i.id AND r.estado IN ('ATIVA','CONSUMIDA')), 0)
        """, (pedido_id,)).fetchone()["n"]
    estado = "RESERVADO" if total_itens and resolvidos == total_itens else "PARCIAL"
    conn.execute("UPDATE wms_pedidos SET estado_reserva = ?, atualizado_em = ? WHERE id = ?",
                 (estado, agora, pedido_id))
    return {"estado": estado, "reservas": criadas, "pendencias": pendencias}


def cancelar_reservas(conn, pedido_id: int, motivo: str = "") -> int:
    """Libera as reservas ATIVAS do pedido e marca o pedido como CANCELADO."""
    agora = wms.agora()
    cur = conn.execute(
        "UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? WHERE pedido_id = ? AND estado = 'ATIVA'",
        (agora, pedido_id))
    conn.execute("UPDATE wms_pedidos SET estado_reserva = 'CANCELADO', atualizado_em = ? WHERE id = ?",
                 (agora, pedido_id))
    return cur.rowcount
```

- [ ] **Step 4: Rodar e ver passar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: PASS (20 testes)

- [ ] **Step 5: Commit**

```bash
git add painel_agentes/wms_pedidos.py painel_agentes/test_wms_pedidos.py
git commit -m "WMS: reserva FEFO por pedido, com pendencia quando falta saldo"
```

---

### Task 6: Baixa na expedicao

**Files:**
- Modify: `painel_agentes/wms_pedidos.py`
- Modify: `expedir_pedidos.py:1239` (o da RAIZ — e o que roda em producao)
- Test: `painel_agentes/test_wms_pedidos.py`

**Interfaces:**
- Consumes: `wms.registrar_movimento(conn, tipo="SAIDA", produto_id, quantidade, lote, validade, origem, uuid, observacao)`.
- Produces: `baixar_por_expedicao(conn, codigo_ps: str, operador: dict | None = None) -> dict` com `{"pedido_id": int|None, "baixas": int, "ja_baixado": bool, "erros": list[str]}`.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `painel_agentes/test_wms_pedidos.py`:

```python
class TestBaixaNaExpedicao(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, dun, qtd_por_caixa, "
            "unidade, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'111111111111', '222222222222', 6, 'UN', '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=10,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        self.pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 39751, "codigo_ps": "PS-39751", "embarcador": "MARIA DOLORES",
             "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO 1", "qtd_embalagem": 4}])
        wms_pedidos.reservar_pedido(self.conn, self.pid)

    def test_baixa_gera_saida_e_derruba_o_saldo(self):
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39751")
        self.assertEqual(r["baixas"], 1)
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N1", 1, "L-A", "2026-10-15"), 6)
        mov = self.conn.execute("SELECT * FROM wms_movimentos WHERE tipo = 'SAIDA'").fetchone()
        self.assertEqual(mov["quantidade"], 4)
        self.assertEqual(mov["posicao_origem"], "C9-E1-N1")

    def test_baixar_duas_vezes_nao_baixa_em_dobro(self):
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-39751")
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-39751")
        self.assertTrue(r["ja_baixado"])
        self.assertEqual(wms._saldo_atual(self.conn, "C9-E1-N1", 1, "L-A", "2026-10-15"), 6)
        n = self.conn.execute("SELECT COUNT(*) n FROM wms_movimentos WHERE tipo='SAIDA'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_baixa_marca_pedido_e_reservas(self):
        wms_pedidos.baixar_por_expedicao(self.conn, "PS-39751")
        estado = self.conn.execute("SELECT estado_reserva FROM wms_pedidos WHERE id = ?",
                                   (self.pid,)).fetchone()["estado_reserva"]
        self.assertEqual(estado, "BAIXADO")
        reserva = self.conn.execute("SELECT * FROM wms_reservas").fetchone()
        self.assertEqual(reserva["estado"], "CONSUMIDA")
        self.assertTrue(reserva["movimento_uuid"])

    def test_codigo_com_sufixo_de_reentrega_encontra_o_pedido(self):
        # a VUUPT devolve '#PS-39751-R2' em reentrega; tem que cair no mesmo pedido
        r = wms_pedidos.baixar_por_expedicao(self.conn, "#PS-39751-R2")
        self.assertEqual(r["pedido_id"], self.pid)

    def test_pedido_desconhecido_nao_explode(self):
        r = wms_pedidos.baixar_por_expedicao(self.conn, "PS-00000")
        self.assertIsNone(r["pedido_id"])
        self.assertEqual(r["baixas"], 0)
```

- [ ] **Step 2: Rodar e ver falhar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: FAIL com `AttributeError: module 'wms_pedidos' has no attribute 'baixar_por_expedicao'`

- [ ] **Step 3: Implementar**

Em `painel_agentes/wms_pedidos.py`:

```python
import re


def _id_stokki_do_codigo(codigo_ps: str) -> int | None:
    """
    Tira o id numerico do codigo do pedido. A VUUPT devolve '#PS-39751',
    'PS-39751' ou '#PS-39751-R2' (reentrega) -- todos sao o mesmo pedido.
    """
    m = re.search(r"PS-?(\d+)", str(codigo_ps or "").upper())
    return int(m.group(1)) if m else None


def baixar_por_expedicao(conn, codigo_ps: str, operador: dict | None = None) -> dict:
    """
    Converte as reservas ATIVAS do pedido em SAIDA de estoque.

    O uuid do movimento e deterministico (ps-<id>-item-<linha>-<n>) e o
    registrar_movimento ja e idempotente por uuid: rodar duas vezes nao
    baixa duas vezes. Um erro numa reserva nao derruba as outras.
    """
    id_stokki = _id_stokki_do_codigo(codigo_ps)
    if not id_stokki:
        return {"pedido_id": None, "baixas": 0, "ja_baixado": False,
                "erros": [f"codigo de pedido nao reconhecido: {codigo_ps!r}"]}
    pedido = conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = ?", (id_stokki,)).fetchone()
    if not pedido:
        return {"pedido_id": None, "baixas": 0, "ja_baixado": False, "erros": []}

    reservas = conn.execute("""
        SELECT r.*, i.linha FROM wms_reservas r
          JOIN wms_pedido_itens i ON i.id = r.item_id
         WHERE r.pedido_id = ? AND r.estado = 'ATIVA'
         ORDER BY i.linha, r.id""", (pedido["id"],)).fetchall()
    if not reservas:
        ja = pedido["estado_reserva"] == "BAIXADO"
        return {"pedido_id": pedido["id"], "baixas": 0, "ja_baixado": ja, "erros": []}

    baixas, erros = 0, []
    por_item = {}
    for r in reservas:
        por_item[r["linha"]] = por_item.get(r["linha"], 0) + 1
        uuid = f"ps-{id_stokki}-item-{r['linha']}-{por_item[r['linha']]}"
        try:
            mov = wms.registrar_movimento(
                conn, tipo="SAIDA", produto_id=r["produto_id"], quantidade=r["quantidade_un"],
                lote=r["lote"], validade=r["validade"] or None, origem=r["posicao"],
                operador=operador, uuid=uuid,
                observacao=f"Baixa automatica da expedicao do {pedido['codigo_ps']}",
                permitir_negativo=True)
            conn.execute(
                "UPDATE wms_reservas SET estado = 'CONSUMIDA', movimento_uuid = ?, atualizado_em = ? WHERE id = ?",
                (mov["uuid"], wms.agora(), r["id"]))
            if not mov.get("duplicado"):
                baixas += 1
        except Exception as e:  # noqa: BLE001 -- uma reserva ruim nao derruba a expedicao
            erros.append(f"linha {r['linha']}: {e}")
    conn.execute("UPDATE wms_pedidos SET estado_reserva = 'BAIXADO', atualizado_em = ? WHERE id = ?",
                 (wms.agora(), pedido["id"]))
    conn.commit()
    return {"pedido_id": pedido["id"], "baixas": baixas, "ja_baixado": False, "erros": erros}
```

Nota sobre `permitir_negativo=True`: a mercadoria ja saiu fisicamente do galpao quando a expedicao roda. Recusar a baixa por divergencia de cadastro deixaria o estoque mais errado ainda, nao menos.

- [ ] **Step 4: Rodar e ver passar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: PASS (25 testes)

- [ ] **Step 5: Plugar na expedicao**

Em `expedir_pedidos.py`, dentro do bloco `if resultado_exp in ("expedido", "ja_expedido"):` (hoje na linha 1239), logo depois do incremento dos contadores e **antes** do tratamento do canhoto:

```python
                # Baixa do estoque proprio (WMS fase 2). Nunca derruba a
                # expedicao: se o WMS falhar, o pedido ja foi expedido na
                # Stokki e isso e o que importa aqui.
                try:
                    import wms_pedidos
                    conn_wms = wms_pedidos.conectar()
                    try:
                        r_wms = wms_pedidos.baixar_por_expedicao(conn_wms, codigo_ps)
                    finally:
                        conn_wms.close()
                    if r_wms["baixas"]:
                        logger.info(f"  {codigo_ps}: WMS baixou {r_wms['baixas']} reserva(s).")
                    for erro in r_wms["erros"]:
                        logger.warning(f"  {codigo_ps}: WMS {erro}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"  {codigo_ps}: baixa no WMS falhou ({e}) -- expedicao segue.")
```

O `sys.path` da raiz ja inclui `painel_agentes/`? Conferir no topo de `expedir_pedidos.py`; se nao incluir, acrescentar junto dos outros `sys.path.insert`:

```python
sys.path.insert(0, str(Path(__file__).parent / "painel_agentes"))
```

- [ ] **Step 6: Verificar que a expedicao continua compilando e passando**

```bash
py -3.11 -m py_compile expedir_pedidos.py painel_agentes/wms_pedidos.py
py -3.11 -m unittest painel_agentes.test_wms_pedidos -v
```

Esperado: compila sem erro, 25 testes passam.

- [ ] **Step 7: Commit**

```bash
git add painel_agentes/wms_pedidos.py painel_agentes/test_wms_pedidos.py expedir_pedidos.py
git commit -m "WMS: expedicao baixa as reservas do pedido (idempotente por uuid)"
```

---

### Task 7: Rotina de lote da reserva + timer

**Files:**
- Create: `sincronizar_pedidos_wms.py`
- Create: `infra/stokki-wms-reservar-pedidos.service`
- Create: `infra/stokki-wms-reservar-pedidos.timer`
- Modify: `painel_agentes/wms_pedidos.py` (funcao `pendencias`)

**Interfaces:**
- Consumes: `stokki.pedidos.listar_pedidos`, `extrair_itens_do_pedido` (Task 2), `registrar_pedido`/`reservar_pedido` (Task 5).
- Produces: `main(argv=None) -> int` com `--modo-teste`, `--limite`, `--embarcador`; `wms_pedidos.pendencias(conn, limite=50) -> list[dict]`.

- [ ] **Step 1: Escrever o teste da pendencia**

Acrescentar em `painel_agentes/test_wms_pedidos.py`:

```python
class TestPendencias(BaseWMS):
    def test_pendencias_lista_item_nao_resolvido_com_o_pedido(self):
        pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 40001, "codigo_ps": "PS-40001", "embarcador": "MARIA DOLORES",
             "situacao": "Waiting for Carrier"},
            [{"linha": 1, "sku": "FANTASMA", "ean_linha": "000",
              "descricao": "NAO EXISTE", "qtd_embalagem": 1}])
        wms_pedidos.reservar_pedido(self.conn, pid)
        p = wms_pedidos.pendencias(self.conn)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["codigo_ps"], "PS-40001")
        self.assertIn("nao encontrado", p[0]["motivo_pendencia"].lower())
```

- [ ] **Step 2: Rodar e ver falhar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos.TestPendencias -v`
Esperado: FAIL com `AttributeError: module 'wms_pedidos' has no attribute 'pendencias'`

- [ ] **Step 3: Implementar a pendencia**

Em `painel_agentes/wms_pedidos.py`:

```python
def pendencias(conn, limite: int = 50) -> list[dict]:
    """Itens que nao viraram reserva -- produto/unidade nao resolvidos."""
    rows = conn.execute("""
        SELECT i.*, p.codigo_ps, p.embarcador, p.situacao, p.estado_reserva
          FROM wms_pedido_itens i JOIN wms_pedidos p ON p.id = i.pedido_id
         WHERE i.motivo_pendencia <> '' AND p.estado_reserva <> 'CANCELADO'
         ORDER BY p.id DESC, i.linha LIMIT ?""", (int(limite),)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Rodar e ver passar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: PASS (26 testes)

- [ ] **Step 5: Escrever a rotina de lote**

Criar `sincronizar_pedidos_wms.py`:

```python
# -*- coding: utf-8 -*-
"""
sincronizar_pedidos_wms.py

Le os pedidos do embarcador piloto na Stokki, espelha itens em
wms_pedido_itens e reserva o estoque (FEFO) pra cada um.

Reserva nao mexe em saldo fisico: o disponivel e derivado. Falta de saldo
nao bloqueia nada -- vira pendencia (decisao do Hugo, 21/09).

COMO USAR (timer systemd a cada 15 min, infra/stokki-wms-reservar-pedidos.*):
    python sincronizar_pedidos_wms.py
    python sincronizar_pedidos_wms.py --modo-teste      # nao grava nada
    python sincronizar_pedidos_wms.py --limite 20
"""
import argparse
import logging
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

import wms_pedidos
from stokki import pedidos as stokki_pedidos
from stokki.auth import StokkiSession

logger = logging.getLogger("sincronizar_pedidos_wms")

STATUS_INTERESSANTES = ("Waiting for Carrier", "Separating", "Pack")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _embarcador_piloto(config: dict) -> str:
    return (config.get("wms", {}) or {}).get("embarcador_piloto", "MARIA DOLORES").strip().upper()


def rodar(conn, sess, piloto: str, limite: int, modo_teste: bool) -> dict:
    res = {"lidos": 0, "do_piloto": 0, "reservados": 0, "parciais": 0, "pendencias": 0, "erros": 0}
    vistos = set()
    for status in STATUS_INTERESSANTES:
        resposta = stokki_pedidos.listar_pedidos(sess, status=status, por_pagina=limite,
                                                 ordenar_coluna="1", ordenar_dir="desc")
        for linha in (resposta.get("aaData") or []):
            res["lidos"] += 1
            id_pedido = stokki_pedidos.extrair_id_da_linha(linha)
            if not id_pedido or id_pedido in vistos:
                continue
            vistos.add(id_pedido)
            try:
                detalhe = stokki_pedidos.obter_detalhe(sess, id_pedido)
            except Exception as e:  # noqa: BLE001 -- um pedido ruim nao derruba a rodada
                logger.warning("Detalhe do pedido %s falhou: %s", id_pedido, e)
                res["erros"] += 1
                continue
            embarcador = ((detalhe.get("cliente") or {}).get("nome") or "").upper()
            if piloto not in embarcador:
                continue
            res["do_piloto"] += 1
            if modo_teste:
                logger.info("[teste] %s (%s) seria reservado", id_pedido, embarcador[:40])
                continue

            # A leitura HTTP fica FORA da transacao; commit por pedido, porque
            # o dados.db e compartilhado com o painel e todos os agentes.
            html = sess.get(
                f"{stokki_pedidos.BASE_URL}/pt-br/administrator/inventory/outbound/show/{id_pedido}").text
            itens = stokki_pedidos.extrair_itens_do_pedido(html)
            if not itens:
                logger.warning("Pedido %s sem itens legiveis -- pulando.", id_pedido)
                res["erros"] += 1
                continue
            try:
                pedido_id = wms_pedidos.registrar_pedido(conn, {
                    "id_stokki": id_pedido,
                    "codigo_ps": stokki_pedidos.extrair_codigo_ps_da_linha(linha),
                    "embarcador": embarcador,
                    "situacao": status,
                }, itens)
                r = wms_pedidos.reservar_pedido(conn, pedido_id)
                conn.commit()
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                logger.warning("Reserva do pedido %s falhou: %s", id_pedido, e)
                res["erros"] += 1
                continue
            res["reservados" if r["estado"] == "RESERVADO" else "parciais"] += 1
            res["pendencias"] += len(r["pendencias"])
            for p in r["pendencias"]:
                logger.info("  %s: %s", id_pedido, p)
            time.sleep(0.3)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modo-teste", action="store_true", help="nao grava nada, so mostra o que faria")
    ap.add_argument("--limite", type=int, default=50, help="pedidos por status nesta rodada")
    ap.add_argument("--embarcador", help="sobrescreve o embarcador piloto do config.yaml")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "wms_reservar_pedidos.log", encoding="utf-8")])
    config = _carregar_config()
    piloto = (args.embarcador or _embarcador_piloto(config)).upper()
    conn = wms_pedidos.conectar()
    sess = StokkiSession(config)
    t0 = time.time()
    try:
        res = rodar(conn, sess, piloto, args.limite, args.modo_teste)
    finally:
        conn.close()
    logger.info("Piloto %s: %s (%.0fs)", piloto, res, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Rodar em modo teste, de verdade**

```bash
py -3.11 -m py_compile sincronizar_pedidos_wms.py
```

E na VPS (leitura da Stokki, sem gravar):

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python sincronizar_pedidos_wms.py --modo-teste --limite 20"
```

Esperado: linhas `[teste] <id> (MARIA DOLORES ...) seria reservado` e o resumo final. Zero gravacao.

- [ ] **Step 7: Criar as units**

`infra/stokki-wms-reservar-pedidos.service`:

```ini
[Unit]
Description=Stokki Eventos - Reserva estoque do WMS pros pedidos do embarcador piloto
OnFailure=stokki-alerta-falha@%n.service

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/opt/stokki-eventos
ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/sincronizar_pedidos_wms.py
```

`infra/stokki-wms-reservar-pedidos.timer`:

```ini
[Unit]
Description=Dispara stokki-wms-reservar-pedidos.service a cada 15 min (deslocado dos outros syncs)

[Timer]
OnCalendar=*-*-* *:04/15:00
Persistent=false
AccuracySec=1min

[Install]
WantedBy=timers.target
```

Antes de criar, conferir que o horario nao colide e que o Hugo nao criou nada parecido:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "systemctl list-timers --all | grep -i wms; ls /etc/systemd/system/ | grep -i wms"
```

- [ ] **Step 8: Commit**

```bash
git add sincronizar_pedidos_wms.py infra/stokki-wms-reservar-pedidos.service infra/stokki-wms-reservar-pedidos.timer painel_agentes/wms_pedidos.py painel_agentes/test_wms_pedidos.py
git commit -m "WMS: rotina de 15 min que reserva estoque pros pedidos do piloto"
```

---

### Task 8: Recebimento puxado da Stokki

**Files:**
- Create: `stokki/recebimentos.py`
- Create: `stokki/test_recebimentos.py`
- Create: `sincronizar_recebimentos_wms.py`
- Create: `infra/stokki-wms-recebimentos.service`, `infra/stokki-wms-recebimentos.timer`
- Modify: `painel_agentes/wms_pedidos.py` (tabela `wms_recebimentos`)

**Interfaces:**
- Consumes: `StokkiSession`, `/pt-br/administrator/inventory/incoming/table` (colunas confirmadas em 21/09: `ID do Pedido | Chegada | Tipo | Movimento | Cliente | Origem | unidade | Recebimento | Marcador | Situacao | Acoes`).
- Produces:
  - `stokki.recebimentos.listar_recebimentos(sessao, status="all", por_pagina=100, pagina=0) -> dict` (formato DataTables, mesmas chaves `aaData`/`iTotalRecords` do outbound).
  - `stokki.recebimentos.extrair_itens_do_recebimento(html) -> list[dict]` (mesmas chaves de `extrair_itens_do_pedido`).
  - `wms_pedidos.registrar_recebimento(conn, recebimento: dict, itens: list[dict]) -> int`.

- [ ] **Step 1: Descobrir os parametros reais do datatable de recebimento**

Igual foi feito com `lot/table`: ler a tela e ver o que o JS manda.

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python -c \"
import re, yaml, pathlib
from stokki.auth import StokkiSession, BASE_URL
cfg = yaml.safe_load(pathlib.Path('config.yaml').read_text(encoding='utf-8'))
s = StokkiSession(cfg)
h = s.get(f'{BASE_URL}/pt-br/administrator/inventory/incoming').text
m = re.search(r'incoming/table.{0,1500}', h, re.S)
print(re.sub(r'\s+',' ', m.group(0))[:1200] if m else 'nao achou')
\""
```

Anotar no topo de `stokki/recebimentos.py` os nomes de coluna e filtros que aparecerem, do jeito que `stokki/pedidos.py` documenta os dele. **Nao inventar parametros**: a Stokki devolve 500 quando o conjunto esta incompleto (ja aconteceu com `outbound/table`).

- [ ] **Step 2: Escrever o teste do parser com fixture**

Criar `stokki/fixtures/recebimento_itens.html` com o HTML real de um recebimento (copiar da VPS, como no Step 1 da Task 2) e `stokki/test_recebimentos.py` no mesmo formato do `test_extrair_itens.py`, cobrindo:

```python
    def test_le_itens_do_recebimento(self):
        itens = extrair_itens_do_recebimento(self.html)
        self.assertTrue(itens)
        self.assertIn("sku", itens[0])
        self.assertIn("qtd_embalagem", itens[0])

    def test_recebimento_sem_itens_devolve_lista_vazia(self):
        self.assertEqual(extrair_itens_do_recebimento("<html></html>"), [])
```

- [ ] **Step 3: Rodar e ver falhar**

Rodar: `py -3.11 -m unittest stokki.test_recebimentos -v`
Esperado: FAIL com `ModuleNotFoundError: No module named 'stokki.recebimentos'`

- [ ] **Step 4: Implementar o modulo de recebimento**

`stokki/recebimentos.py` segue a estrutura de `stokki/pedidos.py`: constantes de URL no topo, `listar_recebimentos` montando **todos** os parametros do DataTables (copiar o conjunto descoberto no Step 1), e `extrair_itens_do_recebimento(html)` reusando a mesma busca de tabela por cabecalho de `extrair_itens_do_pedido` (procura a `<table>` cujos `<th>` tenham PRODUTO/EAN/QUANTIDADE).

Acrescentar em `painel_agentes/wms_pedidos.py`, dentro de `_DDL`:

```sql
CREATE TABLE IF NOT EXISTS wms_recebimentos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    id_stokki      INTEGER NOT NULL UNIQUE,
    embarcador     TEXT NOT NULL DEFAULT '',
    situacao       TEXT NOT NULL DEFAULT '',
    chegada        TEXT NOT NULL DEFAULT '',
    estado         TEXT NOT NULL DEFAULT 'ESPERADO',  -- ESPERADO | ENDERECADO
    lido_em        TEXT NOT NULL,
    atualizado_em  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wms_recebimento_itens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recebimento_id  INTEGER NOT NULL REFERENCES wms_recebimentos(id),
    linha           INTEGER NOT NULL,
    sku             TEXT NOT NULL DEFAULT '',
    ean_linha       TEXT NOT NULL DEFAULT '',
    descricao       TEXT NOT NULL DEFAULT '',
    qtd_embalagem   REAL NOT NULL,
    qtd_un          REAL,
    produto_id      INTEGER REFERENCES wms_produtos(id),
    qtd_enderecada  REAL NOT NULL DEFAULT 0,
    UNIQUE (recebimento_id, linha)
);
```

E `registrar_recebimento(conn, recebimento, itens)`, igual a `registrar_pedido` mas gravando nessas tabelas (reusa `resolver_item` pra preencher `produto_id`/`qtd_un`).

- [ ] **Step 5: Rodar e ver passar**

Rodar: `py -3.11 -m unittest stokki.test_recebimentos painel_agentes.test_wms_pedidos -v`
Esperado: PASS

- [ ] **Step 6: Escrever a rotina de lote do recebimento**

Criar `sincronizar_recebimentos_wms.py`:

```python
# -*- coding: utf-8 -*-
"""
sincronizar_recebimentos_wms.py

Le os recebimentos do embarcador piloto na Stokki e cria a entrada
esperada no WMS. Quem endereca de fato e o operador, pela tela do
celular: aqui so nasce a lista do que deve chegar.

COMO USAR (timer systemd, infra/stokki-wms-recebimentos.*):
    python sincronizar_recebimentos_wms.py
    python sincronizar_recebimentos_wms.py --modo-teste
"""
import argparse
import logging
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

import wms_pedidos
from stokki import recebimentos as stokki_recebimentos
from stokki.auth import StokkiSession

logger = logging.getLogger("sincronizar_recebimentos_wms")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def rodar(conn, sess, piloto: str, limite: int, modo_teste: bool) -> dict:
    res = {"lidos": 0, "do_piloto": 0, "gravados": 0, "erros": 0}
    resposta = stokki_recebimentos.listar_recebimentos(sess, por_pagina=limite)
    for linha in (resposta.get("aaData") or []):
        res["lidos"] += 1
        dados = stokki_recebimentos.extrair_cabecalho_da_linha(linha)
        if piloto not in (dados.get("embarcador") or "").upper():
            continue
        res["do_piloto"] += 1
        if modo_teste:
            logger.info("[teste] recebimento %s (%s) seria gravado",
                        dados.get("id_stokki"), (dados.get("embarcador") or "")[:40])
            continue
        try:
            # Leitura HTTP fora da transacao; commit por recebimento, porque
            # o dados.db e compartilhado com o painel e todos os agentes.
            itens = stokki_recebimentos.ler_itens(sess, dados["id_stokki"])
            if not itens:
                logger.warning("Recebimento %s sem itens legiveis -- pulando.", dados["id_stokki"])
                res["erros"] += 1
                continue
            wms_pedidos.registrar_recebimento(conn, dados, itens)
            conn.commit()
            res["gravados"] += 1
        except Exception as e:  # noqa: BLE001 -- um recebimento ruim nao derruba a rodada
            conn.rollback()
            logger.warning("Recebimento %s falhou: %s", dados.get("id_stokki"), e)
            res["erros"] += 1
        time.sleep(0.3)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modo-teste", action="store_true", help="nao grava nada, so mostra o que faria")
    ap.add_argument("--limite", type=int, default=50, help="recebimentos nesta rodada")
    ap.add_argument("--embarcador", help="sobrescreve o embarcador piloto do config.yaml")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "wms_recebimentos.log", encoding="utf-8")])
    config = _carregar_config()
    piloto = (args.embarcador or (config.get("wms", {}) or {}).get(
        "embarcador_piloto", "MARIA DOLORES")).strip().upper()
    conn = wms_pedidos.conectar()
    sess = StokkiSession(config)
    t0 = time.time()
    try:
        res = rodar(conn, sess, piloto, args.limite, args.modo_teste)
    finally:
        conn.close()
    logger.info("Piloto %s: %s (%.0fs)", piloto, res, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Isso exige mais duas funcoes em `stokki/recebimentos.py`, implementadas no Step 4:
`extrair_cabecalho_da_linha(linha) -> dict` (com `id_stokki`, `codigo`, `embarcador`, `situacao`, `chegada`, lidos das colunas do DataTables descobertas no Step 1) e
`ler_itens(sessao, id_recebimento) -> list[dict]` (GET no `show/{id}` do incoming + `extrair_itens_do_recebimento`).

- [ ] **Step 7: Criar as units do recebimento**

`infra/stokki-wms-recebimentos.service`:

```ini
[Unit]
Description=Stokki Eventos - Espelha os recebimentos do embarcador piloto como entrada esperada no WMS
OnFailure=stokki-alerta-falha@%n.service

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/opt/stokki-eventos
ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/sincronizar_recebimentos_wms.py
```

`infra/stokki-wms-recebimentos.timer`:

```ini
[Unit]
Description=Dispara stokki-wms-recebimentos.service a cada 30 min (deslocado dos outros syncs)

[Timer]
OnCalendar=*-*-* *:09/30:00
Persistent=false
AccuracySec=1min

[Install]
WantedBy=timers.target
```

Provar com `--modo-teste` na VPS antes de qualquer gravacao:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python sincronizar_recebimentos_wms.py --modo-teste --limite 20"
```

- [ ] **Step 8: Commit**

```bash
git add stokki/recebimentos.py stokki/test_recebimentos.py stokki/fixtures/recebimento_itens.html sincronizar_recebimentos_wms.py infra/stokki-wms-recebimentos.service infra/stokki-wms-recebimentos.timer painel_agentes/wms_pedidos.py
git commit -m "WMS: recebimento puxado da Stokki vira entrada esperada"
```

---

### Task 9: Etiqueta de produto com QR de lote e validade

**Files:**
- Create: `painel_agentes/wms_etiqueta_produto.py`
- Create: `painel_agentes/test_wms_etiqueta_produto.py`
- Modify: `painel_agentes/wms.py` (`ler_codigo` entende o QR novo)

**Interfaces:**
- Consumes: `wms._fonte`, `wms._qr_imagem`, `wms._ajustar_fonte`, `wms._aplicar_margem`, `wms._LARGURA_PX`, `wms._ALTURA_PX`, `wms._validar_validade`.
- Produces:
  - `conteudo_qr(sku: str, lote: str, validade: str | None) -> str` → `"FL|SKU|LOTE|AAAA-MM-DD"`.
  - `parse_qr(texto: str) -> dict | None` → `{"sku", "lote", "validade"}` ou `None`.
  - `desenhar_etiqueta_produto(dados: dict)` → imagem PIL. `dados`: `descricao`, `sku`, `lote`, `validade`, `embarcador`.
  - `gerar_etiquetas_produto_pdf(conn, produto_id, lote, validade, copias, orientacao="paisagem", margem_mm=0, desloc_x_mm=0, desloc_y_mm=0) -> bytes`.
  - Em `wms.py`: `PREFIXO_QR_PRODUTO = "FL|"` e `ler_codigo` devolvendo `{"tipo": "produto_lote", "produtos": [...], "lote": str, "validade": str}`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `painel_agentes/test_wms_etiqueta_produto.py`:

```python
# -*- coding: utf-8 -*-
"""
test_wms_etiqueta_produto.py

Etiqueta de mercadoria com QR de lote e validade (WMS fase 2).
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_wms_etiqueta_produto -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_etiqueta_produto as etq  # noqa: E402
import wms_pedidos  # noqa: E402


class TestConteudoQR(unittest.TestCase):
    def test_monta_o_texto_do_qr(self):
        self.assertEqual(etq.conteudo_qr("72400017", "L-A", "2026-10-15"),
                         "FL|72400017|L-A|2026-10-15")

    def test_sem_validade_deixa_o_campo_vazio(self):
        self.assertEqual(etq.conteudo_qr("72400017", "L-A", None), "FL|72400017|L-A|")

    def test_parse_devolve_os_tres_campos(self):
        self.assertEqual(etq.parse_qr("FL|72400017|L-A|2026-10-15"),
                         {"sku": "72400017", "lote": "L-A", "validade": "2026-10-15"})

    def test_parse_ignora_o_que_nao_e_nosso(self):
        self.assertIsNone(etq.parse_qr("POS:C9-E1-N1"))
        self.assertIsNone(etq.parse_qr("7891234567890"))

    def test_ida_e_volta(self):
        texto = etq.conteudo_qr("SKU1", "LOTE 2", "2027-01-31")
        self.assertEqual(etq.parse_qr(texto)["lote"], "LOTE 2")


class TestLerCodigo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = wms_pedidos.conectar(Path(self._tmp.name) / "dados.db")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, '72400017', 'COXINHA', 'MARIA DOLORES', "
            "'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_bipar_a_etiqueta_traz_produto_lote_e_validade(self):
        r = wms.ler_codigo(self.conn, "FL|72400017|L-A|2026-10-15")
        self.assertEqual(r["tipo"], "produto_lote")
        self.assertEqual(r["produtos"][0]["id"], 1)
        self.assertEqual(r["lote"], "L-A")
        self.assertEqual(r["validade"], "2026-10-15")

    def test_etiqueta_de_produto_que_nao_existe(self):
        r = wms.ler_codigo(self.conn, "FL|NAOEXISTE|L-A|2026-10-15")
        self.assertEqual(r["tipo"], "desconhecido")


class TestPDF(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = wms_pedidos.conectar(Path(self._tmp.name) / "dados.db")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA ZC 5KG PCT', 'MARIA DOLORES', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_gera_pdf_valido(self):
        pdf = etq.gerar_etiquetas_produto_pdf(self.conn, 1, "L-A", "2026-10-15", copias=1)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)

    def test_mais_copias_geram_pdf_maior(self):
        # Contar '/Type /Page' no PDF nao serve: a arvore de paginas usa
        # '/Type /Pages', que contem a mesma substring. Comparar tamanho e
        # o jeito honesto de provar que as copias sairam.
        um = etq.gerar_etiquetas_produto_pdf(self.conn, 1, "L-A", "2026-10-15", copias=1)
        tres = etq.gerar_etiquetas_produto_pdf(self.conn, 1, "L-A", "2026-10-15", copias=3)
        self.assertGreater(len(tres), len(um))

    def test_produto_inexistente_da_erro_claro(self):
        with self.assertRaises(wms.ErroWMS):
            etq.gerar_etiquetas_produto_pdf(self.conn, 999, "L-A", "2026-10-15")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_etiqueta_produto -v`
Esperado: FAIL com `ModuleNotFoundError: No module named 'wms_etiqueta_produto'`

- [ ] **Step 3: Implementar a etiqueta**

Criar `painel_agentes/wms_etiqueta_produto.py`:

```python
# -*- coding: utf-8 -*-
"""
painel_agentes/wms_etiqueta_produto.py

Etiqueta de mercadoria (10 × 5 cm) com QR de SKU, lote e validade.
Decisao do Hugo (21/09/2026): sem serial -- a etiqueta vale pra qualquer
caixa daquele lote, entao da pra imprimir um bolo e colar em tudo.

O layout reusa as medidas e as fontes da etiqueta de posicao, que ja esta
calibrada na Elgin L42 Pro Full (midia deitada, config wms.etiqueta_* da
VPS).
"""
import wms

PREFIXO = "FL|"


def conteudo_qr(sku: str, lote: str, validade: str | None) -> str:
    """Texto que vai dentro do QR: FL|SKU|LOTE|AAAA-MM-DD."""
    return f"{PREFIXO}{str(sku or '').strip()}|{str(lote or '').strip()}|{str(validade or '').strip()}"


def parse_qr(texto: str) -> dict | None:
    """Le o QR da nossa etiqueta. Devolve None pro que nao for nosso."""
    bruto = str(texto or "").strip()
    if not bruto.upper().startswith(PREFIXO):
        return None
    partes = bruto[len(PREFIXO):].split("|")
    if len(partes) < 3:
        return None
    return {"sku": partes[0].strip(), "lote": partes[1].strip(), "validade": partes[2].strip()}


def desenhar_etiqueta_produto(dados: dict):
    """Imagem PIL (modo L, 1181×591 @300dpi) da etiqueta de mercadoria."""
    from PIL import Image, ImageDraw
    W, H = wms._LARGURA_PX, wms._ALTURA_PX
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)

    # QR a esquerda
    lado = H - 70
    qr = wms._qr_imagem(conteudo_qr(dados.get("sku"), dados.get("lote"), dados.get("validade")), lado)
    img.paste(qr, (30, 35))

    x0 = 30 + lado + 40
    largura = W - x0 - 30
    descricao = (dados.get("descricao") or "").upper()[:60]
    f_desc = wms._ajustar_fonte(draw, descricao, largura, 54, negrito=True, condensada=True)
    draw.text((x0, 40), descricao, fill=0, font=f_desc)

    linha_lote = f"LOTE {dados.get('lote') or '-'}"
    f_lote = wms._ajustar_fonte(draw, linha_lote, largura, 92, condensada=True)
    draw.text((x0, 40 + f_desc.size + 25), linha_lote, fill=0, font=f_lote)

    validade = dados.get("validade") or ""
    if validade:
        try:
            from datetime import date
            validade = date.fromisoformat(validade).strftime("%d/%m/%Y")
        except ValueError:
            pass
    linha_val = f"VAL {validade}" if validade else "SEM VALIDADE"
    f_val = wms._ajustar_fonte(draw, linha_val, largura, 92, condensada=True)
    draw.text((x0, 40 + f_desc.size + 25 + f_lote.size + 15), linha_val, fill=0, font=f_val)

    rodape = f"{dados.get('sku') or '-'}  ·  {(dados.get('embarcador') or '')[:28]}  ·  Freshlog"
    f_rod = wms._ajustar_fonte(draw, rodape, largura, 30, negrito=False)
    draw.text((x0, H - 75), rodape, fill=0, font=f_rod)
    return img


def gerar_etiquetas_produto_pdf(conn, produto_id: int, lote: str, validade: str | None, copias: int = 1,
                                orientacao: str = "paisagem", margem_mm: float = 0,
                                desloc_x_mm: float = 0, desloc_y_mm: float = 0) -> bytes:
    """PDF com uma etiqueta por pagina, no tamanho exato da midia termica."""
    import io

    produto = wms.obter_produto(conn, produto_id)
    if not produto:
        raise wms.ErroWMS("Produto nao encontrado.")
    validade = wms._validar_validade(validade)
    copias = max(1, min(int(copias or 1), 200))
    dados = {"descricao": produto["descricao"], "sku": produto["sku"], "lote": lote,
             "validade": validade, "embarcador": produto["embarcador"]}
    arte = desenhar_etiqueta_produto(dados)
    if orientacao == "retrato":
        arte = arte.rotate(90, expand=True)
    elif orientacao == "retrato-inv":
        arte = arte.rotate(270, expand=True)
    pagina = wms._aplicar_margem(arte, margem_mm, desloc_x_mm, desloc_y_mm)
    paginas = [pagina.convert("L")] * copias
    buf = io.BytesIO()
    paginas[0].save(buf, format="PDF", save_all=True, append_images=paginas[1:], resolution=wms._DPI)
    return buf.getvalue()
```

Em `painel_agentes/wms.py`, junto das outras constantes do topo:

```python
PREFIXO_QR_PRODUTO = "FL|"
```

E no comeco de `ler_codigo`, logo depois do teste de vazio:

```python
    if bruto.upper().startswith(PREFIXO_QR_PRODUTO):
        import wms_etiqueta_produto
        lido = wms_etiqueta_produto.parse_qr(bruto)
        if lido:
            produtos = buscar_por_codigo(conn, lido["sku"])
            if not produtos:
                return {"tipo": "desconhecido", "codigo": bruto, "parece_ean": False}
            for p in produtos:
                p["saldos"] = saldos_produto(conn, p["id"])
                p["total"] = round(sum(s["quantidade"] for s in p["saldos"]), 3)
            return {"tipo": "produto_lote", "produtos": produtos, "codigo": bruto,
                    "lote": lido["lote"], "validade": lido["validade"]}
```

- [ ] **Step 4: Rodar e ver passar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_etiqueta_produto painel_agentes.test_wms_pedidos -v`
Esperado: PASS

- [ ] **Step 5: Conferir a etiqueta a olho antes de imprimir**

```bash
py -3.11 -c "import sys; sys.path[:0]=['painel_agentes','.']; import wms_pedidos, wms_etiqueta_produto as e; \
c=wms_pedidos.conectar(); \
open('C:/Users/HUGOMA~1/AppData/Local/Temp/claude/etiqueta.pdf','wb').write(e.gerar_etiquetas_produto_pdf(c, 1, 'L-TESTE', '2026-12-31', 2))"
```

Abrir o PDF e conferir: QR legivel, lote e validade em fonte grande, nada cortado. **Imprimir em 100% com papel 100×50** (a calibracao de 04/09 depende disso).

- [ ] **Step 6: Commit**

```bash
git add painel_agentes/wms_etiqueta_produto.py painel_agentes/test_wms_etiqueta_produto.py painel_agentes/wms.py
git commit -m "WMS: etiqueta de mercadoria com QR de SKU, lote e validade"
```

---

### Task 10: Telas e rotas

**Files:**
- Modify: `painel_agentes/painel_agentes.py` (rotas novas depois da linha 2690, no bloco `/api/wms/*`)
- Modify: `painel_agentes/templates/wms.html` (telas do operador)
- Create: `painel_agentes/templates/wms_estoque.html` (tela interna da equipe)

**Interfaces:**
- Consumes: `wms_pedidos.*` (Tasks 1–8), `wms_etiqueta_produto.*` (Task 9), decoradores `@requer_auth(niveis=_NIVEIS_WMS)` e `@exige_mesma_origem` ja usados nas rotas `/api/wms/*`.
- Produces: rotas `GET /api/wms/pedidos`, `GET /api/wms/pedidos/<int:pedido_id>`, `POST /api/wms/reservas/<int:reserva_id>/trocar-lote`, `GET /api/wms/pendencias`, `GET /api/wms/etiqueta-produto.pdf`, `GET /painel/wms/estoque`.

- [ ] **Step 1: Escrever o teste da troca de lote**

Acrescentar em `painel_agentes/test_wms_pedidos.py`:

```python
class TestTrocarLote(BaseWMS):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, ean, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, 'SKU1', 'PRODUTO 1', 'MARIA DOLORES', "
            "'111111111111', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5,
                                lote="L-A", validade="2026-10-15", destino="C9-E1-N1")
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=1, quantidade=5,
                                lote="L-B", validade="2026-12-31", destino="C9-E1-N2")
        self.pid = wms_pedidos.registrar_pedido(
            self.conn,
            {"id_stokki": 39751, "codigo_ps": "PS-39751", "embarcador": "MARIA DOLORES",
             "situacao": "Separating"},
            [{"linha": 1, "sku": "SKU1", "ean_linha": "111111111111",
              "descricao": "PRODUTO 1", "qtd_embalagem": 3}])
        wms_pedidos.reservar_pedido(self.conn, self.pid)

    def test_operador_pegou_outro_lote(self):
        reserva = self.conn.execute("SELECT * FROM wms_reservas WHERE estado='ATIVA'").fetchone()
        self.assertEqual(reserva["lote"], "L-A")  # FEFO sugeriu o que vence antes
        nova = wms_pedidos.trocar_lote_reserva(self.conn, reserva["id"], "C9-E1-N2", "L-B", "2026-12-31")
        self.assertEqual(nova["lote"], "L-B")
        self.assertEqual(nova["origem"], "MANUAL")
        self.assertEqual(nova["quantidade_un"], 3)
        ativas = self.conn.execute("SELECT COUNT(*) n FROM wms_reservas WHERE estado='ATIVA'").fetchone()["n"]
        self.assertEqual(ativas, 1)

    def test_trocar_pra_lote_sem_saldo_avisa_e_nao_troca(self):
        reserva = self.conn.execute("SELECT * FROM wms_reservas WHERE estado='ATIVA'").fetchone()
        with self.assertRaises(wms.ErroWMS):
            wms_pedidos.trocar_lote_reserva(self.conn, reserva["id"], "C9-E1-N2", "L-FANTASMA", "2027-01-01")
```

- [ ] **Step 2: Rodar e ver falhar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos.TestTrocarLote -v`
Esperado: FAIL com `AttributeError: module 'wms_pedidos' has no attribute 'trocar_lote_reserva'`

- [ ] **Step 3: Implementar a troca de lote**

Em `painel_agentes/wms_pedidos.py`:

```python
def trocar_lote_reserva(conn, reserva_id: int, posicao: str, lote: str, validade: str | None) -> dict:
    """
    O operador pegou outro lote do que o FEFO sugeriu. Cancela a reserva
    sugerida e cria uma MANUAL no lote que ele bipou, se houver disponivel.
    """
    r = conn.execute("SELECT * FROM wms_reservas WHERE id = ?", (int(reserva_id),)).fetchone()
    if not r:
        raise wms.ErroWMS("Reserva nao encontrada.")
    if r["estado"] != "ATIVA":
        raise wms.ErroWMS(f"Reserva ja esta {r['estado'].lower()}.")
    posicao = wms.normalizar_codigo(posicao)
    lote = " ".join(str(lote or "").split()).upper()
    validade = wms._validar_validade(validade) or ""
    alvo = [d for d in disponivel_por_lote(conn, r["produto_id"])
            if d["posicao"] == posicao and d["lote"] == lote and d["validade"] == validade]
    disponivel = alvo[0]["disponivel"] if alvo else 0
    if disponivel < r["quantidade_un"]:
        raise wms.ErroWMS(
            f"Lote {lote or '(sem lote)'} em {posicao} tem {disponivel:g} UN disponivel, "
            f"menos que os {r['quantidade_un']:g} UN da reserva.")
    agora = wms.agora()
    conn.execute("UPDATE wms_reservas SET estado='CANCELADA', atualizado_em=? WHERE id=?", (agora, r["id"]))
    cur = conn.execute("""
        INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade,
                                  quantidade_un, estado, origem, criado_em, atualizado_em)
        VALUES (?,?,?,?,?,?,?,'ATIVA','MANUAL',?,?)""",
        (r["pedido_id"], r["item_id"], r["produto_id"], posicao, lote, validade,
         r["quantidade_un"], agora, agora))
    conn.commit()
    return dict(conn.execute("SELECT * FROM wms_reservas WHERE id = ?", (cur.lastrowid,)).fetchone())


def listar_pedidos_wms(conn, estado: str | None = None, limite: int = 50) -> list[dict]:
    """Pedidos espelhados, com contagem de itens e reservas -- pra tela."""
    sql = """
        SELECT p.*,
               (SELECT COUNT(*) FROM wms_pedido_itens i WHERE i.pedido_id = p.id) AS itens,
               (SELECT COUNT(*) FROM wms_reservas r WHERE r.pedido_id = p.id AND r.estado='ATIVA') AS reservas_ativas,
               (SELECT COUNT(*) FROM wms_pedido_itens i WHERE i.pedido_id = p.id AND i.motivo_pendencia <> '') AS pendencias
          FROM wms_pedidos p {onde} ORDER BY p.id DESC LIMIT ?"""
    if estado:
        rows = conn.execute(sql.format(onde="WHERE p.estado_reserva = ?"), (estado, int(limite))).fetchall()
    else:
        rows = conn.execute(sql.format(onde=""), (int(limite),)).fetchall()
    return [dict(r) for r in rows]


def separacao_do_pedido(conn, pedido_id: int) -> list[dict]:
    """Lista de separacao: uma linha por reserva, com posicao, lote e validade."""
    rows = conn.execute("""
        SELECT r.*, i.linha, i.descricao, i.sku, i.qtd_un AS qtd_item,
               pr.unidade, pr.qtd_por_caixa
          FROM wms_reservas r
          JOIN wms_pedido_itens i ON i.id = r.item_id
          JOIN wms_produtos pr ON pr.id = r.produto_id
         WHERE r.pedido_id = ? AND r.estado = 'ATIVA'
         ORDER BY r.posicao, i.linha""", (int(pedido_id),)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Rodar e ver passar**

Rodar: `py -3.11 -m unittest painel_agentes.test_wms_pedidos -v`
Esperado: PASS (29 testes)

- [ ] **Step 5: Criar as rotas**

Em `painel_agentes/painel_agentes.py`, depois da ultima rota `/api/wms/*` (hoje linha 2690), com `import wms_pedidos` e `import wms_etiqueta_produto` junto do `import wms` que ja existe no topo:

```python
@app.route("/api/wms/pedidos")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_pedidos():
    conn = wms_pedidos.conectar()
    try:
        return jsonify({"pedidos": wms_pedidos.listar_pedidos_wms(
            conn, estado=request.args.get("estado"), limite=request.args.get("limite", 50, type=int))})
    finally:
        conn.close()


@app.route("/api/wms/pedidos/<int:pedido_id>")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_pedido(pedido_id):
    conn = wms_pedidos.conectar()
    try:
        pedido = conn.execute("SELECT * FROM wms_pedidos WHERE id = ?", (pedido_id,)).fetchone()
        if not pedido:
            return jsonify({"erro": "Pedido nao encontrado."}), 404
        itens = [dict(r) for r in conn.execute(
            "SELECT * FROM wms_pedido_itens WHERE pedido_id = ? ORDER BY linha", (pedido_id,))]
        return jsonify({"pedido": dict(pedido), "itens": itens,
                        "separacao": wms_pedidos.separacao_do_pedido(conn, pedido_id)})
    finally:
        conn.close()


@app.route("/api/wms/reservas/<int:reserva_id>/trocar-lote", methods=["POST"])
@requer_auth(niveis=_NIVEIS_WMS)
@exige_mesma_origem
def api_wms_trocar_lote(reserva_id):
    _wms_exige_operador()
    body = request.get_json(force=True) or {}
    conn = wms_pedidos.conectar()
    try:
        nova = wms_pedidos.trocar_lote_reserva(
            conn, reserva_id, body.get("posicao"), body.get("lote"), body.get("validade"))
    except (wms.ErroWMS, ValueError) as e:
        return _wms_json_erro(e)
    finally:
        conn.close()
    return jsonify({"ok": True, "reserva": nova})


@app.route("/api/wms/pendencias")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_pendencias():
    conn = wms_pedidos.conectar()
    try:
        return jsonify({"pendencias": wms_pedidos.pendencias(
            conn, request.args.get("limite", 50, type=int))})
    finally:
        conn.close()


@app.route("/api/wms/etiqueta-produto.pdf")
@requer_auth(niveis=_NIVEIS_WMS)
def api_wms_etiqueta_produto():
    conn = wms_pedidos.conectar()
    try:
        pdf = wms_etiqueta_produto.gerar_etiquetas_produto_pdf(
            conn,
            request.args.get("produto_id", type=int),
            request.args.get("lote", ""),
            request.args.get("validade") or None,
            copias=request.args.get("copias", 1, type=int),
            orientacao=request.args.get("orientacao", _wms_config().get("etiqueta_orientacao", "paisagem")),
            margem_mm=request.args.get("margem", _wms_config().get("etiqueta_margem_mm", 0), type=float),
            desloc_x_mm=request.args.get("dx", _wms_config().get("etiqueta_dx_mm", 0), type=float),
            desloc_y_mm=request.args.get("dy", _wms_config().get("etiqueta_dy_mm", 0), type=float))
    except (wms.ErroWMS, ValueError) as e:
        return _wms_json_erro(e)
    finally:
        conn.close()
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": "inline; filename=etiqueta-produto.pdf"})
```

Se `_wms_config()` nao existir com esse nome, usar o mesmo helper que a rota de etiqueta de posicao ja usa pra ler `wms.etiqueta_*` do config.yaml (conferir na rota `/api/wms/etiquetas.pdf` existente e reaproveitar — **nao duplicar** a leitura de config).

- [ ] **Step 6: Tela da equipe**

A rota usa `niveis=("total", "operador", "leitura")` — equipe interna. **Nao** usar `_NIVEIS_WMS`: o nivel `galpao` e login fixo do aparelho compartilhado e so enxerga `/wms` (D11).

Em `painel_agentes/painel_agentes.py`:

```python
@app.route("/wms/estoque")
@requer_auth(niveis=("total", "operador", "leitura"))
def wms_estoque():
    return render_template("wms_estoque.html")
```

Criar `painel_agentes/templates/wms_estoque.html` (as telas internas estendem `base.html`, ao contrario do `wms.html`, que e PWA standalone):

```html
{% extends "base.html" %}
{% block titulo %}Estoque do Galpão{% endblock %}

{% block estilo_extra %}
<style>
  main { max-width: 1200px; }
  .chip { padding: 2px 10px; border-radius: 999px; font-size: .78rem; font-weight: 600; }
  .chip-reservado { background: var(--verde-fraco); color: var(--verde-forte); }
  .chip-parcial   { background: var(--ambar-fraco); color: var(--ambar-forte); }
  .chip-baixado   { background: var(--cinza-fraco); color: var(--cinza-forte); }
  .chip-cancelado { background: var(--vermelho-fraco); color: var(--vermelho-forte); }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 8px 10px; border-bottom: 1px solid var(--borda); text-align: left; }
  .vazio { color: var(--texto-fraco); padding: 16px 0; }
</style>
{% endblock %}

{% block conteudo %}
<h1>Estoque do Galpão</h1>

<section>
  <h2>Pedidos com reserva</h2>
  <table id="tab-pedidos"><tbody></tbody></table>
  <p class="vazio" id="sem-pedidos" hidden>Nenhum pedido espelhado ainda.</p>
</section>

<section>
  <h2>Pendências <small>(itens que não viraram reserva)</small></h2>
  <table id="tab-pendencias"><tbody></tbody></table>
  <p class="vazio" id="sem-pendencias" hidden>Nenhuma pendência — todos os itens resolveram.</p>
</section>

<section>
  <h2>Saldo por lote</h2>
  <input id="busca-produto" type="search" placeholder="Produto, SKU ou EAN" autocomplete="off">
  <table id="tab-saldo"><tbody></tbody></table>
</section>
{% endblock %}

{% block script_extra %}
<script>
const CHIP = {RESERVADO: "chip-reservado", PARCIAL: "chip-parcial",
              BAIXADO: "chip-baixado", CANCELADO: "chip-cancelado", PENDENTE: "chip-baixado"};

async function carregarPedidos() {
  const r = await fetch("{{ url_for('api_wms_pedidos') }}", {credentials: "same-origin"});
  const {pedidos} = await r.json();
  const corpo = document.querySelector("#tab-pedidos tbody");
  corpo.innerHTML = pedidos.map(p => `
    <tr><td><strong>${p.codigo_ps}</strong></td>
        <td>${p.embarcador}</td>
        <td><span class="chip ${CHIP[p.estado_reserva] || ""}">${p.estado_reserva}</span></td>
        <td>${p.reservas_ativas}/${p.itens} itens</td>
        <td>${p.pendencias ? p.pendencias + " pendência(s)" : ""}</td></tr>`).join("");
  document.getElementById("sem-pedidos").hidden = pedidos.length > 0;
}

async function carregarPendencias() {
  const r = await fetch("{{ url_for('api_wms_pendencias') }}", {credentials: "same-origin"});
  const {pendencias} = await r.json();
  const corpo = document.querySelector("#tab-pendencias tbody");
  corpo.innerHTML = pendencias.map(p => `
    <tr><td>${p.codigo_ps}</td><td>linha ${p.linha}</td>
        <td>${p.descricao}</td><td>${p.motivo_pendencia}</td></tr>`).join("");
  document.getElementById("sem-pendencias").hidden = pendencias.length > 0;
}

document.getElementById("busca-produto").addEventListener("change", async (ev) => {
  const q = ev.target.value.trim();
  if (!q) return;
  const r = await fetch("{{ url_for('api_wms_ler') }}?codigo=" + encodeURIComponent(q),
                        {credentials: "same-origin"});
  const dados = await r.json();
  const produto = (dados.produtos || [])[0];
  const corpo = document.querySelector("#tab-saldo tbody");
  if (!produto) { corpo.innerHTML = "<tr><td>Produto não encontrado.</td></tr>"; return; }
  const s = await (await fetch("{{ url_for('api_wms_produto_saldo', produto_id=0) }}".replace("/0", "/" + produto.id),
                               {credentials: "same-origin"})).json();
  corpo.innerHTML = (s.lotes || []).map(l => `
    <tr><td>${l.posicao}</td><td>${l.lote || "—"}</td><td>${l.validade || "—"}</td>
        <td>${l.saldo}</td><td>${l.reservado}</td><td><strong>${l.disponivel}</strong></td></tr>`).join("");
});

carregarPedidos();
carregarPendencias();
</script>
{% endblock %}
```

Isso exige mais uma rota, que devolve o disponivel por lote de um produto:

```python
@app.route("/api/wms/produtos/<int:produto_id>/saldo")
@requer_auth(niveis=("total", "operador", "leitura", "galpao"))
def api_wms_produto_saldo(produto_id):
    conn = wms_pedidos.conectar()
    try:
        return jsonify({"lotes": wms_pedidos.disponivel_por_lote(conn, produto_id)})
    finally:
        conn.close()
```

Conferir no `base.html` os nomes reais dos blocos (`conteudo`, `estilo_extra`, `script_extra`) e das variaveis de cor da paleta antes de colar — se diferirem, usar os do arquivo, nao os daqui.

- [ ] **Step 7: Telas do operador**

Em `painel_agentes/templates/wms.html`, acrescentar duas abas no mesmo padrao das existentes:

- **Receber** — lista os recebimentos esperados (Task 8); ao abrir um, o operador bipa o produto, digita lote/validade/quantidade e a posicao, e o POST vai pro `/api/wms/movimentos` que ja existe (tipo ENTRADA). Botao "Imprimir etiquetas" abre `/api/wms/etiqueta-produto.pdf?produto_id=..&lote=..&validade=..&copias=..`.
- **Separar** — lista os pedidos `RESERVADO`/`PARCIAL`; ao abrir um, mostra a separacao com posicao/lote/validade sugeridos. Bipar um QR `FL|...` diferente do sugerido chama `/api/wms/reservas/<id>/trocar-lote`.

Lembrete da fase 1: **template so recarrega com restart do waitress**, e a tela nunca recarrega HTML sozinha — no teste local, reiniciar o processo depois de editar.

- [ ] **Step 8: Provar no painel local, em outra porta**

```bash
cd painel_agentes && py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"
```

Abrir `http://127.0.0.1:8099/wms/estoque`, conferir que as tres secoes carregam sem erro no console. **Nunca** `Stop-Process` num PID que ja existia antes da sessao (o painel da porta 8070 pode estar rodando de verdade).

- [ ] **Step 9: Commit**

```bash
git add painel_agentes/painel_agentes.py painel_agentes/wms_pedidos.py painel_agentes/test_wms_pedidos.py painel_agentes/templates/wms.html painel_agentes/templates/wms_estoque.html
git commit -m "WMS: telas de recebimento, separacao e estoque por lote"
```

---

### Task 11: Deploy e prova em producao

**Files:** nenhum arquivo novo — e a etapa de por no ar.

- [ ] **Step 1: Conferir o estado da VPS antes**

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && git status --short && git log --oneline -3"
```

Se houver coisa nao commitada la que voce nao fez, **parar e falar com o Hugo** — e de outra sessao.

- [ ] **Step 2: Config do piloto**

No `config.yaml` **da VPS** (nunca no local, nunca colar valores no chat), acrescentar na secao `wms`:

```yaml
wms:
  embarcador_piloto: "MARIA DOLORES"
```

- [ ] **Step 3: Deploy**

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && git pull --no-rebase && chown -R www-data:www-data /opt/stokki-eventos && \
  cp infra/stokki-wms-reservar-pedidos.* infra/stokki-wms-recebimentos.* /etc/systemd/system/ && \
  systemctl daemon-reload && systemctl enable --now stokki-wms-reservar-pedidos.timer stokki-wms-recebimentos.timer && \
  systemctl restart painel-agentes"
```

- [ ] **Step 4: Provar de verdade (nao basta `is-active`)**

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python sincronizar_pedidos_wms.py --modo-teste --limite 10 && \
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8071/wms/estoque"
```

Esperado: a rodada de teste lista pedidos da Maria Dolores e o painel responde (302 pra login conta como no ar).

Lembrete: `Type=oneshot` fica em `activating` enquanto roda. Pra esperar terminar, usar `systemctl show -p ActiveState --value <unit>` — `is-active --quiet` mente.

- [ ] **Step 5: O teste que importa**

Nada disso esta pronto enquanto **um pedido real da Maria Dolores** nao passar pelo ciclo inteiro (criterio da secao 12 do spec):

1. Endereçar no galpao alguns produtos dela, com lote e validade, pela tela do celular.
2. Esperar (ou disparar) a rodada de reserva e ver o pedido virar `RESERVADO` em `/wms/estoque`.
3. Separar pelo celular.
4. Depois da expedicao, conferir que nasceu `SAIDA` em `wms_movimentos` e que o saldo caiu:

```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python -c \"
import sys; sys.path[:0]=['painel_agentes','.']
import wms_pedidos
c = wms_pedidos.conectar()
print('pedidos:', [dict(r) for r in c.execute('SELECT codigo_ps, estado_reserva FROM wms_pedidos ORDER BY id DESC LIMIT 5')])
print('saidas:', c.execute(\\\"SELECT COUNT(*) FROM wms_movimentos WHERE tipo='SAIDA'\\\").fetchone()[0])
\""
```

Enquanto esse ciclo nao acontecer com pedido de verdade, a fase 2 nao esta pronta — foi exatamente o que faltou na fase 1.

---

## Ordem de execucao

Tasks 1 → 2 → 3 → 4 → 5 → 6 sao a espinha dorsal e devem sair nessa ordem (cada uma usa a anterior). A Task 7 depende de 2 e 5. A **Task 8 (recebimento) e a 9 (etiqueta) sao independentes entre si** e podem sair em paralelo depois da 3. A Task 10 depende de tudo; a 11 e o deploy.

Sem a Task 8 ou a 9 o ciclo ainda funciona com entrada manual — mas sem a Task 6 nao existe baixa, e sem a Task 5 nao existe reserva.
