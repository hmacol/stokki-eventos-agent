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

    def test_garantir_tabela_nao_comita_transacao_aberta(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.execute("INSERT INTO t VALUES (1)")  # transacao aberta, sem commit
        ppc.garantir_tabela(conn)
        ppc.garantir_tabela(conn)  # idempotente
        conn.rollback()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
        finally:
            conn.close()

    def test_indice_unico_impede_duas_pausas_abertas_do_mesmo_codigo(self):
        import sqlite3
        conn = ppc.conectar(self.db)
        try:
            conn.execute("INSERT INTO pedidos_pausados_cliente (codigo, pausado_em) VALUES ('PS-10', ?)", (AGORA,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO pedidos_pausados_cliente (codigo, pausado_em) VALUES ('PS-10', ?)", (AGORA,))
        finally:
            conn.close()

    def test_abrir_com_corrida_devolve_a_pausa_existente(self):
        conn = ppc.conectar(self.db)
        try:
            conn.execute("INSERT INTO pedidos_pausados_cliente (codigo, pausado_em) VALUES ('PS-10', ?)", (AGORA,))
            conn.commit()
            existente = conn.execute("SELECT id FROM pedidos_pausados_cliente").fetchone()["id"]
            real = conn.execute

            class Espiao:  # o 1o SELECT "nao enxerga" a linha: simula o outro clique que ganhou a corrida
                _viu = False

                def __getattr__(self, n):
                    return getattr(conn, n)

                def execute(self, sql, *a):
                    if sql.lstrip().startswith("SELECT id") and not self._viu:
                        self._viu = True
                        return real("SELECT NULL AS id WHERE 0")
                    return real(sql, *a)

            self.assertEqual(ppc.abrir(Espiao(), "PS-10", 1, 2, [3], AGORA), existente)
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
