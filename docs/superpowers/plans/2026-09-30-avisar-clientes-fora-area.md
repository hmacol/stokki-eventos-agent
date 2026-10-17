# Avisar clientes fora da área — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Botão "Avisar clientes" no bloco "Fora da área de atendimento" do planejamento que manda, de uma vez, e-mail e WhatsApp (grupo do cliente) aos embarcadores dos pedidos selecionados, com prévia e registro; mais a tela de cadastro do grupo de WhatsApp por embarcador.

**Architecture:** Lógica pura em `avisar_fora_area.py` (raiz): prévia, texto, envio e registro na tabela `avisos_fora_area`. O e-mail reaproveita o texto de `roteirizacao/notificar_area_nao_atendida.py`; o WhatsApp sai por `notificar_whatsapp.despachar` com `grupo_id` do cliente e sem contar no teto diário. O painel (`painel_agentes/painel_agentes.py`) só expõe as rotas HTTP; a tela de cadastro segue o padrão de módulo com `registrar(app, ...)` (como `atendimento_chamados.py`), não Blueprint.

**Tech Stack:** Python 3.11, Flask, SQLite (`dados/dados.db`), `unittest`, gateway OpenWA (REST), SMTP via `email_utils.enviar_email`.

**Spec:** `docs/superpowers/specs/2026-09-30-avisar-clientes-fora-area-design.md`

## Global Constraints

- Python **sempre `py -3.11`**; testes com `py -3.11 -m unittest <modulo>` (sem pytest).
- Idioma: português em código, comentários, commits e mensagens. Comentários e strings de log **sem acento** nos arquivos que já seguem esse padrão (`notificar_whatsapp.py`, `integracao_openwa.py`, `atendimento_chamados.py`); com acento onde o arquivo já usa (`planejamento_rotas.py`, templates).
- Commits: `Área: o que mudou e por quê`, terminados com `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Só `git add` dos arquivos da tarefa; nunca `git add -A`. Antes de cada commit: `git status --short` e `git log --oneline -5` (outras sessões editam o mesmo working tree).
- Nada de commit/push/deploy sem o Hugo pedir: o plano deixa os passos de commit; o executor faz o commit local, mas **não faz push**.
- `MAPA_DO_SISTEMA.txt` atualizado no mesmo commit que cria módulo/tela/tabela.
- Níveis de acesso: rotas novas do planejamento e da tela = `("total", "operador")`; `_menu_lateral_nav.html` espelha o `niveis=` da rota.
- Texto do WhatsApp ao cliente **não** tem o limite de 200 caracteres; lista até 10 pedidos.
- WhatsApp ao cliente **não** conta no teto diário nem na janela de repetição; respeita intervalo mínimo e disjuntor.
- `pedidos_area_notificada` não é lida nem escrita por nada deste plano.
- Config: `avisos_fora_area.forcar_destino` (string vazia por padrão) redireciona só o e-mail.

## Review Focus

1. `interno.email` com vários endereços separados por `;`, `,` ou tab (já acontece hoje): todos recebem e o registro guarda a lista — teste em Task 4 (`test_email_com_varios_enderecos`).
2. Dois embarcadores diferentes com pedidos no mesmo clique, o primeiro falhando no SMTP: o segundo ainda recebe e-mail e WhatsApp — teste em Task 4 (`test_falha_em_um_bloco_nao_para_o_proximo`).
3. `service_ids` repetidos ou inexistentes no POST (clique duplo, card já saiu do pool): sem duplicar linha e sem 500 — teste em Task 4 (`test_ids_repetidos_ou_desconhecidos`).
4. `sender_id` sem linha em `interno` (cliente novo ainda não cadastrado): bloco aparece com nome "Remetente N", sem e-mail e sem grupo, não enviável — teste em Task 4 (`test_embarcador_sem_cadastro`).
5. Grupo salvo na tela com id que não é `...@g.us` (usuário cola o link de convite): 400 e nada gravado — teste em Task 7 (`test_formato_invalido`).

---

### Task 1: `contar_no_teto` em `notificar_whatsapp.despachar`

**Files:**
- Modify: `notificar_whatsapp.py:321-345` (`_motivo_para_nao_enviar`), `:389-432` (`_despachar`, `despachar`)
- Test: `test_notificar_whatsapp.py`

**Interfaces:**
- Produces: `despachar(config, origem, tipo, texto, assinatura=None, modo_teste=False, conn=None, agora=None, dormir=time.sleep, grupo_id=None, contar_no_teto=True) -> str`. Com `contar_no_teto=False`: ignora "teto diario atingido" e "repetida dentro da janela", mantém "canal em pausa", intervalo mínimo e registro em `notificacoes_whatsapp`.

- [ ] **Step 1: Escrever os testes que falham**

Em `test_notificar_whatsapp.py`, depois da classe `TestDespacharVolume`, adicionar:

```python
class TestDespacharForaDoTeto(_ComBanco):
    """Envio manual ao cliente (avisar_fora_area.py): nao conta no teto
    diario nem na janela de repeticao, mas respeita intervalo e disjuntor."""

    def test_ignora_teto_diario(self):
        for i in range(3):   # teto_diario = 3 no _config()
            self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=i)), "enviado")
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=10)), "nao_enviado")
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=11), contar_no_teto=False), "enviado")
        self.assertEqual(self.linhas()[-1][1], "enviado")

    def test_ignora_janela_de_repeticao(self):
        self.assertEqual(self.despachar(assinatura="x", contar_no_teto=False), "enviado")
        self.assertEqual(self.despachar(assinatura="x", agora=AGORA + timedelta(minutes=1),
                                        contar_no_teto=False), "enviado")

    def test_respeita_o_disjuntor(self):
        self.enviar.return_value = (False, None)
        with patch.object(nw, "_alertar_se_canal_parou"):
            for i in range(3):
                self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=i)), "falhou")
            self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=5), contar_no_teto=False),
                             "nao_enviado")

    def test_respeita_o_intervalo(self):
        self.despachar()
        self.despachar(agora=AGORA + timedelta(seconds=5), contar_no_teto=False)
        self.dormir.assert_called_once_with(15.0)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_notificar_whatsapp.TestDespacharForaDoTeto -v`
Expected: 4 falhas com `TypeError: ... unexpected keyword argument 'contar_no_teto'`.

- [ ] **Step 3: Implementar**

Em `_motivo_para_nao_enviar`, trocar a assinatura e envolver teto e janela:

```python
def _motivo_para_nao_enviar(conn, cfg: dict, origem: str, assinatura: str | None, agora: datetime,
                            contar_no_teto: bool = True) -> str | None:
    if assinatura and contar_no_teto:
        desde = ...  # bloco existente da janela, sem mudanca
    # Disjuntor: ... (bloco existente, sem mudanca)
    if not contar_no_teto:
        return None
    inicio_do_dia = ...  # bloco existente do teto, sem mudanca
```

Em `_despachar`, adicionar o parâmetro `contar_no_teto` no fim da assinatura e passar adiante:

```python
def _despachar(config, origem, tipo, texto, assinatura, modo_teste, conn, agora, dormir, grupo_id,
               contar_no_teto=True) -> str:
    ...
        motivo = _motivo_para_nao_enviar(conn, cfg, origem, assinatura, agora, contar_no_teto)
```

Em `despachar`, adicionar `contar_no_teto: bool = True` à assinatura e ao repasse, e completar a docstring: "`contar_no_teto=False` (envio manual ao cliente, avisar_fora_area.py): sem teto diario nem janela de repeticao; intervalo e disjuntor continuam."

- [ ] **Step 4: Rodar tudo do módulo**

Run: `py -3.11 -m unittest test_notificar_whatsapp -v`
Expected: todos passam (os antigos continuam iguais porque o default é `True`).

- [ ] **Step 5: Commit**

```bash
git add notificar_whatsapp.py test_notificar_whatsapp.py
git commit -m "WhatsApp: despachar aceita envio fora do teto diario (aviso manual ao cliente)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `integracao_openwa.listar_grupos`

**Files:**
- Modify: `integracao_openwa.py` (função nova no fim)
- Test: `test_integracao_openwa.py`

**Interfaces:**
- Produces: `listar_grupos(cfg: dict) -> list[dict] | None`. `GET {base_url}/sessions/{sessao}/groups`, header `X-API-Key`, timeout 15. Devolve `[{"id": "...@g.us", "nome": "..."}]` ordenado por nome (sem diferenciar maiúsculas); `None` quando não configurado ou quando a rede/JSON falhar (o chamador mostra "gateway fora do ar"). Nunca levanta exceção.

- [ ] **Step 1: Testes que falham**

No fim de `test_integracao_openwa.py`:

```python
class TestListarGrupos(unittest.TestCase):
    def test_lista_ordenada_por_nome(self):
        corpo = [{"id": "2@g.us", "name": "zeta"}, {"id": "1@g.us", "name": "Alfa"},
                 {"id": "3@g.us", "name": "beta", "linkedParentJID": None}]
        with patch.object(openwa.requests, "get", return_value=_resposta(corpo=corpo)) as get:
            self.assertEqual(openwa.listar_grupos(CFG), [
                {"id": "1@g.us", "nome": "Alfa"}, {"id": "3@g.us", "nome": "beta"}, {"id": "2@g.us", "nome": "zeta"}])
        args, kwargs = get.call_args
        self.assertEqual(args[0], "http://127.0.0.1:2785/api/sessions/abc-123/groups")
        self.assertEqual(kwargs["headers"], {"X-API-Key": "chave"})
        self.assertEqual(kwargs["timeout"], 15)

    def test_sem_config_nao_chama_a_rede(self):
        with patch.object(openwa.requests, "get") as get:
            self.assertIsNone(openwa.listar_grupos({}))
            self.assertIsNone(openwa.listar_grupos(dict(CFG, sessao="")))
        get.assert_not_called()

    def test_falha_de_rede_http_ou_json_vira_none(self):
        with patch.object(openwa.requests, "get", side_effect=requests.ConnectionError("x")):
            self.assertIsNone(openwa.listar_grupos(CFG))
        with patch.object(openwa.requests, "get", return_value=_resposta(status=401)):
            self.assertIsNone(openwa.listar_grupos(CFG))
        with patch.object(openwa.requests, "get", return_value=_resposta(json_invalido=True)):
            self.assertIsNone(openwa.listar_grupos(CFG))
        with patch.object(openwa.requests, "get", return_value=_resposta(corpo={"nao": "lista"})):
            self.assertIsNone(openwa.listar_grupos(CFG))

    def test_item_sem_id_ou_sem_nome_e_pulado(self):
        corpo = [{"id": "1@g.us"}, {"name": "x"}, {"id": "2@g.us", "name": "Ok"}]
        with patch.object(openwa.requests, "get", return_value=_resposta(corpo=corpo)):
            self.assertEqual(openwa.listar_grupos(CFG), [{"id": "2@g.us", "nome": "Ok"}])
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_integracao_openwa.TestListarGrupos -v`
Expected: `AttributeError: module 'integracao_openwa' has no attribute 'listar_grupos'`.

- [ ] **Step 3: Implementar**

No fim de `integracao_openwa.py`:

```python
def listar_grupos(cfg: dict) -> list[dict] | None:
    """GET /sessions/{sessao}/groups: grupos de que o numero participa, como
    [{"id": "...@g.us", "nome": "..."}] ordenados por nome. None quando nao
    configurado ou quando o gateway nao responde (quem chama avisa que o
    gateway esta fora do ar). Nunca levanta excecao."""
    if not configurado(cfg):
        return None
    url = f"{cfg['base_url'].rstrip('/')}/sessions/{cfg['sessao']}/groups"
    try:
        resp = requests.get(url, headers={"X-API-Key": cfg["api_key"]}, timeout=_TIMEOUT)
        resp.raise_for_status()
        dados = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"Falha ao listar grupos no OpenWA: {exc}")
        return None
    if not isinstance(dados, list):
        return None
    grupos = [{"id": g["id"], "nome": g["name"]} for g in dados
              if isinstance(g, dict) and g.get("id") and g.get("name")]
    return sorted(grupos, key=lambda g: g["nome"].lower())
```

Atualizar a docstring do módulo: "usado pelas notificacoes internas (notificar_whatsapp.py) e pelo aviso manual aos clientes (avisar_fora_area.py)".

- [ ] **Step 4: Rodar**

Run: `py -3.11 -m unittest test_integracao_openwa -v`
Expected: todos passam.

- [ ] **Step 5: Commit**

```bash
git add integracao_openwa.py test_integracao_openwa.py
git commit -m "OpenWA: listar grupos do numero (cadastro do grupo por embarcador)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: extrair `assunto_e_conteudo` em `notificar_area_nao_atendida.py`

**Files:**
- Modify: `roteirizacao/notificar_area_nao_atendida.py:133-172` (`_montar_conteudo`), `:217-219` (assunto dentro de `notificar_remetentes`)
- Test: `roteirizacao/test_notificar_area_nao_atendida.py` (novo)

**Interfaces:**
- Produces: `assunto_e_conteudo(nome_remetente: str, tipo: str, pedidos: list[dict]) -> tuple[str, str]` → `(assunto, conteudo_html)`. `pedidos` são serviços da Vuupt (usa `code` e `address`/cidade via `extrair_cidade`/`extrair_uf`). Comportamento do e-mail da roteirização não muda.

- [ ] **Step 1: Teste que falha**

Criar `roteirizacao/test_notificar_area_nao_atendida.py`:

```python
# -*- coding: utf-8 -*-
"""
Texto do e-mail de area nao atendida, compartilhado entre a roteirizacao
(notificar_remetentes) e o botao "Avisar clientes" do planejamento
(avisar_fora_area.py), 30/09/2026.

    py -3.11 -m unittest roteirizacao.test_notificar_area_nao_atendida
"""
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import notificar_area_nao_atendida as na  # noqa: E402

PEDIDOS = [{"id": 1, "code": "#PS-1", "address": "Rua A, 1 - Centro, Curitiba - PR, 80000-000"},
           {"id": 2, "code": "PS-2", "address": "Rua B, 2 - Centro, Blumenau - SC, 89000-000"}]


class AssuntoEConteudo(unittest.TestCase):
    def test_fora_sp(self):
        assunto, html = na.assunto_e_conteudo("ACME", na.TIPO_FORA_SP, PEDIDOS)
        self.assertEqual(assunto, "[Freshlog] 2 pedido(s) — fora de SP — confirmar redespacho")
        self.assertIn("Entrega fora do estado de São Paulo", html)
        self.assertIn("Olá, ACME.", html)
        self.assertIn("#PS-1", html)
        self.assertIn("#PS-2", html)
        self.assertIn("Curitiba - PR", html)

    def test_sp_nao_atendido(self):
        assunto, html = na.assunto_e_conteudo("", na.TIPO_SP_NAO_ATENDIDO, PEDIDOS[:1])
        self.assertEqual(assunto, "[Freshlog] 1 pedido(s) — cotação necessária")
        self.assertIn("Região fora da área de atendimento", html)

    def test_escapa_html(self):
        _, html = na.assunto_e_conteudo("<b>x</b>", na.TIPO_FORA_SP, PEDIDOS[:1])
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest roteirizacao.test_notificar_area_nao_atendida -v`
Expected: `AttributeError: ... has no attribute 'assunto_e_conteudo'`.

Se `extrair_cidade` não extrair "Curitiba" desse `address` (a função lê o serviço; conferir em `roteirizacao/regioes_dia_fixo.py` qual campo ela usa e ajustar a fixture `PEDIDOS` para o formato real, por exemplo `{"address": ..., "city": ..., "state": ...}`), corrigir a fixture, não a função.

- [ ] **Step 3: Implementar**

Em `notificar_area_nao_atendida.py`, logo depois de `_montar_conteudo`:

```python
def assunto_e_conteudo(nome_remetente: str, tipo: str, pedidos: list[dict]) -> tuple[str, str]:
    """Assunto e miolo HTML do e-mail de área não atendida -- usado pela
    roteirização (notificar_remetentes) e pelo botão "Avisar clientes" do
    planejamento (avisar_fora_area.py, 30/09), pra não ter dois textos."""
    assunto_tipo = "cotação necessária" if tipo == TIPO_SP_NAO_ATENDIDO else "fora de SP — confirmar redespacho"
    assunto = f"[Freshlog] {len(pedidos)} pedido(s) — {assunto_tipo}"
    return assunto, _montar_conteudo(nome_remetente, tipo, pedidos)
```

Em `notificar_remetentes`, substituir as três linhas:

```python
        assunto_tipo = "cotação necessária" if tipo == TIPO_SP_NAO_ATENDIDO else "fora de SP — confirmar redespacho"
        assunto = f"[Freshlog] {len(pedidos)} pedido(s) — {assunto_tipo}"
        conteudo = _montar_conteudo(emb["nome"], tipo, pedidos)
```

por:

```python
        assunto, conteudo = assunto_e_conteudo(emb["nome"], tipo, pedidos)
```

- [ ] **Step 4: Rodar**

Run: `py -3.11 -m unittest roteirizacao.test_notificar_area_nao_atendida -v` e `py -3.11 -m py_compile roteirizacao/notificar_area_nao_atendida.py`
Expected: passam.

- [ ] **Step 5: Commit**

```bash
git add roteirizacao/notificar_area_nao_atendida.py roteirizacao/test_notificar_area_nao_atendida.py
git commit -m "Roteirizacao: texto do e-mail de area nao atendida numa funcao reutilizavel

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `avisar_fora_area.py` — tabela, coluna, prévia, texto, envio

**Files:**
- Create: `avisar_fora_area.py` (raiz)
- Test: `test_avisar_fora_area.py` (raiz)

**Interfaces:**
- Consumes: `notificar_whatsapp.despachar(..., grupo_id=, contar_no_teto=False)` (Task 1); `notificar_area_nao_atendida.assunto_e_conteudo` (Task 3); `email_utils.enviar_email(destinos, assunto, corpo, config_email) -> bool` e `envelope_html(conteudo, rodape=, cor_acento=)`; `regioes_dia_fixo.extrair_cidade(servico)`, `extrair_uf(servico)`.
- Produces:
  - `TIPO_SP = "sp_nao_atendido"`, `TIPO_FORA_SP = "fora_sp"`, `MAX_PEDIDOS_WHATSAPP = 10`, `DB_PATH`.
  - `conectar(db_path=DB_PATH) -> sqlite3.Connection` (row_factory Row; cria `avisos_fora_area`; garante a coluna).
  - `garantir_coluna_grupo(conn) -> None` (idempotente; `ALTER TABLE interno ADD COLUMN whatsapp_grupo_id TEXT`).
  - `embarcadores(conn) -> dict[int, dict]`: `sender_id -> {"nome", "emails": [..], "whatsapp_grupo_id"}`.
  - `avisados_por_service_id(conn) -> dict[int, dict]`: último `enviado` por pedido: `{"em": "2026-09-30 10:12", "por": "hugo"}`.
  - `texto_whatsapp(tipo, pedidos, com_email: bool) -> str`; `pedidos` = `[{"codigo", "cidade", "uf"}]`.
  - `montar_previa(servicos, tipos_area, config, conn, grupos_nomes=None) -> dict` → `{"blocos": [...], "ignorados": [service_id...], "whatsapp_disponivel": bool}`.
  - `enviar(itens, servicos, tipos_area, config, por, conn, modo_teste=False, agora=None) -> dict` → `{"resultados": [...], "ignorados": [...]}`.
  - `whatsapp_disponivel(config) -> bool`.

- [ ] **Step 1: Escrever os testes**

Criar `test_avisar_fora_area.py`:

```python
# -*- coding: utf-8 -*-
"""
Aviso em massa aos embarcadores sobre pedidos fora da area de atendimento
(botao "Avisar clientes" do planejamento, Hugo 30/09/2026).

    py -3.11 -m unittest test_avisar_fora_area
"""
import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch

import avisar_fora_area as afa

AGORA = datetime(2026, 9, 30, 10, 12, 0)
S1 = {"id": 1, "code": "#PS-1", "sender_id": 10, "address": "Rua A, 1 - Centro, Curitiba - PR, 80000-000"}
S2 = {"id": 2, "code": "PS-2", "sender_id": 10, "address": "Rua B, 2 - Centro, Blumenau - SC, 89000-000"}
S3 = {"id": 3, "code": "PS-3", "sender_id": 20, "address": "Rua C, 3 - Centro, Bauru - SP, 17000-000"}
S4 = {"id": 4, "code": "PS-4", "sender_id": 10, "address": "Rua D, 4 - Centro, Bauru - SP, 17000-000"}
S5 = {"id": 5, "code": "PS-5", "sender_id": 30, "address": "Rua E, 5 - Centro, Manaus - AM, 69000-000"}
SERVICOS = [S1, S2, S3, S4, S5]
TIPOS = {1: afa.TIPO_FORA_SP, 2: afa.TIPO_FORA_SP, 3: afa.TIPO_SP, 4: afa.TIPO_SP, 5: afa.TIPO_FORA_SP}


def _config(**wa):
    cfg = {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s", "grupo_id": "9@g.us"}
    cfg.update(wa)
    return {"whatsapp_notificacoes": cfg, "email": {"remetente": "a@b.com"}, "avisos_fora_area": {}}


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self.conn = afa.conectar(":memory:")
        self.conn.executescript("""
            CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT,
                                  email TEXT, sender_id INTEGER);
            INSERT INTO interno VALUES ('1', 'ACME LTDA', 'ACME', 'a@acme.com; b@acme.com', 10),
                                       ('2', 'BETA', NULL, '', 20);
        """)
        afa.garantir_coluna_grupo(self.conn)
        self.conn.execute("UPDATE interno SET whatsapp_grupo_id = '111@g.us' WHERE sender_id = 10")
        self.addCleanup(self.conn.close)
        self.email = patch.object(afa, "enviar_email", return_value=True)
        self.enviar_email = self.email.start()
        self.addCleanup(self.email.stop)
        self.wa = patch.object(afa.notificar_whatsapp, "despachar", return_value="enviado")
        self.despachar = self.wa.start()
        self.addCleanup(self.wa.stop)

    def linhas(self):
        return self.conn.execute(
            "SELECT service_id, sender_id, tipo, canal, situacao, destino, por FROM avisos_fora_area ORDER BY id"
        ).fetchall()


class Coluna(unittest.TestCase):
    def test_garantir_coluna_duas_vezes(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, email TEXT, sender_id INTEGER)")
        afa.garantir_coluna_grupo(conn)
        afa.garantir_coluna_grupo(conn)
        colunas = [r[1] for r in conn.execute("PRAGMA table_info(interno)")]
        self.assertEqual(colunas.count("whatsapp_grupo_id"), 1)


class Texto(unittest.TestCase):
    PEDIDOS = [{"codigo": "PS-1", "cidade": "Curitiba", "uf": "PR"}, {"codigo": "PS-2", "cidade": "Blumenau", "uf": "SC"}]

    def test_fora_sp_com_email(self):
        self.assertEqual(afa.texto_whatsapp(afa.TIPO_FORA_SP, self.PEDIDOS, com_email=True), "\n".join([
            "⚠️ *Fresh Log · pedidos fora da área de atendimento*",
            "PS-1 · Curitiba/PR",
            "PS-2 · Blumenau/SC",
            "Esses destinos ficam fora do estado de SP. Haverá redespacho por transportadora? "
            "Se sim, nos envie o endereço completo com CEP e o nome da transportadora. Detalhes no e-mail.",
        ]))

    def test_sp_sem_email(self):
        texto = afa.texto_whatsapp(afa.TIPO_SP, self.PEDIDOS[:1], com_email=False)
        self.assertTrue(texto.endswith("Se quiser, fazemos uma cotação de entrega dedicada."))
        self.assertNotIn("Detalhes no e-mail", texto)

    def test_ate_dez_pedidos_e_mais_n(self):
        pedidos = [{"codigo": f"PS-{i}", "cidade": "X", "uf": "PR"} for i in range(13)]
        linhas = afa.texto_whatsapp(afa.TIPO_FORA_SP, pedidos, com_email=True).split("\n")
        self.assertEqual(len([l for l in linhas if l.startswith("PS-")]), 10)
        self.assertEqual(linhas[11], "e mais 3")

    def test_sem_cidade(self):
        texto = afa.texto_whatsapp(afa.TIPO_FORA_SP, [{"codigo": "PS-1", "cidade": None, "uf": None}], True)
        self.assertIn("\nPS-1\n", texto)

    def test_perde_formatacao_do_whatsapp(self):
        texto = afa.texto_whatsapp(afa.TIPO_FORA_SP, [{"codigo": "*PS-1*", "cidade": "_X_", "uf": "PR"}], True)
        self.assertIn("\nPS-1 · X/PR\n", texto)


class Previa(_ComBanco):
    def test_agrupa_por_embarcador_e_tipo(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn)
        chaves = [(b["sender_id"], b["tipo"]) for b in previa["blocos"]]
        self.assertEqual(chaves, [(10, afa.TIPO_FORA_SP), (10, afa.TIPO_SP), (20, afa.TIPO_SP), (30, afa.TIPO_FORA_SP)])
        acme = previa["blocos"][0]
        self.assertEqual(acme["nome"], "ACME")
        self.assertEqual(acme["emails"], ["a@acme.com", "b@acme.com"])
        self.assertEqual(acme["whatsapp_grupo_id"], "111@g.us")
        self.assertEqual([p["codigo"] for p in acme["pedidos"]], ["PS-1", "PS-2"])
        self.assertEqual(acme["pedidos"][0]["cidade"], "Curitiba")
        self.assertEqual(acme["pedidos"][0]["uf"], "PR")
        self.assertTrue(acme["enviavel"])
        self.assertIsNone(acme["ultimo_aviso"])
        self.assertTrue(previa["whatsapp_disponivel"])

    def test_nome_do_grupo_quando_informado(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn, grupos_nomes={"111@g.us": "ACME x Fresh"})
        self.assertEqual(previa["blocos"][0]["whatsapp_grupo_nome"], "ACME x Fresh")

    def test_sem_email_e_sem_grupo_nao_enviavel(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn)
        beta = previa["blocos"][2]
        self.assertEqual(beta["nome"], "BETA")
        self.assertEqual(beta["emails"], [])
        self.assertIsNone(beta["whatsapp_grupo_id"])
        self.assertFalse(beta["enviavel"])

    def test_embarcador_sem_cadastro(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn)
        sem = previa["blocos"][3]
        self.assertEqual(sem["nome"], "Remetente 30")
        self.assertFalse(sem["enviavel"])

    def test_pedido_fora_dos_tipos_e_ignorado(self):
        previa = afa.montar_previa(SERVICOS + [{"id": 9, "code": "PS-9", "sender_id": 10}], TIPOS, _config(), self.conn)
        self.assertEqual(previa["ignorados"], [9])

    def test_whatsapp_desligado(self):
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(ativo=False), self.conn)
        self.assertFalse(previa["whatsapp_disponivel"])
        self.assertFalse(afa.whatsapp_disponivel(_config(api_key="")))
        self.assertFalse(afa.whatsapp_disponivel({}))

    def test_ultimo_aviso_vem_do_registro(self):
        afa.enviar([{"sender_id": 10, "tipo": afa.TIPO_FORA_SP, "service_ids": [1, 2], "canais": ["email"]}],
                   SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        previa = afa.montar_previa(SERVICOS, TIPOS, _config(), self.conn)
        self.assertEqual(previa["blocos"][0]["ultimo_aviso"], {"em": "30/09 10:12", "por": "hugo"})
        self.assertEqual(previa["blocos"][0]["pedidos"][0]["avisado_em"], "30/09 10:12")
        self.assertIsNone(previa["blocos"][1]["ultimo_aviso"])


class Enviar(_ComBanco):
    def item(self, sender_id=10, tipo=afa.TIPO_FORA_SP, ids=(1, 2), canais=("email", "whatsapp")):
        return {"sender_id": sender_id, "tipo": tipo, "service_ids": list(ids), "canais": list(canais)}

    def test_envia_email_e_whatsapp_e_registra(self):
        r = afa.enviar([self.item()], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["resultados"], [{"sender_id": 10, "tipo": afa.TIPO_FORA_SP, "email": "enviado",
                                            "whatsapp": "enviado", "detalhe": ""}])
        destinos, assunto, corpo, cfg_email = self.enviar_email.call_args[0]
        self.assertEqual(destinos, ["a@acme.com", "b@acme.com"])
        self.assertIn("fora de SP", assunto)
        self.assertIn("#PS-1", corpo)
        self.assertEqual(cfg_email, {"remetente": "a@b.com"})
        kw = self.despachar.call_args.kwargs
        self.assertEqual(kw["grupo_id"], "111@g.us")
        self.assertFalse(kw["contar_no_teto"])
        self.assertEqual(self.despachar.call_args.args[1:3], ("avisar_fora_area", "fora_area_cliente"))
        self.assertIn("Detalhes no e-mail.", self.despachar.call_args.args[3])
        self.assertEqual(self.linhas(), [
            (1, 10, afa.TIPO_FORA_SP, "email", "enviado", "a@acme.com; b@acme.com", "hugo"),
            (2, 10, afa.TIPO_FORA_SP, "email", "enviado", "a@acme.com; b@acme.com", "hugo"),
            (1, 10, afa.TIPO_FORA_SP, "whatsapp", "enviado", "111@g.us", "hugo"),
            (2, 10, afa.TIPO_FORA_SP, "whatsapp", "enviado", "111@g.us", "hugo"),
        ])

    def test_email_com_varios_enderecos(self):
        afa.enviar([self.item(canais=("email",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(self.enviar_email.call_args[0][0], ["a@acme.com", "b@acme.com"])
        self.despachar.assert_not_called()

    def test_forcar_destino_redireciona_so_o_email(self):
        config = _config()
        config["avisos_fora_area"] = {"forcar_destino": "hugo@x.com"}
        afa.enviar([self.item()], SERVICOS, TIPOS, config, "hugo", self.conn, agora=AGORA)
        self.assertEqual(self.enviar_email.call_args[0][0], ["hugo@x.com"])
        self.assertEqual(self.despachar.call_args.kwargs["grupo_id"], "111@g.us")
        self.assertEqual(self.linhas()[0][5], "hugo@x.com")

    def test_so_whatsapp_tira_a_frase_do_email(self):
        afa.enviar([self.item(canais=("whatsapp",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.enviar_email.assert_not_called()
        self.assertNotIn("Detalhes no e-mail", self.despachar.call_args.args[3])

    def test_falha_em_um_bloco_nao_para_o_proximo(self):
        self.enviar_email.side_effect = [False, True]
        self.conn.execute("UPDATE interno SET email = 'b@beta.com' WHERE sender_id = 20")
        r = afa.enviar([self.item(canais=("email",)), self.item(20, afa.TIPO_SP, (3,), ("email",))],
                       SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual([x["email"] for x in r["resultados"]], ["falhou", "enviado"])
        self.assertEqual([l[4] for l in self.linhas()], ["falhou", "falhou", "enviado"])

    def test_whatsapp_falhou_ou_nao_enviado_e_registrado(self):
        self.despachar.return_value = "nao_enviado"
        r = afa.enviar([self.item(canais=("whatsapp",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["resultados"][0]["whatsapp"], "nao_enviado")
        self.assertEqual([l[4] for l in self.linhas()], ["falhou", "falhou"])

    def test_bloco_sem_canal_utilizavel_e_pulado(self):
        r = afa.enviar([self.item(20, afa.TIPO_SP, (3,))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["resultados"][0]["email"], "pulado")
        self.assertEqual(r["resultados"][0]["whatsapp"], "pulado")
        self.assertIn("sem e-mail", r["resultados"][0]["detalhe"])
        self.assertEqual(self.linhas(), [])

    def test_whatsapp_desligado_devolve_desligado(self):
        self.despachar.return_value = "desligado"
        r = afa.enviar([self.item(canais=("whatsapp",))], SERVICOS, TIPOS, _config(ativo=False), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["resultados"][0]["whatsapp"], "desligado")
        self.assertEqual(self.linhas(), [])

    def test_ids_repetidos_ou_desconhecidos(self):
        r = afa.enviar([self.item(ids=(1, 1, 99), canais=("email",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["ignorados"], [99])
        self.assertEqual(len(self.linhas()), 1)

    def test_pedido_que_saiu_da_area_e_ignorado(self):
        tipos = dict(TIPOS)
        del tipos[2]
        r = afa.enviar([self.item(canais=("email",))], SERVICOS, tipos, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["ignorados"], [2])
        self.assertIn("#PS-1", self.enviar_email.call_args[0][2])
        self.assertNotIn("#PS-2", self.enviar_email.call_args[0][2])

    def test_tipo_do_item_diferente_da_classificacao_e_ignorado(self):
        r = afa.enviar([self.item(tipo=afa.TIPO_SP, canais=("email",))], SERVICOS, TIPOS, _config(), "hugo", self.conn, agora=AGORA)
        self.assertEqual(r["ignorados"], [1, 2])
        self.enviar_email.assert_not_called()

    def test_modo_teste_nao_envia_nem_registra(self):
        self.despachar.return_value = "modo_teste"   # o despachar real devolve isso em modo_teste
        r = afa.enviar([self.item()], SERVICOS, TIPOS, _config(), "hugo", self.conn, modo_teste=True, agora=AGORA)
        self.enviar_email.assert_not_called()
        self.assertEqual(r["resultados"][0]["email"], "modo_teste")
        self.assertEqual(r["resultados"][0]["whatsapp"], "modo_teste")
        self.assertTrue(self.despachar.call_args.kwargs["modo_teste"])
        self.assertEqual(self.linhas(), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest test_avisar_fora_area -v`
Expected: `ModuleNotFoundError: No module named 'avisar_fora_area'`.

- [ ] **Step 3: Implementar `avisar_fora_area.py`**

```python
# -*- coding: utf-8 -*-
"""
avisar_fora_area.py

Aviso em massa aos EMBARCADORES sobre pedidos fora da area de atendimento,
disparado pelo botao "Avisar clientes" do bloco "Fora da area" do
planejamento (Hugo, 30/09/2026). E-mail (mesmo texto da roteirizacao,
notificar_area_nao_atendida.assunto_e_conteudo) + WhatsApp no grupo que o
cliente ja tem com a Fresh (interno.whatsapp_grupo_id), pelo numero do Hugo
(notificar_whatsapp.despachar, fora do teto diario).

Nao mexe em pedidos_area_notificada (marca da roteirizacao, que continua
tirando o pedido da rota automatica e mandando o e-mail so pro Hugo). O
historico do botao fica em avisos_fora_area, uma linha por pedido e canal.

config.yaml (opcional):
    avisos_fora_area:
      forcar_destino: ""   # preenchido = e-mail vai so pra esse endereco
"""
import html as _html
import logging
import re
import sqlite3
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent
if str(_RAIZ / "roteirizacao") not in sys.path:
    sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import notificar_whatsapp
from email_utils import COR_DESTAQUE, envelope_html, enviar_email
from notificar_area_nao_atendida import TIPO_FORA_SP, TIPO_SP_NAO_ATENDIDO, assunto_e_conteudo
from regioes_dia_fixo import extrair_cidade, extrair_uf

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"
TIPO_SP = TIPO_SP_NAO_ATENDIDO
MAX_PEDIDOS_WHATSAPP = 10
ORIGEM_WHATSAPP = "avisar_fora_area"
TIPO_WHATSAPP = "fora_area_cliente"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS avisos_fora_area (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id  INTEGER NOT NULL,
    codigo      TEXT,
    sender_id   INTEGER,
    tipo        TEXT NOT NULL,
    canal       TEXT NOT NULL,
    situacao    TEXT NOT NULL,
    destino     TEXT,
    por         TEXT,
    criado_em   TEXT NOT NULL
)"""

FRASES = {
    TIPO_FORA_SP: ("Esses destinos ficam fora do estado de SP. Haverá redespacho por transportadora? "
                   "Se sim, nos envie o endereço completo com CEP e o nome da transportadora."),
    TIPO_SP: "Esses destinos ficam fora da nossa área de atendimento padrão. Se quiser, fazemos uma cotação de entrega dedicada.",
}


# --- Banco ----------------------------------------------------------------------

def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def garantir_coluna_grupo(conn) -> None:
    """ALTER TABLE idempotente: interno.whatsapp_grupo_id (30/09)."""
    colunas = [r[1] for r in conn.execute("PRAGMA table_info(interno)")]
    if colunas and "whatsapp_grupo_id" not in colunas:
        conn.execute("ALTER TABLE interno ADD COLUMN whatsapp_grupo_id TEXT")
        conn.commit()


def _emails(bruto) -> list[str]:
    return [e.strip() for e in re.split(r"[,;\t]+", bruto or "") if e.strip() and "@" in e]


def embarcadores(conn) -> dict[int, dict]:
    """sender_id -> {nome, emails, whatsapp_grupo_id} (mesmo criterio de nome
    de notificar_area_nao_atendida._carregar_embarcadores_por_sender_id)."""
    garantir_coluna_grupo(conn)
    rows = conn.execute(
        "SELECT sender_id, nome_remetente, apelido, email, whatsapp_grupo_id FROM interno WHERE sender_id IS NOT NULL"
    ).fetchall()
    return {r["sender_id"]: {"nome": r["apelido"] or r["nome_remetente"] or "",
                             "emails": _emails(r["email"]),
                             "whatsapp_grupo_id": (r["whatsapp_grupo_id"] or "").strip() or None}
            for r in rows}


def _rotulo_quando(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d/%m %H:%M")


def avisados_por_service_id(conn) -> dict[int, dict]:
    """Ultimo envio com sucesso por pedido (qualquer canal): {service_id: {em, por}}."""
    rows = conn.execute(
        "SELECT service_id, por, MAX(criado_em) AS em FROM avisos_fora_area "
        "WHERE situacao = 'enviado' GROUP BY service_id").fetchall()
    return {r["service_id"]: {"em": _rotulo_quando(r["em"]), "por": r["por"] or ""} for r in rows}


def _registrar(conn, agora, pedidos, sender_id, tipo, canal, situacao, destino, por):
    conn.executemany(
        "INSERT INTO avisos_fora_area (service_id, codigo, sender_id, tipo, canal, situacao, destino, por, criado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(p["service_id"], p["codigo"], sender_id, tipo, canal, situacao, destino, por,
          agora.isoformat(timespec="seconds")) for p in pedidos])
    conn.commit()


# --- Texto ---------------------------------------------------------------------

def _limpo(texto) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_~`]", "", str(texto or ""))).strip()


def texto_whatsapp(tipo: str, pedidos: list[dict], com_email: bool) -> str:
    """Cada item: {codigo, cidade, uf}. Sem limite de 200 caracteres (lista
    pedidos, como o aviso de insucesso); ate MAX_PEDIDOS_WHATSAPP linhas."""
    linhas = ["⚠️ *Fresh Log · pedidos fora da área de atendimento*"]
    for p in pedidos[:MAX_PEDIDOS_WHATSAPP]:
        codigo = _limpo(p.get("codigo")).lstrip("#")
        cidade, uf = _limpo(p.get("cidade")), _limpo(p.get("uf"))
        lugar = "/".join(x for x in (cidade, uf) if x)
        linhas.append(f"{codigo} · {lugar}" if lugar else codigo)
    resto = len(pedidos) - MAX_PEDIDOS_WHATSAPP
    if resto > 0:
        linhas.append(f"e mais {resto}")
    frase = FRASES.get(tipo, FRASES[TIPO_SP])
    linhas.append(frase + " Detalhes no e-mail." if com_email else frase)
    return "\n".join(linhas)


# --- Previa -----------------------------------------------------------------------

def whatsapp_disponivel(config: dict) -> bool:
    cfg = (config or {}).get("whatsapp_notificacoes") or {}
    return bool(cfg.get("ativo")) and notificar_whatsapp.integracao_openwa.configurado(cfg)


def _pedido(servico: dict) -> dict:
    return {"service_id": servico["id"], "codigo": str(servico.get("code") or "").lstrip("#").strip(),
            "cidade": extrair_cidade(servico), "uf": extrair_uf(servico)}


def _agrupar(servicos: list[dict], tipos_area: dict) -> tuple["OrderedDict[tuple, list]", list[int]]:
    """(sender_id, tipo) -> servicos, na ordem de chegada; ignorados = fora
    dos tipos_area (nao esta mais fora da area) ou repetidos."""
    grupos: "OrderedDict[tuple, list]" = OrderedDict()
    ignorados, vistos = [], set()
    for s in servicos:
        sid = s["id"]
        if sid in vistos:
            continue
        vistos.add(sid)
        tipo = tipos_area.get(sid)
        if not tipo:
            ignorados.append(sid)
            continue
        grupos.setdefault((s.get("sender_id"), tipo), []).append(s)
    return grupos, ignorados


def montar_previa(servicos: list[dict], tipos_area: dict, config: dict, conn,
                  grupos_nomes: dict | None = None) -> dict:
    embs = embarcadores(conn)
    avisados = avisados_por_service_id(conn)
    grupos, ignorados = _agrupar(servicos, tipos_area)
    wa_ok = whatsapp_disponivel(config)
    blocos = []
    for (sender_id, tipo), lista in grupos.items():
        emb = embs.get(sender_id) or {"nome": "", "emails": [], "whatsapp_grupo_id": None}
        pedidos = []
        for s in lista:
            p = _pedido(s)
            aviso = avisados.get(s["id"])
            p["avisado_em"] = aviso["em"] if aviso else None
            p["avisado_por"] = aviso["por"] if aviso else None
            pedidos.append(p)
        avisos = [avisados[s["id"]] for s in lista if s["id"] in avisados]
        ultimo = max(avisos, key=lambda a: datetime.strptime(a["em"], "%d/%m %H:%M")) if avisos else None
        grupo_id = emb["whatsapp_grupo_id"]
        blocos.append({
            "sender_id": sender_id, "nome": emb["nome"] or f"Remetente {sender_id}", "tipo": tipo,
            "pedidos": pedidos, "emails": emb["emails"], "whatsapp_grupo_id": grupo_id,
            "whatsapp_grupo_nome": (grupos_nomes or {}).get(grupo_id) if grupo_id else None,
            "ultimo_aviso": ultimo,
            "enviavel": bool(emb["emails"] or (grupo_id and wa_ok)),
        })
    # Ordem da previa: por nome do embarcador, e dentro dele fora_sp antes
    # de sp_nao_atendido (ordem alfabetica dos tipos).
    blocos.sort(key=lambda b: (b["nome"].lower(), b["tipo"]))
    return {"blocos": blocos, "ignorados": ignorados, "whatsapp_disponivel": wa_ok}


# --- Envio ----------------------------------------------------------------------

def _forcar_destino(config: dict) -> str:
    return str(((config or {}).get("avisos_fora_area") or {}).get("forcar_destino") or "").strip()


def enviar(itens: list[dict], servicos: list[dict], tipos_area: dict, config: dict, por: str, conn,
           modo_teste: bool = False, agora: datetime | None = None) -> dict:
    """itens: [{sender_id, tipo, service_ids, canais}]. Reclassifica com
    tipos_area (pula o que saiu da area entre a previa e o clique), manda
    canal a canal e registra. Falha num bloco nao para o proximo."""
    agora = agora or datetime.now()
    por_id = {s["id"]: s for s in servicos}
    embs = embarcadores(conn)
    forcar = _forcar_destino(config)
    resultados, ignorados = [], []
    for item in itens:
        sender_id, tipo = item.get("sender_id"), item.get("tipo")
        canais = set(item.get("canais") or [])
        lista = []
        for sid in dict.fromkeys(item.get("service_ids") or []):
            s = por_id.get(sid)
            if not s or tipos_area.get(sid) != tipo:
                ignorados.append(sid)
                continue
            lista.append(s)
        r = {"sender_id": sender_id, "tipo": tipo, "email": "pulado", "whatsapp": "pulado", "detalhe": ""}
        resultados.append(r)
        if not lista:
            r["detalhe"] = "nenhum pedido fora da área"
            continue
        emb = embs.get(sender_id) or {"nome": "", "emails": [], "whatsapp_grupo_id": None}
        pedidos = [_pedido(s) for s in lista]
        faltas = []

        quer_email = "email" in canais
        if quer_email and not emb["emails"] and not forcar:
            faltas.append("sem e-mail")
            quer_email = False
        if quer_email:
            assunto, conteudo = assunto_e_conteudo(emb["nome"], tipo, lista)
            corpo = envelope_html(conteudo, rodape="Mensagem enviada pela equipe Fresh Log.", cor_acento=COR_DESTAQUE)
            destinos = [forcar] if forcar else emb["emails"]
            if modo_teste:
                logger.info(f"[MODO TESTE] e-mail nao enviado para {destinos}: {assunto}")
                r["email"] = "modo_teste"
            else:
                ok = enviar_email(destinos, assunto, corpo, (config or {}).get("email", {}))
                r["email"] = "enviado" if ok else "falhou"
                _registrar(conn, agora, pedidos, sender_id, tipo, "email", r["email"], "; ".join(destinos), por)

        quer_wa = "whatsapp" in canais
        if quer_wa and not emb["whatsapp_grupo_id"]:
            faltas.append("sem grupo de WhatsApp")
            quer_wa = False
        if quer_wa:
            texto = texto_whatsapp(tipo, pedidos, com_email=r["email"] in ("enviado", "modo_teste"))
            situacao = notificar_whatsapp.despachar(config, ORIGEM_WHATSAPP, TIPO_WHATSAPP, texto, None,
                                                    modo_teste=modo_teste, conn=conn, agora=agora,
                                                    grupo_id=emb["whatsapp_grupo_id"], contar_no_teto=False)
            r["whatsapp"] = situacao
            if situacao in ("enviado", "falhou", "nao_enviado"):
                _registrar(conn, agora, pedidos, sender_id, tipo, "whatsapp",
                           "enviado" if situacao == "enviado" else "falhou", emb["whatsapp_grupo_id"], por)
        r["detalhe"] = ", ".join(faltas)
    return {"resultados": resultados, "ignorados": ignorados}
```

Atenção ao `despachar` chamado com `conn=conn`: ele executa `CREATE TABLE IF NOT EXISTS notificacoes_whatsapp` nessa conexão — no banco real é a mesma `dados.db`, então está certo.

- [ ] **Step 4: Rodar**

Run: `py -3.11 -m unittest test_avisar_fora_area -v`
Expected: todos passam. Se `extrair_cidade`/`extrair_uf` não lerem o `address` da fixture, ajustar as fixtures `S1..S5` para o formato que essas funções usam (ver `roteirizacao/regioes_dia_fixo.py`), mantendo as asserções de cidade/UF.

- [ ] **Step 5: Commit**

```bash
git add avisar_fora_area.py test_avisar_fora_area.py
git commit -m "Planejamento: nucleo do aviso em massa aos clientes sobre pedidos fora da area

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: rotas do painel + `avisado_em` no pool

**Files:**
- Modify: `painel_agentes/planejamento_rotas.py:512-560` (`_servico_para_pool`), `:809-858` (`buscar_pool_e_agendados`)
- Modify: `painel_agentes/painel_agentes.py` (depois de `api_planejamento_dedicado`, linha ~2460)
- Test: `painel_agentes/test_avisar_fora_area_rotas.py` (novo)

**Interfaces:**
- Consumes: `avisar_fora_area.conectar/montar_previa/enviar/avisados_por_service_id`, `integracao_openwa.listar_grupos`.
- Produces:
  - `planejamento_rotas.servicos_fora_area(config) -> tuple[list[dict], dict[int, str]]`: `(servicos_brutos, tipos_area)` do pool ao vivo (mesma fonte e classificação do `buscar_pool_e_agendados`).
  - Item do pool ganha `"avisado_em": "30/09 10:12" | None`.
  - `POST /api/planejamento/avisar-fora-area/previa` `{service_ids}` → JSON de `montar_previa` filtrado aos ids pedidos.
  - `POST /api/planejamento/avisar-fora-area` `{itens}` → JSON de `enviar`.

- [ ] **Step 1: Testes que falham**

Criar `painel_agentes/test_avisar_fora_area_rotas.py`:

```python
# -*- coding: utf-8 -*-
"""
Rotas do botao "Avisar clientes" (planejamento, 30/09/2026).

    py -3.11 -m unittest painel_agentes.test_avisar_fora_area_rotas
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import painel_agentes as pa  # noqa: E402
import avisar_fora_area as afa  # noqa: E402

S1 = {"id": 1, "code": "PS-1", "sender_id": 10, "address": "Rua A, 1 - Centro, Curitiba - PR, 80000-000"}
S2 = {"id": 2, "code": "PS-2", "sender_id": 10, "address": "Rua B, 2 - Centro, Bauru - SP, 17000-000"}
TIPOS = {1: afa.TIPO_FORA_SP}


class Rotas(unittest.TestCase):
    def setUp(self):
        pa.app.config["TESTING"] = True
        self.cliente = pa.app.test_client()
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "operador"
            sess["usuario"] = "maria"
        self.conn = afa.conectar(":memory:")
        self.conn.executescript("""
            CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, email TEXT, sender_id INTEGER);
            INSERT INTO interno VALUES ('1', 'ACME', 'ACME', 'a@acme.com', 10);""")
        self.addCleanup(self.conn.close)
        self.p_conn = patch.object(afa, "conectar", return_value=self.conn); self.p_conn.start(); self.addCleanup(self.p_conn.stop)
        self.p_srv = patch.object(pa, "servicos_fora_area", return_value=([S1, S2], TIPOS)); self.p_srv.start(); self.addCleanup(self.p_srv.stop)
        self.p_cfg = patch.object(pa, "_carregar_config", return_value={"whatsapp_notificacoes": {"ativo": False}, "email": {}}); self.p_cfg.start(); self.addCleanup(self.p_cfg.stop)
        self.p_grupos = patch.object(pa.integracao_openwa, "listar_grupos", return_value=None); self.p_grupos.start(); self.addCleanup(self.p_grupos.stop)

    def _post(self, url, body):
        return self.cliente.post(url, json=body, headers={"Origin": "http://localhost"})

    def test_previa_filtra_ids_e_ignora_quem_nao_esta_fora(self):
        resp = self._post("/api/planejamento/avisar-fora-area/previa", {"service_ids": [1, 2, 7]})
        self.assertEqual(resp.status_code, 200, resp.data)
        dados = resp.get_json()
        self.assertEqual([b["sender_id"] for b in dados["blocos"]], [10])
        self.assertEqual(dados["blocos"][0]["pedidos"][0]["codigo"], "PS-1")
        self.assertEqual(dados["ignorados"], [2])          # 7 nao esta no pool: nem entra
        self.assertFalse(dados["whatsapp_disponivel"])

    def test_previa_sem_ids_e_400(self):
        self.assertEqual(self._post("/api/planejamento/avisar-fora-area/previa", {}).status_code, 400)

    def test_enviar_chama_o_nucleo_com_o_usuario(self):
        with patch.object(afa, "enviar", return_value={"resultados": [], "ignorados": []}) as env:
            resp = self._post("/api/planejamento/avisar-fora-area",
                              {"itens": [{"sender_id": 10, "tipo": afa.TIPO_FORA_SP, "service_ids": [1], "canais": ["email"]}]})
        self.assertEqual(resp.status_code, 200, resp.data)
        args = env.call_args
        self.assertEqual(args.args[0][0]["sender_id"], 10)
        self.assertEqual(args.args[4], "maria")

    def test_enviar_sem_itens_e_400(self):
        self.assertEqual(self._post("/api/planejamento/avisar-fora-area", {"itens": []}).status_code, 400)

    def test_nivel_leitura_nao_acessa(self):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "leitura"
        self.assertEqual(self._post("/api/planejamento/avisar-fora-area/previa", {"service_ids": [1]}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
```

Antes de rodar, conferir em `painel_agentes.py:171-215` como `requer_auth` lê a sessão (nome da chave de nível e do usuário) e como os testes existentes do painel autenticam (`grep -n "session_transaction\|nivel_acesso" painel_agentes/test_*.py`); ajustar o `setUp` ao padrão encontrado. Se `requer_auth` devolver redirect (302) em vez de 403 para nível insuficiente, ajustar a asserção para o que a função faz de fato.

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_avisar_fora_area_rotas -v`
Expected: `AttributeError: module 'painel_agentes' has no attribute 'servicos_fora_area'` ou 404 nas rotas.

- [ ] **Step 3: `servicos_fora_area` e `avisado_em` em `planejamento_rotas.py`**

Em `buscar_pool_e_agendados`, extrair o trecho de classificação (linhas 813-819) para uma função de módulo, logo antes de `buscar_pool_e_agendados`:

```python
def _classificar_area(servicos_brutos: list[dict], gmaps_key: str) -> dict[int, str]:
    """service_id -> tipo_area, mesma classificação do pipeline automático
    (roteirizacao/notificar_area_nao_atendida.py). Falha vira aviso: a tela
    segue sem a marcação."""
    tipos_area: dict[int, str] = {}
    try:
        from notificar_area_nao_atendida import identificar_area_nao_atendida
        for s, tipo in identificar_area_nao_atendida(servicos_brutos, gmaps_key):
            tipos_area[s["id"]] = tipo
    except Exception as e:
        logger.warning(f"Falha ao classificar área não atendida pro pool (tela segue sem essa marcação): {e}")
    return tipos_area


def servicos_fora_area(config: dict | None = None) -> tuple[list[dict], dict[int, str]]:
    """Pool ao vivo + classificação de área, pro botão "Avisar clientes"
    (Hugo, 30/09): o servidor reclassifica em vez de confiar no tipo_area
    que o navegador mandou."""
    config = config or _carregar_config()
    vuupt = VuuptClient(config.get("vuupt_api", {}).get("token", ""))
    servicos_brutos = listar_pool_not_assigned(config, vuupt)
    return servicos_brutos, _classificar_area(servicos_brutos, config.get("google_maps", {}).get("api_key", ""))
```

e em `buscar_pool_e_agendados` substituir o bloco original por `tipos_area = _classificar_area(servicos_brutos, gmaps_key)`.

`avisado_em`: em `buscar_pool_e_agendados`, logo depois de `dedicados_por_id`, adicionar:

```python
    # Avisado pelo botão "Avisar clientes" (Hugo, 30/09): chip "avisado DD/MM"
    # nos cards fora da área. Só leitura; falha não derruba a tela.
    avisados: dict[int, dict] = {}
    try:
        import avisar_fora_area
        conn_avisos = avisar_fora_area.conectar()
        try:
            avisados = avisar_fora_area.avisados_por_service_id(conn_avisos)
        finally:
            conn_avisos.close()
    except Exception as e:
        logger.warning(f"Falha ao carregar avisos de fora da área (tela segue sem o chip): {e}")
```

e no laço que monta `pool`, depois de `p["fora_dia_fixo"] = ...`:

```python
            aviso = avisados.get(p["service_id"])
            p["avisado_em"] = aviso["em"] if aviso else None
```

- [ ] **Step 4: Rotas em `painel_agentes.py`**

Adicionar `servicos_fora_area` ao `from planejamento_rotas import (...)` (linha 63) e, no topo com os outros imports da raiz, `import avisar_fora_area` e `import integracao_openwa`. Depois de `api_planejamento_dedicado`:

```python
# ── Avisar clientes sobre pedidos fora da área (Hugo, 30/09) ─────────────────
# Botão do bloco "Fora da área" do planejamento: prévia por embarcador e
# envio de e-mail + WhatsApp (grupo do cliente). Lógica em avisar_fora_area.py.

def _servicos_fora_area_pedidos(service_ids: list[int]):
    servicos, tipos = servicos_fora_area(_carregar_config())
    pedidos = set(service_ids)
    return [s for s in servicos if s["id"] in pedidos], tipos


@app.route("/api/planejamento/avisar-fora-area/previa", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_avisar_fora_area_previa():
    body = request.get_json(force=True, silent=True) or {}
    try:
        ids = [int(x) for x in body.get("service_ids") or []]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        return jsonify({"erro": "nenhum pedido informado"}), 400
    try:
        config = _carregar_config()
        servicos, tipos = _servicos_fora_area_pedidos(ids)
        grupos = integracao_openwa.listar_grupos(config.get("whatsapp_notificacoes") or {}) or []
        conn = avisar_fora_area.conectar()
        try:
            previa = avisar_fora_area.montar_previa(servicos, tipos, config, conn,
                                                   grupos_nomes={g["id"]: g["nome"] for g in grupos})
        finally:
            conn.close()
        return jsonify(previa)
    except Exception as e:
        logging.getLogger(__name__).exception("Falha na prévia de avisar fora da área")
        return jsonify({"erro": str(e)}), 500


@app.route("/api/planejamento/avisar-fora-area", methods=["POST"])
@requer_auth(niveis=("total", "operador"))
@exige_mesma_origem
def api_avisar_fora_area():
    body = request.get_json(force=True, silent=True) or {}
    itens = body.get("itens") or []
    if not itens:
        return jsonify({"erro": "nenhum cliente marcado"}), 400
    por = session.get("usuario") or g.nivel_acesso
    try:
        config = _carregar_config()
        ids = [int(sid) for it in itens for sid in (it.get("service_ids") or [])]
        servicos, tipos = _servicos_fora_area_pedidos(ids)
        conn = avisar_fora_area.conectar()
        try:
            return jsonify(avisar_fora_area.enviar(itens, servicos, tipos, config, por, conn))
        finally:
            conn.close()
    except Exception as e:
        logging.getLogger(__name__).exception("Falha ao avisar clientes fora da área")
        return jsonify({"erro": str(e)}), 500
```

Nota do teste: no `test_enviar_chama_o_nucleo_com_o_usuario` o `enviar` é chamado posicionalmente com `(itens, servicos, tipos, config, por, conn)`, como acima — manter posicional para o `args.args[4]` bater.

- [ ] **Step 5: Rodar**

Run: `py -3.11 -m unittest painel_agentes.test_avisar_fora_area_rotas -v` e `py -3.11 -m py_compile painel_agentes/painel_agentes.py painel_agentes/planejamento_rotas.py`
Expected: passam. Rodar também `py -3.11 -m unittest painel_agentes.test_atendimento_liberar` para conferir que o import novo não quebrou o app.

- [ ] **Step 6: Commit**

```bash
git add painel_agentes/painel_agentes.py painel_agentes/planejamento_rotas.py painel_agentes/test_avisar_fora_area_rotas.py
git commit -m "Planejamento: rotas de previa e envio do aviso aos clientes fora da area

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: botão, modal e chip no template do planejamento

**Files:**
- Modify: `painel_agentes/templates/planejamento_rotas.html`: cabeçalho do bloco (`:2451-2458`), modal (depois de `#modal-dedicado`, `:2017`), `badgesPedidoHtml` (`:2700-2712`), `li.dataset` (`:2786-2792`), CSS (`:492` região do `.badge-fora-area`), JS novo perto de `abrirModalDedicado` (`:5903`).

**Interfaces:**
- Consumes: as duas rotas da Task 5; `POOL_BY_ID`, `SELECIONADOS`, `postJSON`, `escaparHtml`, `ROTULO_TIPO_AREA`, `p.avisado_em`.

- [ ] **Step 1: Botão no cabeçalho do bloco**

Em `:2455`, depois do botão "Selecionar" do bloco `fora-area`:

```html
          <button type="button" class="botao-mini" id="botao-avisar-fora-area" title="Avisa os embarcadores dos pedidos selecionados deste bloco (ou de todos os visíveis, se nada estiver selecionado) por e-mail e WhatsApp, com prévia antes de enviar">Avisar clientes</button>
```

- [ ] **Step 2: Modal**

Depois de `</div>` que fecha `#modal-dedicado` (`:2017`):

```html
<div class="modal-overlay-editar-endereco" id="modal-avisar-fora-area">
  <div class="modal-editar-endereco modal-avisar-fora-area" role="dialog" aria-modal="true" aria-labelledby="titulo-modal-avisar-fora-area">
    <h2 id="titulo-modal-avisar-fora-area">Avisar clientes</h2>
    <p class="subtitulo">E-mail com a lista dos pedidos e mensagem curta no grupo de WhatsApp de cada cliente. Quem já foi avisado vem desmarcado; marque de novo se quiser cobrar.</p>
    <p class="aviso-avisar" id="avisar-fora-area-aviso" hidden></p>
    <div id="avisar-fora-area-blocos" class="blocos-avisar"></div>
    <div class="acoes">
      <button type="button" class="cancelar" id="botao-cancelar-avisar-fora-area">Fechar</button>
      <button type="button" class="confirmar" id="botao-confirmar-avisar-fora-area" disabled>Enviar</button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: CSS**

Perto de `.item-parada .badge-fora-area` (`:492`), adicionar:

```css
  .item-parada .badge-avisado { display: inline-flex; align-items: center; gap: 3px; font-size: 10.5px; padding: 1px 6px; border-radius: 100px; background: var(--ok-bg, #e6f4ea); color: var(--ok, #1e7e34); }
  /* Modal "Avisar clientes" (Hugo, 30/09) */
  .modal-avisar-fora-area { max-width: 640px; }
  .blocos-avisar { max-height: 55vh; overflow: auto; display: flex; flex-direction: column; gap: 8px; }
  .bloco-avisar { border: 1px solid var(--borda); border-radius: 8px; padding: 8px 10px; font-size: 12.5px; }
  .bloco-avisar.desabilitado { opacity: 0.55; }
  .bloco-avisar .cab { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .bloco-avisar .cab strong { flex: 1; }
  .bloco-avisar .canais { display: flex; gap: 12px; margin-top: 4px; }
  .bloco-avisar .falta { color: var(--erro-texto, #b91c1c); font-weight: 700; }
  .bloco-avisar .ja-avisado { color: var(--texto-suave); }
  .bloco-avisar ul { margin: 4px 0 0; padding-left: 18px; }
  .bloco-avisar .resultado { margin-top: 4px; font-weight: 700; }
  .bloco-avisar .resultado.falhou { color: var(--erro-texto, #b91c1c); }
  .aviso-avisar { color: var(--erro-texto, #b91c1c); font-size: 12.5px; margin: 0 0 8px; }
```

- [ ] **Step 4: Chip e dataset**

Em `badgesPedidoHtml`, logo depois do `if (p.tipo_area) {...}`:

```js
    if (p.tipo_area && p.avisado_em) {
      partes.push(`<span class="badge-avisado" title="Cliente avisado pelo botão Avisar clientes">✉ avisado ${escaparHtml(p.avisado_em.split(" ")[0])}</span>`);
    }
```

Em `:2790`, depois de `li.dataset.tipoArea = ...`: `li.dataset.avisadoEm = p.avisado_em || "";`

- [ ] **Step 5: JS do botão e do modal**

Depois de `removerDedicado` (`:5943`):

```js
  // ── Avisar clientes fora da área (Hugo, 30/09) ─────────────────────────────
  const ROTULO_CANAL_RESULTADO = { enviado: "enviado", falhou: "falhou", pulado: "não enviado", nao_enviado: "não enviado (canal em pausa)", desligado: "WhatsApp desligado", modo_teste: "modo teste" };
  let previaAvisar = null;   // resposta da prévia, na ordem dos blocos

  function idsParaAvisar() {
    // mesmo critério de "visível" que selecionarSecao usa (ver a função,
    // perto do listener .botao-selecionar-secao): se lá for `hidden`,
    // classe ou display, copiar o mesmo teste aqui
    const lista = document.getElementById("lista-pool-fora-area");
    const visiveis = [...lista.querySelectorAll(".item-parada")].filter(li => li.style.display !== "none" && !li.hidden).map(li => Number(li.dataset.serviceId));
    const selecionados = visiveis.filter(id => SELECIONADOS.has(id));
    return selecionados.length ? selecionados : visiveis;
  }

  function htmlBlocoAvisar(b, i, wa) {
    const jaAvisado = b.ultimo_aviso ? `<span class="ja-avisado">avisado em ${escaparHtml(b.ultimo_aviso.em)} por ${escaparHtml(b.ultimo_aviso.por)}</span>` : "";
    const marcado = b.enviavel && !b.ultimo_aviso;
    const email = b.emails.length ? escaparHtml(b.emails.join(", ")) : `<span class="falta">sem e-mail</span>`;
    const grupo = !wa ? `<span class="falta">WhatsApp indisponível</span>`
      : b.whatsapp_grupo_id ? escaparHtml(b.whatsapp_grupo_nome || b.whatsapp_grupo_id) : `<span class="falta">sem grupo</span>`;
    const podeEmail = b.emails.length > 0, podeWa = wa && !!b.whatsapp_grupo_id;
    return `<div class="bloco-avisar${b.enviavel ? "" : " desabilitado"}" data-i="${i}">
      <label class="cab"><input type="checkbox" class="bloco-marcado" ${marcado ? "checked" : ""} ${b.enviavel ? "" : "disabled"}>
        <strong>${escaparHtml(b.nome)}</strong><span>${escaparHtml(ROTULO_TIPO_AREA[b.tipo] || b.tipo)}</span>${jaAvisado}</label>
      <ul>${b.pedidos.map(p => `<li>${escaparHtml(p.codigo)} · ${escaparHtml([p.cidade, p.uf].filter(Boolean).join("/") || "—")}${p.avisado_em ? ` <span class="ja-avisado">(avisado ${escaparHtml(p.avisado_em)})</span>` : ""}</li>`).join("")}</ul>
      <div class="canais">
        <label><input type="checkbox" class="canal-email" ${podeEmail ? "checked" : "disabled"}> E-mail: ${email}</label>
        <label><input type="checkbox" class="canal-whatsapp" ${podeWa ? "checked" : "disabled"}> WhatsApp: ${grupo}</label>
      </div>
      <div class="resultado" hidden></div>
    </div>`;
  }

  function atualizarBotaoAvisar() {
    const n = [...document.querySelectorAll("#avisar-fora-area-blocos .bloco-marcado:checked")].length;
    const botao = document.getElementById("botao-confirmar-avisar-fora-area");
    botao.disabled = n === 0;
    botao.textContent = n ? `Enviar para ${n} cliente${n > 1 ? "s" : ""}` : "Enviar";
  }

  async function abrirModalAvisarForaArea() {
    const ids = idsParaAvisar();
    const aviso = document.getElementById("avisar-fora-area-aviso");
    const blocos = document.getElementById("avisar-fora-area-blocos");
    aviso.hidden = true; aviso.textContent = "";
    blocos.innerHTML = "<p>Carregando…</p>";
    document.getElementById("botao-confirmar-avisar-fora-area").disabled = true;
    document.getElementById("modal-avisar-fora-area").classList.add("aberto");
    if (!ids.length) { blocos.innerHTML = "<p>Nenhum pedido fora da área neste bloco.</p>"; return; }
    try {
      previaAvisar = await postJSON("/api/planejamento/avisar-fora-area/previa", { service_ids: ids });
    } catch (e) {
      blocos.innerHTML = ""; aviso.hidden = false; aviso.textContent = `Falha ao montar a prévia: ${e.message}`; return;
    }
    const foraDaSelecao = [...SELECIONADOS].filter(id => !ids.includes(id)).length;
    const avisos = [];
    if (previaAvisar.ignorados.length) avisos.push(`${previaAvisar.ignorados.length} pedido(s) não está(ão) mais fora da área e foi(ram) ignorado(s).`);
    if (foraDaSelecao) avisos.push(`${foraDaSelecao} pedido(s) selecionado(s) fora deste bloco foi(ram) ignorado(s).`);
    if (!previaAvisar.whatsapp_disponivel) avisos.push("WhatsApp indisponível: só o e-mail será enviado.");
    if (avisos.length) { aviso.hidden = false; aviso.textContent = avisos.join(" "); }
    blocos.innerHTML = previaAvisar.blocos.length
      ? previaAvisar.blocos.map((b, i) => htmlBlocoAvisar(b, i, previaAvisar.whatsapp_disponivel)).join("")
      : "<p>Nenhum cliente para avisar.</p>";
    atualizarBotaoAvisar();
  }

  async function confirmarAvisarForaArea(botao) {
    if (!previaAvisar) return;
    const itens = [];
    document.querySelectorAll("#avisar-fora-area-blocos .bloco-avisar").forEach(el => {
      if (!el.querySelector(".bloco-marcado").checked) return;
      const b = previaAvisar.blocos[Number(el.dataset.i)];
      const canais = [];
      if (el.querySelector(".canal-email").checked) canais.push("email");
      if (el.querySelector(".canal-whatsapp").checked) canais.push("whatsapp");
      if (!canais.length) return;
      itens.push({ sender_id: b.sender_id, tipo: b.tipo, service_ids: b.pedidos.map(p => p.service_id), canais });
    });
    if (!itens.length) { alert("Marque ao menos um cliente com um canal."); return; }
    if (!confirm(`Enviar o aviso para ${itens.length} cliente(s) agora?`)) return;
    botao.disabled = true; botao.textContent = "Enviando…";
    let dados;
    try {
      dados = await postJSON("/api/planejamento/avisar-fora-area", { itens });
    } catch (e) {
      alert(`Falha ao enviar: ${e.message}`); botao.disabled = false; atualizarBotaoAvisar(); return;
    }
    const hoje = new Date();
    const rotuloHoje = `${String(hoje.getDate()).padStart(2, "0")}/${String(hoje.getMonth() + 1).padStart(2, "0")}`;
    dados.resultados.forEach(r => {
      const i = previaAvisar.blocos.findIndex(b => b.sender_id === r.sender_id && b.tipo === r.tipo);
      const el = document.querySelector(`#avisar-fora-area-blocos .bloco-avisar[data-i="${i}"]`);
      if (!el) return;
      const partes = [`e-mail: ${ROTULO_CANAL_RESULTADO[r.email] || r.email}`, `WhatsApp: ${ROTULO_CANAL_RESULTADO[r.whatsapp] || r.whatsapp}`];
      if (r.detalhe) partes.push(r.detalhe);
      const res = el.querySelector(".resultado");
      res.hidden = false; res.textContent = partes.join(" · ");
      res.classList.toggle("falhou", r.email === "falhou" || r.whatsapp === "falhou");
      el.querySelector(".bloco-marcado").checked = false;
      if (r.email === "enviado" || r.whatsapp === "enviado") {
        previaAvisar.blocos[i].pedidos.forEach(p => {
          const li = document.querySelector(`#lista-pool-fora-area .item-parada[data-service-id="${p.service_id}"]`);
          if (li && POOL_BY_ID[p.service_id]) { POOL_BY_ID[p.service_id].avisado_em = `${rotuloHoje} agora`; }
          if (li && !li.querySelector(".badge-avisado")) {
            const badge = li.querySelector(".badge-fora-area");
            if (badge) badge.insertAdjacentHTML("afterend", ` <span class="badge-avisado">✉ avisado ${rotuloHoje}</span>`);
          }
        });
      }
    });
    botao.textContent = "Enviar"; atualizarBotaoAvisar();
  }

  document.getElementById("botao-avisar-fora-area").addEventListener("click", (e) => { e.stopPropagation(); abrirModalAvisarForaArea(); });
  document.getElementById("botao-cancelar-avisar-fora-area").addEventListener("click", () => document.getElementById("modal-avisar-fora-area").classList.remove("aberto"));
  document.getElementById("botao-confirmar-avisar-fora-area").addEventListener("click", (e) => confirmarAvisarForaArea(e.currentTarget));
  document.getElementById("avisar-fora-area-blocos").addEventListener("change", atualizarBotaoAvisar);
```

Os quatro `addEventListener` do fim: se os listeners dos botões do modal Dedicado (`botao-cancelar-dedicado`, `botao-confirmar-dedicado`, `botao-dedicado-selecao`) forem registrados dentro de uma função de inicialização (`initMap`/`DOMContentLoaded`), registrar os novos no mesmo lugar, não no nível do script.

Conferir: o clique no cabeçalho do bloco alterna o colapso (`:4137` só ignora `.botao-selecionar-secao`); por isso o `stopPropagation` no botão novo. Se o listener do cabeçalho usar `e.target.closest(".botao-selecionar-secao")`, adicionar `|| e.target.closest("#botao-avisar-fora-area")` a ele (`:4137`).

Conferir também se `.botao-mini` fica escondido no nível `leitura` por algum CSS/JS de "somente consulta" (`grep -n "pode_editar\|somente-consulta" planejamento_rotas.html`); se o botão "Dedicado" some nesse caso, aplicar a mesma regra ao `#botao-avisar-fora-area`.

- [ ] **Step 6: Prova manual local**

Subir o painel em outra porta (de dentro de `painel_agentes/`):
`py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"`.
Abrir `http://127.0.0.1:8099/planejamento`, entrar no bloco "Fora da área", clicar "Avisar clientes": a prévia abre com os blocos (WhatsApp indisponível no local). Sem `forcar_destino` no config local, **não confirmar o envio** (o e-mail iria para o cliente de verdade). Com `avisos_fora_area.forcar_destino: hugo@freshlogbr.com` no config local, enviar 1 bloco e conferir: e-mail chegou, linha em `avisos_fora_area`, chip "avisado" no card, prévia reaberta mostra o bloco desmarcado com "avisado em".

Não usar o console (F12) com a página aberta em produção; se precisar de Playwright, ver a memória `feedback_testar_painel_playwright_local`.

- [ ] **Step 7: Commit**

```bash
git add painel_agentes/templates/planejamento_rotas.html
git commit -m "Planejamento: botao Avisar clientes no bloco fora da area, com previa e chip de avisado

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: tela "WhatsApp dos clientes" (cadastro do grupo por embarcador)

**Files:**
- Create: `painel_agentes/embarcadores_whatsapp.py`, `painel_agentes/templates/embarcadores_whatsapp.html`
- Modify: `painel_agentes/painel_agentes.py` (registro do módulo, ao lado do `_atendimento_web.registrar`, `:3167`), `painel_agentes/templates/_menu_lateral_nav.html` (grupo "Operar", depois de Atendimento)
- Test: `painel_agentes/test_embarcadores_whatsapp.py`

**Interfaces:**
- Consumes: `avisar_fora_area.conectar/garantir_coluna_grupo`, `integracao_openwa.listar_grupos`.
- Produces:
  - `embarcadores_whatsapp.listar(conn) -> list[dict]`: `[{cnpj, nome, email, whatsapp_grupo_id}]` ordenado por nome.
  - `embarcadores_whatsapp.salvar_grupo(conn, cnpj, grupo_id) -> None`; `ValueError` se formato inválido ou CNPJ inexistente.
  - `embarcadores_whatsapp.registrar(app, *, requer_auth, exige_mesma_origem, carregar_config)` com rotas `GET /embarcadores/whatsapp` (endpoint `embarcadores_whatsapp`), `GET /api/embarcadores/whatsapp/grupos`, `POST /api/embarcadores/<cnpj>/whatsapp-grupo`.

- [ ] **Step 1: Testes que falham**

Criar `painel_agentes/test_embarcadores_whatsapp.py`:

```python
# -*- coding: utf-8 -*-
"""
Cadastro do grupo de WhatsApp por embarcador (tela /embarcadores/whatsapp,
Hugo 30/09/2026).

    py -3.11 -m unittest painel_agentes.test_embarcadores_whatsapp
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import avisar_fora_area as afa  # noqa: E402
import embarcadores_whatsapp as ew  # noqa: E402
import painel_agentes as pa  # noqa: E402


def _banco():
    conn = afa.conectar(":memory:")
    conn.executescript("""
        CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, email TEXT, sender_id INTEGER);
        INSERT INTO interno VALUES ('11', 'ZETA LTDA', NULL, 'z@z.com', 1), ('22', 'ACME LTDA', 'ACME', '', 2);""")
    afa.garantir_coluna_grupo(conn)
    return conn


class Nucleo(unittest.TestCase):
    def setUp(self):
        self.conn = _banco()
        self.addCleanup(self.conn.close)

    def test_listar_ordenado_por_nome(self):
        self.assertEqual([e["nome"] for e in ew.listar(self.conn)], ["ACME", "ZETA LTDA"])
        self.assertEqual(ew.listar(self.conn)[0], {"cnpj": "22", "nome": "ACME", "email": "", "whatsapp_grupo_id": None})

    def test_salvar_e_remover(self):
        ew.salvar_grupo(self.conn, "22", "123@g.us")
        self.assertEqual(ew.listar(self.conn)[0]["whatsapp_grupo_id"], "123@g.us")
        ew.salvar_grupo(self.conn, "22", "")
        self.assertIsNone(ew.listar(self.conn)[0]["whatsapp_grupo_id"])

    def test_formato_invalido(self):
        for ruim in ("https://chat.whatsapp.com/abc", "123", "123@c.us", " 123@g.us "):
            with self.assertRaises(ValueError, msg=ruim):
                ew.salvar_grupo(self.conn, "22", ruim)
        self.assertIsNone(ew.listar(self.conn)[0]["whatsapp_grupo_id"])

    def test_cnpj_inexistente(self):
        with self.assertRaises(ValueError):
            ew.salvar_grupo(self.conn, "99", "123@g.us")


class Rotas(unittest.TestCase):
    def setUp(self):
        pa.app.config["TESTING"] = True
        self.cliente = pa.app.test_client()
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "operador"
            sess["usuario"] = "maria"
        self.conn = _banco()
        self.addCleanup(self.conn.close)
        self.p_conn = patch.object(afa, "conectar", return_value=self.conn); self.p_conn.start(); self.addCleanup(self.p_conn.stop)
        self.p_grupos = patch.object(ew.integracao_openwa, "listar_grupos", return_value=[{"id": "1@g.us", "nome": "G1"}])
        self.grupos = self.p_grupos.start(); self.addCleanup(self.p_grupos.stop)

    def test_tela_abre(self):
        resp = self.cliente.get("/embarcadores/whatsapp")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ACME", resp.get_data(as_text=True))

    def test_grupos(self):
        self.assertEqual(self.cliente.get("/api/embarcadores/whatsapp/grupos").get_json(),
                         {"grupos": [{"id": "1@g.us", "nome": "G1"}], "indisponivel": False})
        self.grupos.return_value = None
        self.assertEqual(self.cliente.get("/api/embarcadores/whatsapp/grupos").get_json(), {"grupos": [], "indisponivel": True})

    def test_salvar(self):
        resp = self.cliente.post("/api/embarcadores/22/whatsapp-grupo", json={"grupo_id": "1@g.us"}, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(ew.listar(self.conn)[0]["whatsapp_grupo_id"], "1@g.us")

    def test_salvar_invalido_e_400(self):
        resp = self.cliente.post("/api/embarcadores/22/whatsapp-grupo", json={"grupo_id": "x"}, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 400)

    def test_nivel_leitura_nao_acessa(self):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "leitura"
        self.assertEqual(self.cliente.get("/embarcadores/whatsapp").status_code, 403)


if __name__ == "__main__":
    unittest.main()
```

Mesma ressalva da Task 5 sobre como `requer_auth` lê a sessão e o que devolve para nível insuficiente: ajustar o `setUp`/asserção ao padrão real.

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest painel_agentes.test_embarcadores_whatsapp -v`
Expected: `ModuleNotFoundError: No module named 'embarcadores_whatsapp'`.

- [ ] **Step 3: Módulo `painel_agentes/embarcadores_whatsapp.py`**

```python
# -*- coding: utf-8 -*-
"""
embarcadores_whatsapp.py

Tela /embarcadores/whatsapp: grupo de WhatsApp de cada embarcador
(interno.whatsapp_grupo_id), usado pelo botao "Avisar clientes" do
planejamento (avisar_fora_area.py). Os grupos vem do numero do Hugo pelo
OpenWA (integracao_openwa.listar_grupos); se o gateway nao responder, a
tela deixa colar o id (...@g.us) a mao.

Mesmo padrao de atendimento_chamados.py: modulo separado que recebe os
decoradores do painel em registrar().
"""
import logging
import re
import sys
from pathlib import Path

from flask import g, jsonify, render_template, request, session

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import avisar_fora_area  # noqa: E402
import integracao_openwa  # noqa: E402

logger = logging.getLogger(__name__)

NIVEIS = ("total", "operador")
_FORMATO_GRUPO = re.compile(r"^\d+@g\.us$")


def listar(conn) -> list[dict]:
    avisar_fora_area.garantir_coluna_grupo(conn)
    rows = conn.execute(
        "SELECT cnpj_embarcador, nome_remetente, apelido, email, whatsapp_grupo_id FROM interno").fetchall()
    itens = [{"cnpj": r["cnpj_embarcador"], "nome": r["apelido"] or r["nome_remetente"] or "",
              "email": r["email"] or "", "whatsapp_grupo_id": (r["whatsapp_grupo_id"] or "").strip() or None}
             for r in rows]
    return sorted(itens, key=lambda e: e["nome"].lower())


def salvar_grupo(conn, cnpj: str, grupo_id: str | None) -> None:
    """grupo_id vazio/None tira o grupo. Formato exigido: <numeros>@g.us
    (link de convite ou @c.us nao valem)."""
    avisar_fora_area.garantir_coluna_grupo(conn)
    grupo_id = grupo_id or ""
    if grupo_id and not _FORMATO_GRUPO.match(grupo_id):
        raise ValueError("id de grupo invalido: use o formato 123456789@g.us")
    cur = conn.execute("UPDATE interno SET whatsapp_grupo_id = ? WHERE cnpj_embarcador = ?",
                       (grupo_id or None, cnpj))
    if cur.rowcount == 0:
        raise ValueError("embarcador nao encontrado")
    conn.commit()


def registrar(app, *, requer_auth, exige_mesma_origem, carregar_config):
    def _cfg_wa() -> dict:
        return (carregar_config() or {}).get("whatsapp_notificacoes") or {}

    @app.route("/embarcadores/whatsapp")
    @requer_auth(niveis=NIVEIS)
    def embarcadores_whatsapp():
        conn = avisar_fora_area.conectar()
        try:
            embarcadores = listar(conn)
        finally:
            conn.close()
        return render_template("embarcadores_whatsapp.html", embarcadores=embarcadores)

    @app.route("/api/embarcadores/whatsapp/grupos")
    @requer_auth(niveis=NIVEIS)
    def api_embarcadores_whatsapp_grupos():
        grupos = integracao_openwa.listar_grupos(_cfg_wa())
        return jsonify({"grupos": grupos or [], "indisponivel": grupos is None})

    @app.route("/api/embarcadores/<cnpj>/whatsapp-grupo", methods=["POST"])
    @requer_auth(niveis=NIVEIS)
    @exige_mesma_origem
    def api_embarcadores_whatsapp_grupo(cnpj):
        body = request.get_json(force=True, silent=True) or {}
        grupo_id = str(body.get("grupo_id") or "").strip()
        conn = avisar_fora_area.conectar()
        try:
            salvar_grupo(conn, cnpj, grupo_id)
        except ValueError as e:
            return jsonify({"erro": str(e)}), 400
        finally:
            conn.close()
        logger.info("Grupo de WhatsApp do embarcador %s = %r (por %s)", cnpj, grupo_id or None,
                    session.get("usuario") or g.nivel_acesso)
        return jsonify({"ok": True, "whatsapp_grupo_id": grupo_id or None})
```

Cuidado com o teste `test_formato_invalido` e `" 123@g.us "`: `salvar_grupo` recebe o valor como veio; quem faz `strip()` é a rota. Manter assim para o teste do núcleo valer.

- [ ] **Step 4: Template `embarcadores_whatsapp.html`**

```html
{% extends "base.html" %}
{% block titulo %}WhatsApp dos clientes{% endblock %}

{% block estilo_extra %}
<style>
  main { max-width: 1100px; }
  h1 { font-size: 20px; margin: 0 0 6px; }
  .sub { color: var(--texto-suave); font-size: 13px; margin: 0 0 14px; line-height: 1.5; }
  .busca { margin: 0 0 12px; }
  .busca input { width: 100%; max-width: 420px; padding: 8px 10px; border: 1px solid var(--borda); border-radius: 8px; font-size: 13px; }
  .tabela-wrap { overflow-x: auto; background: var(--superficie); border: 1px solid var(--borda); border-radius: 10px; }
  table { width: 100%; border-collapse: collapse; min-width: 720px; }
  th, td { text-align: left; padding: 10px 12px; font-size: 13px; border-bottom: 1px solid var(--borda); vertical-align: middle; }
  th { color: var(--texto-suave); font-weight: 700; text-transform: uppercase; font-size: 11px; letter-spacing: 0.4px; }
  tr:last-child td { border-bottom: none; }
  .mono { font-size: 12px; color: var(--texto-suave); }
  td select, td input[type=text] { width: 100%; max-width: 360px; padding: 6px 8px; border: 1px solid var(--borda); border-radius: 6px; font-size: 13px; }
  .acoes { white-space: nowrap; }
  .estado { font-size: 12px; margin-left: 8px; color: var(--texto-suave); }
  .estado.erro { color: var(--erro); }
  .aviso { color: var(--erro); font-size: 13px; margin: 0 0 10px; }
</style>
{% endblock %}

{% block conteudo %}
<h1>WhatsApp dos clientes</h1>
<p class="sub">
  Grupo de WhatsApp que a Fresh já tem com cada embarcador. É para lá que vai o aviso do botão
  <strong>Avisar clientes</strong> do Planejamento (pedidos fora da área de atendimento). A lista de grupos vem do
  número da Fresh; se o gateway estiver fora do ar, cole o id do grupo (<span class="mono">123456789@g.us</span>).
</p>
<p class="aviso" id="aviso" hidden></p>
<div class="busca"><input type="search" id="busca" placeholder="Filtrar embarcadores ou grupos…" autocomplete="off"></div>
<div class="tabela-wrap">
  <table>
    <thead><tr><th>Embarcador</th><th>E-mail</th><th>Grupo de WhatsApp</th><th></th></tr></thead>
    <tbody>
    {% for e in embarcadores %}
      <tr data-cnpj="{{ e.cnpj }}" data-grupo="{{ e.whatsapp_grupo_id or '' }}" data-busca="{{ (e.nome ~ ' ' ~ e.email) | lower }}">
        <td>{{ e.nome or '(sem nome)' }}<div class="mono">{{ e.cnpj }}</div></td>
        <td class="mono">{{ e.email or '—' }}</td>
        <td class="celula-grupo"><input type="text" value="{{ e.whatsapp_grupo_id or '' }}" placeholder="123456789@g.us"></td>
        <td class="acoes"><button type="button" class="botao" data-acao="salvar">Salvar</button><span class="estado"></span></td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<script>
(function () {
  const aviso = document.getElementById("aviso");
  let GRUPOS = null;

  function montarSelect(linha) {
    const atual = linha.dataset.grupo;
    const opcoes = ['<option value="">— nenhum —</option>']
      .concat(GRUPOS.map(g => `<option value="${g.id}" ${g.id === atual ? "selected" : ""}>${g.nome}</option>`));
    if (atual && !GRUPOS.some(g => g.id === atual)) opcoes.push(`<option value="${atual}" selected>${atual} (grupo não encontrado)</option>`);
    const select = document.createElement("select");
    select.innerHTML = opcoes.join("");
    linha.querySelector(".celula-grupo").replaceChildren(select);
  }

  async function carregarGrupos() {
    try {
      const resp = await fetch(BASE_PATH + "/api/embarcadores/whatsapp/grupos");
      const dados = await resp.json();
      if (!resp.ok) throw new Error(dados.erro || "Falha ao listar grupos");
      if (dados.indisponivel) { aviso.hidden = false; aviso.textContent = "Gateway do WhatsApp fora do ar: cole o id do grupo à mão."; return; }
      GRUPOS = dados.grupos.map(g => ({ id: g.id, nome: g.nome.replace(/</g, "&lt;") }));
      document.querySelectorAll("tr[data-cnpj]").forEach(montarSelect);
    } catch (e) {
      aviso.hidden = false; aviso.textContent = e.message;
    }
  }

  document.getElementById("busca").addEventListener("input", (ev) => {
    const termo = ev.target.value.trim().toLowerCase();
    document.querySelectorAll("tr[data-cnpj]").forEach(linha => {
      const campo = linha.querySelector("select, input[type=text]");
      const texto = campo && campo.tagName === "SELECT" ? campo.options[campo.selectedIndex].text.toLowerCase() : (campo ? campo.value.toLowerCase() : "");
      linha.hidden = !!termo && !linha.dataset.busca.includes(termo) && !texto.includes(termo);
    });
  });

  document.querySelectorAll("tr[data-cnpj] button[data-acao=salvar]").forEach(botao => {
    botao.addEventListener("click", async () => {
      const linha = botao.closest("tr");
      const campo = linha.querySelector("select, input[type=text]");
      const estado = linha.querySelector(".estado");
      botao.disabled = true; estado.className = "estado"; estado.textContent = "Salvando…";
      try {
        const resp = await fetch(BASE_PATH + "/api/embarcadores/" + encodeURIComponent(linha.dataset.cnpj) + "/whatsapp-grupo", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ grupo_id: campo.value }),
        });
        const dados = await resp.json();
        if (!resp.ok) throw new Error(dados.erro || "Falha ao salvar");
        linha.dataset.grupo = dados.whatsapp_grupo_id || "";
        estado.textContent = "Salvo";
      } catch (e) {
        estado.className = "estado erro"; estado.textContent = e.message;
      } finally {
        botao.disabled = false;
      }
    });
  });

  carregarGrupos();
})();
</script>
{% endblock %}
```

Conferir em `base.html` que `BASE_PATH` está definido globalmente (o `clientes_agenda.html` já usa); se não estiver, usar `{{ url_for('embarcadores_whatsapp') }}` como base da mesma forma que o template mais próximo faz.

- [ ] **Step 5: Registro no painel e item no menu**

Em `painel_agentes.py`, depois do bloco `_atendimento_web.registrar(...)` (`:3168`):

```python
# ── WhatsApp dos clientes: grupo por embarcador (Hugo, 30/09) ───────────────
import embarcadores_whatsapp as _embarcadores_whatsapp_web
_embarcadores_whatsapp_web.registrar(app, requer_auth=requer_auth, exige_mesma_origem=exige_mesma_origem,
                                     carregar_config=_carregar_config)
```

Em `_menu_lateral_nav.html`, no grupo `Operar`, depois do item `atendimento`:

```jinja
    {'rota': 'embarcadores_whatsapp', 'rotulo': 'WhatsApp clientes', 'titulo': 'Grupo de WhatsApp de cada embarcador (aviso de pedidos fora da área)', 'niveis': ('total', 'operador'), 'badge': none,
     'icone': '<path d="M17 10a7 7 0 01-10.3 6.2L3.5 17l.9-3.1A7 7 0 1117 10z"/><path d="M7.8 8.2c.3 2 1.9 3.6 4 4l1-1-1.5-.9-.6.5c-.7-.4-1.3-1-1.7-1.7l.5-.6-.9-1.5z"/>'},
```

- [ ] **Step 6: Rodar**

Run: `py -3.11 -m unittest painel_agentes.test_embarcadores_whatsapp -v` e `py -3.11 -m py_compile painel_agentes/embarcadores_whatsapp.py painel_agentes/painel_agentes.py`
Expected: passam. Abrir localmente (`porta 8099`) `/embarcadores/whatsapp`: tabela aparece, aviso "gateway fora do ar" no local, salvar um id válido numa linha e ver "Salvo"; id inválido mostra o erro em vermelho.

- [ ] **Step 7: Commit**

```bash
git add painel_agentes/embarcadores_whatsapp.py painel_agentes/templates/embarcadores_whatsapp.html painel_agentes/test_embarcadores_whatsapp.py painel_agentes/painel_agentes.py painel_agentes/templates/_menu_lateral_nav.html
git commit -m "Painel: tela WhatsApp dos clientes (grupo por embarcador)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `MAPA_DO_SISTEMA.txt` e rodada final

**Files:**
- Modify: `MAPA_DO_SISTEMA.txt` (seção de scripts da raiz perto de `notificar_whatsapp.py` `:327-330`; seção 10 `painel_agentes/` `:545+`; lista de tabelas `:772+`)

- [ ] **Step 1: Mapa**

Na lista da raiz, depois de `notificar_whatsapp.py`:

```
 avisar_fora_area.py          Aviso em massa aos embarcadores sobre pedidos fora
                              da area (botao "Avisar clientes" do planejamento):
                              e-mail + WhatsApp no grupo do cliente, registro em
                              avisos_fora_area. Nao mexe em pedidos_area_notificada.
```

Na seção 10 (`painel_agentes/`), junto das outras telas:

```
 embarcadores_whatsapp.py  /embarcadores/whatsapp: grupo de WhatsApp por
                           embarcador (interno.whatsapp_grupo_id), lista vinda
                           do OpenWA; usado pelo Avisar clientes.
```

Na lista de tabelas:

```
   avisos_fora_area          avisar_fora_area.py (uma linha por pedido e canal)
   interno.whatsapp_grupo_id coluna criada por avisar_fora_area.garantir_coluna_grupo
```

Na linha `interno  tipo de carga por embarcador (só leitura)` (`:773`), trocar por `interno  tipo de carga por embarcador; whatsapp_grupo_id editável em /embarcadores/whatsapp`.

- [ ] **Step 2: Rodada completa de testes**

Run:
```
py -3.11 -m unittest test_notificar_whatsapp test_integracao_openwa test_avisar_fora_area roteirizacao.test_notificar_area_nao_atendida painel_agentes.test_avisar_fora_area_rotas painel_agentes.test_embarcadores_whatsapp painel_agentes.test_atendimento_liberar
```
Expected: todos passam.

- [ ] **Step 3: Commit**

```bash
git add MAPA_DO_SISTEMA.txt
git commit -m "Mapa: avisar_fora_area, tela WhatsApp dos clientes e tabela avisos_fora_area

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 4: Entregar ao Hugo (sem push)**

Relatar: commits feitos, testes rodados com contagem, o que falta que só ele decide — (a) push + deploy (`deploy-vps`), (b) chave nova do OpenWA sem restrição de chat no `config.yaml` da VPS (backup antes), (c) prova real: cadastrar o grupo de teste dele num embarcador de teste, clicar o botão com 1 pedido, conferir e-mail, WhatsApp, `avisos_fora_area` e `notificacoes_whatsapp`.
