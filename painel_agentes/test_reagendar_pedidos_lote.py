# -*- coding: utf-8 -*-
"""
test_reagendar_pedidos_lote.py

Testes automatizados de planejamento_rotas.reagendar_pedidos -- versão
em lote de reagendar_pedido, botão "Agendar" da barra de seleção
múltipla da tela de planejamento (Hugo, 20/08: "quando vários pedidos
estiverem selecionados, alterar o endereço ou agendamento de todos os
pedidos selecionados").

Cobre: a MESMA janela de data/horário aplicada a todos os itens, que a
falha de UM item não aborta os demais, e validação de data/hora
inválida ou lista vazia sem tentar nada. Usa a conversão de data REAL
(_converter_data_para_iso é uma função pura, sem rede/config) em vez de
mockada, pra também pegar regressão na integração entre as duas.

Nenhum teste bate na VUUPT real: só VuuptClient é mockado. Rodar com (a
partir da raiz do repo):
    python -m unittest painel_agentes.test_reagendar_pedidos_lote -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas
from vuupt_client import VuuptAPIError


class ReagendarPedidosLoteTestCase(unittest.TestCase):

    def setUp(self):
        patch_config = mock.patch.object(
            planejamento_rotas, "_carregar_config",
            return_value={"vuupt_api": {"token": "tok"}},
        )
        patch_vuupt_cls = mock.patch.object(planejamento_rotas, "VuuptClient")

        self.mock_carregar_config = patch_config.start()
        self.mock_vuupt_cls = patch_vuupt_cls.start()
        self.addCleanup(patch_config.stop)
        self.addCleanup(patch_vuupt_cls.stop)

        self.mock_vuupt = self.mock_vuupt_cls.return_value
        self.itens = [{"service_id": 111}, {"service_id": 222}, {"service_id": 333}]

    # -- validação de entrada, sem tocar em rede -------------------------

    def test_data_invalida_retorna_erro_sem_tentar(self):
        resultado = planejamento_rotas.reagendar_pedidos(self.itens, "não é data", "08:00", "18:00")

        self.assertFalse(resultado["ok"])
        self.assertIn("Data/horário inválidos", resultado["erro"])
        self.mock_vuupt_cls.assert_not_called()

    def test_sem_itens_retorna_erro_sem_tentar(self):
        resultado = planejamento_rotas.reagendar_pedidos([], "2026-08-25", "08:00", "18:00")

        self.assertEqual(resultado, {"ok": False, "erro": "Nenhum pedido selecionado."})
        self.mock_vuupt_cls.assert_not_called()

    # -- mesma janela aplicada a todos os itens ---------------------------

    def test_aplica_a_mesma_janela_a_todos_os_itens(self):
        resultado = planejamento_rotas.reagendar_pedidos(self.itens, "2026-08-25", "08:00", "18:00")

        self.assertTrue(resultado["ok"])
        esperado = {
            "scheduled_start": "2026-08-25T08:00:00-03:00",
            "scheduled_end": "2026-08-25T18:00:00-03:00",
        }
        self.mock_vuupt.atualizar_servico.assert_has_calls([
            mock.call(111, esperado), mock.call(222, esperado), mock.call(333, esperado),
        ], any_order=True)
        self.assertEqual(self.mock_vuupt.atualizar_servico.call_count, 3)

    # -- falha isolada não aborta os demais -------------------------------

    def test_falha_em_um_item_nao_aborta_os_demais(self):
        def falha_no_222(service_id, dados):
            if service_id == 222:
                raise VuuptAPIError("Status 422: horário inválido")

        self.mock_vuupt.atualizar_servico.side_effect = falha_no_222

        resultado = planejamento_rotas.reagendar_pedidos(self.itens, "2026-08-25", "08:00", "18:00")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["falhas"], [{"service_id": 222, "erro": "Status 422: horário inválido"}])
        self.assertEqual(self.mock_vuupt.atualizar_servico.call_count, 3)

    def test_falhas_vazia_quando_tudo_da_certo(self):
        resultado = planejamento_rotas.reagendar_pedidos(self.itens, "2026-08-25", "08:00", "18:00")

        self.assertEqual(resultado, {"ok": True, "falhas": []})

    def test_erro_de_rede_generico_em_um_item_tambem_nao_aborta_os_demais(self):
        """Regressão (achado 20/08, Hugo reportou lote "não altera
        todos"): o loop só protegia contra VuuptAPIError -- qualquer
        outro erro (timeout, conexão) no meio do lote derrubava a
        exceção pra fora e travava os itens seguintes. Erro aqui NÃO é
        VuuptAPIError de propósito."""
        def falha_no_222(service_id, dados):
            if service_id == 222:
                raise ConnectionError("conexão perdida")

        self.mock_vuupt.atualizar_servico.side_effect = falha_no_222

        resultado = planejamento_rotas.reagendar_pedidos(self.itens, "2026-08-25", "08:00", "18:00")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["falhas"], [{"service_id": 222, "erro": "conexão perdida"}])
        self.assertEqual(self.mock_vuupt.atualizar_servico.call_count, 3)


if __name__ == "__main__":
    unittest.main()
