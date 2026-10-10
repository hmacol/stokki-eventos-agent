# Motoristas sem app (iPhone) — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a rota de motorista marcado `SEM_APP` não é devolvida ao pool pelo job das 15h45 e, no dia seguinte, aparece na Torre com uma tela para dar baixa (entregue / insucesso com motivo) na Vuupt.

**Architecture:** `regras/preferencias_motoristas.py` lê a coluna `SEM_APP`; o job `cancelar_rotas_sem_motorista.py` pula esses `agent_id`; um módulo novo `nucleo/baixa_sem_app.py` concentra a regra da Torre (lê o núcleo), o lote de baixa (tabela `baixas_sem_app`) e a conclusão na Vuupt; o painel ganha a tela `/baixa-sem-app` e duas APIs; a Torre chama `excecoes_torre` do módulo novo.

**Tech Stack:** Python 3.11, SQLite (`dados/dados.db`), Flask/Jinja (painel), pandas/openpyxl, unittest, API Vuupt (`vuupt_client.VuuptClient`, `roteirizacao/rotas_client.py`).

**Spec:** `docs/superpowers/specs/2026-10-08-motoristas-sem-app-design.md`

## Global Constraints

- Python: **`py -3.11`**; testes com `py -3.11 -m unittest`, nunca pytest. Testes de `painel_agentes` em comando separado dos demais.
- Coluna `SEM_APP` (SIM/vazio) no `dados/BD_MOTORISTAS.xlsx`; vazio ou ausente = não. Marcados agora: 50191 (Iago Mendes), 51469 (Watson Kaue), 50199 (Rafael Batista Ribeiro).
- Tela/API de baixa: níveis `total` e `operador`; POST com `@exige_mesma_origem`.
- Não entregue exige `failed_reason_id` de `insucesso_entrega/motivos_falha.MOTIVOS_FALHA`.
- Serviço já `done`/`canceled` na Vuupt: pulado (idempotente).
- Torre lê só o núcleo (`nucleo_rotas`/`nucleo_paradas`), nunca a Vuupt.
- Falha ao ler a planilha no job das 15h45: conjunto vazio + aviso (comportamento de hoje).
- Commits só quando o Hugo pedir, só os arquivos da tarefa (CLAUDE.md). `dados/BD_MOTORISTAS.xlsx` **não** entra em commit: a VPS tem a planilha modificada localmente pelo cadastro do painel; a coluna é criada na VPS no deploy (Task 6).

## Review Focus

1. Planilha sem a coluna `SEM_APP` (VPS antes do passo de deploy, ou local): nada muda no job e a Torre não mostra item (teste na Task 1 e Task 3).
2. Rota de motorista sem app em que **parte** já foi concluída na Vuupt: tela mostra os fechados em cinza e só baixa o resto (teste na Task 3).
3. Clique duplo em "Confirmar" / repetir a baixa: segundo lote não reconclui nada (teste na Task 3).
4. POST com rota de motorista que **tem** app, ou item não entregue sem motivo: recusado com 400 (teste na Task 5).
5. Erro da Vuupt no meio do lote (ex.: 409): o item fica "erro" com a mensagem e os demais seguem (teste na Task 3).

---

## Mapa de arquivos

| Arquivo | Papel |
|---|---|
| `regras/preferencias_motoristas.py` (mod) | campo `sem_app`, `agent_ids_sem_app()` com cache por mtime |
| `roteirizacao/cancelar_rotas_sem_motorista.py` (mod) | pula `sem_app`, resumo `aguardando_baixa` |
| `nucleo/baixa_sem_app.py` (novo) | Torre, lote `baixas_sem_app`, `concluir_servico`, `executar_lote` |
| `painel_agentes/torre_controle.py` (mod) | chama `nucleo.baixa_sem_app.excecoes_torre` |
| `painel_agentes/painel_agentes.py` (mod) | `GET /baixa-sem-app`, `POST /api/baixa-sem-app`, `GET /api/baixa-sem-app/<lote>` |
| `painel_agentes/templates/baixa_sem_app.html` (novo) | tela |
| testes | `regras/test_preferencias_motoristas.py`, `roteirizacao/test_devolver_rotas_passadas.py`, `nucleo/test_baixa_sem_app.py` (novo), `painel_agentes/test_baixa_sem_app_tela.py` (novo) |
| docs | `MAPA_DO_SISTEMA.txt`, spec |

---

### Task 1: `SEM_APP` no catálogo de motoristas

**Files:**
- Modify: `regras/preferencias_motoristas.py` (dataclass ~linha 142, `_construir_motorista` ~linha 245, fim do arquivo)
- Test: `regras/test_preferencias_motoristas.py`

**Interfaces:**
- Produces: `MotoristaPreferencias.sem_app: bool`; `agent_ids_sem_app(caminho: str | Path | None = None) -> set[int]` (padrão `dados/BD_MOTORISTAS.xlsx` da raiz do projeto; cache por `(caminho, mtime)`; planilha ausente → `set()`).

- [ ] **Step 1: Teste que falha** — acrescentar em `regras/test_preferencias_motoristas.py`:

```python
class TestSemApp(unittest.TestCase):
    def _planilha(self, tmp, com_coluna=True):
        caminho = Path(tmp) / "m.xlsx"
        linhas = [{"AGENT_ID_VUUPT": 1, "NOME_MOTORISTA": "Iphone", "ATIVO": "SIM"},
                  {"AGENT_ID_VUUPT": 2, "NOME_MOTORISTA": "Android", "ATIVO": "SIM"}]
        if com_coluna:
            linhas[0]["SEM_APP"] = "SIM"
            linhas[1]["SEM_APP"] = None
        pd.DataFrame(linhas).to_excel(caminho, index=False)
        return caminho

    def test_coluna_sim_vira_sem_app(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            caminho = self._planilha(tmp)
            por_id = {m.agent_id: m for m in CatalogoMotoristas.carregar(str(caminho), "").motoristas}
            self.assertTrue(por_id[1].sem_app)
            self.assertFalse(por_id[2].sem_app)
            self.assertEqual(agent_ids_sem_app(caminho), {1})

    def test_sem_coluna_ou_sem_planilha_e_vazio(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.assertEqual(agent_ids_sem_app(self._planilha(tmp, com_coluna=False)), set())
            self.assertEqual(agent_ids_sem_app(Path(tmp) / "nao_existe.xlsx"), set())
```

e no import do topo do teste acrescentar `agent_ids_sem_app` ao `from regras.preferencias_motoristas import ...` (conferir a linha de import existente e só somar o nome).

- [ ] **Step 2: Ver falhar** — `py -3.11 -m unittest regras.test_preferencias_motoristas` → `ImportError: cannot import name 'agent_ids_sem_app'`.

- [ ] **Step 3: Implementar.** Na dataclass, depois de `apenas_carga_seca`:

```python
    sem_app: bool = False  # coluna SEM_APP -- iPhone, sem app Vuupt (Hugo, 08/10): rota nao e devolvida as 15h45, baixa pela Torre
```

No `return MotoristaPreferencias(...)` de `_construir_motorista`, depois de `apenas_carga_seca=...`:

```python
        sem_app=_parse_bool(registro.get("SEM_APP")),
```

No fim do arquivo:

```python
PLANILHA_PADRAO = Path(__file__).resolve().parent.parent / "dados" / "BD_MOTORISTAS.xlsx"
_cache_sem_app: dict[tuple[str, float], set[int]] = {}


def agent_ids_sem_app(caminho: str | Path | None = None) -> set[int]:
    """agent_ids marcados SEM_APP na planilha (Hugo, 08/10). Planilha
    ausente ou sem a coluna = conjunto vazio. Cache por mtime: a Torre chama
    isto a cada carga."""
    caminho = Path(caminho) if caminho else PLANILHA_PADRAO
    if not caminho.exists():
        return set()
    chave = (str(caminho), caminho.stat().st_mtime)
    if chave not in _cache_sem_app:
        catalogo = CatalogoMotoristas.carregar(caminho)
        _cache_sem_app.clear()
        _cache_sem_app[chave] = {m.agent_id for m in catalogo.motoristas if m.sem_app}
    return set(_cache_sem_app[chave])
```

(conferir que `Path` já está importado no módulo; está, é usado em `CatalogoMotoristas.carregar`.)

- [ ] **Step 4: Ver passar** — `py -3.11 -m unittest regras.test_preferencias_motoristas` → OK.

---

### Task 2: Job das 15h45 pula motorista sem app

**Files:**
- Modify: `roteirizacao/cancelar_rotas_sem_motorista.py` (`devolver_pendentes_de_rotas_passadas` ~linha 165, `main` ~linha 225, docstring do módulo)
- Test: `roteirizacao/test_devolver_rotas_passadas.py`

**Interfaces:**
- Consumes: `agent_ids_sem_app()` (Task 1).
- Produces: `devolver_pendentes_de_rotas_passadas(token, hoje, modo_teste, sem_app: set[int] | None = None) -> dict` com chave nova `aguardando_baixa: list[str]`.

- [ ] **Step 1: Teste que falha** — em `roteirizacao/test_devolver_rotas_passadas.py`, mudar `_rota` para aceitar `agent_id` e acrescentar o teste:

```python
def _rota(servicos, status="in_progress", rota_id=10, start_at="2026-09-28 08:00:00", agent_id=None):
    return {"id": rota_id, "name": "Planejamento - 28/09/2026 - #1", "status": status,
            "start_at": start_at, "agent_id": agent_id, "services": {"data": servicos}}
```

```python
    def test_motorista_sem_app_nao_e_devolvido(self):
        rotas = [_rota([_s(1, "assigned"), _s(2, "done")], rota_id=10, agent_id=50191),
                 _rota([_s(3, "assigned")], rota_id=11, agent_id=999)]
        with mock.patch.object(crsm, "listar_rotas", return_value=rotas), \
             mock.patch.object(crsm, "atualizar_rota") as atualizar, \
             mock.patch.object(crsm, "cancelar_rota") as cancelar, \
             mock.patch.object(crsm, "reverter_por_vuupt_route_id"), \
             mock.patch.object(crsm.tratativas, "registrar_evento"), \
             mock.patch("nucleo.sincronizar_servicos_vuupt.ressincronizar_ids"), \
             mock.patch("vuupt_client.VuuptClient"):
            r = crsm.devolver_pendentes_de_rotas_passadas("t", HOJE, modo_teste=False, sem_app={50191})
        cancelar.assert_called_once_with("t", 11, services_action="unassign")
        atualizar.assert_not_called()
        self.assertEqual(r["devolvidos"], ["PS-3"])
        self.assertEqual(len(r["aguardando_baixa"]), 1)
        self.assertIn("1 pedido", r["aguardando_baixa"][0])
```

(Conferir se o teste existente `test_rota_de_hoje_nao_entra_e_devolucao_registra` continua passando com o `_rota` novo — `agent_id=None` mantém o comportamento.)

- [ ] **Step 2: Ver falhar** — `py -3.11 -m unittest roteirizacao.test_devolver_rotas_passadas` → `TypeError: ... unexpected keyword argument 'sem_app'`.

- [ ] **Step 3: Implementar.** Assinatura e início do laço:

```python
def devolver_pendentes_de_rotas_passadas(token: str, hoje: date, modo_teste: bool,
                                         sem_app: set[int] | None = None) -> dict:
    """Passo 2 (ver docstring do módulo). Retorna o resumo pro e-mail.
    `sem_app`: agent_ids de motorista sem app (iPhone, BD_MOTORISTAS
    SEM_APP; Hugo, 08/10) -- a rota deles NUNCA e devolvida: o motorista
    fez a entrega mas nada foi registrado; a baixa e pela Torre."""
    sem_app = sem_app or set()
```

`resumo` passa a ser `{"devolvidos": [], "iniciados": [], "erros": [], "aguardando_baixa": []}` e, logo depois de `nome = ...`/antes de `plano = plano_devolucao(...)`, mover o cálculo do `nome` para cima se preciso e inserir:

```python
        if rota.get("agent_id") in sem_app:
            abertos = [s for s in extrair_servicos_da_rota(rota)
                       if s.get("id") and (s.get("status") or "") not in ("done", "canceled", "cancelled")]
            if rota.get("status") not in STATUS_ROTA_ENCERRADA and abertos:
                resumo["aguardando_baixa"].append(
                    f"{rota.get('name') or rota.get('id')} (agent {rota.get('agent_id')}, "
                    f"{data_rota:%d/%m}, {len(abertos)} pedido(s))")
            continue
```

Em `main`, antes do `try` do passo 2:

```python
    try:
        from regras.preferencias_motoristas import agent_ids_sem_app
        sem_app = agent_ids_sem_app()
    except Exception as e:
        logger.warning(f"BD_MOTORISTAS (SEM_APP) ilegivel -- segue sem excecao de motorista: {e}")
        sem_app = set()
```

chamar `devolver_pendentes_de_rotas_passadas(token, date.today(), modo_teste, sem_app)` e, depois do bloco `if dev["iniciados"]`, acrescentar:

```python
        if dev["aguardando_baixa"]:
            detalhe += (f". Rotas de motorista sem app aguardando baixa na Torre (não devolvidas): "
                        f"{'; '.join(dev['aguardando_baixa'])}")
```

Docstring do módulo: acrescentar um parágrafo "08/10 (Hugo): rota de motorista SEM_APP (iPhone) não é devolvida -- baixa pela Torre (nucleo/baixa_sem_app.py)".

- [ ] **Step 4: Ver passar** — `py -3.11 -m unittest roteirizacao.test_devolver_rotas_passadas` → OK.

---

### Task 3: `nucleo/baixa_sem_app.py`

**Files:**
- Create: `nucleo/baixa_sem_app.py`
- Test: `nucleo/test_baixa_sem_app.py`

**Interfaces:**
- Consumes: `nucleo.banco` (`conectar(caminho)`, `PARADA_*`, `ROTA_*`), `regras.preferencias_motoristas.agent_ids_sem_app`, `lalamove_integracao.STATUS_VUUPT_FECHADO`, `lalamove_integracao._liberar_rota_agendada(vuupt, route_id)`, `tratativas.registrar_evento(code, origem, evento, *, service_id, motivo_id, motivo_texto, texto)`, `nucleo.sincronizar_servicos_vuupt.ressincronizar_ids(vuupt, ids)`.
- Produces:
  - `rotas_aguardando_baixa(conn, hoje: date, sem_app: set[int]) -> list[dict]` (chaves `vuupt_route_id, agent_id, motorista_nome, data_rota, nome, abertos`)
  - `excecoes_torre(data_iso: str, db_path=None, hoje: date | None = None, sem_app: set[int] | None = None) -> list[dict]`
  - `concluir_servico(vuupt, service_id: int, sucesso: bool, failed_reason_id: int | None, agent_id: int | None) -> str` (`"ja_fechado" | "entregue" | "insucesso"`)
  - `criar_lote(conn, vuupt_route_id: int, agent_id: int, por: str, itens: list[dict]) -> int` (item: `service_id, codigo, entregue, failed_reason_id`)
  - `ler_lote(conn, lote_id: int) -> dict | None` (com `itens` decodificados e `terminado`)
  - `executar_lote(lote_id: int, vuupt, db_path=None) -> dict`

- [ ] **Step 1: Teste que falha** — criar `nucleo/test_baixa_sem_app.py`:

```python
# -*- coding: utf-8 -*-
"""Testes de nucleo/baixa_sem_app.py (banco temporario, Vuupt falsa). Rodar da raiz:
py -3.11 -m unittest nucleo.test_baixa_sem_app"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from nucleo import baixa_sem_app as b
from nucleo import banco

HOJE = date(2026, 10, 9)


class VuuptFalsa:
    def __init__(self, status):
        self.status = dict(status)          # service_id -> status
        self.concluidos = []

    def buscar_servico_por_id(self, sid):
        return {"id": sid, "status": self.status[sid], "route_id": 77}

    def concluir_como_agente(self, sid, sucesso=True, failed_reason_id=None, status_atual=""):
        if self.status[sid] == "erro":
            raise RuntimeError("409 conflito")
        self.concluidos.append((sid, sucesso, failed_reason_id))
        self.status[sid] = "done"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = banco.conectar(self.db)
        self.addCleanup(self.conn.close)

    def rota(self, data_rota, agent_id, situacoes, status="PLANEJADA", vuupt_id=5000):
        cur = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, nome, provedor, vuupt_route_id, agent_id, motorista_nome, status) "
            "VALUES (?, 'Rota X', 'VUUPT', ?, ?, 'Iago', ?)", (data_rota, vuupt_id, agent_id, status))
        for i, s in enumerate(situacoes):
            self.conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, codigo, situacao) VALUES (?, ?, ?, ?)",
                              (cur.lastrowid, i, f"PS-{i}", s))
        self.conn.commit()


class Torre(Base):
    def test_rota_de_ontem_com_pendente_aparece(self):
        self.rota("2026-10-08", 50191, ["PENDENTE", "PENDENTE", "ENTREGUE"])
        itens = b.excecoes_torre("2026-10-09", db_path=self.db, hoje=HOJE, sem_app={50191})
        self.assertEqual([x["id"] for x in itens], ["semapp:5000"])
        self.assertIn("2 pedido(s)", itens[0]["descricao"])
        self.assertEqual(itens[0]["acao"]["url"], "/baixa-sem-app?rota=5000")

    def test_filtros(self):
        self.rota("2026-10-09", 50191, ["PENDENTE"], vuupt_id=1)                       # hoje
        self.rota("2026-10-08", 50191, ["ENTREGUE"], vuupt_id=2)                       # nada pendente
        self.rota("2026-10-08", 50191, ["PENDENTE"], status="CANCELADA", vuupt_id=3)   # cancelada
        self.rota("2026-10-08", 999, ["PENDENTE"], vuupt_id=4)                         # motorista com app
        self.rota("2026-10-08", 50191, ["PENDENTE"], vuupt_id=None)                    # sem rota na Vuupt
        self.assertEqual(b.excecoes_torre("2026-10-09", db_path=self.db, hoje=HOJE, sem_app={50191}), [])

    def test_sem_marcados_nao_gera_nada(self):
        self.rota("2026-10-08", 50191, ["PENDENTE"])
        self.assertEqual(b.excecoes_torre("2026-10-09", db_path=self.db, hoje=HOJE, sem_app=set()), [])


class Lote(Base):
    def itens(self):
        return [{"service_id": 1, "codigo": "PS-1", "entregue": True, "failed_reason_id": None},
                {"service_id": 2, "codigo": "PS-2", "entregue": False, "failed_reason_id": 5433},
                {"service_id": 3, "codigo": "PS-3", "entregue": True, "failed_reason_id": None}]

    def executar(self, vuupt, lote):
        with mock.patch.object(b, "_liberar_rota_agendada"), \
             mock.patch.object(b, "_registrar_tratativa"), \
             mock.patch.object(b, "_ressincronizar"):
            return b.executar_lote(lote, vuupt, db_path=self.db)

    def test_entregue_e_insucesso_com_motivo(self):
        vuupt = VuuptFalsa({1: "assigned", 2: "assigned", 3: "done"})
        lote = b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens())
        r = self.executar(vuupt, lote)
        self.assertEqual(vuupt.concluidos, [(1, True, None), (2, False, 5433)])
        self.assertEqual([i["status"] for i in r["itens"]], ["entregue", "insucesso", "ja_fechado"])
        self.assertTrue(r["terminado"])

    def test_repetir_nao_reconclui(self):
        vuupt = VuuptFalsa({1: "assigned", 2: "assigned", 3: "assigned"})
        self.executar(vuupt, b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens()))
        r = self.executar(vuupt, b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens()))
        self.assertEqual(len(vuupt.concluidos), 3)
        self.assertEqual({i["status"] for i in r["itens"]}, {"ja_fechado"})

    def test_erro_num_item_nao_para_os_outros(self):
        vuupt = VuuptFalsa({1: "erro", 2: "assigned", 3: "assigned"})
        r = self.executar(vuupt, b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens()))
        self.assertEqual([i["status"] for i in r["itens"]], ["erro", "insucesso", "entregue"])
        self.assertIn("409", r["itens"][0]["erro"])
        self.assertEqual(b.ler_lote(self.conn, r["id"])["itens"][0]["status"], "erro")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Ver falhar** — `py -3.11 -m unittest nucleo.test_baixa_sem_app` → `ImportError: cannot import name 'baixa_sem_app'`.

- [ ] **Step 3: Implementar** — criar `nucleo/baixa_sem_app.py`:

```python
# -*- coding: utf-8 -*-
"""
nucleo/baixa_sem_app.py

Motorista sem app (iPhone; BD_MOTORISTAS SEM_APP, Hugo 08/10): a rota
dele nao e devolvida as 15h45 e, no dia seguinte, aparece na Torre
"aguardando baixa". A tela /baixa-sem-app marca o que voltou e este
modulo conclui na Vuupt: entregue = sucesso, voltou = insucesso com o
motivo (fluxo normal de insucesso). Spec:
docs/superpowers/specs/2026-10-08-motoristas-sem-app-design.md
"""
import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from nucleo import banco

logger = logging.getLogger(__name__)
FMT = "%Y-%m-%d %H:%M:%S"
PARADAS_PENDENTES = (banco.PARADA_PENDENTE, banco.PARADA_EM_DESLOCAMENTO, banco.PARADA_EM_ROTA)
STATUS_FECHADO = {"done", "canceled", "cancelled"}


def _liberar_rota_agendada(vuupt, route_id: int) -> None:
    from lalamove_integracao import _liberar_rota_agendada as liberar
    liberar(vuupt, route_id)


def garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS baixas_sem_app (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            vuupt_route_id INTEGER NOT NULL,
            agent_id       INTEGER,
            criado_por     TEXT,
            criado_em      TEXT NOT NULL,
            itens_json     TEXT NOT NULL,
            terminado_em   TEXT
        )""")
    conn.commit()


# ── Torre ─────────────────────────────────────────────────────────────────────

def rotas_aguardando_baixa(conn: sqlite3.Connection, hoje: date, sem_app: set[int]) -> list[dict]:
    if not sem_app:
        return []
    ags = ",".join("?" * len(sem_app))
    pend = ",".join("?" * len(PARADAS_PENDENTES))
    return [dict(r) for r in conn.execute(f"""
        SELECT r.vuupt_route_id, r.agent_id, r.motorista_nome, r.data_rota, r.nome,
               SUM(CASE WHEN p.situacao IN ({pend}) THEN 1 ELSE 0 END) AS abertos
        FROM nucleo_rotas r JOIN nucleo_paradas p ON p.rota_id = r.id
        WHERE r.agent_id IN ({ags}) AND r.data_rota < ? AND r.vuupt_route_id IS NOT NULL
          AND r.status NOT IN (?, ?)
        GROUP BY r.id HAVING abertos > 0
        ORDER BY r.data_rota, r.id
    """, (*PARADAS_PENDENTES, *sorted(sem_app), hoje.isoformat(), banco.ROTA_CONCLUIDA, banco.ROTA_CANCELADA))]


def excecoes_torre(data_iso: str, db_path: Path | None = None, hoje: date | None = None,
                   sem_app: set[int] | None = None) -> list[dict]:
    """Formato de torre_controle._montar_excecoes (sem _epoch): uma rota de
    motorista sem app, de dia anterior, com pedido pendente = um item."""
    hoje = hoje or date.today()
    if sem_app is None:
        from regras.preferencias_motoristas import agent_ids_sem_app
        sem_app = agent_ids_sem_app()
    if not sem_app:
        return []
    conn = banco.conectar(db_path)
    try:
        rotas = rotas_aguardando_baixa(conn, hoje, sem_app)
    finally:
        conn.close()
    itens = []
    for r in rotas:
        dia = datetime.strptime(r["data_rota"][:10], "%Y-%m-%d").strftime("%d/%m")
        itens.append({
            "id": f"semapp:{r['vuupt_route_id']}",
            "severidade": "atencao", "tipo": "Sem app",
            "descricao": f"Rota de {r['motorista_nome'] or r['agent_id']} de {dia} aguardando baixa "
                         f"({r['abertos']} pedido(s)).",
            "quando": None,
            "acao": {"tipo": "link", "url": f"/baixa-sem-app?rota={r['vuupt_route_id']}", "rotulo": "Dar baixa"},
        })
    return itens


# ── Baixa ─────────────────────────────────────────────────────────────────────

def concluir_servico(vuupt, service_id: int, sucesso: bool, failed_reason_id: int | None,
                     agent_id: int | None) -> str:
    """Mesmo caminho de lalamove_integracao._concluir_na_vuupt, com insucesso."""
    servico = vuupt.buscar_servico_por_id(service_id) or {}
    status = str(servico.get("status") or "")
    if status in STATUS_FECHADO:
        return "ja_fechado"
    if status in ("", "not_assigned"):
        route_id = servico.get("route_id")
        if route_id:
            _liberar_rota_agendada(vuupt, int(route_id))
            status = str((vuupt.buscar_servico_por_id(service_id) or {}).get("status") or "")
        elif agent_id:
            vuupt.atribuir_agente(service_id, agent_id)
            status = "assigned"
    vuupt.concluir_como_agente(service_id, sucesso=sucesso,
                               failed_reason_id=None if sucesso else failed_reason_id, status_atual=status)
    return "entregue" if sucesso else "insucesso"


def criar_lote(conn: sqlite3.Connection, vuupt_route_id: int, agent_id: int, por: str, itens: list[dict]) -> int:
    garantir_tabela(conn)
    itens = [{"service_id": int(i["service_id"]), "codigo": i.get("codigo") or "",
              "entregue": bool(i["entregue"]), "failed_reason_id": i.get("failed_reason_id"),
              "status": "pendente"} for i in itens]
    cur = conn.execute(
        "INSERT INTO baixas_sem_app (vuupt_route_id, agent_id, criado_por, criado_em, itens_json) VALUES (?, ?, ?, ?, ?)",
        (vuupt_route_id, agent_id, por, datetime.now().strftime(FMT), json.dumps(itens, ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid


def ler_lote(conn: sqlite3.Connection, lote_id: int) -> dict | None:
    garantir_tabela(conn)
    row = conn.execute("SELECT * FROM baixas_sem_app WHERE id = ?", (lote_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["itens"] = json.loads(d.pop("itens_json"))
    d["terminado"] = bool(d["terminado_em"])
    return d


def _salvar_itens(conn, lote_id, itens, terminado=False):
    conn.execute("UPDATE baixas_sem_app SET itens_json = ?" + (", terminado_em = ?" if terminado else "") +
                 " WHERE id = ?",
                 (json.dumps(itens, ensure_ascii=False), *([datetime.now().strftime(FMT)] if terminado else []), lote_id))
    conn.commit()


def _registrar_tratativa(item: dict, por: str) -> None:
    import tratativas
    from insucesso_entrega.motivos_falha import texto_do_motivo
    if item["status"] == "entregue":
        texto = f"Baixa de motorista sem app por {por}: entregue."
        tratativas.registrar_evento(item["codigo"], "BAIXA_SEM_APP", "BAIXA_SEM_APP",
                                    service_id=item["service_id"], texto=texto)
    elif item["status"] == "insucesso":
        motivo = texto_do_motivo(item["failed_reason_id"])
        tratativas.registrar_evento(item["codigo"], "BAIXA_SEM_APP", "BAIXA_SEM_APP",
                                    service_id=item["service_id"], motivo_id=item["failed_reason_id"],
                                    motivo_texto=motivo, texto=f"Baixa de motorista sem app por {por}: voltou ({motivo}).")


def _ressincronizar(vuupt, ids: list[int]) -> None:
    from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids
    ressincronizar_ids(vuupt, ids)


def executar_lote(lote_id: int, vuupt, db_path: Path | None = None) -> dict:
    """Roda em thread de fundo (painel). Salva o andamento item a item."""
    conn = banco.conectar(db_path)
    try:
        lote = ler_lote(conn, lote_id)
        itens = lote["itens"]
        for item in itens:
            if item["status"] != "pendente":
                continue
            try:
                item["status"] = concluir_servico(vuupt, item["service_id"], item["entregue"],
                                                  item.get("failed_reason_id"), lote["agent_id"])
            except Exception as e:  # noqa: BLE001 -- um item com erro nao para os outros
                item["status"], item["erro"] = "erro", str(e)[:200]
                logger.warning(f"Baixa sem app lote {lote_id}: {item['codigo']} falhou: {e}")
            _salvar_itens(conn, lote_id, itens)
            if item["status"] in ("entregue", "insucesso"):
                try:
                    _registrar_tratativa(item, lote["criado_por"] or "?")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Tratativa de {item['codigo']} nao registrada: {e}")
        _salvar_itens(conn, lote_id, itens, terminado=True)
        feitos = [i["service_id"] for i in itens if i["status"] in ("entregue", "insucesso")]
        if feitos:
            try:
                _ressincronizar(vuupt, feitos)
            except Exception as e:  # noqa: BLE001 -- o timer de 15 min alcanca
                logger.warning(f"Ressincronizacao do nucleo falhou: {e}")
        return ler_lote(conn, lote_id)
    finally:
        conn.close()
```

- [ ] **Step 4: Ver passar** — `py -3.11 -m unittest nucleo.test_baixa_sem_app` → OK (6 testes). Conferir também que `texto_do_motivo` existe em `insucesso_entrega/motivos_falha.py` (existe, linha ~153) e que `tratativas.registrar_evento` aceita `motivo_id`, `motivo_texto`, `texto` (conferir a assinatura em `tratativas.py:88`; se `texto` não for parâmetro, usar o nome que ela tiver).

---

### Task 4: Torre chama o módulo

**Files:**
- Modify: `painel_agentes/torre_controle.py` (logo depois do bloco do batimento, ~linha 1515)
- Test: `painel_agentes/test_baixa_sem_app_tela.py` (criado aqui, ampliado na Task 5)

- [ ] **Step 1: Teste que falha** — criar `painel_agentes/test_baixa_sem_app_tela.py` com:

```python
# -*- coding: utf-8 -*-
"""
Tela /baixa-sem-app e Torre (motorista sem app, 08/10).

    py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela
"""
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


class TestTorreSemApp(unittest.TestCase):
    def test_torre_chama_o_modulo(self):
        src = (_AQUI / "torre_controle.py").read_text(encoding="utf-8")
        self.assertIn("from nucleo.baixa_sem_app import excecoes_torre as sem_app_excecoes", src)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Ver falhar** — `py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela` → AssertionError.

- [ ] **Step 3: Implementar** — em `torre_controle.py`, depois do `except` do batimento:

```python
    # Motorista sem app (iPhone, 08/10): rota de dia anterior com pedido
    # pendente aguardando baixa. Só lê o núcleo.
    try:
        from nucleo.baixa_sem_app import excecoes_torre as sem_app_excecoes
        for x in sem_app_excecoes(data_iso):
            excecoes.append({**x, "_epoch": 0.0})
    except Exception as e:
        logger.warning(f"[torre] Baixa sem app indisponível: {e}")
```

- [ ] **Step 4: Ver passar** — `py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela painel_agentes.test_torre_batimento` → OK.

---

### Task 5: Tela e APIs de baixa no painel

**Files:**
- Modify: `painel_agentes/painel_agentes.py` (rotas novas logo depois de `api_batimento_tratar`)
- Create: `painel_agentes/templates/baixa_sem_app.html`
- Test: `painel_agentes/test_baixa_sem_app_tela.py`

**Interfaces:**
- Consumes: `rotas_client.buscar_rota(token, route_id, include=["services"])`, `mapa_util.extrair_servicos_da_rota(rota)`, `agent_ids_sem_app()`, `nucleo.baixa_sem_app.criar_lote/ler_lote/executar_lote`, `insucesso_entrega.motivos_falha.MOTIVOS_FALHA`, `vuupt_client.VuuptClient(token)`, `_carregar_config()`.
- Produces: `GET /baixa-sem-app?rota=<id>`; `POST /api/baixa-sem-app` body `{"rota": int, "itens": [{"service_id", "codigo", "entregue", "failed_reason_id"}]}` → `{"lote": id}` ou 400 `{"erro"}`; `GET /api/baixa-sem-app/<lote>` → lote.

- [ ] **Step 1: Testes que falham** — acrescentar em `painel_agentes/test_baixa_sem_app_tela.py`:

```python
ROTA = {"id": 5000, "name": "Planejamento - 07/10/2026 - #18", "agent_id": 50191, "start_at": "2026-10-07 08:00:00",
        "services": {"data": [{"id": 1, "code": "#PS-1", "status": "assigned", "title": "Cliente A"},
                              {"id": 2, "code": "#PS-2", "status": "done", "title": "Cliente B"}]}}


class TestTelaBaixa(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                  mock.patch("rotas_client.buscar_rota", return_value=ROTA),
                  mock.patch("regras.preferencias_motoristas.agent_ids_sem_app", return_value={50191}),
                  mock.patch("nucleo.baixa_sem_app.executar_lote")):
            p.start()
            self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def _post(self, body):
        return self.cliente.post("/api/baixa-sem-app", json=body, headers={"Origin": "http://localhost"})

    def test_tela_lista_pedidos_e_motivos(self):
        self._logar("operador")
        html = self.cliente.get("/baixa-sem-app?rota=5000").get_data(as_text=True)
        for trecho in ("PS-1", "PS-2", "Cliente A", "Endereço Incorreto", "Confirmar baixa"):
            self.assertIn(trecho, html)

    def test_leitura_nao_entra(self):
        self._logar("leitura")
        self.assertIn(self.cliente.get("/baixa-sem-app?rota=5000").status_code, (302, 401, 403))
        self.assertIn(self._post({"rota": 5000, "itens": []}).status_code, (302, 401, 403))

    def test_post_cria_lote_e_dispara(self):
        self._logar("operador")
        r = self._post({"rota": 5000, "itens": [
            {"service_id": 1, "codigo": "PS-1", "entregue": False, "failed_reason_id": 5433}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        lote = r.get_json()["lote"]
        st = self.cliente.get(f"/api/baixa-sem-app/{lote}").get_json()
        self.assertEqual(st["itens"][0]["failed_reason_id"], 5433)

    def test_post_recusa_sem_motivo_e_motorista_com_app(self):
        self._logar("operador")
        r = self._post({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": False}]})
        self.assertEqual(r.status_code, 400)
        with mock.patch("regras.preferencias_motoristas.agent_ids_sem_app", return_value=set()):
            r = self._post({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": True}]})
        self.assertEqual(r.status_code, 400)
```

- [ ] **Step 2: Ver falhar** — `py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela` → 404s / falhas.

- [ ] **Step 3: Rotas.** Em `painel_agentes.py`, depois de `api_batimento_tratar`:

```python
def _rota_sem_app(route_id: int) -> tuple[dict | None, str | None]:
    """Rota da Vuupt + checagem de motorista SEM_APP. (rota, erro)."""
    import rotas_client
    from regras import preferencias_motoristas
    token = (_carregar_config().get("vuupt_api") or {}).get("token", "")
    rota = rotas_client.buscar_rota(token, route_id, include=["services"])
    if not rota:
        return None, "Rota não encontrada na Vuupt."
    if rota.get("agent_id") not in preferencias_motoristas.agent_ids_sem_app():
        return rota, "Essa rota não é de motorista marcado SEM_APP: a baixa é pelo app do motorista."
    return rota, None


@app.route("/baixa-sem-app")
@requer_auth(niveis=("total", "operador"))
def baixa_sem_app():
    """Baixa de rota de motorista sem app (iPhone, Hugo 08/10): marca o que
    voltou e conclui na Vuupt (nucleo/baixa_sem_app.py)."""
    from mapa_util import extrair_servicos_da_rota
    from insucesso_entrega.motivos_falha import MOTIVOS_FALHA
    route_id = request.args.get("rota", type=int)
    rota, erro = _rota_sem_app(route_id) if route_id else (None, "Rota não informada.")
    servicos = []
    for s in (extrair_servicos_da_rota(rota) if rota else []):
        servicos.append({"id": s.get("id"), "codigo": (s.get("code") or "").lstrip("#"),
                         "titulo": s.get("title") or "", "endereco": s.get("address") or "",
                         "status": s.get("status") or "",
                         "fechado": (s.get("status") or "") in ("done", "canceled", "cancelled")})
    motivos = sorted(((k, v["texto"]) for k, v in MOTIVOS_FALHA.items()), key=lambda kv: kv[1])
    return render_template("baixa_sem_app.html", rota=rota, erro=erro, servicos=servicos, motivos=motivos)


@app.route("/api/baixa-sem-app", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_baixa_sem_app():
    import threading
    from nucleo import baixa_sem_app as bsa
    from vuupt_client import VuuptClient
    from insucesso_entrega.motivos_falha import MOTIVOS_FALHA
    body = request.get_json(force=True) or {}
    try:
        route_id = int(body.get("rota"))
    except (TypeError, ValueError):
        return jsonify({"erro": "rota inválida"}), 400
    itens = body.get("itens")
    if not isinstance(itens, list) or not itens:
        return jsonify({"erro": "nenhum pedido"}), 400
    for i in itens:
        if not i.get("entregue") and int(i.get("failed_reason_id") or 0) not in MOTIVOS_FALHA:
            return jsonify({"erro": f"{i.get('codigo')}: escolha o motivo de quem voltou"}), 400
    rota, erro = _rota_sem_app(route_id)
    if erro:
        return jsonify({"erro": erro}), 400
    ids_rota = {s.get("id") for s in __import__("mapa_util").extrair_servicos_da_rota(rota)}
    if any(int(i.get("service_id") or 0) not in ids_rota for i in itens):
        return jsonify({"erro": "pedido que não é dessa rota"}), 400
    conn = banco_nucleo.conectar()
    try:
        lote = bsa.criar_lote(conn, route_id, rota.get("agent_id"), session.get("usuario") or g.nivel_acesso, itens)
    finally:
        conn.close()
    token = (_carregar_config().get("vuupt_api") or {}).get("token", "")
    threading.Thread(target=bsa.executar_lote, args=(lote, VuuptClient(token)), daemon=True).start()
    return jsonify({"lote": lote})


@app.route("/api/baixa-sem-app/<int:lote>")
@requer_auth(niveis=("total", "operador"))
def api_baixa_sem_app_status(lote: int):
    from nucleo import baixa_sem_app as bsa
    conn = banco_nucleo.conectar()
    try:
        d = bsa.ler_lote(conn, lote)
    finally:
        conn.close()
    return (jsonify(d), 200) if d else (jsonify({"erro": "lote não encontrado"}), 404)
```

Ruling a confirmar na execução: o painel já importa o banco do núcleo com algum nome (procurar `from nucleo import banco` em `painel_agentes.py`); usar o nome existente no lugar de `banco_nucleo`, ou importar `from nucleo import banco as banco_nucleo` dentro das funções. O `mock.patch.object(banco, "DB_PATH", ...)` do teste vale porque `conectar()` lê `DB_PATH` na chamada. `rotas_client` e `mapa_util` estão em `roteirizacao/`, já no `sys.path` do painel (conferir com `grep -n "roteirizacao" painel_agentes.py`).

- [ ] **Step 4: Template** — criar `painel_agentes/templates/baixa_sem_app.html`:

```html
{% extends "base.html" %}
{% block titulo %}Baixa de rota sem app{% endblock %}
{% block estilo_extra %}
<style>
  main { max-width: 1100px; }
  h1 { font-size: 20px; margin: 0 0 6px; }
  .sub { color: var(--texto-suave); font-size: 13px; margin: 0 0 18px; }
  .tabela-wrap { overflow-x: auto; background: var(--superficie); border-radius: 10px; }
  table { width: 100%; border-collapse: collapse; min-width: 640px; }
  th, td { text-align: left; padding: 9px 12px; font-size: 13px; border-bottom: 1px solid var(--borda); vertical-align: middle; }
  th { color: var(--texto-suave); font-weight: 700; text-transform: uppercase; font-size: 11px; }
  tr.fechado td { color: var(--texto-suave); }
  select { padding: 6px 8px; border: 1px solid var(--borda); border-radius: 6px; font-size: 13px; }
  .acoes { margin: 16px 0; display: flex; gap: 10px; align-items: center; }
  .st-entregue { color: var(--sucesso, #16a34a); font-weight: 700; }
  .st-insucesso, .st-erro { color: var(--erro); font-weight: 700; }
</style>
{% endblock %}
{% block conteudo %}
<h1>Baixa de rota de motorista sem app</h1>
{% if erro %}<p style="color:var(--erro);">{{ erro }}</p>{% endif %}
{% if rota and not erro %}
<p class="sub">{{ rota.name }} · {{ (rota.start_at or '')[:10] }} · agent {{ rota.agent_id }}.
  Tudo marcado como entregue; desmarque o que voltou e escolha o motivo.</p>
<div class="tabela-wrap"><table>
  <thead><tr><th>Entregue</th><th>Pedido</th><th>Cliente</th><th>Motivo (se voltou)</th><th>Status</th></tr></thead>
  <tbody>
  {% for s in servicos %}
    <tr class="{{ 'fechado' if s.fechado }}" data-id="{{ s.id }}" data-codigo="{{ s.codigo }}">
      <td>{% if not s.fechado %}<input type="checkbox" class="entregue" checked>{% endif %}</td>
      <td class="mono">{{ s.codigo }}</td>
      <td>{{ s.titulo }}<div class="suave">{{ s.endereco }}</div></td>
      <td>{% if not s.fechado %}
        <select class="motivo" disabled><option value="">—</option>
          {% for k, t in motivos %}<option value="{{ k }}">{{ t }}</option>{% endfor %}
        </select>{% endif %}</td>
      <td class="status">{{ 'já fechado' if s.fechado else s.status }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table></div>
<div class="acoes">
  <button type="button" class="botao" id="confirmar">Confirmar baixa</button>
  <span id="andamento" class="suave"></span>
</div>
<script>
document.querySelectorAll('tr[data-id]').forEach(function (tr) {
  var cb = tr.querySelector('.entregue'), sel = tr.querySelector('.motivo');
  if (cb) cb.addEventListener('change', function () { sel.disabled = cb.checked; if (cb.checked) sel.value = ''; });
});
document.getElementById('confirmar').addEventListener('click', async function () {
  var btn = this, itens = [];
  document.querySelectorAll('tr[data-id]').forEach(function (tr) {
    var cb = tr.querySelector('.entregue'); if (!cb) return;
    var sel = tr.querySelector('.motivo');
    itens.push({service_id: +tr.dataset.id, codigo: tr.dataset.codigo, entregue: cb.checked,
                failed_reason_id: cb.checked ? null : (+sel.value || null)});
  });
  if (!itens.length) return;
  var voltou = itens.filter(function (i) { return !i.entregue; }).length;
  if (!confirm('Concluir ' + (itens.length - voltou) + ' como entregue e ' + voltou + ' como insucesso na Vuupt?')) return;
  btn.disabled = true;
  var r = await fetch('{{ url_for("api_baixa_sem_app") }}', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({rota: {{ rota.id }}, itens: itens})});
  var j = {}; try { j = await r.json(); } catch (e) {}
  if (!r.ok) { alert(j.erro || ('Falha (' + r.status + ')')); btn.disabled = false; return; }
  var url = '{{ url_for("api_baixa_sem_app_status", lote=0) }}'.replace(/0$/, j.lote);
  (async function acompanhar() {
    var st = await (await fetch(url)).json(), feitos = 0;
    st.itens.forEach(function (i) {
      var tr = document.querySelector('tr[data-id="' + i.service_id + '"] .status');
      if (tr) { tr.textContent = i.status + (i.erro ? ' (' + i.erro + ')' : ''); tr.className = 'status st-' + i.status; }
      if (i.status !== 'pendente') feitos++;
    });
    document.getElementById('andamento').textContent = feitos + ' de ' + st.itens.length + (st.terminado ? ' — concluído.' : '…');
    if (!st.terminado) setTimeout(acompanhar, 2000);
  })();
});
</script>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Ver passar** — `py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela` → OK; depois `py -3.11 -m unittest painel_agentes.test_batimento_tela painel_agentes.test_torre_batimento painel_agentes.test_inicio_tela` → OK.

---

### Task 6: Docs, conferência visual, coluna na VPS e prova

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (bloco do job das 15h45, telas do painel, `nucleo/`)

- [ ] **Step 1: Mapa.** No horário do `cancelar-rotas-sem-motorista`, acrescentar "(pula motorista SEM_APP; aguardando baixa vai no e-mail)". Nas telas: `/baixa-sem-app  baixa_sem_app.html (motorista sem app/iPhone: entregue ou insucesso com motivo; aberta pela Torre)`. Em `nucleo/`: `baixa_sem_app.py  Torre "aguardando baixa", lote baixas_sem_app, conclusão na Vuupt`.

- [ ] **Step 2: Todos os testes** (comandos separados):
  - `py -3.11 -m unittest regras.test_preferencias_motoristas roteirizacao.test_devolver_rotas_passadas`
  - `py -3.11 -m unittest nucleo.test_baixa_sem_app`
  - `py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela painel_agentes.test_batimento_tela painel_agentes.test_torre_batimento painel_agentes.test_inicio_tela`
  - `py -3.11 -m py_compile` em todos os `.py` editados.

- [ ] **Step 3: Playwright local** (porta 8099, sessão injetada como no teste do Fechamento; `rotas_client.buscar_rota` e `nucleo.baixa_sem_app.executar_lote` trocados por dublês no script do servidor de teste — nada toca a Vuupt): abrir `/baixa-sem-app?rota=5000`, desmarcar um pedido, escolher motivo, confirmar, ver o andamento; screenshot desktop e 390px.

- [ ] **Step 4: Commit (só com ok do Hugo)** — `git add` dos arquivos das Tasks 1–6 (não incluir `dados/BD_MOTORISTAS.xlsx`), mensagem `Motoristas sem app: rota nao volta ao pool e baixa pela Torre`.

- [ ] **Step 5: Deploy (só com ok do Hugo).**
  1. Coluna na VPS (antes do restart), como www-data, idempotente:
     ```
     sudo -u www-data venv/bin/python -c "
     import openpyxl; p='dados/BD_MOTORISTAS.xlsx'; wb=openpyxl.load_workbook(p); ws=wb.active
     h=[c.value for c in ws[1]]; ia=h.index('AGENT_ID_VUUPT')
     if 'SEM_APP' not in h: ws.cell(row=1, column=len(h)+1, value='SEM_APP'); h.append('SEM_APP')
     js=h.index('SEM_APP')+1; achados=[]
     for row in ws.iter_rows(min_row=2):
         if row[ia].value is not None and int(row[ia].value) in (50191,51469,50199):
             ws.cell(row=row[0].row, column=js, value='SIM'); achados.append(int(row[ia].value))
     wb.save(p); print('SEM_APP marcados:', sorted(achados))"
     ```
     (`wb.active`: conferir antes que a planilha tem uma aba só, ou trocar por `wb["Motoristas"]`.)
     Esperado: `[50191, 50199, 51469]`. Se faltar algum (ex.: Watson sem linha na planilha), parar e avisar o Hugo.
  2. `vps_ops.py deploy --restart painel-agentes` (sem `--units`: nenhum timer muda).
  3. Prova: `venv/bin/python -c "from regras.preferencias_motoristas import agent_ids_sem_app; print(agent_ids_sem_app())"`; `cancelar_rotas_sem_motorista.py --modo-teste` (lista sem devolver; conferir "aguardando baixa"; ATENÇÃO: esse modo teste manda o e-mail de execução `[MODO TESTE]` real pro hugo@ — avisar o Hugo antes); `test_client` em `/baixa-sem-app?rota=<rota real de motorista sem app, se houver>` com status 200; log do painel sem Traceback.
