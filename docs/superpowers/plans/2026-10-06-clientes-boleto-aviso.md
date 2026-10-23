# Clientes com boleto + aviso de boleto faltando — Plano de implementação

> **Para agentes:** SUB-SKILL OBRIGATÓRIA: superpowers:subagent-driven-development (recomendado) ou superpowers:executing-plans. Passos com checkbox (`- [ ]`).

**Objetivo:** marcar os pares embarcador + cliente que pagam com boleto (o sistema sugere pela NF e o time confirma no painel) e avisar o embarcador quando falta boleto. O aviso sai às 18h30, para os pedidos de amanhã, por e-mail, WhatsApp e chamado. Há também uma seção nova no resumo das 20h.

**Arquitetura:**
- `regras/clientes_boleto.py`: tabela, sugestão, decisão e consulta.
- `boleto_faltando.py`: seleção dos pedidos e os três canais.
- Duas rotinas novas: `marcar_clientes_boleto.py` e `aviso_boleto_faltando.py`, ambas no fim de `infra/sequencia_incremento.sh`.
- Uma tela no painel.
- A régua "boleto esperado" passa a respeitar a marcação no romaneio e no vigia.

**Stack:** Python 3.11, SQLite (`dados/dados.db`), Flask (painel), OpenWA (WhatsApp), unittest.

**Spec:** `docs/superpowers/specs/2026-10-06-clientes-boleto-aviso-design.md`. Ler antes.

## Restrições globais

- Trabalhar num worktree a partir de `origin/master`: `git worktree add .claude/worktrees/clientes-boleto -b clientes-boleto origin/master`. Copiar o plano e a spec para lá (não commitar os dois).
- Python **sempre** `py -3.11`. Testes com `py -3.11 -m unittest <modulo>`, a partir da raiz do worktree.
  - Nunca misturar `painel_agentes` com `roteirizacao`/`nucleo` no mesmo comando.
  - O teste de tela do painel roda com `py -3.11 -m unittest painel_agentes.test_clientes_boleto_tela`.
- Português em código, comentários e commits, com acento quando o arquivo já usa acento.
- Commits: `Área: o que mudou` + linha em branco + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Só `git add` dos arquivos da tarefa. Sem push (só quando o Hugo pedir).
- `config.yaml`: nunca ler valores nem commitar. As chaves novas ficam documentadas no plano.
- **Envio real desligado por padrão:**
  - e-mail do aviso com `aviso_boleto_faltando.forcar_destino` (sem a chave = hugo@);
  - WhatsApp com `whatsapp_notificacoes.clientes.forcar_destino`, que já existe;
  - seção do resumo atrás de `resumo_diario_embarcador.secao_boleto` (padrão False);
  - **chamado abre de verdade** (decisão do Hugo).
- Normalizar documento só com dígitos. Código de pedido: base `PS-\d+` (reentrega `-R1` usa a base).
- Atualizar `MAPA_DO_SISTEMA.txt` na Tarefa 9.

## Foco de revisão

1. **Falso aviso ao embarcador.** Só par **TEM_BOLETO** gera aviso, e só depois de atualizar os documentos. Testes na Tarefa 6: par PENDENTE não avisa, pedido com boleto ENVIADO não avisa, reentrega `-R1` com boleto na base não avisa.
2. **Aviso repetido.** O mesmo pedido e a mesma data de entrega só são avisados uma vez; o registro só acontece se algum canal funcionou. Testes na Tarefa 6.
3. **Pedido em rota E em rascunho.** Aparece uma vez só. Teste na Tarefa 6.
4. **Chamado duplicado.** Com um chamado de boleto aberto, acrescenta mensagem em vez de abrir outro; chamado RESOLVIDO não é reaproveitado. Teste na Tarefa 6.
5. **Romaneio e vigia sem a tabela** (banco antigo ou erro): continua funcionando com a regra atual. Teste na Tarefa 4.

---

### Tarefa 1: `regras/clientes_boleto.py` — tabela, sugestão, decisão, consulta

**Arquivos:**
- Criar: `regras/clientes_boleto.py`
- Criar: `regras/test_clientes_boleto.py`

**Interfaces produzidas:**
- Constantes: `PENDENTE = "PENDENTE"`, `TEM_BOLETO = "TEM_BOLETO"`, `NAO_TEM_BOLETO = "NAO_TEM_BOLETO"`, `ROTULOS: dict[str, str]`.
- `DB_PATH: Path`, que é `<raiz>/dados/dados.db` e pode ser substituído com `patch` nos testes.
- `so_digitos(s) -> str`, `codigo_base(codigo: str) -> str` (`"#PS-1-R2"` vira `"PS-1"`).
- `sugerir(dias: int = 90, modo_teste: bool = False) -> dict`, que devolve `{"novos": [(sender_id, documento, nome)], "atualizados": int}`.
- `listar(situacao: str | None = None) -> list[dict]`, com as chaves `sender_id`, `embarcador`, `documento`, `documento_formatado`, `nome`, `situacao`, `rotulo`, `nfs_com_duplicata`, `nfs_sem_duplicata`, `boletos_recebidos`, `ultimo_pedido`, `sugerido_em`, `decidido_em`, `decidido_por`.
- `decidir(sender_id: int, documento: str, tem_boleto: bool, por: str) -> dict`, que devolve `{"situacao"}`. Vale para qualquer situação: o time pode trocar a decisão.
- `situacao_por_pedido(codigos) -> dict[str, str]`, que devolve `{codigo_base: situacao}` só dos pedidos cujo par existe na tabela. Sem a tabela, devolve `{}`.

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest regras.test_clientes_boleto
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from regras import clientes_boleto as cb  # noqa: E402


def _banco(caminho: Path):
    c = sqlite3.connect(caminho)
    c.executescript("""
        CREATE TABLE nucleo_pedidos (codigo TEXT, sender_id INTEGER, destinatario_codigo TEXT,
                                     destinatario_nome TEXT, remetente_nome TEXT);
        CREATE TABLE documentos_processados (hash_conteudo TEXT PRIMARY KEY, tipo TEXT, codigo_pedido TEXT,
                                             status TEXT, cobranca INTEGER, processado_em TEXT);
        INSERT INTO nucleo_pedidos VALUES ('PS-1', 11, '12.345.678/0001-90', 'MERCADO A', 'NUU'),
                                          ('PS-2', 11, '12345678000190', 'MERCADO A', 'NUU'),
                                          ('PS-3', 22, '98765432000110', 'PADARIA B', 'SOTILLE'),
                                          ('PS-4', 22, '11122233344', 'FULANO', 'SOTILLE');
    """)
    c.commit()
    return c


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        self.con = _banco(self.db)
        self.addCleanup(self.con.close)
        p = patch.object(cb, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)

    def _doc(self, h, tipo, codigo, cobranca=None, quando="2099-01-01 10:00:00", status="ENVIADO"):
        self.con.execute("INSERT INTO documentos_processados VALUES (?,?,?,?,?,?)",
                         (h, tipo, codigo, status, cobranca, quando))
        self.con.commit()


class Sugerir(_Base):
    def test_uma_danfe_com_duplicata_vira_pendente(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        r = cb.sugerir()
        self.assertEqual(r["novos"], [(11, "12345678000190", "MERCADO A")])
        linha = cb.listar()[0]
        self.assertEqual((linha["situacao"], linha["nfs_com_duplicata"]), (cb.PENDENTE, 1))

    def test_boleto_recebido_tambem_sugere(self):
        self._doc("h1", "Boleto", "PS-3")
        self.assertEqual(cb.sugerir()["novos"], [(22, "98765432000110", "PADARIA B")])

    def test_so_a_vista_nao_sugere(self):
        self._doc("h1", "Nota Fiscal", "PS-3", 0)
        self._doc("h2", "Nota Fiscal", "PS-4", -1)
        self.assertEqual(cb.sugerir()["novos"], [])
        self.assertEqual(cb.listar(), [])

    def test_revisao_manual_nao_conta(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1, status="REVISAO_MANUAL")
        self.assertEqual(cb.sugerir()["novos"], [])

    def test_decidido_nao_volta_a_pendente_mas_evidencia_atualiza(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        cb.sugerir()
        cb.decidir(11, "12345678000190", tem_boleto=False, por="hugo")
        self._doc("h2", "Nota Fiscal", "PS-2", 1)
        r = cb.sugerir()
        self.assertEqual(r["novos"], [])
        linha = cb.listar()[0]
        self.assertEqual(linha["situacao"], cb.NAO_TEM_BOLETO)
        self.assertEqual(linha["nfs_com_duplicata"], 2)

    def test_modo_teste_nao_grava(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        self.assertEqual(len(cb.sugerir(modo_teste=True)["novos"]), 1)
        self.assertEqual(cb.listar(), [])

    def test_fora_da_janela_nao_conta(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1, quando="2000-01-01 10:00:00")
        self.assertEqual(cb.sugerir(dias=90)["novos"], [])


class DecidirEConsultar(_Base):
    def test_decidir_e_trocar(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        cb.sugerir()
        self.assertEqual(cb.decidir(11, "12.345.678/0001-90", True, "hugo")["situacao"], cb.TEM_BOLETO)
        self.assertEqual(cb.decidir(11, "12345678000190", False, "gabriel")["situacao"], cb.NAO_TEM_BOLETO)
        self.assertEqual(cb.listar()[0]["decidido_por"], "gabriel")

    def test_decidir_par_inexistente(self):
        with self.assertRaises(ValueError):
            cb.decidir(99, "1", True, "hugo")

    def test_situacao_por_pedido(self):
        self._doc("h1", "Nota Fiscal", "PS-1", 1)
        cb.sugerir()
        cb.decidir(11, "12345678000190", True, "hugo")
        r = cb.situacao_por_pedido(["#PS-2-R1", "PS-3", "PS-1"])
        self.assertEqual(r, {"PS-2": cb.TEM_BOLETO, "PS-1": cb.TEM_BOLETO})

    def test_situacao_sem_tabela(self):
        self.assertEqual(cb.situacao_por_pedido(["PS-1"]), {})

    def test_codigo_base(self):
        self.assertEqual(cb.codigo_base("#PS-36327-R2"), "PS-36327")
        self.assertEqual(cb.codigo_base("PS-1, PS-2"), "PS-1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Passo 2:** `py -3.11 -m unittest regras.test_clientes_boleto -v` falha porque o módulo não existe.

- [ ] **Passo 3: implementar `regras/clientes_boleto.py`**

```python
# -*- coding: utf-8 -*-
"""
regras/clientes_boleto.py

Quais clientes (destinatários) pagam com boleto, por embarcador -- pedido
do Hugo, 06/10/2026. A chave é o par (sender_id, documento do
destinatário só com dígitos): o mesmo cliente pode pagar com boleto a um
embarcador e à vista a outro.

O sistema SUGERE (PENDENTE) quando o par teve, nos últimos 90 dias, ao
menos 1 DANFE com duplicata (documentos_processados.cobranca = 1) ou 1
boleto recebido. O time decide na tela /clientes-boleto do painel:
TEM_BOLETO ou NAO_TEM_BOLETO. Nunca sugere NAO_TEM_BOLETO; par decidido
só tem a evidência atualizada.

Quem usa: aviso_boleto_faltando.py e o resumo das 20h (só TEM_BOLETO),
e a régua do boleto esperado do romaneio/vigia (regras_documentos.
boleto_esperado). Spec: docs/superpowers/specs/2026-10-06-clientes-boleto-aviso-design.md
"""
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"

PENDENTE, TEM_BOLETO, NAO_TEM_BOLETO = "PENDENTE", "TEM_BOLETO", "NAO_TEM_BOLETO"
ROTULOS = {PENDENTE: "Aguardando decisão", TEM_BOLETO: "Tem boleto", NAO_TEM_BOLETO: "Não tem boleto"}

_RE_BASE = re.compile(r"PS-\d+", re.IGNORECASE)


def so_digitos(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def codigo_base(codigo: str) -> str:
    m = _RE_BASE.search(str(codigo or ""))
    return m.group(0).upper() if m else str(codigo or "").lstrip("#").strip()


def formatar_documento(doc: str) -> str:
    d = so_digitos(doc)
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d


def _conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS clientes_boleto ("
        " sender_id INTEGER NOT NULL,"
        " documento TEXT NOT NULL,"
        " nome TEXT,"
        " embarcador TEXT,"
        " situacao TEXT NOT NULL,"
        " nfs_com_duplicata INTEGER NOT NULL DEFAULT 0,"
        " nfs_sem_duplicata INTEGER NOT NULL DEFAULT 0,"
        " boletos_recebidos INTEGER NOT NULL DEFAULT 0,"
        " ultimo_pedido TEXT,"
        " sugerido_em TEXT,"
        " decidido_em TEXT,"
        " decidido_por TEXT,"
        " PRIMARY KEY (sender_id, documento))"
    )
    conn.commit()
    return conn


def _tabela_existe(conn: sqlite3.Connection, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def _evidencias(conn: sqlite3.Connection, dias: int) -> dict[tuple[int, str], dict]:
    """{(sender_id, documento): {nome, embarcador, com, sem, boletos, ultimo}} a partir dos
    documentos ENVIADOS dos últimos `dias` dias, ligados ao par pelo nucleo_pedidos."""
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    pedidos = {}
    for r in conn.execute("SELECT codigo, sender_id, destinatario_codigo, destinatario_nome, remetente_nome "
                          "FROM nucleo_pedidos WHERE sender_id IS NOT NULL"):
        doc = so_digitos(r["destinatario_codigo"])
        if doc:
            pedidos[codigo_base(r["codigo"])] = (r["sender_id"], doc, r["destinatario_nome"] or "",
                                                 r["remetente_nome"] or "")
    ev: dict[tuple[int, str], dict] = {}
    for r in conn.execute("SELECT tipo, codigo_pedido, cobranca, processado_em FROM documentos_processados "
                          "WHERE status = 'ENVIADO' AND codigo_pedido IS NOT NULL "
                          "AND tipo IN ('Nota Fiscal', 'Boleto') AND processado_em >= ? ORDER BY processado_em",
                          (desde,)):
        par = pedidos.get(codigo_base(r["codigo_pedido"]))
        if not par:
            continue
        sid, doc, nome, emb = par
        e = ev.setdefault((sid, doc), {"nome": nome, "embarcador": emb.split(" - ")[-1].strip(),
                                       "com": 0, "sem": 0, "boletos": 0, "ultimo": None})
        if r["tipo"] == "Boleto":
            e["boletos"] += 1
        elif r["cobranca"] == 1:
            e["com"] += 1
        else:
            e["sem"] += 1
        e["ultimo"] = codigo_base(r["codigo_pedido"])
    return ev


def sugerir(dias: int = 90, modo_teste: bool = False) -> dict:
    conn = _conectar()
    try:
        if not (_tabela_existe(conn, "nucleo_pedidos") and _tabela_existe(conn, "documentos_processados")):
            return {"novos": [], "atualizados": 0}
        ev = _evidencias(conn, dias)
        existentes = {(r["sender_id"], r["documento"]) for r in conn.execute(
            "SELECT sender_id, documento FROM clientes_boleto")}
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        novos, atualizados = [], 0
        for (sid, doc), e in sorted(ev.items()):
            if (sid, doc) in existentes:
                atualizados += 1
                if not modo_teste:
                    conn.execute("UPDATE clientes_boleto SET nfs_com_duplicata=?, nfs_sem_duplicata=?, "
                                 "boletos_recebidos=?, ultimo_pedido=? WHERE sender_id=? AND documento=?",
                                 (e["com"], e["sem"], e["boletos"], e["ultimo"], sid, doc))
            elif e["com"] or e["boletos"]:
                novos.append((sid, doc, e["nome"]))
                if not modo_teste:
                    conn.execute("INSERT INTO clientes_boleto (sender_id, documento, nome, embarcador, situacao, "
                                 "nfs_com_duplicata, nfs_sem_duplicata, boletos_recebidos, ultimo_pedido, sugerido_em) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (sid, doc, e["nome"], e["embarcador"], PENDENTE, e["com"], e["sem"],
                                  e["boletos"], e["ultimo"], agora))
        if not modo_teste:
            conn.commit()
        return {"novos": novos, "atualizados": atualizados}
    finally:
        conn.close()


def listar(situacao: str | None = None) -> list[dict]:
    conn = _conectar()
    try:
        sql, params = "SELECT * FROM clientes_boleto", ()
        if situacao:
            sql, params = sql + " WHERE situacao = ?", (situacao,)
        sql += (" ORDER BY CASE situacao WHEN 'PENDENTE' THEN 0 ELSE 1 END, "
                "COALESCE(decidido_em, sugerido_em) DESC, embarcador, nome")
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [{**dict(r), "documento_formatado": formatar_documento(r["documento"]),
             "rotulo": ROTULOS.get(r["situacao"], r["situacao"])} for r in rows]


def decidir(sender_id: int, documento: str, tem_boleto: bool, por: str) -> dict:
    doc = so_digitos(documento)
    situacao = TEM_BOLETO if tem_boleto else NAO_TEM_BOLETO
    conn = _conectar()
    try:
        cur = conn.execute("UPDATE clientes_boleto SET situacao=?, decidido_em=datetime('now','localtime'), "
                           "decidido_por=? WHERE sender_id=? AND documento=?",
                           (situacao, por or "", int(sender_id), doc))
        conn.commit()
    finally:
        conn.close()
    if not cur.rowcount:
        raise ValueError(f"Cliente {formatar_documento(doc)} do remetente {sender_id} não está na lista.")
    return {"situacao": situacao}


def situacao_por_pedido(codigos) -> dict[str, str]:
    bases = sorted({codigo_base(c) for c in codigos if c})
    if not bases or not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        if not (_tabela_existe(conn, "clientes_boleto") and _tabela_existe(conn, "nucleo_pedidos")):
            return {}
        situacoes = {(sid, doc): s for sid, doc, s in conn.execute(
            "SELECT sender_id, documento, situacao FROM clientes_boleto")}
        saida = {}
        for i in range(0, len(bases), 900):
            lote = bases[i:i + 900]
            marc = ",".join("?" * len(lote))
            for codigo, sid, dest in conn.execute(
                    f"SELECT codigo, sender_id, destinatario_codigo FROM nucleo_pedidos "
                    f"WHERE UPPER(REPLACE(codigo, '#', '')) IN ({marc})", lote):
                s = situacoes.get((sid, so_digitos(dest)))
                if s:
                    saida[codigo_base(codigo)] = s
        return saida
    finally:
        conn.close()
```

Notas:
- O teste `test_situacao_sem_tabela` cria o banco sem a `clientes_boleto` (só as duas tabelas da fixture): `situacao_por_pedido` tem que devolver `{}` sem criar tabela. Por isso usa conexão read-only e não chama `_conectar`.
- No teste de sugestão, `processado_em` é `2099-...`, sempre dentro da janela.

- [ ] **Passo 4:** `py -3.11 -m unittest regras.test_clientes_boleto -v` passa com 12 testes OK.

- [ ] **Passo 5: commit**

```bash
git add regras/clientes_boleto.py regras/test_clientes_boleto.py
git commit -m "Boleto: tabela clientes_boleto com sugestão pela DANFE e decisão do time"
```

---

### Tarefa 2: rotina `marcar_clientes_boleto.py` com aviso interno de pendentes

**Arquivos:**
- Criar: `marcar_clientes_boleto.py` (raiz)
- Criar: `test_marcar_clientes_boleto.py` (raiz)
- Modificar: `notificar_whatsapp.py`, com `texto_clientes_boleto` e `avisar_clientes_boleto` ao lado de `texto_clientes_agenda` e `avisar_clientes_agenda`

**Interfaces:**
- Consome `regras.clientes_boleto.sugerir`, `listar` e `PENDENTE`.
- Produz:
  - `marcar_clientes_boleto.main(modo_teste: bool = False) -> dict`;
  - `marcar_clientes_boleto.link_tela(config) -> str` (`<painel_agentes.url_base>/clientes-boleto`);
  - `notificar_whatsapp.avisar_clientes_boleto(novos: int, total: int, link: str, config: dict, modo_teste: bool = False, **kw) -> str`.

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest test_marcar_clientes_boleto
"""
import unittest
from unittest.mock import patch

import marcar_clientes_boleto as mcb
import notificar_whatsapp as nw


class Rotina(unittest.TestCase):
    def _rodar(self, novos, modo_teste=False):
        with patch.object(mcb, "_carregar_config", return_value={"painel_agentes": {"url_base": "https://x/painel"}}), \
             patch.object(mcb.cb, "sugerir", return_value={"novos": novos, "atualizados": 3}) as sug, \
             patch.object(mcb.cb, "listar", return_value=[{}] * (len(novos) + 2)), \
             patch.object(mcb, "notificar_execucao") as notif, \
             patch.object(mcb.notificar_whatsapp, "avisar_clientes_boleto", return_value="enviado") as zap:
            r = mcb.main(modo_teste=modo_teste)
        return r, sug, notif, zap

    def test_com_pendente_novo_avisa(self):
        r, sug, notif, zap = self._rodar([(11, "123", "MERCADO A")])
        self.assertEqual(r["novos"], 1)
        zap.assert_called_once()
        self.assertEqual(zap.call_args.args[:3], (1, 3, "https://x/painel/clientes-boleto"))
        notif.assert_called_once()
        self.assertIn("https://x/painel/clientes-boleto", notif.call_args.args[0]["Clientes com boleto"]["detalhe"])

    def test_sem_novo_nao_avisa(self):
        r, sug, notif, zap = self._rodar([])
        zap.assert_not_called()
        notif.assert_not_called()

    def test_modo_teste_repassa(self):
        _r, sug, _n, _z = self._rodar([], modo_teste=True)
        self.assertTrue(sug.call_args.kwargs["modo_teste"])


class TextoWhatsApp(unittest.TestCase):
    def test_texto_curto(self):
        t = nw.texto_clientes_boleto(3, 40, "https://app.freshhub.com.br/painel/clientes-boleto")
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        self.assertIn("3 novos", t)
        self.assertIn("/clientes-boleto", t)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Passo 2:** `py -3.11 -m unittest test_marcar_clientes_boleto -v` falha porque o módulo não existe.

- [ ] **Passo 3: implementar**

Em `notificar_whatsapp.py`, logo depois de `texto_clientes_agenda`:

```python
def texto_clientes_boleto(novos: int, total: int, link: str, agora: datetime | None = None) -> str:
    """Pares embarcador+cliente sugeridos como "tem boleto" esperando o time
    decidir (marcar_clientes_boleto.py). Só contagens; os nomes ficam na tela."""
    quando = (agora or datetime.now()).strftime("%d/%m %H:%M")
    frase = (f"{novos} novo aguarda" if novos == 1 else f"{novos} novos aguardam")
    return "\n".join([
        f"💳 *Clientes com boleto* · {quando}",
        f"{frase} decisão ({total} no total).",
        link,
    ])
```

E logo depois de `avisar_clientes_agenda`:

```python
def avisar_clientes_boleto(novos: int, total: int, link: str, config: dict, modo_teste: bool = False, **kw) -> str:
    """Pendentes novos de marcar_clientes_boleto.py, uma mensagem por rodada."""
    try:
        if not novos:
            return "nao_relevante"
        return despachar(config, "marcar_clientes_boleto", "clientes_boleto",
                         texto_clientes_boleto(novos, total, link, kw.get("agora")), modo_teste=modo_teste, **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"
```

`marcar_clientes_boleto.py`:

```python
# -*- coding: utf-8 -*-
"""
marcar_clientes_boleto.py

Sugere os pares embarcador+cliente que pagam com boleto (regras/
clientes_boleto.py) -- pedido do Hugo, 06/10/2026. Par novo vira
PENDENTE e o time decide em /painel/clientes-boleto; o link chega no
WhatsApp interno quando entra pendente novo.

Roda na sequência das 18h (infra/sequencia_incremento.sh), antes do
aviso_boleto_faltando.py.

COMO USAR:
    py -3.11 marcar_clientes_boleto.py
    py -3.11 marcar_clientes_boleto.py --modo-teste
"""
import argparse
import logging
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import yaml

import notificar_whatsapp
from notificar_execucao_agente import notificar_execucao
from regras import clientes_boleto as cb

logger = logging.getLogger("marcar_clientes_boleto")
TITULO = "Clientes com boleto"
URL_PAINEL_PADRAO = "https://app.freshhub.com.br/painel"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def link_tela(config: dict) -> str:
    base = ((config or {}).get("painel_agentes") or {}).get("url_base") or URL_PAINEL_PADRAO
    return base.rstrip("/") + "/clientes-boleto"


def main(modo_teste: bool = False) -> dict:
    inicio = time.time()
    config = _carregar_config()
    resumo, r = {}, {"novos": 0, "total_pendentes": 0}
    try:
        res = cb.sugerir(modo_teste=modo_teste)
        r["novos"] = len(res["novos"])
        r["total_pendentes"] = len(cb.listar(cb.PENDENTE)) + (r["novos"] if modo_teste else 0)
        for sid, doc, nome in res["novos"]:
            logger.info(f"  {'[Teste] ' if modo_teste else ''}{nome or '(sem nome)'} ({doc}) remetente {sid}: "
                        f"sugerido como 'tem boleto'.")
        if r["novos"]:
            resumo[TITULO] = {"status": "ok",
                              "detalhe": f"{r['novos']} cliente(s) sugerido(s) como 'tem boleto' "
                                         f"({r['total_pendentes']} aguardando). Decidir em {link_tela(config)}"}
    except Exception as e:
        logger.exception(f"Erro ao sugerir clientes com boleto: {e}")
        resumo[TITULO] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    if resumo:
        try:
            notificar_execucao(resumo, duracao, modo_teste, config)
        except Exception as e:
            logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")
    if r["novos"]:
        notificar_whatsapp.avisar_clientes_boleto(r["novos"], r["total_pendentes"], link_tela(config), config,
                                                  modo_teste=modo_teste)
    return r


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="Sugere clientes que pagam com boleto (fila de decisão no painel)")
    parser.add_argument("--modo-teste", action="store_true", help="Só lista, sem gravar nem avisar o grupo")
    main(modo_teste=parser.parse_args().modo_teste)
```

- [ ] **Passo 4:** `py -3.11 -m unittest test_marcar_clientes_boleto test_notificar_whatsapp -v` passa. O `test_notificar_whatsapp` já existe e não pode quebrar.

- [ ] **Passo 5: commit**

```bash
git add marcar_clientes_boleto.py test_marcar_clientes_boleto.py notificar_whatsapp.py
git commit -m "Boleto: rotina que sugere clientes com boleto e avisa o grupo quando entra pendente"
```

---

### Tarefa 3: tela `/clientes-boleto` no painel

**Arquivos:**
- Modificar: `painel_agentes/painel_agentes.py` (duas rotas logo depois de `api_clientes_agenda_decidir`)
- Criar: `painel_agentes/templates/clientes_boleto.html`
- Modificar: `painel_agentes/templates/_menu_lateral_nav.html` (item logo depois do `clientes_agenda`)
- Criar: `painel_agentes/test_clientes_boleto_tela.py`

**Interfaces:** consome `regras.clientes_boleto.listar`, `decidir`, `PENDENTE`, `DB_PATH`.

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Tela /clientes-boleto do painel (06/10).

    py -3.11 -m unittest painel_agentes.test_clientes_boleto_tela
"""
import sqlite3
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

from regras import clientes_boleto as cb  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)


class TestTelaClientesBoleto(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        banco = Path(self._tmp.name) / "dados.db"
        c = sqlite3.connect(banco)
        c.executescript("""
            CREATE TABLE nucleo_pedidos (codigo TEXT, sender_id INTEGER, destinatario_codigo TEXT,
                                         destinatario_nome TEXT, remetente_nome TEXT);
            CREATE TABLE documentos_processados (hash_conteudo TEXT PRIMARY KEY, tipo TEXT, codigo_pedido TEXT,
                                                 status TEXT, cobranca INTEGER, processado_em TEXT);
            INSERT INTO nucleo_pedidos VALUES ('PS-1', 11, '49749452000160', 'VERO PANE', 'MARIA DOLORES - NUU');
            INSERT INTO documentos_processados VALUES ('h1', 'Nota Fiscal', 'PS-1', 'ENVIADO', 1, '2099-01-01 10:00:00');
        """)
        c.commit()
        c.close()
        p = mock.patch.object(cb, "DB_PATH", banco)
        p.start()
        self.addCleanup(p.stop)
        cb.sugerir()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def test_total_ve_o_pendente_com_evidencia(self):
        self._logar("total")
        resp = self.cliente.get("/clientes-boleto")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("VERO PANE", html)
        self.assertIn("49.749.452/0001-60", html)
        self.assertIn("NUU", html)
        self.assertIn('data-acao="tem"', html)

    def test_outros_niveis_nao_entram(self):
        for nivel in ("operador", "leitura", "atendimento"):
            self._logar(nivel)
            self.assertEqual(self.cliente.get("/clientes-boleto").status_code, 403, nivel)

    def test_decidir_tem_e_trocar(self):
        self._logar("total")
        corpo = {"sender_id": 11, "documento": "49749452000160", "acao": "tem"}
        resp = self.cliente.post("/api/clientes-boleto/decidir", json=corpo, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["situacao"], cb.TEM_BOLETO)
        corpo["acao"] = "nao_tem"
        resp = self.cliente.post("/api/clientes-boleto/decidir", json=corpo, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.get_json()["situacao"], cb.NAO_TEM_BOLETO)
        self.assertEqual(cb.listar()[0]["decidido_por"], "teste")

    def test_acao_invalida_origem_estranha_e_par_inexistente(self):
        self._logar("total")
        h = {"Origin": "http://localhost"}
        self.assertEqual(self.cliente.post("/api/clientes-boleto/decidir",
                                           json={"sender_id": 11, "documento": "49749452000160", "acao": "x"},
                                           headers=h).status_code, 400)
        self.assertEqual(self.cliente.post("/api/clientes-boleto/decidir",
                                           json={"sender_id": 11, "documento": "49749452000160", "acao": "tem"},
                                           headers={"Origin": "http://outro.site"}).status_code, 403)
        self.assertEqual(self.cliente.post("/api/clientes-boleto/decidir",
                                           json={"sender_id": 99, "documento": "1", "acao": "tem"},
                                           headers=h).status_code, 400)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Passo 2:** `py -3.11 -m unittest painel_agentes.test_clientes_boleto_tela -v` falha com 404.

- [ ] **Passo 3: implementar**

Rotas em `painel_agentes.py`, logo depois de `api_clientes_agenda_decidir`. Seguem o mesmo padrão; `requer_auth` sem `niveis` = só nível total, como na AGENDA:

```python
@app.route("/clientes-boleto")
@requer_auth
def clientes_boleto():
    """Clientes que pagam com boleto, por embarcador (06/10). Quem alimenta
    é marcar_clientes_boleto.py (sequência das 18h): par com DANFE com
    duplicata vira PENDENTE e o time decide aqui. Só TEM_BOLETO gera aviso
    de boleto faltando ao embarcador (aviso_boleto_faltando.py)."""
    from regras import clientes_boleto as cb
    try:
        linhas, erro = cb.listar(), None
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao listar clientes com boleto")
        linhas, erro = [], str(e)
    pendentes = [l for l in linhas if l["situacao"] == cb.PENDENTE]
    decididos = [l for l in linhas if l["situacao"] != cb.PENDENTE]
    return render_template("clientes_boleto.html", pendentes=pendentes, decididos=decididos, erro=erro)


@app.route("/api/clientes-boleto/decidir", methods=["POST"])
@requer_auth
@exige_mesma_origem
def api_clientes_boleto_decidir():
    from regras import clientes_boleto as cb
    body = request.get_json(force=True) or {}
    acao = body.get("acao")
    if acao not in ("tem", "nao_tem"):
        return jsonify({"erro": "ação inválida"}), 400
    try:
        resultado = cb.decidir(int(body.get("sender_id") or 0), body.get("documento", ""), acao == "tem",
                               session.get("usuario", ""))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao decidir cliente com boleto")
        return jsonify({"erro": str(e)}), 500
    return jsonify({"ok": True, "situacao": resultado["situacao"]})
```

Conferir se `session` já está importado no topo de `painel_agentes.py` (`from flask import ... session ...`). Se não estiver, acrescentar ao import existente.

Menu: em `_menu_lateral_nav.html`, logo depois do item `clientes_agenda`:

```
    {'rota': 'clientes_boleto', 'rotulo': 'Clientes com boleto', 'titulo': 'Clientes que pagam com boleto (decidir sugestões)', 'niveis': ('total',), 'badge': none,
     'icone': '<rect x="3" y="5" width="14" height="10" rx="1.5"/><path d="M3 8.5h14"/><path d="M6 12h3"/>'},
```

Template `clientes_boleto.html`: copiar a estrutura de `clientes_agenda.html` (mesmo bloco de estilo) com estas diferenças:
- título e h1 "Clientes com boleto";
- parágrafo `.sub` explicando: "Clientes que tiveram NF com duplicata (venda a prazo) ou boleto recebido nos últimos 90 dias. Só os marcados como **Tem boleto** geram aviso ao embarcador quando o boleto faltar. **Não tem** desliga a cobrança de boleto desse cliente no romaneio e no vigia.";
- tabela de pendentes com as colunas Embarcador (`c.embarcador`), Cliente (`c.nome`), CNPJ/CPF (`c.documento_formatado`), Evidência e as ações;
  - evidência: `{{ c.nfs_com_duplicata }} NF(s) com duplicata · {{ c.nfs_sem_duplicata }} à vista · {{ c.boletos_recebidos }} boleto(s) · último {{ c.ultimo_pedido or '—' }}`;
  - `<tr data-sender="{{ c.sender_id }}" data-documento="{{ c.documento }}">` com os botões `data-acao="tem"` (Tem boleto) e `data-acao="nao_tem"` (Não tem, classe `secundario`);
- seção "Já decididos" com as mesmas colunas, mais uma pílula com `c.rotulo` (classes `TEM_BOLETO` verde e `NAO_TEM_BOLETO` vermelha, como `AUTORIZADO`/`NAO_MARCAR`), quem decidiu e quando;
  - cada linha tem um botão para trocar: se for TEM_BOLETO, "Não tem"; senão, "Tem boleto";
  - acima da tabela, um `<input type="search" id="busca" placeholder="Buscar cliente, CNPJ ou embarcador">` que filtra as linhas por `textContent` (JS simples, sem biblioteca);
- script: o mesmo da AGENDA, mandando `{sender_id: Number(linha.dataset.sender), documento, acao}` para `BASE_PATH + "/api/clientes-boleto/decidir"`, com confirmação "Marcar <nome> como TEM boleto?" ou "Marcar <nome> como NÃO tem boleto?", e reload ao concluir.

- [ ] **Passo 4:**

```bash
py -3.11 -m unittest painel_agentes.test_clientes_boleto_tela painel_agentes.test_clientes_agenda_tela -v
```

Esperado: OK.

- [ ] **Passo 5: conferência visual (opcional, recomendada).** Rodar o painel em porta separada, de dentro de `painel_agentes/`, com um banco de teste:

```bash
py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"
```

Abrir `/clientes-boleto` logado como total. Nunca usar a porta 8070 e nunca derrubar processo que já existia.

- [ ] **Passo 6: commit**

```bash
git add painel_agentes/painel_agentes.py painel_agentes/templates/clientes_boleto.html \
        painel_agentes/templates/_menu_lateral_nav.html painel_agentes/test_clientes_boleto_tela.py
git commit -m "Painel: tela Clientes com boleto para decidir as sugestões"
```

---

### Tarefa 4: régua do boleto esperado respeita a marcação (romaneio e vigia)

**Arquivos:**
- Modificar: `documentos_pedido/regras_documentos.py` (`boleto_esperado`)
- Modificar: `documentos_pedido/test_regras_documentos.py`
- Modificar: `roteirizacao/gerar_pdf_romaneios.py` (`avaliar_documentos_pedido`, `montar_pdf_rota`)
- Modificar: `roteirizacao/test_romaneio_boleto_esperado.py`
- Modificar: `documentos_pedido/vigia_documentos.py` (`_avaliar`, `main`)
- Modificar: `documentos_pedido/test_vigia_documentos.py`

**Interfaces:**
- `regras_documentos.boleto_esperado(sender_id, cobrancas, situacao_cliente: str | None = None) -> bool`, com as strings `"TEM_BOLETO"` e `"NAO_TEM_BOLETO"`. O módulo continua sem imports do projeto, então as strings ficam literais.
- `gerar_pdf_romaneios.avaliar_documentos_pedido(codigo, sender_id, docs_por_pedido, situacao_cliente: str | None = None) -> dict`.

- [ ] **Passo 1: testes (falha)**

Em `test_regras_documentos.py`, classe `BoletoEsperado`:

```python
    def test_marcacao_vence_a_nf(self):
        self.assertFalse(boleto_esperado(QUATRO_ESTRELAS, [1], "NAO_TEM_BOLETO"))
        self.assertTrue(boleto_esperado(QUATRO_ESTRELAS, [0], "TEM_BOLETO"))
        self.assertTrue(boleto_esperado(None, [], "TEM_BOLETO"))

    def test_pendente_usa_a_regra_atual(self):
        self.assertTrue(boleto_esperado(QUATRO_ESTRELAS, [1], "PENDENTE"))
        self.assertFalse(boleto_esperado(QUATRO_ESTRELAS, [-1], "PENDENTE"))
```

Em `test_romaneio_boleto_esperado.py`, classe `AvaliarDocumentos`. Usar o sender neutro que o arquivo já usa (`SENDER_COMUM`); se a constante tiver outro nome no arquivo, usar o dele:

```python
    def _av_sit(self, sender, docs, situacao):
        with patch.object(gpr, "_abrir_documentos", side_effect=_abrir_tudo):
            return gpr.avaliar_documentos_pedido("PS-1", sender, {"PS-1": docs}, situacao_cliente=situacao)

    def test_nao_tem_boleto_desliga_cobranca(self):
        r = self._av_sit(SENDER_COMUM, [_nf(1)], "NAO_TEM_BOLETO")
        self.assertNotIn("sem boleto", r["faltas"])

    def test_tem_boleto_liga_mesmo_sem_quadro(self):
        self.assertIn("sem boleto", self._av_sit(SENDER_COMUM, [_nf(-1)], "TEM_BOLETO")["faltas"])
```

Em `test_vigia_documentos.py`, classe `Pendencias`:

```python
    def test_situacao_do_cliente_chega_na_avaliacao(self):
        servicos = [{"codigo": "PS-1", "sender_id": 1, "embarcador": "NUU", "situacao_cliente": "NAO_TEM_BOLETO"}]
        with patch.object(vd, "_avaliar_romaneio", return_value={"faltas": []}) as av:
            vd.checar_pendencias(servicos, {})
        self.assertEqual(av.call_args.kwargs["situacao_cliente"], "NAO_TEM_BOLETO")
```

- [ ] **Passo 2:** rodar os três módulos e ver falhar.

- [ ] **Passo 3: implementar**

`regras_documentos.boleto_esperado`:

```python
def boleto_esperado(sender_id: int | None, cobrancas: list[int | None],
                    situacao_cliente: str | None = None) -> bool:
    """cobrancas: o valor de cobranca_da_danfe de cada NF do pedido
    (lista vazia = pedido sem NF). situacao_cliente: decisão do time em
    regras/clientes_boleto.py (06/10) -- TEM_BOLETO/NAO_TEM_BOLETO vencem
    a NF; sem decisão (None/PENDENTE) vale a regra pela NF."""
    if situacao_cliente == "NAO_TEM_BOLETO":
        return False
    if situacao_cliente == "TEM_BOLETO":
        return True
    cobrancas = [None if c == -1 else c for c in cobrancas]
    ...  # resto igual ao de hoje
```

O `...` acima não é para copiar: mantenha exatamente o corpo atual da função depois da normalização (`any(c == 1 ...)`, `all(c == 0 ...)`, lista de senders). Só o docstring e os dois `if` do topo são novos.

`gerar_pdf_romaneios.avaliar_documentos_pedido`:
- acrescentar o parâmetro `situacao_cliente: str | None = None`;
- na chamada de `_boleto_esperado(...)`, passar `situacao_cliente=situacao_cliente`;
- no docstring, uma linha: "situacao_cliente: marcação do par em regras/clientes_boleto (06/10)".

`montar_pdf_rota`: antes do loop dos serviços, carregar as situações uma vez. Com falha, segue sem marcação, que é a regra atual:

```python
    try:
        from regras import clientes_boleto
        situacoes = clientes_boleto.situacao_por_pedido(
            {c for s in servicos for c in _codigos_base_lista(s.get("code") or "")})
    except Exception as e:
        logger.warning(f"  Clientes com boleto indisponível ({e}) -- romaneio segue pela regra da NF.")
        situacoes = {}
```

Dentro do loop, na chamada de `avaliar_documentos_pedido`:

```python
        situacao = next((situacoes[c] for c in _codigos_base_lista(codigo) if c in situacoes), None)
        av = avaliar_documentos_pedido(codigo, sender_id, docs_por_pedido, situacao_cliente=situacao)
```

`vigia_documentos.py`:

(a) Renomear o import interno para facilitar o teste. A função `_avaliar(servico, docs_por_pedido)` passa a ser:

```python
def _avaliar_romaneio(codigo, sender_id, docs_por_pedido, situacao_cliente=None):
    import gerar_pdf_romaneios as gpr
    return gpr.avaliar_documentos_pedido(codigo, sender_id, docs_por_pedido, situacao_cliente=situacao_cliente)


def _avaliar(servico: dict, docs_por_pedido: dict) -> dict:
    return _avaliar_romaneio(servico["codigo"], servico["sender_id"], docs_por_pedido,
                             situacao_cliente=servico.get("situacao_cliente"))
```

Os testes antigos que fazem patch em `vd._avaliar` continuam valendo.

(b) Em `main`, depois de `servicos = servicos_do_dia(...)` e de calcular `codigos`:

```python
        try:
            from regras import clientes_boleto
            situacoes = clientes_boleto.situacao_por_pedido(codigos)
        except Exception:
            situacoes = {}
        for s in servicos:
            s["situacao_cliente"] = next(
                (situacoes[c] for c in gpr._codigos_base_lista(s["codigo"]) if c in situacoes), None)
```

O caminho de imports do vigia já tem a raiz do repositório no `sys.path`, então `from regras import clientes_boleto` funciona.

- [ ] **Passo 4:**

```bash
py -3.11 -m unittest documentos_pedido.test_regras_documentos documentos_pedido.test_vigia_documentos -v
py -3.11 -m unittest roteirizacao.test_romaneio_boleto_esperado roteirizacao.test_canhoteira_transportadora roteirizacao.test_documentacao_rota -v
```

Esperado: OK.

- [ ] **Passo 5: commit**

```bash
git add documentos_pedido/regras_documentos.py documentos_pedido/test_regras_documentos.py \
        roteirizacao/gerar_pdf_romaneios.py roteirizacao/test_romaneio_boleto_esperado.py \
        documentos_pedido/vigia_documentos.py documentos_pedido/test_vigia_documentos.py
git commit -m "Romaneio: boleto esperado respeita a marcação do cliente (tem / não tem boleto)"
```

---

### Tarefa 5: peças de canal — preferência, WhatsApp e área de chamado

**Arquivos:**
- Modificar: `preferencias_notificacao.py` (novo tipo em `TIPOS`, `whatsapp_do_embarcador` com o parâmetro `tipo`)
- Modificar: `notificar_whatsapp.py` (`texto_boleto_faltando`, `avisar_cliente_boleto_faltando`)
- Modificar: `portal_cliente/chamados.py` (`AREAS` ganha `"boleto": "Boleto"`)
- Criar: `test_canais_boleto.py` (raiz)

**Interfaces produzidas:**
- `preferencias_notificacao.TIPOS["boleto_faltando"]`, com `grupo="acao"` e `default=True`, inserido **antes** de `chamado_sem_resposta`. Existe teste que exige `chamado_sem_resposta` em último.
- `preferencias_notificacao.whatsapp_do_embarcador(conn, cnpj, tipo: str = "chamado_sem_resposta") -> str | None`.
- `notificar_whatsapp.texto_boleto_faltando(codigos: list[str], quando: str) -> str`, onde `quando` é "amanhã" ou "hoje". Sempre ≤ `MAX_MENSAGEM`.
- `notificar_whatsapp.avisar_cliente_boleto_faltando(chave: str, telefone: str, texto: str, config: dict, modo_teste: bool = False, **kw) -> str`, com as mesmas situações de `avisar_cliente_fora_dia_fixo`.
- `chamados.AREAS["boleto"] == "Boleto"`.

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest test_canais_boleto
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "portal_cliente"))

import notificar_whatsapp as nw  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402
import chamados as ch  # noqa: E402


class Preferencia(unittest.TestCase):
    def test_tipo_novo_antes_do_whatsapp(self):
        self.assertIn("boleto_faltando", pn.TIPOS)
        self.assertTrue(pn.TIPOS["boleto_faltando"]["default"])
        self.assertEqual(list(pn.TIPOS)[-1], "chamado_sem_resposta")

    def test_whatsapp_por_tipo(self):
        c = sqlite3.connect(":memory:")
        pn._garantir_tabela(c)
        c.execute("INSERT INTO preferencias_notificacao (cnpj_embarcador, whatsapp, boleto_faltando, "
                  "chamado_sem_resposta) VALUES ('1', '5511987654321', 0, 1)")
        self.assertIsNone(pn.whatsapp_do_embarcador(c, "1", tipo="boleto_faltando"))
        self.assertEqual(pn.whatsapp_do_embarcador(c, "1"), "5511987654321")


class TextoWhatsApp(unittest.TestCase):
    def test_cabe_e_corta_codigos(self):
        cods = [f"PS-4{i:04d}" for i in range(40)]
        t = nw.texto_boleto_faltando(cods, "amanhã")
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        self.assertIn("40 pedidos de amanhã sem boleto", t)
        self.assertIn("PS-40000", t)
        self.assertIn("+", t)

    def test_um_pedido(self):
        t = nw.texto_boleto_faltando(["PS-1"], "hoje")
        self.assertIn("1 pedido de hoje sem boleto (PS-1)", t)


class AvisoWhatsApp(unittest.TestCase):
    def _cfg(self, forcar=""):
        return {"whatsapp_notificacoes": {"ativo": True, "clientes": {"ativo": True, "forcar_destino": forcar}}}

    def test_desligado_sem_telefone(self):
        self.assertEqual(nw.avisar_cliente_boleto_faltando("k", "", "t", self._cfg()), "desligado")

    def test_forcar_destino_prefixa_e_redireciona(self):
        with patch.object(nw.integracao_openwa, "numero_existe", return_value=True), \
             patch.object(nw, "despachar", return_value="enviado") as d:
            r = nw.avisar_cliente_boleto_faltando("boleto:11:2026-10-07", "5511911112222", "texto",
                                                  self._cfg("5511999990000"))
        self.assertEqual(r, "enviado")
        self.assertTrue(d.call_args.args[3].startswith("[teste → +5511911112222]"))
        self.assertEqual(d.call_args.kwargs["grupo_id"], "5511999990000@c.us")
        self.assertEqual(d.call_args.args[4], "boleto:11:2026-10-07")


class AreaChamado(unittest.TestCase):
    def test_area_boleto(self):
        self.assertEqual(ch.AREAS["boleto"], "Boleto")


if __name__ == "__main__":
    unittest.main()
```

Conferir se `clientes_ligado` lê exatamente `whatsapp_notificacoes.ativo` e `whatsapp_notificacoes.clientes.ativo`. Se ler outras chaves, ajustar `_cfg` do teste para ligar do jeito que a função espera.

- [ ] **Passo 2:** `py -3.11 -m unittest test_canais_boleto -v` falha.

- [ ] **Passo 3: implementar**

`preferencias_notificacao.py`: em `TIPOS`, logo antes de `"chamado_sem_resposta"`:

```python
    # Aviso ao embarcador quando falta boleto de cliente que paga com boleto
    # (aviso_boleto_faltando.py, 06/10/2026). Vale pro e-mail e, com número
    # gravado, pro WhatsApp.
    "boleto_faltando": {
        "grupo": "acao",
        "rotulo": "Boleto faltando",
        "descricao": "Na véspera da entrega, os pedidos de clientes que pagam com boleto e ainda estão sem o "
                     "boleto, para você enviar a tempo de ir junto com a mercadoria.",
        "default": True,
    },
```

`whatsapp_do_embarcador`:

```python
def whatsapp_do_embarcador(conn: sqlite3.Connection, cnpj, tipo: str = "chamado_sem_resposta") -> str | None:
    """Numero do WhatsApp do embarcador pra um aviso do `tipo` (chamado sem
    resposta, boleto faltando...): so com a chave do tipo ligada e numero
    valido gravado. So leitura: sem tabela ou sem coluna e None."""
    if tipo not in TIPOS:
        raise ValueError(f"Tipo de notificacao desconhecido: {tipo}")
    if not _tabela_existe(conn) or "whatsapp" not in _colunas(conn) or tipo not in _colunas(conn):
        return None
    row = conn.execute(
        f"SELECT whatsapp, {tipo} FROM preferencias_notificacao WHERE cnpj_embarcador = ?",
        (_so_digitos(cnpj),)).fetchone()
    if not row or not row[1]:
        return None
    numero = _so_digitos(row[0])
    return numero if _RE_WHATSAPP.fullmatch(numero) else None
```

`notificar_whatsapp.py`, perto de `texto_clientes_boleto`:

```python
def texto_boleto_faltando(codigos: list[str], quando: str) -> str:
    """Pro embarcador, até MAX_MENSAGEM: quantos pedidos estão sem boleto e
    os primeiros códigos que couberem (aviso_boleto_faltando.py, 06/10)."""
    n = len(codigos)
    cabeca = f"Fresh Log: {n} pedido{'s' if n != 1 else ''} de {quando} sem boleto"
    rodape = "Detalhes no e-mail e no portal."
    for k in range(n, 0, -1):
        lista = ", ".join(codigos[:k]) + (f" +{n - k}" if k < n else "")
        texto = f"{cabeca} ({lista}). {rodape}"
        if len(texto) <= MAX_MENSAGEM:
            return texto
    return f"{cabeca}. {rodape}"[:MAX_MENSAGEM]
```

Logo depois de `avisar_cliente_fora_dia_fixo`, crie `avisar_cliente_boleto_faltando` com o mesmo corpo de `avisar_cliente_fora_dia_fixo`, mudando só três coisas:
- assinatura `(chave: str, telefone: str, texto: str, config: dict, modo_teste: bool = False, **kw)`;
- mensagens de log com `(boleto faltando {chave})`;
- `despachar(config, ORIGEM_CLIENTE, "boleto_faltando", texto, chave, modo_teste=modo_teste, grupo_id=f"{destino}@c.us", **kw)`.

Docstring: "Aviso direto ao embarcador (aviso_boleto_faltando.py e resumo das 20h). `chave` é a assinatura de não-repetição."

`portal_cliente/chamados.py`, em `AREAS`, depois de `"envios"`:

```python
    "boleto": "Boleto",  # boleto faltando (06/10), aberto pelo sistema -- aviso_boleto_faltando.py
```

- [ ] **Passo 4:** rodar os testes abaixo. Os que já existem não podem quebrar.

```bash
py -3.11 -m unittest test_canais_boleto test_preferencias_whatsapp test_notificar_whatsapp -v
```

Rodar também a suíte do portal, antes e depois, como na tarefa de 05/10:

```bash
py -3.11 -m unittest discover -s portal_cliente -p "test_*.py"
```

O número de erros pré-existentes tem que ser o mesmo.

- [ ] **Passo 5: commit**

```bash
git add preferencias_notificacao.py notificar_whatsapp.py portal_cliente/chamados.py test_canais_boleto.py
git commit -m "Boleto: preferência boleto_faltando, WhatsApp ao embarcador e área Boleto no atendimento"
```

---

### Tarefa 6: `boleto_faltando.py` — seleção dos pedidos e os três canais

**Arquivos:**
- Criar: `boleto_faltando.py` (raiz)
- Criar: `test_boleto_faltando.py` (raiz)

**Interfaces produzidas:**
- `DB_PATH: Path`.
- `pedidos_da_data(conn, data_iso: str) -> list[dict]`: pedidos com rota no núcleo ou em rascunho na data, sem duplicar. Cada item tem `codigo` (base), `codigo_servico`, `sender_id`, `documento`, `cliente`, `embarcador`.
- `faltando(conn, pedidos: list[dict], situacoes: dict[str, str]) -> list[dict]`: só os pares TEM_BOLETO sem boleto ENVIADO na base. Acrescenta `nf` (número da NF ENVIADA, se houver).
- `ja_avisados(conn, data_iso: str) -> set[str]` e `registrar_avisados(conn, itens: list[dict], data_iso: str, canais: list[str]) -> None`, sobre a tabela `boleto_faltando_avisos`.
- `agrupar_por_embarcador(itens) -> dict[int, list[dict]]`.
- `email_aviso(nome_emb: str, itens: list[dict], data_entrega: str, destino_original: list[str] | None) -> tuple[str, str]`, que devolve assunto e html.
- `registrar_no_chamado(emb: dict, sender_id: int, texto: str, pedido_ref: str, config: dict, conectar=None) -> int | None`, que devolve o id do chamado.

Tabela nova:

```
boleto_faltando_avisos (codigo TEXT, data_entrega TEXT, sender_id INTEGER, avisado_em TEXT, canais TEXT,
                        PRIMARY KEY (codigo, data_entrega))
```

Isso desvia da spec, que fala em `notificacoes_enviadas`. Ruling do autor do plano: uma tabela própria é mais simples de consultar e de testar, e nada mais lê essas chaves.

- [ ] **Passo 1: teste (falha)**

```python
# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest test_boleto_faltando
"""
import sqlite3
import unittest
from unittest.mock import MagicMock

import boleto_faltando as bf


def _con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE nucleo_rotas (vuupt_route_id INT, data_rota TEXT, status TEXT, cancelada_em TEXT);
        CREATE TABLE nucleo_pedidos (codigo TEXT, vuupt_service_id INT, sender_id INT, destinatario_codigo TEXT,
                                     destinatario_nome TEXT, remetente_nome TEXT, tipo TEXT, vuupt_route_id INT,
                                     excluido_em TEXT);
        CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, data_alvo TEXT, status TEXT);
        CREATE TABLE rascunhos_parada (rascunho_id INT, service_id INT, codigo TEXT, sender_id INT);
        CREATE TABLE documentos_processados (hash_conteudo TEXT, tipo TEXT, codigo_pedido TEXT, status TEXT,
                                             numero_nf TEXT);
        INSERT INTO nucleo_rotas VALUES (1, '2026-10-07', 'PLANEJADA', NULL), (2, '2026-10-07', 'CANCELADA', 'x');
        INSERT INTO nucleo_pedidos VALUES
          ('PS-1', 101, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', 1, NULL),
          ('PS-2', 102, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', NULL, NULL),
          ('PS-3', 103, 22, '98765432000110', 'PADARIA B', 'SOTILLE', 'delivery', 2, NULL),
          ('PS-4-R1', 104, 11, '12345678000190', 'MERCADO A', 'NUU', 'delivery', 1, NULL);
        INSERT INTO rascunhos_rota VALUES (7, '2026-10-07', 'RASCUNHO'), (8, '2026-10-07', 'DESCARTADO');
        INSERT INTO rascunhos_parada VALUES (7, 102, 'PS-2', 11), (7, 101, 'PS-1', 11), (8, 103, 'PS-3', 22);
        INSERT INTO documentos_processados VALUES ('n1', 'Nota Fiscal', 'PS-1', 'ENVIADO', '40958'),
                                                  ('b4', 'Boleto', 'PS-4', 'ENVIADO', NULL),
                                                  ('b2', 'Boleto', 'PS-2', 'REVISAO_MANUAL', NULL);
    """)
    return c


class Selecao(unittest.TestCase):
    def test_rota_e_rascunho_sem_duplicar_sem_cancelada_sem_descartado(self):
        cods = sorted(p["codigo"] for p in bf.pedidos_da_data(_con(), "2026-10-07"))
        self.assertEqual(cods, ["PS-1", "PS-2", "PS-4"])

    def test_faltando_so_tem_boleto_sem_boleto_enviado(self):
        c = _con()
        peds = bf.pedidos_da_data(c, "2026-10-07")
        sit = {"PS-1": "TEM_BOLETO", "PS-2": "PENDENTE", "PS-4": "TEM_BOLETO"}
        r = bf.faltando(c, peds, sit)
        self.assertEqual([(i["codigo"], i["nf"]) for i in r], [("PS-1", "40958")])

    def test_avisados_uma_vez(self):
        c = _con()
        itens = [{"codigo": "PS-1", "sender_id": 11}]
        self.assertEqual(bf.ja_avisados(c, "2026-10-07"), set())
        bf.registrar_avisados(c, itens, "2026-10-07", ["email"])
        bf.registrar_avisados(c, itens, "2026-10-07", ["email"])
        self.assertEqual(bf.ja_avisados(c, "2026-10-07"), {"PS-1"})
        self.assertEqual(bf.ja_avisados(c, "2026-10-08"), set())


class Email(unittest.TestCase):
    def test_email_tem_pedidos_e_aviso_de_piloto(self):
        itens = [{"codigo": "PS-1", "cliente": "MERCADO A", "nf": "40958"}]
        assunto, corpo = bf.email_aviso("NUU", itens, "07/10/2026", destino_original=["fin@nuu.com"])
        self.assertIn("07/10", assunto)
        self.assertIn("PS-1", corpo)
        self.assertIn("MERCADO A", corpo)
        self.assertIn("40958", corpo)
        self.assertIn("fin@nuu.com", corpo)
        self.assertIn("entregas@freshlogbr.com", corpo)


class Chamado(unittest.TestCase):
    def _ch(self, abertos):
        ch = MagicMock()
        ch.STATUS_AGUARDANDO_FL = "AGUARDANDO_FL"
        ch.ORIGEM_SISTEMA = "sistema"
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = abertos
        ch.conectar.return_value = conn
        ch.buscar_chamado.side_effect = lambda c, i: {"id": i}
        ch.criar_chamado.return_value = {"id": 77}
        return ch, conn

    def test_reaproveita_chamado_aberto(self):
        ch, conn = self._ch((5,))
        cid = bf.registrar_no_chamado({"cnpj": "1", "nome": "NUU"}, 11, "texto", "PS-1", {}, modulo_chamados=ch)
        self.assertEqual(cid, 5)
        ch.criar_chamado.assert_not_called()
        ch.mensagem_sistema.assert_called_once()

    def test_abre_quando_nao_ha(self):
        ch, conn = self._ch(None)
        cid = bf.registrar_no_chamado({"cnpj": "1", "nome": "NUU"}, 11, "texto", "PS-1", {}, modulo_chamados=ch)
        self.assertEqual(cid, 77)
        kw = ch.criar_chamado.call_args.kwargs
        self.assertEqual((kw["area"], kw["assunto"]), ("boleto", "Boleto faltando"))
        ch.whatsapp_para_atendimento.assert_called_once()
        sql = conn.execute.call_args_list[0].args[0]
        self.assertIn("area = 'boleto'", sql)
        self.assertIn("status != 'RESOLVIDO'", sql)


if __name__ == "__main__":
    unittest.main()
```

A interface de `registrar_no_chamado` fica com o parâmetro `modulo_chamados=None`. Quando é None, importa `chamados` de `portal_cliente`. Isso substitui o `conectar=None` da lista de interfaces acima; vale o código do teste.

- [ ] **Passo 2:** `py -3.11 -m unittest test_boleto_faltando -v` falha porque o módulo não existe.

- [ ] **Passo 3: implementar `boleto_faltando.py`**

```python
# -*- coding: utf-8 -*-
"""
boleto_faltando.py

Peças do aviso de boleto faltando ao embarcador (06/10/2026, spec
docs/superpowers/specs/2026-10-06-clientes-boleto-aviso-design.md):
quais pedidos de uma data estão sem boleto (só clientes marcados
TEM_BOLETO em regras/clientes_boleto.py), quem já foi avisado, o e-mail
e o chamado. Usado por aviso_boleto_faltando.py (18h30, pedidos de
amanhã) e pelo resumo das 20h (entregues hoje).
"""
import html
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from email_utils import envelope_html  # noqa: E402
from regras.clientes_boleto import TEM_BOLETO, codigo_base, so_digitos  # noqa: E402

DB_PATH = _RAIZ / "dados" / "dados.db"
EMAIL_RESPOSTA = "entregas@freshlogbr.com"
URL_PORTAL = "https://app.freshhub.com.br/cliente"
AREA_CHAMADO = "boleto"
STATUS_RASCUNHO_VIVO = ("RASCUNHO", "ENVIADO", "ERRO_ENVIO")


def pedidos_da_data(conn: sqlite3.Connection, data_iso: str) -> list[dict]:
    rows = conn.execute("""
        SELECT p.codigo, p.sender_id, p.destinatario_codigo, p.destinatario_nome, p.remetente_nome
        FROM nucleo_rotas r JOIN nucleo_pedidos p ON p.vuupt_route_id = r.vuupt_route_id
        WHERE r.data_rota = ? AND r.cancelada_em IS NULL AND COALESCE(r.status, '') != 'CANCELADA'
          AND p.excluido_em IS NULL AND p.tipo = 'delivery'
        UNION ALL
        SELECT p.codigo, p.sender_id, p.destinatario_codigo, p.destinatario_nome, p.remetente_nome
        FROM rascunhos_rota r JOIN rascunhos_parada rp ON rp.rascunho_id = r.id
             JOIN nucleo_pedidos p ON p.vuupt_service_id = rp.service_id
        WHERE r.data_alvo = ? AND r.status IN (?, ?, ?) AND p.excluido_em IS NULL AND p.tipo = 'delivery'
    """, (data_iso, data_iso, *STATUS_RASCUNHO_VIVO)).fetchall()
    vistos, saida = set(), []
    for codigo, sid, dest, nome, emb in rows:
        base = codigo_base(codigo)
        if base in vistos:
            continue
        vistos.add(base)
        saida.append({"codigo": base, "codigo_servico": str(codigo or "").lstrip("#"), "sender_id": sid,
                      "documento": so_digitos(dest), "cliente": nome or "",
                      "embarcador": (emb or "").split(" - ")[-1].strip()})
    return saida


def faltando(conn: sqlite3.Connection, pedidos: list[dict], situacoes: dict[str, str]) -> list[dict]:
    alvo = [p for p in pedidos if situacoes.get(p["codigo"]) == TEM_BOLETO]
    if not alvo:
        return []
    marc = ",".join("?" * len(alvo))
    codigos = [p["codigo"] for p in alvo]
    com_boleto = {r[0] for r in conn.execute(
        f"SELECT DISTINCT codigo_pedido FROM documentos_processados WHERE status = 'ENVIADO' "
        f"AND tipo = 'Boleto' AND codigo_pedido IN ({marc})", codigos)}
    nfs = {r[0]: r[1] for r in conn.execute(
        f"SELECT codigo_pedido, MAX(numero_nf) FROM documentos_processados WHERE status = 'ENVIADO' "
        f"AND tipo = 'Nota Fiscal' AND codigo_pedido IN ({marc}) GROUP BY codigo_pedido", codigos)}
    return [{**p, "nf": nfs.get(p["codigo"]) or ""} for p in alvo if p["codigo"] not in com_boleto]


def _garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS boleto_faltando_avisos (codigo TEXT, data_entrega TEXT, "
                 "sender_id INTEGER, avisado_em TEXT, canais TEXT, PRIMARY KEY (codigo, data_entrega))")


def ja_avisados(conn: sqlite3.Connection, data_iso: str) -> set[str]:
    _garantir_tabela(conn)
    return {r[0] for r in conn.execute("SELECT codigo FROM boleto_faltando_avisos WHERE data_entrega = ?",
                                       (data_iso,))}


def registrar_avisados(conn: sqlite3.Connection, itens: list[dict], data_iso: str, canais: list[str]) -> None:
    _garantir_tabela(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany("INSERT OR IGNORE INTO boleto_faltando_avisos VALUES (?, ?, ?, ?, ?)",
                     [(i["codigo"], data_iso, i.get("sender_id"), agora, ",".join(canais)) for i in itens])
    conn.commit()


def agrupar_por_embarcador(itens: list[dict]) -> dict[int, list[dict]]:
    grupos: dict[int, list[dict]] = {}
    for i in itens:
        grupos.setdefault(i["sender_id"], []).append(i)
    return grupos


def email_aviso(nome_emb: str, itens: list[dict], data_entrega: str,
                destino_original: list[str] | None) -> tuple[str, str]:
    n = len(itens)
    assunto = f"[Freshlog] {n} pedido{'s' if n != 1 else ''} sem boleto para entrega em {data_entrega[:5]}"
    linhas = "".join(
        f"<tr><td style='padding:8px 10px;border-top:1px solid #E5E7EB;white-space:nowrap;'>"
        f"<strong>{html.escape(i.get('nf') or '—')}</strong><br><span style='font-size:12px;color:#6B7280;'>"
        f"{html.escape(i['codigo'])}</span></td>"
        f"<td style='padding:8px 10px;border-top:1px solid #E5E7EB;'>{html.escape(i.get('cliente') or '')}</td></tr>"
        for i in sorted(itens, key=lambda x: (x.get("nf") or "~", x["codigo"])))
    aviso = (f"<p style='margin:0 0 16px;padding:10px 14px;font-size:12px;background:#FEF3C7;border-radius:6px;'>"
             f"<strong>Redirecionado (piloto).</strong> Destino real: "
             f"{html.escape(', '.join(destino_original) or 'sem e-mail cadastrado')}</p>"
             if destino_original is not None else "")
    conteudo = f"""
    {aviso}
    <h2 style="margin:0 0 6px;font-size:19px;">Pedidos sem boleto · entrega em {html.escape(data_entrega)}</h2>
    <p style="margin:0 0 16px;font-size:14px;line-height:1.6;">Olá, <strong>{html.escape(nome_emb)}</strong>!
      Estes pedidos são de clientes que pagam com boleto e ainda não recebemos o boleto. Para ele seguir junto
      com a mercadoria, <strong>responda este e-mail com o boleto</strong> ou anexe no pedido da Stokki.</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #E5E7EB;border-radius:8px;margin-bottom:20px;font-size:13px;">
      <tr style="background:#F3F4F6;"><td style="padding:8px 10px;font-weight:600;">NF / pedido</td>
      <td style="padding:8px 10px;font-weight:600;">Cliente</td></tr>
      {linhas}
    </table>
    <p style="margin:0 0 8px;font-size:13px;line-height:1.6;">Se algum desses clientes não paga com boleto,
      responda avisando que ajustamos aqui. Também abrimos um chamado no
      <a href="{URL_PORTAL}">portal do cliente</a> para acompanhar.</p>
    <p style="margin:0;font-size:13px;">Dúvidas: <a href="mailto:{EMAIL_RESPOSTA}">{EMAIL_RESPOSTA}</a></p>"""
    return assunto, envelope_html(conteudo, "Freshlog Logística - aviso automático de boleto faltando.")


def registrar_no_chamado(emb: dict, sender_id: int, texto: str, pedido_ref: str, config: dict,
                         modulo_chamados=None) -> int | None:
    """Mensagem no chamado de boleto aberto do embarcador, ou abre um
    (área 'boleto'). Devolve o id ou None se falhar (não derruba o aviso)."""
    ch = modulo_chamados
    if ch is None:
        sys.path.insert(0, str(_RAIZ / "portal_cliente"))
        import chamados as ch
    conn = ch.conectar()
    try:
        r = conn.execute(f"SELECT id FROM portal_chamados WHERE cnpj_embarcador = ? AND area = '{AREA_CHAMADO}' "
                         f"AND status != 'RESOLVIDO' ORDER BY id DESC LIMIT 1", (emb.get("cnpj") or "",)).fetchone()
        if r:
            chamado = ch.buscar_chamado(conn, r[0])
        else:
            chamado = ch.criar_chamado(conn, {"cnpj": emb.get("cnpj") or "", "sender_id": sender_id,
                                              "nome": emb.get("nome") or ""},
                                       ch.ORIGEM_SISTEMA, ch.STATUS_AGUARDANDO_FL, assunto="Boleto faltando",
                                       area=AREA_CHAMADO, pedido_ref=pedido_ref)
            ch.whatsapp_para_atendimento(chamado, config)
        ch.mensagem_sistema(conn, chamado, texto)
        return chamado["id"]
    finally:
        conn.close()
```

`envelope_html` tem a assinatura `envelope_html(conteudo, rodape=..., cor_acento=...)`; a chamada acima passa o rodapé como segundo argumento posicional. O texto "abrimos um chamado" no e-mail está certo, porque o chamado abre de verdade.

- [ ] **Passo 4:** `py -3.11 -m unittest test_boleto_faltando -v` passa com 7 testes OK.

- [ ] **Passo 5: commit**

```bash
git add boleto_faltando.py test_boleto_faltando.py
git commit -m "Boleto: seleção dos pedidos sem boleto, registro de avisados, e-mail e chamado do embarcador"
```

---

### Tarefa 7: rotina `aviso_boleto_faltando.py` (18h30) + modo escopado com e-mail + sequência

**Arquivos:**
- Modificar: `documentos_pedido/processar_documentos.py` (parâmetro `com_email_embarcadores` em `main`)
- Modificar: `documentos_pedido/test_processar_documentos_modos.py`
- Criar: `aviso_boleto_faltando.py` (raiz)
- Criar: `test_aviso_boleto_faltando.py` (raiz)
- Modificar: `infra/sequencia_incremento.sh`

**Interfaces:**
- `processar_documentos.main(..., com_email_embarcadores: bool = False)`. No modo escopado, roda também `_etapas_email_embarcadores`.
- `aviso_boleto_faltando.executar(config: dict, data_iso: str, modo_teste: bool = False, atualizar: bool = True, conn=None, enviar=None, situacoes=None, embarcadores=None, rodar_documentos=None) -> dict`. As dependências são injetáveis para teste. Devolve `{"candidatos", "faltando", "embarcadores", "avisados", "falhas": [str]}`.

- [ ] **Passo 1: testes (falha)**

Em `test_processar_documentos_modos.py`, nova classe:

```python
class EscopadoComEmail(_Base):
    def test_escopado_com_email_roda_embarcadores(self):
        m = self._rodar(pedidos_stokki=["PS-9"], com_email_embarcadores=True)
        m["embarcadores"].assert_called_once()
        m["email"].assert_not_called()          # busca ampla da INBOX continua de fora
        m["retentar"].assert_not_called()

    def test_escopado_padrao_continua_sem_email(self):
        m = self._rodar(pedidos_stokki=["PS-9"])
        m["embarcadores"].assert_not_called()
```

`test_aviso_boleto_faltando.py`:

```python
# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest test_aviso_boleto_faltando
"""
import unittest
from unittest.mock import MagicMock, patch

import aviso_boleto_faltando as abf

PEDIDOS = [
    {"codigo": "PS-1", "codigo_servico": "PS-1", "sender_id": 11, "documento": "1", "cliente": "A", "embarcador": "NUU"},
    {"codigo": "PS-2", "codigo_servico": "PS-2", "sender_id": 11, "documento": "1", "cliente": "A", "embarcador": "NUU"},
    {"codigo": "PS-3", "codigo_servico": "PS-3", "sender_id": 22, "documento": "2", "cliente": "B", "embarcador": "SOT"},
]
EMBS = {11: {"nome": "NUU", "emails": ["fin@nuu.com"], "cnpj": "111", "desligado": False},
        22: {"nome": "SOTILLE", "emails": [], "cnpj": "222", "desligado": True}}
CFG = {"aviso_boleto_faltando": {"ativo": True, "forcar_destino": "hugo@freshlogbr.com"}}


class Executar(unittest.TestCase):
    def _rodar(self, faltando_antes, faltando_depois, ja=frozenset(), modo_teste=False, atualizar=True,
               email_ok=True, zap="enviado", chamado_ok=True):
        conn = MagicMock()
        rodar_docs = MagicMock()
        enviar = MagicMock(return_value=email_ok)
        seq = iter([faltando_antes, faltando_depois])
        with patch.object(abf.bf, "pedidos_da_data", return_value=PEDIDOS), \
             patch.object(abf.bf, "faltando", side_effect=lambda c, p, s: [x for x in next(seq)]), \
             patch.object(abf.bf, "ja_avisados", return_value=set(ja)), \
             patch.object(abf.bf, "registrar_avisados") as reg, \
             patch.object(abf.bf, "registrar_no_chamado", return_value=5 if chamado_ok else None) as cham, \
             patch.object(abf, "_telefone", return_value="5511911112222"), \
             patch.object(abf.notificar_whatsapp, "avisar_cliente_boleto_faltando", return_value=zap) as wz:
            r = abf.executar(CFG, "2026-10-07", modo_teste=modo_teste, atualizar=atualizar, conn=conn,
                             enviar=enviar, situacoes={"PS-1": "TEM_BOLETO"}, embarcadores=EMBS,
                             rodar_documentos=rodar_docs)
        return r, rodar_docs, enviar, wz, cham, reg

    def test_atualiza_so_os_candidatos_e_avisa_quem_continua_faltando(self):
        r, docs, enviar, wz, cham, reg = self._rodar([PEDIDOS[0], PEDIDOS[1]], [PEDIDOS[0]])
        docs.assert_called_once()
        self.assertEqual(sorted(docs.call_args.args[0]), ["PS-1", "PS-2"])
        self.assertEqual(r["avisados"], 1)
        self.assertEqual(enviar.call_args.args[0], ["hugo@freshlogbr.com"])   # forcar_destino
        wz.assert_called_once()
        cham.assert_called_once()
        reg.assert_called_once()
        self.assertEqual([i["codigo"] for i in reg.call_args.args[1]], ["PS-1"])

    def test_ja_avisado_nao_entra(self):
        r, docs, enviar, *_ = self._rodar([PEDIDOS[0]], [PEDIDOS[0]], ja={"PS-1"})
        docs.assert_not_called()
        enviar.assert_not_called()
        self.assertEqual(r["avisados"], 0)

    def test_nada_faltando_nao_atualiza_nem_avisa(self):
        r, docs, enviar, *_ = self._rodar([], [])
        docs.assert_not_called()
        enviar.assert_not_called()

    def test_embarcador_desligado_so_chamado(self):
        r, docs, enviar, wz, cham, reg = self._rodar([PEDIDOS[2]], [PEDIDOS[2]])
        enviar.assert_not_called()
        wz.assert_not_called()
        cham.assert_called_once()
        reg.assert_called_once()                 # chamado conta como canal

    def test_todos_os_canais_falham_nao_registra(self):
        r, docs, enviar, wz, cham, reg = self._rodar([PEDIDOS[0]], [PEDIDOS[0]], email_ok=False,
                                                     zap="falhou", chamado_ok=False)
        reg.assert_not_called()
        self.assertTrue(r["falhas"])

    def test_modo_teste_nao_grava_nem_abre_chamado(self):
        r, docs, enviar, wz, cham, reg = self._rodar([PEDIDOS[0]], [PEDIDOS[0]], modo_teste=True)
        reg.assert_not_called()
        cham.assert_not_called()
        self.assertEqual(enviar.call_args.args[0], ["hugo@freshlogbr.com"])

    def test_sem_atualizar(self):
        r, docs, *_ = self._rodar([PEDIDOS[0]], [PEDIDOS[0]], atualizar=False)
        docs.assert_not_called()

    def test_falha_na_atualizacao_segue_e_marca_falha(self):
        conn = MagicMock()
        with patch.object(abf.bf, "pedidos_da_data", return_value=PEDIDOS), \
             patch.object(abf.bf, "faltando", return_value=[PEDIDOS[0]]), \
             patch.object(abf.bf, "ja_avisados", return_value=set()), \
             patch.object(abf.bf, "registrar_avisados"), \
             patch.object(abf.bf, "registrar_no_chamado", return_value=5), \
             patch.object(abf, "_telefone", return_value=None):
            r = abf.executar(CFG, "2026-10-07", conn=conn, enviar=MagicMock(return_value=True),
                             situacoes={}, embarcadores=EMBS, rodar_documentos=MagicMock(side_effect=RuntimeError("stokki")))
        self.assertEqual(r["avisados"], 1)
        self.assertTrue(any("stokki" in f for f in r["falhas"]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Passo 2:** rodar `py -3.11 -m unittest documentos_pedido.test_processar_documentos_modos` e, separadamente, `py -3.11 -m unittest test_aviso_boleto_faltando`. Os dois falham.

- [ ] **Passo 3: implementar**

`processar_documentos.main`:
- novo parâmetro `com_email_embarcadores: bool = False`;
- trocar `if not escopado:` antes de `_etapas_email_embarcadores(...)` por `if not escopado or com_email_embarcadores:`, com o comentário `# 06/10: aviso_boleto_faltando.py pede o e-mail dos embarcadores no modo escopado (boleto que chegou depois das 16h)`;
- docstring de `main`: uma linha sobre o parâmetro novo.

`aviso_boleto_faltando.py`:

```python
# -*- coding: utf-8 -*-
"""
aviso_boleto_faltando.py

Aviso de boleto faltando ao embarcador (06/10/2026, spec
docs/superpowers/specs/2026-10-06-clientes-boleto-aviso-design.md).
Último passo da sequência das 18h (infra/sequencia_incremento.sh), com
as rotas de amanhã já montadas:

  1. pedidos de amanhã (rota ou rascunho) de clientes TEM_BOLETO sem boleto;
  2. busca documentos SÓ desses (e-mail dos embarcadores + aba da Stokki),
     pra não avisar de boleto que chegou depois da rodada das 16h;
  3. quem continua sem boleto: e-mail + WhatsApp ao embarcador e chamado
     (área Boleto) no atendimento. Cada pedido é avisado uma vez por data
     de entrega, e só conta como avisado se algum canal funcionou.

Envio real desligado por padrão: aviso_boleto_faltando.forcar_destino
(sem a chave = hugo@) e whatsapp_notificacoes.clientes.forcar_destino.
O chamado abre de verdade (decisão do Hugo).

COMO USAR:
    py -3.11 aviso_boleto_faltando.py --modo-teste
    py -3.11 aviso_boleto_faltando.py --data 2026-10-07 --sem-atualizar
"""
import argparse
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import yaml

import boleto_faltando as bf
import notificar_whatsapp
import preferencias_notificacao
from email_utils import enviar_email
from notificar_execucao_agente import notificar_execucao
from regras import clientes_boleto

logger = logging.getLogger("aviso_boleto_faltando")
TITULO = "Aviso de boleto faltando"
EMAIL_TESTE = "hugo@freshlogbr.com"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _secao(config: dict) -> dict:
    return (config or {}).get("aviso_boleto_faltando") or {}


def forcar_destino(config: dict) -> str:
    secao = _secao(config)
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def _telefone(cnpj: str) -> str | None:
    conn = sqlite3.connect(bf.DB_PATH, timeout=30)
    try:
        return preferencias_notificacao.whatsapp_do_embarcador(conn, cnpj, tipo="boleto_faltando")
    finally:
        conn.close()


def _rodar_documentos(codigos: list[str], modo_teste: bool) -> None:
    sys.path.insert(0, str(_RAIZ / "documentos_pedido"))
    import processar_documentos
    processar_documentos.main(modo_teste=modo_teste, pedidos_stokki=codigos, notificar=False,
                              com_email_embarcadores=True)


def executar(config: dict, data_iso: str, modo_teste: bool = False, atualizar: bool = True, conn=None,
             enviar=None, situacoes=None, embarcadores=None, rodar_documentos=None) -> dict:
    r = {"candidatos": 0, "faltando": 0, "embarcadores": 0, "avisados": 0, "falhas": []}
    enviar = enviar or enviar_email
    rodar_documentos = rodar_documentos or _rodar_documentos
    proprio = conn is None
    conn = conn or sqlite3.connect(bf.DB_PATH, timeout=30)
    try:
        pedidos = bf.pedidos_da_data(conn, data_iso)
        if situacoes is None:
            situacoes = clientes_boleto.situacao_por_pedido([p["codigo"] for p in pedidos])
        avisados = bf.ja_avisados(conn, data_iso)
        candidatos = [p for p in bf.faltando(conn, pedidos, situacoes) if p["codigo"] not in avisados]
        r["candidatos"] = len(candidatos)
        if not candidatos:
            return r

        if atualizar:
            try:
                rodar_documentos([p["codigo"] for p in candidatos], modo_teste)
            except Exception as e:
                logger.exception(f"Atualização dos documentos falhou ({e}) -- avisando com o que há no banco.")
                r["falhas"].append(f"atualização dos documentos: {e}")
        ainda = {p["codigo"] for p in candidatos}
        itens = [p for p in bf.faltando(conn, pedidos, situacoes) if p["codigo"] in ainda]
        r["faltando"] = len(itens)

        if embarcadores is None:
            embarcadores = preferencias_notificacao.carregar_embarcadores("boleto_faltando")
        forcar = forcar_destino(config)
        data_br = datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
        quando = "amanhã" if data_iso == (date.today() + timedelta(days=1)).isoformat() else f"{data_br[:5]}"

        for sid, lista in bf.agrupar_por_embarcador(itens).items():
            r["embarcadores"] += 1
            emb = embarcadores.get(sid) or {"nome": lista[0]["embarcador"], "emails": [], "cnpj": "", "desligado": True}
            nome = emb["nome"] or lista[0]["embarcador"] or f"Remetente {sid}"
            codigos = [i["codigo"] for i in lista]
            canais = []

            if not emb["desligado"] and (emb["emails"] or forcar or modo_teste):
                destinos = [EMAIL_TESTE] if modo_teste else ([forcar] if forcar else emb["emails"])
                original = emb["emails"] if (modo_teste or forcar) else None
                assunto, corpo = bf.email_aviso(nome, lista, data_br, original)
                if enviar(destinos, assunto, corpo, config.get("email", {}),
                          cabecalhos_extra={"Reply-To": bf.EMAIL_RESPOSTA}):
                    canais.append("email")
                else:
                    r["falhas"].append(f"{nome}: e-mail não saiu")

            if not emb["desligado"] and emb.get("cnpj"):
                tel = _telefone(emb["cnpj"])
                if tel:
                    st = notificar_whatsapp.avisar_cliente_boleto_faltando(
                        f"boleto:{sid}:{data_iso}", tel, notificar_whatsapp.texto_boleto_faltando(codigos, quando),
                        config, modo_teste=modo_teste)
                    if st in ("enviado", "modo_teste"):
                        canais.append("whatsapp")
                    elif st not in ("desligado", "nao_enviado"):
                        r["falhas"].append(f"{nome}: WhatsApp {st}")

            if not modo_teste:
                texto = (f"Pedidos com entrega em {data_br} de clientes que pagam com boleto, ainda sem boleto: "
                         + ", ".join(f"{i['codigo']}{' (NF ' + i['nf'] + ')' if i.get('nf') else ''} - {i['cliente']}"
                                     for i in lista)
                         + ". Envie o boleto respondendo aqui, por e-mail ou anexando no pedido da Stokki.")
                try:
                    if bf.registrar_no_chamado(emb, sid, texto, codigos[0], config):
                        canais.append("chamado")
                except Exception as e:
                    logger.exception(f"  {nome}: chamado não abriu: {e}")
                    r["falhas"].append(f"{nome}: chamado {e}")

            if canais and not modo_teste:
                bf.registrar_avisados(conn, lista, data_iso, canais)
            if canais:
                r["avisados"] += len(lista)
                logger.info(f"  {nome}: {len(lista)} pedido(s) avisado(s) por {', '.join(canais)}")
            else:
                r["falhas"].append(f"{nome}: nenhum canal funcionou ({', '.join(codigos)})")
        return r
    finally:
        if proprio:
            conn.close()


def main(data: str | None = None, modo_teste: bool = False, atualizar: bool = True) -> dict:
    inicio = time.time()
    config = _carregar_config()
    data_iso = data or (date.today() + timedelta(days=1)).isoformat()
    if not modo_teste and not _secao(config).get("ativo", True):
        logger.info("aviso_boleto_faltando.ativo=false -- nada a fazer.")
        return {}
    resumo = {}
    try:
        r = executar(config, data_iso, modo_teste=modo_teste, atualizar=atualizar)
        resumo[TITULO] = {"status": "erro" if r["falhas"] else "ok",
                          "detalhe": f"{data_iso}: {r['candidatos']} candidato(s), {r['faltando']} ainda sem boleto, "
                                     f"{r['avisados']} avisado(s) em {r['embarcadores']} embarcador(es)"
                                     + (" | " + "; ".join(r["falhas"][:5]) if r["falhas"] else "")}
    except Exception as e:
        logger.exception(f"Erro no aviso de boleto faltando: {e}")
        r, resumo[TITULO] = {}, {"status": "erro", "detalhe": str(e)}
    try:
        notificar_execucao(resumo, time.time() - inicio, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")
    return r


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(_RAIZ / "dados" / "aviso_boleto_faltando.log", encoding="utf-8")])
    p = argparse.ArgumentParser(description="Avisa o embarcador dos pedidos de amanhã sem boleto")
    p.add_argument("--data", default=None, help="Data de entrega AAAA-MM-DD (padrão: amanhã)")
    p.add_argument("--modo-teste", action="store_true", help="Não grava, não abre chamado; e-mail/WhatsApp só pro teste")
    p.add_argument("--sem-atualizar", action="store_true", help="Não busca documentos antes de avisar")
    a = p.parse_args()
    main(a.data, modo_teste=a.modo_teste, atualizar=not a.sem_atualizar)
```

Conferências antes de finalizar:
- `enviar_email` aceita `cabecalhos_extra`; o mock do teste aceita qualquer kwargs.
- `MagicMock` como `conn`: as funções de `bf` estão todas mockadas no teste, então `conn` não é usado de fato.
- O retorno `"nao_enviado"` do `despachar` (teto ou janela) não conta como falha.

`infra/sequencia_incremento.sh`: acrescentar ao fim. `|| true` porque um aviso que falha não pode quebrar a sequência; o erro aparece na notificação de execução.

```bash
cd "$RAIZ" && "$PY" marcar_clientes_boleto.py || true
cd "$RAIZ" && "$PY" aviso_boleto_faltando.py || true
```

Atualizar o comentário do topo do script: "Depois do incremento: sugere clientes com boleto e avisa o embarcador dos pedidos de amanhã sem boleto (06/10)."

- [ ] **Passo 4:**

```bash
py -3.11 -m unittest documentos_pedido.test_processar_documentos_modos
py -3.11 -m unittest test_aviso_boleto_faltando test_boleto_faltando
```

Esperado: OK.

- [ ] **Passo 5: commit**

```bash
git add documentos_pedido/processar_documentos.py documentos_pedido/test_processar_documentos_modos.py \
        aviso_boleto_faltando.py test_aviso_boleto_faltando.py infra/sequencia_incremento.sh
git commit -m "Boleto: aviso das 18h30 ao embarcador (e-mail, WhatsApp e chamado) depois de atualizar os documentos"
```

---

### Tarefa 8: seção "Entregues hoje sem boleto" no resumo das 20h

**Arquivos:**
- Modificar: `resumo_diario_embarcador.py`
- Modificar ou criar: o teste do resumo. Procurar com `git ls-files | grep -i resumo_diario`; se houver `test_resumo_diario_embarcador.py`, acrescentar lá, senão criar com esse nome.

**Interfaces:** consome `boleto_faltando.faltando`, `regras.clientes_boleto.situacao_por_pedido`, `notificar_whatsapp.texto_boleto_faltando`, `avisar_cliente_boleto_faltando`, `boleto_faltando.registrar_no_chamado`.

- [ ] **Passo 1: teste (falha)**

```python
class SecaoBoleto(unittest.TestCase):
    BLOCOS = {"entregues": [{"codigo": "PS-1", "nf": "40958", "destinatario": "MERCADO A", "endereco": "",
                             "concluido_em": "10:00"}], "falhas": [], "abertos": []}

    def test_bloco_aparece_com_pedidos(self):
        assunto, corpo = rde.montar_email("NUU", self.BLOCOS, rde.date(2026, 10, 7), {}, lambda p: None,
                                          sem_boleto=[{"codigo": "PS-1", "nf": "40958", "cliente": "MERCADO A"}])
        self.assertIn("Entregues sem boleto", corpo)
        self.assertIn("40958", corpo)

    def test_sem_lista_nao_tem_bloco(self):
        _a, corpo = rde.montar_email("NUU", self.BLOCOS, rde.date(2026, 10, 7), {}, lambda p: None)
        self.assertNotIn("Entregues sem boleto", corpo)

    def test_chave_desligada_por_padrao(self):
        self.assertFalse(rde.secao_boleto_ligada({}))
        self.assertTrue(rde.secao_boleto_ligada({"resumo_diario_embarcador": {"secao_boleto": True}}))

    def test_entregues_sem_boleto_filtra(self):
        with patch.object(rde, "_situacoes", return_value={"PS-1": "TEM_BOLETO"}), \
             patch.object(rde.boleto_faltando, "faltando",
                          side_effect=lambda c, p, s: [{**x, "nf": "40958"} for x in p if s.get(x["codigo"]) == "TEM_BOLETO"]):
            r = rde.entregues_sem_boleto(MagicMock(), self.BLOCOS["entregues"])
        self.assertEqual([i["codigo"] for i in r], ["PS-1"])
```

Usar o alias de import do módulo que o arquivo de teste já usa (`rde` no exemplo). Importar `MagicMock` e `patch` se ainda não estiverem importados.

- [ ] **Passo 2:** rodar e ver falhar.

- [ ] **Passo 3: implementar** em `resumo_diario_embarcador.py`:

(a) Imports, junto dos outros:

```python
import boleto_faltando
import notificar_whatsapp
```

(b) Funções novas, perto de `rotina_ativa`:

```python
def secao_boleto_ligada(config: dict) -> bool:
    """Seção "Entregues sem boleto" (06/10): desligada por padrão porque o
    resumo já sai de verdade pro embarcador -- ligar é decisão do Hugo."""
    return bool(_secao(config).get("secao_boleto", False))


def _situacoes(codigos: list[str]) -> dict[str, str]:
    from regras import clientes_boleto
    return clientes_boleto.situacao_por_pedido(codigos)


def entregues_sem_boleto(conn: sqlite3.Connection, entregues: list[dict]) -> list[dict]:
    """Entregues hoje de clientes TEM_BOLETO sem boleto ENVIADO."""
    from regras.clientes_boleto import codigo_base
    pedidos = [{"codigo": codigo_base(p["codigo"]), "sender_id": p.get("sender_id"),
                "cliente": p.get("destinatario") or "", "embarcador": ""} for p in entregues]
    if not pedidos:
        return []
    return boleto_faltando.faltando(conn, pedidos, _situacoes([p["codigo"] for p in pedidos]))


def _bloco_sem_boleto(itens: list[dict]) -> str:
    linhas = "".join("<tr>" + _nf({"nf": i.get("nf"), "codigo": i["codigo"]})
                     + _td(html.escape(i.get("cliente") or "")) + "</tr>" for i in itens)
    return _tabela(f"Entregues sem boleto ({len(itens)})", COR_AMBAR_TEXTO, ("NF / pedido", "Cliente"), linhas)
```

(c) `montar_email` ganha `sem_boleto: list[dict] | None = None`. No `corpo_blocos`, logo depois das falhas:

```python
    corpo_blocos = ((_bloco_falhas(falhas, tratativas) if falhas else "")
                    + (_bloco_sem_boleto(sem_boleto) if sem_boleto else "")
                    + (_bloco_abertos(abertos) if abertos else "")
                    + (_bloco_entregues(entregues, link_do_canhoto) if entregues else ""))
```

Se houver `sem_boleto`, acrescentar ao final, antes da assinatura, um parágrafo: "Os pedidos em <strong>Entregues sem boleto</strong> são de clientes que pagam com boleto e o boleto não chegou até nós. Responda este e-mail com o boleto ou anexe no pedido da Stokki."

(d) Em `executar`, dentro do loop, depois de `tratativas = ...`:

```python
                sem_boleto = []
                if secao_boleto_ligada(config):
                    try:
                        sem_boleto = entregues_sem_boleto(conn, blocos["entregues"])
                    except Exception as e:
                        logger.warning(f"  {nome}: seção sem boleto indisponível ({e}) -- resumo segue sem ela.")
```

Passar `sem_boleto=sem_boleto` para `montar_email`. Depois do envio OK (dentro do `if enviar(...)`), acrescentar:

```python
                    if sem_boleto and not modo_teste:
                        _avisar_sem_boleto(config, sid, emb, sem_boleto, data_alvo)
```

E a função auxiliar, perto de `entregues_sem_boleto`:

```python
def _avisar_sem_boleto(config: dict, sid: int, emb: dict, itens: list[dict], data_alvo: date) -> None:
    """WhatsApp com a contagem + mensagem no chamado de boleto (06/10, opção
    A do Hugo). Falha aqui nunca derruba o resumo."""
    codigos = [i["codigo"] for i in itens]
    try:
        if emb.get("cnpj"):
            conn = sqlite3.connect(DB_PATH, timeout=30)
            try:
                tel = preferencias_notificacao.whatsapp_do_embarcador(conn, emb["cnpj"], tipo="boleto_faltando")
            finally:
                conn.close()
            if tel:
                notificar_whatsapp.avisar_cliente_boleto_faltando(
                    f"boleto_entregues:{sid}:{data_alvo.isoformat()}", tel,
                    notificar_whatsapp.texto_boleto_faltando(codigos, "hoje"), config)
        boleto_faltando.registrar_no_chamado(
            emb, sid, f"Entregues em {data_alvo.strftime('%d/%m/%Y')} sem boleto: "
                      + ", ".join(f"{i['codigo']}{' (NF ' + i['nf'] + ')' if i.get('nf') else ''}" for i in itens)
                      + ". Envie o boleto respondendo aqui ou por e-mail.", codigos[0], config)
    except Exception as e:
        logger.warning(f"  Aviso de entregues sem boleto falhou ({e}) -- resumo já saiu.")
```

O texto do WhatsApp com `quando="hoje"` sai como "Fresh Log: 2 pedidos de hoje sem boleto (...)". A spec dizia "2 entregas de hoje sem boleto"; aceitável como está.

- [ ] **Passo 4:** rodar o teste do resumo:

```bash
py -3.11 -m unittest test_resumo_diario_embarcador
```

Esperado: OK, incluindo os testes antigos.

- [ ] **Passo 5: commit**

```bash
git add resumo_diario_embarcador.py test_resumo_diario_embarcador.py
git commit -m "Resumo 20h: seção Entregues sem boleto (atrás de secao_boleto, desligada) com WhatsApp e chamado"
```

---

### Tarefa 9: `MAPA_DO_SISTEMA.txt`

**Arquivos:** Modificar `MAPA_DO_SISTEMA.txt`, a cópia do worktree. Ler antes as seções de rotinas, sequências, tabelas e telas do painel, e seguir o formato das linhas vizinhas.

- [ ] **Passo 1:** acrescentar:
- na seção da sequência das 18h (`sequencia_incremento.sh`), os dois passos novos no fim: `marcar_clientes_boleto.py` e `aviso_boleto_faltando.py`;
- na lista de rotinas e scripts da raiz:
  - `marcar_clientes_boleto.py   Sugere clientes que pagam com boleto (PENDENTE p/ o time decidir).`
  - `aviso_boleto_faltando.py    18h30: avisa o embarcador dos pedidos de amanhã sem boleto (e-mail, WhatsApp, chamado).`
  - `boleto_faltando.py          Peças do aviso: seleção, avisados, e-mail, chamado.`
- em `regras/`: `clientes_boleto.py   Par embarcador+cliente que paga com boleto (sugestão/decisão).`;
- nas telas do painel: `/clientes-boleto   Decidir clientes com boleto (só total).`;
- nas tabelas: `clientes_boleto` e `boleto_faltando_avisos`;
- em config, as chaves novas: `aviso_boleto_faltando.ativo` e `.forcar_destino` (sem a chave = hugo@), e `resumo_diario_embarcador.secao_boleto` (padrão false);
- em armadilhas: "Chamado de boleto (área `boleto`) abre de verdade mesmo com o aviso em piloto — o embarcador vê no portal."

- [ ] **Passo 2: commit**

```bash
git add MAPA_DO_SISTEMA.txt
git commit -m "Mapa: clientes com boleto, aviso de boleto faltando e chaves novas"
```

---

### Tarefa 10: prova em produção (só quando o Hugo pedir deploy)

Seguir a skill `deploy-vps`.

- [ ] **Passo 1: suíte local.**

```bash
py -3.11 -m unittest regras.test_clientes_boleto test_marcar_clientes_boleto test_canais_boleto test_boleto_faltando test_aviso_boleto_faltando test_resumo_diario_embarcador test_preferencias_whatsapp test_notificar_whatsapp
py -3.11 -m unittest documentos_pedido.test_regras_documentos documentos_pedido.test_vigia_documentos documentos_pedido.test_processar_documentos_modos
py -3.11 -m unittest roteirizacao.test_romaneio_boleto_esperado roteirizacao.test_canhoteira_transportadora roteirizacao.test_documentacao_rota
py -3.11 -m unittest painel_agentes.test_clientes_boleto_tela painel_agentes.test_clientes_agenda_tela
```

- [ ] **Passo 2: deploy.** Merge com o `origin/master` mais novo, push por refspec quando o Hugo pedir, e `vps_ops.py deploy --restart painel-agentes portal-cliente`. Não há unit nova: a sequência das 18h chama os scripts.

- [ ] **Passo 3: primeira carga em modo teste.**

```bash
vps_ops.py rodar marcar_clientes_boleto.py --modo-teste
```

Conferir quantos pares seriam sugeridos e mostrar ao Hugo. Depois rodar de verdade (`--real`) para popular a tela, porque só grava sugestões.

- [ ] **Passo 4: tela.** `vps_ler.py saude` e um `curl` em `/painel/clientes-boleto` (porta 8071, espera 302 sem sessão). Pedir ao Hugo para abrir a tela logado.

- [ ] **Passo 5: aviso em modo teste.**

```bash
vps_ops.py rodar aviso_boleto_faltando.py --modo-teste
```

Com zero pares TEM_BOLETO, deve dizer "0 candidato(s)". Depois que o time marcar alguns, rodar de novo em modo teste. O e-mail e o WhatsApp vão para o Hugo, e nenhum chamado é aberto.

- [ ] **Passo 6: resumo das 20h.** Só depois do Hugo ligar `secao_boleto: true` no config da VPS. Rodar `resumo_diario_embarcador.py --modo-teste` e conferir a seção no e-mail.

---

## Fora deste plano

- Desmarcar automaticamente pela resposta do embarcador no chamado.
- Sugerir NAO_TEM_BOLETO automaticamente.
- O embarcador marcar os clientes pelo portal.
- Muai, que não tem sender_id na `interno`.
