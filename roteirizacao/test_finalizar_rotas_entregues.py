# -*- coding: utf-8 -*-
import unittest
from unittest import mock

import finalizar_rotas_entregues as f


def _rota(status="started", servicos=("done", "done"), rid=1, agente=31828):
    return {"id": rid, "status": status, "name": f"Planejamento - #{rid}", "agent_id": agente,
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


class LalamoveTest(unittest.TestCase):
    def test_so_lalamove_nao_iniciada_com_tudo_entregue(self):
        lala = f.AGENTE_LALAMOVE
        self.assertEqual(len(f.rotas_lalamove_para_finalizar([_rota("assigned", agente=lala)])), 1)
        self.assertEqual(f.rotas_lalamove_para_finalizar([_rota("assigned")]), [])  # motorista de verdade
        self.assertEqual(f.rotas_lalamove_para_finalizar([_rota("started", agente=lala)]), [])  # vai pelo finish
        self.assertEqual(f.rotas_lalamove_para_finalizar([_rota("assigned", ("done", "assigned"), agente=lala)]), [])

    def _rodar(self, falha_em=None, volta=True):
        chamadas = []
        estado = {"agent_id": f.AGENTE_LALAMOVE}

        def trocar(token, rid, agente):
            chamadas.append(("trocar", agente))
            if agente == f.AGENTE_DO_TOKEN or volta:
                estado["agent_id"] = agente

        def comando(token, rid, acao):
            chamadas.append(acao)
            if acao == falha_em:
                raise RuntimeError("403")

        def ler(token, rid):
            return _rota("assigned", rid=rid, agente=estado["agent_id"])

        with mock.patch.multiple(f, trocar_agente_rota=trocar, comando_rota=comando, _ler_rota=ler,
                                 finalizar_rota=lambda token, rid: chamadas.append("finish")):
            try:
                return f.finalizar_rota_lalamove("t", 9), chamadas
            except Exception as e:
                return e, chamadas

    def test_ordem_da_troca(self):
        ok, chamadas = self._rodar()
        self.assertIs(ok, True)
        self.assertEqual(chamadas, [("trocar", f.AGENTE_DO_TOKEN), "accept", "start",
                                    ("trocar", f.AGENTE_LALAMOVE), "finish"])

    def test_start_falhou_devolve_e_nao_finaliza(self):
        erro, chamadas = self._rodar(falha_em="start")
        self.assertIsInstance(erro, RuntimeError)
        self.assertEqual(chamadas[-1], ("trocar", f.AGENTE_LALAMOVE))

    def test_accept_recusado_segue(self):
        ok, _ = self._rodar(falha_em="accept")
        self.assertIs(ok, True)

    def test_rota_que_nao_voltou_para_tudo(self):
        erro, chamadas = self._rodar(volta=False)
        self.assertIsInstance(erro, f.DevolucaoFalhou)
        self.assertNotIn("finish", chamadas)


if __name__ == "__main__":
    unittest.main()
