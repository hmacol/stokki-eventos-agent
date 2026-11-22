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


class _Resp429:
    ok = False
    status_code = 429
    reason = "Too Many Requests"
    url = "x"
    text = "{}"

    def json(self):
        return {"message": "Too Many Attempts."}


class TestInterativo(unittest.TestCase):
    """Checklist de carregamento (M1, revisao 22/11): 1 tentativa, timeout curto, sem dormir no 429."""

    def test_interativo_429_uma_tentativa_sem_sleep(self):
        casos = (("get", lambda: rotas_client.buscar_rota("tok", 7, include=["services"], interativo=True)),
                 ("delete", lambda: rotas_client.remover_servico_da_rota("tok", 7, 1, interativo=True)),
                 ("delete", lambda: rotas_client.cancelar_rota("tok", 7, "unassign", interativo=True)))
        for metodo, chamar in casos:
            falso = mock.Mock(return_value=_Resp429())
            with mock.patch.object(requests, metodo, falso), mock.patch("time.sleep") as dormir:
                with self.assertRaises(requests.exceptions.HTTPError) as cm:
                    chamar()
            dormir.assert_not_called()
            falso.assert_called_once()
            self.assertEqual(falso.call_args.kwargs["timeout"], rotas_client.TIMEOUT_INTERATIVO)
            self.assertEqual(cm.exception.response.status_code, 429)

    def test_padrao_continua_com_retry_e_timeout_30(self):
        for chamar in (lambda: rotas_client.buscar_rota("tok", 7, include=["services"]),
                       lambda: rotas_client.remover_servico_da_rota("tok", 7, 1),
                       lambda: rotas_client.cancelar_rota("tok", 7, "unassign")):
            duble = _Duble({"route": {}})
            with mock.patch.object(rotas_client, "chamar_com_retry", duble):
                chamar()
            self.assertEqual(len(duble.chamadas), 1)
            self.assertEqual(duble.chamadas[0][2]["timeout"], 30)
            self.assertNotIn("interativo", duble.chamadas[0][2])


if __name__ == "__main__":
    unittest.main()
