# -*- coding: utf-8 -*-
"""
test_editar_endereco_pedidos_lote.py

Testes automatizados de planejamento_rotas.editar_endereco_pedidos --
versão em lote de editar_endereco_pedido, botão "Editar endereço" da
barra de seleção múltipla da tela de planejamento (Hugo, 20/08: "quando
vários pedidos estiverem selecionados, alterar o endereço ou
agendamento de todos os pedidos selecionados").

Cobre: geocodificação feita UMA ÚNICA VEZ pra todos os itens (mesmo
endereço), gravação por item via o mesmo helper _gravar_endereco_pedido
de editar_endereco_pedido (serviço no nível raiz + sincronização
best-effort do contato + cópia local do rascunho quando aplicável), e
que a falha de UM item não aborta os demais.

Nenhum teste bate na VUUPT real: VuuptClient, geocodificacao.geocodificar
e rascunhos_rota.atualizar_endereco_parada são todos mockados. Rodar
com (a partir da raiz do repo):
    python -m unittest painel_agentes.test_editar_endereco_pedidos_lote -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas
import rascunhos_rota
from vuupt_client import VuuptAPIError


class EditarEnderecoPedidosLoteTestCase(unittest.TestCase):

    def setUp(self):
        patch_config = mock.patch.object(
            planejamento_rotas, "_carregar_config",
            return_value={"vuupt_api": {"token": "tok"}, "google_maps": {"api_key": "key"}},
        )
        patch_vuupt_cls = mock.patch.object(planejamento_rotas, "VuuptClient")
        patch_geocodificar = mock.patch("geocodificacao.geocodificar")
        patch_atualiza_parada = mock.patch.object(rascunhos_rota, "atualizar_endereco_parada")

        self.mock_carregar_config = patch_config.start()
        self.mock_vuupt_cls = patch_vuupt_cls.start()
        self.mock_geocodificar = patch_geocodificar.start()
        self.mock_atualiza_parada = patch_atualiza_parada.start()
        self.addCleanup(patch_config.stop)
        self.addCleanup(patch_vuupt_cls.stop)
        self.addCleanup(patch_geocodificar.stop)
        self.addCleanup(patch_atualiza_parada.stop)

        self.mock_vuupt = self.mock_vuupt_cls.return_value
        # customer_id derivado do service_id só pra distinguir nos
        # asserts qual chamada foi de qual pedido
        self.mock_vuupt.buscar_servico_por_id.side_effect = (
            lambda service_id: {"id": service_id, "customer_id": service_id * 10}
        )
        self.mock_geocodificar.return_value = (-23.55, -46.63)

        self.itens = [
            {"service_id": 111, "rascunho_id": 42},
            {"service_id": 222, "rascunho_id": None},
            {"service_id": 333, "rascunho_id": 42},
        ]

    # -- validação de entrada, sem tocar em rede -------------------------

    def test_endereco_vazio_nao_tenta_nada(self):
        resultado = planejamento_rotas.editar_endereco_pedidos(self.itens, "   ")

        self.assertEqual(resultado, {"ok": False, "erro": "Endereço não pode ficar em branco."})
        self.mock_vuupt_cls.assert_not_called()

    def test_sem_itens_retorna_erro_sem_tentar(self):
        resultado = planejamento_rotas.editar_endereco_pedidos([], "Rua Nova, 100")

        self.assertEqual(resultado, {"ok": False, "erro": "Nenhum pedido selecionado."})
        self.mock_vuupt_cls.assert_not_called()

    # -- geocodificação única, aplicada a todos --------------------------

    def test_geocodifica_uma_unica_vez_pra_varios_itens(self):
        planejamento_rotas.editar_endereco_pedidos(self.itens, "Rua Nova, 100")

        self.mock_geocodificar.assert_called_once_with("Rua Nova, 100", "key")

    def test_grava_o_mesmo_endereco_em_todos_os_servicos(self):
        planejamento_rotas.editar_endereco_pedidos(self.itens, "Rua Nova, 100")

        esperado = {"address": "Rua Nova, 100", "latitude": -23.55, "longitude": -46.63}
        self.mock_vuupt.atualizar_servico.assert_has_calls([
            mock.call(111, esperado), mock.call(222, esperado), mock.call(333, esperado),
        ], any_order=True)
        self.assertEqual(self.mock_vuupt.atualizar_servico.call_count, 3)

    def test_sincroniza_o_contato_certo_de_cada_item(self):
        planejamento_rotas.editar_endereco_pedidos(self.itens, "Rua Nova, 100")

        esperado = {"address": "Rua Nova, 100", "latitude": -23.55, "longitude": -46.63}
        self.mock_vuupt.atualizar_customer.assert_has_calls([
            mock.call(1110, esperado), mock.call(2220, esperado), mock.call(3330, esperado),
        ], any_order=True)

    # -- cópia local do rascunho só pros itens que têm rascunho_id -------

    def test_atualiza_cache_so_dos_itens_com_rascunho_id(self):
        planejamento_rotas.editar_endereco_pedidos(self.itens, "Rua Nova, 100")

        self.assertEqual(self.mock_atualiza_parada.call_count, 2)
        self.mock_atualiza_parada.assert_has_calls([
            mock.call(42, 111, "Rua Nova, 100", -23.55, -46.63),
            mock.call(42, 333, "Rua Nova, 100", -23.55, -46.63),
        ], any_order=True)

    # -- falha isolada não aborta os demais -------------------------------

    def test_falha_em_um_item_nao_aborta_os_demais(self):
        def falha_no_222(service_id, dados):
            if service_id == 222:
                raise VuuptAPIError("Status 500: instável")

        self.mock_vuupt.atualizar_servico.side_effect = falha_no_222

        resultado = planejamento_rotas.editar_endereco_pedidos(self.itens, "Rua Nova, 100")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["falhas"], [{"service_id": 222, "erro": "Status 500: instável"}])
        # os outros 2 ainda foram tentados apesar da falha do do meio
        self.assertEqual(self.mock_vuupt.atualizar_servico.call_count, 3)

    def test_falhas_vazia_quando_tudo_da_certo(self):
        resultado = planejamento_rotas.editar_endereco_pedidos(self.itens, "Rua Nova, 100")

        self.assertEqual(resultado, {"ok": True, "falhas": []})

    def test_erro_de_rede_generico_em_um_item_tambem_nao_aborta_os_demais(self):
        """Regressão (achado 20/08, Hugo reportou que o lote "não altera
        todos"): o loop só protegia contra VuuptAPIError -- qualquer
        outro erro (timeout, conexão) no meio do lote derrubava a
        exceção pra fora do loop e travava os itens seguintes sem
        sequer tentar. Aqui o erro NÃO é VuuptAPIError de propósito."""
        def falha_no_222(service_id, dados):
            if service_id == 222:
                raise ConnectionError("conexão perdida")

        self.mock_vuupt.atualizar_servico.side_effect = falha_no_222

        resultado = planejamento_rotas.editar_endereco_pedidos(self.itens, "Rua Nova, 100")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["falhas"], [{"service_id": 222, "erro": "conexão perdida"}])
        self.assertEqual(self.mock_vuupt.atualizar_servico.call_count, 3)

    def test_erro_de_rede_generico_ao_sincronizar_contato_nao_aborta_o_lote(self):
        """Mesma regressão, mas na sincronização best-effort do contato
        (dentro de _gravar_endereco_pedido) -- também tem que engolir
        qualquer exceção, não só VuuptAPIError, senão um erro aqui
        derruba o item inteiro (e o resto do lote) por causa só do
        passo secundário."""
        def falha_no_customer_2220(customer_id, dados):
            if customer_id == 2220:
                raise TimeoutError("deu ruim")

        self.mock_vuupt.atualizar_customer.side_effect = falha_no_customer_2220

        resultado = planejamento_rotas.editar_endereco_pedidos(self.itens, "Rua Nova, 100")

        self.assertEqual(resultado, {"ok": True, "falhas": []})
        self.assertEqual(self.mock_vuupt.atualizar_servico.call_count, 3)


if __name__ == "__main__":
    unittest.main()
