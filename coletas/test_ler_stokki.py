# -*- coding: utf-8 -*-
import sqlite3
import unittest
from datetime import datetime
from types import SimpleNamespace

from coletas import banco, ler_stokki


class _Catalogo:
    TIPOS = {"J3 FLEX": "RETIRADA", "FRESHLOG": "ENTREGA"}

    def resolver(self, nome, cnpj=""):
        self.cnpj = cnpj
        return SimpleNamespace(tipo=self.TIPOS.get(nome.upper()))


class TestLerStokki(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        banco.garantir_esquema(self.conn)
        self.addCleanup(self.conn.close)
        self.soltou = 0

    def _ler(self, linhas, hora=8, conta="principal"):
        def listar(config, c):
            for l in linhas:
                if isinstance(l, Exception):
                    raise l
                yield l

        def soltar():
            self.soltou += 1
        return ler_stokki.ler(self.conn, {}, listar=listar, catalogo=_Catalogo(),
                              agora=datetime(2026, 11, 12, hora, 0), travar=lambda: conta, soltar=soltar)

    def _fila(self):
        return {r["id_stokki"]: dict(r) for r in self.conn.execute("SELECT * FROM expedicao_coletas_fila")}

    def test_primeira_leitura_marca_carga_inicial(self):
        self.assertEqual(self._ler([(1, "NONNA", "J3 FLEX"), (2, "ATW", "FRESHLOG")]), "ok")
        self._ler([(1, "NONNA", "J3 FLEX"), (2, "ATW", "FRESHLOG"),
                   (3, "PURO", "CLIENTE RETIRA 15.060.670/0001-70")], hora=9)
        f = self._fila()
        self.assertEqual([f[i]["carga_inicial"] for i in (1, 2, 3)], [1, 1, 0])
        self.assertEqual([f[i]["retirada"] for i in (1, 2, 3)], [1, 0, 1])
        self.assertEqual((f[1]["codigo"], f[1]["visto_primeiro_em"], f[1]["visto_ultimo_em"]),
                         ("PS-1", "2026-11-12 08:00:00", "2026-11-12 09:00:00"))
        self.assertEqual(self.soltou, 2)

    def test_quem_sumiu_ganha_saiu_em_e_quem_voltou_perde(self):
        self._ler([(1, "NONNA", "J3 FLEX"), (2, "NONNA", "J3 FLEX")])
        self._ler([(2, "NONNA", "J3 FLEX")], hora=9)
        self.assertEqual(self._fila()[1]["saiu_em"], "2026-11-12 09:00:00")
        self._ler([(2, "NONNA", "J3 FLEX")], hora=10)   # nao remarca a hora de quem ja saiu
        self.assertEqual(self._fila()[1]["saiu_em"], "2026-11-12 09:00:00")
        self._ler([(1, "NONNA", "J3 FLEX"), (2, "NONNA", "J3 FLEX")], hora=11)
        self.assertIsNone(self._fila()[1]["saiu_em"])
        self.assertEqual(self._fila()[1]["carga_inicial"], 1)

    def test_erro_no_meio_nao_grava_nada(self):
        self._ler([(1, "NONNA", "J3 FLEX"), (2, "NONNA", "J3 FLEX")])
        antes = self._fila()
        self.assertEqual(self._ler([(1, "NONNA", "J3 FLEX"), RuntimeError("401")], hora=9), "erro")
        self.assertEqual(self._fila(), antes)
        self.assertEqual(self.soltou, 2)
        self.assertEqual(ler_stokki.ultima_leitura(self.conn), "2026-11-12 08:00:00")

    def test_primeira_leitura_com_erro_nao_gasta_a_carga_inicial(self):
        self._ler([RuntimeError("fora do ar")])
        self._ler([(1, "NONNA", "J3 FLEX")], hora=9)
        self.assertEqual(self._fila()[1]["carga_inicial"], 1)

    def test_trava_ocupada_devolve_ocupada(self):
        self.assertEqual(self._ler([(1, "NONNA", "J3 FLEX")], conta=False), "ocupada")
        self.assertEqual(self._fila(), {})
        self.assertIsNone(ler_stokki.ultima_leitura(self.conn))

    def test_eh_retirada(self):
        c = _Catalogo()
        self.assertTrue(ler_stokki.eh_retirada("CLIENTE RETIRA 15.060.670/0001-70", c))
        self.assertTrue(ler_stokki.eh_retirada("Cliente Retira", None))
        self.assertTrue(ler_stokki.eh_retirada("J3 FLEX", c))
        # a listagem da Stokki cola o CNPJ no nome
        self.assertTrue(ler_stokki.eh_retirada("J3 FLEX 15.119.003/0001-16", c))
        self.assertEqual(c.cnpj, "15119003000116")
        self.assertFalse(ler_stokki.eh_retirada("FRESHLOG 15.060.670/0001-70", c))
        for nome in ("FRESHLOG", "", None, "QUALY-TRANSPORTE"):
            self.assertFalse(ler_stokki.eh_retirada(nome, c), nome)


if __name__ == "__main__":
    unittest.main()
