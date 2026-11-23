# -*- coding: utf-8 -*-
import sqlite3
import unittest

from coletas import banco


class TestBanco(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        banco.garantir_esquema(self.conn)
        self.addCleanup(self.conn.close)

    def _pedido(self, coleta, estado):
        self.conn.execute("INSERT INTO expedicao_coletas_pedidos (coleta_id, id_stokki, codigo, estado) "
                          "VALUES (?, 7, 'PS-7', ?)", (coleta, estado))

    def test_esquema_pode_rodar_de_novo(self):
        banco.garantir_esquema(self.conn)
        self.assertEqual(banco.numero(12), "COL-12")

    def test_pedido_so_em_uma_folha_viva(self):
        self._pedido(1, "NA_FOLHA")
        with self.assertRaises(sqlite3.IntegrityError):
            self._pedido(2, "NA_FOLHA")

    def test_pedido_que_nao_saiu_entra_em_outra_folha(self):
        self._pedido(1, "NAO_SAIU")
        self._pedido(2, "NA_FOLHA")


if __name__ == "__main__":
    unittest.main()
