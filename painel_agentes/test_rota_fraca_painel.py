# -*- coding: utf-8 -*-
"""
Rota fraca no painel (Hugo, 29/09): coluna rota_fraca_motivo do rascunho
e etiqueta no card da rota.
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_rota_fraca_painel -v
"""
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import rascunhos_rota  # noqa: E402


class TestColunaMotivo(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.caminho = Path(self.tmp.name) / "dados.db"
        for patcher in (mock.patch.object(rascunhos_rota, "DB_PATH", self.caminho),
                        mock.patch("mapa_util.carregar_remetentes_por_sender_id", lambda: {})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rascunho(self, **extra):
        return {"nome": "Rota 1", "start_location_base_id": 1, "start_at": "2026-09-30T09:00:00Z",
                "sublote": [], **extra}

    def test_migracao_em_banco_antigo(self):
        conn = sqlite3.connect(self.caminho)
        conn.execute("""
            CREATE TABLE rascunhos_rota (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_alvo TEXT NOT NULL, lote_id TEXT NOT NULL, nome TEXT NOT NULL,
                start_location_base_id INTEGER NOT NULL, start_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'RASCUNHO',
                criado_em TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                atualizado_em TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.commit()
        conn.close()
        conn = rascunhos_rota._conectar()
        try:
            self.assertIn("rota_fraca_motivo", {r["name"] for r in conn.execute("PRAGMA table_info(rascunhos_rota)")})
        finally:
            conn.close()

    def test_motivo_gravado_volta_na_leitura(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 30), [
            self._rascunho(rota_fraca_motivo="vizinha mais próxima a 27 km")])
        rotas = rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 30))
        self.assertEqual(rotas[0]["rota_fraca_motivo"], "vizinha mais próxima a 27 km")

    def test_sem_motivo_fica_nulo(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 30), [self._rascunho()])
        self.assertIsNone(rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 30))[0]["rota_fraca_motivo"])


if __name__ == "__main__":
    unittest.main()
