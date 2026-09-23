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
import wms_etiqueta_produto as etq  # noqa: E402
import wms_pedidos  # noqa: E402


class TestConteudoQR(unittest.TestCase):
    def test_monta_o_texto_do_qr(self):
        self.assertEqual(etq.conteudo_qr("72400017", "L-A", "2026-10-15"),
                         "FL|72400017|L-A|2026-10-15")

    def test_sem_validade_deixa_o_campo_vazio(self):
        self.assertEqual(etq.conteudo_qr("72400017", "L-A", None), "FL|72400017|L-A|")

    def test_parse_devolve_os_tres_campos(self):
        self.assertEqual(etq.parse_qr("FL|72400017|L-A|2026-10-15"),
                         {"sku": "72400017", "lote": "L-A", "validade": "2026-10-15"})

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

    def test_sem_sku_e_sem_ean_recusa_em_vez_de_imprimir_etiqueta_cega(self):
        with self.assertRaises(wms.ErroWMS):
            etq.gerar_etiquetas_produto_pdf(self.conn, 2, "L-A", "2026-10-15")


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


if __name__ == "__main__":
    unittest.main()
