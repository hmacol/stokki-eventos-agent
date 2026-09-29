"""Testes do transporte do WhatsApp das notificacoes (integracao_openwa.py).

Rodar: py -3.11 -m unittest test_integracao_openwa
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

import integracao_openwa as openwa

CFG = {"base_url": "http://127.0.0.1:2785/api/", "api_key": "chave", "sessao": "abc-123"}


def _resposta(status=200, corpo=None, json_invalido=False):
    resp = MagicMock()
    resp.status_code = status
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    if json_invalido:
        resp.json.side_effect = ValueError("nao e json")
    else:
        resp.json.return_value = corpo
    return resp


class TestConfigurado(unittest.TestCase):
    def test_completo(self):
        self.assertTrue(openwa.configurado(CFG))

    def test_incompleto_ou_vazio(self):
        self.assertFalse(openwa.configurado(None))
        self.assertFalse(openwa.configurado({}))
        self.assertFalse(openwa.configurado(dict(CFG, api_key="")))
        self.assertFalse(openwa.configurado({"base_url": "x", "api_key": "y"}))


class TestEnviarTexto(unittest.TestCase):
    def test_sucesso_monta_rota_cabecalho_e_corpo(self):
        with patch.object(openwa.requests, "post",
                          return_value=_resposta(corpo={"messageId": "m1", "timestamp": 1})) as post:
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (True, "m1"))
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:2785/api/sessions/abc-123/messages/send-text")
        self.assertEqual(kwargs["json"], {"chatId": "1@g.us", "text": "oi"})
        self.assertEqual(kwargs["headers"], {"X-API-Key": "chave"})
        self.assertEqual(kwargs["timeout"], 15)

    def test_sem_config_destino_ou_texto_nao_chama_a_rede(self):
        with patch.object(openwa.requests, "post") as post:
            self.assertEqual(openwa.enviar_texto({}, "1@g.us", "oi"), (False, None))
            self.assertEqual(openwa.enviar_texto(CFG, "", "oi"), (False, None))
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", ""), (False, None))
        post.assert_not_called()

    def test_erro_http_vira_falha(self):
        for status in (401, 404, 500):
            with patch.object(openwa.requests, "post", return_value=_resposta(status=status)):
                self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (False, None))

    def test_erro_de_rede_vira_falha(self):
        with patch.object(openwa.requests, "post", side_effect=requests.ConnectionError("fora do ar")):
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (False, None))

    def test_200_sem_json_conta_como_enviado(self):
        with patch.object(openwa.requests, "post", return_value=_resposta(json_invalido=True)):
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (True, None))

    def test_200_sem_message_id_ou_com_lista(self):
        with patch.object(openwa.requests, "post", return_value=_resposta(corpo={})):
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (True, None))
        with patch.object(openwa.requests, "post", return_value=_resposta(corpo=[1, 2])):
            self.assertEqual(openwa.enviar_texto(CFG, "1@g.us", "oi"), (True, None))

    def test_chave_nao_aparece_no_log(self):
        with patch.object(openwa.requests, "post", side_effect=requests.ConnectionError("x")), \
                self.assertLogs(openwa.logger, level="WARNING") as logs:
            openwa.enviar_texto(CFG, "1@g.us", "oi")
        self.assertNotIn("chave", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
