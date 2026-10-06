# Batimento: saída (aba Fechamento, Torre, e-mail) — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** levar o resultado do batimento (tabelas `batimento_pedidos` / `batimento_rodadas`, já em produção) para a aba Fechamento do `/vigia`, para a Fila de ação da Torre e para um e-mail interno.

**Architecture:** tudo novo fica em `batimento/` (padrão do `vigia/`): `regras.py` ganha os grupos de divergência, `banco.py` ganha a tratativa, `consulta.py` (novo) monta aba e Torre, `bater.py` manda o e-mail no fim da rodada. O painel só ganha a aba, uma rota POST e 6 linhas na Torre.

**Tech Stack:** Python 3.11, SQLite, Flask/Jinja (painel_agentes), unittest (sem pytest), `email_utils.enviar_email`.

**Spec:** `docs/superpowers/specs/2026-10-05-batimento-saida-design.md` (e `DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md`).

## Global Constraints

- Python: **sempre `py -3.11`**; testes com `py -3.11 -m unittest`, nunca pytest.
- Testes de `batimento/` rodam **da raiz do worktree** (`C:\agente_stokki_eventos_batimento`); testes de `painel_agentes/` rodam com `py -3.11 -m unittest painel_agentes.<modulo>` da raiz também, carregando o app pelo caminho (padrão de `painel_agentes/test_clientes_agenda_tela.py`). Nunca misturar `painel_agentes` com `batimento`/`nucleo` no mesmo comando.
- Idioma: português; comentários e logs sem acento nos `.py` de `batimento/` (padrão do módulo); textos de tela e e-mail com acento.
- Dia útil = segunda a sexta (feriados fora do escopo).
- Divergências reais: `EXPEDIDO_SEM_ENTREGA`, `EXPEDIDO_SEM_DOCUMENTO`, `EXPEDIDO_COM_INSUCESSO_ABERTO`, `ENTREGUE_NAO_EXPEDIDO`, `CANCELADO_STOKKI_SERVICO_VIVO`, `CANCELADO_VUUPT_STOKKI_ABERTO`, `STATUS_STOKKI_DESCONHECIDO`. De comprovante: `REDESPACHO_SEM_COMPROVANTE`, `RETIRADA_SEM_COMPROVANTE`, `LALAMOVE_SEM_COMPROVANTE`.
- Botão/rota "Tratar": níveis `total` e `operador`. Aba visível para `total`, `operador`, `leitura` (mesmo `niveis=` da rota `/vigia`).
- E-mail: destino `config["notificacao_execucao"]["destinatario"]`, fallback `hugo@freshlogbr.com`; só com novidade; nunca com `--resumo`; falha no envio não muda o exit code.
- URLs dentro de itens da Torre são relativas ao painel (`/vigia?...`), sem `url_for`. Link no e-mail: `https://app.freshhub.com.br/painel/vigia?aba=fechamento`.
- Commits: `Batimento: ...` / `Painel: ...` em português, com `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. `git add` só dos arquivos da tarefa. **Commit/push só quando o Hugo pedir** (CLAUDE.md): os passos "Commit" abaixo são feitos no fim, em lote, se o Hugo autorizar.

## Review Focus

1. Pedido que muda de motivo depois de tratado: a tratativa some e ele volta pra Torre (teste na Task 2).
2. Banco sem nenhuma rodada (painel local, primeira vez): aba renderiza vazia sem erro e a Torre devolve `[]` (testes nas Tasks 3 e 5).
3. Divergência que aparece numa sexta: só vence na segunda no mesmo horário, não no sábado (teste na Task 3).
4. Rodada que grava mas o e-mail falha (SMTP fora): job continua com exit 0 se a conta fecha (teste na Task 4).
5. Nível `leitura` tentando POST em `/api/batimento/tratar`: recusado, e o botão nem aparece (teste na Task 5).

---

## Mapa de arquivos

| Arquivo | Papel |
|---|---|
| `batimento/regras.py` (modificar) | + `DIVERGENCIAS_REAIS`, `DIVERGENCIAS_COMPROVANTE`, `ROTULOS_DIVERGENCIA` |
| `batimento/banco.py` (modificar) | + coluna `tratado_obs`, limpa tratativa na troca de motivo, `marcar_tratado()` |
| `batimento/consulta.py` (criar) | `vencida`, `ultima_rodada`, `fechamento`, `novidades`, `excecoes_torre`, `tratar` |
| `batimento/bater.py` (modificar) | e-mail de novidade no fim da rodada |
| `painel_agentes/painel_agentes.py` (modificar) | `/vigia` lê `aba`; rota `POST /api/batimento/tratar` |
| `painel_agentes/templates/vigia_pedidos.html` (modificar) | faixa de abas + bloco Fechamento |
| `painel_agentes/torre_controle.py` (modificar) | chama `batimento.consulta.excecoes_torre` |
| testes | `batimento/test_regras.py`, `test_banco.py`, `test_consulta.py` (novo), `test_bater.py` (novo), `painel_agentes/test_batimento_tela.py` (novo) |
| docs | `DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md`, `MAPA_DO_SISTEMA.txt` |

---

### Task 1: Grupos de divergência em `regras.py`

**Files:**
- Modify: `batimento/regras.py` (depois da lista de motivos, ~linha 60)
- Test: `batimento/test_regras.py`

**Interfaces:**
- Produces: `regras.DIVERGENCIAS_REAIS: tuple[str, ...]`, `regras.DIVERGENCIAS_COMPROVANTE: tuple[str, ...]`, `regras.ROTULOS_DIVERGENCIA: dict[str, str]`.

- [ ] **Step 1: Teste que falha** — acrescentar em `batimento/test_regras.py`:

```python
class GruposDivergencia(unittest.TestCase):
    def test_grupos_cobrem_todos_os_motivos_sem_sobrepor(self):
        motivos = {r.EXPEDIDO_SEM_ENTREGA, r.EXPEDIDO_SEM_DOCUMENTO, r.ENTREGUE_NAO_EXPEDIDO,
                   r.CANCELADO_STOKKI_SERVICO_VIVO, r.CANCELADO_VUUPT_STOKKI_ABERTO,
                   r.REDESPACHO_SEM_COMPROVANTE, r.RETIRADA_SEM_COMPROVANTE, r.LALAMOVE_SEM_COMPROVANTE,
                   r.EXPEDIDO_COM_INSUCESSO_ABERTO, r.STATUS_STOKKI_DESCONHECIDO}
        reais, comp = set(r.DIVERGENCIAS_REAIS), set(r.DIVERGENCIAS_COMPROVANTE)
        self.assertEqual(reais | comp, motivos)
        self.assertFalse(reais & comp)
        self.assertEqual(set(r.ROTULOS_DIVERGENCIA), motivos)
```

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest batimento.test_regras` → `AttributeError: ... DIVERGENCIAS_REAIS`.

- [ ] **Step 3: Implementar** — em `batimento/regras.py`, logo depois de `STATUS_STOKKI_DESCONHECIDO = ...`:

```python
# Grupos (decisao do Hugo, 05/10): so as REAIS vao pra Torre. As de
# comprovante existem porque a captura ainda nao existe (opcao A) e ficam
# so na aba e no e-mail.
DIVERGENCIAS_REAIS = (
    EXPEDIDO_SEM_ENTREGA, EXPEDIDO_SEM_DOCUMENTO, EXPEDIDO_COM_INSUCESSO_ABERTO,
    ENTREGUE_NAO_EXPEDIDO, CANCELADO_STOKKI_SERVICO_VIVO, CANCELADO_VUUPT_STOKKI_ABERTO,
    STATUS_STOKKI_DESCONHECIDO,
)
DIVERGENCIAS_COMPROVANTE = (REDESPACHO_SEM_COMPROVANTE, RETIRADA_SEM_COMPROVANTE, LALAMOVE_SEM_COMPROVANTE)

ROTULOS_DIVERGENCIA = {
    EXPEDIDO_SEM_ENTREGA: "Expedido sem entrega",
    EXPEDIDO_SEM_DOCUMENTO: "Entregue sem canhoto",
    EXPEDIDO_COM_INSUCESSO_ABERTO: "Expedido com insucesso",
    ENTREGUE_NAO_EXPEDIDO: "Entregue e não expedido",
    CANCELADO_STOKKI_SERVICO_VIVO: "Cancelado na Stokki, serviço vivo",
    CANCELADO_VUUPT_STOKKI_ABERTO: "Cancelado na Vuupt, aberto na Stokki",
    STATUS_STOKKI_DESCONHECIDO: "Status da Stokki desconhecido",
    REDESPACHO_SEM_COMPROVANTE: "Redespacho sem comprovante",
    RETIRADA_SEM_COMPROVANTE: "Retirada sem comprovante",
    LALAMOVE_SEM_COMPROVANTE: "Lalamove sem comprovante",
}
```

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest batimento.test_regras` → OK.

---

### Task 2: Tratativa em `banco.py`

**Files:**
- Modify: `batimento/banco.py` (`garantir_esquema`, `gravar_rodada`, nova `marcar_tratado`)
- Test: `batimento/test_banco.py`

**Interfaces:**
- Consumes: nada novo.
- Produces: coluna `batimento_pedidos.tratado_obs`; `banco.marcar_tratado(conn: sqlite3.Connection, codigo: str, por: str, obs: str, agora: datetime | None = None) -> None` (levanta `ValueError` com mensagem em português se obs vazia, pedido inexistente ou fora de `DIVERGENCIA`).

- [ ] **Step 1: Testes que falham** — acrescentar na classe `GravarRodada` de `batimento/test_banco.py`:

```python
    def test_marcar_tratado_grava_quem_quando_obs(self):
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], resumo(1), 1, self.t1)
        banco.marcar_tratado(self.conn, "PS-5", "hugo", "  cliente confirmou recebimento  ", self.t2)
        l = self.linha("PS-5")
        self.assertEqual((l["tratado_por"], l["tratado_obs"], l["tratado_em"]),
                         ("hugo", "cliente confirmou recebimento", "2026-10-06 07:25:00"))

    def test_marcar_tratado_recusa_obs_vazia_e_fora_de_divergencia(self):
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA"),
                                        ped("PS-6", "DESTINO", "ENTREGUE")], resumo(2), 1, self.t1)
        with self.assertRaises(ValueError):
            banco.marcar_tratado(self.conn, "PS-5", "hugo", "   ")
        with self.assertRaises(ValueError):
            banco.marcar_tratado(self.conn, "PS-6", "hugo", "x")
        with self.assertRaises(ValueError):
            banco.marcar_tratado(self.conn, "PS-404", "hugo", "x")

    def test_troca_de_motivo_limpa_tratativa(self):
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], resumo(1), 1, self.t1)
        banco.marcar_tratado(self.conn, "PS-5", "hugo", "vendo com o motorista", self.t1)
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], resumo(1), 1, self.t2)
        self.assertEqual(self.linha("PS-5")["tratado_por"], "hugo")      # mesmo motivo: mantem
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_DOCUMENTO")], resumo(1), 1,
                            datetime(2026, 10, 7, 7, 25))
        l = self.linha("PS-5")
        self.assertIsNone(l["tratado_em"])
        self.assertIsNone(l["tratado_por"])
        self.assertIsNone(l["tratado_obs"])
```

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest batimento.test_banco` → `AttributeError: ... marcar_tratado` / coluna `tratado_obs` inexistente.

- [ ] **Step 3: Implementar.** Em `garantir_esquema`, depois do `executescript`:

```python
    colunas = {r[1] for r in conn.execute("PRAGMA table_info(batimento_pedidos)")}
    if "tratado_obs" not in colunas:
        conn.execute("ALTER TABLE batimento_pedidos ADD COLUMN tratado_obs TEXT")
        conn.commit()
```

Em `gravar_rodada`, trocar o trecho `{", desde = ?" if trocou else ""}` por:

```python
                {", desde = ?, tratado_em = NULL, tratado_por = NULL, tratado_obs = NULL" if trocou else ""}
```

(a tupla de parâmetros não muda: `*([agora_txt] if trocou else [])` continua certo). Acrescentar no fim do arquivo:

```python
def marcar_tratado(conn: sqlite3.Connection, codigo: str, por: str, obs: str,
                   agora: datetime | None = None) -> None:
    """Tratativa manual de uma divergencia (aba Fechamento). Vale enquanto
    o pedido nao trocar de motivo: gravar_rodada limpa na troca."""
    obs = (obs or "").strip()
    if not obs:
        raise ValueError("Escreva o que foi feito (observação obrigatória).")
    row = conn.execute("SELECT caixa FROM batimento_pedidos WHERE codigo = ?", (codigo,)).fetchone()
    if row is None:
        raise ValueError(f"{codigo} não está no batimento.")
    if row[0] != "DIVERGENCIA":
        raise ValueError(f"{codigo} não está em divergência.")
    conn.execute("UPDATE batimento_pedidos SET tratado_em = ?, tratado_por = ?, tratado_obs = ? WHERE codigo = ?",
                 ((agora or datetime.now()).strftime(FMT), por, obs[:300], codigo))
    conn.commit()
```

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest batimento.test_banco` → OK (10 testes).

---

### Task 3: `batimento/consulta.py`

**Files:**
- Create: `batimento/consulta.py`
- Test: `batimento/test_consulta.py`

**Interfaces:**
- Consumes: `banco.conectar(db_path)`, `banco.FMT`, `banco.marcar_tratado`, `regras.DIVERGENCIAS_REAIS`, `regras.ROTULOS_DIVERGENCIA`.
- Produces:
  - `vencida(desde: str | None, agora: datetime) -> bool`
  - `ultima_rodada(conn) -> dict | None` (colunas de `batimento_rodadas` + `resumo` = `resumo_json` decodificado)
  - `fechamento(motivo="", so_reais=False, incluir_tratadas=False, busca="", db_path=None, agora=None) -> dict` com chaves `linhas` (list[dict]: colunas de `batimento_pedidos` + `motivo_txt`, `real`, `vencida`, `evidencias` list[str], `tratada`), `por_motivo` (list[dict]: `motivo`, `rotulo`, `total`, `real`), `total_abertas` (int), `ultima` (dict | None), `rodadas` (list[dict], 14 mais recentes primeiro)
  - `novidades(conn, rodada_em: str) -> list[dict]` (divergências que entraram nesta rodada)
  - `excecoes_torre(data_iso: str, db_path=None, agora=None) -> list[dict]`
  - `tratar(codigo: str, por: str, obs: str, db_path=None) -> None`

- [ ] **Step 1: Testes que falham** — criar `batimento/test_consulta.py`:

```python
# -*- coding: utf-8 -*-
"""Testes de batimento/consulta.py (sqlite em arquivo temporario). Rodar da raiz:
py -3.11 -m unittest batimento.test_consulta"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from batimento import banco, consulta


def ped(codigo, caixa, rotulo, **kw):
    return {"codigo": codigo, "caixa": caixa, "rotulo": rotulo, "embarcador": "EMB", "evidencias": "a | b", **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"

    def gravar(self, pedidos, quando, fecha=True):
        conn = banco.conectar(self.db)
        try:
            banco.gravar_rodada(conn, pedidos, {"lancados": len(pedidos), "equacao_fecha": fecha}, 1, quando)
        finally:
            conn.close()


class Vencida(unittest.TestCase):
    def test_um_dia_util(self):
        self.assertFalse(consulta.vencida("2026-10-05 07:25:00", datetime(2026, 10, 6, 7, 24)))   # seg -> ter
        self.assertTrue(consulta.vencida("2026-10-05 07:25:00", datetime(2026, 10, 6, 7, 25)))

    def test_sexta_so_vence_na_segunda(self):
        self.assertFalse(consulta.vencida("2026-10-09 07:25:00", datetime(2026, 10, 11, 23, 0)))  # domingo
        self.assertTrue(consulta.vencida("2026-10-09 07:25:00", datetime(2026, 10, 12, 7, 25)))   # segunda

    def test_desde_invalido_nao_vence(self):
        self.assertFalse(consulta.vencida(None, datetime(2026, 10, 12)))


class Fechamento(Base):
    def test_sem_rodada_volta_vazio(self):
        d = consulta.fechamento(db_path=self.db)
        self.assertEqual((d["linhas"], d["por_motivo"], d["total_abertas"], d["ultima"], d["rodadas"]),
                         ([], [], 0, None, []))

    def test_lista_so_divergencias_com_filtros(self):
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA"),
                     ped("PS-2", "DIVERGENCIA", "REDESPACHO_SEM_COMPROVANTE"),
                     ped("PS-3", "DESTINO", "ENTREGUE")], datetime(2026, 10, 5, 7, 25))
        agora = datetime(2026, 10, 7, 9, 0)
        d = consulta.fechamento(db_path=self.db, agora=agora)
        self.assertEqual([l["codigo"] for l in d["linhas"]], ["PS-1", "PS-2"])
        self.assertEqual(d["total_abertas"], 2)
        self.assertEqual(d["por_motivo"][0]["motivo"], "EXPEDIDO_SEM_ENTREGA")   # reais primeiro
        self.assertTrue(d["linhas"][0]["real"] and d["linhas"][0]["vencida"])
        self.assertEqual(d["linhas"][0]["evidencias"], ["a", "b"])
        self.assertEqual(d["ultima"]["lancados"], 3)
        self.assertEqual(len(d["rodadas"]), 1)
        self.assertEqual([l["codigo"] for l in consulta.fechamento(so_reais=True, db_path=self.db)["linhas"]],
                         ["PS-1"])
        self.assertEqual([l["codigo"] for l in consulta.fechamento(
            motivo="REDESPACHO_SEM_COMPROVANTE", db_path=self.db)["linhas"]], ["PS-2"])
        self.assertEqual([l["codigo"] for l in consulta.fechamento(busca="ps-2", db_path=self.db)["linhas"]],
                         ["PS-2"])

    def test_tratada_sai_da_lista_salvo_se_pedir(self):
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], datetime(2026, 10, 5, 7, 25))
        consulta.tratar("PS-1", "hugo", "resolvido", db_path=self.db)
        self.assertEqual(consulta.fechamento(db_path=self.db)["linhas"], [])
        d = consulta.fechamento(incluir_tratadas=True, db_path=self.db)
        self.assertTrue(d["linhas"][0]["tratada"])
        self.assertEqual(d["total_abertas"], 0)


class Novidades(Base):
    def test_so_quem_entrou_em_divergencia_nesta_rodada(self):
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], datetime(2026, 10, 5, 7, 25))
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA"),
                     ped("PS-2", "DIVERGENCIA", "RETIRADA_SEM_COMPROVANTE"),
                     ped("PS-3", "EM_ANDAMENTO", "NO_POOL")], datetime(2026, 10, 6, 7, 25))
        conn = banco.conectar(self.db)
        try:
            novas = consulta.novidades(conn, "2026-10-06 07:25:00")
        finally:
            conn.close()
        self.assertEqual([n["codigo"] for n in novas], ["PS-2"])


class ExcecoesTorre(Base):
    def test_sem_rodada_nao_gera_nada(self):
        self.assertEqual(consulta.excecoes_torre("2026-10-07", db_path=self.db), [])

    def test_so_reais_vencidas_e_nao_tratadas(self):
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA"),
                     ped("PS-2", "DIVERGENCIA", "REDESPACHO_SEM_COMPROVANTE"),
                     ped("PS-3", "DIVERGENCIA", "ENTREGUE_NAO_EXPEDIDO")], datetime(2026, 10, 5, 7, 25))
        consulta.tratar("PS-3", "hugo", "ok", db_path=self.db)
        itens = consulta.excecoes_torre("2026-10-07", db_path=self.db, agora=datetime(2026, 10, 7, 9, 0))
        self.assertEqual([x["id"] for x in itens], ["batimento:PS-1:EXPEDIDO_SEM_ENTREGA"])
        x = itens[0]
        self.assertEqual((x["tipo"], x["severidade"]), ("Batimento", "atencao"))
        self.assertEqual(x["acao"]["url"], "/vigia?aba=fechamento&busca=PS-1")
        self.assertIn("Expedido sem entrega", x["descricao"])
        # ainda no prazo: nada
        self.assertEqual(consulta.excecoes_torre("2026-10-05", db_path=self.db,
                                                 agora=datetime(2026, 10, 5, 12, 0)), [])

    def test_rodada_que_nao_fecha_gera_critico(self):
        self.gravar([ped("PS-1", "EM_ANDAMENTO", "NO_POOL")], datetime(2026, 10, 5, 7, 25), fecha=False)
        itens = consulta.excecoes_torre("2026-10-05", db_path=self.db, agora=datetime(2026, 10, 5, 8, 0))
        self.assertEqual(len(itens), 1)
        self.assertEqual((itens[0]["severidade"], itens[0]["id"]),
                         ("critico", "batimento:nao-fecha:2026-10-05 07:25:00"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest batimento.test_consulta` → `ImportError: cannot import name 'consulta'`.

- [ ] **Step 3: Implementar** — criar `batimento/consulta.py`:

```python
# -*- coding: utf-8 -*-
"""
batimento/consulta.py

Leitura do batimento pra aba Fechamento do /vigia, pra Torre e pro e-mail
do bater.py (spec docs/superpowers/specs/2026-10-05-batimento-saida-design.md).
So le batimento_pedidos/batimento_rodadas, exceto tratar().
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from batimento import banco, regras

RODADAS_NO_HISTORICO = 14


def _proximo_dia_util(dt: datetime) -> datetime:
    d = dt + timedelta(days=1)
    while d.weekday() >= 5:   # sabado/domingo; feriados ainda em aberto
        d += timedelta(days=1)
    return d


def vencida(desde: str | None, agora: datetime) -> bool:
    """Passou 1 dia util desde `desde` (mesmo horario do proximo dia de
    segunda a sexta)."""
    try:
        dt = datetime.strptime(desde or "", banco.FMT)
    except ValueError:
        return False
    return agora >= _proximo_dia_util(dt)


def _rodada(row) -> dict:
    d = dict(row)
    try:
        d["resumo"] = json.loads(d.get("resumo_json") or "{}")
    except ValueError:
        d["resumo"] = {}
    return d


def ultima_rodada(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM batimento_rodadas ORDER BY id DESC LIMIT 1").fetchone()
    return _rodada(row) if row else None


def _linha(row, agora: datetime) -> dict:
    l = dict(row)
    l["motivo_txt"] = regras.ROTULOS_DIVERGENCIA.get(l["rotulo"], l["rotulo"])
    l["real"] = l["rotulo"] in regras.DIVERGENCIAS_REAIS
    l["vencida"] = vencida(l["desde"], agora)
    l["tratada"] = bool(l.get("tratado_em"))
    try:
        l["evidencias"] = [e for e in json.loads(l.get("evidencias_json") or "[]") if e]
    except ValueError:
        l["evidencias"] = []
    return l


def fechamento(motivo: str = "", so_reais: bool = False, incluir_tratadas: bool = False, busca: str = "",
               db_path: Path | None = None, agora: datetime | None = None) -> dict:
    agora = agora or datetime.now()
    conn = banco.conectar(db_path)
    try:
        todas = [_linha(r, agora) for r in conn.execute(
            "SELECT * FROM batimento_pedidos WHERE caixa = 'DIVERGENCIA' ORDER BY desde, codigo")]
        ultima = ultima_rodada(conn)
        rodadas = [_rodada(r) for r in conn.execute(
            "SELECT * FROM batimento_rodadas ORDER BY id DESC LIMIT ?", (RODADAS_NO_HISTORICO,))]
    finally:
        conn.close()

    abertas = [l for l in todas if not l["tratada"]]
    contagem: dict[str, int] = {}
    for l in abertas:
        contagem[l["rotulo"]] = contagem.get(l["rotulo"], 0) + 1
    por_motivo = sorted(
        ({"motivo": m, "rotulo": regras.ROTULOS_DIVERGENCIA.get(m, m), "total": n,
          "real": m in regras.DIVERGENCIAS_REAIS} for m, n in contagem.items()),
        key=lambda x: (not x["real"], -x["total"], x["motivo"]))

    termo = busca.strip().upper()
    linhas = [
        l for l in todas
        if (incluir_tratadas or not l["tratada"])
        and (not motivo or l["rotulo"] == motivo)
        and (not so_reais or l["real"])
        and (not termo or termo in l["codigo"] or termo in (l["embarcador"] or "").upper()
             or termo in (l["transportadora"] or "").upper())
    ]
    return {"linhas": linhas, "por_motivo": por_motivo, "total_abertas": len(abertas),
            "ultima": ultima, "rodadas": rodadas}


def novidades(conn: sqlite3.Connection, rodada_em: str) -> list[dict]:
    """Divergencias que entraram (ou trocaram de motivo) nesta rodada."""
    agora = datetime.strptime(rodada_em, banco.FMT)
    return [_linha(r, agora) for r in conn.execute(
        "SELECT * FROM batimento_pedidos WHERE caixa = 'DIVERGENCIA' AND desde = ? AND visto_em = ? "
        "ORDER BY codigo", (rodada_em, rodada_em))]


def excecoes_torre(data_iso: str, db_path: Path | None = None, agora: datetime | None = None) -> list[dict]:
    """Formato de torre_controle._montar_excecoes (sem _epoch). Um item por
    divergencia REAL nao tratada ha mais de 1 dia util (id estavel por
    pedido+motivo: "Tratar" na Torre esconde ate o motivo mudar) e um
    critico se a ultima rodada nao fechou."""
    agora = agora or datetime.now()
    try:
        conn = banco.conectar(db_path)
    except sqlite3.Error:
        return []
    try:
        ultima = ultima_rodada(conn)
        if ultima is None:
            return []
        marcas = ",".join("?" * len(regras.DIVERGENCIAS_REAIS))
        rows = conn.execute(
            f"SELECT codigo, embarcador, rotulo, desde FROM batimento_pedidos "
            f"WHERE caixa = 'DIVERGENCIA' AND tratado_em IS NULL AND rotulo IN ({marcas}) ORDER BY desde, codigo",
            regras.DIVERGENCIAS_REAIS).fetchall()
    finally:
        conn.close()

    itens = []
    if not ultima["fecha"]:
        itens.append({
            "id": f"batimento:nao-fecha:{ultima['rodada_em']}",
            "severidade": "critico", "tipo": "Batimento",
            "descricao": (f"Batimento de {ultima['rodada_em'][:16]} não fecha: {ultima['lancados']} lançados "
                          f"≠ {ultima['destino']} destino + {ultima['em_andamento']} andamento + "
                          f"{ultima['divergencia']} divergência."),
            "quando": None,
            "acao": {"tipo": "link", "url": "/vigia?aba=fechamento", "rotulo": "Ver no Fechamento"},
        })
    for r in rows:
        if not vencida(r["desde"], agora):
            continue
        desde = datetime.strptime(r["desde"], banco.FMT).strftime("%d/%m")
        itens.append({
            "id": f"batimento:{r['codigo']}:{r['rotulo']}",
            "severidade": "atencao", "tipo": "Batimento",
            "descricao": (f"{r['codigo']} {r['embarcador'] or ''}: "
                          f"{regras.ROTULOS_DIVERGENCIA.get(r['rotulo'], r['rotulo'])} desde {desde}").replace("  ", " "),
            "quando": None,
            "acao": {"tipo": "link", "url": f"/vigia?aba=fechamento&busca={r['codigo']}",
                     "rotulo": "Ver no Fechamento"},
        })
    return itens


def tratar(codigo: str, por: str, obs: str, db_path: Path | None = None) -> None:
    conn = banco.conectar(db_path)
    try:
        banco.marcar_tratado(conn, codigo, por, obs)
    finally:
        conn.close()
```

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest batimento.test_consulta batimento.test_banco batimento.test_regras` → OK.

---

### Task 4: E-mail de novidade no `bater.py`

**Files:**
- Modify: `batimento/bater.py`
- Test: `batimento/test_bater.py` (novo)

**Interfaces:**
- Consumes: `consulta.novidades(conn, rodada_em)`, `consulta.fechamento(db_path=...)`, `banco.gravar_rodada(..., agora)` (retorna dict com `fecha`, `totais`), `email_utils.enviar_email`, `email_utils.envelope_html`, `COR_ERRO`, `COR_DESTAQUE`.
- Produces: `bater.montar_email(novas: list[dict], rodada: dict, por_motivo: list[dict]) -> tuple[str, str]` (assunto, html sem envelope); `bater.avisar(config: dict, novas: list[dict], rodada: dict, por_motivo: list[dict]) -> bool` (True se mandou; nunca levanta).

- [ ] **Step 1: Testes que falham** — criar `batimento/test_bater.py`:

```python
# -*- coding: utf-8 -*-
"""Testes do e-mail do batimento/bater.py (envio mockado). Rodar da raiz:
py -3.11 -m unittest batimento.test_bater"""
import unittest
from unittest import mock

from batimento import bater

RODADA = {"rodada_em": "2026-10-06 07:25:00", "lancados": 10, "destino": 7, "em_andamento": 2,
          "divergencia": 1, "fecha": 1}
NOVA = {"codigo": "PS-1", "embarcador": "EMB", "motivo_txt": "Expedido sem entrega", "real": True,
        "evidencias": ["stokki=EXPEDIDO", "nucleo=None"]}
POR_MOTIVO = [{"motivo": "EXPEDIDO_SEM_ENTREGA", "rotulo": "Expedido sem entrega", "total": 1, "real": True}]


class MontarEmail(unittest.TestCase):
    def test_assunto_e_corpo_com_novidade(self):
        assunto, html = bater.montar_email([NOVA], RODADA, POR_MOTIVO)
        self.assertEqual(assunto, "[Freshlog] Batimento: 1 divergência(s) nova(s)")
        for trecho in ("PS-1", "Expedido sem entrega", "10 lançados", "vigia?aba=fechamento"):
            self.assertIn(trecho, html)

    def test_assunto_quando_nao_fecha(self):
        assunto, _ = bater.montar_email([], {**RODADA, "fecha": 0}, POR_MOTIVO)
        self.assertTrue(assunto.startswith("[Freshlog] Batimento NÃO FECHA"))


class Avisar(unittest.TestCase):
    def test_sem_novidade_e_fechando_nao_manda(self):
        with mock.patch("email_utils.enviar_email") as env:
            self.assertFalse(bater.avisar({}, [], RODADA, POR_MOTIVO))
        env.assert_not_called()

    def test_manda_pro_destinatario_interno(self):
        config = {"notificacao_execucao": {"destinatario": "x@freshlogbr.com"}, "email": {"remetente": "r"}}
        with mock.patch("email_utils.enviar_email", return_value=True) as env:
            self.assertTrue(bater.avisar(config, [NOVA], RODADA, POR_MOTIVO))
        self.assertEqual(env.call_args.args[0], ["x@freshlogbr.com"])
        self.assertEqual(env.call_args.args[3], {"remetente": "r"})

    def test_fallback_e_falha_no_envio_nao_levanta(self):
        with mock.patch("email_utils.enviar_email", side_effect=RuntimeError("smtp fora")) as env:
            self.assertFalse(bater.avisar({}, [NOVA], RODADA, POR_MOTIVO))
        self.assertEqual(env.call_args.args[0], ["hugo@freshlogbr.com"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest batimento.test_bater` → `AttributeError: ... montar_email`.

- [ ] **Step 3: Implementar.** Em `batimento/bater.py`:

Imports (substituir a linha `from batimento import banco, medir  # noqa: E402` e o bloco de imports do topo):

```python
import argparse
import html
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from batimento import banco, consulta, medir  # noqa: E402
```

Constantes e funções novas, antes de `def main`:

```python
LINK_ABA = "https://app.freshhub.com.br/painel/vigia?aba=fechamento"


def montar_email(novas: list[dict], rodada: dict, por_motivo: list[dict]) -> tuple[str, str]:
    """(assunto, html sem envelope). Novidade = divergencia nova nesta
    rodada ou conta que nao fecha (decisao do Hugo, 05/10)."""
    if not rodada["fecha"]:
        assunto = f"[Freshlog] Batimento NÃO FECHA: {rodada['lancados']} lançados"
    else:
        assunto = f"[Freshlog] Batimento: {len(novas)} divergência(s) nova(s)"
    e = html.escape
    selo = "fecha" if rodada["fecha"] else "<b style='color:#EF4444'>NÃO FECHA</b>"
    partes = [
        f"<h2 style='margin:0 0 8px'>Batimento de {e(rodada['rodada_em'][:16])}</h2>",
        f"<p style='margin:0 0 12px'><b>{rodada['lancados']} lançados</b> = {rodada['destino']} destino + "
        f"{rodada['em_andamento']} em andamento + {rodada['divergencia']} divergência — {selo}.</p>",
    ]
    if novas:
        partes.append("<h3 style='margin:16px 0 6px'>Divergências novas</h3>"
                      "<table style='border-collapse:collapse;font-size:13px'>"
                      "<tr><th align='left'>Pedido</th><th align='left'>Embarcador</th>"
                      "<th align='left'>Motivo</th><th align='left'>Evidências</th></tr>")
        for n in novas:
            partes.append(
                f"<tr><td style='padding:3px 10px 3px 0'>{e(n['codigo'])}</td>"
                f"<td style='padding:3px 10px 3px 0'>{e(n.get('embarcador') or '')}</td>"
                f"<td style='padding:3px 10px 3px 0'>{'<b>' if n.get('real') else ''}{e(n['motivo_txt'])}"
                f"{'</b>' if n.get('real') else ''}</td>"
                f"<td style='padding:3px 0;color:#6B7280'>{e(' · '.join(n.get('evidencias') or []))}</td></tr>")
        partes.append("</table>")
    if por_motivo:
        partes.append("<h3 style='margin:16px 0 6px'>Em aberto por motivo</h3><ul style='margin:0;padding-left:18px'>")
        for m in por_motivo:
            partes.append(f"<li>{'<b>' if m['real'] else ''}{e(m['rotulo'])}{'</b>' if m['real'] else ''}: "
                          f"{m['total']}</li>")
        partes.append("</ul>")
    partes.append(f"<p style='margin:16px 0 0'><a href='{LINK_ABA}'>Abrir o Fechamento no painel</a></p>")
    return assunto, "".join(partes)


def avisar(config: dict, novas: list[dict], rodada: dict, por_motivo: list[dict]) -> bool:
    """Manda o e-mail interno se houver novidade. Nunca levanta: o batimento
    ja gravou, falha de e-mail so loga."""
    if not novas and rodada["fecha"]:
        logger.info("Sem divergencia nova e equacao fecha: sem e-mail.")
        return False
    try:
        from email_utils import COR_DESTAQUE, COR_ERRO, envelope_html, enviar_email
        destino = (config.get("notificacao_execucao") or {}).get("destinatario") or "hugo@freshlogbr.com"
        assunto, corpo = montar_email(novas, rodada, por_motivo)
        ok = enviar_email([destino], assunto,
                          envelope_html(corpo, cor_acento=COR_DESTAQUE if rodada["fecha"] else COR_ERRO),
                          config.get("email", {}))
        logger.info(f"E-mail do batimento {'enviado' if ok else 'NAO enviado'} para {destino}: {assunto}")
        return bool(ok)
    except Exception as ex:  # noqa: BLE001
        logger.exception(f"Falha ao mandar o e-mail do batimento: {ex}")
        return False
```

Em `main`, trocar o bloco de gravação por:

```python
    agora = datetime.now()
    conn = banco.conectar(medir.DB_PATH)
    try:
        r = banco.gravar_rodada(conn, pedidos, resumo, id_minimo, agora)
        rodada = consulta.ultima_rodada(conn)
        novas = consulta.novidades(conn, agora.strftime(banco.FMT))
    finally:
        conn.close()
    logger.info(f"Gravado: {r['novos']} novo(s), {r['mudaram']} mudaram, {r['iguais']} iguais, "
                f"{r['congelados']} ja em DESTINO | rodada: {r['totais']} | equacao "
                f"{'FECHA' if r['fecha'] else 'NAO FECHA'} | {len(novas)} divergencia(s) nova(s)")

    with open(medir.CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    avisar(config, novas, rodada, consulta.fechamento(db_path=medir.DB_PATH)["por_motivo"])
    return 0 if r["fecha"] else 1
```

(`banco.FMT` já existe; `rodada_em` gravado = `agora.strftime(FMT)`, então `novidades` acha as linhas com `desde == visto_em == rodada_em`.)

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest batimento.test_bater batimento.test_consulta batimento.test_banco batimento.test_regras batimento.test_medir` → OK; `py -3.11 -m batimento.bater --help` imprime o uso.

---

### Task 5: Aba Fechamento e rota Tratar no painel

**Files:**
- Modify: `painel_agentes/painel_agentes.py:867-889` (rota `vigia_pedidos`) e nova rota logo abaixo
- Modify: `painel_agentes/templates/vigia_pedidos.html`
- Test: `painel_agentes/test_batimento_tela.py` (novo)

**Interfaces:**
- Consumes: `batimento.consulta.fechamento(...)`, `batimento.consulta.tratar(codigo, por, obs)`, `batimento.regras.ROTULOS_DIVERGENCIA`.
- Produces: `GET /vigia?aba=fechamento&motivo=&reais=1&tratadas=1&busca=`; `POST /api/batimento/tratar` body `{"codigo", "obs"}` → `{"ok": true}` ou 400 `{"erro"}`.

- [ ] **Step 1: Teste que falha** — criar `painel_agentes/test_batimento_tela.py`:

```python
# -*- coding: utf-8 -*-
"""
Aba Fechamento do /vigia e POST /api/batimento/tratar (05/10).

    py -3.11 -m unittest painel_agentes.test_batimento_tela
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from batimento import banco  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)


class TestAbaFechamento(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        p = mock.patch.object(banco, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def _gravar(self):
        conn = banco.conectar(self.db)
        banco.gravar_rodada(conn, [
            {"codigo": "PS-1", "caixa": "DIVERGENCIA", "rotulo": "EXPEDIDO_SEM_ENTREGA", "embarcador": "EMB A",
             "evidencias": "stokki=EXPEDIDO"},
            {"codigo": "PS-2", "caixa": "DESTINO", "rotulo": "ENTREGUE", "evidencias": ""},
        ], {"lancados": 2, "equacao_fecha": True}, 1, datetime(2026, 10, 5, 7, 25))
        conn.close()

    def test_sem_rodada_renderiza_vazio(self):
        self._logar("leitura")
        r = self.cliente.get("/vigia?aba=fechamento")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Nenhuma rodada", r.get_data(as_text=True))

    def test_aba_mostra_conta_e_divergencia(self):
        self._gravar()
        self._logar("operador")
        html = self.cliente.get("/vigia?aba=fechamento").get_data(as_text=True)
        for trecho in ("PS-1", "Expedido sem entrega", "EMB A", "fecha", "btn-tratar"):
            self.assertIn(trecho, html)

    def test_leitura_nao_ve_botao_nem_trata(self):
        self._gravar()
        self._logar("leitura")
        html = self.cliente.get("/vigia?aba=fechamento").get_data(as_text=True)
        self.assertNotIn("btn-tratar", html)
        r = self.cliente.post("/api/batimento/tratar", json={"codigo": "PS-1", "obs": "x"})
        self.assertIn(r.status_code, (302, 401, 403))

    def test_operador_trata(self):
        self._gravar()
        self._logar("operador")
        r = self.cliente.post("/api/batimento/tratar", json={"codigo": "PS-1", "obs": "falei com o motorista"},
                              headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        conn = banco.conectar(self.db)
        row = conn.execute("SELECT tratado_por, tratado_obs FROM batimento_pedidos WHERE codigo='PS-1'").fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("teste", "falei com o motorista"))
        r = self.cliente.post("/api/batimento/tratar", json={"codigo": "PS-2", "obs": "x"},
                              headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 400)

    def test_aba_padrao_continua_sendo_pedidos_abertos(self):
        self._logar("leitura")
        r = self.cliente.get("/vigia")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Vigia de pedidos abertos", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
```

Antes de rodar, conferir como `exige_mesma_origem` valida (grep `def exige_mesma_origem` em `painel_agentes.py`) e ajustar o header do teste (`Origin`/`Referer`) para o que ele aceita no `test_client` (host `localhost`). Se o teste de outra tela já faz POST com `exige_mesma_origem` (ex.: `test_usuarios_extras.py`), copiar o header de lá.

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest painel_agentes.test_batimento_tela` → falha (`Nenhuma rodada` ausente, rota 404).

- [ ] **Step 3: Rota.** Em `painel_agentes/painel_agentes.py`, substituir o corpo de `vigia_pedidos` por:

```python
@app.route("/vigia")
@requer_auth(niveis=("total", "operador", "leitura"))
def vigia_pedidos():
    """Vigia de pedidos abertos (Hugo, 28/09): cada pedido aberto com o
    estado, há quanto tempo e se o prazo venceu. Só leitura -- quem
    calcula é vigia/vigiar.py (timer de 15 min). Aba "fechamento" (05/10):
    resultado do batimento (batimento/bater.py, 07h25)."""
    aba = "fechamento" if (request.args.get("aba") or "").lower() == "fechamento" else "abertos"
    if aba == "fechamento":
        from batimento import consulta as batimento_consulta
        from batimento.regras import ROTULOS_DIVERGENCIA
        filtros = {
            "motivo": request.args.get("motivo", ""),
            "reais": request.args.get("reais", "") == "1",
            "tratadas": request.args.get("tratadas", "") == "1",
            "busca": request.args.get("busca", ""),
        }
        try:
            dados = batimento_consulta.fechamento(filtros["motivo"], filtros["reais"], filtros["tratadas"],
                                                  filtros["busca"])
            erro = None
        except Exception as e:
            logging.getLogger(__name__).exception("Falha ao ler o batimento")
            dados = {"linhas": [], "por_motivo": [], "total_abertas": 0, "ultima": None, "rodadas": []}
            erro = str(e)
        return render_template("vigia_pedidos.html", aba=aba, dados=dados, filtros=filtros, erro=erro,
                               motivos=list(ROTULOS_DIVERGENCIA.items()),
                               pode_tratar=g.nivel_acesso in ("total", "operador"))

    from vigia import consulta as vigia_consulta
    filtros = {
        "estado": request.args.get("estado", ""),
        "vencidos": request.args.get("vencidos", "") == "1",
        "busca": request.args.get("busca", ""),
    }
    try:
        dados = vigia_consulta.listar(filtros["estado"], filtros["vencidos"], filtros["busca"])
        erro = None
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao ler o vigia")
        dados = {"linhas": [], "resumo": [], "total": 0, "vencidos": 0,
                 "ultima_rodada": None, "ultima_listagem_stokki": None}
        erro = str(e)
    from vigia.regras import ORDEM, ROTULOS
    return render_template("vigia_pedidos.html", aba=aba, dados=dados, filtros=filtros, erro=erro,
                           estados=[(e, ROTULOS[e]) for e in ORDEM])


@app.route("/api/batimento/tratar", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_batimento_tratar():
    """Marca uma divergência do batimento como tratada (aba Fechamento)."""
    from batimento import consulta as batimento_consulta
    body = request.get_json(force=True) or {}
    try:
        batimento_consulta.tratar(str(body.get("codigo") or ""), session.get("usuario") or g.nivel_acesso,
                                  str(body.get("obs") or ""))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True})
```

(Conferir que `g`, `session`, `jsonify` já estão importados no topo — estão, são usados pelas rotas da Torre.)

- [ ] **Step 4: Template.** Em `painel_agentes/templates/vigia_pedidos.html`:

(a) No `<style>`, acrescentar antes do `@media`:

```css
  .abas { display: flex; gap: 6px; margin: 0 0 16px; border-bottom: 1px solid var(--borda); }
  .abas a { padding: 8px 14px; text-decoration: none; color: var(--texto-suave); font-weight: 700; font-size: 13px;
            border-bottom: 2px solid transparent; margin-bottom: -1px; }
  .abas a.ativa { color: var(--texto); border-bottom-color: var(--acento-real); }
  .conta { background: var(--superficie); border: 1px solid var(--borda); border-radius: 10px;
           padding: 14px 16px; margin: 0 0 18px; font-size: 14px; }
  .conta .numero { font-size: 22px; font-weight: 700; }
  .selo { display: inline-block; padding: 3px 10px; border-radius: 100px; font-size: 11px; font-weight: 700; }
  .selo.ok { background: var(--teste-bg); color: var(--teste-texto); }
  .selo.erro { background: var(--erro); color: #fff; }
  .evid { font-size: 12px; color: var(--texto-suave); }
  h2.secao { font-size: 15px; margin: 22px 0 8px; }
```

(b) Envolver o conteúdo atual: logo depois de `{% block conteudo %}` inserir

```html
<nav class="abas">
  <a href="{{ url_for('vigia_pedidos') }}" class="{{ 'ativa' if aba != 'fechamento' }}">Pedidos abertos</a>
  <a href="{{ url_for('vigia_pedidos', aba='fechamento') }}" class="{{ 'ativa' if aba == 'fechamento' }}">Fechamento</a>
</nav>
{% if aba == 'fechamento' %}
{% include "_vigia_fechamento.html" %}
{% else %}
```

e antes de `{% endblock %}` (o último) inserir `{% endif %}`.

(c) Criar `painel_agentes/templates/_vigia_fechamento.html`:

```html
<h1>Fechamento do batimento</h1>
<p class="sub">Todo pedido lançado na Stokki desde 28/09 precisa ter destino final com documento. Aqui fica o que não fechou. Roda todo dia às 07h25.</p>
{% if erro %}<p style="color:var(--erro);">Falha ao ler o batimento: {{ erro }}</p>{% endif %}

{% set u = dados.ultima %}
<div class="conta">
  {% if u %}
    <span class="numero">{{ u.lancados }}</span> lançados =
    <strong>{{ u.destino }}</strong> com destino +
    <strong>{{ u.em_andamento }}</strong> em andamento +
    <strong>{{ u.divergencia }}</strong> em divergência
    &nbsp;<span class="selo {{ 'ok' if u.fecha else 'erro' }}">{{ 'fecha' if u.fecha else 'NÃO fecha' }}</span>
    <div class="suave" style="font-size:12px;margin-top:6px;">
      Rodada de {{ u.rodada_em[:16] }}
      {% set c = u.resumo.get('cobertura') or {} %}
      {% if c.get('faixa') %} · faixa {{ c.faixa }}: {{ c.listados }} listados, {{ (c.importacao or [])|length }} em Importação, {{ (c.inexistentes or [])|length }} inexistentes{% endif %}
    </div>
  {% else %}
    Nenhuma rodada do batimento ainda.
  {% endif %}
</div>

<div class="cards">
  {% for m in dados.por_motivo %}
  <a class="card {% if m.real %}critico{% endif %} {% if filtros.motivo == m.motivo %}ativo{% endif %}"
     href="{{ url_for('vigia_pedidos', aba='fechamento', motivo=m.motivo) }}">
    <div class="rotulo">{{ m.rotulo }}</div>
    <div class="numeros">{{ m.total }}</div>
    <div class="suave" style="font-size:12px;">{{ 'pede ação' if m.real else 'cobrar comprovante' }}</div>
  </a>
  {% endfor %}
</div>

<form class="filtros" method="GET" action="{{ url_for('vigia_pedidos') }}">
  <input type="hidden" name="aba" value="fechamento">
  <div class="campo">
    <label for="busca">Buscar</label>
    <input type="text" id="busca" name="busca" value="{{ filtros.busca }}" placeholder="PS, embarcador, transportadora">
  </div>
  <div class="campo">
    <label for="motivo">Motivo</label>
    <select id="motivo" name="motivo">
      <option value="">Todos</option>
      {% for chave, rotulo in motivos %}
      <option value="{{ chave }}" {% if filtros.motivo == chave %}selected{% endif %}>{{ rotulo }}</option>
      {% endfor %}
    </select>
  </div>
  <label class="campo check"><input type="checkbox" name="reais" value="1" {% if filtros.reais %}checked{% endif %}> Só as que pedem ação</label>
  <label class="campo check"><input type="checkbox" name="tratadas" value="1" {% if filtros.tratadas %}checked{% endif %}> Incluir tratadas</label>
  <div class="campo"><button type="submit" class="botao">Filtrar</button></div>
  {% if filtros.busca or filtros.motivo or filtros.reais or filtros.tratadas %}
  <div class="campo"><a class="botao" style="background:var(--texto-suave);" href="{{ url_for('vigia_pedidos', aba='fechamento') }}">Limpar</a></div>
  {% endif %}
</form>

<div class="tabela-wrap">
<table>
  <thead>
    <tr><th>Pedido</th><th>Motivo</th><th>Desde</th><th>Embarcador / transportadora</th><th>Evidências</th><th>Tratativa</th></tr>
  </thead>
  <tbody>
    {% for l in dados.linhas %}
    <tr class="{% if l.real and l.vencida and not l.tratada %}vencido{% endif %}">
      <td class="mono"><a href="{{ url_for('historico_tratativas', busca=l.codigo) }}">#{{ l.codigo }}</a></td>
      <td><span class="pilula {% if l.real %}vencido{% endif %}">{{ l.motivo_txt }}</span></td>
      <td class="mono">{{ l.desde[:16] }}{% if l.real and l.vencida and not l.tratada %}<div class="suave">mais de 1 dia útil</div>{% endif %}</td>
      <td>{{ l.embarcador or '—' }}<div class="suave">{{ l.transportadora or '' }}</div></td>
      <td class="evid">{{ l.evidencias|join(' · ') }}</td>
      <td>
        {% if l.tratada %}
          <div>{{ l.tratado_obs }}</div><div class="suave">{{ l.tratado_por }} · {{ l.tratado_em[:16] }}</div>
        {% elif pode_tratar %}
          <button type="button" class="botao btn-tratar" data-codigo="{{ l.codigo }}">Tratar</button>
        {% else %}<span class="suave">—</span>{% endif %}
      </td>
    </tr>
    {% else %}
    <tr><td colspan="6" class="suave">Nenhuma divergência nesse filtro.</td></tr>
    {% endfor %}
  </tbody>
</table>
</div>

{% if dados.rodadas %}
<h2 class="secao">Últimas rodadas</h2>
<div class="tabela-wrap">
<table>
  <thead><tr><th>Rodada</th><th>Lançados</th><th>Destino</th><th>Em andamento</th><th>Divergência</th><th>Conta</th></tr></thead>
  <tbody>
    {% for r in dados.rodadas %}
    <tr><td class="mono">{{ r.rodada_em[:16] }}</td><td>{{ r.lancados }}</td><td>{{ r.destino }}</td>
        <td>{{ r.em_andamento }}</td><td>{{ r.divergencia }}</td>
        <td><span class="selo {{ 'ok' if r.fecha else 'erro' }}">{{ 'fecha' if r.fecha else 'NÃO fecha' }}</span></td></tr>
    {% endfor %}
  </tbody>
</table>
</div>
{% endif %}

{% if pode_tratar %}
<script>
document.querySelectorAll('.btn-tratar').forEach(function (b) {
  b.addEventListener('click', async function () {
    var obs = prompt('O que foi feito com ' + b.dataset.codigo + '?');
    if (obs === null || !obs.trim()) return;
    b.disabled = true;
    var r = await fetch('{{ url_for("api_batimento_tratar") }}', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({codigo: b.dataset.codigo, obs: obs.trim()})
    });
    if (r.ok) { location.reload(); return; }
    var j = {}; try { j = await r.json(); } catch (e) {}
    alert(j.erro || ('Falha ao tratar (' + r.status + ')'));
    b.disabled = false;
  });
});
</script>
{% endif %}
```

- [ ] **Step 5: Rodar e ver passar** — `py -3.11 -m unittest painel_agentes.test_batimento_tela` → OK. Depois `py -3.11 -m unittest painel_agentes.test_clientes_agenda_tela painel_agentes.test_inicio_tela` (telas vizinhas continuam OK).

---

### Task 6: Torre puxa o batimento

**Files:**
- Modify: `painel_agentes/torre_controle.py:1462-1470`
- Test: `painel_agentes/test_batimento_tela.py` (acrescentar)

**Interfaces:**
- Consumes: `batimento.consulta.excecoes_torre(data_iso)`.

- [ ] **Step 1: Teste que falha** — acrescentar em `painel_agentes/test_batimento_tela.py`:

```python
class TestTorreBatimento(unittest.TestCase):
    def test_torre_chama_o_batimento(self):
        src = (_AQUI / "torre_controle.py").read_text(encoding="utf-8")
        self.assertIn("from batimento.consulta import excecoes_torre as batimento_excecoes", src)
```

Se existir em `painel_agentes/test_torre_tratar.py` um jeito pronto de chamar `_montar_excecoes(data_iso)` com banco sintético, usar esse jeito e conferir que o item com `id` `batimento:PS-1:...` aparece em `ativas`; senão, ficar com a checagem acima (o comportamento real é provado no Step 4 da Task 7).

- [ ] **Step 2: Rodar e ver falhar** — `py -3.11 -m unittest painel_agentes.test_batimento_tela` → `AssertionError` no import.

- [ ] **Step 3: Implementar** — em `torre_controle.py`, logo depois do `except` do bloco do vigia (linha ~1470):

```python
    # Batimento (05/10): divergencia REAL parada ha mais de 1 dia util, um
    # item por pedido, e um critico se a ultima rodada nao fechou. Só lê
    # batimento_* -- quem calcula é o timer das 07h25.
    try:
        from batimento.consulta import excecoes_torre as batimento_excecoes
        for x in batimento_excecoes(data_iso):
            excecoes.append({**x, "_epoch": 0.0})
    except Exception as e:
        logger.warning(f"[torre] Batimento indisponível: {e}")
```

- [ ] **Step 4: Rodar e ver passar** — `py -3.11 -m unittest painel_agentes.test_batimento_tela painel_agentes.test_torre_tratar` → OK.

---

### Task 7: Docs, verificação visual e prova

**Files:**
- Modify: `DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md` (Status e "A implementar → Saída")
- Modify: `MAPA_DO_SISTEMA.txt` (bloco `batimento/`)

- [ ] **Step 1: Doc.** No `DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md`, trocar a linha de Status por "Status (05/10): job diário no ar (07h25); saída (aba Fechamento, Torre, e-mail) conforme `docs/superpowers/specs/2026-10-05-batimento-saida-design.md`." e, em "A implementar", substituir o item "Saída:" por um item em "Feitas" descrevendo: aba `/vigia?aba=fechamento` com Tratar (total/operador), Torre só divergências reais com mais de 1 dia útil (+ crítico se não fecha), e-mail só com novidade para `notificacao_execucao.destinatario`.

- [ ] **Step 2: Mapa.** No bloco `batimento/` do `MAPA_DO_SISTEMA.txt`, acrescentar:

```
   consulta.py aba Fechamento (/vigia?aba=fechamento), excecoes_torre
               (só divergência real > 1 dia útil + crítico se não fecha),
               novidades pro e-mail, tratar (POST /api/batimento/tratar).
```

e no `bater.py` acrescentar "e-mail interno só com divergência nova ou conta que não fecha".

- [ ] **Step 3: Todos os testes** — da raiz do worktree:
  - `py -3.11 -m unittest batimento.test_regras batimento.test_banco batimento.test_consulta batimento.test_bater batimento.test_medir`
  - `py -3.11 -m unittest painel_agentes.test_batimento_tela painel_agentes.test_torre_tratar painel_agentes.test_inicio_tela`
  - `py -3.11 -m py_compile` em todos os `.py` editados.

- [ ] **Step 4: Playwright local** (memória `feedback_testar_painel_playwright_local`: `/login?proximo=`, bloquear `/api/torre/**`, cookie Secure off, `tour_visto`). Copiar as tabelas `batimento_*` não existem no banco local congelado: gerar uma rodada sintética num banco temporário apontando `batimento.banco.DB_PATH` para ele, subir o painel em `127.0.0.1:8099` (`py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"` de dentro de `painel_agentes/`, com o patch de `DB_PATH` num script de scratchpad), abrir `/vigia?aba=fechamento` em 1366×800 e 390×844, tirar screenshot, clicar "Tratar" (preencher o `prompt`) e conferir que a linha some e reaparece com "Incluir tratadas". Abrir `/torre` e conferir o item "Batimento".

- [ ] **Step 5: Commit (só com autorização do Hugo)**

```bash
git add batimento/regras.py batimento/banco.py batimento/consulta.py batimento/bater.py \
  batimento/test_regras.py batimento/test_banco.py batimento/test_consulta.py batimento/test_bater.py \
  painel_agentes/painel_agentes.py painel_agentes/torre_controle.py \
  painel_agentes/templates/vigia_pedidos.html painel_agentes/templates/_vigia_fechamento.html \
  painel_agentes/test_batimento_tela.py DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md MAPA_DO_SISTEMA.txt \
  docs/superpowers/specs/2026-10-05-batimento-saida-design.md docs/superpowers/plans/2026-10-05-batimento-saida.md
git commit -m "Batimento: aba Fechamento no /vigia, divergencia real na Torre e e-mail de novidade"
```

Deploy (quando o Hugo pedir): `vps_ops.py deploy --restart painel-agentes` (sem `--units`); prova: `curl` em `/vigia?aba=fechamento` na 8071 logado ou `vps_ler.py log painel-agentes --grep Traceback` depois de abrir a aba, e `venv/bin/python -c "from batimento import consulta; print(consulta.excecoes_torre('2026-10-06'))"` como www-data.
