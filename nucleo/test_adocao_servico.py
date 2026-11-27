# -*- coding: utf-8 -*-
"""
test_adocao_servico.py

adotar_servico: id sintetico negativo vira o id real da Vuupt nos rascunhos.

    py -3.11 -m unittest nucleo.test_adocao_servico -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, pedidos, pool, sincronizar_servicos_vuupt as sync
from nucleo.adocao_servico import adotar_servico

# DDL minimo copiado de painel_agentes/rascunhos_rota.py (nucleo nao importa o painel)
_DDL = """
CREATE TABLE rascunhos_parada (
    id INTEGER PRIMARY KEY AUTOINCREMENT, rascunho_id INTEGER NOT NULL, ordem INTEGER NOT NULL,
    service_id INTEGER NOT NULL, codigo TEXT);
CREATE UNIQUE INDEX idx_rascunhos_parada_unico ON rascunhos_parada(rascunho_id, service_id);
CREATE TABLE rascunhos_plano_original (
    lote_id TEXT NOT NULL, rascunho_id INTEGER NOT NULL, service_id INTEGER NOT NULL);
"""

_INS = "INSERT INTO rascunhos_parada (rascunho_id, ordem, service_id, codigo) VALUES (?, ?, ?, 'PS-100')"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()
        self.conn.executescript(_DDL)
        pedidos.upsert_pedido(self.conn, "PS-100", {"titulo": "x"}, banco.ORIGEM_PIPELINE)
        self.conn.commit()
        self.neg = -self.conn.execute("SELECT id_pool FROM nucleo_pedidos WHERE codigo='PS-100'").fetchone()[0]

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _ids(self, tabela):
        return [r[0] for r in self.conn.execute(f"SELECT service_id FROM {tabela} ORDER BY rascunho_id, service_id")]

    def _sid(self):
        return self.conn.execute("SELECT vuupt_service_id FROM nucleo_pedidos WHERE codigo='PS-100'").fetchone()[0]


class TestAdotarServico(_Base):
    def test_reescreve_rascunho_e_plano_original(self):
        self.conn.execute(_INS, (1, 1, self.neg))
        self.conn.execute("INSERT INTO rascunhos_plano_original VALUES ('L', 1, ?)", (self.neg,))
        self.assertEqual(adotar_servico(self.conn, "#ps-100", 777), 2)
        self.assertEqual(self._ids("rascunhos_parada"), [777])
        self.assertEqual(self._ids("rascunhos_plano_original"), [777])
        self.assertEqual(self._sid(), 777)

    def test_unique_nao_estoura_se_id_real_ja_no_rascunho(self):
        self.conn.execute(_INS, (1, 1, self.neg))
        self.conn.execute(_INS, (1, 2, 777))
        self.conn.execute(_INS, (2, 1, self.neg))
        adotar_servico(self.conn, "PS-100", 777)
        self.assertEqual(self._ids("rascunhos_parada"), [777, 777])  # um por rascunho
        self.assertEqual(self.conn.execute("SELECT COUNT(DISTINCT rascunho_id) FROM rascunhos_parada").fetchone()[0], 2)

    def test_idempotente(self):
        self.conn.execute(_INS, (1, 1, self.neg))
        self.assertEqual(adotar_servico(self.conn, "PS-100", 777), 1)
        self.assertEqual(adotar_servico(self.conn, "PS-100", 777), 0)
        # outro id real ja gravado: nao troca
        self.conn.execute(_INS, (3, 1, self.neg))
        self.assertEqual(adotar_servico(self.conn, "PS-100", 888), 0)
        self.assertEqual(self._sid(), 777)
        self.assertIn(self.neg, self._ids("rascunhos_parada"))

    def test_sync_que_traz_servico_nao_duplica_no_pool(self):
        self.conn.execute(_INS, (1, 1, self.neg))
        self.conn.commit()
        sync.sincronizar_servicos([{"id": 555, "code": "PS-100", "status": "pending"}], self.conn)
        self.assertEqual(self._ids("rascunhos_parada"), [555])
        self.assertEqual(self._sid(), 555)

    def test_pedido_sem_rascunho_so_grava_id(self):
        self.conn.execute("DROP TABLE rascunhos_parada")
        self.conn.execute("DROP TABLE rascunhos_plano_original")  # tabelas ausentes
        self.assertEqual(adotar_servico(self.conn, "PS-100", 777), 0)
        self.assertEqual(self._sid(), 777)
        self.assertEqual(adotar_servico(self.conn, "PS-404", 1), 0)  # codigo desconhecido


if __name__ == "__main__":
    unittest.main()
