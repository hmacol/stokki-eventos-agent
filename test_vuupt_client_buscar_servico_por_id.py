# -*- coding: utf-8 -*-
"""
test_vuupt_client_buscar_servico_por_id.py

Testes automatizados do VuuptClient.buscar_servico_por_id (GET
/services/{id}), adicionado para permitir editar_endereco_pedido buscar
o customer_id do pedido antes de gravar o endereço direto no contato
(ver painel_agentes/test_editar_endereco_pedido.py -- investigação do
Hugo, 20/08, sobre como a edição de endereço em rascunhos é reproduzida
na VUUPT).

Não bate na API real: substitui client.session por um MagicMock e
injeta respostas fake. Rodar com: python -m unittest
test_vuupt_client_buscar_servico_por_id -v
"""
import unittest
from unittest.mock import MagicMock

from vuupt_client import VuuptClient, BASE_URL


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _client_com_session_fake():
    client = VuuptClient(token="token-fake-teste")
    client.session = MagicMock()
    return client


class BuscarServicoPorIdTestCase(unittest.TestCase):

    def test_resposta_direta_sem_wrapper(self):
        """API pode devolver o objeto do serviço direto, sem chave 'service'."""
        client = _client_com_session_fake()
        client.session.get.return_value = FakeResponse(
            200, {"id": 123, "code": "PS-1", "customer_id": 456}
        )

        resultado = client.buscar_servico_por_id(123)

        self.assertEqual(resultado, {"id": 123, "code": "PS-1", "customer_id": 456})

    def test_resposta_embrulhada_em_service(self):
        """API pode devolver {"service": {...}} -- mesmo padrão defensivo já
        usado em resolver_customer_id/criar_ou_atualizar_servico
        (resultado.get('service', resultado))."""
        client = _client_com_session_fake()
        client.session.get.return_value = FakeResponse(
            200, {"service": {"id": 123, "customer_id": 456}}
        )

        resultado = client.buscar_servico_por_id(123)

        self.assertEqual(resultado, {"id": 123, "customer_id": 456})

    def test_404_retorna_none(self):
        client = _client_com_session_fake()
        client.session.get.return_value = FakeResponse(404, {"message": "not found"})

        resultado = client.buscar_servico_por_id(999)

        self.assertIsNone(resultado)

    def test_erro_http_generico_retorna_none(self):
        """5xx (ou qualquer outro erro) não deve propagar exceção -- vira
        None, igual buscar_customer_por_id."""
        client = _client_com_session_fake()
        client.session.get.return_value = FakeResponse(500, {"message": "boom"})

        resultado = client.buscar_servico_por_id(123)

        self.assertIsNone(resultado)

    def test_excecao_de_rede_retorna_none(self):
        client = _client_com_session_fake()
        client.session.get.side_effect = ConnectionError("timeout")

        resultado = client.buscar_servico_por_id(123)

        self.assertIsNone(resultado)

    def test_chama_url_e_timeout_corretos(self):
        client = _client_com_session_fake()
        client.session.get.return_value = FakeResponse(200, {"id": 777})

        client.buscar_servico_por_id(777)

        client.session.get.assert_called_once_with(
            f"{BASE_URL}/services/777", timeout=15
        )


if __name__ == "__main__":
    unittest.main()
