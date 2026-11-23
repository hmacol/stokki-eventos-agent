# -*- coding: utf-8 -*-
"""
test_fingerprint_status_vuupt.py

Marcação "já saiu de not_assigned": só 'done' vale pra sempre; o resto
vence (unassign devolve o serviço pro pool -- achado 28/09).

Rodar (da raiz):
    python -m unittest test_fingerprint_status_vuupt -v
"""
import unittest
from datetime import datetime

from fingerprint_status_vuupt import VALIDADE_HORAS, marcacao_vigente

AGORA = datetime(2026, 9, 28, 12, 0, 0)


class TestMarcacaoVigente(unittest.TestCase):
    def test_done_vale_pra_sempre(self):
        self.assertTrue(marcacao_vigente("done", "2026-01-01 00:00:00", AGORA))

    def test_assigned_recente_vale(self):
        self.assertTrue(marcacao_vigente("assigned", "2026-09-28 08:00:00", AGORA))

    def test_assigned_antigo_vence(self):
        self.assertFalse(marcacao_vigente("assigned", "2026-09-27 23:59:59", AGORA))
        self.assertEqual(VALIDADE_HORAS, 12)

    def test_data_invalida_vence(self):
        self.assertFalse(marcacao_vigente("on_route", None, AGORA))
        self.assertFalse(marcacao_vigente("", "lixo", AGORA))


if __name__ == "__main__":
    unittest.main()
