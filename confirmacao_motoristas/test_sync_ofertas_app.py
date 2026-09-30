# -*- coding: utf-8 -*-
"""
Escolha/cancelamento de oferta vindos do APP do motorista (Hugo, 14/09):
POST /api/sync/ofertas/escolher e /cancelar -- mesmo claim atômico da
página, protegido pelo segredo de sync.
    python -m unittest confirmacao_motoristas.test_sync_ofertas_app -v
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


class TestSyncOfertasApp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ.update({"DB_PATH": str(Path(self._tmp.name) / "c.db"), "TOKEN_SECRET": "t", "SYNC_SECRET": "segredo"})
        sys.path.insert(0, str(Path(__file__).parent))
        import app as modulo
        self.m = importlib.reload(modulo)
        self.cli = self.m.app.test_client()
        self.h = {"X-Sync-Secret": "segredo"}
        r = self.cli.post("/api/sync/ofertas/upsert", headers=self.h, json={"ofertas": [
            {"rascunho_id": 810, "data_alvo": "2026-09-20", "resumo_json": "{}", "status": "ABERTA",
             "agent_ids_elegiveis": json.dumps([{"agent_id": 1}, {"agent_id": 2}])},
        ]})
        self.assertEqual(r.status_code, 200)

    def tearDown(self):
        self._tmp.cleanup()

    def _post(self, acao, agent_id, rascunho_id=810, headers=None):
        return self.cli.post(f"/api/sync/ofertas/{acao}", headers=headers or self.h,
                             json={"agent_id": agent_id, "rascunho_id": rascunho_id})

    def test_escolher_cancelar_e_disputa(self):
        self.assertEqual(self._post("escolher", 1, headers={"X-Sync-Secret": "errado"}).status_code, 401)
        self.assertEqual(self._post("escolher", 1, rascunho_id=999).status_code, 404)
        self.assertEqual(self._post("escolher", 3).status_code, 409)            # não elegível

        r = self._post("escolher", 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["escolhido_em"])
        r = self._post("escolher", 2)                                           # perdeu a corrida
        self.assertEqual(r.status_code, 409)
        self.assertIn("outro motorista", r.get_json()["erro"])

        # o que a sincronização local vê: ESCOLHIDA pelo 1 -> não desfaz
        st = self.cli.get("/api/sync/ofertas/status?ids=810", headers=self.h).get_json()["ofertas"][0]
        self.assertEqual((st["status"], st["escolhido_por"]), ("ESCOLHIDA", 1))

        r = self._post("cancelar", 2)                                           # não é dele
        self.assertEqual(r.status_code, 409)
        self.assertEqual((r.get_json()["status"], r.get_json()["escolhido_por"]), ("ESCOLHIDA", 1))
        self.assertEqual(self._post("cancelar", 1).status_code, 200)
        st = self.cli.get("/api/sync/ofertas/status?ids=810", headers=self.h).get_json()["ofertas"][0]
        self.assertEqual(st["status"], "ABERTA")


if __name__ == "__main__":
    unittest.main()
