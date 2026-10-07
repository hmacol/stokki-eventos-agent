# Cancelamento de pedido pelo portal — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** quando o embarcador cancela no portal um pedido já criado, o sistema cancela de verdade na Vuupt e na Stokki (enquanto o motorista não saiu) e o portal mostra "Cancelado"; o que não puder ser automático vai pra operação com e-mail + WhatsApp.

**Architecture:** o clique só grava `CANCELANDO` + solicitação PENDENTE. O worker `portal_cliente/enviar_stokki.py --loop` (serviço `portal-cliente-envios`, ciclo de 20 s) processa os `CANCELANDO` dentro da trava da Stokki: decide pela regra pura `portal_cliente/cancelamento.decidir()` (lê `nucleo_pedidos`/`nucleo_rotas`), cancela na Vuupt (`roteirizacao/cancelar_servico.py`, PUT /cancel) e na Stokki (`stokki/cancelar.py`, POST outbound/cancel + poll) e grava um dos três fins. Planejamento não muda (escopo B).

**Tech Stack:** Python 3.11, sqlite3 (`dados/dados.db`), `requests` via `stokki.auth.StokkiSession`, `vuupt_client.VuuptClient`, `unittest`. Sem Playwright.

**Spec:** `DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md` (raiz da worktree). Ler antes de começar.

## Global Constraints

- Python local: sempre `py -3.11`. Testes com `py -3.11 -m unittest <modulo>` a partir da raiz da worktree `C:\agente_stokki_eventos\.claude\worktrees\cancelar-agendamento`.
- Nunca misturar `roteirizacao`/`nucleo` com `painel_agentes` no mesmo comando de teste (sys.path).
- Código, comentários e commits em português; comentários sem acento quando o arquivo já segue isso (`notificar_whatsapp.py`, `stokki/`); `portal_cliente/` usa acento.
- Commits: `Área: o que mudou e por quê`, terminando com `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. `git add` só dos arquivos da tarefa. Sem push (o Hugo pede).
- Nunca escrever na Stokki/Vuupt a partir do processo web; só no worker, dentro de `sessao_uso.adquirir`.
- Motivo enviado à Stokki tem **mínimo 15 caracteres**; usar sempre `PUT /services/{id}/cancel` (`cancelar_servico_oficial`), nunca DELETE.
- Ao criar/mover arquivo, atualizar `MAPA_DO_SISTEMA.txt` no mesmo commit (Tarefa 8).

## Review Focus

1. Envio `CRIADO` sem `codigo_pedido` (worker ainda não conciliou o PS): deve ir pra operação com motivo "código do pedido ainda não identificado", nunca estourar. Teste em `test_cancelamento.py::test_sem_codigo_vai_pra_operacao` (Tarefa 4).
2. Pedido com reentrega viva (`PS-1` cancelado/insucesso e `PS-1-R1` em rota não iniciada): todos os serviços vivos devem ser cancelados, não só o base. Teste `test_cancela_todas_as_reentregas_vivas` (Tarefa 4).
3. Stokki responde `processing: true` mas o poll nunca chega a `Canceled` em 30 s: deve ser falha técnica (volta pra CRIADO + aviso), não sucesso. Teste `test_poll_que_nao_conclui_e_falha` (Tarefa 1).
4. Vuupt devolve 409 (serviço já `done`) ou já `canceled`: `canceled` conta como sucesso, `done` é falha → operação. Testes `test_ja_cancelado_conta_como_ok` e `test_erro_vuupt_vira_falha` (Tarefa 2).
5. Dois cliques seguidos em "Cancelar" (o segundo chega com o envio já `CANCELANDO`): deve recusar com mensagem, sem duplicar solicitação. Teste `test_cancelar_duas_vezes_recusa` (Tarefa 3).

---

### Task 1: `stokki/cancelar.py` — cancelar pedido na Stokki por HTTP

**Files:**
- Create: `stokki/cancelar.py`
- Test: `stokki/test_cancelar.py`

**Interfaces:**
- Consumes: `stokki.auth.StokkiSession` (`.get(url)`, `.post(url, files=..., headers=...)`), `stokki.pedidos.BASE_URL`.
- Produces: `cancelar_pedido(sessao, id_stokki: int, motivo: str, dormir=time.sleep) -> dict` com chaves `ok: bool`, `situacao: str` (texto da "Situação" depois), `ja_estava: bool`, `erro: str`. Também `situacao_e_token(sessao, id_stokki) -> tuple[str, str]`.

- [ ] **Step 1: Escrever os testes (falham)**

```python
# stokki/test_cancelar.py
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_cancelar"""
import unittest

from stokki import cancelar

# Trecho REAL da pagina show/39959 (sonda de 06/10): linha "Situação" e o form_cancel.
def _pagina(situacao: str) -> str:
    return (
        '<tr><th style="width:280px">Situação:</th><td>\n <span class="badge badge-secondary">'
        f'<i class="fa fa-x"></i>{situacao}</span>\n</td></tr>'
        '<!-- Modal Cancel --> <div class="modal fade" id="modal_cancel"><form id="form_cancel" method="post" enctype="multipart/form-data" class="w-100">'
        '<div class="form-group" id="validation_errors_cancel"></div> <input type="hidden" name="_token" value="TOKEN123">'
        '<input class="form-control" id="cancel_reason" name="reason" placeholder="Mínimo de 15 caracteres">'
        '<input type="number" id="cancel_id" name="provider_outbound_id" value="39959" hidden> <input type="text" name="page" value="show" hidden>'
        '</form></div>'
    )


class _Resp:
    def __init__(self, status, texto="", json_=None):
        self.status_code, self.text, self._json = status, texto, json_

    def json(self):
        if self._json is None:
            raise ValueError("sem json")
        return self._json


class _Sessao:
    """Sessao falsa: `paginas` = respostas do GET em ordem; `posts` guarda o que foi enviado."""
    def __init__(self, gets, post=None):
        self.gets, self.post_resp, self.posts, self.urls = list(gets), post, [], []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.gets.pop(0)

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return self.post_resp


class CancelarPedido(unittest.TestCase):
    def test_situacao_e_token(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador"))])
        self.assertEqual(cancelar.situacao_e_token(s, 39959), ("Aguardando Transportador", "TOKEN123"))

    def test_sucesso_com_poll(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador")),
                     _Resp(200, '{"success":true,"state":"Processing"}', {"success": True, "state": "Processing"}),
                     _Resp(200, '{"success":true,"state":"Canceled"}', {"success": True, "state": "Canceled"}),
                     _Resp(200, _pagina("Cancelado"))],
                    post=_Resp(200, "", {"success": True, "processing": True}))
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu", dormir=lambda n: None)
        self.assertEqual((r["ok"], r["situacao"], r["ja_estava"]), (True, "Cancelado", False))
        url, kw = s.posts[0]
        self.assertTrue(url.endswith("/inventory/outbound/cancel"))
        self.assertEqual(kw["files"]["_token"], (None, "TOKEN123"))
        self.assertEqual(kw["files"]["provider_outbound_id"], (None, "39959"))
        self.assertEqual(kw["files"]["page"], (None, "show"))
        self.assertEqual(kw["files"]["reason"], (None, "Portal Fresh Hub: cliente desistiu"))

    def test_ja_cancelado_nao_posta(self):
        s = _Sessao([_Resp(200, _pagina("Cancelado"))])
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu")
        self.assertEqual((r["ok"], r["ja_estava"], s.posts), (True, True, []))

    def test_expedido_nao_cancela(self):
        s = _Sessao([_Resp(200, _pagina("Enviado"))])
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu")
        self.assertFalse(r["ok"])
        self.assertIn("expedido", r["erro"])
        self.assertEqual(s.posts, [])

    def test_motivo_curto_e_completado(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador")),
                     _Resp(200, "", {"success": True, "state": "Canceled"}),
                     _Resp(200, _pagina("Cancelado"))],
                    post=_Resp(200, "", {"success": True, "processing": True}))
        cancelar.cancelar_pedido(s, 39959, "", dormir=lambda n: None)
        motivo = s.posts[0][1]["files"]["reason"][1]
        self.assertGreaterEqual(len(motivo), 15)
        self.assertTrue(motivo.startswith("Portal Fresh Hub"))

    def test_erro_422_vira_erro(self):
        s = _Sessao([_Resp(200, _pagina("Aguardando Transportador"))],
                    post=_Resp(422, '{"errors":{"reason":["minimo 15"]}}', {"errors": {"reason": ["minimo 15"]}}))
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu")
        self.assertFalse(r["ok"])
        self.assertIn("minimo 15", r["erro"])

    def test_poll_que_nao_conclui_e_falha(self):
        gets = [_Resp(200, _pagina("Aguardando Transportador"))]
        gets += [_Resp(200, "", {"success": True, "state": "Processing"})] * 15
        gets += [_Resp(200, _pagina("Aguardando Transportador"))]
        s = _Sessao(gets, post=_Resp(200, "", {"success": True, "processing": True}))
        r = cancelar.cancelar_pedido(s, 39959, "Portal Fresh Hub: cliente desistiu", dormir=lambda n: None)
        self.assertFalse(r["ok"])
        self.assertIn("processamento", r["erro"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest stokki.test_cancelar`
Expected: `ImportError: cannot import name 'cancelar'` (ou `ModuleNotFoundError`).

- [ ] **Step 3: Implementar `stokki/cancelar.py`**

```python
# -*- coding: utf-8 -*-
"""
stokki/cancelar.py

Cancela um pedido de saida na Stokki pelo mesmo fluxo da tela (sonda de
06/10/2026, provado no PS-39959 -- memoria reference_stokki_cancelar_pedido_endpoint):

  GET  /administrator/inventory/outbound/show/{id}      -> "Situação" + _token do form_cancel
  POST /administrator/inventory/outbound/cancel         -> {"success":true,"processing":true}
  GET  /administrator/inventory/outbound/cancel/status/{id} a cada 2 s ate {"state":"Canceled"}

Sem Playwright: usa a StokkiSession (requests) e exige que quem chama
esteja com a trava stokki/sessao_uso.py (login concorrente derruba a
outra sessao). Motivo tem minimo de 15 caracteres na Stokki.
"""
import logging
import re
import time

from stokki.pedidos import BASE_URL

logger = logging.getLogger(__name__)

URL_SHOW = f"{BASE_URL}/pt-br/administrator/inventory/outbound/show/{{id}}"
URL_CANCEL = f"{BASE_URL}/pt-br/administrator/inventory/outbound/cancel"
URL_STATUS = f"{BASE_URL}/pt-br/administrator/inventory/outbound/cancel/status/{{id}}"
PREFIXO_MOTIVO = "Portal Fresh Hub"
MOTIVO_PADRAO = f"{PREFIXO_MOTIVO}: cancelado pelo embarcador"
TENTATIVAS_POLL = 15
INTERVALO_POLL = 2

_RE_SITUACAO = re.compile(r"Situa[çc][ãa]o:\s*</th>\s*<td>(.*?)</td>", re.S)
_RE_TOKEN = re.compile(r'<form id="form_cancel".*?name="_token" value="([^"]+)"', re.S)


def _limpar(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def situacao_e_token(sessao, id_stokki: int) -> tuple[str, str]:
    """Texto da "Situação" e o _token do formulario de cancelamento."""
    resp = sessao.get(URL_SHOW.format(id=id_stokki))
    resp.raise_for_status() if hasattr(resp, "raise_for_status") else None
    html = resp.text
    m = _RE_SITUACAO.search(html)
    t = _RE_TOKEN.search(html)
    return (_limpar(m.group(1)) if m else "", t.group(1) if t else "")


def montar_motivo(motivo_cliente: str) -> str:
    texto = (motivo_cliente or "").strip()
    if not texto:
        return MOTIVO_PADRAO
    if not texto.startswith(PREFIXO_MOTIVO):
        texto = f"{PREFIXO_MOTIVO}: {texto}"
    return texto[:200]


def cancelar_pedido(sessao, id_stokki: int, motivo: str, dormir=time.sleep) -> dict:
    """Devolve {"ok", "situacao", "ja_estava", "erro"}. Nunca levanta por
    resposta da Stokki (so por falha de rede do requests)."""
    situacao, token = situacao_e_token(sessao, id_stokki)
    sit = situacao.lower()
    if "cancel" in sit:
        return {"ok": True, "situacao": situacao, "ja_estava": True, "erro": ""}
    if any(p in sit for p in ("enviado", "sent", "expedido")):
        return {"ok": False, "situacao": situacao, "ja_estava": False, "erro": f"pedido ja expedido na Stokki ({situacao})"}
    if not token:
        return {"ok": False, "situacao": situacao, "ja_estava": False, "erro": "formulario de cancelamento nao encontrado na pagina"}

    resp = sessao.post(
        URL_CANCEL,
        files={"_token": (None, token), "reason": (None, montar_motivo(motivo)),
               "provider_outbound_id": (None, str(id_stokki)), "page": (None, "show")},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": URL_SHOW.format(id=id_stokki)},
    )
    try:
        corpo = resp.json()
    except ValueError:
        corpo = {}
    if resp.status_code != 200 or not corpo.get("success", True):
        erros = corpo.get("errors") or corpo.get("message") or resp.text[:200]
        return {"ok": False, "situacao": situacao, "ja_estava": False, "erro": f"Stokki {resp.status_code}: {erros}"}

    if corpo.get("processing"):
        concluiu = False
        for _ in range(TENTATIVAS_POLL):
            dormir(INTERVALO_POLL)
            st = sessao.get(URL_STATUS.format(id=id_stokki))
            try:
                if st.json().get("state") == "Canceled":
                    concluiu = True
                    break
            except ValueError:
                pass
        if not concluiu:
            return {"ok": False, "situacao": situacao, "ja_estava": False,
                    "erro": f"cancelamento ainda em processamento depois de {TENTATIVAS_POLL * INTERVALO_POLL}s"}

    depois, _ = situacao_e_token(sessao, id_stokki)
    ok = "cancel" in depois.lower()
    logger.info(f"Stokki: pedido {id_stokki} -> {depois!r} ({'ok' if ok else 'NAO cancelou'})")
    return {"ok": ok, "situacao": depois, "ja_estava": False,
            "erro": "" if ok else f"Stokki aceitou mas a situacao ficou {depois!r}"}
```

Observação: `_Resp` do teste não tem `raise_for_status`; o `hasattr` cobre isso e o `requests.Response` real.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest stokki.test_cancelar`
Expected: `Ran 7 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add stokki/cancelar.py stokki/test_cancelar.py
git commit -m "Stokki: cancelar pedido de saida por HTTP (POST outbound/cancel + poll), sem Playwright"
```

---

### Task 2: `roteirizacao/cancelar_servico.py` — cancelar serviço na Vuupt em qualquer lugar

**Files:**
- Create: `roteirizacao/cancelar_servico.py`
- Test: `roteirizacao/test_cancelar_servico.py`

**Interfaces:**
- Consumes: `vuupt_client.VuuptClient(token)` (`.cancelar_servico_oficial(id)`, `.buscar_servico_por_id(id)`), `painel_agentes/rascunhos_rota.py` (`buscar_rascunho`, `preparar_cancelamento_de_parada`, `remover_parada`, `STATUS_ENVIADO`, `STATUS_DESCARTADO`, `DB_PATH`), `nucleo.sincronizar_servicos_vuupt.ressincronizar_ids(vuupt, ids)`.
- Produces: `cancelar_servico_completo(token: str, service_id: int, vuupt=None) -> dict` com `ok: bool`, `ja_estava: bool`, `erro: str`; `rascunho_do_servico(service_id) -> tuple[int | None, str | None]`.

Decisão (escopo B da spec): `painel_agentes/planejamento_rotas.cancelar_pedido` **não é alterado**; este módulo repete os mesmos 4 passos (rascunho enviado → `preparar_cancelamento_de_parada`; `PUT /cancel`; `remover_parada` se rascunho não enviado; ressincronizar o núcleo). Unificar quando o Planejamento entrar no escopo.

- [ ] **Step 1: Escrever os testes (falham)**

```python
# roteirizacao/test_cancelar_servico.py
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest roteirizacao.test_cancelar_servico"""
import unittest
from unittest import mock

from roteirizacao import cancelar_servico as cs


class CancelarServicoCompleto(unittest.TestCase):
    def setUp(self):
        self.vuupt = mock.Mock()
        self.vuupt.buscar_servico_por_id.return_value = {"id": 111, "status": "not_assigned"}
        mock.patch.object(cs, "ressincronizar_ids").start()
        mock.patch.object(cs.rascunhos_rota, "preparar_cancelamento_de_parada", return_value={"ok": True}).start()
        self.remover = mock.patch.object(cs.rascunhos_rota, "remover_parada").start()
        self.preparar = cs.rascunhos_rota.preparar_cancelamento_de_parada
        self.addCleanup(mock.patch.stopall)

    def test_no_pool_cancela_direto(self):
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(None, None)):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertEqual(r, {"ok": True, "ja_estava": False, "erro": ""})
        self.vuupt.cancelar_servico_oficial.assert_called_once_with(111)
        self.vuupt.cancelar_servico.assert_not_called()
        cs.ressincronizar_ids.assert_called_once_with(self.vuupt, [111])

    def test_em_rascunho_tira_a_parada(self):
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(7, "RASCUNHO")):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertTrue(r["ok"])
        self.remover.assert_called_once_with(7, 111)
        self.preparar.assert_not_called()

    def test_em_rota_enviada_prepara_antes(self):
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(7, "ENVIADO")):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertTrue(r["ok"])
        self.preparar.assert_called_once_with(7, 111, "tok")
        self.remover.assert_not_called()

    def test_preparo_que_falha_nao_cancela(self):
        self.preparar.return_value = {"ok": False, "erro": "rota ja iniciou"}
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(7, "ENVIADO")):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertEqual((r["ok"], r["erro"]), (False, "rota ja iniciou"))
        self.vuupt.cancelar_servico_oficial.assert_not_called()

    def test_ja_cancelado_conta_como_ok(self):
        self.vuupt.buscar_servico_por_id.return_value = {"id": 111, "status": "canceled"}
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(None, None)):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertEqual((r["ok"], r["ja_estava"]), (True, True))
        self.vuupt.cancelar_servico_oficial.assert_not_called()

    def test_erro_vuupt_vira_falha(self):
        self.vuupt.cancelar_servico_oficial.side_effect = cs.VuuptAPIError("409 ja done")
        with mock.patch.object(cs, "rascunho_do_servico", return_value=(None, None)):
            r = cs.cancelar_servico_completo("tok", 111, vuupt=self.vuupt)
        self.assertFalse(r["ok"])
        self.assertIn("409", r["erro"])
        cs.ressincronizar_ids.assert_not_called()


class RascunhoDoServico(unittest.TestCase):
    def test_acha_o_rascunho_vivo_mais_recente(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE rascunhos_parada (id INTEGER PRIMARY KEY, rascunho_id INTEGER, service_id INTEGER);
            INSERT INTO rascunhos_rota VALUES (1, 'DESCARTADO'), (2, 'RASCUNHO');
            INSERT INTO rascunhos_parada VALUES (1, 1, 111), (2, 2, 111);
        """)
        with mock.patch.object(cs, "_conectar_rascunhos", return_value=conn):
            self.assertEqual(cs.rascunho_do_servico(111), (2, "RASCUNHO"))
            self.assertEqual(cs.rascunho_do_servico(999), (None, None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_cancelar_servico`
Expected: `ModuleNotFoundError: No module named 'roteirizacao.cancelar_servico'`

- [ ] **Step 3: Implementar**

```python
# roteirizacao/cancelar_servico.py
# -*- coding: utf-8 -*-
"""
roteirizacao/cancelar_servico.py

Cancela um servico na Vuupt onde quer que ele esteja (pool, rascunho local
ou rota ja enviada), do jeito que o botao "Cancelar pedido" do Planejamento
faz (painel_agentes/planejamento_rotas.cancelar_pedido), mas chamavel de
fora do painel -- usado pelo worker do portal do cliente
(portal_cliente/cancelamento.py). O Planejamento NAO foi alterado
(escopo B da DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md).

Sempre PUT /services/{id}/cancel (status 'canceled'): DELETE faz o
pipeline recriar o pedido ainda aberto na Stokki (memoria
project_cancelar_pedido_delete_recria).
"""
import logging
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", _RAIZ / "painel_agentes"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import rascunhos_rota  # noqa: E402  (painel_agentes/)
from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids  # noqa: E402
from vuupt_client import VuuptAPIError, VuuptClient  # noqa: E402

logger = logging.getLogger(__name__)


def _conectar_rascunhos() -> sqlite3.Connection:
    conn = sqlite3.connect(rascunhos_rota.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def rascunho_do_servico(service_id: int) -> tuple[int | None, str | None]:
    """(rascunho_id, status) do rascunho vivo mais recente que contem o
    servico; (None, None) se esta no pool."""
    conn = _conectar_rascunhos()
    try:
        row = conn.execute("""
            SELECT r.id, r.status FROM rascunhos_parada p JOIN rascunhos_rota r ON r.id = p.rascunho_id
            WHERE p.service_id = ? AND r.status != ? ORDER BY r.id DESC LIMIT 1
        """, (service_id, rascunhos_rota.STATUS_DESCARTADO)).fetchone()
        return (row["id"], row["status"]) if row else (None, None)
    finally:
        conn.close()


def cancelar_servico_completo(token: str, service_id: int, vuupt=None) -> dict:
    """Devolve {"ok", "ja_estava", "erro"}. `vuupt` injetavel pra teste."""
    vuupt = vuupt or VuuptClient(token)
    try:
        atual = vuupt.buscar_servico_por_id(service_id) or {}
    except VuuptAPIError as e:
        return {"ok": False, "ja_estava": False, "erro": f"Vuupt nao respondeu o servico {service_id}: {e}"}
    if atual.get("status") == "canceled":
        return {"ok": True, "ja_estava": True, "erro": ""}

    rascunho_id, status_rascunho = rascunho_do_servico(service_id)
    if rascunho_id and status_rascunho == rascunhos_rota.STATUS_ENVIADO:
        preparo = rascunhos_rota.preparar_cancelamento_de_parada(rascunho_id, service_id, token)
        if not preparo.get("ok"):
            return {"ok": False, "ja_estava": False, "erro": preparo.get("erro") or "rota enviada nao liberou a parada"}

    try:
        vuupt.cancelar_servico_oficial(service_id)
    except VuuptAPIError as e:
        return {"ok": False, "ja_estava": False, "erro": f"Vuupt: {e}"}

    if rascunho_id and status_rascunho != rascunhos_rota.STATUS_ENVIADO:
        rascunhos_rota.remover_parada(rascunho_id, service_id)
    try:
        ressincronizar_ids(vuupt, [service_id])
    except Exception as e:  # noqa: BLE001 -- a escrita na Vuupt ja aconteceu; o timer de 15 min alcanca
        logger.warning(f"ressincronizacao do servico {service_id} falhou: {e}")
    return {"ok": True, "ja_estava": False, "erro": ""}
```

`rascunhos_rota.DB_PATH` existe (linha 42 do módulo) e é o mesmo `dados/dados.db`.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest roteirizacao.test_cancelar_servico`
Expected: `Ran 7 tests ... OK`. Se o import de `rascunhos_rota` puxar módulo que não existe fora do painel, o traceback diz qual; adicionar o diretório dele ao `sys.path` no topo do módulo (nunca `import painel_agentes`).

- [ ] **Step 5: Garantir que o Planejamento continua verde**

Run (em comando separado, dentro de `painel_agentes/`): `cd painel_agentes && py -3.11 -m unittest test_ressincronizar_pool`
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add roteirizacao/cancelar_servico.py roteirizacao/test_cancelar_servico.py
git commit -m "Roteirizacao: cancelar servico na Vuupt (pool, rascunho ou rota enviada) chamavel fora do painel"
```

---

### Task 3: Portal — status `CANCELANDO`, ação de cancelar e conclusão da solicitação

**Files:**
- Modify: `portal_cliente/envio_pedidos.py` (constantes ~72-95; `_linha` ~1335-1370; `aplicar_acao` 1452-1476; `concluir_solicitacao` 1590-1593)
- Modify: `portal_cliente/templates/_envios.html` (CSS ~linha 24)
- Test: `portal_cliente/test_cancelamento.py` (novo; a Tarefa 4 acrescenta classes nele)

**Interfaces:**
- Produces: `STATUS_CANCELANDO = "CANCELANDO"`; `aplicar_acao(..., "cancelar", ...)` em CRIADO/DUPLICADO devolve `{"aplicado": False, "precisa_operacao": False, "em_andamento": True, "mensagem": ...}`; `concluir_solicitacao(conn, id, resposta, recusada=False)` marca o envio `CANCELADO` quando a solicitação é `cancelar` e não foi recusada; `buscar_solicitacao_cancelamento(conn, envio_id) -> dict | None`.

- [ ] **Step 1: Escrever os testes (falham)**

```python
# portal_cliente/test_cancelamento.py
# -*- coding: utf-8 -*-
"""
Cancelamento de pedido pelo portal (DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md).

    py -3.11 -m unittest portal_cliente.test_cancelamento
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

COLUNAS_ENVIO = ("id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT, atualizado_em TEXT, "
                 "criado_stokki_em TEXT, emitida_em TEXT, numero_nf TEXT, referencia TEXT, destinatario_doc TEXT, "
                 "destinatario_nome TEXT, destinatario_endereco TEXT, destinatario_bairro TEXT, destinatario_municipio TEXT, "
                 "codigo_pedido TEXT, requer_agendamento INTEGER, agendamento_pendente INTEGER, agendamento_data TEXT, "
                 "data_expedicao TEXT, xml_path TEXT, itens_json TEXT, erro TEXT, bloqueio_motivo TEXT")


def conn_portal(envios=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(f"CREATE TABLE portal_envios ({COLUNAS_ENVIO});"
                       "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);")
    for e in envios:
        conn.execute("INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, codigo_pedido, requer_agendamento, agendamento_pendente, destinatario_nome) "
                     "VALUES (?, '111', ?, datetime('now','localtime'), ?, ?, 1, 1, 'DESTINO LTDA')", e)
    conn.commit()
    return conn


def envio(conn, id_):
    return dict(conn.execute("SELECT * FROM portal_envios WHERE id = ?", (id_,)).fetchone())


class AplicarAcaoCancelar(unittest.TestCase):
    def test_criado_vira_cancelando_com_solicitacao_pendente(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        r = ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {"motivo": "cliente desistiu"}, "cliente")
        self.assertEqual((r["aplicado"], r["precisa_operacao"], r["em_andamento"]), (False, False, True))
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELANDO)
        s = conn.execute("SELECT tipo, status, detalhes FROM portal_solicitacoes").fetchall()
        self.assertEqual([tuple(x) for x in s], [("cancelar", "PENDENTE", "cliente desistiu")])

    def test_duplicado_tambem_vira_cancelando(self):
        conn = conn_portal([(51, ep.STATUS_DUPLICADO, "9960", "PS-39960")])
        ep.aplicar_acao(conn, envio(conn, 51), "cancelar", {}, "cliente")
        self.assertEqual(envio(conn, 51)["status"], ep.STATUS_CANCELANDO)

    def test_cancelar_duas_vezes_recusa(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM portal_solicitacoes").fetchone()[0], 1)

    def test_na_fila_continua_cancelando_na_hora(self):
        conn = conn_portal([(52, ep.STATUS_NA_FILA, "9961", None)])
        r = ep.aplicar_acao(conn, envio(conn, 52), "cancelar", {}, "cliente")
        self.assertTrue(r["aplicado"])
        self.assertEqual(envio(conn, 52)["status"], ep.STATUS_CANCELADO)


class LinhaCancelando(unittest.TestCase):
    def test_cancelando_nao_oferece_acoes_nem_cobra_agendamento(self):
        conn = conn_portal([(50, ep.STATUS_CANCELANDO, "9959", "PS-39959")])
        conn.execute("INSERT INTO portal_solicitacoes (envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em) "
                     "VALUES (50, '111', 'cancelar', '', 'PENDENTE', 'cliente', '2026-10-06 10:00:00')")
        conn.commit()
        l = ep.listar_envios(conn, "111")[0]
        self.assertEqual(l["status_rotulo"], "Cancelando")
        self.assertFalse(l["pode_cancelar"] or l["pode_reagendar"] or l["pode_em_espera"] or l["pode_reenviar"])
        self.assertFalse(l["agendamento_pendente"])
        self.assertEqual(ep.resumo_envios([l])["agendamento_pendente"], 0)


class ConcluirSolicitacao(unittest.TestCase):
    def test_concluir_cancelamento_marca_envio_cancelado(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        conn.execute("UPDATE portal_envios SET status = ? WHERE id = 50", (ep.STATUS_CRIADO,))  # worker devolveu pra operacao
        sid = conn.execute("SELECT id FROM portal_solicitacoes").fetchone()[0]
        ep.concluir_solicitacao(conn, sid, "cancelado na Stokki pela equipe")
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELADO)
        self.assertEqual(conn.execute("SELECT status, resposta FROM portal_solicitacoes").fetchone()[:], ("CONCLUIDA", "cancelado na Stokki pela equipe"))

    def test_recusar_cancelamento_nao_mexe_no_envio(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        conn.execute("UPDATE portal_envios SET status = ? WHERE id = 50", (ep.STATUS_CRIADO,))
        sid = conn.execute("SELECT id FROM portal_solicitacoes").fetchone()[0]
        ep.concluir_solicitacao(conn, sid, "ja entregue", recusada=True)
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)

    def test_buscar_solicitacao_cancelamento(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        self.assertIsNone(ep.buscar_solicitacao_cancelamento(conn, 50))
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {"motivo": "x"}, "cliente")
        self.assertEqual(ep.buscar_solicitacao_cancelamento(conn, 50)["detalhes"], "x")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_cancelamento`
Expected: `AttributeError: module 'envio_pedidos' has no attribute 'STATUS_CANCELANDO'`.

- [ ] **Step 3: Constantes e rótulos** (`portal_cliente/envio_pedidos.py`, perto da linha 77)

```python
STATUS_CANCELADO = "CANCELADO"
STATUS_CANCELANDO = "CANCELANDO"   # cancelamento pedido pelo cliente, worker ainda vai executar (06/10)
```
e em `ROTULOS_STATUS`:
```python
    STATUS_CANCELANDO: "Cancelando",
```
Também atualizar o comentário de estados no topo do arquivo (linha ~29): `... ; CANCELANDO -> CANCELADO | CRIADO (precisa da operação)`.

- [ ] **Step 4: `aplicar_acao` — ramo `cancelar`** (substituir as linhas 1462-1476)

```python
    if tipo == "cancelar":
        if st == STATUS_ENVIANDO:
            raise ErroEnvio("Esse pedido está sendo enviado à Stokki agora -- tente de novo em alguns instantes.")
        if st == STATUS_CANCELADO:
            raise ErroEnvio("Esse pedido já está cancelado.")
        if st == STATUS_CANCELANDO:
            raise ErroEnvio("O cancelamento desse pedido já está em andamento.")
        if st in (STATUS_NA_FILA, STATUS_ERRO, STATUS_AGUARDANDO_LIBERACAO):
            conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?", (STATUS_CANCELADO, agora, envio["id"]))
            _registrar_solicitacao(conn, envio, tipo, dados.get("motivo", ""), por, status="CONCLUIDA")
            _remover_dedicado(conn, envio["id"], por)
            conn.commit()
            return {"aplicado": True, "precisa_operacao": False, "mensagem": "Pedido cancelado -- não será enviado à Stokki."}
        # Já criado na Stokki (CRIADO/DUPLICADO): o worker portal-cliente-envios
        # cancela na Vuupt e na Stokki no próximo ciclo (06/10, spec do
        # cancelamento). Se o motorista já saiu, ele devolve pra operação.
        conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?", (STATUS_CANCELANDO, agora, envio["id"]))
        _registrar_solicitacao(conn, envio, tipo, dados.get("motivo", ""), por)
        conn.commit()
        return {"aplicado": False, "precisa_operacao": False, "em_andamento": True,
                "mensagem": "Cancelamento em andamento -- você verá aqui em instantes."}
```

- [ ] **Step 5: `_linha` — CANCELANDO sem ações** (linhas ~1364-1368)

Trocar as quatro chaves `pode_*` por:
```python
        "pode_cancelar": d["status"] in (STATUS_NA_FILA, STATUS_CRIADO, STATUS_ERRO, STATUS_DUPLICADO, STATUS_AGUARDANDO_LIBERACAO) and not cancelamento_pedido,
        "pode_em_espera": d["status"] in (STATUS_CRIADO, STATUS_DUPLICADO) and not any(s["tipo"] in ("em_espera", "cancelar") for s in pend),
        "pode_reagendar": d["status"] in (STATUS_NA_FILA, STATUS_CRIADO, STATUS_DUPLICADO) and not cancelamento_pedido,
        "pode_reenviar": d["status"] in (STATUS_ERRO, STATUS_CANCELADO),
```
(já é assim desde ed92629; CANCELANDO não está em nenhuma lista, então nada muda aqui — conferir e seguir). E logo acima, onde `cancelamento_pedido` é calculado, incluir o status:
```python
    cancelamento_pedido = d["status"] == STATUS_CANCELANDO or any(s["tipo"] == "cancelar" for s in pend)
```

- [ ] **Step 6: `concluir_solicitacao` e `buscar_solicitacao_cancelamento`** (substituir 1590-1593)

```python
def buscar_solicitacao_cancelamento(conn: sqlite3.Connection, envio_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM portal_solicitacoes WHERE envio_id = ? AND tipo = 'cancelar' AND status = 'PENDENTE' "
                     "ORDER BY id DESC LIMIT 1", (envio_id,)).fetchone()
    return dict(r) if r else None


def concluir_solicitacao(conn: sqlite3.Connection, solicitacao_id: int, resposta: str, recusada: bool = False) -> None:
    """Fecha a solicitação. Cancelamento concluído (não recusado) também
    marca o envio como CANCELADO -- é a CLI da operação fechando o que o
    worker devolveu pra ela (06/10)."""
    s = conn.execute("SELECT envio_id, tipo FROM portal_solicitacoes WHERE id = ?", (solicitacao_id,)).fetchone()
    conn.execute("UPDATE portal_solicitacoes SET status = ?, resposta = ?, concluido_em = ? WHERE id = ?",
                 ("RECUSADA" if recusada else "CONCLUIDA", resposta, _agora(), solicitacao_id))
    if s and s["tipo"] == "cancelar" and not recusada:
        conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?",
                     (STATUS_CANCELADO, _agora(), s["envio_id"]))
        _remover_dedicado(conn, s["envio_id"], "conclusao")
    conn.commit()
```

- [ ] **Step 7: CSS do badge** (`portal_cliente/templates/_envios.html`, depois da linha `.b-CANCELADO { ... }`)

```css
  .b-CANCELANDO { background: var(--interrompido-bg); color: var(--interrompido-texto); opacity: .75; }
```

- [ ] **Step 8: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_cancelamento portal_cliente.test_cancelamento_agendamento portal_cliente.test_reenviar_erros portal_cliente.test_bloqueio_area`
Expected: todos `OK`.

- [ ] **Step 9: Commit**

```bash
git add portal_cliente/envio_pedidos.py portal_cliente/templates/_envios.html portal_cliente/test_cancelamento.py
git commit -m "Portal: cancelar pedido ja criado vira CANCELANDO pro worker executar; concluir solicitacao marca Cancelado"
```

---

### Task 4: `portal_cliente/cancelamento.py` — regra de decisão e processamento dos CANCELANDO

**Files:**
- Create: `portal_cliente/cancelamento.py`
- Test: `portal_cliente/test_cancelamento.py` (acrescentar classes)

**Interfaces:**
- Consumes: Tarefa 3 (`ep.STATUS_CANCELANDO`, `ep.STATUS_CANCELADO`, `ep.STATUS_CRIADO`, `ep.buscar_solicitacao_cancelamento`, `ep._remover_dedicado`, `ep._agora`, `ep.rotulo_envio`), `stokki.cancelar.montar_motivo`.
- Produces:
  - `SOZINHO = "SOZINHO"`, `OPERACAO = "OPERACAO"`
  - `decidir(servicos: list[dict], rotas: dict[int, dict], agent_lalamove: int = 0) -> tuple[str, str]`
  - `servicos_do_pedido(conn, codigo_base: str) -> tuple[list[dict], dict[int, dict]]`
  - `processar_cancelamentos(conn, config, cancelar_vuupt, cancelar_stokki, avisar, agora=None) -> dict` com `{"cancelados", "operacao", "falhas"}`; `cancelar_vuupt(service_id) -> {"ok","erro"}`, `cancelar_stokki(id_stokki, motivo) -> {"ok","erro"}`, `avisar(envio, motivo, erro)`.
  - `id_stokki_do_codigo(codigo) -> int | None`.

- [ ] **Step 1: Acrescentar os testes ao `portal_cliente/test_cancelamento.py` (falham)**

```python
import cancelamento as cm  # noqa: E402  (logo abaixo de `import envio_pedidos as ep`)


def _serv(codigo, status, provedor="not_assigned", route=None, sid=1, excluido=None):
    return {"codigo": codigo, "vuupt_service_id": sid, "status": status, "status_provedor": provedor,
            "vuupt_route_id": route, "excluido_em": excluido}


class Decidir(unittest.TestCase):
    def test_sem_servico_cancela_sozinho(self):
        self.assertEqual(cm.decidir([], {})[0], cm.SOZINHO)

    def test_no_pool_sozinho(self):
        self.assertEqual(cm.decidir([_serv("PS-1", "ABERTO")], {})[0], cm.SOZINHO)

    def test_rota_planejada_nao_iniciada_sozinho(self):
        rotas = {10: {"status": "PLANEJADA", "status_provedor": "assigned", "iniciada_em": None, "agent_id": 5}}
        self.assertEqual(cm.decidir([_serv("PS-1", "EM_ROTA", "assigned", 10)], rotas)[0], cm.SOZINHO)

    def test_rota_iniciada_vai_pra_operacao(self):
        rotas = {10: {"status": "EM_ROTA", "status_provedor": "started", "iniciada_em": "2026-10-06 08:00:00", "agent_id": 5}}
        d, motivo = cm.decidir([_serv("PS-1", "EM_ROTA", "assigned", 10)], rotas)
        self.assertEqual(d, cm.OPERACAO)
        self.assertIn("em rota", motivo)

    def test_rota_lalamove_vai_pra_operacao(self):
        rotas = {10: {"status": "PLANEJADA", "status_provedor": "assigned", "iniciada_em": None, "agent_id": 99}}
        self.assertEqual(cm.decidir([_serv("PS-1", "EM_ROTA", "assigned", 10)], rotas, agent_lalamove=99)[0], cm.OPERACAO)

    def test_entregue_ou_insucesso_vai_pra_operacao(self):
        self.assertEqual(cm.decidir([_serv("PS-1", "ENTREGUE", "done")], {})[0], cm.OPERACAO)
        self.assertEqual(cm.decidir([_serv("PS-1", "INSUCESSO", "done")], {})[0], cm.OPERACAO)

    def test_servico_cancelado_ou_excluido_nao_conta(self):
        self.assertEqual(cm.decidir([_serv("PS-1", "CANCELADO", "canceled"), _serv("PS-1-R1", "ABERTO", sid=2)], {})[0], cm.SOZINHO)
        self.assertEqual(cm.decidir([_serv("PS-1", "ENTREGUE", "done", excluido="2026-10-01 00:00:00")], {})[0], cm.SOZINHO)


class IdStokki(unittest.TestCase):
    def test_codigos(self):
        self.assertEqual(cm.id_stokki_do_codigo("PS-39959"), 39959)
        self.assertEqual(cm.id_stokki_do_codigo("#PS-39959"), 39959)
        self.assertIsNone(cm.id_stokki_do_codigo(None))
        self.assertIsNone(cm.id_stokki_do_codigo("ABC"))


def conn_completo(envios=()):
    conn = conn_portal(envios)
    conn.executescript("""
        CREATE TABLE nucleo_pedidos (codigo TEXT, vuupt_service_id INTEGER, status TEXT, status_provedor TEXT, vuupt_route_id INTEGER, excluido_em TEXT);
        CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, status TEXT, status_provedor TEXT, iniciada_em TEXT, agent_id INTEGER);
    """)
    return conn


class Processar(unittest.TestCase):
    def setUp(self):
        self.vuupt_chamadas, self.stokki_chamadas, self.avisos = [], [], []
        self.vuupt_ok, self.stokki_ok = True, True

    def cancelar_vuupt(self, sid):
        self.vuupt_chamadas.append(sid)
        return {"ok": self.vuupt_ok, "erro": "" if self.vuupt_ok else "Vuupt: 409"}

    def cancelar_stokki(self, id_stokki, motivo):
        self.stokki_chamadas.append((id_stokki, motivo))
        return {"ok": self.stokki_ok, "erro": "" if self.stokki_ok else "Stokki 500"}

    def avisar(self, envio, motivo, erro):
        self.avisos.append((envio["id"], motivo, erro))

    def rodar(self, conn):
        return cm.processar_cancelamentos(conn, {"lalamove": {"agent_id_vuupt": 99}}, self.cancelar_vuupt, self.cancelar_stokki, self.avisar)

    def preparar(self, status_nucleo="ABERTO", route=None, rota=None, codigo="PS-39959"):
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", codigo)])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {"motivo": "cliente desistiu"}, "cliente")
        if status_nucleo:
            conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-39959', 111, ?, 'not_assigned', ?, NULL)", (status_nucleo, route))
        if rota:
            conn.execute("INSERT INTO nucleo_rotas VALUES (?, ?, ?, ?, ?)", rota)
        conn.commit()
        return conn

    def test_sozinho_cancela_nas_duas_pontas_e_conclui(self):
        conn = self.preparar()
        r = self.rodar(conn)
        self.assertEqual(r, {"cancelados": 1, "operacao": 0, "falhas": 0})
        self.assertEqual(self.vuupt_chamadas, [111])
        self.assertEqual(self.stokki_chamadas[0][0], 39959)
        self.assertIn("cliente desistiu", self.stokki_chamadas[0][1])
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELADO)
        s = conn.execute("SELECT status, resposta FROM portal_solicitacoes").fetchone()
        self.assertEqual(s["status"], "CONCLUIDA")
        self.assertIn("automaticamente", s["resposta"])
        self.assertEqual(self.avisos, [])

    def test_sem_servico_no_nucleo_cancela_so_na_stokki(self):
        conn = self.preparar(status_nucleo=None)
        self.assertEqual(self.rodar(conn)["cancelados"], 1)
        self.assertEqual(self.vuupt_chamadas, [])
        self.assertEqual(len(self.stokki_chamadas), 1)

    def test_em_rota_volta_pra_criado_e_avisa(self):
        conn = self.preparar("EM_ROTA", 10, (10, "EM_ROTA", "started", "2026-10-06 08:00:00", 5))
        r = self.rodar(conn)
        self.assertEqual(r, {"cancelados": 0, "operacao": 1, "falhas": 0})
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)
        self.assertEqual(conn.execute("SELECT status FROM portal_solicitacoes").fetchone()[0], "PENDENTE")
        self.assertEqual(self.vuupt_chamadas + self.stokki_chamadas, [])
        self.assertEqual(self.avisos[0][0], 50)
        self.assertIn("em rota", self.avisos[0][1])

    def test_falha_na_vuupt_nao_toca_a_stokki(self):
        self.vuupt_ok = False
        conn = self.preparar()
        r = self.rodar(conn)
        self.assertEqual(r["falhas"], 1)
        self.assertEqual(self.stokki_chamadas, [])
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)
        self.assertIn("409", self.avisos[0][2])

    def test_falha_na_stokki_volta_pra_criado(self):
        self.stokki_ok = False
        conn = self.preparar()
        self.assertEqual(self.rodar(conn)["falhas"], 1)
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)
        self.assertIn("Stokki 500", self.avisos[0][2])

    def test_sem_codigo_vai_pra_operacao(self):
        conn = self.preparar(status_nucleo=None, codigo=None)
        self.assertEqual(self.rodar(conn)["operacao"], 1)
        self.assertIn("não identificado", self.avisos[0][1])

    def test_cancela_todas_as_reentregas_vivas(self):
        conn = self.preparar("CANCELADO")
        conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-39959-R1', 222, 'ABERTO', 'not_assigned', NULL, NULL)")
        conn.commit()
        self.assertEqual(self.rodar(conn)["cancelados"], 1)
        self.assertEqual(self.vuupt_chamadas, [222])

    def test_nada_a_fazer(self):
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        self.assertEqual(self.rodar(conn), {"cancelados": 0, "operacao": 0, "falhas": 0})
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_cancelamento`
Expected: `ModuleNotFoundError: No module named 'cancelamento'`.

- [ ] **Step 3: Implementar `portal_cliente/cancelamento.py`**

```python
# -*- coding: utf-8 -*-
"""
portal_cliente/cancelamento.py

Executa, no worker (enviar_stokki.ciclo), os cancelamentos que o cliente
pediu no portal (portal_envios.status = CANCELANDO) -- ver
DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md.

Regra "pode cancelar sozinho" (decidir): lida do núcleo, sem chamar a
Vuupt. Precisa da operação se algum serviço vivo já foi ENTREGUE /
INSUCESSO ou se a rota dele já começou (nucleo_rotas EM_ROTA / started /
iniciada_em) ou é da Lalamove. Atenção: nucleo_pedidos.status = EM_ROTA
significa só "atribuído a uma rota" (status_provedor = assigned).

Três fins: CANCELADO (as duas pontas), CRIADO + solicitação PENDENTE +
aviso (precisa da operação ou falha técnica). Nunca fica tentando sozinho.
"""
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

logger = logging.getLogger("portal_cancelamento")

SOZINHO = "SOZINHO"
OPERACAO = "OPERACAO"
_RE_PS = re.compile(r"PS-(\d+)")
_ROTA_COMECOU = ("EM_ROTA", "CONCLUIDA")
_PROVEDOR_ROTA_COMECOU = ("started", "finished")


def id_stokki_do_codigo(codigo) -> int | None:
    m = _RE_PS.search(str(codigo or "").upper())
    return int(m.group(1)) if m else None


def _vivos(servicos: list[dict]) -> list[dict]:
    return [s for s in servicos if not s.get("excluido_em")
            and s.get("status") != "CANCELADO" and s.get("status_provedor") != "canceled"]


def decidir(servicos: list[dict], rotas: dict[int, dict], agent_lalamove: int = 0) -> tuple[str, str]:
    """(SOZINHO | OPERACAO, motivo em texto curto)."""
    vivos = _vivos(servicos)
    if not vivos:
        return SOZINHO, "sem serviço vivo na Vuupt"
    for s in vivos:
        if s.get("status") == "ENTREGUE":
            return OPERACAO, f"pedido já entregue ({s['codigo']})"
        if s.get("status") == "INSUCESSO":
            return OPERACAO, f"insucesso em tratamento ({s['codigo']})"
        rota = rotas.get(s.get("vuupt_route_id")) if s.get("vuupt_route_id") else None
        if rota:
            if agent_lalamove and rota.get("agent_id") == agent_lalamove:
                return OPERACAO, f"rota Lalamove ({s['codigo']}): cancelar na Lalamove à mão"
            if rota.get("status") in _ROTA_COMECOU or rota.get("status_provedor") in _PROVEDOR_ROTA_COMECOU or rota.get("iniciada_em"):
                return OPERACAO, f"motorista em rota ({s['codigo']}, rota {s['vuupt_route_id']})"
    return SOZINHO, "pool, rascunho ou rota não iniciada"


def servicos_do_pedido(conn: sqlite3.Connection, codigo_base: str) -> tuple[list[dict], dict[int, dict]]:
    """Linhas de nucleo_pedidos do pedido-base e reentregas (-R1, -C1...) e
    as rotas delas. Tabelas podem não existir no banco local de teste."""
    try:
        rows = conn.execute("""
            SELECT codigo, vuupt_service_id, status, status_provedor, vuupt_route_id, excluido_em
            FROM nucleo_pedidos WHERE codigo = ? OR codigo LIKE ? OR codigo LIKE ?
        """, (codigo_base, f"{codigo_base}-R%", f"{codigo_base}-C%")).fetchall()
    except sqlite3.OperationalError:
        return [], {}
    servicos = [dict(r) for r in rows]
    ids = [s["vuupt_route_id"] for s in servicos if s.get("vuupt_route_id")]
    rotas = {}
    if ids:
        marcas = ",".join("?" * len(ids))
        for r in conn.execute(f"SELECT id, status, status_provedor, iniciada_em, agent_id FROM nucleo_rotas WHERE id IN ({marcas})", ids):
            rotas[r["id"]] = dict(r)
    return servicos, rotas


def _voltar_pra_operacao(conn, envio: dict, agora: str) -> None:
    conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?", (ep.STATUS_CRIADO, agora, envio["id"]))
    conn.commit()


def _concluir(conn, envio: dict, agora: str, quando: datetime) -> None:
    sol = ep.buscar_solicitacao_cancelamento(conn, envio["id"])
    conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?", (ep.STATUS_CANCELADO, agora, envio["id"]))
    if sol:
        conn.execute("UPDATE portal_solicitacoes SET status = 'CONCLUIDA', resposta = ?, concluido_em = ? WHERE id = ?",
                     (f"cancelado automaticamente em {quando.strftime('%d/%m %H:%M')}", agora, sol["id"]))
    ep._remover_dedicado(conn, envio["id"], "cancelamento-automatico")
    conn.commit()


def processar_cancelamentos(conn: sqlite3.Connection, config: dict, cancelar_vuupt, cancelar_stokki, avisar,
                            agora: datetime | None = None) -> dict:
    """`cancelar_vuupt(service_id)` e `cancelar_stokki(id_stokki, motivo)`
    devolvem {"ok", "erro"}; `avisar(envio, motivo, erro)` manda e-mail +
    WhatsApp. Injetados pra teste; o worker passa os reais."""
    quando = agora or datetime.now()
    agora_txt = quando.strftime("%Y-%m-%d %H:%M:%S")
    agent_lalamove = int((config.get("lalamove") or {}).get("agent_id_vuupt") or 0)
    total = {"cancelados": 0, "operacao": 0, "falhas": 0}
    rows = conn.execute("SELECT * FROM portal_envios WHERE status = ? ORDER BY id", (ep.STATUS_CANCELANDO,)).fetchall()
    for r in rows:
        envio = dict(r)
        sol = ep.buscar_solicitacao_cancelamento(conn, envio["id"]) or {}
        motivo_cliente = sol.get("detalhes") or ""
        rotulo = ep.rotulo_envio(envio)
        id_stokki = id_stokki_do_codigo(envio.get("codigo_pedido"))
        if not id_stokki:
            _voltar_pra_operacao(conn, envio, agora_txt)
            avisar(envio, "código do pedido ainda não identificado", "")
            total["operacao"] += 1
            continue

        servicos, rotas = servicos_do_pedido(conn, f"PS-{id_stokki}")
        decisao, motivo = decidir(servicos, rotas, agent_lalamove)
        if decisao == OPERACAO:
            logger.info(f"{rotulo} PS-{id_stokki}: precisa da operação ({motivo}).")
            _voltar_pra_operacao(conn, envio, agora_txt)
            avisar(envio, motivo, "")
            total["operacao"] += 1
            continue

        erro = ""
        for s in _vivos(servicos):
            res = cancelar_vuupt(s["vuupt_service_id"])
            if not res.get("ok"):
                erro = f"Vuupt ({s['codigo']}): {res.get('erro')}"
                break
        if not erro:
            res = cancelar_stokki(id_stokki, motivo_cliente)
            if not res.get("ok"):
                erro = f"Stokki: {res.get('erro')}"
        if erro:
            logger.warning(f"{rotulo} PS-{id_stokki}: cancelamento falhou -- {erro}")
            _voltar_pra_operacao(conn, envio, agora_txt)
            avisar(envio, "falha técnica no cancelamento automático", erro)
            total["falhas"] += 1
            continue

        _concluir(conn, envio, agora_txt, quando)
        logger.info(f"{rotulo} PS-{id_stokki}: cancelado na Vuupt e na Stokki.")
        total["cancelados"] += 1
    return total
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_cancelamento`
Expected: `OK` (todas as classes).

- [ ] **Step 5: Commit**

```bash
git add portal_cliente/cancelamento.py portal_cliente/test_cancelamento.py
git commit -m "Portal: regra de cancelamento sozinho x operacao e processamento dos CANCELANDO"
```

---

### Task 5: Avisos — WhatsApp no grupo do atendimento e e-mail da operação

**Files:**
- Modify: `notificar_whatsapp.py` (depois de `avisar_nao_expedidos`, ~linha 583)
- Modify: `portal_cliente/cancelamento.py` (acrescentar `avisar_operacao`)
- Test: `test_notificar_whatsapp_cancelamento.py` (raiz, novo), `portal_cliente/test_cancelamento.py` (classe nova)

**Interfaces:**
- Produces: `notificar_whatsapp.texto_cancelamento_pendente(envio: dict, motivo: str, erro: str) -> str`; `notificar_whatsapp.avisar_cancelamento_pendente(envio, motivo, erro, config, **kw) -> str` (usa `despachar(..., grupo_id=cfg["grupo_atendimento_id"])`, origem `"portal_cancelamento"`, tipo `"cancelamento"`, assinatura `f"cancelamento:{envio['id']}"`); `cancelamento.avisar_operacao(config, envio, motivo, erro) -> None` (e-mail + WhatsApp; nunca levanta).

- [ ] **Step 1: Testes (falham)**

```python
# test_notificar_whatsapp_cancelamento.py
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_notificar_whatsapp_cancelamento"""
import unittest
from unittest import mock

import notificar_whatsapp as nw

ENVIO = {"id": 50, "numero_nf": "9959", "codigo_pedido": "PS-39959", "destinatario_nome": "LOURENCO DISTRIBUIDORA",
         "cnpj_embarcador": "22135070000190", "nome_embarcador": "MARIA DOLORES"}


class TextoCancelamento(unittest.TestCase):
    def test_texto_curto_com_motivo(self):
        t = nw.texto_cancelamento_pendente(ENVIO, "motorista em rota (PS-39959, rota 10)", "")
        self.assertIn("NF 9959", t)
        self.assertIn("PS-39959", t)
        self.assertIn("MARIA DOLORES", t)
        self.assertIn("motorista em rota", t)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)

    def test_texto_com_erro(self):
        t = nw.texto_cancelamento_pendente(ENVIO, "falha técnica no cancelamento automático", "Stokki 500: x" * 20)
        self.assertIn("falhou", t)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)


class AvisarCancelamento(unittest.TestCase):
    def test_vai_pro_grupo_do_atendimento_com_assinatura(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "grupo_id": "g@g.us", "grupo_atendimento_id": "atend@g.us"}}
        with mock.patch.object(nw, "despachar", return_value="enviado") as d:
            self.assertEqual(nw.avisar_cancelamento_pendente(ENVIO, "motorista em rota", "", config), "enviado")
        kw = d.call_args.kwargs
        self.assertEqual(kw["grupo_id"], "atend@g.us")
        self.assertEqual(d.call_args.args[1:3], ("portal_cancelamento", "cancelamento"))
        self.assertEqual(d.call_args.args[4], "cancelamento:50")

    def test_sem_grupo_do_atendimento_nao_envia(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "grupo_id": "g@g.us"}}
        with mock.patch.object(nw, "despachar") as d:
            self.assertEqual(nw.avisar_cancelamento_pendente(ENVIO, "x", "", config), "desligado")
        d.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

E em `portal_cliente/test_cancelamento.py`:

```python
class AvisarOperacao(unittest.TestCase):
    def test_manda_email_e_whatsapp_e_nunca_levanta(self):
        from unittest import mock
        config = {"email": {"email_atendimento": "atendimento@x.com"}, "whatsapp_notificacoes": {}}
        e = {"id": 50, "numero_nf": "9959", "codigo_pedido": "PS-39959", "destinatario_nome": "D", "cnpj_embarcador": "111"}
        with mock.patch.object(cm, "enviar_email", return_value=True) as em, \
             mock.patch("notificar_whatsapp.avisar_cancelamento_pendente", return_value="modo_teste") as wa:
            cm.avisar_operacao(config, e, "motorista em rota", "")
        self.assertEqual(em.call_args.args[0], ["atendimento@x.com"])
        self.assertIn("PS-39959", em.call_args.args[1])
        self.assertIn("motorista em rota", em.call_args.args[2])
        wa.assert_called_once()
        with mock.patch.object(cm, "enviar_email", side_effect=RuntimeError("smtp fora")):
            cm.avisar_operacao(config, e, "x", "")  # nao levanta
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_notificar_whatsapp_cancelamento portal_cliente.test_cancelamento`
Expected: `AttributeError: ... has no attribute 'texto_cancelamento_pendente'` / `'avisar_operacao'`.

- [ ] **Step 3: `notificar_whatsapp.py`** (acrescentar depois de `avisar_nao_expedidos`)

```python
def texto_cancelamento_pendente(envio: dict, motivo: str, erro: str) -> str:
    """Cancelamento pedido pelo cliente no portal que o worker nao conseguiu
    fazer sozinho (06/10). Cabe em MAX_MENSAGEM."""
    nf = envio.get("numero_nf") or envio.get("referencia") or "?"
    ps = envio.get("codigo_pedido") or "sem PS"
    emb = (envio.get("nome_embarcador") or envio.get("cnpj_embarcador") or "")[:30]
    detalhe = f"falhou: {erro}" if erro else motivo
    texto = f"Cancelamento pedido pelo cliente: NF {nf} · {ps} · {emb} · {detalhe} -- precisa cancelar à mão"
    return texto[:MAX_MENSAGEM - 1] + "…" if len(texto) > MAX_MENSAGEM else texto


def avisar_cancelamento_pendente(envio: dict, motivo: str, erro: str, config: dict, **kw) -> str:
    """Grupo do ATENDIMENTO (grupo_atendimento_id), nao o dos alertas.
    Assinatura por envio: o mesmo pedido nao repete no dia."""
    try:
        grupo = _cfg(config).get("grupo_atendimento_id")
        if not grupo:
            return "desligado"
        return despachar(config, "portal_cancelamento", "cancelamento",
                         texto_cancelamento_pendente(envio, motivo, erro), f"cancelamento:{envio['id']}",
                         grupo_id=grupo, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"
```

- [ ] **Step 4: `portal_cliente/cancelamento.py` — `avisar_operacao`** (acrescentar no fim; no topo: `from email_utils import enviar_email, envelope_html`)

```python
def avisar_operacao(config: dict, envio: dict, motivo: str, erro: str) -> None:
    """E-mail ao atendimento + WhatsApp no grupo do atendimento. Best-effort:
    o envio já voltou pra CRIADO com a solicitação PENDENTE; o aviso não
    pode derrubar o ciclo."""
    rotulo = ep.rotulo_envio(envio)
    ps = envio.get("codigo_pedido") or "(código ainda não identificado)"
    try:
        email_cfg = config.get("email", {}) or {}
        destino = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
        if destino:
            html = envelope_html(
                f"<p>O cliente <b>{envio.get('nome_embarcador') or envio.get('cnpj_embarcador')}</b> pediu pelo portal o "
                f"<b>cancelamento</b> do pedido {ps} · {rotulo} · {envio.get('destinatario_nome') or ''}.</p>"
                f"<p>O sistema não cancelou sozinho: <b>{motivo}</b>.</p>"
                + (f"<p style='color:#B91C1C'>Erro: {erro[:400]}</p>" if erro else "")
                + "<p>Cancele na Vuupt e na Stokki e feche a solicitação: "
                  "<code>portal_cliente/gerenciar_clientes.py solicitacoes</code> / <code>concluir &lt;id&gt; \"motivo\"</code>.</p>",
                rodape="Fresh Log · Portal do cliente · cancelamento", cor_acento="#F5A623")
            enviar_email([destino], f"[Portal] Cancelamento precisa da operação · {rotulo} · {ps}", html, email_cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"e-mail do cancelamento {envio.get('id')} falhou: {e}")
    try:
        import notificar_whatsapp
        notificar_whatsapp.avisar_cancelamento_pendente(envio, motivo, erro, config)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"WhatsApp do cancelamento {envio.get('id')} falhou: {e}")
```

`nome_embarcador` não existe em `portal_envios`; o worker preenche a partir de `ep.config_stokki_cliente(conn, cnpj, config)["nome"]` (Tarefa 6) antes de chamar `avisar_operacao`. Se a chave faltar, cai no CNPJ.

- [ ] **Step 5: Rodar e ver passar**

Run: `py -3.11 -m unittest test_notificar_whatsapp_cancelamento portal_cliente.test_cancelamento`
Expected: `OK`. Rodar também `py -3.11 -m unittest test_notificar_whatsapp` (os testes antigos do módulo seguem verdes).

- [ ] **Step 6: Commit**

```bash
git add notificar_whatsapp.py test_notificar_whatsapp_cancelamento.py portal_cliente/cancelamento.py portal_cliente/test_cancelamento.py
git commit -m "Portal: cancelamento que precisa da operacao avisa por e-mail e no grupo de WhatsApp do atendimento"
```

---

### Task 6: Worker — processar CANCELANDO dentro da trava, e órfão de 30 min

**Files:**
- Modify: `portal_cliente/enviar_stokki.py` (`ciclo()` 746-772; imports 61-64)
- Test: `portal_cliente/test_cancelamento.py` (classe `Worker`)

**Interfaces:**
- Consumes: `cancelamento.processar_cancelamentos`, `cancelamento.avisar_operacao`, `stokki.cancelar.cancelar_pedido`, `roteirizacao.cancelar_servico.cancelar_servico_completo`, `stokki.auth.StokkiSession`, `sessao_uso.adquirir/liberar`.
- Produces: `enviar_stokki.processar_cancelamentos_do_ciclo(conn, config, simular=False) -> dict` chamado por `ciclo()`; resultado do ciclo ganha a chave `"cancelamentos"`.

- [ ] **Step 1: Teste (falha)** — em `portal_cliente/test_cancelamento.py`

```python
class Worker(unittest.TestCase):
    def test_ciclo_processa_cancelando_sob_a_trava(self):
        from unittest import mock
        import enviar_stokki as es
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        with mock.patch.object(es.sessao_uso, "adquirir", return_value=True) as adq, \
             mock.patch.object(es.sessao_uso, "liberar") as lib, \
             mock.patch.object(es, "StokkiSession") as sess, \
             mock.patch.object(es.cm, "processar_cancelamentos", return_value={"cancelados": 1, "operacao": 0, "falhas": 0}) as proc:
            r = es.processar_cancelamentos_do_ciclo(conn, {"vuupt_api": {"token": "t"}})
        self.assertEqual(r["cancelados"], 1)
        adq.assert_called_once()
        lib.assert_called_once_with(es.DONO_TRAVA)
        sess.assert_called_once()
        self.assertEqual(proc.call_args.args[0], conn)

    def test_sem_cancelando_nao_abre_sessao(self):
        from unittest import mock
        import enviar_stokki as es
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        with mock.patch.object(es, "StokkiSession") as sess, mock.patch.object(es.sessao_uso, "adquirir") as adq:
            self.assertEqual(es.processar_cancelamentos_do_ciclo(conn, {}), {"cancelados": 0, "operacao": 0, "falhas": 0})
        sess.assert_not_called()
        adq.assert_not_called()

    def test_trava_ocupada_deixa_pro_proximo_ciclo(self):
        from unittest import mock
        import enviar_stokki as es
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        with mock.patch.object(es.sessao_uso, "adquirir", return_value=False), mock.patch.object(es.sessao_uso, "em_uso", return_value="pipeline"):
            es.processar_cancelamentos_do_ciclo(conn, {})
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELANDO)

    def test_cancelando_orfao_volta_pra_criado(self):
        import enviar_stokki as es
        conn = conn_completo([(50, ep.STATUS_CANCELANDO, "9959", "PS-39959")])
        conn.execute("UPDATE portal_envios SET atualizado_em = datetime('now','localtime','-31 minutes') WHERE id = 50")
        conn.commit()
        es.resetar_orfaos(conn)
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)
```

`enviar_stokki` importa `yaml` e `pedidos_dedicados`; ambos existem no ambiente local. Se o import do módulo no teste puxar o importador do repo separado, não: `carregar_importador` só roda dentro de `processar_lote`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_cancelamento.Worker`
Expected: `AttributeError: module 'enviar_stokki' has no attribute 'processar_cancelamentos_do_ciclo'`.

- [ ] **Step 3: Implementar em `enviar_stokki.py`**

Imports (linha 61-64):
```python
import cancelamento as cm
import envio_pedidos as ep
import pedidos_dedicados
from email_utils import enviar_email, envelope_html
from stokki import sessao_uso
from stokki.auth import StokkiSession
```

Funções novas (antes de `ciclo`):
```python
# ── Cancelamentos pedidos pelo cliente (06/10) ────────────────────────────────

def resetar_orfaos(conn) -> None:
    """ENVIANDO/CANCELANDO órfão (worker caiu no meio) volta depois de 30 min."""
    conn.execute("UPDATE portal_envios SET status = 'NA_FILA', atualizado_em = datetime('now','localtime') "
                 "WHERE status = 'ENVIANDO' AND atualizado_em < datetime('now','localtime','-30 minutes')")
    conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = datetime('now','localtime') "
                 "WHERE status = ? AND atualizado_em < datetime('now','localtime','-30 minutes')",
                 (ep.STATUS_CRIADO, ep.STATUS_CANCELANDO))
    conn.commit()


def processar_cancelamentos_do_ciclo(conn, config: dict, simular: bool = False) -> dict:
    """Cancela na Vuupt e na Stokki o que o cliente pediu (status CANCELANDO),
    dentro da trava da Stokki. Sem CANCELANDO não abre sessão nenhuma."""
    vazio = {"cancelados": 0, "operacao": 0, "falhas": 0}
    n = conn.execute("SELECT COUNT(*) FROM portal_envios WHERE status = ?", (ep.STATUS_CANCELANDO,)).fetchone()[0]
    if not n:
        return vazio
    if simular:
        logger.info(f"SIMULAÇÃO: {n} cancelamento(s) ficam como estão.")
        return vazio
    if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=10 * 60, esperar_segundos=60, alternativa=True):
        logger.info(f"Stokki ocupada por '{sessao_uso.em_uso()}' -- {n} cancelamento(s) esperam o próximo ciclo.")
        return vazio
    try:
        sessao = StokkiSession(config)
        token = (config.get("vuupt_api") or {}).get("token", "")

        def cancelar_vuupt(service_id):
            from roteirizacao.cancelar_servico import cancelar_servico_completo
            return cancelar_servico_completo(token, service_id)

        def cancelar_stokki(id_stokki, motivo):
            from stokki.cancelar import cancelar_pedido
            return cancelar_pedido(sessao, id_stokki, motivo)

        def avisar(envio, motivo, erro):
            try:
                envio = {**envio, "nome_embarcador": ep.config_stokki_cliente(conn, envio["cnpj_embarcador"], config)["nome"]}
            except Exception:  # noqa: BLE001 -- sem nome, o aviso sai com o CNPJ
                pass
            cm.avisar_operacao(config, envio, motivo, erro)

        return cm.processar_cancelamentos(conn, config, cancelar_vuupt, cancelar_stokki, avisar)
    finally:
        sessao_uso.liberar(DONO_TRAVA)
```

Em `ciclo()`: substituir as linhas 751-754 (o UPDATE do ENVIANDO órfão) por `resetar_orfaos(conn)`, e logo depois acrescentar:
```python
        total = {"lotes": 0, "criados": 0, "duplicados": 0, "erros": 0, "adiados": 0}
        try:
            total["cancelamentos"] = processar_cancelamentos_do_ciclo(conn, config, simular=simular)
        except Exception as e:  # noqa: BLE001 -- cancelamento não derruba a fila de envios
            logger.error(f"cancelamentos falharam: {e}\n{traceback.format_exc()}")
```
(mover a criação de `total` pra antes do laço dos lotes, que hoje está na linha 759, e remover a duplicata.) No `main`, o log do ciclo passa a sair também quando `r.get("cancelamentos", {}).get("cancelados")` ou `operacao`/`falhas` for > 0:
```python
                if r["lotes"] or any((r.get("cancelamentos") or {}).values()):
                    logger.info(f"ciclo: {r}")
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_cancelamento`
Expected: `OK`. Depois a suíte do portal: `py -3.11 -m unittest discover -s portal_cliente -p "test_*.py" -t .` (o único erro aceitável é `test_notificacoes_portal...no such table: interno`, pré-existente na worktree sem `dados.db`).

- [ ] **Step 5: Commit**

```bash
git add portal_cliente/enviar_stokki.py portal_cliente/test_cancelamento.py
git commit -m "Portal: worker de envios executa os cancelamentos pedidos pelo cliente dentro da trava da Stokki"
```

---

### Task 7: Resposta da API e tela — "Cancelamento em andamento"

**Files:**
- Modify: `portal_cliente/app.py:857-882` (`api_envios_acao`)
- Modify: `portal_cliente/templates/_envios.html` (handler do botão cancelar; procurar `data-acao="cancelar"` e a função que trata a resposta, ~linhas 460-520)

**Interfaces:**
- Consumes: `aplicar_acao` devolvendo `em_andamento: True` (Tarefa 3).

- [ ] **Step 1: `app.py`** — nada muda na lógica: `precisa_operacao` vem `False`, então não há e-mail; `resultado` já volta inteiro no JSON (`{"ok": True, **resultado}`). Conferir que a linha 876 continua `if resultado.get("precisa_operacao"):`. Sem edição se for isso.

- [ ] **Step 2: Template** — `carregar()` (linha 201-215 de `_envios.html`) já recarrega a cada 20 s enquanto houver `NA_FILA`/`ENVIANDO`. Incluir o `CANCELANDO` nessa condição (linha 213):

```js
    // 06/10: cancelamento roda no worker (ciclo de 20 s): recarrega enquanto houver "Cancelando"
    const ativo = dados && dados.envios.some(x => x.status === 'NA_FILA' || x.status === 'ENVIANDO' || x.status === 'CANCELANDO');
```
Nada mais muda na tela: o badge usa `status_rotulo` e os botões usam `pode_*` (Tarefa 3).

- [ ] **Step 3: Testar à mão no portal local**

Run (de dentro de `portal_cliente/`, porta livre): `py -3.11 -c "import app; app.app.run(host='127.0.0.1', port=8098)"` e abrir `http://127.0.0.1:8098/` logado como o cliente de teste (memória `reference_cliente_teste_portal`: CNPJ 00.000.000/0001-91 · PIN 123456). Clicar "Cancelar" num envio `CRIADO` de teste: badge vira "Cancelando", botões somem, mensagem "Cancelamento em andamento". Com o worker parado o envio fica `CANCELANDO` (esperado) — rodar `py -3.11 portal_cliente/enviar_stokki.py --uma-vez --simular` e ver o log "SIMULAÇÃO: 1 cancelamento(s)".

- [ ] **Step 4: Commit**

```bash
git add portal_cliente/templates/_envios.html portal_cliente/app.py
git commit -m "Portal: tela recarrega sozinha enquanto um cancelamento esta em andamento"
```
(se `app.py` não mudou, não incluir.)

---

### Task 8: Mapa do sistema, spec e memória

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (seções `portal_cliente/` ~linha 635, `stokki/`, `roteirizacao/`, e o fluxo "Envio de pedido pelo cliente" ~97)
- Modify: `DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md` (status → implementado, sem deploy)

- [ ] **Step 1: Mapa** — acrescentar nas seções certas:

```
 portal_cliente/cancelamento.py  Cancelamento pedido pelo cliente (status CANCELANDO):
                           regra sozinho x operação (núcleo) + execução no worker.
 stokki/cancelar.py        Cancela pedido de saída por HTTP (POST outbound/cancel + poll).
 roteirizacao/cancelar_servico.py  Cancela serviço na Vuupt (pool/rascunho/rota enviada)
                           fora do painel; PUT /cancel. Planejamento NÃO usa (escopo B, 06/10).
```
e no fluxo do portal (linha ~97): `Cancelar pedido já criado -> CANCELANDO -> worker enviar_stokki (cancelamento.py) -> CANCELADO | CRIADO + solicitação PENDENTE (e-mail + WhatsApp atendimento)`.

- [ ] **Step 2: Spec** — trocar `Status: **especificado, não implementado**` por `Status: **implementado em <data>, sem deploy** (commits do ramo cancelar-agendamento)`; e na peça `roteirizacao/cancelar_servico.py`, registrar que o Planejamento **não** passou a chamá-la (duplicação consciente, escopo B).

- [ ] **Step 3: Commit**

```bash
git add MAPA_DO_SISTEMA.txt DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md
git commit -m "Docs: mapa e spec do cancelamento pelo portal"
```

---

### Task 9: Depois do deploy (só com o Hugo pedindo "deploy")

Não é código; fica aqui pra não esquecer. Deploy pelo skill `deploy-vps` com `--restart portal-cliente portal-cliente-envios` (o worker importa `cancelamento.py`; o `portal-cliente` importa `envio_pedidos.py`).

- [ ] **Step 1: Acertar os dados antigos** (VPS, como www-data):

```
cd /opt/stokki-eventos
sudo -u www-data venv/bin/python portal_cliente/gerenciar_clientes.py concluir 3 "cancelado em 06/10 (Vuupt pela operacao em 05/10, Stokki pelo spike)"
sudo -u www-data venv/bin/python portal_cliente/gerenciar_clientes.py concluir 4 "resolvido por outro caminho (data definida no portal)"
sudo -u www-data venv/bin/python portal_cliente/gerenciar_clientes.py concluir 22 "resolvido por outro caminho (data definida no portal)"
```
Conferir: envio 50 `CANCELADO` (`concluir` do #3 marca sozinho); `solicitacoes` sem pendências.

- [ ] **Step 2: Prova real** — criar um envio pro cliente de teste (CNPJ 00.000.000/0001-91) pelo portal, esperar virar `CRIADO`, clicar "Cancelar", e em até 40 s ver `CANCELADO` no portal; conferir `canceled` na Vuupt (`nucleo_pedidos`) e "Cancelado" na Stokki (`stokki/cancelar.situacao_e_token`). Log: `journalctl -u portal-cliente-envios -n 40`.

- [ ] **Step 3: Prova do caminho da operação** — num envio de teste cujo serviço esteja numa rota iniciada (ou forçando `decidir` com `--modo-teste` do WhatsApp), ver e-mail em `hugo@` e o texto no log `[MODO TESTE] WhatsApp nao enviado`.
