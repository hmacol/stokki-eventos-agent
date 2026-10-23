# -*- coding: utf-8 -*-
"""
Aderencia do plano (Hugo, 17/10): quanto do plano do roteirizador chegou
ao envio sem a equipe mexer.
Rodar (de roteirizacao/): py -3.11 -m unittest test_aderencia_plano -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aderencia_plano as ap


def _banco():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, data_alvo TEXT, lote_id TEXT, status TEXT);
        CREATE TABLE rascunhos_parada (rascunho_id INTEGER, service_id INTEGER);
        CREATE TABLE rascunhos_plano_original (lote_id TEXT, rascunho_id INTEGER, service_id INTEGER,
                                               gravado_em TEXT DEFAULT (datetime('now')));
    """)
    return conn


class AderenciaPlanoTest(unittest.TestCase):
    def test_conta_mesma_rota_movidas_fora_e_extras(self):
        conn = _banco()
        conn.executemany("INSERT INTO rascunhos_rota VALUES (?, '2026-10-17', 'L1', ?)",
                         [(1, "ENVIADO"), (2, "ENVIADO"), (3, "DESCARTADO")])
        # plano: rota 1 = {10, 11}, rota 2 = {20}, rota 3 = {30, 31}
        conn.executemany("INSERT INTO rascunhos_plano_original (lote_id, rascunho_id, service_id) "
                         "VALUES ('L1', ?, ?)", [(1, 10), (1, 11), (2, 20), (3, 30), (3, 31)])
        # envio: 10 ficou, 11 foi pra rota 2, 30 (da rota fundida) foi pra rota 2,
        # 31 ficou de fora, 99 entrou depois (incremento)
        conn.executemany("INSERT INTO rascunhos_parada VALUES (?, ?)",
                         [(1, 10), (2, 11), (2, 20), (2, 30), (3, 30), (3, 31), (1, 99)])
        r = ap.aderencia_do_dia(conn, "2026-10-17")
        self.assertEqual(r["paradas_plano"], 5)
        self.assertEqual(r["mesma_rota"], 2)      # 10 e 20
        self.assertEqual(r["movidas"], 2)         # 11 e 30
        self.assertEqual(r["fora"], 1)            # 31
        self.assertEqual(r["extras"], 1)          # 99
        self.assertEqual(r["rotas_plano"], 3)
        self.assertEqual(r["rotas_plano_enviadas"], 2)
        self.assertAlmostEqual(r["aderencia"], 0.4)

    def test_dia_sem_foto_devolve_none(self):
        conn = _banco()
        conn.execute("INSERT INTO rascunhos_rota VALUES (1, '2026-10-17', 'L1', 'ENVIADO')")
        self.assertIsNone(ap.aderencia_do_dia(conn, "2026-10-17"))

    def test_usa_o_lote_mais_recente_com_envio(self):
        conn = _banco()
        conn.executemany("INSERT INTO rascunhos_rota VALUES (?, '2026-10-17', ?, ?)",
                         [(1, "L1", "DESCARTADO"), (2, "L2", "ENVIADO")])
        conn.executemany("INSERT INTO rascunhos_plano_original (lote_id, rascunho_id, service_id) VALUES (?, ?, ?)",
                         [("L1", 1, 10), ("L1", 1, 11), ("L2", 2, 10)])
        conn.execute("INSERT INTO rascunhos_parada VALUES (2, 10)")
        r = ap.aderencia_do_dia(conn, "2026-10-17")
        self.assertEqual(r["paradas_plano"], 1)
        self.assertEqual(r["mesma_rota"], 1)


if __name__ == "__main__":
    unittest.main()
