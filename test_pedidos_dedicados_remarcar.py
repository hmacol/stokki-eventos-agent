# -*- coding: utf-8 -*-
"""Marcar de novo depois de reagendar pela pagina de ciencia (design: secao 2)."""
import sqlite3
import unittest

import pedidos_dedicados as pd


class RemarcarTest(unittest.TestCase):
    def setUp(self):
        self.conn = pd.conectar(":memory:")

    def tearDown(self):
        self.conn.close()

    def _marcar_e_remover(self, codigo, service_id, por):
        pd.marcar(self.conn, [{"codigo_pedido": codigo, "service_id": service_id}], 100.0, "teste")
        pd.remover(self.conn, codigo_pedido=codigo, por=por)

    def test_ativo_continua_bloqueando(self):
        pd.marcar(self.conn, [{"codigo_pedido": "PS-1", "service_id": 11}], 100.0, "teste")
        codigos, servicos = pd.codigos_e_servicos_ja_marcados(self.conn)
        self.assertIn("PS-1", codigos)
        self.assertIn(11, servicos)

    def test_removido_pela_equipe_continua_bloqueando(self):
        self._marcar_e_remover("PS-2", 22, "equipe")
        codigos, servicos = pd.codigos_e_servicos_ja_marcados(self.conn)
        self.assertIn("PS-2", codigos)
        self.assertIn(22, servicos)

    def test_removido_pela_pagina_de_ciencia_libera_nova_marcacao(self):
        self._marcar_e_remover("PS-3", 33, pd.POR_CIENCIA_REAGENDADO)
        codigos, servicos = pd.codigos_e_servicos_ja_marcados(self.conn)
        self.assertNotIn("PS-3", codigos)
        self.assertNotIn(33, servicos)


if __name__ == "__main__":
    unittest.main()
