# -*- coding: utf-8 -*-
"""
Testes da posicao DOCA (espera do recebimento por etapas).
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_wms_doca -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_doca  # noqa: E402
import wms_pedidos  # noqa: E402


class TestDoca(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = wms_doca.conectar(self.db)
        self.pid = 1
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, dun, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA', 'MARIA DOLORES', 48, '724000170000', NULL, 6, 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_garantir_doca_cria_uma_vez(self):
        self.assertEqual(wms_doca.garantir_doca(self.conn), "DOC1-P1")
        self.assertEqual(wms_doca.garantir_doca(self.conn), "DOC1-P1")
        self.assertEqual(wms.obter_area(self.conn, "DOC1")["tipo"], "DOCA")
        self.assertEqual(len(wms.listar_posicoes(self.conn, area="DOC1")), 1)

    def test_e_doca(self):
        self.assertTrue(wms_doca.e_doca("doc1-p1"))
        self.assertFalse(wms_doca.e_doca("C1-E1-N1"))
        self.assertFalse(wms_doca.e_doca(None))

    def test_saldo_na_doca_atende_pedido(self):
        # decisao do Hugo 22/11: a doca vale como posicao comum na reserva
        wms_doca.garantir_doca(self.conn)
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=10,
                                lote="L1", validade="2027-01-01", destino="DOC1-P1")
        lotes = wms_pedidos.disponivel_por_lote(self.conn, self.pid)
        self.assertEqual([(l["posicao"], l["disponivel"]) for l in lotes], [("DOC1-P1", 10.0)])

    def test_temperatura_do_cadastro(self):
        wms.definir_temperatura_produto(self.conn, self.pid, "REFRIGERADO")
        self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "REFRIGERADO")

    def test_temperatura_deduzida_de_onde_esta(self):
        wms.criar_area(self.conn, "CG1", "CAM_CONGELADA"); wms.criar_posicao(self.conn, "CG1-P1")
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=5, destino="CG1-P1", validade="2027-01-01")
        self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "CONGELADO")

    def test_temperatura_desconhecida_e_doca_nao_conta(self):
        wms_doca.garantir_doca(self.conn)
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=5, destino="DOC1-P1", validade="2027-01-01")
        self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "")

    def test_temperatura_invalida(self):
        with self.assertRaises(wms.ErroWMS):
            wms.definir_temperatura_produto(self.conn, self.pid, "MORNO")

    def test_temperatura_vazia_limpa(self):
        wms.definir_temperatura_produto(self.conn, self.pid, "CONGELADO")
        self.assertEqual(wms.definir_temperatura_produto(self.conn, self.pid, "")["temperatura"], "")


if __name__ == "__main__":
    unittest.main()
