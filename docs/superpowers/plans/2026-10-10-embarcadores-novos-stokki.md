# Embarcadores novos na Stokki: pré-cadastro e convite — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** detectar de hora em hora cliente da Stokki que não está em `interno`, pré-cadastrar, criar ou reaproveitar o remetente da Vuupt, convidar pelo WhatsApp (ou e-mail) com o link do portal e cobrar no portal o tipo de carga, os e-mails e o WhatsApp, com aprovação do Hugo antes de valer no roteirizador.

**Architecture:** um módulo de estado na raiz (`cadastro_embarcador.py`, tabela `cadastro_embarcador`) é lido e escrito por três pontas: a rotina de lote `embarcadores_stokki.py` (timer `stokki-embarcadores-novos`), a tela `/completar-cadastro` do portal e a aprovação no `/inicio` do painel e no CLI `gerenciar_clientes.py cadastro`. O WhatsApp ao cliente passa pelo canal de clientes que já existe (`notificar_whatsapp`, teto e `forcar_destino`); o aviso interno vai pro grupo ALERTAS por `notificar_whatsapp.despachar`.

**Tech Stack:** Python 3.11, SQLite (`dados/dados.db`), Flask (portal e painel), unittest, systemd timer, OpenWA REST, Vuupt API (`POST /customers`, já em `VuuptClient.criar_customer`).

**Spec:** `docs/superpowers/specs/2026-10-08-embarcadores-novos-stokki-design.md` (ver a seção "Ajustes do plano" no fim dela).

## Global Constraints

- Trabalhar num worktree a partir de `origin/master` (o `master` local está 129 commits atrás): `git worktree add .claude/worktrees/embarcadores-novos -b embarcadores-novos origin/master`. Todos os caminhos abaixo são relativos à raiz desse worktree.
- Idioma: português. Comentários e logs sem acento nos arquivos que já seguem isso (`notificar_whatsapp.py`, `embarcadores_stokki.py`, `cadastro_embarcador.py`).
- Python local: `py -3.11`. Testes: `py -3.11 -m unittest <modulo>` da raiz; testes de `painel_agentes` rodam de dentro de `painel_agentes/`. Nunca misturar `painel_agentes` com módulos da raiz no mesmo comando.
- `config.yaml` não existe no worktree e nunca é lido nem copiado. Teste nenhum pode depender dele: injete config.
- Envio real a cliente fica preso por padrão: WhatsApp em `whatsapp_notificacoes.clientes.forcar_destino`; e-mail em `cadastro_embarcador.forcar_destino_email`, que AUSENTE vale `hugo@freshlogbr.com` (envio real exige `forcar_destino_email: ""` explícito).
- `interno.habilidade` só muda na aprovação. A rotina nunca escreve habilidade.
- Situações de `cadastro_embarcador.situacao`: `INATIVO`, `PENDENTE`, `CONVIDADO`, `REENVIADO`, `SEM_CONTATO`, `PREENCHIDO`, `APROVADO`.
- Ativo = pedido em `nucleo_pedidos` com o CNPJ do remetente nos últimos 30 dias. Reenvio único após 3 dias sem PIN definido.
- Cada aviso no grupo sai uma vez por evento (assinatura conferida em `notificacoes_whatsapp`).
- Commits: um por tarefa, mensagem `Área: o que mudou`, só `git add` dos arquivos da tarefa, terminando com a linha `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Push e deploy só quando o Hugo pedir.

## Review Focus

1. Rótulo da Stokki com parênteses dentro da razão social, ex. `PADOCA (PADARIA (FILIAL 2) LTDA - 11.111.111/0001-11)`: apelido e CNPJ saem certos. Teste na Task 2.
2. CNPJ já em `interno` cadastrado à mão sem `stkkc_id`: a rotina só preenche o `stkkc_id`, não duplica nem convida. Teste na Task 3.
3. Duas rodadas seguidas: a segunda não reenvia convite, não manda e-mail de novo e não repete aviso no grupo. Teste na Task 5.
4. Cliente que define o PIN e tenta abrir qualquer tela ou API antes de completar: sempre cai na tela de cadastro; a equipe operando o portal nunca é desviada. Teste na Task 6.
5. Formulário com e-mail inválido ou sem tipo de carga: nada é gravado, nem preferências nem cadastro. Teste na Task 6.

---

### Task 1: estado do pré-cadastro (`cadastro_embarcador.py`)

**Files:**
- Create: `cadastro_embarcador.py`
- Test: `test_cadastro_embarcador.py`

**Interfaces:**
- Consumes: nada.
- Produces:
  - constantes `INATIVO, PENDENTE, CONVIDADO, REENVIADO, SEM_CONTATO, PREENCHIDO, APROVADO`, `ABERTOS_PRO_CLIENTE`, `HABILIDADES`, `DESCRICOES_HABILIDADE: dict[str, str]`
  - `class CadastroInvalido(ValueError)` com `.mensagem: str` e `.codigo: int`
  - `garantir_tabela(conn) -> None`
  - `buscar(conn, cnpj) -> dict | None`
  - `registrar(conn, cnpj, stkkc_id: int, situacao: str, celular: str = "", email: str = "", agora: datetime | None = None) -> dict`
  - `atualizar(conn, cnpj, agora: datetime | None = None, **campos) -> dict`
  - `normalizar_habilidade(valor) -> str`
  - `precisa_completar(conn, cnpj) -> bool`
  - `salvar_preenchimento(conn, cnpj, habilidade, agora=None) -> dict`
  - `aprovar(conn, cnpj, por: str, habilidade: str | None = None, agora=None) -> dict`
  - `listar_abertos(conn) -> list[dict]` (linhas de `cadastro_embarcador` + chave `nome`)

- [ ] **Step 1: Write the failing test**

`test_cadastro_embarcador.py`:

```python
# -*- coding: utf-8 -*-
"""Estado do pre-cadastro automatico de embarcador (cadastro_embarcador.py).

Rodar: py -3.11 -m unittest test_cadastro_embarcador
"""
import sqlite3
import unittest
from datetime import datetime

import cadastro_embarcador as cad

DDL_INTERNO = ("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, habilidade TEXT, nome_remetente TEXT, "
               "apelido TEXT, fator_ponderado REAL, observacoes TEXT, criado_em TEXT, atualizado_em TEXT, "
               "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
CNPJ = "49874051000131"
AGORA = datetime(2026, 10, 10, 8, 0, 0)


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(DDL_INTERNO)
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, apelido, sender_id, stkkc_id) "
                          "VALUES (?, 'NONNA DISTRIBUICAO', 'FABENNE', 23084712, 100)", (CNPJ,))
        self.conn.commit()

    def habilidade_interno(self):
        return self.conn.execute("SELECT habilidade FROM interno WHERE cnpj_embarcador = ?", (CNPJ,)).fetchone()[0]


class TestRegistro(_Base):
    def test_registrar_normaliza_cnpj_e_devolve_a_linha(self):
        c = cad.registrar(self.conn, "49.874.051/0001-31", 100, cad.PENDENTE, celular="(11) 98888-7777",
                          email="m@fabenne.com", agora=AGORA)
        self.assertEqual((c["cnpj_embarcador"], c["stkkc_id"], c["situacao"], c["celular_stokki"]),
                         (CNPJ, 100, "PENDENTE", "(11) 98888-7777"))
        self.assertEqual(c["criado_em"], "2026-10-10 08:00:00")

    def test_registrar_de_novo_nao_sobrescreve(self):
        cad.registrar(self.conn, CNPJ, 100, cad.PENDENTE, agora=AGORA)
        c = cad.registrar(self.conn, CNPJ, 100, cad.INATIVO, agora=AGORA)
        self.assertEqual(c["situacao"], "PENDENTE")

    def test_atualizar_recusa_coluna_desconhecida(self):
        cad.registrar(self.conn, CNPJ, 100, cad.PENDENTE, agora=AGORA)
        with self.assertRaises(ValueError):
            cad.atualizar(self.conn, CNPJ, AGORA, cnpj_embarcador="1")

    def test_buscar_inexistente(self):
        self.assertIsNone(cad.buscar(self.conn, "11111111000111"))


class TestPrecisaCompletar(_Base):
    def test_situacoes(self):
        self.assertFalse(cad.precisa_completar(self.conn, CNPJ))
        cad.registrar(self.conn, CNPJ, 100, cad.CONVIDADO, agora=AGORA)
        self.assertTrue(cad.precisa_completar(self.conn, CNPJ))
        for situacao, esperado in ((cad.REENVIADO, True), (cad.SEM_CONTATO, True), (cad.PENDENTE, True),
                                   (cad.PREENCHIDO, False), (cad.APROVADO, False), (cad.INATIVO, False)):
            cad.atualizar(self.conn, CNPJ, AGORA, situacao=situacao)
            self.assertEqual(cad.precisa_completar(self.conn, CNPJ), esperado, situacao)


class TestPreenchimentoEAprovacao(_Base):
    def setUp(self):
        super().setUp()
        cad.registrar(self.conn, CNPJ, 100, cad.CONVIDADO, agora=AGORA)

    def test_preencher_nao_mexe_em_interno(self):
        c = cad.salvar_preenchimento(self.conn, CNPJ, " refrigerado ", agora=AGORA)
        self.assertEqual((c["situacao"], c["habilidade_informada"]), ("PREENCHIDO", "Refrigerado"))
        self.assertIsNone(self.habilidade_interno())

    def test_tipo_invalido(self):
        with self.assertRaises(cad.CadastroInvalido):
            cad.salvar_preenchimento(self.conn, CNPJ, "Quente", agora=AGORA)
        self.assertEqual(cad.buscar(self.conn, CNPJ)["situacao"], "CONVIDADO")

    def test_aprovar_grava_habilidade_e_quem(self):
        cad.salvar_preenchimento(self.conn, CNPJ, "Seco", agora=AGORA)
        c = cad.aprovar(self.conn, CNPJ, "hugo", agora=AGORA)
        self.assertEqual((c["situacao"], c["aprovado_por"]), ("APROVADO", "hugo"))
        self.assertEqual(self.habilidade_interno(), "Seco")

    def test_aprovar_com_outro_tipo(self):
        cad.salvar_preenchimento(self.conn, CNPJ, "Seco", agora=AGORA)
        c = cad.aprovar(self.conn, CNPJ, "hugo", habilidade="congelado", agora=AGORA)
        self.assertEqual((c["habilidade_informada"], self.habilidade_interno()), ("Congelado", "Congelado"))

    def test_aprovar_sem_preenchimento_da_409(self):
        with self.assertRaises(cad.CadastroInvalido) as ctx:
            cad.aprovar(self.conn, CNPJ, "hugo", agora=AGORA)
        self.assertEqual(ctx.exception.codigo, 409)
        self.assertIsNone(self.habilidade_interno())

    def test_aprovar_inexistente_da_404(self):
        with self.assertRaises(cad.CadastroInvalido) as ctx:
            cad.aprovar(self.conn, "11111111000111", "hugo", habilidade="Seco")
        self.assertEqual(ctx.exception.codigo, 404)

    def test_listar_abertos_exclui_aprovado_e_inativo_e_poe_preenchido_primeiro(self):
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, stkkc_id) VALUES ('22222222000122', 'BETA', 5)")
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, stkkc_id) VALUES ('33333333000133', 'GAMA', 6)")
        cad.registrar(self.conn, "22222222000122", 5, cad.PREENCHIDO, agora=AGORA)
        cad.registrar(self.conn, "33333333000133", 6, cad.INATIVO, agora=AGORA)
        nomes = [(l["nome"], l["situacao"]) for l in cad.listar_abertos(self.conn)]
        self.assertEqual(nomes, [("BETA", "PREENCHIDO"), ("FABENNE", "CONVIDADO")])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m unittest test_cadastro_embarcador`
Expected: FAIL com `ModuleNotFoundError: No module named 'cadastro_embarcador'`

- [ ] **Step 3: Write minimal implementation**

`cadastro_embarcador.py`:

```python
# -*- coding: utf-8 -*-
"""
cadastro_embarcador.py

Andamento do pre-cadastro automatico de embarcador novo da Stokki (pedido do
Hugo, 07/10/2026; spec em
docs/superpowers/specs/2026-10-08-embarcadores-novos-stokki-design.md).

Uma linha por CNPJ. Quem escreve:
- embarcadores_stokki.py (rotina de hora em hora): cria e convida;
- portal (tela /completar-cadastro): o cliente informa o tipo de carga;
- painel /inicio e gerenciar_clientes.py cadastro: aprovacao.

interno.habilidade so muda em aprovar(): o tipo informado pelo cliente nao
vale no roteirizador antes disso.
"""
import re
from datetime import datetime

INATIVO = "INATIVO"          # sem pedido nos ultimos 30 dias: sem convite
PENDENTE = "PENDENTE"        # ativo, ainda sem sender ou sem convite
CONVIDADO = "CONVIDADO"
REENVIADO = "REENVIADO"
SEM_CONTATO = "SEM_CONTATO"  # nem WhatsApp valido nem e-mail na Stokki
PREENCHIDO = "PREENCHIDO"    # cliente informou, falta a aprovacao
APROVADO = "APROVADO"

ABERTOS_PRO_CLIENTE = (PENDENTE, CONVIDADO, REENVIADO, SEM_CONTATO)
HABILIDADES = ("Seco", "Refrigerado", "Congelado")
DESCRICOES_HABILIDADE = {
    "Seco": "Temperatura ambiente: bebidas, mercearia, embalagens.",
    "Refrigerado": "Precisa de geladeira: laticínios, frios, hortifrúti.",
    "Congelado": "Precisa ficar congelado: sorvetes, carnes e massas congeladas.",
}

_DDL = """
CREATE TABLE IF NOT EXISTS cadastro_embarcador (
    cnpj_embarcador      TEXT PRIMARY KEY,
    stkkc_id             INTEGER NOT NULL,
    celular_stokki       TEXT NOT NULL DEFAULT '',
    email_stokki         TEXT NOT NULL DEFAULT '',
    situacao             TEXT NOT NULL,
    canal_convite        TEXT,
    convite_enviado_em   TEXT,
    reenvio_em           TEXT,
    habilidade_informada TEXT,
    preenchido_em        TEXT,
    aprovado_em          TEXT,
    aprovado_por         TEXT,
    criado_em            TEXT NOT NULL,
    atualizado_em        TEXT NOT NULL
)"""

_EDITAVEIS = ("stkkc_id", "celular_stokki", "email_stokki", "situacao", "canal_convite", "convite_enviado_em",
              "reenvio_em", "habilidade_informada", "preenchido_em", "aprovado_em", "aprovado_por")


class CadastroInvalido(ValueError):
    def __init__(self, mensagem: str, codigo: int = 400):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.codigo = codigo


def _cnpj(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _fmt(agora: datetime | None) -> str:
    return (agora or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _como_dicts(cur) -> list[dict]:
    colunas = [d[0] for d in cur.description]
    return [dict(zip(colunas, tuple(r))) for r in cur.fetchall()]


def garantir_tabela(conn) -> None:
    conn.execute(_DDL)


def buscar(conn, cnpj) -> dict | None:
    garantir_tabela(conn)
    linhas = _como_dicts(conn.execute("SELECT * FROM cadastro_embarcador WHERE cnpj_embarcador = ?", (_cnpj(cnpj),)))
    return linhas[0] if linhas else None


def registrar(conn, cnpj, stkkc_id: int, situacao: str, celular: str = "", email: str = "",
              agora: datetime | None = None) -> dict:
    """Cria a linha. Ja existindo, nao mexe (a rotina pode passar duas vezes)."""
    garantir_tabela(conn)
    momento = _fmt(agora)
    conn.execute(
        "INSERT OR IGNORE INTO cadastro_embarcador (cnpj_embarcador, stkkc_id, celular_stokki, email_stokki, situacao, "
        "criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_cnpj(cnpj), int(stkkc_id), str(celular or ""), str(email or ""), situacao, momento, momento))
    conn.commit()
    return buscar(conn, cnpj)


def atualizar(conn, cnpj, agora: datetime | None = None, **campos) -> dict:
    desconhecidos = [c for c in campos if c not in _EDITAVEIS]
    if desconhecidos:
        raise ValueError(f"Campo desconhecido em cadastro_embarcador: {', '.join(desconhecidos)}")
    garantir_tabela(conn)
    if campos:
        atribuicoes = ", ".join(f"{c} = ?" for c in campos)
        conn.execute(f"UPDATE cadastro_embarcador SET {atribuicoes}, atualizado_em = ? WHERE cnpj_embarcador = ?",
                     (*campos.values(), _fmt(agora), _cnpj(cnpj)))
        conn.commit()
    return buscar(conn, cnpj)


def normalizar_habilidade(valor) -> str:
    tipo = str(valor or "").strip().capitalize()
    if tipo not in HABILIDADES:
        raise CadastroInvalido("Escolha o tipo de carga: Seco, Refrigerado ou Congelado.")
    return tipo


def precisa_completar(conn, cnpj) -> bool:
    c = buscar(conn, cnpj)
    return bool(c and c["situacao"] in ABERTOS_PRO_CLIENTE)


def salvar_preenchimento(conn, cnpj, habilidade, agora: datetime | None = None) -> dict:
    tipo = normalizar_habilidade(habilidade)
    c = buscar(conn, cnpj)
    if not c:
        raise CadastroInvalido("Cadastro não encontrado.", 404)
    if c["situacao"] == APROVADO:
        raise CadastroInvalido("Cadastro já aprovado.", 409)
    return atualizar(conn, cnpj, agora, habilidade_informada=tipo, preenchido_em=_fmt(agora), situacao=PREENCHIDO)


def aprovar(conn, cnpj, por: str, habilidade: str | None = None, agora: datetime | None = None) -> dict:
    """Grava interno.habilidade. Sem `habilidade`, usa o que o cliente
    informou (exige PREENCHIDO); com ela, a equipe corrige o tipo."""
    c = buscar(conn, cnpj)
    if not c:
        raise CadastroInvalido("Cadastro não encontrado.", 404)
    if habilidade is None:
        if c["situacao"] != PREENCHIDO:
            raise CadastroInvalido("O cliente ainda não informou o tipo de carga.", 409)
        tipo = c["habilidade_informada"]
    else:
        tipo = normalizar_habilidade(habilidade)
    cur = conn.execute("UPDATE interno SET habilidade = ?, atualizado_em = ? WHERE cnpj_embarcador = ?",
                       (tipo, _fmt(agora), c["cnpj_embarcador"]))
    if cur.rowcount == 0:
        conn.rollback()
        raise CadastroInvalido("Embarcador não está em interno.", 404)
    return atualizar(conn, cnpj, agora, situacao=APROVADO, aprovado_em=_fmt(agora),
                     aprovado_por=str(por or "")[:40], habilidade_informada=tipo)


def listar_abertos(conn) -> list[dict]:
    """Tudo que nao esta APROVADO nem INATIVO, PREENCHIDO primeiro."""
    garantir_tabela(conn)
    return _como_dicts(conn.execute("""
        SELECT c.*, COALESCE(i.apelido, i.nome_remetente, c.cnpj_embarcador) AS nome
        FROM cadastro_embarcador c LEFT JOIN interno i ON i.cnpj_embarcador = c.cnpj_embarcador
        WHERE c.situacao NOT IN ('APROVADO', 'INATIVO')
        ORDER BY CASE c.situacao WHEN 'PREENCHIDO' THEN 0 ELSE 1 END, c.criado_em, c.cnpj_embarcador
    """))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.11 -m unittest test_cadastro_embarcador`
Expected: PASS (12 testes)

- [ ] **Step 5: Commit**

```bash
git add cadastro_embarcador.py test_cadastro_embarcador.py
git commit -m "Cadastro de embarcador: tabela e estados do pre-cadastro automatico" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: leitura da Stokki (funções puras de `embarcadores_stokki.py`)

**Files:**
- Create: `embarcadores_stokki.py`
- Test: `test_embarcadores_stokki.py`

**Interfaces:**
- Consumes: `preferencias_notificacao.normalizar_whatsapp(valor) -> str` (levanta `ValueError`).
- Produces:
  - `interpretar_rotulo(select_label: str, name: str) -> tuple[str | None, str, str]` = (apelido ou None, razão social, CNPJ só dígitos ou "")
  - `clientes_do_select(bruto) -> list[dict]` com chaves `stkkc_id: int, apelido: str | None, razao: str, cnpj: str`
  - `extrair_contato(html: str) -> dict` com chaves `celular, telefone, email, cidade` (str)
  - `celular_whatsapp(valor) -> str` ("55DDDNUMERO" ou "")

- [ ] **Step 1: Write the failing test**

`test_embarcadores_stokki.py` (arquivo novo; as Tasks 3 a 5 acrescentam classes nele):

```python
# -*- coding: utf-8 -*-
"""Rotina de embarcadores novos da Stokki (embarcadores_stokki.py). Sem
rede: Stokki, Vuupt, OpenWA e e-mail substituidos.

Rodar: py -3.11 -m unittest test_embarcadores_stokki
"""
import unittest

import embarcadores_stokki as rot

HTML_EDIT = '''<form><input type="text" class="form-control" name="name" value="FABENNE">
<input type="text" name="cellphone" class="form-control phone" value="(11) 999 999 999">
<input type="text" name="phone" class="form-control phone"  value="" >
<input type="email" name="email" value="Mariana@Fabenne.com">
<input type="text" name="company_phone" value="(11) 7646 5184">
<input type="text" name="company_email[]" value="mariana@fabenne.com">
<input type="text" name="city" value="S&atilde;o Paulo"></form>'''


class TestRotulo(unittest.TestCase):
    def test_com_apelido(self):
        self.assertEqual(
            rot.interpretar_rotulo("TERRA NUTRI (RIC INDUSTRIA E COMERCIO LTDA - 02.368.118/0006-07)",
                                   "RIC INDUSTRIA E COMERCIO LTDA"),
            ("TERRA NUTRI", "RIC INDUSTRIA E COMERCIO LTDA", "02368118000607"))

    def test_sem_apelido_quando_repete_a_razao(self):
        self.assertEqual(
            rot.interpretar_rotulo("COGUMELADO ALIMENTOS LTDA (COGUMELADO ALIMENTOS LTDA - 29.909.190/0001-46)",
                                   "COGUMELADO ALIMENTOS LTDA"),
            (None, "COGUMELADO ALIMENTOS LTDA", "29909190000146"))

    def test_parenteses_dentro_da_razao(self):
        self.assertEqual(
            rot.interpretar_rotulo("PADOCA (PADARIA (FILIAL 2) LTDA - 11.111.111/0001-11)", "PADARIA (FILIAL 2) LTDA"),
            ("PADOCA", "PADARIA (FILIAL 2) LTDA", "11111111000111"))

    def test_fora_do_padrao_fica_sem_cnpj(self):
        self.assertEqual(rot.interpretar_rotulo("QUALQUER COISA", " X LTDA "), (None, "X LTDA", ""))

    def test_clientes_do_select(self):
        bruto = {"success": True, "units": [], "clients": [
            {"id": 100, "name": "NONNA DISTRIBUICAO E COMERCIO DIGITAL LTDA",
             "select_label": "FABENNE (NONNA DISTRIBUICAO E COMERCIO DIGITAL LTDA - 49.874.051/0001-31)"}]}
        self.assertEqual(rot.clientes_do_select(bruto), [
            {"stkkc_id": 100, "apelido": "FABENNE", "razao": "NONNA DISTRIBUICAO E COMERCIO DIGITAL LTDA",
             "cnpj": "49874051000131"}])
        self.assertEqual(rot.clientes_do_select({"clients": []}), [])


class TestContato(unittest.TestCase):
    def test_extrai_da_tela_de_edicao(self):
        self.assertEqual(rot.extrair_contato(HTML_EDIT), {
            "celular": "(11) 999 999 999", "telefone": "(11) 7646 5184",
            "email": "mariana@fabenne.com", "cidade": "São Paulo"})

    def test_html_vazio(self):
        self.assertEqual(rot.extrair_contato(""), {"celular": "", "telefone": "", "email": "", "cidade": ""})

    def test_celular_whatsapp(self):
        casos = {"(11) 999 999 999": "", "(11) 98888-7777": "5511988887777", "": "",
                 "(11) 7646 5184": "", "+55 21 99876-5432": "5521998765432", "abc": ""}
        for entrada, esperado in casos.items():
            self.assertEqual(rot.celular_whatsapp(entrada), esperado, entrada)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m unittest test_embarcadores_stokki`
Expected: FAIL com `ModuleNotFoundError: No module named 'embarcadores_stokki'`

- [ ] **Step 3: Write minimal implementation**

`embarcadores_stokki.py` (início do arquivo; as próximas tarefas acrescentam funções abaixo):

```python
# -*- coding: utf-8 -*-
"""
embarcadores_stokki.py

Embarcador novo na Stokki vira pre-cadastro em `interno` e recebe convite
pra completar o cadastro no portal (pedido do Hugo, 07/10/2026; spec em
docs/superpowers/specs/2026-10-08-embarcadores-novos-stokki-design.md).

Roda de hora em hora, 07h-19h (infra/stokki-embarcadores-novos.timer):
1. le o select de clientes da Stokki e compara com interno.stkkc_id;
2. novo: linha em interno (sem habilidade) + cadastro_embarcador;
3. ativo (pedido nos ultimos 30 dias): sender da Vuupt (o do pedido mais
   antigo no nucleo, ou cria o customer) e convite: WhatsApp do celular da
   Stokki, senao e-mail, senao so aviso no grupo;
4. convite sem PIN depois de 3 dias: reenvia uma vez.

Envio ao cliente preso por padrao: WhatsApp em
whatsapp_notificacoes.clientes.forcar_destino; e-mail em
cadastro_embarcador.forcar_destino_email (ausente = hugo@).

Uso:
    python embarcadores_stokki.py [--modo-teste]
"""
import argparse
import html as html_lib
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import auth_cliente as auth  # noqa: E402  (portal_cliente/auth_cliente.py)
import cadastro_embarcador as cad  # noqa: E402
import notificar_whatsapp as nw  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402

logger = logging.getLogger("embarcadores_stokki")

_RE_ROTULO = re.compile(r"^(?P<apelido>.*?)\s*\((?P<razao>.*)\s+-\s+(?P<cnpj>[\d./-]{14,18})\)\s*$")


def interpretar_rotulo(select_label: str, name: str) -> tuple[str | None, str, str]:
    """'APELIDO (RAZAO - 00.000.000/0000-00)' -> (apelido, razao, cnpj).
    Apelido igual a razao vira None. Fora do padrao: (None, name, '')."""
    m = _RE_ROTULO.match(str(select_label or "").strip())
    if not m:
        return None, str(name or "").strip(), ""
    razao = m.group("razao").strip()
    apelido = m.group("apelido").strip()
    cnpj = re.sub(r"\D", "", m.group("cnpj"))
    if len(cnpj) != 14:
        return None, razao or str(name or "").strip(), ""
    if not apelido or apelido.upper() == razao.upper():
        apelido = None
    return apelido, razao, cnpj


def clientes_do_select(bruto) -> list[dict]:
    """Resposta de stokki.pedidos.listar_embarcadores -> lista de clientes."""
    itens = bruto.get("clients", []) if isinstance(bruto, dict) else (bruto or [])
    clientes = []
    for c in itens:
        apelido, razao, cnpj = interpretar_rotulo(c.get("select_label", ""), c.get("name", ""))
        clientes.append({"stkkc_id": int(c["id"]), "apelido": apelido, "razao": razao, "cnpj": cnpj})
    return clientes


def _valor_input(html: str, nome: str) -> str:
    m = re.search(r'<input[^>]*\bname="%s"[^>]*>' % re.escape(nome), html or "")
    if not m:
        return ""
    v = re.search(r'\bvalue="([^"]*)"', m.group(0))
    return html_lib.unescape(v.group(1)).strip() if v else ""


def extrair_contato(html: str) -> dict:
    """Tela /administrator/client/edit/{id} da Stokki -> contato. Pessoa
    primeiro, empresa como reserva."""
    return {
        "celular": _valor_input(html, "cellphone") or _valor_input(html, "company_cellphone"),
        "telefone": _valor_input(html, "phone") or _valor_input(html, "company_phone"),
        "email": (_valor_input(html, "email") or _valor_input(html, "company_email[]")).lower(),
        "cidade": _valor_input(html, "city"),
    }


def celular_whatsapp(valor) -> str:
    """Numero que vale tentar no WhatsApp, ou ''. Recusa invalido e numero de
    mentira com o mesmo digito repetido, ex. (11) 999 999 999."""
    try:
        numero = pn.normalizar_whatsapp(valor)
    except ValueError:
        return ""
    if not numero or len(set(numero[4:])) == 1:
        return ""
    return numero
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.11 -m unittest test_embarcadores_stokki`
Expected: PASS (8 testes)

- [ ] **Step 5: Commit**

```bash
git add embarcadores_stokki.py test_embarcadores_stokki.py
git commit -m "Embarcadores Stokki: leitura do select e do contato do cliente" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: detecção, sender e pré-cadastro

**Files:**
- Modify: `embarcadores_stokki.py` (acrescentar abaixo de `celular_whatsapp`)
- Test: `test_embarcadores_stokki.py` (acrescentar `_ComBanco` e `TestDeteccao`)

**Interfaces:**
- Consumes: `cad.registrar`, `cad.PENDENTE`, `cad.INATIVO` (Task 1); `auth.conectar()`, `auth.formatar_cnpj(digitos)`; `VuuptClient.criar_customer(dados) -> dict` (já existe, devolve `{"customer": {"id": ...}}`).
- Produces:
  - `DIAS_ATIVO = 30`
  - `_fmt(agora: datetime) -> str`
  - `tem_pedido_recente(conn, cnpj, agora, dias=DIAS_ATIVO) -> bool`
  - `sender_do_nucleo(conn, cnpj) -> int | None`
  - `resolver_sender(conn, cnpj, razao, vuupt) -> int | None` (`vuupt=None` nunca cria)
  - `separar(clientes, conn) -> dict` com `novos: list[dict]`, `sem_stkkc: list[dict]`, `conflitos: list[tuple[dict, int]]`, `ignorados: list[dict]`
  - `pre_cadastrar(conn, cli: dict, contato: dict, ativo: bool, agora: datetime) -> None`

- [ ] **Step 1: Conferir o esquema real de `interno` na VPS (só leitura)**

Run:
```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python -c \"import sqlite3; c=sqlite3.connect('dados/dados.db'); print([(r[1], r[2], r[3], r[4]) for r in c.execute('PRAGMA table_info(interno)')])\""
```
Expected: só `cnpj_embarcador` como chave e nenhuma coluna `NOT NULL` sem default além das que o `INSERT` do Step 4 preenche. Se aparecer outra, acrescente-a ao `INSERT` de `pre_cadastrar` e ao `DDL_INTERNO` do teste.

- [ ] **Step 2: Write the failing test**

Acrescente em `test_embarcadores_stokki.py`, depois dos imports existentes:

```python
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import cadastro_embarcador as cad
import notificar_whatsapp as nw

DDL_INTERNO = ("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, habilidade TEXT, nome_remetente TEXT, "
               "apelido TEXT, fator_ponderado REAL, observacoes TEXT, criado_em TEXT, atualizado_em TEXT, "
               "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
DDL_NUCLEO = ("CREATE TABLE nucleo_pedidos (codigo TEXT PRIMARY KEY, remetente_codigo TEXT, sender_id INTEGER, "
              "criado_em TEXT)")
AGORA = datetime(2026, 10, 10, 9, 10, 0)
COGUMELADO = {"stkkc_id": 7, "apelido": "COGUMELADO", "razao": "COGUMELADO ALIMENTOS LTDA", "cnpj": "29909190000146"}
NONNA = {"stkkc_id": 100, "apelido": "FABENNE", "razao": "NONNA DISTRIBUICAO E COMERCIO DIGITAL LTDA",
         "cnpj": "49874051000131"}
RIC = {"stkkc_id": 56, "apelido": "TERRA NUTRI", "razao": "RIC INDUSTRIA E COMERCIO LTDA", "cnpj": "02368118000607"}
ANTIGO = {"stkkc_id": 23, "apelido": None, "razao": "ANTIGO COMERCIO LTDA", "cnpj": "33333333000133"}
CONTATOS = {
    100: '<input name="cellphone" value="(11) 98888-7777"><input name="email" value="mariana@fabenne.com">',
    56: '<input name="cellphone" value="(11) 999 999 999"><input name="email" value="contato@ric.com">',
}


def _config(**extra):
    c = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s",
                                   "grupo_id": "1@g.us", "teto_diario": 50, "intervalo_min_seg": 0,
                                   "clientes": {"ativo": True, "teto_diario": 30, "forcar_destino": ""}},
         "portal_cliente": {"url_base": "https://app.freshhub.com.br/cliente", "secret_key": "segredo-de-teste"},
         "cadastro_embarcador": {"forcar_destino_email": ""}, "email": {}}
    c.update(extra)
    return c


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        mock.patch.object(rot.auth, "DB_PATH", Path(self._tmp.name) / "t.db").start()
        self.enviar = mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1")).start()
        self.existe = mock.patch.object(nw.integracao_openwa, "numero_existe", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.conn = rot.auth.conectar()
        self.addCleanup(self.conn.close)
        self.conn.execute(DDL_INTERNO)
        self.conn.execute(DDL_NUCLEO)
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, apelido, sender_id, stkkc_id) "
                          "VALUES ('29909190000146', 'COGUMELADO ALIMENTOS LTDA', 'COGUMELADO', 21000001, 7)")
        self.conn.commit()
        self.email = mock.MagicMock(return_value=True)
        self.dormir = mock.MagicMock()

    def pedido(self, codigo, cnpj_formatado, sender, dias_atras):
        quando = (AGORA - timedelta(days=dias_atras)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES (?, ?, ?, ?)", (codigo, cnpj_formatado, sender, quando))
        self.conn.commit()

    def interno(self, cnpj):
        cur = self.conn.execute("SELECT * FROM interno WHERE cnpj_embarcador = ?", (cnpj,))
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], tuple(row))) if row else None


class TestDeteccao(_ComBanco):
    def test_separar(self):
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, stkkc_id) VALUES ('02368118000607', 'RIC', NULL)")
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, stkkc_id) VALUES ('33333333000133', 'OUTRO', 99)")
        sem_cnpj = {"stkkc_id": 70, "apelido": None, "razao": "SEM CNPJ", "cnpj": ""}
        g = rot.separar([COGUMELADO, NONNA, RIC, ANTIGO, sem_cnpj], self.conn)
        self.assertEqual([c["stkkc_id"] for c in g["novos"]], [100])
        self.assertEqual([c["stkkc_id"] for c in g["sem_stkkc"]], [56])
        self.assertEqual([(c["stkkc_id"], outro) for c, outro in g["conflitos"]], [(23, 99)])
        self.assertEqual([c["stkkc_id"] for c in g["ignorados"]], [70])

    def test_pedido_recente_compara_cnpj_formatado_e_30_dias(self):
        self.pedido("PS-1", "49.874.051/0001-31", 23084712, 29)
        self.pedido("PS-2", "02.368.118/0006-07", 22872019, 31)
        self.assertTrue(rot.tem_pedido_recente(self.conn, NONNA["cnpj"], AGORA))
        self.assertFalse(rot.tem_pedido_recente(self.conn, RIC["cnpj"], AGORA))

    def test_pedido_recente_sem_tabela_do_nucleo(self):
        self.conn.execute("DROP TABLE nucleo_pedidos")
        self.assertFalse(rot.tem_pedido_recente(self.conn, NONNA["cnpj"], AGORA))
        self.assertIsNone(rot.sender_do_nucleo(self.conn, NONNA["cnpj"]))

    def test_sender_do_pedido_mais_antigo(self):
        self.pedido("PS-2", "49.874.051/0001-31", 23084712, 1)
        self.pedido("PS-1", "49.874.051/0001-31", 23072631, 5)
        self.pedido("PS-0", "49.874.051/0001-31", None, 9)
        vuupt = mock.MagicMock()
        self.assertEqual(rot.resolver_sender(self.conn, NONNA["cnpj"], NONNA["razao"], vuupt), 23072631)
        vuupt.criar_customer.assert_not_called()

    def test_sem_pedido_cria_customer(self):
        vuupt = mock.MagicMock()
        vuupt.criar_customer.return_value = {"customer": {"id": 555}}
        self.assertEqual(rot.resolver_sender(self.conn, ANTIGO["cnpj"], ANTIGO["razao"], vuupt), 555)
        vuupt.criar_customer.assert_called_once_with({"name": "ANTIGO COMERCIO LTDA", "code": "33.333.333/0001-33"})

    def test_falha_ou_sem_vuupt_devolve_none(self):
        vuupt = mock.MagicMock()
        vuupt.criar_customer.side_effect = RuntimeError("422")
        self.assertIsNone(rot.resolver_sender(self.conn, ANTIGO["cnpj"], ANTIGO["razao"], vuupt))
        self.assertIsNone(rot.resolver_sender(self.conn, ANTIGO["cnpj"], ANTIGO["razao"], None))

    def test_pre_cadastrar_ativo_e_inativo(self):
        rot.pre_cadastrar(self.conn, NONNA, rot.extrair_contato(CONTATOS[100]), True, AGORA)
        rot.pre_cadastrar(self.conn, ANTIGO, rot.extrair_contato(""), False, AGORA)
        n = self.interno(NONNA["cnpj"])
        self.assertEqual((n["stkkc_id"], n["nome_remetente"], n["apelido"], n["email"], n["habilidade"],
                          n["notificar_email"], n["sender_id"], n["fator_ponderado"]),
                         (100, NONNA["razao"], "FABENNE", "mariana@fabenne.com", None, 1, None, 1.0))
        self.assertIn("Pré-cadastro automático 10/10/2026 (Stokki #stkkc-100)", n["observacoes"])
        a = self.interno(ANTIGO["cnpj"])
        self.assertEqual((a["notificar_email"], a["email"]), (0, None))
        self.assertIn("inativo", a["observacoes"])
        self.assertEqual(cad.buscar(self.conn, NONNA["cnpj"])["situacao"], "PENDENTE")
        self.assertEqual(cad.buscar(self.conn, NONNA["cnpj"])["celular_stokki"], "(11) 98888-7777")
        self.assertEqual(cad.buscar(self.conn, ANTIGO["cnpj"])["situacao"], "INATIVO")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `py -3.11 -m unittest test_embarcadores_stokki`
Expected: FAIL com `AttributeError: module 'embarcadores_stokki' has no attribute 'separar'` (e as outras funções novas)

- [ ] **Step 4: Write minimal implementation**

Acrescente em `embarcadores_stokki.py`:

```python
DIAS_ATIVO = 30
_SQL_CNPJ_REMETENTE = "REPLACE(REPLACE(REPLACE(remetente_codigo, '.', ''), '/', ''), '-', '')"


def _fmt(agora: datetime) -> str:
    return agora.strftime("%Y-%m-%d %H:%M:%S")


def _tabela_existe(conn, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nome,)).fetchone() is not None


def tem_pedido_recente(conn, cnpj: str, agora: datetime, dias: int = DIAS_ATIVO) -> bool:
    if not _tabela_existe(conn, "nucleo_pedidos"):
        return False
    limite = _fmt(agora - timedelta(days=dias))
    return conn.execute(f"SELECT 1 FROM nucleo_pedidos WHERE {_SQL_CNPJ_REMETENTE} = ? AND criado_em >= ? LIMIT 1",
                        (cnpj, limite)).fetchone() is not None


def sender_do_nucleo(conn, cnpj: str) -> int | None:
    """Sender do pedido mais antigo desse remetente: e o que a Vuupt criou
    primeiro; os pedidos seguintes passam a apontar pra ele via interno."""
    if not _tabela_existe(conn, "nucleo_pedidos"):
        return None
    row = conn.execute(f"SELECT sender_id FROM nucleo_pedidos WHERE {_SQL_CNPJ_REMETENTE} = ? AND sender_id IS NOT NULL "
                       "ORDER BY criado_em, codigo LIMIT 1", (cnpj,)).fetchone()
    return int(row[0]) if row else None


def resolver_sender(conn, cnpj: str, razao: str, vuupt) -> int | None:
    sender = sender_do_nucleo(conn, cnpj)
    if sender or vuupt is None:
        return sender
    try:
        resposta = vuupt.criar_customer({"name": razao, "code": auth.formatar_cnpj(cnpj)})
    except Exception as exc:
        logger.warning(f"Falha ao criar o remetente de {cnpj} na Vuupt: {exc}")
        return None
    dados = resposta.get("customer", resposta) if isinstance(resposta, dict) else {}
    return int(dados["id"]) if isinstance(dados, dict) and dados.get("id") else None


def separar(clientes: list[dict], conn) -> dict:
    """novos: CNPJ fora de interno; sem_stkkc: CNPJ em interno sem stkkc_id
    (cadastro manual antigo); conflitos: CNPJ em interno com outro stkkc_id;
    ignorados: rotulo sem CNPJ."""
    por_stkkc, por_cnpj = set(), {}
    for cnpj, stkkc in conn.execute("SELECT cnpj_embarcador, stkkc_id FROM interno").fetchall():
        if stkkc is not None:
            por_stkkc.add(int(stkkc))
        por_cnpj[re.sub(r"\D", "", str(cnpj or ""))] = stkkc
    g = {"novos": [], "sem_stkkc": [], "conflitos": [], "ignorados": []}
    for cli in clientes:
        if cli["stkkc_id"] in por_stkkc:
            continue
        if not cli["cnpj"]:
            g["ignorados"].append(cli)
        elif cli["cnpj"] not in por_cnpj:
            g["novos"].append(cli)
        elif por_cnpj[cli["cnpj"]] is None:
            g["sem_stkkc"].append(cli)
        else:
            g["conflitos"].append((cli, int(por_cnpj[cli["cnpj"]])))
    return g


def pre_cadastrar(conn, cli: dict, contato: dict, ativo: bool, agora: datetime) -> None:
    momento = _fmt(agora)
    obs = f"Pré-cadastro automático {agora:%d/%m/%Y} (Stokki #stkkc-{cli['stkkc_id']})"
    if not ativo:
        obs += ", inativo: sem pedido nos últimos 30 dias"
    conn.execute(
        "INSERT INTO interno (cnpj_embarcador, stkkc_id, nome_remetente, apelido, email, fator_ponderado, habilidade, "
        "notificar_email, observacoes, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, 1.0, NULL, ?, ?, ?, ?)",
        (cli["cnpj"], cli["stkkc_id"], cli["razao"], cli["apelido"], contato["email"] or None,
         1 if ativo else 0, obs, momento, momento))
    conn.commit()
    cad.registrar(conn, cli["cnpj"], cli["stkkc_id"], cad.PENDENTE if ativo else cad.INATIVO,
                  celular=contato["celular"], email=contato["email"], agora=agora)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `py -3.11 -m unittest test_embarcadores_stokki`
Expected: PASS (15 testes)

- [ ] **Step 6: Commit**

```bash
git add embarcadores_stokki.py test_embarcadores_stokki.py
git commit -m "Embarcadores Stokki: deteccao do cliente novo, sender da Vuupt e pre-cadastro em interno" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: convite, reenvio e avisos no grupo

**Files:**
- Modify: `notificar_whatsapp.py` (nova função logo depois de `avisar_cliente_fora_dia_fixo`)
- Modify: `embarcadores_stokki.py`
- Test: `test_embarcadores_stokki.py` (acrescentar `_ComCadastro` e `TestConvite`)

**Interfaces:**
- Consumes: `nw.clientes_ligado`, `nw._cfg`, `nw._cfg_clientes`, `nw.despachar(config, origem, tipo, texto, assinatura=None, modo_teste=False, conn=None, agora=None, dormir=..., grupo_id=None) -> str`, `nw.ORIGEM_CLIENTE`, `nw._SCHEMA`, `nw.MAX_MENSAGEM`, `integracao_openwa.numero_existe(cfg, numero) -> bool | None`; `auth.gerar_token_definir_pin(secret, conn, cnpj) -> str`; `pn.ler(conn, cnpj) -> dict`, `pn.salvar(conn, cnpj, emails, flags, por, whatsapp=None)`; `email_utils.enviar_email(destinatarios, assunto, corpo_html, config_email, cc=None) -> bool`, `email_utils.envelope_html(conteudo, rodape=...) -> str`; `cad.buscar`, `cad.atualizar`, `cad.CONVIDADO`, `cad.REENVIADO`, `cad.SEM_CONTATO` (Task 1); `celular_whatsapp`, `_fmt` (Tasks 2 e 3).
- Produces:
  - `nw.avisar_cliente_cadastro(cnpj, telefone, texto, assinatura, config, modo_teste=False, **kw) -> str` (situações: desligado | modo_teste | numero_sem_whatsapp | indeterminado | nao_enviado | enviado | falhou)
  - `DIAS_REENVIO = 3`, `ORIGEM_GRUPO = "embarcadores_stokki"`, `FORCAR_EMAIL_PADRAO = "hugo@freshlogbr.com"`
  - `link_convite(conn, config, cnpj) -> str`
  - `texto_convite(link) -> str`
  - `destino_email(config, email) -> tuple[list[str], bool]` (bool = é teste)
  - `enviar_convite_email(config, nome, cnpj, email, link, enviar=None) -> bool`
  - `avisar_grupo(conn, config, texto, assinatura, modo_teste=False, **kw) -> str` (devolve "repetido" se a assinatura já foi enviada)
  - `convidar(conn, config, cnpj, agora, modo_teste=False, enviar_email=None, reenvio=False, **kw) -> str` (whatsapp | email | nenhum | adiado)
  - `reenviar_vencidos(conn, config, agora, modo_teste=False, enviar_email=None, **kw) -> int`
  - textos do grupo: `texto_grupo_novo(nome, razao, canal)`, `texto_reenvio(nome)`, `texto_conflito(cli, outro)`, `texto_sem_sender(nome)`, `texto_inativos(n)`

- [ ] **Step 1: Write the failing test**

Acrescente em `test_embarcadores_stokki.py`:

```python
class _ComCadastro(_ComBanco):
    def preparar(self, cli, ativo=True, sender=23084712):
        rot.pre_cadastrar(self.conn, cli, rot.extrair_contato(CONTATOS.get(cli["stkkc_id"], "")), ativo, AGORA)
        if sender:
            self.conn.execute("UPDATE interno SET sender_id = ? WHERE cnpj_embarcador = ?", (sender, cli["cnpj"]))
            self.conn.commit()

    def convidar(self, cli, config=None, agora=AGORA, **kw):
        return rot.convidar(self.conn, config or _config(), cli["cnpj"], agora, enviar_email=self.email,
                            dormir=self.dormir, **kw)

    def grupo(self):
        return [c.args[2] for c in self.enviar.call_args_list if c.args[1] == "1@g.us"]


class TestConvite(_ComCadastro):
    def test_celular_valido_vai_por_whatsapp_e_guarda_o_numero(self):
        self.preparar(NONNA)
        self.assertEqual(self.convidar(NONNA), "whatsapp")
        chat, texto = self.enviar.call_args.args[1], self.enviar.call_args.args[2]
        self.assertEqual(chat, "5511988887777@c.us")
        self.assertIn("https://app.freshhub.com.br/cliente/definir-pin/", texto)
        self.assertLessEqual(len(rot.texto_convite("x")), 60)
        c = cad.buscar(self.conn, NONNA["cnpj"])
        self.assertEqual((c["situacao"], c["canal_convite"], c["convite_enviado_em"]),
                         ("CONVIDADO", "whatsapp", "2026-10-10 09:10:00"))
        self.assertEqual(rot.pn.ler(self.conn, NONNA["cnpj"])["whatsapp"], "5511988887777")
        self.email.assert_not_called()

    def test_celular_de_mentira_vai_por_email(self):
        self.preparar(RIC)
        self.assertEqual(self.convidar(RIC), "email")
        destinos, assunto, corpo = self.email.call_args.args[:3]
        self.assertEqual(destinos, ["contato@ric.com"])
        self.assertIn("/definir-pin/", corpo)
        self.assertEqual(cad.buscar(self.conn, RIC["cnpj"])["canal_convite"], "email")
        self.enviar.assert_not_called()

    def test_sem_forcar_configurado_o_email_vai_pro_hugo(self):
        self.preparar(RIC)
        config = _config()
        del config["cadastro_embarcador"]
        self.convidar(RIC, config=config)
        destinos, assunto, corpo = self.email.call_args.args[:3]
        self.assertEqual(destinos, ["hugo@freshlogbr.com"])
        self.assertIn("Convite pra repassar", corpo)
        self.assertIn("contato@ric.com", corpo)

    def test_numero_sem_whatsapp_cai_no_email(self):
        self.preparar(NONNA)
        self.existe.return_value = False
        self.assertEqual(self.convidar(NONNA), "email")
        self.assertEqual(self.email.call_args.args[0], ["mariana@fabenne.com"])

    def test_sem_contato(self):
        self.preparar(ANTIGO)
        self.assertEqual(self.convidar(ANTIGO), "nenhum")
        self.assertEqual(cad.buscar(self.conn, ANTIGO["cnpj"])["situacao"], "SEM_CONTATO")
        self.email.assert_not_called()
        self.enviar.assert_not_called()

    def test_gateway_sem_resposta_adia_sem_gravar(self):
        self.preparar(NONNA)
        self.existe.return_value = None
        self.assertEqual(self.convidar(NONNA), "adiado")
        self.assertEqual(cad.buscar(self.conn, NONNA["cnpj"])["situacao"], "PENDENTE")
        self.email.assert_not_called()

    def test_email_que_falha_adia(self):
        self.preparar(RIC)
        self.email.return_value = False
        self.assertEqual(self.convidar(RIC), "adiado")
        self.assertEqual(cad.buscar(self.conn, RIC["cnpj"])["situacao"], "PENDENTE")

    def test_modo_teste_nao_grava_nem_envia(self):
        self.preparar(NONNA)
        self.preparar(RIC, sender=22872019)
        self.assertEqual(self.convidar(NONNA, modo_teste=True), "whatsapp")
        self.assertEqual(self.convidar(RIC, modo_teste=True), "email")
        self.enviar.assert_not_called()
        self.email.assert_not_called()
        self.assertEqual(cad.buscar(self.conn, NONNA["cnpj"])["situacao"], "PENDENTE")
        self.assertEqual(rot.pn.ler(self.conn, NONNA["cnpj"])["whatsapp"], "")

    def test_reenvio_unico_depois_de_3_dias(self):
        self.preparar(NONNA)
        self.convidar(NONNA, agora=AGORA - timedelta(days=4))
        self.assertEqual(rot.reenviar_vencidos(self.conn, _config(), AGORA, enviar_email=self.email,
                                               dormir=self.dormir), 1)
        c = cad.buscar(self.conn, NONNA["cnpj"])
        self.assertEqual((c["situacao"], c["reenvio_em"]), ("REENVIADO", "2026-10-10 09:10:00"))
        self.assertEqual(rot.reenviar_vencidos(self.conn, _config(), AGORA + timedelta(days=5),
                                               enviar_email=self.email, dormir=self.dormir), 0)
        self.assertEqual(len(self.grupo()), 1)
        self.assertIn("reenviado", self.grupo()[0])

    def test_reenvio_pula_convite_recente_e_quem_ja_definiu_pin(self):
        self.preparar(NONNA)
        self.preparar(RIC, sender=22872019)
        self.convidar(NONNA, agora=AGORA - timedelta(days=2))
        self.convidar(RIC, agora=AGORA - timedelta(days=4))
        rot.auth.definir_pin(self.conn, RIC["cnpj"], "135790")
        self.assertEqual(rot.reenviar_vencidos(self.conn, _config(), AGORA, enviar_email=self.email,
                                               dormir=self.dormir), 0)

    def test_aviso_no_grupo_sai_uma_vez_por_assinatura(self):
        self.assertEqual(rot.avisar_grupo(self.conn, _config(), "oi", "novo:1", dormir=self.dormir), "enviado")
        self.assertEqual(rot.avisar_grupo(self.conn, _config(), "oi", "novo:1", dormir=self.dormir), "repetido")
        self.assertEqual(len(self.grupo()), 1)

    def test_whatsapp_com_forcar_destino_vai_pro_numero_do_teste(self):
        config = _config()
        config["whatsapp_notificacoes"]["clientes"]["forcar_destino"] = "5511000001111"
        r = nw.avisar_cliente_cadastro(NONNA["cnpj"], "5511988887777", "Fresh Log: x", "cadastro:1:convite", config,
                                       conn=self.conn, dormir=self.dormir)
        self.assertEqual(r, "enviado")
        self.assertEqual(self.enviar.call_args.args[1], "5511000001111@c.us")
        self.assertTrue(self.enviar.call_args.args[2].startswith("[teste → +5511988887777]"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m unittest test_embarcadores_stokki`
Expected: FAIL com `AttributeError` em `convidar`, `reenviar_vencidos`, `avisar_grupo` e `avisar_cliente_cadastro`

- [ ] **Step 3: Add the WhatsApp entry point in `notificar_whatsapp.py`**

Logo depois de `avisar_cliente_fora_dia_fixo` (mesmo molde, origem de cliente: divide o teto diário dos clientes e respeita `forcar_destino`):

```python
def avisar_cliente_cadastro(cnpj: str, telefone: str, texto: str, assinatura: str, config: dict,
                            modo_teste: bool = False, **kw) -> str:
    """Convite do pre-cadastro automatico (embarcadores_stokki.py): link pra
    completar o cadastro no portal. Quem chama garante uma tentativa por
    convite (assinatura cadastro:<cnpj>:convite|reenvio). Com
    clientes.forcar_destino a mensagem vai pra esse numero com o destino real
    na primeira linha."""
    try:
        telefone = re.sub(r"\D", "", str(telefone or ""))
        if not clientes_ligado(config) or not telefone:
            return "desligado"
        destino = re.sub(r"\D", "", str(_cfg_clientes(config).get("forcar_destino") or ""))
        if destino:
            texto = f"[teste → +{telefone}]\n{texto}"
        else:
            destino = telefone
        if not modo_teste:
            existe = integracao_openwa.numero_existe(_cfg(config), destino)
            if existe is None:
                logger.warning(f"WhatsApp nao enviado (cadastro {cnpj}): gateway nao confirmou o numero.")
                return "indeterminado"
            if not existe:
                logger.info(f"WhatsApp nao enviado (cadastro {cnpj}): numero sem WhatsApp.")
                return "numero_sem_whatsapp"
        return despachar(config, ORIGEM_CLIENTE, "cadastro", texto, assinatura, modo_teste=modo_teste,
                         grupo_id=f"{destino}@c.us", **kw)
    except Exception as exc:
        logger.warning(f"Falha na notificacao por WhatsApp (nao afeta a rotina): {exc}")
        return "falhou"
```

- [ ] **Step 4: Add the invitation logic in `embarcadores_stokki.py`**

```python
DIAS_REENVIO = 3
ORIGEM_GRUPO = "embarcadores_stokki"
FORCAR_EMAIL_PADRAO = "hugo@freshlogbr.com"
_DESFECHOS = {"whatsapp": "Convite por WhatsApp.", "email": "Sem WhatsApp válido, convite por e-mail.",
              "nenhum": "Sem contato na Stokki, cadastrar à mão."}


def link_convite(conn, config: dict, cnpj: str) -> str:
    portal = config.get("portal_cliente") or {}
    if not portal.get("secret_key"):
        raise RuntimeError("portal_cliente.secret_key ausente no config.yaml")
    url_base = (portal.get("url_base") or "https://app.freshhub.com.br/cliente").rstrip("/")
    return f"{url_base}/definir-pin/{auth.gerar_token_definir_pin(portal['secret_key'], conn, cnpj)}"


def texto_convite(link: str) -> str:
    return f"Fresh Log: complete o cadastro da sua empresa: {link}"


def texto_grupo_novo(nome: str, razao: str, canal: str) -> str:
    return f"Embarcador novo na Stokki: {nome} ({razao[:40]}). {_DESFECHOS[canal]}"


def texto_reenvio(nome: str) -> str:
    return f"Convite reenviado a {nome}: ainda sem cadastro depois de {DIAS_REENVIO} dias."


def texto_conflito(cli: dict, outro: int) -> str:
    return (f"Stokki #{cli['stkkc_id']} ({cli['apelido'] or cli['razao'][:30]}): CNPJ "
            f"{auth.formatar_cnpj(cli['cnpj'])} já está em interno com stkkc_id {outro}. Conferir.")


def texto_sem_sender(nome: str) -> str:
    return f"Embarcador novo na Stokki: {nome}. Não consegui o remetente na Vuupt; tento de novo na próxima hora."


def texto_inativos(n: int) -> str:
    return f"{n} embarcador(es) da Stokki sem pedido nos últimos {DIAS_ATIVO} dias entraram como inativos (sem convite)."


def destino_email(config: dict, email: str) -> tuple[list[str], bool]:
    """Ausente = Hugo; envio real so com forcar_destino_email: '' explicito."""
    forcar = str((config.get("cadastro_embarcador") or {}).get("forcar_destino_email", FORCAR_EMAIL_PADRAO) or "").strip()
    return ([forcar], True) if forcar else ([email], False)


def enviar_convite_email(config: dict, nome: str, cnpj: str, email: str, link: str, enviar=None) -> bool:
    from email_utils import envelope_html
    if enviar is None:
        from email_utils import enviar_email as enviar
    destinos, teste = destino_email(config, email)
    nome_html, cnpj_fmt = html_lib.escape(nome), auth.formatar_cnpj(cnpj)
    aviso = (f"<p style='background:#FEF3C7;border:1px solid #FCD34D;border-radius:7px;padding:10px 12px;font-size:13px'>"
             f"<b>Convite pra repassar.</b> Pré-cadastro automático de <b>{nome_html}</b> (CNPJ {cnpj_fmt}). "
             f"E-mail da Stokki: {html_lib.escape(email)}.</p>") if teste else ""
    corpo = envelope_html(
        aviso +
        f"<p>Olá, <strong>{nome_html}</strong>.</p>"
        f"<p>A Fresh Log abriu o cadastro da sua empresa (CNPJ {cnpj_fmt}). Falta pouco: defina seu PIN e informe "
        f"o tipo de carga e os contatos para os avisos de entrega.</p>"
        f"<p style='margin:24px 0'><a href='{link}' style='background:#0EA575;color:#fff;padding:12px 22px;"
        f"border-radius:7px;text-decoration:none;font-weight:700'>Completar cadastro</a></p>"
        f"<p style='color:#6B7280;font-size:13px'>O link vale por 24 horas e só funciona uma vez.</p>",
        rodape="Fresh Log · Portal de acompanhamento de entregas")
    assunto = (f"[Convite pra repassar] {nome} · pré-cadastro na Fresh Log" if teste
               else "Fresh Log · Complete o cadastro da sua empresa")
    return bool(enviar(destinos, assunto, corpo, config.get("email", {})))


def _ja_avisado(conn, assinatura: str) -> bool:
    conn.execute(nw._SCHEMA)
    return conn.execute("SELECT 1 FROM notificacoes_whatsapp WHERE origem = ? AND assinatura = ? "
                        "AND situacao = 'enviado' LIMIT 1", (ORIGEM_GRUPO, assinatura)).fetchone() is not None


def avisar_grupo(conn, config: dict, texto: str, assinatura: str, modo_teste: bool = False, **kw) -> str:
    """Grupo ALERTAS, uma vez por evento (a janela de 2 h do despachar nao basta)."""
    if _ja_avisado(conn, assinatura):
        return "repetido"
    return nw.despachar(config, ORIGEM_GRUPO, "cadastro", texto[:nw.MAX_MENSAGEM], assinatura,
                        modo_teste=modo_teste, conn=conn, **kw)


def convidar(conn, config: dict, cnpj: str, agora: datetime, modo_teste: bool = False, enviar_email=None,
             reenvio: bool = False, **kw) -> str:
    """Convite pelo melhor canal: whatsapp | email | nenhum | adiado. Adiado
    (teto, gateway ou SMTP fora) nao grava nada: tenta na proxima rodada."""
    c = cad.buscar(conn, cnpj)
    emb = conn.execute("SELECT apelido, nome_remetente, email FROM interno WHERE cnpj_embarcador = ?",
                       (c["cnpj_embarcador"],)).fetchone()
    nome = emb[0] or emb[1] or c["cnpj_embarcador"]
    email = (c["email_stokki"] or emb[2] or "").split(",")[0].strip()
    link = link_convite(conn, config, c["cnpj_embarcador"])
    numero = celular_whatsapp(c["celular_stokki"])
    canal = None
    if numero:
        if not modo_teste and not pn.ler(conn, c["cnpj_embarcador"])["whatsapp"]:
            pn.salvar(conn, c["cnpj_embarcador"], [], {}, "pre-cadastro", whatsapp=numero)
        assinatura = f"cadastro:{c['cnpj_embarcador']}:{'reenvio' if reenvio else 'convite'}"
        situacao = nw.avisar_cliente_cadastro(c["cnpj_embarcador"], numero, texto_convite(link), assinatura, config,
                                              modo_teste=modo_teste, conn=conn, agora=agora, **kw)
        if situacao in ("enviado", "modo_teste"):
            canal = "whatsapp"
        elif situacao in ("nao_enviado", "indeterminado", "falhou"):
            logger.info(f"Convite de {nome} adiado (WhatsApp: {situacao}).")
            return "adiado"
    if canal is None and email:
        if modo_teste:
            logger.info(f"[MODO TESTE] convite por e-mail pra {destino_email(config, email)[0]} ({nome}).")
            canal = "email"
        elif enviar_convite_email(config, nome, c["cnpj_embarcador"], email, link, enviar_email):
            canal = "email"
        else:
            logger.info(f"Convite de {nome} adiado (e-mail falhou).")
            return "adiado"
    canal = canal or "nenhum"
    if not modo_teste:
        if reenvio:
            cad.atualizar(conn, cnpj, agora, situacao=cad.REENVIADO, reenvio_em=_fmt(agora), canal_convite=canal)
        else:
            cad.atualizar(conn, cnpj, agora, situacao=cad.CONVIDADO if canal != "nenhum" else cad.SEM_CONTATO,
                          canal_convite=canal, convite_enviado_em=_fmt(agora))
    return canal


def reenviar_vencidos(conn, config: dict, agora: datetime, modo_teste: bool = False, enviar_email=None, **kw) -> int:
    """CONVIDADO ha mais de DIAS_REENVIO dias sem PIN definido: reenvia uma vez."""
    limite = _fmt(agora - timedelta(days=DIAS_REENVIO))
    rows = conn.execute("""
        SELECT c.cnpj_embarcador, COALESCE(i.apelido, i.nome_remetente, c.cnpj_embarcador)
        FROM cadastro_embarcador c
        LEFT JOIN interno i ON i.cnpj_embarcador = c.cnpj_embarcador
        LEFT JOIN clientes_portal p ON p.cnpj = c.cnpj_embarcador
        WHERE c.situacao = ? AND c.convite_enviado_em <= ? AND p.pin_hash IS NULL
    """, (cad.CONVIDADO, limite)).fetchall()
    enviados = 0
    for cnpj, nome in rows:
        if convidar(conn, config, cnpj, agora, modo_teste, enviar_email, reenvio=True, **kw) == "adiado":
            continue
        enviados += 1
        avisar_grupo(conn, config, texto_reenvio(nome), f"reenvio:{cnpj}", modo_teste, **kw)
    return enviados
```

- [ ] **Step 5: Run test to verify it passes**

Run: `py -3.11 -m unittest test_embarcadores_stokki test_notificar_whatsapp`
Expected: PASS (27 testes deste arquivo + os de `test_notificar_whatsapp` sem regressão)

- [ ] **Step 6: Commit**

```bash
git add notificar_whatsapp.py embarcadores_stokki.py test_embarcadores_stokki.py
git commit -m "Embarcadores Stokki: convite por WhatsApp ou e-mail, reenvio unico e aviso no grupo" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: a rodada (`executar`, `main`) e o timer

**Files:**
- Modify: `embarcadores_stokki.py`
- Create: `infra/stokki-embarcadores-novos.service`, `infra/stokki-embarcadores-novos.timer`
- Test: `test_embarcadores_stokki.py` (acrescentar `TestRodada` e `TestMain`)

**Interfaces:**
- Consumes: tudo das Tasks 1 a 4; `stokki.auth.StokkiSession(config)`, `stokki.pedidos.listar_embarcadores(sessao)`, `stokki.pedidos.BASE_URL`; `vuupt_client.VuuptClient(token)`.
- Produces:
  - `executar(config, conn, clientes, ler_contato, vuupt, agora=None, modo_teste=False, enviar_email=None, **kw) -> dict` com chaves `novos, ativos, inativos, convites: dict[cnpj, canal], reenvios, conflitos, sem_sender, stkkc_preenchido`
  - `_carregar_config() -> dict`
  - `main(argv=None) -> int`

- [ ] **Step 1: Write the failing test**

Acrescente em `test_embarcadores_stokki.py`:

```python
class TestRodada(_ComCadastro):
    def rodar(self, clientes, agora=AGORA, vuupt=None, **kw):
        return rot.executar(_config(), self.conn, clientes, lambda cid: CONTATOS.get(cid, ""),
                            vuupt or mock.MagicMock(), agora=agora, enviar_email=self.email, dormir=self.dormir, **kw)

    def test_primeira_rodada(self):
        self.pedido("PS-1", "49.874.051/0001-31", 23084712, 1)
        self.pedido("PS-2", "02.368.118/0006-07", 22872019, 3)
        r = self.rodar([COGUMELADO, NONNA, RIC, ANTIGO])
        self.assertEqual(r["novos"], 3)
        self.assertEqual(r["convites"], {NONNA["cnpj"]: "whatsapp", RIC["cnpj"]: "email"})
        self.assertEqual(self.interno(NONNA["cnpj"])["sender_id"], 23084712)
        self.assertEqual(self.interno(RIC["cnpj"])["sender_id"], 22872019)
        antigo = self.interno(ANTIGO["cnpj"])
        self.assertEqual((antigo["sender_id"], antigo["notificar_email"]), (None, 0))
        self.assertEqual(cad.buscar(self.conn, ANTIGO["cnpj"])["situacao"], "INATIVO")
        grupo = self.grupo()
        self.assertEqual(len(grupo), 3)
        self.assertTrue(any("1 embarcador(es)" in t for t in grupo))
        self.assertTrue(any("FABENNE" in t and "WhatsApp" in t for t in grupo))
        self.assertTrue(any("TERRA NUTRI" in t and "e-mail" in t for t in grupo))

    def test_segunda_rodada_nao_repete_nada(self):
        self.pedido("PS-1", "49.874.051/0001-31", 23084712, 1)
        self.pedido("PS-2", "02.368.118/0006-07", 22872019, 3)
        self.rodar([COGUMELADO, NONNA, RIC, ANTIGO])
        envios, emails = self.enviar.call_count, self.email.call_count
        r = self.rodar([COGUMELADO, NONNA, RIC, ANTIGO], agora=AGORA + timedelta(hours=1))
        self.assertEqual((r["novos"], r["convites"]), (0, {}))
        self.assertEqual((self.enviar.call_count, self.email.call_count), (envios, emails))

    def test_inativo_que_ganha_pedido_recebe_convite(self):
        self.rodar([ANTIGO])
        self.pedido("PS-9", "33.333.333/0001-33", 22999999, 0)
        r = self.rodar([ANTIGO], agora=AGORA + timedelta(hours=1))
        self.assertEqual(r["convites"], {ANTIGO["cnpj"]: "nenhum"})
        self.assertEqual(self.interno(ANTIGO["cnpj"])["sender_id"], 22999999)
        self.assertEqual(self.interno(ANTIGO["cnpj"])["notificar_email"], 1)
        self.assertEqual(cad.buscar(self.conn, ANTIGO["cnpj"])["situacao"], "SEM_CONTATO")

    def test_sem_sender_avisa_uma_vez_e_tenta_de_novo(self):
        self.pedido("PS-1", "49.874.051/0001-31", None, 1)
        vuupt = mock.MagicMock()
        vuupt.criar_customer.side_effect = RuntimeError("fora")
        r = self.rodar([NONNA], vuupt=vuupt)
        self.assertEqual((r["sem_sender"], r["convites"]), (1, {}))
        self.rodar([NONNA], vuupt=vuupt, agora=AGORA + timedelta(hours=3))
        self.assertEqual(len([t for t in self.grupo() if "remetente" in t]), 1)
        vuupt.criar_customer.side_effect = None
        vuupt.criar_customer.return_value = {"customer": {"id": 777}}
        r = self.rodar([NONNA], vuupt=vuupt, agora=AGORA + timedelta(hours=4))
        self.assertEqual(r["convites"], {NONNA["cnpj"]: "whatsapp"})
        self.assertEqual(self.interno(NONNA["cnpj"])["sender_id"], 777)

    def test_preenche_stkkc_de_cadastro_manual_sem_convidar(self):
        self.conn.execute("INSERT INTO interno (cnpj_embarcador, nome_remetente, sender_id, stkkc_id) "
                          "VALUES ('02368118000607', 'RIC', 22872019, NULL)")
        self.conn.commit()
        r = self.rodar([RIC])
        self.assertEqual((r["stkkc_preenchido"], r["novos"], r["convites"]), (1, 0, {}))
        self.assertEqual(self.interno(RIC["cnpj"])["stkkc_id"], 56)
        self.assertIsNone(cad.buscar(self.conn, RIC["cnpj"]))

    def test_modo_teste_nao_grava_nem_envia(self):
        self.pedido("PS-1", "49.874.051/0001-31", 23084712, 1)
        r = self.rodar([COGUMELADO, NONNA, ANTIGO], modo_teste=True)
        self.assertEqual((r["novos"], r["ativos"], r["inativos"]), (2, 1, 1))
        self.assertIsNone(self.interno(NONNA["cnpj"]))
        self.assertIsNone(cad.buscar(self.conn, NONNA["cnpj"]))
        self.enviar.assert_not_called()
        self.email.assert_not_called()


class TestMain(unittest.TestCase):
    def test_stokki_fora_sai_sem_erro(self):
        with mock.patch.object(rot, "_carregar_config", return_value=_config()), \
             mock.patch("stokki.auth.StokkiSession", side_effect=RuntimeError("Stokki ocupada")), \
             mock.patch.object(rot, "executar") as executar:
            self.assertEqual(rot.main([]), 0)
        executar.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m unittest test_embarcadores_stokki`
Expected: FAIL com `AttributeError: module 'embarcadores_stokki' has no attribute 'executar'` (e `_carregar_config`, `main`)

- [ ] **Step 3: Write minimal implementation**

Acrescente no fim de `embarcadores_stokki.py`:

```python
def executar(config: dict, conn, clientes: list[dict], ler_contato, vuupt, agora: datetime | None = None,
             modo_teste: bool = False, enviar_email=None, **kw) -> dict:
    """Uma rodada. `clientes` vem de clientes_do_select; `ler_contato(id)`
    devolve o HTML da tela de edicao do cliente na Stokki. `kw` (ex.: dormir)
    so nos testes."""
    agora = agora or datetime.now()
    cad.garantir_tabela(conn)
    resumo = {"novos": 0, "ativos": 0, "inativos": 0, "convites": {}, "reenvios": 0, "conflitos": 0,
              "sem_sender": 0, "stkkc_preenchido": 0}
    grupos = separar(clientes, conn)

    for cli in grupos["ignorados"]:
        logger.warning(f"Stokki #{cli['stkkc_id']} ({cli['razao']}): rotulo sem CNPJ, ignorado.")
    for cli in grupos["sem_stkkc"]:
        resumo["stkkc_preenchido"] += 1
        logger.info(f"Stokki #{cli['stkkc_id']}: CNPJ {cli['cnpj']} ja em interno sem stkkc_id, preenchendo.")
        if not modo_teste:
            conn.execute("UPDATE interno SET stkkc_id = ?, atualizado_em = ? WHERE cnpj_embarcador = ?",
                         (cli["stkkc_id"], _fmt(agora), cli["cnpj"]))
            conn.commit()
    for cli, outro in grupos["conflitos"]:
        resumo["conflitos"] += 1
        avisar_grupo(conn, config, texto_conflito(cli, outro), f"conflito:{cli['stkkc_id']}", modo_teste, **kw)

    novos_inativos = 0
    for cli in grupos["novos"]:
        resumo["novos"] += 1
        try:
            contato = extrair_contato(ler_contato(cli["stkkc_id"]) or "")
        except Exception as exc:
            logger.warning(f"Contato do cliente #{cli['stkkc_id']} nao lido na Stokki ({exc}): segue sem contato.")
            contato = extrair_contato("")
        ativo = tem_pedido_recente(conn, cli["cnpj"], agora)
        if modo_teste:
            resumo["ativos" if ativo else "inativos"] += 1
            logger.info(f"[MODO TESTE] novo #{cli['stkkc_id']} {cli['apelido'] or cli['razao']} ({cli['cnpj']}): "
                        f"{'ativo' if ativo else 'inativo'}, sender do nucleo {sender_do_nucleo(conn, cli['cnpj'])}, "
                        f"celular {'valido' if celular_whatsapp(contato['celular']) else 'invalido'}, "
                        f"e-mail {contato['email'] or '-'}")
            continue
        pre_cadastrar(conn, cli, contato, ativo, agora)
        if not ativo:
            novos_inativos += 1
    if novos_inativos:
        avisar_grupo(conn, config, texto_inativos(novos_inativos), f"inativos:{_fmt(agora)[:13]}", modo_teste, **kw)

    pendentes = conn.execute("SELECT cnpj_embarcador, situacao FROM cadastro_embarcador WHERE situacao IN (?, ?) "
                             "ORDER BY criado_em, cnpj_embarcador", (cad.PENDENTE, cad.INATIVO)).fetchall()
    for cnpj, situacao in pendentes:
        if situacao == cad.INATIVO and not tem_pedido_recente(conn, cnpj, agora):
            if not modo_teste:
                resumo["inativos"] += 1
            continue
        emb = conn.execute("SELECT apelido, nome_remetente, sender_id FROM interno WHERE cnpj_embarcador = ?",
                           (cnpj,)).fetchone()
        if emb is None:
            continue
        resumo["ativos"] += 1
        nome = emb[0] or emb[1] or cnpj
        sender = emb[2] or resolver_sender(conn, cnpj, emb[1] or nome, None if modo_teste else vuupt)
        if not sender:
            resumo["sem_sender"] += 1
            avisar_grupo(conn, config, texto_sem_sender(nome), f"sem_sender:{cnpj}", modo_teste, **kw)
            continue
        if not modo_teste:
            if not emb[2]:
                conn.execute("UPDATE interno SET sender_id = ?, notificar_email = 1, atualizado_em = ? "
                             "WHERE cnpj_embarcador = ?", (sender, _fmt(agora), cnpj))
                conn.commit()
            if situacao == cad.INATIVO:
                cad.atualizar(conn, cnpj, agora, situacao=cad.PENDENTE)
        canal = convidar(conn, config, cnpj, agora, modo_teste, enviar_email, **kw)
        if canal == "adiado":
            continue
        resumo["convites"][cnpj] = canal
        avisar_grupo(conn, config, texto_grupo_novo(nome, emb[1] or "", canal), f"novo:{cnpj}", modo_teste, **kw)

    resumo["reenvios"] = reenviar_vencidos(conn, config, agora, modo_teste, enviar_email, **kw)
    return resumo


def _carregar_config() -> dict:
    import yaml
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pre-cadastro de embarcador novo da Stokki.")
    parser.add_argument("--modo-teste", action="store_true", help="so mostra o que faria, sem gravar nem enviar")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = _carregar_config()
    try:
        from stokki.auth import StokkiSession
        from stokki.pedidos import BASE_URL, listar_embarcadores
        sessao = StokkiSession(config)
        clientes = clientes_do_select(listar_embarcadores(sessao))
    except Exception as exc:
        logger.warning(f"Stokki indisponivel ({exc}): tenta na proxima rodada.")
        return 0

    def ler_contato(stkkc_id: int) -> str:
        resp = sessao.get(f"{BASE_URL}/pt-br/administrator/client/edit/{stkkc_id}")
        resp.raise_for_status()
        return resp.text

    from vuupt_client import VuuptClient
    vuupt = VuuptClient((config.get("vuupt_api") or {}).get("token", ""))
    conn = auth.conectar()
    try:
        resumo = executar(config, conn, clientes, ler_contato, vuupt, modo_teste=args.modo_teste)
    finally:
        conn.close()
    logger.info(f"Resumo: {resumo}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
```

`infra/stokki-embarcadores-novos.service`:

```ini
[Unit]
Description=Stokki Eventos - pre-cadastro de embarcador novo da Stokki e convite pra completar o cadastro no portal
OnFailure=stokki-alerta-falha@%n.service

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/opt/stokki-eventos
ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/embarcadores_stokki.py
```

`infra/stokki-embarcadores-novos.timer`:

```ini
[Unit]
Description=Dispara stokki-embarcadores-novos.service de hora em hora, 07h10 as 19h10

[Timer]
OnCalendar=*-*-* 07..19:10:00
Persistent=false
AccuracySec=1min

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.11 -m unittest test_embarcadores_stokki test_cadastro_embarcador`
Expected: PASS (34 + 12 testes)

- [ ] **Step 5: Commit**

```bash
git add embarcadores_stokki.py test_embarcadores_stokki.py infra/stokki-embarcadores-novos.service infra/stokki-embarcadores-novos.timer
git commit -m "Embarcadores Stokki: rodada de hora em hora com modo teste e timer 07h-19h" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: tela "Complete o cadastro" no portal

**Files:**
- Modify: `portal_cliente/app.py` (import, `_carregar_cliente`, nova rota)
- Create: `portal_cliente/templates/completar_cadastro.html`
- Test: `portal_cliente/test_completar_cadastro.py`

**Interfaces:**
- Consumes: `cad.precisa_completar`, `cad.normalizar_habilidade`, `cad.salvar_preenchimento`, `cad.DESCRICOES_HABILIDADE`, `cad.CadastroInvalido` (Task 1); `preferencias.ler`, `preferencias.salvar`; `notificar_whatsapp.despachar`.
- Produces: rota `completar_cadastro` em `GET|POST /completar-cadastro`; o `before_request` desvia o cliente com cadastro aberto (APIs respondem 403).

- [ ] **Step 1: Write the failing test**

`portal_cliente/test_completar_cadastro.py`:

```python
"""Tela /completar-cadastro do portal (pre-cadastro automatico, 10/10).

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_completar_cadastro
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_AQUI = Path(__file__).parent
sys.path.insert(0, str(_AQUI.parent))
sys.path.insert(0, str(_AQUI))

import app as portal  # noqa: E402
import cadastro_embarcador as cad  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402

CNPJ_A = "49874051000131"   # pre-cadastro aberto
CNPJ_B = "22222222000122"   # cliente antigo, sem pre-cadastro
ORIGEM = {"Origin": "http://localhost"}


class TestCompletarCadastro(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        patcher = patch.object(portal.auth, "DB_PATH", self.db)
        patcher.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(patcher.stop)
        self.despachar = patch("notificar_whatsapp.despachar", return_value="enviado").start()
        self.addCleanup(patch.stopall)

        conn = portal.auth.conectar()
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER, "
                     "habilidade TEXT, atualizado_em TEXT)")
        conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?,NULL,NULL)", [
            (CNPJ_A, "NONNA DISTRIBUICAO", "FABENNE", "mariana@fabenne.com", 1, 23084712, 100),
            (CNPJ_B, "Beta SA", "Beta", "cadastro@beta.com", 1, 102, 9002)])
        conn.commit()
        cad.registrar(conn, CNPJ_A, 100, cad.CONVIDADO)
        for cnpj in (CNPJ_A, CNPJ_B):
            portal.auth.definir_pin(conn, cnpj, "135790")
        self.versao = {c: portal.auth.versao_conta(portal.auth.buscar_conta(conn, c)) for c in (CNPJ_A, CNPJ_B)}
        conn.close()
        portal.app.config["TESTING"] = True
        self.tc = portal.app.test_client()

    def entrar(self, cnpj=CNPJ_A):
        with self.tc.session_transaction() as s:
            s["cnpj"], s["v"] = cnpj, self.versao[cnpj]

    def conn(self):
        c = sqlite3.connect(self.db)
        self.addCleanup(c.close)
        return c

    def test_inicio_e_api_desviam_pra_completar(self):
        self.entrar()
        r = self.tc.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/completar-cadastro", r.headers["Location"])
        r = self.tc.get("/api/notificacoes")
        self.assertEqual(r.status_code, 403)
        self.assertIn("cadastro", r.get_json()["erro"])

    def test_tela_mostra_tipos_e_o_email_do_cadastro(self):
        self.entrar()
        html = self.tc.get("/completar-cadastro").get_data(as_text=True)
        for tipo in ("Seco", "Refrigerado", "Congelado"):
            self.assertIn(f'value="{tipo}"', html)
        self.assertIn("mariana@fabenne.com", html)

    def test_salvar_grava_preferencias_e_cadastro_sem_mexer_em_interno(self):
        self.entrar()
        r = self.tc.post("/completar-cadastro", headers=ORIGEM, data={
            "habilidade": "Refrigerado", "emails": "avisos@fabenne.com\nfiscal@fabenne.com", "whatsapp": "(11) 98888-7777"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True))
        self.assertNotIn("/completar-cadastro", r.headers["Location"])
        prefs = pn.ler(self.conn(), CNPJ_A)
        self.assertEqual((prefs["emails"], prefs["whatsapp"]), (["avisos@fabenne.com", "fiscal@fabenne.com"], "5511988887777"))
        c = cad.buscar(self.conn(), CNPJ_A)
        self.assertEqual((c["situacao"], c["habilidade_informada"]), ("PREENCHIDO", "Refrigerado"))
        self.assertIsNone(self.conn().execute("SELECT habilidade FROM interno WHERE cnpj_embarcador = ?", (CNPJ_A,)).fetchone()[0])
        self.assertIn("FABENNE completou o cadastro: Refrigerado", self.despachar.call_args.args[3])
        self.assertEqual(self.tc.get("/api/notificacoes").status_code, 200)

    def test_email_invalido_nao_grava_nada(self):
        self.entrar()
        r = self.tc.post("/completar-cadastro", headers=ORIGEM, data={
            "habilidade": "Seco", "emails": "isso-nao-e-email", "whatsapp": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIn("E-mail inválido", r.get_data(as_text=True))
        self.assertEqual(cad.buscar(self.conn(), CNPJ_A)["situacao"], "CONVIDADO")
        self.assertEqual(pn.ler(self.conn(), CNPJ_A)["emails"], [])
        self.despachar.assert_not_called()

    def test_sem_tipo_ou_sem_email_nao_grava(self):
        self.entrar()
        r = self.tc.post("/completar-cadastro", headers=ORIGEM, data={"habilidade": "", "emails": "a@b.com"})
        self.assertIn("tipo de carga", r.get_data(as_text=True))
        r = self.tc.post("/completar-cadastro", headers=ORIGEM, data={"habilidade": "Seco", "emails": " "})
        self.assertIn("pelo menos um e-mail", r.get_data(as_text=True))
        self.assertEqual(cad.buscar(self.conn(), CNPJ_A)["situacao"], "CONVIDADO")

    def test_cliente_sem_pre_cadastro_nao_ve_a_tela(self):
        self.entrar(CNPJ_B)
        self.assertEqual(self.tc.get("/api/notificacoes").status_code, 200)
        r = self.tc.get("/completar-cadastro")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/completar-cadastro", r.headers["Location"])

    def test_equipe_nao_e_desviada(self):
        with self.tc.session_transaction() as s:
            s["equipe"] = {"usuario": "hugo", "nivel": "operador"}
            s["cnpj_equipe"] = CNPJ_A
        self.assertEqual(self.tc.get("/api/notificacoes").status_code, 200)
        r = self.tc.get("/completar-cadastro")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/completar-cadastro", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m unittest portal_cliente.test_completar_cadastro`
Expected: FAIL (`/` responde 200 em vez de 302 e `/completar-cadastro` dá 404). Se falhar antes, com `RuntimeError: portal_cliente.secret_key ausente`, o worktree não tem `config.yaml`: rode este teste e o Step 4 com a variável de ambiente que os outros testes do portal usam (veja como `portal_cliente/test_notificacoes_portal.py` roda no worktree; não copie o `config.yaml`).

- [ ] **Step 3: Write minimal implementation**

Em `portal_cliente/app.py`, junto dos imports da raiz (depois de `import preferencias_notificacao as preferencias`):

```python
import cadastro_embarcador as cad_emb
```

Troque o trecho do cliente logado em `_carregar_cliente` (hoje com `g.cliente = auth.sessao_valida(...)` e o `elif` da troca de PIN) por:

```python
    cnpj, versao = session.get("cnpj"), session.get("v")
    if cnpj and versao:
        conn = auth.conectar()
        try:
            g.cliente = auth.sessao_valida(conn, cnpj, versao)
            completar = bool(g.cliente) and cad_emb.precisa_completar(conn, g.cliente["cnpj"])
        finally:
            conn.close()
        if not g.cliente:
            session.clear()
        elif g.cliente.get("trocar_pin") and request.endpoint not in _LIVRES_ANTES_DA_TROCA:
            # Conta criada com o PIN padrao (02/10): nada abre antes da troca.
            if request.path.startswith("/api/"):
                return jsonify({"erro": "Antes de continuar, é preciso trocar o PIN."}), 403
            return redirect(url_for("trocar_pin"))
        elif completar and request.endpoint not in _LIVRES_ANTES_DO_CADASTRO:
            # Pre-cadastro automatico (10/10): tipo de carga e contatos antes de tudo.
            if request.path.startswith("/api/"):
                return jsonify({"erro": "Antes de continuar, complete o cadastro da empresa."}), 403
            return redirect(url_for("completar_cadastro"))
```

Logo abaixo da linha `_LIVRES_ANTES_DA_TROCA = {...}`:

```python
_LIVRES_ANTES_DO_CADASTRO = _LIVRES_ANTES_DA_TROCA | {"completar_cadastro"}
```

Depois da rota `trocar_pin`:

```python
def _avisar_cadastro_preenchido(nome: str, tipo: str) -> None:
    try:
        import notificar_whatsapp as nw
        nw.despachar(_CONFIG, "embarcadores_stokki", "cadastro",
                     f"{nome} completou o cadastro: {tipo}. Aguardando aprovação no /inicio do painel.")
    except Exception as e:
        logger.warning(f"Aviso de cadastro preenchido nao enviado: {e}")


@app.route("/completar-cadastro", methods=["GET", "POST"])
def completar_cadastro():
    """Pre-cadastro automatico (10/10): tipo de carga, e-mails e WhatsApp antes
    de abrir o portal. O tipo só vale no roteirizador depois da aprovação."""
    if g.get("equipe"):
        return redirect(url_for("inicio"))
    if not g.cliente:
        return redirect(url_for("login"))
    cnpj = g.cliente["cnpj"]
    conn = auth.conectar()
    try:
        if not cad_emb.precisa_completar(conn, cnpj):
            return redirect(url_for("inicio"))
        prefs = preferencias.ler(conn, cnpj)
        valores = {"habilidade": "", "emails": "\n".join(prefs["emails"] or prefs["emails_cadastro"]),
                   "whatsapp": prefs["whatsapp"]}
        erro = None
        if request.method == "POST":
            valores = {chave: (request.form.get(chave) or "") for chave in ("habilidade", "emails", "whatsapp")}
            emails = valores["emails"].replace(";", "\n").replace(",", "\n").splitlines()
            try:
                tipo = cad_emb.normalizar_habilidade(valores["habilidade"])
                if not any(e.strip() for e in emails):
                    raise ValueError("Informe pelo menos um e-mail para os avisos.")
                preferencias.salvar(conn, cnpj, emails, {}, "cliente", whatsapp=valores["whatsapp"])
                cad_emb.salvar_preenchimento(conn, cnpj, tipo)
            except ValueError as e:
                erro = str(e)
            else:
                logger.info(f"cadastro preenchido cnpj={cnpj} tipo={tipo}")
                _avisar_cadastro_preenchido(g.cliente["nome"], tipo)
                return redirect(url_for("inicio"))
        return render_template("completar_cadastro.html", valores=valores, erro=erro,
                               habilidades=list(cad_emb.DESCRICOES_HABILIDADE.items()))
    finally:
        conn.close()
```

`portal_cliente/templates/completar_cadastro.html`:

```html
{% extends "base_acesso.html" %}
{% block titulo %}Complete o cadastro{% endblock %}
{% block conteudo %}
<style>
  fieldset { border: none; padding: 0; margin: 0 0 16px; }
  legend { font-size: 13px; font-weight: 600; color: var(--primaria); margin-bottom: 8px; }
  label.opcao { display: flex; gap: 10px; align-items: flex-start; padding: 9px 11px; border: 1px solid var(--borda);
                border-radius: 7px; margin-bottom: 6px; cursor: pointer; font-weight: 400; color: var(--texto); }
  label.opcao:has(input:checked) { border-color: var(--acento); }
  label.opcao input { margin-top: 3px; accent-color: var(--acento); }
  label.opcao b { display: block; font-size: 13.5px; color: var(--primaria); }
  label.opcao span { font-size: 12.5px; line-height: 1.4; color: var(--texto-suave); }
  textarea, input[type=tel] { width: 100%; padding: 10px 12px; border: 1px solid var(--borda); border-radius: 7px;
                              font-size: 14px; margin-bottom: 6px; font-family: inherit; }
  textarea { min-height: 70px; resize: vertical; }
  textarea:focus, input[type=tel]:focus { outline: 2px solid var(--acento); outline-offset: -1px; border-color: var(--acento); }
  .ajuda { font-size: 12px; color: var(--texto-suave); margin: 0 0 16px; }
</style>
<h1>Complete o cadastro</h1>
<p><strong>{{ cliente.nome }}</strong><br><span class="suave">CNPJ {{ cliente.cnpj_formatado }}</span></p>
<p class="suave">Falta pouco. Essas informações ajudam a Fresh Log a planejar as suas entregas e a avisar a pessoa certa.</p>
{% if erro %}<div class="erro" role="alert">{{ erro }}</div>{% endif %}
<form method="post" autocomplete="off">
  <fieldset>
    <legend>Tipo de carga</legend>
    {% for tipo, descricao in habilidades %}
    <label class="opcao">
      <input type="radio" name="habilidade" value="{{ tipo }}" {% if valores.habilidade == tipo %}checked{% endif %} required>
      <span><b>{{ tipo }}</b>{{ descricao }}</span>
    </label>
    {% endfor %}
  </fieldset>
  <label for="emails">E-mails para os avisos de entrega</label>
  <textarea id="emails" name="emails" rows="3" spellcheck="false" placeholder="um e-mail por linha">{{ valores.emails }}</textarea>
  <p class="ajuda">Até 5 endereços, um por linha.</p>
  <label for="whatsapp">WhatsApp (celular com DDD)</label>
  <input type="tel" id="whatsapp" name="whatsapp" inputmode="tel" autocomplete="tel-national" placeholder="(11) 99999-0000" maxlength="20" value="{{ valores.whatsapp }}">
  <p class="ajuda">Opcional. Usado só para avisos de chamado aguardando a sua resposta.</p>
  <button type="submit" class="principal">Salvar e entrar</button>
</form>
<p class="suave" style="margin-top:14px"><a href="{{ url_for('sair') }}">Sair</a></p>
{% endblock %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.11 -m unittest portal_cliente.test_completar_cadastro portal_cliente.test_pin_padrao portal_cliente.test_notificacoes_portal`
Expected: PASS (7 testes novos e os existentes sem regressão)

- [ ] **Step 5: Commit**

```bash
git add portal_cliente/app.py portal_cliente/templates/completar_cadastro.html portal_cliente/test_completar_cadastro.py
git commit -m "Portal: tela Complete o cadastro para embarcador pre-cadastrado da Stokki" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: aprovação no `/inicio` do painel

**Files:**
- Modify: `painel_agentes/pagina_inicial.py` (`montar_cadastros`, `_cadastros_reais`, `montar_dados`)
- Modify: `painel_agentes/painel_agentes.py` (rota de aprovação, depois de `api_cadastro_recusar`)
- Modify: `painel_agentes/templates/inicio.html` (seção, CSS e JS)
- Test: `painel_agentes/test_cadastro_embarcador_painel.py`; `painel_agentes/test_pagina_inicial.py` (acrescentar casos)

**Interfaces:**
- Consumes: `cad.listar_abertos(conn)`, `cad.aprovar(conn, cnpj, por, habilidade=None)`, `cad.CadastroInvalido` (Task 1); `nucleo.banco.conectar()`.
- Produces:
  - `pagina_inicial.montar_cadastros(linhas: list[dict]) -> list[dict]` com chaves `cnpj, nome, situacao, detalhe, pode_aprovar, habilidade`
  - `montar_dados(..., cadastros=None)`: chave `cadastros_embarcador` só pro nível total
  - rota `POST /api/cadastro-embarcador/<cnpj>/aprovar` (nível total, mesma origem) com corpo opcional `{"habilidade": "..."}` → `{"ok": true, "cadastro": {...}}`

- [ ] **Step 1: Write the failing tests**

Acrescente em `painel_agentes/test_pagina_inicial.py`, no fim (antes do `if __name__`):

```python
class TestCadastrosEmbarcador(unittest.TestCase):
    LINHAS = [
        {"cnpj_embarcador": "49874051000131", "nome": "FABENNE", "situacao": "PREENCHIDO", "habilidade_informada": "Seco",
         "preenchido_em": "2026-10-10 10:00:00", "convite_enviado_em": "2026-10-09 09:10:00", "reenvio_em": None,
         "canal_convite": "whatsapp"},
        {"cnpj_embarcador": "02368118000607", "nome": "TERRA NUTRI", "situacao": "CONVIDADO", "habilidade_informada": None,
         "preenchido_em": None, "convite_enviado_em": "2026-10-09 09:10:00", "reenvio_em": None, "canal_convite": "email"},
        {"cnpj_embarcador": "33333333000133", "nome": "ANTIGO", "situacao": "SEM_CONTATO", "habilidade_informada": None,
         "preenchido_em": None, "convite_enviado_em": "2026-10-09 09:10:00", "reenvio_em": None, "canal_convite": "nenhum"},
    ]

    def test_montar_cadastros(self):
        c = pagina_inicial.montar_cadastros(self.LINHAS)
        self.assertEqual([(x["nome"], x["pode_aprovar"]) for x in c],
                         [("FABENNE", True), ("TERRA NUTRI", False), ("ANTIGO", False)])
        self.assertEqual(c[0]["detalhe"], "informou Seco em 10/10")
        self.assertEqual(c[1]["detalhe"], "convidado em 09/10 por email, sem resposta")
        self.assertEqual(c[2]["detalhe"], "sem contato na Stokki, cadastrar à mão")

    def test_so_o_total_recebe_o_bloco(self):
        fontes = dict(contadores=lambda n: {}, niveis_por_contador=NIVEIS, snapshot=lambda d: None,
                      execucoes=lambda: [], cadastros=lambda: self.LINHAS)
        self.assertEqual(len(pagina_inicial.montar_dados("total", _url, **fontes)["cadastros_embarcador"]), 3)
        self.assertNotIn("cadastros_embarcador", pagina_inicial.montar_dados("operador", _url, **fontes))

    def test_fonte_que_falha_vira_lista_vazia(self):
        def quebra():
            raise RuntimeError("banco travado")
        d = pagina_inicial.montar_dados("total", _url, contadores=lambda n: {}, niveis_por_contador=NIVEIS,
                                        snapshot=lambda d: None, execucoes=lambda: [], cadastros=quebra)
        self.assertEqual(d["cadastros_embarcador"], [])
```

`painel_agentes/test_cadastro_embarcador_painel.py`:

```python
# -*- coding: utf-8 -*-
"""Aprovacao do pre-cadastro de embarcador pelo painel (10/10): so o nivel
total grava interno.habilidade.

    cd painel_agentes && py -3.11 -m unittest test_cadastro_embarcador_painel
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

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

import cadastro_embarcador as cad  # noqa: E402
from nucleo import banco  # noqa: E402

CONFIG_PAINEL = {"usuario": "u_total", "senha": "s_total", "usuario_operador": "u_op", "senha_operador": "s_op",
                 "usuario_leitura": "u_le", "senha_leitura": "s_le"}
CNPJ = "49874051000131"


class TestAprovarCadastroEmbarcador(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch_db = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch_db.start()
        config = {**painel_agentes._carregar_config(), "painel_agentes": CONFIG_PAINEL}
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=config)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        conn = banco.conectar()
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "habilidade TEXT, atualizado_em TEXT)")
        conn.execute("INSERT INTO interno VALUES (?, 'NONNA', 'FABENNE', NULL, NULL)", (CNPJ,))
        conn.commit()
        cad.registrar(conn, CNPJ, 100, cad.CONVIDADO)
        cad.salvar_preenchimento(conn, CNPJ, "Refrigerado")
        conn.close()

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "hugo"

    def _post(self, body=None, origem="http://localhost"):
        return self.cliente.post(f"/api/cadastro-embarcador/{CNPJ}/aprovar", json=body or {},
                                 headers={"Origin": origem, "Referer": origem + "/"})

    def _habilidade(self):
        conn = banco.conectar()
        try:
            return conn.execute("SELECT habilidade FROM interno WHERE cnpj_embarcador = ?", (CNPJ,)).fetchone()[0]
        finally:
            conn.close()

    def test_so_total_aprova(self):
        self._logar("operador")
        self.assertIn(self._post().status_code, (302, 403))
        self.assertIsNone(self._habilidade())

    def test_aprovar_grava_e_some_da_lista(self):
        self._logar("total")
        r = self._post()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["cadastro"]["aprovado_por"], "hugo")
        self.assertEqual(self._habilidade(), "Refrigerado")
        conn = banco.conectar()
        try:
            self.assertEqual(cad.listar_abertos(conn), [])
        finally:
            conn.close()
        self.assertEqual(self._post().status_code, 409)

    def test_corrigir_tipo_e_tipo_invalido(self):
        self._logar("total")
        self.assertEqual(self._post({"habilidade": "Quente"}).status_code, 400)
        self.assertEqual(self._post({"habilidade": "Seco"}).status_code, 200)
        self.assertEqual(self._habilidade(), "Seco")

    def test_origem_estranha(self):
        self._logar("total")
        self.assertEqual(self._post(origem="http://outro.site").status_code, 403)
        self.assertIsNone(self._habilidade())


if __name__ == "__main__":
    unittest.main()
```

Atenção: o 2º `_post()` de `test_aprovar_grava_e_some_da_lista` espera 409 porque, já APROVADO, sem `habilidade` no corpo a função `cad.aprovar` exige PREENCHIDO.

- [ ] **Step 2: Run tests to verify they fail**

Run (de dentro de `painel_agentes/`): `py -3.11 -m unittest test_pagina_inicial test_cadastro_embarcador_painel`
Expected: FAIL com `AttributeError: module 'pagina_inicial' has no attribute 'montar_cadastros'`, `TypeError` no argumento `cadastros` e 404 na rota.

- [ ] **Step 3: Write minimal implementation**

Em `painel_agentes/pagina_inicial.py`, antes de `montar_dados`:

```python
def _dia_mes(texto: str | None) -> str:
    return f"{texto[8:10]}/{texto[5:7]}" if texto else "-"


def montar_cadastros(linhas: list[dict]) -> list[dict]:
    """Bloco "Cadastros de embarcador" (so total, 10/10): PREENCHIDO tem
    botao Aprovar; o resto so informa em que pe esta."""
    saida = []
    for l in linhas:
        situacao = l["situacao"]
        if situacao == "PREENCHIDO":
            detalhe = f"informou {l['habilidade_informada']} em {_dia_mes(l['preenchido_em'])}"
        elif situacao == "SEM_CONTATO":
            detalhe = "sem contato na Stokki, cadastrar à mão"
        elif situacao == "PENDENTE":
            detalhe = "aguardando remetente ou convite"
        else:
            detalhe = (f"convidado em {_dia_mes(l['reenvio_em'] or l['convite_enviado_em'])} "
                       f"por {l['canal_convite']}, sem resposta")
        saida.append({"cnpj": l["cnpj_embarcador"], "nome": l["nome"], "situacao": situacao, "detalhe": detalhe,
                      "pode_aprovar": situacao == "PREENCHIDO", "habilidade": l["habilidade_informada"]})
    return saida


def _cadastros_reais() -> list[dict]:
    from nucleo import banco as nucleo_banco
    import cadastro_embarcador as cad_emb
    conn = nucleo_banco.conectar()
    try:
        return cad_emb.listar_abertos(conn)
    finally:
        conn.close()
```

Em `montar_dados`, acrescente `cadastros=None` na assinatura (`snapshot=None, execucoes=None, cadastros=None) -> dict:`) e, dentro do `if nivel_acesso == "total":`, depois do bloco das rotinas:

```python
        try:
            dados["cadastros_embarcador"] = montar_cadastros((cadastros or _cadastros_reais)())
        except Exception as e:
            logger.warning(f"[inicio] Falha ao ler cadastros de embarcador: {e}")
            dados["cadastros_embarcador"] = []
```

Se algum teste existente de `test_pagina_inicial.py` compara o dicionário inteiro do nível total, passe `cadastros=lambda: []` naquela chamada e acrescente `"cadastros_embarcador": []` ao esperado. Não mexa no resto do teste.

Em `painel_agentes/painel_agentes.py`, logo depois de `api_cadastro_recusar`:

```python
# ── Pre-cadastro de embarcador da Stokki (10/10): aprovação só do nível total ──
@app.route("/api/cadastro-embarcador/<cnpj>/aprovar", methods=["POST"])
@requer_auth(niveis=("total",))
@exige_mesma_origem
def api_cadastro_embarcador_aprovar(cnpj):
    """Grava em interno.habilidade o tipo de carga que o cliente informou no
    portal, ou o do corpo ({"habilidade": ...}) se a equipe corrigir."""
    import cadastro_embarcador as cad_emb
    from nucleo import banco as nucleo_banco
    body = request.get_json(silent=True) or {}
    conn = nucleo_banco.conectar()
    try:
        c = cad_emb.aprovar(conn, cnpj, session.get("usuario") or g.nivel_acesso, habilidade=body.get("habilidade"))
    except cad_emb.CadastroInvalido as e:
        return jsonify({"erro": e.mensagem}), e.codigo
    finally:
        conn.close()
    return jsonify({"ok": True, "cadastro": c})
```

Em `painel_agentes/templates/inicio.html`:

No `<style>`, antes de `</style>`:

```css
  .rotina button.botao { white-space: nowrap; }
  .erro-decisao { color: var(--erro); font-size: 13px; margin: 8px 0 0; min-height: 18px; }
```

Antes de `{% if mostra_rotinas %}` (a seção das rotinas):

```html
{% if mostra_rotinas %}
<section class="secao" id="cadastros-embarcador" aria-labelledby="titulo-cadastros" hidden>
  <h2 id="titulo-cadastros">Cadastros de embarcador</h2>
  <div id="cadastros-lista" class="lista-rotinas"></div>
  <p class="erro-decisao" id="cadastros-aviso" role="status"></p>
</section>
{% endif %}
```

No `<script>`, depois de `var rotinasEl = ...`:

```js
    var cadastrosSecao = document.getElementById("cadastros-embarcador");
    var cadastrosEl = document.getElementById("cadastros-lista");
    var cadastrosAviso = document.getElementById("cadastros-aviso");
```

Depois da função `pintarRotinas`:

```js
    function aprovarCadastro(c, botao) {
      if (!window.confirm("Gravar " + c.habilidade + " como tipo de carga de " + c.nome + "? Passa a valer no roteirizador.")) return;
      botao.disabled = true;
      cadastrosAviso.textContent = "";
      fetch(BASE_PATH + "/api/cadastro-embarcador/" + encodeURIComponent(c.cnpj) + "/aprovar", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: "{}"
      }).then(function(r) {
        return r.json().then(function(d) { if (!r.ok) throw new Error(d.erro || "Falha ao aprovar."); });
      }).then(buscar).catch(function(e) {
        cadastrosAviso.textContent = e.message;
        botao.disabled = false;
      });
    }

    function pintarCadastros(lista) {
      if (!cadastrosSecao) return;
      cadastrosSecao.hidden = !lista.length;
      var frag = document.createDocumentFragment();
      lista.forEach(function(c) {
        var linha = el("div", "rotina");
        linha.appendChild(el("span", "nome", c.nome));
        linha.appendChild(el("span", "quando", c.detalhe));
        if (c.pode_aprovar) {
          var b = el("button", "botao", "Aprovar " + c.habilidade);
          b.type = "button";
          b.addEventListener("click", function() { aprovarCadastro(c, b); });
          linha.appendChild(b);
        }
        frag.appendChild(linha);
      });
      cadastrosEl.replaceChildren(frag);
    }
```

Em `buscar()`, depois de `if (d.rotinas) pintarRotinas(d.rotinas);`:

```js
          if (d.cadastros_embarcador) pintarCadastros(d.cadastros_embarcador);
```

- [ ] **Step 4: Run tests to verify they pass**

Run (de dentro de `painel_agentes/`): `py -3.11 -m unittest test_pagina_inicial test_cadastro_embarcador_painel test_inicio_tela`
Expected: PASS (casos novos e os existentes sem regressão)

- [ ] **Step 5: Conferir a tela no navegador**

Suba o painel em porta de teste (de dentro de `painel_agentes/`, nunca na 8070): `py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"`. Com Playwright (memória "Testar painel com Playwright local"), logue como total, insira à mão um `cadastro_embarcador` PREENCHIDO no banco local de dev e confira: o bloco aparece, o botão "Aprovar Seco" grava e a linha some. Pare só o processo que você subiu.

- [ ] **Step 6: Commit**

```bash
git add painel_agentes/pagina_inicial.py painel_agentes/painel_agentes.py painel_agentes/templates/inicio.html painel_agentes/test_pagina_inicial.py painel_agentes/test_cadastro_embarcador_painel.py
git commit -m "Painel: aprovar no /inicio o tipo de carga do embarcador pre-cadastrado" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: CLI `gerenciar_clientes.py cadastro`

**Files:**
- Modify: `portal_cliente/gerenciar_clientes.py` (docstring, `_comando_cadastro`, subparser, dispatch)
- Test: `portal_cliente/test_gerenciar_cadastro.py`

**Interfaces:**
- Consumes: `cad.listar_abertos`, `cad.aprovar`, `cad.CadastroInvalido` (Task 1); `auth.conectar`, `auth.formatar_cnpj`.
- Produces: `cadastro` (lista), `cadastro aprovar <cnpj>`, `cadastro habilidade <cnpj> <tipo>`; retorno 0 em sucesso, 2 em erro.

- [ ] **Step 1: Write the failing test**

`portal_cliente/test_gerenciar_cadastro.py`:

```python
"""CLI gerenciar_clientes.py cadastro (pre-cadastro automatico, 10/10).

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_gerenciar_cadastro
"""
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_AQUI = Path(__file__).parent
sys.path.insert(0, str(_AQUI.parent))
sys.path.insert(0, str(_AQUI))

import cadastro_embarcador as cad  # noqa: E402
import gerenciar_clientes as gc  # noqa: E402

CNPJ = "49874051000131"


class TestCliCadastro(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        for p in (patch.object(gc.auth, "DB_PATH", self.db), patch.object(gc, "_config", return_value={})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        conn = gc.auth.conectar()
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "habilidade TEXT, atualizado_em TEXT)")
        conn.execute("INSERT INTO interno VALUES (?, 'NONNA', 'FABENNE', NULL, NULL)", (CNPJ,))
        conn.commit()
        cad.registrar(conn, CNPJ, 100, cad.CONVIDADO)
        cad.salvar_preenchimento(conn, CNPJ, "Refrigerado")
        conn.close()

    def rodar(self, *argv):
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = gc.main(list(argv))
        return codigo, saida.getvalue()

    def habilidade(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT habilidade FROM interno").fetchone()[0]
        finally:
            conn.close()

    def test_listar(self):
        codigo, saida = self.rodar("cadastro")
        self.assertEqual(codigo, 0)
        self.assertIn("49.874.051/0001-31", saida)
        self.assertIn("PREENCHIDO", saida)
        self.assertIn("Refrigerado", saida)

    def test_aprovar(self):
        codigo, saida = self.rodar("cadastro", "aprovar", CNPJ)
        self.assertEqual(codigo, 0, saida)
        self.assertEqual(self.habilidade(), "Refrigerado")
        self.assertEqual(self.rodar("cadastro")[1].strip(), "Nenhum pré-cadastro aberto.")

    def test_corrigir_tipo(self):
        self.assertEqual(self.rodar("cadastro", "habilidade", CNPJ, "seco")[0], 0)
        self.assertEqual(self.habilidade(), "Seco")

    def test_erros(self):
        self.assertEqual(self.rodar("cadastro", "aprovar")[0], 2)
        self.assertEqual(self.rodar("cadastro", "habilidade", CNPJ)[0], 2)
        codigo, saida = self.rodar("cadastro", "habilidade", CNPJ, "quente")
        self.assertEqual(codigo, 2)
        self.assertIn("Nada gravado", saida)
        self.assertIsNone(self.habilidade())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m unittest portal_cliente.test_gerenciar_cadastro`
Expected: FAIL com `SystemExit: 2` (argparse: `invalid choice: 'cadastro'`)

- [ ] **Step 3: Write minimal implementation**

Na docstring de `portal_cliente/gerenciar_clientes.py`, depois do bloco "Máscara de envio de pedidos":

```
Pré-cadastro automático da Stokki (10/10) -- embarcadores_stokki.py:
    py -3 portal_cliente/gerenciar_clientes.py cadastro                                  # o que não está APROVADO
    py -3 portal_cliente/gerenciar_clientes.py cadastro aprovar 49874051000131           # grava o tipo que o cliente informou
    py -3 portal_cliente/gerenciar_clientes.py cadastro habilidade 49874051000131 Seco   # aprova com outro tipo
```

Antes de `def main`:

```python
def _comando_cadastro(conn, args) -> int:
    import cadastro_embarcador as cad_emb
    if not args.acao:
        linhas = cad_emb.listar_abertos(conn)
        if not linhas:
            print("Nenhum pré-cadastro aberto.")
        for l in linhas:
            quando = (l["reenvio_em"] or l["convite_enviado_em"] or "")[:16]
            print(f"{auth.formatar_cnpj(l['cnpj_embarcador'])}  {l['situacao']:<11} {l['nome'][:30]:<30} "
                  f"tipo informado: {l['habilidade_informada'] or '-':<11} convite: {l['canal_convite'] or '-'} {quando}")
        return 0
    if not args.cnpj:
        print("Informe o CNPJ.")
        return 2
    if args.acao == "habilidade" and not args.tipo:
        print("Informe o tipo: Seco, Refrigerado ou Congelado.")
        return 2
    try:
        c = cad_emb.aprovar(conn, args.cnpj, "cli", habilidade=args.tipo if args.acao == "habilidade" else None)
    except cad_emb.CadastroInvalido as e:
        print(f"Nada gravado: {e.mensagem}")
        return 2
    print(f"Aprovado: {auth.formatar_cnpj(c['cnpj_embarcador'])} com tipo de carga {c['habilidade_informada']}.")
    return 0
```

Em `main`, depois de `cc.add_argument("--recusar", action="store_true")`:

```python
    ce = sub.add_parser("cadastro", help="pré-cadastros automáticos da Stokki: lista, aprova ou corrige o tipo de carga")
    ce.add_argument("acao", nargs="?", choices=["aprovar", "habilidade"])
    ce.add_argument("cnpj", nargs="?")
    ce.add_argument("tipo", nargs="?", help="Seco | Refrigerado | Congelado (só com 'habilidade')")
```

Logo depois de `conn = auth.conectar()` / `try:` em `main`, antes de `if args.cmd == "listar":`:

```python
        if args.cmd == "cadastro":
            return _comando_cadastro(conn, args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.11 -m unittest portal_cliente.test_gerenciar_cadastro portal_cliente.test_checar_cliente`
Expected: PASS (4 testes novos e os existentes sem regressão)

- [ ] **Step 5: Commit**

```bash
git add portal_cliente/gerenciar_clientes.py portal_cliente/test_gerenciar_cadastro.py
git commit -m "Portal: comando cadastro no gerenciar_clientes para aprovar pre-cadastro" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: mapa, skill de cadastro e regressão

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt`
- Modify: `.claude/skills/cadastrar-cliente-portal/SKILL.md`

**Interfaces:**
- Consumes: tudo acima.
- Produces: documentação; nenhuma interface nova.

- [ ] **Step 1: Atualizar o mapa**

No índice "onde fica" (perto da linha de `Portal do cliente`):

```
 Embarcador novo na Stokki (pré-cadastro) .. embarcadores_stokki.py (timer stokki-embarcadores-novos, 07h-19h)
                                             + cadastro_embarcador.py; tela /cliente/completar-cadastro;
                                             aprovação no /inicio (total) ou gerenciar_clientes.py cadastro
```

Na seção dos scripts da raiz, junto de `avisar_cliente_sem_resposta.py`:

```
 embarcadores_stokki.py        De hora em hora (07h-19h): cliente da Stokki fora de
                               `interno` vira pré-cadastro (sem habilidade), ganha o
                               sender da Vuupt (pedido mais antigo do núcleo ou
                               POST /customers) e convite pra completar o cadastro
                               no portal: WhatsApp do celular da Stokki, senão
                               e-mail, senão aviso no grupo. Sem pedido em 30 dias
                               = INATIVO, sem convite. Reenvio único em 3 dias.
 cadastro_embarcador.py        Tabela cadastro_embarcador (situação do pré-cadastro)
                               e aprovação: só aprovar() grava interno.habilidade.
```

Na seção 11 (portal), linha nova abaixo de `auth_cliente.py`:

```
                           Cliente com pré-cadastro aberto (cadastro_embarcador)
                           só abre /completar-cadastro até informar tipo de
                           carga, e-mails e WhatsApp.
```

Na lista de timers/units, a linha de `stokki-embarcadores-novos.timer` (07..19:10). Em "Armadilhas":

```
 - Pré-cadastro automático ESCREVE em `interno` (embarcadores_stokki.py). E-mail
   do convite vai pro Hugo enquanto cadastro_embarcador.forcar_destino_email não
   for "" explícito; WhatsApp segue whatsapp_notificacoes.clientes.forcar_destino.
```

- [ ] **Step 2: Atualizar a skill de cadastro manual**

Em `.claude/skills/cadastrar-cliente-portal/SKILL.md`, depois do item "1. Checar", acrescente:

```markdown
Antes de cadastrar à mão, rode `gerenciar_clientes.py cadastro`: embarcador novo da Stokki pode já estar pré-cadastrado pela rotina `embarcadores_stokki.py` (convite enviado ou esperando aprovação do tipo de carga). Nesse caso não mande `enviar-link`: o convite já foi, e o reenvio é automático em 3 dias.
```

- [ ] **Step 3: Regressão completa**

Run (da raiz do worktree):
```bash
py -3.11 -m py_compile cadastro_embarcador.py embarcadores_stokki.py notificar_whatsapp.py portal_cliente/app.py portal_cliente/gerenciar_clientes.py painel_agentes/pagina_inicial.py painel_agentes/painel_agentes.py
py -3.11 -m unittest test_cadastro_embarcador test_embarcadores_stokki test_notificar_whatsapp test_avisar_cliente_sem_resposta test_preferencias_whatsapp
py -3.11 -m unittest portal_cliente.test_completar_cadastro portal_cliente.test_gerenciar_cadastro portal_cliente.test_pin_padrao portal_cliente.test_notificacoes_portal portal_cliente.test_checar_cliente portal_cliente.test_grupos
```
e, de dentro de `painel_agentes/`:
```bash
py -3.11 -m unittest test_pagina_inicial test_inicio_tela test_cadastro_embarcador_painel test_cadastros_motoristas
```
Expected: todos PASS. Se um teste que não é desta tarefa falhar, confira com `git stash` no worktree se ele já falhava em `origin/master` antes de mexer.

- [ ] **Step 4: Commit**

```bash
git add MAPA_DO_SISTEMA.txt .claude/skills/cadastrar-cliente-portal/SKILL.md
git commit -m "Mapa: rotina de embarcadores novos da Stokki e tela de completar cadastro" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: prova em produção (só depois do "deploy" do Hugo)

**Files:** nenhum.

**Interfaces:**
- Consumes: o ramo `embarcadores-novos` mergeado em `master` e pushado.

- [ ] **Step 1: Review antes do deploy**

Rode a skill `code-review` (nível high) sobre o ramo e trate o que for confirmado. O hook do projeto lembra a review em push de módulo público (portal).

- [ ] **Step 2: Deploy pela skill `deploy-vps`**

Seguir o roteiro do CLAUDE.md: `git status` e `git log` na VPS, `git pull --no-rebase`, `chown -R www-data:www-data`, copiar `infra/stokki-embarcadores-novos.{service,timer}` pra `/etc/systemd/system/`, `daemon-reload`, `systemctl restart portal-cliente painel-agentes`. Ainda NÃO habilitar o timer.

- [ ] **Step 3: Modo teste na VPS**

Run:
```bash
ssh -i ~/.ssh/atendimento_vps root@187.127.52.197 "cd /opt/stokki-eventos && sudo -u www-data venv/bin/python embarcadores_stokki.py --modo-teste 2>&1 | tail -60"
```
Expected: `[MODO TESTE] novo #100 FABENNE ... ativo, sender do nucleo 23072631` e `#56 TERRA NUTRI ... ativo`, os demais como inativos (perto de 35), e `Resumo:` sem Traceback. Nada gravado: `SELECT COUNT(*) FROM interno` igual antes e depois.

- [ ] **Step 4: Rodada real com o Hugo**

Com o ok do Hugo: confirmar que `whatsapp_notificacoes.clientes.forcar_destino` tem o número dele e que `cadastro_embarcador.forcar_destino_email` está ausente (vai pro Hugo). Rodar `systemctl start stokki-embarcadores-novos.service`, esperar sair de `activating` (`systemctl show -p ActiveState --value`), conferir no log NONNA e RIC convidados, ~35 inativos, mensagens no grupo ALERTAS e no WhatsApp/e-mail do Hugo com o link. Abrir o link num navegador, definir PIN, preencher a tela e aprovar no `/inicio`. Então `systemctl enable --now stokki-embarcadores-novos.timer` e conferir a 1ª rodada automática com a skill `conferir-rodada`.

- [ ] **Step 5: Registrar na memória**

Criar `project_embarcadores_novos_stokki.md` com o estado (deployado, timer ligado, envio preso no Hugo) e a pendência "liberar envio real = esvaziar os dois forcar_destino, decisão do Hugo", e a linha no `MEMORY.md`.
