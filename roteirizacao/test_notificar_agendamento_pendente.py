# -*- coding: utf-8 -*-
"""
Agendamento pendente x data automatica (revisao final, 02/11): destinatario
que virou AGENDA depois de o pedido ganhar a data automatica continua
pendente (a data do sistema nao e agendamento do cliente).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_notificar_agendamento_pendente -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import notificar_agendamento_pendente as nap

HOJE = date(2026, 11, 10)
AGENDA = {"111"}


def _servico(sid, start="2026-11-11T07:00:00-03:00", automatica=False):
    s = {"id": sid, "code": f"#PS-{sid}", "customer_id": 900 + sid, "scheduled_start": start}
    if automatica:
        s["_data_automatica"] = True
    return s


class TestPendentes(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(nap.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        self.vuupt = mock.Mock()
        self.vuupt.buscar_customer_por_id.side_effect = lambda cid: {"code": "111" if cid != 903 else "222",
                                                                       "name": "DEST"}

    def _pendentes(self, servicos, confirmacao=None):
        return nap.identificar_pendentes(servicos, AGENDA, self.vuupt, lambda doc, conj: doc in conj,
                                         hoje=HOJE, buscar_confirmacao_fn=confirmacao)

    def test_data_automatica_futura_de_destinatario_agenda_fica_pendente(self):
        pend = self._pendentes([_servico(1, automatica=True)])
        self.assertEqual([s["id"] for s in pend], [1])

    def test_data_do_cliente_futura_continua_valida(self):
        self.assertEqual(self._pendentes([_servico(2)]), [])

    def test_data_automatica_de_destinatario_sem_agenda_nao_e_pendente(self):
        self.assertEqual(self._pendentes([_servico(3, automatica=True)]), [])

    def test_data_automatica_com_confirmacao_recebida_nao_e_pendente(self):
        self.assertEqual(self._pendentes([_servico(1, automatica=True)], confirmacao=lambda cod: {"data": "x"}), [])

    def test_precisa_de_agendamento_valido(self):
        self.assertTrue(nap._precisa_de_agendamento_valido(_servico(1, automatica=True), HOJE))
        self.assertFalse(nap._precisa_de_agendamento_valido(_servico(1), HOJE))
        self.assertTrue(nap._precisa_de_agendamento_valido(_servico(1, start=None), HOJE))
        self.assertTrue(nap._precisa_de_agendamento_valido(_servico(1, start="2026-11-09T07:00:00-03:00"), HOJE))


if __name__ == "__main__":
    unittest.main()
