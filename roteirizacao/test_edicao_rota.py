# -*- coding: utf-8 -*-
"""
Regra unica de edicao de rota enviada (le a rota AO VIVO da Vuupt).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_edicao_rota -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import edicao_rota


def _servico(i, status="assigned", code=None):
    return {"id": i, "code": code or f"#PS-{i}", "status": status, "dimension_3": 1.5}


def _corpo(servicos, status="not_started", agent_id=10, vehicle_id=20, rid=7):
    return {"route": {"id": rid, "status": status, "agent_id": agent_id, "vehicle_id": vehicle_id,
                      "services": {"data": servicos}}}


class _Rotas:
    """Duble de rotas_client com a rota em memoria."""

    def __init__(self, corpo=None, erro_busca=None, erro_escrita=None):
        self.corpo = corpo
        self.erro_busca = erro_busca
        self.erro_escrita = erro_escrita
        self.deletes, self.cancelamentos, self.puts, self.adds = [], [], [], []
        self.kwargs = []          # kwargs extras de cada chamada (interativo so quando pedido)
        self.resp_add = {"data": {"info": {"failed": []}}}

    def buscar_rota(self, token, route_id, include=None, **kw):
        self.kwargs.append(("buscar", kw))
        if self.erro_busca:
            raise self.erro_busca
        return self.corpo

    def remover_servico_da_rota(self, token, route_id, service_id, **kw):
        self.kwargs.append(("remover", kw))
        if self.erro_escrita:
            raise self.erro_escrita
        self.deletes.append((route_id, service_id))
        return {"success": True}

    def cancelar_rota(self, token, route_id, services_action="keep", **kw):
        self.kwargs.append(("cancelar", kw))
        if self.erro_escrita:
            raise self.erro_escrita
        self.cancelamentos.append((route_id, services_action))

    def atualizar_motorista_veiculo(self, token, route_id, agent_id, vehicle_id):
        self.puts.append((route_id, agent_id, vehicle_id))
        return {"id": route_id, "agent_id": agent_id, "vehicle_id": vehicle_id}

    def adicionar_atividades(self, token, route_id, service_ids, position="optimize"):
        self.adds.append((route_id, list(service_ids), position))
        return self.resp_add


class TestEdicaoRota(unittest.TestCase):
    def _com(self, rotas):
        p = mock.patch.object(edicao_rota, "rotas_client", rotas)
        p.start()
        self.addCleanup(p.stop)
        return rotas

    def test_ler_rota_formato(self):
        r = self._com(_Rotas(_corpo([_servico(1)])))
        rota = edicao_rota.ler_rota("t", 7)
        self.assertEqual(rota, {"id": 7, "name": None, "status": "not_started", "agent_id": 10, "vehicle_id": 20,
                                "servicos": [{"id": 1, "code": "#PS-1", "status": "assigned",
                                              "dimension_3": 1.5}]})

    def test_ler_rota_404(self):
        resp = mock.Mock(status_code=404)
        self._com(_Rotas(erro_busca=requests.exceptions.HTTPError("404", response=resp)))
        with self.assertRaises(edicao_rota.ErroEdicao) as cm:
            edicao_rota.ler_rota("t", 7)
        self.assertEqual(str(cm.exception), "Rota #7 não existe mais na Vuupt.")

    def test_tira_pendente_usa_delete(self):
        r = self._com(_Rotas(_corpo([_servico(1), _servico(2)])))
        res = edicao_rota.tirar_pedido("t", 7, 1)
        self.assertTrue(res["ok"])
        self.assertFalse(res["rota_cancelada"])
        self.assertEqual(r.deletes, [(7, 1)])
        self.assertEqual(res["servico"]["id"], 1)

    def test_recusa_pelo_status_ao_vivo(self):
        r = self._com(_Rotas(_corpo([_servico(1, "on_route"), _servico(2)], status="started")))
        res = edicao_rota.tirar_pedido("t", 7, 1)
        self.assertFalse(res["ok"])
        self.assertIn("já começou", res["erro"])
        self.assertEqual(r.deletes, [])

    def test_recusa_entregue_e_insucesso(self):
        r = self._com(_Rotas(_corpo([_servico(1, "done"), _servico(2)], status="started")))
        res = edicao_rota.tirar_pedido("t", 7, 1)
        self.assertFalse(res["ok"])
        self.assertIn("concluído", res["erro"])
        self.assertEqual(r.deletes, [])

    def test_recusa_cancelado(self):
        r = self._com(_Rotas(_corpo([_servico(1, "canceled"), _servico(2)])))
        res = edicao_rota.tirar_pedido("t", 7, 1)
        self.assertFalse(res["ok"])
        self.assertEqual(r.deletes, [])

    def test_ultimo_pedido_sem_nada_comecado_cancela_rota(self):
        r = self._com(_Rotas(_corpo([_servico(1)])))
        res = edicao_rota.tirar_pedido("t", 7, 1)
        self.assertTrue(res["ok"])
        self.assertTrue(res["rota_cancelada"])
        self.assertEqual(r.cancelamentos, [(7, "unassign")])
        self.assertEqual(r.deletes, [])

    def test_ultimo_pendente_com_entregue_nao_cancela_rota(self):
        r = self._com(_Rotas(_corpo([_servico(1, "done"), _servico(2)], status="started")))
        res = edicao_rota.tirar_pedido("t", 7, 2)
        self.assertTrue(res["ok"])
        self.assertFalse(res["rota_cancelada"])
        self.assertTrue(res["rota_iniciada"])
        self.assertEqual(r.cancelamentos, [])
        self.assertEqual(r.deletes, [(7, 2)])

    def test_pedido_fora_da_rota(self):
        self._com(_Rotas(_corpo([_servico(1)])))
        res = edicao_rota.tirar_pedido("t", 7, 99)
        self.assertEqual(res, {"ok": False, "fora_da_rota": True, "erro": "Pedido 99 não está mais nessa rota."})

    def test_rota_encerrada_recusa(self):
        for st in ("finished", "canceled"):
            r = self._com(_Rotas(_corpo([_servico(1), _servico(2)], status=st)))
            res = edicao_rota.tirar_pedido("t", 7, 1)
            self.assertFalse(res["ok"], st)
            self.assertEqual(r.deletes, [])
            self.assertEqual(r.cancelamentos, [])

    def test_troca_motorista_recusada_em_rota_iniciada(self):
        r = self._com(_Rotas(_corpo([_servico(1, "on_route")], status="started", agent_id=10)))
        res = edicao_rota.trocar_motorista_veiculo("t", 7, 11, 30)
        self.assertEqual(res, {"ok": False, "erro": "Rota já começou: só o carro pode mudar."})
        self.assertEqual(r.puts, [])

    def test_troca_so_carro_em_rota_iniciada(self):
        r = self._com(_Rotas(_corpo([_servico(1, "on_route")], status="started", agent_id=10)))
        res = edicao_rota.trocar_motorista_veiculo("t", 7, 10, 30)
        self.assertTrue(res["ok"])
        self.assertEqual(r.puts, [(7, 10, 30)])

    def test_troca_motorista_em_rota_nao_iniciada(self):
        for st in ("assigned", "accepted"):
            r = self._com(_Rotas(_corpo([_servico(1, st)], agent_id=10)))
            res = edicao_rota.trocar_motorista_veiculo("t", 7, 11, 30)
            self.assertTrue(res["ok"], st)
            self.assertEqual(r.puts, [(7, 11, 30)])

    def test_troca_recusada_em_rota_encerrada(self):
        r = self._com(_Rotas(_corpo([_servico(1, "done")], status="finished")))
        res = edicao_rota.trocar_motorista_veiculo("t", 7, 10, 30)
        self.assertFalse(res["ok"])
        self.assertEqual(r.puts, [])

    def test_adicionar_devolve_falharam(self):
        r = self._com(_Rotas())
        r.resp_add = {"data": {"info": {"failed": [{"id": 9}]}}}
        res = edicao_rota.adicionar_pedidos("t", 7, [8, 9])
        self.assertEqual(res, {"ok": True, "falharam": [9]})
        self.assertEqual(r.adds, [(7, [8, 9], "end")])

    def test_adicionar_erro_vira_ok_false(self):
        r = self._com(_Rotas())
        r.adicionar_atividades = mock.Mock(side_effect=RuntimeError("boom"))
        res = edicao_rota.adicionar_pedidos("t", 7, [8])
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["erro"])

    # ── M1/M2 (revisao de seguranca 22/11): checklist de carregamento ─────────

    def test_padrao_nao_passa_interativo_ao_client(self):
        r = self._com(_Rotas(_corpo([_servico(1), _servico(2)])))
        edicao_rota.tirar_pedido("t", 7, 1)
        self.assertEqual(r.kwargs, [("buscar", {}), ("remover", {})])

    def test_interativo_chega_ao_client(self):
        r = self._com(_Rotas(_corpo([_servico(1)])))
        res = edicao_rota.tirar_pedido("t", 7, 1, interativo=True)
        self.assertTrue(res["rota_cancelada"])
        self.assertEqual(r.kwargs, [("buscar", {"interativo": True}), ("cancelar", {"interativo": True})])

    def test_interativo_429_na_leitura_vira_mensagem_curta(self):
        erro = requests.exceptions.HTTPError("429 Too Many Requests", response=mock.Mock(status_code=429))
        r = self._com(_Rotas(erro_busca=erro))
        res = edicao_rota.tirar_pedido("t", 7, 1, interativo=True)
        self.assertEqual(res, {"ok": False, "erro": edicao_rota.MSG_VUUPT_OCUPADA})
        self.assertEqual(r.deletes, [])

    def test_interativo_timeout_e_429_no_delete_viram_mensagem_curta(self):
        for erro, msg in ((requests.exceptions.ReadTimeout("lento"), edicao_rota.MSG_VUUPT_SEM_RESPOSTA),
                          (requests.exceptions.HTTPError("429", response=mock.Mock(status_code=429)),
                           edicao_rota.MSG_VUUPT_OCUPADA)):
            self._com(_Rotas(_corpo([_servico(1), _servico(2)]), erro_escrita=erro))
            self.assertEqual(edicao_rota.tirar_pedido("t", 7, 1, interativo=True), {"ok": False, "erro": msg})

    def test_padrao_429_mantem_mensagem_antiga(self):
        erro = requests.exceptions.HTTPError("429 Too Many Requests", response=mock.Mock(status_code=429))
        self._com(_Rotas(erro_busca=erro))
        res = edicao_rota.tirar_pedido("t", 7, 1)
        self.assertEqual(res["erro"], "Falha ao consultar a rota #7 na Vuupt: 429 Too Many Requests")

    def test_rota_de_outro_motorista_na_vuupt_nao_mexe_em_nada(self):
        for servicos in ([_servico(1), _servico(2)], [_servico(1)]):      # DELETE e cancel
            r = self._com(_Rotas(_corpo(servicos, agent_id=10)))
            res = edicao_rota.tirar_pedido("t", 7, 1, agent_id_esperado=99)
            self.assertEqual(res, {"ok": False, "rota_de_outro_motorista": True,
                                   "erro": "Esta rota não é mais sua. Fale com a logística."})
            self.assertEqual((r.deletes, r.cancelamentos), ([], []))

    def test_rota_sem_motorista_na_vuupt_conta_como_de_outro(self):
        r = self._com(_Rotas(_corpo([_servico(1), _servico(2)], agent_id=None)))
        res = edicao_rota.tirar_pedido("t", 7, 1, agent_id_esperado=10)
        self.assertTrue(res["rota_de_outro_motorista"])
        self.assertEqual(r.deletes, [])

    def test_mesmo_motorista_segue(self):
        r = self._com(_Rotas(_corpo([_servico(1), _servico(2)], agent_id=10)))
        self.assertTrue(edicao_rota.tirar_pedido("t", 7, 1, agent_id_esperado=10)["ok"])
        self.assertEqual(r.deletes, [(7, 1)])

    def test_rota_iniciada(self):
        self.assertFalse(edicao_rota.rota_iniciada({"status": "not_started", "servicos": [{"status": "assigned"}]}))
        self.assertTrue(edicao_rota.rota_iniciada({"status": "started", "servicos": []}))
        self.assertTrue(edicao_rota.rota_iniciada({"status": "finished", "servicos": []}))
        for st in ("on_route", "arrived", "done"):
            self.assertTrue(edicao_rota.rota_iniciada({"status": "x", "servicos": [{"status": st}]}), st)


if __name__ == "__main__":
    unittest.main()
