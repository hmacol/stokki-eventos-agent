# -*- coding: utf-8 -*-
"""
test_rota_tarde_tela.py

rascunhos_rota: a coluna `turno` ('TARDE' ou NULL) marca a rota da tarde
(janela tardia que nao cabe na manha, Hugo 23/10). O nome da rota nao muda.

Usa um SQLite temporario (DB_PATH trocado).
Rodar (de dentro de painel_agentes/):
    py -3.11 -m unittest test_rota_tarde_tela -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import rascunhos_rota


def _rascunho(nome, turno=None):
    r = {"nome": nome, "start_location_base_id": 6950, "end_location_base_id": 6950,
         "start_at": "2026-10-23T16:15:00Z", "sublote": [{"id": 1, "code": "#PS-1"}]}
    if turno:
        r["turno"] = turno
    return r


class LoteGravaTurnoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(rascunhos_rota, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                  mock.patch("mapa_util.carregar_remetentes_por_sender_id", return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def test_lote_grava_turno(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 10, 23), [
            _rascunho("Planejamento - 23/10/2026 - #1"),
            _rascunho("Planejamento - 23/10/2026 - #2", turno="TARDE"),
        ])
        conn = rascunhos_rota._conectar()
        try:
            turnos = [r["turno"] for r in conn.execute("SELECT turno FROM rascunhos_rota ORDER BY id")]
        finally:
            conn.close()
        self.assertEqual(turnos, [None, "TARDE"])


if __name__ == "__main__":
    unittest.main()
