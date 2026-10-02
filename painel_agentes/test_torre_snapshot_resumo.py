# -*- coding: utf-8 -*-
"""
test_torre_snapshot_resumo.py

Snapshot do resumo do dia que buscar_dados_torre publica pra pagina
inicial (/inicio): le sem coletar nada e nunca devolve numero de outro
dia.

Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_torre_snapshot_resumo -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import torre_controle  # noqa: E402

ROTAS = {"concluidas": 4, "em_andamento": 6, "nao_iniciadas": 1, "atrasadas": 2}
PEDIDOS = {"total": 118, "sucesso": 71, "falha": 3, "qtd_nao_atribuidos": 5}


class TestSnapshotResumoDia(unittest.TestCase):

    def setUp(self):
        self._guardado = dict(torre_controle._snapshot_resumo_dia)

    def tearDown(self):
        torre_controle._snapshot_resumo_dia.clear()
        torre_controle._snapshot_resumo_dia.update(self._guardado)

    def test_sem_leitura_devolve_none(self):
        torre_controle._snapshot_resumo_dia.clear()
        torre_controle._snapshot_resumo_dia.update({"data_iso": None})
        self.assertIsNone(torre_controle.snapshot_resumo_dia(date(2026, 9, 30)))

    def test_publicar_e_ler_no_mesmo_dia(self):
        torre_controle._publicar_resumo_dia(date(2026, 9, 30), "14:32:10", ROTAS, PEDIDOS)
        snap = torre_controle.snapshot_resumo_dia(date(2026, 9, 30))
        self.assertEqual(snap["gerado_em"], "14:32:10")
        self.assertEqual(snap["rotas_resumo"], ROTAS)
        self.assertEqual(snap["pedidos"],
                         {"total": 118, "entregues": 71, "insucessos": 3, "sem_rota": 5})

    def test_leitura_de_outro_dia_devolve_none(self):
        torre_controle._publicar_resumo_dia(date(2026, 9, 30), "23:59:00", ROTAS, PEDIDOS)
        self.assertIsNone(torre_controle.snapshot_resumo_dia(date(2026, 10, 1)))

    def test_snapshot_e_copia(self):
        torre_controle._publicar_resumo_dia(date(2026, 9, 30), "14:32:10", ROTAS, PEDIDOS)
        snap = torre_controle.snapshot_resumo_dia(date(2026, 9, 30))
        snap["rotas_resumo"]["atrasadas"] = 99
        self.assertEqual(torre_controle.snapshot_resumo_dia(date(2026, 9, 30))["rotas_resumo"]["atrasadas"], 2)


if __name__ == "__main__":
    unittest.main()
