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


if __name__ == "__main__":
    unittest.main()
