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

        def trocar(token, rid, agente, veiculo=None):
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


class RotaDoAppTest(unittest.TestCase):
    APP = {7}

    def test_so_rota_do_app_com_motorista_real_nao_iniciada(self):
        pega = lambda **kw: f.rotas_app_para_finalizar([_rota(rid=7, **kw)], self.APP)
        self.assertEqual(len(pega(status="not_started")), 1)
        self.assertEqual(len(pega(status="assigned")), 1)
        self.assertEqual(pega(status="started"), [])                       # vai pelo finish
        self.assertEqual(pega(status="not_started", servicos=("done", "assigned")), [])
        self.assertEqual(pega(status="not_started", agente=None), [])
        self.assertEqual(pega(status="not_started", agente=f.AGENTE_LALAMOVE), [])
        self.assertEqual(pega(status="not_started", agente=f.AGENTE_DO_TOKEN), [])
        # rota da Vuupt (fora do nucleo operada_app): nao entra
        self.assertEqual(f.rotas_app_para_finalizar([_rota("not_started", rid=8)], self.APP), [])

    def _rodar(self, volta=True, veiculo_volta=True, falha_em=None, status_atual="not_started"):
        chamadas, estado = [], {"agent_id": 47084, "vehicle_id": 103182}

        def trocar(token, rid, agente, veiculo=None):
            chamadas.append(("trocar", agente, veiculo))
            if agente == f.AGENTE_DO_TOKEN:
                estado["agent_id"] = agente
            elif volta:
                estado["agent_id"] = agente
                estado["vehicle_id"] = veiculo if veiculo_volta else None

        def comando(token, rid, acao):
            chamadas.append(acao)
            if acao == falha_em:
                raise RuntimeError("403")

        def ler(token, rid):
            return {**_rota(status_atual, rid=rid, agente=estado["agent_id"]), "vehicle_id": estado["vehicle_id"]}

        elegivel = lambda rs: f.rotas_app_para_finalizar(rs, {9})
        with mock.patch.multiple(f, trocar_agente_rota=trocar, comando_rota=comando, _ler_rota=ler,
                                 finalizar_rota=lambda token, rid: chamadas.append("finish")):
            try:
                return f.finalizar_rota_pela_troca("t", 9, elegivel), chamadas
            except Exception as e:
                return e, chamadas

    def test_devolve_agente_e_veiculo_originais(self):
        ok, chamadas = self._rodar()
        self.assertIs(ok, True)
        self.assertEqual(chamadas, [("trocar", f.AGENTE_DO_TOKEN, None), "accept", "start",
                                    ("trocar", 47084, 103182), "finish"])

    def test_veiculo_que_nao_voltou_para_tudo(self):
        erro, chamadas = self._rodar(veiculo_volta=False)
        self.assertIsInstance(erro, f.DevolucaoFalhou)
        self.assertNotIn("finish", chamadas)

    def test_agente_que_nao_voltou_para_tudo(self):
        erro, chamadas = self._rodar(volta=False)
        self.assertIsInstance(erro, f.DevolucaoFalhou)
        self.assertNotIn("finish", chamadas)

    def test_start_falhou_devolve_e_nao_finaliza(self):
        erro, chamadas = self._rodar(falha_em="start")
        self.assertIsInstance(erro, RuntimeError)
        self.assertEqual(chamadas[-1], ("trocar", 47084, 103182))
        self.assertNotIn("finish", chamadas)

    def test_rota_que_mudou_desde_a_listagem_nao_e_tocada(self):
        ok, chamadas = self._rodar(status_atual="started")
        self.assertIs(ok, False)
        self.assertEqual(chamadas, [])


class MainTest(unittest.TestCase):
    def _main(self, chave, modo_teste, rotas, ids=frozenset({7, 8})):
        chamadas = []
        config = {"vuupt_api": {"token": "t"}, "finalizar_rotas_entregues": {"fechar_app_nao_iniciada": chave}}
        with mock.patch.multiple(f, _carregar_config=lambda: config, listar_rotas=lambda *a, **k: rotas,
                                 _ids_rotas_do_app=lambda: set(ids),
                                 finalizar_rota_pela_troca=lambda t, rid, el: chamadas.append(rid) or True,
                                 finalizar_rota=lambda t, rid: chamadas.append(("finish", rid))):
            falhas = f.main(modo_teste=modo_teste)
        return falhas, chamadas

    def test_chave_desligada_nao_troca_nada(self):
        _, chamadas = self._main(False, False, [_rota("not_started", rid=7)])
        self.assertEqual(chamadas, [])

    def test_chave_ligada_troca_so_a_rota_do_app(self):
        _, chamadas = self._main(True, False, [_rota("not_started", rid=7), _rota("assigned", rid=99)])
        self.assertEqual(chamadas, [7])

    def test_modo_teste_nunca_escreve(self):
        _, chamadas = self._main(True, True, [_rota("not_started", rid=7), _rota("started", rid=5)])
        self.assertEqual(chamadas, [])

    def test_limite_por_rodada(self):
        rotas = [_rota("not_started", rid=i) for i in range(1, 30)]
        _, chamadas = self._main(True, False, rotas, ids=range(1, 30))
        self.assertEqual(len(chamadas), f.MAX_TROCAS_POR_RODADA)


if __name__ == "__main__":
    unittest.main()
