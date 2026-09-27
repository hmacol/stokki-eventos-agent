# Aba Pedidos de Entrada no portal — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** o cliente (piloto Maria Dolores) anuncia no portal a mercadoria que vai chegar no galpão (XML da NF-e de remessa ou planilha), o portal cria o recebimento `#PE` na Stokki pelo wizard de XML múltiplo sem ninguém digitar, o WMS amarra o `#PE` à entrada pelo número/chave da NF-e, e o cliente acompanha ANUNCIADO → CHEGOU → ENDERECADO | DIVERGENCIA, podendo cancelar enquanto ANUNCIADO.

**Architecture:** módulo novo `portal_cliente/entradas.py` (tabelas `portal_entradas` + `portal_entrada_itens`, leitura, validação, ciclo de vida, amarração com `wms_recebimentos`, `sincronizar_status`), sem tocar em `portal_envios`. Worker novo `portal_cliente/enviar_entradas_stokki.py` com o mesmo desenho de `enviar_stokki.py` (trava cooperativa, Playwright no wizard, hook em `XMLHttpRequest` pra capturar a resposta do `incoming/xml/multiple/store`, descoberta do `#PE` pela chave). O timer `stokki-wms-recebimentos` ganha um passo de amarração e chama `sincronizar_status`; a rota de divergência do painel também. Aba nova `_entradas.html` no portal, rotas `/api/entradas/*`.

**Tech Stack:** Python 3.11 (`py -3.11`), SQLite (`dados/dados.db`), Flask + Jinja (portal, porta 8074), Playwright (wizard da Stokki, só na VPS), `unittest`, systemd (VPS).

**Spec:** `docs/superpowers/specs/2026-09-24-pedidos-de-entrada-portal-design.md` (seção 8 tem os achados da sondagem e o desenho do worker).

## Global Constraints

- Idioma: código, comentários, testes e commits em português. Em `portal_cliente/` os comentários usam acento (padrão do diretório); em `stokki/` e `painel_agentes/wms_*.py` não usam.
- Python: sempre `py -3.11`. Testes: `py -3.11 -m unittest <modulo> -v` (sem pytest). Compilar tudo que editar: `py -3.11 -m py_compile <arquivo>`.
- **Commits locais por tarefa, só no ramo `pedidos-entrada` deste worktree** (decisão do controlador em 25/09: a execução por subagentes revisa por intervalo de commits). Cada tarefa termina com testes verdes, `git add` só dos arquivos listados na tarefa (nunca `git add -A`) e um commit no formato `Área: o que mudou e por quê`, com a linha `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` no fim. **Nunca `git push`, nunca merge, nunca mexer no `master`**: isso é do Hugo.
- **Ramo:** o working tree está em `wms-fase2`, 0 à frente e 45 atrás de `origin/master` (tudo do WMS já está no master), com ~28 arquivos de outra sessão modificados. Este plano é executado num **worktree novo a partir de `origin/master`**, ramo `pedidos-entrada`. Confirmar com o Hugo no início da execução.
- Só a Maria Dolores vê a aba: `portal_clientes_envio.entradas_ativo` (coluna nova, default 0). O timer do WMS continua lendo só o piloto (`wms.embarcador_piloto_id`, hoje 48).
- Config novo (opcional): `portal_entradas.cnpj_freshlog` no `config.yaml`. Sem a chave, o aviso de destinatário não é emitido. `config.yaml` é gitignored: nunca commitar, nunca colar valores no chat.
- **Nenhuma escrita em `wms_movimentos` ou `wms_saldos`** a partir do portal ou do worker.
- **Nunca sondar URL da Stokki por adivinhação** (URL inexistente redireciona pro `/login` e o `auth.py` refaz login via Playwright, derrubando as outras sessões). O worker e o timer só usam as URLs da seção 8.1 da spec.
- Testes nunca tocam `dados/dados.db` real nem a rede: `mock.patch.object(ep, "DB_PATH", <tmp>)` como em `portal_cliente/test_envio_planilha.py`; sessão Stokki sempre dublê (`SessaoFalsa`), como em `test_sincronizar_recebimentos_wms.py`.
- Na VPS, sempre `sudo -u www-data venv/bin/python`; o worker roda como `www-data` pela unit.
- Envio de e-mail ao cliente continua como está (relatório de faltas já existe e nasce redirecionado pro Hugo). Este plano não cria e-mail novo.

## Review Focus

1. Cliente sobe a **mesma NF-e duas vezes no mesmo lote** ou depois de já anunciada: a prévia tem que recusar a segunda com "já anunciada em DD/MM (status)", e só permitir de novo quando a anterior estiver CANCELADO (Tarefa 3, `test_chave_ja_anunciada_e_cancelada`).
2. **Resposta perdida do store**: o worker cria o `#PE`, cai antes de gravar, e na rodada seguinte tentaria criar de novo. A checagem pela chave antes de subir (`procurar_pe`) tem que devolver o `#PE` existente e marcar CRIADO sem criar (Tarefa 8, `test_ja_existe_na_stokki_vira_criado_sem_criar`).
3. **NF-e com o mesmo SKU em duas linhas** (embalagens diferentes): o wizard soma; a aba mostra as linhas como vieram e a conferência do galpão por SKU (Tarefa 4, `test_conferencia_do_galpao_agrupa_por_sku`).
4. **Cancelar depois que o galpão começou a endereçar**: o status já é CHEGOU, então `cancelar_entrada` recusa; e cancelar com `#PE` já criado marca CANCELADO no portal, tira do WMS só se ESPERADO sem endereçamento, e abre chamado (Tarefa 4, `test_cancelar_*`).
5. **Data prevista passada** no anúncio (cliente digita ontem) ou no worker (anunciou pra hoje e o worker roda amanhã): `confirmar_entradas` recusa data passada; o worker manda `arrival_date` = hoje quando a data já passou, porque o wizard tem `minDate` = hoje (Tarefas 4 e 8).

---

### Task 1: `stokki/recebimentos.py` lê a chave NF-e e a "Ref. do Pedido" do detalhe

**Files:**
- Modify: `stokki/recebimentos.py` (docstring das linhas 23-28 e funções novas depois de `ler_itens`, linha 265)
- Create: `stokki/fixtures/recebimento_detalhe_cabecalho.html`
- Test: `stokki/test_recebimentos.py`

**Interfaces:**
- Produces: `extrair_chave_nfe(html: str) -> str` (44 dígitos ou `""`), `extrair_ref_pedido(html: str) -> str` (`""` quando "Não informado"), `extrair_ref_da_linha(linha: dict) -> str` (o segundo número do campo `id` da listagem), `ler_detalhe(sessao, id_recebimento: int) -> dict` com chaves `itens`, `chave_nfe`, `ref_pedido`.

- [ ] **Step 1: Criar a fixture com a estrutura real do cabeçalho do `#PE`**

Criar `stokki/fixtures/recebimento_detalhe_cabecalho.html` (estrutura copiada do `#PE-2497` real em 25/09/2026, com chave e CNPJ trocados por valores sintéticos; a tabela de itens é a mesma da fixture `recebimento_itens.html`):

```html
<html><body>
<table>
  <tr><th>ID do Pedido:</th><td>#PE-2497</td></tr>
  <tr><th>Ref. do Pedido:</th><td>41221</td></tr>
  <tr><th>Tipo:</th><td><span>Entrada</span></td></tr>
  <tr><th>Movimento:</th><td><span>Remessa</span></td></tr>
  <tr><th>Cliente:</th><td>EMBARCADOR TESTE LTDA (#stkkc-48)</td></tr>
  <tr><th>unidade:</th><td>Freshlog - São Paulo, SP</td></tr>
</table>
<table>
  <tr><th>Situação:</th><td><span><i></i>Recebido</span></td></tr>
  <tr><th>Chegada Prevista:</th><td>24/09/2026</td></tr>
  <tr><th>Tipo de Acondicionamento:</th><td> Carga Solta (Caixas)</td></tr>
  <tr><th> Número de paletes: <i title="Número estimado de paletes."></i> </th><td> Não informado </td></tr>
  <tr><th> Número de caixas: <i title="Número estimado de caixas."></i> </th><td> Não informado </td></tr>
  <tr><th>Tipo de Transporte:</th><td>Fracionado (LTL)</td></tr>
  <tr><th>NF-e:</th><td> <a href="http://www.nfe.fazenda.gov.br/portal/consultaRecaptcha.aspx?tipoConsulta=completa&amp;nfe=35260912345678000195550010000412211000000017" target="_blank"> 35260912345678000195550010000412211000000017 </a> </td></tr>
  <tr><th>CT-e:</th><td> Não informado </td></tr>
</table>
<table class="table">
  <thead><tr><th>NR.</th><th>ID</th><th>SKU</th><th>Nome</th><th>Localização</th><th>Quantidade</th><th>unidade</th><th>Valor Unitário</th><th>Quantidade total recebida</th><th>Obs</th></tr></thead>
  <tbody><tr><td>1</td><td>#ITM-927</td><td>NUU001FD</td><td>MINI CX PÃO DE QUEIJO NUU PEQ 15g - 1KG FOOD</td><td>Recebimento</td><td>20</td><td>CX</td><td>R$ 64,7700</td><td>20</td><td></td></tr></tbody>
</table>
</body></html>
```

- [ ] **Step 2: Escrever os testes que falham**

Acrescentar em `stokki/test_recebimentos.py`, no import: `extrair_chave_nfe, extrair_ref_pedido, extrair_ref_da_linha, ler_detalhe`, e no fim (antes do `if __name__`):

```python
_FIXTURE_CABECALHO = Path(__file__).parent / "fixtures" / "recebimento_detalhe_cabecalho.html"


class TestCabecalhoDoDetalhe(unittest.TestCase):
    """Sondagem de 24-25/09/2026: o detalhe de um #PE mostra 'NF-e:' com a
    chave de 44 digitos e 'Ref. do Pedido:' com o numero da NF (o wizard
    XML grava po = nrNota). E por isso que a aba Pedidos de Entrada do
    portal concilia pela chave, exata."""

    def setUp(self):
        self.html = _FIXTURE_CABECALHO.read_text(encoding="utf-8")

    def test_chave_nfe_sai_com_44_digitos_sem_espacos(self):
        self.assertEqual(extrair_chave_nfe(self.html), "35260912345678000195550010000412211000000017")

    def test_ref_do_pedido_e_o_numero_da_nf(self):
        self.assertEqual(extrair_ref_pedido(self.html), "41221")

    def test_sem_nfe_devolve_vazio(self):
        html = self.html.replace("35260912345678000195550010000412211000000017", "Não informado")
        self.assertEqual(extrair_chave_nfe(html), "")

    def test_ref_nao_informada_devolve_vazio(self):
        html = self.html.replace("<td>41221</td>", "<td> Não informado </td>")
        self.assertEqual(extrair_ref_pedido(html), "")

    def test_pagina_sem_cabecalho_devolve_vazio(self):
        self.assertEqual(extrair_chave_nfe("<html></html>"), "")
        self.assertEqual(extrair_ref_pedido("<html></html>"), "")

    def test_ler_detalhe_junta_itens_chave_e_ref(self):
        class _Resp:
            status_code = 200
            text = self.html

            def raise_for_status(self):
                pass

        class _Sessao:
            chamadas = []

            def get(self, url, **kw):
                self.chamadas.append(url)
                return _Resp()

        sess = _Sessao()
        d = ler_detalhe(sess, 2497)
        self.assertTrue(sess.chamadas[0].endswith("/incoming/show/2497"))
        self.assertEqual(d["chave_nfe"], "35260912345678000195550010000412211000000017")
        self.assertEqual(d["ref_pedido"], "41221")
        self.assertEqual([i["sku"] for i in d["itens"]], ["NUU001FD"])


class TestRefDaLinha(unittest.TestCase):
    def test_segundo_numero_da_listagem_e_a_ref_do_pedido(self):
        # Correcao (25/09/2026): o numero solto ao lado do #PE-2478 e a
        # "Ref. do Pedido" (= numero da NF quando criado por XML), nao um
        # id interno. Continua NAO servindo pra abrir /show/{id}.
        self.assertEqual(extrair_ref_da_linha(_LINHA_ARMADILHA), "41099")

    def test_linha_sem_segundo_numero_devolve_vazio(self):
        self.assertEqual(extrair_ref_da_linha({"id": '<a href="/show/2478">#PE-2478</a>'}), "")
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `py -3.11 -m unittest stokki.test_recebimentos -v`
Expected: `ImportError: cannot import name 'extrair_chave_nfe'`.

- [ ] **Step 4: Implementar**

Em `stokki/recebimentos.py`, trocar as linhas 23-28 do docstring por:

```
ARMADILHA -- a linha tem dois numeros diferentes no campo 'id':
    <a href="https://freshlog.stokki.com.br/.../incoming/show/2478">#PE-2478</a><br>
    <span class="text-muted">41099</span>
O codigo e '#PE-2478', o id que abre o detalhe e 2478 (do href). O 41099
e a "Ref. do Pedido" (= numero da NF quando o #PE foi criado por XML,
confirmado na sondagem de 25/09/2026) -- serve pra conciliar com o
portal, mas NAO serve pra abrir o detalhe: incoming/show/41099 devolve
500. Extrair o id sempre do href.

Cabecalho do detalhe (duas tabelas <tr><th>rotulo</th><td>valor</td></tr>):
  ID do Pedido | Ref. do Pedido | Tipo | Movimento | Cliente | unidade
  Situacao | Chegada Prevista | Tipo de Acondicionamento | ... | NF-e | CT-e
'NF-e:' traz a chave de 44 digitos num <a> pro portal da Fazenda.
```

E acrescentar no fim do arquivo:

```python
# ── Cabecalho do detalhe (chave NF-e e Ref. do Pedido) ────────────────────────

def _valor_do_rotulo(soup: BeautifulSoup, rotulo: str) -> str:
    """Valor do <td> ao lado do <th> cujo texto comeca com `rotulo`, nas
    tabelas de cabecalho do detalhe. Acha pelo texto do rotulo, nunca por
    posicao (mesma regra do resto do modulo)."""
    alvo = rotulo.upper().rstrip(":")
    for th in soup.find_all("th"):
        texto = th.get_text(" ", strip=True).upper().rstrip(":")
        if texto == alvo or texto.startswith(alvo):
            td = th.find_next_sibling("td")
            if td is not None:
                return td.get_text(" ", strip=True)
    return ""


def _nao_informado(valor: str) -> bool:
    return not valor or valor.strip().upper().replace("Ã", "A") in ("NAO INFORMADO", "-", "—")


def extrair_chave_nfe(html: str) -> str:
    """Chave de acesso (44 digitos) do campo 'NF-e:' do detalhe, ou ''."""
    soup = BeautifulSoup(html, "html.parser")
    digitos = re.sub(r"\D", "", _valor_do_rotulo(soup, "NF-e"))
    return digitos if len(digitos) == 44 else ""


def extrair_ref_pedido(html: str) -> str:
    """'Ref. do Pedido:' do detalhe (= numero da NF quando criado por XML;
    = po quando criado por Excel), ou '' quando 'Nao informado'."""
    soup = BeautifulSoup(html, "html.parser")
    valor = _valor_do_rotulo(soup, "Ref. do Pedido")
    return "" if _nao_informado(valor) else valor.strip()


def extrair_ref_da_linha(linha) -> str:
    """O segundo numero do campo 'id' da listagem ('#PE-2478 ... 41099'):
    e a Ref. do Pedido, nao um id. '' quando nao ha."""
    html = str(linha.get("id", "")) if isinstance(linha, dict) else ""
    texto = re.sub(r"<[^>]+>", " ", html)
    texto = re.sub(r"#PE-\d+", " ", texto)
    m = re.search(r"\S+", texto)
    return m.group(0) if m else ""


def ler_detalhe(sessao: StokkiSession, id_recebimento: int) -> dict:
    """GET no detalhe UMA vez e devolve itens + chave NF-e + Ref. do Pedido
    (o timer do WMS e o worker do portal precisam dos tres do mesmo HTML)."""
    resp = sessao.get(f"{BASE_URL}/pt-br/administrator/inventory/incoming/show/{id_recebimento}")
    resp.raise_for_status()
    html = resp.text
    return {"itens": extrair_itens_do_recebimento(html),
            "chave_nfe": extrair_chave_nfe(html),
            "ref_pedido": extrair_ref_pedido(html)}
```

- [ ] **Step 5: Rodar e ver passar**

Run: `py -3.11 -m unittest stokki.test_recebimentos -v`
Expected: todos PASS (os antigos continuam verdes).

- [ ] **Step 6: Compilar e conferir o git**

Run: `py -3.11 -m py_compile stokki/recebimentos.py && git status --short`
Expected: só `stokki/recebimentos.py`, `stokki/test_recebimentos.py` e a fixture nova aparecem como seus.

---

### Task 2: `ler_nfe` aceita NF-e de entrada e devolve os itens

**Files:**
- Modify: `portal_cliente/envio_pedidos.py:268-343` (`ler_nfe`)
- Test: `portal_cliente/test_entradas.py` (novo; recebe os testes das Tarefas 2, 3, 4 e 5)

**Interfaces:**
- Produces: `ler_nfe(conteudo, nome_arquivo="", permitir_entrada=False) -> dict`. Com `permitir_entrada=False` comporta-se exatamente como hoje (recusa `tpNF=0`). O dict ganha `tipo_nf` (`"0"`/`"1"`) e `itens_lista: list[dict]` com `{linha, sku, ean, descricao, quantidade, unidade, valor_unitario}` por `<det>`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `portal_cliente/test_entradas.py`:

```python
# -*- coding: utf-8 -*-
"""
Aba Pedidos de Entrada do portal (spec
docs/superpowers/specs/2026-09-24-pedidos-de-entrada-portal-design.md).
Sem rede e sem tocar no dados.db real.

    py -3.11 -m unittest portal_cliente.test_entradas -v
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

CNPJ = "12345678000195"          # o cliente (emitente da remessa)
CNPJ_FRESHLOG = "11222333000181"  # destinatário (galpão)
OUTRO = "99887766000155"
HOJE = date.today()
AMANHA = (HOJE + timedelta(days=1)).isoformat()


def xml_remessa(nf="41221", emitente=CNPJ, dest=CNPJ_FRESHLOG, tp_nf="1", itens=None, chave=None):
    """NF-e de remessa pra armazenagem: emitente = cliente, destinatário =
    Fresh Log. tpNF=1 (saída do ponto de vista de quem emite)."""
    chave = chave or f"3526091234567800019555001000{nf.zfill(6)}1000000017"[:44]
    itens = itens or [("NUU001FD", "7891234567890", "MINI CX PAO DE QUEIJO", 20, "CX", 64.77)]
    dets = "".join(
        f'<det nItem="{i}"><prod><cProd>{sku}</cProd><cEAN>{ean}</cEAN><xProd>{desc}</xProd>'
        f'<qCom>{qtd}</qCom><uCom>{un}</uCom><vUnCom>{vu}</vUnCom></prod></det>'
        for i, (sku, ean, desc, qtd, un, vu) in enumerate(itens, start=1))
    return f"""<?xml version="1.0"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{chave}">
      <ide><nNF>{nf}</nNF><serie>1</serie><tpNF>{tp_nf}</tpNF><dhEmi>2026-09-24T10:00:00-03:00</dhEmi></ide>
      <emit><CNPJ>{emitente}</CNPJ><xNome>Cliente Teste</xNome></emit>
      <dest><CNPJ>{dest}</CNPJ><xNome>FRESHLOG</xNome><enderDest><xLgr>Rua G</xLgr><nro>1</nro><xBairro>B</xBairro>
      <xMun>São Paulo</xMun><UF>SP</UF><CEP>01310100</CEP></enderDest></dest>
      {dets}
      <transp><vol><qVol>3</qVol><pesoB>12.5</pesoB></vol></transp><total><ICMSTot><vNF>1295.40</vNF></ICMSTot></total>
    </infNFe></NFe></nfeProc>""".encode("utf-8")


class TestLerNfeEntrada(unittest.TestCase):
    def test_envios_continua_recusando_tpnf_zero(self):
        with self.assertRaises(ep.ErroEnvio):
            ep.ler_nfe(xml_remessa(tp_nf="0"), "x.xml")

    def test_permitir_entrada_aceita_tpnf_zero_e_um(self):
        for tp in ("0", "1"):
            nfe = ep.ler_nfe(xml_remessa(tp_nf=tp), "x.xml", permitir_entrada=True)
            self.assertEqual(nfe["tipo_nf"], tp)
            self.assertEqual(nfe["numero_nf"], "41221")

    def test_itens_lista_vem_do_det(self):
        nfe = ep.ler_nfe(xml_remessa(itens=[("A", "7890000000001", "Prod A", "2.5", "CX", "10.00"),
                                            ("B", "SEM GTIN", "Prod B", 1, "UN", 3)]), "x.xml")
        self.assertEqual(nfe["itens"], 2)
        self.assertEqual(nfe["itens_lista"][0], {"linha": 1, "sku": "A", "ean": "7890000000001", "descricao": "Prod A",
                                                 "quantidade": 2.5, "unidade": "CX", "valor_unitario": 10.0})
        self.assertEqual(nfe["itens_lista"][1]["ean"], "")   # 'SEM GTIN' vira vazio
        self.assertEqual(nfe["itens_lista"][1]["quantidade"], 1.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_entradas -v`
Expected: `TypeError: ler_nfe() got an unexpected keyword argument 'permitir_entrada'` e `KeyError: 'itens_lista'`.

- [ ] **Step 3: Implementar**

Em `portal_cliente/envio_pedidos.py`, trocar a assinatura e o bloco do `tpNF` (linhas 268-293):

```python
def ler_nfe(conteudo: bytes, nome_arquivo: str = "", permitir_entrada: bool = False) -> dict:
    """Extrai os campos que a máscara mostra/valida. Levanta ErroEnvio com
    mensagem pro usuário quando o arquivo não é uma NF-e (validação 1 do
    item 9). `permitir_entrada` (aba Pedidos de Entrada, 25/09): aceita
    tpNF=0 também -- Envios continua recusando."""
```

e, no lugar das linhas 291-293:

```python
    tipo_nf = _texto(ide, "nfe:tpNF")
    if tipo_nf == "0" and not permitir_entrada:
        raise ErroEnvio(f"{nome_arquivo or 'Arquivo'}: NF-e de ENTRADA (tpNF=0) -- só notas de saída viram pedido de entrega.")
```

Depois de `dets = inf.findall("nfe:det", NS)` (linha 295), acrescentar:

```python
    itens_lista = []
    for i, det in enumerate(dets, start=1):
        prod = det.find("nfe:prod", NS)
        ean = _texto(prod, "nfe:cEAN")
        try:
            qtd = float(_texto(prod, "nfe:qCom") or 0)
        except ValueError:
            qtd = 0.0
        try:
            vu = float(_texto(prod, "nfe:vUnCom") or 0)
        except ValueError:
            vu = 0.0
        itens_lista.append({
            "linha": int(det.get("nItem") or i), "sku": _texto(prod, "nfe:cProd"),
            "ean": _so_digitos(ean) if ean.upper() != "SEM GTIN" else "",
            "descricao": _texto(prod, "nfe:xProd"), "quantidade": qtd,
            "unidade": _texto(prod, "nfe:uCom"), "valor_unitario": vu,
        })
```

E no dict devolvido (linha 321 em diante), acrescentar duas chaves:

```python
        "tipo_nf": tipo_nf,
        "itens_lista": itens_lista,
```

- [ ] **Step 4: Rodar e ver passar; regressão de Envios**

Run: `py -3.11 -m unittest portal_cliente.test_entradas -v && py -3.11 -m pytest portal_cliente/test_envio_planilha.py -q 2>/dev/null || py -3.11 -m unittest portal_cliente.test_bloqueio_area -v`
Expected: `test_entradas` PASS; os testes de envios existentes continuam verdes (o `test_envio_planilha.py` é pytest e pode não rodar sem pytest instalado: nesse caso rode só o unittest de bloqueio e confira manualmente que `ler_nfe(xml)` sem o parâmetro continua recusando `tpNF=0`, que é o primeiro teste desta tarefa).

- [ ] **Step 5: Compilar**

Run: `py -3.11 -m py_compile portal_cliente/envio_pedidos.py portal_cliente/test_entradas.py`

---

### Task 3: `portal_cliente/entradas.py` — tabelas, leitura e validação (XML e planilha)

**Files:**
- Create: `portal_cliente/entradas.py`
- Test: `portal_cliente/test_entradas.py`

**Interfaces:**
- Produces: constantes `STATUS_ANUNCIADO="ANUNCIADO"`, `STATUS_CHEGOU="CHEGOU"`, `STATUS_ENDERECADO="ENDERECADO"`, `STATUS_DIVERGENCIA="DIVERGENCIA"`, `STATUS_CANCELADO="CANCELADO"`, `ROTULOS_STATUS`, `STOKKI_NA_FILA="NA_FILA"`, `STOKKI_ENVIANDO="ENVIANDO"`, `STOKKI_CRIADO="CRIADO"`, `STOKKI_ERRO="ERRO"`, `ROTULOS_STOKKI`, `ORIGEM_XML`, `ORIGEM_PLANILHA` (os de `envio_pedidos`), `DIAS_LISTAGEM=30`.
- Produces: `garantir_tabelas(conn)`, `conectar() -> sqlite3.Connection`, `ler_nfe_entrada(conteudo, nome) -> dict`, `validar_entrada(conn, item, cnpj_cliente, config, outras_empresas=None) -> {ok, erros, avisos, existente}`, `COLUNAS_PLANILHA_ENTRADA`, `chave_planilha_entrada(cnpj, referencia) -> str`, `ler_planilha_entrada(conteudo, nome, cnpj) -> (pedidos, rejeitados)`, `gerar_modelo_planilha_entrada(nome_cliente="") -> bytes`, `cnpj_freshlog(config) -> str`.
- Consumes: `ep.ler_nfe(..., permitir_entrada=True)` (Tarefa 2), `ep.validar_skus`, `ep.config_stokki_cliente`, `ep._abrir_planilha`, `ep._normalizar_texto`, `ep._celula_texto`, `ep._numero_br`, `ep._data_planilha`, `ep.formatar_documento`, `ep._so_digitos`, `ep._garantir_colunas`.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `portal_cliente/test_entradas.py` (depois de `TestLerNfeEntrada`):

```python
import entradas  # noqa: E402  (fica junto dos outros imports no topo do arquivo)


class BasePortal(unittest.TestCase):
    """Banco temporário com `interno` (o cliente é o stkkc 48, piloto) e o
    catálogo wms_produtos do embarcador -- o mesmo desenho de
    test_envio_planilha.ambiente, em unittest."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        raiz = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(ep, "DB_PATH", raiz / "dados.db"),
            mock.patch.object(ep, "_RAIZ", raiz),
            mock.patch.object(ep, "PASTA_XMLS", raiz / "portal_envios"),
            mock.patch.object(ep, "PASTA_TEMP", raiz / "portal_envios" / "_temporarios"),
        ]
        for p in self._patches:
            p.start()
        self.conn = entradas.conectar()
        self.conn.execute("CREATE TABLE IF NOT EXISTS interno (cnpj_embarcador TEXT, apelido TEXT, nome_remetente TEXT, "
                          "stkkc_id INTEGER, sender_id INTEGER, email TEXT, notificar_email INTEGER)")
        self.conn.execute("INSERT INTO interno VALUES (?, 'CLIENTE TESTE', 'CLIENTE TESTE', 48, 1, 'c@t.com', 1)", (CNPJ,))
        self.conn.execute("INSERT INTO interno VALUES (?, 'OUTRO', 'OUTRO', 77, 2, 'o@t.com', 1)", (OUTRO,))
        self.conn.execute("CREATE TABLE IF NOT EXISTS wms_produtos (id INTEGER PRIMARY KEY, stokki_id INTEGER, sku TEXT, ean TEXT, dun TEXT, "
                          "descricao TEXT, embarcador TEXT, embarcador_id INTEGER, qtd_por_caixa REAL, unidade TEXT, ativo INTEGER, atualizado_em TEXT)")
        self.conn.execute("INSERT INTO wms_produtos VALUES (1, 900, 'NUU001FD', '7891234567890', NULL, 'MINI CX PAO DE QUEIJO', "
                          "'CLIENTE TESTE', 48, 1, 'UN', 1, '2026-09-25 10:00:00')")
        self.conn.execute("INSERT INTO wms_produtos VALUES (2, 901, 'SKU-B', NULL, NULL, 'PRODUTO B', 'CLIENTE TESTE', 48, 1, 'UN', 1, '2026-09-25 10:00:00')")
        self.conn.commit()
        self.config = {"portal_entradas": {"cnpj_freshlog": CNPJ_FRESHLOG}}

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class TestTabelas(BasePortal):
    def test_cria_tabelas_e_coluna_entradas_ativo(self):
        nomes = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("portal_entradas", nomes)
        self.assertIn("portal_entrada_itens", nomes)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(portal_clientes_envio)")}
        self.assertIn("entradas_ativo", cols)

    def test_garantir_tabelas_e_idempotente_em_conexao_alheia(self):
        # o timer do WMS abre a conexão dele (wms_pedidos.conectar) e chama
        # garantir_tabelas antes de amarrar -- rodar duas vezes não quebra.
        entradas.garantir_tabelas(self.conn)
        entradas.garantir_tabelas(self.conn)


class TestValidarEntrada(BasePortal):
    def _item(self, **kw):
        nfe = entradas.ler_nfe_entrada(xml_remessa(**kw), "nota.xml")
        return nfe

    def test_nfe_valida_sem_erros_com_itens(self):
        v = entradas.validar_entrada(self.conn, self._item(), CNPJ, self.config)
        self.assertTrue(v["ok"], v["erros"])
        self.assertEqual(v["avisos"], [])

    def test_emitente_diferente_do_cliente_e_erro(self):
        v = entradas.validar_entrada(self.conn, self._item(emitente=OUTRO), CNPJ, self.config)
        self.assertFalse(v["ok"])
        self.assertIn("não é o da sua empresa", v["erros"][0])

    def test_emitente_de_outra_empresa_do_grupo_manda_trocar_o_seletor(self):
        v = entradas.validar_entrada(self.conn, self._item(emitente=OUTRO), CNPJ, self.config, {OUTRO: "OUTRO LTDA"})
        self.assertIn("Troque a empresa no seletor", v["erros"][0])

    def test_destinatario_diferente_da_freshlog_e_so_aviso(self):
        v = entradas.validar_entrada(self.conn, self._item(dest="55555555000199"), CNPJ, self.config)
        self.assertTrue(v["ok"])
        self.assertIn("destinatário da nota não é a Fresh Log", v["avisos"][0])

    def test_sem_cnpj_freshlog_no_config_nao_avisa(self):
        v = entradas.validar_entrada(self.conn, self._item(dest="55555555000199"), CNPJ, {})
        self.assertEqual(v["avisos"], [])

    def test_sku_fora_do_catalogo_e_erro(self):
        item = self._item(itens=[("NAO-EXISTE", "", "X", 1, "UN", 1)])
        v = entradas.validar_entrada(self.conn, item, CNPJ, self.config)
        self.assertFalse(v["ok"])
        self.assertIn("SKU não encontrado no catálogo", v["erros"][0])

    def test_chave_ja_anunciada_e_cancelada(self):
        item = self._item()
        entradas.garantir_tabelas(self.conn)
        self.conn.execute("INSERT INTO portal_entradas (cnpj_embarcador, origem, chave_nfe, numero_nf, data_prevista, arquivo_path, "
                          "status, criado_em, atualizado_em) VALUES (?, 'xml', ?, '41221', ?, 'x', 'ANUNCIADO', '2026-09-24 10:00:00', '2026-09-24 10:00:00')",
                          (CNPJ, item["chave_nfe"], AMANHA))
        self.conn.commit()
        v = entradas.validar_entrada(self.conn, item, CNPJ, self.config)
        self.assertFalse(v["ok"])
        self.assertIn("já anunciada em 24/09/2026 10:00 (Anunciado)", v["erros"][0])
        self.conn.execute("UPDATE portal_entradas SET status = 'CANCELADO'")
        self.conn.commit()
        v = entradas.validar_entrada(self.conn, item, CNPJ, self.config)
        self.assertTrue(v["ok"])
        self.assertIn("será reaberta", v["avisos"][0])
        self.assertEqual(v["existente"]["status"], "CANCELADO")


class TestPlanilhaEntrada(BasePortal):
    def _planilha(self, linhas, cabecalho=None):
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        cab = cabecalho or [c[1] for c in entradas.COLUNAS_PLANILHA_ENTRADA]
        chaves = [c[0] for c in entradas.COLUNAS_PLANILHA_ENTRADA]
        ws.append(cab)
        for l in linhas:
            ws.append([l.get(k, "") for k in (chaves if not cabecalho else cabecalho)])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _linha(self, **kw):
        base = {"referencia": "REM-1", "data_prevista": "/".join(reversed(AMANHA.split("-"))), "sku": "NUU001FD", "quantidade": 10,
                "unidade": "CX", "numero_nf": "", "volumes": 3, "peso_kg": "12,5", "observacoes": ""}
        base.update(kw)
        return base

    def test_modelo_tem_as_colunas_e_e_lido_de_volta(self):
        import io
        import openpyxl
        conteudo = entradas.gerar_modelo_planilha_entrada("Cliente X")
        wb = openpyxl.load_workbook(io.BytesIO(conteudo))
        self.assertEqual(wb.sheetnames, ["Entradas", "Instruções"])
        self.assertEqual([c.value for c in wb["Entradas"][1]], [c[1] for c in entradas.COLUNAS_PLANILHA_ENTRADA])
        pedidos, rejeitados = entradas.ler_planilha_entrada(conteudo, "modelo.xlsx", CNPJ)
        self.assertEqual(rejeitados, [])
        self.assertEqual([p["referencia"] for p in pedidos], ["REM-1001", "REM-1002"])

    def test_agrupa_por_referencia_e_le_data_prevista(self):
        conteudo = self._planilha([self._linha(), self._linha(sku="SKU-B", quantidade=2, data_prevista=""),
                                   self._linha(referencia="REM-2", sku="SKU-B", quantidade="1,5")])
        pedidos, rejeitados = entradas.ler_planilha_entrada(conteudo, "e.xlsx", CNPJ)
        self.assertEqual(rejeitados, [])
        self.assertEqual(len(pedidos), 2)
        p1 = pedidos[0]
        self.assertEqual(p1["origem"], "planilha")
        self.assertEqual(p1["referencia"], "REM-1")
        self.assertEqual(p1["data_prevista"], AMANHA)
        self.assertEqual([i["sku"] for i in p1["itens_lista"]], ["NUU001FD", "SKU-B"])
        self.assertEqual(p1["itens_lista"][0]["unidade"], "CX")
        self.assertEqual(p1["volumes"], 3)
        self.assertEqual(p1["peso_kg"], 12.5)
        self.assertEqual(p1["emitente_cnpj"], CNPJ)
        self.assertTrue(p1["chave_nfe"].startswith(f"PLANILHA-ENTRADA-{CNPJ}-REM-1-"))
        self.assertEqual(pedidos[1]["itens_lista"][0]["quantidade"], 1.5)

    def test_rejeita_grupo_com_problema_e_mantem_os_outros(self):
        ontem = "/".join(reversed((HOJE - timedelta(days=1)).isoformat().split("-")))
        linhas = [self._linha(),
                  self._linha(referencia="REM-2", data_prevista=""),                       # sem data
                  self._linha(referencia="REM-3", data_prevista=ontem),                    # passada
                  self._linha(referencia="REM-4", quantidade=0),                           # sem item válido
                  self._linha(referencia="REM-5"), self._linha(referencia="REM-5", data_prevista="31/12/2030"),  # datas diferentes
                  {"sku": "X", "quantidade": 1}]                                           # sem referência
        pedidos, rejeitados = entradas.ler_planilha_entrada(self._planilha(linhas), "e.xlsx", CNPJ)
        self.assertEqual([p["referencia"] for p in pedidos], ["REM-1"])
        por = {r["rotulo"]: r["erro"] for r in rejeitados}
        self.assertIn("data prevista", por["Remessa REM-2"].lower())
        self.assertIn("já passou", por["Remessa REM-3"])
        self.assertIn("maior que zero", por["Remessa REM-4"])
        self.assertIn("data prevista diferente", por["Remessa REM-5"])
        self.assertIn("linha 8", por)

    def test_cabecalho_com_apelidos(self):
        cab = ["Pedido", "Chegada", "Código", "Qtd"]
        linhas = [{"Pedido": 77, "Chegada": "/".join(reversed(AMANHA.split("-"))), "Código": "SKU-B", "Qtd": 3}]
        pedidos, rejeitados = entradas.ler_planilha_entrada(self._planilha(linhas, cab), "x.xlsx", CNPJ)
        self.assertEqual(rejeitados, [])
        self.assertEqual(pedidos[0]["referencia"], "77")
        self.assertEqual(pedidos[0]["itens_lista"][0]["sku"], "SKU-B")

    def test_falta_coluna_obrigatoria(self):
        with self.assertRaises(ep.ErroEnvio):
            entradas.ler_planilha_entrada(self._planilha([{"Pedido": 1}], ["Pedido", "SKU"]), "x.xlsx", CNPJ)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_entradas -v`
Expected: `ModuleNotFoundError: No module named 'entradas'`.

- [ ] **Step 3: Implementar**

Criar `portal_cliente/entradas.py`:

```python
# -*- coding: utf-8 -*-
"""
portal_cliente/entradas.py

Aba "Pedidos de Entrada" do portal do cliente (pedido do Hugo, 24/09/2026):
o embarcador anuncia a mercadoria que vai chegar no galpão (XML da NF-e de
remessa emitida por ele, ou planilha no modelo Fresh Log), o worker
enviar_entradas_stokki.py cria o recebimento (#PE) na Stokki pelo wizard
de XML múltiplo, o timer stokki-wms-recebimentos amarra o #PE a esta
entrada pela chave da NF-e, e o cliente acompanha até o galpão endereçar.

Spec: docs/superpowers/specs/2026-09-24-pedidos-de-entrada-portal-design.md

Tabelas (dados/dados.db):
  portal_entradas        -- uma por NF-e/remessa (chave_nfe única): dados da
                            nota, data prevista, status do ciclo, fila da
                            Stokki, ligação com wms_recebimentos.
  portal_entrada_itens   -- as linhas da nota/planilha como vieram.
  portal_clientes_envio  -- ganha entradas_ativo (só o piloto, D3).

Ciclo (portal_entradas.status), só anda pra frente:
  ANUNCIADO -> CHEGOU -> ENDERECADO | DIVERGENCIA ; CANCELADO (só a partir de ANUNCIADO)
Fila da Stokki (portal_entradas.stokki_status):
  NA_FILA -> ENVIANDO -> CRIADO | ERRO

O que este módulo NUNCA faz: escrever em wms_movimentos/wms_saldos. O
estoque entra quando o operador endereça no celular, como hoje.
"""
import hashlib
import json
import logging
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

logger = logging.getLogger(__name__)

DIAS_LISTAGEM = 30
ORIGEM_XML = ep.ORIGEM_XML
ORIGEM_PLANILHA = ep.ORIGEM_PLANILHA

STATUS_ANUNCIADO = "ANUNCIADO"
STATUS_CHEGOU = "CHEGOU"
STATUS_ENDERECADO = "ENDERECADO"
STATUS_DIVERGENCIA = "DIVERGENCIA"
STATUS_CANCELADO = "CANCELADO"
STATUS_ABERTOS = (STATUS_ANUNCIADO, STATUS_CHEGOU)
ROTULOS_STATUS = {
    STATUS_ANUNCIADO: "Anunciado",
    STATUS_CHEGOU: "Chegou no galpão",
    STATUS_ENDERECADO: "Endereçado",
    STATUS_DIVERGENCIA: "Com divergência",
    STATUS_CANCELADO: "Cancelado",
}
STOKKI_NA_FILA = "NA_FILA"
STOKKI_ENVIANDO = "ENVIANDO"
STOKKI_CRIADO = "CRIADO"
STOKKI_ERRO = "ERRO"
ROTULOS_STOKKI = {
    STOKKI_NA_FILA: "Na fila pra Stokki",
    STOKKI_ENVIANDO: "Criando na Stokki",
    STOKKI_CRIADO: "Recebimento criado na Stokki",
    STOKKI_ERRO: "Erro na Stokki",
}

ErroEnvio = ep.ErroEnvio
_so_digitos = ep._so_digitos
_agora = ep._agora
formatar_documento = ep.formatar_documento


# ── Banco ──────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS portal_entradas (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_embarcador    TEXT NOT NULL,
    origem             TEXT NOT NULL,
    chave_nfe          TEXT NOT NULL UNIQUE,
    numero_nf          TEXT,
    serie              TEXT,
    emitida_em         TEXT,
    referencia         TEXT,
    data_prevista      TEXT NOT NULL,
    volumes            INTEGER,
    peso_kg            REAL,
    valor_nf           REAL,
    arquivo_path       TEXT NOT NULL,
    status             TEXT NOT NULL,
    stokki_status      TEXT NOT NULL DEFAULT 'NA_FILA',
    stokki_id          INTEGER,
    stokki_codigo      TEXT,
    stokki_erro        TEXT,
    stokki_tentativas  INTEGER NOT NULL DEFAULT 0,
    wms_recebimento_id INTEGER,
    observacoes        TEXT,
    enviado_por        TEXT,
    criado_em          TEXT NOT NULL,
    atualizado_em      TEXT NOT NULL,
    cancelado_em       TEXT,
    cancelado_por      TEXT,
    chamado_id         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_portal_entradas_emb ON portal_entradas (cnpj_embarcador, criado_em);
CREATE INDEX IF NOT EXISTS idx_portal_entradas_status ON portal_entradas (status);
CREATE TABLE IF NOT EXISTS portal_entrada_itens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entrada_id  INTEGER NOT NULL REFERENCES portal_entradas(id),
    linha       INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    ean         TEXT NOT NULL DEFAULT '',
    descricao   TEXT NOT NULL DEFAULT '',
    quantidade  REAL NOT NULL,
    unidade     TEXT NOT NULL DEFAULT '',
    UNIQUE (entrada_id, linha)
);
"""


def garantir_tabelas(conn: sqlite3.Connection) -> None:
    """Idempotente. Chamada por conectar() e também pelo timer do WMS
    (que abre a conexão dele em wms_pedidos.conectar) antes de amarrar."""
    conn.executescript(_DDL)
    if conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'portal_clientes_envio'").fetchone():
        ep._garantir_colunas(conn, "portal_clientes_envio", {"entradas_ativo": "INTEGER NOT NULL DEFAULT 0"})
    conn.commit()


def conectar() -> sqlite3.Connection:
    """A conexão de envio_pedidos (cria portal_envios/portal_clientes_envio)
    mais as tabelas desta aba."""
    conn = ep.conectar()
    garantir_tabelas(conn)
    return conn


def _pasta_entradas() -> Path:
    # função (não constante) porque os testes trocam ep._RAIZ
    return ep._RAIZ / "dados" / "portal_entradas"


def cnpj_freshlog(config: dict | None) -> str:
    return _so_digitos(((config or {}).get("portal_entradas") or {}).get("cnpj_freshlog") or "")


# ── Leitura ────────────────────────────────────────────────────────────────────

def ler_nfe_entrada(conteudo: bytes, nome_arquivo: str = "") -> dict:
    """NF-e de remessa pra armazenagem (D1): aceita tpNF 0 ou 1; a
    validação de quem emitiu fica em validar_entrada."""
    nfe = ep.ler_nfe(conteudo, nome_arquivo, permitir_entrada=True)
    nfe["origem"] = ORIGEM_XML
    nfe["referencia"] = ""
    nfe["data_prevista"] = ""
    return nfe


def rotulo_entrada(e: dict) -> str:
    if e.get("numero_nf"):
        return f"NF {e['numero_nf']}"
    return f"Remessa {e.get('referencia') or '?'}"


def _quando_br(valor: str | None) -> str:
    if not valor:
        return ""
    try:
        return datetime.strptime(valor[:19], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return valor


def validar_entrada(conn: sqlite3.Connection, item: dict, cnpj_cliente: str, config: dict | None,
                    outras_empresas: dict[str, str] | None = None) -> dict:
    """{ok, erros[], avisos[], existente} -- as 5 regras da spec (7.1),
    menos a data prevista, que só existe na confirmação."""
    erros, avisos = [], []
    emb = _so_digitos(cnpj_cliente)
    if item.get("emitente_cnpj") != emb:
        do_grupo = (outras_empresas or {}).get(item.get("emitente_cnpj"))
        if do_grupo:
            erros.append(f"Essa nota é da {do_grupo} ({formatar_documento(item['emitente_cnpj'])}). "
                         f"Troque a empresa no seletor acima e envie de novo.")
        else:
            erros.append(f"O CNPJ emitente da nota ({formatar_documento(item.get('emitente_cnpj') or '')} · "
                         f"{item.get('emitente_nome') or ''}) não é o da sua empresa.")
    existente = conn.execute("SELECT id, status, criado_em, stokki_codigo FROM portal_entradas WHERE chave_nfe = ?",
                             (item["chave_nfe"],)).fetchone()
    if existente and existente["status"] != STATUS_CANCELADO:
        erros.append(f"{rotulo_entrada(item)} já anunciada em {_quando_br(existente['criado_em'])} "
                     f"({ROTULOS_STATUS.get(existente['status'], existente['status'])}"
                     + (f", {existente['stokki_codigo']}" if existente["stokki_codigo"] else "") + ").")
    elif existente:
        avisos.append("Já esteve anunciada e foi cancelada -- será reaberta.")
    fl = cnpj_freshlog(config)
    if fl and item.get("origem") == ORIGEM_XML and _so_digitos(item.get("destinatario_doc")) != fl:
        avisos.append(f"O destinatário da nota não é a Fresh Log ({item.get('destinatario_nome') or formatar_documento(item.get('destinatario_doc') or '')}). "
                      f"Confira se é mesmo uma remessa pro galpão.")
    try:
        cfg = ep.config_stokki_cliente(conn, emb, config)
    except ErroEnvio as e:
        erros.append(str(e))
        cfg = None
    if cfg is not None:
        erros_sku, avisos_sku = ep.validar_skus(conn, item, cfg)
        erros.extend(erros_sku)
        avisos.extend(avisos_sku)
    if not item.get("itens_lista"):
        erros.append("A nota não tem itens (<det>) -- não dá pra anunciar o que vai chegar.")
    return {"ok": not erros, "erros": erros, "avisos": avisos, "existente": dict(existente) if existente else None}


# ── Planilha de entrada ────────────────────────────────────────────────────────
# Modelo próprio: UMA LINHA POR ITEM; linhas com a mesma "Referência" formam
# uma remessa. Sem endereço/destinatário/valor: o destino é sempre o galpão.

COLUNAS_PLANILHA_ENTRADA: list[tuple[str, str, bool, tuple[str, ...], int, str]] = [
    ("referencia", "Referência (nº do pedido de compra/remessa)", True,
     ("referencia", "pedido", "remessa", "n pedido", "numero do pedido", "po", "ordem", "order"), 30, "REM-1001"),
    ("data_prevista", "Data prevista de chegada (DD/MM/AAAA)", True,
     ("data prevista", "data prevista de chegada", "chegada", "data de chegada", "previsao", "data"), 22, ""),
    ("sku", "SKU do produto", True, ("sku", "codigo", "codigo do produto", "cod produto", "produto", "ean", "gtin", "item", "cod"), 18, "NUU001FD"),
    ("quantidade", "Quantidade", True, ("quantidade", "qtd", "qtde", "qte", "quant"), 12, "10"),
    ("unidade", "Unidade (CX, UN...)", False, ("unidade", "un", "und", "embalagem"), 12, "CX"),
    ("numero_nf", "Nº da NF (opcional)", False, ("nf", "n nf", "numero nf", "numero da nf", "nota", "nota fiscal", "nfe"), 16, ""),
    ("volumes", "Volumes", False, ("volumes", "vol", "qtd volumes", "caixas"), 10, "3"),
    ("peso_kg", "Peso (kg)", False, ("peso", "peso kg", "peso (kg)", "peso bruto"), 10, "12,5"),
    ("observacoes", "Observações", False, ("observacoes", "observacao", "obs"), 30, ""),
]
_APELIDOS_ENTRADA: dict[str, str] = {}
for _c in COLUNAS_PLANILHA_ENTRADA:
    _APELIDOS_ENTRADA[ep._normalizar_texto(_c[1])] = _c[0]
    for _a in _c[3]:
        _APELIDOS_ENTRADA.setdefault(ep._normalizar_texto(_a), _c[0])
MAX_LINHAS_PLANILHA = 2000


def chave_planilha_entrada(cnpj_embarcador: str, referencia: str) -> str:
    """Chave sintética única por embarcador+referência (vai em chave_nfe só
    pra dedupe). Prefixo próprio pra nunca colidir com a de Envios."""
    ref = re.sub(r"[^A-Z0-9]+", "-", str(referencia or "").upper()).strip("-")[:40]
    digest = hashlib.sha1(f"entrada|{_so_digitos(cnpj_embarcador)}|{str(referencia or '').strip().upper()}".encode()).hexdigest()[:10]
    return f"PLANILHA-ENTRADA-{_so_digitos(cnpj_embarcador)}-{ref}-{digest}"


def _achar_cabecalho(linhas: list[list]) -> tuple[int, dict[int, str]]:
    melhor = (0, -1, {})
    for i, linha in enumerate(linhas[:15]):
        mapa = {}
        for j, v in enumerate(linha):
            chave = _APELIDOS_ENTRADA.get(ep._normalizar_texto(v))
            if chave and chave not in mapa.values():
                mapa[j] = chave
        if len(mapa) > melhor[0]:
            melhor = (len(mapa), i, mapa)
    if melhor[0] < 3:
        raise ErroEnvio("Não achei o cabeçalho da planilha -- use o modelo Fresh Log de entrada (botão \"Baixar modelo\") "
                        "e mantenha a primeira linha com os nomes das colunas.")
    return melhor[1], melhor[2]


def ler_planilha_entrada(conteudo: bytes, nome: str, cnpj_embarcador: str) -> tuple[list[dict], list[dict]]:
    """(remessas, rejeitados). Cada remessa tem o MESMO formato do dict de
    ler_nfe_entrada (origem, referencia, data_prevista, itens_lista, ...)."""
    linhas = ep._abrir_planilha(conteudo, nome)
    if not linhas:
        raise ErroEnvio(f"{nome}: a planilha está vazia.")
    i_cab, mapa = _achar_cabecalho(linhas)
    faltando = [c[1] for c in COLUNAS_PLANILHA_ENTRADA if c[2] and c[0] not in mapa.values()]
    if faltando:
        raise ErroEnvio(f"{nome}: faltam colunas obrigatórias no cabeçalho: {', '.join(faltando)}. "
                        f"Baixe o modelo Fresh Log de entrada pra conferir os nomes.")
    emb = _so_digitos(cnpj_embarcador)
    hoje = date.today()
    grupos: dict[str, dict] = {}
    erros_grupo: dict[str, list[str]] = {}
    rejeitados: list[dict] = []
    total = 0
    for n, linha in enumerate(linhas[i_cab + 1:], start=i_cab + 2):
        campos = {chave: (linha[j] if j < len(linha) else None) for j, chave in mapa.items()}
        if not any(ep._celula_texto(v) for v in campos.values()):
            continue
        total += 1
        if total > MAX_LINHAS_PLANILHA:
            raise ErroEnvio(f"{nome}: a planilha tem mais de {MAX_LINHAS_PLANILHA} linhas -- divida em arquivos menores.")
        ref = ep._celula_texto(campos.get("referencia"))
        if not ref:
            rejeitados.append({"arquivo": nome, "rotulo": f"linha {n}", "linha": n, "erro": "Sem a referência da remessa."})
            continue
        chave_grupo = ref.strip().upper()
        erros = erros_grupo.setdefault(chave_grupo, [])
        sku = ep._celula_texto(campos.get("sku"))
        try:
            qtd = ep._numero_br(campos.get("quantidade"))
        except ValueError:
            qtd = None
            erros.append(f"linha {n}: quantidade inválida ({ep._celula_texto(campos.get('quantidade'))}).")
        if not sku:
            erros.append(f"linha {n}: sem SKU.")
        elif qtd is None or qtd <= 0:
            erros.append(f"linha {n}: quantidade precisa ser maior que zero.")
        data_txt = ep._celula_texto(campos.get("data_prevista"))
        try:
            data_prev = ep._data_planilha(campos.get("data_prevista"))
        except ValueError:
            data_prev = None
            erros.append(f"linha {n}: data prevista inválida ({data_txt}) -- use DD/MM/AAAA.")
        g = grupos.get(chave_grupo)
        if g is None:
            if not data_prev and not data_txt:
                erros.append("Sem a data prevista de chegada (obrigatória).")
            elif data_prev and data_prev < hoje:
                erros.append(f"Data prevista {data_prev.strftime('%d/%m/%Y')} já passou.")
            volumes = peso = None
            try:
                volumes = ep._numero_br(campos.get("volumes"))
                peso = ep._numero_br(campos.get("peso_kg"))
            except ValueError:
                erros.append("Volumes ou peso com número inválido.")
            g = grupos[chave_grupo] = {
                "chave_nfe": chave_planilha_entrada(emb, ref), "origem": ORIGEM_PLANILHA, "referencia": ref[:60],
                "numero_nf": _so_digitos(campos.get("numero_nf"))[:20], "serie": "", "emitida_em": "",
                "emitente_cnpj": emb, "emitente_nome": "", "destinatario_doc": "", "destinatario_nome": "",
                "data_prevista": data_prev.isoformat() if data_prev else "",
                "volumes": int(volumes) if volumes else 0, "peso_kg": round(peso or 0.0, 3), "valor_nf": 0.0,
                "observacoes": ep._celula_texto(campos.get("observacoes"))[:500],
                "itens_lista": [], "linhas": [], "nome_arquivo": nome,
            }
        elif data_prev and g["data_prevista"] and data_prev.isoformat() != g["data_prevista"]:
            erros.append(f"linha {n}: data prevista diferente da primeira linha da remessa {ref}.")
        g["itens_lista"].append({"linha": len(g["itens_lista"]) + 1, "sku": sku, "ean": "",
                                 "descricao": "", "quantidade": qtd or 0,
                                 "unidade": ep._celula_texto(campos.get("unidade")).upper()[:10], "valor_unitario": 0.0})
        g["linhas"].append(n)

    pedidos = []
    for chave_grupo, g in grupos.items():
        erros = erros_grupo.get(chave_grupo) or []
        if not any(i["sku"] and i["quantidade"] > 0 for i in g["itens_lista"]):
            erros.append("Nenhum item válido (SKU + quantidade).")
        if erros:
            rejeitados.append({"arquivo": nome, "rotulo": f"Remessa {g['referencia']}", "linha": g["linhas"][0] if g["linhas"] else None,
                               "erro": " ".join(dict.fromkeys(erros))})
            continue
        g["itens"] = len(g["itens_lista"])
        if not g["volumes"]:
            g["volumes"] = 1
        pedidos.append(g)
    return pedidos, rejeitados


def gerar_modelo_planilha_entrada(nome_cliente: str = "") -> bytes:
    import io
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Entradas"
    cab = Font(bold=True, color="FFFFFF")
    fundo_ob, fundo_op = PatternFill("solid", fgColor="0EA575"), PatternFill("solid", fgColor="6B7280")
    for j, (chave, rotulo, obrig, _ap, larg, _ex) in enumerate(COLUNAS_PLANILHA_ENTRADA, start=1):
        c = ws.cell(row=1, column=j, value=rotulo)
        c.font, c.fill = cab, (fundo_ob if obrig else fundo_op)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(j)].width = larg
    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "A2"
    amanha = (date.today() + timedelta(days=1)).strftime("%d/%m/%Y")
    exemplos = [
        {c[0]: c[5] for c in COLUNAS_PLANILHA_ENTRADA} | {"data_prevista": amanha},
        {"referencia": "REM-1001", "sku": "SKU-B", "quantidade": "4", "unidade": "UN"},
        {"referencia": "REM-1002", "data_prevista": amanha, "sku": "NUU001FD", "quantidade": "2", "unidade": "CX", "volumes": "1"},
    ]
    for i, ex in enumerate(exemplos, start=2):
        for j, col in enumerate(COLUNAS_PLANILHA_ENTRADA, start=1):
            v = ex.get(col[0], "")
            if v != "":
                ws.cell(row=i, column=j, value=v).font = Font(italic=True, color="9CA3AF")
    wi = wb.create_sheet("Instruções")
    wi.column_dimensions["A"].width = 110
    texto = [
        f"Modelo Fresh Log de anúncio de mercadoria (Pedidos de Entrada){(' · ' + nome_cliente) if nome_cliente else ''}",
        "",
        "1. Cada LINHA é um ITEM (SKU + quantidade). Linhas com a mesma \"Referência\" formam uma remessa só.",
        "2. Colunas em verde são obrigatórias; em cinza, opcionais. Mantenha os nomes do cabeçalho (a ordem pode mudar).",
        "3. Data prevista de chegada em DD/MM/AAAA, hoje ou futura -- é ela que organiza a fila do galpão.",
        "4. SKU é o código do produto cadastrado na Fresh Log/Stokki. Quantidade maior que zero.",
        "5. Não precisa de endereço nem destinatário: a mercadoria vem sempre pro galpão da Fresh Log.",
        "6. Apague as linhas de exemplo (em cinza) antes de enviar. Limite: 2.000 linhas por arquivo.",
        "",
        "Ao subir a planilha no portal, você vê uma prévia por remessa, confirma, e o recebimento é criado na Stokki automaticamente.",
    ]
    for i, t in enumerate(texto, start=1):
        c = wi.cell(row=i, column=1, value=t)
        if i == 1:
            c.font = Font(bold=True, size=13)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_entradas -v`
Expected: todos PASS.

- [ ] **Step 5: Compilar**

Run: `py -3.11 -m py_compile portal_cliente/entradas.py portal_cliente/test_entradas.py`

---

### Task 4: `entradas.py` — confirmar, listar, cancelar e flag do cliente

**Files:**
- Modify: `portal_cliente/entradas.py` (acrescentar no fim)
- Test: `portal_cliente/test_entradas.py`

**Interfaces:**
- Produces: `confirmar_entradas(conn, cnpj, itens, enviado_por, config) -> list[dict]` (`itens = [{token, data_prevista, observacoes}]`), `listar_entradas(conn, cnpj, dias=30) -> list[dict]`, `resumo_entradas(lista) -> dict`, `buscar_entrada(conn, entrada_id, cnpj) -> dict | None`, `caminho_arquivo(entrada) -> Path`, `cancelar_entrada(conn, entrada, por, cliente, config) -> dict` (`cliente = {cnpj, sender_id, nome}`), `config_entradas_cliente(conn, cnpj) -> {"entradas_ativo": bool}`, `definir_entradas_ativo(conn, cnpj, ativo)`, `entradas_ativas_para(conn, cnpjs) -> bool`.
- Consumes: `ep.guardar_temporario`, `ep.guardar_temporario_pedido`, `ep._caminho_temporario`, `chamados.criar_chamado/mensagem_sistema/conectar` (só em `cancelar_entrada`, com `#PE`).

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar em `portal_cliente/test_entradas.py`:

```python
def _confirmar(self_or_conn, conn=None, itens_extra=None, **kw):
    """Helper: analisa um XML de remessa e confirma pra AMANHA."""
    conn = conn or self_or_conn.conn
    conteudo = xml_remessa(**kw)
    nfe = entradas.ler_nfe_entrada(conteudo, "nota.xml")
    v = entradas.validar_entrada(conn, nfe, CNPJ, {})
    assert v["ok"], v["erros"]
    token = ep.guardar_temporario(conteudo)
    item = {"token": token, "data_prevista": AMANHA, "observacoes": "portão 2"}
    item.update(itens_extra or {})
    return entradas.confirmar_entradas(conn, CNPJ, [item], "cliente", {})


class TestConfirmar(BasePortal):
    def test_grava_entrada_itens_e_move_o_arquivo(self):
        criados = _confirmar(self)
        self.assertEqual(len(criados), 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["status"], e["stokki_status"], e["origem"]), ("ANUNCIADO", "NA_FILA", "xml"))
        self.assertEqual(e["numero_nf"], "41221")
        self.assertEqual(e["data_prevista"], AMANHA)
        self.assertEqual(e["observacoes"], "portão 2")
        self.assertTrue(e["arquivo_path"].endswith(f"{e['chave_nfe']}.xml"))
        self.assertTrue((ep._RAIZ / e["arquivo_path"]).is_file())
        self.assertFalse(list(ep.PASTA_TEMP.glob("*")))
        itens = [dict(r) for r in self.conn.execute("SELECT * FROM portal_entrada_itens ORDER BY linha")]
        self.assertEqual([(i["sku"], i["quantidade"], i["unidade"]) for i in itens], [("NUU001FD", 20.0, "CX")])

    def test_data_prevista_obrigatoria_e_nao_passada(self):
        for ruim in ("", None, (HOJE - timedelta(days=1)).isoformat(), "31/12/2030"):
            with self.assertRaises(ep.ErroEnvio):
                _confirmar(self, itens_extra={"data_prevista": ruim})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM portal_entradas").fetchone()[0], 0)
        _confirmar(self, itens_extra={"data_prevista": HOJE.isoformat()})   # hoje vale

    def test_reanunciar_cancelada_regrava_a_mesma_linha(self):
        _confirmar(self)
        self.conn.execute("UPDATE portal_entradas SET status = 'CANCELADO', stokki_status = 'CRIADO', stokki_id = 9")
        self.conn.execute("INSERT INTO portal_entrada_itens (entrada_id, linha, sku, quantidade) VALUES (1, 99, 'VELHO', 1)")
        self.conn.commit()
        _confirmar(self, itens_extra={"data_prevista": (HOJE + timedelta(days=3)).isoformat()})
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM portal_entradas")]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["stokki_status"], rows[0]["stokki_id"]), ("ANUNCIADO", "NA_FILA", None))
        self.assertEqual(rows[0]["data_prevista"], (HOJE + timedelta(days=3)).isoformat())
        skus = [r[0] for r in self.conn.execute("SELECT sku FROM portal_entrada_itens")]
        self.assertEqual(skus, ["NUU001FD"])

    def test_planilha_confirmada_guarda_a_planilha_uma_vez(self):
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([c[1] for c in entradas.COLUNAS_PLANILHA_ENTRADA])
        d = "/".join(reversed(AMANHA.split("-")))
        ws.append(["REM-1", d, "NUU001FD", 10, "CX", "", 3, "12,5", ""])
        ws.append(["REM-2", d, "SKU-B", 1, "UN", "", "", "", ""])
        buf = io.BytesIO()
        wb.save(buf)
        conteudo = buf.getvalue()
        pedidos, rej = entradas.ler_planilha_entrada(conteudo, "e.xlsx", CNPJ)
        self.assertEqual(rej, [])
        tk_plan = ep.guardar_temporario(conteudo, "xlsx")
        itens = [{"token": ep.guardar_temporario_pedido(p, tk_plan), "data_prevista": p["data_prevista"]} for p in pedidos]
        criados = entradas.confirmar_entradas(self.conn, CNPJ, itens, "equipe:hugo", {})
        self.assertEqual([c["referencia"] for c in criados], ["REM-1", "REM-2"])
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM portal_entradas ORDER BY id")]
        self.assertEqual(rows[0]["arquivo_path"], rows[1]["arquivo_path"])
        self.assertTrue(rows[0]["arquivo_path"].endswith("_e.xlsx"))
        self.assertEqual(rows[0]["enviado_por"], "equipe:hugo")


class TestListar(BasePortal):
    def test_lista_com_rotulos_atrasada_e_pode_cancelar(self):
        _confirmar(self)
        self.conn.execute("UPDATE portal_entradas SET data_prevista = ?", ((HOJE - timedelta(days=2)).isoformat(),))
        self.conn.commit()
        lista = entradas.listar_entradas(self.conn, CNPJ)
        e = lista[0]
        self.assertEqual(e["rotulo"], "NF 41221")
        self.assertEqual(e["status_rotulo"], "Anunciado")
        self.assertTrue(e["atrasada"])
        self.assertTrue(e["pode_cancelar"])
        self.assertEqual(e["data_prevista_br"], (HOJE - timedelta(days=2)).strftime("%d/%m/%Y"))
        self.assertEqual([i["sku"] for i in e["itens_lista"]], ["NUU001FD"])
        self.assertEqual(e["conferencia"], [])   # sem wms_recebimento_id ainda
        self.assertNotIn("arquivo_path", e)
        r = entradas.resumo_entradas(lista)
        self.assertEqual((r["anunciados"], r["atrasados"], r["divergencias"], r["chegaram_hoje"]), (1, 1, 0, 0))

    def test_conferencia_do_galpao_agrupa_por_sku(self):
        # NF com o mesmo SKU em duas linhas (embalagens diferentes): a aba
        # mostra as linhas como vieram e a conferência do galpão por SKU.
        _confirmar(self, itens=[("NUU001FD", "7891234567890", "MINI CX", 20, "CX", 64.77),
                                ("NUU001FD", "17891234567897", "MINI CX (DUN)", 5, "CX", 64.77)])
        self.conn.execute("CREATE TABLE wms_recebimentos (id INTEGER PRIMARY KEY, id_stokki INTEGER, codigo TEXT, estado TEXT, situacao TEXT, "
                          "observacao_divergencia TEXT DEFAULT '', encerrado_em TEXT DEFAULT '')")
        self.conn.execute("CREATE TABLE wms_recebimento_itens (id INTEGER PRIMARY KEY, recebimento_id INTEGER, linha INTEGER, sku TEXT, "
                          "qtd_embalagem REAL, qtd_un REAL, produto_id INTEGER, qtd_enderecada REAL DEFAULT 0, falta_un REAL DEFAULT 0)")
        self.conn.execute("INSERT INTO wms_recebimentos VALUES (7, 2497, '#PE-2497', 'DIVERGENCIA', 'Recebido', 'chegou avariado', '2026-09-25 11:00:00')")
        self.conn.execute("INSERT INTO wms_recebimento_itens VALUES (1, 7, 1, 'NUU001FD', 25, 25, 1, 22, 3)")
        self.conn.execute("UPDATE portal_entradas SET wms_recebimento_id = 7, status = 'DIVERGENCIA', stokki_id = 2497, stokki_codigo = '#PE-2497'")
        self.conn.commit()
        e = entradas.listar_entradas(self.conn, CNPJ)[0]
        self.assertEqual(len(e["itens_lista"]), 2)
        self.assertEqual(e["conferencia"], [{"sku": "NUU001FD", "descricao": "MINI CX PAO DE QUEIJO", "unidade": "UN",
                                             "anunciado": 25.0, "recebido": 22.0, "falta": 3.0}])
        self.assertEqual(e["observacao_divergencia"], "chegou avariado")
        self.assertFalse(e["pode_cancelar"])

    def test_janela_de_30_dias_mas_abertas_sempre_aparecem(self):
        _confirmar(self)
        self.conn.execute("UPDATE portal_entradas SET criado_em = '2026-01-01 10:00:00'")
        self.conn.commit()
        self.assertEqual(len(entradas.listar_entradas(self.conn, CNPJ)), 1)     # ANUNCIADO: aparece
        self.conn.execute("UPDATE portal_entradas SET status = 'ENDERECADO'")
        self.conn.commit()
        self.assertEqual(entradas.listar_entradas(self.conn, CNPJ), [])         # fechada e velha: some


class TestCancelar(BasePortal):
    def _cliente(self):
        return {"cnpj": CNPJ, "sender_id": 1, "nome": "CLIENTE TESTE"}

    def test_cancelar_anunciada_sem_pe(self):
        _confirmar(self)
        e = entradas.buscar_entrada(self.conn, 1, CNPJ)
        r = entradas.cancelar_entrada(self.conn, e, "cliente", self._cliente(), {})
        self.assertTrue(r["aplicado"])
        self.assertIsNone(r["chamado_id"])
        e = entradas.buscar_entrada(self.conn, 1, CNPJ)
        self.assertEqual(e["status"], "CANCELADO")
        self.assertEqual(e["cancelado_por"], "cliente")

    def test_cancelar_com_pe_abre_chamado(self):
        _confirmar(self)
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'CRIADO', stokki_id = 2497, stokki_codigo = '#PE-2497'")
        self.conn.commit()
        chamado_falso = mock.Mock()
        chamado_falso.criar_chamado.return_value = {"id": 55}
        chamado_falso.conectar.return_value = mock.MagicMock()
        with mock.patch.object(entradas, "_chamados", return_value=chamado_falso):
            r = entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, 1, CNPJ), "cliente", self._cliente(), {})
        self.assertTrue(r["aplicado"])
        self.assertEqual(r["chamado_id"], 55)
        self.assertIn("#PE-2497", chamado_falso.criar_chamado.call_args.kwargs["assunto"])
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["chamado_id"], 55)

    def test_cancelar_tira_do_wms_so_se_esperado_sem_enderecamento(self):
        _confirmar(self)
        self.conn.execute("CREATE TABLE wms_recebimentos (id INTEGER PRIMARY KEY, id_stokki INTEGER, codigo TEXT, estado TEXT, atualizado_em TEXT)")
        self.conn.execute("CREATE TABLE wms_recebimento_itens (id INTEGER PRIMARY KEY, recebimento_id INTEGER, linha INTEGER, sku TEXT, "
                          "qtd_embalagem REAL, qtd_un REAL, produto_id INTEGER, qtd_enderecada REAL DEFAULT 0, falta_un REAL DEFAULT 0)")
        self.conn.execute("INSERT INTO wms_recebimentos VALUES (7, 2497, '#PE-2497', 'ESPERADO', '')")
        self.conn.execute("INSERT INTO wms_recebimento_itens VALUES (1, 7, 1, 'NUU001FD', 20, 20, 1, 0, 0)")
        self.conn.execute("UPDATE portal_entradas SET wms_recebimento_id = 7")
        self.conn.commit()
        entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, 1, CNPJ), "cliente", self._cliente(), {})
        self.assertEqual(self.conn.execute("SELECT estado FROM wms_recebimentos WHERE id = 7").fetchone()[0], "CANCELADO")

    def test_cancelar_fora_de_anunciado_e_recusado(self):
        _confirmar(self)
        for st in ("CHEGOU", "ENDERECADO", "DIVERGENCIA", "CANCELADO"):
            self.conn.execute("UPDATE portal_entradas SET status = ?", (st,))
            self.conn.commit()
            with self.assertRaises(ep.ErroEnvio):
                entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, 1, CNPJ), "cliente", self._cliente(), {})

    def test_cancelar_enquanto_worker_envia_e_recusado(self):
        _confirmar(self)
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'ENVIANDO'")
        self.conn.commit()
        with self.assertRaises(ep.ErroEnvio):
            entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, 1, CNPJ), "cliente", self._cliente(), {})


class TestFlagCliente(BasePortal):
    def test_nasce_desligada_e_liga_por_cnpj(self):
        self.assertFalse(entradas.config_entradas_cliente(self.conn, CNPJ)["entradas_ativo"])
        entradas.definir_entradas_ativo(self.conn, CNPJ, True)
        self.assertTrue(entradas.config_entradas_cliente(self.conn, CNPJ)["entradas_ativo"])
        self.assertTrue(entradas.entradas_ativas_para(self.conn, [OUTRO, CNPJ]))
        self.assertFalse(entradas.entradas_ativas_para(self.conn, [OUTRO]))
        entradas.definir_entradas_ativo(self.conn, CNPJ, False)
        self.assertFalse(entradas.config_entradas_cliente(self.conn, CNPJ)["entradas_ativo"])
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_entradas -v`
Expected: `AttributeError: module 'entradas' has no attribute 'confirmar_entradas'`.

- [ ] **Step 3: Implementar**

Acrescentar no fim de `portal_cliente/entradas.py`:

```python
# ── Confirmação ────────────────────────────────────────────────────────────────

def _data_prevista_valida(valor) -> str:
    try:
        d = date.fromisoformat(str(valor or ""))
    except ValueError:
        raise ErroEnvio("Informe a data prevista de chegada (AAAA-MM-DD).")
    if d < date.today():
        raise ErroEnvio(f"A data prevista {d.strftime('%d/%m/%Y')} já passou -- precisa ser hoje ou depois.")
    return d.isoformat()


def _caminho_definitivo_xml(cnpj: str, chave: str) -> Path:
    pasta = _pasta_entradas() / _so_digitos(cnpj)
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta / f"{chave}.xml"


def _caminho_definitivo_planilha(cnpj: str, nome_original: str) -> Path:
    import secrets
    pasta = _pasta_entradas() / _so_digitos(cnpj) / "planilhas"
    pasta.mkdir(parents=True, exist_ok=True)
    seguro = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(nome_original or "planilha.xlsx").name)[:80] or "planilha.xlsx"
    return pasta / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}_{seguro}"


def confirmar_entradas(conn: sqlite3.Connection, cnpj_embarcador: str, itens: list[dict], enviado_por: str,
                       config: dict | None) -> list[dict]:
    """Grava as entradas ANUNCIADO / NA_FILA a partir dos temporários da
    prévia. Revalida tudo (o catálogo pode ter mudado entre analisar e
    confirmar). Uma entrada que já existia CANCELADO é regravada na mesma
    linha (UPDATE), com os itens antigos apagados. Commit por entrada."""
    emb = _so_digitos(cnpj_embarcador)
    criados = []
    planilhas_movidas: dict[str, Path] = {}
    for item in itens:
        caminho = ep._caminho_temporario(item.get("token", ""))
        conteudo = caminho.read_bytes()
        if caminho.suffix == ".json":
            nfe = json.loads(conteudo.decode("utf-8"))
            if nfe.get("origem") != ORIGEM_PLANILHA or _so_digitos(nfe.get("emitente_cnpj")) != emb:
                raise ErroEnvio("Remessa temporária inválida -- envie a planilha de novo.")
        else:
            nfe = ler_nfe_entrada(conteudo)
        v = validar_entrada(conn, nfe, emb, config)
        if not v["ok"]:
            raise ErroEnvio(f"{rotulo_entrada(nfe)}: " + " ".join(v["erros"]))
        data_prevista = _data_prevista_valida(item.get("data_prevista") or nfe.get("data_prevista"))

        planilha = nfe.get("origem") == ORIGEM_PLANILHA
        if planilha:
            tk = nfe.get("arquivo_token", "")
            destino = planilhas_movidas.get(tk)
            if destino is None:
                origem_plan = ep._caminho_temporario(tk)
                destino = _caminho_definitivo_planilha(emb, nfe.get("nome_arquivo") or origem_plan.name)
                destino.write_bytes(origem_plan.read_bytes())
                planilhas_movidas[tk] = destino
                try:
                    origem_plan.unlink()
                except OSError:
                    pass
        else:
            destino = _caminho_definitivo_xml(emb, nfe["chave_nfe"])
            destino.write_bytes(conteudo)
        try:
            caminho.unlink()
        except OSError:
            pass

        agora = _agora()
        campos = {
            "cnpj_embarcador": emb, "origem": nfe["origem"], "chave_nfe": nfe["chave_nfe"],
            "numero_nf": nfe.get("numero_nf") or None, "serie": nfe.get("serie") or None,
            "emitida_em": nfe.get("emitida_em") or None, "referencia": nfe.get("referencia") or None,
            "data_prevista": data_prevista, "volumes": nfe.get("volumes") or 1, "peso_kg": nfe.get("peso_kg") or 0,
            "valor_nf": nfe.get("valor_nf") or 0, "arquivo_path": str(destino.relative_to(ep._RAIZ)),
            "status": STATUS_ANUNCIADO, "stokki_status": STOKKI_NA_FILA, "stokki_id": None, "stokki_codigo": None,
            "stokki_erro": None, "stokki_tentativas": 0, "wms_recebimento_id": None,
            "observacoes": (item.get("observacoes") or nfe.get("observacoes") or "")[:500],
            "enviado_por": enviado_por, "criado_em": agora, "atualizado_em": agora,
            "cancelado_em": None, "cancelado_por": None, "chamado_id": None,
        }
        existente = v["existente"]
        if existente:
            sets = ", ".join(f"{k} = ?" for k in campos if k != "chave_nfe")
            conn.execute(f"UPDATE portal_entradas SET {sets} WHERE id = ?",
                         (*[x for k, x in campos.items() if k != "chave_nfe"], existente["id"]))
            entrada_id = existente["id"]
            conn.execute("DELETE FROM portal_entrada_itens WHERE entrada_id = ?", (entrada_id,))
        else:
            cols = ", ".join(campos)
            conn.execute(f"INSERT INTO portal_entradas ({cols}) VALUES ({','.join('?' * len(campos))})", tuple(campos.values()))
            entrada_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for it in nfe.get("itens_lista") or []:
            conn.execute("INSERT INTO portal_entrada_itens (entrada_id, linha, sku, ean, descricao, quantidade, unidade) VALUES (?,?,?,?,?,?,?)",
                         (entrada_id, int(it["linha"]), str(it["sku"])[:60], str(it.get("ean") or "")[:20],
                          str(it.get("descricao") or "")[:200], float(it["quantidade"]), str(it.get("unidade") or "")[:10]))
        conn.commit()
        criados.append({"id": entrada_id, "numero_nf": nfe.get("numero_nf"), "referencia": nfe.get("referencia"),
                        "origem": nfe["origem"], "data_prevista": data_prevista, "status": STATUS_ANUNCIADO})
    return criados


# ── Listagem ───────────────────────────────────────────────────────────────────

def _tem_tabela(conn, nome: str) -> bool:
    return conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (nome,)).fetchone() is not None


def _conferencia_do_galpao(conn, recebimento_id: int | None) -> list[dict]:
    """Anunciado × recebido × falta POR SKU, em UN, do wms_recebimento_itens
    ligado. Vazio sem ligação ou sem as tabelas do WMS (banco só do portal)."""
    if not recebimento_id or not _tem_tabela(conn, "wms_recebimento_itens"):
        return []
    if _tem_tabela(conn, "wms_produtos"):
        sql = ("SELECT i.sku, COALESCE(p.descricao, '') AS descricao, COALESCE(p.unidade, 'UN') AS unidade, "
               "SUM(COALESCE(i.qtd_un, 0)) AS anunciado, SUM(COALESCE(i.qtd_enderecada, 0)) AS recebido, "
               "SUM(COALESCE(i.falta_un, 0)) AS falta FROM wms_recebimento_itens i LEFT JOIN wms_produtos p ON p.id = i.produto_id "
               "WHERE i.recebimento_id = ? GROUP BY i.sku ORDER BY MIN(i.linha)")
    else:
        sql = ("SELECT i.sku, '' AS descricao, 'UN' AS unidade, SUM(COALESCE(i.qtd_un, 0)) AS anunciado, "
               "SUM(COALESCE(i.qtd_enderecada, 0)) AS recebido, SUM(COALESCE(i.falta_un, 0)) AS falta "
               "FROM wms_recebimento_itens i WHERE i.recebimento_id = ? GROUP BY i.sku ORDER BY MIN(i.linha)")
    saida = []
    for r in conn.execute(sql, (int(recebimento_id),)):
        saida.append({"sku": r["sku"], "descricao": r["descricao"], "unidade": r["unidade"],
                      "anunciado": round(float(r["anunciado"]), 3), "recebido": round(float(r["recebido"]), 3),
                      "falta": round(float(r["falta"]), 3)})
    return saida


def _linha(conn, r: sqlite3.Row, itens: dict[int, list[dict]]) -> dict:
    d = dict(r)
    hoje = date.today().isoformat()
    rec = None
    if d.get("wms_recebimento_id") and _tem_tabela(conn, "wms_recebimentos"):
        rec = conn.execute("SELECT * FROM wms_recebimentos WHERE id = ?", (d["wms_recebimento_id"],)).fetchone()
    d.update({
        "rotulo": rotulo_entrada(d),
        "status_rotulo": ROTULOS_STATUS.get(d["status"], d["status"]),
        "stokki_rotulo": ROTULOS_STOKKI.get(d["stokki_status"], d["stokki_status"]),
        "criado_em_br": _quando_br(d["criado_em"]),
        "data_prevista_br": "/".join(reversed(d["data_prevista"].split("-"))) if d.get("data_prevista") else "",
        "atrasada": d["status"] == STATUS_ANUNCIADO and bool(d.get("data_prevista")) and d["data_prevista"] < hoje,
        "itens_lista": itens.get(d["id"], []),
        "conferencia": _conferencia_do_galpao(conn, d.get("wms_recebimento_id")),
        "observacao_divergencia": (rec["observacao_divergencia"] if rec is not None and "observacao_divergencia" in rec.keys() else "") or "",
        "encerrado_em_br": _quando_br(rec["encerrado_em"]) if rec is not None and "encerrado_em" in rec.keys() else "",
        "pode_cancelar": d["status"] == STATUS_ANUNCIADO and d["stokki_status"] != STOKKI_ENVIANDO,
        "arquivo_ext": Path(d.get("arquivo_path") or "").suffix.lstrip(".").lower() or "xml",
    })
    d.pop("arquivo_path", None)
    return d


def listar_entradas(conn: sqlite3.Connection, cnpj_embarcador: str, dias: int = DIAS_LISTAGEM) -> list[dict]:
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d 00:00:00")
    emb = _so_digitos(cnpj_embarcador)
    rows = conn.execute("SELECT * FROM portal_entradas WHERE cnpj_embarcador = ? AND (criado_em >= ? OR status IN ('ANUNCIADO','CHEGOU')) "
                        "ORDER BY data_prevista, criado_em DESC, id DESC", (emb, desde)).fetchall()
    itens: dict[int, list[dict]] = {}
    if rows:
        ids = [r["id"] for r in rows]
        for it in conn.execute(f"SELECT * FROM portal_entrada_itens WHERE entrada_id IN ({','.join('?' * len(ids))}) ORDER BY entrada_id, linha", ids):
            itens.setdefault(it["entrada_id"], []).append({k: it[k] for k in ("linha", "sku", "ean", "descricao", "quantidade", "unidade")})
    return [_linha(conn, r, itens) for r in rows]


def resumo_entradas(lista: list[dict]) -> dict:
    hoje = date.today().strftime("%d/%m/%Y")
    return {
        "total": len(lista),
        "anunciados": sum(1 for e in lista if e["status"] == STATUS_ANUNCIADO),
        "atrasados": sum(1 for e in lista if e["atrasada"]),
        # "Chegaram": tudo que já está no galpão (conferindo, endereçado ou com
        # divergência) -- o nome da chave é histórico do desenho, o tile diz "no galpão"
        "chegaram_hoje": sum(1 for e in lista if e["status"] in (STATUS_CHEGOU, STATUS_ENDERECADO, STATUS_DIVERGENCIA)),
        "divergencias": sum(1 for e in lista if e["status"] == STATUS_DIVERGENCIA),
        "erros_stokki": sum(1 for e in lista if e["stokki_status"] == STOKKI_ERRO and e["status"] == STATUS_ANUNCIADO),
    }


def buscar_entrada(conn: sqlite3.Connection, entrada_id: int, cnpj_embarcador: str) -> dict | None:
    r = conn.execute("SELECT * FROM portal_entradas WHERE id = ? AND cnpj_embarcador = ?",
                     (entrada_id, _so_digitos(cnpj_embarcador))).fetchone()
    return dict(r) if r else None


def caminho_arquivo(entrada: dict) -> Path:
    return ep._RAIZ / entrada["arquivo_path"]


# ── Cancelar ───────────────────────────────────────────────────────────────────

def _chamados():
    import chamados as ch
    return ch


def _abrir_chamado_cancelamento(entrada: dict, cliente: dict, por: str) -> int | None:
    """Com #PE já criado, a Stokki não é cancelada sozinha (spec D5): abre
    chamado pra equipe, o mesmo desenho de bloqueio_area.abrir_bloqueio.
    Falha do chat não desfaz o cancelamento no portal."""
    try:
        ch = _chamados()
        conn_ch = ch.conectar()
        try:
            chamado = ch.criar_chamado(conn_ch, cliente, ch.ORIGEM_SISTEMA, ch.STATUS_AGUARDANDO_FL,
                                       assunto=f"Cancelar recebimento {entrada.get('stokki_codigo') or entrada.get('stokki_id')} na Stokki",
                                       area="entradas", pedido_ref=rotulo_entrada(entrada))
            quem = "pela equipe Fresh Log" if str(por).startswith("equipe") else "pelo cliente"
            ch.mensagem_sistema(conn_ch, chamado,
                                f"{rotulo_entrada(entrada)} foi cancelada {quem} na aba Pedidos de Entrada. O recebimento "
                                f"{entrada.get('stokki_codigo') or ''} já existe na Stokki e precisa ser cancelado lá à mão.")
            return chamado["id"]
        finally:
            conn_ch.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"entradas: não abriu chamado do cancelamento da entrada {entrada.get('id')}: {e}")
        return None


def cancelar_entrada(conn: sqlite3.Connection, entrada: dict, por: str, cliente: dict, config: dict | None) -> dict:
    """{aplicado, mensagem, chamado_id}. Só a partir de ANUNCIADO e nunca
    enquanto o worker está criando o #PE."""
    if entrada["status"] != STATUS_ANUNCIADO:
        raise ErroEnvio("Só uma entrada ainda anunciada (que não chegou) pode ser cancelada.")
    if entrada["stokki_status"] == STOKKI_ENVIANDO:
        raise ErroEnvio("Essa entrada está sendo criada na Stokki agora -- tente de novo em alguns instantes.")
    agora = _agora()
    conn.execute("UPDATE portal_entradas SET status = ?, cancelado_em = ?, cancelado_por = ?, atualizado_em = ? WHERE id = ?",
                 (STATUS_CANCELADO, agora, por, agora, entrada["id"]))
    if entrada.get("wms_recebimento_id") and _tem_tabela(conn, "wms_recebimentos"):
        conn.execute("UPDATE wms_recebimentos SET estado = 'CANCELADO', atualizado_em = ? WHERE id = ? AND estado = 'ESPERADO' "
                     "AND NOT EXISTS (SELECT 1 FROM wms_recebimento_itens i WHERE i.recebimento_id = wms_recebimentos.id AND i.qtd_enderecada > 0)",
                     (agora, entrada["wms_recebimento_id"]))
    conn.commit()
    chamado_id = None
    if entrada.get("stokki_id"):
        chamado_id = _abrir_chamado_cancelamento(entrada, cliente, por)
        if chamado_id:
            conn.execute("UPDATE portal_entradas SET chamado_id = ? WHERE id = ?", (chamado_id, entrada["id"]))
            conn.commit()
        mensagem = ("Entrada cancelada. O recebimento já existia na Stokki: a Fresh Log vai cancelá-lo lá e você acompanha pelo chat."
                    if chamado_id else "Entrada cancelada. O recebimento já existia na Stokki -- avise a Fresh Log pelo chat pra cancelar lá.")
    else:
        mensagem = "Entrada cancelada -- não será criada na Stokki."
    return {"aplicado": True, "mensagem": mensagem, "chamado_id": chamado_id}


# ── Flag por cliente (D3: só o piloto) ─────────────────────────────────────────

def config_entradas_cliente(conn: sqlite3.Connection, cnpj_embarcador: str) -> dict:
    r = conn.execute("SELECT entradas_ativo FROM portal_clientes_envio WHERE cnpj = ?", (_so_digitos(cnpj_embarcador),)).fetchone()
    return {"entradas_ativo": bool(r["entradas_ativo"]) if r else False}


def definir_entradas_ativo(conn: sqlite3.Connection, cnpj_embarcador: str, ativo: bool) -> None:
    emb = _so_digitos(cnpj_embarcador)
    if not conn.execute("SELECT 1 FROM portal_clientes_envio WHERE cnpj = ?", (emb,)).fetchone():
        ep.definir_parametros_cliente(conn, emb)   # cria a linha com os padrões de Envios
    conn.execute("UPDATE portal_clientes_envio SET entradas_ativo = ?, atualizado_em = ? WHERE cnpj = ?", (int(bool(ativo)), _agora(), emb))
    conn.commit()


def entradas_ativas_para(conn: sqlite3.Connection, cnpjs: list[str]) -> bool:
    return any(config_entradas_cliente(conn, c)["entradas_ativo"] for c in cnpjs or [])
```

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_entradas -v`
Expected: todos PASS. Se `definir_parametros_cliente(conn, emb)` sem campos falhar, confira em `envio_pedidos.py:1068` que ele aceita `**campos` vazio (ele monta a linha com os padrões e faz `INSERT ... ON CONFLICT`); se não aceitar, chame `ep.definir_parametros_cliente(conn, emb, envio_ativo=1)`.

- [ ] **Step 5: Compilar**

Run: `py -3.11 -m py_compile portal_cliente/entradas.py portal_cliente/test_entradas.py`

---

### Task 5: ligação com o WMS — amarração, `sincronizar_status` e CANCELADO

**Files:**
- Modify: `portal_cliente/entradas.py` (acrescentar no fim)
- Modify: `painel_agentes/wms_pedidos.py:126-136` (`_COLUNAS_NOVAS`)
- Modify: `sincronizar_recebimentos_wms.py:108-188` (`rodar`)
- Modify: `painel_agentes/painel_agentes.py:2984-2992` (rota `encerrar-divergencia`) e `:2866-2885` (`/api/wms/recebimentos`)
- Modify: `painel_agentes/templates/wms.html:664-668` (card da aba Receber)
- Test: `portal_cliente/test_entradas.py`, `test_sincronizar_recebimentos_wms.py`

**Interfaces:**
- Produces: `entradas.amarrar_recebimento(conn, recebimento_id, id_stokki, codigo, stkkc_id, chave_nfe, ref_pedido) -> int | None` (id da entrada amarrada) e `entradas.sincronizar_status(conn) -> int` (quantas mudaram). Colunas novas `wms_recebimentos.portal_entrada_id INTEGER` e `wms_recebimentos.data_prevista TEXT NOT NULL DEFAULT ''`. Estado novo `CANCELADO` em `wms_recebimentos.estado`.
- Consumes: `stokki_recebimentos.ler_detalhe` (Tarefa 1), `wms_pedidos.registrar_recebimento` (inalterada).

- [ ] **Step 1: Escrever os testes que falham (portal)**

Acrescentar em `portal_cliente/test_entradas.py`:

```python
class TestAmarracaoEStatus(BasePortal):
    """As tabelas do WMS aqui são um recorte mínimo (o teste do timer, em
    test_sincronizar_recebimentos_wms.py, usa as reais)."""

    def setUp(self):
        super().setUp()
        self.conn.execute("CREATE TABLE wms_recebimentos (id INTEGER PRIMARY KEY, id_stokki INTEGER, codigo TEXT, stkkc_id TEXT, "
                          "situacao TEXT DEFAULT '', estado TEXT DEFAULT 'ESPERADO', observacao_divergencia TEXT DEFAULT '', "
                          "encerrado_em TEXT DEFAULT '', portal_entrada_id INTEGER, data_prevista TEXT DEFAULT '', atualizado_em TEXT DEFAULT '')")
        self.conn.execute("CREATE TABLE wms_recebimento_itens (id INTEGER PRIMARY KEY, recebimento_id INTEGER, linha INTEGER, sku TEXT, "
                          "qtd_embalagem REAL, qtd_un REAL, produto_id INTEGER, qtd_enderecada REAL DEFAULT 0, falta_un REAL DEFAULT 0)")
        self.conn.commit()

    def _rec(self, rid=7, id_stokki=2497, estado="ESPERADO", situacao="Em transito", enderecada=0):
        self.conn.execute("INSERT INTO wms_recebimentos (id, id_stokki, codigo, stkkc_id, situacao, estado) VALUES (?,?,?,?,?,?)",
                          (rid, id_stokki, f"#PE-{id_stokki}", "48", situacao, estado))
        self.conn.execute("INSERT INTO wms_recebimento_itens (recebimento_id, linha, sku, qtd_embalagem, qtd_un, produto_id, qtd_enderecada) "
                          "VALUES (?, 1, 'NUU001FD', 20, 20, 1, ?)", (rid, enderecada))
        self.conn.commit()

    def test_amarra_por_stokki_id(self):
        _confirmar(self)
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'CRIADO', stokki_id = 2497")
        self.conn.commit()
        self._rec()
        eid = entradas.amarrar_recebimento(self.conn, 7, 2497, "#PE-2497", "48", chave_nfe="", ref_pedido="")
        self.assertEqual(eid, 1)
        rec = dict(self.conn.execute("SELECT * FROM wms_recebimentos WHERE id = 7").fetchone())
        self.assertEqual((rec["portal_entrada_id"], rec["data_prevista"]), (1, AMANHA))
        e = entradas.buscar_entrada(self.conn, 1, CNPJ)
        self.assertEqual((e["wms_recebimento_id"], e["stokki_codigo"]), (7, "#PE-2497"))

    def test_amarra_pela_chave_nfe_quando_o_pe_foi_criado_a_mao(self):
        criados = _confirmar(self)
        chave = entradas.buscar_entrada(self.conn, criados[0]["id"], CNPJ)["chave_nfe"]
        self._rec()
        eid = entradas.amarrar_recebimento(self.conn, 7, 2497, "#PE-2497", "48", chave_nfe=chave, ref_pedido="41221")
        self.assertEqual(eid, 1)
        e = entradas.buscar_entrada(self.conn, 1, CNPJ)
        self.assertEqual((e["stokki_id"], e["stokki_codigo"], e["stokki_status"]), (2497, "#PE-2497", "CRIADO"))

    def test_amarra_planilha_pela_referencia_do_mesmo_embarcador(self):
        entradas.garantir_tabelas(self.conn)
        self.conn.execute("INSERT INTO portal_entradas (cnpj_embarcador, origem, chave_nfe, referencia, data_prevista, arquivo_path, status, "
                          "criado_em, atualizado_em) VALUES (?, 'planilha', 'PLANILHA-ENTRADA-x', 'REM-9', ?, 'x', 'ANUNCIADO', ?, ?)",
                          (CNPJ, AMANHA, _agora_txt(), _agora_txt()))
        self.conn.execute("INSERT INTO portal_entradas (cnpj_embarcador, origem, chave_nfe, referencia, data_prevista, arquivo_path, status, "
                          "criado_em, atualizado_em) VALUES (?, 'planilha', 'PLANILHA-ENTRADA-y', 'REM-9', ?, 'x', 'ANUNCIADO', ?, ?)",
                          (OUTRO, AMANHA, _agora_txt(), _agora_txt()))
        self.conn.commit()
        self._rec()
        eid = entradas.amarrar_recebimento(self.conn, 7, 2497, "#PE-2497", "48", chave_nfe="", ref_pedido="REM-9")
        self.assertEqual(eid, 1)   # a do cliente 48, não a do OUTRO (stkkc 77)

    def test_sem_par_nao_amarra_e_nao_inventa(self):
        _confirmar(self)
        self._rec()
        self.assertIsNone(entradas.amarrar_recebimento(self.conn, 7, 2497, "#PE-2497", "48", chave_nfe="0" * 44, ref_pedido="999"))
        self.assertIsNone(entradas.buscar_entrada(self.conn, 1, CNPJ)["wms_recebimento_id"])

    def test_sincronizar_status_anda_so_pra_frente(self):
        _confirmar(self)
        self._rec(situacao="Em transito")
        self.conn.execute("UPDATE portal_entradas SET wms_recebimento_id = 7, stokki_id = 2497")
        self.conn.commit()
        self.assertEqual(entradas.sincronizar_status(self.conn), 0)
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "ANUNCIADO")
        self.conn.execute("UPDATE wms_recebimentos SET situacao = 'Recebido'")
        self.conn.commit()
        self.assertEqual(entradas.sincronizar_status(self.conn), 1)
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "CHEGOU")
        self.conn.execute("UPDATE wms_recebimentos SET estado = 'ENDERECADO'")
        self.conn.commit()
        entradas.sincronizar_status(self.conn)
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "ENDERECADO")
        self.conn.execute("UPDATE wms_recebimentos SET estado = 'ESPERADO', situacao = 'Em transito'")
        self.conn.commit()
        entradas.sincronizar_status(self.conn)
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "ENDERECADO")   # nunca volta

    def test_primeiro_enderecamento_tambem_vira_chegou(self):
        _confirmar(self)
        self._rec(situacao="Em transito", enderecada=5)
        self.conn.execute("UPDATE portal_entradas SET wms_recebimento_id = 7")
        self.conn.commit()
        entradas.sincronizar_status(self.conn)
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "CHEGOU")

    def test_divergencia_e_cancelado(self):
        _confirmar(self)
        self._rec(estado="DIVERGENCIA", situacao="Recebido")
        self.conn.execute("UPDATE portal_entradas SET wms_recebimento_id = 7")
        self.conn.commit()
        entradas.sincronizar_status(self.conn)
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "DIVERGENCIA")
        self.conn.execute("UPDATE portal_entradas SET status = 'CANCELADO'")
        self.conn.execute("UPDATE wms_recebimentos SET estado = 'ENDERECADO'")
        self.conn.commit()
        entradas.sincronizar_status(self.conn)
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "CANCELADO")   # CANCELADO nunca reabre
```

E, no topo do arquivo de teste, o helper:

```python
def _agora_txt():
    return ep._agora()
```

- [ ] **Step 2: Escrever os testes que falham (timer)**

Acrescentar em `test_sincronizar_recebimentos_wms.py` (dentro de `TestRodar`), e no `_HTML_ITENS` do topo do arquivo incluir antes das tabelas as duas tabelas de cabeçalho (copiar da fixture da Tarefa 1, com a chave `35260912345678000195550010000412211000000017` e "Ref. do Pedido" `41221`):

```python
    def test_amarra_entrada_do_portal_e_propaga_status(self):
        import entradas
        entradas.garantir_tabelas(self.conn)
        self.conn.execute("CREATE TABLE IF NOT EXISTS interno (cnpj_embarcador TEXT, stkkc_id INTEGER)")
        self.conn.execute("INSERT INTO interno VALUES ('12345678000195', 48)")
        self.conn.execute("INSERT INTO portal_entradas (cnpj_embarcador, origem, chave_nfe, numero_nf, data_prevista, arquivo_path, status, "
                          "stokki_status, criado_em, atualizado_em) VALUES ('12345678000195', 'xml', "
                          "'35260912345678000195550010000412211000000017', '41221', '2026-09-26', 'x', 'ANUNCIADO', 'CRIADO', "
                          "'2026-09-25 10:00:00', '2026-09-25 10:00:00')")
        self.conn.commit()
        sess = SessaoFalsa([_linha(2497, PILOTO_ID, situacao="Recebido")], {"2497": _HTML_ITENS})

        res = mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)

        self.assertEqual(res["gravados"], 1)
        self.assertEqual(res["amarrados"], 1)
        rec = self.conn.execute("SELECT * FROM wms_recebimentos WHERE id_stokki = 2497").fetchone()
        self.assertEqual(rec["portal_entrada_id"], 1)
        self.assertEqual(rec["data_prevista"], "2026-09-26")
        e = self.conn.execute("SELECT * FROM portal_entradas WHERE id = 1").fetchone()
        self.assertEqual((e["wms_recebimento_id"], e["stokki_id"], e["stokki_codigo"], e["status"]), (rec["id"], 2497, "#PE-2497", "CHEGOU"))
        # só UM GET no detalhe: itens, chave e ref vêm do mesmo HTML
        self.assertEqual(sum(1 for url, _ in sess.chamadas if "/show/" in url), 1)

    def test_recebimento_cancelado_nao_e_relido(self):
        linhas = [_linha(2489, PILOTO_ID)]
        sess = SessaoFalsa(linhas, {"2489": _HTML_ITENS})
        mod.rodar(self.conn, sess, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        self.conn.execute("UPDATE wms_recebimentos SET estado = 'CANCELADO' WHERE id_stokki = 2489")
        self.conn.commit()
        sess2 = SessaoFalsa(linhas, {"2489": _HTML_ITENS})
        res = mod.rodar(self.conn, sess2, PILOTO_NOME, PILOTO_ID, limite=50, modo_teste=False)
        self.assertEqual(res["ja_fechados"], 1)
        self.assertFalse(any("/show/" in url for url, _ in sess2.chamadas))
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_entradas test_sincronizar_recebimentos_wms -v`
Expected: `AttributeError: module 'entradas' has no attribute 'amarrar_recebimento'`; no timer, `KeyError: 'amarrados'`.

- [ ] **Step 4: Implementar em `entradas.py`**

Acrescentar no fim de `portal_cliente/entradas.py`:

```python
# ── Ligação com o WMS ──────────────────────────────────────────────────────────
# Quem chama: sincronizar_recebimentos_wms.rodar() (a cada 30 min, logo
# depois de registrar_recebimento) e a rota de encerrar-divergência do
# painel. As duas abrem a conexão delas (wms_pedidos.conectar) no mesmo
# dados.db; garantir_tabelas() cobre banco onde a aba nunca rodou.

def _cnpj_do_stkkc(conn, stkkc_id: str) -> str:
    if not stkkc_id or not _tem_tabela(conn, "interno"):
        return ""
    r = conn.execute("SELECT cnpj_embarcador FROM interno WHERE stkkc_id = ?", (int(stkkc_id),)).fetchone()
    return _so_digitos(r["cnpj_embarcador"]) if r else ""


def amarrar_recebimento(conn: sqlite3.Connection, recebimento_id: int, id_stokki: int, codigo: str, stkkc_id: str,
                        chave_nfe: str = "", ref_pedido: str = "") -> int | None:
    """Liga um wms_recebimentos recém-registrado à entrada do portal:
    1) por stokki_id (o worker já sabia o #PE); 2) pela chave NF-e do
    detalhe (XML, exata); 3) pela referência = Ref. do Pedido, do mesmo
    embarcador (planilha). Sem par: devolve None e não inventa nada."""
    garantir_tabelas(conn)
    e = conn.execute("SELECT id, data_prevista FROM portal_entradas WHERE stokki_id = ? AND status IN ('ANUNCIADO','CHEGOU')",
                     (int(id_stokki),)).fetchone()
    if e is None and chave_nfe:
        e = conn.execute("SELECT id, data_prevista FROM portal_entradas WHERE chave_nfe = ? AND status IN ('ANUNCIADO','CHEGOU') "
                         "AND wms_recebimento_id IS NULL", (chave_nfe,)).fetchone()
    if e is None and ref_pedido:
        cnpj = _cnpj_do_stkkc(conn, stkkc_id)
        if cnpj:
            rows = conn.execute("SELECT id, data_prevista FROM portal_entradas WHERE cnpj_embarcador = ? AND origem = 'planilha' "
                                "AND referencia = ? AND status IN ('ANUNCIADO','CHEGOU') AND wms_recebimento_id IS NULL",
                                (cnpj, str(ref_pedido).strip())).fetchall()
            if len(rows) == 1:
                e = rows[0]
            elif len(rows) > 1:
                logger.warning(f"entradas: {len(rows)} remessas com referência {ref_pedido!r} do embarcador {cnpj} -- não amarrei o #PE {id_stokki}")
    if e is None:
        return None
    agora = _agora()
    conn.execute("UPDATE wms_recebimentos SET portal_entrada_id = ?, data_prevista = ?, atualizado_em = ? WHERE id = ?",
                 (e["id"], e["data_prevista"] or "", agora, int(recebimento_id)))
    # O #PE existe na Stokki (o timer acabou de ler), entao a fila esta
    # resolvida mesmo se o worker ainda nao tinha gravado (resposta perdida).
    conn.execute("UPDATE portal_entradas SET wms_recebimento_id = ?, stokki_id = ?, stokki_codigo = ?, "
                 "stokki_status = 'CRIADO', stokki_erro = NULL, atualizado_em = ? WHERE id = ?",
                 (int(recebimento_id), int(id_stokki), codigo or f"#PE-{id_stokki}", agora, e["id"]))
    conn.commit()
    return e["id"]


def sincronizar_status(conn: sqlite3.Connection) -> int:
    """ANUNCIADO -> CHEGOU -> ENDERECADO | DIVERGENCIA, lendo o
    wms_recebimentos ligado. Só anda pra frente; CANCELADO nunca reabre.
    Um UPDATE por entrada (dados.db compartilhado)."""
    if not _tem_tabela(conn, "portal_entradas") or not _tem_tabela(conn, "wms_recebimentos"):
        return 0
    rows = conn.execute("""
        SELECT e.id, e.status, r.estado, r.situacao,
               (SELECT COALESCE(SUM(i.qtd_enderecada), 0) FROM wms_recebimento_itens i WHERE i.recebimento_id = r.id) AS enderecado
          FROM portal_entradas e JOIN wms_recebimentos r ON r.id = e.wms_recebimento_id
         WHERE e.status IN ('ANUNCIADO', 'CHEGOU')""").fetchall()
    mudou = 0
    for r in rows:
        novo = None
        if r["estado"] == "ENDERECADO":
            novo = STATUS_ENDERECADO
        elif r["estado"] == "DIVERGENCIA":
            novo = STATUS_DIVERGENCIA
        elif r["status"] == STATUS_ANUNCIADO and ("RECEB" in (r["situacao"] or "").upper() or float(r["enderecado"] or 0) > 0):
            novo = STATUS_CHEGOU
        if novo and novo != r["status"]:
            conn.execute("UPDATE portal_entradas SET status = ?, atualizado_em = ? WHERE id = ? AND status IN ('ANUNCIADO','CHEGOU')",
                         (novo, _agora(), r["id"]))
            conn.commit()
            mudou += 1
    return mudou
```

Nota: `"RECEB" in situacao.upper()` cobre "Recebido" e "Recebimento iniciado" (os dois estados vistos na Stokki em 25/09); "Em transito" não casa.

- [ ] **Step 5: Implementar no WMS e no timer**

Em `painel_agentes/wms_pedidos.py`, na entrada `"wms_recebimentos"` de `_COLUNAS_NOVAS` (linha 131), acrescentar:

```python
                         "portal_entrada_id": "INTEGER",
                         "data_prevista": "TEXT NOT NULL DEFAULT ''",
```

Em `sincronizar_recebimentos_wms.py`:

1. Import no topo (depois de `import wms_pedidos`): `sys.path.insert(0, str(_RAIZ / "portal_cliente"))` e `import entradas as portal_entradas`.
2. Em `rodar`, na linha 109-110 acrescentar `"amarrados": 0` ao dict `res`.
3. Linha 116-117: o SELECT dos fechados passa a `WHERE estado IN ('ENDERECADO', 'DIVERGENCIA', 'CANCELADO')`.
4. Linhas 157-163: trocar `itens = stokki_recebimentos.ler_itens(sess, dados["id_stokki"])` por:

```python
        try:
            detalhe = stokki_recebimentos.ler_detalhe(sess, dados["id_stokki"])
            itens = detalhe["itens"]
        except Exception as e:  # noqa: BLE001 -- um recebimento ruim nao derruba a rodada
            logger.warning("Leitura do recebimento %s falhou: %s", dados["id_stokki"], e)
            res["erros"] += 1
            continue
```

5. Linhas 178-181: depois de `wms_pedidos.registrar_recebimento(conn, dados, itens)` e antes do `conn.commit()`:

```python
            recebimento_id = wms_pedidos.registrar_recebimento(conn, dados, itens)
            conn.commit()
            res["gravados"] += 1
            # Aba Pedidos de Entrada do portal (25/09): amarra o #PE a entrada
            # anunciada pelo cliente -- por stokki_id, pela chave NF-e ou pela
            # referencia. Falha aqui nao derruba o recebimento ja gravado.
            try:
                if portal_entradas.amarrar_recebimento(conn, recebimento_id, dados["id_stokki"], dados.get("codigo", ""),
                                                       stkkc_id, detalhe["chave_nfe"], detalhe["ref_pedido"]):
                    res["amarrados"] += 1
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                logger.warning("Amarracao do recebimento %s com o portal falhou: %s", dados["id_stokki"], e)
```

(`registrar_recebimento` já devolve o id; o `res["gravados"] += 1` e o `conn.commit()` existentes ficam como estão, só mudando pra capturar o retorno.)

6. No fim de `rodar`, antes do `return res`:

```python
    try:
        res["status_portal"] = portal_entradas.sincronizar_status(conn)
    except Exception as e:  # noqa: BLE001
        logger.warning("sincronizar_status do portal falhou: %s", e)
    return res
```

Em `painel_agentes/painel_agentes.py`, na rota `api_wms_recebimento_encerrar_divergencia`, depois do bloco do e-mail (linha 2988) e ainda dentro do `try`:

```python
        try:
            sys.path.insert(0, str(_RAIZ / "portal_cliente")) if str(_RAIZ / "portal_cliente") not in sys.path else None
            import entradas as portal_entradas
            portal_entradas.sincronizar_status(conn)
        except Exception:  # noqa: BLE001 -- o portal nunca derruba o encerramento
            logging.getLogger(__name__).exception("sincronizar_status do portal falhou no recebimento %s", recebimento_id)
```

(Confira o nome da variável da raiz do repo no topo de `painel_agentes.py`; se for outro que não `_RAIZ`, use o que existir.)

Em `painel_agentes/templates/wms.html`, linha 667, trocar o `<div class="meta">` por:

```html
          <div class="meta">${esc((x.embarcador || '').split(' ').slice(0, 3).join(' '))}${x.situacao ? ' · ' + esc(x.situacao) : ''} · ${x.itens} item(ns)${x.chegada ? ' · chegada ' + esc(x.chegada) : ''}${x.data_prevista ? ' · prevista ' + esc(x.data_prevista.split('-').reverse().join('/')) : ''}${x.portal_entrada_id ? ' · <b>portal</b>' : ''}</div>
```

- [ ] **Step 6: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_entradas test_sincronizar_recebimentos_wms -v && (cd painel_agentes && py -3.11 -m unittest test_wms_pedidos -v)`
Expected: todos PASS (inclusive os antigos do timer e do WMS). Os testes de `painel_agentes/` só carregam de dentro da pasta (o pacote não expõe `painel_agentes.test_*`).

- [ ] **Step 7: Compilar**

Run: `py -3.11 -m py_compile portal_cliente/entradas.py sincronizar_recebimentos_wms.py painel_agentes/wms_pedidos.py painel_agentes/painel_agentes.py`

---

### Task 6: rotas `/api/entradas/*`, `aba_inicial` e comando `entradas` no CLI

**Files:**
- Modify: `portal_cliente/app.py` (import na linha 46, `inicio()` linhas 434-441, rotas novas depois de `api_envios_xml`, linha 852)
- Modify: `portal_cliente/gerenciar_clientes.py` (docstring, `_comandos_envio`, `main`)
- Test: `portal_cliente/test_entradas.py` (só o CLI; as rotas são provadas à mão no Step 5)

**Interfaces:**
- Produces: `GET /api/entradas`, `POST /api/entradas/analisar` (multipart `arquivos`), `GET /api/entradas/modelo-planilha`, `POST /api/entradas/confirmar` (JSON `{itens:[{token,data_prevista,observacoes}]}`), `POST /api/entradas/<id>/cancelar` (JSON `{motivo}`), `GET /api/entradas/<id>/arquivo`. Todas usam `_empresa_envio()` (cabeçalho `X-Portal-Empresa`) e `_exige_pode_enviar()`, como Envios. `inicio()` passa `entradas_ativo` ao template e aceita `?aba=entradas`.
- Produces: `gerenciar_clientes.py entradas <cnpj> [--ativar|--desativar]`.

- [ ] **Step 1: Escrever o teste do CLI que falha**

Acrescentar em `portal_cliente/test_entradas.py`:

```python
class TestCli(BasePortal):
    def test_entradas_ativar_avisa_quando_nao_e_o_piloto(self):
        import io
        import contextlib
        import gerenciar_clientes as cli
        saida = io.StringIO()
        # sem patch em envios.conectar: ep.DB_PATH já aponta pro banco temporário
        # (BasePortal) e o comando chama garantir_tabelas antes de tudo
        with mock.patch.object(cli, "_config", return_value={"wms": {"embarcador_piloto_id": "48"}}), \
             contextlib.redirect_stdout(saida):
            cli.main(["entradas", OUTRO, "--ativar"])
            cli.main(["entradas", CNPJ, "--ativar"])
            cli.main(["entradas", CNPJ])
        texto = saida.getvalue()
        self.assertIn("ATENÇÃO", texto)                 # OUTRO (stkkc 77) não é o piloto 48
        self.assertIn("Pedidos de Entrada: ATIVO", texto)
        self.assertTrue(entradas.config_entradas_cliente(self.conn, CNPJ)["entradas_ativo"])
        self.assertTrue(entradas.config_entradas_cliente(self.conn, OUTRO)["entradas_ativo"])   # avisa, mas obedece
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_entradas.TestCli -v`
Expected: `SystemExit: 2` (argparse não conhece `entradas`).

- [ ] **Step 3: Implementar o CLI**

Em `portal_cliente/gerenciar_clientes.py`:

1. Docstring, depois do bloco "Máscara de envio de pedidos": 

```
Pedidos de Entrada (25/09, só o piloto do WMS):
    py -3 portal_cliente/gerenciar_clientes.py entradas 22135070000190              # mostra se a aba está ligada
    py -3 portal_cliente/gerenciar_clientes.py entradas 22135070000190 --ativar     # liga a aba pro embarcador
    py -3 portal_cliente/gerenciar_clientes.py entradas 22135070000190 --desativar
```

2. Import: `import entradas as entradas_portal` depois de `import envio_pedidos as envios`.

3. Em `_comandos_envio`, antes do `if args.cmd == "solicitacoes":`:

```python
        if args.cmd == "entradas":
            entradas_portal.garantir_tabelas(conn)
            if args.ativar or args.desativar:
                entradas_portal.definir_entradas_ativo(conn, args.cnpj, bool(args.ativar))
            c = envios.config_stokki_cliente(conn, args.cnpj, cfg)
            ativo = entradas_portal.config_entradas_cliente(conn, args.cnpj)["entradas_ativo"]
            piloto = str((cfg.get("wms", {}) or {}).get("embarcador_piloto_id", "48"))
            print(f"{c['nome']} ({auth.formatar_cnpj(c['cnpj'])})  stkkc_id {c['client_id'] or '(FALTA)'}")
            print(f"  Pedidos de Entrada: {'ATIVO' if ativo else 'DESATIVADO'}")
            if ativo and str(c["client_id"]) != piloto:
                print(f"ATENÇÃO: o timer do WMS só lê recebimentos do piloto (stkkc {piloto}); entradas deste embarcador "
                      f"vão ficar ANUNCIADO pra sempre até o WMS atender mais de um embarcador.")
            return 0
```

4. Em `main`, depois do parser `envio`:

```python
    en = sub.add_parser("entradas", help="aba Pedidos de Entrada (só o piloto do WMS)")
    en.add_argument("cnpj")
    ge = en.add_mutually_exclusive_group()
    ge.add_argument("--ativar", action="store_true")
    ge.add_argument("--desativar", action="store_true")
```

e na linha `if args.cmd in ("envio", "solicitacoes", "fila", "concluir"):` incluir `"entradas"`.

- [ ] **Step 4: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_entradas.TestCli -v`
Expected: PASS.

- [ ] **Step 5: Implementar as rotas**

Em `portal_cliente/app.py`:

1. Linha 46, depois de `import envio_pedidos as envios`: `import entradas as entradas_mod`.

2. `inicio()` (linhas 434-441) vira:

```python
@app.route("/")
@requer_cliente
def inicio():
    aba = request.args.get("aba") or "acompanhamento"
    conn = entradas_mod.conectar()
    try:
        entradas_ativo = entradas_mod.entradas_ativas_para(conn, [e["cnpj"] for e in g.cliente["empresas"]])
    finally:
        conn.close()
    abas = ("acompanhamento", "envios") + (("entradas",) if entradas_ativo else ())
    return render_template("acompanhamento.html", data_inicial=_data_da_query().isoformat(),
                           empresas_envio=_empresas_com_envio(),
                           hoje=date.today().isoformat(), aba_inicial=aba if aba in abas else "acompanhamento",
                           entradas_ativo=entradas_ativo,
                           pode_enviar=not (g.get("equipe") and g.equipe.get("nivel") not in _NIVEIS_EQUIPE_ENVIA))
```

3. Depois de `api_envios_xml` (linha 852), as rotas novas:

```python
# ── Pedidos de Entrada (25/09) ─────────────────────────────────────────────────

def _exige_entradas_ativas(conn) -> None:
    if not entradas_mod.config_entradas_cliente(conn, _empresa_envio()["cnpj"])["entradas_ativo"]:
        abort(Response(jsonify({"erro": "A aba Pedidos de Entrada não está liberada pra esta empresa."}).get_data(),
                       status=403, mimetype="application/json"))


@app.route("/api/entradas")
@requer_cliente
def api_entradas_listar():
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
        lista = entradas_mod.listar_entradas(conn, _empresa_envio()["cnpj"])
    finally:
        conn.close()
    return jsonify({"entradas": lista, "resumo": entradas_mod.resumo_entradas(lista), "dias": entradas_mod.DIAS_LISTAGEM,
                    "hoje": date.today().isoformat(), "atualizado_em": datetime.now().strftime("%H:%M")})


@app.route("/api/entradas/analisar", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_entradas_analisar():
    """Recebe .xml/.zip/.xlsx, lê cada NF-e de remessa ou remessa da
    planilha, valida e devolve a prévia. Nada entra na fila ainda."""
    _exige_pode_enviar()
    arquivos = request.files.getlist("arquivos")
    if not arquivos:
        return jsonify({"erro": "Selecione pelo menos um arquivo: XML da NF-e de remessa (ou ZIP) ou planilha .xlsx no modelo Fresh Log de entrada."}), 400
    itens, rejeitados, vistos = [], [], set()
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
        cnpj = _empresa_envio()["cnpj"]
        for f in arquivos:
            try:
                partes = envios.expandir_upload(f.filename or "", f.read())
            except envios.ErroEnvio as e:
                rejeitados.append({"arquivo": f.filename or "arquivo", "erro": str(e)})
                continue
            for nome, conteudo in partes:
                if envios.e_planilha(nome, conteudo):
                    try:
                        remessas, rej = entradas_mod.ler_planilha_entrada(conteudo, nome, cnpj)
                    except envios.ErroEnvio as e:
                        rejeitados.append({"arquivo": nome, "erro": str(e)})
                        continue
                    rejeitados.extend(rej)
                    token_plan = envios.guardar_temporario(conteudo, Path(nome).suffix.lstrip(".") or "xlsx")
                    lidos = [(entradas_mod.rotulo_entrada(r), r, lambda r=r: envios.guardar_temporario_pedido(r, token_plan)) for r in remessas]
                else:
                    try:
                        nfe = entradas_mod.ler_nfe_entrada(conteudo, nome)
                    except envios.ErroEnvio as e:
                        rejeitados.append({"arquivo": nome, "erro": str(e)})
                        continue
                    lidos = [(entradas_mod.rotulo_entrada(nfe), nfe, lambda c=conteudo: envios.guardar_temporario(c))]
                for rotulo, item, guardar in lidos:
                    if item["chave_nfe"] in vistos:
                        rejeitados.append({"arquivo": nome, "rotulo": rotulo, "erro": "Repetida dentro do mesmo envio."})
                        continue
                    vistos.add(item["chave_nfe"])
                    v = entradas_mod.validar_entrada(conn, item, cnpj, _CONFIG, _outras_empresas_envio())
                    if not v["ok"]:
                        rejeitados.append({"arquivo": nome, "rotulo": rotulo, "erro": " ".join(v["erros"])})
                        continue
                    previa = {k: x for k, x in item.items() if k not in ("emitente_cnpj",)}
                    previa.update({"token": guardar(), "avisos": v["avisos"], "rotulo": rotulo})
                    itens.append(previa)
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    finally:
        conn.close()
    logger.info(f"entradas.analisar cnpj={_empresa_envio()['cnpj']} por={_quem_envia()} validos={len(itens)} rejeitados={len(rejeitados)}")
    return jsonify({"itens": itens, "rejeitados": rejeitados, "hoje": date.today().isoformat()})


@app.route("/api/entradas/modelo-planilha")
@requer_cliente
def api_entradas_modelo_planilha():
    conteudo = entradas_mod.gerar_modelo_planilha_entrada(_empresa_envio()["nome"] or "")
    return send_file(io.BytesIO(conteudo), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="modelo_entradas_freshlog.xlsx", max_age=0)


@app.route("/api/entradas/confirmar", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_entradas_confirmar():
    _exige_pode_enviar()
    corpo = request.get_json(silent=True) or {}
    itens = corpo.get("itens") or []
    if not isinstance(itens, list) or not itens:
        return jsonify({"erro": "Nenhuma remessa pra confirmar."}), 400
    if len(itens) > 200:
        return jsonify({"erro": "Anuncie no máximo 200 notas por vez."}), 400
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
        criados = entradas_mod.confirmar_entradas(conn, _empresa_envio()["cnpj"], itens, _quem_envia(), _CONFIG)
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    except Exception as e:
        logger.exception("confirmar_entradas falhou")
        return jsonify({"erro": f"Não foi possível registrar as entradas ({type(e).__name__})."}), 500
    finally:
        conn.close()
    n = len(criados)
    logger.info(f"entradas.confirmar cnpj={_empresa_envio()['cnpj']} por={_quem_envia()} n={n}")
    return jsonify({"ok": True, "criados": criados,
                    "mensagem": f"{n} entrada{'s' if n > 1 else ''} anunciada{'s' if n > 1 else ''}. "
                                f"O recebimento é criado na Stokki em instantes -- acompanhe o status na lista."})


@app.route("/api/entradas/<int:entrada_id>/cancelar", methods=["POST"])
@requer_cliente
@exige_mesma_origem
def api_entradas_cancelar(entrada_id):
    _exige_pode_enviar()
    conn = entradas_mod.conectar()
    try:
        _exige_entradas_ativas(conn)
        entrada = entradas_mod.buscar_entrada(conn, entrada_id, _empresa_envio()["cnpj"])
        if not entrada:
            return jsonify({"erro": "Entrada não encontrada."}), 404
        emp = _empresa_envio()
        # CNPJ do LOGIN (g.cliente), como bloqueio_area: o chat busca o chamado pelo CNPJ logado
        r = entradas_mod.cancelar_entrada(conn, entrada, _quem_envia(),
                                          {"cnpj": g.cliente["cnpj"], "sender_id": emp.get("sender_id"), "nome": emp.get("nome")}, _CONFIG)
    except envios.ErroEnvio as e:
        return _json_erro_envio(e)
    finally:
        conn.close()
    logger.info(f"entradas.cancelar cnpj={_empresa_envio()['cnpj']} entrada={entrada_id} por={_quem_envia()} -> {r}")
    return jsonify({"ok": True, **r})


@app.route("/api/entradas/<int:entrada_id>/arquivo")
@requer_cliente
def api_entradas_arquivo(entrada_id):
    conn = entradas_mod.conectar()
    try:
        entrada = entradas_mod.buscar_entrada(conn, entrada_id, _empresa_envio()["cnpj"])
    finally:
        conn.close()
    if not entrada:
        abort(404)
    caminho = entradas_mod.caminho_arquivo(entrada)
    if not caminho.is_file():
        abort(404)
    if entrada["origem"] == envios.ORIGEM_PLANILHA:
        tipos = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xls": "application/vnd.ms-excel"}
        return send_file(caminho, mimetype=tipos.get(caminho.suffix.lower(), "application/octet-stream"), as_attachment=True,
                         download_name=caminho.name.split("_", 3)[-1] if caminho.name.count("_") >= 3 else caminho.name, max_age=0)
    return send_file(caminho, mimetype="application/xml", as_attachment=True, download_name=f"{entrada['chave_nfe']}.xml", max_age=0)
```

- [ ] **Step 6: Provar as rotas à mão (porta 8099, nunca 8074/8070)**

Run (de dentro de `portal_cliente/`, com `PORTAL_CLIENTE_DEV=1`): `py -3.11 -c "import os; os.environ['PORTAL_CLIENTE_DEV']='1'; import app; app.app.run(host='127.0.0.1', port=8099)"`

Depois, noutro terminal, com o cliente de teste do portal (CNPJ 00.000.000/0001-91, PIN 123456, ver memória `reference_cliente_teste_portal`) e a flag ligada pra ele (`py -3.11 portal_cliente/gerenciar_clientes.py entradas 00000000000191 --ativar`):

```
curl -s -c c.txt -b c.txt -X POST http://127.0.0.1:8099/login -d "cnpj=00000000000191&pin=123456" -o /dev/null
curl -s -b c.txt http://127.0.0.1:8099/api/entradas
curl -s -b c.txt -F "arquivos=@portal_cliente/fixtures_entradas/remessa_teste.xml" -H "Origin: http://127.0.0.1:8099" http://127.0.0.1:8099/api/entradas/analisar
```

Expected: o primeiro devolve `{"entradas": [], "resumo": {...}}`; o segundo devolve `itens` com um item e `token` (crie o XML de teste com `xml_remessa()` do teste, emitente = 00000000000191, e guarde em `portal_cliente/fixtures_entradas/` — pasta nova, entra no commit como fixture). Depois desligue a flag do cliente de teste: `--desativar`.

- [ ] **Step 7: Compilar**

Run: `py -3.11 -m py_compile portal_cliente/app.py portal_cliente/gerenciar_clientes.py`

---

### Task 7: aba `_entradas.html`, inclusão em `acompanhamento.html` e passos do tour

**Files:**
- Create: `portal_cliente/templates/_entradas.html`
- Modify: `portal_cliente/templates/acompanhamento.html:219-221` (include)
- Modify: `portal_cliente/templates/_tour_portal.html:36, 50-52, 99-116` (passos)
- Test: prova à mão no navegador (Step 4); sem teste automatizado de template no projeto

**Interfaces:**
- Consumes: `.vistas` (criado pelo script de `_envios.html`, que roda antes porque é incluído antes), `aba_inicial`, `entradas_ativo`, `pode_enviar`, `empresas_envio` do `render_template` (Tarefa 6), rotas da Tarefa 6.
- Produces: botão `data-vista="entradas"` na barra `.vistas`; seção `#vista-entradas`; ids `ent-zona`, `ent-tiles`, `ent-abas`, `ent-tabela`, `ent-previa`, `ent-modal`.

- [ ] **Step 1: Criar o parcial**

Criar `portal_cliente/templates/_entradas.html`:

```html
{# Aba "Pedidos de Entrada" (25/09/2026): o cliente anuncia a mercadoria que
   vai chegar no galpão (XML da NF-e de remessa ou planilha). Incluído por
   acompanhamento.html DEPOIS de _envios.html: reaproveita o CSS de lá
   (.zona, .previa, .msg, .modal*, .chip, .badge) e a barra .vistas, à qual
   acrescenta o terceiro botão. Conversa com /api/entradas/* (app.py). #}
{% if entradas_ativo %}
<style>
  #vista-entradas { display: flex; flex-direction: column; gap: 16px; }
  .b-ANUNCIADO { background: var(--rodando-bg); color: var(--rodando-texto); }
  .b-CHEGOU { background: var(--rota-bg); color: var(--st-rota); }
  .b-ENDERECADO { background: var(--sucesso-bg); color: var(--st-sucesso); }
  .b-DIVERGENCIA { background: var(--erro-bg); color: var(--st-falha); }
  .chip.atrasada { background: var(--erro-bg); color: var(--st-falha); }
  .chip.stokki-erro { background: var(--erro-bg); color: var(--st-falha); }
  .ent-itens { font-size: 12px; margin: 6px 0 0; border-collapse: collapse; }
  .ent-itens th, .ent-itens td { padding: 3px 8px; border-bottom: 1px dashed var(--borda); text-align: left; }
  .ent-itens td.num { text-align: right; }
  .ent-itens .falta { color: var(--st-falha); font-weight: 700; }
  tr.ent-detalhe td { background: var(--fundo); }
  .ent-expandir { border: none; background: none; color: var(--acento); cursor: pointer; font-size: 12px; padding: 0; }
</style>

<section id="vista-entradas" class="oculto" data-aba-inicial="{{ aba_inicial }}" data-pode-enviar="{{ 1 if pode_enviar else 0 }}">
  <div class="env-grade">
    <div class="coluna">
      <div class="card" id="ent-card-anunciar" style="display:flex; flex-direction:column; gap:14px;">
        <div style="display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;">
          <h3 style="margin:0;">Anunciar mercadoria que vai chegar</h3>
          {% if empresas_envio | length > 1 %}
          <select id="ent-empresa" aria-label="Empresa" style="font-family:inherit; font-size:12.5px; color:var(--texto); background:var(--superficie); border:1px solid var(--borda); border-radius:6px; padding:5px 9px; max-width:280px;">
            {% for e in empresas_envio %}<option value="{{ e.cnpj }}">{{ e.nome }}</option>{% endfor %}
          </select>
          {% endif %}
        </div>
        <label class="zona" id="ent-zona">
          <input type="file" id="ent-arquivos" multiple accept=".xml,.zip,.xlsx,.xls,text/xml,application/xml,application/zip,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
          <div><b>Arraste o XML da NF-e de remessa ou a planilha de entrada aqui</b> ou clique pra escolher</div>
          <div style="font-size:11.5px; margin-top:4px;">XML: a nota de remessa emitida pela sua empresa pro galpão Fresh Log (vários .xml ou um .zip). Planilha: .xlsx no modelo de entrada, uma linha por item.</div>
        </label>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:12px; color:var(--texto-suave);">
          <a class="botao-linha" id="ent-btn-modelo" href="{{ request.script_root }}/api/entradas/modelo-planilha" download style="text-decoration:none;">Baixar modelo de planilha de entrada (.xlsx)</a>
        </div>
        <div class="msg" id="ent-msg"></div>
        <div class="previa" id="ent-previa">
          <div class="rej oculto" id="ent-previa-rej"></div>
          <div>
            <h4 id="ent-previa-titulo"></h4>
            <div style="font-size:12px; color:var(--texto-suave); margin:4px 0 8px;">Data prevista pra todas: <input type="date" id="ent-data-todas"> <span>(ajuste por linha se precisar)</span></div>
            <div class="rolagem"><table id="ent-previa-tabela"><thead><tr><th>NF / Remessa</th><th>Itens</th><th style="text-align:center">Vol.</th><th>Data prevista</th><th>Observação</th></tr></thead><tbody></tbody></table></div>
          </div>
          <div class="barra-confirmar">
            <button type="button" class="botao" id="ent-btn-confirmar">Confirmar anúncio</button>
            <button type="button" class="botao-linha" id="ent-btn-limpar">Descartar</button>
            <span style="font-size:12px; color:var(--texto-suave);">Depois de confirmar, o recebimento é criado na Stokki e o galpão passa a esperar a mercadoria.</span>
          </div>
        </div>
      </div>

      <div class="card" style="padding:14px 18px;">
        <div class="grade-tiles" id="ent-tiles"><div class="carregando">Carregando as entradas…</div></div>
      </div>

      <div class="card card-tabela">
        <div class="ferramentas">
          <div class="abas" id="ent-abas"></div>
          <div class="busca">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
            <input type="search" id="ent-busca" placeholder="Buscar por NF, remessa, SKU ou #PE">
          </div>
          <button type="button" class="botao-linha" id="ent-atualizar">Atualizar</button>
        </div>
        <div class="rolagem"><table id="ent-tabela"><thead><tr><th>Anunciado em</th><th>NF / Remessa</th><th>Data prevista</th><th style="text-align:center">Itens</th><th style="text-align:center">Vol.</th><th>Situação</th><th>Stokki</th><th>Ações</th></tr></thead><tbody></tbody></table></div>
        <div class="vazio oculto" id="ent-vazia"></div>
        <div class="rodape-tabela"><span id="ent-contagem"></span><span id="ent-carimbo" style="font-size:12px;"></span></div>
      </div>
    </div>

    <aside class="coluna">
      <div class="card">
        <h3>Como funciona</h3>
        <div class="lista-lateral" style="font-size:12.5px; color:var(--texto-suave); gap:10px;">
          <div><b style="color:var(--texto)">1.</b> Você sobe o XML da NF-e de remessa que a sua empresa emitiu pro galpão da Fresh Log (ou a planilha de entrada) e informa a data prevista de chegada.</div>
          <div><b style="color:var(--texto)">2.</b> O recebimento é criado na Stokki automaticamente e o galpão passa a esperar a mercadoria.</div>
          <div><b style="color:var(--texto)">3.</b> Quando chega, o galpão confere e endereça. Se chegar menos do que foi anunciado, a divergência aparece aqui e você recebe o relatório por e-mail.</div>
          <div><b style="color:var(--texto)">4.</b> Enquanto ainda não chegou, dá pra cancelar por aqui.</div>
        </div>
      </div>
    </aside>
  </div>
</section>

<div class="modal-fundo" id="ent-modal"><div class="modal" id="ent-modal-caixa"></div></div>

<script>
(function () {
  const BASE = {{ request.script_root | tojson }};
  const secao = document.getElementById('vista-entradas');
  const PODE_ENVIAR = secao.dataset.podeEnviar === '1';
  const EMPRESAS = {{ empresas_envio | tojson }};
  let empresa = EMPRESAS.length ? EMPRESAS[0].cnpj : '';
  const fetch = (url, o = {}) => window.fetch(url, { ...o, headers: { ...(o.headers || {}), ...(empresa ? { 'X-Portal-Empresa': empresa } : {}) } });
  const comEmpresa = url => empresa ? `${url}?empresa=${empresa}` : url;
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = v => (Number(v) || 0).toLocaleString('pt-BR', { maximumFractionDigits: 3 });
  const dataBr = iso => iso ? iso.split('-').reverse().join('/') : '';

  // ── Terceiro botão na barra .vistas (criada por _envios.html) ──────────────
  const vistas = document.querySelector('.vistas');
  const btn = document.createElement('button');
  btn.type = 'button'; btn.dataset.vista = 'entradas';
  btn.innerHTML = 'Pedidos de entrada <em id="ent-badge"></em>';
  const envBtn = vistas.querySelector('[data-vista="envios"]');
  envBtn.insertAdjacentElement('afterend', btn);
  let vista = 'acompanhamento';
  // O handler de _envios.html já esconde a grade e a vista de envios pra
  // qualquer data-vista; aqui só se cuida da própria seção e do ?aba=.
  vistas.addEventListener('click', e => {
    const b = e.target.closest('[data-vista]'); if (!b) return;
    vista = b.dataset.vista;
    secao.classList.toggle('oculto', vista !== 'entradas');
    const url = new URL(location.href);
    if (vista === 'entradas') { url.searchParams.set('aba', 'entradas'); history.replaceState(null, '', url); carregar(); }
  });

  // ── Estado ─────────────────────────────────────────────────────────────────
  let dados = null, aba = 'abertas', busca = '', timer = null, previa = null, abertos = new Set();

  async function carregar() {
    try {
      const r = await fetch(`${BASE}/api/entradas`, { credentials: 'same-origin' });
      if (r.status === 401) { location.href = `${BASE}/login?proximo=${encodeURIComponent(location.pathname + location.search)}`; return; }
      const j = await r.json();
      if (!r.ok) throw new Error(j.erro || 'Falha ao carregar as entradas.');
      dados = j; render();
    } catch (e) { $('ent-carimbo').textContent = e.message; }
    clearTimeout(timer);
    const ativo = dados && dados.entradas.some(x => x.stokki_status === 'NA_FILA' || x.stokki_status === 'ENVIANDO');
    timer = setTimeout(carregar, ativo ? 20000 : 120000);
  }

  function render() {
    const d = dados, k = d.resumo;
    if (!PODE_ENVIAR) $('ent-card-anunciar').classList.add('oculto');
    const badge = k.atrasados + k.divergencias + k.erros_stokki;
    $('ent-badge').textContent = badge; $('ent-badge').classList.toggle('on', badge > 0);
    $('ent-tiles').innerHTML = `
      <div class="tile" data-aba="abertas"><span class="rotulo">Anunciadas</span><span class="valor">${k.anunciados}</span><span class="sub">aguardando chegar</span></div>
      <div class="tile ${k.atrasados ? 'ruim' : ''}" data-aba="atrasadas"><span class="rotulo">Atrasadas</span><span class="valor">${k.atrasados}</span><span class="sub">${k.atrasados ? 'passou da data prevista' : '—'}</span></div>
      <div class="tile rota" data-aba="chegaram"><span class="rotulo">Chegaram</span><span class="valor">${k.chegaram_hoje}</span><span class="sub">no galpão</span></div>
      <div class="tile ${k.divergencias ? 'ruim' : ''}" data-aba="divergencias"><span class="rotulo">Com divergência</span><span class="valor">${k.divergencias}</span><span class="sub">${k.divergencias ? 'chegou menos que o anunciado' : '—'}</span></div>`;
    const abas = [['abertas', 'Abertas', k.anunciados], ['atrasadas', 'Atrasadas', k.atrasados], ['chegaram', 'Chegaram', k.chegaram_hoje], ['divergencias', 'Divergência', k.divergencias], ['todas', 'Todas', k.total]];
    $('ent-abas').innerHTML = abas.map(([id, r, n]) => `<button type="button" class="${aba === id ? 'ativa' : ''}" data-aba="${id}">${r} <em>${n}</em></button>`).join('');
    renderTabela();
    $('ent-carimbo').textContent = `Atualizado às ${d.atualizado_em}`;
  }

  function filtradas() {
    let lista = dados.entradas;
    if (aba === 'abertas') lista = lista.filter(x => x.status === 'ANUNCIADO');
    else if (aba === 'atrasadas') lista = lista.filter(x => x.atrasada);
    else if (aba === 'chegaram') lista = lista.filter(x => ['CHEGOU', 'ENDERECADO', 'DIVERGENCIA'].includes(x.status));
    else if (aba === 'divergencias') lista = lista.filter(x => x.status === 'DIVERGENCIA');
    const q = busca.trim().toLowerCase();
    if (q) lista = lista.filter(x => [x.numero_nf, x.referencia, x.stokki_codigo, ...(x.itens_lista || []).map(i => i.sku)].join(' ').toLowerCase().includes(q));
    return lista;
  }

  function detalhe(x) {
    const itens = `<table class="ent-itens"><thead><tr><th>SKU</th><th>Descrição</th><th class="num">Qtd anunciada</th></tr></thead><tbody>` +
      (x.itens_lista || []).map(i => `<tr><td class="mono">${esc(i.sku)}</td><td>${esc(i.descricao || '')}</td><td class="num">${num(i.quantidade)} ${esc(i.unidade || '')}</td></tr>`).join('') + '</tbody></table>';
    const conf = (x.conferencia || []).length ? `<div style="margin-top:8px;font-size:12px;font-weight:600">Conferência do galpão (por SKU)</div><table class="ent-itens"><thead><tr><th>SKU</th><th>Descrição</th><th class="num">Anunciado</th><th class="num">Recebido</th><th class="num">Faltou</th></tr></thead><tbody>` +
      x.conferencia.map(c => `<tr><td class="mono">${esc(c.sku)}</td><td>${esc(c.descricao)}</td><td class="num">${num(c.anunciado)} ${esc(c.unidade)}</td><td class="num">${num(c.recebido)} ${esc(c.unidade)}</td><td class="num ${c.falta > 0 ? 'falta' : ''}">${c.falta > 0 ? num(c.falta) + ' ' + esc(c.unidade) : '—'}</td></tr>`).join('') + '</tbody></table>' +
      (x.observacao_divergencia ? `<div style="margin-top:6px;font-size:12px"><b>Observação de quem recebeu:</b> ${esc(x.observacao_divergencia)}</div>` : '') : '';
    return `<tr class="ent-detalhe"><td colspan="8">${itens}${conf}${x.observacoes ? `<div style="margin-top:6px;font-size:12px;color:var(--texto-suave)">Obs.: ${esc(x.observacoes)}</div>` : ''}</td></tr>`;
  }

  function renderTabela() {
    const lista = filtradas();
    $('ent-tabela').querySelector('tbody').innerHTML = lista.map(x => `<tr>
      <td class="nw num">${esc(x.criado_em_br)}${x.enviado_por && x.enviado_por.startsWith('equipe') ? ' <span class="chip" title="Anunciado pela equipe Fresh Log">equipe</span>' : ''}</td>
      <td class="nw"><span class="mono num" style="font-weight:600">${esc(x.numero_nf || x.referencia || '—')}</span>${x.origem === 'planilha' ? ' <span class="chip">planilha</span>' : ''}${x.origem === 'planilha' && x.numero_nf && x.referencia ? `<div class="end">${esc(x.referencia)}</div>` : ''}</td>
      <td class="nw num">${esc(x.data_prevista_br)}${x.atrasada ? ' <span class="chip atrasada">atrasada</span>' : ''}</td>
      <td class="num nw" style="text-align:center"><button type="button" class="ent-expandir" data-exp="${x.id}">${(x.itens_lista || []).length} ${abertos.has(x.id) ? '▴' : '▾'}</button></td>
      <td class="num nw" style="text-align:center">${x.volumes || '—'}</td>
      <td class="nw"><span class="badge b-${x.status}"><span class="pt"></span>${esc(x.status_rotulo)}</span>${x.encerrado_em_br ? `<div class="end">${esc(x.encerrado_em_br)}</div>` : ''}</td>
      <td class="nw">${x.stokki_codigo ? `<span class="mono">${esc(x.stokki_codigo)}</span>` : (x.stokki_status === 'ERRO' ? `<span class="chip stokki-erro" title="${esc(x.stokki_erro || '')}">erro na Stokki</span><div class="erro-txt">${esc((x.stokki_erro || '').slice(0, 90))}</div>` : (x.status === 'ANUNCIADO' ? `<span style="color:var(--texto-suave)">${esc(x.stokki_rotulo)}</span>` : '—'))}</td>
      <td><div class="acoes-linha">
        ${PODE_ENVIAR && x.pode_cancelar ? `<button type="button" class="perigo" data-acao="cancelar" data-id="${x.id}">Cancelar</button>` : ''}
        <button type="button" data-acao="arquivo" data-id="${x.id}">${x.origem === 'planilha' ? 'Planilha' : 'XML'}</button>
      </div></td></tr>` + (abertos.has(x.id) ? detalhe(x) : '')).join('');
    const vazio = $('ent-vazia');
    if (!lista.length) { vazio.classList.remove('oculto'); vazio.textContent = busca ? 'Nada bate com a busca.' : (dados.entradas.length ? 'Nenhuma entrada nessa situação.' : 'Você ainda não anunciou nenhuma mercadoria por aqui.'); }
    else vazio.classList.add('oculto');
    $('ent-contagem').textContent = lista.length ? `${lista.length} entrada${lista.length > 1 ? 's' : ''}` : '';
  }

  // ── Upload → prévia → confirmar ────────────────────────────────────────────
  function mensagem(texto, tipo) { const m = $('ent-msg'); m.className = 'msg' + (texto ? ' ' + tipo : ''); m.textContent = texto || ''; }

  async function analisar(arquivos) {
    if (!arquivos || !arquivos.length) return;
    const fd = new FormData();
    for (const f of arquivos) fd.append('arquivos', f);
    mensagem(`Lendo ${arquivos.length} arquivo${arquivos.length > 1 ? 's' : ''}…`, 'info');
    try {
      const r = await fetch(`${BASE}/api/entradas/analisar`, { method: 'POST', credentials: 'same-origin', body: fd });
      const j = await r.json();
      if (!r.ok) throw new Error(j.erro || 'Falha ao ler os arquivos.');
      previa = j; mensagem('', ''); renderPrevia();
    } catch (e) { mensagem(e.message, 'erro'); }
    $('ent-arquivos').value = '';
  }

  function renderPrevia() {
    const p = previa, box = $('ent-previa');
    if (!p.itens.length && !p.rejeitados.length) { box.classList.remove('aberta'); return; }
    box.classList.add('aberta');
    const rej = $('ent-previa-rej');
    if (p.rejeitados.length) {
      rej.classList.remove('oculto');
      rej.innerHTML = `<b>${p.rejeitados.length} ${p.rejeitados.length > 1 ? 'itens não podem ser anunciados' : 'item não pode ser anunciado'}:</b>` +
        p.rejeitados.map(r => `<div>• ${esc(r.rotulo || r.arquivo)} — ${esc(r.erro)}</div>`).join('');
    } else { rej.classList.add('oculto'); rej.innerHTML = ''; }
    $('ent-previa-titulo').textContent = p.itens.length ? `${p.itens.length} remessa${p.itens.length > 1 ? 's' : ''} pronta${p.itens.length > 1 ? 's' : ''} pra anunciar` : 'Nenhuma remessa válida neste lote';
    $('ent-data-todas').min = p.hoje;
    $('ent-previa-tabela').querySelector('tbody').innerHTML = p.itens.map((it, i) => `<tr data-i="${i}">
      <td class="nw"><span class="mono num" style="font-weight:600">${esc(it.rotulo)}</span>${it.avisos.length ? `<div class="avisos">${esc(it.avisos.join(' '))}</div>` : ''}</td>
      <td>${(it.itens_lista || []).slice(0, 4).map(x => `<div class="end">${esc(x.sku)} × ${num(x.quantidade)} ${esc(x.unidade || '')}</div>`).join('')}${(it.itens_lista || []).length > 4 ? `<div class="end">e mais ${it.itens_lista.length - 4}…</div>` : ''}</td>
      <td class="num nw" style="text-align:center">${it.volumes}</td>
      <td class="nw"><input type="date" class="ent-data" min="${p.hoje}" value="${esc(it.data_prevista || '')}" required></td>
      <td><input type="text" class="ent-obs" maxlength="500" placeholder="opcional" style="width:100%"></td>
    </tr>`).join('');
    $('ent-btn-confirmar').disabled = !p.itens.length;
    $('ent-btn-confirmar').textContent = p.itens.length ? `Confirmar anúncio de ${p.itens.length} remessa${p.itens.length > 1 ? 's' : ''}` : 'Nada a anunciar';
  }

  $('ent-data-todas').addEventListener('change', e => { $('ent-previa-tabela').querySelectorAll('.ent-data').forEach(i => { i.value = e.target.value; }); });

  async function confirmar() {
    const itens = [];
    for (let i = 0; i < previa.itens.length; i++) {
      const tr = $('ent-previa-tabela').querySelector(`tr[data-i="${i}"]`);
      const data = tr.querySelector('.ent-data').value;
      if (!data) { mensagem(`${previa.itens[i].rotulo}: informe a data prevista de chegada.`, 'erro'); return; }
      itens.push({ token: previa.itens[i].token, data_prevista: data, observacoes: tr.querySelector('.ent-obs').value });
    }
    $('ent-btn-confirmar').disabled = true;
    mensagem('Registrando as entradas…', 'info');
    try {
      const r = await fetch(`${BASE}/api/entradas/confirmar`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ itens }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.erro || 'Falha ao confirmar.');
      previa = null; $('ent-previa').classList.remove('aberta');
      mensagem(j.mensagem, 'ok'); aba = 'abertas'; carregar();
    } catch (e) { mensagem(e.message, 'erro'); $('ent-btn-confirmar').disabled = false; }
  }

  // ── Cancelar ───────────────────────────────────────────────────────────────
  function modal(html) { $('ent-modal-caixa').innerHTML = html; $('ent-modal').classList.add('aberto'); }
  function fecharModal() { $('ent-modal').classList.remove('aberto'); }
  $('ent-modal').addEventListener('click', e => { if (e.target === $('ent-modal') || e.target.closest('[data-fechar]')) fecharModal(); });

  function abrirCancelar(id) {
    const x = dados.entradas.find(e => e.id === Number(id)); if (!x) return;
    modal(`<h3>Cancelar entrada</h3><p><b>${esc(x.rotulo)}</b> · prevista pra ${esc(x.data_prevista_br)}${x.stokki_codigo ? ' · ' + esc(x.stokki_codigo) : ''}</p>
      <p>${x.stokki_codigo ? 'O recebimento já existe na Stokki: a Fresh Log vai cancelá-lo lá e você acompanha pelo chat.' : 'A entrada ainda não foi criada na Stokki — será cancelada na hora.'}</p>
      <label>Motivo (opcional) <textarea id="ent-m-motivo" maxlength="300"></textarea></label>
      <div class="msg" id="ent-m-msg"></div>
      <div class="pe"><button type="button" class="botao-linha" data-fechar>Voltar</button><button type="button" class="botao" id="ent-m-ok">Cancelar entrada</button></div>`);
    $('ent-m-ok').addEventListener('click', async () => {
      $('ent-m-ok').disabled = true;
      try {
        const r = await fetch(`${BASE}/api/entradas/${id}/cancelar`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ motivo: $('ent-m-motivo').value }) });
        const j = await r.json();
        if (!r.ok) throw new Error(j.erro || 'Falha ao cancelar.');
        fecharModal(); mensagem(j.mensagem, 'ok'); carregar();
      } catch (e) { const m = $('ent-m-msg'); m.className = 'msg erro'; m.textContent = e.message; $('ent-m-ok').disabled = false; }
    });
  }

  // ── Eventos ────────────────────────────────────────────────────────────────
  const zona = $('ent-zona');
  $('ent-btn-modelo').addEventListener('click', e => e.stopPropagation());
  $('ent-arquivos').addEventListener('change', e => analisar(e.target.files));
  ['dragenter', 'dragover'].forEach(ev => zona.addEventListener(ev, e => { e.preventDefault(); zona.classList.add('sobre'); }));
  ['dragleave', 'drop'].forEach(ev => zona.addEventListener(ev, e => { e.preventDefault(); zona.classList.remove('sobre'); }));
  zona.addEventListener('drop', e => analisar(e.dataTransfer.files));
  $('ent-btn-confirmar').addEventListener('click', confirmar);
  $('ent-btn-limpar').addEventListener('click', () => { previa = null; $('ent-previa').classList.remove('aberta'); mensagem('', ''); });
  if ($('ent-empresa')) {
    $('ent-empresa').addEventListener('change', () => { empresa = $('ent-empresa').value; previa = null; $('ent-previa').classList.remove('aberta'); $('ent-btn-modelo').href = comEmpresa(`${BASE}/api/entradas/modelo-planilha`); if (vista === 'entradas') carregar(); });
    $('ent-btn-modelo').href = comEmpresa(`${BASE}/api/entradas/modelo-planilha`);
  }
  $('ent-abas').addEventListener('click', e => { const b = e.target.closest('[data-aba]'); if (!b) return; aba = b.dataset.aba; render(); });
  $('ent-tiles').addEventListener('click', e => { const t = e.target.closest('[data-aba]'); if (!t) return; aba = t.dataset.aba; render(); });
  $('ent-busca').addEventListener('input', e => { busca = e.target.value; renderTabela(); });
  $('ent-atualizar').addEventListener('click', () => carregar());
  secao.addEventListener('click', e => {
    const ex = e.target.closest('[data-exp]');
    if (ex) { const id = Number(ex.dataset.exp); abertos.has(id) ? abertos.delete(id) : abertos.add(id); renderTabela(); return; }
    const b = e.target.closest('[data-acao]'); if (!b) return;
    if (b.dataset.acao === 'arquivo') { window.open(comEmpresa(`${BASE}/api/entradas/${b.dataset.id}/arquivo`), '_blank'); return; }
    abrirCancelar(b.dataset.id);
  });

  // badge sem abrir a vista + aba inicial
  fetch(`${BASE}/api/entradas`, { credentials: 'same-origin' }).then(r => r.ok ? r.json() : null).then(j => {
    if (!j || dados) return;
    dados = j; const n = j.resumo.atrasados + j.resumo.divergencias + j.resumo.erros_stokki;
    $('ent-badge').textContent = n; $('ent-badge').classList.toggle('on', n > 0);
  }).catch(() => {});
  if (secao.dataset.abaInicial === 'entradas') btn.click();
})();
</script>
{% endif %}
```

- [ ] **Step 2: Incluir na tela**

Em `portal_cliente/templates/acompanhamento.html`, depois da linha 220 (`{% include "_envios.html" %}`):

```html
  {# Pedidos de Entrada (25/09) -- só quando entradas_ativo; depende da barra .vistas de _envios.html #}
  {% include "_entradas.html" %}
```

- [ ] **Step 3: Passos do tour**

Em `portal_cliente/templates/_tour_portal.html`:

1. Linha 19, depois de `var envios = ...`: `var entradas = function () { mostrarVista('entradas'); };` e `var temEntradas = !!document.querySelector('.vistas [data-vista="entradas"]');`.
2. Linha 36: `var A = 'Acompanhamento', E = 'Enviar pedidos', N = 'Pedidos de entrada', G = 'Geral';`
3. Linhas 50-52, o passo `.vistas` passa a dizer: `'<p><b>Acompanhamento</b> mostra o que está acontecendo com as suas entregas, dia a dia.</p><p><b>Enviar pedidos</b> é onde você sobe as notas fiscais pra criar novos pedidos de entrega.</p>' + (temEntradas ? '<p><b>Pedidos de entrada</b> é onde você anuncia a mercadoria que vai chegar no galpão da Fresh Log.</p>' : '') + '<p>O número vermelho numa aba avisa que há algo pendente nela.</p>'`.
4. Depois do passo `card('env-pendencias')` (linha 116), acrescentar:

```js
    // ── Pedidos de entrada (só quando a aba existe; alvo ausente = passo pulado) ──
    { alvo: '#ent-zona', capitulo: N, titulo: 'Anunciar mercadoria',
      texto: '<p>Arraste o XML da <b>NF-e de remessa</b> que a sua empresa emitiu pro galpão da Fresh Log, ou a <b>planilha de entrada</b> (uma linha por item).</p><p>Na prévia você informa a <b>data prevista de chegada</b> de cada remessa e confirma. O recebimento é criado na Stokki na hora.</p>',
      antes: function () { chat(false); entradas(); } },
    { alvo: '#ent-tiles', capitulo: N, titulo: 'Resumo das entradas',
      texto: '<ul><li><b>Anunciadas</b>: aguardando chegar.</li><li><b>Atrasadas</b>: passou da data prevista e ainda não chegou.</li><li><b>Chegaram</b>: já no galpão (conferindo ou endereçadas).</li><li><b>Com divergência</b>: chegou menos do que foi anunciado.</li></ul>', antes: entradas },
    { alvo: '#ent-tabela', capitulo: N, titulo: 'Suas entradas',
      texto: '<p>Cada linha é uma remessa anunciada: data prevista, situação e o número do recebimento (<b>#PE</b>) na Stokki. Clique no número de itens pra abrir a lista do que foi anunciado e, depois da chegada, a <b>conferência do galpão</b> por SKU.</p><p><b>Cancelar</b> só enquanto está Anunciada. <b>XML</b> baixa o arquivo original.</p>', antes: entradas },
```

- [ ] **Step 4: Provar no navegador (porta 8099)**

Com o portal local rodando na 8099 (Tarefa 6, Step 6) e a flag ligada pro cliente de teste:

1. Abrir `http://127.0.0.1:8099/?aba=entradas`, logar. Expected: a barra tem três botões e a aba Pedidos de entrada abre sozinha, com tiles zerados.
2. Soltar o XML de teste na zona. Expected: prévia com 1 remessa, campo de data vazio; confirmar sem data mostra a mensagem de erro; com data de amanhã, confirma e a linha aparece como Anunciado / "Na fila pra Stokki".
3. Clicar no número de itens: a linha expande com os SKUs. Clicar Cancelar: modal, confirma, linha vira Cancelado.
4. Clicar "Guia" no topo: os três passos novos aparecem no fim, depois dos de Envios. Trocar pra Acompanhamento e conferir que nada da aba antiga quebrou (tiles, tabela, tour).
5. Abrir o mesmo endereço com a flag desligada (`--desativar`): a barra volta a ter dois botões e `?aba=entradas` cai em Acompanhamento.

Desligar a flag do cliente de teste ao terminar.

---

### Task 8: worker `enviar_entradas_stokki.py` — cria o `#PE` na Stokki e descobre o id

**Files:**
- Create: `portal_cliente/enviar_entradas_stokki.py`
- Create: `infra/portal-cliente-entradas.service`
- Test: `portal_cliente/test_enviar_entradas_stokki.py` (novo)

**Interfaces:**
- Consumes: `entradas` (Tarefa 4), `envio_pedidos.config_stokki_cliente`, `envio_pedidos.xlsx_pedido_stokki`, `enviar_stokki.carregar_config/carregar_importador/_credenciais/_cfg_portal/_erros_da_resposta/_json_ou_none/_escolher_transportadora/_HEADERS_AJAX`, `stokki.recebimentos.listar_recebimentos/extrair_id_da_linha/extrair_codigo_da_linha/extrair_ref_da_linha/ler_detalhe` (Tarefa 1), `stokki.sessao_uso`, do importador `wiz.fazer_login`, `wiz.selecionar_valor_select`, `wiz.aguardar_processamento_completo`.
- Produces: `processar_lote(conn, cnpj, lista, config, simular=False, headless=True) -> dict`, `ciclo(config, simular, headless) -> dict`, `procurar_pe(sessao, client_id, busca, chave_nfe="", referencia="") -> tuple[int | None, str]`, `resultados_do_lote(linhas, chaves, respostas) -> dict[str, dict]`, `_arrival_date(data_prevista) -> str` (dd/mm/aaaa, nunca passada), `executar_wizard_xml(...)` e `executar_wizard_excel(...)` com o contrato `{chave: {"criado", "ja_existia", "erro", "stokki_id", "codigo", "resposta"}}`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `portal_cliente/test_enviar_entradas_stokki.py`:

```python
# -*- coding: utf-8 -*-
"""
Worker da fila de Pedidos de Entrada (cria o #PE na Stokki). Nada aqui
abre navegador nem toca a rede: o wizard é dublê (mock) e a listagem da
Stokki é uma SessaoFalsa.

    py -3.11 -m unittest portal_cliente.test_enviar_entradas_stokki -v
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402
import entradas  # noqa: E402
import enviar_entradas_stokki as worker  # noqa: E402
from test_entradas import CNPJ, AMANHA, xml_remessa  # noqa: E402

CHAVE = "35260912345678000195550010000412211000000017"
HTML_DETALHE = (_RAIZ / "stokki" / "fixtures" / "recebimento_detalhe_cabecalho.html").read_text(encoding="utf-8")


class _Resp:
    def __init__(self, json_data=None, text=""):
        self._json, self.text, self.status_code = json_data, text, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class SessaoFalsa:
    def __init__(self, linhas, html_por_id):
        self.linhas, self.html_por_id, self.chamadas = linhas, html_por_id, []

    def get(self, url, params=None, headers=None):
        self.chamadas.append((url, params))
        if url.endswith("/table"):
            return _Resp(json_data={"aaData": self.linhas})
        if "/show/" in url:
            return _Resp(text=self.html_por_id.get(url.rstrip("/").split("/")[-1], ""))
        raise AssertionError(url)


def _linha(id_stokki, ref):
    return {"id": f'<a href="https://freshlog.stokki.com.br/pt-br/administrator/inventory/incoming/show/{id_stokki}">#PE-{id_stokki}</a>'
                  f'<br><span class="text-muted">{ref}</span>', "client": "X <span>#stkkc-48</span>", "state": "Em transito", "arrival_date": ""}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        raiz = Path(self._tmp.name)
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "dados.db"), mock.patch.object(ep, "_RAIZ", raiz),
                         mock.patch.object(ep, "PASTA_XMLS", raiz / "portal_envios"),
                         mock.patch.object(ep, "PASTA_TEMP", raiz / "portal_envios" / "_temporarios"),
                         mock.patch.object(worker, "_avisar_erros", lambda *a, **k: None)]
        for p in self._patches:
            p.start()
        self.conn = entradas.conectar()
        self.conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER, sender_id INTEGER, email TEXT, notificar_email INTEGER)")
        self.conn.execute("INSERT INTO interno VALUES (?, 'CLIENTE TESTE', 'CLIENTE TESTE', 48, 1, 'c@t.com', 1)", (CNPJ,))
        self.conn.commit()
        entradas.definir_entradas_ativo(self.conn, CNPJ, True)
        self.config = {"portal_cliente": {}}

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _anunciar(self, **kw):
        conteudo = xml_remessa(chave=CHAVE, **kw)
        nfe = entradas.ler_nfe_entrada(conteudo, "nota.xml")
        token = ep.guardar_temporario(conteudo)
        entradas.confirmar_entradas(self.conn, CNPJ, [{"token": token, "data_prevista": AMANHA}], "cliente", {})
        return dict(self.conn.execute("SELECT * FROM portal_entradas ORDER BY id DESC LIMIT 1").fetchone())

    def _fila(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM portal_entradas WHERE stokki_status = 'NA_FILA' AND status = 'ANUNCIADO'")]


class TestSimulado(Base):
    def test_simular_marca_criado_sem_id(self):
        self._anunciar()
        r = worker.processar_lote(self.conn, CNPJ, self._fila(), self.config, simular=True)
        self.assertEqual(r["criados"], 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["stokki_status"], e["stokki_id"], e["status"]), ("CRIADO", None, "ANUNCIADO"))


class TestWizardDublado(Base):
    def _rodar(self, resultado_xml):
        with mock.patch.object(worker, "carregar_importador", return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(worker, "_credenciais", return_value=("u", "s")), \
             mock.patch.object(worker.sessao_uso, "adquirir", return_value=True), \
             mock.patch.object(worker.sessao_uso, "liberar"), \
             mock.patch.object(worker, "executar_wizard_xml", return_value=resultado_xml) as wiz:
            r = worker.processar_lote(self.conn, CNPJ, self._fila(), self.config)
        return r, wiz

    def test_criado_grava_pe(self):
        self._anunciar()
        r, wiz = self._rodar({CHAVE: {"criado": True, "ja_existia": False, "erro": "", "stokki_id": 2497, "codigo": "#PE-2497", "resposta": "{}"}})
        self.assertEqual(r["criados"], 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["stokki_status"], e["stokki_id"], e["stokki_codigo"]), ("CRIADO", 2497, "#PE-2497"))
        # o wizard recebeu a data prevista no formato da Stokki e o arquivo por chave
        _cfg, _u, _s, lote, arquivos = wiz.call_args.args[:5]
        self.assertEqual(lote[0]["chave_nfe"], CHAVE)
        self.assertTrue(arquivos[CHAVE].name.endswith(f"{CHAVE}.xml"))

    def test_ja_existe_na_stokki_vira_criado_sem_criar(self):
        self._anunciar()
        r, _ = self._rodar({CHAVE: {"criado": False, "ja_existia": True, "erro": "", "stokki_id": 2400, "codigo": "#PE-2400", "resposta": ""}})
        self.assertEqual(r["ja_existiam"], 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["stokki_status"], e["stokki_id"]), ("CRIADO", 2400))

    def test_recusa_da_stokki_vira_erro_com_mensagem(self):
        self._anunciar()
        r, _ = self._rodar({CHAVE: {"criado": False, "ja_existia": False, "erro": "CNPJ inválido", "stokki_id": None, "codigo": "", "resposta": '{"errors":["CNPJ inválido"]}'}})
        self.assertEqual(r["erros"], 1)
        e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
        self.assertEqual((e["stokki_status"], e["stokki_erro"], e["status"]), ("ERRO", "CNPJ inválido", "ANUNCIADO"))

    def test_falha_tecnica_volta_pra_fila_e_na_terceira_vira_erro(self):
        self._anunciar()
        for tentativa in (1, 2, 3):
            with mock.patch.object(worker, "carregar_importador", return_value=(mock.Mock(), mock.Mock())), \
                 mock.patch.object(worker, "_credenciais", return_value=("u", "s")), \
                 mock.patch.object(worker.sessao_uso, "adquirir", return_value=True), \
                 mock.patch.object(worker.sessao_uso, "liberar"), \
                 mock.patch.object(worker, "executar_wizard_xml", side_effect=RuntimeError("playwright caiu")):
                worker.processar_lote(self.conn, CNPJ, self._fila(), self.config)
            e = dict(self.conn.execute("SELECT * FROM portal_entradas").fetchone())
            self.assertEqual(e["stokki_tentativas"], tentativa)
            self.assertEqual(e["stokki_status"], "ERRO" if tentativa == 3 else "NA_FILA")

    def test_trava_ocupada_adia_sem_tocar(self):
        self._anunciar()
        with mock.patch.object(worker, "carregar_importador", return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(worker.sessao_uso, "adquirir", return_value=False), \
             mock.patch.object(worker.sessao_uso, "em_uso", return_value="outro"), \
             mock.patch.object(worker, "executar_wizard_xml") as wiz:
            r = worker.processar_lote(self.conn, CNPJ, self._fila(), {"portal_cliente": {"espera_stokki_minutos": 0}})
        self.assertEqual(r["adiados"], 1)
        wiz.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT stokki_status FROM portal_entradas").fetchone()[0], "NA_FILA")


class TestCiclo(Base):
    def test_ciclo_pega_so_anunciadas_na_fila(self):
        self._anunciar()
        self.conn.execute("UPDATE portal_entradas SET status = 'CANCELADO'")
        self.conn.commit()
        with mock.patch.object(worker, "processar_lote") as pl:
            r = worker.ciclo(self.config, simular=True)
        pl.assert_not_called()
        self.assertEqual(r["lotes"], 0)

    def test_enviando_orfao_volta_pra_fila(self):
        self._anunciar()
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'ENVIANDO', atualizado_em = '2026-01-01 00:00:00'")
        self.conn.commit()
        with mock.patch.object(worker, "processar_lote", return_value={"criados": 0, "ja_existiam": 0, "erros": 0, "adiados": 0}) as pl:
            worker.ciclo(self.config, simular=True)
        self.assertEqual(pl.call_args.args[2][0]["stokki_status"], "NA_FILA")


class TestPuros(unittest.TestCase):
    def test_arrival_date_nunca_passada(self):
        hoje = date.today()
        self.assertEqual(worker._arrival_date((hoje - timedelta(days=1)).isoformat()), hoje.strftime("%d/%m/%Y"))
        self.assertEqual(worker._arrival_date((hoje + timedelta(days=2)).isoformat()), (hoje + timedelta(days=2)).strftime("%d/%m/%Y"))
        self.assertEqual(worker._arrival_date(""), hoje.strftime("%d/%m/%Y"))

    def test_resultados_do_lote_resposta_http_manda(self):
        linhas = [{"nome": f"{CHAVE}.xml (12.3 KB)", "invoice": CHAVE, "barra_texto": "Pedido criado", "barra_classe": "progress-bar bg-success", "erros": []}]
        respostas = {CHAVE: {"status": 422, "body": '{"errors": {"po": ["Chave da NFe já utilizada"]}}'}}
        r = worker.resultados_do_lote(linhas, [CHAVE], respostas)
        self.assertFalse(r[CHAVE]["criado"])
        self.assertIn("já utilizada", r[CHAVE]["erro"])
        respostas = {CHAVE: {"status": 200, "body": '{"success":true}'}}
        self.assertTrue(worker.resultados_do_lote(linhas, [CHAVE], respostas)[CHAVE]["criado"])
        # sem resposta capturada e sem linha na tela: não criado (melhor tentar de novo)
        r = worker.resultados_do_lote([], [CHAVE], {})
        self.assertFalse(r[CHAVE]["criado"])

    def test_procurar_pe_pela_chave_so_abre_o_detalhe_com_a_mesma_ref(self):
        sess = SessaoFalsa([_linha(2400, "41000"), _linha(2497, "41221")], {"2497": HTML_DETALHE, "2400": "<html>outra</html>"})
        id_stokki, codigo = worker.procurar_pe(sess, "48", "41221", chave_nfe=CHAVE)
        self.assertEqual((id_stokki, codigo), (2497, "#PE-2497"))
        self.assertEqual([u for u, _ in sess.chamadas if "/show/" in u], ["https://freshlog.stokki.com.br/pt-br/administrator/inventory/incoming/show/2497"])
        self.assertEqual(sess.chamadas[0][1]["client"], "48")

    def test_procurar_pe_por_referencia_nao_abre_detalhe(self):
        sess = SessaoFalsa([_linha(2500, "REM-9")], {})
        self.assertEqual(worker.procurar_pe(sess, "48", "REM-9", referencia="REM-9"), (2500, "#PE-2500"))
        self.assertFalse(any("/show/" in u for u, _ in sess.chamadas))

    def test_procurar_pe_sem_par(self):
        sess = SessaoFalsa([_linha(2400, "41000")], {"2400": "<html></html>"})
        self.assertEqual(worker.procurar_pe(sess, "48", "41221", chave_nfe=CHAVE), (None, ""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `py -3.11 -m unittest portal_cliente.test_enviar_entradas_stokki -v`
Expected: `ModuleNotFoundError: No module named 'enviar_entradas_stokki'`.

- [ ] **Step 3: Implementar o worker**

Criar `portal_cliente/enviar_entradas_stokki.py`:

```python
# -*- coding: utf-8 -*-
"""
portal_cliente/enviar_entradas_stokki.py

Worker da fila de Pedidos de Entrada (25/09/2026): pega as entradas
ANUNCIADO / NA_FILA em portal_entradas e cria o recebimento (#PE) na
Stokki. Mesmo desenho de enviar_stokki.py (fila de saída): trava
cooperativa stokki/sessao_uso.py, Playwright no wizard, 3 tentativas
técnicas antes de ERRO, recusa da Stokki vira ERRO na hora com o texto
na aba do cliente.

Sondagem de 24-25/09 (spec, seção 8.1): a Stokki cria recebimento por
  - inventory/incoming/xml/multiple/create -> POST incoming/xml/multiple/store
    (XML da NF-e; gêmeo do sale/xml do outbound; o JS da página lê a
    NF-e e manda po = nº da NF, invoice = chave, sku[]/quantity[]);
  - inventory/incoming/create/excel/incoming -> POST incoming/excel/store
    (planilha SKU|Quantidade|Valor Unitário, o mesmo modelo do outbound).

Por entrada, o resultado é:
  CRIADO      -- 2xx no store; o #PE é descoberto em seguida pela chave
                 (procurar_pe) e gravado em stokki_id/stokki_codigo;
  CRIADO (já existia) -- antes de subir, procurar_pe achou um #PE com a
                 mesma chave (resposta perdida numa rodada anterior, ou a
                 equipe criou à mão): não cria de novo;
  ERRO        -- 4xx com errors, ou 3 falhas técnicas seguidas.

NUNCA sonda URL da Stokki por adivinhação (URL inexistente redireciona
pro /login e o auth.py refaz o login, derrubando as outras sessões).

COMO RODAR:
    py -3 portal_cliente/enviar_entradas_stokki.py --loop              # serviço (VPS)
    py -3 portal_cliente/enviar_entradas_stokki.py --uma-vez           # um ciclo
    py -3 portal_cliente/enviar_entradas_stokki.py --uma-vez --simular # sem tocar a Stokki
    py -3 portal_cliente/enviar_entradas_stokki.py --uma-vez --visivel # navegador na tela
"""
import argparse
import json
import logging
import re
import shutil
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_AQUI = Path(__file__).parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import entradas as en
import envio_pedidos as ep
import enviar_stokki as fila_saida   # reaproveita config, importador, credenciais e helpers do wizard Excel
from email_utils import enviar_email, envelope_html
from stokki import recebimentos as stokki_recebimentos
from stokki import sessao_uso

logger = logging.getLogger("portal_entradas")

DONO_TRAVA = "portal-entradas"
INTERVALO_LOOP_SEGUNDOS = 20
MAX_TENTATIVAS_TECNICAS = 3
URL_BASE = "https://freshlog.stokki.com.br"
URL_WIZARD_XML = f"{URL_BASE}/pt-br/administrator/inventory/incoming/xml/multiple/create"
URL_WIZARD_EXCEL = f"{URL_BASE}/pt-br/administrator/inventory/incoming/create/excel/incoming"
URL_STORE_EXCEL = f"{URL_BASE}/pt-br/administrator/inventory/incoming/excel/store"
URL_CLIENTE_EXCEL = f"{URL_BASE}/pt-br/administrator/inventory/incoming/create/excel/client/{{client_id}}"
_HEADERS_AJAX = fila_saida._HEADERS_AJAX

carregar_config = fila_saida.carregar_config
carregar_importador = fila_saida.carregar_importador
_credenciais = fila_saida._credenciais
_cfg_portal = fila_saida._cfg_portal


def _pasta_lotes() -> Path:
    return ep._RAIZ / "dados" / "portal_entradas" / "_lotes"


def _arrival_date(data_prevista: str) -> str:
    """dd/mm/aaaa pro wizard, nunca no passado (o datepicker da Stokki tem
    minDate = hoje): anunciou pra ontem e o worker rodou hoje -> hoje."""
    hoje = date.today()
    try:
        d = date.fromisoformat(str(data_prevista or ""))
    except ValueError:
        d = hoje
    return max(d, hoje).strftime("%d/%m/%Y")


# ── Hook no XMLHttpRequest (mesma técnica de importar_stokki.py) ──────────────

_JS_HOOK_XHR = """
(() => {
  window.__stokki_respostas_incoming = [];
  const send = XMLHttpRequest.prototype.send;
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m, u) { this.__url = String(u || ''); return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(body) {
    try {
      if (this.__url.indexOf('/incoming/xml/multiple/store') !== -1) {
        let invoice = null;
        if (body && typeof body.get === 'function') invoice = body.get('invoice');
        const xhr = this;
        this.addEventListener('loadend', function() {
          window.__stokki_respostas_incoming.push({ invoice: invoice, status: xhr.status, body: String(xhr.responseText || '').slice(0, 1000) });
        });
      }
    } catch (e) {}
    return send.apply(this, arguments);
  };
})();
"""

_JS_LINHAS = """
() => Array.from(document.querySelectorAll('form.form_incoming')).map(f => {
    const nome = (f.querySelector('.col-1') || {}).textContent || '';
    const barra = f.querySelector('.progress-bar');
    let erros = Array.from(f.querySelectorAll('[id^=validation_errors_order] li')).map(e => e.textContent.trim()).filter(Boolean);
    if (!erros.length) erros = Array.from(f.querySelectorAll('[id^=validation_errors_order] .alert')).map(e => e.textContent.trim()).filter(Boolean);
    const invoice = (f.querySelector('input[name=invoice]') || {}).value || '';
    return { nome: nome.trim(), invoice: invoice, barra_texto: barra ? barra.textContent.trim() : '',
             barra_classe: barra ? barra.className : '', erros: Array.from(new Set(erros)) };
})
"""


def _criado_da_resposta(resp: dict | None) -> tuple[bool | None, str]:
    """(criado, motivo) da resposta HTTP capturada; None sem resposta."""
    if not resp:
        return None, ""
    body = resp.get("body", "")
    try:
        dados = json.loads(body)
    except Exception:
        dados = None
    if isinstance(dados, dict):
        erros = dados.get("errors") or dados.get("error") or []
        if isinstance(erros, dict):
            erros = [m for v in erros.values() for m in (v if isinstance(v, list) else [v])]
        if isinstance(erros, str):
            erros = [erros]
        if erros:
            return False, "; ".join(str(e).strip() for e in erros)
    if 200 <= int(resp.get("status", 0)) < 300:
        return True, ""
    return False, f"HTTP {resp.get('status')}: {body[:200].strip() or 'sem corpo'}"


def resultados_do_lote(linhas: list[dict], chaves: list[str], respostas: dict) -> dict[str, dict]:
    """{chave: {criado, erro}} cruzando a tela (barra verde/vermelha) com a
    resposta HTTP do store. A resposta manda; a tela só confirma. Sem os
    dois: não criado (a Stokki recusa como duplicado se já existir)."""
    saida = {}
    for chave in chaves:
        linha = next((l for l in linhas if l.get("invoice") == chave or l.get("nome", "").startswith(chave)), None)
        criado_http, motivo_http = _criado_da_resposta(respostas.get(chave))
        if linha is None and criado_http is None:
            saida[chave] = {"criado": False, "erro": "linha do arquivo não encontrada na tela da Stokki"}
            continue
        criado_tela, motivo_tela = None, ""
        if linha is not None:
            texto, classe = linha["barra_texto"].lower(), linha["barra_classe"]
            criado_tela = ("pedido criado" in texto or "bg-success" in classe) and "bg-danger" not in classe
            motivo_tela = "; ".join(linha["erros"]) or linha["barra_texto"]
        if criado_http is None:
            criado, motivo = bool(criado_tela), motivo_tela or "sem mensagem na tela"
        else:
            criado = criado_http and (criado_tela is not False)
            motivo = motivo_http or motivo_tela or ("" if criado else "sem mensagem na tela")
        saida[chave] = {"criado": criado, "erro": "" if criado else motivo}
    return saida


# ── Descoberta do #PE pela listagem (sessão do próprio navegador) ─────────────

class _SessaoNavegador:
    """Adapta page.context.request ao contrato .get() de stokki.recebimentos
    (mesmo truque de enviar_stokki._buscar_codigos_na_listagem)."""

    def __init__(self, page):
        self._page = page

    def get(self, url, params=None, headers=None):
        r = self._page.context.request.get(url, params=params or {}, headers=headers or {})

        class _R:
            status_code = r.status
            text = r.text()

            def raise_for_status(self):
                if not r.ok:
                    raise RuntimeError(f"HTTP {r.status}")

            def json(self):
                return r.json()
        return _R()


def procurar_pe(sessao, client_id: str, busca: str, chave_nfe: str = "", referencia: str = "") -> tuple[int | None, str]:
    """(id_stokki, '#PE-n') do recebimento do cliente que bate com a chave
    NF-e (XML: abre o detalhe só das linhas cuja Ref. do Pedido = nº da NF)
    ou com a referência (planilha: Ref. do Pedido da própria linha). Sem
    par: (None, '')."""
    vistos = set()
    for tentativa in ({"busca": busca, "por_pagina": 10}, {"busca": "", "por_pagina": 20}):
        try:
            dados = stokki_recebimentos.listar_recebimentos(sessao, cliente=str(client_id), **tentativa)
        except Exception as e:  # noqa: BLE001
            logger.info(f"   (listagem de recebimentos falhou: {e})")
            return None, ""
        for linha in dados.get("aaData") or []:
            id_stokki = stokki_recebimentos.extrair_id_da_linha(linha)
            if not id_stokki or id_stokki in vistos:
                continue
            vistos.add(id_stokki)
            ref = stokki_recebimentos.extrair_ref_da_linha(linha).strip().upper()
            codigo = stokki_recebimentos.extrair_codigo_da_linha(linha)
            if referencia and not chave_nfe:
                if ref == str(referencia).strip().upper():
                    return id_stokki, codigo
                continue
            if busca and ref and ref != str(busca).strip().upper():
                continue   # outro número de NF: nem abre o detalhe
            try:
                det = stokki_recebimentos.ler_detalhe(sessao, id_stokki)
            except Exception as e:  # noqa: BLE001
                logger.info(f"   (detalhe do #PE {id_stokki} falhou: {e})")
                continue
            if chave_nfe and det["chave_nfe"] == chave_nfe:
                return id_stokki, codigo
        if not busca:
            break
    return None, ""


# ── Wizard XML múltiplo ────────────────────────────────────────────────────────

def _novo_navegador(p, wiz, usuario: str, senha: str, headless: bool):
    browser = p.chromium.launch(headless=headless)
    context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    context.add_init_script(_JS_HOOK_XHR)
    page = context.new_page()
    wiz.fazer_login(page, usuario, senha, modo_automatico=True)
    return browser, page


def executar_wizard_xml(cfg: dict, usuario: str, senha: str, lote: list[dict], arquivos: dict[str, Path], pasta_logs: Path, wiz,
                        headless: bool = True) -> dict[str, dict]:
    """Um wizard por data prevista (o formulário tem UM arrival_date pro
    lote). Antes de subir, procura #PE já existente pela chave. Devolve
    {chave: {criado, ja_existia, erro, stokki_id, codigo, resposta}}."""
    from playwright.sync_api import sync_playwright, Error as PlaywrightError

    pasta_logs.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saida: dict[str, dict] = {}
    with sync_playwright() as p:
        browser, page = _novo_navegador(p, wiz, usuario, senha, headless)
        try:
            sessao = _SessaoNavegador(page)
            a_subir: list[dict] = []
            for e in lote:
                id_pe, codigo = procurar_pe(sessao, cfg["client_id"], e.get("numero_nf") or "", chave_nfe=e["chave_nfe"])
                if id_pe:
                    logger.info(f"   {en.rotulo_entrada(e)} já existe na Stokki ({codigo}) -- não cria de novo")
                    saida[e["chave_nfe"]] = {"criado": False, "ja_existia": True, "erro": "", "stokki_id": id_pe, "codigo": codigo, "resposta": ""}
                else:
                    a_subir.append(e)
            por_data: dict[str, list[dict]] = {}
            for e in a_subir:
                por_data.setdefault(_arrival_date(e.get("data_prevista")), []).append(e)
            for data_br, grupo in por_data.items():
                caminhos = [str(arquivos[e["chave_nfe"]]) for e in grupo]
                page.goto(URL_WIZARD_XML, wait_until="networkidle")
                page.wait_for_timeout(1500)
                wiz.selecionar_valor_select(page, "#client_id", cfg["client_id"])
                wiz.selecionar_valor_select(page, "#warehouse_id", cfg["warehouse_id"], aguardar_opcoes=True)
                wiz.selecionar_valor_select(page, "#type_transport", cfg["tipo_transporte"])
                page.wait_for_timeout(500)
                wiz.selecionar_valor_select(page, "#packaging", cfg["embalagem"])
                page.wait_for_timeout(300)
                if data_br == date.today().strftime("%d/%m/%Y"):
                    page.evaluate("() => { const c = document.querySelector('#same_day_receipt'); if (c && !c.checked) { c.checked = true; c.dispatchEvent(new Event('change')); } }")
                else:
                    page.evaluate("(v) => { const el = document.querySelector('#arrival_date'); el.value = v; el.dispatchEvent(new Event('change')); }", data_br)
                page.wait_for_timeout(300)
                page.set_input_files("#input_drop_file", caminhos)
                page.wait_for_timeout(2000)
                page.get_by_role("link", name="Próximo").click()
                page.wait_for_selector("text=Arquivos anexados", timeout=30000)
                page.wait_for_timeout(1500)
                page.screenshot(path=str(pasta_logs / f"{ts}_{data_br.replace('/', '-')}_arquivos.png"), full_page=True)
                try:
                    page.get_by_role("button", name="Criar todos pedidos").click()
                except PlaywrightError:
                    page.get_by_role("button", name="Criar pedido").first.click()
                wiz.aguardar_processamento_completo(page, esperado=len(caminhos))
                page.screenshot(path=str(pasta_logs / f"{ts}_{data_br.replace('/', '-')}_final.png"), full_page=True)
                try:
                    capturadas = page.evaluate("() => window.__stokki_respostas_incoming || []")
                except PlaywrightError:
                    capturadas = []
                respostas = {i["invoice"]: {"status": i["status"], "body": i["body"]} for i in capturadas if i.get("invoice")}
                try:
                    linhas = page.evaluate(_JS_LINHAS)
                except PlaywrightError:
                    linhas = []
                res = resultados_do_lote(linhas, [e["chave_nfe"] for e in grupo], respostas)
                for e in grupo:
                    r = res[e["chave_nfe"]]
                    item = {"criado": r["criado"], "ja_existia": False, "erro": r["erro"], "stokki_id": None, "codigo": "",
                            "resposta": (respostas.get(e["chave_nfe"]) or {}).get("body", "")}
                    if r["criado"]:
                        item["stokki_id"], item["codigo"] = procurar_pe(sessao, cfg["client_id"], e.get("numero_nf") or "", chave_nfe=e["chave_nfe"])
                    saida[e["chave_nfe"]] = item
        finally:
            browser.close()
    return saida


# ── Wizard Excel (remessas de planilha) ───────────────────────────────────────

def _primeira_origem(dados) -> str:
    """origin_id do JSON de create/excel/client/<id>, formato desconhecido:
    procura uma lista com 'id' em chaves plausíveis. '' se não houver."""
    if not isinstance(dados, dict):
        return ""
    for chave in ("origins", "origin", "addresses", "address", "data"):
        v = dados.get(chave)
        if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("id") not in (None, ""):
            return str(v[0]["id"])
        if isinstance(v, dict) and v.get("id") not in (None, ""):
            return str(v["id"])
    return ""


def executar_wizard_excel(cfg: dict, usuario: str, senha: str, lote: list[dict], arquivos: dict[str, Path], pasta_logs: Path, wiz,
                          headless: bool = True) -> dict[str, dict]:
    from playwright.sync_api import sync_playwright

    pasta_logs.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saida: dict[str, dict] = {}
    with sync_playwright() as p:
        browser, page = _novo_navegador(p, wiz, usuario, senha, headless)
        try:
            sessao = _SessaoNavegador(page)
            page.goto(URL_WIZARD_EXCEL, wait_until="networkidle")
            page.wait_for_timeout(1500)
            token_csrf = page.evaluate("() => (document.querySelector('meta[name=csrf-token]') || {}).content || ''") \
                or page.evaluate("() => (document.querySelector('input[name=_token]') || {}).value || ''")
            wiz.selecionar_valor_select(page, "#client_id", cfg["client_id"])
            page.wait_for_timeout(1500)
            try:
                r = page.context.request.get(URL_CLIENTE_EXCEL.format(client_id=cfg["client_id"]), headers=_HEADERS_AJAX, timeout=30000)
                origem_id = _primeira_origem(fila_saida._json_ou_none(r)) if r.ok else ""
            except Exception as e:  # noqa: BLE001
                logger.info(f"   (origem do cliente no wizard Excel indisponível: {e})")
                origem_id = ""
            carrier = fila_saida._escolher_transportadora(page, cfg)
            page.screenshot(path=str(pasta_logs / f"{ts}_excel_form.png"), full_page=True)
            for e in lote:
                chave = e["chave_nfe"]
                id_pe, codigo = procurar_pe(sessao, cfg["client_id"], e.get("referencia") or "", referencia=e.get("referencia") or "")
                if id_pe:
                    saida[chave] = {"criado": False, "ja_existia": True, "erro": "", "stokki_id": id_pe, "codigo": codigo, "resposta": ""}
                    continue
                data_br = _arrival_date(e.get("data_prevista"))
                campos = {
                    "_token": token_csrf, "position": "0", "motion": "incoming", "client_id": cfg["client_id"],
                    "destination_id": cfg["warehouse_id"], "po": (e.get("referencia") or "")[:60], "origin_id": origem_id,
                    "same_day_receipt": "1" if data_br == date.today().strftime("%d/%m/%Y") else "0",
                    "arrival_date": data_br, "carrier_id": carrier,
                    "file_excel[]": {"name": arquivos[chave].name,
                                     "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                     "buffer": arquivos[chave].read_bytes()},
                }
                try:
                    r = page.context.request.post(URL_STORE_EXCEL, multipart=campos, headers=_HEADERS_AJAX, timeout=120000)
                except Exception as ex:  # noqa: BLE001
                    saida[chave] = {"criado": False, "ja_existia": False, "erro": f"falha ao enviar à Stokki: {ex}", "stokki_id": None, "codigo": "", "resposta": str(ex)}
                    continue
                corpo = (r.text() or "")[:4000]
                if r.ok:
                    id_pe, codigo = procurar_pe(sessao, cfg["client_id"], e.get("referencia") or "", referencia=e.get("referencia") or "")
                    saida[chave] = {"criado": True, "ja_existia": False, "erro": "", "stokki_id": id_pe, "codigo": codigo, "resposta": corpo}
                else:
                    erro = fila_saida._erros_da_resposta(r)
                    if not origem_id and "origin" in erro.lower():
                        erro = "A Stokki exige uma origem cadastrada pra remessa por planilha e o cliente não tem nenhuma -- " \
                               "anuncie pelo XML da NF-e ou peça à Fresh Log pra cadastrar a origem na Stokki. Detalhe: " + erro
                    saida[chave] = {"criado": False, "ja_existia": False, "erro": erro, "stokki_id": None, "codigo": "", "resposta": corpo}
        finally:
            browser.close()
    return saida


# ── Lote ───────────────────────────────────────────────────────────────────────

def _marcar(conn, entrada_id: int, **campos) -> None:
    campos["atualizado_em"] = ep._agora()
    sets = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(f"UPDATE portal_entradas SET {sets} WHERE id = ?", (*campos.values(), entrada_id))


def _falha_tecnica(conn, lote: list[dict], erro: str) -> list[dict]:
    definitivos = []
    for e in lote:
        tentativas = int(e.get("stokki_tentativas") or 0) + 1
        if tentativas >= MAX_TENTATIVAS_TECNICAS:
            _marcar(conn, e["id"], stokki_status=en.STOKKI_ERRO, stokki_tentativas=tentativas,
                    stokki_erro=f"Falha ao criar na Stokki ({tentativas}x): {erro}"[:900])
            definitivos.append({**e, "erro": erro})
        else:
            _marcar(conn, e["id"], stokki_status=en.STOKKI_NA_FILA, stokki_tentativas=tentativas,
                    stokki_erro=f"Tentativa {tentativas} falhou, vai tentar de novo: {erro}"[:900])
    conn.commit()
    return definitivos


def processar_lote(conn, cnpj: str, lote: list[dict], config: dict, simular: bool = False, headless: bool = True) -> dict:
    cfg = ep.config_stokki_cliente(conn, cnpj, config)
    resumo = {"cliente": cfg["nome"], "criados": 0, "ja_existiam": 0, "erros": 0, "adiados": 0}
    if not en.config_entradas_cliente(conn, cnpj)["entradas_ativo"]:
        logger.info(f"[{cfg['nome']}] Pedidos de Entrada desativado -- {len(lote)} entrada(s) ficam na fila.")
        resumo["adiados"] = len(lote)
        return resumo
    if not cfg["client_id"]:
        for e in lote:
            _marcar(conn, e["id"], stokki_status=en.STOKKI_ERRO, stokki_erro="Embarcador sem client_id da Stokki (interno.stkkc_id).")
        conn.commit()
        resumo["erros"] = len(lote)
        return resumo

    ids = [e["id"] for e in lote]
    conn.execute(f"UPDATE portal_entradas SET stokki_status = ?, atualizado_em = ? WHERE id IN ({','.join('?' * len(ids))})",
                 (en.STOKKI_ENVIANDO, ep._agora(), *ids))
    conn.commit()

    pasta_lote = _pasta_lotes() / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{cfg['cnpj']}"
    pasta_lote.mkdir(parents=True, exist_ok=True)
    arquivos: dict[str, Path] = {}
    prontos_xml: list[dict] = []
    prontos_plan: list[dict] = []
    try:
        wiz = None
        if not simular:
            wiz, _px = carregar_importador(config)
        for e in lote:
            try:
                if e["origem"] == ep.ORIGEM_PLANILHA:
                    itens = [dict(r) for r in conn.execute("SELECT sku, quantidade FROM portal_entrada_itens WHERE entrada_id = ? ORDER BY linha", (e["id"],))]
                    if not itens:
                        raise ValueError("remessa de planilha sem itens")
                    destino = pasta_lote / f"{e['chave_nfe']}.xlsx"
                    destino.write_bytes(ep.xlsx_pedido_stokki([{"sku": i["sku"], "quantidade": i["quantidade"], "valor_unitario": None} for i in itens]))
                    prontos_plan.append(e)
                else:
                    destino = pasta_lote / f"{e['chave_nfe']}.xml"
                    shutil.copyfile(en.caminho_arquivo(e), destino)
                    prontos_xml.append(e)
                arquivos[e["chave_nfe"]] = destino
            except Exception as ex:  # noqa: BLE001
                _marcar(conn, e["id"], stokki_status=en.STOKKI_ERRO, stokki_erro=f"Falha ao preparar o arquivo: {ex}"[:900])
                conn.commit()
                resumo["erros"] += 1
        prontos = prontos_xml + prontos_plan
        if not prontos:
            return resumo

        if simular:
            logger.info(f"[{cfg['nome']}] SIMULAÇÃO: {len(prontos)} entrada(s) marcadas como criadas sem tocar a Stokki.")
            resultados = {e["chave_nfe"]: {"criado": True, "ja_existia": False, "erro": "", "stokki_id": None, "codigo": "", "resposta": ""} for e in prontos}
        else:
            espera = int(_cfg_portal(config).get("espera_stokki_minutos") or 45) * 60
            if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=30 * 60, esperar_segundos=espera):
                logger.warning(f"[{cfg['nome']}] Stokki ocupada por '{sessao_uso.em_uso()}' -- lote volta pra fila.")
                conn.execute(f"UPDATE portal_entradas SET stokki_status = ?, atualizado_em = ? WHERE id IN ({','.join('?' * len(prontos))})",
                             (en.STOKKI_NA_FILA, ep._agora(), *[e["id"] for e in prontos]))
                conn.commit()
                resumo["adiados"] = len(prontos)
                return resumo
            try:
                usuario, senha = _credenciais(config)
                resultados = {}
                if prontos_xml:
                    logger.info(f"[{cfg['nome']}] criando {len(prontos_xml)} recebimento(s) por XML na Stokki (client_id={cfg['client_id']})...")
                    resultados.update(executar_wizard_xml(cfg, usuario, senha, prontos_xml, arquivos, pasta_lote, wiz, headless=headless))
                if prontos_plan:
                    logger.info(f"[{cfg['nome']}] criando {len(prontos_plan)} recebimento(s) por planilha na Stokki...")
                    resultados.update(executar_wizard_excel(cfg, usuario, senha, prontos_plan, arquivos, pasta_lote, wiz, headless=headless))
            finally:
                sessao_uso.liberar(DONO_TRAVA)

        erros_definitivos = []
        for e in prontos:
            r = resultados.get(e["chave_nfe"])
            if r is None:
                _falha_tecnica(conn, [e], "a Stokki não devolveu resultado pra esse arquivo")
                resumo["erros"] += 1
                continue
            if r["criado"] or r["ja_existia"]:
                _marcar(conn, e["id"], stokki_status=en.STOKKI_CRIADO, stokki_erro=None, stokki_id=r["stokki_id"],
                        stokki_codigo=r["codigo"] or None)
                resumo["ja_existiam" if r["ja_existia"] else "criados"] += 1
                if r["criado"] and not r["stokki_id"]:
                    logger.info(f"   {en.rotulo_entrada(e)} criado, mas o #PE ainda não foi achado -- o timer do WMS amarra pela chave.")
            else:
                _marcar(conn, e["id"], stokki_status=en.STOKKI_ERRO, stokki_erro=(r["erro"] or "recusado pela Stokki")[:900],
                        stokki_tentativas=int(e.get("stokki_tentativas") or 0) + 1)
                erros_definitivos.append({**e, "erro": r["erro"]})
                resumo["erros"] += 1
        conn.commit()
        if erros_definitivos:
            _avisar_erros(config, cfg, erros_definitivos, "A Stokki recusou a(s) entrada(s) abaixo.")
        logger.info(f"[{cfg['nome']}] lote concluído: {resumo}")
        return resumo
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[{cfg['nome']}] falha técnica no lote: {ex}\n{traceback.format_exc()}")
        pendentes = (prontos_xml + prontos_plan) or lote
        definitivos = _falha_tecnica(conn, pendentes, f"{type(ex).__name__}: {ex}")
        if definitivos:
            _avisar_erros(config, cfg, definitivos, "Não conseguimos criar a(s) entrada(s) abaixo na Stokki depois de 3 tentativas.")
        resumo["erros"] += len(definitivos)
        resumo["adiados"] += len(pendentes) - len(definitivos)
        return resumo


def _avisar_erros(config: dict, cfg: dict, lote: list[dict], cabecalho: str) -> None:
    """Mesmo desenho de enviar_stokki._avisar_erros: e-mail pro cliente com
    cópia pro atendimento. Respeita portal_cliente.envios.forcar_destino
    (default hugo@) enquanto o Hugo não ligar o envio real."""
    if not lote:
        return
    import bloqueio_area
    email_cfg = config.get("email", {}) or {}
    forcar = bloqueio_area.forcar_destino(config)
    destinos = [forcar] if forcar else list(cfg.get("emails") or [])
    atendimento = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
    cc = [] if forcar else ([atendimento] if atendimento and atendimento not in destinos else [])
    if not destinos and not cc:
        return
    url = (_cfg_portal(config).get("url_base") or "https://app.freshhub.com.br/cliente").rstrip("/")
    linhas = "".join(f"<tr><td style='padding:6px 10px;border-bottom:1px solid #E5E7EB'><b>{en.rotulo_entrada(e)}</b></td>"
                     f"<td style='padding:6px 10px;border-bottom:1px solid #E5E7EB;color:#B91C1C'>{(e.get('erro') or '')[:300]}</td></tr>" for e in lote)
    corpo = envelope_html(f"<p>Olá, <strong>{cfg['nome']}</strong>.</p><p>{cabecalho}</p>"
                          f"<table style='border-collapse:collapse;font-size:13px;width:100%'><tr><th align='left' style='padding:6px 10px'>Entrada</th>"
                          f"<th align='left' style='padding:6px 10px'>Motivo</th></tr>{linhas}</table>"
                          f"<p style='margin-top:20px'>Você pode cancelar e anunciar de novo pelo portal: <a href='{url}/?aba=entradas'>{url}</a>.</p>",
                          rodape="Fresh Log · Portal do cliente · pedidos de entrada", cor_acento="#EF4444")
    enviar_email(destinos or cc, f"Fresh Log · Entrada(s) não criada(s) na Stokki ({len(lote)})", corpo, email_cfg, cc=cc if destinos else None)


# ── Ciclo ──────────────────────────────────────────────────────────────────────

def ciclo(config: dict, simular: bool = False, headless: bool = True) -> dict:
    ep.limpar_temporarios()
    conn = en.conectar()
    try:
        lote_max = int(_cfg_portal(config).get("lote_maximo") or 30)
        conn.execute("UPDATE portal_entradas SET stokki_status = 'NA_FILA', atualizado_em = datetime('now','localtime') "
                     "WHERE stokki_status = 'ENVIANDO' AND atualizado_em < datetime('now','localtime','-30 minutes')")
        conn.commit()
        rows = conn.execute("SELECT * FROM portal_entradas WHERE stokki_status = 'NA_FILA' AND status = 'ANUNCIADO' "
                            "ORDER BY cnpj_embarcador, data_prevista, id").fetchall()
        por_cliente: dict[str, list[dict]] = {}
        for r in rows:
            por_cliente.setdefault(r["cnpj_embarcador"], []).append(dict(r))
        total = {"lotes": 0, "criados": 0, "ja_existiam": 0, "erros": 0, "adiados": 0}
        for cnpj, lote in por_cliente.items():
            for i in range(0, len(lote), lote_max):
                resumo = processar_lote(conn, cnpj, lote[i:i + lote_max], config, simular=simular, headless=headless)
                total["lotes"] += 1
                for k in ("criados", "ja_existiam", "erros", "adiados"):
                    total[k] += resumo.get(k, 0)
        return total
    finally:
        conn.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Worker da fila de Pedidos de Entrada do portal (cria o #PE na Stokki)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--loop", action="store_true", help="roda pra sempre, um ciclo a cada 20 s (serviço)")
    g.add_argument("--uma-vez", action="store_true", help="um ciclo só")
    p.add_argument("--simular", action="store_true", help="não toca a Stokki: marca como criado (teste local)")
    p.add_argument("--visivel", action="store_true", help="abre o navegador na tela (debug)")
    args = p.parse_args(argv)
    config = carregar_config()
    if args.loop:
        logger.info(f"worker de entradas iniciado (ciclo a cada {INTERVALO_LOOP_SEGUNDOS}s{' · SIMULAÇÃO' if args.simular else ''})")
        while True:
            try:
                r = ciclo(config, simular=args.simular, headless=not args.visivel)
                if r["lotes"]:
                    logger.info(f"ciclo: {r}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"ciclo falhou: {e}\n{traceback.format_exc()}")
            time.sleep(INTERVALO_LOOP_SEGUNDOS)
    r = ciclo(config, simular=args.simular, headless=not args.visivel)
    logger.info(f"ciclo: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Criar a unit**

Criar `infra/portal-cliente-entradas.service` (cópia da `portal-cliente-envios.service` que está na VPS, ajustada):

```ini
[Unit]
Description=Fresh Log - Portal do cliente: worker da fila de Pedidos de Entrada (cria o #PE na Stokki; portal_cliente/enviar_entradas_stokki.py --loop)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/stokki-eventos
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=/opt/stokki-eventos/venv/bin/python /opt/stokki-eventos/portal_cliente/enviar_entradas_stokki.py --loop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 5: Rodar e ver passar**

Run: `py -3.11 -m unittest portal_cliente.test_enviar_entradas_stokki portal_cliente.test_entradas -v`
Expected: todos PASS. Se `import enviar_stokki` exigir algo do `config.yaml` na importação, veja `enviar_stokki.py:66-72`: ele só configura logging no import, não lê config; se algum teste quebrar por causa do `bloqueio_area` em `_avisar_erros`, o `mock.patch.object(worker, "_avisar_erros", ...)` do `Base.setUp` já cobre.

- [ ] **Step 6: Compilar**

Run: `py -3.11 -m py_compile portal_cliente/enviar_entradas_stokki.py portal_cliente/test_enviar_entradas_stokki.py`

---

### Task 9: deploy e prova em produção (com o Hugo)

**Files:** nenhum arquivo novo. Esta tarefa é o roteiro de prova real; cada passo só começa quando o Hugo pedir commit/deploy.

**Interfaces:**
- Consumes: tudo das Tarefas 1-8; skill `deploy-vps` (`.claude/scripts/vps_ops.py`), skill `vps` (`vps_ler.py`).

- [ ] **Step 1: Testes completos e git limpo**

Run: `py -3.11 -m unittest stokki.test_recebimentos portal_cliente.test_entradas portal_cliente.test_enviar_entradas_stokki test_sincronizar_recebimentos_wms -v && (cd painel_agentes && py -3.11 -m unittest test_wms_pedidos -v) && git status --short`
Expected: tudo PASS; só os arquivos deste plano modificados/novos.

- [ ] **Step 2: Commit (quando o Hugo pedir)**

```bash
git add stokki/recebimentos.py stokki/test_recebimentos.py stokki/fixtures/recebimento_detalhe_cabecalho.html \
  portal_cliente/envio_pedidos.py portal_cliente/entradas.py portal_cliente/test_entradas.py \
  portal_cliente/enviar_entradas_stokki.py portal_cliente/test_enviar_entradas_stokki.py \
  portal_cliente/app.py portal_cliente/gerenciar_clientes.py portal_cliente/templates/_entradas.html \
  portal_cliente/templates/acompanhamento.html portal_cliente/templates/_tour_portal.html \
  painel_agentes/wms_pedidos.py painel_agentes/painel_agentes.py painel_agentes/templates/wms.html \
  sincronizar_recebimentos_wms.py test_sincronizar_recebimentos_wms.py infra/portal-cliente-entradas.service \
  docs/superpowers/specs/2026-09-24-pedidos-de-entrada-portal-design.md docs/superpowers/plans/2026-09-25-pedidos-de-entrada-portal.md
git commit -m "Portal: aba Pedidos de Entrada -- anuncio de remessa vira #PE na Stokki e entrada esperada no WMS

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

Push e merge no `master` seguem a regra da memória `feedback_commit_indice_ramo_errado` (SHA conferido, push num comando separado).

- [ ] **Step 3: Deploy**

Run: `py -3.11 .claude/scripts/vps_ler.py git` (working tree da VPS limpo?), depois
`py -3.11 .claude/scripts/vps_ops.py deploy --restart portal-cliente painel-agentes --units`
Expected: pull OK, `portal-cliente` e `painel-agentes` `active`. O worker de entradas **não** é habilitado ainda (`--timers` vazio; `enable` só no Step 6).

- [ ] **Step 4: Ligar a aba pro piloto e provar a tela**

Run: `py -3.11 .claude/scripts/vps_ops.py rodar --cwd portal_cliente --real gerenciar_clientes.py entradas 22135070000190 --ativar`
Expected: `Pedidos de Entrada: ATIVO`, sem ATENÇÃO (stkkc 48 = piloto).

Prova: o Hugo (ou a equipe, em nome da Maria Dolores) abre `app.freshhub.com.br/cliente/?aba=entradas` e sobe **um** XML de remessa **novo** (uma nota que ainda não virou `#PE` na Stokki), combinado com o Hugo, com data prevista real. Não usar uma nota antiga já recebida: a chave já existe na Stokki e o worker só marcaria "já existia", sem provar a criação. Expected: linha ANUNCIADO / "Na fila pra Stokki" em `portal_entradas` (`vps_ler.py sql "SELECT id, numero_nf, status, stokki_status, data_prevista FROM portal_entradas"`).

- [ ] **Step 5: Worker contra a Stokki real, uma nota só**

`--simular` não toca a Stokki (só marca CRIADO no banco), então **não** prova nada em produção: pule a simulação aqui (ela já foi exercitada nos testes) e vá direto ao passo real. Antes de qualquer `--uma-vez` manual, o serviço `portal-cliente-entradas` tem que estar **parado** (ele só é habilitado no Step 6): dois workers ao mesmo tempo resetam o ENVIANDO um do outro.

O passo que cria de verdade, **uma nota só**, com o Hugo acompanhando:
`py -3.11 .claude/scripts/vps_ops.py rodar --cwd portal_cliente --real enviar_entradas_stokki.py --uma-vez`
Expected: log `criando 1 recebimento(s) por XML`, screenshots em `dados/portal_entradas/_lotes/<ts>_<cnpj>/`, linha com `stokki_status = CRIADO` e `stokki_id`/`stokki_codigo` preenchidos; na Stokki, o `#PE` aparece na listagem de Pedidos de Entrada com Ref. = nº da NF e a chave no detalhe. Se `stokki_id` ficar vazio mas o `#PE` existir, o timer amarra na rodada seguinte (Step 7); anotar no plano o que `procurar_pe` não achou e por quê (olhar `dados/sondagem/` se precisar de HTML).

Se a Stokki recusar (`ERRO` com mensagem): ler a mensagem na aba, corrigir a causa (SKU, CNPJ, regime) e **não** repetir sem entender. Rodar de novo só depois de `stokki_status = NA_FILA` de novo.

- [ ] **Step 6: Habilitar o serviço do worker**

Run: `py -3.11 .claude/scripts/vps_ops.py deploy --units --timers portal-cliente-entradas.service`
(o `--timers` faz `enable --now` da unit; ela é `.service`, não `.timer`, e o script aceita o nome). Depois `py -3.11 .claude/scripts/vps_ler.py log portal-cliente-entradas -n 30`.
Expected: `worker de entradas iniciado (ciclo a cada 20s)`, sem Traceback.

- [ ] **Step 7: Ligação com o WMS e ciclo do cliente**

Run: `py -3.11 .claude/scripts/vps_ops.py disparar stokki-wms-recebimentos`
Expected no log: `amarrados: 1` (ou 0 se a Stokki ainda estiver "Em transito" sem itens: aí a amarração acontece quando a mercadoria chegar e a Stokki marcar Recebido). Conferir:
`vps_ler.py sql "SELECT e.id, e.status, e.stokki_codigo, e.wms_recebimento_id, r.estado, r.data_prevista, r.portal_entrada_id FROM portal_entradas e LEFT JOIN wms_recebimentos r ON r.id = e.wms_recebimento_id"`.

Quando a mercadoria chegar: o galpão vê o recebimento na aba Receber com "prevista dd/mm" e o chip **portal**; ao endereçar tudo, a próxima rodada do timer leva a entrada a ENDERECADO; ao encerrar com divergência, a rota do painel leva a DIVERGENCIA na hora e a aba mostra a conferência por SKU com a falta.

Conferir também, nesta etapa, o que a revisão final deixou pra produção (ledger `.superpowers/sdd/.../progress.md`, linhas `⚠️`): o `#PE` ainda "Em transito" já mostra a chave NF-e no detalhe (senão `procurar_pe` não acha o id antes da chegada)? O timer trata um `#PE` cancelado sem virar `erros` a cada 30 min? `same_day_receipt` tem custo comercial na Stokki (o worker marca sozinho quando a data prevista é hoje ou já passou)?

- [ ] **Step 7b: Cancelamento com chamado e smoke de Envios**

Com um `#PE` ainda em trânsito, o cliente cancela pela aba: expected linha CANCELADO, chamado aberto no atendimento com o `#PE` (ou "sem #PE identificado") e o motivo digitado; `wms_recebimentos.estado = 'CANCELADO'` só se ainda ESPERADO sem endereçamento. A equipe cancela o `#PE` na Stokki à mão e fecha o chamado.

Smoke de Envios (a aba antiga compartilha `ler_nfe` e `_achar_cabecalho`): subir na aba Enviar pedidos um XML de saída e uma planilha de saída até a prévia e descartar. Expected: prévia igual à de antes do deploy.

- [ ] **Step 7c: Remessa por planilha (só depois do XML provado)**

O worker manda `origin_id` vazio no wizard Excel (a URL `create/excel/client/<id>` foi tirada por nunca ter sido sondada). Anunciar UMA remessa por planilha e ver o que a Stokki responde: criou → ok; recusou por origem → a entrada fica ERRO com a mensagem clara, e aí decidir com o Hugo (cadastrar a origem na Stokki, ou sondar read-only o que o wizard oferece em `#origin_search`) antes de liberar planilha pro cliente.

- [ ] **Step 8: Registrar**

Atualizar a memória `project_pedidos_entrada_portal.md` (o que foi deployado, commit, o que foi provado e o que ficou pendente) e avisar o Hugo no chat: commit, serviços reiniciados, como provou, e que o e-mail de erro do worker nasce redirecionado (`portal_cliente.envios.forcar_destino`).

---

## Self-review do plano (feito ao escrever, 25/09/2026)

**Cobertura da spec:** §5 modelo de dados → Tarefas 3 e 5; §6 ciclo e cancelar → Tarefas 4 e 5; §7 leitura/validação/planilha/prévia → Tarefas 2, 3, 6 e 7; §8.2 worker (XML e Excel, hook, descoberta do `#PE`, checagem pela chave) → Tarefa 8; §8.3 amarração → Tarefa 5; §8.4 `extrair_chave_nfe`/`extrair_ref_pedido` e docstring → Tarefa 1; §9 tela, rotas, tour, aba Receber → Tarefas 6, 7 e 5; §10 tabela de arquivos → todos os arquivos aparecem em alguma tarefa; §11 testes → um arquivo de teste por módulo; §12 ordem de entrega → Tarefa 9 segue núcleo → tela → worker → WMS. Fora: "e-mail ao cliente quando chega" (fora de escopo na spec); a nota de faltas continua a existente.

**Review Focus → testes:** 1 → `test_chave_ja_anunciada_e_cancelada` (T3); 2 → `test_ja_existe_na_stokki_vira_criado_sem_criar` + `test_procurar_pe_*` (T8); 3 → `test_conferencia_do_galpao_agrupa_por_sku` (T4); 4 → `test_cancelar_fora_de_anunciado_e_recusado`, `test_cancelar_com_pe_abre_chamado`, `test_cancelar_tira_do_wms_so_se_esperado_sem_enderecamento` (T4); 5 → `test_data_prevista_obrigatoria_e_nao_passada` (T4) + `test_arrival_date_nunca_passada` (T8).

**Consistência de nomes conferida:** `entradas.garantir_tabelas/conectar/ler_nfe_entrada/validar_entrada/ler_planilha_entrada/gerar_modelo_planilha_entrada/confirmar_entradas/listar_entradas/resumo_entradas/buscar_entrada/caminho_arquivo/cancelar_entrada/config_entradas_cliente/definir_entradas_ativo/entradas_ativas_para/amarrar_recebimento/sincronizar_status/rotulo_entrada` são os mesmos nomes nas Tarefas 3-8; `stokki_recebimentos.ler_detalhe/extrair_chave_nfe/extrair_ref_pedido/extrair_ref_da_linha` idem nas Tarefas 1, 5 e 8; contrato do wizard `{criado, ja_existia, erro, stokki_id, codigo, resposta}` é o mesmo em `executar_wizard_xml`, `executar_wizard_excel`, `processar_lote` e nos testes.

