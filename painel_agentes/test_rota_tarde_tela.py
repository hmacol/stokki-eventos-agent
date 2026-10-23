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

import planejamento_rotas
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

    def test_roteirizar_continua_numeracao_com_tarde(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 10, 23), [
            _rascunho("Planejamento - 23/10/2026 - #18", turno="TARDE")])
        conn = rascunhos_rota._conectar()
        try:
            self.assertEqual(rascunhos_rota._proximo_numero_ordem(conn, date(2026, 10, 23)), 19)
        finally:
            conn.close()


def _rascunho_tela(turno="TARDE", **extra):
    paradas = [{"codigo": "#PS-1", "nivel_dificuldade": 1, "volume_caixas": 5,
                "latitude": -23.55, "longitude": -46.63,
                "janela_inicio": "14:00", "janela_fim": "17:00"}]
    return {"nome": "Planejamento - 23/10/2026 - #18", "turno": turno,
            "start_at": "2026-10-23T16:15:00Z", "paradas": paradas, **extra}


class TelaTardeTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(planejamento_rotas, "_garantir_coords_base", return_value=(-23.5, -46.6))
        p.start()
        self.addCleanup(p.stop)

    def test_badge_tarde(self):
        badges = planejamento_rotas._badges_trava(_rascunho_tela())
        self.assertIn("Tarde · sai 13:15", badges)

    def test_sem_badge_tarde_na_rota_normal(self):
        badges = planejamento_rotas._badges_trava(_rascunho_tela(turno=None))
        self.assertFalse([b for b in badges if b.startswith("Tarde")])

    def test_tarde_sem_badge_de_rota_fraca(self):
        r = _rascunho_tela(rota_fraca_motivo="nao coube")
        self.assertFalse([b for b in planejamento_rotas._badges_trava(r) if "rota fraca" in b])

    def test_simulacao_usa_saida_da_rota(self):
        paradas = _rascunho_tela()["paradas"]
        sim = planejamento_rotas._simular_rascunho(paradas, hora_saida=13.25)
        self.assertGreater(sim["chegadas"][0], 13.25)


if __name__ == "__main__":
    unittest.main()
