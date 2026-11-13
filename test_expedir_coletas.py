# -*- coding: utf-8 -*-
"""
test_expedir_coletas.py

Retirada/coleta no galpao sem entrega na VUUPT (Hugo, 12/11): depois que a aba
Coletas da Expedicao existe, so expede com coleta confirmada com foto
(--coleta N); o acumulado da 1a leitura ainda sai pelo botao da /pedidos.
    py -3.11 -m unittest test_expedir_coletas -v
"""
import sqlite3
import unittest
from types import SimpleNamespace

import expedir_pedidos as ep
from coletas import banco, regras


class _Catalogo:
    def resolver(self, nome, cnpj=""):
        return SimpleNamespace(tipo={"J3 FLEX": "RETIRADA", "FRESHLOG": "ENTREGA"}.get(nome.upper()))


class TestDecidirRetirada(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        banco.garantir_esquema(self.conn)
        self.addCleanup(self.conn.close)

    def _leu(self):
        self.conn.execute("INSERT INTO expedicao_coletas_leituras VALUES ('2026-11-12 08:00:00', 'ok', '')")

    def _fila(self, id_, inicial):
        self.conn.execute("INSERT INTO expedicao_coletas_fila (id_stokki, codigo, embarcador, transportadora, retirada,"
                          " carga_inicial) VALUES (?, ?, 'NONNA', 'J3 FLEX', 1, ?)", (id_, f"PS-{id_}", inicial))

    def _decidir(self, codigo, nome="J3 FLEX", coleta=None):
        return ep._decidir_retirada(self.conn, codigo, nome, _Catalogo(), coleta)

    def test_transportadora_que_nao_e_retirada_recusa(self):
        self._leu()
        for nome in ("FRESHLOG", "", "QUALY"):
            pode, motivo = self._decidir("PS-1", nome)
            self.assertFalse(pode)
            self.assertIn("não é de retirada/coleta no galpão", motivo)

    def test_antes_da_aba_ler_a_stokki_vale_a_regra_antiga(self):
        self.assertEqual(self._decidir("PS-1"), (True, ""))
        self.assertEqual(self._decidir("#PS-1", "CLIENTE RETIRA 15.060.670/0001-70"), (True, ""))

    def test_acumulado_sai_pelo_botao_e_retirada_nova_nao(self):
        self._leu()
        self._fila(1, inicial=1)
        self._fila(2, inicial=0)
        self.assertEqual(self._decidir("PS-1"), (True, ""))
        pode, motivo = self._decidir("PS-2")
        self.assertFalse(pode)
        self.assertIn("aba Coletas", motivo)
        pode, motivo = self._decidir("PS-3")   # nem esta na fila ainda
        self.assertFalse(pode)
        self.assertIn("ainda não lida", motivo)

    def test_com_coleta_so_expede_se_tem_foto(self):
        self._leu()
        self._fila(2, inicial=0)
        self._fila(3, inicial=0)
        cid = regras.gerar_folha(self.conn, [2, 3], "expedicao")
        pode, motivo = self._decidir("PS-2", coleta=cid)   # folha ainda aberta
        self.assertFalse(pode)
        self.assertIn("sem foto", motivo)
        regras.confirmar(self.conn, cid, [2], "JOSE", "ABC1D23", "COL-1.jpg", "expedicao")
        self.assertEqual(self._decidir("PS-2", coleta=cid), (True, ""))
        self.assertEqual(self._decidir("#PS-2", coleta=cid), (True, ""))
        self.assertFalse(self._decidir("PS-3", coleta=cid)[0])    # nao saiu nesta coleta
        self.assertFalse(self._decidir("PS-2", coleta=999)[0])
        self.assertFalse(self._decidir("PS-2", "FRESHLOG", coleta=cid)[0])


if __name__ == "__main__":
    unittest.main()
