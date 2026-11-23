# Canhoto na tela de baixa sem app — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** na tela `/baixa-sem-app`, anexar (opcional) o canhoto de cada pedido entregue; o canhoto é gravado antes da conclusão na Vuupt, a expedição o anexa na Stokki quando a Vuupt não tiver, e o batimento o conta como comprovante.

**Architecture:** módulo novo `nucleo/canhotos_manuais.py` (converte para PDF, grava em `dados/canhotos_manuais/{PS}.pdf`, cópia no GCS, tabela `canhotos_manuais`, busca por código). O POST da tela vira multipart e grava os canhotos antes de criar o lote. `expedir_pedidos.py` (raiz) ganha `pdf_do_canhoto()` com fallback para o canhoto manual. `batimento/medir.py` reconhece a fonte `painel`.

**Tech Stack:** Python 3.11, Flask, SQLite, Pillow 12 (sem `pillow_heif`), `documentos_pedido.storage_gcs.enviar_documento`, unittest.

**Spec:** `docs/superpowers/specs/2026-10-08-motoristas-sem-app-design.md`, seção 7.

## Global Constraints

- Canhoto **opcional**; só para item marcado "Entregue".
- Tipos aceitos: JPEG, PNG, PDF; até **15 MB** por arquivo. HEIC recusado com mensagem (sem `pillow_heif` no servidor).
- Imagem vira PDF de 1 página; PDF é guardado como veio.
- Arquivo: `dados/canhotos_manuais/{PS}.pdf` (código normalizado: sem `#`, maiúsculo; reenvio substitui). GCS best-effort via `enviar_documento(config, caminho, codigo, "Canhoto")`.
- Os canhotos são gravados **antes** de `criar_lote` (a expedição roda a cada 30 min e a Stokki não aceita anexo em pedido expedido).
- Expedição: muda o `expedir_pedidos.py` **da raiz** (é o que roda em produção; existe outra cópia em `insucesso_entrega/`, não mexer).
- Batimento: ordem de evidência app > vuupt_foto > **painel** > expedicao_anexou.
- `py -3.11`; unittest; testes do painel em comando separado. Commit só com ok do Hugo; deploy reinicia só `painel-agentes` (expedição e batimento são timers).

## Review Focus

1. Arquivo inválido num item e válido em outro: nada é gravado e o POST volta 400 (validar tudo antes de gravar) — teste na Task 2.
2. Canhoto num item marcado "voltou": ignorado (não grava) — teste na Task 2.
3. Pedido reentregue (`#PS-1-R1`) com canhoto manual em `PS-1-R1`: a expedição acha pelo código exato — teste na Task 3.
4. Foto de celular girada (EXIF): PDF sai na orientação certa — `ImageOps.exif_transpose` na Task 1 (teste com imagem com tag de orientação).
5. Requisição maior que o limite do Flask: conferir `MAX_CONTENT_LENGTH` do painel (Task 2, passo 1) para não recusar 3 fotos de 5 MB.

---

### Task 1: `nucleo/canhotos_manuais.py`

**Files:** Create `nucleo/canhotos_manuais.py`; Test `nucleo/test_canhotos_manuais.py`

**Interfaces — Produces:**
- `class CanhotoInvalido(ValueError)`
- `normalizar(codigo) -> str`
- `para_pdf(conteudo: bytes) -> bytes` (levanta `CanhotoInvalido`)
- `salvar(conn, codigo: str, service_id: int | None, pdf: bytes, por: str, config: dict | None = None) -> dict` (`pdf` já convertido; grava arquivo, GCS best-effort, upsert na tabela; devolve `{"codigo", "caminho", "caminho_gcs"}`)
- `caminho_canhoto_manual(codigo: str, db_path=None) -> Path | None`

- [ ] **Step 1: Teste que falha** — `nucleo/test_canhotos_manuais.py`:

```python
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest nucleo.test_canhotos_manuais"""
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from nucleo import banco
from nucleo import canhotos_manuais as cm


def _jpeg(orientacao=None, tamanho=(40, 20)):
    img = Image.new("RGB", tamanho, "white")
    buf = io.BytesIO()
    if orientacao:
        exif = Image.Exif()
        exif[0x0112] = orientacao
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


class ParaPdf(unittest.TestCase):
    def test_jpeg_vira_pdf(self):
        self.assertTrue(cm.para_pdf(_jpeg()).startswith(b"%PDF-"))

    def test_pdf_passa_igual(self):
        pdf = b"%PDF-1.4\n%fake\n"
        self.assertEqual(cm.para_pdf(pdf), pdf)

    def test_recusa_grande_heic_e_lixo(self):
        with self.assertRaises(cm.CanhotoInvalido):
            cm.para_pdf(b"%PDF-" + b"0" * (cm.LIMITE_BYTES + 1))
        with self.assertRaises(cm.CanhotoInvalido):
            cm.para_pdf(b"\x00\x00\x00\x18ftypheic" + b"0" * 50)
        with self.assertRaises(cm.CanhotoInvalido):
            cm.para_pdf(b"nao sou imagem")

    def test_orientacao_exif_aplicada(self):
        pdf = cm.para_pdf(_jpeg(orientacao=6, tamanho=(40, 20)))   # 6 = girar 90: vira retrato
        self.assertIn(b"/MediaBox", pdf)
        mb = pdf.split(b"/MediaBox")[1].split(b"]")[0]
        nums = [float(x) for x in mb.replace(b"[", b" ").split()]
        self.assertGreater(nums[3], nums[2])                       # altura > largura


class Salvar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        p = mock.patch.object(banco, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)

    def test_grava_busca_e_substitui(self):
        conn = banco.conectar(self.db)
        try:
            r = cm.salvar(conn, "#ps-1-r1", 10, b"%PDF-1 a", "hugo")
            self.assertEqual(r["codigo"], "PS-1-R1")
            cm.salvar(conn, "PS-1-R1", 10, b"%PDF-1 b", "hugo")
        finally:
            conn.close()
        caminho = cm.caminho_canhoto_manual("#PS-1-R1", db_path=self.db)
        self.assertEqual(caminho.read_bytes(), b"%PDF-1 b")
        self.assertEqual(caminho.parent, self.db.parent / "canhotos_manuais")
        self.assertIsNone(cm.caminho_canhoto_manual("PS-2", db_path=self.db))

    def test_gcs_falha_nao_impede(self):
        conn = banco.conectar(self.db)
        try:
            with mock.patch("documentos_pedido.storage_gcs.enviar_documento", side_effect=RuntimeError("403")):
                r = cm.salvar(conn, "PS-3", 1, b"%PDF-1", "hugo", config={"gcs": {"bucket_name": "x"}})
        finally:
            conn.close()
        self.assertIsNone(r["caminho_gcs"])
        self.assertIsNotNone(cm.caminho_canhoto_manual("PS-3", db_path=self.db))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Ver falhar** — `py -3.11 -m unittest nucleo.test_canhotos_manuais` → ImportError.

- [ ] **Step 3: Implementar** — `nucleo/canhotos_manuais.py`:

```python
# -*- coding: utf-8 -*-
"""
nucleo/canhotos_manuais.py

Canhoto enviado pela tela de baixa de motorista sem app (Hugo, 09/10):
foto/PDF vira PDF em dados/canhotos_manuais/{PS}.pdf (+ copia no GCS) e a
expedicao (expedir_pedidos.py da raiz) anexa na Stokki quando a Vuupt
nao tem canhoto. Tem que existir ANTES da conclusao na Vuupt: a Stokki
nao aceita anexo em pedido ja expedido.
"""
import io
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from nucleo import banco

logger = logging.getLogger(__name__)
LIMITE_BYTES = 15 * 1024 * 1024
_HEIC = (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1", b"ftypmsf1")


class CanhotoInvalido(ValueError):
    pass


def normalizar(codigo) -> str:
    return str(codigo or "").strip().lstrip("#").upper()


def _pasta(db_path: Path | None = None) -> Path:
    return Path(db_path or banco.DB_PATH).parent / "canhotos_manuais"


def para_pdf(conteudo: bytes) -> bytes:
    if len(conteudo) > LIMITE_BYTES:
        raise CanhotoInvalido("Arquivo maior que 15 MB.")
    if conteudo[:5] == b"%PDF-":
        return conteudo
    if conteudo[4:12] in _HEIC:
        raise CanhotoInvalido("Foto em HEIC não é aceita: tire a foto pela própria tela ou envie JPG/PDF.")
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(conteudo))).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise CanhotoInvalido("Formato não aceito: envie foto (JPG/PNG) ou PDF.") from e
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)
    return buf.getvalue()


def garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS canhotos_manuais (
            codigo      TEXT PRIMARY KEY,
            service_id  INTEGER,
            caminho     TEXT NOT NULL,
            caminho_gcs TEXT,
            enviado_por TEXT,
            enviado_em  TEXT NOT NULL,
            origem      TEXT NOT NULL DEFAULT 'BAIXA_SEM_APP'
        )""")
    conn.commit()


def salvar(conn: sqlite3.Connection, codigo: str, service_id: int | None, pdf: bytes, por: str,
           config: dict | None = None) -> dict:
    cod = normalizar(codigo)
    pasta = _pasta()
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"{cod}.pdf"
    caminho.write_bytes(pdf)
    caminho_gcs = None
    if config and config.get("gcs"):
        try:
            from documentos_pedido.storage_gcs import enviar_documento
            caminho_gcs = enviar_documento(config, caminho, cod, "Canhoto")
        except Exception as e:  # noqa: BLE001 -- disco basta pra expedicao
            logger.warning(f"Canhoto de {cod} salvo em disco, mas nao subiu pro GCS: {e}")
    garantir_tabela(conn)
    conn.execute("""
        INSERT INTO canhotos_manuais (codigo, service_id, caminho, caminho_gcs, enviado_por, enviado_em)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(codigo) DO UPDATE SET service_id = excluded.service_id, caminho = excluded.caminho,
            caminho_gcs = excluded.caminho_gcs, enviado_por = excluded.enviado_por, enviado_em = excluded.enviado_em
    """, (cod, service_id, str(caminho), caminho_gcs, por, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    return {"codigo": cod, "caminho": str(caminho), "caminho_gcs": caminho_gcs}


def caminho_canhoto_manual(codigo: str, db_path: Path | None = None) -> Path | None:
    caminho = _pasta(db_path) / f"{normalizar(codigo)}.pdf"
    return caminho if caminho.exists() else None
```

- [ ] **Step 4: Ver passar** — `py -3.11 -m unittest nucleo.test_canhotos_manuais` → OK.

---

### Task 2: POST multipart e campo na tela

**Files:** Modify `painel_agentes/painel_agentes.py` (`api_baixa_sem_app`), `painel_agentes/templates/baixa_sem_app.html`; Test `painel_agentes/test_baixa_sem_app_tela.py`

**Interfaces — Consumes:** `canhotos_manuais.para_pdf`, `canhotos_manuais.salvar`, `CanhotoInvalido`.

- [ ] **Step 1: Conferir limite** — `grep -n "MAX_CONTENT_LENGTH" painel_agentes/painel_agentes.py`. Se existir e for menor que 60 MB, ler o comentário e decidir (ledger): ou aumentar, ou fixar o limite da tela em 4 arquivos. Se não existir, nada a fazer.

- [ ] **Step 2: Testes que falham** — em `TestTelaBaixa` de `painel_agentes/test_baixa_sem_app_tela.py`:

```python
    def _post_multipart(self, dados, arquivos):
        import io
        import json
        data = {"dados": json.dumps(dados)}
        for nome, conteudo in arquivos.items():
            data[nome] = (io.BytesIO(conteudo), "canhoto.pdf")
        return self.cliente.post("/api/baixa-sem-app", data=data, content_type="multipart/form-data",
                                 headers={"Origin": "http://localhost"})

    def test_canhoto_gravado_antes_do_lote(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar", return_value={}) as salvar:
            r = self._post_multipart({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": True}]},
                                     {"canhoto_1": b"%PDF-1.4 x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        salvar.assert_called_once()
        self.assertEqual(salvar.call_args.args[1], "PS-1")

    def test_canhoto_invalido_nao_grava_nada(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar") as salvar, \
             mock.patch("nucleo.baixa_sem_app.criar_lote") as criar:
            r = self._post_multipart({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": True}]},
                                     {"canhoto_1": b"nao sou imagem"})
        self.assertEqual(r.status_code, 400)
        salvar.assert_not_called()
        criar.assert_not_called()

    def test_canhoto_de_item_que_voltou_e_ignorado(self):
        self._logar("operador")
        with mock.patch("nucleo.canhotos_manuais.salvar") as salvar:
            r = self._post_multipart({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": False,
                                                               "failed_reason_id": 5433}]},
                                     {"canhoto_1": b"%PDF-1.4 x"})
        self.assertEqual(r.status_code, 200)
        salvar.assert_not_called()

    def test_tela_tem_campo_de_canhoto(self):
        self._logar("operador")
        self.assertIn('class="canhoto"', self.cliente.get("/baixa-sem-app?rota=5000").get_data(as_text=True))
```

- [ ] **Step 3: Ver falhar** — `py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela`.

- [ ] **Step 4: Implementar no POST.** Trocar o início de `api_baixa_sem_app`:

```python
    body = request.get_json(force=True) or {}
```

por:

```python
    if (request.content_type or "").startswith("multipart/form-data"):
        try:
            body = json.loads(request.form.get("dados") or "{}")
        except ValueError:
            return jsonify({"erro": "dados inválidos"}), 400
    else:
        body = request.get_json(force=True) or {}
```

(conferir que `json` está importado no topo do painel; se não, `import json` local.) Depois da checagem de `lote_em_andamento` e **antes** de `criar_lote`, dentro do mesmo `try` com `conn`:

```python
        from nucleo import canhotos_manuais
        canhotos = []
        for i in itens:
            arq = request.files.get(f"canhoto_{int(i.get('service_id') or 0)}")
            if not arq or not i.get("entregue"):
                continue
            try:
                canhotos.append((i, canhotos_manuais.para_pdf(arq.read())))
            except canhotos_manuais.CanhotoInvalido as e:
                return jsonify({"erro": f"{i.get('codigo')}: {e}"}), 400
        por = session.get("usuario") or g.nivel_acesso
        for i, pdf in canhotos:
            canhotos_manuais.salvar(conn, i.get("codigo") or "", int(i["service_id"]), pdf, por, _carregar_config())
```

e usar `por` no `criar_lote`.

- [ ] **Step 5: Template.** Em `baixa_sem_app.html`:
  - cabeçalho: `<th>Canhoto</th>` entre "Cliente" e "Motivo";
  - célula: `<td>{% if not s.fechado %}<input type="file" class="canhoto" accept="image/*,application/pdf" capture="environment" aria-label="Canhoto {{ s.codigo }}">{% endif %}</td>`;
  - no `change` do checkbox: `var fc = tr.querySelector('.canhoto'); if (fc) { fc.disabled = !cb.checked; if (!cb.checked) fc.value = ''; }`;
  - no envio, trocar o `fetch` JSON por `FormData`:

```js
  var fd = new FormData();
  fd.append('dados', JSON.stringify({rota: {{ rota.id }}, itens: itens}));
  document.querySelectorAll('tr[data-id]').forEach(function (tr) {
    var fc = tr.querySelector('.canhoto'), cb = tr.querySelector('.entregue');
    if (fc && cb && cb.checked && fc.files.length) fd.append('canhoto_' + tr.dataset.id, fc.files[0]);
  });
  var r = await fetch('{{ url_for("api_baixa_sem_app") }}', {method: 'POST', body: fd});
```

  - texto do confirm: incluir `' (' + nCanhotos + ' com canhoto)'`.

- [ ] **Step 6: Ver passar** — `py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela` → OK (todos, inclusive os antigos em JSON).

---

### Task 3: Expedição usa o canhoto manual

**Files:** Modify `expedir_pedidos.py` (raiz; ~linha 1539 e nova função perto de `baixar_canhoto_pdf`); Test `test_expedir_canhoto_manual.py` (raiz)

**Interfaces — Consumes:** `nucleo.canhotos_manuais.caminho_canhoto_manual(codigo)`. **Produces:** `pdf_do_canhoto(vuupt_token, servico, codigo_ps) -> Path | None`.

- [ ] **Step 1: Teste que falha** — `test_expedir_canhoto_manual.py`:

```python
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_expedir_canhoto_manual"""
import unittest
from pathlib import Path
from unittest import mock

import expedir_pedidos as ep


class PdfDoCanhoto(unittest.TestCase):
    def test_vuupt_tem_prioridade(self):
        with mock.patch.object(ep, "tem_canhoto", return_value=True), \
             mock.patch.object(ep, "extrair_checklist_id", return_value=9), \
             mock.patch.object(ep, "baixar_canhoto_pdf", return_value=Path("vuupt.pdf")), \
             mock.patch("nucleo.canhotos_manuais.caminho_canhoto_manual") as manual:
            self.assertEqual(ep.pdf_do_canhoto("t", {}, "#PS-1"), Path("vuupt.pdf"))
        manual.assert_not_called()

    def test_sem_vuupt_usa_manual_pelo_codigo_exato(self):
        with mock.patch.object(ep, "tem_canhoto", return_value=False), \
             mock.patch("nucleo.canhotos_manuais.caminho_canhoto_manual", return_value=Path("m.pdf")) as manual:
            self.assertEqual(ep.pdf_do_canhoto("t", {}, "#PS-1-R1"), Path("m.pdf"))
        manual.assert_called_once_with("#PS-1-R1")

    def test_nenhum(self):
        with mock.patch.object(ep, "tem_canhoto", return_value=False), \
             mock.patch("nucleo.canhotos_manuais.caminho_canhoto_manual", return_value=None):
            self.assertIsNone(ep.pdf_do_canhoto("t", {}, "#PS-2"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Ver falhar** — `py -3.11 -m unittest test_expedir_canhoto_manual` → AttributeError `pdf_do_canhoto`.

- [ ] **Step 3: Implementar** — logo depois de `baixar_canhoto_pdf`:

```python
def pdf_do_canhoto(vuupt_token: str, servico: dict, codigo_ps: str) -> Path | None:
    """PDF do canhoto pra anexar na Stokki: o do checklist da Vuupt e, se
    nao houver, o enviado pela tela de baixa de motorista sem app
    (nucleo/canhotos_manuais.py, Hugo 09/10)."""
    checklist_id = extrair_checklist_id(servico) if tem_canhoto(servico) else None
    pdf = baixar_canhoto_pdf(vuupt_token, checklist_id, codigo_ps) if checklist_id else None
    if pdf:
        return pdf
    try:
        from nucleo.canhotos_manuais import caminho_canhoto_manual
        manual = caminho_canhoto_manual(codigo_ps)
    except Exception as e:  # noqa: BLE001 -- sem manual, segue como hoje (sem comprovante)
        logger.warning(f"  {codigo_ps}: falha ao procurar canhoto manual: {e}")
        return None
    if manual:
        logger.info(f"  {codigo_ps}: usando canhoto enviado pelo painel (baixa sem app).")
    return manual
```

e na linha ~1539 trocar `pdf_path = baixar_canhoto_pdf(vuupt_token, checklist_id, codigo_ps) if checklist_id else None` por `pdf_path = pdf_do_canhoto(vuupt_token, servico, codigo_ps)`. Se `checklist_id` não for usado em mais nada no laço (conferir com grep dentro da função), remover a linha que o calcula ali.

- [ ] **Step 4: Ver passar** — `py -3.11 -m unittest test_expedir_canhoto_manual` → OK; e rodar os testes de expedição existentes (`ls test_expedir*.py` na raiz) → OK.

---

### Task 4: Batimento conta o canhoto do painel

**Files:** Modify `batimento/medir.py` (`ler_banco` ~linha 340, `montar_fato` ~linha 425); Test `batimento/test_medir.py`

- [ ] **Step 1: Teste que falha** — em `batimento/test_medir.py`:

```python
class CanhotoPainel(unittest.TestCase):
    def test_canhoto_manual_vira_fonte_painel(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE nucleo_pedidos (codigo TEXT, status TEXT, fluxo TEXT, status_provedor TEXT, "
                     "status_done_provedor TEXT, excluido_em TEXT, vuupt_route_id INTEGER, qtd_checklists INTEGER, "
                     "criado_em_provedor TEXT)")
        conn.execute("CREATE TABLE canhotos_manuais (codigo TEXT PRIMARY KEY, service_id INTEGER, caminho TEXT, "
                     "caminho_gcs TEXT, enviado_por TEXT, enviado_em TEXT, origem TEXT)")
        conn.execute("INSERT INTO canhotos_manuais (codigo, caminho, enviado_em) VALUES ('PS-1', 'x', 'y')")
        banco = medir.ler_banco(conn, {"PS-1"}, 0)
        fato = medir.montar_fato({"codigo": "PS-1", "transportadora": ""}, banco["PS-1"], None, None)
        self.assertEqual(fato["canhoto_fonte"], "painel")
```

- [ ] **Step 2: Ver falhar** — `py -3.11 -m unittest batimento.test_medir`.

- [ ] **Step 3: Implementar** — em `ler_banco`, depois do bloco de `nucleo_comprovantes`:

```python
    # canhoto enviado pela tela de baixa de motorista sem app (09/10)
    if _tabela_existe(conn, "canhotos_manuais"):
        for r in conn.execute("SELECT codigo FROM canhotos_manuais"):
            e = pega(r["codigo"])
            if e is not None:
                e["canhoto_painel"] = True
```

em `montar_fato`, entre o ramo `vuupt_foto` e o `expedicao_anexou`:

```python
    elif banco.get("canhoto_painel"):
        canhoto_fonte, validado = "painel", False
```

- [ ] **Step 4: Ver passar** — `py -3.11 -m unittest batimento.test_medir batimento.test_regras` → OK.

---

### Task 5: Docs, conferência visual, revisão e deploy

- [ ] **Step 1:** `MAPA_DO_SISTEMA.txt`: em "Baixa de motorista sem app" acrescentar "canhoto opcional na tela -> nucleo/canhotos_manuais.py (dados/canhotos_manuais/{PS}.pdf + GCS); expedir_pedidos.pdf_do_canhoto usa quando a Vuupt não tem". Spec, seção 7: status "implementado".
- [ ] **Step 2:** todos os testes (grupos separados): `nucleo.test_canhotos_manuais nucleo.test_baixa_sem_app`; `test_expedir_canhoto_manual` + testes de expedição existentes; `batimento.test_medir batimento.test_regras`; `painel_agentes.test_baixa_sem_app_tela painel_agentes.test_batimento_tela painel_agentes.test_torre_batimento`. `py_compile` de tudo.
- [ ] **Step 3:** Playwright no servidor de teste do scratchpad (`servidor_semapp.py`, dublês): anexar um JPG num pedido, desmarcar outro, confirmar; conferir que `dados/canhotos_manuais/` do banco temporário recebeu o PDF (o servidor de teste usa `banco.DB_PATH` no scratchpad).
- [ ] **Step 4:** revisão final por revisor novo.
- [ ] **Step 5 (com ok do Hugo):** commit dos arquivos das Tasks 1–5; deploy `--restart painel-agentes`; prova: `test_client` POST multipart contra rota real de motorista sem app **não** é feito (escreveria na Vuupt) — provar com GET da tela (campo presente), `para_pdf` de um JPG no servidor (Pillow da VPS), e `expedir_pedidos.pdf_do_canhoto` importável como www-data.
