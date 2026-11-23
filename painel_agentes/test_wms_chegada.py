# -*- coding: utf-8 -*-
"""
Testes da etapa Receber (chegada do caminhao): faixas de temperatura.
Rodar (de dentro de painel_agentes/): py -3.11 -m unittest test_wms_chegada -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_caixas  # noqa: E402
import wms_chegada  # noqa: E402


class TestFaixas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        wms_caixas.conectar(self.db).close()  # cria wms_produtos e recebimentos
        self.conn = wms_chegada.conectar(self.db)
        for pid, sku, temp in ((1, "R1", "REFRIGERADO"), (2, "C1", "CONGELADO"), (3, "S1", ""), (4, "A1", "AMBIENTE")):
            self.conn.execute(
                "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, unidade, "
                "temperatura, atualizado_em) VALUES (?, ?, ?, ?, 'MARIA', 48, 'UN', ?, '2026-09-21 10:00:00')",
                (pid, 900 + pid, sku, "PROD " + sku, temp))
        self.p_refri = 1
        self.rec = self._receb(1, [1, 2, 3])
        self.rec_seco = self._receb(2, [3, 4])
        self.conn.commit()

    def _receb(self, id_stokki, produtos):
        cur = self.conn.execute(
            "INSERT INTO wms_recebimentos (id_stokki, lido_em, atualizado_em) VALUES (?, '2026-11-22', '2026-11-22')",
            (id_stokki,))
        rid = cur.lastrowid
        for i, pid in enumerate(produtos, 1):
            self.conn.execute(
                "INSERT INTO wms_recebimento_itens (recebimento_id, linha, qtd_embalagem, produto_id) "
                "VALUES (?, ?, 1, ?)", (rid, i, pid))
        return rid

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_faixas_padrao_e_config(self):
        self.assertEqual(wms_chegada.faixas({}), {"REFRIGERADO": (0.0, 7.0), "CONGELADO": (-18.0, -12.0)})
        f = wms_chegada.faixas({"wms": {"temperatura_faixas": {"REFRIGERADO": [2, 8], "CONGELADO": "x"}}})
        self.assertEqual(f, {"REFRIGERADO": (2.0, 8.0), "CONGELADO": (-18.0, -12.0)})

    def test_classes_da_carga(self):
        self.assertEqual(wms_chegada.classes_da_carga(self.conn, [self.rec]), ["REFRIGERADO", "CONGELADO"])

    def test_carga_sem_temperatura_nao_tem_classe(self):
        self.assertEqual(wms_chegada.classes_da_carga(self.conn, [self.rec_seco]), [])
        self.assertEqual(wms_chegada.classes_da_carga(self.conn, []), [])

    def test_faixa_propria_do_produto_estreita(self):
        wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=2, temp_max=5)
        self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [self.rec], "REFRIGERADO", {}), (2.0, 5.0))
        # congelado sem faixa propria continua no padrao
        self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [self.rec], "CONGELADO", {}), (-18.0, -12.0))

    def test_faixa_do_produto_nao_alarga_a_da_classe(self):
        wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=-5, temp_max=20)
        self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [self.rec], "REFRIGERADO", {}), (0.0, 7.0))

    def test_limpar_faixa(self):
        wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=2, temp_max=5)
        p = wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=None, temp_max=None)
        self.assertIsNone(p["temp_min"])
        self.assertIsNone(p["temp_max"])

    def test_faixa_invertida_ou_pela_metade_recusa(self):
        with self.assertRaises(wms.ErroWMS):
            wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=6, temp_max=2)
        with self.assertRaises(wms.ErroWMS):
            wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=2, temp_max=None)


if __name__ == "__main__":
    unittest.main()
