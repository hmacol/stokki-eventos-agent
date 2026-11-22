# -*- coding: utf-8 -*-
"""
test_carregamento.py

Checklist de carregamento (nucleo/carregamento.py): config, tabelas e
quando a rota exige o checklist.
    py -3.11 -m unittest nucleo.test_carregamento -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, carregamento


def _cfg(**kw) -> dict:
    return {"api_motorista": {"checklist_carregamento": kw}}


class TestConfig(unittest.TestCase):
    def test_config_desligada_por_padrao(self):
        c = carregamento.config_carregamento({})
        self.assertFalse(c["ativo"])
        self.assertEqual(c["pilotos"], set())

    def test_config_pilotos(self):
        c = carregamento.config_carregamento(_cfg(ativo=True, pilotos=["10", 20]))
        self.assertTrue(c["ativo"])
        self.assertEqual(c["pilotos"], {10, 20})


class TestBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _rota(self, agent_id=10, status="ACEITA", provedor=None):
        cur = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, agent_id, status, provedor) VALUES ('2026-11-23', ?, ?, ?)",
            (agent_id, status, provedor or banco.PROVEDOR_APP))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM nucleo_rotas WHERE id=?", (cur.lastrowid,)).fetchone()

    def test_tabelas_existem(self):
        nomes = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("nucleo_carregamentos", nomes)
        self.assertIn("nucleo_carregamento_itens", nomes)

    def test_exigido_so_com_chave_ligada_e_rota_aceita(self):
        ligada = carregamento.config_carregamento(_cfg(ativo=True, pilotos=[10]))
        desligada = carregamento.config_carregamento(_cfg(ativo=False))
        self.assertTrue(carregamento.exigido(self._rota(), ligada))
        self.assertTrue(carregamento.exigido(self._rota(status="PLANEJADA"), ligada))
        self.assertFalse(carregamento.exigido(self._rota(), desligada))
        self.assertFalse(carregamento.exigido(self._rota(status="EM_ROTA"), ligada))
        self.assertFalse(carregamento.exigido(self._rota(agent_id=99), ligada))
        sem_pilotos = carregamento.config_carregamento(_cfg(ativo=True))
        self.assertTrue(carregamento.exigido(self._rota(agent_id=99), sem_pilotos))

    def test_carregamento_da_rota(self):
        rota = self._rota()
        self.assertIsNone(carregamento.carregamento_da_rota(self.conn, rota["id"]))
        self.conn.execute("INSERT INTO nucleo_carregamentos (rota_id, agent_id, uuid) VALUES (?, 10, 'u1')", (rota["id"],))
        self.conn.commit()
        self.assertEqual(carregamento.carregamento_da_rota(self.conn, rota["id"])["uuid"], "u1")


if __name__ == "__main__":
    unittest.main()
