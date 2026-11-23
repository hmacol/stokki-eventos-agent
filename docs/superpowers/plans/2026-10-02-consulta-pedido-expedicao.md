# Consulta de pedido na Expedição — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tela `/expedicao/consulta` onde o operador digita um código de pedido e recebe, cruzando Vuupt e Stokki ao vivo, o veredito "deveria estar no galpão / já foi entregue / saiu em outra rota / retirado / cancelado / não encontrado".

**Architecture:** Módulo novo `painel_agentes/consulta_expedicao.py` com uma função pura `decidir_veredito` (a tabela de regras, testada sem rede) e `consultar` (busca Vuupt + Stokki só leitura e chama a pura). Duas rotas no `painel_agentes.py` (página + API JSON) e um template responsivo que chama a API por `fetch`. Item novo no menu lateral.

**Tech Stack:** Python 3.11, Flask, Jinja2, unittest + `unittest.mock`; Vuupt via `VuuptClient`/`rotas_client.buscar_rota`; Stokki via `StokkiSession` + helpers de `pedidos_parados_triagem`.

**Spec:** `docs/superpowers/specs/2026-10-02-consulta-pedido-expedicao-design.md`

## Global Constraints

- Python sempre `py -3.11` (local). Testes com `py -3.11 -m unittest ...`, rodados **da raiz do worktree** (padrão de `painel_agentes/test_inicio_tela.py`). Não misturar com testes de `roteirizacao`/`nucleo` no mesmo comando.
- A consulta é **só leitura**: nunca chamar `verificar_na_stokki`, `verificar_na_vuupt`, `classificar` nem nada que grave em `dados.db`, Stokki ou Vuupt.
- Níveis com acesso: `("total", "operador", "expedicao", "galpao")` — no `@requer_auth` das duas rotas **e** no `niveis` do item do menu (os dois lugares espelham).
- Templates: `url_for()` sempre, nunca caminho absoluto (o painel roda atrás de `/painel` via ProxyFix).
- Idioma: português em código, comentários, textos e commits (`Área: o que mudou e por quê`).
- Commit/push só quando o Hugo pedir. Só `git add` dos arquivos desta tarefa. Nunca `git add -A`.
- O working tree principal (`C:\agente_stokki_eventos`) tem `painel_agentes.py` e `_menu_lateral_nav.html` modificados por outra sessão e está 22 commits atrás de `origin/master`: **trabalhar num worktree a partir de `origin/master`** (Task 0).

## Review Focus

1. **Código digitado em formatos variados** (`38123`, `ps 38123`, `#PS-38123`, `PS-38123-r1`, com espaços): deve normalizar para `PS-38123[-R1]`; lixo (`abc`, vazio, `123`) dá 400 com mensagem clara, nunca 500. → testes em Task 1.
2. **Stokki ocupada no meio da separação** (agente rodando às 08–19h a cada 30 min): o operador ainda recebe veredito pela Vuupt + aviso, não um erro. → teste em Task 2.
3. **Serviço na Vuupt mas sem rota (`not_assigned`) e Stokki "Enviado" sem transportadora de retirada**: veredito verde com faixa de divergência (não "entregue"). → teste em Task 1.
4. **Sem serviço na Vuupt e Stokki "Enviado"** (pedido de TERCEIROS que saiu por transportadora): veredito "Saiu" com o nome da transportadora, não "deveria estar no galpão". → teste em Task 1.
5. **Rota antiga que nunca foi iniciada** (serviço `accepted` numa rota cujo status já não é "não iniciada"): veredito "Saiu em outra rota", com motorista. → teste em Task 1.

---

### Task 0: Worktree isolado

**Files:** nenhum (ambiente)

- [ ] **Step 1: Criar worktree a partir de `origin/master`**

```bash
cd /c/agente_stokki_eventos
git fetch origin
git worktree add -b consulta-pedido-expedicao ../agente_stokki_eventos-consulta origin/master
cp config.yaml ../agente_stokki_eventos-consulta/config.yaml
```

(`config.yaml` é gitignored; a cópia é só pra os testes do painel carregarem config. Nunca commitar.)

- [ ] **Step 2: Conferir que a suíte de referência passa no worktree**

Run (da raiz do worktree): `py -3.11 -m unittest painel_agentes.test_inicio_tela -v`
Expected: OK.

Todas as tarefas a seguir trabalham dentro de `../agente_stokki_eventos-consulta`.

---

### Task 1: Regra pura do veredito

**Files:**
- Create: `painel_agentes/consulta_expedicao.py`
- Test: `painel_agentes/test_consulta_expedicao.py`

**Interfaces:**
- Produces:
  - `normalizar_codigo(texto: str) -> str | None` — `"ps 38123-r1"` → `"PS-38123-R1"`; inválido → `None`.
  - `decidir_veredito(servico: dict | None, rota: dict | None, stokki: dict, agora: datetime) -> dict`
    - `servico`: serviço Vuupt cru (o mais recente da cadeia) ou `None`.
    - `rota`: `{"id", "nome", "status", "iniciada": bool, "motorista": str | None, "placa": str | None}` ou `None`.
    - `stokki`: `{"estado": "ok" | "inexistente" | "nao_conferida", "status": str, "transportadora": str, "retira": bool}` (`status`/`transportadora`/`retira` só com `estado == "ok"`).
    - `agora`: datetime com fuso de São Paulo.
    - retorno: `{"veredito", "titulo", "contexto", "divergencia", "aviso", "stokki_bruto", "vuupt_bruto"}`; `veredito` ∈ `{"galpao", "entregue", "saiu", "retirado", "cancelado", "nao_encontrado"}`; `divergencia`/`aviso` são `str | None`.
  - Constantes `TITULOS: dict[str, str]`, `AVISO_STOKKI_NAO_CONFERIDA: str`.

- [ ] **Step 1: Escrever os testes que falham**

`painel_agentes/test_consulta_expedicao.py`:

```python
# -*- coding: utf-8 -*-
"""
Consulta de pedido na Expedicao (02/10): regra do veredito, busca nas
duas fontes e rotas do painel.

    py -3.11 -m unittest painel_agentes.test_consulta_expedicao
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import consulta_expedicao as ce  # noqa: E402

SP = ZoneInfo("America/Sao_Paulo")
AGORA = datetime(2026, 10, 2, 10, 0, tzinfo=SP)

STOKKI_AGUARDANDO = {"estado": "ok", "status": "Aguardando Transportador", "transportadora": "FRESHLOG", "retira": False}
STOKKI_ENVIADO = {"estado": "ok", "status": "Enviado", "transportadora": "FRESHLOG", "retira": False}
STOKKI_NAO_CONFERIDA = {"estado": "nao_conferida"}
ROTA_PARADA = {"id": 9, "nome": "Planejamento - 02/10 - #3", "status": "accepted", "iniciada": False,
               "motorista": "Iago Mendes", "placa": "ABC1D23"}
ROTA_RODANDO = {**ROTA_PARADA, "status": "started", "iniciada": True}


class TestNormalizarCodigo(unittest.TestCase):

    def test_formatos_aceitos(self):
        for entrada, esperado in (
            ("38123", "PS-38123"), ("PS-38123", "PS-38123"), ("#PS-38123", "PS-38123"),
            ("ps 38123", "PS-38123"), (" PS.38123 ", "PS-38123"), ("PS-38123-r1", "PS-38123-R1"),
            ("38123-R1-R1", "PS-38123-R1-R1"),
        ):
            self.assertEqual(ce.normalizar_codigo(entrada), esperado, entrada)

    def test_invalidos(self):
        for entrada in ("", "   ", "abc", "123", "PS-", None):
            self.assertIsNone(ce.normalizar_codigo(entrada), entrada)


class TestDecidirVeredito(unittest.TestCase):

    def _v(self, servico, rota=None, stokki=STOKKI_AGUARDANDO):
        return ce.decidir_veredito(servico, rota, stokki, AGORA)

    def test_pool_sem_agendamento(self):
        r = self._v({"status": "not_assigned"})
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("sem rota", r["contexto"].lower())
        self.assertIsNone(r["divergencia"])

    def test_pool_agendado(self):
        r = self._v({"status": "not_assigned", "scheduled_start": "2026-10-05 08:00:00"})
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("05/10", r["contexto"])

    def test_rota_nao_iniciada_mostra_rota_e_motorista(self):
        r = self._v({"status": "accepted", "route_id": 9}, ROTA_PARADA)
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("#3", r["contexto"])
        self.assertIn("Iago Mendes", r["contexto"])

    def test_on_route_saiu(self):
        r = self._v({"status": "on_route", "route_id": 9}, ROTA_RODANDO)
        self.assertEqual(r["veredito"], "saiu")
        self.assertIn("ABC1D23", r["contexto"])

    def test_rota_antiga_ja_nao_parada_conta_como_saiu(self):
        r = self._v({"status": "accepted", "route_id": 9}, {**ROTA_PARADA, "status": "finished", "iniciada": True})
        self.assertEqual(r["veredito"], "saiu")

    def test_entregue(self):
        r = self._v({"status": "done", "status_done": "success", "completed_at": "2026-10-02 12:30:00"},
                    stokki=STOKKI_ENVIADO)
        self.assertEqual(r["veredito"], "entregue")
        self.assertIn("02/10 09:30", r["contexto"])  # completed_at vem em UTC
        self.assertIsNone(r["divergencia"])

    def test_insucesso_deveria_ter_voltado(self):
        with mock.patch.object(ce, "texto_do_motivo", return_value="Cliente ausente"):
            r = self._v({"status": "done", "status_done": "failed", "failed_reason_id": 7,
                         "completed_at": "2026-10-01 18:00:00"})
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("insucesso", r["contexto"].lower())
        self.assertIn("Cliente ausente", r["contexto"])

    def test_cancelado_na_stokki_vence_tudo(self):
        r = self._v({"status": "on_route"}, ROTA_RODANDO,
                    {"estado": "ok", "status": "Cancelado", "transportadora": "", "retira": False})
        self.assertEqual(r["veredito"], "cancelado")

    def test_cancelado_na_vuupt(self):
        r = self._v({"status": "canceled"})
        self.assertEqual(r["veredito"], "cancelado")

    def test_retirado(self):
        r = self._v({"status": "done", "status_done": "success"}, stokki={
            "estado": "ok", "status": "Enviado", "transportadora": "CLIENTE RETIRA", "retira": True})
        self.assertEqual(r["veredito"], "retirado")
        self.assertIn("CLIENTE RETIRA", r["contexto"])

    def test_sem_vuupt_stokki_aguardando(self):
        r = self._v(None)
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("roteiriza", r["contexto"].lower())

    def test_sem_vuupt_stokki_enviado_por_transportadora(self):
        r = self._v(None, stokki={"estado": "ok", "status": "Enviado", "transportadora": "JADLOG", "retira": False})
        self.assertEqual(r["veredito"], "saiu")
        self.assertIn("JADLOG", r["contexto"])

    def test_nao_encontrado(self):
        r = self._v(None, stokki={"estado": "inexistente"})
        self.assertEqual(r["veredito"], "nao_encontrado")

    def test_sem_vuupt_e_stokki_nao_conferida(self):
        r = self._v(None, stokki=STOKKI_NAO_CONFERIDA)
        self.assertEqual(r["veredito"], "nao_encontrado")
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)

    def test_divergencia_pool_mas_stokki_enviado(self):
        r = self._v({"status": "not_assigned"}, stokki=STOKKI_ENVIADO)
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("Enviado", r["divergencia"])

    def test_divergencia_entregue_ha_mais_de_24h_sem_expedir(self):
        r = self._v({"status": "done", "status_done": "success", "completed_at": "2026-09-30 15:00:00"})
        self.assertEqual(r["veredito"], "entregue")
        self.assertIn("Aguardando Transportador", r["divergencia"])

    def test_entregue_ha_pouco_sem_expedir_nao_e_divergencia(self):
        r = self._v({"status": "done", "status_done": "success", "completed_at": "2026-10-02 11:00:00"})
        self.assertIsNone(r["divergencia"])

    def test_stokki_nao_conferida_decide_pela_vuupt_com_aviso(self):
        r = self._v({"status": "accepted", "route_id": 9}, ROTA_PARADA, STOKKI_NAO_CONFERIDA)
        self.assertEqual(r["veredito"], "galpao")
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)
        self.assertIn("não conferida", r["stokki_bruto"])

    def test_linhas_brutas(self):
        r = self._v({"status": "accepted", "route_id": 9}, ROTA_PARADA)
        self.assertEqual(r["stokki_bruto"], "Stokki: Aguardando Transportador · FRESHLOG")
        self.assertIn("accepted", r["vuupt_bruto"])
        self.assertIn("#3", r["vuupt_bruto"])
        self.assertEqual(r["titulo"], ce.TITULOS["galpao"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_consulta_expedicao -v`
Expected: erro `ModuleNotFoundError: No module named 'consulta_expedicao'`.

- [ ] **Step 3: Implementar a parte pura**

`painel_agentes/consulta_expedicao.py`:

```python
# -*- coding: utf-8 -*-
"""
consulta_expedicao.py

Consulta de UM pedido pra separação da rota (Hugo, 02/10): o operador
não acha o pedido no galpão e quer saber se ele deveria estar ali.
Cruza Vuupt e Stokki AO VIVO e devolve um veredito pronto.

A Vuupt manda (é o registro físico do motorista); a Stokki complementa
e desempata. Regras: docs/superpowers/specs/2026-10-02-consulta-pedido-
expedicao-design.md.

Só leitura: não usa verificar_na_stokki/verificar_na_vuupt da triagem
de Pedidos Parados porque elas GRAVAM classificação.
"""
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", _RAIZ / "insucesso_entrega"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pedidos_parados_triagem as triagem
from motivos_falha import texto_do_motivo

logger = logging.getLogger(__name__)

FUSO_LOCAL = ZoneInfo("America/Sao_Paulo")

TITULOS = {
    "galpao": "Deveria estar no galpão",
    "entregue": "Já foi entregue",
    "saiu": "Saiu em outra rota",
    "retirado": "Retirado",
    "cancelado": "Cancelado — não procurar",
    "nao_encontrado": "Pedido não encontrado",
}
AVISO_STOKKI_NAO_CONFERIDA = "Stokki não conferida (sessão ocupada por um agente) — tente de novo em 1 min."

# Entregue há mais que isso e ainda sem "Enviado" na Stokki = divergência
# (mesmo corte da checagem das 07h15, verificar_entregues_nao_expedidos.py).
HORAS_DIVERGENCIA_EXPEDICAO = 24

_RE_CODIGO = re.compile(r"^#?(?:PS[.\-_]*)?(\d{4,6})((?:-[RC]\d+)*)$")


def normalizar_codigo(texto) -> str | None:
    """'ps 38123-r1' -> 'PS-38123-R1'; sem código reconhecível -> None."""
    compacto = re.sub(r"\s+", "", str(texto or "")).upper()
    m = _RE_CODIGO.match(compacto)
    if not m:
        return None
    return f"PS-{m.group(1)}{m.group(2)}"


def _vuupt_local(ts) -> datetime | None:
    """completed_at da Vuupt vem em UTC sem fuso -> hora de São Paulo."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(FUSO_LOCAL)


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%d/%m %H:%M") if dt else "data não informada"


def _quem(rota: dict | None) -> str:
    if not rota:
        return ""
    partes = [rota.get("motorista") or "motorista não identificado"]
    if rota.get("placa"):
        partes.append(rota["placa"])
    return " · ".join(partes)


def _bruto_stokki(stokki: dict) -> str:
    estado = stokki.get("estado")
    if estado == "ok":
        texto = f"Stokki: {stokki.get('status') or '?'}"
        if stokki.get("transportadora"):
            texto += f" · {stokki['transportadora']}"
        return texto
    if estado == "inexistente":
        return "Stokki: pedido não existe"
    return "Stokki: não conferida"


def _bruto_vuupt(servico: dict | None, rota: dict | None) -> str:
    if not servico:
        return "Vuupt: nenhum serviço com esse código"
    texto = f"Vuupt: {servico.get('status') or '?'}"
    if servico.get("status_done"):
        texto += f"/{servico['status_done']}"
    if rota:
        texto += f" · {rota.get('nome') or 'rota ' + str(rota.get('id'))}"
    return texto


def _veredito_vuupt(servico: dict, rota: dict | None, agora: datetime) -> tuple[str, str]:
    """(veredito, contexto) só pela Vuupt -- linhas da tabela do spec."""
    status = servico.get("status") or ""
    if status == "done":
        quando = _fmt(_vuupt_local(servico.get("completed_at")))
        if servico.get("status_done") == "failed":
            motivo = texto_do_motivo(servico.get("failed_reason_id"))
            return "galpao", f"Voltou de insucesso em {quando} ({motivo})"
        quem = _quem(rota)
        return "entregue", f"Entregue em {quando}" + (f" — {quem}" if quem else "")
    if status == "canceled":
        return "cancelado", "Serviço cancelado na Vuupt"
    if status == "on_route" or (rota and rota.get("iniciada")):
        return "saiu", f"{rota.get('nome') if rota else 'Rota em andamento'} — {_quem(rota) or 'motorista não identificado'}"
    if rota:
        return "galpao", f"Na {rota.get('nome')} ({_quem(rota)}), rota ainda não saiu"
    data_ag = triagem._data_agendamento_local(servico.get("scheduled_start"))
    if data_ag and data_ag > agora.date():
        return "galpao", f"Sem rota, agendado pra {data_ag:%d/%m}"
    return "galpao", "Sem rota (no pool da roteirização)"


def decidir_veredito(servico: dict | None, rota: dict | None, stokki: dict, agora: datetime) -> dict:
    estado_stokki = stokki.get("estado", "nao_conferida")
    status_stokki = stokki.get("status") or ""
    cancelado_stokki = estado_stokki == "ok" and re.search(r"cancel", status_stokki, re.IGNORECASE)
    enviado_stokki = estado_stokki == "ok" and re.search(r"enviado", status_stokki, re.IGNORECASE)

    divergencia = None
    if cancelado_stokki:
        veredito, contexto = "cancelado", "Cancelado na Stokki"
    elif enviado_stokki and stokki.get("retira"):
        quando = _vuupt_local((servico or {}).get("completed_at"))
        contexto = f"Retirado no galpão ({stokki.get('transportadora')})"
        veredito, contexto = "retirado", contexto + (f" em {_fmt(quando)}" if quando else "")
    elif servico is None:
        if estado_stokki == "ok" and enviado_stokki:
            veredito, contexto = "saiu", f"Enviado pela transportadora {stokki.get('transportadora') or '?'} (sem serviço na Vuupt)"
        elif estado_stokki == "ok":
            veredito, contexto = "galpao", "Ainda não foi pra roteirização (sem serviço na Vuupt)"
        elif estado_stokki == "inexistente":
            veredito, contexto = "nao_encontrado", "Não existe na Vuupt nem na Stokki — confira o código"
        else:
            veredito, contexto = "nao_encontrado", "Não está na Vuupt e a Stokki não pôde ser conferida"
    else:
        veredito, contexto = _veredito_vuupt(servico, rota, agora)
        if veredito == "galpao" and enviado_stokki:
            divergencia = f"Stokki diz {status_stokki}"
        elif veredito == "entregue" and estado_stokki == "ok" and not enviado_stokki:
            entregue_em = _vuupt_local(servico.get("completed_at"))
            if entregue_em and agora - entregue_em > timedelta(hours=HORAS_DIVERGENCIA_EXPEDICAO):
                divergencia = f"Stokki diz {status_stokki} (entregue há mais de {HORAS_DIVERGENCIA_EXPEDICAO}h e não expedido)"

    return {
        "veredito": veredito,
        "titulo": TITULOS[veredito],
        "contexto": contexto,
        "divergencia": divergencia,
        "aviso": AVISO_STOKKI_NAO_CONFERIDA if estado_stokki == "nao_conferida" else None,
        "stokki_bruto": _bruto_stokki(stokki),
        "vuupt_bruto": _bruto_vuupt(servico, rota),
    }
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest painel_agentes.test_consulta_expedicao -v`
Expected: todos OK. Se `test_entregue` falhar na hora, conferir que `completed_at` sem fuso está sendo tratado como UTC (12:30 UTC = 09:30 SP).

- [ ] **Step 5: Commit (só quando o Hugo autorizar commits nesta tarefa; senão seguir sem commitar)**

```bash
git add painel_agentes/consulta_expedicao.py painel_agentes/test_consulta_expedicao.py
git commit -m "Expedicao: regra do veredito da consulta de pedido (Vuupt manda, Stokki desempata)"
```

---

### Task 2: Busca ao vivo nas duas fontes (`consultar`)

**Files:**
- Modify: `painel_agentes/consulta_expedicao.py` (acrescentar no fim)
- Test: `painel_agentes/test_consulta_expedicao.py` (acrescentar classe)

**Interfaces:**
- Consumes: `normalizar_codigo`, `decidir_veredito` (Task 1); `triagem._vuupt()`, `triagem._servico_mais_recente_da_cadeia(vuupt, servico)`, `triagem._lock_consulta_stokki`, `triagem._painel_tem_execucao_rodando()`, `triagem._status_e_transportadora_stokki(sessao, id_stokki) -> {"status","transportadora"} | None`, `triagem._tipo_retira(nome, catalogo) -> str | None`, `triagem._catalogo_transportadoras()`; `expedicao._rota_do_corpo`, `expedicao._STATUS_ROTA_NAO_INICIADA`, `expedicao._catalogo_motoristas() -> {agent_id: MotoristaPreferencias(nome, placa)}`, `expedicao._carregar_config()`; `rotas_client.buscar_rota(token, route_id, include=[...])`.
- Produces:
  - `consultar(codigo_digitado: str) -> dict` — o dict de `decidir_veredito` + `"codigo"`. Lança `ValueError` (código inválido) ou `ConsultaIndisponivel` (Vuupt falhou).
  - `class ConsultaIndisponivel(Exception)`.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `painel_agentes/test_consulta_expedicao.py`, antes do `if __name__`:

```python
class _VuuptFalso:
    def __init__(self, servico=None, erro=None):
        self.servico, self.erro, self.codigos = servico, erro, []

    def buscar_servico_por_code(self, codigo):
        self.codigos.append(codigo)
        if self.erro:
            raise self.erro
        return self.servico


class TestConsultar(unittest.TestCase):

    def setUp(self):
        self.vuupt = _VuuptFalso({"id": 1, "status": "accepted", "route_id": 9})
        patches = [
            mock.patch.object(ce.triagem, "_vuupt", side_effect=lambda: self.vuupt),
            mock.patch.object(ce.triagem, "_servico_mais_recente_da_cadeia", side_effect=lambda v, s: s),
            mock.patch.object(ce, "_carregar_config", return_value={"vuupt_api": {"token": "t"}}),
            mock.patch.object(ce, "buscar_rota", return_value={"route": {
                "id": 9, "name": "Planejamento - 02/10 - #3", "status": "accepted", "agent_id": 50191}}),
            mock.patch.object(ce, "_catalogo_motoristas", return_value={
                50191: mock.Mock(nome="Iago Mendes", placa="ABC1D23")}),
            mock.patch.object(ce.triagem, "_painel_tem_execucao_rodando", return_value=False),
            mock.patch("stokki.auth.StokkiSession", return_value=object()),
            mock.patch.object(ce.triagem, "_status_e_transportadora_stokki",
                              return_value={"status": "Aguardando Transportador", "transportadora": "FRESHLOG"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_fluxo_completo_rota_parada(self):
        r = ce.consultar(" ps 38123 ")
        self.assertEqual(r["codigo"], "PS-38123")
        self.assertEqual(self.vuupt.codigos, ["PS-38123"])
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("Iago Mendes", r["contexto"])
        self.assertIsNone(r["aviso"])

    def test_stokki_recebe_so_os_digitos(self):
        ce.consultar("PS-38123-R1")
        self.assertEqual(ce.triagem._status_e_transportadora_stokki.call_args.args[1], "38123")

    def test_codigo_invalido(self):
        with self.assertRaises(ValueError):
            ce.consultar("abc")

    def test_vuupt_fora(self):
        self.vuupt.erro = RuntimeError("timeout")
        with self.assertRaises(ce.ConsultaIndisponivel):
            ce.consultar("38123")

    def test_agente_rodando_nao_abre_stokki(self):
        with mock.patch.object(ce.triagem, "_painel_tem_execucao_rodando", return_value=True):
            r = ce.consultar("38123")
        ce.triagem._status_e_transportadora_stokki.assert_not_called()
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)
        self.assertEqual(r["veredito"], "galpao")

    def test_lock_ocupado_nao_abre_stokki(self):
        ce.triagem._lock_consulta_stokki.acquire()
        try:
            r = ce.consultar("38123")
        finally:
            ce.triagem._lock_consulta_stokki.release()
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)

    def test_erro_na_stokki_vira_nao_conferida_e_solta_o_lock(self):
        with mock.patch.object(ce.triagem, "_status_e_transportadora_stokki", side_effect=RuntimeError("401")):
            r = ce.consultar("38123")
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)
        self.assertTrue(ce.triagem._lock_consulta_stokki.acquire(blocking=False))
        ce.triagem._lock_consulta_stokki.release()

    def test_stokki_inexistente(self):
        self.vuupt.servico = None
        with mock.patch.object(ce.triagem, "_status_e_transportadora_stokki", return_value=None):
            r = ce.consultar("38123")
        self.assertEqual(r["veredito"], "nao_encontrado")

    def test_retira_so_consulta_catalogo_quando_enviado(self):
        with mock.patch.object(ce.triagem, "_catalogo_transportadoras") as cat:
            ce.consultar("38123")
        cat.assert_not_called()

    def test_enviado_cliente_retira(self):
        self.vuupt.servico = {"id": 1, "status": "done", "status_done": "success"}
        with mock.patch.object(ce.triagem, "_status_e_transportadora_stokki",
                               return_value={"status": "Enviado", "transportadora": "CLIENTE RETIRA"}), \
             mock.patch.object(ce.triagem, "_catalogo_transportadoras", return_value=None):
            r = ce.consultar("38123")
        self.assertEqual(r["veredito"], "retirado")

    def test_servico_sem_rota_nao_busca_rota(self):
        self.vuupt.servico = {"id": 1, "status": "not_assigned"}
        ce.consultar("38123")
        ce.buscar_rota.assert_not_called()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_consulta_expedicao -v`
Expected: os testes de `TestConsultar` falham com `AttributeError` (`consultar`, `buscar_rota`, `_catalogo_motoristas`, `_carregar_config` não existem em `ce`).

- [ ] **Step 3: Implementar**

No topo de `consulta_expedicao.py`, junto dos imports existentes:

```python
from expedicao import _STATUS_ROTA_NAO_INICIADA, _carregar_config, _catalogo_motoristas, _rota_do_corpo
from rotas_client import buscar_rota
```

No fim do arquivo:

```python
class ConsultaIndisponivel(Exception):
    """Vuupt não respondeu: sem ela não há veredito (a Stokki sozinha não
    diz se o pedido está num caminhão)."""


def _resumo_rota(token: str, route_id: int) -> dict:
    corpo = _rota_do_corpo(buscar_rota(token, route_id, include=["agent"]))
    status = corpo.get("status") or ""
    agent_id = corpo.get("agent_id")
    motorista = placa = None
    if agent_id:
        try:
            m = _catalogo_motoristas().get(agent_id)
        except Exception as e:  # planilha fora do ar não pode derrubar a consulta
            logger.warning(f"[consulta-expedicao] catálogo de motoristas indisponível: {e}")
            m = None
        motorista = m.nome if m else f"agente {agent_id}"
        placa = m.placa if m else None
    return {"id": route_id, "nome": corpo.get("name") or f"rota {route_id}", "status": status,
            "iniciada": status not in _STATUS_ROTA_NAO_INICIADA,
            "motorista": motorista, "placa": placa}


def _consultar_stokki(id_stokki: str) -> dict:
    """Mesmas travas da triagem: uma consulta por vez e nunca com agente
    rodando (login concorrente derruba a sessão dele)."""
    if not triagem._lock_consulta_stokki.acquire(blocking=False):
        return {"estado": "nao_conferida"}
    try:
        if triagem._painel_tem_execucao_rodando():
            return {"estado": "nao_conferida"}
        from stokki.auth import StokkiSession
        visto = triagem._status_e_transportadora_stokki(StokkiSession(_carregar_config()), id_stokki)
    except Exception as e:
        logger.warning(f"[consulta-expedicao] Stokki falhou pro pedido {id_stokki}: {e}")
        return {"estado": "nao_conferida"}
    finally:
        triagem._lock_consulta_stokki.release()
    if not visto:
        return {"estado": "inexistente"}
    retira = False
    if re.search(r"enviado", visto["status"], re.IGNORECASE):
        retira = triagem._tipo_retira(visto["transportadora"], triagem._catalogo_transportadoras()) is not None
    return {"estado": "ok", "status": visto["status"], "transportadora": visto["transportadora"], "retira": retira}


def consultar(codigo_digitado: str) -> dict:
    codigo = normalizar_codigo(codigo_digitado)
    if not codigo:
        raise ValueError("Código inválido. Digite PS-12345 ou só o número.")

    try:
        vuupt = triagem._vuupt()
        servico = vuupt.buscar_servico_por_code(codigo)
        if servico:
            servico = triagem._servico_mais_recente_da_cadeia(vuupt, servico)
        rota = None
        if servico and servico.get("route_id"):
            token = _carregar_config().get("vuupt_api", {}).get("token", "")
            rota = _resumo_rota(token, servico["route_id"])
    except Exception as e:
        logger.warning(f"[consulta-expedicao] Vuupt falhou pro pedido {codigo}: {e}")
        raise ConsultaIndisponivel("Vuupt indisponível — tente de novo.") from e

    id_stokki = re.search(r"\d+", codigo).group(0)
    resultado = decidir_veredito(servico, rota, _consultar_stokki(id_stokki), datetime.now(FUSO_LOCAL))
    resultado["codigo"] = codigo
    return resultado
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest painel_agentes.test_consulta_expedicao -v`
Expected: todos OK (Task 1 + Task 2).

- [ ] **Step 5: Commit (mesma condição da Task 1)**

```bash
git add painel_agentes/consulta_expedicao.py painel_agentes/test_consulta_expedicao.py
git commit -m "Expedicao: consulta de pedido ao vivo na Vuupt e na Stokki, sem gravar nada"
```

---

### Task 3: Rotas, tela e item no menu

**Files:**
- Modify: `painel_agentes/painel_agentes.py` (import perto da linha 76; rotas logo depois de `api_expedicao_excluir_pedido`)
- Create: `painel_agentes/templates/consulta_pedido_expedicao.html`
- Modify: `painel_agentes/templates/_menu_lateral_nav.html` (grupo `'Operar'`, logo depois do item `expedicao`)
- Test: `painel_agentes/test_consulta_expedicao.py` (acrescentar classes)

**Interfaces:**
- Consumes: `consulta_expedicao.consultar`, `consulta_expedicao.ConsultaIndisponivel` (Task 2).
- Produces: endpoints Flask `expedicao_consulta` (`GET /expedicao/consulta`) e `api_expedicao_consulta` (`GET /api/expedicao/consulta?codigo=`). O nome `consulta_pedido` já existe (`/consulta/pedido/<codigo>`): **não reutilizar**.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `painel_agentes/test_consulta_expedicao.py`, antes do `if __name__`:

```python
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

CONFIG_PAINEL = {
    "usuario": "u_total", "senha": "s_total",
    "usuario_operador": "u_op", "senha_operador": "s_op",
    "usuario_leitura": "u_le", "senha_leitura": "s_le",
    "usuario_expedicao": "u_ex", "senha_expedicao": "s_ex",
    "usuario_galpao": "u_ga", "senha_galpao": "s_ga",
    "usuario_atendimento": "u_at", "senha_atendimento": "s_at",
}
RESULTADO = {"codigo": "PS-38123", "veredito": "galpao", "titulo": "Deveria estar no galpão",
             "contexto": "Sem rota", "divergencia": None, "aviso": None,
             "stokki_bruto": "Stokki: Em espera", "vuupt_bruto": "Vuupt: not_assigned"}


class _BaseApp(unittest.TestCase):
    def setUp(self):
        config = {**painel_agentes._carregar_config(), "painel_agentes": CONFIG_PAINEL}
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=config)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"


class TestTelaConsulta(_BaseApp):

    def test_abre_para_os_quatro_niveis(self):
        for nivel in ("total", "operador", "expedicao", "galpao"):
            self._logar(nivel)
            r = self.cliente.get("/expedicao/consulta")
            self.assertEqual(r.status_code, 200, nivel)
            self.assertIn('id="codigo"', r.get_data(as_text=True))

    def test_403_para_leitura_e_atendimento(self):
        for nivel in ("leitura", "atendimento"):
            self._logar(nivel)
            self.assertEqual(self.cliente.get("/expedicao/consulta").status_code, 403, nivel)

    def test_menu_mostra_item_para_expedicao(self):
        self._logar("expedicao")
        html = self.cliente.get("/expedicao/consulta").get_data(as_text=True)
        self.assertIn(">Consultar pedido<", html)
        self.assertIn('aria-current="page"', html)

    def test_menu_esconde_item_de_leitura(self):
        self._logar("leitura")
        # /expedicao busca as rotas na Vuupt: sem o patch o teste iria à rede
        with mock.patch.object(painel_agentes, "listar_rotas_do_dia", return_value=[]):
            html = self.cliente.get("/expedicao").get_data(as_text=True)
        self.assertNotIn(">Consultar pedido<", html)


class TestApiConsulta(_BaseApp):

    def test_devolve_o_resultado(self):
        self._logar("galpao")
        with mock.patch.object(painel_agentes.consulta_expedicao, "consultar", return_value=RESULTADO) as m:
            r = self.cliente.get("/api/expedicao/consulta?codigo=38123")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), RESULTADO)
        m.assert_called_once_with("38123")

    def test_codigo_invalido_400(self):
        self._logar("expedicao")
        with mock.patch.object(painel_agentes.consulta_expedicao, "consultar", side_effect=ValueError("Código inválido")):
            r = self.cliente.get("/api/expedicao/consulta?codigo=abc")
        self.assertEqual(r.status_code, 400)
        self.assertIn("inválido", r.get_json()["erro"])

    def test_vuupt_fora_503(self):
        self._logar("expedicao")
        with mock.patch.object(painel_agentes.consulta_expedicao, "consultar",
                               side_effect=painel_agentes.consulta_expedicao.ConsultaIndisponivel("Vuupt indisponível")):
            r = self.cliente.get("/api/expedicao/consulta?codigo=38123")
        self.assertEqual(r.status_code, 503)

    def test_401_sem_sessao(self):
        self.assertEqual(self.cliente.get("/api/expedicao/consulta?codigo=1").status_code, 401)

    def test_403_para_leitura(self):
        self._logar("leitura")
        self.assertEqual(self.cliente.get("/api/expedicao/consulta?codigo=38123").status_code, 403)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_consulta_expedicao -v`
Expected: `TestTelaConsulta`/`TestApiConsulta` falham com 404 / `AttributeError: consulta_expedicao`.

- [ ] **Step 3: Rotas no `painel_agentes.py`**

Logo abaixo de `import pedidos_parados_triagem` (bloco de imports, ~linha 80):

```python
import consulta_expedicao
```

Logo depois da função `api_expedicao_excluir_pedido`:

```python
# Consulta de pedido (Hugo, 02/10): na separação da rota o operador não
# acha um pedido e quer saber se ele deveria estar no galpão. Só leitura;
# o galpão também consulta. Os mesmos níveis estão no item do menu
# (_menu_lateral_nav.html) -- mexer nos dois lugares.
NIVEIS_CONSULTA_PEDIDO = ("total", "operador", "expedicao", "galpao")


@app.route("/expedicao/consulta")
@requer_auth(niveis=NIVEIS_CONSULTA_PEDIDO)
def expedicao_consulta():
    return render_template("consulta_pedido_expedicao.html")


@app.route("/api/expedicao/consulta")
@requer_auth(niveis=NIVEIS_CONSULTA_PEDIDO)
def api_expedicao_consulta():
    """Veredito de UM pedido cruzando Vuupt e Stokki ao vivo -- ver
    consulta_expedicao.py. 400 código inválido, 503 Vuupt fora."""
    try:
        return jsonify(consulta_expedicao.consultar(request.args.get("codigo", "")))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except consulta_expedicao.ConsultaIndisponivel as e:
        return jsonify({"erro": str(e)}), 503
    except Exception as e:
        logging.getLogger(__name__).exception("Falha na consulta de pedido da expedição")
        return jsonify({"erro": f"Falha na consulta: {e}"}), 500
```

- [ ] **Step 4: Item no menu**

Em `painel_agentes/templates/_menu_lateral_nav.html`, no grupo `('Operar', [`, logo depois da linha do item `'rota': 'expedicao'` (e do seu `'icone'`):

```jinja
    {'rota': 'expedicao_consulta', 'rotulo': 'Consultar pedido', 'titulo': 'Consultar se um pedido deveria estar no galpão (Vuupt + Stokki ao vivo)', 'niveis': ('total', 'operador', 'expedicao', 'galpao'), 'badge': none,
     'icone': '<rect x="3" y="7" width="9" height="8" rx="1"/><path d="M3 10h9"/><circle cx="14" cy="12" r="2.6"/><path d="M15.9 13.9L17.5 15.5"/>'},
```

- [ ] **Step 5: Template da tela**

`painel_agentes/templates/consulta_pedido_expedicao.html`:

```html
{% extends "base.html" %}
{% block titulo %}Consultar pedido{% endblock %}

{% block estilo_extra %}
<style>
  .consulta { max-width: 560px; margin: 0 auto; }
  .consulta form { display: flex; gap: 8px; margin-bottom: 18px; }
  .consulta input#codigo {
    flex: 1; min-width: 0; font-size: 22px; font-weight: 700; padding: 14px 16px;
    border: 2px solid var(--borda); border-radius: 10px;
    background: var(--superficie); color: var(--texto); font-family: inherit; letter-spacing: .5px;
  }
  .consulta input#codigo:focus { outline: none; border-color: var(--acento); }
  .consulta button {
    flex: none; background: var(--acento); color: #fff; border: 0; border-radius: 10px;
    font-size: 16px; font-weight: 700; padding: 0 22px; cursor: pointer; font-family: inherit;
  }
  .consulta button:disabled { opacity: .6; cursor: wait; }
  .ajuda { font-size: 13px; color: var(--texto-suave); margin: -8px 0 18px; }

  .veredito {
    border-radius: 14px; padding: 22px 20px; border: 2px solid transparent;
    background: var(--superficie);
  }
  .veredito .codigo { font-size: 13px; font-weight: 700; color: var(--texto-suave); letter-spacing: .5px; }
  .veredito .titulo { font-size: 26px; font-weight: 800; line-height: 1.15; margin: 6px 0 8px; }
  .veredito .contexto { font-size: 16px; line-height: 1.4; }
  .veredito .faixa { margin-top: 14px; padding: 10px 12px; border-radius: 8px; font-size: 14px; font-weight: 600; }
  .veredito .faixa.divergencia { background: #fff3cd; color: #7a5200; }
  .veredito .faixa.aviso { background: var(--fundo); color: var(--texto-suave); }
  .veredito .brutos { margin-top: 14px; font-size: 12.5px; color: var(--texto-suave); line-height: 1.6; }

  .veredito.galpao        { border-color: #2e9e5b; } .veredito.galpao .titulo        { color: #23824a; }
  .veredito.entregue      { border-color: #2f6fd6; } .veredito.entregue .titulo      { color: #2a62bd; }
  .veredito.saiu          { border-color: #d9a300; } .veredito.saiu .titulo          { color: #9a7300; }
  .veredito.retirado,
  .veredito.cancelado     { border-color: #8a8f98; } .veredito.retirado .titulo,
                                                     .veredito.cancelado .titulo     { color: #5f646c; }
  .veredito.nao_encontrado{ border-color: var(--borda); border-style: dashed; }
  .veredito.erro          { border-color: #c0392b; } .veredito.erro .titulo          { color: #c0392b; font-size: 20px; }
  .carregando { font-size: 15px; color: var(--texto-suave); padding: 18px 4px; }

  @media (max-width: 520px) {
    .consulta input#codigo { font-size: 20px; }
    .veredito .titulo { font-size: 23px; }
  }
</style>
{% endblock %}

{% block conteudo %}
<div class="consulta">
  <form id="form-consulta" autocomplete="off">
    <input id="codigo" name="codigo" inputmode="numeric" placeholder="PS-38123" aria-label="Código do pedido" autofocus>
    <button type="submit" id="botao">Consultar</button>
  </form>
  <p class="ajuda">Digite o código do pedido (com ou sem PS). Consulta a Vuupt e a Stokki agora.</p>
  <div id="resultado" aria-live="polite"></div>
</div>

<script>
(function () {
  const URL_API = "{{ url_for('api_expedicao_consulta') }}";
  const form = document.getElementById("form-consulta");
  const campo = document.getElementById("codigo");
  const botao = document.getElementById("botao");
  const saida = document.getElementById("resultado");

  function el(tag, classe, texto) {
    const e = document.createElement(tag);
    if (classe) e.className = classe;
    if (texto) e.textContent = texto;
    return e;
  }

  function mostrar(r) {
    const card = el("div", "veredito " + r.veredito);
    card.appendChild(el("div", "codigo", r.codigo));
    card.appendChild(el("div", "titulo", r.titulo));
    card.appendChild(el("div", "contexto", r.contexto));
    if (r.divergencia) card.appendChild(el("div", "faixa divergencia", "⚠ Divergência: " + r.divergencia + ". Avise o responsável."));
    if (r.aviso) card.appendChild(el("div", "faixa aviso", r.aviso));
    const brutos = el("div", "brutos");
    brutos.appendChild(el("div", "", r.stokki_bruto));
    brutos.appendChild(el("div", "", r.vuupt_bruto));
    card.appendChild(brutos);
    saida.replaceChildren(card);
  }

  function erro(msg) {
    const card = el("div", "veredito erro");
    card.appendChild(el("div", "titulo", msg));
    saida.replaceChildren(card);
  }

  form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    const codigo = campo.value.trim();
    if (!codigo) { campo.focus(); return; }
    botao.disabled = true;
    saida.replaceChildren(el("div", "carregando", "Consultando Vuupt e Stokki…"));
    try {
      const resp = await fetch(URL_API + "?codigo=" + encodeURIComponent(codigo), {credentials: "same-origin"});
      if (resp.status === 401) { window.location.reload(); return; }
      const dados = await resp.json().catch(() => ({}));
      if (!resp.ok) erro(dados.erro || ("Falha na consulta (" + resp.status + ")"));
      else mostrar(dados);
    } catch (e) {
      erro("Sem conexão com o painel. Tente de novo.");
    } finally {
      botao.disabled = false;
      campo.focus();
      campo.select();
    }
  });
})();
</script>
{% endblock %}
```

- [ ] **Step 6: Rodar e ver passar**

Run: `py -3.11 -m unittest painel_agentes.test_consulta_expedicao -v`
Expected: todos OK.

Run também a suíte vizinha, pra garantir que o menu não quebrou outra tela:
`py -3.11 -m unittest painel_agentes.test_inicio_tela painel_agentes.test_wms_telas -v`
Expected: OK.

- [ ] **Step 7: Commit (mesma condição)**

```bash
git add painel_agentes/painel_agentes.py painel_agentes/templates/consulta_pedido_expedicao.html painel_agentes/templates/_menu_lateral_nav.html painel_agentes/test_consulta_expedicao.py
git commit -m "Expedicao: tela Consultar pedido (deveria estar no galpao?) no celular e no computador"
```

---

### Task 4: Prova real, mapa e entrega

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (seção do painel: lista de módulos ~linha 568 e lista de rotas ~linha 591)

- [ ] **Step 1: Subir o painel do worktree na porta 8099** (nunca 8070)

```bash
cd ../agente_stokki_eventos-consulta/painel_agentes
py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"
```

(rodar em background; parar só esse processo no fim)

- [ ] **Step 2: Três códigos reais**

Pegar no Vuupt (ou na Torre de produção) um pedido entregue hoje, um no pool e um em rota não iniciada. Logar como expedição no `http://127.0.0.1:8099/login` e consultar os três. Conferir que cada veredito bate com o que a Torre/Vuupt mostra e que as linhas brutas da Stokki aparecem (se aparecer "Stokki não conferida", ver se há agente rodando — é o comportamento esperado). Lembrar: o `dados.db` local está congelado; a consulta usa só Vuupt/Stokki ao vivo, o banco só entra no lock de "agente rodando" e na cadeia de reentrega.

- [ ] **Step 3: Celular**

Com Playwright (ver memória "Testar painel com Playwright local": `/login?proximo=`, cookie Secure off), viewport 390×844: abrir `/expedicao/consulta`, consultar um código, tirar screenshot. Conferir: sem rolagem horizontal, campo e botão na mesma linha, título do veredito legível, faixa e linhas brutas visíveis.

- [ ] **Step 4: Atualizar o mapa**

Em `MAPA_DO_SISTEMA.txt`:
- na lista de módulos do painel, depois de `expedicao.py ...`:
  `consulta_expedicao.py     Consulta de 1 pedido (Vuupt + Stokki ao vivo, só leitura): deveria estar no galpão?`
- na lista de rotas, depois de `/expedicao ...`:
  `  /expedicao/consulta        consulta_pedido_expedicao.html (+ /api/expedicao/consulta)`

- [ ] **Step 5: Verificação final**

```bash
py -3.11 -m py_compile painel_agentes/consulta_expedicao.py painel_agentes/painel_agentes.py
py -3.11 -m unittest painel_agentes.test_consulta_expedicao painel_agentes.test_inicio_tela -v
git status --short
```

Expected: compila; testes OK; `git status` só com os arquivos desta tarefa.

- [ ] **Step 6: Entregar ao Hugo**

Relatar: o que foi feito, os três vereditos reais vistos, o screenshot do celular, e que commit/push/deploy esperam a palavra dele (deploy = `painel-agentes.service` restart + `curl` na `/api/expedicao/consulta` local da VPS, ver skill deploy-vps). O MAPA entra no mesmo commit da Task 3 ou num commit próprio, como ele preferir.
