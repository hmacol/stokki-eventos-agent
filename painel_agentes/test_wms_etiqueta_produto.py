# -*- coding: utf-8 -*-
"""
test_wms_etiqueta_produto.py

Etiqueta de mercadoria com QR de lote e validade (WMS fase 2).
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_wms_etiqueta_produto -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_caixas  # noqa: E402
import wms_etiqueta_produto as etq  # noqa: E402
import wms_pedidos  # noqa: E402


class TestConteudoQR(unittest.TestCase):
    def test_monta_o_texto_do_qr(self):
        self.assertEqual(etq.conteudo_qr("72400017", "L-A", "2026-10-15"),
                         "FL|72400017|L-A|2026-10-15")

    def test_sem_validade_deixa_o_campo_vazio(self):
        self.assertEqual(etq.conteudo_qr("72400017", "L-A", None), "FL|72400017|L-A|")

    def test_qr_com_serial(self):
        self.assertEqual(etq.conteudo_qr("72400017", "L-A", "2026-10-15", "C0000001"),
                         "FL|72400017|L-A|2026-10-15|C0000001")
        self.assertEqual(etq.parse_qr("FL|72400017|L-A|2026-10-15|C0000001")["serial"], "C0000001")

    def test_parse_da_etiqueta_antiga_tem_serial_vazio(self):
        # substitui test_parse_devolve_os_tres_campos
        self.assertEqual(etq.parse_qr("FL|72400017|L-A|2026-10-15"),
                         {"sku": "72400017", "lote": "L-A", "validade": "2026-10-15", "serial": ""})

    def test_parse_ignora_o_que_nao_e_nosso(self):
        self.assertIsNone(etq.parse_qr("POS:C9-E1-N1"))
        self.assertIsNone(etq.parse_qr("7891234567890"))

    def test_ida_e_volta(self):
        texto = etq.conteudo_qr("SKU1", "LOTE 2", "2027-01-31")
        self.assertEqual(etq.parse_qr(texto)["lote"], "LOTE 2")


class TestLerCodigo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = wms_pedidos.conectar(Path(self._tmp.name) / "dados.db")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, '72400017', 'COXINHA', 'MARIA DOLORES', "
            "'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_bipar_a_etiqueta_traz_produto_lote_e_validade(self):
        r = wms.ler_codigo(self.conn, "FL|72400017|L-A|2026-10-15")
        self.assertEqual(r["tipo"], "produto_lote")
        self.assertEqual(r["produtos"][0]["id"], 1)
        self.assertEqual(r["lote"], "L-A")
        self.assertEqual(r["validade"], "2026-10-15")

    def test_etiqueta_de_produto_que_nao_existe(self):
        r = wms.ler_codigo(self.conn, "FL|NAOEXISTE|L-A|2026-10-15")
        self.assertEqual(r["tipo"], "desconhecido")


class TestPDF(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = wms_pedidos.conectar(Path(self._tmp.name) / "dados.db")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, unidade, "
            "qtd_por_caixa, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA ZC 5KG PCT', 'MARIA DOLORES', 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_gera_pdf_valido(self):
        pdf = etq.gerar_etiquetas_produto_pdf(self.conn, 1, "L-A", "2026-10-15", copias=1)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)

    def test_mais_copias_geram_pdf_maior(self):
        # Contar '/Type /Page' no PDF nao serve: a arvore de paginas usa
        # '/Type /Pages', que contem a mesma substring. Comparar tamanho e
        # o jeito honesto de provar que as copias sairam.
        um = etq.gerar_etiquetas_produto_pdf(self.conn, 1, "L-A", "2026-10-15", copias=1)
        tres = etq.gerar_etiquetas_produto_pdf(self.conn, 1, "L-A", "2026-10-15", copias=3)
        self.assertGreater(len(tres), len(um))

    def test_produto_inexistente_da_erro_claro(self):
        with self.assertRaises(wms.ErroWMS):
            etq.gerar_etiquetas_produto_pdf(self.conn, 999, "L-A", "2026-10-15")

    def test_sem_produto_id_da_erro_claro_em_vez_de_estourar(self):
        # M3: a rota chama com request.args.get('produto_id', type=int), que
        # vira None quando o parametro nao veio -- antes dava int(None) e
        # HTTP 500; agora e ErroWMS, que a rota traduz em 400.
        with self.assertRaises(wms.ErroWMS):
            etq.gerar_etiquetas_produto_pdf(self.conn, None, "L-A", "2026-10-15")


class TestProdutoSemSKU(unittest.TestCase):
    """
    M4: produto sem SKU gerava QR 'FL||LOTE|VAL', que ler_codigo() nao
    resolve de volta -- etiqueta colada na caixa que ninguem consegue
    bipar. Decisao: cai pro EAN (o wms.buscar_por_codigo procura por EAN,
    DUN e SKU, entao a ida e volta funciona igual); sem SKU e sem EAN,
    recusa em vez de imprimir etiqueta cega.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = wms_pedidos.conectar(Path(self._tmp.name) / "dados.db")
        # cadastro rapido do galpao: tem EAN, nao tem SKU
        self.conn.execute(
            "INSERT INTO wms_produtos (id, sku, descricao, embarcador, ean, unidade, qtd_por_caixa, "
            "atualizado_em) VALUES (1, '', 'PRODUTO MANUAL', 'MARIA DOLORES', '7891234567895', "
            "'UN', 1, '2026-09-22 10:00:00')")
        # sem SKU e sem EAN: nao ha como identificar no QR
        self.conn.execute(
            "INSERT INTO wms_produtos (id, sku, descricao, embarcador, unidade, qtd_por_caixa, "
            "atualizado_em) VALUES (2, '', 'PRODUTO SEM CODIGO', 'MARIA DOLORES', 'UN', 1, "
            "'2026-09-22 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_sem_sku_usa_o_ean_e_o_qr_volta_a_ser_legivel(self):
        pdf = etq.gerar_etiquetas_produto_pdf(self.conn, 1, "L-A", "2026-10-15")
        self.assertTrue(pdf.startswith(b"%PDF"))
        texto = etq.conteudo_qr(etq.identificador_do_produto(
            dict(self.conn.execute("SELECT * FROM wms_produtos WHERE id = 1").fetchone())),
            "L-A", "2026-10-15")
        self.assertEqual(texto, "FL|7891234567895|L-A|2026-10-15")
        lido = wms.ler_codigo(self.conn, texto)
        self.assertEqual(lido["tipo"], "produto_lote")
        self.assertEqual(lido["produtos"][0]["id"], 1)

    def test_produto_sem_sku_e_sem_ean_ganha_codigo_interno_em_vez_de_recusar(self):
        # substitui o teste que esperava ErroWMS
        pdf = etq.gerar_etiquetas_produto_pdf(self.conn, 2, "L-A", "2026-10-15")
        self.assertTrue(pdf.startswith(b"%PDF"))
        codigo = self.conn.execute("SELECT codigo_interno FROM wms_produtos WHERE id = 2").fetchone()[0]
        self.assertEqual(codigo, "FLP000001")
        r = wms.ler_codigo(self.conn, etq.conteudo_qr(codigo, "L-A", "2026-10-15"))
        self.assertEqual(r["tipo"], "produto_lote")
        self.assertEqual(r["produtos"][0]["id"], 2)


class TestCorteDeTexto(unittest.TestCase):
    """M5: a etiqueta vai na caixa que o operador le -- 'GRANDE' virando
    'GRAND' e pior do que faltar a palavra inteira."""

    def test_curto_passa_inteiro(self):
        self.assertEqual(etq._cortar("COXINHA FESTA", 60), "COXINHA FESTA")

    def test_corta_por_palavra_e_marca_com_reticencias(self):
        texto = "BOLINHA DE QUEIJO FESTA ZC 5KG PCT EMBALAGEM GRANDE ESPECIAL"
        cortado = etq._cortar(texto, 50)
        self.assertLessEqual(len(cortado), 50)
        self.assertTrue(cortado.endswith("…"))
        # nenhuma palavra picada no meio
        self.assertTrue(texto.startswith(cortado[:-1].rstrip()))
        self.assertIn(cortado[:-1].rstrip().split()[-1], texto.split())


class TestCaixasEtiqueta(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = wms_caixas.conectar(Path(self._tmp.name) / "dados.db")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA ZC 5KG PCT', 'MARIA DOLORES', 48, NULL, 6, 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _criar(self, n=1):
        return wms_caixas.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15",
                                       qtd_un=None, quantidade=n)["seriais"]

    def test_pdf_de_caixas_tem_uma_pagina_por_serial(self):
        import re
        seriais = self._criar(3)
        pdf = etq.gerar_pdf_caixas(self.conn, seriais)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf)), 3)

    def test_pdf_recusa_serial_cancelado_ou_inexistente(self):
        seriais = self._criar(2)
        self.conn.execute("UPDATE wms_caixas SET situacao = 'CANCELADA' WHERE serial = ?", (seriais[1],))
        self.conn.commit()
        with self.assertRaises(wms.ErroWMS):
            etq.gerar_pdf_caixas(self.conn, seriais)
        with self.assertRaises(wms.ErroWMS):
            etq.gerar_pdf_caixas(self.conn, ["C9999999"])
        with self.assertRaises(wms.ErroWMS):
            etq.gerar_pdf_caixas(self.conn, [])

    def test_reimpressao_de_caixa_em_estoque_funciona(self):
        s = self._criar(1)
        self.conn.execute("UPDATE wms_caixas SET situacao = 'EM_ESTOQUE' WHERE serial = ?", (s[0],))
        self.conn.commit()
        self.assertTrue(etq.gerar_pdf_caixas(self.conn, s).startswith(b"%PDF"))

    def test_ler_codigo_qr_com_serial_vira_caixa(self):
        self._criar(1)
        r = wms.ler_codigo(self.conn, "FL|72400017|L1|2026-12-15|C0000001")
        self.assertEqual((r["tipo"], r["caixa"]["serial"]), ("caixa", "C0000001"))

    def test_ler_codigo_com_serial_inexistente(self):
        r = wms.ler_codigo(self.conn, "FL|72400017|L1|2026-12-15|C0000099")
        self.assertEqual((r["tipo"], r["serial"]), ("caixa_inexistente", "C0000099"))

    def test_ler_codigo_com_serial_em_conexao_sem_tabelas_de_caixa(self):
        conn = wms_pedidos.conectar(Path(self._tmp.name) / "outro.db")
        try:
            r = wms.ler_codigo(conn, "FL|72400017|L1|2026-12-15|C0000001")
            self.assertEqual(r["tipo"], "caixa_inexistente")
        finally:
            conn.close()

    def test_qr_sem_serial_nao_vira_caixa(self):
        self._criar(1)
        self.assertEqual(wms.ler_codigo(self.conn, "FL|72400017|L1|2026-12-15")["tipo"], "produto_lote")

    def test_buscar_pelo_codigo_interno(self):
        codigo = wms_caixas.garantir_codigo_interno(self.conn, 1)
        achados = wms.buscar_por_codigo(self.conn, codigo)
        self.assertEqual([(p["id"], p["lido_como"]) for p in achados], [(1, "INTERNO")])
        self.assertEqual(len(wms.buscar_por_codigo(self.conn, codigo.lower())), 1)


if __name__ == "__main__":
    unittest.main()
