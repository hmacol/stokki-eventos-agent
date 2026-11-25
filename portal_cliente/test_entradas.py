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
import entradas  # noqa: E402

CNPJ = "12345678000195"          # o cliente (emitente da remessa)
CNPJ_FRESHLOG = "11222333000181"  # destinatário (galpão)
OUTRO = "99887766000155"
HOJE = date.today()
AMANHA = (HOJE + timedelta(days=1)).isoformat()


def _agora_txt():
    return ep._agora()


def xml_remessa(nf="41221", emitente=CNPJ, dest=CNPJ_FRESHLOG, tp_nf="1", itens=None, chave=None):
    """NF-e de remessa pra armazenagem: emitente = cliente, destinatário =
    Fresh Log. tpNF=1 (saída do ponto de vista de quem emite)."""
    # chave de acesso real: dígitos 7-20 são o CNPJ do emitente (a leitura confere isso)
    chave = chave or f"352609{emitente}55001000{nf.zfill(6)}1000000017"[:44]
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

    def test_chave_que_nao_bate_com_o_emitente_e_recusada(self):
        # revisão de segurança 23/11 (M1): chave de NF de outra empresa num XML que diz ser do cliente
        chave_alheia = f"352609{OUTRO}55001000" + "000777" + "1000000017"
        self.assertEqual(len(chave_alheia), 44)
        with self.assertRaises(ep.ErroEnvio) as c:
            entradas.ler_nfe_entrada(xml_remessa(emitente=CNPJ, chave=chave_alheia), "x.xml")
        self.assertIn("chave de acesso", str(c.exception))

    def test_xml_com_dtd_ou_entidade_e_recusado(self):
        # revisão de segurança 23/11 (B4): NF-e não usa DTD; recusa billion laughs e XXE antes de parsear
        base = xml_remessa().decode("utf-8")
        laughs = ('<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                  + base.split("?>", 1)[1].replace("<nNF>41221</nNF>", "<nNF>&b;</nNF>"))
        xxe = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
               + base.split("?>", 1)[1].replace("<nNF>41221</nNF>", "<nNF>&e;</nNF>"))
        for nome, conteudo in (("laughs", laughs), ("xxe", xxe), ("minusculo", laughs.replace("DOCTYPE", "doctype").replace("ENTITY", "entity"))):
            with self.assertRaises(ep.ErroEnvio, msg=nome) as c:
                entradas.ler_nfe_entrada(conteudo.encode("utf-8"), "x.xml")
            self.assertIn("DTD", str(c.exception), nome)
        utf16 = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE l [<!ENTITY a "x">]><r/>').encode("utf-16")
        with self.assertRaises(ep.ErroEnvio):
            entradas.ler_nfe_entrada(utf16, "u16.xml")

    def test_chave_que_bate_com_o_emitente_passa(self):
        nfe = entradas.ler_nfe_entrada(xml_remessa(), "x.xml")
        self.assertEqual(nfe["chave_nfe"][6:20], CNPJ)

    def test_itens_lista_vem_do_det(self):
        nfe = ep.ler_nfe(xml_remessa(itens=[("A", "7890000000001", "Prod A", "2.5", "CX", "10.00"),
                                            ("B", "SEM GTIN", "Prod B", 1, "UN", 3)]), "x.xml")
        self.assertEqual(nfe["itens"], 2)
        self.assertEqual(nfe["itens_lista"][0], {"linha": 1, "sku": "A", "ean": "7890000000001", "descricao": "Prod A",
                                                 "quantidade": 2.5, "unidade": "CX", "valor_unitario": 10.0})
        self.assertEqual(nfe["itens_lista"][1]["ean"], "")   # 'SEM GTIN' vira vazio
        self.assertEqual(nfe["itens_lista"][1]["quantidade"], 1.0)


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
        nomes = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("portal_entradas", nomes)
        self.assertIn("portal_entrada_itens", nomes)


class TestValidarEntrada(BasePortal):
    def _item(self, **kw):
        nfe = entradas.ler_nfe_entrada(xml_remessa(**kw), "nota.xml")
        return nfe

    def test_nfe_valida_sem_erros_com_itens(self):
        v = entradas.validar_entrada(self.conn, self._item(), CNPJ, self.config)
        self.assertTrue(v["ok"], v["erros"])
        self.assertEqual(v["avisos"], [])

    def test_chave_ja_registrada_por_outra_empresa_nao_vaza_nem_reabre(self):
        # revisão de segurança 23/11 (M1), defesa em profundidade: mesmo que uma chave alheia
        # chegue até aqui, nada da entrada do outro cliente aparece e nada é reaberto.
        item = self._item()
        chave = item["chave_nfe"]
        for status in ("ANUNCIADO", "CANCELADO"):
            self.conn.execute("DELETE FROM portal_entradas")
            self.conn.execute(
                "INSERT INTO portal_entradas (cnpj_embarcador, origem, chave_nfe, data_prevista, arquivo_path, status, stokki_codigo, "
                "criado_em, atualizado_em) VALUES (?, 'xml', ?, '2030-01-01', 'x.xml', ?, '#PE-9999', '2026-11-01 10:00:00', '2026-11-01 10:00:00')",
                (OUTRO, chave, status))
            self.conn.commit()
            v = entradas.validar_entrada(self.conn, item, CNPJ, self.config)
            self.assertFalse(v["ok"], status)
            self.assertIsNone(v["existente"], status)
            texto = " ".join(v["erros"] + v["avisos"])
            self.assertNotIn("#PE-9999", texto)
            self.assertNotIn("01/11/2026", texto)

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
        self.assertIn("não existe(m) no cadastro", v["erros"][0])

    # ── conferência produto por produto (spec v2, V4) ──

    def _catalogo(self, linhas):
        self.conn.execute("DELETE FROM wms_produtos")
        for i, (sku, ean, dun, qtd_cx, un) in enumerate(linhas, start=1):
            self.conn.execute("INSERT INTO wms_produtos VALUES (?, ?, ?, ?, ?, ?, 'CLIENTE TESTE', 48, ?, ?, 1, '2026-09-25 10:00:00')",
                              (i, 900 + i, sku, ean, dun, f"DESC {sku}", qtd_cx, un))
        self.conn.commit()

    def _cfg(self):
        return {"client_id": 48, "nome": "CLIENTE TESTE"}

    def test_conferencia_converte_cx_e_un(self):
        self._catalogo([("A", None, None, 12, "UN"), ("B", None, None, None, "UN")])
        item = {"itens_lista": [
            {"linha": 1, "sku": "A", "descricao": "a", "quantidade": 20, "unidade": "CX"},
            {"linha": 2, "sku": "A", "descricao": "a", "quantidade": 96, "unidade": "UN"},
            {"linha": 3, "sku": "B", "descricao": "b", "quantidade": 5, "unidade": "UN"},
            {"linha": 4, "sku": "B", "descricao": "b", "quantidade": 5, "unidade": "cx"},
        ]}
        c = entradas.conferir_itens(self.conn, item, self._cfg())
        self.assertEqual((c[0]["caixas"], c[0]["unidades"], c[0]["qtd_por_caixa"]), (20, 240, 12))
        self.assertEqual((c[1]["caixas"], c[1]["unidades"]), (8, 96))
        self.assertIsNone(c[2]["caixas"])
        self.assertEqual(c[2]["unidades"], 5)
        self.assertEqual(c[3]["caixas"], 5)
        self.assertIsNone(c[3]["unidades"])
        self.assertTrue(all(x["cadastrado"] for x in c))
        self.assertEqual(c[0]["descricao"], "DESC A")   # descrição do catálogo

    def test_conferencia_aceita_ean_e_dun(self):
        self._catalogo([("A", "7891111111111", "17891111111118", 6, "UN")])
        item = {"itens_lista": [
            {"linha": 1, "sku": "7891111111111", "descricao": "x", "quantidade": 1, "unidade": "UN"},
            {"linha": 2, "sku": "17891111111118", "descricao": "y", "quantidade": 1, "unidade": "UN"},
            {"linha": 3, "sku": "a", "descricao": "z", "quantidade": 1, "unidade": "UN"},
        ]}
        c = entradas.conferir_itens(self.conn, item, self._cfg())
        self.assertEqual([x["cadastrado"] for x in c], [True, True, True])

    def test_produto_fora_do_cadastro_trava_com_texto(self):
        item = self._item(itens=[("NUU001FD", "7891234567890", "MINI", 1, "UN", 1), ("X9", "", "FORA", 1, "UN", 1)])
        v = entradas.validar_entrada(self.conn, item, CNPJ, self.config)
        self.assertFalse(v["ok"])
        self.assertEqual(v["erros"], ["1 produto(s) não existe(m) no cadastro: X9. Peça o cadastro à Fresh Log."])
        self.assertEqual([x["cadastrado"] for x in v["conferencia"]], [True, False])

    def test_sem_catalogo_trava(self):
        # V4: sem nenhum produto do embarcador no cadastro, trava (na v1 era só aviso)
        self.conn.execute("DELETE FROM wms_produtos")
        self.conn.commit()
        v = entradas.validar_entrada(self.conn, self._item(), CNPJ, self.config)
        self.assertFalse(v["ok"])
        self.assertEqual(v["erros"], ["Nenhum produto seu está no cadastro da Fresh Log ainda. Peça o cadastro à Fresh Log antes de anunciar."])
        self.assertIsNone(v["conferencia"])

    def test_nota_ok_devolve_conferencia(self):
        v = entradas.validar_entrada(self.conn, self._item(), CNPJ, self.config)
        self.assertTrue(v["ok"])
        self.assertEqual(len(v["conferencia"]), 1)
        self.assertTrue(v["conferencia"][0]["cadastrado"])

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
                                             "anunciado": 25.0, "recebido": 22.0, "falta": 3.0, "sobra": 0.0, "situacao": "FALTA"}])
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

    def test_cancelar_criado_sem_id_abre_chamado(self):
        # o worker pode marcar CRIADO sem ainda ter achado o #PE (o timer do
        # WMS amarra depois) -- o cancelamento não pode decidir só pelo
        # stokki_id e deixar o #PE vivo sem chamado nenhum.
        _confirmar(self)
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'CRIADO', stokki_id = NULL, stokki_codigo = NULL")
        self.conn.commit()
        chamado_falso = mock.Mock()
        chamado_falso.criar_chamado.return_value = {"id": 55}
        chamado_falso.conectar.return_value = mock.MagicMock()
        with mock.patch.object(entradas, "_chamados", return_value=chamado_falso):
            r = entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, 1, CNPJ), "cliente", self._cliente(), {})
        self.assertTrue(r["aplicado"])
        self.assertEqual(r["chamado_id"], 55)
        self.assertIn("NF 41221", chamado_falso.criar_chamado.call_args.kwargs["assunto"])
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "CANCELADO")

    def test_cancelar_com_pe_possivelmente_criado_abre_chamado_mesmo_sem_status_criado(self):
        # revisão de segurança 23/11 (B2): resultado indeterminado / crash depois do store deixa a
        # entrada em NA_FILA ou ERRO com tentativas > 0, mas o #PE pode existir na Stokki.
        for stokki_status, tentativas in (("NA_FILA", 1), ("ERRO", 3)):
            self.conn.execute("DELETE FROM portal_entradas")
            self.conn.execute("DELETE FROM portal_entrada_itens")
            self.conn.commit()
            _confirmar(self)
            self.conn.execute("UPDATE portal_entradas SET stokki_status = ?, stokki_tentativas = ?, stokki_id = NULL, stokki_codigo = NULL",
                              (stokki_status, tentativas))
            self.conn.commit()
            chamado_falso = mock.Mock()
            chamado_falso.criar_chamado.return_value = {"id": 77}
            chamado_falso.conectar.return_value = mock.MagicMock()
            with mock.patch.object(entradas, "_chamados", return_value=chamado_falso):
                e = self.conn.execute("SELECT id FROM portal_entradas").fetchone()[0]
                r = entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, e, CNPJ), "cliente", self._cliente(), {})
            self.assertEqual(r["chamado_id"], 77, stokki_status)
            self.assertNotIn("não será criada", r["mensagem"], stokki_status)

    def test_cancelar_na_fila_sem_tentativa_nao_abre_chamado(self):
        _confirmar(self)   # nasce NA_FILA, tentativas 0: o worker nunca tocou
        chamado_falso = mock.Mock()
        with mock.patch.object(entradas, "_chamados", return_value=chamado_falso):
            r = entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, 1, CNPJ), "cliente", self._cliente(), {})
        chamado_falso.criar_chamado.assert_not_called()
        self.assertIsNone(r["chamado_id"])

    def test_cancelar_recusa_se_mudou_de_estado_entre_ler_e_gravar(self):
        _confirmar(self)
        e = entradas.buscar_entrada(self.conn, 1, CNPJ)
        # mudou por fora depois que a rota leu `e`
        self.conn.execute("UPDATE portal_entradas SET status = 'CHEGOU'")
        self.conn.commit()
        with self.assertRaises(ep.ErroEnvio):
            entradas.cancelar_entrada(self.conn, e, "cliente", self._cliente(), {})
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "CHEGOU")

    def test_cancelar_nao_mexe_no_wms_com_enderecamento_ou_fora_de_esperado(self):
        # cenário artificial (a entrada em si segue ANUNCIADO e é cancelada
        # nos dois casos), só pra provar o guard SQL do UPDATE em
        # wms_recebimentos: nunca mexe fora de ESPERADO sem endereçamento.
        _confirmar(self)
        self.conn.execute("CREATE TABLE wms_recebimentos (id INTEGER PRIMARY KEY, id_stokki INTEGER, codigo TEXT, estado TEXT, atualizado_em TEXT)")
        self.conn.execute("CREATE TABLE wms_recebimento_itens (id INTEGER PRIMARY KEY, recebimento_id INTEGER, linha INTEGER, sku TEXT, "
                          "qtd_embalagem REAL, qtd_un REAL, produto_id INTEGER, qtd_enderecada REAL DEFAULT 0, falta_un REAL DEFAULT 0)")
        self.conn.execute("INSERT INTO wms_recebimentos VALUES (7, 2497, '#PE-2497', 'ESPERADO', '')")
        self.conn.execute("INSERT INTO wms_recebimento_itens VALUES (1, 7, 1, 'NUU001FD', 20, 20, 1, 5, 0)")  # já endereçou parte
        self.conn.execute("UPDATE portal_entradas SET wms_recebimento_id = 7")
        self.conn.commit()
        entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, 1, CNPJ), "cliente", self._cliente(), {})
        self.assertEqual(self.conn.execute("SELECT estado FROM wms_recebimentos WHERE id = 7").fetchone()[0], "ESPERADO")
        self.assertEqual(entradas.buscar_entrada(self.conn, 1, CNPJ)["status"], "CANCELADO")

        _confirmar(self, nf="41222")
        self.conn.execute("UPDATE portal_entradas SET wms_recebimento_id = 7 WHERE id = 2")
        self.conn.execute("UPDATE wms_recebimentos SET estado = 'DIVERGENCIA'")
        self.conn.commit()
        entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, 2, CNPJ), "cliente", self._cliente(), {})
        self.assertEqual(self.conn.execute("SELECT estado FROM wms_recebimentos WHERE id = 7").fetchone()[0], "DIVERGENCIA")
        self.assertEqual(entradas.buscar_entrada(self.conn, 2, CNPJ)["status"], "CANCELADO")

    def test_cancelar_guarda_motivo_e_manda_pro_chamado(self):
        _confirmar(self)
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'CRIADO', stokki_id = 2497, stokki_codigo = '#PE-2497', "
                          "observacoes = 'portão 2'")
        self.conn.commit()
        chamado_falso = mock.Mock()
        chamado_falso.criar_chamado.return_value = {"id": 55}
        chamado_falso.conectar.return_value = mock.MagicMock()
        with mock.patch.object(entradas, "_chamados", return_value=chamado_falso):
            entradas.cancelar_entrada(self.conn, entradas.buscar_entrada(self.conn, 1, CNPJ), "cliente", self._cliente(), {},
                                      motivo="pedido cancelado pelo comprador")
        e = entradas.buscar_entrada(self.conn, 1, CNPJ)
        self.assertEqual(e["observacoes"], "portão 2 | Cancelada: pedido cancelado pelo comprador")
        msg = chamado_falso.mensagem_sistema.call_args.args[2]
        self.assertIn("pedido cancelado pelo comprador", msg)


class TestFlagCliente(BasePortal):
    def test_nasce_desligada_e_liga_por_cnpj(self):
        self.assertFalse(entradas.config_entradas_cliente(self.conn, CNPJ)["entradas_ativo"])
        entradas.definir_entradas_ativo(self.conn, CNPJ, True)
        self.assertTrue(entradas.config_entradas_cliente(self.conn, CNPJ)["entradas_ativo"])
        self.assertTrue(entradas.entradas_ativas_para(self.conn, [OUTRO, CNPJ]))
        self.assertFalse(entradas.entradas_ativas_para(self.conn, [OUTRO]))
        entradas.definir_entradas_ativo(self.conn, CNPJ, False)
        self.assertFalse(entradas.config_entradas_cliente(self.conn, CNPJ)["entradas_ativo"])


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

    def test_amarrar_por_chave_nao_liga_pe_de_outro_embarcador(self):
        # revisão de segurança 23/11 (B1): a chave bate, mas o #PE é de outro embarcador (stkkc 77)
        criados = _confirmar(self)
        chave = entradas.buscar_entrada(self.conn, criados[0]["id"], CNPJ)["chave_nfe"]
        self.conn.execute("UPDATE interno SET stkkc_id = 77 WHERE cnpj_embarcador = ?", (OUTRO,))
        self.conn.execute("INSERT INTO wms_recebimentos (id, id_stokki, codigo, stkkc_id, situacao, estado) VALUES (9, 2700, '#PE-2700', '77', 'Em transito', 'ESPERADO')")
        self.conn.commit()
        self.assertIsNone(entradas.amarrar_recebimento(self.conn, 9, 2700, "#PE-2700", "77", chave_nfe=chave, ref_pedido=""))
        e = entradas.buscar_entrada(self.conn, 1, CNPJ)
        self.assertIsNone(e["wms_recebimento_id"])

    def test_amarrar_por_chave_nao_rouba_entrada_ja_ligada_a_outro_pe(self):
        # entrada já ligada ao #PE 2497 (stokki_id): um #PE NOVO (2600) com a
        # MESMA chave de NF-e não pode roubar essa entrada -- caso raro
        # (chave duplicada), mas o guard tem que recusar em vez de
        # sobrescrever o stokki_id de quem já estava ligado.
        criados = _confirmar(self)
        chave = entradas.buscar_entrada(self.conn, criados[0]["id"], CNPJ)["chave_nfe"]
        self.conn.execute("UPDATE portal_entradas SET stokki_id = 2497, stokki_codigo = '#PE-2497', stokki_status = 'CRIADO'")
        self.conn.commit()
        self._rec(rid=8, id_stokki=2600)
        eid = entradas.amarrar_recebimento(self.conn, 8, 2600, "#PE-2600", "48", chave_nfe=chave, ref_pedido="")
        self.assertIsNone(eid)
        e = entradas.buscar_entrada(self.conn, 1, CNPJ)
        self.assertEqual((e["stokki_id"], e["stokki_codigo"]), (2497, "#PE-2497"))

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


class TestSituacaoDaLista(BasePortal):
    """Rótulos em linguagem de operação (spec v2, seção 3), itens recebidos,
    sobra/falta por SKU na conferência e a marca da equipe."""

    def setUp(self):
        super().setUp()
        self.conn.execute("CREATE TABLE wms_recebimentos (id INTEGER PRIMARY KEY, id_stokki INTEGER, codigo TEXT, estado TEXT, situacao TEXT, "
                          "observacao_divergencia TEXT DEFAULT '', encerrado_em TEXT DEFAULT '')")
        self.conn.execute("CREATE TABLE wms_recebimento_itens (id INTEGER PRIMARY KEY, recebimento_id INTEGER, linha INTEGER, sku TEXT, "
                          "qtd_embalagem REAL, qtd_un REAL, produto_id INTEGER, qtd_enderecada REAL DEFAULT 0, falta_un REAL DEFAULT 0)")
        self.conn.commit()

    def _entrada(self, **campos):
        _confirmar(self, itens=[("NUU001FD", "7891234567890", "MINI", 10, "UN", 1), ("SKU-B", "", "B", 10, "UN", 1)])
        sets = ", ".join(f"{k} = ?" for k in campos)
        self.conn.execute(f"UPDATE portal_entradas SET {sets}", tuple(campos.values()))
        self.conn.commit()

    def _rotulo(self):
        return entradas.listar_entradas(self.conn, CNPJ)[0]["situacao_rotulo"]

    def _receber(self, estado, itens):
        """itens: [(sku, qtd_un, qtd_enderecada, falta_un)]"""
        self.conn.execute("INSERT INTO wms_recebimentos VALUES (7, 1188, '#PE-1188', ?, 'Recebido', '', '')", (estado,))
        for i, (sku, qtd, end, falta) in enumerate(itens, start=1):
            self.conn.execute("INSERT INTO wms_recebimento_itens (recebimento_id, linha, sku, qtd_embalagem, qtd_un, produto_id, qtd_enderecada, falta_un) "
                              "VALUES (7, ?, ?, ?, ?, 1, ?, ?)", (i, sku, qtd, qtd, end, falta))
        self.conn.execute("UPDATE portal_entradas SET wms_recebimento_id = 7")
        self.conn.commit()

    def test_situacao_rotulo_em_cada_estado(self):
        self._entrada(stokki_status="NA_FILA")
        self.assertEqual(self._rotulo(), "Enviando pra Stokki")
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'ENVIANDO'")
        self.conn.commit()
        self.assertEqual(self._rotulo(), "Enviando pra Stokki")
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'CRIADO', stokki_codigo = '#PE-1188'")
        self.conn.commit()
        self.assertEqual(self._rotulo(), "Criado na Stokki · #PE-1188")
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'ERRO', stokki_erro = 'x'")
        self.conn.commit()
        self.assertEqual(self._rotulo(), "Stokki recusou")
        self.conn.execute("UPDATE portal_entradas SET stokki_status = 'CRIADO', status = 'CANCELADO'")
        self.conn.commit()
        self.assertEqual(self._rotulo(), "Cancelado")
        self.conn.execute("UPDATE portal_entradas SET status = 'ENDERECADO'")
        self.conn.commit()
        self.assertEqual(self._rotulo(), "Encerrado")

    def test_recebendo_conta_itens_recebidos_e_total(self):
        self._entrada(stokki_status="CRIADO", stokki_codigo="#PE-1188", status="CHEGOU")
        self._receber("ESPERADO", [("NUU001FD", 10, 4, 0), ("SKU-B", 10, 0, 0)])
        e = entradas.listar_entradas(self.conn, CNPJ)[0]
        self.assertEqual(e["situacao_rotulo"], "Recebendo · 1 de 2 itens")
        self.assertEqual((e["itens_recebidos"], e["itens_total"]), (1, 2))

    def test_sem_recebimento_itens_zerados(self):
        self._entrada(stokki_status="CRIADO")
        e = entradas.listar_entradas(self.conn, CNPJ)[0]
        self.assertEqual((e["itens_recebidos"], e["itens_total"]), (0, 0))

    def test_divergencia_com_falta(self):
        self._entrada(stokki_status="CRIADO", status="DIVERGENCIA")
        self._receber("DIVERGENCIA", [("NUU001FD", 10, 0, 12), ("SKU-B", 10, 10, 0)])
        self.assertEqual(self._rotulo(), "Com divergência · falta 12 un")

    def test_linha_com_sobra_e_falta(self):
        self._entrada(stokki_status="CRIADO", status="DIVERGENCIA")
        self._receber("DIVERGENCIA", [("NUU001FD", 10, 12, 0), ("SKU-B", 10, 0, 10)])
        e = entradas.listar_entradas(self.conn, CNPJ)[0]
        a, b = e["conferencia"]
        self.assertEqual((a["sku"], a["situacao"], a["sobra"]), ("NUU001FD", "SOBRA", 2))
        self.assertEqual((b["sku"], b["situacao"], b["sobra"]), ("SKU-B", "FALTA", 0))
        self.assertEqual(e["situacao_rotulo"], "Com divergência · falta 10 un")

    def test_situacao_por_sku_ok_e_falta_sem_falta_congelada(self):
        # encerrado com recebido < anunciado mas sem falta_un gravada: ainda é FALTA
        self._entrada(stokki_status="CRIADO", status="ENDERECADO")
        self._receber("ENDERECADO", [("NUU001FD", 10, 10, 0), ("SKU-B", 10, 6, 0)])
        c = entradas.listar_entradas(self.conn, CNPJ)[0]["conferencia"]
        self.assertEqual([x["situacao"] for x in c], ["OK", "FALTA"])

    def test_em_andamento_recebido_menor_nao_e_falta(self):
        self._entrada(stokki_status="CRIADO", status="CHEGOU")
        self._receber("ESPERADO", [("NUU001FD", 10, 4, 0), ("SKU-B", 10, 0, 0)])
        c = entradas.listar_entradas(self.conn, CNPJ)[0]["conferencia"]
        self.assertEqual([x["situacao"] for x in c], ["OK", "OK"])

    def test_lancado_pela_equipe(self):
        self._entrada(enviado_por="equipe:hugo")
        self.assertTrue(entradas.listar_entradas(self.conn, CNPJ)[0]["lancado_pela_equipe"])
        self.conn.execute("UPDATE portal_entradas SET enviado_por = 'cliente'")
        self.conn.commit()
        self.assertFalse(entradas.listar_entradas(self.conn, CNPJ)[0]["lancado_pela_equipe"])


class TestRotaAnalisar(BasePortal):
    """Lote com várias notas: a que tem produto fora do cadastro trava sozinha
    (vai pra `rejeitados` com a conferência); as outras seguem confirmáveis."""

    @classmethod
    def setUpClass(cls):
        # antes do setUp: o BasePortal troca ep._RAIZ, e o app.py resolve
        # documentos_pedido/ a partir dele ao ser importado
        from test_endereco_entrega_api import _importar_app
        cls.app_mod = _importar_app()

    def _cliente_http(self):
        app = self.app_mod
        entradas.definir_entradas_ativo(self.conn, CNPJ, True)
        cliente = {"cnpj": CNPJ, "sender_id": 1, "nome": "CLIENTE TESTE", "cnpj_formatado": "12.345.678/0001-95",
                   "versao": "x", "sender_ids": [1],
                   "empresas": [{"cnpj": CNPJ, "cnpj_formatado": "12.345.678/0001-95", "sender_id": 1, "nome": "CLIENTE TESTE"}]}
        patches = [
            mock.patch.object(ep, "config_stokki_cliente", return_value={"client_id": 48, "nome": "CLIENTE TESTE"}),
            mock.patch.object(app.auth, "sessao_valida", lambda c, cnpj, v: dict(cliente)),
            mock.patch.object(app, "_CONFIG", self.config),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        (ep.PASTA_TEMP).mkdir(parents=True, exist_ok=True)
        app.app.config["TESTING"] = True
        tc = app.app.test_client()
        with tc.session_transaction() as s:
            s["cnpj"] = CNPJ
            s["v"] = "x"
        return tc

    def test_arquivo_e_modelo_exigem_a_aba_ativa_e_funcionam_com_ela(self):
        # revisão de segurança 23/11 (B5): as duas rotas também passam pelo guard da aba
        tc = self._cliente_http()
        self.assertEqual(tc.get("/api/entradas/modelo-planilha").status_code, 200)
        entradas.definir_entradas_ativo(self.conn, CNPJ, False)
        self.assertEqual(tc.get("/api/entradas/modelo-planilha").status_code, 403)
        self.assertEqual(tc.get("/api/entradas/1/arquivo").status_code, 403)
        entradas.definir_entradas_ativo(self.conn, CNPJ, True)
        self.assertEqual(tc.get("/api/entradas/1/arquivo").status_code, 404)   # ativa, mas não existe: 404 como antes

    def test_upload_com_arquivos_demais_e_recusado_antes_de_guardar_qualquer_um(self):
        # revisão de segurança 23/11 (B4): sem teto, um ZIP/upload cheio de XMLs enche _temporarios
        import io
        tc = self._cliente_http()
        n = entradas.MAX_ARQUIVOS_UPLOAD + 1
        arquivos = [(io.BytesIO(xml_remessa(nf=str(i))), f"n{i}.xml") for i in range(1, n + 1)]
        r = tc.post("/api/entradas/analisar", data={"arquivos": arquivos}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True)[:200])
        self.assertIn("arquivos", r.get_json()["erro"])
        self.assertEqual(list(ep.PASTA_TEMP.glob("*")), [])

    def test_conferencia_trava_so_a_nota_com_produto_fora(self):
        import io
        tc = self._cliente_http()
        boa = xml_remessa(nf="100", chave="35260912345678000195550010000001001000000011")
        ruim = xml_remessa(nf="101", chave="35260912345678000195550010000001011000000012",
                           itens=[("NUU001FD", "7891234567890", "MINI", 1, "UN", 1), ("X9", "", "FORA", 1, "UN", 1)])
        r = tc.post("/api/entradas/analisar", data={"arquivos": [(io.BytesIO(boa), "boa.xml"), (io.BytesIO(ruim), "ruim.xml")]},
                    content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(len(j["itens"]), 1)
        self.assertEqual(len(j["rejeitados"]), 1)
        self.assertEqual(j["itens"][0]["numero_nf"], "100")
        self.assertTrue(j["itens"][0]["conferencia"])
        rej = j["rejeitados"][0]
        self.assertIn("X9", rej["erro"])
        self.assertEqual([x["cadastrado"] for x in rej["conferencia"]], [True, False])


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


if __name__ == "__main__":
    unittest.main()
