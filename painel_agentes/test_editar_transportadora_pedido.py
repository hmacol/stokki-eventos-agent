# -*- coding: utf-8 -*-
"""
test_editar_transportadora_pedido.py

Testes de planejamento_rotas.editar_transportadora_pedidos -- opcao
"Transportadora (redespacho)" do menu de contexto e das barras de selecao
da tela de planejamento (Hugo, 16/09): escolher uma transportadora
TERCEIROS da BD_TRANSPORTADORAS troca o endereco do pedido pelo endereco
do galpao de redespacho dela, reaproveitando o mesmo caminho do "Editar
endereco" (editar_endereco_pedidos: VUUPT + contato + copia do rascunho).

Nenhum teste bate na VUUPT nem abre a planilha real: VuuptClient,
geocodificacao.geocodificar, rascunhos_rota.atualizar_endereco_parada e
o carregador do catalogo sao mockados. Rodar (da raiz do repo):
    py -3.11 -m unittest painel_agentes.test_editar_transportadora_pedido -v
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
from regras.transportadoras import (
    CatalogoTransportadoras, EnderecoRedespacho, _Entrada, _normalizar,
)


def _entrada(nome, tipo="TERCEIROS", endereco=None):
    return _Entrada(nome_original=nome, nome_normalizado=_normalizar(nome), cnpj="",
                    tipo=tipo, endereco=endereco)


ENDERECO_KANEJO = EnderecoRedespacho(
    uf="SP", municipio="São Paulo", bairro="Jardim Japão", logradouro="Rua Osaka",
    numero="880", complemento="KANEJO", cep="02124-040",
)
TEXTO_KANEJO = "Rua Osaka, 880, KANEJO, Jardim Japão, São Paulo - SP, 02124-040"


class EditarTransportadoraPedidosTestCase(unittest.TestCase):

    def setUp(self):
        catalogo = CatalogoTransportadoras([
            _entrada("KANEJO", endereco=ENDERECO_KANEJO),
            _entrada("CLIENTE RETIRA", tipo="RETIRADA"),
        ])
        patch_catalogo = mock.patch.object(
            planejamento_rotas, "_catalogo_transportadoras", return_value=catalogo,
        )
        patch_config = mock.patch.object(
            planejamento_rotas, "_carregar_config",
            return_value={"vuupt_api": {"token": "tok"}, "google_maps": {"api_key": "key"}},
        )
        patch_vuupt_cls = mock.patch.object(planejamento_rotas, "VuuptClient")
        patch_geocodificar = mock.patch("geocodificacao.geocodificar")
        patch_atualiza_parada = mock.patch.object(rascunhos_rota, "atualizar_endereco_parada")

        self.mock_catalogo = patch_catalogo.start()
        self.mock_vuupt_cls = patch_vuupt_cls.start()
        self.mock_geocodificar = patch_geocodificar.start()
        self.mock_atualiza_parada = patch_atualiza_parada.start()
        patch_config.start()
        for p in (patch_catalogo, patch_config, patch_vuupt_cls, patch_geocodificar, patch_atualiza_parada):
            self.addCleanup(p.stop)

        self.mock_vuupt = self.mock_vuupt_cls.return_value
        self.mock_vuupt.buscar_servico_por_id.return_value = {"id": 555, "customer_id": 999}
        self.mock_geocodificar.return_value = (-23.51, -46.58)

        self.itens = [{"service_id": 555, "rascunho_id": 42}]

    # -- validacao, sem tocar em rede --------------------------------------

    def test_transportadora_vazia_nao_chama_vuupt(self):
        resultado = planejamento_rotas.editar_transportadora_pedidos(self.itens, "  ")

        self.assertEqual(resultado, {"ok": False, "erro": "Escolha a transportadora."})
        self.mock_vuupt_cls.assert_not_called()

    def test_sem_itens_nao_chama_vuupt(self):
        resultado = planejamento_rotas.editar_transportadora_pedidos([], "KANEJO")

        self.assertEqual(resultado, {"ok": False, "erro": "Nenhum pedido selecionado."})
        self.mock_vuupt_cls.assert_not_called()

    def test_transportadora_que_nao_e_terceiros_e_recusada(self):
        resultado = planejamento_rotas.editar_transportadora_pedidos(self.itens, "CLIENTE RETIRA")

        self.assertFalse(resultado["ok"])
        self.assertIn("CLIENTE RETIRA", resultado["erro"])
        self.assertIn("TERCEIROS", resultado["erro"])
        self.mock_vuupt_cls.assert_not_called()

    def test_transportadora_desconhecida_e_recusada(self):
        resultado = planejamento_rotas.editar_transportadora_pedidos(self.itens, "NAO EXISTE")

        self.assertFalse(resultado["ok"])
        self.mock_vuupt_cls.assert_not_called()

    def test_planilha_indisponivel_vira_erro_reportavel(self):
        self.mock_catalogo.return_value = None

        resultado = planejamento_rotas.editar_transportadora_pedidos(self.itens, "KANEJO")

        self.assertFalse(resultado["ok"])
        self.assertIn("BD_TRANSPORTADORAS", resultado["erro"])
        self.mock_vuupt_cls.assert_not_called()

    # -- efeito principal: endereco do galpao gravado como no "Editar endereco" --

    def test_grava_o_endereco_do_galpao_no_servico(self):
        resultado = planejamento_rotas.editar_transportadora_pedidos(self.itens, "KANEJO")

        self.assertEqual(resultado["ok"], True)
        self.assertEqual(resultado["falhas"], [])
        self.assertEqual(resultado["endereco"], TEXTO_KANEJO)
        self.mock_geocodificar.assert_called_once_with(TEXTO_KANEJO, "key")
        self.mock_vuupt.atualizar_servico.assert_called_once_with(
            555, {"address": TEXTO_KANEJO, "latitude": -23.51, "longitude": -46.58}
        )
        self.mock_atualiza_parada.assert_called_once_with(42, 555, TEXTO_KANEJO, -23.51, -46.58)

    def test_nome_tolera_caixa_diferente(self):
        resultado = planejamento_rotas.editar_transportadora_pedidos(self.itens, "kanejo")

        self.assertEqual(resultado["ok"], True)
        self.mock_vuupt.atualizar_servico.assert_called_once()

    def test_falha_num_pedido_nao_aborta_os_demais(self):
        itens = [{"service_id": 111, "rascunho_id": None}, {"service_id": 222, "rascunho_id": None}]
        self.mock_vuupt.atualizar_servico.side_effect = [ConnectionError("timeout"), None]

        resultado = planejamento_rotas.editar_transportadora_pedidos(itens, "KANEJO")

        self.assertEqual(resultado["ok"], True)
        self.assertEqual(resultado["falhas"], [{"service_id": 111, "erro": "timeout"}])
        self.assertEqual(self.mock_vuupt.atualizar_servico.call_count, 2)


class ListarTransportadorasTerceirosTestCase(unittest.TestCase):

    def test_devolve_lista_do_catalogo(self):
        catalogo = CatalogoTransportadoras([_entrada("KANEJO", endereco=ENDERECO_KANEJO)])
        with mock.patch.object(planejamento_rotas, "_catalogo_transportadoras", return_value=catalogo):
            lista = planejamento_rotas.listar_transportadoras_terceiros()

        self.assertEqual(lista, [{"nome": "KANEJO", "endereco": TEXTO_KANEJO}])

    def test_planilha_indisponivel_devolve_lista_vazia(self):
        with mock.patch.object(planejamento_rotas, "_catalogo_transportadoras", return_value=None):
            self.assertEqual(planejamento_rotas.listar_transportadoras_terceiros(), [])


if __name__ == "__main__":
    unittest.main()
