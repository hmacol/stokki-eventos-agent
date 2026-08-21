# -*- coding: utf-8 -*-
"""
test_editar_endereco_pedido.py

Testes automatizados de planejamento_rotas.editar_endereco_pedido --
opção "Editar endereço" do menu de contexto da tela de planejamento
(investigação do Hugo, 20/08: como a edição de endereço em rascunhos é
reproduzida na VUUPT).

Cobre especificamente a correção de 20/08: gravar o endereço via PUT
/customers/{customer_id} (atualizar_customer) em vez de embutir
'customer' num PUT /services/{id} -- o padrão anterior, documentado em
vuupt_client.resolver_customer_id como não confiável pra contato já
existente (VUUPT ignora telefone e, em alguns casos, dados de contato
JÁ EXISTENTE quando embutido no serviço).

Nenhum teste bate na VUUPT real: VuuptClient, geocodificacao.geocodificar
e rascunhos_rota.atualizar_endereco_parada são todos mockados. Rodar
com (a partir da raiz do repo):
    python -m unittest painel_agentes.test_editar_endereco_pedido -v
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


class EditarEnderecoPedidoTestCase(unittest.TestCase):

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

        # instância que VuuptClient(token) devolve dentro da função
        self.mock_vuupt = self.mock_vuupt_cls.return_value
        self.mock_vuupt.buscar_servico_por_id.return_value = {"id": 555, "customer_id": 999}
        self.mock_geocodificar.return_value = (-23.55, -46.63)

    # -- validação de entrada, sem tocar em rede -----------------------

    def test_endereco_vazio_nao_chama_vuupt(self):
        resultado = planejamento_rotas.editar_endereco_pedido(555, "   ")

        self.assertEqual(resultado, {"ok": False, "erro": "Endereço não pode ficar em branco."})
        self.mock_vuupt_cls.assert_not_called()
        self.mock_geocodificar.assert_not_called()

    def test_endereco_none_nao_chama_vuupt(self):
        resultado = planejamento_rotas.editar_endereco_pedido(555, None)

        self.assertFalse(resultado["ok"])
        self.mock_vuupt_cls.assert_not_called()

    # -- pedido sem contato resolvível na VUUPT -------------------------

    def test_servico_nao_encontrado_retorna_erro_sem_gravar(self):
        self.mock_vuupt.buscar_servico_por_id.return_value = None

        resultado = planejamento_rotas.editar_endereco_pedido(555, "Rua Nova, 100")

        self.assertFalse(resultado["ok"])
        self.assertIn("555", resultado["erro"])
        self.mock_vuupt.atualizar_customer.assert_not_called()
        self.mock_geocodificar.assert_not_called()

    def test_servico_sem_customer_id_retorna_erro_sem_gravar(self):
        self.mock_vuupt.buscar_servico_por_id.return_value = {"id": 555}

        resultado = planejamento_rotas.editar_endereco_pedido(555, "Rua Nova, 100")

        self.assertFalse(resultado["ok"])
        self.mock_vuupt.atualizar_customer.assert_not_called()

    # -- fluxo feliz ------------------------------------------------------

    def test_grava_via_atualizar_customer_com_coords(self):
        resultado = planejamento_rotas.editar_endereco_pedido(555, "Rua Nova, 100")

        self.assertEqual(resultado, {"ok": True})
        self.mock_vuupt.buscar_servico_por_id.assert_called_once_with(555)
        self.mock_vuupt.atualizar_customer.assert_called_once_with(
            999, {"address": "Rua Nova, 100", "latitude": -23.55, "longitude": -46.63}
        )

    def test_endereco_e_stripado_antes_de_geocodificar_e_gravar(self):
        planejamento_rotas.editar_endereco_pedido(555, "  Rua Nova, 100  ")

        self.mock_geocodificar.assert_called_once_with("Rua Nova, 100", "key")
        self.mock_vuupt.atualizar_customer.assert_called_once_with(
            999, {"address": "Rua Nova, 100", "latitude": -23.55, "longitude": -46.63}
        )

    def test_geocodificacao_falha_envia_so_texto_do_endereco(self):
        self.mock_geocodificar.return_value = None

        resultado = planejamento_rotas.editar_endereco_pedido(555, "Endereço não resolvido, 1")

        self.assertEqual(resultado, {"ok": True})
        self.mock_vuupt.atualizar_customer.assert_called_once_with(
            999, {"address": "Endereço não resolvido, 1"}
        )

    # -- regressão: NÃO pode voltar a embutir 'customer' no PUT de serviço --

    def test_nao_usa_mais_atualizar_servico_para_editar_endereco(self):
        """Guarda contra reintroduzir o padrão antigo (PUT /services/{id}
        com 'customer' embutido) -- vuupt_client.resolver_customer_id
        documenta que a VUUPT não reflete isso de forma confiável quando
        o contato já existe, que é sempre o caso aqui."""
        planejamento_rotas.editar_endereco_pedido(555, "Rua Nova, 100")

        self.mock_vuupt.atualizar_servico.assert_not_called()

    # -- erro da VUUPT ao gravar o contato --------------------------------

    def test_erro_da_vuupt_ao_atualizar_customer_retorna_erro(self):
        self.mock_vuupt.atualizar_customer.side_effect = VuuptAPIError("Status 422: campo inválido")

        resultado = planejamento_rotas.editar_endereco_pedido(555, "Rua Nova, 100")

        self.assertEqual(resultado, {"ok": False, "erro": "Status 422: campo inválido"})
        self.mock_atualiza_parada.assert_not_called()

    # -- cópia local em rascunhos_parada -----------------------------------

    def test_atualiza_cache_do_rascunho_quando_rascunho_id_informado(self):
        resultado = planejamento_rotas.editar_endereco_pedido(555, "Rua Nova, 100", rascunho_id=42)

        self.assertEqual(resultado, {"ok": True})
        self.mock_atualiza_parada.assert_called_once_with(42, 555, "Rua Nova, 100", -23.55, -46.63)

    def test_atualiza_cache_do_rascunho_com_coords_none_se_geocodificacao_falhou(self):
        self.mock_geocodificar.return_value = None

        planejamento_rotas.editar_endereco_pedido(555, "Rua Nova, 100", rascunho_id=42)

        self.mock_atualiza_parada.assert_called_once_with(42, 555, "Rua Nova, 100", None, None)

    def test_nao_atualiza_cache_quando_rascunho_id_e_none(self):
        planejamento_rotas.editar_endereco_pedido(555, "Rua Nova, 100", rascunho_id=None)

        self.mock_atualiza_parada.assert_not_called()


if __name__ == "__main__":
    unittest.main()
