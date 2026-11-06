# -*- coding: utf-8 -*-
"""
Retirada no galpao nunca sobe pra Vuupt (Hugo, 06/11). Rodar (da raiz):
py -3.11 -m unittest test_pipeline_retirada -v
"""
import unittest
from unittest import mock

import pipeline


def _res_end():
    return mock.Mock(endereco={}, horario_entrega=None, fonte="destino", requer_revisao=False,
                     observacao="", transportadora={"tipo": "RETIRADA", "nome": "CLIENTE RETIRA"})


class RetiradaNaoSobeTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(pipeline, "ja_confirmado_atribuido", return_value=False),
            mock.patch.object(pipeline.stokki_pedidos, "obter_detalhe", return_value={"transportadora": {}}),
            mock.patch.object(pipeline, "resolver_endereco_entrega", return_value=_res_end()),
            mock.patch.object(pipeline, "config_consulta", return_value={"ativo": False}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _vuupt(self, existente=None):
        v = mock.Mock()
        v.STATUS_ATUALIZAVEL = "not_assigned"
        v.buscar_servico_por_code.return_value = existente
        return v

    def _processar(self, vuupt, modo_teste=False):
        res = pipeline.processar_pedido(
            41679, "PS-1", sess_stokki=mock.Mock(), catalogo=mock.Mock(), vuupt=vuupt,
            anthropic_api_key="", modo_teste=modo_teste)
        self.assertIsNone(res["erro"])
        self.assertEqual(res["acao"], "ignorado_retirada")
        return res

    def _nada_criado(self, v):
        v.criar_servico.assert_not_called()
        v.criar_ou_atualizar_servico.assert_not_called()
        v.atualizar_servico.assert_not_called()
        v.atribuir_agente.assert_not_called()

    def test_sem_servico_nao_cria_nada(self):
        v = self._vuupt()
        self._processar(v)
        self._nada_criado(v)
        v.cancelar_servico_oficial.assert_not_called()

    def test_entrega_not_assigned_que_virou_retirada_e_cancelada(self):
        v = self._vuupt({"id": 9, "status": "not_assigned"})
        self._processar(v)
        self._nada_criado(v)
        v.cancelar_servico_oficial.assert_called_once_with(9)

    def test_modo_teste_nao_cancela(self):
        v = self._vuupt({"id": 9, "status": "not_assigned"})
        self._processar(v, modo_teste=True)
        v.cancelar_servico_oficial.assert_not_called()

    def test_servico_cancelado_nao_vira_pendencia(self):
        v = self._vuupt({"id": 9, "status": "canceled"})
        res = self._processar(v)
        self.assertFalse(res["requer_revisao"])
        self._nada_criado(v)
        v.cancelar_servico_oficial.assert_not_called()

    def test_falha_no_cancelamento_nao_derruba_e_pede_revisao(self):
        v = self._vuupt({"id": 9, "status": "not_assigned"})
        v.cancelar_servico_oficial.side_effect = RuntimeError("500")
        res = self._processar(v)
        self.assertTrue(res["requer_revisao"])


if __name__ == "__main__":
    unittest.main()
