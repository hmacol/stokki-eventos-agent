# -*- coding: utf-8 -*-
"""
Testes das caixas com serial e do codigo interno (dados).
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_wms_caixas -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_caixas as cx  # noqa: E402
import wms_fila_impressao as fila  # noqa: E402
import wms_pedidos  # noqa: E402


class BaseCaixas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = cx.conectar(self.db)
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, dun, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA', 'MARIA DOLORES', 48, '724000170000', NULL, 6, 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (2, 901, NULL, 'SEM CODIGO', "
            "'MARIA DOLORES', 48, NULL, 1, 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def n(self, tabela):
        return self.conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]


class TestCaixas(BaseCaixas):
    def test_criar_tres_caixas_gera_seriais_em_sequencia_e_um_trabalho_na_fila(self):
        r = cx.criar_caixas(self.conn, produto_id=1, lote="l1", validade="15/12/2026", qtd_un=None, quantidade=3)
        self.assertEqual(r["seriais"], ["C0000001", "C0000002", "C0000003"])
        c = cx.obter_caixa(self.conn, "C0000002")
        self.assertEqual((c["situacao"], c["lote"], c["validade"], c["qtd_un"]), ("IMPRESSA", "L1", "2026-12-15", 6))
        self.assertEqual(c["produto"]["id"], 1)
        job = fila.obter(self.conn, r["impressao_id"])
        self.assertEqual(job["itens"], r["seriais"])
        self.assertEqual((job["tipo"], job["tamanho"], job["titulo"]), ("CAIXA", "10x5", "COXINHA FESTA · 3 caixas"))
        self.assertEqual(self.n("wms_fila_impressao"), 1)
        ev = self.conn.execute("SELECT de, para FROM wms_caixa_eventos").fetchall()
        self.assertEqual(len(ev), 3)
        self.assertEqual((ev[0][0], ev[0][1]), (None, "IMPRESSA"))

    def test_serial_continua_de_onde_parou(self):
        cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=None, quantidade=2)
        r = cx.criar_caixas(self.conn, produto_id=1, lote="L2", validade="2026-12-20", qtd_un=None, quantidade=1)
        self.assertEqual(r["seriais"], ["C0000003"])

    def test_qtd_un_informada_prevalece(self):
        r = cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=4, quantidade=1)
        self.assertEqual(cx.obter_caixa(self.conn, r["seriais"][0])["qtd_un"], 4)

    def test_quantidade_fora_de_1_a_200_recusa(self):
        for q in (0, 201):
            with self.assertRaises(wms.ErroWMS):
                cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=None, quantidade=q)
        self.assertEqual(self.n("wms_caixas"), 0)
        r = cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade="2026-12-15", qtd_un=None, quantidade=200)
        self.assertEqual(len(r["seriais"]), 200)

    def test_validade_obrigatoria_quando_produto_controla(self):
        with self.assertRaises(wms.ErroWMS):
            cx.criar_caixas(self.conn, produto_id=1, lote="L1", validade=None, qtd_un=None, quantidade=1)
        self.assertEqual(self.n("wms_caixas"), 0)

    def test_criar_caixas_nao_cria_nada_se_falhar(self):
        with self.assertRaises(wms.ErroWMS):
            cx.criar_caixas(self.conn, produto_id=999, lote="L1", validade="2026-12-15", qtd_un=None, quantidade=2)
        self.assertEqual(self.n("wms_caixas"), 0)
        self.assertEqual(self.n("wms_fila_impressao"), 0)
        self.assertEqual(self.n("wms_caixa_eventos"), 0)
        self.assertEqual(self.n("wms_contadores"), 0)
        self.assertFalse(self.conn.in_transaction)

    def test_lote_vazio_e_aceito(self):
        r = cx.criar_caixas(self.conn, produto_id=1, lote="", validade="2026-12-15", qtd_un=None, quantidade=1)
        self.assertEqual(cx.obter_caixa(self.conn, r["seriais"][0])["lote"], "")

    def test_obter_caixa_inexistente(self):
        self.assertIsNone(cx.obter_caixa(self.conn, "C9999999"))

    def test_produto_usa_caixas(self):
        self.assertTrue(cx.produto_usa_caixas({"embarcador_id": 48}, 48))
        self.assertFalse(cx.produto_usa_caixas({"embarcador_id": 7}, 48))


class TestCodigoInterno(BaseCaixas):
    def test_codigo_interno_e_unico_e_estavel(self):
        a = cx.garantir_codigo_interno(self.conn, 2)
        b = cx.garantir_codigo_interno(self.conn, 2)
        self.assertEqual((a, b), ("FLP000001", "FLP000001"))
        self.assertEqual(cx.garantir_codigo_interno(self.conn, 1), "FLP000002")

    def test_produto_inexistente(self):
        with self.assertRaises(wms.ErroWMS):
            cx.garantir_codigo_interno(self.conn, 999)


class TestGarantirTabelas(unittest.TestCase):
    def test_garantir_tabelas_em_conexao_do_wms_pedidos_e_idempotente(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = wms_pedidos.conectar(Path(tmp) / "dados.db")
            try:
                for _ in range(2):
                    cx.garantir_tabelas(conn)
                conn.commit()
                nomes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for t in ("wms_caixas", "wms_caixa_eventos", "wms_contadores", "wms_fila_impressao"):
                    self.assertIn(t, nomes)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
