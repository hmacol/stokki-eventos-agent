# -*- coding: utf-8 -*-
"""
Testes da importação de pedidos por planilha no portal do cliente
(pedido do Hugo, 09/09/2026): modelo, leitura/agrupamento, validações,
confirmação (fila) e lote simulado do worker.

    py -3 -m pytest portal_cliente/test_envio_planilha.py -q
"""
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import openpyxl
import pytest

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

CNPJ = "12345678000195"
DEST = "98765432000110"
AMANHA = (date.today() + timedelta(days=1)).strftime("%d/%m/%Y")


def _planilha(linhas: list[dict], cabecalho: list[str] | None = None) -> bytes:
    """Monta um .xlsx com o cabeçalho do modelo (ou um custom) e as linhas."""
    wb = openpyxl.Workbook()
    ws = wb.active
    cab = cabecalho or [c[1] for c in ep.COLUNAS_PLANILHA]
    chaves = [c[0] for c in ep.COLUNAS_PLANILHA]
    ws.append(cab)
    for l in linhas:
        ws.append([l.get(k, "") for k in chaves] if not cabecalho else [l.get(k, "") for k in cabecalho])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _linha(**kw) -> dict:
    base = {"referencia": "PED-1", "destinatario_doc": "98.765.432/0001-10", "destinatario_nome": "Mercado Teste",
            "cep": "01310-100", "logradouro": "Av. Paulista", "numero": "1578", "bairro": "Bela Vista", "cidade": "São Paulo",
            "uf": "SP", "sku": "SKU-A", "quantidade": 10, "valor_unitario": "12,50", "data_expedicao": AMANHA}
    base.update(kw)
    return base


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "DB_PATH", tmp_path / "dados.db")
    monkeypatch.setattr(ep, "_RAIZ", tmp_path)   # xml_path é relativo à raiz
    monkeypatch.setattr(ep, "PASTA_XMLS", tmp_path / "portal_envios")
    monkeypatch.setattr(ep, "PASTA_TEMP", tmp_path / "portal_envios" / "_temporarios")
    # horário do destinatário: não escrever na tabela real da roteirização
    import regras.complexidade_entrega as ce
    monkeypatch.setattr(ce, "definir_ajuste_manual", lambda *a, **k: None)
    monkeypatch.setattr(ce, "carregar_ajustes_manuais", lambda: {})
    conn = ep.conectar()
    conn.execute("CREATE TABLE IF NOT EXISTS interno (cnpj_embarcador TEXT, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER, "
                 "sender_id INTEGER, email TEXT, notificar_email INTEGER)")
    conn.execute("INSERT INTO interno VALUES (?, 'EMB TESTE', 'EMB TESTE', 98, 1, 'x@y.z', 1)", (CNPJ,))
    conn.commit()
    yield conn
    conn.close()


# ── Modelo ─────────────────────────────────────────────────────────────────────

def test_modelo_tem_todas_as_colunas_e_e_lido_de_volta():
    conteudo = ep.gerar_modelo_planilha("Cliente X")
    wb = openpyxl.load_workbook(io.BytesIO(conteudo))
    assert wb.sheetnames == ["Pedidos", "Instruções"]
    cab = [c.value for c in wb["Pedidos"][1]]
    assert cab == [c[1] for c in ep.COLUNAS_PLANILHA]
    # as linhas de exemplo do modelo são lidas como 2 pedidos válidos
    pedidos, rejeitados = ep.ler_planilha(conteudo, "modelo.xlsx", CNPJ)
    assert rejeitados == []
    assert [p["referencia"] for p in pedidos] == ["PED-1001", "PED-1002"]
    assert pedidos[0]["itens"] == 2 and pedidos[1]["itens"] == 1


# ── Leitura ────────────────────────────────────────────────────────────────────

def test_agrupa_linhas_do_mesmo_pedido_e_calcula_valor():
    conteudo = _planilha([_linha(), _linha(sku="SKU-B", quantidade=2, valor_unitario=3, destinatario_doc="", destinatario_nome=""),
                          _linha(referencia="PED-2", destinatario_doc="123.456.789-09", destinatario_nome="Ana", sku="SKU-C", quantidade="1,5")])
    pedidos, rejeitados = ep.ler_planilha(conteudo, "p.xlsx", CNPJ)
    assert rejeitados == []
    assert len(pedidos) == 2
    p1 = pedidos[0]
    assert p1["origem"] == "planilha" and p1["referencia"] == "PED-1"
    assert p1["destinatario_doc"] == DEST and p1["destinatario_cep"] == "01310100"
    assert p1["destinatario_endereco"] == "Av. Paulista, 1578"
    assert [i["sku"] for i in p1["itens_lista"]] == ["SKU-A", "SKU-B"]
    assert p1["itens"] == 2 and p1["linhas"] == [2, 3]
    assert p1["valor_nf"] == 10 * 12.5 + 2 * 3
    assert p1["volumes"] == 1
    assert p1["data_expedicao"] == (date.today() + timedelta(days=1)).isoformat()
    assert p1["emitente_cnpj"] == CNPJ
    assert p1["chave_nfe"].startswith(f"PLANILHA-{CNPJ}-PED-1-")
    p2 = pedidos[1]
    assert p2["destinatario_doc"] == "12345678909" and p2["itens_lista"][0]["quantidade"] == 1.5


def test_cabecalho_com_apelidos_e_ordem_diferente():
    cab = ["CNPJ", "Cliente", "Pedido", "Endereço", "Nº", "Bairro", "Cidade", "Estado", "CEP", "Código", "Qtd"]
    linhas = [{"CNPJ": "98765432000110", "Cliente": "Loja", "Pedido": 77, "Endereço": "Rua A", "Nº": 5, "Bairro": "Centro",
               "Cidade": "Campinas", "Estado": "sp", "CEP": 13010000, "Código": 7891234567890, "Qtd": 3}]
    pedidos, rejeitados = ep.ler_planilha(_planilha(linhas, cab), "x.xlsx", CNPJ)
    assert rejeitados == []
    assert pedidos[0]["referencia"] == "77" and pedidos[0]["destinatario_uf"] == "SP"
    assert pedidos[0]["destinatario_cep"] == "13010000"
    assert pedidos[0]["itens_lista"][0]["sku"] == "7891234567890"
    assert pedidos[0]["data_expedicao"] == date.today().isoformat()   # em branco = hoje


def test_rejeita_pedido_com_problema_mas_mantem_os_outros():
    linhas = [
        _linha(),
        _linha(referencia="PED-2", destinatario_doc="123"),                     # doc inválido
        _linha(referencia="PED-3", quantidade=0),                                # sem item válido
        _linha(referencia="PED-4", uf="XX", cep="123"),                          # uf + cep
        _linha(referencia="PED-5", data_expedicao="01/01/2020"),                 # data passada
        _linha(referencia="PED-6", destinatario_doc="11.111.111/0001-11"),
        _linha(referencia="PED-6", destinatario_doc="22.222.222/0001-22", sku="B"),  # destinatário diferente no mesmo pedido
        {"sku": "X", "quantidade": 1},                                           # sem referência
    ]
    pedidos, rejeitados = ep.ler_planilha(_planilha(linhas), "p.xlsx", CNPJ)
    assert [p["referencia"] for p in pedidos] == ["PED-1"]
    por = {r["rotulo"]: r["erro"] for r in rejeitados}
    assert "14 ou 11 dígitos" in por["Pedido PED-2"]
    assert "maior que zero" in por["Pedido PED-3"]
    assert "UF inválida" in por["Pedido PED-4"] and "CEP inválido" in por["Pedido PED-4"]
    assert "já passou" in por["Pedido PED-5"]
    assert "destinatário diferente" in por["Pedido PED-6"]
    assert "linha 9" in por and "referência" in por["linha 9"]


def test_falta_coluna_obrigatoria_e_arquivo_nao_planilha():
    cab = ["Pedido", "CNPJ", "Cliente", "SKU", "Qtd"]
    # faltando coluna obrigatória a planilha não é mais recusada: pede pra indicar (16/11)
    with pytest.raises(ep.ColunasPorIndicar, match="faltam: CEP"):
        ep.ler_planilha(_planilha([{"Pedido": 1}], cab), "x.xlsx", CNPJ)
    with pytest.raises(ep.ErroEnvio):
        ep.ler_planilha(b"isso nao e xlsx", "x.xlsx", CNPJ)
    assert ep.e_planilha("a.XLSX") and ep.e_planilha("b.xls") and not ep.e_planilha("c.xml", b"<xml/>")


def test_expandir_upload_nao_confunde_xlsx_com_zip():
    conteudo = _planilha([_linha()])
    assert conteudo[:2] == b"PK"
    partes = ep.expandir_upload("pedidos.xlsx", conteudo)
    assert partes == [("pedidos.xlsx", conteudo)]


# ── Catálogo de SKUs ───────────────────────────────────────────────────────────

def test_validar_skus_erro_so_quando_catalogo_do_embarcador_existe(ambiente):
    conn = ambiente
    cfg = {"client_id": "98", "nome": "EMB TESTE"}
    pedido = {"itens_lista": [{"sku": "SKU-A"}, {"sku": "sku-z"}]}
    erros, avisos = ep.validar_skus(conn, pedido, cfg)
    assert not erros and avisos                         # sem tabela: só aviso
    conn.execute("CREATE TABLE wms_produtos (sku TEXT, ean TEXT, dun TEXT, descricao TEXT, embarcador TEXT, embarcador_id INTEGER, ativo INTEGER)")
    conn.execute("INSERT INTO wms_produtos VALUES ('SKU-A', '789', NULL, 'Produto A', 'EMB TESTE', 98, 1)")
    conn.execute("INSERT INTO wms_produtos VALUES ('SKU-Z', NULL, NULL, 'Produto Z', 'OUTRO', 5, 1)")
    conn.commit()
    erros, avisos = ep.validar_skus(conn, pedido, cfg)
    assert erros and "sku-z" in erros[0]
    ok, _ = ep.validar_skus(conn, {"itens_lista": [{"sku": "789"}]}, cfg)   # EAN também vale
    assert ok == []


# ── Confirmação (fila) ─────────────────────────────────────────────────────────

def _analisar_e_confirmar(conn, conteudo, nome="p.xlsx", **item_extra):
    pedidos, rejeitados = ep.ler_planilha(conteudo, nome, CNPJ)
    assert rejeitados == []
    token_plan = ep.guardar_temporario(conteudo, "xlsx")
    itens = []
    for ped in pedidos:
        v = ep.validar_item(conn, ped, CNPJ)
        assert v["ok"], v["erros"]
        tk = ep.guardar_temporario_pedido(ped, token_plan)
        itens.append({"token": tk, "horario_inicio": "08:00", "horario_fim": "17:00", **item_extra})
    return ep.confirmar_envios(conn, CNPJ, itens, "cliente", {}, "nenhuma")


def test_confirmar_grava_na_fila_com_itens_e_guarda_planilha(ambiente):
    conn = ambiente
    conteudo = _planilha([_linha(), _linha(sku="SKU-B", quantidade=2), _linha(referencia="PED-2", sku="C", quantidade=1)])
    criados = _analisar_e_confirmar(conn, conteudo, observacoes="Deixar na portaria")
    assert len(criados) == 2 and all(c["origem"] == "planilha" for c in criados)
    rows = [dict(r) for r in conn.execute("SELECT * FROM portal_envios ORDER BY id")]
    assert [r["referencia"] for r in rows] == ["PED-1", "PED-2"]
    assert all(r["status"] == "NA_FILA" and r["origem"] == "planilha" and r["regra_xml"] is None for r in rows)
    itens = json.loads(rows[0]["itens_json"])
    assert [i["sku"] for i in itens["itens"]] == ["SKU-A", "SKU-B"]
    assert itens["destinatario"]["destinatario_logradouro"] == "Av. Paulista"
    assert rows[0]["linhas_planilha"] == "2,3" and rows[0]["observacoes"] == "Deixar na portaria"
    # os 2 pedidos apontam pra MESMA planilha guardada, e o temporário sumiu
    assert rows[0]["xml_path"] == rows[1]["xml_path"] and rows[0]["xml_path"].endswith("_p.xlsx")
    assert (ep._RAIZ / rows[0]["xml_path"]).is_file()
    assert not list(ep.PASTA_TEMP.glob("*"))
    # listagem: rótulo e chip
    lista = ep.listar_envios(conn, CNPJ)
    assert lista[0]["rotulo"] == "Pedido PED-2" and lista[0]["origem"] == "planilha" and lista[0]["arquivo_ext"] == "xlsx"
    assert "itens_json" not in lista[0]
    # reenviar o mesmo pedido = duplicado
    ped, _ = ep.ler_planilha(_planilha([_linha()]), "p2.xlsx", CNPJ)
    v = ep.validar_item(conn, ped[0], CNPJ)
    assert not v["ok"] and "PED-1 já foi enviado" in v["erros"][0]


def test_caminho_xml_continua_igual(ambiente):
    """Regressão: NF-e pelo fluxo original ainda grava origem='xml', o XML em
    <cnpj>/<chave>.xml e rótulo 'NF n'."""
    conn = ambiente
    chave = "35260912345678000195550010000123451000012345"
    xml = f"""<?xml version="1.0"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{chave}">
      <ide><nNF>12345</nNF><serie>1</serie><tpNF>1</tpNF><dhEmi>2026-09-09T10:00:00-03:00</dhEmi></ide>
      <emit><CNPJ>{CNPJ}</CNPJ><xNome>Emitente</xNome></emit>
      <dest><CNPJ>{DEST}</CNPJ><xNome>Destino XML</xNome><enderDest><xLgr>Rua A</xLgr><nro>1</nro><xBairro>B</xBairro>
      <xMun>São Paulo</xMun><UF>SP</UF><CEP>01310100</CEP></enderDest></dest>
      <det nItem="1"><prod><cProd>X</cProd></prod></det>
      <transp><vol><qVol>2</qVol><pesoB>3.5</pesoB></vol></transp><total><ICMSTot><vNF>99.90</vNF></ICMSTot></total>
    </infNFe></NFe></nfeProc>"""
    nfe = ep.ler_nfe(xml.encode("utf-8"), "nota.xml")
    assert nfe["chave_nfe"] == chave and nfe["volumes"] == 2
    tk = ep.guardar_temporario(xml.encode("utf-8"))
    criados = ep.confirmar_envios(conn, CNPJ, [{"token": tk, "horario_inicio": "08:00", "horario_fim": "12:00"}], "cliente", {}, "muai")
    assert criados[0]["origem"] == "xml" and criados[0]["numero_nf"] == "12345"
    r = dict(conn.execute("SELECT * FROM portal_envios").fetchone())
    assert r["origem"] == "xml" and r["regra_xml"] == "muai" and r["itens_json"] is None and r["referencia"] is None
    assert r["xml_path"].endswith(f"{chave}.xml") and r["valor_nf"] == 99.9
    lista = ep.listar_envios(conn, CNPJ)
    assert lista[0]["rotulo"] == "NF 12345" and lista[0]["arquivo_ext"] == "xml" and lista[0]["documento_curto"] == "12345"


def test_xlsx_pedido_stokki_formato_do_modelo():
    conteudo = ep.xlsx_pedido_stokki([{"sku": "A", "quantidade": 3, "valor_unitario": 1.5}, {"sku": "B", "quantidade": 2.5, "valor_unitario": None}])
    ws = openpyxl.load_workbook(io.BytesIO(conteudo)).active
    assert [c.value for c in ws[1]] == ["SKU", "Quantidade", "Valor Unitário"]
    assert [c.value for c in ws[2]] == ["A", 3, 1.5]
    assert [c.value for c in ws[3]] == ["B", 2.5, None]


# ── Worker (simulado) ──────────────────────────────────────────────────────────

def test_worker_simulado_marca_planilha_como_criada(ambiente, monkeypatch, tmp_path):
    conn = ambiente
    import enviar_stokki as worker
    monkeypatch.setattr(worker, "PASTA_LOTES", tmp_path / "lotes")
    monkeypatch.setattr(worker, "_avisar_erros", lambda *a, **k: None)
    _analisar_e_confirmar(conn, _planilha([_linha(), _linha(referencia="PED-2", sku="C", quantidade=1)]))
    envios = [dict(r) for r in conn.execute("SELECT * FROM portal_envios WHERE status = 'NA_FILA'")]
    resumo = worker.processar_lote(conn, CNPJ, envios, {"portal_cliente": {}}, simular=True)
    assert resumo["criados"] == 2 and resumo["erros"] == 0
    assert [r[0] for r in conn.execute("SELECT status FROM portal_envios")] == ["CRIADO", "CRIADO"]
    gerados = sorted((tmp_path / "lotes").rglob("*.xlsx"))
    assert len(gerados) == 2
    ws = openpyxl.load_workbook(gerados[0]).active
    assert [c.value for c in ws[1]] == ["SKU", "Quantidade", "Valor Unitário"]


def test_worker_planilha_sem_itens_vira_erro(ambiente, monkeypatch, tmp_path):
    conn = ambiente
    import enviar_stokki as worker
    monkeypatch.setattr(worker, "PASTA_LOTES", tmp_path / "lotes")
    monkeypatch.setattr(worker, "_avisar_erros", lambda *a, **k: None)
    _analisar_e_confirmar(conn, _planilha([_linha()]))
    conn.execute("UPDATE portal_envios SET itens_json = '{}'")
    conn.commit()
    envios = [dict(r) for r in conn.execute("SELECT * FROM portal_envios")]
    resumo = worker.processar_lote(conn, CNPJ, envios, {"portal_cliente": {}}, simular=True)
    assert resumo["erros"] == 1
    r = conn.execute("SELECT status, erro FROM portal_envios").fetchone()
    assert r["status"] == "ERRO" and "sem itens" in r["erro"]


# ── Login de grupo: a planilha diz a empresa de cada pedido (12/11) ────────────

EMP_A, EMP_B, EMP_ZERO = "54993021000184", "48654566000163", "04565018000272"
EMPRESAS = {EMP_A: "MARCHEF", EMP_B: "ITAUEIRA", EMP_ZERO: "TRES M"}


def _planilha_grupo(linhas: list[dict]) -> bytes:
    colunas = [ep.COLUNA_EMPRESA] + ep.COLUNAS_PLANILHA
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([c[1] for c in colunas])
    for l in linhas:
        ws.append([l.get(c[0], "") for c in colunas])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_modelo_do_grupo_tem_a_coluna_da_empresa_e_e_lido_de_volta():
    conteudo = ep.gerar_modelo_planilha("Marchef", EMPRESAS)
    wb = openpyxl.load_workbook(io.BytesIO(conteudo))
    cab = [c.value for c in wb["Pedidos"][1]]
    assert cab == [ep.COLUNA_EMPRESA[1]] + [c[1] for c in ep.COLUNAS_PLANILHA]
    instrucoes = " ".join(str(c.value or "") for c in wb["Instruções"]["A"])
    assert "48.654.566/0001-63" in instrucoes and "ITAUEIRA" in instrucoes
    pedidos, rejeitados = ep.ler_planilha(conteudo, "modelo.xlsx", EMP_A, EMPRESAS)
    assert rejeitados == [] and [p["emitente_cnpj"] for p in pedidos] == [EMP_A, EMP_A]
    # o modelo dos outros clientes continua sem a coluna
    assert ep.COLUNA_EMPRESA[1] not in [c.value for c in openpyxl.load_workbook(io.BytesIO(ep.gerar_modelo_planilha("X")))["Pedidos"][1]]


def test_planilha_do_grupo_separa_os_pedidos_pela_coluna_da_empresa():
    conteudo = _planilha_grupo([
        _linha(referencia="P-A", empresa_cnpj="54.993.021/0001-84"),
        _linha(referencia="P-A", empresa_cnpj="", sku="SKU-B"),            # 2a linha do pedido: empresa em branco
        _linha(referencia="P-B", empresa_cnpj=EMP_B),
        _linha(referencia="P-Z", empresa_cnpj=4565018000272),              # Excel comeu o zero a esquerda
    ])
    pedidos, rejeitados = ep.ler_planilha(conteudo, "p.xlsx", EMP_A, EMPRESAS)
    assert rejeitados == []
    assert {p["referencia"]: p["emitente_cnpj"] for p in pedidos} == {"P-A": EMP_A, "P-B": EMP_B, "P-Z": EMP_ZERO}
    assert pedidos[0]["itens"] == 2 and pedidos[1]["emitente_nome"] == "ITAUEIRA"
    # a chave do pedido leva a empresa: a mesma referencia em outra empresa nao e duplicada
    assert pedidos[1]["chave_nfe"] == ep.chave_planilha(EMP_B, "P-B") != ep.chave_planilha(EMP_A, "P-B")


def test_planilha_do_grupo_recusa_pedido_sem_empresa_de_fora_ou_misturado():
    conteudo = _planilha_grupo([
        _linha(referencia="SEM", empresa_cnpj=""),
        _linha(referencia="FORA", empresa_cnpj=CNPJ),
        _linha(referencia="MIX", empresa_cnpj=EMP_A),
        _linha(referencia="MIX", empresa_cnpj=EMP_B, sku="SKU-B"),
        _linha(referencia="OK", empresa_cnpj=EMP_B),
    ])
    pedidos, rejeitados = ep.ler_planilha(conteudo, "p.xlsx", EMP_A, EMPRESAS)
    assert [p["referencia"] for p in pedidos] == ["OK"]
    erros = {r["rotulo"]: r["erro"] for r in rejeitados}
    assert "Sem o CNPJ da empresa" in erros["Pedido SEM"]
    assert "não faz parte do seu acesso" in erros["Pedido FORA"]
    assert "empresa diferente da primeira linha" in erros["Pedido MIX"]


def test_planilha_do_grupo_sem_a_coluna_e_recusada_inteira():
    with pytest.raises(ep.ColunasPorIndicar, match="faltam: CNPJ da empresa") as e:
        ep.ler_planilha(_planilha([_linha()]), "antiga.xlsx", EMP_A, EMPRESAS)
    assert e.value.dados["campos"][0]["chave"] == "empresa_cnpj"   # o campo da empresa entra na tela de indicar
    # login de uma empresa so: a coluna nao e exigida e, se vier, e ignorada
    pedidos, _ = ep.ler_planilha(_planilha_grupo([_linha(empresa_cnpj=EMP_B)]), "p.xlsx", CNPJ)
    assert pedidos[0]["emitente_cnpj"] == CNPJ


def _confirmar_grupo(conn, conteudo, por_empresa):
    pedidos, rejeitados = ep.ler_planilha(conteudo, "grupo.xlsx", EMP_A, EMPRESAS)
    assert rejeitados == []
    token_plan = ep.guardar_temporario(conteudo, "xlsx")
    itens = [{"token": ep.guardar_temporario_pedido(p, token_plan), "horario_inicio": "08:00", "horario_fim": "17:00"}
             for p in pedidos]
    return ep.confirmar_envios(conn, EMP_A, itens, "cliente", {}, "nenhuma", por_empresa=por_empresa)


def test_confirmar_do_grupo_grava_cada_pedido_na_empresa_dele(ambiente):
    conn = ambiente
    conteudo = _planilha_grupo([_linha(referencia="P-A", empresa_cnpj=EMP_A), _linha(referencia="P-B", empresa_cnpj=EMP_B),
                                _linha(referencia="P-B2", empresa_cnpj=EMP_B)])
    ctx = {"regra_xml": "nenhuma", "fluxos": ["armazenagem"], "sender_id": 1}
    criados = _confirmar_grupo(conn, conteudo, {EMP_A: ctx, EMP_B: ctx})
    assert [c["cnpj_embarcador"] for c in criados] == [EMP_A, EMP_B, EMP_B]
    rows = [dict(r) for r in conn.execute("SELECT * FROM portal_envios ORDER BY id")]
    assert [(r["referencia"], r["cnpj_embarcador"], r["status"]) for r in rows] == [
        ("P-A", EMP_A, "NA_FILA"), ("P-B", EMP_B, "NA_FILA"), ("P-B2", EMP_B, "NA_FILA")]
    # uma copia da planilha por empresa (a pasta e por CNPJ) e nenhum temporario sobrando
    assert rows[0]["xml_path"] != rows[1]["xml_path"] == rows[2]["xml_path"]
    assert all((ep._RAIZ / r["xml_path"]).is_file() for r in rows)
    assert not list(ep.PASTA_TEMP.glob("*"))
    # cada empresa so enxerga os dela
    assert [l["referencia"] for l in ep.listar_envios(conn, EMP_A)] == ["P-A"]
    assert sorted(l["referencia"] for l in ep.listar_envios(conn, EMP_B)) == ["P-B", "P-B2"]
    # horario do destinatario fica guardado na empresa do pedido
    assert {r[0] for r in conn.execute("SELECT cnpj_embarcador FROM portal_destinatarios")} == {EMP_A, EMP_B}


def test_confirmar_do_grupo_com_empresa_de_fora_nao_grava_nada(ambiente):
    conn = ambiente
    conteudo = _planilha_grupo([_linha(referencia="P-A", empresa_cnpj=EMP_A), _linha(referencia="P-B", empresa_cnpj=EMP_B)])
    ctx = {"regra_xml": "nenhuma", "fluxos": ["armazenagem"], "sender_id": 1}
    with pytest.raises(ep.ErroEnvio, match="não é de uma empresa do seu acesso"):
        _confirmar_grupo(conn, conteudo, {EMP_A: ctx})   # EMP_B saiu do acesso entre a previa e o confirmar
    assert conn.execute("SELECT COUNT(*) FROM portal_envios").fetchone()[0] == 0


# ── Colunas indicadas pelo cliente (16/11) ─────────────────────────────────────

CAB_ANTIGA = ["Empresa CNPJ", "Nr Doc", "CNPJ", "Razão do cliente", "CEP entrega", "Rua", "Nr", "Bairro", "Município", "UF",
              "Cod. item", "Qt", "Extra"]
CAMPOS_ANTIGA = ["empresa_cnpj", "referencia", "destinatario_doc", "destinatario_nome", "cep", "logradouro", "numero", "bairro",
                 "cidade", "uf", "sku", "quantidade"]


def _planilha_antiga(linhas: list[dict], titulo: bool = False) -> bytes:
    """Planilha do cliente: nomes de coluna dele (alguns desconhecidos) e,
    opcionalmente, uma linha de título antes do cabeçalho."""
    wb = openpyxl.Workbook()
    ws = wb.active
    if titulo:
        ws.append(["Pedidos da semana"])
    ws.append(CAB_ANTIGA)
    for l in linhas:
        ws.append([l.get(k, "") for k in CAMPOS_ANTIGA] + ["x"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _indicado(linha: int = 0) -> dict:
    return {"linha": linha, "colunas": {k: j for j, k in enumerate(CAMPOS_ANTIGA)}}


def test_planilha_do_cliente_pede_pra_indicar_e_depois_le_sozinha():
    conteudo = _planilha_antiga([_linha(empresa_cnpj=EMP_B), _linha(referencia="PED-2", empresa_cnpj=EMP_A)], titulo=True)
    with pytest.raises(ep.ColunasPorIndicar) as e:
        ep.ler_planilha(conteudo, "minha.xlsx", EMP_A, EMPRESAS)
    d = e.value.dados
    assert d["linha"] == 1 and [c["nome"] for c in d["colunas"]] == CAB_ANTIGA
    assert d["colunas"][1]["exemplos"] == ["PED-1", "PED-2"]
    assert d["sugestao"]["uf"] == 9 and d["sugestao"]["bairro"] == 7    # o que tem nome conhecido já vem sugerido
    assert "referencia" not in d["sugestao"] and "Pedido (nº ou referência)" in d["faltando"]

    mapa: dict = {}
    pedidos, rej = ep.ler_planilha(conteudo, "minha.xlsx", EMP_A, EMPRESAS, indicado=_indicado(1), mapa_usado=mapa)
    assert rej == [] and {p["referencia"]: p["emitente_cnpj"] for p in pedidos} == {"PED-1": EMP_B, "PED-2": EMP_A}
    assert pedidos[0]["destinatario_doc"] == DEST and pedidos[0]["itens_lista"][0]["sku"] == "SKU-A"
    assert mapa["referencia"] == {"nome": "nr doc", "pos": 1} and mapa["numero_nf"] is None

    # a próxima planilha com os mesmos nomes (até com as colunas em outra posição) entra direto
    outra = _planilha_antiga([_linha(referencia="PED-9", empresa_cnpj=EMP_A)])
    pedidos, rej = ep.ler_planilha(outra, "minha2.xlsx", EMP_A, EMPRESAS, guardado=mapa)
    assert rej == [] and pedidos[0]["referencia"] == "PED-9" and pedidos[0]["emitente_cnpj"] == EMP_A


def test_duas_colunas_com_o_mesmo_nome_valem_pela_posicao_guardada():
    wb = openpyxl.Workbook()
    wb.active.append(["CNPJ", "Pedido", "CNPJ", "Nome", "CEP", "Endereço", "Número", "Bairro", "Cidade", "UF", "SKU", "Quantidade"])
    wb.active.append([EMP_B, "P-1", DEST, "Mercado", "01310100", "Av. Paulista", "10", "Bela Vista", "São Paulo", "SP", "A", 1])
    buf = io.BytesIO()
    wb.save(buf)
    campos = ["empresa_cnpj", "referencia", "destinatario_doc", "destinatario_nome", "cep", "logradouro", "numero", "bairro",
              "cidade", "uf", "sku", "quantidade"]
    mapa: dict = {}
    pedidos, _ = ep.ler_planilha(buf.getvalue(), "p.xlsx", EMP_A, EMPRESAS,
                                 indicado={"linha": 0, "colunas": {k: j for j, k in enumerate(campos)}}, mapa_usado=mapa)
    assert (pedidos[0]["emitente_cnpj"], pedidos[0]["destinatario_doc"]) == (EMP_B, DEST)
    pedidos, _ = ep.ler_planilha(buf.getvalue(), "p.xlsx", EMP_A, EMPRESAS, guardado=mapa)
    assert (pedidos[0]["emitente_cnpj"], pedidos[0]["destinatario_doc"]) == (EMP_B, DEST)


def test_indicar_a_pedido_campo_sem_coluna_e_escolha_invalida():
    modelo = _planilha([_linha(numero_nf="123")])
    with pytest.raises(ep.ColunasPorIndicar) as e:          # nomes conhecidos, mas o cliente pediu pra indicar
        ep.ler_planilha(modelo, "p.xlsx", CNPJ, pedir_indicacao=True)
    sugestao = e.value.dados["sugestao"]
    assert e.value.dados["faltando"] == [] and sugestao["referencia"] == 0 and sugestao["numero_nf"] == 1
    # "não tem" na NF: fica guardado e o apelido conhecido não traz a coluna de volta
    mapa: dict = {}
    escolhas = {k: v for k, v in sugestao.items() if k != "numero_nf"}
    pedidos, _ = ep.ler_planilha(modelo, "p.xlsx", CNPJ, indicado={"linha": 0, "colunas": escolhas}, mapa_usado=mapa)
    assert pedidos[0]["numero_nf"] == "" and mapa["numero_nf"] is None
    assert ep.ler_planilha(modelo, "p.xlsx", CNPJ, guardado=mapa)[0][0]["numero_nf"] == ""
    assert ep.ler_planilha(modelo, "p.xlsx", CNPJ)[0][0]["numero_nf"] == "123"
    # obrigatória sem coluna: pede de novo; escolha fora da planilha: erro
    sem_cep = {k: v for k, v in sugestao.items() if k != "cep"}
    with pytest.raises(ep.ColunasPorIndicar, match="faltam: CEP"):
        ep.ler_planilha(modelo, "p.xlsx", CNPJ, indicado={"linha": 0, "colunas": sem_cep})
    for ruim in ({"linha": 99, "colunas": sugestao}, {"linha": "x", "colunas": sugestao}, {"colunas": sugestao}, {"linha": 0, "colunas": "x"}):
        with pytest.raises(ep.ErroEnvio, match="colunas indicadas inválidas"):
            ep.ler_planilha(modelo, "p.xlsx", CNPJ, indicado=ruim)
    with pytest.raises(ep.ColunasPorIndicar):               # coluna que não existe é ignorada: o campo fica faltando
        ep.ler_planilha(modelo, "p.xlsx", CNPJ, indicado={"linha": 0, "colunas": {**sugestao, "cep": 500}})


def test_mesma_coluna_em_dois_campos_continua_valendo_no_mapa_guardado():
    conteudo = _planilha_antiga([_linha(empresa_cnpj=EMP_A, numero_nf="")])
    escolhas = {**_indicado()["colunas"], "numero_nf": 1}      # "Nr Doc" é o pedido E o número da NF
    mapa: dict = {}
    pedidos, _ = ep.ler_planilha(conteudo, "p.xlsx", EMP_A, EMPRESAS, indicado={"linha": 0, "colunas": escolhas}, mapa_usado=mapa)
    assert (pedidos[0]["referencia"], pedidos[0]["numero_nf"]) == ("PED-1", "1")
    pedidos, _ = ep.ler_planilha(conteudo, "p.xlsx", EMP_A, EMPRESAS, guardado=mapa)
    assert (pedidos[0]["referencia"], pedidos[0]["numero_nf"]) == ("PED-1", "1")


def test_mapa_de_colunas_e_guardado_por_login(ambiente):
    assert ep.carregar_mapa_planilha(ambiente, CNPJ) is None
    ep.guardar_mapa_planilha(ambiente, CNPJ, {"referencia": {"nome": "nr doc", "pos": 1}, "numero_nf": None}, "cliente")
    ep.guardar_mapa_planilha(ambiente, "12.345.678/0001-95", {"referencia": {"nome": "doc", "pos": 0}}, "cliente")
    assert ep.carregar_mapa_planilha(ambiente, CNPJ) == {"referencia": {"nome": "doc", "pos": 0}}
    assert ep.carregar_mapa_planilha(ambiente, EMP_A) is None


# ── Um pedido por linha, itens em pares Sku N / Quantidade N (planilha da Marchef, 16/11) ──

CAB_PARES = ["Número da PO", "Nome do Destino/Nome da Empresa", "Telefone", "E-mail", "CPF/CNPJ", "Sobrenome/Razão Social",
             "cep", "Rua", "Número", "Complemento", "Bairro", "Cidade", "Estado", "País", "Armazém de Origem", "Tipo de Conta"]


def _planilha_pares(linhas: list[list], n_pares: int = 100, extra: list[str] | None = None) -> bytes:
    """`linhas` = [valores das colunas fixas (+ extras), [(sku, qtd), ...]]."""
    wb = openpyxl.Workbook()
    cab = CAB_PARES + (extra or [])
    wb.active.append(cab + [t for k in range(1, n_pares + 1) for t in (f"Sku{k}", f"Quantidade{k}")])
    for fixos, itens in linhas:
        wb.active.append(list(fixos) + [None] * (len(cab) - len(fixos)) + [v for par in itens for v in par])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fixos(po, extra=()):
    return [po, "PADARIA TESTE", "(11) 99999-0000", "compras@teste.com.br", "98.765.432/0001-10", "PADARIA TESTE LTDA",
            "02221-001", "AVENIDA TESTE, ", 1298, None, "JARDIM TESTE", "SÃO PAULO", "SP", "Brasil", "#stkkw-1",
            "Pessoa Jurídica", *extra]


def test_pares_de_sku_e_quantidade_um_pedido_por_linha():
    conteudo = _planilha_pares([(_fixos(7878), [("D21TCI30510D", 2), ("ABC", "1,5"), (None, None), ("XYZ", 3)]),
                                (_fixos(7879), [("ABC", 4)])])
    # só falta dizer qual dos dois nomes é o do destinatário; SKU e quantidade nem aparecem na janela
    with pytest.raises(ep.ColunasPorIndicar) as e:
        ep.ler_planilha(conteudo, "m.xlsx", CNPJ)
    d = e.value.dados
    assert d["faltando"] == ["Nome do destinatário"] and d["pares"] == 100
    assert [c["nome"] for c in d["colunas"]] == CAB_PARES
    assert not {"sku", "quantidade", "valor_unitario"} & {c["chave"] for c in d["campos"]}
    mapa: dict = {}
    pedidos, rej = ep.ler_planilha(conteudo, "m.xlsx", CNPJ, mapa_usado=mapa,
                                   indicado={"linha": 0, "colunas": {**d["sugestao"], "destinatario_nome": 5}})
    assert rej == [] and [p["referencia"] for p in pedidos] == ["7878", "7879"]
    assert [(i["sku"], i["quantidade"]) for i in pedidos[0]["itens_lista"]] == [("D21TCI30510D", 2), ("ABC", 1.5), ("XYZ", 3)]
    assert pedidos[0]["itens"] == 3 and pedidos[1]["itens"] == 1
    assert (pedidos[0]["destinatario_nome"], pedidos[0]["destinatario_doc"], pedidos[0]["destinatario_numero"]) == \
        ("PADARIA TESTE LTDA", "98765432000110", "1298")
    # a escolha guardada não fecha SKU/quantidade: a planilha do modelo (uma linha por item) continua entrando
    assert "sku" not in mapa and "quantidade" not in mapa
    assert len(ep.ler_planilha(conteudo, "m.xlsx", CNPJ, guardado=mapa)[0]) == 2
    assert len(ep.ler_planilha(_planilha([_linha()]), "p.xlsx", CNPJ, guardado=mapa)[0]) == 1


def test_pares_com_problema_recusam_so_o_pedido_e_grupo_usa_a_coluna_da_empresa():
    conteudo = _planilha_pares([(_fixos("A1", [EMP_A]), [("ABC", 2)]),
                                (_fixos("A2", [EMP_B]), [("ABC", None), (None, 5), ("XYZ", "dois")]),
                                (_fixos("A3", [EMP_A]), [])], n_pares=3, extra=["CNPJ da empresa"])
    col = {"referencia": 0, "destinatario_nome": 1, "destinatario_doc": 4, "cep": 6, "logradouro": 7, "numero": 8, "bairro": 10,
           "cidade": 11, "uf": 12, "empresa_cnpj": 16}
    pedidos, rej = ep.ler_planilha(conteudo, "m.xlsx", EMP_A, EMPRESAS, indicado={"linha": 0, "colunas": col})
    assert [(p["referencia"], p["emitente_cnpj"]) for p in pedidos] == [("A1", EMP_A)]
    erros = {r["rotulo"]: r["erro"] for r in rej}
    assert "linha 3 (ABC): quantidade precisa ser maior que zero." in erros["Pedido A2"]
    assert "linha 3: sem SKU." in erros["Pedido A2"] and "linha 3 (XYZ): quantidade inválida (dois)." in erros["Pedido A2"]
    assert "Nenhum item válido" in erros["Pedido A3"]


def test_pares_tem_teto_de_itens_e_numero_absurdo_no_cabecalho_nao_derruba(monkeypatch):
    monkeypatch.setattr(ep, "MAX_ITENS_PLANILHA", 3)
    col = {"referencia": 0, "destinatario_nome": 1, "destinatario_doc": 4, "cep": 6, "logradouro": 7, "numero": 8, "bairro": 10,
           "cidade": 11, "uf": 12}
    conteudo = _planilha_pares([(_fixos("A1"), [("A", 1), ("B", 1)]), (_fixos("A2"), [("A", 1), ("B", 1)])], n_pares=2)
    with pytest.raises(ep.ErroEnvio, match="mais de 3 itens"):
        ep.ler_planilha(conteudo, "m.xlsx", CNPJ, indicado={"linha": 0, "colunas": col})
    assert ep._pares_de_itens(["Sku" + "9" * 5000, "Quantidade" + "9" * 5000, "Sku1", "Qtd 1"]) == [(2, 3)]


# ── Coluna de item alterada pelo cliente: lê, avisa e não trava (17/11) ───────
# As tres planilhas que a Marchef mandou em 17/11 (PO 7878) e que foram recusadas.

COL_PARES = {"referencia": 0, "destinatario_nome": 1, "destinatario_doc": 4, "cep": 6, "logradouro": 7, "numero": 8,
             "bairro": 10, "cidade": 11, "uf": 12}


def _planilha_cab(cab_itens: list[str], linhas: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    wb.active.append(CAB_PARES + cab_itens)
    for fixos, itens in linhas:
        wb.active.append(list(fixos) + list(itens))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_quantidade_do_par_renomeada_e_lida_pela_coluna_ao_lado_com_aviso():
    conteudo = _planilha_cab(["Sku1", "Volumes", "Sku2", "Quantidade2"], [(_fixos(7878), ["D21TCI30510D", 2, "ABC", 3])])
    avisos: list = []
    pedidos, rej = ep.ler_planilha(conteudo, "m.xlsx", CNPJ, indicado={"linha": 0, "colunas": COL_PARES}, avisos=avisos)
    assert rej == []
    assert [(i["sku"], i["quantidade"]) for i in pedidos[0]["itens_lista"]] == [("D21TCI30510D", 2), ("ABC", 3)]
    assert len(avisos) == 1 and '"Volumes"' in avisos[0] and '"Sku1"' in avisos[0]
    # com o mapa que ficou guardado na Marchef (Volumes -> a mesma coluna) continua lendo
    guardado = {**{k: {"nome": ep._normalizar_texto(CAB_PARES[j]), "pos": j} for k, j in COL_PARES.items()},
                "volumes": {"nome": "volumes", "pos": 17}}
    pedidos, rej = ep.ler_planilha(conteudo, "m.xlsx", CNPJ, guardado=guardado)
    assert rej == [] and pedidos[0]["itens"] == 2


def test_colunas_do_modelo_no_lugar_do_primeiro_par_valem_como_item_com_aviso():
    conteudo = _planilha_cab(["SKU do produto", "Quantidade", "Sku2", "Quantidade2"], [(_fixos(7878), ["D21TCI30510D", 2])])
    avisos: list = []
    pedidos, rej = ep.ler_planilha(conteudo, "m.xlsx", CNPJ, indicado={"linha": 0, "colunas": COL_PARES}, avisos=avisos)
    assert rej == []
    assert [(i["sku"], i["quantidade"]) for i in pedidos[0]["itens_lista"]] == [("D21TCI30510D", 2)]
    assert len(avisos) == 1 and '"SKU do produto"' in avisos[0]


def test_planilha_sem_alteracao_nao_gera_aviso_e_vizinho_que_e_campo_do_pedido_nao_vira_quantidade():
    avisos: list = []
    conteudo = _planilha_cab(["Sku1", "Quantidade1"], [(_fixos(1), ["ABC", 2])])
    assert ep.ler_planilha(conteudo, "m.xlsx", CNPJ, indicado={"linha": 0, "colunas": COL_PARES}, avisos=avisos)[1] == []
    assert ep.ler_planilha(_planilha([_linha()]), "p.xlsx", CNPJ, avisos=avisos)[1] == []
    assert avisos == []
    # Sku1 sem quantidade e a coluna ao lado e outro SKU ou um campo do pedido: nao adivinha
    assert ep._pares_de_itens(["Sku1", "Sku2", "Quantidade2"]) == [(1, 2)]
    assert ep._pares_de_itens(["Sku1", "Bairro", "Sku2", "Quantidade2"], ocupadas={1}) == [(2, 3)]
