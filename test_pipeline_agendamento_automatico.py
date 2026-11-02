# -*- coding: utf-8 -*-
"""
Data automatica na criacao do servico (pipeline). Rodar (da raiz):
py -3.11 -m unittest test_pipeline_agendamento_automatico -v
"""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import pipeline


class TestAgendamentoAutomatico(unittest.TestCase):
    INICIO = datetime(2026, 11, 9, 15, 5)

    def test_servico_novo_ganha_data_e_horario(self):
        r = pipeline.agendamento_automatico(None, None, "123", "", self.INICIO, {"123": ("07:00", "10:00")}, {})
        self.assertEqual(r, (pipeline._converter_data_para_iso("11/11/2026 07:00"),
                             pipeline._converter_data_para_iso("11/11/2026 10:00")))

    def test_servico_que_ja_existe_nao_ganha_nem_muda(self):
        for existente in ({"id": 1, "scheduled_start": None}, {"id": 1, "scheduled_start": "2026-11-10 03:00:00"}):
            self.assertIsNone(pipeline.agendamento_automatico(existente, None, "123", "", self.INICIO, {}, {}))

    def test_antes_de_03_11_ou_sem_inicio_nao_age(self):
        self.assertIsNone(pipeline.agendamento_automatico(None, None, "1", "", datetime(2026, 11, 2, 22), {}, {}))
        self.assertIsNone(pipeline.agendamento_automatico(None, None, "1", "", None, {}, {}))

    def test_registra_origem_automatica(self):
        from roteirizacao import registro_dia_fixo as reg
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            servico = {"id": 9, "code": "#PS-1"}
            n = pipeline.registrar_data_automatica(servico, "2026-11-11T07:00:00-03:00", db_path=db)
            self.assertEqual(n, 1)
            conn = reg.conectar(db)
            try:
                self.assertTrue(reg.data_e_automatica(conn, servico, date(2026, 11, 11)))
                self.assertFalse(reg.data_e_automatica(conn, servico, date(2026, 11, 12)))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
