# Entrega 1: pool do planejamento lê do núcleo — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** o pool do planejamento (tela, roteirizar selecionados, `criar_rotas_diarias`, `incrementar_rotas`) passa a poder ler de `nucleo_pedidos` em vez da API da Vuupt, escolhido por `planejamento.fonte_pool` no config, com comparador em sombra pra provar que os dois pools são iguais antes da virada.

**Architecture:** `nucleo/pool.py` devolve os pedidos ABERTO do núcleo no **mesmo formato do serviço da Vuupt** (`id`, `code`, `address`, `dimension_3`, `scheduled_*`, `customer{...}`), então nenhum consumidor muda de forma; uma função `listar_pool_not_assigned(config, vuupt)` escolhe a fonte. O espelho ganha três coisas: serviço com vários códigos, ressincronização imediata de um serviço depois de uma escrita na Vuupt, e uma função `executar()` reutilizável pelo botão "Atualizar" da tela. `nucleo/comparar_pool.py` compara os dois pools código a código, de hora em hora, e guarda o placar.

**Tech Stack:** Python 3.11 (`py -3.11`), SQLite (`dados/dados.db`, tabela `nucleo_pedidos`), Flask (painel), `unittest`, systemd (VPS).

**Spec:** `docs/superpowers/specs/2026-09-24-pool-pelo-nucleo-pedidos-portal-rascunho-14h-design.md` (seção "Entrega 1").

## Global Constraints

- Idioma: código, comentários, testes e commits em português. Nos arquivos de `nucleo/` os comentários usam acento (padrão do diretório).
- Python: sempre `py -3.11`. Testes: `py -3.11 -m unittest <modulo> -v` (sem pytest). Compilar tudo que editar: `py -3.11 -m py_compile <arquivo>`.
- **Sem commit por tarefa.** Commit e push só quando o Hugo pedir. Cada tarefa termina com testes verdes e `git status --short` conferido; quando ele pedir, `git add` só dos arquivos listados na tarefa, nunca `git add -A`.
- **Ramo:** o working tree está em `wms-fase2` com 28 arquivos de outra sessão modificados. Esta entrega é feita num worktree novo a partir de `origin/master` (ramo `pool-nucleo`), pra não misturar com o WMS. Confirmar com o Hugo no início da execução.
- `nucleo/pool.py` **não pode importar `vuupt_client`** (recebe a instância pronta): `nucleo/test_acoplamento_vuupt.py` reprova arquivo novo que chame a Vuupt. `nucleo/comparar_pool.py` chama a Vuupt de propósito e entra em `PERMITIDOS` com motivo.
- Chave de config: `planejamento.fonte_pool`, valores `vuupt` (padrão, chave ausente = vuupt) ou `nucleo`. `config.yaml` é gitignored: nunca commitar, nunca colar valores no chat.
- Comportamento com `fonte_pool: vuupt` tem que ficar **idêntico** ao de hoje (os testes existentes de `painel_agentes/` e `roteirizacao/` continuam verdes).
- Testes nunca tocam `dados/dados.db` real: `mock.patch.object(banco, "DB_PATH", <tmp>)` como em `nucleo/test_sincronizar_servicos.py`.

## Review Focus

1. Pedido com `scheduled_start` — o núcleo guarda em hora local e a Vuupt devolve UTC sem fuso; o pool tem que mostrar exatamente a mesma janela de hoje (teste de ida e volta, Tarefa 3).
2. Pedido que o pipeline gravou "pulado" (sem `vuupt_service_id`) não pode aparecer no pool, senão vira parada sem id no rascunho (Tarefa 3).
3. Retirada no galpão (`fluxo = RETIRADA`), pedido cancelado e excluído (`excluido_em`) ficam fora do pool (Tarefa 3).
4. Serviço com vários códigos (`"#PS-1, PS-2"`) hoje é rejeitado pelo espelho; tem que entrar e aparecer no pool com todos os códigos (Tarefas 1, 2 e 3).
5. Vuupt fora do ar no botão "Atualizar" com `fonte_pool: nucleo`: a sincronização falha, mas o pool do núcleo ainda carrega, com aviso (Tarefa 7).

---

### Task 1: `local_para_vuupt` e código composto em `nucleo/normalizacao.py`

**Files:**
- Modify: `nucleo/normalizacao.py:30-33` (`normalizar_codigo`) e acrescentar `local_para_vuupt` depois de `para_local` (`:63-74`)
- Test: `nucleo/test_pool.py` (novo; recebe também os testes das Tarefas 3)

**Interfaces:**
- Produces: `normalizar_codigo(codigo) -> str | None` passa a tratar lista separada por vírgula: `"#PS-1, #PS-2"` → `"PS-1, PS-2"`. Códigos simples inalterados.
- Produces: `local_para_vuupt(ts) -> str | None`: hora local `"YYYY-MM-DD HH:MM:SS"` (ou ISO com offset) → UTC sem fuso `"YYYY-MM-DD HH:MM:SS"`, o formato que a Vuupt devolve. Inverso exato de `vuupt_para_local`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `nucleo/test_pool.py`:

```python
# -*- coding: utf-8 -*-
"""
test_pool.py

Pool do planejamento lido do núcleo (Entrega 1 do spec
docs/superpowers/specs/2026-09-24-pool-pelo-nucleo-pedidos-portal-rascunho-14h-design.md).
Sem rede e sem tocar no dados.db real.

    py -3.11 -m unittest nucleo.test_pool -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo.normalizacao import local_para_vuupt, normalizar_codigo, vuupt_para_local


class TestNormalizacao(unittest.TestCase):
    def test_local_para_vuupt_e_o_inverso_de_vuupt_para_local(self):
        self.assertEqual(local_para_vuupt("2026-09-16 09:00:00"), "2026-09-16 12:00:00")
        self.assertEqual(vuupt_para_local(local_para_vuupt("2026-09-16 09:00:00")), "2026-09-16 09:00:00")
        self.assertEqual(local_para_vuupt("2026-09-16T09:00:00-03:00"), "2026-09-16 12:00:00")
        self.assertIsNone(local_para_vuupt(None))
        self.assertIsNone(local_para_vuupt(""))
        self.assertEqual(local_para_vuupt("não é data"), "não é data")

    def test_normalizar_codigo_composto_tira_o_cerquilha_de_cada_parte(self):
        self.assertEqual(normalizar_codigo("#PS-1, #PS-2"), "PS-1, PS-2")
        self.assertEqual(normalizar_codigo("#PS-1,PS-2"), "PS-1, PS-2")
        self.assertEqual(normalizar_codigo(" #ps-12345 "), "PS-12345")   # simples: igual a antes
        self.assertIsNone(normalizar_codigo(""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest nucleo.test_pool -v`
Expected: `ImportError: cannot import name 'local_para_vuupt'`.

- [ ] **Step 3: Implementar**

Em `nucleo/normalizacao.py`, trocar `normalizar_codigo`:

```python
def normalizar_codigo(codigo) -> str | None:
    """'#PS-12345' / ' ps-12345 ' -> 'PS-12345'. Vazio -> None.
    Serviço com mais de um pedido ('#PS-1, #PS-2', achado 20/08) vira
    'PS-1, PS-2': cada parte sem '#', separadas por ', '."""
    texto = str(codigo if codigo is not None else "").strip().upper()
    if "," in texto:
        partes = [p.strip().lstrip("#").strip() for p in texto.split(",")]
        texto = ", ".join(p for p in partes if p)
    else:
        texto = texto.lstrip("#").strip()
    return texto or None
```

E acrescentar depois de `para_local`:

```python
def local_para_vuupt(ts) -> str | None:
    """Inverso de vuupt_para_local: hora local do núcleo -> o formato que a
    API da Vuupt devolve (UTC sem fuso). Usado por nucleo/pool.py pra
    entregar o pedido no MESMO formato que o serviço da Vuupt, sem mudar o
    que as telas enxergam. Sem fuso = hora de SP; com offset, respeitado.
    Texto que não é data volta como veio."""
    if ts is None or str(ts).strip() == "":
        return None
    dt = _parse(ts)
    if dt is None:
        return str(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO_LOCAL)
    return dt.astimezone(timezone.utc).strftime(FORMATO)
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest nucleo.test_pool -v`
Expected: 2 testes OK.

Run: `py -3.11 -m unittest nucleo.test_espelho_fiel nucleo.test_sincronizar_servicos -v`
Expected: tudo OK (a normalização de código simples não mudou).

- [ ] **Step 5: Conferir**

Run: `py -3.11 -m py_compile nucleo/normalizacao.py nucleo/test_pool.py; git status --short`
Expected: só `nucleo/normalizacao.py` (M) e `nucleo/test_pool.py` (??) entre os seus.

---

### Task 2: espelho aceita serviço com vários códigos

**Files:**
- Modify: `nucleo/sincronizar_servicos_vuupt.py:64-69` (`_RE_CODIGO_PEDIDO`)
- Test: `nucleo/test_sincronizar_servicos.py` (acrescentar um teste na classe `TestPool`)

**Interfaces:**
- Produces: `sincronizar_servicos([...])` grava `"#PS-1, PS-2"` como a linha `codigo = "PS-1, PS-2"`.

- [ ] **Step 1: Escrever o teste que falha**

Em `nucleo/test_sincronizar_servicos.py`, dentro de `class TestPool(_Base)`, acrescentar:

```python
    def test_servico_com_varios_codigos_entra_como_uma_linha(self):
        """'#PS-1, PS-2' (dois pedidos no mesmo serviço, achado 20/08) era
        rejeitado pela regex e ficava fora do núcleo -- logo fora do pool."""
        stats = sinc.sincronizar_servicos([_servico(code="#PS-1, PS-2", service_id=7)], self.conn)
        self.assertEqual(stats["ignorados_sem_codigo"], 0)
        p = self._pedido("PS-1, PS-2")
        self.assertIsNotNone(p)
        self.assertEqual(p["vuupt_service_id"], 7)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest nucleo.test_sincronizar_servicos.TestPool -v`
Expected: `AssertionError: 1 != 0` no teste novo.

- [ ] **Step 3: Implementar**

Em `nucleo/sincronizar_servicos_vuupt.py`, trocar a regex:

```python
# Código de pedido de verdade: PS-12345, PS-12345-R1, PS-12345-C1, e o
# serviço que agrupa mais de um pedido ("PS-1, PS-2", já sem '#' -- ver
# normalizacao.normalizar_codigo). O que não casa (ex.: "COLETA QUATRO
# ESTRELAS", código fixo que se repete todo dia) NÃO vira linha em
# nucleo_pedidos -- viraria uma linha só, sobrescrita diariamente. Esses
# serviços seguem existindo como parada da rota em que entram.
_UM_CODIGO = r"[A-Z]{1,4}-?\d{2,}(?:-[A-Z0-9]+)*"
_RE_CODIGO_PEDIDO = re.compile(rf"^{_UM_CODIGO}(?:, {_UM_CODIGO})*$")
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest nucleo.test_sincronizar_servicos -v`
Expected: tudo OK, inclusive `test_codigo_que_nao_e_pedido_nao_vira_linha`.

- [ ] **Step 5: Conferir**

Run: `py -3.11 -m py_compile nucleo/sincronizar_servicos_vuupt.py nucleo/test_sincronizar_servicos.py`

---

### Task 3: `nucleo/pool.py` — o pool do núcleo no formato da Vuupt

**Files:**
- Create: `nucleo/pool.py`
- Test: `nucleo/test_pool.py` (acrescentar classes)

**Interfaces:**
- Produces: `fonte_pool(config: dict) -> str` (`"vuupt"` | `"nucleo"`, padrão `"vuupt"`).
- Produces: `pedido_para_servico(p: sqlite3.Row | dict) -> dict` (uma linha de `nucleo_pedidos` → dict no formato de `GET /services`, com `customer` achatado).
- Produces: `listar_pool(conn=None) -> list[dict]`: pedidos `ABERTO`, entrega, não excluídos, com `vuupt_service_id`, ordenados por código.
- Produces: `listar_em_rota(conn=None) -> list[dict]`: pedidos `EM_ROTA`, entrega, não excluídos (pro resumo de agendados).
- Produces: `listar_pool_not_assigned(config: dict, vuupt) -> list[dict]`: escolhe a fonte; com `vuupt`, chama `vuupt.listar_servicos([{"field": "status", "operator": "eq", "value": "not_assigned"}], per_page=100, include=["customer"])` — exatamente a chamada que os consumidores fazem hoje.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `nucleo/test_pool.py` (depois dos imports existentes):

```python
from nucleo import banco, pool, sincronizar_servicos_vuupt as sinc


def _servico(service_id=5001, code="#PS-100", status="not_assigned", **extra):
    """Formato real de GET /services (horários em UTC sem fuso), igual ao
    fixture de test_sincronizar_servicos."""
    base = {"id": service_id, "code": code, "title": f"{code} - 12 / BRAZO / CLIENTE", "status": status,
            "status_done": None, "address": "Rua A, 1 - Centro, São Paulo - SP, 01000-000",
            "address_complement": "sala 2", "latitude": -23.5, "longitude": -46.6, "sender_id": 11,
            "dimension_3": 4, "customer_id": 77, "route_id": None, "driver_id": None, "note": None,
            "deleted_at": None, "recreated_order_origin_id": None, "type": "delivery",
            "created_at": "2026-09-16 12:00:00", "updated_at": "2026-09-16 13:00:00",
            "scheduled_start": "2026-09-25 11:00:00", "scheduled_end": "2026-09-25 15:00:00",
            "customer": {"name": "Cliente A", "code": "12.345.678/0001-90", "phone_number": "+5511999990000",
                         "operating_hour_start": "08:00", "operating_hour_end": "17:00"}}
    base.update(extra)
    return base


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()


class TestListarPool(_Base):
    def test_pedido_aberto_volta_no_formato_da_vuupt(self):
        sinc.sincronizar_servicos([_servico()], self.conn)
        itens = pool.listar_pool(self.conn)
        self.assertEqual(len(itens), 1)
        s = itens[0]
        self.assertEqual(s["id"], 5001)
        self.assertEqual(s["code"], "#PS-100")
        self.assertEqual(s["address"], "Rua A, 1 - Centro, São Paulo - SP, 01000-000")
        self.assertEqual((s["latitude"], s["longitude"]), (-23.5, -46.6))
        self.assertEqual(s["dimension_3"], 4)
        self.assertEqual(s["sender_id"], 11)
        self.assertEqual(s["status"], "not_assigned")
        # o que a Vuupt devolveu (UTC sem fuso) é o que sai daqui de novo
        self.assertEqual(s["scheduled_start"], "2026-09-25 11:00:00")
        self.assertEqual(s["scheduled_end"], "2026-09-25 15:00:00")
        self.assertEqual(s["created_at"], "2026-09-16 12:00:00")
        self.assertEqual(s["customer"]["code"], "12.345.678/0001-90")
        self.assertEqual(s["customer"]["name"], "Cliente A")
        self.assertEqual(s["customer"]["operating_hour_start"], "08:00")
        self.assertEqual(s["customer_id"], 77)
        self.assertEqual(s["_fonte"], "nucleo")

    def test_sem_agendamento_volta_none(self):
        sinc.sincronizar_servicos([_servico(scheduled_start=None, scheduled_end=None)], self.conn)
        s = pool.listar_pool(self.conn)[0]
        self.assertIsNone(s["scheduled_start"])
        self.assertIsNone(s["scheduled_end"])

    def test_ficam_fora_retirada_cancelado_excluido_e_sem_service_id(self):
        sinc.sincronizar_servicos([
            _servico(),                                                         # entra
            _servico(service_id=2, code="#PS-200", status="assigned", driver_id=50259,
                     title="[RETIRADA] #PS-200 - 33 / BRAZO / CLIENTE / via X"),  # retirada: fora
            _servico(service_id=3, code="#PS-300", status="canceled"),          # cancelado: fora
            _servico(service_id=4, code="#PS-400", deleted_at="2026-09-16 14:00:00"),  # excluído: fora
            _servico(service_id=5, code="#PS-500", status="assigned", route_id=9),     # em rota: fora
        ], self.conn)
        # pedido "pulado" pelo pipeline: ABERTO sem vuupt_service_id
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, status, origem) VALUES ('PS-600', 'ABERTO', 'PIPELINE')")
        self.conn.commit()
        self.assertEqual([s["code"] for s in pool.listar_pool(self.conn)], ["#PS-100"])

    def test_varios_codigos_saem_com_cerquilha_na_frente(self):
        sinc.sincronizar_servicos([_servico(code="#PS-1, PS-2", service_id=7)], self.conn)
        self.assertEqual(pool.listar_pool(self.conn)[0]["code"], "#PS-1, PS-2")

    def test_ordem_por_codigo(self):
        sinc.sincronizar_servicos([_servico(service_id=2, code="#PS-200"), _servico(service_id=1, code="#PS-100")],
                                  self.conn)
        self.assertEqual([s["id"] for s in pool.listar_pool(self.conn)], [1, 2])

    def test_listar_em_rota_traz_so_em_rota(self):
        sinc.sincronizar_servicos([_servico(), _servico(service_id=5, code="#PS-500", status="on_route", route_id=9)],
                                  self.conn)
        self.assertEqual([s["code"] for s in pool.listar_em_rota(self.conn)], ["#PS-500"])


class TestFonte(unittest.TestCase):
    def test_fonte_padrao_e_vuupt(self):
        self.assertEqual(pool.fonte_pool({}), "vuupt")
        self.assertEqual(pool.fonte_pool({"planejamento": {}}), "vuupt")
        self.assertEqual(pool.fonte_pool({"planejamento": {"fonte_pool": "NUCLEO"}}), "nucleo")

    def test_listar_pool_not_assigned_escolhe_pela_chave(self):
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = [{"id": 1, "code": "#PS-1"}]
        self.assertEqual(pool.listar_pool_not_assigned({}, vuupt), [{"id": 1, "code": "#PS-1"}])
        vuupt.listar_servicos.assert_called_once_with(
            [{"field": "status", "operator": "eq", "value": "not_assigned"}], per_page=100, include=["customer"])
        with mock.patch.object(pool, "listar_pool", return_value=[{"id": 2}]) as lp:
            self.assertEqual(pool.listar_pool_not_assigned({"planejamento": {"fonte_pool": "nucleo"}}, vuupt), [{"id": 2}])
            lp.assert_called_once_with()
        vuupt.listar_servicos.assert_called_once()   # não chamou a Vuupt de novo
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest nucleo.test_pool -v`
Expected: `ImportError: cannot import name 'pool'` (ou `ModuleNotFoundError`).

- [ ] **Step 3: Implementar `nucleo/pool.py`**

```python
# -*- coding: utf-8 -*-
"""
nucleo/pool.py

O pool do planejamento lido do NÚCLEO (Entrega 1 do spec
docs/superpowers/specs/2026-09-24-pool-pelo-nucleo-pedidos-portal-rascunho-14h-design.md,
Etapa 4 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md).

Até aqui a Vuupt era dona do pool: planejamento_rotas.buscar_pool_e_agendados,
roteirizar_selecionados, criar_rotas_diarias.py e incrementar_rotas.py liam
`not_assigned` ao vivo. Aqui cada pedido ABERTO de nucleo_pedidos volta no
MESMO formato do serviço da Vuupt (GET /services com include=customer), então
_servico_para_pool, resolver_janela, regra_dia_fixo_do_servico,
extrair_volume_caixas e chegou_dentro_do_corte continuam lendo o que sempre
leram. Horários voltam como a Vuupt devolve (UTC sem fuso, ver
normalizacao.local_para_vuupt): esta entrega NÃO muda o que as telas mostram.

A fonte é escolhida por `planejamento.fonte_pool` no config.yaml
('vuupt' | 'nucleo'; ausente = vuupt). Voltar atrás é trocar a chave.

Este módulo NÃO importa vuupt_client de propósito (nucleo/test_acoplamento_vuupt):
quem precisa da Vuupt passa a instância pronta.
"""
import sqlite3

from nucleo import banco
from nucleo.normalizacao import local_para_vuupt

FONTE_VUUPT = "vuupt"
FONTE_NUCLEO = "nucleo"

_SQL_BASE = """SELECT * FROM nucleo_pedidos
               WHERE status = ? AND COALESCE(fluxo, ?) = ? AND excluido_em IS NULL"""


def fonte_pool(config: dict | None) -> str:
    valor = ((config or {}).get("planejamento") or {}).get("fonte_pool") or FONTE_VUUPT
    return str(valor).strip().lower()


def pedido_para_servico(p) -> dict:
    """Linha de nucleo_pedidos -> dict no formato do serviço da Vuupt. As
    chaves com '_' na frente são nossas (a tela usa '_fonte' só pra depurar)."""
    return {
        "id": p["vuupt_service_id"],
        "code": f"#{p['codigo']}",
        "title": p["titulo"],
        "type": p["tipo"],
        "status": p["status_provedor"] or "not_assigned",
        "status_done": p["status_done_provedor"],
        "address": p["endereco"],
        "address_complement": p["complemento"],
        "latitude": p["latitude"],
        "longitude": p["longitude"],
        "sender_id": p["sender_id"],
        "dimension_3": p["caixas"],
        "note": p["nota"],
        "customer_id": p["customer_id"],
        "route_id": p["vuupt_route_id"],
        "driver_id": p["driver_id"],
        "scheduled_start": local_para_vuupt(p["agendamento_inicio"]),
        "scheduled_end": local_para_vuupt(p["agendamento_fim"]),
        "created_at": local_para_vuupt(p["criado_em_provedor"] or p["criado_em"]),
        "updated_at": local_para_vuupt(p["atualizado_em_provedor"] or p["atualizado_em"]),
        "deleted_at": None,
        "recreated_order_origin_id": p["reentrega_de_service_id"],
        "customer": {
            "name": p["destinatario_nome"],
            "code": p["destinatario_codigo"],
            "phone_number": p["destinatario_telefone"],
            "operating_hour_start": p["horario_inicio"],
            "operating_hour_end": p["horario_fim"],
        },
        "_fonte": FONTE_NUCLEO,
        "_origem": p["origem"],
    }


def _listar(status: str, conn: sqlite3.Connection | None, so_com_service_id: bool) -> list[dict]:
    sql = _SQL_BASE + (" AND vuupt_service_id IS NOT NULL" if so_com_service_id else "") + " ORDER BY codigo"
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        return [pedido_para_servico(p) for p in conn.execute(sql, (status, banco.FLUXO_ENTREGA, banco.FLUXO_ENTREGA))]
    finally:
        if fechar:
            conn.close()


def listar_pool(conn: sqlite3.Connection | None = None) -> list[dict]:
    """O que hoje é `not_assigned` na Vuupt: pedido ABERTO, de entrega, não
    excluído, e COM serviço na Vuupt (pedido "pulado" pelo pipeline fica
    fora: sem id não vira parada de rascunho -- rascunhos_parada.service_id
    é NOT NULL até a Entrega 2)."""
    return _listar(banco.PEDIDO_ABERTO, conn, so_com_service_id=True)


def listar_em_rota(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Pedidos EM_ROTA (assigned/accepted/on_route na Vuupt) -- o resumo de
    agendados do planejamento soma esses ao pool."""
    return _listar(banco.PEDIDO_EM_ROTA, conn, so_com_service_id=False)


def listar_pool_not_assigned(config: dict | None, vuupt) -> list[dict]:
    """A troca de fonte, num lugar só. `vuupt` é um VuuptClient já criado
    por quem chama (só é usado com fonte 'vuupt'). A chamada à Vuupt é
    EXATAMENTE a que os consumidores faziam antes desta função existir."""
    if fonte_pool(config) == FONTE_NUCLEO:
        return listar_pool()
    filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
    return vuupt.listar_servicos(filtro, per_page=100, include=["customer"])
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest nucleo.test_pool -v`
Expected: todos OK.

- [ ] **Step 5: Teste de equivalência com `_servico_para_pool`**

Acrescentar em `nucleo/test_pool.py`:

```python
class TestEquivalenciaComATela(_Base):
    """O item do pool montado pela tela tem que sair IGUAL, venha o serviço
    da Vuupt ou do núcleo."""

    def test_servico_para_pool_da_o_mesmo_item(self):
        sys.path.insert(0, str(_RAIZ / "painel_agentes"))
        sys.path.insert(0, str(_RAIZ / "roteirizacao"))
        import planejamento_rotas

        original = _servico()
        sinc.sincronizar_servicos([original], self.conn)
        do_nucleo = pool.listar_pool(self.conn)[0]
        remetentes = {11: "BRAZO"}
        esperado = planejamento_rotas._servico_para_pool(original, remetentes)
        obtido = planejamento_rotas._servico_para_pool(do_nucleo, remetentes)
        self.assertEqual(obtido, esperado)
        self.assertEqual(obtido["janela_inicio"], esperado["janela_inicio"])
        self.assertEqual(obtido["agendado_para"], "2026-09-25")
```

Run: `py -3.11 -m unittest nucleo.test_pool.TestEquivalenciaComATela -v`
Expected: OK. Se falhar por import de `planejamento_rotas` (dependência ausente no ambiente local), anotar qual e corrigir o `sys.path`, não o teste.

- [ ] **Step 6: Conferir**

Run: `py -3.11 -m py_compile nucleo/pool.py nucleo/test_pool.py; py -3.11 -m unittest nucleo.test_acoplamento_vuupt -v`
Expected: `test_nenhum_arquivo_novo_chama_a_vuupt_direto` OK (pool.py não importa vuupt_client).

---

### Task 4: `ressincronizar_ids` e `executar()` em `nucleo/sincronizar_servicos_vuupt.py`

**Files:**
- Modify: `nucleo/sincronizar_servicos_vuupt.py` (acrescentar `ressincronizar_ids` depois de `reconciliar_pool` `:253-281`; extrair `executar` de `main` `:343-395`)
- Test: `nucleo/test_sincronizar_servicos.py` (classe nova)

**Interfaces:**
- Produces: `ressincronizar_ids(vuupt, service_ids, conn=None) -> int`: busca cada serviço por id (`_buscar_servico`) e aplica no espelho; 404 vira CANCELADO com evento `PEDIDO_SUMIU_DA_VUUPT`; falha de rede é ignorada com aviso. Devolve quantos foram aplicados. **Nunca levanta exceção** (é chamada depois de escritas na Vuupt que já deram certo).
- Produces: `executar(vuupt, conn, token, inicio=None, sem_pool=False, limite_sumidos=LIMITE_SUMIDOS, limite_rotas=20) -> dict`: o corpo do `main` (incremental + rotas tocadas + pool + cursor). `inicio=None` lê o cursor (ou 2 dias). Devolve `{"incremental": stats, "pool": stats_pool | None, "vinculo": ..., "abertos": n, "pool_vuupt": n}`.

- [ ] **Step 1: Escrever os testes que falham**

Em `nucleo/test_sincronizar_servicos.py`, acrescentar no fim (antes do `if __name__`):

```python
class _Resp:
    def __init__(self, status_code, corpo=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._corpo = corpo

    def json(self):
        return self._corpo


class _VuuptFalso:
    """Só o que _buscar_servico usa: session.get(url, timeout=...)."""
    def __init__(self, respostas: dict):
        self.respostas = respostas          # {service_id: _Resp | Exception}
        self.session = self

    def get(self, url, timeout=None):
        sid = int(url.rsplit("/", 1)[1])
        r = self.respostas[sid]
        if isinstance(r, Exception):
            raise r
        return r


class TestRessincronizarIds(_Base):
    def test_aplica_o_servico_que_voltou_e_cancela_o_404(self):
        sinc.sincronizar_servicos([_servico(service_id=1, code="#PS-1"), _servico(service_id=2, code="#PS-2")], self.conn)
        vuupt = _VuuptFalso({
            1: _Resp(200, {"service": _servico(service_id=1, code="#PS-1", status="assigned", route_id=9)}),
            2: _Resp(404),
        })
        n = sinc.ressincronizar_ids(vuupt, [1, 2, None], self.conn)
        self.assertEqual(n, 2)
        self.assertEqual(self._pedido("PS-1")["status"], "EM_ROTA")
        self.assertEqual(self._pedido("PS-2")["status"], "CANCELADO")
        self.assertIn("PEDIDO_SUMIU_DA_VUUPT", self._eventos())

    def test_falha_de_rede_nao_estoura_nem_muda_nada(self):
        sinc.sincronizar_servicos([_servico(service_id=1, code="#PS-1")], self.conn)
        vuupt = _VuuptFalso({1: ConnectionError("rede fora")})
        self.assertEqual(sinc.ressincronizar_ids(vuupt, [1], self.conn), 0)
        self.assertEqual(self._pedido("PS-1")["status"], "ABERTO")

    def test_lista_vazia_nao_abre_conexao(self):
        with mock.patch.object(banco, "conectar", side_effect=AssertionError("não devia conectar")):
            self.assertEqual(sinc.ressincronizar_ids(object(), [], None), 0)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest nucleo.test_sincronizar_servicos.TestRessincronizarIds -v`
Expected: `AttributeError: module ... has no attribute 'ressincronizar_ids'`.

- [ ] **Step 3: Implementar `ressincronizar_ids`**

Depois de `reconciliar_pool` em `nucleo/sincronizar_servicos_vuupt.py`:

```python
def ressincronizar_ids(vuupt, service_ids, conn: sqlite3.Connection | None = None) -> int:
    """Traz pro espelho, AGORA, os serviços que acabaram de ser escritos na
    Vuupt pela tela ou por um script (cancelar, reagendar, endereço, dia
    fixo, envio de rota). Sem isto o pool lido do núcleo (nucleo/pool.py)
    ficava até 15 min atrás da Vuupt, o intervalo do timer.

    Best-effort por contrato: a escrita na Vuupt já aconteceu, então falha
    aqui só pode virar aviso no log -- o timer alcança depois. 404 é a única
    resposta que autoriza CANCELADO (mesma regra de reconciliar_pool)."""
    ids = [int(i) for i in (service_ids or []) if i]
    if not ids:
        return 0
    fechar = conn is None
    try:
        conn = conn or banco.conectar()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ressincronizar {ids}: banco indisponível ({str(exc)[:120]})")
        return 0
    aplicados = 0
    try:
        for sid in ids:
            try:
                servico = _buscar_servico(vuupt, sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"ressincronizar {sid}: {str(exc)[:120]}")
                continue
            if servico is None:
                codigo = _codigo_por_service_id(conn, sid)
                if codigo:
                    conn.execute("""UPDATE nucleo_pedidos SET status = ?, excluido_em = COALESCE(excluido_em, ?),
                                           atualizado_em = ? WHERE codigo = ?""",
                                 (banco.PEDIDO_CANCELADO, banco.agora(), banco.agora(), codigo))
                    registrar_evento(conn, "PEDIDO_SUMIU_DA_VUUPT", banco.ORIGEM_VUUPT_SYNC,
                                     dados={"codigo": codigo, "service_id": sid})
                    aplicados += 1
                continue
            if not servico.get("code"):
                continue   # _buscar_servico devolve {"id": sid} em falha de rede/HTTP: nada a concluir
            sincronizar_servicos([servico], conn)
            aplicados += 1
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ressincronizar {ids}: {str(exc)[:160]}")
    finally:
        if fechar:
            conn.close()
    return aplicados
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest nucleo.test_sincronizar_servicos -v`
Expected: todos OK.

- [ ] **Step 5: Extrair `executar()` do `main`**

Substituir o bloco `try: ... finally: conn.close()` do `main` (`:343-395`) por:

```python
    conn = banco.conectar()
    try:
        agora_utc = datetime.now(timezone.utc)
        inicio = None
        if args.dias or args.desde:
            inicio = (args.desde if args.desde else
                      (agora_utc - timedelta(days=args.dias)).strftime("%Y-%m-%d %H:%M:%S"))
        if args.modo_teste:
            inicio = inicio or ler_cursor(conn) or (agora_utc - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            servicos = vuupt.listar_servicos([{"field": "updated_at", "operator": "gte", "value": inicio}],
                                             include=["customer", "sender", "zone", "checklistAnswers", "attachments"])
            por_status = {}
            for s in servicos:
                por_status[s.get("status")] = por_status.get(s.get("status"), 0) + 1
            logger.info(f"[TESTE] {len(servicos)} serviço(s) desde {inicio} (UTC); por status: {por_status}")
            pool = vuupt.listar_servicos([{"field": "status", "operator": "eq", "value": "not_assigned"}])
            logger.info(f"[TESTE] pool na VUUPT: {len(pool)} serviço(s); nada foi gravado.")
            return 0
        resultado = executar(vuupt, conn, config["vuupt_api"]["token"], inicio=inicio, sem_pool=args.sem_pool,
                             limite_sumidos=args.limite_sumidos, limite_rotas=args.limite_rotas)
        logger.info(f"Resultado: {resultado}")
    finally:
        conn.close()
    return 0
```

E acrescentar, antes de `main`:

```python
def executar(vuupt, conn: sqlite3.Connection, token: str, inicio: str | None = None, sem_pool: bool = False,
             limite_sumidos: int = LIMITE_SUMIDOS, limite_rotas: int = 20) -> dict:
    """Uma rodada completa (o que o timer roda a cada 15 min), reutilizável
    pelo botão "Atualizar" do planejamento com fonte_pool=nucleo. `inicio`
    é 'YYYY-MM-DD HH:MM:SS' em UTC; None lê o cursor (ou 2 dias atrás)."""
    agora_utc = datetime.now(timezone.utc)
    if not inicio:
        cursor = ler_cursor(conn)
        inicio = cursor if cursor else (agora_utc - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Serviços alterados desde {inicio} (UTC).")
    servicos = vuupt.listar_servicos([{"field": "updated_at", "operator": "gte", "value": inicio}],
                                     include=["customer", "sender", "zone", "checklistAnswers", "attachments"])
    logger.info(f"{len(servicos)} serviço(s) alterado(s).")
    stats = sincronizar_servicos(servicos, conn)
    logger.info(f"Incremental: {stats}")
    resultado = {"incremental": stats, "pool": None, "vinculo": None, "abertos": None, "pool_vuupt": None}

    alvo = rotas_a_ressincronizar(servicos, conn)[:limite_rotas]
    if alvo:
        from nucleo.sincronizar_vuupt import reconciliar_rotas_sumidas
        # `vistas=set()` de propósito: aqui a gente QUER buscar cada uma
        # dessas rotas por id, mesmo que a listagem do dia não as traga.
        res_rotas = reconciliar_rotas_sumidas(conn, token, [date.today()], set(), nomes=None, ids=alvo)
        logger.info(f"Rotas tocadas por serviço que mudou: {len(alvo)} -> {res_rotas}")
    if not sem_pool:
        pool = vuupt.listar_servicos([{"field": "status", "operator": "eq", "value": "not_assigned"}],
                                     include=["customer", "sender", "zone", "checklistAnswers", "attachments"])
        stats_pool = reconciliar_pool(pool, conn, lambda sid: _buscar_servico(vuupt, sid), limite=limite_sumidos)
        # O pool inteiro também entra no espelho (pedido que nunca passou
        # pelo pipeline com dual-write, ex.: criado na tela da VUUPT).
        stats_pool_upsert = sincronizar_servicos(pool, conn)
        stats_vinculo = vincular_sem_service_id(conn, vuupt.buscar_servico_por_code)
        logger.info(f"Pool: {stats_pool} | upsert: {stats_pool_upsert} | vínculo: {stats_vinculo}")
        abertos = conn.execute("SELECT COUNT(*) FROM nucleo_pedidos WHERE status = ?",
                               (banco.PEDIDO_ABERTO,)).fetchone()[0]
        if abertos != len(pool):
            logger.warning(f"Pool do núcleo ({abertos}) != pool da VUUPT ({len(pool)}) -- "
                           f"confira com nucleo/comparar_pool.py.")
        resultado.update({"pool": stats_pool, "vinculo": stats_vinculo, "abertos": abertos, "pool_vuupt": len(pool)})
    # Cursor com margem: perder mudança é pior do que reprocessar.
    gravar_cursor(conn, (agora_utc - timedelta(minutes=MARGEM_MIN)).strftime("%Y-%m-%d %H:%M:%S"), stats)
    return resultado
```

- [ ] **Step 6: Teste do `executar` com Vuupt falsa**

Acrescentar em `nucleo/test_sincronizar_servicos.py`:

```python
class TestExecutar(_Base):
    def test_executar_roda_incremental_e_pool_e_grava_cursor(self):
        vuupt = mock.Mock()
        vuupt.listar_servicos.side_effect = [
            [_servico(service_id=1, code="#PS-1")],          # incremental
            [_servico(service_id=1, code="#PS-1")],          # pool
        ]
        vuupt.buscar_servico_por_code.return_value = None
        resultado = sinc.executar(vuupt, self.conn, token="t", inicio="2026-09-16 00:00:00")
        self.assertEqual(resultado["incremental"]["novos"], 1)
        self.assertEqual(resultado["pool_vuupt"], 1)
        self.assertEqual(resultado["abertos"], 1)
        self.assertIsNotNone(sinc.ler_cursor(self.conn))

    def test_executar_sem_pool_so_faz_o_incremental(self):
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = []
        resultado = sinc.executar(vuupt, self.conn, token="t", inicio="2026-09-16 00:00:00", sem_pool=True)
        self.assertIsNone(resultado["pool"])
        vuupt.listar_servicos.assert_called_once()
```

Run: `py -3.11 -m unittest nucleo.test_sincronizar_servicos -v`
Expected: todos OK.

- [ ] **Step 7: Conferir**

Run: `py -3.11 -m py_compile nucleo/sincronizar_servicos_vuupt.py; py -3.11 nucleo/sincronizar_servicos_vuupt.py --modo-teste`
Expected: compila; o `--modo-teste` local só lê a Vuupt (token do `config.yaml` local) e mostra contagens, sem gravar. Se não houver token local, a saída é `vuupt_api.token ausente` com código 1, o que também está certo.

---

### Task 5: consumidores passam por `listar_pool_not_assigned`

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py:37-74` (imports), `:687-697` e `:756-765` (`buscar_pool_e_agendados`), `:981-989` (`roteirizar_selecionados`)
- Modify: `roteirizacao/criar_rotas_diarias.py:645-646`
- Modify: `roteirizacao/incrementar_rotas.py:388-392`
- Test: os existentes (`painel_agentes/test_*.py`, `roteirizacao/test_*.py`) continuam verdes; smoke manual com `fonte_pool: nucleo`.

**Interfaces:**
- Consumes: `nucleo.pool.listar_pool_not_assigned(config, vuupt)`, `fonte_pool(config)`, `listar_em_rota()`.

- [ ] **Step 1: `planejamento_rotas.py`**

No bloco de imports (depois de `import rascunhos_rota`, `:74`):

```python
from nucleo.pool import fonte_pool, listar_em_rota, listar_pool_not_assigned
```

Em `buscar_pool_e_agendados`, trocar `:695-697`:

```python
    vuupt = VuuptClient(token)
    # Fonte do pool (Hugo, 24/09): Vuupt ao vivo OU o núcleo, pela chave
    # planejamento.fonte_pool -- ver nucleo/pool.py. Mesmo formato de item.
    servicos_brutos = listar_pool_not_assigned(config, vuupt)
```

E trocar `:759-765` (o bloco `servicos_resumo`):

```python
    servicos_resumo = list(servicos_brutos)
    if fonte_pool(config) == "nucleo":
        servicos_resumo += listar_em_rota()
    else:
        for status_rota in ("accepted", "on_route"):
            try:
                servicos_resumo += vuupt.listar_servicos(
                    [{"field": "status", "operator": "eq", "value": status_rota}], per_page=100)
            except Exception as e:
                logger.warning(f"Falha ao buscar serviços '{status_rota}' pro resumo de agendados: {e}")
```

Em `roteirizar_selecionados`, trocar `:987-989`:

```python
    vuupt = VuuptClient(token)
    servicos_brutos = listar_pool_not_assigned(config, vuupt)
```

- [ ] **Step 2: `criar_rotas_diarias.py`**

Acrescentar o import junto de `from rascunhos_rota import criar_lote_rascunhos` (`:101`):

```python
from nucleo.pool import listar_pool_not_assigned
```

Trocar `:645-646`:

```python
        # Fonte do pool (Hugo, 24/09): Vuupt ao vivo OU o núcleo, pela chave
        # planejamento.fonte_pool -- ver nucleo/pool.py.
        servicos_brutos = listar_pool_not_assigned(config, vuupt)
```

- [ ] **Step 3: `incrementar_rotas.py`**

Acrescentar o import junto de `from criar_rotas_diarias import (` (`:107`):

```python
from nucleo.pool import listar_pool_not_assigned
```

Trocar `:388-392`:

```python
        # Fonte do pool (Hugo, 24/09): Vuupt ao vivo OU o núcleo, pela chave
        # planejamento.fonte_pool -- ver nucleo/pool.py. include=customer
        # continua vindo (CNPJ do destinatário resolve nível e horário).
        servicos = listar_pool_not_assigned(config, vuupt)
```

- [ ] **Step 4: Compilar e rodar os testes existentes**

Run: `py -3.11 -m py_compile painel_agentes/planejamento_rotas.py roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py`

Run: `py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" -v 2>&1 | tail -n 5`
Expected: OK (mesmo número de testes de antes).

Run: `py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" -v 2>&1 | tail -n 5`
Expected: OK.

- [ ] **Step 5: Smoke local com a fonte trocada (sem tocar no config real)**

Run:
```
py -3.11 -c "import sys; sys.path[:0]=['painel_agentes','roteirizacao','.']; from unittest import mock; import planejamento_rotas as pr; cfg=pr._carregar_config(); cfg.setdefault('planejamento',{})['fonte_pool']='nucleo'; from datetime import date; r=pr.buscar_pool_e_agendados(date.today(), cfg); print(len(r['pool']), 'no pool do nucleo;', len(r['resumo_agendados']), 'dias no resumo')"
```
Expected: imprime contagens sem exceção (o banco local congelado pode ter poucos ABERTO; o que importa é não quebrar). O `VuuptClient` ainda é instanciado mas só é chamado pras retiradas.

---

### Task 6: ressincronização imediata depois de escrita na Vuupt

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py` (`cancelar_pedido` `:1551-1594`, `reagendar_pedido` `:1597-1631`, `reagendar_pedidos` `:1650-1693`, `_gravar_endereco_pedido` `:1696-1757`)
- Modify: `painel_agentes/rascunhos_rota.py` (`enviar_rascunho`, logo depois de `marcar_alocado` ~`:1292`)
- Modify: `roteirizacao/criar_rotas_diarias.py:654-664` e `roteirizacao/incrementar_rotas.py` (o bloco equivalente de `aplicar_regioes_dia_fixo`)
- Test: `painel_agentes/test_ressincronizar_pool.py` (novo)

**Interfaces:**
- Consumes: `nucleo.sincronizar_servicos_vuupt.ressincronizar_ids(vuupt, ids)`.
- Produces: `planejamento_rotas._ressincronizar(vuupt, ids)` (wrapper fino, patchável nos testes).

- [ ] **Step 1: Escrever os testes que falham**

Criar `painel_agentes/test_ressincronizar_pool.py`:

```python
# -*- coding: utf-8 -*-
"""
test_ressincronizar_pool.py

Depois de cancelar / reagendar / editar endereço na Vuupt pela tela, o
espelho do pedido é atualizado NA HORA (Entrega 1, Hugo 24/09) -- sem isso
o pool lido do núcleo ficava até 15 min atrasado.

    py -3.11 -m unittest painel_agentes.test_ressincronizar_pool -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas as pr  # noqa: E402


class TestRessincronizaDepoisDeEscrever(unittest.TestCase):
    def setUp(self):
        self.mock_vuupt = mock.Mock()
        self.mock_vuupt.buscar_servico_por_id.return_value = {"id": 111, "customer_id": None}
        p1 = mock.patch.object(pr, "VuuptClient", return_value=self.mock_vuupt)
        p2 = mock.patch.object(pr, "_carregar_config", return_value={"vuupt_api": {"token": "t"}, "google_maps": {}})
        p3 = mock.patch.object(pr, "_ressincronizar")
        p4 = mock.patch.object(pr.rascunhos_rota, "buscar_rascunho", return_value=None)
        self.ressinc = p3.start()
        for p in (p1, p2, p4):
            p.start()
        self.addCleanup(mock.patch.stopall)

    def test_cancelar_pedido(self):
        self.assertEqual(pr.cancelar_pedido(111), {"ok": True})
        self.ressinc.assert_called_once_with(self.mock_vuupt, [111])

    def test_reagendar_pedido(self):
        self.assertEqual(pr.reagendar_pedido(111, "2026-09-25", "08:00", "12:00"), {"ok": True})
        self.ressinc.assert_called_once_with(self.mock_vuupt, [111])

    def test_reagendar_pedidos_em_lote_ressincroniza_so_os_que_deram_certo(self):
        self.mock_vuupt.atualizar_servico.side_effect = [None, Exception("falhou"), None]
        r = pr.reagendar_pedidos([{"service_id": 1}, {"service_id": 2}, {"service_id": 3}], "2026-09-25", "08:00", "12:00")
        self.assertEqual([f["service_id"] for f in r["falhas"]], [2])
        self.ressinc.assert_called_once_with(self.mock_vuupt, [1, 3])

    def test_editar_endereco(self):
        with mock.patch("geocodificacao.geocodificar", return_value=None):
            self.assertEqual(pr.editar_endereco_pedido(111, "Rua Nova, 1"), {"ok": True})
        self.ressinc.assert_called_once_with(self.mock_vuupt, [111])

    def test_falha_na_vuupt_nao_ressincroniza(self):
        self.mock_vuupt.cancelar_servico.side_effect = pr.VuuptAPIError("500")
        self.assertFalse(pr.cancelar_pedido(111)["ok"])
        self.ressinc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_ressincronizar_pool -v`
Expected: `AttributeError: <module 'planejamento_rotas'> does not have the attribute '_ressincronizar'`.

- [ ] **Step 3: Implementar em `planejamento_rotas.py`**

Depois de `_carregar_config` (`:244-246`):

```python
def _ressincronizar(vuupt: VuuptClient, service_ids: list[int]) -> None:
    """Depois de uma escrita na Vuupt feita por esta tela, traz o serviço pro
    espelho na hora (Hugo, 24/09: pool lido do núcleo, ver nucleo/pool.py).
    Best-effort: falha vira aviso, o timer de 15 min alcança depois."""
    try:
        from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids
        ressincronizar_ids(vuupt, service_ids)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Ressincronização do núcleo falhou pra {service_ids}: {e}")
```

Em `cancelar_pedido`, trocar `:1586-1589`:

```python
    vuupt = VuuptClient(token)
    try:
        vuupt.cancelar_servico(service_id)
    except VuuptAPIError as e:
        return {"ok": False, "erro": str(e)}
    _ressincronizar(vuupt, [service_id])
```

Em `reagendar_pedido`, trocar `:1623-1629`:

```python
    vuupt = VuuptClient(token)
    try:
        vuupt.atualizar_servico(service_id, {
            "scheduled_start": scheduled_start,
            "scheduled_end": scheduled_end,
        })
    except VuuptAPIError as e:
        return {"ok": False, "erro": str(e)}
    _ressincronizar(vuupt, [service_id])
```

Em `reagendar_pedidos`, dentro do laço, guardar os que deram certo e ressincronizar no fim:

```python
    falhas = []
    ok_ids = []
    for item in itens:
        service_id = int(item["service_id"])
        try:
            vuupt.atualizar_servico(service_id, {
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
            })
            logger.info(f"Agendamento em lote: serviço {service_id} atualizado.")
            ok_ids.append(service_id)
        except Exception as e:
            # (comentário existente mantido)
            logger.warning(f"Agendamento em lote: falha no serviço {service_id}: {e}")
            falhas.append({"service_id": service_id, "erro": str(e)})

    if ok_ids:
        _ressincronizar(vuupt, ok_ids)
    return {"ok": True, "falhas": falhas}
```

Em `_gravar_endereco_pedido`, no fim da função (depois do bloco `if rascunho_id is not None:`), acrescentar:

```python
    _ressincronizar(vuupt, [service_id])
```

(`editar_endereco_pedido`, `editar_endereco_pedidos` e `editar_transportadora_pedidos` passam todos por `_gravar_endereco_pedido`, então ganham a ressincronização de uma vez.)

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest painel_agentes.test_ressincronizar_pool painel_agentes.test_editar_endereco_pedido painel_agentes.test_editar_endereco_pedidos_lote painel_agentes.test_reagendar_pedidos_lote painel_agentes.test_editar_transportadora_pedido -v`
Expected: todos OK. Os testes antigos usam `VuuptClient` dublê: `_buscar_servico` recebe um Mock como resposta, não acha `code` e não grava nada — confirmado pelo teste `test_falha_de_rede_nao_estoura_nem_muda_nada` da Tarefa 4.

- [ ] **Step 5: `enviar_rascunho` (rascunhos_rota.py)**

Em `enviar_rascunho` (`rascunhos_rota.py:1234`), o laço `:1291-1292` é:

```python
    for s in sublote_criado:
        marcar_alocado(s["id"], rota["id"])
```

Logo depois dele (antes de `marcar_enviado(rascunho_id, rota["id"])`, `:1294`), acrescentar:

```python
    # O pool lido do núcleo (nucleo/pool.py) precisa ver esses pedidos
    # como EM_ROTA já -- senão, quando o lote ativo trocar, eles voltariam
    # a aparecer no pool por até 15 min (Hugo, 24/09). Best-effort.
    try:
        from vuupt_client import VuuptClient
        from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids
        ressincronizar_ids(VuuptClient(token), [s["id"] for s in sublote_criado])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Ressincronização do núcleo após envio do rascunho {rascunho_id} falhou: {e}")
```

(`rascunhos_rota.py` não importa `VuuptClient` no topo; o import local fica dentro do `try`.)

- [ ] **Step 6: dia fixo nos dois scripts**

Em `roteirizacao/criar_rotas_diarias.py`, dentro do `try` de `aplicar_regioes_dia_fixo` (`:654-664`), logo depois de `agendados_dia_fixo = aplicar_regioes_dia_fixo(servicos_brutos, vuupt)`:

```python
            if agendados_dia_fixo:
                # scheduled_* acabou de ser gravado na Vuupt; o espelho
                # precisa disso já, senão a próxima rodada (pool do núcleo)
                # agendaria e AVISARIA o remetente de novo (Hugo, 24/09).
                from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids
                ressincronizar_ids(vuupt, [a["servico"]["id"] for a in agendados_dia_fixo])
```

Em `roteirizacao/incrementar_rotas.py:450` a chamada é `agendados_dia_fixo = aplicar_regioes_dia_fixo(servicos, vuupt)` (mesmos nomes). Acrescentar logo depois dela, dentro do mesmo `try`:

```python
            if agendados_dia_fixo:
                from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids
                ressincronizar_ids(vuupt, [a["servico"]["id"] for a in agendados_dia_fixo])
```

- [ ] **Step 7: Compilar e rodar tudo**

Run: `py -3.11 -m py_compile painel_agentes/planejamento_rotas.py painel_agentes/rascunhos_rota.py roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py`

Run: `py -3.11 -m unittest discover -s painel_agentes -p "test_*.py" 2>&1 | tail -n 3; py -3.11 -m unittest discover -s roteirizacao -p "test_*.py" 2>&1 | tail -n 3; py -3.11 -m unittest nucleo.test_acoplamento_vuupt`
Expected: OK, OK, OK (`rascunhos_rota.py` e os dois scripts já estão em `PERMITIDOS`).

---

### Task 7: botão "Atualizar" sincroniza o núcleo antes de ler o pool

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py` (função nova `sincronizar_pool_agora`)
- Modify: `painel_agentes/painel_agentes.py:1169-1187` (`api_pool`)
- Modify: `painel_agentes/templates/planejamento_rotas.html:5736` (`atualizarPool`)
- Test: `painel_agentes/test_ressincronizar_pool.py` (classe nova)

**Interfaces:**
- Produces: `planejamento_rotas.sincronizar_pool_agora(config=None) -> dict`: com `fonte_pool != nucleo` devolve `{"pulado": True}`; senão roda `sincronizar_servicos_vuupt.executar(...)` e devolve o resultado. **Levanta** exceção se a Vuupt falhar (quem chama decide).
- Produces: `GET /api/planejamento/pool?data=...&sincronizar=1` — devolve o JSON de hoje mais `"aviso"` (string) quando a sincronização falhou.

- [ ] **Step 1: Escrever o teste que falha**

Acrescentar em `painel_agentes/test_ressincronizar_pool.py`:

```python
class TestSincronizarPoolAgora(unittest.TestCase):
    def test_pulado_quando_a_fonte_e_vuupt(self):
        self.assertEqual(pr.sincronizar_pool_agora({"planejamento": {"fonte_pool": "vuupt"}}), {"pulado": True})

    def test_roda_o_executar_quando_a_fonte_e_nucleo(self):
        cfg = {"planejamento": {"fonte_pool": "nucleo"}, "vuupt_api": {"token": "t"}}
        conn = mock.Mock()
        with mock.patch.object(pr, "VuuptClient") as vc, \
             mock.patch("nucleo.banco.conectar", return_value=conn), \
             mock.patch("nucleo.sincronizar_servicos_vuupt.executar", return_value={"abertos": 3}) as ex:
            self.assertEqual(pr.sincronizar_pool_agora(cfg), {"abertos": 3})
            ex.assert_called_once_with(vc.return_value, conn, "t")
        conn.close.assert_called_once()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_ressincronizar_pool.TestSincronizarPoolAgora -v`
Expected: `AttributeError: ... 'sincronizar_pool_agora'`.

- [ ] **Step 3: Implementar**

Em `planejamento_rotas.py`, depois de `_ressincronizar`:

```python
def sincronizar_pool_agora(config: dict | None = None) -> dict:
    """Botão "Atualizar" do pool com fonte_pool=nucleo (Hugo, 24/09): roda a
    mesma rodada do timer de 15 min (incremental + pool) antes de ler o
    núcleo, pra quem mexeu direto no site da Vuupt não esperar o timer.
    Com fonte vuupt não faz nada. Exceção sobe pra quem chama."""
    config = config or _carregar_config()
    if fonte_pool(config) != "nucleo":
        return {"pulado": True}
    from nucleo import banco
    from nucleo.sincronizar_servicos_vuupt import executar
    token = config.get("vuupt_api", {}).get("token", "")
    conn = banco.conectar()
    try:
        return executar(VuuptClient(token), conn, token)
    finally:
        conn.close()
```

Em `painel_agentes.py`, `api_pool`:

```python
    data_alvo = _parse_data_param()
    aviso = None
    if request.args.get("sincronizar") == "1":
        try:
            sincronizar_pool_agora()
        except Exception as e:
            logging.getLogger(__name__).warning(f"Sincronização do pool antes de atualizar falhou: {e}")
            aviso = f"Não consegui sincronizar com a Vuupt agora ({e}); mostrando o pool do núcleo como está."
    try:
        resultado = buscar_pool_e_agendados(data_alvo)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify({
        "pool": resultado["pool"],
        "pool_retiradas": resultado.get("pool_retiradas", []),
        "resumo_html": render_template("_resumo_agendados.html", resumo_agendados=resultado["resumo_agendados"]),
        "aviso": aviso,
    })
```

E acrescentar `sincronizar_pool_agora` no bloco `from planejamento_rotas import (` de `painel_agentes.py:63-64`, na mesma linha de `buscar_pool_e_agendados`.

Em `planejamento_rotas.html`, `atualizarPool` (`:5736`):

```javascript
      botao.textContent = "Sincronizando...";
      const resp = await fetch(`${BASE_PATH}/api/planejamento/pool?data=${encodeURIComponent(DATA_ALVO_ISO)}&sincronizar=1`);
      const dados = await resp.json();
      if (!resp.ok) throw new Error(dados.erro || "Falha ao buscar o pool.");
      if (dados.aviso) alert(dados.aviso);
```

(A linha `botao.textContent = "Buscando...";` de antes do `fetch` é substituída pela de "Sincronizando...".)

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest painel_agentes.test_ressincronizar_pool -v`
Expected: todos OK.

Run: `py -3.11 -m py_compile painel_agentes/planejamento_rotas.py painel_agentes/painel_agentes.py`

- [ ] **Step 5: Smoke do endpoint noutra porta**

Run (de dentro de `painel_agentes/`, em segundo plano): `py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"`

Run: `curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8099/api/planejamento/pool?data=2026-09-25&sincronizar=1"`
Expected: `200` (ou `401`/redirect se a rota exige login: aí conferir pelo navegador logado em `http://127.0.0.1:8099/planejamento`, clicar "Atualizar" e ver "Sincronizando..." → contagem). Com `fonte_pool` ausente no config local, a sincronização é pulada e o comportamento é o de hoje. Encerrar o processo da porta 8099 ao terminar (só ele).

---

### Task 8: comparador em sombra `nucleo/comparar_pool.py` + timer

**Files:**
- Create: `nucleo/comparar_pool.py`
- Create: `infra/stokki-comparar-pool.service`, `infra/stokki-comparar-pool.timer`
- Modify: `nucleo/test_acoplamento_vuupt.py:37-93` (`PERMITIDOS`)
- Test: `nucleo/test_comparar_pool.py` (novo)

**Interfaces:**
- Produces: `projetar(servico: dict) -> dict` (os campos comparados), `comparar(pool_vuupt: list[dict], pool_nucleo: list[dict]) -> dict` com `{"total_vuupt", "total_nucleo", "so_na_vuupt": [codes], "so_no_nucleo": [codes], "diferentes": [{"code", "campos": {campo: [vuupt, nucleo]}}], "total_divergencias"}`.
- Produces: tabela `nucleo_reconciliacoes_pool`, `salvar(resultado, conn)`, `historico(conn, limite)`, `dias_limpos_seguidos(conn) -> int`.
- CLI: `comparar_pool.py [--salvar] [--email] [--exemplos N] [--json] [--db CAMINHO] [--historico N]`. Saída 0 sem divergência, 2 com.

- [ ] **Step 1: Escrever os testes que falham**

Criar `nucleo/test_comparar_pool.py`:

```python
# -*- coding: utf-8 -*-
"""
test_comparar_pool.py -- comparador do pool Vuupt × núcleo (sombra da Entrega 1).

    py -3.11 -m unittest nucleo.test_comparar_pool -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, comparar_pool as cp


def _s(code="#PS-1", **extra):
    base = {"id": 1, "code": code, "address": "Rua A, 1 - Sorocaba - SP", "latitude": -23.5, "longitude": -46.6,
            "dimension_3": 4, "sender_id": 11, "scheduled_start": "2026-09-25 11:00:00",
            "scheduled_end": "2026-09-25 15:00:00",
            "customer": {"code": "12.345.678/0001-90", "operating_hour_start": "08:00", "operating_hour_end": "17:00"}}
    base.update(extra)
    return base


class TestComparar(unittest.TestCase):
    def test_iguais_nao_dao_divergencia(self):
        r = cp.comparar([_s()], [_s()])
        self.assertEqual(r["total_divergencias"], 0)
        self.assertEqual((r["total_vuupt"], r["total_nucleo"]), (1, 1))

    def test_codigo_com_e_sem_cerquilha_e_o_mesmo_pedido(self):
        self.assertEqual(cp.comparar([_s(code="#PS-1")], [_s(code="PS-1")])["total_divergencias"], 0)

    def test_so_de_um_lado(self):
        r = cp.comparar([_s(code="#PS-1"), _s(code="#PS-2", id=2)], [_s(code="#PS-1"), _s(code="#PS-3", id=3)])
        self.assertEqual(r["so_na_vuupt"], ["PS-2"])
        self.assertEqual(r["so_no_nucleo"], ["PS-3"])
        self.assertEqual(r["total_divergencias"], 2)

    def test_campo_diferente_aponta_o_campo(self):
        r = cp.comparar([_s()], [_s(dimension_3=9, latitude=-23.50000001)])
        self.assertEqual(len(r["diferentes"]), 1)
        self.assertEqual(list(r["diferentes"][0]["campos"]), ["dimension_3"])   # lat arredondada a 5 casas: igual
        self.assertEqual(r["diferentes"][0]["campos"]["dimension_3"], [4, 9])

    def test_janela_e_dia_fixo_entram_na_projecao(self):
        p = cp.projetar(_s())
        self.assertEqual(p["janela"], ("11:00", "15:00"))
        self.assertEqual(p["agendado_para"], "2026-09-25")
        self.assertEqual(p["dia_fixo"], "Sorocaba")


class TestPlacar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def test_dias_limpos_seguidos_conta_por_dia_e_para_na_primeira_divergencia(self):
        limpo = cp.comparar([_s()], [_s()])
        sujo = cp.comparar([_s()], [])
        for rodado_em, r in (("2026-09-20 08:10:00", sujo), ("2026-09-21 08:10:00", limpo),
                             ("2026-09-21 09:10:00", limpo), ("2026-09-22 08:10:00", limpo)):
            cp.salvar(r, self.conn, rodado_em=rodado_em)
        self.assertEqual(cp.dias_limpos_seguidos(self.conn), 2)
        self.assertEqual(len(cp.historico(self.conn, 10)), 3)   # uma linha por dia
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest nucleo.test_comparar_pool -v`
Expected: `ImportError ... comparar_pool`.

- [ ] **Step 3: Implementar `nucleo/comparar_pool.py`**

```python
# -*- coding: utf-8 -*-
"""
nucleo/comparar_pool.py

Comparador do POOL: Vuupt (`not_assigned` ao vivo) × núcleo (nucleo/pool.py)
-- a sombra da Entrega 1 do spec
docs/superpowers/specs/2026-09-24-pool-pelo-nucleo-pedidos-portal-rascunho-14h-design.md.
O planejamento só pode trocar de fonte (planejamento.fonte_pool: nucleo)
depois de 3 dias úteis seguidos sem divergência sem explicação.

Compara código a código o que a tela enxerga: id, endereço, coordenadas,
caixas, remetente, CNPJ do destinatário, data agendada, janela resolvida
(roteirizacao_dados.resolver_janela) e regra de dia fixo. Nível, área e NF
derivam desses campos, então não precisam entrar.

Nunca escreve na Vuupt. Grava no banco só com --salvar/--email.

COMO USAR (VPS, como www-data):
    venv/bin/python nucleo/comparar_pool.py                  # imprime as diferenças
    venv/bin/python nucleo/comparar_pool.py --salvar --email # o que o timer horário roda
    venv/bin/python nucleo/comparar_pool.py --historico 10   # placar por dia (não vai na Vuupt)
Saída 0 = pools iguais; 2 = houve divergência.
"""
import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo import banco, pool
from nucleo.normalizacao import normalizar_codigo
from regioes_dia_fixo import regra_dia_fixo_do_servico
from roteirizacao_dados import resolver_janela

logger = logging.getLogger("nucleo.comparar_pool")

CAMPOS = ("id", "address", "latitude", "longitude", "dimension_3", "sender_id", "customer_code",
          "agendado_para", "janela", "dia_fixo")


def _num(valor):
    try:
        return round(float(valor), 5)
    except (TypeError, ValueError):
        return None


def projetar(s: dict) -> dict:
    ini, fim, _fonte = resolver_janela(s)
    regra = regra_dia_fixo_do_servico(s)
    return {
        "id": s.get("id"),
        "address": (s.get("address") or "").strip(),
        "latitude": _num(s.get("latitude")),
        "longitude": _num(s.get("longitude")),
        "dimension_3": s.get("dimension_3"),
        "sender_id": s.get("sender_id"),
        "customer_code": (s.get("customer") or {}).get("code"),
        "agendado_para": (s.get("scheduled_start") or "")[:10] or None,
        "janela": (ini, fim) if ini else None,
        "dia_fixo": regra["nome"] if regra else None,
    }


def comparar(pool_vuupt: list[dict], pool_nucleo: list[dict]) -> dict:
    """Puro (é o que os testes exercitam)."""
    def por_codigo(itens):
        mapa = {}
        for s in itens:
            codigo = normalizar_codigo(s.get("code"))
            if codigo:
                mapa[codigo] = projetar(s)
        return mapa

    v, n = por_codigo(pool_vuupt), por_codigo(pool_nucleo)
    diferentes = []
    for codigo in sorted(set(v) & set(n)):
        campos = {c: [v[codigo][c], n[codigo][c]] for c in CAMPOS if v[codigo][c] != n[codigo][c]}
        if campos:
            diferentes.append({"code": codigo, "campos": campos})
    so_v, so_n = sorted(set(v) - set(n)), sorted(set(n) - set(v))
    return {"total_vuupt": len(v), "total_nucleo": len(n), "so_na_vuupt": so_v, "so_no_nucleo": so_n,
            "diferentes": diferentes, "total_divergencias": len(so_v) + len(so_n) + len(diferentes)}


def resumir(r: dict, exemplos: int = 5) -> str:
    linhas = [f"Pool: Vuupt {r['total_vuupt']} × núcleo {r['total_nucleo']} -- "
              f"{r['total_divergencias']} divergência(s)"]
    if r["so_na_vuupt"]:
        linhas.append(f"  só na Vuupt   ({len(r['so_na_vuupt'])}): {', '.join(r['so_na_vuupt'][:exemplos])}")
    if r["so_no_nucleo"]:
        linhas.append(f"  só no núcleo  ({len(r['so_no_nucleo'])}): {', '.join(r['so_no_nucleo'][:exemplos])}")
    for d in r["diferentes"][:exemplos]:
        linhas.append(f"  {d['code']}: " + "; ".join(f"{c}: {a!r} × {b!r}" for c, (a, b) in d["campos"].items()))
    if len(r["diferentes"]) > exemplos:
        linhas.append(f"  ... e mais {len(r['diferentes']) - exemplos} com campo diferente")
    return "\n".join(linhas)


# ── Placar ────────────────────────────────────────────────────────────────────

def _garantir_tabela(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE IF NOT EXISTS nucleo_reconciliacoes_pool (
                        rodado_em          TEXT PRIMARY KEY,
                        total_vuupt        INTEGER NOT NULL DEFAULT 0,
                        total_nucleo       INTEGER NOT NULL DEFAULT 0,
                        total_divergencias INTEGER NOT NULL DEFAULT 0,
                        detalhes_json      TEXT)""")


def salvar(r: dict, conn: sqlite3.Connection, rodado_em: str | None = None):
    _garantir_tabela(conn)
    conn.execute("""INSERT OR REPLACE INTO nucleo_reconciliacoes_pool
                    (rodado_em, total_vuupt, total_nucleo, total_divergencias, detalhes_json)
                    VALUES (?, ?, ?, ?, ?)""",
                 (rodado_em or banco.agora(), r["total_vuupt"], r["total_nucleo"], r["total_divergencias"],
                  json.dumps({k: r[k] for k in ("so_na_vuupt", "so_no_nucleo", "diferentes")},
                             ensure_ascii=False, default=str)))
    conn.commit()


def historico(conn: sqlite3.Connection, limite: int = 20) -> list[dict]:
    """Uma linha por DIA: pior rodada do dia (máximo de divergências)."""
    _garantir_tabela(conn)
    return [dict(r) for r in conn.execute(
        """SELECT substr(rodado_em, 1, 10) AS dia, COUNT(*) AS rodadas, MAX(total_vuupt) AS total_vuupt,
                  MAX(total_divergencias) AS pior, SUM(total_divergencias) AS soma
           FROM nucleo_reconciliacoes_pool GROUP BY dia ORDER BY dia DESC LIMIT ?""", (limite,))]


def dias_limpos_seguidos(conn: sqlite3.Connection) -> int:
    """Dias COM pool, do mais recente pra trás, em que NENHUMA rodada achou
    divergência. Dia sem pool (fim de semana vazio) nem conta nem quebra."""
    seguidos = 0
    for linha in historico(conn, limite=60):
        if not linha["total_vuupt"]:
            continue
        if linha["pior"]:
            break
        seguidos += 1
    return seguidos


def _enviar_email(r: dict, conn: sqlite3.Connection, config: dict, exemplos: int):
    from email_utils import COR_ACENTO, COR_ERRO, envelope_html, enviar_email
    limpos = dias_limpos_seguidos(conn)
    destino = (config.get("notificacao_execucao", {}) or {}).get("destinatario") or "hugo@freshlogbr.com"
    titulo = f"Pool Vuupt × núcleo: {r['total_divergencias']} divergência(s)"
    corpo = [f"<h2 style='margin:0 0 12px'>{titulo}</h2>",
             f"<p>Critério da Entrega 1: 3 dias úteis seguidos sem divergência. Hoje: <b>{limpos}</b>.</p>",
             "<pre style='white-space:pre-wrap;font-size:12px;background:#F3F4F6;padding:12px;border-radius:6px'>",
             resumir(r, exemplos), "</pre>"]
    enviar_email([destino], titulo, envelope_html("".join(corpo), cor_acento=COR_ERRO), config.get("email", {}))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compara o pool Vuupt × núcleo (somente leitura na Vuupt).")
    parser.add_argument("--exemplos", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", help="caminho do banco (padrão: dados/dados.db)")
    parser.add_argument("--salvar", action="store_true", help="grava a rodada em nucleo_reconciliacoes_pool")
    parser.add_argument("--email", action="store_true", help="manda e-mail SÓ quando há divergência")
    parser.add_argument("--historico", type=int, metavar="N", help="placar dos últimos N dias (não vai na Vuupt)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    caminho = Path(args.db) if args.db else banco.DB_PATH
    conn = banco.conectar(caminho)
    try:
        if args.historico:
            for linha in historico(conn, args.historico):
                marca = "OK " if not linha["pior"] else "!! "
                print(f"{marca}{linha['dia']}  rodadas={linha['rodadas']:2d} pool={linha['total_vuupt']:3d} "
                      f"pior={linha['pior']:3d} soma={linha['soma']:4d}")
            print(f"\nDias com pool, seguidos, sem divergência: {dias_limpos_seguidos(conn)} (critério: 3 úteis)")
            return 0

        import yaml
        caminho_config = next((c for c in (_RAIZ / "config.yaml", Path.cwd() / "config.yaml") if c.exists()), None)
        if caminho_config is None:
            logger.error("config.yaml não encontrado.")
            return 1
        config = yaml.safe_load(caminho_config.read_text(encoding="utf-8")) or {}
        token = (config.get("vuupt_api") or {}).get("token", "")
        if not token:
            logger.error("vuupt_api.token ausente no config.yaml.")
            return 1

        from vuupt_client import VuuptClient
        vuupt = VuuptClient(token)
        pool_vuupt = vuupt.listar_servicos([{"field": "status", "operator": "eq", "value": "not_assigned"}],
                                           per_page=100, include=["customer"])
        pool_nucleo = pool.listar_pool(conn)
        r = comparar(pool_vuupt, pool_nucleo)
        if args.salvar:
            salvar(r, conn)
        if args.email and r["total_divergencias"]:
            _enviar_email(r, conn, config, args.exemplos)
    finally:
        conn.close()

    print(json.dumps(r, ensure_ascii=False, indent=1, default=str) if args.json else resumir(r, args.exemplos))
    return 2 if r["total_divergencias"] else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest nucleo.test_comparar_pool -v`
Expected: todos OK. Se `test_janela_e_dia_fixo_entram_na_projecao` falhar no `dia_fixo`, conferir o nome exato da região de Sorocaba em `roteirizacao/regioes_dia_fixo.REGIOES` e ajustar o endereço do fixture (não a função).

- [ ] **Step 5: Lista de acoplamento**

Em `nucleo/test_acoplamento_vuupt.py`, dentro de `PERMITIDOS`, depois de `"nucleo/comparar_vuupt.py"`:

```python
    "nucleo/comparar_pool.py",              # 24/09: sombra do pool (Entrega 1); some na virada
```

Run: `py -3.11 -m unittest nucleo.test_acoplamento_vuupt -v`
Expected: OK.

- [ ] **Step 6: Units systemd**

Criar `infra/stokki-comparar-pool.service`:

```ini
[Unit]
Description=Stokki Eventos - Compara o pool Vuupt x nucleo (sombra da Entrega 1) e avisa por e-mail se divergir
OnFailure=stokki-alerta-falha@%n.service

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/opt/stokki-eventos
# Saida 2 = achou divergencia (nao e falha do job; o e-mail ja conta a historia).
SuccessExitStatus=2
ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/nucleo/comparar_pool.py --salvar --email
```

Criar `infra/stokki-comparar-pool.timer`:

```ini
[Unit]
Description=Dispara stokki-comparar-pool.service de hora em hora (07h-20h, :10, depois do sync de :05)

[Timer]
OnCalendar=*-*-* 07..20:10:00
Persistent=false
AccuracySec=1min

[Install]
WantedBy=timers.target
```

- [ ] **Step 7: Rodar contra a Vuupt de verdade (leitura), localmente**

Run: `py -3.11 nucleo/comparar_pool.py --exemplos 10`
Expected: imprime `Pool: Vuupt N × núcleo M -- K divergência(s)` e sai com 0 ou 2. Localmente o núcleo está congelado, então `só na Vuupt` vai ser grande: o que se prova aqui é que o script roda de ponta a ponta. A medição que vale é na VPS (Tarefa 9).

Run: `py -3.11 -m py_compile nucleo/comparar_pool.py nucleo/test_comparar_pool.py`

---

### Task 9: deploy em sombra, medição e critério de virada (runbook)

**Files:**
- Nenhum arquivo de código. Anotar resultados em `DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md` (seção 5, log de status) quando o Hugo pedir o commit.

- [ ] **Step 1: Testes completos antes de pedir commit**

Run: `py -3.11 -m unittest nucleo.test_pool nucleo.test_sincronizar_servicos nucleo.test_comparar_pool nucleo.test_acoplamento_vuupt nucleo.test_espelho_fiel painel_agentes.test_ressincronizar_pool -v 2>&1 | tail -n 5`
Expected: OK.

Run: `git status --short`
Expected: só os arquivos das Tarefas 1–8 (novos: `nucleo/pool.py`, `nucleo/comparar_pool.py`, `nucleo/test_pool.py`, `nucleo/test_comparar_pool.py`, `painel_agentes/test_ressincronizar_pool.py`, `infra/stokki-comparar-pool.*`; modificados: `nucleo/normalizacao.py`, `nucleo/sincronizar_servicos_vuupt.py`, `nucleo/test_sincronizar_servicos.py`, `nucleo/test_acoplamento_vuupt.py`, `painel_agentes/planejamento_rotas.py`, `painel_agentes/painel_agentes.py`, `painel_agentes/rascunhos_rota.py`, `painel_agentes/templates/planejamento_rotas.html`, `roteirizacao/criar_rotas_diarias.py`, `roteirizacao/incrementar_rotas.py`). Qualquer outro arquivo modificado é de outra sessão: não incluir.

- [ ] **Step 2: Pedir ao Hugo commit + deploy** (skill `deploy-vps`). Mensagem de commit sugerida:
  `Planejamento: pool pode ler do nucleo (fonte_pool) + comparador em sombra + ressincronizacao imediata`

- [ ] **Step 3: Na VPS, depois do pull/chown**

```
cp infra/stokki-comparar-pool.* /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now stokki-comparar-pool.timer
systemctl restart painel-agentes
sudo -u www-data venv/bin/python nucleo/comparar_pool.py --exemplos 10
```
Expected: `Pool: Vuupt N × núcleo N -- 0 divergência(s)` (em 16/09 já era 74 = 74 por contagem). Se houver divergência, cada linha diz o campo: é a lista de trabalho antes da virada (fuso do agendamento e `customer.name` são os suspeitos do spec).

- [ ] **Step 4: Medir o botão "Atualizar" com fonte nucleo (sem virar a tela ainda)**

Na VPS: `sudo -u www-data bash -c 'time venv/bin/python nucleo/sincronizar_servicos_vuupt.py'`
Expected: anotar o tempo. Se passar de ~10 s, abrir tarefa pra rodar em thread + polling no front (fora deste plano).

- [ ] **Step 5: Sombra**

Deixar o timer rodar. Todo dia: `sudo -u www-data venv/bin/python nucleo/comparar_pool.py --historico 10`. Critério: **3 dias úteis seguidos** com `pior=0`.

- [ ] **Step 6: Virada (decisão do Hugo)**

Na VPS, em `/opt/stokki-eventos/config.yaml`, acrescentar:

```yaml
planejamento:
  fonte_pool: nucleo
```

`systemctl restart painel-agentes` (o config é lido por requisição em `planejamento_rotas`, mas o restart garante). Provar: abrir `/planejamento`, clicar "Atualizar" (deve mostrar "Sincronizando..." e o pool), cancelar/reagendar um pedido de teste e ver o card refletir sem esperar 15 min. Rodar `criar_rotas_diarias.py --gerar-rascunho --modo-teste` como www-data e conferir no log a contagem de `not_assigned` igual à do comparador.

Volta atrás: trocar a chave pra `vuupt` (ou remover) e reiniciar o painel.
