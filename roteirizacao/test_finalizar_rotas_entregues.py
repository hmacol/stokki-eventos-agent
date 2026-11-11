# -*- coding: utf-8 -*-
import unittest

import finalizar_rotas_entregues as f


def _rota(status="started", servicos=("done", "done"), rid=1):
    return {"id": rid, "status": status, "name": f"Planejamento - #{rid}",
            "services": {"data": [{"id": i, "status": s} for i, s in enumerate(servicos, 1)]}}


class RotasParaFinalizarTest(unittest.TestCase):
    def test_iniciada_com_tudo_entregue_entra(self):
        self.assertEqual([r["id"] for r in f.rotas_para_finalizar([_rota()])], [1])

    def test_servico_aberto_segura_a_rota(self):
        for aberto in ("assigned", "accepted", "on_route", "arrived", "not_assigned"):
            self.assertEqual(f.rotas_para_finalizar([_rota(servicos=("done", aberto))]), [], aberto)

    def test_so_rota_iniciada(self):
        # a Vuupt recusa (409) finalizar rota que o motorista nao iniciou
        for status in ("assigned", "not_started", "scheduled", "finished", "canceled"):
            self.assertEqual(f.rotas_para_finalizar([_rota(status=status)]), [], status)

    def test_cancelado_nao_segura_mas_precisa_de_um_entregue(self):
        self.assertEqual(len(f.rotas_para_finalizar([_rota(servicos=("done", "canceled"))])), 1)
        self.assertEqual(f.rotas_para_finalizar([_rota(servicos=("canceled",))]), [])

    def test_rota_sem_servico_fica_de_fora(self):
        self.assertEqual(f.rotas_para_finalizar([_rota(servicos=())]), [])


if __name__ == "__main__":
    unittest.main()
