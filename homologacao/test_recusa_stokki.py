"""StokkiSession na homologacao: recusa antes da fila e do login (nunca login na Stokki)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from homologacao import registro
from homologacao.guarda import EnvioBloqueadoHomolog
from stokki import auth


class TestRecusaStokki(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = mock.patch.object(registro, "CAMINHO_BANCO", Path(self.dir.name) / "dados.db")
        p.start()
        self.addCleanup(p.stop)

    def test_homolog_recusa_sem_entrar_na_fila(self):
        with mock.patch.dict(os.environ, {"FRESH_AMBIENTE": "homolog"}), \
                mock.patch.object(auth.StokkiSession, "_entrar_na_fila",
                                  side_effect=AssertionError("entrou na fila")):
            with self.assertRaises(EnvioBloqueadoHomolog) as cm:
                auth.StokkiSession({"stokki": {"usuario": "u", "senha": "s"}})
        self.assertTrue(str(cm.exception).startswith("indisponível na homologação"))
        self.assertEqual(registro.listar_envios()[0]["canal"], "stokki")

    def test_producao_sem_variavel_segue_igual(self):
        env = {k: v for k, v in os.environ.items() if k != "FRESH_AMBIENTE"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(auth.StokkiSession, "_entrar_na_fila", return_value="principal"):
            with self.assertRaises(ValueError):  # sem credenciais: o erro de sempre
                auth.StokkiSession({"stokki": {}})
        self.assertEqual(registro.listar_envios(), [])


if __name__ == "__main__":
    unittest.main()
