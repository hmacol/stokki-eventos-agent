# Pedidos parados: registro no painel — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O galpão registra pedido parado na aba "Registrar" de `/pedidos-parados`, gravando em `pedidos_parados_registros`; a aba "Triagem" (tela atual) passa a ler dessa tabela e o painel para de consultar o Lovable para pedidos parados.

**Architecture:** Tudo dentro do módulo já existente `painel_agentes/pedidos_parados_triagem.py` (tabela nova em `_conectar()`, funções `registrar`/`apagar_registro`/`registros_hoje`, e `_montar_base_externa` trocando o Lovable por SQL local). Rotas novas em `painel_agentes.py` no bloco atual de `/api/pedidos-parados/*`; template único com abas `.seg` (mesmo componente de `retornos.html`). Encaminhamento às Demandas fica atrás de uma flag desligada.

**Tech Stack:** Python 3.11 / Flask / SQLite (`dados/dados.db`), template Jinja + JS puro, unittest. Playwright local só pra prova de tela.

**Spec:** `docs/superpowers/specs/2026-10-24-pedidos-parados-registro-painel-design.md`

## Global Constraints

- Idioma: código, comentários, commits em português; comentários/logs sem acento onde o arquivo já segue isso (`painel_agentes.py` mistura; `pedidos_parados_triagem.py` usa acento em docstrings: manter o estilo de cada arquivo).
- Python: `py -3.11`. Testes de `painel_agentes` rodam **de dentro de `painel_agentes/`**: `py -3.11 -m unittest test_x -v`. Nunca misturar com `roteirizacao`/`nucleo`.
- **Sem commit nem push até o Hugo pedir** (CLAUDE.md). Cada task termina em `py -3.11 -m py_compile` + testes verdes, não em commit. Antes de editar: `git status --short` (outras sessões editam o mesmo working tree; `pedidos_parados_triagem.py` é co-editado).
- `dados/dados.db` local é só desenvolvimento; testes usam DB temporário via `mock.patch.object(ppt, "DB_PATH", ...)`.
- Hora local sempre `datetime.now().strftime("%Y-%m-%d %H:%M:%S")` (padrão de `_marcar_acao`), fuso `America/Sao_Paulo`.
- Níveis: registrar = `("total", "operador", "logistica", "galpao", "expedicao")`; tela e `/dados` = níveis atuais + `galpao`, `expedicao`; demais rotas inalteradas.
- Foto: jpg/png/webp, ≤ `retornos.MAX_FOTO_BYTES` (10 MB), em `dados/pedidos_parados/<id>/foto.<ext>`.
- Painel local pode estar rodando na 8070: prova de tela sempre em outra porta (8099).
- `MAPA_DO_SISTEMA.txt` atualizado no mesmo conjunto de mudanças (Task 5).

## Review Focus

1. `order_number` só com espaços, só "PS-" ou com letras/pontuação fora do padrão ("PS 40.525", "nf 1234/2") → `registrar` recusa vazio e guarda o resto normalizado sem quebrar o link da Stokki (teste em Task 1).
2. Volumes vindo como "3.0", "abc", "" ou 0 do formulário → 400 com mensagem, nunca 500 nem gravar 0 (teste em Task 1 e Task 3).
3. Registro feito 23:58 e re-registro 00:03: o cálculo de "dia mais recente"/`dias_parado` usa a data local do `criado_em`, e o aviso "já registrado hoje" compara só a data (teste em Task 2).
4. Duas abas do galpão apagando o mesmo registro ao mesmo tempo → segunda chamada recebe 404 limpo e a pasta da foto não gera exceção se já sumiu (teste em Task 1).
5. Flag de Demandas religada depois (`ENCAMINHAMENTO_DEMANDAS_ATIVO = True`): linhas classificadas durante a pausa voltam a mostrar o botão Encaminhar e a contar como não tratadas; nada fica gravado como "concluída" por causa da pausa (teste em Task 2: `classificar` não toca `acao_status`; verificação visual em Task 4).

---

### Task 1: Tabela de registros, `registrar`, `apagar_registro`, `registros_hoje`

**Files:**
- Modify: `painel_agentes/pedidos_parados_triagem.py` (`_conectar()` ~linha 163-214; funções novas logo após `_marcar_acao`, ~linha 528)
- Test: `painel_agentes/test_pedidos_parados_registro.py` (novo)

**Interfaces:**
- Consumes: `normalizar_order_number` (já importado), `retornos._extensao_imagem`, `retornos.MAX_FOTO_BYTES`.
- Produces:
  - `PASTA_FOTOS = _RAIZ / "dados" / "pedidos_parados"`
  - `PRAZO_APAGAR_MIN = 15`
  - `class ErroRegistro(ValueError)`
  - `registrar(order_number_bruto: str, volumes, usuario: str, foto: bytes | None = None) -> dict` → `{"id": int, "order_number": str, "ja_hoje": int}` (`ja_hoje` = quantos registros do mesmo `order_number` já existiam hoje, antes deste). Levanta `ErroRegistro` com mensagens: `"Informe o número do pedido."`, `"Volumes precisa ser um número inteiro maior que zero."`, `"Arquivo não é uma foto (use JPG, PNG ou WEBP)."`, `"Foto muito grande (máximo 10 MB)."`. Zera `_cache_base["quando"] = 0.0` ao final.
  - `apagar_registro(registro_id: int, usuario: str) -> None`. Levanta `ErroRegistro("Registro não encontrado.")` (404 na rota), `ErroRegistro("Só quem registrou pode apagar.")` e `ErroRegistro("Passaram mais de 15 minutos; o registro fica como histórico.")` (403). Remove a pasta `PASTA_FOTOS/<id>` com `shutil.rmtree(..., ignore_errors=True)`. Zera o cache.
  - `caminho_foto(registro_id: int) -> Path | None` (mesma proteção de `retornos.caminho_arquivo`: resolve dentro de `PASTA_FOTOS/<id>`; `None` se não existe).
  - `registros_hoje(usuario: str) -> dict` → `{"hoje": [...], "ontem_sem_hoje": [...]}`; item = `{"id", "order_number", "volumes", "foto": bool, "registrado_por", "criado_em", "classificacao", "acao_status", "dias_parado", "pode_apagar": bool}`. `hoje` = registros com `date(criado_em) == hoje local`, mais recentes primeiro. `ontem_sem_hoje` = último registro de cada `order_number` com data == ontem e sem registro hoje. `dias_parado` = `(hoje - date(primeiro criado_em daquele order_number)).days + 1`. `pode_apagar` = `registrado_por == usuario` e `criado_em` ≤ 15 min atrás. `classificacao`/`acao_status` vêm de `pedidos_parados_classificacao`.
  - `_agora_local() -> str` e `_hoje_local() -> date` (helpers; `_hoje_local` usa `datetime.now(FUSO_LOCAL).date()`).

- [ ] **Step 1: Escrever os testes que falham**

```python
# painel_agentes/test_pedidos_parados_registro.py  (unittest; cabeçalho igual ao de test_pedidos_parados_reenvio.py)
class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.fotos = Path(self.tmp.name) / "fotos"
        self._p = [mock.patch.object(ppt, "DB_PATH", self.db), mock.patch.object(ppt, "PASTA_FOTOS", self.fotos)]
        for p in self._p: p.start()
    def tearDown(self):
        for p in self._p: p.stop()
        self.tmp.cleanup()

class TestRegistrar(_Base):
    def test_normaliza_e_guarda_bruto(self):
        r = ppt.registrar("PS-40525", "3", "quecia")
        row = ppt._conectar().execute("SELECT * FROM pedidos_parados_registros WHERE id=?", (r["id"],)).fetchone()
        self.assertEqual((r["order_number"], row["order_number_bruto"], row["volumes"], row["registrado_por"]), ("40525", "PS-40525", 3, "quecia"))
        self.assertEqual(r["ja_hoje"], 0)
    def test_pedido_vazio_ou_so_prefixo(self):
        for bruto in ("", "   ", "PS-"):
            with self.assertRaises(ppt.ErroRegistro): ppt.registrar(bruto, 1, "x")
    def test_volumes_invalidos(self):
        for v in (0, -1, "abc", "", "3.0", None):
            with self.assertRaises(ppt.ErroRegistro): ppt.registrar("40525", v, "x")
    def test_foto_invalida_e_grande(self):
        with self.assertRaises(ppt.ErroRegistro): ppt.registrar("1", 1, "x", foto=b"nao e imagem")
        with self.assertRaises(ppt.ErroRegistro): ppt.registrar("1", 1, "x", foto=b"\xff\xd8" + b"0" * ppt.MAX_FOTO_BYTES)
    def test_foto_jpg_vai_pra_pasta(self):
        r = ppt.registrar("1", 1, "x", foto=b"\xff\xd8\xff\xe0teste")
        self.assertTrue((self.fotos / str(r["id"]) / "foto.jpg").is_file())
        self.assertEqual(ppt.caminho_foto(r["id"]).name, "foto.jpg")
        self.assertIsNone(ppt.caminho_foto(r["id"] + 99))
    def test_segundo_registro_no_dia_conta_ja_hoje(self):
        ppt.registrar("40525", 3, "quecia")
        self.assertEqual(ppt.registrar("40525", 3, "werlly")["ja_hoje"], 1)

class TestApagar(_Base):
    def test_so_autor_e_so_15_min(self):
        rid = ppt.registrar("1", 1, "quecia", foto=b"\x89PNGxx")["id"]
        with self.assertRaises(ppt.ErroRegistro): ppt.apagar_registro(rid, "werlly")
        ppt.apagar_registro(rid, "quecia")
        self.assertFalse((self.fotos / str(rid)).exists())
        with self.assertRaises(ppt.ErroRegistro): ppt.apagar_registro(rid, "quecia")  # já apagado -> "não encontrado"
    def test_passou_do_prazo(self):
        rid = ppt.registrar("1", 1, "quecia")["id"]
        c = ppt._conectar(); c.execute("UPDATE pedidos_parados_registros SET criado_em=? WHERE id=?",
            ((datetime.now() - timedelta(minutes=16)).strftime("%Y-%m-%d %H:%M:%S"), rid)); c.commit(); c.close()
        with self.assertRaises(ppt.ErroRegistro): ppt.apagar_registro(rid, "quecia")

class TestRegistrosHoje(_Base):
    def _inserir(self, numero, quando, por="x", volumes=2):
        c = ppt._conectar(); c.execute("INSERT INTO pedidos_parados_registros (order_number, order_number_bruto, volumes, registrado_por, criado_em) VALUES (?,?,?,?,?)",
            (numero, numero, volumes, por, quando.strftime("%Y-%m-%d %H:%M:%S"))); c.commit(); c.close()
    def test_ontem_sem_hoje_e_dias_parado(self):
        hoje = datetime.now().replace(hour=10); ontem = hoje - timedelta(days=1)
        self._inserir("A", ontem - timedelta(days=4)); self._inserir("A", ontem)   # parado há 6 dias, não registrado hoje
        self._inserir("B", ontem); self._inserir("B", hoje, por="quecia")          # registrado hoje
        r = ppt.registros_hoje("quecia")
        self.assertEqual([x["order_number"] for x in r["hoje"]], ["B"])
        self.assertEqual([(x["order_number"], x["dias_parado"], x["volumes"]) for x in r["ontem_sem_hoje"]], [("A", 6, 2)])
        self.assertTrue(r["hoje"][0]["pode_apagar"])
        self.assertFalse(ppt.registros_hoje("werlly")["hoje"][0]["pode_apagar"])
    def test_ainda_parado_tira_de_ontem(self):
        self._inserir("A", datetime.now() - timedelta(days=1))
        ppt.registrar("A", 2, "x")
        r = ppt.registros_hoje("x")
        self.assertEqual(r["ontem_sem_hoje"], []); self.assertEqual(r["hoje"][0]["dias_parado"], 2)
    def test_traz_classificacao(self):
        rid = ppt.registrar("A", 1, "x")["id"]
        ppt.classificar("A", str(rid), "Reenvio", "x")
        self.assertEqual(ppt.registros_hoje("x")["hoje"][0]["classificacao"], "Reenvio")
```

- [ ] **Step 2: Rodar e ver falhar**

Run (de `painel_agentes/`): `py -3.11 -m unittest test_pedidos_parados_registro -v`
Expected: FAIL/ERROR com `AttributeError: module ... has no attribute 'PASTA_FOTOS'` (ou `registrar`).

- [ ] **Step 3: Implementar em `pedidos_parados_triagem.py`**

Na `_conectar()`, após `pedidos_parados_vuupt`, `CREATE TABLE IF NOT EXISTS pedidos_parados_registros` com as colunas da spec e `CREATE INDEX IF NOT EXISTS ix_pp_registros_num ON pedidos_parados_registros (order_number, criado_em)`. Constantes `PASTA_FOTOS`, `PRAZO_APAGAR_MIN`, `from retornos import MAX_FOTO_BYTES, _extensao_imagem` (import ao lado de `import tratativas`; o módulo é irmão, mesmo `sys.path`). Volumes: aceitar só `int` ou string de dígitos (`str(v).strip().isdigit()`), ≥ 1. Foto gravada depois do INSERT (precisa do id); se a gravação falhar, apagar a linha e relançar. `registros_hoje` faz duas consultas (registros de hoje; último registro por `order_number` de ontem `NOT IN` os de hoje) mais `MIN(criado_em) GROUP BY order_number` pra `dias_parado`.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest test_pedidos_parados_registro -v` → todos `ok`. Depois `py -3.11 -m unittest test_pedidos_parados_reenvio -v` → segue verde. `py -3.11 -m py_compile pedidos_parados_triagem.py`.

---

### Task 2: Triagem lê a tabela local; pausa das Demandas; limpeza do Lovable

**Files:**
- Modify: `painel_agentes/pedidos_parados_triagem.py`: imports (53-55), constante `LIMITE_HISTORICO_PEDIDOS_PARADOS` (68-74), `_data_local` (530-538), `_montar_base_externa` (591-627), docstring/`revisao_urgente` em `listar_com_classificacao` (647-741), `encaminhar_operacao` (850-891), docstring do topo do arquivo.
- Test: `painel_agentes/test_pedidos_parados_registro.py` (classe nova `TestListagemLocal`)

**Interfaces:**
- Consumes: tabela e `registrar` da Task 1.
- Produces:
  - `ENCAMINHAMENTO_DEMANDAS_ATIVO = False` (comentário: "Hugo, 24/10: pausado junto com a saída do Lovable; religar = True. Enquanto False, encaminhar_operacao recusa e a tela conta a classificação como tratada").
  - `_montar_base_externa()` mantém o mesmo dict de retorno (`pedidos_do_dia`, `primeira_vez`, `contagem`, `dia_mais_recente`, `ids_stokki`), onde cada `p` em `pedidos_do_dia` agora é `{"id": int, "order_number", "volumes", "recebedor_name": registrado_por, "created_at": "YYYY-MM-DDTHH:MM:SS" (local), "foto": bool}`.
  - `listar_com_classificacao()` devolve os mesmos campos de hoje mais `"registrado_por"` e `"foto": bool`; `freshhub_id` = `str(p["id"])`.
  - `_data_local(texto: str) -> date`: data sem fuso é LOCAL (docstring atualizada); com fuso, converte.
  - `encaminhar_operacao` levanta `ValueError("Encaminhamento às Demandas pausado (Hugo, 24/10).")` antes de tocar Vuupt/Lovable quando a flag é False.

- [ ] **Step 1: Testes que falham**

```python
class TestListagemLocal(_Base):
    def setUp(self):
        super().setUp()
        self._p2 = [mock.patch.object(ppt, "_entregues_com_cache", return_value=set()),
                    mock.patch.object(ppt, "_resolver_ids_stokki", return_value={}),
                    mock.patch.object(ppt, "_vuupt", return_value=mock.Mock()),
                    mock.patch.object(ppt, "_embarcadores_por_pedido", return_value={})]
        for p in self._p2: p.start()
        ppt._cache_base.update(quando=0.0, dados=None)
    def tearDown(self):
        for p in self._p2: p.stop(); super().tearDown()
    def _inserir(self, numero, quando, por="x"):  # igual ao da TestRegistrosHoje
        ...
    def test_so_dia_mais_recente_e_dias_parado(self):
        agora = datetime.now().replace(hour=10)
        self._inserir("A", agora - timedelta(days=3)); self._inserir("A", agora - timedelta(days=1)); self._inserir("A", agora)
        self._inserir("B", agora - timedelta(days=1))
        lista = ppt.listar_com_classificacao()
        self.assertEqual([(p["order_number"], p["dias_parado"], p["vezes_registrado"], p["prioridade"]) for p in lista], [("A", 4, 3, True)])
        self.assertEqual(lista[0]["registrado_por"], "x"); self.assertFalse(lista[0]["foto"])
        self.assertEqual(lista[0]["created_at"], agora.strftime("%Y-%m-%dT%H:%M:%S"))
    def test_data_local_sem_fuso_e_local(self):
        self.assertEqual(ppt._data_local("2026-10-24 00:30:00"), date(2026, 10, 24))
        self.assertEqual(ppt._data_local("2026-10-24T02:30:00+00:00"), date(2026, 10, 23))
    def test_revisao_urgente_com_hora_local(self):
        rid = ppt.registrar("A", 1, "x")["id"]
        ppt.classificar("A", str(rid), "Entregue", "x")
        ppt._cache_base.update(quando=0.0, dados=None)
        self.assertFalse(ppt.listar_com_classificacao()[0]["revisao_urgente"])
        self._inserir("A", datetime.now() + timedelta(minutes=2)); ppt._cache_base.update(quando=0.0, dados=None)
        self.assertTrue(ppt.listar_com_classificacao()[0]["revisao_urgente"])
    def test_registrar_invalida_cache(self):
        ppt.listar_com_classificacao(); ppt.registrar("Z", 1, "x")
        self.assertEqual([p["order_number"] for p in ppt.listar_com_classificacao()], ["Z"])
    def test_encaminhar_pausado_nao_toca_nada(self):
        with mock.patch.object(ppt, "ENCAMINHAMENTO_DEMANDAS_ATIVO", False), \
             mock.patch.object(ppt, "_vuupt") as v, mock.patch.object(ppt, "_sessao_freshhub") as s:
            with self.assertRaises(ValueError): ppt.encaminhar_operacao("A", "Cancelados", "x")
        v.assert_not_called(); s.assert_not_called()
    def test_classificar_pausado_nao_conclui(self):
        rid = ppt.registrar("A", 1, "x")["id"]; ppt.classificar("A", str(rid), "Cancelados", "x")
        self.assertIsNone(ppt.registros_hoje("x")["hoje"][0]["acao_status"])  # não vira 'concluida' por causa da pausa
```

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest test_pedidos_parados_registro.TestListagemLocal -v` → falhas (`_montar_base_externa` ainda chama o Lovable: `ValueError` de credenciais ou `KeyError 'registrado_por'`).

- [ ] **Step 3: Implementar**

Remover `from lovable.auth import FreshHubSession` do topo e `listar_pedidos_parados` do import (manter `normalizar_order_number` e `criar_demanda_para_tratativa`); `_sessao_freshhub` passa a importar `FreshHubSession` dentro da função. Apagar `LIMITE_HISTORICO_PEDIDOS_PARADOS` e seu comentário. `_montar_base_externa`: `SELECT id, order_number, volumes, registrado_por, criado_em, foto FROM pedidos_parados_registros ORDER BY criado_em` e montar `brutos` no formato acima (`created_at = criado_em.replace(" ", "T")`); o laço seguinte fica igual. `_data_local`: `if dt.tzinfo is None: return dt.date()`. Em `listar_com_classificacao`, no `revisao_urgente`, parsear `created_at` com `datetime.fromisoformat` e comparar naive com naive (tirar o `replace(tzinfo=...)` dos dois lados); acrescentar `"registrado_por": p["recebedor_name"], "foto": bool(p.get("foto"))` no dict. Atualizar docstrings do módulo e de `listar_com_classificacao` ("origem: tabela pedidos_parados_registros, aba Registrar; Lovable saiu em 24/10"). `encaminhar_operacao`: checagem da flag como primeira linha.

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest test_pedidos_parados_registro test_pedidos_parados_reenvio -v` → verde; `py -3.11 -m py_compile pedidos_parados_triagem.py`; `grep -n "lovable" pedidos_parados_triagem.py` mostra só `normalizar_order_number`, `criar_demanda_para_tratativa` e o import local em `_sessao_freshhub`.

---

### Task 3: Rotas no painel

**Files:**
- Modify: `painel_agentes/painel_agentes.py` (bloco 1615-1770); `painel_agentes/contadores_menu.py:62`
- Test: `painel_agentes/test_pedidos_parados_registro.py` (classe `TestRotas`, Flask `test_client`)

**Interfaces:**
- Consumes: Task 1/2 (`registrar`, `apagar_registro`, `registros_hoje`, `caminho_foto`, `ErroRegistro`, `ENCAMINHAMENTO_DEMANDAS_ATIVO`).
- Produces (nomes de endpoint Flask, usados pelo template via `url_for`):
  - `pedidos_parados` (GET `/pedidos-parados`): níveis `("total","operador","logistica","leitura","atendimento","galpao","expedicao")`; `render_template(..., pode_editar=<como hoje>, pode_registrar=g.nivel_acesso in NIVEIS_PP_REGISTRAR, aba_inicial="registrar" if g.nivel_acesso in ("galpao","expedicao") else "triagem", encaminhamento_ativo=pedidos_parados_triagem.ENCAMINHAMENTO_DEMANDAS_ATIVO)`.
  - `api_pedidos_parados_dados`: mesmos níveis da tela.
  - `api_pedidos_parados_registros_hoje` (GET `/api/pedidos-parados/registros-hoje`, níveis da tela) → `jsonify(registros_hoje(session.get("usuario") or g.nivel_acesso))`.
  - `api_pedidos_parados_registrar` (POST `/api/pedidos-parados/registrar`, `NIVEIS_PP_REGISTRAR = ("total","operador","logistica","galpao","expedicao")`, `@exige_mesma_origem`): lê `request.form["order_number"]`, `request.form["volumes"]`, `request.files.get("foto")`; `ErroRegistro` → 400 `{"erro": str(e)}`; sucesso → 200 com o dict de `registrar`.
  - `api_pedidos_parados_registro_apagar` (POST `/api/pedidos-parados/registro/apagar`, JSON `{id}`, mesmos níveis de registrar, `@exige_mesma_origem`): "não encontrado" → 404; demais `ErroRegistro` → 403; sucesso `{"ok": true}`.
  - `pedidos_parados_foto` (GET `/pedidos-parados/foto/<int:registro_id>`, níveis da tela): `send_file(caminho, max_age=3600)` ou 404.
  - `api_pedidos_parados_encaminhar`: com flag False responde 409 `{"erro": "Encaminhamento às Demandas pausado (Hugo, 24/10)."}` antes de chamar o módulo.
  - `contadores_menu.NIVEIS_POR_CONTADOR["pedidos_parados"]` += `"galpao", "expedicao"`.

- [ ] **Step 1: Testes que falham** (usar o padrão de login de testes já existente em `painel_agentes/test_*.py` que exercitam rotas — se nenhum fizer login por sessão, montar `with app.test_client() as c: with c.session_transaction() as s: s["nivel_acesso"]="galpao"; s["usuario"]="galpao"` e checar o que `requer_auth`/`exige_mesma_origem` exigem: ler os dois decorators em `painel_agentes.py:283-430` antes de escrever o teste):

```python
class TestRotas(_Base):
    def _cliente(self, nivel="galpao", usuario="galpao"):  # sessão autenticada + header de mesma origem
        ...
    def test_registrar_e_listar(self):
        c = self._cliente()
        r = c.post("/api/pedidos-parados/registrar", data={"order_number": "PS-1", "volumes": "2"})
        self.assertEqual(r.status_code, 200); self.assertEqual(r.get_json()["order_number"], "1")
        self.assertEqual(c.get("/api/pedidos-parados/registros-hoje").get_json()["hoje"][0]["volumes"], 2)
    def test_volumes_invalido_400(self):
        self.assertEqual(self._cliente().post("/api/pedidos-parados/registrar", data={"order_number": "1", "volumes": "3.0"}).status_code, 400)
    def test_leitura_nao_registra(self):
        self.assertEqual(self._cliente("leitura", "leitor").post("/api/pedidos-parados/registrar", data={"order_number": "1", "volumes": "1"}).status_code, 403)
    def test_apagar_de_outro_403_e_inexistente_404(self):
        rid = self._cliente().post("/api/pedidos-parados/registrar", data={"order_number": "1", "volumes": "1"}).get_json()["id"]
        self.assertEqual(self._cliente("operador", "quecia").post("/api/pedidos-parados/registro/apagar", json={"id": rid}).status_code, 403)
        self.assertEqual(self._cliente().post("/api/pedidos-parados/registro/apagar", json={"id": rid + 9}).status_code, 404)
    def test_foto_servida_e_404(self):
        rid = self._cliente().post("/api/pedidos-parados/registrar", data={"order_number": "1", "volumes": "1", "foto": (io.BytesIO(b"\xff\xd8\xff\xe0x"), "f.jpg")}).get_json()["id"]
        self.assertEqual(self._cliente().get(f"/pedidos-parados/foto/{rid}").status_code, 200)
        self.assertEqual(self._cliente().get(f"/pedidos-parados/foto/{rid + 9}").status_code, 404)
    def test_encaminhar_pausado_409(self):
        with mock.patch.object(ppt, "ENCAMINHAMENTO_DEMANDAS_ATIVO", False):
            self.assertEqual(self._cliente("total", "hugo").post("/api/pedidos-parados/encaminhar", json={"order_number": "1", "classificacao": "Cancelados"}).status_code, 409)
```

Importar o app como os outros testes de rota do pacote fazem (`import painel_agentes as pa; app = pa.app; app.config["TESTING"] = True`); `exige_mesma_origem` provavelmente olha `Origin`/`Referer` ou `Sec-Fetch-Site` — passar o header que ele exige.

- [ ] **Step 2: Rodar e ver falhar** — 404 nas rotas novas.

- [ ] **Step 3: Implementar** as rotas no bloco de `/api/pedidos-parados/*`, no mesmo estilo de `api_retornos_descarga` (form + files) e `retornos_arquivo` (send_file). Atualizar a docstring de `pedidos_parados()` (origem agora é a aba Registrar). Ajustar `contadores_menu`.

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest test_pedidos_parados_registro -v` verde; `py -3.11 -m py_compile painel_agentes.py contadores_menu.py`; `py -3.11 -m unittest test_acesso_telas -v` segue verde.

---

### Task 4: Template — abas, aba Registrar, colunas novas, CSV, pausa

**Files:**
- Modify: `painel_agentes/templates/pedidos_parados_triagem.html` (inteiro: estilo, HTML, JS)
- Modify: `painel_agentes/templates/_tour_pedidos_parados.html` (passo 1: trocar "no Fresh Hub (freshhub.com.br/pedidos-parados)" por "na aba Registrar desta tela"; passo do botão Encaminhar: dizer que está pausado quando `encaminhamento_ativo` for falso — o tour é Jinja, pode ler a variável)

**Interfaces:**
- Consumes: variáveis `pode_editar`, `pode_registrar`, `aba_inicial`, `encaminhamento_ativo`; rotas da Task 3 via `url_for('api_pedidos_parados_registrar')`, `url_for('api_pedidos_parados_registros_hoje')`, `url_for('api_pedidos_parados_registro_apagar')`, `url_for('pedidos_parados_foto', registro_id=0)` (substituir o `0` no JS, como `retornos.html` faz com `URL_DETALHE`); campos `registrado_por`, `foto`, `freshhub_id` (= id do registro) em `/dados`.
- Produces: ids estáveis pro tour e pra prova Playwright: `#aba-registrar`, `#aba-triagem` (botões `.seg`), `#tela-registrar`, `#tela-triagem`, `#reg-pedido`, `#reg-volumes`, `#reg-menos`, `#reg-mais`, `#reg-foto` (input file, `accept="image/*" capture="environment"`), `#reg-previa`, `#reg-aviso`, `#reg-enviar`, `#reg-hoje`, `#reg-ontem`, `#botao-exportar-csv`.

- [ ] **Step 1: Estrutura e estilo.** `.seg` com os dois botões logo abaixo do `<h1>`; `main` continua `max-width: 1400px`, mas `#tela-registrar` tem `max-width: 520px` e campos ≥ 44 px de altura (celular). Reaproveitar o CSS de `.seg`, `.cartao`, `.erro-msg` de `retornos.html` (copiar as regras; não há CSS compartilhado). Aba inicial = `location.hash` (`#registrar`/`#triagem`) senão `aba_inicial`; trocar de aba atualiza o hash com `history.replaceState`. Sem `pode_registrar`, a aba Registrar mostra só as listas (sem formulário). Subtítulo: "Registrado pelo galpão na aba Registrar desta tela (o Fresh Hub do Lovable saiu em 24/10). Mostra só quem foi registrado de novo no dia mais recente…" (manter o resto do texto atual).

- [ ] **Step 2: JS da aba Registrar.** `carregarRegistros()` → GET registros-hoje → renderiza `#reg-hoje` (pílula: `classificacao || "sem triagem"`, `✓` se `acao_status === "concluida"`; botão "Apagar" quando `pode_apagar`) e `#reg-ontem` (botão "Ainda parado" → POST registrar com `order_number`+`volumes`, sem foto). Ao digitar em `#reg-pedido`, normalizar no cliente com a mesma regex de `normalizar_order_number` (`/^\s*#?\s*PS\s*[.\-_ ]*\s*(\d+)\s*$/i`) e mostrar `#reg-aviso` "Já registrado hoje às HH:MM por X" se bater com algum item de `hoje`. Enviar: `FormData` (order_number, volumes, foto se houver), botão desabilitado durante o envio, erro em `.erro-msg`, sucesso limpa o formulário, recarrega listas e (se a Triagem já foi carregada) chama `carregarDados()`. − / + mantém ≥ 1. Prévia da foto por `URL.createObjectURL`.

- [ ] **Step 3: Triagem.** Cabeçalhos "Registrado por" e "Foto" entre "Registrado em" e "Dias parado" (com filtros de coluna: texto pra registrado por; `select` sim/não pra foto); `colspan` de 7 → 9; célula Foto = `<a target=_blank href="<url foto>"><img class="foto-mini"></a>` ou "—". Botão `#botao-exportar-csv` em `.barra-utilidade`: gera CSV (`;` como separador, cabeçalho Pedido;Embarcador;Volumes;Registrado em;Registrado por;Dias parado;Classificação;Tratativa) das `linhasFiltradas()` e baixa via `Blob` + `<a download="pedidos-parados-AAAA-MM-DD.csv">`. `copiarTabela`/`verificarStokki`/`verificarVuupt` inalterados.

- [ ] **Step 4: Pausa.** `const ENCAMINHAMENTO_ATIVO = {{ 'true' if encaminhamento_ativo else 'false' }};` Em `renderTabela`, o ramo `CRIAM_DEMANDA` só mostra o botão se `ENCAMINHAMENTO_ATIVO`; senão `<span class="status-ok">✓ classificado · encaminhamento pausado</span>`. `estaTratado(p)`: `|| (!ENCAMINHAMENTO_ATIVO && CRIAM_DEMANDA.includes(p.classificacao))`. Comentário no JS apontando pra flag do Python.

- [ ] **Step 5: Conferir sintaxe e render.** `py -3.11 -c "import jinja2,pathlib; e=jinja2.Environment(); [e.parse(pathlib.Path(p).read_text(encoding='utf-8')) for p in ('templates/pedidos_parados_triagem.html','templates/_tour_pedidos_parados.html')]; print('ok')"`. Subir `py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"` de dentro de `painel_agentes/` e abrir `/pedidos-parados` logado; sem erro no log.

---

### Task 5: Prova de tela, documentação e mapa

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (linhas ~818, ~880, ~1103-1105, ~1235 e a nota de renomeação), `TRATATIVAS_PEDIDOS_PARADOS.md` (nota no topo), `painel_agentes/README.md` se citar Fresh Hub em pedidos parados (`grep -n "Fresh Hub\|lovable" painel_agentes/README.md`).
- Script descartável no scratchpad: `prova_pedidos_parados.py` (Playwright, viewport 390×844, segue `feedback_testar_painel_playwright_local`: `/login?proximo=`, cookie Secure off, `localStorage tour_visto`).

- [ ] **Step 1: Prova Playwright na porta 8099** (banco local de desenvolvimento): logar como galpão → aba Registrar aberta por padrão → registrar "PS-40525", 3 volumes, com um JPG gerado → aparece em "Registrados hoje" → repetir o número e ver o aviso amarelo → "Apagar" some da lista → logar como total → Triagem mostra a linha com "Registrado por" e miniatura → "Exportar CSV" baixa arquivo → classificar "Cancelados" mostra "✓ classificado · encaminhamento pausado" e some com "só não tratados" marcado. Prints em `scratchpad/prova_pp_*.png`; ler os prints. Ao final, apagar do `dados.db` local os registros de teste (`DELETE FROM pedidos_parados_registros`, `DELETE FROM pedidos_parados_classificacao WHERE order_number='40525'`) e a pasta `dados/pedidos_parados/`.

- [ ] **Step 2: Documentação.** `TRATATIVAS_PEDIDOS_PARADOS.md`: bloco "**24/10/2026 — origem mudou**" no topo (registro na aba Registrar do painel, tabela `pedidos_parados_registros`, sem importar histórico do Lovable, Demandas pausadas por `ENCAMINHAMENTO_DEMANDAS_ATIVO`, classificações que criavam Demanda contam como tratadas enquanto pausado). `MAPA_DO_SISTEMA.txt`: descrição de `pedidos_parados_triagem.py` (origem local, aba Registrar, tabela, pasta `dados/pedidos_parados/`, flag), rotas novas na lista de `/pedidos-parados`, `lovable/` passa a "só Demandas (pausado) e `normalizar_order_number`", e a armadilha do `sessao_freshhub.json` ganha "só importa se religar as Demandas".

- [ ] **Step 3: Rodada final.** De `painel_agentes/`: `py -3.11 -m unittest test_pedidos_parados_registro test_pedidos_parados_reenvio test_acesso_telas -v` verde; `py -3.11 -m py_compile painel_agentes.py pedidos_parados_triagem.py contadores_menu.py`; `git status --short` só com os arquivos deste plano (mais os docs já untracked de outras sessões). Relatar ao Hugo e esperar o "commit"/"deploy" (deploy: `painel-agentes` reinicia; na VPS criar `dados/pedidos_parados/` como `www-data` se o primeiro registro não criar sozinho).
