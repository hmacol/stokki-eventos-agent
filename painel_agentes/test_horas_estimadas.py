# -*- coding: utf-8 -*-
"""
test_horas_estimadas.py

A coluna horas_estimadas (22/09, Hugo) alimenta o rodizio de rotas
longas na alocacao: so da pra saber quem pegou rota pesada na semana se
a duracao de cada rota ficar gravada. A duracao REAL (iniciada_em/
concluida_em) nao serve -- o motorista confirma paradas em lote na Vuupt
e o timestamp sai distorcido.

Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_horas_estimadas -v
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


class TestColunaHorasEstimadas(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        caminho = Path(self.tmp.name) / "dados.db"
        for patcher in (
            mock.patch.object(rascunhos_rota, "DB_PATH", caminho),
            mock.patch("mapa_util.carregar_remetentes_por_sender_id", lambda: {}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.caminho = caminho

    def _colunas(self):
        conn = rascunhos_rota._conectar()
        try:
            return {r["name"] for r in conn.execute("PRAGMA table_info(rascunhos_rota)")}
        finally:
            conn.close()

    def test_coluna_existe(self):
        self.assertIn("horas_estimadas", self._colunas())

    def test_migracao_em_banco_antigo(self):
        # Banco criado antes de 22/09 nao tem a coluna; _conectar precisa
        # adiciona-la (CREATE TABLE IF NOT EXISTS nao altera tabela).
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
        self.assertIn("horas_estimadas", self._colunas())

    def test_valor_gravado_volta_na_leitura(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 22), [{
            "nome": "Rota 1", "start_location_base_id": 1,
            "start_at": "2026-09-22T09:00:00Z",
            "km_estimado": 42.0, "horas_estimadas": 7.5, "sublote": [],
        }])
        rotas = rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 22))
        self.assertEqual(rotas[0]["horas_estimadas"], 7.5)

    def test_sem_valor_fica_nulo(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 22), [{
            "nome": "Rota 2", "start_location_base_id": 1,
            "start_at": "2026-09-22T09:00:00Z", "sublote": [],
        }])
        rotas = rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 22))
        self.assertIsNone(rotas[0]["horas_estimadas"])

    def test_duplicar_copia_as_horas(self):
        rascunhos_rota.criar_lote_rascunhos(date(2026, 9, 22), [{
            "nome": "Rota 3", "start_location_base_id": 1,
            "start_at": "2026-09-22T09:00:00Z", "horas_estimadas": 8.2, "sublote": [],
        }])
        origem = rascunhos_rota.listar_rascunhos_do_dia(date(2026, 9, 22))[0]
        novo_id = rascunhos_rota.duplicar_rascunho(origem["id"])
        self.assertEqual(rascunhos_rota.buscar_rascunho(novo_id)["horas_estimadas"], 8.2)


if __name__ == "__main__":
    unittest.main()
