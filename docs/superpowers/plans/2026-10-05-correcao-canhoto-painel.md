# Correção de canhoto pelo painel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O escritório (níveis `total`, `operador`, `atendimento`) completa ou substitui o checklist de uma parada já concluída pelo painel, acha os casos numa fila "Canhotos a resolver", e o cliente vê a versão corrigida no portal e no link do e-mail.

**Architecture:** Um módulo de domínio novo (`nucleo/correcao_comprovante.py`) grava cada correção como retrato completo: arquivos em `nucleo_comprovantes` (origem `PAINEL`, mesmo `correcao_id`) e campos de texto no evento `COMPROVANTE_CORRIGIDO`. O portal ganha `portal_cliente/canhoto_corrigido.py`, que monta o PDF da correção mais recente pelo `service_id` e cai para o PDF da Vuupt quando não há correção. O painel ganha o formulário na ficha do pedido e a aba nova em `/canhotos`.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db`), Pillow (páginas do PDF), pypdf (juntar PDFs enviados), unittest.

**Spec:** `docs/superpowers/specs/2026-10-05-correcao-canhoto-painel-design.md`

## Global Constraints

- Interpretador local: `py -3.11`. Testes com `py -3.11 -m unittest ...`; nunca pytest.
- Testes de `painel_agentes` rodam **de dentro de `painel_agentes/`**; nunca no mesmo comando que `nucleo`.
- Idioma: português em código, comentários, mensagens e commits (`Área: o que mudou e por quê`).
- **Trabalhar num worktree a partir de `origin/master`** (o master local está 88 commits atrás e o working tree tem edições não commitadas de outras sessões em `painel_agentes.py`, `_menu_lateral_nav.html`, `portal_cliente/app.py`). Antes de começar: `git fetch origin` e `git worktree add .claude/worktrees/correcao-canhoto -b correcao-canhoto origin/master`. Rodar os testes de lá. `config.yaml` não existe no worktree: copiar o da raiz só para teste local (`cp ../../../config.yaml .`), nunca commitar.
- Só `git add` dos arquivos da tarefa; nunca `git add -A`. Commit só quando o Hugo pedir; push/deploy só com ordem dele.
- Arquivo enviado: até **10 MB**, só **jpg, png, webp ou PDF** (detectado pelo conteúdo, não pela extensão).
- Fila "Canhotos a resolver": janela de **30 dias** pela `data_rota`.
- Correção **não muda o resultado** da parada; fluxo vem de `situacao`: `ENTREGUE→ENTREGUE`, `PARCIAL→PARCIAL`, `INSUCESSO→NAO_ENTREGUE`. Outra situação → 409.
- Nada é apagado: comprovantes do app e PDF da Vuupt continuam. Vale a correção mais recente.
- Ligação com o portal sempre por `nucleo_paradas.service_id`, nunca só pelo código.
- Endpoints POST do painel levam `@exige_mesma_origem`; `niveis=` do menu bate com o `@requer_auth` da rota.
- Cada envio é completo: campos obrigatórios do `checklist_modelo` (texto e arquivo) precisam vir no mesmo envio; os textos já vêm pré-preenchidos no formulário.

## Review Focus

1. **Reentrega com o mesmo código** (duas paradas `PS-1`, `service_id` diferentes): a correção de uma não pode aparecer no canhoto da outra → teste em Task 2 (`test_correcao_vigente_separa_service_id`).
2. **Correção enviada duas vezes**: o portal mostra só a mais recente, não mistura arquivos das duas → teste em Task 2 (`test_correcao_vigente_e_a_mais_recente`) e Task 4 (`test_pdf_tem_so_arquivos_da_ultima`).
3. **Arquivo com extensão de foto mas conteúdo de outra coisa** (ex.: `.jpg` que é um `.exe`/HTML): recusado com 400 → teste em Task 2 (`test_recusa_conteudo_que_nao_e_imagem_nem_pdf`).
4. **Falha no meio da gravação** (INSERT quebra depois de gravar arquivo): não fica arquivo órfão nem linha pela metade → teste em Task 2 (`test_falha_no_banco_apaga_arquivos_gravados`).
5. **PDF da correção não monta** (imagem corrompida no disco): portal cai para o PDF da Vuupt em vez de 500 → teste em Task 4 (`test_falha_ao_montar_cai_para_vuupt`).

---

## Arquivos

| Arquivo | Papel |
|---|---|
| `nucleo/banco.py` (modificar) | colunas `origem`, `enviado_por`, `correcao_id`; tabela `canhoto_marcacoes`; índices |
| `nucleo/correcao_comprovante.py` (criar) | domínio: registrar correção, marcar ruim, correção vigente, valores iniciais, fila |
| `nucleo/test_correcao_comprovante.py` (criar) | testes do domínio e do esquema |
| `portal_cliente/canhoto_corrigido.py` (criar) | acha a correção vigente por `service_id` e devolve o PDF (pronto ou montado) |
| `portal_cliente/test_canhoto_corrigido.py` (criar) | testes do PDF e das duas rotas do portal |
| `portal_cliente/app.py` (modificar `api_canhoto`) | prefere a correção |
| `portal_cliente/link_canhoto.py` (modificar `canhoto_por_link`) | prefere a correção |
| `painel_agentes/painel_agentes.py` (modificar) | rotas de correção/marcação, aba `A_RESOLVER`, níveis, ficha com dados da correção |
| `painel_agentes/templates/consulta_pedido.html` (modificar) | botões, formulário, exibição de PDF/escritório |
| `painel_agentes/templates/canhotos.html` (modificar) | aba "A resolver" |
| `painel_agentes/templates/_menu_lateral_nav.html` (modificar) | `atendimento` no item Canhotos |
| `painel_agentes/test_canhotos_a_resolver.py` (criar) | testes das rotas do painel |
| `MAPA_DO_SISTEMA.txt` (modificar) | tela, tabela, módulos novos |

---

### Task 1: Esquema no banco

**Files:**
- Modify: `nucleo/banco.py` (DDL logo após `idx_nucleo_comprovantes_parada`, ~linha 228; listas de colunas ~linha 336; `garantir_esquema` ~linha 355)
- Test: `nucleo/test_correcao_comprovante.py`

**Interfaces:**
- Produces: tabela `canhoto_marcacoes(id, parada_id, motivo, marcado_por, marcado_em, resolvido_em, resolvido_por)`; colunas `nucleo_comprovantes.origem|enviado_por|correcao_id` (TEXT); índices `idx_nucleo_comprovantes_correcao`, `idx_nucleo_paradas_service`, `idx_nucleo_pedidos_service`.

- [ ] **Step 1: Write the failing test**

Criar `nucleo/test_correcao_comprovante.py`:

```python
# -*- coding: utf-8 -*-
"""
Correção de canhoto pelo painel (nucleo/correcao_comprovante.py, 05/10).
Cada teste usa um SQLite temporário -- nada toca o dados.db real.

    py -3.11 -m unittest nucleo.test_correcao_comprovante -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: no Windows o SQLite segura o handle do arquivo até o GC
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.pasta = Path(self._tmp.name)
        p = mock.patch.object(banco, "DB_PATH", self.pasta / "teste.db")
        p.start()
        self.addCleanup(p.stop)
        self.conn = banco.conectar()
        self.addCleanup(self.conn.close)


class TestEsquema(_Base):
    def test_colunas_novas_em_comprovantes(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(nucleo_comprovantes)")}
        self.assertTrue({"origem", "enviado_por", "correcao_id"} <= cols)

    def test_tabela_de_marcacoes(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(canhoto_marcacoes)")}
        self.assertEqual(cols, {"id", "parada_id", "motivo", "marcado_por", "marcado_em",
                                "resolvido_em", "resolvido_por"})

    def test_indices_por_service_id(self):
        nomes = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"idx_nucleo_comprovantes_correcao", "idx_nucleo_paradas_service",
                         "idx_nucleo_pedidos_service"} <= nomes)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run (da raiz do worktree): `py -3.11 -m unittest nucleo.test_correcao_comprovante -v`
Expected: 3 FAIL/ERROR (`canhoto_marcacoes` vazia → set vazio; colunas ausentes; índices ausentes).

- [ ] **Step 3: Implementar**

Em `nucleo/banco.py`, dentro de `_DDL`, logo depois de
`CREATE INDEX IF NOT EXISTS idx_nucleo_comprovantes_parada ON nucleo_comprovantes(parada_id);`:

```sql

-- Correção de canhoto pelo painel (05/10): "marcar canhoto ruim" põe a
-- parada na fila /canhotos?resultado=A_RESOLVER até alguém enviar a correção.
CREATE TABLE IF NOT EXISTS canhoto_marcacoes (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    parada_id               INTEGER NOT NULL REFERENCES nucleo_paradas(id) ON DELETE CASCADE,
    motivo                  TEXT NOT NULL,
    marcado_por             TEXT,
    marcado_em              TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resolvido_em            TEXT,
    resolvido_por           TEXT
);
CREATE INDEX IF NOT EXISTS idx_canhoto_marcacoes_parada ON canhoto_marcacoes(parada_id, resolvido_em);
```

Depois de `_COLUNAS_ROTAS_NOVAS`:

```python


# Colunas novas de nucleo_comprovantes (05/10): correção de canhoto pelo
# painel. origem NULL = APP (linhas antigas); correcao_id agrupa os
# arquivos de um mesmo envio do formulário.
_COLUNAS_COMPROVANTES_NOVAS = [
    ("origem", "TEXT"),          # APP | PAINEL
    ("enviado_por", "TEXT"),     # usuário do painel
    ("correcao_id", "TEXT"),
]
```

Em `garantir_esquema`, depois da linha do `_migrar_colunas(conn, "nucleo_pedagios", ...)`:

```python
    _migrar_colunas(conn, "nucleo_comprovantes", _COLUNAS_COMPROVANTES_NOVAS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nucleo_comprovantes_correcao ON nucleo_comprovantes(correcao_id)")
    # Portal e fila acham a parada pelo service_id da VUUPT (a reentrega com
    # o mesmo código sobrescreve nucleo_pedidos -- código sozinho não serve).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nucleo_paradas_service ON nucleo_paradas(service_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nucleo_pedidos_service ON nucleo_pedidos(vuupt_service_id)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.11 -m unittest nucleo.test_correcao_comprovante -v` → 3 OK.
Run também: `py -3.11 -m unittest nucleo.test_consulta nucleo.test_api_motorista` → OK (não quebrou o esquema).

- [ ] **Step 5: Commit (quando o Hugo autorizar)**

```bash
git add nucleo/banco.py nucleo/test_correcao_comprovante.py
git commit -m "Nucleo: esquema da correcao de canhoto pelo painel (origem, correcao_id, marcacoes)"
```

---

### Task 2: Registrar correção e achar a vigente

**Files:**
- Create: `nucleo/correcao_comprovante.py`
- Test: `nucleo/test_correcao_comprovante.py` (acrescentar)

**Interfaces:**
- Consumes: Task 1 (colunas/tabela); `nucleo.operacao.OperacaoInvalida(mensagem, codigo=400)`, `nucleo.operacao.carregar_checklist(conn) -> {"fluxos": {fluxo: [{"chave","rotulo","tipo","obrigatorio","opcoes","aviso"}]}, "motivos": [...]}`; `nucleo.rotas.registrar_evento(conn, tipo, origem, ocorrido_em, rota_id=, parada_id=, dados=)`.
- Produces:
  - `EVENTO = "COMPROVANTE_CORRIGIDO"`, `LIMITE_BYTES = 10 * 1024 * 1024`, `TIPOS_ARQUIVO = {"FOTO", "DOCUMENTO"}`
  - `fluxo_da_parada(parada: sqlite3.Row | dict) -> str` (409 se sem resultado)
  - `modelo_do_fluxo(conn, fluxo: str) -> list[dict]`
  - `registrar_correcao(conn, parada_id: int, campos: dict[str, str], arquivos: dict[str, bytes], usuario: str, pasta: Path) -> {"correcao_id": str, "fluxo": str, "arquivos": int}`
  - `correcao_da_parada(conn, parada_id: int) -> dict | None`
  - `correcao_vigente(conn, service_id: int) -> dict | None`
  - formato do dict de correção: `{"correcao_id", "parada_id", "codigo", "destinatario_nome", "data_rota", "completed_at", "fluxo", "enviado_por", "enviado_em", "campos": {chave: valor}, "rotulos": {chave: rotulo}, "arquivos": [{"id", "chave", "tipo", "caminho": Path absoluto, "pdf": bool}]}`
  - `valores_iniciais(conn, parada_id: int) -> dict[str, str]`

- [ ] **Step 1: Write the failing tests**

Acrescentar a `nucleo/test_correcao_comprovante.py` (antes do `if __name__`), e trocar o import do topo por
`from nucleo import banco, correcao_comprovante as cc  # noqa: E402`:

```python
JPG = b"\xff\xd8\xff\xe0" + b"0" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200
PDF = b"%PDF-1.4\n" + b"0" * 200

_MODELO = [
    # fluxo, chave, rotulo, tipo, obrigatorio, opcoes, ordem
    ("ENTREGUE", "nome_recebedor", "Nome de quem recebeu", "TEXTO", 1, None, 1),
    ("ENTREGUE", "vinculo", "Quem e essa pessoa?", "SELECAO", 1, '["Porteiro","Gerente"]', 2),
    ("ENTREGUE", "documento", "Documento da pessoa", "DOCUMENTO", 0, None, 3),
    ("ENTREGUE", "foto_canhoto", "Foto do canhoto / comprovante", "FOTO", 1, None, 4),
    ("NAO_ENTREGUE", "motivo_ocorrencia", "Motivo da ocorrencia", "SELECAO", 1, None, 1),
    ("NAO_ENTREGUE", "foto_ocorrencia", "Foto (opcional)", "FOTO", 0, None, 2),
]


class _BaseCorrecao(_Base):
    def setUp(self):
        super().setUp()
        self.conn.execute("""CREATE TABLE checklist_modelo (
            id INTEGER PRIMARY KEY AUTOINCREMENT, fluxo TEXT NOT NULL, chave TEXT NOT NULL,
            rotulo TEXT NOT NULL, tipo_campo TEXT NOT NULL, obrigatorio INTEGER NOT NULL DEFAULT 1,
            opcoes TEXT, aviso TEXT, ordem INTEGER NOT NULL DEFAULT 0, ativo INTEGER NOT NULL DEFAULT 1)""")
        self.conn.executemany("INSERT INTO checklist_modelo (fluxo, chave, rotulo, tipo_campo, obrigatorio, opcoes, ordem) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)", _MODELO)
        self.conn.commit()
        self.fotos = self.pasta / "comprovantes"
        self.rota = self._rota("2026-10-01")

    def _rota(self, data_rota, provedor="VUUPT"):
        cur = self.conn.execute("INSERT INTO nucleo_rotas (data_rota, nome, provedor, agent_id, motorista_nome, status) "
                                "VALUES (?, 'Rota 1', ?, 501, 'JOAO', 'CONCLUIDA')", (data_rota, provedor))
        self.conn.commit()
        return cur.lastrowid

    def _parada(self, codigo="PS-1", service_id=555, situacao="ENTREGUE", rota_id=None):
        cur = self.conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, destinatario_nome, situacao) "
                                "VALUES (?, 1, ?, ?, 'MERCADO CENTRAL', ?)",
                                (rota_id or self.rota, service_id, codigo, situacao))
        self.conn.commit()
        return cur.lastrowid

    def _corrigir(self, parada_id, campos=None, arquivos=None, usuario="ana"):
        campos = {"nome_recebedor": "Jose", "vinculo": "Porteiro"} if campos is None else campos
        arquivos = {"foto_canhoto": JPG} if arquivos is None else arquivos
        return cc.registrar_correcao(self.conn, parada_id, campos, arquivos, usuario, self.fotos)


class TestRegistrarCorrecao(_BaseCorrecao):
    def test_grava_arquivo_linha_e_evento(self):
        pid = self._parada()
        r = self._corrigir(pid)
        self.assertEqual(r["fluxo"], "ENTREGUE")
        self.assertEqual(r["arquivos"], 1)
        linha = self.conn.execute("SELECT * FROM nucleo_comprovantes WHERE parada_id = ?", (pid,)).fetchone()
        self.assertEqual((linha["tipo"], linha["origem"], linha["enviado_por"], linha["resultado_validacao"],
                          linha["validado_por"], linha["correcao_id"]),
                         ("CANHOTO", "PAINEL", "ana", "APROVADO", "HUMANO", r["correcao_id"]))
        caminho = Path(linha["caminho_local"])
        self.assertTrue((caminho if caminho.is_absolute() else _RAIZ / caminho).exists())
        evento = self.conn.execute("SELECT * FROM nucleo_eventos WHERE parada_id = ? AND tipo = ?",
                                   (pid, cc.EVENTO)).fetchone()
        self.assertEqual(evento["origem"], "PAINEL")
        self.assertIn('"nome_recebedor": "Jose"', evento["dados_json"])

    def test_nao_apaga_o_comprovante_do_motorista(self):
        pid = self._parada()
        self.conn.execute("INSERT INTO nucleo_comprovantes (uuid, parada_id, tipo, caminho_local) "
                          "VALUES ('app-1', ?, 'CANHOTO', 'dados/comprovantes/PS-1/x.jpg')", (pid,))
        self.conn.commit()
        self._corrigir(pid)
        n = self.conn.execute("SELECT COUNT(*) FROM nucleo_comprovantes WHERE parada_id = ?", (pid,)).fetchone()[0]
        self.assertEqual(n, 2)

    def test_documento_vira_comprovante_documento(self):
        pid = self._parada()
        self._corrigir(pid, arquivos={"foto_canhoto": JPG, "documento": PNG})
        tipos = sorted(r[0] for r in self.conn.execute("SELECT tipo FROM nucleo_comprovantes WHERE parada_id = ?", (pid,)))
        self.assertEqual(tipos, ["CANHOTO", "DOCUMENTO"])

    def test_insucesso_usa_checklist_nao_entregue_e_aceita_sem_arquivo(self):
        pid = self._parada(situacao="INSUCESSO")
        r = self._corrigir(pid, campos={"motivo_ocorrencia": "Cliente fechado"}, arquivos={})
        self.assertEqual((r["fluxo"], r["arquivos"]), ("NAO_ENTREGUE", 0))

    def test_parada_sem_resultado_da_409(self):
        pid = self._parada(situacao="PENDENTE")
        with self.assertRaises(cc.OperacaoInvalida) as ctx:
            self._corrigir(pid)
        self.assertEqual(ctx.exception.codigo, 409)

    def test_parada_inexistente_da_404(self):
        with self.assertRaises(cc.OperacaoInvalida) as ctx:
            self._corrigir(99999)
        self.assertEqual(ctx.exception.codigo, 404)

    def test_obrigatorio_faltando_lista_os_rotulos(self):
        pid = self._parada()
        with self.assertRaises(cc.OperacaoInvalida) as ctx:
            self._corrigir(pid, campos={"nome_recebedor": "  "}, arquivos={})
        msg = ctx.exception.mensagem
        for rotulo in ("Nome de quem recebeu", "Quem e essa pessoa?", "Foto do canhoto / comprovante"):
            self.assertIn(rotulo, msg)

    def test_campo_de_outro_fluxo_e_recusado(self):
        pid = self._parada()
        with self.assertRaises(cc.OperacaoInvalida):
            self._corrigir(pid, campos={"nome_recebedor": "Jose", "vinculo": "Porteiro", "motivo_ocorrencia": "x"})

    def test_recusa_conteudo_que_nao_e_imagem_nem_pdf(self):
        pid = self._parada()
        with self.assertRaises(cc.OperacaoInvalida) as ctx:
            self._corrigir(pid, arquivos={"foto_canhoto": b"<html>nao sou foto</html>"})
        self.assertEqual(ctx.exception.codigo, 400)
        self.assertFalse(self.fotos.exists() and any(self.fotos.rglob("*")))

    def test_recusa_arquivo_acima_de_10mb(self):
        pid = self._parada()
        with self.assertRaises(cc.OperacaoInvalida):
            self._corrigir(pid, arquivos={"foto_canhoto": JPG + b"0" * cc.LIMITE_BYTES})

    def test_falha_no_banco_apaga_arquivos_gravados(self):
        pid = self._parada()
        with mock.patch.object(cc, "registrar_evento", side_effect=RuntimeError("banco caiu")):
            with self.assertRaises(RuntimeError):
                self._corrigir(pid)
        self.assertEqual(list(self.fotos.rglob("*.jpg")), [])
        n = self.conn.execute("SELECT COUNT(*) FROM nucleo_comprovantes").fetchone()[0]
        self.assertEqual(n, 0)


class TestCorrecaoVigente(_BaseCorrecao):
    def test_sem_correcao_devolve_none(self):
        self._parada()
        self.assertIsNone(cc.correcao_vigente(self.conn, 555))

    def test_correcao_vigente_monta_campos_rotulos_e_arquivos(self):
        pid = self._parada()
        self._corrigir(pid, arquivos={"foto_canhoto": PDF})
        c = cc.correcao_vigente(self.conn, 555)
        self.assertEqual(c["parada_id"], pid)
        self.assertEqual(c["codigo"], "PS-1")
        self.assertEqual(c["campos"], {"nome_recebedor": "Jose", "vinculo": "Porteiro"})
        self.assertEqual(c["rotulos"]["nome_recebedor"], "Nome de quem recebeu")
        self.assertEqual(len(c["arquivos"]), 1)
        self.assertTrue(c["arquivos"][0]["pdf"])
        self.assertTrue(c["arquivos"][0]["caminho"].is_absolute())

    def test_correcao_vigente_e_a_mais_recente(self):
        pid = self._parada()
        self._corrigir(pid, campos={"nome_recebedor": "Primeiro", "vinculo": "Porteiro"})
        segunda = self._corrigir(pid, campos={"nome_recebedor": "Segundo", "vinculo": "Gerente"},
                                 arquivos={"foto_canhoto": PNG})
        c = cc.correcao_vigente(self.conn, 555)
        self.assertEqual(c["correcao_id"], segunda["correcao_id"])
        self.assertEqual(c["campos"]["nome_recebedor"], "Segundo")
        self.assertEqual([a["caminho"].suffix for a in c["arquivos"]], [".png"])

    def test_correcao_vigente_separa_service_id(self):
        # reentrega com o MESMO código, service_id diferente
        p1 = self._parada(codigo="PS-1", service_id=555)
        self._parada(codigo="PS-1", service_id=777)
        self._corrigir(p1)
        self.assertIsNotNone(cc.correcao_vigente(self.conn, 555))
        self.assertIsNone(cc.correcao_vigente(self.conn, 777))

    def test_correcao_da_parada(self):
        pid = self._parada()
        self.assertIsNone(cc.correcao_da_parada(self.conn, pid))
        self._corrigir(pid)
        self.assertEqual(cc.correcao_da_parada(self.conn, pid)["enviado_por"], "ana")


class TestValoresIniciais(_BaseCorrecao):
    def test_vazio_sem_nada(self):
        self.assertEqual(cc.valores_iniciais(self.conn, self._parada()), {})

    def test_usa_o_checklist_do_motorista_sem_campos_de_arquivo(self):
        pid = self._parada()
        self.conn.execute("INSERT INTO nucleo_eventos (rota_id, parada_id, tipo, origem, ocorrido_em, dados_json) "
                          "VALUES (?, ?, 'ENTREGUE', 'APP', '2026-10-01 10:00:00', ?)",
                          (self.rota, pid, '{"checklist": {"nome_recebedor": "Maria", "foto_canhoto": "uuid-1"}}'))
        self.conn.commit()
        self.assertEqual(cc.valores_iniciais(self.conn, pid), {"nome_recebedor": "Maria"})

    def test_correcao_anterior_vence_o_motorista(self):
        pid = self._parada()
        self.conn.execute("INSERT INTO nucleo_eventos (rota_id, parada_id, tipo, origem, ocorrido_em, dados_json) "
                          "VALUES (?, ?, 'ENTREGUE', 'APP', '2026-10-01 10:00:00', ?)",
                          (self.rota, pid, '{"checklist": {"nome_recebedor": "Maria"}}'))
        self.conn.commit()
        self._corrigir(pid, campos={"nome_recebedor": "Jose", "vinculo": "Porteiro"})
        self.assertEqual(cc.valores_iniciais(self.conn, pid), {"nome_recebedor": "Jose", "vinculo": "Porteiro"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3.11 -m unittest nucleo.test_correcao_comprovante -v`
Expected: ImportError (`cannot import name 'correcao_comprovante'`).

- [ ] **Step 3: Implementar `nucleo/correcao_comprovante.py`**

```python
# -*- coding: utf-8 -*-
"""
nucleo/correcao_comprovante.py

Correção de canhoto/checklist pelo escritório (Hugo, 05/10/2026 --
docs/superpowers/specs/2026-10-05-correcao-canhoto-painel-design.md).

O escritório preenche o checklist do resultado JÁ marcado na parada (não
muda o resultado). Cada envio é um retrato completo: os campos de texto
vão no evento COMPROVANTE_CORRIGIDO e os arquivos viram linhas em
nucleo_comprovantes (origem PAINEL, mesmo correcao_id). Nada é apagado;
vale o envio mais recente. O portal acha a correção pelo service_id da
parada, nunca só pelo código (reentrega com o mesmo código sobrescreve
nucleo_pedidos).
"""
import hashlib
import json
import sqlite3
import uuid as uuidlib
from datetime import date, timedelta
from pathlib import Path

from werkzeug.utils import secure_filename

from nucleo import banco
from nucleo.operacao import OperacaoInvalida, carregar_checklist
from nucleo.rotas import registrar_evento

EVENTO = "COMPROVANTE_CORRIGIDO"
LIMITE_BYTES = 10 * 1024 * 1024
TIPOS_ARQUIVO = {"FOTO", "DOCUMENTO"}   # tipo_campo do checklist_modelo que vira arquivo

FLUXO_POR_SITUACAO = {
    banco.PARADA_ENTREGUE: "ENTREGUE",
    banco.PARADA_PARCIAL: "PARCIAL",
    banco.PARADA_INSUCESSO: "NAO_ENTREGUE",
}
# Mesmo mapa do app (app_motorista/app/parada/[id].tsx)
TIPO_POR_CHAVE = {
    "foto_canhoto": "CANHOTO", "foto_nf_devolucao": "NF_DEVOLUCAO", "foto_produto_devolvido": "PRODUTO",
    "foto_ocorrencia": "OCORRENCIA", "documento": "DOCUMENTO",
}

_RAIZ = Path(__file__).parent.parent


def _extensao(conteudo: bytes) -> str:
    """Pelo conteúdo, não pelo nome: um .jpg que é HTML não passa."""
    if conteudo.startswith(b"%PDF"):
        return "pdf"
    if conteudo.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if conteudo.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if conteudo[:4] == b"RIFF" and conteudo[8:12] == b"WEBP":
        return "webp"
    raise OperacaoInvalida("Arquivo precisa ser foto (jpg, png, webp) ou PDF.")


def _ler_json(v) -> dict:
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}


def _parada(conn: sqlite3.Connection, parada_id: int) -> sqlite3.Row:
    p = conn.execute("SELECT * FROM nucleo_paradas WHERE id = ?", (parada_id,)).fetchone()
    if not p:
        raise OperacaoInvalida("Parada não encontrada.", 404)
    return p


def fluxo_da_parada(parada) -> str:
    fluxo = FLUXO_POR_SITUACAO.get(parada["situacao"])
    if not fluxo:
        raise OperacaoInvalida("Esta parada ainda não tem resultado de entrega para corrigir.", 409)
    return fluxo


def modelo_do_fluxo(conn: sqlite3.Connection, fluxo: str) -> list[dict]:
    return carregar_checklist(conn)["fluxos"].get(fluxo, [])


def registrar_correcao(conn: sqlite3.Connection, parada_id: int, campos: dict, arquivos: dict,
                       usuario: str, pasta: Path) -> dict:
    """Grava um envio completo do formulário. `campos` = chave -> texto;
    `arquivos` = chave -> bytes. Arquivos são gravados antes do INSERT e
    apagados se qualquer passo do banco falhar."""
    p = _parada(conn, parada_id)
    fluxo = fluxo_da_parada(p)
    modelo = modelo_do_fluxo(conn, fluxo)
    if not modelo:
        raise OperacaoInvalida(f"Sem checklist cadastrado para {fluxo}.", 409)
    chaves_arquivo = {c["chave"] for c in modelo if c["tipo"] in TIPOS_ARQUIVO}
    chaves_texto = {c["chave"] for c in modelo} - chaves_arquivo
    campos = {k: str(v).strip() for k, v in (campos or {}).items() if str(v or "").strip()}
    arquivos = {k: v for k, v in (arquivos or {}).items() if v}

    estranhas = (set(campos) - chaves_texto) | (set(arquivos) - chaves_arquivo)
    if estranhas:
        raise OperacaoInvalida(f"Campo fora do checklist de {fluxo}: {', '.join(sorted(estranhas))}.")
    faltando = [c["rotulo"] for c in modelo if c["obrigatorio"]
                and c["chave"] not in (arquivos if c["tipo"] in TIPOS_ARQUIVO else campos)]
    if faltando:
        raise OperacaoInvalida("Falta preencher: " + "; ".join(faltando) + ".")
    extensoes = {}
    for chave, conteudo in arquivos.items():
        if len(conteudo) > LIMITE_BYTES:
            raise OperacaoInvalida(f"O arquivo de '{chave}' passa de 10 MB.")
        extensoes[chave] = _extensao(conteudo)

    correcao_id = uuidlib.uuid4().hex[:12]
    agora = banco.agora()
    destino = Path(pasta) / secure_filename(p["codigo"] or f"parada_{parada_id}")
    gravados: list[Path] = []
    try:
        for chave, conteudo in arquivos.items():
            destino.mkdir(parents=True, exist_ok=True)
            caminho = destino / f"painel_{correcao_id}_{chave}.{extensoes[chave]}"
            caminho.write_bytes(conteudo)
            gravados.append(caminho)
            relativo = str(caminho.relative_to(_RAIZ)) if _RAIZ in caminho.parents else str(caminho)
            conn.execute("""
                INSERT INTO nucleo_comprovantes (uuid, rota_id, parada_id, tipo, caminho_local, sha256,
                                                 tamanho_bytes, capturado_em, validado_em, validado_por,
                                                 resultado_validacao, origem, enviado_por, correcao_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'HUMANO', 'APROVADO', ?, ?, ?)
            """, (f"painel-{correcao_id}-{chave}", p["rota_id"], parada_id, TIPO_POR_CHAVE.get(chave, "OCORRENCIA"),
                  relativo, hashlib.sha256(conteudo).hexdigest(), len(conteudo), agora, agora,
                  banco.ORIGEM_PAINEL, usuario, correcao_id))
        registrar_evento(conn, EVENTO, banco.ORIGEM_PAINEL, agora, rota_id=p["rota_id"], parada_id=parada_id,
                         dados={"correcao_id": correcao_id, "enviado_por": usuario, "fluxo": fluxo,
                                "checklist": campos, "arquivos": sorted(arquivos)})
        conn.execute("UPDATE canhoto_marcacoes SET resolvido_em = ?, resolvido_por = ? "
                     "WHERE parada_id = ? AND resolvido_em IS NULL", (agora, usuario, parada_id))
        conn.commit()
    except Exception:
        conn.rollback()
        for caminho in gravados:
            caminho.unlink(missing_ok=True)
        raise
    return {"correcao_id": correcao_id, "fluxo": fluxo, "arquivos": len(arquivos)}


def _montar_correcao(conn: sqlite3.Connection, evento: sqlite3.Row) -> dict:
    dados = _ler_json(evento["dados_json"])
    fluxo = dados.get("fluxo") or ""
    arquivos = []
    for r in conn.execute("SELECT id, uuid, tipo, caminho_local FROM nucleo_comprovantes "
                          "WHERE correcao_id = ? ORDER BY id", (dados.get("correcao_id"),)):
        caminho = Path(r["caminho_local"] or "")
        if not caminho.is_absolute():
            caminho = _RAIZ / caminho
        chave = (r["uuid"] or "").split("-", 2)[-1]
        arquivos.append({"id": r["id"], "chave": chave, "tipo": r["tipo"], "caminho": caminho,
                         "pdf": caminho.suffix.lower() == ".pdf"})
    return {
        "correcao_id": dados.get("correcao_id"), "parada_id": evento["parada_id"], "codigo": evento["codigo"],
        "destinatario_nome": evento["destinatario_nome"], "data_rota": evento["data_rota"],
        "completed_at": evento["completed_at"], "fluxo": fluxo, "enviado_por": dados.get("enviado_por"),
        "enviado_em": evento["ocorrido_em"], "campos": dados.get("checklist") or {},
        "rotulos": {c["chave"]: c["rotulo"] for c in modelo_do_fluxo(conn, fluxo)}, "arquivos": arquivos,
    }


_SQL_ULTIMA = """
    SELECT e.dados_json, e.ocorrido_em, e.parada_id, p.codigo, p.destinatario_nome, p.completed_at, r.data_rota
    FROM nucleo_eventos e
    JOIN nucleo_paradas p ON p.id = e.parada_id
    LEFT JOIN nucleo_rotas r ON r.id = p.rota_id
    WHERE e.tipo = ? AND {filtro}
    ORDER BY e.id DESC LIMIT 1
"""


def correcao_da_parada(conn: sqlite3.Connection, parada_id: int) -> dict | None:
    ev = conn.execute(_SQL_ULTIMA.format(filtro="e.parada_id = ?"), (EVENTO, parada_id)).fetchone()
    return _montar_correcao(conn, ev) if ev else None


def correcao_vigente(conn: sqlite3.Connection, service_id: int) -> dict | None:
    """Última correção de qualquer parada com esse service_id (o mesmo
    serviço da VUUPT pode ter passado por mais de uma rota)."""
    ev = conn.execute(_SQL_ULTIMA.format(filtro="p.service_id = ?"), (EVENTO, int(service_id))).fetchone()
    return _montar_correcao(conn, ev) if ev else None


def valores_iniciais(conn: sqlite3.Connection, parada_id: int) -> dict:
    """Pré-preenchimento do formulário: a última correção; senão o
    checklist que o motorista mandou pelo app (só os campos de texto)."""
    anterior = correcao_da_parada(conn, parada_id)
    if anterior:
        return dict(anterior["campos"])
    p = _parada(conn, parada_id)
    fluxo = FLUXO_POR_SITUACAO.get(p["situacao"])
    texto = {c["chave"] for c in modelo_do_fluxo(conn, fluxo) if c["tipo"] not in TIPOS_ARQUIVO} if fluxo else set()
    for r in conn.execute("SELECT dados_json FROM nucleo_eventos WHERE parada_id = ? AND origem = ? "
                          "ORDER BY id DESC", (parada_id, banco.ORIGEM_APP)):
        checklist = _ler_json(r["dados_json"]).get("checklist")
        if isinstance(checklist, dict) and checklist:
            return {k: str(v) for k, v in checklist.items() if k in texto and v not in (None, "")}
    return {}
```

Conferir antes de rodar: `banco.ORIGEM_APP` existe (`grep -n "ORIGEM_APP" nucleo/banco.py`); se o nome for outro, usar o que estiver lá.

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3.11 -m unittest nucleo.test_correcao_comprovante -v` → todos OK.

- [ ] **Step 5: Commit (quando o Hugo autorizar)**

```bash
git add nucleo/correcao_comprovante.py nucleo/test_correcao_comprovante.py
git commit -m "Nucleo: registrar correcao de canhoto pelo escritorio e achar a vigente por service_id"
```

---

### Task 3: Marcar canhoto ruim e fila "a resolver"

**Files:**
- Modify: `nucleo/correcao_comprovante.py` (acrescentar no fim)
- Test: `nucleo/test_correcao_comprovante.py` (acrescentar)

**Interfaces:**
- Consumes: Task 2 (`_parada`, `registrar_correcao`, `EVENTO`).
- Produces:
  - `marcar_canhoto_ruim(conn, parada_id: int, motivo: str, usuario: str) -> int` (id da marcação aberta; idempotente)
  - `listar_canhotos_a_resolver(conn, dias: int = 30, hoje: date | None = None) -> list[dict]` — cada item: `{"parada_id", "codigo", "destinatario_nome", "remetente_nome", "situacao", "data_rota", "rota_nome", "motorista_nome", "provedor", "motivo_fila": "MARCADO" | "SEM_CANHOTO", "motivo_marcacao", "marcado_por", "marcado_em"}`

- [ ] **Step 1: Write the failing tests**

```python
class TestFila(_BaseCorrecao):
    HOJE = __import__("datetime").date(2026, 10, 5)

    def _pedido(self, service_id, qtd_checklists):
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, qtd_checklists) VALUES (?, ?, ?)",
                          (f"PS-{service_id}", service_id, qtd_checklists))
        self.conn.commit()

    def _fila(self):
        return {i["parada_id"]: i for i in cc.listar_canhotos_a_resolver(self.conn, hoje=self.HOJE)}

    def test_vuupt_sem_checklist_entra(self):
        pid = self._parada(codigo="PS-555", service_id=555)
        self._pedido(555, 0)
        self.assertEqual(self._fila()[pid]["motivo_fila"], "SEM_CANHOTO")

    def test_vuupt_com_checklist_ou_desconhecido_nao_entra(self):
        com = self._parada(codigo="PS-555", service_id=555)
        self._pedido(555, 1)
        nulo = self._parada(codigo="PS-556", service_id=556)
        self._pedido(556, None)
        fila = self._fila()
        self.assertNotIn(com, fila)
        self.assertNotIn(nulo, fila)

    def test_app_sem_foto_de_canhoto_entra_e_com_foto_nao(self):
        rota_app = self._rota("2026-10-02", provedor="APP")
        sem = self._parada(codigo="PS-10", service_id=None, rota_id=rota_app)
        com = self._parada(codigo="PS-11", service_id=None, rota_id=rota_app)
        self.conn.execute("INSERT INTO nucleo_comprovantes (uuid, parada_id, tipo) VALUES ('u1', ?, 'CANHOTO')", (com,))
        self.conn.commit()
        fila = self._fila()
        self.assertIn(sem, fila)
        self.assertNotIn(com, fila)

    def test_insucesso_sem_canhoto_nao_entra(self):
        pid = self._parada(codigo="PS-555", service_id=555, situacao="INSUCESSO")
        self._pedido(555, 0)
        self.assertNotIn(pid, self._fila())

    def test_fora_da_janela_de_30_dias_nao_entra(self):
        velha = self._rota("2026-09-01")
        pid = self._parada(codigo="PS-555", service_id=555, rota_id=velha)
        self._pedido(555, 0)
        self.assertNotIn(pid, self._fila())

    def test_correcao_tira_da_fila(self):
        pid = self._parada(codigo="PS-555", service_id=555)
        self._pedido(555, 0)
        self._corrigir(pid)
        self.assertNotIn(pid, self._fila())

    def test_marcado_entra_mesmo_com_canhoto_e_sai_com_correcao(self):
        pid = self._parada(codigo="PS-555", service_id=555)
        self._pedido(555, 1)
        cc.marcar_canhoto_ruim(self.conn, pid, "foto ilegível", "ana")
        item = self._fila()[pid]
        self.assertEqual((item["motivo_fila"], item["motivo_marcacao"], item["marcado_por"]),
                         ("MARCADO", "foto ilegível", "ana"))
        self._corrigir(pid, usuario="bia")
        self.assertNotIn(pid, self._fila())
        m = self.conn.execute("SELECT resolvido_por FROM canhoto_marcacoes WHERE parada_id = ?", (pid,)).fetchone()
        self.assertEqual(m["resolvido_por"], "bia")

    def test_marcar_de_novo_depois_da_correcao_volta_pra_fila(self):
        pid = self._parada(codigo="PS-555", service_id=555)
        self._corrigir(pid)
        cc.marcar_canhoto_ruim(self.conn, pid, "cliente reclamou de novo", "ana")
        self.assertEqual(self._fila()[pid]["motivo_fila"], "MARCADO")

    def test_marcar_e_idempotente_enquanto_aberta(self):
        pid = self._parada()
        a = cc.marcar_canhoto_ruim(self.conn, pid, "ruim", "ana")
        b = cc.marcar_canhoto_ruim(self.conn, pid, "ruim de novo", "bia")
        self.assertEqual(a, b)
        n = self.conn.execute("SELECT COUNT(*) FROM canhoto_marcacoes").fetchone()[0]
        self.assertEqual(n, 1)

    def test_marcar_sem_motivo_e_recusado(self):
        pid = self._parada()
        with self.assertRaises(cc.OperacaoInvalida):
            cc.marcar_canhoto_ruim(self.conn, pid, "   ", "ana")

    def test_marcar_parada_inexistente_da_404(self):
        with self.assertRaises(cc.OperacaoInvalida) as ctx:
            cc.marcar_canhoto_ruim(self.conn, 99999, "ruim", "ana")
        self.assertEqual(ctx.exception.codigo, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3.11 -m unittest nucleo.test_correcao_comprovante.TestFila -v`
Expected: AttributeError (`listar_canhotos_a_resolver` / `marcar_canhoto_ruim` não existem).

- [ ] **Step 3: Implementar (fim de `nucleo/correcao_comprovante.py`)**

```python


# ── Fila "Canhotos a resolver" ─────────────────────────────────────────────────

def marcar_canhoto_ruim(conn: sqlite3.Connection, parada_id: int, motivo: str, usuario: str) -> int:
    """Põe a parada na fila. Enquanto houver marcação aberta, devolve a
    mesma (não empilha)."""
    motivo = (motivo or "").strip()[:300]
    if not motivo:
        raise OperacaoInvalida("Diga o motivo (ex.: foto ilegível, cliente reclamou).")
    _parada(conn, parada_id)
    aberta = conn.execute("SELECT id FROM canhoto_marcacoes WHERE parada_id = ? AND resolvido_em IS NULL",
                          (parada_id,)).fetchone()
    if aberta:
        return aberta["id"]
    cur = conn.execute("INSERT INTO canhoto_marcacoes (parada_id, motivo, marcado_por, marcado_em) VALUES (?, ?, ?, ?)",
                       (parada_id, motivo, usuario, banco.agora()))
    conn.commit()
    return cur.lastrowid


def listar_canhotos_a_resolver(conn: sqlite3.Connection, dias: int = 30, hoje: date | None = None) -> list[dict]:
    """Paradas dos últimos `dias` que precisam de canhoto:
      - MARCADO: alguém marcou como ruim e ainda não corrigiu;
      - SEM_CANHOTO: entregue/parcial sem foto (VUUPT: qtd_checklists = 0
        no pedido do MESMO service_id -- nulo é "não sabemos" e fica de
        fora; APP: nenhum comprovante CANHOTO) e sem correção do escritório."""
    desde = ((hoje or date.today()) - timedelta(days=dias)).isoformat()
    sql = """
        SELECT p.id AS parada_id, p.codigo, p.destinatario_nome, p.remetente_nome, p.situacao,
               r.data_rota, r.nome AS rota_nome, r.motorista_nome, r.provedor,
               pe.qtd_checklists,
               m.motivo AS motivo_marcacao, m.marcado_por, m.marcado_em,
               EXISTS (SELECT 1 FROM nucleo_comprovantes c WHERE c.parada_id = p.id AND c.tipo = 'CANHOTO') AS tem_canhoto,
               EXISTS (SELECT 1 FROM nucleo_eventos e WHERE e.parada_id = p.id AND e.tipo = ?) AS corrigida
        FROM nucleo_paradas p
        JOIN nucleo_rotas r ON r.id = p.rota_id
        LEFT JOIN nucleo_pedidos pe ON p.service_id IS NOT NULL AND pe.vuupt_service_id = p.service_id
        LEFT JOIN canhoto_marcacoes m ON m.parada_id = p.id AND m.resolvido_em IS NULL
        WHERE r.data_rota >= ?
        ORDER BY r.data_rota DESC, p.codigo
    """
    saida = []
    for r in conn.execute(sql, (EVENTO, desde)):
        d = dict(r)
        if d["motivo_marcacao"]:
            d["motivo_fila"] = "MARCADO"
        elif d["situacao"] in (banco.PARADA_ENTREGUE, banco.PARADA_PARCIAL) and not d["corrigida"] and (
                (d["provedor"] == banco.PROVEDOR_VUUPT and d["qtd_checklists"] == 0)
                or (d["provedor"] == banco.PROVEDOR_APP and not d["tem_canhoto"])):
            d["motivo_fila"] = "SEM_CANHOTO"
        else:
            continue
        for k in ("qtd_checklists", "tem_canhoto", "corrigida"):
            d.pop(k)
        saida.append(d)
    return saida
```

Nota: o `LEFT JOIN nucleo_pedidos` pode duplicar a parada se houver dois pedidos com o mesmo `vuupt_service_id`. Se o teste mostrar duplicata, trocar o join por subconsulta escalar:
`(SELECT pe.qtd_checklists FROM nucleo_pedidos pe WHERE pe.vuupt_service_id = p.service_id LIMIT 1) AS qtd_checklists`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3.11 -m unittest nucleo.test_correcao_comprovante -v` → todos OK.

- [ ] **Step 5: Commit (quando o Hugo autorizar)**

```bash
git add nucleo/correcao_comprovante.py nucleo/test_correcao_comprovante.py
git commit -m "Nucleo: fila de canhotos a resolver (sem canhoto + marcados como ruins)"
```

---

### Task 4: Portal serve a correção

**Files:**
- Create: `portal_cliente/canhoto_corrigido.py`
- Modify: `portal_cliente/app.py` (`api_canhoto`, ~linha 508), `portal_cliente/link_canhoto.py` (`canhoto_por_link`, ~linha 56)
- Test: `portal_cliente/test_canhoto_corrigido.py`

**Interfaces:**
- Consumes: Task 2 (`correcao_vigente(conn, service_id) -> dict | None`); `portal_cliente/cotacao_pdf.py` (`A4`, `DPI`, `MARGEM`, `NAVY`, `CINZA_TXT`, `PRETO`, `_fonte`, `_quebrar`, `_nova_pagina`, `_logo`); `nucleo.banco.conectar()`.
- Produces:
  - `canhoto_corrigido.pdf_para_servico(service_id: int, conn=None) -> Path | None` — `None` = sem correção ou falha (quem chama segue para a Vuupt).
  - `canhoto_corrigido.montar_pdf(correcao: dict) -> bytes`

- [ ] **Step 1: Write the failing tests**

Criar `portal_cliente/test_canhoto_corrigido.py`:

```python
# -*- coding: utf-8 -*-
"""Portal prefere o canhoto corrigido pelo escritório (05/10).

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_canhoto_corrigido
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader, PdfWriter

_AQUI = Path(__file__).parent
sys.path.insert(0, str(_AQUI.parent))
sys.path.insert(0, str(_AQUI))

import canhoto_corrigido  # noqa: E402
import link_canhoto  # noqa: E402


def _jpg(cor="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), cor).save(buf, format="JPEG")
    return buf.getvalue()


def _pdf(paginas=1) -> bytes:
    w = PdfWriter()
    for _ in range(paginas):
        w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class _BaseArquivos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.pasta = Path(self._tmp.name)
        p = patch.object(canhoto_corrigido, "CANHOTOS_DIR", self.pasta / "canhotos")
        p.start()
        self.addCleanup(p.stop)

    def _arquivo(self, nome, conteudo):
        caminho = self.pasta / nome
        caminho.write_bytes(conteudo)
        return {"id": 1, "chave": "foto_canhoto", "tipo": "CANHOTO", "caminho": caminho,
                "pdf": caminho.suffix == ".pdf"}

    def _correcao(self, arquivos, campos=None, cid="abc123"):
        return {"correcao_id": cid, "parada_id": 1, "codigo": "PS-1", "destinatario_nome": "MERCADO",
                "data_rota": "2026-10-01", "completed_at": "2026-10-01 10:00:00", "fluxo": "ENTREGUE",
                "enviado_por": "ana", "enviado_em": "2026-10-05 09:00:00",
                "campos": {"nome_recebedor": "Jose"} if campos is None else campos,
                "rotulos": {"nome_recebedor": "Nome de quem recebeu"}, "arquivos": arquivos}


class TestMontarPdf(_BaseArquivos):
    def test_campos_mais_uma_foto_dao_duas_paginas(self):
        pdf = canhoto_corrigido.montar_pdf(self._correcao([self._arquivo("a.jpg", _jpg())]))
        self.assertEqual(len(PdfReader(io.BytesIO(pdf)).pages), 2)

    def test_pdf_enviado_entra_como_paginas_seguintes(self):
        c = self._correcao([self._arquivo("a.jpg", _jpg()), self._arquivo("b.pdf", _pdf(paginas=2))])
        self.assertEqual(len(PdfReader(io.BytesIO(canhoto_corrigido.montar_pdf(c))).pages), 4)


class TestPdfParaServico(_BaseArquivos):
    def _com(self, correcao):
        return patch.object(canhoto_corrigido, "_correcao_vigente", return_value=correcao)

    def test_sem_correcao_devolve_none(self):
        with self._com(None):
            self.assertIsNone(canhoto_corrigido.pdf_para_servico(555))

    def test_um_pdf_sem_campos_vai_como_veio(self):
        arq = self._arquivo("a.pdf", _pdf())
        with self._com(self._correcao([arq], campos={})):
            self.assertEqual(canhoto_corrigido.pdf_para_servico(555), arq["caminho"])

    def test_monta_e_guarda_em_cache(self):
        with self._com(self._correcao([self._arquivo("a.jpg", _jpg())])):
            pdf = canhoto_corrigido.pdf_para_servico(555)
        self.assertEqual(pdf.name, "canhoto_PS-1_painel_abc123.pdf")
        self.assertTrue(pdf.exists())

    def test_pdf_tem_so_arquivos_da_ultima(self):
        # a correção vigente já vem filtrada pelo correcao_id; o PDF não busca nada além dela
        with self._com(self._correcao([self._arquivo("b.jpg", _jpg("blue"))], cid="seg")):
            pdf = canhoto_corrigido.pdf_para_servico(555)
        self.assertEqual(len(PdfReader(pdf).pages), 2)

    def test_falha_ao_montar_cai_para_vuupt(self):
        quebrado = self._arquivo("a.jpg", b"\xff\xd8\xff corrompido")
        with self._com(self._correcao([quebrado])):
            self.assertIsNone(canhoto_corrigido.pdf_para_servico(555))


class TestRotasDoPortal(_BaseArquivos):
    @classmethod
    def setUpClass(cls):
        import app as portal
        cls.portal = portal
        portal.app.config["TESTING"] = True

    def setUp(self):
        super().setUp()
        self.tc = self.portal.app.test_client()
        self.corrigido = self.pasta / "corrigido.pdf"
        self.corrigido.write_bytes(_pdf())
        self.vuupt = self.pasta / "vuupt.pdf"
        self.vuupt.write_bytes(b"%PDF-1.4 vuupt")

    def _abrir_link(self, servico, corrigido):
        dados = self.portal.dados
        token = link_canhoto.gerar(555, 101, self.portal._SECRET)
        with patch.object(dados, "buscar_servico", return_value=servico), \
             patch.object(dados, "checklist_id_do_servico", return_value=9), \
             patch.object(dados, "baixar_canhoto_pdf", return_value=self.vuupt) as vuupt, \
             patch.object(canhoto_corrigido, "pdf_para_servico", return_value=corrigido):
            r = self.tc.get(f"/c/{token}")
            corpo = r.get_data()
        return r, corpo, vuupt

    def test_link_prefere_a_correcao(self):
        r, corpo, vuupt = self._abrir_link({"id": 555, "sender_id": 101, "code": "#PS-1"}, self.corrigido)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(corpo, self.corrigido.read_bytes())
        vuupt.assert_not_called()

    def test_link_sem_correcao_usa_vuupt(self):
        r, corpo, vuupt = self._abrir_link({"id": 555, "sender_id": 101, "code": "#PS-1"}, None)
        self.assertEqual(corpo, b"%PDF-1.4 vuupt")
        vuupt.assert_called_once()

    def test_link_de_outro_embarcador_da_404_mesmo_com_correcao(self):
        r, _, _ = self._abrir_link({"id": 555, "sender_id": 999, "code": "#PS-1"}, self.corrigido)
        self.assertEqual(r.status_code, 404)

    def test_api_logada_prefere_a_correcao(self):
        dados = self.portal.dados
        with self.tc.session_transaction() as s:
            s.update(self._sessao_cliente())
        with patch.object(dados, "buscar_servico", return_value={"id": 555, "sender_id": 22812827, "code": "#PS-1"}), \
             patch.object(dados, "checklist_id_do_servico", return_value=None), \
             patch.object(canhoto_corrigido, "pdf_para_servico", return_value=self.corrigido):
            r = self.tc.get("/api/canhoto/555")
            corpo = r.get_data()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(corpo, self.corrigido.read_bytes())
```

Para `_sessao_cliente`: abrir `portal_cliente/test_acesso_travas.py` e copiar o jeito que ele monta a sessão de cliente logado (chaves de sessão que fazem `g.cliente` ter `sender_ids` com `22812827`); implementar como método `_sessao_cliente(self) -> dict` desta classe, usando exatamente aquelas chaves. Se o `before_request` do portal consultar o banco para montar `g.cliente`, aplicar o mesmo `patch` que aquele teste usa.

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3.11 -m unittest portal_cliente.test_canhoto_corrigido -v`
Expected: `ModuleNotFoundError: No module named 'canhoto_corrigido'`.

- [ ] **Step 3: Implementar `portal_cliente/canhoto_corrigido.py`**

```python
# -*- coding: utf-8 -*-
"""
portal_cliente/canhoto_corrigido.py

Canhoto corrigido pelo escritório (05/10/2026 -- spec
docs/superpowers/specs/2026-10-05-correcao-canhoto-painel-design.md).
O portal (/api/canhoto) e o link do e-mail (/c/<token>) perguntam aqui
antes de ir à VUUPT: havendo correção do painel para o service_id, o
cliente recebe ela; senão (ou se der erro montando) segue o PDF da VUUPT.

PDF montado com Pillow (mesmo desenho de cotacao_pdf.py): página 1 com o
pedido e os campos do checklist, uma página por foto; PDFs enviados pelo
escritório entram em seguida (pypdf).
"""
import io
import logging
from datetime import datetime
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter

import cotacao_pdf as base

logger = logging.getLogger("portal_cliente.canhoto_corrigido")

_RAIZ = Path(__file__).parent.parent
CANHOTOS_DIR = _RAIZ / "dados" / "canhotos"


def _correcao_vigente(service_id: int, conn=None) -> dict | None:
    from nucleo import banco, correcao_comprovante
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        return correcao_comprovante.correcao_vigente(conn, service_id)
    finally:
        if fechar:
            conn.close()


def _rodape(img, draw, texto: str):
    draw.text((img.width // 2, img.height - 60), texto, font=base._fonte(18), fill=base.CINZA_TXT, anchor="mm")


def _pagina_campos(c: dict) -> Image.Image:
    img, draw = base._nova_pagina()
    logo = base._logo()
    if logo is not None:
        img.paste(logo, (base.MARGEM, 78), logo)
    draw.text((img.width - base.MARGEM, 105), "Comprovante de entrega", font=base._fonte(40, True),
              fill=base.NAVY, anchor="rm")
    draw.text((img.width - base.MARGEM, 160), f"Pedido {c['codigo'] or ''}", font=base._fonte(24),
              fill=base.CINZA_TXT, anchor="rm")
    y = 250
    linhas = [("Destinatário", c.get("destinatario_nome") or "—"),
              ("Entrega", (c.get("completed_at") or c.get("data_rota") or "—")[:16])]
    linhas += [(c["rotulos"].get(k, k), v) for k, v in c["campos"].items()]
    largura = img.width - 2 * base.MARGEM
    for rotulo, valor in linhas:
        draw.text((base.MARGEM, y), rotulo.upper(), font=base._fonte(16, True), fill=base.CINZA_TXT)
        y += 26
        for linha in base._quebrar(draw, str(valor), base._fonte(24), largura):
            draw.text((base.MARGEM, y), linha, font=base._fonte(24), fill=base.PRETO)
            y += 32
        y += 18
    _rodape(img, draw, f"Fresh Log · comprovante atualizado em {c['enviado_em'][:16]}")
    return img


def _pagina_foto(caminho: Path, c: dict) -> Image.Image:
    img, draw = base._nova_pagina()
    foto = Image.open(caminho)
    foto.load()   # imagem corrompida estoura aqui, antes de virar página
    foto = foto.convert("RGB")
    caixa = (img.width - 2 * base.MARGEM, img.height - 2 * base.MARGEM - 80)
    foto.thumbnail(caixa)
    img.paste(foto, ((img.width - foto.width) // 2, base.MARGEM))
    _rodape(img, draw, f"Pedido {c['codigo'] or ''}")
    return img


def montar_pdf(c: dict) -> bytes:
    paginas = [_pagina_campos(c)] + [_pagina_foto(a["caminho"], c) for a in c["arquivos"] if not a["pdf"]]
    buf = io.BytesIO()
    paginas[0].save(buf, format="PDF", save_all=True, append_images=paginas[1:], resolution=base.DPI)
    pdfs = [a["caminho"] for a in c["arquivos"] if a["pdf"]]
    if not pdfs:
        return buf.getvalue()
    saida = PdfWriter()
    for origem in [io.BytesIO(buf.getvalue())] + pdfs:
        for pagina in PdfReader(origem).pages:
            saida.add_page(pagina)
    final = io.BytesIO()
    saida.write(final)
    return final.getvalue()


def pdf_para_servico(service_id: int, conn=None) -> Path | None:
    """Caminho do PDF da correção vigente, ou None (sem correção ou erro:
    quem chama segue para o PDF da VUUPT)."""
    try:
        c = _correcao_vigente(service_id, conn)
        if not c:
            return None
        if len(c["arquivos"]) == 1 and c["arquivos"][0]["pdf"] and not c["campos"]:
            return c["arquivos"][0]["caminho"]
        seguro = (c["codigo"] or str(service_id)).lstrip("#").replace("/", "_")
        destino = CANHOTOS_DIR / f"canhoto_{seguro}_painel_{c['correcao_id']}.pdf"
        if not destino.exists():
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(montar_pdf(c))
        return destino
    except Exception:
        logger.exception(f"[portal] canhoto corrigido do service_id={service_id} falhou; seguindo pra VUUPT")
        return None
```

Antes de escrever, confirmar em `portal_cliente/cotacao_pdf.py` que `_quebrar(draw, texto, fonte, largura) -> list[str]` e `_logo()` têm essa assinatura (linhas ~55 e ~84). Se `_logo` depender de `LOGO_PATH` relativo, ele já funciona no portal (é o mesmo pacote).

- [ ] **Step 4: Ligar nas duas rotas**

Em `portal_cliente/app.py`, junto dos outros imports locais (depois de `import dados_cliente as dados`): `import canhoto_corrigido`.

Em `api_canhoto`, logo depois do `abort(404)` da checagem de dono:

```python
    corrigido = canhoto_corrigido.pdf_para_servico(service_id)
    if corrigido:
        nome = f"comprovante_{(servico.get('code') or str(service_id)).lstrip('#')}.pdf"
        return send_file(corrigido, mimetype="application/pdf", as_attachment=False, download_name=nome, max_age=0)
```

Em `portal_cliente/link_canhoto.py`, dentro de `registrar`, depois de `import dados_cliente as dados`: `import canhoto_corrigido`. Em `canhoto_por_link`, logo depois de `codigo = servico.get("code") or str(alvo["service_id"])`:

```python
        corrigido = canhoto_corrigido.pdf_para_servico(alvo["service_id"])
        if corrigido:
            return send_file(corrigido, mimetype="application/pdf", as_attachment=False,
                             download_name=f"comprovante_{codigo.lstrip('#')}.pdf", max_age=0)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `py -3.11 -m unittest portal_cliente.test_canhoto_corrigido portal_cliente.test_link_canhoto -v` → OK.
Run: `py -3.11 -m unittest portal_cliente.test_acesso_travas` → OK.

- [ ] **Step 6: Commit (quando o Hugo autorizar)**

```bash
git add portal_cliente/canhoto_corrigido.py portal_cliente/test_canhoto_corrigido.py portal_cliente/app.py portal_cliente/link_canhoto.py
git commit -m "Portal: canhoto corrigido pelo escritorio vence o PDF da Vuupt (portal e link do e-mail)"
```

---

### Task 5: Painel — formulário na ficha, marcação e aba "A resolver"

**Files:**
- Modify: `painel_agentes/painel_agentes.py` (rotas `/canhotos` ~1903, `canhoto_foto` ~1925, `consulta_pedido` ~2030; rotas novas depois de `revisar_canhoto`)
- Modify: `painel_agentes/templates/consulta_pedido.html`, `painel_agentes/templates/canhotos.html`, `painel_agentes/templates/_menu_lateral_nav.html` (linha do item `canhotos`)
- Test: `painel_agentes/test_canhotos_a_resolver.py`

**Interfaces:**
- Consumes: Task 2/3 (`registrar_correcao`, `marcar_canhoto_ruim`, `listar_canhotos_a_resolver`, `correcao_da_parada`, `valores_iniciais`, `fluxo_da_parada`, `modelo_do_fluxo`, `FLUXO_POR_SITUACAO`, `TIPOS_ARQUIVO`), `nucleo.operacao.carregar_checklist(conn)["motivos"]`.
- Produces:
  - `POST /api/consulta/parada/<int:parada_id>/correcao` (multipart; campos de texto em `request.form`, arquivos em `request.files` com o nome = chave) → `{"ok": true, "correcao": {...}}` | `{"erro"}` 4xx
  - `POST /api/consulta/parada/<int:parada_id>/marcar-ruim` (JSON `{"motivo"}`) → `{"ok": true, "marcacao_id"}`
  - `GET /canhotos?resultado=A_RESOLVER`
  - níveis: ações `("total", "operador", "atendimento")`; leitura de `/canhotos` passa a `("total", "operador", "leitura", "atendimento")`.

- [ ] **Step 1: Write the failing tests**

Criar `painel_agentes/test_canhotos_a_resolver.py`:

```python
# -*- coding: utf-8 -*-
"""
Correção de canhoto pelo painel: ficha do pedido + aba "A resolver" (05/10).

    cd painel_agentes && py -3.11 -m unittest test_canhotos_a_resolver -v
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import banco  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

JPG = b"\xff\xd8\xff\xe0" + b"0" * 200
ORIGEM = {"Origin": "http://localhost"}


class TestCorrecaoPainel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        pasta = Path(self._tmp.name)
        p = mock.patch.object(banco, "DB_PATH", pasta / "teste.db")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "_PASTA_COMPROVANTES", pasta / "comprovantes")
        p.start()
        self.addCleanup(p.stop)

        conn = banco.conectar()
        conn.execute("""CREATE TABLE checklist_modelo (id INTEGER PRIMARY KEY AUTOINCREMENT, fluxo TEXT, chave TEXT,
                        rotulo TEXT, tipo_campo TEXT, obrigatorio INTEGER, opcoes TEXT, aviso TEXT,
                        ordem INTEGER DEFAULT 0, ativo INTEGER DEFAULT 1)""")
        conn.executemany("INSERT INTO checklist_modelo (fluxo, chave, rotulo, tipo_campo, obrigatorio, ordem) VALUES (?,?,?,?,?,?)", [
            ("ENTREGUE", "nome_recebedor", "Nome de quem recebeu", "TEXTO", 1, 1),
            ("ENTREGUE", "foto_canhoto", "Foto do canhoto / comprovante", "FOTO", 1, 2),
        ])
        from datetime import date
        rota = conn.execute("INSERT INTO nucleo_rotas (data_rota, nome, provedor, status) VALUES (?, 'Rota 1', 'VUUPT', 'CONCLUIDA')",
                            (date.today().isoformat(),)).lastrowid
        self.parada = conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, destinatario_nome, situacao) "
                                   "VALUES (?, 1, 555, 'PS-1', 'MERCADO', 'ENTREGUE')", (rota,)).lastrowid
        conn.execute("INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, qtd_checklists) VALUES ('PS-1', 555, 0)")
        conn.commit()
        conn.close()

        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "ana"

    def _corrigir(self, nome="Jose", foto=JPG):
        dados = {"nome_recebedor": nome}
        if foto is not None:
            dados["foto_canhoto"] = (io.BytesIO(foto), "canhoto.jpg")
        return self.cliente.post(f"/api/consulta/parada/{self.parada}/correcao", data=dados,
                                 content_type="multipart/form-data", headers=ORIGEM)

    def test_atendimento_corrige(self):
        self._logar("atendimento")
        r = self._corrigir()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])

    def test_leitura_nao_corrige(self):
        self._logar("leitura")
        self.assertEqual(self._corrigir().status_code, 403)

    def test_sem_origem_e_recusado(self):
        self._logar("total")
        r = self.cliente.post(f"/api/consulta/parada/{self.parada}/correcao",
                              data={"nome_recebedor": "Jose"}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 403)

    def test_faltando_foto_obrigatoria_da_400_com_rotulo(self):
        self._logar("total")
        r = self._corrigir(foto=None)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Foto do canhoto", r.get_json()["erro"])

    def test_aba_a_resolver_mostra_e_some_depois_da_correcao(self):
        self._logar("atendimento")
        html = self.cliente.get("/canhotos?resultado=A_RESOLVER").get_data(as_text=True)
        self.assertIn("PS-1", html)
        self._corrigir()
        html = self.cliente.get("/canhotos?resultado=A_RESOLVER").get_data(as_text=True)
        self.assertNotIn(">PS-1<", html)

    def test_marcar_ruim(self):
        self._logar("operador")
        r = self.cliente.post(f"/api/consulta/parada/{self.parada}/marcar-ruim", json={"motivo": "ilegível"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        html = self.cliente.get("/canhotos?resultado=A_RESOLVER").get_data(as_text=True)
        self.assertIn("ilegível", html)

    def test_ficha_mostra_botao_para_quem_corrige_e_nao_para_leitura(self):
        self._logar("atendimento")
        self.assertIn("Corrigir comprovante", self.cliente.get("/consulta/pedido/PS-1").get_data(as_text=True))
        self._logar("leitura")
        self.assertNotIn("Corrigir comprovante", self.cliente.get("/consulta/pedido/PS-1").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
```

Antes de rodar: conferir como `requer_auth` lê a sessão (`grep -n "def requer_auth" -A25 painel_agentes/painel_agentes.py`) e se o test_client precisa de algo além de `nivel_acesso`/`usuario` (ex.: `tour_visto`, `versao_sessao`). Ajustar `_logar` com as mesmas chaves que `test_clientes_agenda_tela.py` usa. Conferir também que `exige_mesma_origem` compara com `request.host` = `localhost` no test_client.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd painel_agentes && py -3.11 -m unittest test_canhotos_a_resolver -v`
Expected: falha no `mock.patch.object(painel_agentes, "_PASTA_COMPROVANTES")` (atributo não existe) e 404 nas rotas novas.

- [ ] **Step 3: Rotas em `painel_agentes/painel_agentes.py`**

Perto de `_RAIZ = ...` (linha ~26):

```python
_PASTA_COMPROVANTES = _RAIZ / "dados" / "comprovantes"   # mesma pasta do app (nucleo/api_motorista.py)
```

Rota `/canhotos`: trocar o decorator para `@requer_auth(niveis=("total", "operador", "leitura", "atendimento"))` e o corpo para tratar `A_RESOLVER`:

```python
def canhotos():
    from nucleo import banco as nucleo_banco, operacao as nucleo_operacao, correcao_comprovante
    filtro = (request.args.get("resultado") or "REPROVADO").upper()
    if filtro == "TODOS":
        resultado = None
    elif filtro in ("REPROVADO", "APROVADO", "PENDENTE", "A_RESOLVER"):
        resultado = filtro
    else:
        filtro, resultado = "REPROVADO", "REPROVADO"
    conn = nucleo_banco.conectar()
    try:
        a_resolver = correcao_comprovante.listar_canhotos_a_resolver(conn)
        itens = a_resolver if filtro == "A_RESOLVER" else nucleo_operacao.listar_comprovantes_painel(conn, resultado)
        contagem = {(r[0] or "PENDENTE"): r[1] for r in conn.execute(
            "SELECT resultado_validacao, COUNT(*) FROM nucleo_comprovantes GROUP BY resultado_validacao")}
        contagem["A_RESOLVER"] = len(a_resolver)
    finally:
        conn.close()
    return render_template("canhotos.html", itens=itens, resultado=filtro, contagem=contagem,
                           pode_editar=g.nivel_acesso in ("total", "operador"),
                           pode_corrigir=g.nivel_acesso in _NIVEIS_CORRIGE_CANHOTO)
```

Logo antes da rota `/canhotos`:

```python
_NIVEIS_CORRIGE_CANHOTO = ("total", "operador", "atendimento")
```

Rota `consulta_pedido`: anexar o que a ficha precisa:

```python
def consulta_pedido(codigo):
    from nucleo import banco as nucleo_banco, consulta as nucleo_consulta, correcao_comprovante as cc, operacao
    detalhe = nucleo_consulta.detalhar_pedido(codigo)
    if not detalhe:
        abort(404)
    pode_corrigir = g.nivel_acesso in _NIVEIS_CORRIGE_CANHOTO
    conn = nucleo_banco.conectar()
    try:
        motivos = operacao.carregar_checklist(conn)["motivos"]
        for s in detalhe["paradas"]:
            s["correcao"] = cc.correcao_da_parada(conn, s["id"])
            s["marcacao"] = conn.execute("SELECT motivo, marcado_por, marcado_em FROM canhoto_marcacoes "
                                         "WHERE parada_id = ? AND resolvido_em IS NULL", (s["id"],)).fetchone()
            fluxo = cc.FLUXO_POR_SITUACAO.get(s["situacao"])
            s["modelo_correcao"] = cc.modelo_do_fluxo(conn, fluxo) if (fluxo and pode_corrigir) else []
            s["valores_correcao"] = cc.valores_iniciais(conn, s["id"]) if s["modelo_correcao"] else {}
    finally:
        conn.close()
    return render_template("consulta_pedido.html", d=detalhe, pode_corrigir=pode_corrigir, motivos=motivos,
                           tipos_arquivo=cc.TIPOS_ARQUIVO)
```

Rotas novas, depois de `revisar_canhoto`:

```python
@app.route("/api/consulta/parada/<int:parada_id>/correcao", methods=["POST"])
@requer_auth(niveis=_NIVEIS_CORRIGE_CANHOTO)
@exige_mesma_origem
def corrigir_comprovante(parada_id):
    """Escritório preenche o checklist da parada (Hugo, 05/10): foto ruim,
    motorista esqueceu, ocorrência. Vale pro cliente no portal/link."""
    from nucleo import banco as nucleo_banco, correcao_comprovante as cc
    from nucleo.operacao import OperacaoInvalida
    campos = request.form.to_dict()
    arquivos = {chave: f.read() for chave, f in request.files.items() if f and f.filename}
    conn = nucleo_banco.conectar()
    try:
        r = cc.registrar_correcao(conn, parada_id, campos, arquivos,
                                  session.get("usuario") or g.nivel_acesso, _PASTA_COMPROVANTES)
    except OperacaoInvalida as e:
        return jsonify({"erro": e.mensagem}), e.codigo
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao corrigir comprovante")
        return jsonify({"erro": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"ok": True, "correcao": r})


@app.route("/api/consulta/parada/<int:parada_id>/marcar-ruim", methods=["POST"])
@requer_auth(niveis=_NIVEIS_CORRIGE_CANHOTO)
@exige_mesma_origem
def marcar_canhoto_ruim(parada_id):
    from nucleo import banco as nucleo_banco, correcao_comprovante as cc
    from nucleo.operacao import OperacaoInvalida
    body = request.get_json(silent=True) or {}
    conn = nucleo_banco.conectar()
    try:
        marcacao_id = cc.marcar_canhoto_ruim(conn, parada_id, body.get("motivo", ""),
                                             session.get("usuario") or g.nivel_acesso)
    except OperacaoInvalida as e:
        return jsonify({"erro": e.mensagem}), e.codigo
    finally:
        conn.close()
    return jsonify({"ok": True, "marcacao_id": marcacao_id})
```

Conferir: se o app tiver `MAX_CONTENT_LENGTH` menor que ~25 MB (`grep -n MAX_CONTENT_LENGTH painel_agentes/painel_agentes.py`; hoje não há), não mexer. Se o Caddy limitar corpo, anotar para o deploy.

- [ ] **Step 4: Menu**

Em `painel_agentes/templates/_menu_lateral_nav.html`, no item `{'rota': 'canhotos', ...}` trocar `'niveis': NIVEIS_ROTAS` por `'niveis': ('total', 'operador', 'leitura', 'atendimento')`. Só essa linha (o arquivo tem edições de outra sessão no master; no worktree não).

- [ ] **Step 5: Template `canhotos.html` — aba e tabela "A resolver"**

Na tupla de abas, incluir `("A_RESOLVER", "A resolver")` como **primeira** aba. A contagem já vem em `contagem["A_RESOLVER"]`.

Envolver a `<table class="tabela-canhotos">` existente em `{% if resultado != 'A_RESOLVER' %} ... {% else %} ... {% endif %}`, com o ramo novo:

```html
<table class="tabela-canhotos">
  <thead><tr><th>Pedido</th><th>Rota</th><th>Por que está aqui</th><th></th></tr></thead>
  <tbody>
    {% for c in itens %}
    <tr>
      <td><strong>{{ c.codigo or '—' }}</strong><br><span class="suave">{{ c.destinatario_nome or '' }}</span>
          <div class="suave">{{ c.remetente_nome or '' }}</div></td>
      <td>{{ c.data_rota or '' }}<br><span class="suave">{{ c.rota_nome or '' }}<br>{{ c.motorista_nome or '' }}</span></td>
      <td>
        {% if c.motivo_fila == 'MARCADO' %}<span class="badge badge-erro">Marcado como ruim</span>
          <div class="motivo-ia">{{ c.motivo_marcacao }} — {{ c.marcado_por or '' }} {{ (c.marcado_em or '')[:16] }}</div>
        {% else %}<span class="badge">Entregue sem canhoto</span>{% endif %}
      </td>
      <td>{% if c.codigo %}<a href="{{ url_for('consulta_pedido', codigo=c.codigo.lstrip('#')) }}#parada-{{ c.parada_id }}">{{ 'Corrigir' if pode_corrigir else 'Ver' }}</a>{% endif %}</td>
    </tr>
    {% else %}
    <tr><td colspan="4" class="suave">Nada a resolver nos últimos 30 dias.</td></tr>
    {% endfor %}
  </tbody>
</table>
```

Ajustar o subtítulo do cabeçalho para mencionar a aba nova: "Entregas sem canhoto ou marcadas como ruins (últimos 30 dias) e fotos reprovadas pela conferência."

- [ ] **Step 6: Template `consulta_pedido.html` — botões, formulário e exibição**

(a) No `<div class="passagem">` acrescentar `id="parada-{{ s.id }}"`.

(b) Na lista de fotos, trocar o `<img>` para tratar PDF e marcar o que veio do escritório:

```html
          {% if c.caminho_local %}
          <a href="{{ url_for('canhoto_foto', comprovante_id=c.id) }}" target="_blank" rel="noopener">
            {% if c.caminho_local.lower().endswith('.pdf') %}<div class="pdf-chip">PDF</div>
            {% else %}<img src="{{ url_for('canhoto_foto', comprovante_id=c.id) }}" alt="{{ c.tipo|lower }}">{% endif %}
          </a>
          {% else %}<div class="suave">sem arquivo</div>{% endif %}
          <figcaption>{{ c.tipo|capitalize }}
            {% if c.origem == 'PAINEL' %}<br>escritório · {{ c.enviado_por or '' }}{% elif c.resultado_validacao %}<br>{{ c.resultado_validacao|lower }}{% endif %}
          </figcaption>
```

(c) Logo depois do bloco de fotos (antes do `</div>` que fecha a coluna da esquerda):

```html
      {% if s.correcao %}
      <div class="correcao">
        <strong>Corrigido pelo escritório</strong>
        <span class="suave">{{ s.correcao.enviado_por or '' }} · {{ (s.correcao.enviado_em or '')[:16] }}</span>
        {% for k, v in s.correcao.campos.items() %}<div><span class="suave">{{ s.correcao.rotulos.get(k, k) }}:</span> {{ v }}</div>{% endfor %}
      </div>
      {% endif %}
      {% if s.marcacao %}
      <div class="motivo">Marcado como ruim: {{ s.marcacao.motivo }} ({{ s.marcacao.marcado_por or '' }})</div>
      {% endif %}

      {% if pode_corrigir and s.modelo_correcao %}
      <div class="acoes-correcao">
        <details>
          <summary>Corrigir comprovante</summary>
          <form class="form-correcao" data-url="{{ url_for('corrigir_comprovante', parada_id=s.id) }}">
            {% for c in s.modelo_correcao %}
            <label>{{ c.rotulo }}{% if c.obrigatorio %} *{% endif %}
              {% if c.tipo in tipos_arquivo %}
                <input type="file" name="{{ c.chave }}" accept="image/jpeg,image/png,image/webp,application/pdf"{% if c.obrigatorio %} required{% endif %}>
              {% elif c.tipo == 'SELECAO' %}
                <select name="{{ c.chave }}"{% if c.obrigatorio %} required{% endif %}>
                  <option value=""></option>
                  {% for o in (c.opcoes or motivos|map(attribute='motivo_texto')|list) %}
                  <option{% if s.valores_correcao.get(c.chave) == o %} selected{% endif %}>{{ o }}</option>
                  {% endfor %}
                </select>
              {% else %}
                <input type="{{ 'number' if c.tipo == 'NUMERO' else 'text' }}" name="{{ c.chave }}" value="{{ s.valores_correcao.get(c.chave, '') }}"{% if c.obrigatorio %} required{% endif %}>
              {% endif %}
            </label>
            {% endfor %}
            <p class="suave">Foto ou PDF, até 10 MB. A versão nova passa a valer pro cliente no portal; a original fica guardada.</p>
            <button type="submit">Enviar correção</button>
            <div class="erro-correcao"></div>
          </form>
        </details>
        <button type="button" class="btn-marcar-ruim" data-url="{{ url_for('marcar_canhoto_ruim', parada_id=s.id) }}">Marcar canhoto ruim</button>
      </div>
      {% endif %}
```

(d) No bloco de estilo do template, acrescentar:

```css
.correcao { margin-top: 10px; padding: 8px 10px; border-left: 3px solid var(--acento); background: var(--fundo); font-size: 13px; }
.pdf-chip { width: 84px; height: 84px; display: flex; align-items: center; justify-content: center; border: 1px solid var(--borda); border-radius: 6px; font-weight: 700; }
.acoes-correcao { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: flex-start; }
.form-correcao { display: grid; gap: 8px; margin-top: 8px; max-width: 420px; }
.form-correcao label { display: grid; gap: 4px; font-size: 12.5px; }
.erro-correcao { color: var(--erro); font-size: 12.5px; }
```

(e) No fim do template (antes de `{% endblock %}`; se houver um bloco de scripts no `base.html`, usar ele):

```html
<script>
document.querySelectorAll('.form-correcao').forEach(function (form) {
  form.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var erro = form.querySelector('.erro-correcao'), botao = form.querySelector('button[type=submit]');
    erro.textContent = ''; botao.disabled = true;
    try {
      var r = await fetch(form.dataset.url, {method: 'POST', body: new FormData(form)});
      var j = await r.json().catch(function () { return {}; });
      if (!r.ok) { erro.textContent = j.erro || ('Erro ' + r.status); return; }
      location.reload();
    } catch (e) { erro.textContent = 'Sem conexão. Tente de novo.'; }
    finally { botao.disabled = false; }
  });
});
document.querySelectorAll('.btn-marcar-ruim').forEach(function (b) {
  b.addEventListener('click', async function () {
    var motivo = prompt('Por que o canhoto está ruim? (ex.: foto ilegível, cliente reclamou)');
    if (!motivo || !motivo.trim()) return;
    var r = await fetch(b.dataset.url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({motivo: motivo})});
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) { alert(j.erro || ('Erro ' + r.status)); return; }
    location.reload();
  });
});
</script>
```

Se o `base.html` tiver um helper de fetch com CSRF/origem (procurar `fetch(` em outro template do painel, ex. `canhotos.html`), usar o mesmo padrão em vez do `fetch` cru.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd painel_agentes && py -3.11 -m unittest test_canhotos_a_resolver -v` → OK.
Run: `cd painel_agentes && py -3.11 -m unittest test_inicio_tela test_pagina_inicial` → OK (menu não quebrou).

- [ ] **Step 8: Ver na tela de verdade**

Subir em porta de teste (nunca 8070), de dentro de `painel_agentes/` no worktree:
`py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"`
Com o banco local (congelado em 17/08), abrir `/consulta/pedido/<um código entregue em agosto>` logado como `atendimento`, enviar uma correção com foto e PDF, conferir a aba "A resolver" (vai mostrar vazio pela janela de 30 dias — para ver itens, chamar `listar_canhotos_a_resolver(conn, dias=90)` no console). Usar o skill `webapp-testing`/Playwright conforme `feedback_testar_painel_playwright_local`. **Apagar depois as linhas de teste** do `dados/dados.db` local (`DELETE FROM nucleo_comprovantes WHERE origem='PAINEL'`, eventos `COMPROVANTE_CORRIGIDO`, `canhoto_marcacoes`) e os arquivos `dados/comprovantes/*/painel_*`.

- [ ] **Step 9: Commit (quando o Hugo autorizar)**

```bash
git add painel_agentes/painel_agentes.py painel_agentes/templates/consulta_pedido.html painel_agentes/templates/canhotos.html painel_agentes/templates/_menu_lateral_nav.html painel_agentes/test_canhotos_a_resolver.py
git commit -m "Painel: corrigir canhoto pela ficha do pedido e fila de canhotos a resolver"
```

---

### Task 6: Mapa, spec e verificação final

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt`
- Modify: `docs/superpowers/specs/2026-10-05-correcao-canhoto-painel-design.md` (seção 2: funções ficaram em `nucleo/correcao_comprovante.py`, campos de texto no evento — já ajustado na revisão do plano; conferir)

- [ ] **Step 1: Atualizar o mapa**

Em `MAPA_DO_SISTEMA.txt`:
- seção do núcleo (perto de `operacao.py`): `correcao_comprovante.py     Correção de canhoto pelo escritório: registrar, marcar ruim, fila a resolver, correção vigente por service_id.`
- lista de telas do painel, linha `/canhotos`: `/canhotos                  canhotos.html (abas: A resolver, Reprovados...; atendimento também vê)`; e em `/consulta (+/rota, /pedido)` acrescentar `— ficha do pedido tem "Corrigir comprovante" e "Marcar canhoto ruim"`.
- portal: `canhoto_corrigido.py      Canhoto corrigido pelo escritório vence o PDF da VUUPT (/api/canhoto e /c/<token>).`
- tabelas: `canhoto_marcacoes` e as colunas `origem/enviado_por/correcao_id` de `nucleo_comprovantes`.

- [ ] **Step 2: Rodar tudo**

```bash
py -3.11 -m py_compile nucleo/banco.py nucleo/correcao_comprovante.py portal_cliente/canhoto_corrigido.py portal_cliente/app.py portal_cliente/link_canhoto.py painel_agentes/painel_agentes.py
py -3.11 -m unittest nucleo.test_correcao_comprovante nucleo.test_consulta nucleo.test_api_motorista
py -3.11 -m unittest portal_cliente.test_canhoto_corrigido portal_cliente.test_link_canhoto portal_cliente.test_acesso_travas
cd painel_agentes && py -3.11 -m unittest test_canhotos_a_resolver test_inicio_tela test_pagina_inicial
```
Expected: tudo OK. Se algo falhar, não seguir.

- [ ] **Step 3: Commit (quando o Hugo autorizar)**

```bash
git add MAPA_DO_SISTEMA.txt docs/superpowers/specs/2026-10-05-correcao-canhoto-painel-design.md docs/superpowers/plans/2026-10-05-correcao-canhoto-painel.md
git commit -m "Mapa: correcao de canhoto pelo painel"
```

- [ ] **Step 4: Deploy (só com ordem do Hugo)** — skill `deploy-vps`: pull, chown, `systemctl restart painel-agentes portal-cliente`. Prova real: `curl` local em `/painel/canhotos?resultado=A_RESOLVER` (status 200 com sessão) e `venv/bin/python -c "from nucleo import banco, correcao_comprovante as cc; c=banco.conectar(); print(len(cc.listar_canhotos_a_resolver(c)))"` como `www-data` (esperado na casa de ~85, pela medição de 05/10). Não enviar correção real em produção sem o Hugo escolher um pedido.
