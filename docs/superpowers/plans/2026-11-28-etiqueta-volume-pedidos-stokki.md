# Etiqueta de volume para todo pedido finalizado na Stokki — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O operador digita ou bipa o PS de qualquer pedido finalizado na Stokki, informa quantas caixas tem e imprime etiquetas 10×15 com um QR único por caixa.

**Architecture:** Módulo de dados novo `painel_agentes/expedicao_volumes.py` (sem Flask) com a tabela `expedicao_volumes`, ligada ao código base do pedido (`PS-<n>`). Reaproveita a fila `wms_fila_impressao` (tipo `VOLUME`, `10x15`) e a arte de `wms_etiqueta_volume.py`. Quatro rotas sob `/api/wms/etiquetar/*` e uma tela nova no app do galpão (`templates/wms.html`). O piloto (`wms_volumes`, QR de 4 campos) não muda.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db`), Pillow (já usado na arte), `unittest`. JS puro no `wms.html` (SPA do galpão).

**Spec:** `docs/superpowers/specs/2026-11-28-etiqueta-volume-pedidos-stokki-design.md`

## Global Constraints

- Trabalhar em worktree próprio a partir de `origin/master`, ramo `etiqueta-volume-stokki` (o working tree principal está sujo e 30 commits atrás). Commits locais no ramo fazem parte do plano; **push e deploy só quando o Hugo pedir**.
- `git add` só dos arquivos da tarefa; nunca `git add -A`. Commit no formato `Galpão: o que mudou e por quê`.
- Python: sempre `py -3.11`. Testes rodam **de dentro de `painel_agentes/`**: `py -3.11 -m unittest <modulo> -v`.
- Comentários e mensagens de erro do módulo de dados sem acento (padrão dos `wms_*.py`); texto de tela com acento.
- A tela, as rotas e `expedicao_volumes.py` nunca chamam a Stokki nem a Vuupt: só leem o nosso banco. A única leitura da Stokki é a rotina em lote da Tarefa 6 (só leitura, pela `StokkiSession`, que entra sozinha na fila das contas).
- Não alterar comportamento de `wms_separacao.fechar_volumes`, `reimprimir_volume`, `dados_do_volume` nem do QR `FV|PS|n|total` (4 campos). `test_wms_etiqueta_volume` e `test_wms_separacao` têm de continuar verdes.
- Tabelas de outros módulos (`nucleo_pedidos`, `vigia_stokki_abertos`, `volumes_conferidos`) podem não existir no banco: tabela ausente = fonte vazia, nunca erro.
- Limites da spec: quantidade de 1 a **300**; confirmação acima de **99**.
- QR novo: `FV|<PS>|<n>|<total>|<id>`; `<id>` = 8 caracteres do alfabeto `23456789ABCDEFGHJKLMNPQRSTUVWXYZ`.
- Nome da tabela `expedicao_volumes`: conferido livre na produção em 28/11.
- Falta de dado nunca impede imprimir; pedido desconhecido imprime com aviso.
- Atualizar `MAPA_DO_SISTEMA.txt` no commit da tarefa que cria arquivo/rota.

## Review Focus

1. **O operador bipa uma etiqueta (ou o leitor cola o QR inteiro) no campo do pedido** em vez de digitar o PS → o campo entende `FV|PS-45352|2|5|XXXXXXXX` e `FV|PS-45352|2|5` como `PS-45352`. (Tarefa 1, `test_codigo_base`)
2. **Toque duplo no Imprimir / reenvio por rede ruim** → uma só leva de etiquetas; a segunda chamada volta `repetido`. (Tarefa 2, `test_repetir_mesmo_total`)
3. **O total muda com o trabalho antigo ainda pendente na fila** → o trabalho antigo sai da fila e o PDF dele se recusa a imprimir etiqueta anulada. (Tarefa 2 `test_trocar_total`, Tarefa 3 `test_pdf_recusa_anulada`)
4. **Quantidade digitada torta** (`0`, `-1`, `5.5`, `"abc"`, vazio, `301`) → mensagem clara, nada gravado. (Tarefa 2, `test_quantidade_invalida`)
5. **Código que não é um pedido só** (`PS-1, PS-2`, `abc`, vazio) → "Numero de pedido invalido", nada gravado. (Tarefa 1, `test_codigo_base`)

---

## Estrutura de arquivos

| Arquivo | Papel |
|---|---|
| Criar `painel_agentes/expedicao_volumes.py` | tabela, código base, dados do pedido, etiquetar/reimprimir, lista de prontos |
| Criar `painel_agentes/test_expedicao_volumes.py` | testes de dados (tarefas 1 a 3) |
| Criar `painel_agentes/test_expedicao_volumes_tela.py` | testes das rotas (tarefa 4) |
| Modificar `painel_agentes/wms_separacao.py` (`garantir_tabelas`) | criar a tabela nova junto com as demais |
| Modificar `painel_agentes/wms_etiqueta_volume.py` (`gerar_pdf_volumes`) | aceitar o item de 5 campos |
| Modificar `painel_agentes/painel_agentes.py` | rotas `/api/wms/etiquetar/*` |
| Modificar `painel_agentes/templates/wms.html` | tela Etiquetar + entrada no Início |
| Modificar `roteirizacao/atualizar_volumes_conferidos.py` | modo `--prontos`: lê o total conferido dos pedidos prontos (tarefa 6) |
| Criar `roteirizacao/test_volumes_prontos.py` | testes do modo `--prontos` |
| Criar `infra/stokki-volumes-prontos.service` e `.timer` | rodada de hora em hora, 08h25 às 17h25 em dia útil |
| Modificar `MAPA_DO_SISTEMA.txt` | registrar módulo, tabela, rotas, tela e a rotina nova |

---

### Task 1: Tabela, código do pedido e dados da etiqueta

**Files:**
- Create: `painel_agentes/expedicao_volumes.py`
- Create: `painel_agentes/test_expedicao_volumes.py`
- Modify: `painel_agentes/wms_separacao.py` (função `garantir_tabelas`, ~linha 150)

**Interfaces:**
- Consumes: `wms.ErroWMS`, `wms_separacao.conectar(caminho)`.
- Produces:
  - `garantir_tabela(conn) -> None`
  - `codigo_base(texto) -> str` — devolve `PS-<digitos>`; levanta `wms.ErroWMS("Numero de pedido invalido: ...")`
  - `dados_do_pedido(conn, codigo: str) -> dict` com as chaves `codigo, conhecido (bool), embarcador, destinatario, endereco, cidade, uf, nf, cancelado (bool), sugestao (int | None)`; textos vazios = `""`.

Tabela:

```sql
CREATE TABLE IF NOT EXISTS expedicao_volumes (
    id_caixa     TEXT PRIMARY KEY,          -- o <id> do QR
    codigo       TEXT NOT NULL,             -- PS-<n>, pedido base
    numero       INTEGER NOT NULL,
    total        INTEGER NOT NULL,
    impressao_id INTEGER,
    reimpressoes INTEGER NOT NULL DEFAULT 0,
    criado_em    TEXT NOT NULL,
    criado_por   TEXT NOT NULL DEFAULT '',
    anulado_em   TEXT,
    anulado_por  TEXT
);
CREATE INDEX IF NOT EXISTS idx_expedicao_volumes_codigo ON expedicao_volumes (codigo, anulado_em);
```

- [ ] **Step 1: Escrever os testes que falham** em `test_expedicao_volumes.py` (cabeçalho e `sys.path` iguais aos de `test_wms_separacao.py`; `Base.setUp` abre `self.conn = sep.conectar(<tmp>/dados.db)`). As tabelas externas são criadas à mão no teste, só com as colunas usadas:

```python
import expedicao_volumes as ev

def _nucleo(conn):
    conn.execute("CREATE TABLE nucleo_pedidos (codigo TEXT PRIMARY KEY, destinatario_nome TEXT, endereco TEXT, "
                 "remetente_nome TEXT, status TEXT, excluido_em TEXT)")
def _vigia(conn):
    conn.execute("CREATE TABLE vigia_stokki_abertos (codigo TEXT PRIMARY KEY, id_stokki INTEGER, embarcador TEXT, "
                 "status_stokki TEXT, fonte TEXT, primeira_vez_em TEXT, ultima_vez_em TEXT)")
def _conferidos(conn):
    conn.execute("CREATE TABLE volumes_conferidos (id_pedido INTEGER PRIMARY KEY, volumes INTEGER, fonte TEXT, lido_em TEXT)")

class TestCodigo(Base):
    def test_codigo_base(self):
        for entrada in ("#ps-45352", "45352", " PS-45352-R1 ", "ps45352",
                        "FV|PS-45352|2|5|ABCD2345", "FV|PS-45352|2|5"):
            self.assertEqual(ev.codigo_base(entrada), "PS-45352")
        for ruim in ("", None, "abc", "PS-", "PS-1, PS-2", "FV|X|1|1"):
            with self.assertRaises(wms.ErroWMS):
                ev.codigo_base(ruim)

class TestDadosDoPedido(Base):
    def test_sem_nenhuma_fonte_nao_quebra(self):
        d = ev.dados_do_pedido(self.conn, "PS-45352")
        self.assertEqual((d["conhecido"], d["cancelado"], d["sugestao"], d["destinatario"]), (False, False, None, ""))

    def test_fontes_em_ordem_e_completando(self):
        _nucleo(self.conn); _vigia(self.conn)
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-45352','Mercado Bom','Rua A, 10 - Barueri/SP','FABENNE','ABERTO',NULL)")
        self.conn.execute("INSERT INTO wms_pedidos (id_stokki, codigo_ps, embarcador, situacao, lido_em, atualizado_em, "
                          "destinatario, endereco_destino, cidade, uf) VALUES (45352,'#PS-45352','OUTRO','Open','x','x',"
                          "'Outro nome','Rua B','Barueri','SP')")
        d = ev.dados_do_pedido(self.conn, "PS-45352")
        self.assertTrue(d["conhecido"])
        self.assertEqual((d["destinatario"], d["embarcador"], d["endereco"]), ("Mercado Bom", "FABENNE", "Rua A, 10 - Barueri/SP"))
        self.assertEqual((d["cidade"], d["uf"]), ("Barueri", "SP"))      # veio do wms_pedidos

    def test_so_o_retrato_da_stokki_da_o_embarcador(self):
        _vigia(self.conn)
        self.conn.execute("INSERT INTO vigia_stokki_abertos VALUES ('PS-7',7,'MARCHEF','Waiting for Carrier','prioritario','a','b')")
        d = ev.dados_do_pedido(self.conn, "PS-7")
        self.assertEqual((d["conhecido"], d["embarcador"], d["destinatario"]), (True, "MARCHEF", ""))

    def test_cancelado(self):
        _nucleo(self.conn)
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-9','X','Y','Z','CANCELADO',NULL)")
        self.assertTrue(ev.dados_do_pedido(self.conn, "PS-9")["cancelado"])

    def test_sugestao_so_com_conferencia_e_aguardando_transportador(self):
        # regra do Hugo (28/11): sugere o total CONFERIDO quando a Stokki ja esta em
        # "aguardando transportador"; fora disso nao sugere nada
        _vigia(self.conn); _conferidos(self.conn)
        self.conn.executemany("INSERT INTO vigia_stokki_abertos VALUES (?,?,?,?,?,?,?)", [
            ("PS-9", 9, "A", "Waiting for Carrier", "prioritario", "a", "b"),
            ("PS-10", 10, "A", "Waiting for Carrier", "prioritario", "a", "b"),   # so o numero da NF
            ("PS-11", 11, "A", "Ready to Pack", "em_separacao", "a", "b"),        # ainda em conferencia
            ("PS-13", 13, "A", "Waiting for Carrier", "prioritario", "a", "b")])  # nunca lido
        self.conn.execute("INSERT INTO volumes_conferidos VALUES (9, 7, 'conferencia', 'x'), (10, 4, 'nfe', 'x'), "
                          "(11, 5, 'conferencia', 'x'), (12, 6, 'conferencia', 'x')")       # 12: fora do retrato
        self.assertEqual(ev.dados_do_pedido(self.conn, "PS-9")["sugestao"], 7)
        for sem in ("PS-10", "PS-11", "PS-12", "PS-13"):
            self.assertIsNone(ev.dados_do_pedido(self.conn, sem)["sugestao"], sem)

class TestTabela(Base):
    def test_conectar_do_galpao_cria_a_tabela(self):
        self.assertIsNotNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name = 'expedicao_volumes'").fetchone())
```

- [ ] **Step 2: Rodar e ver falhar**

Run (em `painel_agentes/`): `py -3.11 -m unittest test_expedicao_volumes -v`
Expected: erro de import (`No module named 'expedicao_volumes'`).

- [ ] **Step 3: Implementar em `expedicao_volumes.py`**
  - `garantir_tabela(conn)` com o DDL acima (sem commit próprio além do que o chamador faz).
  - `codigo_base(texto)`: se o texto começa com `FV|`, usa o 2º campo; depois aceita `#`, espaços, `PS` com ou sem hífen, e descarta o sufixo `-R<n>`. Regex final sobre o texto em maiúsculas: `^#?\s*(?:PS-?)?(\d+)(?:-R\d+)?$`.
  - `dados_do_pedido(conn, codigo)`: lê, nesta ordem, `nucleo_pedidos` (`codigo = ?`; `destinatario_nome`, `endereco`, `remetente_nome`; `cancelado` = `status == 'CANCELADO'`), `wms_pedidos` (`upper(replace(codigo_ps,'#','')) = ?`, linha mais nova; `embarcador`, `destinatario`, `endereco_destino`, `cidade`, `uf`, `nf` via `dict(row).get`) e `vigia_stokki_abertos` (`embarcador`). Cada fonte só preenche campo ainda vazio. `sugestao` = `volumes_conferidos.volumes` do `id_pedido` (os dígitos do código) **só quando** `fonte = 'conferencia'` **e** o pedido está em `vigia_stokki_abertos` com `status_stokki = 'Waiting for Carrier'`; em qualquer outro caso `None`. Um auxiliar `_ler(conn, sql, params)` devolve `None` quando a tabela/coluna não existe (`sqlite3.OperationalError`).
  - Em `wms_separacao.garantir_tabelas`, logo depois de `wms_lotes_planilha.garantir_tabela(conn)`: `import expedicao_volumes` **dentro da função** (mesmo padrão do `import wms_pedidos` em `conectar`, evita import circular) e `expedicao_volumes.garantir_tabela(conn)`.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest test_expedicao_volumes test_wms_separacao test_wms_etiqueta_volume -v`
Expected: tudo OK (os dois módulos antigos continuam verdes).

- [ ] **Step 5: Commit**

```bash
git add painel_agentes/expedicao_volumes.py painel_agentes/test_expedicao_volumes.py painel_agentes/wms_separacao.py
git commit -m "Galpão: tabela expedicao_volumes e leitura do pedido pelo PS (etiqueta de volume pra todo pedido da Stokki)"
```

---

### Task 2: Etiquetar, repetir, mudar o total, reimprimir e lista de prontos

**Files:**
- Modify: `painel_agentes/expedicao_volumes.py`
- Modify: `painel_agentes/test_expedicao_volumes.py`

**Interfaces:**
- Consumes: Task 1; `wms_fila_impressao.enfileirar(conn, *, tipo, tamanho, itens, titulo, pedido_por) -> int` (não faz commit); `wms_separacao._tx(conn)` (transação `BEGIN IMMEDIATE`); `wms.agora()`.
- Produces:
  - `MAX_CAIXAS = 300`, `CONFIRMAR_ACIMA_DE = 99`
  - `conteudo_qr(codigo: str, numero: int, total: int, id_caixa: str) -> str` → `FV|PS-45352|2|5|K7M2QX9A`
  - `situacao(conn, texto) -> dict` — `dados_do_pedido` + `"volumes"`: `None` ou `{"total", "criado_em", "criado_por", "reimpressoes"}` (do conjunto ativo; `reimpressoes` = maior valor)
  - `etiquetar(conn, texto, total, operador: dict | None, *, confirmado: bool = False) -> dict`
    - precisa confirmar: `{"confirmar": [str, ...]}` e **nada gravado**
    - feito: `{"codigo", "total", "impressao_id", "repetido": bool, "anuladas": int, "conhecido": bool}`
  - `reimprimir(conn, texto, operador: dict | None, numero: int | None = None) -> dict` → `{"codigo", "quantidade", "impressao_id"}`
  - `prontos_sem_etiqueta(conn, limite: int = 50) -> dict` → `{"total": int, "itens": [{"codigo", "embarcador", "desde"}]}`

Regras de `etiquetar` (da spec):
- `total` tem de ser inteiro (aceita `int` ou texto só de dígitos) entre 1 e 300; senão `ErroWMS("Quantidade de caixas deve ficar entre 1 e 300.")`.
- Conjunto ativo com o **mesmo** total → `repetido: True`, mesmo `impressao_id`, nada gravado.
- Mensagens de confirmação (todas as que valerem, nesta ordem; só quando `confirmado` é falso):
  - `total > 99`: `"Sao mesmo {total} caixas?"`
  - pedido cancelado: `"{codigo} esta CANCELADO no nosso sistema. Etiquetar mesmo assim?"`
  - conjunto ativo com outro total: `"{codigo} ja tem {n} etiquetas. As {n} antigas deixam de valer: troque todas pelas {total} novas."`
- Gravação (dentro de `_tx`): anula as ativas (`anulado_em`, `anulado_por`), cancela o trabalho de fila delas que ainda estiver `PENDENTE` (`UPDATE wms_fila_impressao SET situacao = 'CANCELADO' WHERE id IN (...) AND situacao = 'PENDENTE'`), gera N `id_caixa` novos, enfileira **um** trabalho (`tipo="VOLUME"`, `tamanho="10x15"`, itens na ordem 1..N, `titulo=f"{codigo} · {N} volume(s)"`, `pedido_por` = nome do operador) e insere as N linhas com o `impressao_id`.
- `id_caixa`: `secrets.choice` no alfabeto da seção Global Constraints; colisão com a chave primária → sorteia de novo.

`prontos_sem_etiqueta`: linhas de `vigia_stokki_abertos` com `status_stokki = 'Waiting for Carrier'` cujo `codigo_base` não tem conjunto ativo; `total` conta todas, `itens` traz as `limite` mais novas por `primeira_vez_em` (`desde` = `primeira_vez_em`). Tabela ausente → `{"total": 0, "itens": []}`. Linha com código que `codigo_base` recusa é ignorada.

- [ ] **Step 1: Escrever os testes que falham** (acrescentar em `test_expedicao_volumes.py`; `ANA = {"id": "1", "nome": "Ana"}`):

```python
ALFABETO = r"^[2-9A-HJ-NP-Z]{8}$"

class TestEtiquetar(Base):
    def ativos(self, codigo="PS-45352"):
        return self.conn.execute("SELECT * FROM expedicao_volumes WHERE codigo = ? AND anulado_em IS NULL ORDER BY numero",
                                 (codigo,)).fetchall()

    def test_cria_as_caixas_e_um_trabalho_na_fila(self):
        r = ev.etiquetar(self.conn, "45352", 3, ANA)
        self.assertEqual((r["codigo"], r["total"], r["repetido"], r["anuladas"], r["conhecido"]), ("PS-45352", 3, False, 0, False))
        linhas = self.ativos()
        self.assertEqual([l["numero"] for l in linhas], [1, 2, 3])
        self.assertEqual(len({l["id_caixa"] for l in linhas}), 3)
        for l in linhas:
            self.assertRegex(l["id_caixa"], ALFABETO)
            self.assertEqual(l["criado_por"], "Ana")
        t = fila.obter(self.conn, r["impressao_id"])
        self.assertEqual((t["tipo"], t["tamanho"], t["pedido_por"], t["titulo"]), ("VOLUME", "10x15", "Ana", "PS-45352 · 3 volume(s)"))
        self.assertEqual(t["itens"], [ev.conteudo_qr("PS-45352", l["numero"], 3, l["id_caixa"]) for l in linhas])
        self.assertEqual(t["itens"][1], f"FV|PS-45352|2|3|{linhas[1]['id_caixa']}")

    def test_repetir_mesmo_total(self):
        a = ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        b = ev.etiquetar(self.conn, "#ps-45352", 3, ANA)
        self.assertTrue(b["repetido"])
        self.assertEqual(b["impressao_id"], a["impressao_id"])
        self.assertEqual(len(fila.listar(self.conn)), 1)

    def test_trocar_total(self):
        a = ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        antigos = {l["id_caixa"] for l in self.ativos()}
        pede = ev.etiquetar(self.conn, "PS-45352", 4, ANA)
        self.assertEqual(pede, {"confirmar": ["PS-45352 ja tem 3 etiquetas. As 3 antigas deixam de valer: troque todas pelas 4 novas."]})
        self.assertEqual(len(self.ativos()), 3)                       # nada mudou
        r = ev.etiquetar(self.conn, "PS-45352", 4, {"id": "2", "nome": "Bia"}, confirmado=True)
        self.assertEqual((r["total"], r["anuladas"]), (4, 3))
        self.assertEqual(len(self.ativos()), 4)
        self.assertFalse(antigos & {l["id_caixa"] for l in self.ativos()})
        anuladas = self.conn.execute("SELECT anulado_por FROM expedicao_volumes WHERE anulado_em IS NOT NULL").fetchall()
        self.assertEqual([x[0] for x in anuladas], ["Bia"] * 3)
        self.assertEqual(fila.obter(self.conn, a["impressao_id"])["situacao"], "CANCELADO")

    def test_trabalho_ja_impresso_nao_vira_cancelado(self):
        a = ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        fila.marcar_impresso(self.conn, a["impressao_id"], "estacao")
        ev.etiquetar(self.conn, "PS-45352", 2, ANA, confirmado=True)
        self.assertEqual(fila.obter(self.conn, a["impressao_id"])["situacao"], "IMPRESSO")

    def test_mais_de_99_e_cancelado_pedem_confirmacao(self):
        _nucleo(self.conn)
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-9','X','Y','Z','CANCELADO',NULL)")
        self.assertEqual(ev.etiquetar(self.conn, "PS-9", 120, ANA)["confirmar"],
                         ["Sao mesmo 120 caixas?", "PS-9 esta CANCELADO no nosso sistema. Etiquetar mesmo assim?"])
        self.assertEqual(self.ativos("PS-9"), [])
        self.assertEqual(ev.etiquetar(self.conn, "PS-9", 120, ANA, confirmado=True)["total"], 120)
        self.assertEqual(ev.etiquetar(self.conn, "PS-8", 99, ANA)["total"], 99)       # 99 nao pergunta
        self.assertEqual(ev.etiquetar(self.conn, "PS-7", "300", ANA, confirmado=True)["total"], 300)

    def test_quantidade_invalida(self):
        for ruim in (0, -1, 301, 5.5, "abc", "", None, "5,0"):
            with self.assertRaises(wms.ErroWMS):
                ev.etiquetar(self.conn, "PS-45352", ruim, ANA, confirmado=True)
        self.assertEqual(self.ativos(), [])

    def test_situacao(self):
        self.assertIsNone(ev.situacao(self.conn, "45352")["volumes"])
        ev.etiquetar(self.conn, "45352", 3, ANA)
        v = ev.situacao(self.conn, "FV|PS-45352|1|3")["volumes"]
        self.assertEqual((v["total"], v["criado_por"], v["reimpressoes"]), (3, "Ana", 0))

class TestReimprimir(Base):
    def test_todas_e_uma_so(self):
        ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        originais = fila.listar(self.conn)[0]["itens"]
        r = ev.reimprimir(self.conn, "PS-45352", ANA)
        self.assertEqual((r["quantidade"], fila.obter(self.conn, r["impressao_id"])["itens"]), (3, originais))
        r2 = ev.reimprimir(self.conn, "PS-45352", ANA, numero=2)
        self.assertEqual(fila.obter(self.conn, r2["impressao_id"])["itens"], [originais[1]])
        self.assertEqual([x[0] for x in self.conn.execute("SELECT reimpressoes FROM expedicao_volumes ORDER BY numero")], [1, 2, 1])

    def test_sem_etiqueta_ou_numero_que_nao_existe(self):
        with self.assertRaises(wms.ErroWMS):
            ev.reimprimir(self.conn, "PS-45352", ANA)
        ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        with self.assertRaises(wms.ErroWMS):
            ev.reimprimir(self.conn, "PS-45352", ANA, numero=9)

class TestProntos(Base):
    def test_so_pronto_na_stokki_e_sem_etiqueta(self):
        self.assertEqual(ev.prontos_sem_etiqueta(self.conn), {"total": 0, "itens": []})    # sem a tabela
        _vigia(self.conn)
        self.conn.executemany("INSERT INTO vigia_stokki_abertos VALUES (?,?,?,?,?,?,?)", [
            ("PS-1", 1, "FABENNE", "Waiting for Carrier", "aguardando_transportador", "2026-11-28 10:00:00", "x"),
            ("PS-2", 2, "FABENNE", "Open", "prioritario", "2026-11-28 11:00:00", "x"),
            ("PS-3", 3, "MARCHEF", "Waiting for Carrier", "prioritario", "2026-11-28 12:00:00", "x"),
            ("PS-4", 4, "MARCHEF", "Waiting for Carrier", "prioritario", "2026-11-28 13:00:00", "x")])
        ev.etiquetar(self.conn, "PS-3", 2, ANA)
        r = ev.prontos_sem_etiqueta(self.conn, limite=1)
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["itens"], [{"codigo": "PS-4", "embarcador": "MARCHEF", "desde": "2026-11-28 13:00:00"}])
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_expedicao_volumes -v`
Expected: `AttributeError: module 'expedicao_volumes' has no attribute 'etiquetar'` (e afins).

- [ ] **Step 3: Implementar** `conteudo_qr`, `situacao`, `etiquetar`, `reimprimir`, `prontos_sem_etiqueta` conforme as regras acima. `reimprimir` enfileira um trabalho novo com os mesmos QRs e soma 1 em `reimpressoes` das linhas reimpressas.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest test_expedicao_volumes -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add painel_agentes/expedicao_volumes.py painel_agentes/test_expedicao_volumes.py
git commit -m "Galpão: etiquetar pedido pelo PS (caixa com QR único, mudar total anula as antigas, reimprimir, prontos sem etiqueta)"
```

---

### Task 3: PDF da fila aceita o QR de 5 campos

**Files:**
- Modify: `painel_agentes/expedicao_volumes.py`
- Modify: `painel_agentes/wms_etiqueta_volume.py` (`gerar_pdf_volumes`, linhas 107-120)
- Modify: `painel_agentes/test_expedicao_volumes.py`

**Interfaces:**
- Consumes: Tasks 1 e 2; `wms_etiqueta_volume.desenhar_etiqueta_volume(dados)` (chaves `codigo_ps, numero, total, qr, destinatario, endereco, cidade, uf, embarcador, nf`).
- Produces: `expedicao_volumes.dados_da_etiqueta(conn, conteudo: str) -> dict | None` — mesmas chaves acima. `None` quando o texto não tem 5 campos `FV|...` ou o `<id>` não existe / não é daquele PS. Caixa anulada levanta `wms.ErroWMS("{codigo}: etiqueta anulada (o total de caixas mudou). Imprima o trabalho novo.")`. `numero` e `total` saem **da linha gravada**, não do texto.

- [ ] **Step 1: Escrever os testes que falham**

```python
import wms_etiqueta_volume as vol

class TestPdf(Base):
    def test_dados_da_etiqueta(self):
        _nucleo(self.conn)
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-45352','Mercado Bom','Rua A, 10','FABENNE','ABERTO',NULL)")
        r = ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        item = fila.obter(self.conn, r["impressao_id"])["itens"][1]
        d = ev.dados_da_etiqueta(self.conn, item)
        self.assertEqual((d["codigo_ps"], d["numero"], d["total"], d["qr"], d["destinatario"], d["embarcador"]),
                         ("PS-45352", 2, 3, item, "Mercado Bom", "FABENNE"))
        self.assertIsNone(ev.dados_da_etiqueta(self.conn, "FV|PS-45352|2|3|ZZZZZZZZ"))
        self.assertIsNone(ev.dados_da_etiqueta(self.conn, "FV|PS-45352|2|3"))
        outro = item.replace("PS-45352", "PS-1")
        self.assertIsNone(ev.dados_da_etiqueta(self.conn, outro))          # id de outro pedido

    def test_pdf_mistura_piloto_e_novo(self):
        r = ev.etiquetar(self.conn, "PS-45352", 2, ANA)
        itens = fila.obter(self.conn, r["impressao_id"])["itens"] + ["FV|PS-44009|1|1"]
        self.assertTrue(vol.gerar_pdf_volumes(self.conn, itens).startswith(b"%PDF"))

    def test_pdf_recusa_anulada(self):
        a = ev.etiquetar(self.conn, "PS-45352", 2, ANA)
        itens = fila.obter(self.conn, a["impressao_id"])["itens"]
        ev.etiquetar(self.conn, "PS-45352", 3, ANA, confirmado=True)
        with self.assertRaisesRegex(wms.ErroWMS, "anulada"):
            vol.gerar_pdf_volumes(self.conn, itens)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_expedicao_volumes.TestPdf -v`
Expected: `AttributeError ... 'dados_da_etiqueta'`.

- [ ] **Step 3: Implementar** `dados_da_etiqueta` e, em `gerar_pdf_volumes`, escolher a fonte por item: 5 campos → `expedicao_volumes.dados_da_etiqueta` (import dentro da função, como já é feito com `wms_separacao`); senão o caminho atual `wms_separacao.dados_do_volume`. A mensagem "Etiqueta de volume invalida" continua para `None`.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest test_expedicao_volumes test_wms_etiqueta_volume test_fila_etiquetas_tela -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add painel_agentes/expedicao_volumes.py painel_agentes/wms_etiqueta_volume.py painel_agentes/test_expedicao_volumes.py
git commit -m "Galpão: PDF da fila imprime a etiqueta de volume com QR único e recusa etiqueta anulada"
```

---

### Task 4: Rotas `/api/wms/etiquetar/*`

**Files:**
- Modify: `painel_agentes/painel_agentes.py` (import junto dos `wms_*`, ~linha 123; rotas logo depois de `api_wms_separacao_volume_reimprimir`, ~linha 7521)
- Create: `painel_agentes/test_expedicao_volumes_tela.py`
- Modify: `MAPA_DO_SISTEMA.txt`

**Interfaces:**
- Consumes: Tasks 1 a 3; do painel: `requer_auth(niveis=_NIVEIS_WMS)`, `exige_mesma_origem`, `_wms_exige_operador()`, `_sep_chamar(funcao)`, `_sep_corpo()`.
- Produces (JSON; erro de regra = 400 `{"erro": ...}`):
  - `GET  /api/wms/etiquetar/pedido?codigo=<texto>` → `expedicao_volumes.situacao`
  - `POST /api/wms/etiquetar/pedido` corpo `{codigo, total, confirmado}` → `etiquetar`
  - `POST /api/wms/etiquetar/reimprimir` corpo `{codigo, numero?}` → `reimprimir`
  - `GET  /api/wms/etiquetar/prontos` → `prontos_sem_etiqueta`

Todas com `_NIVEIS_WMS` (`total`, `operador`, `galpao`). Os dois POST levam `@exige_mesma_origem` e `_wms_exige_operador()`. As rotas ficam sob `/api/wms/`, que a tela `wms` de `acesso_telas.TELAS` já cobre: **não** criar entrada nova em `TELAS`.

- [ ] **Step 1: Escrever os testes que falham** em `test_expedicao_volumes_tela.py` (mesmo `Base` de `test_wms_separacao_tela.py`: `DB_PATH` de `wms` e `acesso_telas` em pasta temporária, `_carregar_config` e `_wms_gravar_operador` simulados, `cliente(nivel)`, `operador(op)`, `post(c, url, corpo)` com o cabeçalho `ORIGEM`):

```python
class TestRotas(Base):
    def test_fluxo_do_operador(self):
        c = self.cliente("galpao")
        with self.operador(ANA):
            self.assertIsNone(c.get("/api/wms/etiquetar/pedido?codigo=45352").get_json()["volumes"])
            r = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 3})
            self.assertEqual((r.status_code, r.get_json()["total"]), (200, 3))
            pede = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 4}).get_json()
            self.assertIn("confirmar", pede)
            ok = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 4, "confirmado": True}).get_json()
            self.assertEqual(ok["anuladas"], 3)
            self.assertEqual(self.post(c, "/api/wms/etiquetar/reimprimir", {"codigo": "45352", "numero": 2}).get_json()["quantidade"], 1)
            self.assertEqual(c.get("/api/wms/etiquetar/pedido?codigo=45352").get_json()["volumes"]["total"], 4)
            self.assertEqual(c.get("/api/wms/etiquetar/prontos").get_json(), {"total": 0, "itens": []})

    def test_erros_viram_400_com_o_motivo(self):
        c = self.cliente("galpao")
        with self.operador(ANA):
            r = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "abc", "total": 3})
            self.assertEqual(r.status_code, 400)
            self.assertIn("pedido invalido", r.get_json()["erro"])
            self.assertEqual(self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 0}).status_code, 400)
            self.assertEqual(c.get("/api/wms/etiquetar/pedido?codigo=").status_code, 400)

    def test_sem_operador_e_sem_nivel(self):
        c = self.cliente("galpao")
        with self.operador(None):
            r = self.post(c, "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 3})
            self.assertEqual(r.status_code, 409)
            self.assertTrue(r.get_json()["sem_operador"])
        with self.operador(ANA):
            self.assertEqual(self.post(self.cliente("expedicao"), "/api/wms/etiquetar/pedido",
                                       {"codigo": "45352", "total": 3}).status_code, 403)

    def test_estacao_imprime_o_pdf_do_trabalho(self):
        with self.operador(ANA):
            r = self.post(self.cliente("galpao"), "/api/wms/etiquetar/pedido", {"codigo": "45352", "total": 2}).get_json()
        pdf = self.cliente("expedicao").get(f"/etiquetas/fila/{r['impressao_id']}.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_expedicao_volumes_tela -v`
Expected: 404 nas rotas novas.

- [ ] **Step 3: Implementar as quatro rotas** em `painel_agentes.py`, cada uma uma linha de `_sep_chamar(lambda conn: expedicao_volumes.<funcao>(...))`, no mesmo formato de `api_wms_separacao_volumes`.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest test_expedicao_volumes_tela test_wms_separacao_tela -v`
Expected: OK.

- [ ] **Step 5: Atualizar `MAPA_DO_SISTEMA.txt`**: na linha do "Separação/conferência/volumes do galpão" (≈148-175) acrescentar o módulo `painel_agentes/expedicao_volumes.py`, a tabela `expedicao_volumes`, o QR `FV|PS|n|total|id` e as rotas `/api/wms/etiquetar/*`; na lista de tabelas (≈2335) a tabela nova; apontar spec e plano.

- [ ] **Step 6: Commit**

```bash
git add painel_agentes/painel_agentes.py painel_agentes/test_expedicao_volumes_tela.py MAPA_DO_SISTEMA.txt
git commit -m "Galpão: rotas /api/wms/etiquetar (consultar, etiquetar, reimprimir, prontos sem etiqueta)"
```

---

### Task 5: Tela Etiquetar no app do galpão

**Files:**
- Modify: `painel_agentes/templates/wms.html` (botão no `TELAS.inicio`, ≈linha 368; tela nova junto de `TELAS.volumes`, ≈linha 1802)
- Modify: `painel_agentes/test_wms_telas.py` (ou o arquivo de teste de tela que já lê o `wms.html`)

**Interfaces:**
- Consumes: rotas da Task 4; do `wms.html`: `api(caminho, opts)` (prefixo `/api/wms`), `ir(tela, ctx, empilhar)`, `toast(msg, erro)`, `bip(ok)`, `esc()`, `S.operador`, `abrirCamera(fn)`, `TELAS` / `TELAS_POS`.
- Produces: tela `etiquetar` acessível pelo Início.

- [ ] **Step 1: PARAR e mostrar a prévia ao Hugo.** Montar a prévia da tela (HTML estático com o CSS do `wms.html`, 2 variações: entrada no Início como botão largo logo abaixo de "Receber" **ou** aba própria na barra de baixo) e esperar a escolha. Regra do Hugo para tela nova; nenhum código de tela antes disso.

- [ ] **Step 2: Escrever o teste que falha** (padrão dos testes de `test_wms_telas.py`, que leem o texto do template):

```python
def test_tela_etiquetar_usa_as_rotas_novas(self):
    html = (Path(__file__).parent / "templates" / "wms.html").read_text(encoding="utf-8")
    self.assertIn("TELAS.etiquetar", html)
    self.assertIn('data-ir="etiquetar"', html)
    for rota in ("/etiquetar/pedido", "/etiquetar/reimprimir", "/etiquetar/prontos"):
        self.assertIn(rota, html)
    self.assertIn("confirmado: true", html)
```

- [ ] **Step 3: Implementar a tela** conforme a variação escolhida. Comportamento fixo:
  - Campo "Pedido" com foco, Enter ou câmera → `GET /etiquetar/pedido?codigo=`; mostra embarcador → destinatário e cidade; pedido não reconhecido mostra "Não achei esse pedido no nosso sistema: confira o número" (e deixa imprimir); cancelado aparece em destaque.
  - Campo "Quantas caixas?" numérico, já preenchido com `sugestao` quando vier.
  - "Imprimir" → `POST /etiquetar/pedido`. Sem operador identificado: mesmo tratamento do `sepPost` (vai pra tela do operador). Resposta com `confirmar` → `confirm()` com as mensagens, uma por linha; aceitou → reenvia com `confirmado: true`. Sucesso → `toast(N + ' etiqueta(s) enviadas pra fila de impressão')`, `bip(true)`, limpa e volta o foco pro campo do pedido. `repetido` → toast "Esse pedido já tem N etiquetas" e mostra os botões de reimpressão.
  - Pedido já etiquetado: linha "Já etiquetado: N caixas, <data> (<quem>)" e botões **Reimprimir todas**, **Reimprimir uma** (pede o número) e **Mudar total** (leva o foco pro campo de quantidade).
  - Abaixo: "Prontos na Stokki sem etiqueta (total)" com os itens de `GET /etiquetar/prontos`; tocar num item preenche o campo do pedido. Rodapé pequeno: "atualiza a cada rodada do sistema".
  - Erros do servidor aparecem com o texto de `erro` (regra do Hugo: toda recusa diz o motivo).

- [ ] **Step 4: Rodar os testes**

Run: `py -3.11 -m unittest test_wms_telas test_expedicao_volumes_tela -v`
Expected: OK.

- [ ] **Step 5: Prova na tela, em outra porta** (nunca a 8070). De `painel_agentes/`:
  `py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"` e, com Playwright local (cookie Secure desligado, bloquear `/api/torre/**`): entrar em `/wms`, identificar operador, etiquetar `PS-45352` com 3, conferir o trabalho em `/etiquetas/fila`, abrir o PDF (3 páginas 10×15, QR legível, "2 / 3"), mudar pra 4 e ver o trabalho antigo sumir da fila. Guardar um print da tela e a primeira página do PDF para o Hugo.

- [ ] **Step 6: Commit**

```bash
git add painel_agentes/templates/wms.html painel_agentes/test_wms_telas.py
git commit -m "Galpão: tela Etiquetar (bipa o PS, informa as caixas, imprime; lista de prontos na Stokki sem etiqueta)"
```

---

### Task 6: Ler o total conferido dos pedidos prontos (para a sugestão aparecer)

Hoje `volumes_conferidos` só é preenchida às 03h50, para os pedidos das rotas do dia: em 28/11,
de 352 pedidos em "aguardando transportador", só 23 tinham o total conferido gravado. Esta
tarefa lê os demais, de hora em hora, logo depois da rodada do pipeline.

**Files:**
- Modify: `roteirizacao/atualizar_volumes_conferidos.py`
- Create: `roteirizacao/test_volumes_prontos.py`
- Create: `infra/stokki-volumes-prontos.service`, `infra/stokki-volumes-prontos.timer`
- Modify: `MAPA_DO_SISTEMA.txt`

**Interfaces:**
- Consumes: `atualizar(sessao, ids, db_path) -> dict` e `gravar(...)` que já existem no arquivo; tabela `vigia_stokki_abertos`.
- Produces:
  - `ids_prontos_sem_leitura(db_path=DB_PATH, limite: int = 80) -> list[int]` — `id_stokki` das linhas de `vigia_stokki_abertos` com `status_stokki = 'Waiting for Carrier'`, `id_stokki` preenchido e **sem linha** em `volumes_conferidos`; as mais novas por `primeira_vez_em` primeiro; no máximo `limite`. Tabela ausente → `[]`.
  - CLI: `--prontos` (lê esses pedidos em vez das rotas do dia; não chama a Vuupt) e `--limite N` (padrão 80).

Regra: quem já tem linha em `volumes_conferidos` (mesmo `nfe` ou sem número) **não** é relido por `--prontos`; a leitura das 03h50 continua relendo os pedidos das rotas do dia. Com `--prontos` e todos os pedidos falhando, sai com código 1 (mesma regra do modo atual).

- [ ] **Step 1: Escrever os testes que falham** em `roteirizacao/test_volumes_prontos.py` (banco temporário; `atualizar` recebe uma sessão falsa e `stokki.pedidos.obter_detalhe` é simulado com `mock.patch`, como em `test_romaneio_caixas_conferidas.py`):

```python
def test_so_pronto_sem_leitura_mais_novo_primeiro(self):
    # vigia: 1 pronto ja lido, 2 pronto sem leitura (10h), 3 em conferencia, 4 pronto sem leitura (12h), 5 pronto sem id
    # volumes_conferidos: so o 1
    self.assertEqual(avc.ids_prontos_sem_leitura(self.db), [4, 2])
    self.assertEqual(avc.ids_prontos_sem_leitura(self.db, limite=1), [4])

def test_sem_as_tabelas_devolve_vazio(self):
    self.assertEqual(avc.ids_prontos_sem_leitura(self.db_vazio), [])

def test_modo_prontos_grava_e_nao_chama_a_vuupt(self):
    # obter_detalhe simulado devolve {"quantidade_volumes": 6, "fonte_volumes": "conferencia"}
    # buscar_rotas_do_dia simulado levanta AssertionError se for chamado
    self.assertEqual(avc.main_prontos(self.db, sessao=object(), limite=80), 0)
    self.assertEqual(avc.carregar({2, 4}, self.db), {2: (6, "conferencia"), 4: (6, "conferencia")})
```

  (`main_prontos(db_path, sessao, limite) -> int` é o corpo do modo `--prontos`, separado do `main` para ser testável sem config nem Stokki.)

- [ ] **Step 2: Rodar e ver falhar**

Run (na raiz): `py -3.11 -m unittest roteirizacao/test_volumes_prontos.py -v`
Expected: `AttributeError ... 'ids_prontos_sem_leitura'`.

- [ ] **Step 3: Implementar** `ids_prontos_sem_leitura`, `main_prontos` e os argumentos `--prontos` / `--limite` (o `main` atual, das rotas do dia, fica intacto).

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest roteirizacao/test_volumes_prontos.py roteirizacao/test_romaneio_caixas_conferidas.py -v`
Expected: OK.

- [ ] **Step 5: Criar as units** copiando o formato de `infra/stokki-volumes-conferidos.service|.timer`:
  - service: `ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/roteirizacao/atualizar_volumes_conferidos.py --prontos`, `User=www-data`, `TimeoutStartSec=900`, `OnFailure=stokki-alerta-falha@%n.service`.
  - timer: `OnCalendar=Mon..Fri *-*-* 08..17:25:00` (20 min depois do pipeline horário das `:05`, que é quem atualiza o retrato), `Persistent=false`.
  - Registrar no `MAPA_DO_SISTEMA.txt` (rotina, horário, o que lê e grava).

- [ ] **Step 6: Ensaio sem gravar na produção.** Localmente, `py -3.11 roteirizacao/atualizar_volumes_conferidos.py --prontos --limite 3` só se o Hugo autorizar o uso da sessão da Stokki naquele momento (login concorrente derruba a outra sessão); senão, o ensaio fica para o deploy.

- [ ] **Step 7: Commit**

```bash
git add roteirizacao/atualizar_volumes_conferidos.py roteirizacao/test_volumes_prontos.py infra/stokki-volumes-prontos.service infra/stokki-volumes-prontos.timer MAPA_DO_SISTEMA.txt
git commit -m "Galpão: lê de hora em hora o total conferido dos pedidos prontos na Stokki (sugestão de caixas na etiqueta)"
```

---

## Depois do plano (só com pedido do Hugo)

- Push do ramo, bancada de homologação (`homolog subir etiqueta-volume-stokki`) e deploy: reiniciar só o `painel-agentes`; provar com `curl` local em `/api/wms/etiquetar/prontos`.
- Timer novo da Tarefa 6: antes, `systemctl list-timers` e `ls /etc/systemd/system/` na VPS; copiar as duas units, `daemon-reload`, `enable --now`. A primeira rodada pega no máximo 80 pedidos; o atraso de ~330 pedidos some em cerca de quatro rodadas. Conferir a primeira rodada no log.
- Primeiro uso real: um pedido de verdade etiquetado na impressora 10×15 do galpão e o QR lido por um celular.
