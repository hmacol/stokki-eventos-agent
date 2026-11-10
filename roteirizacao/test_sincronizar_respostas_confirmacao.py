# -*- coding: utf-8 -*-
"""Reconciliacao de escolhas revertidas do marketplace: rascunho ja ENVIADO nao e desalocado
(trocar_motorista em ENVIADO iria pra Vuupt sem token), os outros seguem.

    py -3.11 -m unittest roteirizacao.test_sincronizar_respostas_confirmacao
"""
import sys
import types
import unittest
from unittest import mock

from roteirizacao import sincronizar_respostas_confirmacao as srs


class ReconciliarRevertidasTestCase(unittest.TestCase):
    def test_enviado_nao_desaloca_e_os_outros_seguem(self):
        aplicadas = [{"rascunho_id": 1, "escolhido_por": 10}, {"rascunho_id": 2, "escolhido_por": 20}]
        rascunhos = {1: {"id": 1, "nome": "R1", "agent_id": 10, "status": "ENVIADO"},
                     2: {"id": 2, "nome": "R2", "agent_id": 20, "status": "OFERTADA"}}
        resp = mock.Mock()
        resp.json.return_value = {"ofertas": []}  # as duas foram canceladas na VPS
        push = mock.Mock()
        with mock.patch.object(srs.ofertas_rota, "listar_aplicadas", return_value=aplicadas), \
                mock.patch.object(srs.ofertas_rota, "marcar_revertida") as revertida, \
                mock.patch.object(srs.requests, "get", return_value=resp), \
                mock.patch.object(srs, "buscar_rascunho", side_effect=rascunhos.get), \
                mock.patch.object(srs, "trocar_motorista") as trocar, \
                mock.patch.dict(sys.modules, {"avisar_motoristas_rotas": types.SimpleNamespace(
                    push_ofertas_vps=push)}), \
                self.assertLogs("sincronizar_respostas_confirmacao", level="WARNING") as logs:
            srs._reconciliar_escolhas_revertidas("http://vps", "segredo")
        trocar.assert_called_once_with(2, None, None, None)
        self.assertEqual([c.args[0] for c in revertida.call_args_list], [1, 2])
        self.assertTrue(any("ENVIADO" in m and "NÃO foi desalocada" in m for m in logs.output))
        push.assert_called_once()


if __name__ == "__main__":
    unittest.main()
