# -*- coding: utf-8 -*-
"""
Chamadas de edicao de rota do rotas_client (tirar servico, trocar motorista/carro).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_rotas_client_edicao -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import rotas_client


class _Resp:
    ok = True
    status_code = 200
    reason = "OK"
    url = "x"
    text = "{}"

    def __init__(self, corpo):
        self._corpo = corpo

    def json(self):
        return self._corpo


class _Duble:
    """Grava a chamada feita via chamar_com_retry."""

    def __init__(self, corpo):
        self.corpo = corpo
        self.chamadas = []

    def __call__(self, metodo, url, **kw):
        self.chamadas.append((metodo, url, kw))
        return _Resp(self.corpo)


class TestRotasClientEdicao(unittest.TestCase):
    def test_remover_servico_usa_delete_no_caminho_certo(self):
        duble = _Duble({"success": True})
        with mock.patch.object(rotas_client, "chamar_com_retry", duble):
            r = rotas_client.remover_servico_da_rota("tok", 5449893, 54894926)
        metodo, url, _ = duble.chamadas[0]
        self.assertIs(metodo, requests.delete)
        self.assertTrue(url.endswith("/routes/5449893/services/54894926"))
        self.assertEqual(r, {"success": True})

    def test_atualizar_motorista_veiculo_nao_manda_activities(self):
        duble = _Duble({"route": {"id": 5}})
        with mock.patch.object(rotas_client, "chamar_com_retry", duble):
            r = rotas_client.atualizar_motorista_veiculo("tok", 5, 28438, 74274)
        metodo, url, kw = duble.chamadas[0]
        self.assertIs(metodo, requests.put)
        self.assertTrue(url.endswith("/routes/5"))
        self.assertEqual(kw["json"], {"agent_id": 28438, "vehicle_id": 74274})
        self.assertNotIn("activities", kw["json"])
        self.assertEqual(r, {"id": 5})

    def test_atualizar_motorista_veiculo_aceita_none(self):
        duble = _Duble({"route": {}})
        with mock.patch.object(rotas_client, "chamar_com_retry", duble):
            rotas_client.atualizar_motorista_veiculo("tok", 5, None, 74274)
        json_enviado = duble.chamadas[0][2]["json"]
        self.assertIn("agent_id", json_enviado)
        self.assertIsNone(json_enviado["agent_id"])


if __name__ == "__main__":
    unittest.main()
