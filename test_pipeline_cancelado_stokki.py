# -*- coding: utf-8 -*-
"""
Pedido cancelado na Stokki nao sobe pra Vuupt (Hugo, 18/11, PS-44507: cancelado
as 12:15, a rodada tinha listado antes e criou o servico as 12:17; foi pra rota
e o motorista deu "nao coletado"). Rodar (da raiz):
py -3.11 -m unittest test_pipeline_cancelado_stokki -v
"""
import unittest
from unittest import mock

import pipeline
from stokki import pedidos as stokki_pedidos


class CanceladoNaStokkiNaoSobeTest(unittest.TestCase):
    def setUp(self):
        self.detalhe = {"transportadora": {}, "situacao": "Cancelado"}
        self.resolver = mock.Mock()
        patches = [
            mock.patch.object(pipeline, "ja_confirmado_atribuido", return_value=False),
            mock.patch.object(pipeline.stokki_pedidos, "obter_detalhe", side_effect=lambda *a: self.detalhe),
            mock.patch.object(pipeline, "resolver_endereco_entrega", self.resolver),
            mock.patch.object(pipeline, "config_consulta", return_value={"ativo": True}),
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
            44507, "PS-44507", sess_stokki=mock.Mock(), catalogo=mock.Mock(), vuupt=vuupt,
            anthropic_api_key="", modo_teste=modo_teste)
        self.assertIsNone(res["erro"])
        self.assertEqual(res["acao"], "ignorado_cancelado_stokki")
        return res

    def _nada_criado(self, v):
        v.criar_servico.assert_not_called()
        v.criar_ou_atualizar_servico.assert_not_called()
        v.atualizar_servico.assert_not_called()
        v.atribuir_agente.assert_not_called()

    def test_sem_servico_nao_cria_nem_resolve_endereco(self):
        v = self._vuupt()
        self._processar(v)
        self._nada_criado(v)
        self.resolver.assert_not_called()   # nem geocodifica, nem consulta transportadora
        v.cancelar_servico_oficial.assert_not_called()

    def test_servico_not_assigned_e_cancelado_na_vuupt(self):
        v = self._vuupt({"id": 55112055, "status": "not_assigned"})
        self._processar(v)
        self._nada_criado(v)
        v.cancelar_servico_oficial.assert_called_once_with(55112055)

    def test_modo_teste_nao_cancela(self):
        v = self._vuupt({"id": 9, "status": "not_assigned"})
        self._processar(v, modo_teste=True)
        v.cancelar_servico_oficial.assert_not_called()

    def test_servico_ja_cancelado_na_vuupt_nao_vira_pendencia(self):
        v = self._vuupt({"id": 9, "status": "canceled"})
        res = self._processar(v)
        self.assertFalse(res["requer_revisao"])
        v.cancelar_servico_oficial.assert_not_called()

    def test_falha_no_cancelamento_pede_revisao(self):
        v = self._vuupt({"id": 9, "status": "not_assigned"})
        v.cancelar_servico_oficial.side_effect = RuntimeError("500")
        res = self._processar(v)
        self.assertTrue(res["requer_revisao"])

    def test_pedido_aberto_segue_o_fluxo(self):
        self.detalhe = {"transportadora": {}, "situacao": "Aguardando Transportador"}
        self.resolver.side_effect = RuntimeError("seguiu o fluxo")
        res = pipeline.processar_pedido(
            1, "PS-1", sess_stokki=mock.Mock(), catalogo=mock.Mock(), vuupt=self._vuupt(),
            anthropic_api_key="", modo_teste=False)
        self.assertNotEqual(res["acao"], "ignorado_cancelado_stokki")
        self.resolver.assert_called_once()


class SituacaoNoDetalheTest(unittest.TestCase):
    def test_detalhe_le_a_situacao(self):
        html = ('<table><tr><th>Situação:</th>\n<td> <span class="badge">Cancelado</span> </td></tr>'
                '<tr><th>Ref. do Pedido:</th><td>7878</td></tr></table>')
        self.assertEqual(stokki_pedidos._parsear_pagina_detalhe(html, 44507)["situacao"], "Cancelado")

    def test_detalhe_sem_situacao_fica_vazio(self):
        self.assertEqual(stokki_pedidos._parsear_pagina_detalhe("<html></html>", 1)["situacao"], "")


if __name__ == "__main__":
    unittest.main()
