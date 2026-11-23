import unittest
from datetime import datetime

from galpao3d.turno import deve_coletar, em_turno, inicio_dia_operacional, minutos_uteis


class TurnoTest(unittest.TestCase):
    def test_noite_nao_conta(self):
        self.assertEqual(minutos_uteis(datetime(2026, 10, 22, 17, 50), datetime(2026, 10, 23, 8, 10), "separacao_conferencia"), 20)

    def test_fim_de_semana_nao_conta(self):  # 23/10/2026 é sexta
        self.assertEqual(minutos_uteis(datetime(2026, 10, 23, 17, 50), datetime(2026, 10, 26, 8, 10), "separacao_conferencia"), 20)

    def test_mesmo_turno(self):
        self.assertEqual(minutos_uteis(datetime(2026, 10, 22, 11, 22), datetime(2026, 10, 22, 19, 9), "separacao_conferencia"), 398)

    def test_fim_antes_do_inicio_retorna_zero(self):
        self.assertEqual(minutos_uteis(datetime(2026, 10, 22, 11, 0), datetime(2026, 10, 22, 10, 0), "galpao"), 0)

    def test_dia_operacional_zera_as_4h(self):
        self.assertEqual(inicio_dia_operacional(datetime(2026, 10, 23, 3, 59)), datetime(2026, 10, 22, 4, 0))
        self.assertEqual(inicio_dia_operacional(datetime(2026, 10, 23, 4, 0)), datetime(2026, 10, 23, 4, 0))

    def test_em_turno(self):
        self.assertTrue(em_turno(datetime(2026, 10, 22, 8, 0), "separacao_conferencia"))
        self.assertFalse(em_turno(datetime(2026, 10, 22, 18, 0), "separacao_conferencia"))  # fim e exclusivo
        self.assertFalse(em_turno(datetime(2026, 10, 24, 10, 0), "separacao_conferencia"))  # sabado

    def test_janela_de_coleta(self):
        self.assertTrue(deve_coletar(datetime(2026, 10, 23, 3, 50)))
        self.assertTrue(deve_coletar(datetime(2026, 10, 23, 18, 30)))
        self.assertFalse(deve_coletar(datetime(2026, 10, 23, 18, 31)))
        self.assertTrue(deve_coletar(datetime(2026, 10, 23, 22, 15)))
        self.assertFalse(deve_coletar(datetime(2026, 10, 23, 22, 14)))

    def test_coleta_em_dia_sem_turno_so_de_15_em_15(self):  # 24/10/2026 e sabado
        self.assertFalse(deve_coletar(datetime(2026, 10, 24, 10, 7)))
        self.assertTrue(deve_coletar(datetime(2026, 10, 24, 10, 15)))
        self.assertTrue(deve_coletar(datetime(2026, 10, 23, 10, 7)))


if __name__ == "__main__":
    unittest.main()
