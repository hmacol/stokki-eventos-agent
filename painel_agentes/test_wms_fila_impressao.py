# -*- coding: utf-8 -*-
"""
test_wms_fila_impressao.py

Testes da fila de impressao de etiquetas. Banco temporario.
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_wms_fila_impressao -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_fila_impressao as fila  # noqa: E402


class TestFila(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(Path(self._tmp.name) / "dados.db")
        self.conn.row_factory = sqlite3.Row
        fila.garantir_tabela(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_garantir_tabela_idempotente(self):
        fila.garantir_tabela(self.conn)
        fila.garantir_tabela(self.conn)

    def test_enfileirar_e_listar_pendente(self):
        i = fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x5", itens=["C0000001", "C0000002"],
                            titulo="COXINHA · 2 caixas", pedido_por="Kleber"); self.conn.commit()
        [t] = fila.listar(self.conn)
        self.assertEqual((t["id"], t["itens"], t["situacao"]), (i, ["C0000001", "C0000002"], "PENDENTE"))
        self.assertEqual(fila.obter(self.conn, i)["pedido_por"], "Kleber")
        self.assertIsNone(fila.obter(self.conn, 999))

    def test_enfileirar_nao_faz_commit(self):
        fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x5", itens=["C1"], titulo="t")
        self.conn.rollback()
        self.assertEqual(fila.listar(self.conn), [])

    def test_tipo_ou_tamanho_invalido_recusa(self):
        with self.assertRaises(wms.ErroWMS): fila.enfileirar(self.conn, tipo="X", tamanho="10x5", itens=["C1"], titulo="t")
        with self.assertRaises(wms.ErroWMS): fila.enfileirar(self.conn, tipo="CAIXA", tamanho="9x9", itens=["C1"], titulo="t")

    def test_sem_itens_recusa(self):
        with self.assertRaises(wms.ErroWMS): fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x5", itens=[], titulo="t")

    def test_listar_ordem_antiga_primeiro(self):
        a = fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x5", itens=["C1"], titulo="a")
        b = fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x15", itens=["C2"], titulo="b")
        self.conn.commit()
        self.assertEqual([t["id"] for t in fila.listar(self.conn)], [a, b])

    def test_marcar_impresso_tira_da_fila_e_e_idempotente(self):
        i = fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x5", itens=["C0000001"], titulo="t"); self.conn.commit()
        a = fila.marcar_impresso(self.conn, i, "Ana"); b = fila.marcar_impresso(self.conn, i, "Bia")
        self.assertEqual(fila.listar(self.conn), [])
        self.assertEqual((b["impresso_por"], b["impresso_em"]), ("Ana", a["impresso_em"]))
        self.assertEqual(len(fila.listar(self.conn, "IMPRESSO")), 1)

    def test_marcar_inexistente_recusa(self):
        with self.assertRaises(wms.ErroWMS): fila.marcar_impresso(self.conn, 999, "Ana")


if __name__ == "__main__":
    unittest.main()
