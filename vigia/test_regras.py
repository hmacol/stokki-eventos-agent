# -*- coding: utf-8 -*-
"""
Regras puras do vigia (estado + prazo). Rodar (da raiz):
    python -m unittest vigia.test_regras -v
"""
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vigia import regras as r

HOJE = date(2026, 9, 29)  # terça


class TestPrazos(unittest.TestCase):
    def test_dia_util_pula_fim_de_semana(self):
        sexta = datetime(2026, 10, 2, 10, 0)
        self.assertEqual(r.somar_dias_uteis(sexta, 1), datetime(2026, 10, 5, 10, 0))
        sabado = datetime(2026, 10, 3, 15, 0)
        self.assertEqual(r.somar_dias_uteis(sabado, 1), datetime(2026, 10, 6, 0, 0))

    def test_prazos_por_estado(self):
        desde = datetime(2026, 9, 29, 8, 0)
        self.assertEqual(r.prazo(r.SEM_SERVICO, desde), datetime(2026, 9, 29, 12, 0))
        self.assertEqual(r.prazo(r.NO_POOL, desde), datetime(2026, 9, 30, 8, 0))
        self.assertEqual(r.prazo(r.AGUARDANDO_CLIENTE, desde), datetime(2026, 10, 1, 8, 0))
        self.assertEqual(r.prazo(r.INSUCESSO, desde), datetime(2026, 9, 29, 21, 0))
        self.assertEqual(r.prazo(r.ROTA_PASSADA, desde), desde)
        self.assertIsNone(r.prazo(r.AGENDADO, desde))

    def test_rascunho_vence_19h_do_dia_util_anterior(self):
        segunda = date(2026, 10, 5)
        self.assertEqual(r.prazo(r.EM_RASCUNHO, datetime(2026, 10, 2, 18, 0), data_rascunho=segunda),
                         datetime(2026, 10, 2, 19, 0))


class TestClassificar(unittest.TestCase):
    def test_aberto_na_stokki_sem_servico(self):
        est, motivo = r.classificar({"status_nucleo": None, "aberto_stokki": True,
                                     "acao_pipeline": "aguardando_redespacho"}, HOJE)
        self.assertEqual(est, r.SEM_SERVICO)
        self.assertIn("aguardando_redespacho", motivo)
        self.assertIsNone(r.classificar({"status_nucleo": None, "aberto_stokki": False}, HOJE))

    def test_pool_rascunho_agendamento(self):
        base = {"status_nucleo": "ABERTO"}
        self.assertEqual(r.classificar(base, HOJE)[0], r.NO_POOL)
        self.assertEqual(r.classificar({**base, "rascunho_status": "RASCUNHO", "rascunho_data": HOJE}, HOJE)[0],
                         r.EM_RASCUNHO)
        self.assertEqual(r.classificar({**base, "rascunho_status": "ERRO_ENVIO", "rascunho_data": HOJE}, HOJE)[0],
                         r.RASCUNHO_COM_ERRO)
        self.assertEqual(r.classificar({**base, "agendamento": date(2026, 10, 9)}, HOJE)[0], r.AGENDADO)
        self.assertEqual(r.classificar({**base, "agendamento_pendente": True}, HOJE)[0], r.AGUARDANDO_CLIENTE)

    def test_motivo_do_pool(self):
        est, motivo = r.classificar({"status_nucleo": "ABERTO", "motivo_pool": "dedicado"}, HOJE)
        self.assertEqual((est, motivo), (r.NO_POOL, "dedicado"))

    def test_rota(self):
        self.assertEqual(r.classificar({"status_nucleo": "EM_ROTA", "rota_data": date(2026, 9, 28)}, HOJE)[0],
                         r.ROTA_PASSADA)
        self.assertEqual(r.classificar({"status_nucleo": "EM_ROTA", "rota_data": HOJE}, HOJE)[0], r.EM_ROTA)

    def test_insucesso(self):
        base = {"status_nucleo": "INSUCESSO", "aberto_stokki": True}
        self.assertEqual(r.classificar(base, HOJE)[0], r.INSUCESSO)
        self.assertIsNone(r.classificar({**base, "tem_reentrega": True}, HOJE))
        self.assertIsNone(r.classificar({**base, "tratado_na_torre": True}, HOJE))
        self.assertEqual(r.classificar({**base, "recusado": True}, HOJE)[0], r.RECUSADO)

    def test_entregue_some(self):
        self.assertIsNone(r.classificar({"status_nucleo": "ENTREGUE", "aberto_stokki": True}, HOJE))


if __name__ == "__main__":
    unittest.main()
