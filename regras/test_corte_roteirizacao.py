# -*- coding: utf-8 -*-
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

from regras import agendamento_automatico as aa
from regras import corte_roteirizacao as cr


class CorteConfiguravel(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "t.db"

    def test_padrao_sem_valor_salvo(self):
        self.assertEqual(cr.obter_corte(self.db), time(14, 15))

    def test_salva_e_le(self):
        cr.definir_corte("15:30", "Fulano", self.db)
        self.assertEqual(cr.obter_corte(self.db), time(15, 30))
        d = cr.detalhe_corte(self.db)
        self.assertEqual((d["hhmm"], d["alterado_por"]), ("15:30", "Fulano"))

    def test_rejeita_formato_invalido(self):
        for ruim in ("", "25:00", "14:60", "1415", "abc", None):
            with self.assertRaises(ValueError):
                cr.definir_corte(ruim, "x", self.db)
        self.assertEqual(cr.obter_corte(self.db), time(14, 15))

    def test_dentro_do_corte_inclusive(self):
        cr.definir_corte("14:00", "x", self.db)
        self.assertTrue(cr.dentro_do_corte(time(14, 0), self.db))
        self.assertFalse(cr.dentro_do_corte(time(14, 1), self.db))

    def test_data_automatica_usa_corte_informado(self):
        seg = lambda h, m: datetime(2026, 11, 9, h, m)  # segunda
        self.assertEqual(aa.data_automatica(seg(14, 10), corte=time(14, 0)), date(2026, 11, 11))
        self.assertEqual(aa.data_automatica(seg(14, 10), corte=time(15, 0)), date(2026, 11, 10))
        # sabado nunca e "hoje"
        self.assertEqual(aa.data_automatica(datetime(2026, 11, 14, 9, 0), corte=time(23, 0)), date(2026, 11, 17))

    def test_corte_pela_hora_de_lancamento(self):
        corte = time(14, 15)
        rodada = datetime(2026, 11, 9, 14, 30)  # segunda, rodada depois do corte
        # lancado antes do corte, processado depois: vale o lancamento (ter)
        self.assertEqual(aa.data_automatica(rodada, corte=corte, lancado_em=datetime(2026, 11, 9, 13, 50)),
                         date(2026, 11, 10))
        # lancado depois do corte: qua
        self.assertEqual(aa.data_automatica(rodada, corte=corte, lancado_em=datetime(2026, 11, 9, 14, 20)),
                         date(2026, 11, 11))
        # sem lancamento: regra da rodada (qua)
        self.assertEqual(aa.data_automatica(rodada, corte=corte), date(2026, 11, 11))

    def test_lancamento_antigo_nao_gera_data_passada(self):
        corte = time(14, 15)
        rodada = datetime(2026, 11, 13, 9, 0)  # sexta de manha
        # lancado na quarta de manha, so processado na sexta: ter+1=qui seria passado -> regra da rodada
        self.assertEqual(aa.data_automatica(rodada, corte=corte, lancado_em=datetime(2026, 11, 11, 9, 0)),
                         date(2026, 11, 16))
        # lancado quinta depois do corte, processado sexta cedo: seg? qui>corte -> sex+1 = seg
        self.assertEqual(aa.data_automatica(rodada, corte=corte, lancado_em=datetime(2026, 11, 12, 16, 0)),
                         date(2026, 11, 16))

    def test_lancamento_aware_e_naive(self):
        from datetime import timezone, timedelta
        rodada = datetime(2026, 11, 9, 14, 30, tzinfo=timezone(timedelta(hours=-3)))
        self.assertEqual(aa.data_automatica(rodada, corte=time(14, 15), lancado_em=datetime(2026, 11, 9, 10, 0)),
                         date(2026, 11, 10))

    def test_lancado_em_da_linha(self):
        import pipeline
        self.assertEqual(pipeline._lancado_em_da_linha({"created_at": "24/11/2026 15:49"}),
                         datetime(2026, 11, 24, 15, 49))
        self.assertIsNone(pipeline._lancado_em_da_linha({"created_at": "24/11/2026"}))
        self.assertIsNone(pipeline._lancado_em_da_linha({}))
        self.assertIsNone(pipeline._lancado_em_da_linha("texto"))


if __name__ == "__main__":
    unittest.main()
