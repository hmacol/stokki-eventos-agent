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
            colunas = {r["name"] for r in conn.execute("PRAGMA table_info(rascunhos_rota)")}
            self.assertIn("rota_fraca_motivo", colunas)
            self.assertIn("recebeu_rota_fraca", colunas)
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

    def test_marca_de_receptora_gravada_volta_na_leitura(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 30), [
            self._rascunho(nome="Rota 1", recebeu_rota_fraca=True), self._rascunho(nome="Rota 2")])
        rotas = sorted(rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 30)), key=lambda r: r["nome"])
        self.assertEqual([r["recebeu_rota_fraca"] for r in rotas], [1, 0])


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

    def test_com_5h_ou_mais_a_etiqueta_some(self):
        # Hugo, 03/10: rota fraca tambem exige menos de 5h estimadas
        with mock.patch.object(planejamento_rotas, "estimar_tempo_rota", lambda *a, **k: 5.0):
            self.assertEqual(self._badges([_parada(1, 9), _parada(2, 4)], self.MOTIVO), [])

    def test_com_4h54_a_etiqueta_fica(self):
        with mock.patch.object(planejamento_rotas, "estimar_tempo_rota", lambda *a, **k: 4.9):
            self.assertIn("rota fraca: 2 pedido(s), 13 caixa(s). vizinha mais próxima a 27 km",
                          self._badges([_parada(1, 9), _parada(2, 4)], self.MOTIVO))


class TestTetoDaReceptora(unittest.TestCase):
    """Rota que recebeu pedido de rota fraca foi formada com teto + 2
    paradas e 20 km entre paradas (roteirizacao/rotas_fracas.py): o aviso
    de limite usa o mesmo teto nela, e só nela."""

    def setUp(self):
        for patcher in (mock.patch.object(planejamento_rotas, "_simular_rascunho", lambda paradas: None),
                        mock.patch.object(planejamento_rotas, "_garantir_coords_base", lambda: None),
                        mock.patch.object(planejamento_rotas, "estimar_tempo_rota", lambda *a, **k: 1.0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _badges(self, paradas, recebeu):
        return planejamento_rotas._badges_trava(
            {"paradas": paradas, "tipo_veiculo": None, "tipo_rota": "GRANDE_SP", "recebeu_rota_fraca": recebeu})

    def _distantes(self):
        # ~18 km entre as duas: passa dos 15, cabe nos 20
        a, b = _parada(1, 30), _parada(2, 30)
        b["longitude"] = a["longitude"] + 0.176
        return [a, b]

    def test_receptora_com_17_paradas_nao_avisa(self):
        self.assertEqual(self._badges([_parada(i, 5) for i in range(17)], 1), [])

    def test_receptora_com_19_paradas_avisa_com_a_folga(self):
        self.assertIn("19 paradas (máx 18, com folga de rota fraca)",
                      self._badges([_parada(i, 5) for i in range(19)], 1))

    def test_rota_comum_com_17_paradas_continua_avisando(self):
        self.assertIn("17 paradas (máx 16)", self._badges([_parada(i, 5) for i in range(17)], 0))

    def test_receptora_com_18_km_nao_avisa(self):
        self.assertEqual(self._badges(self._distantes(), 1), [])

    def test_rota_comum_com_18_km_continua_avisando(self):
        self.assertIn("paradas a 18km entre si (máx 15km)", self._badges(self._distantes(), 0))

    def test_receptora_alem_de_20_km_avisa_com_a_folga(self):
        paradas = self._distantes()
        paradas[1]["longitude"] = paradas[0]["longitude"] + 0.25  # ~25 km
        self.assertIn("paradas a 25km entre si (máx 20km, com folga de rota fraca)", self._badges(paradas, 1))


class TestCaixasETempoDaReceptora(unittest.TestCase):
    """Folga de caixas (110) e de tempo (10h30) da receptora (Hugo, 03/10):
    só no rascunho com recebeu_rota_fraca; a rota comum continua 100/9h."""

    def setUp(self):
        self.horas = 1.0
        for patcher in (mock.patch.object(planejamento_rotas, "_simular_rascunho", lambda paradas: None),
                        mock.patch.object(planejamento_rotas, "_garantir_coords_base", lambda: None),
                        mock.patch.object(planejamento_rotas, "estimar_tempo_rota", lambda *a, **k: self.horas)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _badges(self, caixas_por_parada, recebeu, n=3):
        paradas = [_parada(i, c) for i, c in enumerate(caixas_por_parada[:n])]
        return planejamento_rotas._badges_trava(
            {"paradas": paradas, "tipo_veiculo": None, "tipo_rota": "GRANDE_SP", "recebeu_rota_fraca": recebeu})

    def test_receptora_com_105_caixas_e_10h_nao_avisa(self):
        self.horas = 10.0
        self.assertEqual(self._badges([35, 35, 35], 1), [])

    def test_receptora_com_111_caixas_avisa_citando_a_folga(self):
        self.assertIn("111 caixa(s) (máx 110, com folga de rota fraca)", self._badges([37, 37, 37], 1))

    def test_receptora_acima_de_10h30_avisa_citando_a_folga(self):
        self.horas = 10.6
        badges = self._badges([5, 5, 5], 1)
        self.assertTrue(any(b.startswith("tempo estimado 10.6h (máx 10h30, com folga de rota fraca") for b in badges),
                        badges)

    def test_rota_comum_com_101_caixas_avisa_como_antes(self):
        self.assertIn("101 caixa(s) (máx 100)", self._badges([34, 34, 33], 0))

    def test_rota_comum_acima_de_9h_avisa_como_antes(self):
        self.horas = 9.5
        badges = self._badges([5, 5, 5], 0)
        self.assertTrue(any(b.startswith("tempo estimado 9.5h (máx 9h,") for b in badges), badges)


if __name__ == "__main__":
    unittest.main()
