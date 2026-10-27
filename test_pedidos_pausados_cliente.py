# -*- coding: utf-8 -*-
import os
import tempfile
import unittest

import pedidos_pausados_cliente as ppc

AGORA = "2026-10-27 10:00:00"
DEPOIS = "2026-10-27 15:00:00"


class PedidosPausadosClienteTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.db) and os.remove(self.db))

    def _abrir(self, codigo="PS-10", ids=(1,)):
        conn = ppc.conectar(self.db)
        try:
            return ppc.abrir(conn, codigo, 100, 200, list(ids), AGORA)
        finally:
            conn.close()

    def test_abrir_e_fechar(self):
        self._abrir()
        self.assertEqual(ppc.codigos_pausados(self.db), {"PS-10"})
        conn = ppc.conectar(self.db)
        try:
            self.assertTrue(ppc.fechar(conn, "PS-10", "retomado", DEPOIS))
            self.assertFalse(ppc.fechar(conn, "PS-10", "retomado", DEPOIS))
            linha = conn.execute("SELECT * FROM pedidos_pausados_cliente").fetchone()
            self.assertEqual((linha["retomado_em"], linha["fim"]), (DEPOIS, "retomado"))
        finally:
            conn.close()
        self.assertEqual(ppc.codigos_pausados(self.db), set())

    def test_abrir_duas_vezes_nao_duplica(self):
        a = self._abrir()
        b = self._abrir()
        self.assertEqual(a, b)
        conn = ppc.conectar(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pedidos_pausados_cliente").fetchone()[0], 1)
        finally:
            conn.close()

    def test_separar_pausados_pelo_codigo_base(self):
        self._abrir("PS-10")
        servicos = [{"id": 1, "code": "#PS-10-R1"}, {"id": 2, "code": "#PS-11"}]
        seguem, fora = ppc.separar_pausados(servicos, self.db)
        self.assertEqual([s["id"] for s in seguem], [2])
        self.assertEqual([s["id"] for s in fora], [1])
        self.assertEqual(ppc.pausados_por_servico(servicos, self.db), {1})

    def test_servico_com_varios_codigos(self):
        self._abrir("PS-20")
        servicos = [{"id": 5, "code": "#PS-19, PS-20"}, {"id": 6, "code": "#PS-21, PS-22"}]
        seguem, fora = ppc.separar_pausados(servicos, self.db)
        self.assertEqual([s["id"] for s in fora], [5])
        self.assertEqual([s["id"] for s in seguem], [6])
        self.assertEqual(ppc.pausados_por_servico(servicos, self.db), {5})

    def test_erro_de_banco_devolve_vazio(self):
        ruim = os.path.join(self.db, "nao", "existe.db")
        servicos = [{"id": 1, "code": "#PS-10"}, {"id": 2, "code": "#PS-11"}]
        self.assertEqual(ppc.codigos_pausados(ruim), set())
        self.assertEqual(ppc.separar_pausados(servicos, ruim), (servicos, []))
        self.assertEqual(ppc.pausados_por_servico(servicos, ruim), set())

    def test_fechado_nao_conta(self):
        self._abrir("PS-10")
        conn = ppc.conectar(self.db)
        try:
            ppc.fechar(conn, "PS-10", "cancelado", DEPOIS)
        finally:
            conn.close()
        servicos = [{"id": 1, "code": "#PS-10"}]
        self.assertEqual(ppc.separar_pausados(servicos, self.db), (servicos, []))
        self.assertEqual(ppc.pausados_por_servico(servicos, self.db), set())
        # pode pausar de novo depois de fechada
        self._abrir("PS-10")
        self.assertEqual(ppc.codigos_pausados(self.db), {"PS-10"})


if __name__ == "__main__":
    unittest.main()
