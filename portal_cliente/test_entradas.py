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


if __name__ == "__main__":
    unittest.main()
