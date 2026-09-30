# Bloqueio de área não atendida + pedidos dedicados — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pedido do portal para cidade não atendida fica retido ("Aguardando liberação"), o cliente é avisado por chat e e-mail, a equipe libera no `/atendimento` (opcionalmente como "dedicado" com valor), qualquer pedido pode ser marcado como dedicado no Planejamento, a roteirização automática pula dedicados, e o financeiro recebe a lista da quinzena nos dias 1 e 16.

**Architecture:** Um módulo novo `pedidos_dedicados.py` (raiz, sem Flask) é dono da tabela `pedidos_dedicados` e de toda a lógica de divisão de valor, quinzena e normalização de código. O portal ganha o status `AGUARDANDO_LIBERACAO` e a checagem reaproveita `identificar_area_nao_atendida` sobre um serviço sintético. O chat existente (`portal_cliente/chamados.py`) carrega o aviso e a liberação. Roteirização e pool consultam `ativos_por_codigo()`.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db`), unittest, JS vanilla nos templates Jinja, systemd timer.

**Spec:** `docs/superpowers/specs/2026-09-23-bloqueio-area-nao-atendida-pedidos-dedicados-design.md`

## Global Constraints

- Python sempre `py -3.11`. Testes: `py -3.11 -m unittest <modulo>` (sem pytest). Compilar: `py -3.11 -m py_compile <arquivos>`.
- Código, comentários, commits em português. Comentários/log sem acento nos arquivos que já seguem esse padrão (scripts da raiz e `infra/`).
- **Commit só quando o Hugo pedir.** Cada task termina com testes verdes e `git add` apenas dos arquivos da task; o passo "Commit" abaixo só executa com autorização dele. Antes de editar: `git status --short` (outra sessão edita o mesmo working tree; `portal_cliente/app.py`, `enviar_stokki.py`, `painel_agentes.py` e `planejamento_rotas.html` já têm modificações alheias não commitadas — não reverter, não incluir).
- Nunca commitar/editar `config.yaml`. Chaves novas de config têm default no código (ver Task 9) e são listadas na mensagem final pro Hugo aplicar na VPS.
- Todo e-mail novo sai redirecionado para `hugo@freshlogbr.com` por padrão (`forcar_destino`), inclusive o do financeiro.
- Código de pedido: formato canônico `PS-NNNNN` (maiúsculo, sem `#`, sem sufixo `-R1`). **Não usar** `freshhub.pedidos_parados.normalizar_order_number` (devolve só dígitos e não trata sufixo); usar `pedidos_dedicados.normalizar_codigo`.
- Telas: `url_for()`/`BASE` + caminhos relativos ao `script_root`; nunca caminho absoluto no template.
- Nada de Blueprint: rotas do painel entram em `painel_agentes.py` com `@requer_auth(niveis=("total","operador"))` + `@exige_mesma_origem`; rotas do atendimento dentro de `atendimento_chamados.registrar()`.

## Review Focus

1. Cliente envia 10 NFs, 4 bloqueadas e 6 normais → só 1 chamado, as 6 vão `NA_FILA`, resposta da rota lista as 4. (Task 3 testa.)
2. Chave do Google ausente ou geocodificação lança exceção → nenhum pedido bloqueado, WARNING no log. (Task 2 testa.)
3. Valor `100.00` para 3 NFs → 33.34 + 33.33 + 33.33; `0.01` para 2 → 0.01 + 0.00; valor com vírgula `"150,50"` vindo do front → 150.5. (Task 1 e Task 5 testam.)
4. Serviço Vuupt com `code="#PS-12345, PS-12346"` (dois pedidos) e só um deles dedicado → serviço inteiro sai da rota automática (conservador). (Task 7 testa.)
5. `--data-ref 2026-10-01` → quinzena 16/09–30/09; `2026-03-01` → 16/02–29/02 (bissexto). (Task 9 testa.)

---

### Task 1: Módulo `pedidos_dedicados.py`

**Files:**
- Create: `pedidos_dedicados.py`
- Test: `test_pedidos_dedicados.py`

**Interfaces:**
- Produces:
  - `DB_PATH: Path`, `conectar(db_path=DB_PATH) -> sqlite3.Connection` (cria a tabela)
  - `normalizar_codigo(codigo) -> str` (`"#ps-1234-R1"` → `"PS-1234"`; sem padrão → texto sem `#`, upper, strip)
  - `codigos_do_servico(servico: dict) -> list[str]` (quebra `code` por vírgula e normaliza)
  - `dividir_valor(total: float, n: int) -> list[float]`
  - `marcar(conn, pedidos: list[dict], valor_total: float, por: str) -> str` (grupo_id)
  - `remover(conn, *, codigo_pedido=None, envio_id=None, service_id=None, por="") -> int`
  - `ativos_por_codigo(conn) -> dict[str, dict]`
  - `ativo_por_envio(conn, envio_id) -> dict | None`
  - `vincular_codigo(conn, envio_id, codigo_pedido) -> None`
  - `filtrar_dedicados(servicos: list[dict], ativos: dict[str, dict]) -> tuple[list[dict], list[dict]]`
  - `listar_quinzena(conn, data_ref: date) -> tuple[date, date, list[dict]]`

- [ ] **Step 1: Escrever os testes**

```python
# test_pedidos_dedicados.py
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_pedidos_dedicados"""
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pedidos_dedicados as pd  # noqa: E402


def _conn():
    return pd.conectar(":memory:")


class Normalizacao(unittest.TestCase):
    def test_formatos(self):
        for bruto, esperado in [("#PS-1234", "PS-1234"), ("ps 1234", "PS-1234"), ("PS-1234-R2", "PS-1234"),
                                ("PS1234", "PS-1234"), ("  #PS-99999 ", "PS-99999"), ("NF123", "NF123"), (None, "")]:
            self.assertEqual(pd.normalizar_codigo(bruto), esperado, bruto)

    def test_codigos_do_servico(self):
        self.assertEqual(pd.codigos_do_servico({"code": "#PS-37189, PS-37176-R1"}), ["PS-37189", "PS-37176"])
        self.assertEqual(pd.codigos_do_servico({}), [])


class Divisao(unittest.TestCase):
    def test_sobra_no_primeiro(self):
        self.assertEqual(pd.dividir_valor(100, 3), [33.34, 33.33, 33.33])

    def test_um_pedido(self):
        self.assertEqual(pd.dividir_valor(150.5, 1), [150.5])

    def test_centavo(self):
        self.assertEqual(pd.dividir_valor(0.01, 2), [0.01, 0.0])

    def test_invalido(self):
        with self.assertRaises(ValueError):
            pd.dividir_valor(10, 0)
        with self.assertRaises(ValueError):
            pd.dividir_valor(-1, 1)


class Marcacao(unittest.TestCase):
    def test_marcar_divide_e_agrupa(self):
        conn = _conn()
        grupo = pd.marcar(conn, [{"codigo_pedido": "#PS-1", "sender_id": 7, "remetente_nome": "ACME", "numero_nf": "10"},
                                 {"codigo_pedido": "PS-2", "sender_id": 7, "remetente_nome": "ACME"},
                                 {"envio_id": 55, "sender_id": 8, "remetente_nome": "Beta"}], 100, "hugo")
        rows = conn.execute("SELECT * FROM pedidos_dedicados ORDER BY id").fetchall()
        self.assertEqual([r["valor"] for r in rows], [33.34, 33.33, 33.33])
        self.assertEqual({r["grupo_id"] for r in rows}, {grupo})
        self.assertEqual(rows[0]["codigo_pedido"], "PS-1")
        self.assertEqual(rows[0]["valor_total_grupo"], 100)
        self.assertEqual(rows[2]["envio_id"], 55)
        self.assertIsNone(rows[2]["codigo_pedido"])

    def test_marcar_de_novo_atualiza(self):
        conn = _conn()
        pd.marcar(conn, [{"codigo_pedido": "PS-1"}], 50, "a")
        pd.marcar(conn, [{"codigo_pedido": "ps-1"}], 80, "b")
        rows = conn.execute("SELECT valor, marcado_por FROM pedidos_dedicados WHERE removido_em IS NULL").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["valor"], rows[0]["marcado_por"]), (80, "b"))

    def test_remover_e_ativos(self):
        conn = _conn()
        pd.marcar(conn, [{"codigo_pedido": "PS-1"}, {"codigo_pedido": "PS-2"}], 10, "a")
        self.assertEqual(pd.remover(conn, codigo_pedido="#PS-1", por="b"), 1)
        ativos = pd.ativos_por_codigo(conn)
        self.assertEqual(list(ativos), ["PS-2"])
        self.assertEqual(ativos["PS-2"]["valor"], 5.0)

    def test_vincular_codigo(self):
        conn = _conn()
        pd.marcar(conn, [{"envio_id": 9}], 30, "a")
        pd.vincular_codigo(conn, 9, "PS-777")
        self.assertIn("PS-777", pd.ativos_por_codigo(conn))
        self.assertEqual(pd.ativo_por_envio(conn, 9)["codigo_pedido"], "PS-777")

    def test_marcar_vazio(self):
        with self.assertRaises(ValueError):
            pd.marcar(_conn(), [], 10, "a")


class Filtro(unittest.TestCase):
    def test_servico_com_dois_codigos_sai_inteiro(self):
        ativos = {"PS-2": {"valor": 1}}
        servicos = [{"id": 1, "code": "#PS-1"}, {"id": 2, "code": "#PS-2, PS-3"}]
        restantes, dedicados = pd.filtrar_dedicados(servicos, ativos)
        self.assertEqual([s["id"] for s in restantes], [1])
        self.assertEqual([s["id"] for s in dedicados], [2])


class Quinzena(unittest.TestCase):
    def _semear(self, conn, quando, codigo):
        conn.execute("INSERT INTO pedidos_dedicados (codigo_pedido, valor, grupo_id, valor_total_grupo, marcado_em, marcado_por) "
                     "VALUES (?, 1, 'g', 1, ?, 'a')", (codigo, quando))

    def test_dia_16(self):
        conn = _conn()
        self._semear(conn, "2026-09-01 00:00:00", "PS-1")
        self._semear(conn, "2026-09-15 23:59:59", "PS-2")
        self._semear(conn, "2026-09-16 00:00:00", "PS-3")
        ini, fim, linhas = pd.listar_quinzena(conn, date(2026, 9, 16))
        self.assertEqual((ini, fim), (date(2026, 9, 1), date(2026, 9, 15)))
        self.assertEqual([l["codigo_pedido"] for l in linhas], ["PS-1", "PS-2"])

    def test_dia_1_mes_anterior_bissexto(self):
        conn = _conn()
        self._semear(conn, "2028-02-29 10:00:00", "PS-1")
        self._semear(conn, "2028-02-15 10:00:00", "PS-2")
        ini, fim, linhas = pd.listar_quinzena(conn, date(2028, 3, 1))
        self.assertEqual((ini, fim), (date(2028, 2, 16), date(2028, 2, 29)))
        self.assertEqual([l["codigo_pedido"] for l in linhas], ["PS-1"])

    def test_outro_dia_usa_quinzena_fechada_anterior(self):
        ini, fim, _ = pd.listar_quinzena(_conn(), date(2026, 9, 23))
        self.assertEqual((ini, fim), (date(2026, 9, 1), date(2026, 9, 15)))
        ini, fim, _ = pd.listar_quinzena(_conn(), date(2026, 9, 10))
        self.assertEqual((ini, fim), (date(2026, 8, 16), date(2026, 8, 31)))

    def test_removido_fica_fora(self):
        conn = _conn()
        self._semear(conn, "2026-09-05 10:00:00", "PS-1")
        conn.execute("UPDATE pedidos_dedicados SET removido_em = '2026-09-06 00:00:00'")
        self.assertEqual(pd.listar_quinzena(conn, date(2026, 9, 16))[2], [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_pedidos_dedicados`
Expected: `ModuleNotFoundError: No module named 'pedidos_dedicados'`

- [ ] **Step 3: Implementar o módulo**

```python
# pedidos_dedicados.py
# -*- coding: utf-8 -*-
"""
Pedidos dedicados (Hugo, 23/09/2026): marcacao de pedido que vai em
transporte dedicado (cotado a parte), com o valor da cotacao dividido
entre os pedidos do mesmo grupo. Alimenta:
  - roteirizacao (criar_rotas_diarias / incrementar_rotas): dedicado fica
    fora da rota compartilhada;
  - planejamento (pool): chip "Dedicado - R$ X";
  - portal do cliente (aba Envios): chip "Envio dedicado";
  - notificar_dedicados_financeiro.py: e-mail da quinzena (dias 1 e 16).

Tabela pedidos_dedicados, uma linha por pedido. Chave logica: codigo_pedido
(PS-NNNNN) OU envio_id (portal_envios.id) enquanto o codigo nao existe.
Remocao e logica (removido_em), pra quinzena fechada nao mudar.
"""
import re
import sqlite3
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent
DB_PATH = _RAIZ / "dados" / "dados.db"

_PADRAO_PS = re.compile(r"^#?\s*PS\s*[-._ ]?\s*(\d{1,7})", re.IGNORECASE)


def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pedidos_dedicados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo_pedido TEXT,
            service_id INTEGER,
            envio_id INTEGER,
            sender_id INTEGER,
            remetente_nome TEXT,
            numero_nf TEXT,
            valor REAL NOT NULL,
            grupo_id TEXT NOT NULL,
            valor_total_grupo REAL NOT NULL,
            marcado_em TEXT NOT NULL,
            marcado_por TEXT,
            removido_em TEXT,
            removido_por TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dedicados_codigo ON pedidos_dedicados (codigo_pedido, removido_em);
        CREATE INDEX IF NOT EXISTS idx_dedicados_envio ON pedidos_dedicados (envio_id, removido_em);
        CREATE INDEX IF NOT EXISTS idx_dedicados_marcado ON pedidos_dedicados (marcado_em);
    """)
    conn.commit()
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalizar_codigo(codigo) -> str:
    """'#ps-1234-R1' / 'PS 1234' / 'PS1234' -> 'PS-1234'. Sem o padrao PS,
    devolve o texto sem '#', maiusculo (pode ser NF do cliente)."""
    texto = str(codigo or "").strip()
    m = _PADRAO_PS.match(texto)
    if m:
        return f"PS-{int(m.group(1))}"
    return texto.lstrip("#").strip().upper()


def codigos_do_servico(servico: dict) -> list[str]:
    """service.code da Vuupt pode trazer mais de um pedido: '#PS-1, PS-2'."""
    bruto = str((servico or {}).get("code") or "")
    return [normalizar_codigo(p) for p in bruto.split(",") if p.strip()]


def dividir_valor(total: float, n: int) -> list[float]:
    """Divide em centavos; a sobra vai pro primeiro. Soma bate com o total."""
    if n <= 0:
        raise ValueError("n precisa ser >= 1")
    if total < 0:
        raise ValueError("valor negativo")
    centavos = int(round(float(total) * 100))
    base, sobra = divmod(centavos, n)
    partes = [base + sobra] + [base] * (n - 1)
    return [p / 100 for p in partes]


def _ativo(conn, *, codigo_pedido=None, envio_id=None) -> sqlite3.Row | None:
    if codigo_pedido:
        r = conn.execute("SELECT * FROM pedidos_dedicados WHERE codigo_pedido = ? AND removido_em IS NULL ORDER BY id DESC LIMIT 1",
                         (codigo_pedido,)).fetchone()
        if r:
            return r
    if envio_id is not None:
        return conn.execute("SELECT * FROM pedidos_dedicados WHERE envio_id = ? AND removido_em IS NULL ORDER BY id DESC LIMIT 1",
                            (envio_id,)).fetchone()
    return None


def marcar(conn: sqlite3.Connection, pedidos: list[dict], valor_total: float, por: str) -> str:
    """Cada pedido: {codigo_pedido?, service_id?, envio_id?, sender_id?,
    remetente_nome?, numero_nf?}. Pedido ja ativo (mesmo codigo ou mesmo
    envio_id) e atualizado, nao duplicado. Devolve o grupo_id."""
    if not pedidos:
        raise ValueError("nenhum pedido")
    partes = dividir_valor(valor_total, len(pedidos))
    grupo = uuid.uuid4().hex[:12]
    agora = _agora()
    for p, valor in zip(pedidos, partes):
        codigo = normalizar_codigo(p.get("codigo_pedido")) or None
        envio_id = p.get("envio_id")
        campos = {
            "codigo_pedido": codigo, "service_id": p.get("service_id"), "envio_id": envio_id,
            "sender_id": p.get("sender_id"), "remetente_nome": p.get("remetente_nome"), "numero_nf": p.get("numero_nf"),
            "valor": valor, "grupo_id": grupo, "valor_total_grupo": float(valor_total),
            "marcado_em": agora, "marcado_por": por,
        }
        existente = _ativo(conn, codigo_pedido=codigo, envio_id=envio_id)
        if existente:
            sets = ", ".join(f"{k} = COALESCE(?, {k})" if k in ("codigo_pedido", "service_id", "envio_id", "sender_id",
                                                              "remetente_nome", "numero_nf") else f"{k} = ?"
                             for k in campos)
            conn.execute(f"UPDATE pedidos_dedicados SET {sets} WHERE id = ?", (*campos.values(), existente["id"]))
        else:
            cols = ", ".join(campos)
            conn.execute(f"INSERT INTO pedidos_dedicados ({cols}) VALUES ({','.join('?' * len(campos))})", tuple(campos.values()))
    conn.commit()
    return grupo


def remover(conn: sqlite3.Connection, *, codigo_pedido=None, envio_id=None, service_id=None, por: str = "") -> int:
    cond, params = [], []
    if codigo_pedido:
        cond.append("codigo_pedido = ?"); params.append(normalizar_codigo(codigo_pedido))
    if envio_id is not None:
        cond.append("envio_id = ?"); params.append(envio_id)
    if service_id is not None:
        cond.append("service_id = ?"); params.append(service_id)
    if not cond:
        return 0
    cur = conn.execute(f"UPDATE pedidos_dedicados SET removido_em = ?, removido_por = ? "
                       f"WHERE removido_em IS NULL AND ({' OR '.join(cond)})", (_agora(), por, *params))
    conn.commit()
    return cur.rowcount


def ativos_por_codigo(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM pedidos_dedicados WHERE removido_em IS NULL AND codigo_pedido IS NOT NULL ORDER BY id").fetchall()
    return {r["codigo_pedido"]: dict(r) for r in rows}


def ativo_por_envio(conn: sqlite3.Connection, envio_id: int) -> dict | None:
    r = _ativo(conn, envio_id=envio_id)
    return dict(r) if r else None


def vincular_codigo(conn: sqlite3.Connection, envio_id: int, codigo_pedido: str) -> None:
    conn.execute("UPDATE pedidos_dedicados SET codigo_pedido = ? WHERE envio_id = ? AND removido_em IS NULL AND codigo_pedido IS NULL",
                 (normalizar_codigo(codigo_pedido), envio_id))
    conn.commit()


def filtrar_dedicados(servicos: list[dict], ativos: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Separa servicos Vuupt: (fora da marca, dedicados). Servico com mais
    de um codigo sai inteiro se qualquer um for dedicado (conservador)."""
    restantes, dedicados = [], []
    for s in servicos:
        if any(c in ativos for c in codigos_do_servico(s)):
            dedicados.append(s)
        else:
            restantes.append(s)
    return restantes, dedicados


def quinzena_anterior(data_ref: date) -> tuple[date, date]:
    """Dia 16 -> 1..15 do mes. Dia 1 -> 16..ultimo do mes anterior. Outro
    dia -> a ultima quinzena fechada antes de data_ref."""
    if data_ref.day >= 16:
        return date(data_ref.year, data_ref.month, 1), date(data_ref.year, data_ref.month, 15)
    ultimo_mes_anterior = date(data_ref.year, data_ref.month, 1) - timedelta(days=1)
    return date(ultimo_mes_anterior.year, ultimo_mes_anterior.month, 16), ultimo_mes_anterior


def listar_quinzena(conn: sqlite3.Connection, data_ref: date) -> tuple[date, date, list[dict]]:
    ini, fim = quinzena_anterior(data_ref)
    rows = conn.execute("SELECT * FROM pedidos_dedicados WHERE removido_em IS NULL AND marcado_em >= ? AND marcado_em < ? "
                        "ORDER BY remetente_nome, marcado_em, id",
                        (f"{ini.isoformat()} 00:00:00", f"{(fim + timedelta(days=1)).isoformat()} 00:00:00")).fetchall()
    return ini, fim, [dict(r) for r in rows]
```

- [ ] **Step 4: Rodar os testes**

Run: `py -3.11 -m unittest test_pedidos_dedicados -v`
Expected: todos PASS. Se `test_outro_dia_usa_quinzena_fechada_anterior` falhar no caso do dia 10, confira `quinzena_anterior`: dia < 16 cai no mês anterior (16..fim) — correto para o dia 1 e para qualquer dia até 15.

- [ ] **Step 5: py_compile e preparar**

Run: `py -3.11 -m py_compile pedidos_dedicados.py test_pedidos_dedicados.py`
`git add pedidos_dedicados.py test_pedidos_dedicados.py`
Commit (só com o ok do Hugo): `Dedicados: modulo pedidos_dedicados com divisao de valor e quinzena`

---

### Task 2: Status `AGUARDANDO_LIBERACAO` e checagem de área em `confirmar_envios`

**Files:**
- Modify: `portal_cliente/envio_pedidos.py` (constantes l.72-86; `conectar` l.198-204; `confirmar_envios` l.1080-1198; `_linha` l.1230-1233; `aplicar_acao` l.1295-1308)
- Test: `portal_cliente/test_bloqueio_area.py`

**Interfaces:**
- Produces:
  - `STATUS_AGUARDANDO_LIBERACAO = "AGUARDANDO_LIBERACAO"`; `ROTULOS_STATUS[...] = "Aguardando liberação"`; `STATUS_ABERTOS` inclui o novo.
  - `MOTIVOS_BLOQUEIO = {"fora_sp": "fora do estado de SP", "sp_nao_atendido": "fora da área atendida em SP"}`
  - `classificar_area_envio(nfe: dict, config: dict | None) -> str | None` (None = atendida)
  - `confirmar_envios(...)` devolve os mesmos dicts, agora com `"status"` e `"bloqueio_motivo"` em cada item.
  - colunas novas `portal_envios.bloqueio_motivo TEXT`, `portal_envios.bloqueio_chamado_id INTEGER`.
- Consumes: `roteirizacao.notificar_area_nao_atendida.identificar_area_nao_atendida(servicos, api_key)`.

- [ ] **Step 1: Escrever os testes**

```python
# portal_cliente/test_bloqueio_area.py
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest portal_cliente.test_bloqueio_area"""
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

import envio_pedidos as ep  # noqa: E402

XML = """<?xml version="1.0"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{chave}">
<ide><nNF>{nf}</nNF><serie>1</serie><dhEmi>2026-09-23T10:00:00-03:00</dhEmi></ide>
<emit><CNPJ>68146976000100</CNPJ><xNome>ACME</xNome></emit>
<dest><CNPJ>09014480000467</CNPJ><xNome>Cliente X</xNome><enderDest><xLgr>Rua A</xLgr><nro>1</nro><xBairro>Centro</xBairro>
<xMun>{cidade}</xMun><UF>{uf}</UF><CEP>01001000</CEP><fone>11999999999</fone></enderDest></dest>
<det nItem="1"><prod><cProd>SKU1</cProd><xProd>Produto</xProd><qCom>1</qCom></prod></det>
<total><ICMSTot><vNF>10.00</vNF></ICMSTot></total><transp><vol><qVol>1</qVol><pesoB>1.0</pesoB></vol></transp>
</infNFe></NFe></nfeProc>"""


class ClassificarAreaEnvio(unittest.TestCase):
    def _nfe(self, cidade="Sao Paulo", uf="SP"):
        return {"destinatario_endereco": "Rua A, 1", "destinatario_municipio": cidade, "destinatario_uf": uf,
                "destinatario_cep": "01001000"}

    def test_monta_servico_sintetico_e_devolve_tipo(self):
        with mock.patch.object(ep, "_identificar_area", return_value=[({"id": 0}, "fora_sp")]) as ident:
            self.assertEqual(ep.classificar_area_envio(self._nfe("Curitiba", "PR"), {"google_maps": {"api_key": "k"}}), "fora_sp")
        servicos, key = ident.call_args[0]
        self.assertEqual(servicos[0]["address"], "Rua A, 1, Curitiba - PR, 01001000")
        self.assertEqual(key, "k")

    def test_atendida(self):
        with mock.patch.object(ep, "_identificar_area", return_value=[]):
            self.assertIsNone(ep.classificar_area_envio(self._nfe(), {}))

    def test_falha_deixa_passar(self):
        with mock.patch.object(ep, "_identificar_area", side_effect=RuntimeError("geocode")):
            with self.assertLogs(ep.logger, level="WARNING"):
                self.assertIsNone(ep.classificar_area_envio(self._nfe(), {}))

    def test_desligado_no_config(self):
        with mock.patch.object(ep, "_identificar_area", return_value=[({"id": 0}, "fora_sp")]):
            self.assertIsNone(ep.classificar_area_envio(self._nfe(), {"portal_cliente": {"envios": {"bloqueio_area_ativo": False}}}))


class ConfirmarComBloqueio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "d.db"),
                         mock.patch.object(ep, "PASTA_XMLS", raiz / "x"),
                         mock.patch.object(ep, "PASTA_TEMP", raiz / "x" / "_temporarios"),
                         mock.patch.object(ep, "_RAIZ", raiz)]
        for p in self._patches:
            p.start()
        (raiz / "x" / "_temporarios").mkdir(parents=True)
        self.conn = ep.conectar()
        self.conn.execute("INSERT INTO portal_clientes_envio (cnpj_embarcador, envio_ativo) VALUES ('68146976000100', 1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _token(self, nf, cidade, uf):
        chave = f"3526096814697600010055001000000{nf:03d}1000000001"[:44]
        conteudo = XML.format(chave=chave, nf=nf, cidade=cidade, uf=uf).encode()
        tk = f"tk{nf}"
        (ep.PASTA_TEMP / f"{tk}.xml").write_bytes(conteudo)
        return tk

    def test_bloqueado_e_normal_no_mesmo_lote(self):
        itens = [{"token": self._token(1, "Curitiba", "PR"), "horario_inicio": "08:00", "horario_fim": "17:00"},
                 {"token": self._token(2, "Sao Paulo", "SP"), "horario_inicio": "08:00", "horario_fim": "17:00"}]
        with mock.patch.object(ep, "classificar_area_envio", side_effect=lambda nfe, cfg: "fora_sp" if nfe["destinatario_uf"] == "PR" else None), \
             mock.patch.object(ep, "validar_skus", return_value={"ok": True, "erros": []}, create=True):
            criados = ep.confirmar_envios(self.conn, "68146976000100", itens, "cliente", {}, "padrao")
        por_nf = {c["numero_nf"]: c for c in criados}
        self.assertEqual(por_nf["1"]["status"], ep.STATUS_AGUARDANDO_LIBERACAO)
        self.assertEqual(por_nf["1"]["bloqueio_motivo"], "fora_sp")
        self.assertEqual(por_nf["2"]["status"], ep.STATUS_NA_FILA)
        rows = {r["numero_nf"]: r for r in self.conn.execute("SELECT numero_nf, status, bloqueio_motivo FROM portal_envios")}
        self.assertEqual(rows["1"]["status"], "AGUARDANDO_LIBERACAO")
        self.assertIsNone(rows["2"]["bloqueio_motivo"])


class LinhaEAcao(unittest.TestCase):
    def _envio(self, status):
        return {"id": 1, "cnpj_embarcador": "1", "status": status, "criado_em": "2026-09-23 10:00:00", "criado_stokki_em": None,
                "emitida_em": None, "destinatario_doc": "", "destinatario_endereco": "", "destinatario_bairro": "",
                "destinatario_municipio": "Curitiba", "destinatario_uf": "PR", "agendamento_data": None, "numero_nf": "1",
                "referencia": None, "data_expedicao": None, "xml_path": "a.xml", "origem": "xml", "agendamento_pendente": 0,
                "bloqueio_motivo": "fora_sp", "bloqueio_chamado_id": 77, "codigo_pedido": None}

    def test_linha_aguardando(self):
        d = ep._linha(self._envio(ep.STATUS_AGUARDANDO_LIBERACAO), {})
        self.assertTrue(d["pode_cancelar"])
        self.assertFalse(d["pode_reagendar"])
        self.assertEqual(d["status_rotulo"], "Aguardando liberação")
        self.assertEqual(d["bloqueio_texto"], "Não atendemos a região de Curitiba/PR.")

    def test_cancelar_aguardando_resolve_na_hora(self):
        import sqlite3
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, atualizado_em TEXT);"
                           "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);"
                           "INSERT INTO portal_envios VALUES (1, '1', 'AGUARDANDO_LIBERACAO', NULL);")
        r = ep.aplicar_acao(conn, self._envio(ep.STATUS_AGUARDANDO_LIBERACAO), "cancelar", {}, "cliente")
        self.assertTrue(r["aplicado"])
        self.assertEqual(conn.execute("SELECT status FROM portal_envios").fetchone()[0], "CANCELADO")


if __name__ == "__main__":
    unittest.main()
```

Observação: `_linha` usa `sqlite3.Row` (`dict(r)`); um `dict` puro também serve porque só faz `dict(r)`. Se `_linha` acessar chave ausente do dict de teste, acrescente a chave no `_envio()` em vez de mudar o `_linha`. O teste `ConfirmarComBloqueio` depende de `ler_nfe`/`validar_item` aceitarem o XML mínimo; se `validar_item` exigir outra coisa (SKU cadastrado, destinatário), mocke `ep.validar_item` com `return_value={"ok": True, "erros": [], "envio_existente": None}` em vez de `validar_skus`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_bloqueio_area`
Expected: `AttributeError: module 'envio_pedidos' has no attribute 'STATUS_AGUARDANDO_LIBERACAO'`

- [ ] **Step 3: Constantes e colunas**

Em `envio_pedidos.py`, após `STATUS_CANCELADO = "CANCELADO"` (l.77):

```python
STATUS_AGUARDANDO_LIBERACAO = "AGUARDANDO_LIBERACAO"  # area nao atendida (Hugo, 23/09): so a equipe libera
STATUS_ABERTOS = (STATUS_NA_FILA, STATUS_ENVIANDO, STATUS_AGUARDANDO_LIBERACAO)
```
(substituindo a linha 78 atual). Em `ROTULOS_STATUS` acrescentar `STATUS_AGUARDANDO_LIBERACAO: "Aguardando liberação",`. Depois de `ROTULOS_SOLICITACAO`:

```python
MOTIVOS_BLOQUEIO = {"fora_sp": "fora do estado de SP", "sp_nao_atendido": "fora da área atendida em SP"}
```

No `conectar()`, no `_garantir_colunas(conn, "portal_envios", {...})` (l.198) acrescentar:
```python
        "bloqueio_motivo": "TEXT",
        "bloqueio_chamado_id": "INTEGER",
```

- [ ] **Step 4: `classificar_area_envio`**

Logo antes de `def confirmar_envios` (l.1080):

```python
def _identificar_area(servicos: list[dict], api_key: str | None):
    """Indirecao pra teste; a regra mora na roteirizacao."""
    sys.path.insert(0, str(_RAIZ / "roteirizacao"))
    from notificar_area_nao_atendida import identificar_area_nao_atendida
    return identificar_area_nao_atendida(servicos, api_key)


def classificar_area_envio(nfe: dict, config: dict | None) -> str | None:
    """Mesma regra da roteirizacao (regiao de dia fixo -> ok; UF != SP ->
    fora_sp; SP a mais de RAIO_GRANDE_SP_KM do centro -> sp_nao_atendido).
    Monta um servico sintetico no formato de endereco da Vuupt. Qualquer
    falha (sem chave, geocodificacao) deixa passar: nunca segura pedido
    por falta de dado."""
    cfg = ((config or {}).get("portal_cliente", {}) or {}).get("envios", {}) or {}
    if not cfg.get("bloqueio_area_ativo", True):
        return None
    endereco = ", ".join(p for p in (nfe.get("destinatario_endereco"), f"{nfe.get('destinatario_municipio', '')} - {nfe.get('destinatario_uf', '')}".strip(" -"),
                                      nfe.get("destinatario_cep")) if p)
    servico = {"id": 0, "address": endereco}
    api_key = ((config or {}).get("google_maps", {}) or {}).get("api_key") or ""
    try:
        for _s, tipo in _identificar_area([servico], api_key):
            return tipo
        return None
    except Exception as e:
        logger.warning(f"classificar_area_envio falhou ({e}); deixando passar NF {nfe.get('numero_nf')}")
        return None
```

Confira se o módulo já define `logger` (grep `logger =`); se não, `logger = logging.getLogger(__name__)` após os imports.

- [ ] **Step 5: Usar em `confirmar_envios`**

Na l.1160 (`agora = _agora()`), antes dela:
```python
        motivo = classificar_area_envio(nfe, config)
```
No dict `campos`, trocar `"status": STATUS_NA_FILA,` por:
```python
            "status": STATUS_AGUARDANDO_LIBERACAO if motivo else STATUS_NA_FILA,
            "bloqueio_motivo": motivo, "bloqueio_chamado_id": None,
```
No `criados.append(...)` (l.1195) acrescentar `"status": campos["status"], "bloqueio_motivo": motivo, "id": envio_id` e também `"destinatario_municipio": nfe["destinatario_municipio"], "destinatario_uf": nfe["destinatario_uf"]` (a Task 3 usa).

- [ ] **Step 6: `_linha` e `aplicar_acao`**

Em `_linha` (l.1216 `d.update({...})`) acrescentar:
```python
        "bloqueio_texto": (f"Não atendemos a região de {d.get('destinatario_municipio') or '?'}/{d.get('destinatario_uf') or '?'}."
                           if d.get("status") == STATUS_AGUARDANDO_LIBERACAO else ""),
        "bloqueio_chamado_id": d.get("bloqueio_chamado_id"),
```
e em `pode_cancelar` incluir `STATUS_AGUARDANDO_LIBERACAO` na tupla. `pode_reagendar` fica como está (não inclui o novo).

Em `aplicar_acao`, l.1300: `if st in (STATUS_NA_FILA, STATUS_ERRO, STATUS_AGUARDANDO_LIBERACAO):`.

`listar_envios` (l.1244): trocar `status IN ('NA_FILA','ENVIANDO')` por `status IN ('NA_FILA','ENVIANDO','AGUARDANDO_LIBERACAO')`.

- [ ] **Step 7: Rodar os testes**

Run: `py -3.11 -m unittest portal_cliente.test_bloqueio_area -v` → PASS.
Run também os que já existem: `py -3.11 -m unittest portal_cliente.test_envio_planilha portal_cliente.test_reconciliar_codigos` → PASS.

- [ ] **Step 8: py_compile e preparar**

`py -3.11 -m py_compile portal_cliente/envio_pedidos.py portal_cliente/test_bloqueio_area.py`
`git add portal_cliente/envio_pedidos.py portal_cliente/test_bloqueio_area.py`
Commit (com ok): `Portal: pedido de area nao atendida fica AGUARDANDO_LIBERACAO em vez de ir pra Stokki`

---

### Task 3: Aviso do bloqueio — chamado + e-mails (`abrir_bloqueio`)

**Files:**
- Create: `portal_cliente/bloqueio_area.py`
- Modify: `portal_cliente/app.py` (rota `api_envios_confirmar` l.736-762)
- Test: `portal_cliente/test_bloqueio_area.py` (acrescentar classe)

**Interfaces:**
- Produces: `bloqueio_area.abrir_bloqueio(conn_envios, cliente: dict, criados: list[dict], config: dict) -> int | None` (chamado_id). `cliente` = `{cnpj, sender_id, nome}`. Só considera itens com `status == AGUARDANDO_LIBERACAO`.
- Produces: `bloqueio_area.forcar_destino(config) -> str` (default `hugo@freshlogbr.com`; `""` no config = envio real).
- Consumes: `chamados.criar_chamado`, `chamados.mensagem_sistema`, `chamados.emails_do_cliente`, `email_utils.enviar_email/envelope_html`.

- [ ] **Step 1: Teste**

Acrescentar em `portal_cliente/test_bloqueio_area.py`:

```python
class AbrirBloqueio(unittest.TestCase):
    def test_um_chamado_para_varias_nfs_e_emails(self):
        import sqlite3
        import bloqueio_area as ba
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, status TEXT, bloqueio_chamado_id INTEGER, atualizado_em TEXT);"
                           "INSERT INTO portal_envios VALUES (1,'AGUARDANDO_LIBERACAO',NULL,NULL),(2,'AGUARDANDO_LIBERACAO',NULL,NULL),(3,'NA_FILA',NULL,NULL);")
        criados = [{"id": 1, "numero_nf": "10", "status": "AGUARDANDO_LIBERACAO", "destinatario_municipio": "Curitiba", "destinatario_uf": "PR", "referencia": None},
                   {"id": 2, "numero_nf": "11", "status": "AGUARDANDO_LIBERACAO", "destinatario_municipio": "Curitiba", "destinatario_uf": "PR", "referencia": None},
                   {"id": 3, "numero_nf": "12", "status": "NA_FILA", "destinatario_municipio": "Sao Paulo", "destinatario_uf": "SP", "referencia": None}]
        cliente = {"cnpj": "68146976000100", "sender_id": 5, "nome": "ACME"}
        config = {"email": {"email_atendimento": "entregas@x"}, "portal_cliente": {"envios": {"forcar_destino": "hugo@x"}}}
        fake_ch = mock.MagicMock()
        fake_ch.criar_chamado.return_value = {"id": 77}
        fake_ch.emails_do_cliente.return_value = ["cliente@x"]
        fake_ch.ORIGEM_SISTEMA, fake_ch.STATUS_AGUARDANDO_FL = "sistema", "AGUARDANDO_FL"
        with mock.patch.object(ba, "_chamados", return_value=fake_ch), mock.patch.object(ba, "enviar_email", return_value=True) as env:
            chamado_id = ba.abrir_bloqueio(conn, cliente, criados, config)
        self.assertEqual(chamado_id, 77)
        fake_ch.criar_chamado.assert_called_once()
        kw = fake_ch.criar_chamado.call_args
        self.assertEqual(kw.kwargs["area"], "envios")
        self.assertEqual(kw.kwargs["pedido_ref"], "NF 10, 11")
        texto = fake_ch.mensagem_sistema.call_args[0][2]
        self.assertIn("Curitiba/PR", texto); self.assertIn("10", texto); self.assertNotIn("12", texto)
        self.assertEqual([r[0] for r in conn.execute("SELECT bloqueio_chamado_id FROM portal_envios ORDER BY id")], [77, 77, None])
        destinos = [c.args[0] for c in env.call_args_list]
        self.assertEqual(destinos, [["hugo@x"], ["hugo@x"]])  # cliente e interno, ambos redirecionados

    def test_sem_bloqueados_nao_faz_nada(self):
        import bloqueio_area as ba
        with mock.patch.object(ba, "_chamados") as ch:
            self.assertIsNone(ba.abrir_bloqueio(None, {}, [{"id": 1, "status": "NA_FILA"}], {}))
        ch.assert_not_called()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_bloqueio_area.AbrirBloqueio` → `ModuleNotFoundError: bloqueio_area`

- [ ] **Step 3: Implementar `portal_cliente/bloqueio_area.py`**

```python
# -*- coding: utf-8 -*-
"""
Aviso do bloqueio por area nao atendida (Hugo, 23/09/2026): 1 chamado por
confirmacao do cliente (nao 1 por NF), mensagem de sistema no chat, e-mail
pro cliente e e-mail interno. Tudo redirecionado por
portal_cliente.envios.forcar_destino (default hugo@) ate o Hugo liberar.
"""
import html
import logging
import sys
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from email_utils import enviar_email, envelope_html  # noqa: E402

logger = logging.getLogger(__name__)
EMAIL_TESTE = "hugo@freshlogbr.com"
URL_PORTAL = "https://app.freshhub.com.br/cliente"
STATUS_BLOQUEADO = "AGUARDANDO_LIBERACAO"


def _chamados():
    import chamados as ch
    return ch


def _secao(config: dict) -> dict:
    return ((config or {}).get("portal_cliente", {}) or {}).get("envios", {}) or {}


def forcar_destino(config: dict) -> str:
    secao = _secao(config)
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def _rotulo(c: dict) -> str:
    return c.get("numero_nf") or c.get("referencia") or f"#{c['id']}"


def texto_bloqueio(bloqueados: list[dict]) -> str:
    destinos = sorted({f"{c.get('destinatario_municipio') or '?'}/{c.get('destinatario_uf') or '?'}" for c in bloqueados})
    nfs = ", ".join(_rotulo(c) for c in bloqueados)
    plural = len(bloqueados) > 1
    return (f"Não atendemos a região de {', '.join(destinos)}. "
            f"{'As NFs' if plural else 'A NF'} {nfs} {'estão' if plural else 'está'} aguardando liberação e não "
            f"{'foram enviadas' if plural else 'foi enviada'} à Stokki.\n"
            f"Você pode solicitar uma cotação de envio dedicado, falar com a equipe Fresh Log ou cancelar o envio, "
            f"pela aba Envios do portal.")


def abrir_bloqueio(conn, cliente: dict, criados: list[dict], config: dict) -> int | None:
    bloqueados = [c for c in criados if c.get("status") == STATUS_BLOQUEADO]
    if not bloqueados:
        return None
    ch = _chamados()
    nfs = ", ".join(_rotulo(c) for c in bloqueados)
    texto = texto_bloqueio(bloqueados)
    chamado_id = None
    try:
        conn_ch = ch.conectar()
        try:
            chamado = ch.criar_chamado(conn_ch, cliente, ch.ORIGEM_SISTEMA, ch.STATUS_AGUARDANDO_FL,
                                       assunto="Envio para região não atendida", area="envios", pedido_ref=f"NF {nfs}")
            ch.mensagem_sistema(conn_ch, chamado, texto)
            chamado_id = chamado["id"]
            emails_cliente = ch.emails_do_cliente(conn_ch, cliente.get("cnpj"))
        finally:
            conn_ch.close()
    except Exception as e:
        logger.exception(f"bloqueio: nao abriu chamado ({e})")
        return None
    conn.execute(f"UPDATE portal_envios SET bloqueio_chamado_id = ? WHERE id IN ({','.join('?' * len(bloqueados))})",
                 (chamado_id, *[c["id"] for c in bloqueados]))
    conn.commit()

    email_cfg = (config or {}).get("email", {}) or {}
    forcar = forcar_destino(config)
    corpo_html = "<p>" + html.escape(texto).replace("\n", "</p><p>") + "</p>"
    aviso = (f"<p style='background:#FFF4D6;padding:8px;border-radius:6px'>Redirecionado (piloto). Destino real: "
             f"{html.escape(', '.join(emails_cliente) or '(cliente sem e-mail)')}</p>" if forcar else "")
    destinos_cliente = [forcar] if forcar else emails_cliente
    if destinos_cliente:
        enviar_email(destinos_cliente, f"[Fresh Log] Envio aguardando liberação · NF {nfs}",
                     envelope_html(f"<p>Olá, <b>{html.escape(cliente.get('nome') or '')}</b>.</p>{corpo_html}"
                                   f"<p><a href='{URL_PORTAL}'>Abrir o portal</a></p>{aviso}",
                                   rodape="Fresh Log · Portal do cliente", cor_acento="#EF4444"), email_cfg)
    interno = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
    if interno:
        enviar_email([forcar] if forcar else [interno], f"[Portal] Bloqueio de área · NF {nfs} · {cliente.get('nome')}",
                     envelope_html(f"<p><b>{html.escape(cliente.get('nome') or '')}</b> enviou pedido(s) para região não atendida. "
                                   f"Chamado #{chamado_id} no atendimento.</p>{corpo_html}",
                                   rodape="Fresh Log · Portal do cliente · bloqueio", cor_acento="#EF4444"), email_cfg)
    return chamado_id
```

`chamados.emails_do_cliente(conn, cnpj)` existe em `chamados.py:1090`; confira a assinatura antes de usar.

- [ ] **Step 4: Chamar na rota de confirmar (`app.py` l.750)**

Após `criados = envios.confirmar_envios(...)` e ainda dentro do `try` (antes do `finally` fechar a conexão):
```python
        if any(c.get("status") == envios.STATUS_AGUARDANDO_LIBERACAO for c in criados):
            import bloqueio_area
            emp = _empresa_envio()
            bloqueio_area.abrir_bloqueio(conn, {"cnpj": emp["cnpj"], "sender_id": emp.get("sender_id"), "nome": emp.get("nome")},
                                         criados, _CONFIG)
```
E na resposta (l.760), acrescentar `"bloqueados": [c for c in criados if c.get("status") == envios.STATUS_AGUARDANDO_LIBERACAO]`; quando houver bloqueados, a `mensagem` vira: `f"{n} pedido(s) recebido(s). {len(bloq)} ficou/ficaram aguardando liberação por região não atendida -- veja a lista abaixo."`.

- [ ] **Step 5: Testes e compile**

`py -3.11 -m unittest portal_cliente.test_bloqueio_area -v` → PASS.
`py -3.11 -m py_compile portal_cliente/bloqueio_area.py portal_cliente/app.py`
`git add portal_cliente/bloqueio_area.py portal_cliente/test_bloqueio_area.py` e **só o hunk** de `app.py` da rota confirmar (`git add -p portal_cliente/app.py`; o arquivo tem edições de outra sessão).
Commit (com ok): `Portal: bloqueio de area abre 1 chamado no chat e avisa cliente e equipe por e-mail`

---

### Task 4: Tela do portal — linha bloqueada, botões, chip "Envio dedicado", widget

**Files:**
- Modify: `portal_cliente/templates/_envios.html` (CSS l.19-26; `filtrados` l.233-242; `renderTabela` l.258-279; handler l.483)
- Modify: `portal_cliente/static/atendimento.js` (expor `window.atdAbrirChamado`)
- Modify: `portal_cliente/envio_pedidos.py` (`listar_envios` l.1240: chip dedicado)

**Interfaces:**
- Produces: `window.atdAbrirChamado(chamadoId: number, textoPronto?: string)` — abre o widget no chamado e, se `textoPronto`, envia como mensagem do cliente.
- Produces: cada item de `listar_envios` traz `"dedicado": {"valor": float} | null`.
- Consumes: `bloqueio_texto`, `bloqueio_chamado_id`, `pode_cancelar` (Task 2); `pedidos_dedicados.ativo_por_envio` (Task 1).

- [ ] **Step 1: `listar_envios` com dedicado**

Em `envio_pedidos.listar_envios`, após montar `sol`:
```python
    import pedidos_dedicados
    ativos = {r["envio_id"]: dict(r) for r in conn.execute(
        "SELECT envio_id, valor FROM pedidos_dedicados WHERE removido_em IS NULL AND envio_id IS NOT NULL")} \
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pedidos_dedicados'").fetchone() else {}
    linhas = [_linha(r, sol) for r in rows]
    for l in linhas:
        l["dedicado"] = {"valor": ativos[l["id"]]["valor"]} if l["id"] in ativos else None
    return linhas
```
(o `import` fica só pra garantir o módulo no path; a leitura é SQL direto na mesma conexão porque a tabela mora no mesmo `dados.db`. A checagem em `sqlite_master` evita quebrar antes da Task 1 rodar em produção.)

Teste rápido em `test_bloqueio_area.py` (classe `LinhaEAcao`):
```python
    def test_listar_envios_com_dedicado(self):
        import sqlite3
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        conn.executescript("""CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT,
            criado_stokki_em, emitida_em, destinatario_doc, destinatario_endereco, destinatario_bairro, destinatario_municipio,
            destinatario_uf, agendamento_data, numero_nf, referencia, data_expedicao, xml_path, origem, agendamento_pendente,
            bloqueio_motivo, bloqueio_chamado_id, codigo_pedido);
            CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);
            CREATE TABLE pedidos_dedicados (id INTEGER PRIMARY KEY, envio_id INTEGER, valor REAL, removido_em TEXT);
            INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, xml_path, origem, agendamento_pendente)
              VALUES (1, '1', 'NA_FILA', '2099-01-01 00:00:00', '1', 'a.xml', 'xml', 0), (2, '1', 'NA_FILA', '2099-01-01 00:00:00', '2', 'b.xml', 'xml', 0);
            INSERT INTO pedidos_dedicados VALUES (1, 1, 33.34, NULL), (2, 2, 5, '2026-01-01');""")
        lista = {l["id"]: l for l in ep.listar_envios(conn, "1")}
        self.assertEqual(lista[1]["dedicado"], {"valor": 33.34})
        self.assertIsNone(lista[2]["dedicado"])
```
Run: `py -3.11 -m unittest portal_cliente.test_bloqueio_area` → PASS.

- [ ] **Step 2: Expor `atdAbrirChamado` no widget**

Em `portal_cliente/static/atendimento.js`, antes do fechamento do IIFE (`})();` no fim do arquivo), acrescentar:
```js
  // Chamado por outra tela (aba Envios, bloqueio de área — Hugo 23/09):
  // abre o widget num chamado e, se vier texto, já manda como mensagem.
  window.atdAbrirChamado = async function (chamadoId, textoPronto) {
    try {
      await abrirChamado(Number(chamadoId));
      abrir(true);
      if (textoPronto) await enviar(textoPronto);
    } catch (e) { alert(e.message || 'Não foi possível abrir a conversa.'); }
  };
```
`abrirChamado(id)` (l.44), `abrir(sim)` (l.259) e `enviar(texto, chip)` (l.207) já existem no IIFE. Confirmar que `enviar` funciona sem `chip` (segundo parâmetro opcional) lendo l.207-230; se `enviar` lê o textarea em vez do argumento, escrever no `$('atd-texto').value` antes e chamar sem argumentos. Subir o `?v=1` de `_widget_atendimento.html:126` para `?v=2`.

- [ ] **Step 3: `_envios.html`**

CSS (após l.24):
```css
  .b-AGUARDANDO_LIBERACAO { background: var(--erro-bg); color: var(--st-falha); }
  .bloq-txt { font-size: 11px; color: var(--st-falha); margin-top: 3px; max-width: 260px; }
  .chip.dedicado { background: var(--rota-bg); color: var(--st-rota); }
  .acoes-linha button.cotacao { border-color: var(--st-rota); color: var(--st-rota); font-weight: 700; }
```

`filtrados()` l.235: `if (aba === 'fila') lista = lista.filter(x => ['NA_FILA','ENVIANDO','AGUARDANDO_LIBERACAO'].includes(x.status));` e na l.238 (`pendencias`) acrescentar `|| x.status === 'AGUARDANDO_LIBERACAO'`.

`renderTabela()` l.265, depois do trecho do erro, acrescentar:
```js
${x.status === 'AGUARDANDO_LIBERACAO' ? `<div class="bloq-txt">${esc(x.bloqueio_texto)}</div>` : ''}${x.dedicado ? `<div><span class="chip dedicado" title="Transporte dedicado combinado com a Fresh Log">Envio dedicado</span></div>` : ''}
```
Na célula de ações (l.268), antes do botão de reagendar:
```js
        ${x.status === 'AGUARDANDO_LIBERACAO' && x.bloqueio_chamado_id ? `<button type="button" class="cotacao" data-acao="cotacao" data-id="${x.id}" data-chamado="${x.bloqueio_chamado_id}">Solicitar cotação de envio dedicado</button><button type="button" data-acao="chat" data-id="${x.id}" data-chamado="${x.bloqueio_chamado_id}">Falar com a equipe Fresh Log</button>` : ''}
```
(`Cancelar` já aparece via `pode_cancelar`.)

No handler l.483, trocar por:
```js
    secao.addEventListener('click', e => {
      const b = e.target.closest('[data-acao]'); if (!b) return;
      if (b.dataset.acao === 'chat' || b.dataset.acao === 'cotacao') {
        const x = dados.envios.find(v => String(v.id) === b.dataset.id) || {};
        const texto = b.dataset.acao === 'cotacao'
          ? `Solicito cotação de envio dedicado para NF ${x.numero_nf || x.referencia || ''} → ${x.destinatario_municipio || ''}/${x.destinatario_uf || ''}.`
          : '';
        if (window.atdAbrirChamado) window.atdAbrirChamado(b.dataset.chamado, texto);
        else location.href = `${BASE}/?chamado=${b.dataset.chamado}`;
        return;
      }
      abrirAcao(b.dataset.acao, b.dataset.id);
    });
```
Confira que `dados.envios` traz `destinatario_municipio`/`destinatario_uf` (vem de `dict(row)` em `_linha`; sim).

Na mensagem de confirmação (`/api/envios/confirmar` no JS): se `j.bloqueados?.length`, mostrar com `mensagem(j.mensagem, 'info')` em vez de `'ok'`. Localizar o `fetch` de `confirmar` no template (grep `envios/confirmar`).

- [ ] **Step 4: Prova manual**

Rodar o portal em porta alternativa: de `portal_cliente/`, `py -3.11 -c "import app; app.app.run(host='127.0.0.1', port=8098)"`. Entrar com o cliente de teste (CNPJ `00.000.000/0001-91`, PIN `123456`). Enviar um XML com `<UF>PR</UF>` (adaptar um XML real de `dados/portal_envios/` trocando `xMun`/`UF`). Verificar: linha vermelha "Aguardando liberação", texto "Não atendemos a região de …", os 3 botões; clicar "Solicitar cotação" abre o widget no chamado com a mensagem enviada; "Cancelar envio" vira Cancelado. Como o worker não roda local, a linha fica parada — esperado.

- [ ] **Step 5: Compile e preparar**

`py -3.11 -m py_compile portal_cliente/envio_pedidos.py`
`git add portal_cliente/templates/_envios.html portal_cliente/static/atendimento.js portal_cliente/templates/_widget_atendimento.html portal_cliente/envio_pedidos.py portal_cliente/test_bloqueio_area.py` (conferir com `git diff` que `_envios.html` não carrega hunks alheios; se carregar, `git add -p`).
Commit (com ok): `Portal: linha "Aguardando liberacao" com cotacao dedicada, chat e cancelar; chip Envio dedicado`

---

### Task 5: `/atendimento` — quadro "NFs aguardando liberação" e rota de liberar

**Files:**
- Modify: `painel_agentes/atendimento_chamados.py` (dentro de `registrar`, após `api_atd_resolver` l.307)
- Modify: `painel_agentes/templates/atendimento.html` (`renderConversa` l.249-273; CSS; handler)
- Test: `painel_agentes/test_atendimento_liberar.py`

**Interfaces:**
- Produces: `atendimento_chamados.liberar_envios(conn_envios, conn_dedicados, chamado: dict, ids: list[int], modo: str, valor, por: str, mensagem_fn) -> dict` (função pura, testável) e as rotas:
  - `GET /api/atendimento/chamados/<id>/envios-bloqueados` → `{"envios": [{id, numero_nf, referencia, destinatario_nome, destinatario_municipio, destinatario_uf, status, status_rotulo, dedicado_valor}]}`
  - `POST /api/atendimento/chamados/<id>/liberar` `{ids: [int], modo: "dedicado"|"simples"|"cancelar", valor?: number|string}` → payload do chamado (`_payload`) + `{"liberados": [ids], "valores": {id: valor}}`.
- Consumes: `envio_pedidos.STATUS_*`, `pedidos_dedicados.marcar`, `chamados.mensagem_sistema`.

- [ ] **Step 1: Testes**

```python
# painel_agentes/test_atendimento_liberar.py
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest painel_agentes.test_atendimento_liberar"""
import sqlite3
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _RAIZ / "portal_cliente", _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pedidos_dedicados as pd  # noqa: E402
from atendimento_chamados import liberar_envios, valor_brl  # noqa: E402


def _envios():
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, numero_nf TEXT, referencia TEXT, destinatario_nome TEXT,
            destinatario_municipio TEXT, destinatario_uf TEXT, status TEXT, bloqueio_chamado_id INTEGER, atualizado_em TEXT, codigo_pedido TEXT);
        INSERT INTO portal_envios VALUES (1,'1','10',NULL,'D1','Curitiba','PR','AGUARDANDO_LIBERACAO',77,NULL,NULL),
                                         (2,'1','11',NULL,'D2','Curitiba','PR','AGUARDANDO_LIBERACAO',77,NULL,NULL),
                                         (3,'1','12',NULL,'D3','Curitiba','PR','CANCELADO',77,NULL,NULL);""")
    return conn


class ValorBrl(unittest.TestCase):
    def test_formatos(self):
        self.assertEqual(valor_brl("150,50"), 150.5)
        self.assertEqual(valor_brl("1.234,56"), 1234.56)
        self.assertEqual(valor_brl(100), 100.0)
        with self.assertRaises(ValueError):
            valor_brl("abc")
        with self.assertRaises(ValueError):
            valor_brl(0)


class Liberar(unittest.TestCase):
    def setUp(self):
        self.env = _envios()
        self.ded = pd.conectar(":memory:")
        self.msgs = []
        self.chamado = {"id": 77, "sender_id": 5, "nome_cliente": "ACME"}

    def _msg(self, texto):
        self.msgs.append(texto)

    def test_dedicado_divide_e_libera(self):
        r = liberar_envios(self.env, self.ded, self.chamado, [1, 2], "dedicado", "100", "hugo", self._msg)
        self.assertEqual(r["liberados"], [1, 2])
        self.assertEqual(r["valores"], {1: 33.34, 2: 33.33})  # sobra no primeiro
        self.assertEqual([x[0] for x in self.env.execute("SELECT status FROM portal_envios WHERE id IN (1,2)")], ["NA_FILA", "NA_FILA"])
        self.assertEqual(self.ded.execute("SELECT COUNT(*) FROM pedidos_dedicados").fetchone()[0], 2)
        self.assertEqual(self.ded.execute("SELECT numero_nf, sender_id, remetente_nome FROM pedidos_dedicados WHERE envio_id=1").fetchone()[:], ("10", 5, "ACME"))
        self.assertIn("R$ 33,34", self.msgs[0]); self.assertIn("NF 10", self.msgs[0])

    def test_simples(self):
        r = liberar_envios(self.env, self.ded, self.chamado, [1], "simples", None, "hugo", self._msg)
        self.assertEqual(r["liberados"], [1])
        self.assertEqual(self.ded.execute("SELECT COUNT(*) FROM pedidos_dedicados").fetchone()[0], 0)
        self.assertEqual(self.env.execute("SELECT status FROM portal_envios WHERE id=1").fetchone()[0], "NA_FILA")

    def test_cancelar(self):
        liberar_envios(self.env, self.ded, self.chamado, [2], "cancelar", None, "hugo", self._msg)
        self.assertEqual(self.env.execute("SELECT status FROM portal_envios WHERE id=2").fetchone()[0], "CANCELADO")
        self.assertIn("cancelada", self.msgs[0])

    def test_id_fora_do_chamado_ou_ja_tratado(self):
        with self.assertRaises(ValueError):
            liberar_envios(self.env, self.ded, self.chamado, [3], "simples", None, "hugo", self._msg)
        with self.assertRaises(ValueError):
            liberar_envios(self.env, self.ded, {"id": 99}, [1], "simples", None, "hugo", self._msg)

    def test_dedicado_exige_valor(self):
        with self.assertRaises(ValueError):
            liberar_envios(self.env, self.ded, self.chamado, [1], "dedicado", "", "hugo", self._msg)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

`py -3.11 -m unittest painel_agentes.test_atendimento_liberar` → `ImportError: cannot import name 'liberar_envios'`

- [ ] **Step 3: Funções puras no módulo (fora de `registrar`, nível de módulo)**

Em `atendimento_chamados.py`, após os imports:
```python
import envio_pedidos as ep  # portal_cliente/ (ja esta no sys.path pelo import de chamados)
import pedidos_dedicados

STATUS_BLOQUEADO = ep.STATUS_AGUARDANDO_LIBERACAO


def valor_brl(v) -> float:
    """'1.234,56' / '150,50' / 100 -> float > 0."""
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v or "").strip().replace("R$", "").replace(" ", "")
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            f = float(s)
        except ValueError:
            raise ValueError("Valor inválido.")
    if f <= 0:
        raise ValueError("Informe um valor maior que zero.")
    return round(f, 2)


def _fmt_brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def envios_do_chamado(conn_envios, chamado_id: int) -> list[dict]:
    rows = conn_envios.execute("SELECT id, numero_nf, referencia, destinatario_nome, destinatario_municipio, destinatario_uf, status, codigo_pedido "
                               "FROM portal_envios WHERE bloqueio_chamado_id = ? ORDER BY id", (chamado_id,)).fetchall()
    return [{**dict(r), "status_rotulo": ep.ROTULOS_STATUS.get(r["status"], r["status"])} for r in rows]


def liberar_envios(conn_envios, conn_dedicados, chamado: dict, ids: list[int], modo: str, valor, por: str, mensagem_fn) -> dict:
    """Aplica a decisao da equipe nas NFs bloqueadas do chamado. Levanta
    ValueError com mensagem pro atendente. mensagem_fn(texto) grava a
    mensagem de sistema no chat."""
    if modo not in ("dedicado", "simples", "cancelar"):
        raise ValueError("Modo inválido.")
    ids = [int(i) for i in ids or []]
    if not ids:
        raise ValueError("Marque ao menos uma NF.")
    todos = {e["id"]: e for e in envios_do_chamado(conn_envios, chamado["id"])}
    alvo = []
    for i in ids:
        e = todos.get(i)
        if not e or e["status"] != STATUS_BLOQUEADO:
            raise ValueError(f"NF {e['numero_nf'] if e else i} não está mais aguardando liberação.")
        alvo.append(e)
    valores = {}
    if modo == "dedicado":
        total = valor_brl(valor)
        grupo = pedidos_dedicados.marcar(conn_dedicados, [{
            "envio_id": e["id"], "codigo_pedido": e.get("codigo_pedido"), "numero_nf": e.get("numero_nf") or e.get("referencia"),
            "sender_id": chamado.get("sender_id"), "remetente_nome": chamado.get("nome_cliente")} for e in alvo], total, por)
        for r in conn_dedicados.execute("SELECT envio_id, valor FROM pedidos_dedicados WHERE grupo_id = ?", (grupo,)):
            valores[r["envio_id"]] = r["valor"]
    novo = ep.STATUS_CANCELADO if modo == "cancelar" else ep.STATUS_NA_FILA
    agora = ep._agora()
    for e in alvo:
        conn_envios.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?", (novo, agora, e["id"]))
    conn_envios.commit()
    for e in alvo:
        rot = e.get("numero_nf") or e.get("referencia") or e["id"]
        if modo == "dedicado":
            mensagem_fn(f"NF {rot} liberada como envio dedicado ({_fmt_brl(valores[e['id']])}) por {por}. O pedido segue para a Stokki.")
        elif modo == "simples":
            mensagem_fn(f"NF {rot} liberada por {por}. O pedido segue para a Stokki.")
        else:
            mensagem_fn(f"NF {rot} cancelada por {por}.")
    return {"liberados": [e["id"] for e in alvo], "valores": valores}
```

`ep._agora()` existe (`envio_pedidos.py:217`). Se `import envio_pedidos` falhar por path, acrescentar antes: `sys.path.insert(0, str(Path(__file__).parent.parent / "portal_cliente"))` seguindo o padrão já usado para `chamados`.

- [ ] **Step 4: Rotas (dentro de `registrar`, após `api_atd_resolver`)**

```python
    @app.route("/api/atendimento/chamados/<int:chamado_id>/envios-bloqueados")
    @requer_auth(niveis=NIVEIS)
    def api_atd_envios_bloqueados(chamado_id):
        conn = ep.conectar()
        try:
            envs = envios_do_chamado(conn, chamado_id)
            ded = {r["envio_id"]: r["valor"] for r in conn.execute(
                "SELECT envio_id, valor FROM pedidos_dedicados WHERE removido_em IS NULL AND envio_id IN (%s)" % (",".join("?" * len(envs)) or "NULL"),
                [e["id"] for e in envs])} if envs else {}
            for e in envs:
                e["dedicado_valor"] = ded.get(e["id"])
            return jsonify({"envios": envs})
        finally:
            conn.close()

    @app.route("/api/atendimento/chamados/<int:chamado_id>/liberar", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_atd_liberar(chamado_id):
        corpo = request.get_json(silent=True) or {}

        def fn(conn, c):
            conn_env = ep.conectar()
            conn_ded = pedidos_dedicados.conectar()
            try:
                try:
                    r = liberar_envios(conn_env, conn_ded, c, corpo.get("ids"), corpo.get("modo"), corpo.get("valor"), _nome(),
                                       lambda texto: ch.mensagem_sistema(conn, c, texto))
                except ValueError as e:
                    raise ch.ErroChamado(str(e))
            finally:
                conn_ded.close()
                conn_env.close()
            logger.info("chamado %s: liberar %s ids=%s por %s", c["id"], corpo.get("modo"), r["liberados"], _nome())
            return ch.buscar_chamado(conn, c["id"]), r
        return _acao(chamado_id, fn)
```
Como `pedidos_dedicados` grava no mesmo `dados.db` que `portal_envios`, a leitura em `envios-bloqueados` funciona na conexão de `ep`. Garanta que a tabela existe (chamar `pedidos_dedicados.conectar().close()` uma vez no início de `registrar`).

- [ ] **Step 5: Testes**

`py -3.11 -m unittest painel_agentes.test_atendimento_liberar -v` → PASS.

- [ ] **Step 6: Front `atendimento.html`**

CSS (junto do `.conv-cab`):
```css
  .quadro-bloq { margin: 8px 16px 0; padding: 10px 12px; border: 1px solid var(--st-falha); border-radius: 8px; background: var(--erro-bg); font-size: 13px; }
  .quadro-bloq h3 { margin: 0 0 6px; font-size: 13px; color: var(--st-falha); }
  .quadro-bloq label.nf { display: flex; gap: 8px; align-items: center; padding: 2px 0; }
  .quadro-bloq label.nf.tratada { opacity: .55; text-decoration: line-through; }
  .quadro-bloq .acoes-bloq { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
  .quadro-bloq input.valor { width: 120px; }
```
HTML: dentro de `#conversa`, entre `#conv-cab` e `#conv-corpo`: `<div class="quadro-bloq oculto" id="quadro-bloq"></div>`.

JS: nova função e chamada ao fim de `abrir(id)` (l.216-223) e após cada `acao`:
```js
  async function renderBloqueio() {
    const q = $('quadro-bloq'); const c = st.chamado;
    if (!c || c.area !== 'envios') { q.classList.add('oculto'); return; }
    let envs = [];
    try { envs = (await api(`/api/atendimento/chamados/${c.id}/envios-bloqueados`)).envios; } catch (e) { q.classList.add('oculto'); return; }
    if (!envs.length) { q.classList.add('oculto'); return; }
    q.classList.remove('oculto');
    const abertos = envs.filter(e => e.status === 'AGUARDANDO_LIBERACAO');
    q.innerHTML = `<h3>NFs aguardando liberação (região não atendida)</h3>` +
      envs.map(e => `<label class="nf ${e.status === 'AGUARDANDO_LIBERACAO' ? '' : 'tratada'}">
        ${e.status === 'AGUARDANDO_LIBERACAO' ? `<input type="checkbox" value="${e.id}" checked>` : '<span style="width:13px"></span>'}
        <b class="mono">NF ${esc(e.numero_nf || e.referencia || e.id)}</b> · ${esc(e.destinatario_nome)} · ${esc(e.destinatario_municipio)}/${esc(e.destinatario_uf)}
        <span class="pill">${esc(e.status_rotulo)}${e.dedicado_valor != null ? ' · dedicado R$ ' + e.dedicado_valor.toFixed(2).replace('.', ',') : ''}</span></label>`).join('') +
      (abertos.length ? `<div class="acoes-bloq">
        <input class="valor" id="bloq-valor" placeholder="Valor da cotação (R$)" inputmode="decimal">
        <button type="button" class="botao-p" data-liberar="dedicado">Liberar como dedicado</button>
        <button type="button" class="botao-linha" data-liberar="simples">Liberar sem dedicado</button>
        <button type="button" class="botao-linha" data-liberar="cancelar">Cancelar envio</button>
      </div>` : '');
  }
  $('quadro-bloq').addEventListener('click', async e => {
    const b = e.target.closest('[data-liberar]'); if (!b || !st.chamado) return;
    const ids = Array.from($('quadro-bloq').querySelectorAll('input[type=checkbox]:checked')).map(i => Number(i.value));
    const modo = b.dataset.liberar, valor = ($('bloq-valor') || {}).value || '';
    if (!ids.length) { alert('Marque ao menos uma NF.'); return; }
    if (modo === 'dedicado' && !valor) { alert('Informe o valor da cotação.'); return; }
    if (modo === 'cancelar' && !confirm(`Cancelar ${ids.length} NF(s)? O cliente vê a mensagem no chat.`)) return;
    b.disabled = true;
    try {
      const j = await api(`/api/atendimento/chamados/${st.chamado.id}/liberar`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids, modo, valor }) });
      st.chamado = j.chamado; st.mensagens = st.mensagens.concat(j.mensagens); if (st.mensagens.length) st.ultimoId = st.mensagens[st.mensagens.length - 1].id;
      renderConversa(); renderBloqueio(); carregarFila();
    } catch (err) { alert(err.message); b.disabled = false; }
  });
```
Chamar `renderBloqueio()` no fim de `abrir(id)` (depois de `renderConversa()`). O `_limpar`/payload da equipe precisa expor `area` no chamado — confirme em `_payload`/`_chamado_dict` que `area` já vem (o `renderConversa` usa `c.area_rotulo`, então o dict inteiro vem; `area` deve estar). Registrar `"envios": "Envios do portal"` em `chamados.AREAS` (l.99) para o rótulo aparecer. Também `atendimento_mobile.html`: não mexer nesta rodada (o Hugo libera no desktop); anotar no relatório final.

- [ ] **Step 7: Prova manual**

Painel em porta 8099 (de `painel_agentes/`: `py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"`). Abrir `/atendimento`, entrar no chamado criado na Task 4, ver o quadro, liberar como dedicado com `100` para 2 NFs → mensagens "R$ 50,00" no chat, linhas riscadas, `portal_envios.status = NA_FILA`, 2 linhas em `pedidos_dedicados`.

- [ ] **Step 8: Compile e preparar**

`py -3.11 -m py_compile painel_agentes/atendimento_chamados.py painel_agentes/test_atendimento_liberar.py portal_cliente/chamados.py`
`git add painel_agentes/atendimento_chamados.py painel_agentes/test_atendimento_liberar.py painel_agentes/templates/atendimento.html portal_cliente/chamados.py` (`atendimento_chamados.py` e `atendimento.html` têm hunks alheios: `git add -p`).
Commit (com ok): `Atendimento: quadro de NFs bloqueadas com liberar (dedicado/simples) e cancelar`

---

### Task 6: Worker — vincular o PS ao dedicado na reconciliação

**Files:**
- Modify: `portal_cliente/enviar_stokki.py` (`reconciliar_codigos` l.713-716)
- Test: `portal_cliente/test_reconciliar_codigos.py` (acrescentar 1 teste)

**Interfaces:**
- Consumes: `pedidos_dedicados.vincular_codigo(conn, envio_id, codigo)`.

- [ ] **Step 1: Teste**

Ler `portal_cliente/test_reconciliar_codigos.py` inteiro para copiar o helper `_conn()` e o caso positivo existente. Acrescentar:
```python
    def test_vincula_codigo_ao_dedicado(self):
        conn = _conn()
        conn.executescript("CREATE TABLE pedidos_dedicados (id INTEGER PRIMARY KEY, envio_id INTEGER, codigo_pedido TEXT, removido_em TEXT);"
                           "INSERT INTO pedidos_dedicados VALUES (1, 1, NULL, NULL);")
        # <semear portal_envios id=1 CRIADO sem codigo + documentos_processados com o PS, igual ao caso positivo existente>
        worker.reconciliar_codigos(conn)
        self.assertEqual(conn.execute("SELECT codigo_pedido FROM pedidos_dedicados WHERE id=1").fetchone()[0], "PS-36327")
```
(substituir o comentário pela semeadura copiada do teste positivo do arquivo, com `id=1` e código `PS-36327`.)

- [ ] **Step 2: Rodar e ver falhar** → `AssertionError: None != 'PS-36327'`

- [ ] **Step 3: Implementar**

Em `reconciliar_codigos`, após `_marcar(conn, e["id"], codigo_pedido=codigo)` e o `conn.commit()`:
```python
            try:
                pedidos_dedicados.vincular_codigo(conn, e["id"], codigo)
            except Exception as ex:
                logger.warning(f"nao vinculou dedicado do envio {e['id']}: {ex}")
```
com `import pedidos_dedicados` no topo (a raiz já está no `sys.path` do worker; confirmar pelo padrão `_RAIZ` do arquivo). `vincular_codigo` faz `UPDATE ... WHERE` e commit; se a tabela não existir a exceção é engolida com WARNING.

- [ ] **Step 4: Testes** → `py -3.11 -m unittest portal_cliente.test_reconciliar_codigos -v` PASS.

- [ ] **Step 5: Compile e preparar**

`py -3.11 -m py_compile portal_cliente/enviar_stokki.py`; `git add -p portal_cliente/enviar_stokki.py` (arquivo com hunks alheios) e `git add portal_cliente/test_reconciliar_codigos.py`.
Commit (com ok): `Worker: reconciliacao do PS preenche codigo_pedido em pedidos_dedicados`

---

### Task 7: Roteirização e pool — dedicado fora da rota compartilhada, chip no card

**Files:**
- Modify: `roteirizacao/criar_rotas_diarias.py` (antes da l.671 `ids_area_nao_atendida = set()`)
- Modify: `roteirizacao/incrementar_rotas.py` (antes da l.465)
- Modify: `painel_agentes/planejamento_rotas.py` (`_servico_para_pool` l.458-491; `buscar_pool_e_agendados` l.713-736; paradas l.857-859)
- Modify: `painel_agentes/templates/planejamento_rotas.html` (`badgesPedidoHtml` l.2493; CSS l.492)
- Test: `roteirizacao/test_dedicado_fora_rota.py`

**Interfaces:**
- Produces: `roteirizacao.dedicados.carregar_dedicados() -> dict[str, dict]` (wrapper: abre `pedidos_dedicados.conectar()`, devolve `ativos_por_codigo`, `{}` em caso de erro com WARNING).
- Produces: item do pool/parada com `"dedicado": {"valor": float, "valor_total": float, "n_grupo": int} | None`.

- [ ] **Step 1: Teste**

```python
# roteirizacao/test_dedicado_fora_rota.py
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest roteirizacao.test_dedicado_fora_rota"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dedicados  # noqa: E402


class CarregarDedicados(unittest.TestCase):
    def test_falha_no_banco_devolve_vazio(self):
        with mock.patch.object(dedicados.pedidos_dedicados, "conectar", side_effect=RuntimeError("db")):
            self.assertEqual(dedicados.carregar_dedicados(), {})

    def test_separar(self):
        ativos = {"PS-2": {"valor": 10.0}}
        with mock.patch.object(dedicados, "carregar_dedicados", return_value=ativos):
            restantes, fora = dedicados.separar_dedicados([{"id": 1, "code": "#PS-1"}, {"id": 2, "code": "#PS-2, PS-3"}])
        self.assertEqual([s["id"] for s in restantes], [1])
        self.assertEqual([s["id"] for s in fora], [2])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar** → `ModuleNotFoundError: dedicados`

- [ ] **Step 3: `roteirizacao/dedicados.py`**

```python
# -*- coding: utf-8 -*-
"""Pedidos dedicados na roteirizacao (Hugo, 23/09): ficam fora da rota
compartilhada e nao geram e-mail de area nao atendida."""
import logging
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402

logger = logging.getLogger(__name__)


def carregar_dedicados() -> dict[str, dict]:
    try:
        conn = pedidos_dedicados.conectar()
        try:
            return pedidos_dedicados.ativos_por_codigo(conn)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"nao carregou pedidos dedicados ({e}); seguindo sem a marca")
        return {}


def separar_dedicados(servicos: list[dict]) -> tuple[list[dict], list[dict]]:
    """(fora da marca, dedicados). Loga os codigos que saem."""
    restantes, fora = pedidos_dedicados.filtrar_dedicados(servicos, carregar_dedicados())
    if fora:
        logger.info(f"{len(fora)} pedido(s) dedicado(s) fora da rota compartilhada: {[s.get('code') for s in fora]}")
    return restantes, fora
```

- [ ] **Step 4: Ligar nos dois scripts**

`criar_rotas_diarias.py`, imediatamente antes do comentário `# Área não atendida (pedido do Hugo, 02/08)` (l.666):
```python
        # Dedicado (Hugo, 23/09): transporte a parte -- sai antes da checagem
        # de area nao atendida pra nao gerar e-mail nem entrar em rota.
        from dedicados import separar_dedicados
        servicos_brutos, _dedicados = separar_dedicados(servicos_brutos)
```
`incrementar_rotas.py`, antes do comentário `# Área não atendida (pedido do Hugo, 02/08) -- diferente do` (l.461):
```python
        from dedicados import separar_dedicados
        servicos, _dedicados = separar_dedicados(servicos)
```
Confirmar que `servicos` nesse ponto do `incrementar_rotas` é a lista bruta da Vuupt e que nada depois depende do tamanho original (l.514 usa `ids_servicos_atuais = {s["id"] for s in servicos}`: ok, dedicado só some do lote).

- [ ] **Step 5: Pool com chip**

`planejamento_rotas.py`: em `_servico_para_pool` acrescentar parâmetro `dedicado: dict | None = None` e chave `"dedicado": dedicado` no dict devolvido. Em `buscar_pool_e_agendados`, após o bloco `tipos_area` (l.719):
```python
    try:
        from dedicados import carregar_dedicados
        import pedidos_dedicados
        _ativos = carregar_dedicados()
        n_por_grupo: dict[str, int] = {}
        for d in _ativos.values():
            n_por_grupo[d["grupo_id"]] = n_por_grupo.get(d["grupo_id"], 0) + 1
        dedicados_por_id: dict[int, dict] = {}
        for s in servicos_brutos:
            for c in pedidos_dedicados.codigos_do_servico(s):
                if c in _ativos:
                    d = _ativos[c]
                    dedicados_por_id[s["id"]] = {"valor": d["valor"], "valor_total": d["valor_total_grupo"], "n_grupo": n_por_grupo[d["grupo_id"]]}
                    break
    except Exception as e:
        logger.warning(f"pool: sem marca de dedicado ({e})")
        dedicados_por_id = {}
```
Passar `dedicados_por_id.get(s["id"])` como `dedicado=` na chamada de `_servico_para_pool` (l.731) e devolver `"dedicados_por_service_id": dedicados_por_id` junto de `tipos_area_por_service_id` (l.778); na l.859 acrescentar `p["dedicado"] = pool_e_agendados["dedicados_por_service_id"].get(p["service_id"])`. Nota: `roteirizacao/` precisa estar no `sys.path` do painel para `from dedicados import`; o módulo já faz `from notificar_area_nao_atendida import` na l.715, então está.

`planejamento_rotas.html`: em `badgesPedidoHtml` (l.2496), antes do `if (p.tipo_area)`:
```js
    if (p.dedicado) {
      const v = Number(p.dedicado.valor).toFixed(2).replace('.', ',');
      const t = p.dedicado.n_grupo > 1 ? ` title="Cotação R$ ${Number(p.dedicado.valor_total).toFixed(2).replace('.', ',')} dividida entre ${p.dedicado.n_grupo} pedidos"` : '';
      partes.push(`<span class="badge-dedicado"${t}>🚚 Dedicado · R$ ${v}</span>`);
    }
```
CSS após `.badge-fora-area` (l.503):
```css
  .item-parada .badge-dedicado { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 100px; margin-top: 3px; background: rgba(0,120,255,0.14); color: var(--acento-texto); }
```
Adicionar `.badge-dedicado` ao seletor que esconde chips no modo "só número" (l.807, junto de `.badge-fora-area`). Gravar `li.dataset.dedicado = p.dedicado ? "1" : ""` em `criarLiParada` (l.2576) — a Task 8 usa.

- [ ] **Step 6: Testes e checagens**

`py -3.11 -m unittest roteirizacao.test_dedicado_fora_rota -v` → PASS.
`py -3.11 -m unittest painel_agentes.test_incrementar_rascunhos` → continua PASS.
`py -3.11 -m py_compile roteirizacao/dedicados.py roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py painel_agentes/planejamento_rotas.py`
Prova: painel 8099 → `/planejamento`: inserir manualmente uma linha em `dados/dados.db` (`INSERT INTO pedidos_dedicados (codigo_pedido, valor, grupo_id, valor_total_grupo, marcado_em, marcado_por) VALUES ('PS-<um código do pool>', 45.5, 'x', 45.5, datetime('now'), 'teste')`) e ver o chip; apagar a linha depois.

- [ ] **Step 7: Preparar**

`git add roteirizacao/dedicados.py roteirizacao/test_dedicado_fora_rota.py roteirizacao/criar_rotas_diarias.py roteirizacao/incrementar_rotas.py painel_agentes/planejamento_rotas.py` e `git add -p painel_agentes/templates/planejamento_rotas.html` (hunks alheios).
Commit (com ok): `Roteirizacao: pedido dedicado fica fora da rota compartilhada; chip Dedicado no pool`

---

### Task 8: Planejamento — marcar/remover dedicado (menu, barra de seleção, modal)

**Files:**
- Modify: `painel_agentes/painel_agentes.py` (rotas, após `api_editar_transportadora` l.2353)
- Modify: `painel_agentes/planejamento_rotas.py` (função `marcar_dedicados`)
- Modify: `painel_agentes/templates/planejamento_rotas.html` (menu l.5537-5542; botão l.2247; modal após l.1838; JS junto do modal de transportadora l.5229-5297; ligações l.6195-6200; habilitar botão l.3580)
- Test: `painel_agentes/test_marcar_dedicados.py`

**Interfaces:**
- Produces: `planejamento_rotas.marcar_dedicados(itens: list[dict], valor: float, por: str, conn=None) -> dict` com `itens = [{service_id, codigo, sender_id, remetente_nome, numero_nf?}]` (o front manda os dados do `POOL_BY_ID`) → `{"ok": True, "grupo_id", "valores": {codigo: valor}}`.
- Produces: `planejamento_rotas.remover_dedicados(codigos: list[str], por: str, conn=None) -> dict` → `{"ok": True, "removidos": int}`.
- Rotas: `POST /api/planejamento/dedicado` `{itens, valor}`; `DELETE /api/planejamento/dedicado` `{codigos}`.

- [ ] **Step 1: Teste**

```python
# painel_agentes/test_marcar_dedicados.py
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest painel_agentes.test_marcar_dedicados"""
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pedidos_dedicados as pd  # noqa: E402
from planejamento_rotas import marcar_dedicados, remover_dedicados  # noqa: E402


class MarcarDedicados(unittest.TestCase):
    def test_marca_e_remove(self):
        conn = pd.conectar(":memory:")
        itens = [{"service_id": 1, "codigo": "#PS-1", "sender_id": 5, "remetente_nome": "ACME", "numero_nf": "10"},
                 {"service_id": 2, "codigo": "#PS-2, PS-3", "sender_id": 5, "remetente_nome": "ACME"}]
        r = marcar_dedicados(itens, 100, "hugo", conn=conn)
        self.assertEqual(r["valores"], {"PS-1": 50.0, "PS-2": 50.0})  # 1o codigo de cada servico
        self.assertEqual(pd.ativos_por_codigo(conn)["PS-1"]["service_id"], 1)
        r2 = remover_dedicados(["#PS-1"], "hugo", conn=conn)
        self.assertEqual(r2["removidos"], 1)
        self.assertEqual(list(pd.ativos_por_codigo(conn)), ["PS-2"])

    def test_sem_itens(self):
        with self.assertRaises(ValueError):
            marcar_dedicados([], 10, "x", conn=pd.conectar(":memory:"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar** → `ImportError: cannot import name 'marcar_dedicados'`

- [ ] **Step 3: Backend em `planejamento_rotas.py`** (perto de `editar_transportadora_pedidos`, l.1945)

```python
def marcar_dedicados(itens: list[dict], valor: float, por: str, conn=None) -> dict:
    """Marca pedidos do pool como dedicados dividindo `valor` entre eles.
    Servico com mais de um codigo usa o primeiro (e o que a Vuupt mostra)."""
    import pedidos_dedicados
    if not itens:
        raise ValueError("Nenhum pedido selecionado.")
    pedidos = []
    for it in itens:
        codigos = pedidos_dedicados.codigos_do_servico({"code": it.get("codigo")})
        if not codigos:
            raise ValueError(f"Pedido #{it.get('service_id')} sem código.")
        pedidos.append({"codigo_pedido": codigos[0], "service_id": it.get("service_id"), "sender_id": it.get("sender_id"),
                        "remetente_nome": it.get("remetente_nome"), "numero_nf": it.get("numero_nf")})
    propria = conn is None
    conn = conn or pedidos_dedicados.conectar()
    try:
        grupo = pedidos_dedicados.marcar(conn, pedidos, float(valor), por)
        valores = {r["codigo_pedido"]: r["valor"] for r in conn.execute(
            "SELECT codigo_pedido, valor FROM pedidos_dedicados WHERE grupo_id = ?", (grupo,))}
    finally:
        if propria:
            conn.close()
    return {"ok": True, "grupo_id": grupo, "valores": valores}


def remover_dedicados(codigos: list[str], por: str, conn=None) -> dict:
    import pedidos_dedicados
    propria = conn is None
    conn = conn or pedidos_dedicados.conectar()
    try:
        n = sum(pedidos_dedicados.remover(conn, codigo_pedido=c, por=por) for c in codigos)
    finally:
        if propria:
            conn.close()
    return {"ok": True, "removidos": n}
```

Rotas em `painel_agentes.py` (importar as duas funções no `from planejamento_rotas import (...)` l.63-68):
```python
@app.route("/api/planejamento/dedicado", methods=["POST", "DELETE"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_planejamento_dedicado():
    """Marca (POST {itens, valor}) ou desmarca (DELETE {codigos}) pedidos
    dedicados -- Hugo, 23/09. Nada vai pra Vuupt; a marca e nossa."""
    body = request.get_json(force=True) or {}
    por = session.get("usuario") or g.nivel_acesso
    try:
        if request.method == "DELETE":
            return jsonify(remover_dedicados([str(c) for c in body.get("codigos") or []], por))
        from atendimento_chamados import valor_brl
        return jsonify(marcar_dedicados(body.get("itens") or [], valor_brl(body.get("valor")), por))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        logging.getLogger(__name__).exception("Falha em dedicado")
        return jsonify({"erro": str(e)}), 500
```
(`valor_brl` vem da Task 5; `session`/`g` já são importados no painel.)

- [ ] **Step 4: Testes** → `py -3.11 -m unittest painel_agentes.test_marcar_dedicados -v` PASS.

- [ ] **Step 5: Front**

Modal (após `</div>` da l.1838):
```html
{# Dedicado (Hugo, 23/09): valor da cotacao dividido entre os pedidos escolhidos #}
<div class="modal-overlay-editar-endereco" id="modal-dedicado">
  <div class="modal-editar-endereco" role="dialog" aria-modal="true" aria-labelledby="titulo-modal-dedicado">
    <h2 id="titulo-modal-dedicado">Marcar como dedicado</h2>
    <p class="subtitulo"><strong id="dedicado-resumo"></strong> — o valor da cotação é dividido em partes iguais e vai pro financeiro na quinzena. Nada muda na Vuupt; a roteirização automática deixa esses pedidos fora das rotas compartilhadas.</p>
    <ul id="dedicado-lista" style="margin:0 0 10px; padding-left:18px; font-size:12.5px; max-height:140px; overflow:auto;"></ul>
    <div class="campo-reagendar">
      <label for="dedicado-valor">Valor total da cotação (R$)</label>
      <input id="dedicado-valor" inputmode="decimal" placeholder="0,00">
      <p class="transportadora-endereco" id="dedicado-previa">—</p>
    </div>
    <div class="acoes">
      <button type="button" class="cancelar" id="botao-cancelar-dedicado">Cancelar</button>
      <button type="button" class="confirmar" id="botao-confirmar-dedicado" disabled>Marcar</button>
    </div>
  </div>
</div>
```
Botão da barra (após l.2247): `<button type="button" id="botao-dedicado-selecao" disabled title="Marca os pedidos selecionados como envio dedicado e divide o valor da cotação entre eles">Dedicado</button>`. Habilitar junto do transportadora (l.3580): `document.getElementById("botao-dedicado-selecao").disabled = SELECIONADOS.size === 0;`.

JS (junto do bloco de transportadora, l.5229):
```js
  let dedicadoPendente = null;  // { itens: [{service_id, codigo, sender_id, remetente_nome, numero_nf}] }

  function itemDedicadoDe(serviceId) {
    const p = POOL_BY_ID[serviceId] || {};
    return { service_id: serviceId, codigo: p.codigo || "", sender_id: p.sender_id, remetente_nome: p.remetente_nome, numero_nf: p.numero_nf || null };
  }

  function atualizarPreviaDedicado() {
    const n = dedicadoPendente ? dedicadoPendente.itens.length : 0;
    const bruto = document.getElementById("dedicado-valor").value.trim().replace(/\./g, "").replace(",", ".");
    const v = Number(bruto);
    const ok = n > 0 && bruto !== "" && isFinite(v) && v > 0;
    document.getElementById("dedicado-previa").textContent = ok ? `R$ ${v.toFixed(2).replace(".", ",")} ÷ ${n} = R$ ${(Math.floor(v * 100 / n) / 100).toFixed(2).replace(".", ",")} cada` : "—";
    document.getElementById("botao-confirmar-dedicado").disabled = !ok;
  }

  function abrirModalDedicado(serviceIds) {
    const itens = serviceIds.map(itemDedicadoDe).filter(it => it.codigo);
    if (!itens.length) { alert("Só pedidos do pool (com código) podem ser marcados."); return; }
    dedicadoPendente = { itens };
    document.getElementById("dedicado-resumo").textContent = `${itens.length} pedido(s)`;
    document.getElementById("dedicado-lista").innerHTML = itens.map(it => `<li>${escaparHtml(it.codigo)} · ${escaparHtml(it.remetente_nome || "")}</li>`).join("");
    document.getElementById("dedicado-valor").value = "";
    atualizarPreviaDedicado();
    document.getElementById("modal-dedicado").classList.add("aberto");
  }

  async function confirmarDedicado(botao) {
    if (!dedicadoPendente) return;
    const original = botao.textContent; botao.disabled = true; botao.textContent = "Marcando...";
    try {
      await postJSON("/api/planejamento/dedicado", { itens: dedicadoPendente.itens, valor: document.getElementById("dedicado-valor").value });
      document.getElementById("modal-dedicado").classList.remove("aberto");
      location.reload();
    } catch (e) {
      alert(`Falha ao marcar dedicado: ${e.message}`); botao.disabled = false; botao.textContent = original;
    }
  }

  async function removerDedicado(serviceIds) {
    const codigos = serviceIds.map(id => (POOL_BY_ID[id] || {}).codigo).filter(Boolean);
    if (!codigos.length || !confirm(`Remover a marca de dedicado de ${codigos.length} pedido(s)?`)) return;
    try {
      const resp = await fetch(BASE_PATH + "/api/planejamento/dedicado", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ codigos }) });
      const dados = await resp.json();
      if (!resp.ok) throw new Error(dados.erro || "Falha");
      location.reload();
    } catch (e) { alert(`Falha ao remover dedicado: ${e.message}`); }
  }
```
`POOL_BY_ID` só cobre o pool; paradas em rascunho podem não estar nele. Se `POOL_BY_ID[id]` for `undefined`, cair no `li`: em `itemDedicadoDe`, quando `!POOL_BY_ID[serviceId]`, ler do DOM `document.querySelector(`.item-parada[data-service-id="${serviceId}"]`)` → `.codigo` e `.remetente` `textContent`.

Menu (l.5542, dentro do `if (PODE_EDITAR)` que tem Transportadora):
```js
      const alvoDedicado = SELECIONADOS.has(serviceId) && SELECIONADOS.size > 1 ? Array.from(SELECIONADOS) : [serviceId];
      if (li.dataset.dedicado) itens.push({ rotulo: "Remover dedicado", acao: () => removerDedicado(alvoDedicado) });
      else itens.push({ rotulo: `Marcar como dedicado${alvoDedicado.length > 1 ? ` (${alvoDedicado.length} selecionados)` : "…"}`, acao: () => abrirModalDedicado(alvoDedicado) });
```
Ligações (junto de l.6195):
```js
    document.getElementById("botao-dedicado-selecao").addEventListener("click", () => abrirModalDedicado(Array.from(SELECIONADOS)));
    document.getElementById("botao-cancelar-dedicado").addEventListener("click", () => { document.getElementById("modal-dedicado").classList.remove("aberto"); dedicadoPendente = null; });
    document.getElementById("dedicado-valor").addEventListener("input", atualizarPreviaDedicado);
    document.getElementById("botao-confirmar-dedicado").addEventListener("click", (e) => confirmarDedicado(e.target));
```
O observador de foco dos modais (l.6323) já cobre `[class^="modal-overlay-"]`, então Esc/Tab funcionam sem mais nada.

- [ ] **Step 6: Prova manual**

Painel 8099 → `/planejamento` (data de hoje ou futura). Botão direito num card → "Marcar como dedicado…" → `100` → chip "Dedicado · R$ 100,00". Selecionar 3 cards → botão "Dedicado" → `100` → prévia "÷ 3 = R$ 33,33 cada" → chips 33,34/33,33/33,33. Botão direito → "Remover dedicado" some o chip. Limpar as linhas de teste em `pedidos_dedicados` ao final (`UPDATE ... SET removido_em = datetime('now') WHERE marcado_por = '<seu usuario>'`), ou deixar, já que o banco local é de desenvolvimento.

- [ ] **Step 7: Compile e preparar**

`py -3.11 -m py_compile painel_agentes/painel_agentes.py painel_agentes/planejamento_rotas.py painel_agentes/test_marcar_dedicados.py`
`git add painel_agentes/test_marcar_dedicados.py painel_agentes/planejamento_rotas.py` + `git add -p painel_agentes/painel_agentes.py painel_agentes/templates/planejamento_rotas.html`.
Commit (com ok): `Planejamento: marcar/remover dedicado por botao direito e selecao, com valor dividido`

---

### Task 9: E-mail do financeiro (dias 1 e 16) + timer + config

**Files:**
- Create: `notificar_dedicados_financeiro.py`
- Create: `infra/stokki-dedicados-financeiro.service`, `infra/stokki-dedicados-financeiro.timer`
- Test: `test_notificar_dedicados_financeiro.py`

**Interfaces:**
- Produces: `montar_email(ini, fim, linhas, destino_original) -> (assunto, html)`, `executar(config, data_ref, modo_teste, conn=None) -> dict`, `main()`.
- Config (defaults no código): `financeiro.email` (default `financeiro@freshlogbr.com`), `financeiro.forcar_destino` (ausente → `hugo@freshlogbr.com`; `""` → envio real), `financeiro.ativo` (default `True`).

- [ ] **Step 1: Testes**

```python
# test_notificar_dedicados_financeiro.py
# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_notificar_dedicados_financeiro"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import pedidos_dedicados as pd  # noqa: E402
import notificar_dedicados_financeiro as nf  # noqa: E402


def _conn():
    conn = pd.conectar(":memory:")
    for cod, nome, valor, quando, nfe in [("PS-1", "ACME", 33.34, "2026-09-03 10:00:00", "10"), ("PS-2", "ACME", 33.33, "2026-09-03 10:00:00", "11"),
                                          ("PS-3", "Beta", 200, "2026-09-14 10:00:00", None), ("PS-4", "Beta", 5, "2026-09-16 00:00:00", None)]:
        conn.execute("INSERT INTO pedidos_dedicados (codigo_pedido, remetente_nome, numero_nf, valor, grupo_id, valor_total_grupo, marcado_em, marcado_por) "
                     "VALUES (?,?,?,?,'g',0,?,'a')", (cod, nome, nfe, valor, quando))
    conn.commit()
    return conn


class MontarEmail(unittest.TestCase):
    def test_subtotais_e_total(self):
        _, _, linhas = pd.listar_quinzena(_conn(), date(2026, 9, 16))
        assunto, html = nf.montar_email(date(2026, 9, 1), date(2026, 9, 15), linhas, None)
        self.assertIn("01/09", assunto); self.assertIn("15/09", assunto)
        for trecho in ("ACME", "PS-1", "NF 10", "R$ 66,67", "Beta", "R$ 200,00", "R$ 266,67"):
            self.assertIn(trecho, html, trecho)
        self.assertNotIn("PS-4", html)

    def test_vazio(self):
        _, html = nf.montar_email(date(2026, 9, 1), date(2026, 9, 15), [], None)
        self.assertIn("Nenhum pedido dedicado", html)


class Executar(unittest.TestCase):
    def test_redireciona_por_padrao(self):
        with mock.patch.object(nf, "enviar_email", return_value=True) as env:
            r = nf.executar({"email": {}}, date(2026, 9, 16), modo_teste=False, conn=_conn())
        self.assertEqual(env.call_args.args[0], ["hugo@freshlogbr.com"])
        self.assertEqual(r["pedidos"], 3)
        self.assertIn("financeiro@freshlogbr.com", env.call_args.args[2])  # aviso de destino real

    def test_envio_real_com_forcar_vazio(self):
        with mock.patch.object(nf, "enviar_email", return_value=True) as env:
            nf.executar({"email": {}, "financeiro": {"forcar_destino": "", "email": "fin@x"}}, date(2026, 9, 16), modo_teste=False, conn=_conn())
        self.assertEqual(env.call_args.args[0], ["fin@x"])

    def test_desligado(self):
        with mock.patch.object(nf, "enviar_email") as env:
            r = nf.executar({"financeiro": {"ativo": False}}, date(2026, 9, 16), modo_teste=False, conn=_conn())
        env.assert_not_called(); self.assertEqual(r["desativado"], "financeiro.ativo=false")

    def test_data_ref_dia_1_bissexto(self):
        with mock.patch.object(nf, "enviar_email", return_value=True):
            r = nf.executar({"email": {}}, date(2028, 3, 1), modo_teste=True, conn=_conn())
        self.assertEqual((r["inicio"], r["fim"]), ("2028-02-16", "2028-02-29"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar** → `ModuleNotFoundError: notificar_dedicados_financeiro`

- [ ] **Step 3: Script**

```python
# notificar_dedicados_financeiro.py
# -*- coding: utf-8 -*-
"""
E-mail quinzenal pro financeiro com os pedidos dedicados (Hugo, 23/09/2026):
remetente, pedido e valor da quinzena anterior, pela data da marcacao.
Roda dia 1 (16..fim do mes anterior) e dia 16 (1..15) as 08:00
(infra/stokki-dedicados-financeiro.timer).

    venv/bin/python notificar_dedicados_financeiro.py --modo-teste
    venv/bin/python notificar_dedicados_financeiro.py --data-ref 2026-10-01

Config (config.yaml):
    financeiro:
      email: financeiro@freshlogbr.com
      forcar_destino: hugo@freshlogbr.com   # "" = envio real
      ativo: true
"""
import argparse
import html
import logging
import sys
import time
from datetime import date
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from email_utils import COR_BORDA, COR_TEXTO_SUAVE, enviar_email, envelope_html  # noqa: E402
from notificar_execucao_agente import notificar_execucao  # noqa: E402
import pedidos_dedicados  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dedicados_financeiro")

CONFIG_PATH = _RAIZ / "config.yaml"
EMAIL_TESTE = "hugo@freshlogbr.com"
EMAIL_FINANCEIRO_PADRAO = "financeiro@freshlogbr.com"


def _carregar_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _secao(config: dict) -> dict:
    return (config or {}).get("financeiro") or {}


def forcar_destino_do_config(config: dict) -> str:
    secao = _secao(config)
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def _brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def montar_email(ini: date, fim: date, linhas: list[dict], destino_original: str | None) -> tuple[str, str]:
    assunto = f"[Fresh Log] Pedidos dedicados {ini.strftime('%d/%m')} a {fim.strftime('%d/%m/%Y')}"
    aviso = (f"<p style='background:#FFF4D6;padding:8px;border-radius:6px'>Redirecionado (piloto). Destino real: "
             f"{html.escape(destino_original)}</p>" if destino_original else "")
    if not linhas:
        corpo = f"<p>Nenhum pedido dedicado marcado entre {_br(ini)} e {_br(fim)}.</p>"
        return assunto, envelope_html(aviso + corpo, "Fresh Log - pedidos dedicados da quinzena (automatico).")
    grupos: dict[str, list[dict]] = {}
    for l in linhas:
        grupos.setdefault(l.get("remetente_nome") or "(remetente nao identificado)", []).append(l)
    partes = [f"<p>Pedidos marcados como <b>envio dedicado</b> entre {_br(ini)} e {_br(fim)} ({len(linhas)} pedido(s)).</p>",
              f"<table style='border-collapse:collapse;width:100%;font-size:13px'><tr style='color:{COR_TEXTO_SUAVE}'>"
              f"<th align='left' style='padding:6px;border-bottom:1px solid {COR_BORDA}'>Remetente</th>"
              f"<th align='left' style='padding:6px;border-bottom:1px solid {COR_BORDA}'>Pedido</th>"
              f"<th align='left' style='padding:6px;border-bottom:1px solid {COR_BORDA}'>Marcado em</th>"
              f"<th align='right' style='padding:6px;border-bottom:1px solid {COR_BORDA}'>Valor</th></tr>"]
    total = 0.0
    for nome, itens in grupos.items():
        sub = 0.0
        for l in itens:
            ped = l.get("codigo_pedido") or f"envio #{l.get('envio_id')}"
            if l.get("numero_nf"):
                ped += f" · NF {l['numero_nf']}"
            partes.append(f"<tr><td style='padding:5px 6px'>{html.escape(nome)}</td><td style='padding:5px 6px'>{html.escape(ped)}</td>"
                          f"<td style='padding:5px 6px'>{l['marcado_em'][8:10]}/{l['marcado_em'][5:7]}</td>"
                          f"<td align='right' style='padding:5px 6px'>{_brl(l['valor'])}</td></tr>")
            sub += l["valor"]
        partes.append(f"<tr><td colspan='3' style='padding:5px 6px;border-top:1px solid {COR_BORDA}'><b>Subtotal {html.escape(nome)}</b></td>"
                      f"<td align='right' style='padding:5px 6px;border-top:1px solid {COR_BORDA}'><b>{_brl(sub)}</b></td></tr>")
        total += sub
    partes.append(f"<tr><td colspan='3' style='padding:8px 6px;border-top:2px solid {COR_BORDA}'><b>Total</b></td>"
                  f"<td align='right' style='padding:8px 6px;border-top:2px solid {COR_BORDA}'><b>{_brl(round(total, 2))}</b></td></tr></table>")
    return assunto, envelope_html(aviso + "".join(partes), "Fresh Log - pedidos dedicados da quinzena (automatico).")


def executar(config: dict, data_ref: date, modo_teste: bool, conn=None) -> dict:
    r = {"inicio": "", "fim": "", "pedidos": 0, "enviado": False}
    secao = _secao(config)
    if not modo_teste and not secao.get("ativo", True):
        r["desativado"] = "financeiro.ativo=false"
        return r
    propria = conn is None
    conn = conn or pedidos_dedicados.conectar()
    try:
        ini, fim, linhas = pedidos_dedicados.listar_quinzena(conn, data_ref)
    finally:
        if propria:
            conn.close()
    r.update({"inicio": ini.isoformat(), "fim": fim.isoformat(), "pedidos": len(linhas)})
    if data_ref.day not in (1, 16):
        logger.info(f"rodando fora do dia 1/16 (data-ref {data_ref}); quinzena {ini}..{fim}")
    destino_real = secao.get("email") or EMAIL_FINANCEIRO_PADRAO
    forcar = EMAIL_TESTE if modo_teste else forcar_destino_do_config(config)
    destinos = [forcar] if forcar else [destino_real]
    assunto, corpo = montar_email(ini, fim, linhas, destino_real if forcar else None)
    if modo_teste:
        assunto = "[MODO TESTE] " + assunto
    r["enviado"] = enviar_email(destinos, assunto, corpo, (config or {}).get("email", {}) or {})
    logger.info(f"quinzena {ini}..{fim}: {len(linhas)} pedido(s), e-mail pra {destinos}: {'ok' if r['enviado'] else 'FALHOU'}")
    return r


def main(modo_teste: bool = False, data_ref: str | None = None) -> None:
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Pedidos dedicados pro financeiro iniciado.")
    inicio = time.time()
    resultado = {"status": "ok", "detalhe": ""}
    config = {}
    try:
        config = _carregar_config()
        r = executar(config, date.fromisoformat(data_ref) if data_ref else date.today(), modo_teste)
        resultado["detalhe"] = f"{r['inicio']} a {r['fim']}: {r['pedidos']} pedido(s)" + (", DESLIGADO" if r.get("desativado") else "")
        if not r.get("desativado") and not r["enviado"]:
            resultado["status"] = "erro"
            resultado["detalhe"] += " -- e-mail nao enviado"
    except Exception as e:
        logger.exception(f"Erro: {e}")
        resultado.update(status="erro", detalhe=str(e))
    finally:
        try:
            notificar_execucao({"Pedidos dedicados (financeiro)": resultado}, time.time() - inicio, modo_teste, config or _carregar_config())
        except Exception as e:
            logger.warning(f"Falha ao notificar execucao: {e}")
    if resultado["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--modo-teste", action="store_true")
    parser.add_argument("--data-ref", default=None, help="AAAA-MM-DD (default hoje); a quinzena e a anterior fechada")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste, data_ref=args.data_ref)
```
Confira a assinatura de `notificar_execucao` em `notificar_execucao_agente.py` (usada igual em `notificar_nfs_em_rota.py:369`).

- [ ] **Step 4: Units**

`infra/stokki-dedicados-financeiro.service`:
```
[Unit]
Description=Stokki Eventos - E-mail quinzenal pro financeiro com os pedidos dedicados
OnFailure=stokki-alerta-falha@%n.service

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/opt/stokki-eventos
ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/notificar_dedicados_financeiro.py
```
`infra/stokki-dedicados-financeiro.timer`:
```
[Unit]
Description=Dispara stokki-dedicados-financeiro.service nos dias 1 e 16 as 08:00

[Timer]
OnCalendar=*-*-01,16 08:00:00
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
```
(`Persistent=true` de propósito: se a VPS estiver fora às 08:00 do dia 1, roda ao voltar; a quinzena é calculada pela data-ref do dia, então até dia 15 ainda pega a quinzena certa.)

- [ ] **Step 5: Testes e prova**

`py -3.11 -m unittest test_notificar_dedicados_financeiro -v` → PASS.
`py -3.11 notificar_dedicados_financeiro.py --modo-teste --data-ref 2026-09-16` → e-mail chega em hugo@ (config local tem SMTP) com as linhas de teste do banco local, ou "Nenhum pedido dedicado".
`py -3.11 -m py_compile notificar_dedicados_financeiro.py test_notificar_dedicados_financeiro.py`

- [ ] **Step 6: Preparar**

`git add notificar_dedicados_financeiro.py test_notificar_dedicados_financeiro.py infra/stokki-dedicados-financeiro.service infra/stokki-dedicados-financeiro.timer`
Commit (com ok): `Financeiro: e-mail quinzenal (dias 1 e 16) com pedidos dedicados, remetente, pedido e valor`

---

### Task 10: Fechamento — suíte inteira, memória, checklist de deploy

**Files:**
- Modify: `docs/superpowers/specs/2026-09-23-bloqueio-area-nao-atendida-pedidos-dedicados-design.md` (seção "Divergências na implementação")
- Memória: `C:\Users\Hugo Maçol\.claude\projects\C--agente-stokki-eventos\memory\project_bloqueio_area_pedidos_dedicados.md` + linha no `MEMORY.md`

- [ ] **Step 1: Rodar tudo**

```
py -3.11 -m unittest test_pedidos_dedicados test_notificar_dedicados_financeiro portal_cliente.test_bloqueio_area portal_cliente.test_reconciliar_codigos portal_cliente.test_envio_planilha painel_agentes.test_atendimento_liberar painel_agentes.test_marcar_dedicados roteirizacao.test_dedicado_fora_rota painel_agentes.test_incrementar_rascunhos
```
Expected: OK, 0 failures.

- [ ] **Step 2: Registrar divergências na spec**

Acrescentar ao fim da spec:
```
## Divergências na implementação (23/09)
- Código canônico é `PS-NNNNN` via `pedidos_dedicados.normalizar_codigo`, não `normalizar_order_number` (essa devolve só dígitos e não trata sufixo `-R1` nem lista com vírgula).
- "Solicitar cotação" não tem rota própria: o botão abre o widget (`window.atdAbrirChamado`) e usa o envio de mensagem que já existe.
- Regra da roteirização entra via `identificar_area_nao_atendida([servico_sintetico], api_key)`, que já fecha `classificar_pedido` com as dependências.
- Quinzena fora dos dias 1/16 = última quinzena fechada antes de `--data-ref`.
- `atendimento_mobile.html` não recebeu o quadro de liberação (desktop só, nesta rodada).
```

- [ ] **Step 3: Memória**

Escrever `project_bloqueio_area_pedidos_dedicados.md` (type: project) com: o que foi feito, estado (NÃO commitado / commitado / deployado), chaves de config que o Hugo precisa pôr na VPS (`portal_cliente.envios.{bloqueio_area_ativo, forcar_destino}`, `financeiro.{email, forcar_destino, ativo}`), timer novo, serviços a reiniciar (`portal-cliente`, `portal-cliente-envios`, `painel-agentes`), e a prova a fazer em produção (1 XML do cliente teste com UF≠SP → liberar → ver na Stokki). Adicionar linha no `MEMORY.md`.

- [ ] **Step 4: Relatório pro Hugo**

Listar: arquivos tocados, testes rodados com contagem, provas manuais feitas, o que ficou redirecionado pra hugo@, e o checklist de deploy (`deploy-vps`): pull, chown, copiar 2 units, `daemon-reload`, `enable --now stokki-dedicados-financeiro.timer`, restart dos 3 serviços, `notificar_dedicados_financeiro.py --modo-teste` como www-data, prova real com o cliente teste.
