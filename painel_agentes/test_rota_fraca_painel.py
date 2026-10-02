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


import planejamento_rotas  # noqa: E402


def _parada(i, caixas=1):
    return {"service_id": i, "codigo": f"PS-{1000 + i}", "endereco": f"Rua {i}", "sender_id": 1,
            "latitude": -23.50, "longitude": -46.60 + i * 0.001, "nivel_dificuldade": 1,
            "volume_caixas": caixas, "janela_inicio": None, "janela_fim": None}


class TestEtiqueta(unittest.TestCase):
    MOTIVO = "vizinha mais próxima a 27 km"

    def setUp(self):
        for patcher in (mock.patch.object(planejamento_rotas, "_simular_rascunho", lambda paradas: None),
                        mock.patch.object(planejamento_rotas, "_garantir_coords_base", lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _badges(self, paradas, motivo):
        return planejamento_rotas._badges_trava(
            {"paradas": paradas, "tipo_veiculo": None, "tipo_rota": "GRANDE_SP", "rota_fraca_motivo": motivo})

    def test_rota_fraca_com_motivo_ganha_etiqueta(self):
        badges = self._badges([_parada(1, 9), _parada(2, 4), _parada(3, 4)], self.MOTIVO)
        self.assertIn("rota fraca: 3 pedido(s), 17 caixa(s). vizinha mais próxima a 27 km", badges)

    def test_sem_motivo_nao_ganha(self):
        self.assertEqual(self._badges([_parada(1)], None), [])

    def test_passou_de_sete_pedidos_a_etiqueta_some(self):
        self.assertEqual(self._badges([_parada(i) for i in range(8)], self.MOTIVO), [])

    def test_passou_de_quarenta_caixas_a_etiqueta_some(self):
        self.assertEqual(self._badges([_parada(1, 41)], self.MOTIVO), [])


if __name__ == "__main__":
    unittest.main()
