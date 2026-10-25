# -*- coding: utf-8 -*-
"""
test_freshhub_client.py -- cliente da API do sistema novo (plano 1.2 do freshhub, Task 5).
Sem rede: requests mockado.

    py -3.11 -m unittest nucleo.test_freshhub_client -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import freshhub_client as fc  # noqa: E402


def _resp(status=200, corpo=None, texto=""):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = corpo
    r.text = texto
    return r


class TestCliente(unittest.TestCase):
    def setUp(self):
        self.c = fc.FreshhubClient("http://127.0.0.1:8100/", "fhs_teste")
        self.get = mock.patch.object(self.c.sessao, "get").start()
        self.post = mock.patch.object(self.c.sessao, "post").start()
        mock.patch("http_retry.time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_sessao_leva_bearer_e_nao_mostra_o_token(self):
        self.assertEqual(self.c.sessao.headers["Authorization"], "Bearer fhs_teste")
        self.assertEqual(self.c.sessao.headers["Accept"], "application/json")
        self.assertEqual(self.c.url, "http://127.0.0.1:8100")
        self.assertNotIn("fhs_teste", repr(self.c))

    def test_listar_pool_devolve_os_itens_e_obter_pool_o_corpo(self):
        corpo = {"itens": [{"id": 1, "code": "#PS-1"}], "dados_de": "2026-10-24T20:10:00Z", "gerado_em": "x"}
        self.get.return_value = _resp(200, corpo)
        self.assertEqual(self.c.listar_pool(), [{"id": 1, "code": "#PS-1"}])
        self.get.assert_called_with("http://127.0.0.1:8100/api/pool", timeout=15)
        self.assertEqual(self.c.obter_pool(), corpo)

    def test_pendentes_resultado_e_eventos(self):
        self.get.return_value = _resp(200, [{"id": "abc", "codigo": "PS-1", "destino": "vuupt", "dados": {}}])
        self.assertEqual(self.c.pendentes("vuupt", limite=10)[0]["codigo"], "PS-1")
        self.get.assert_called_with("http://127.0.0.1:8100/api/replicacao/pendentes",
                                    params={"destino": "vuupt", "limite": 10}, timeout=15)
        self.post.return_value = _resp(204)
        self.assertIsNone(self.c.resultado("abc", True, resultado={"vuupt_service_id": 7}))
        self.post.assert_called_with("http://127.0.0.1:8100/api/replicacao/abc/resultado",
                                     json={"ok": True, "resultado": {"vuupt_service_id": 7}, "erro": None},
                                     timeout=15)
        self.c.resultado("abc", False, erro="x" * 3000)
        self.assertEqual(len(self.post.call_args.kwargs["json"]["erro"]), 2000)  # a API aceita ate 2000
        self.post.return_value = _resp(200, {"aceitos": 1, "repetidos": 0, "desconhecidos": []})
        lista = [{"codigo": "PS-1", "tipo": "roteirizado", "ocorrido_em": "2026-10-24T18:00:00-03:00", "dados": {}}]
        self.assertEqual(self.c.eventos(lista)["aceitos"], 1)
        self.post.assert_called_with("http://127.0.0.1:8100/api/eventos", json={"eventos": lista}, timeout=15)

    def test_erro_http_e_falha_de_rede_viram_erro_freshhub(self):
        self.get.return_value = _resp(401, {"erro": "nao_autenticado"}, texto='{"erro": "nao_autenticado"}')
        with self.assertRaises(fc.ErroFreshhub) as ctx:
            self.c.listar_pool()
        self.assertIn("401", str(ctx.exception))
        self.get.side_effect = requests.ConnectionError("recusada")
        with self.assertRaises(fc.ErroFreshhub):
            self.c.listar_pool()
        self.post.side_effect = requests.Timeout("devagar")
        with self.assertRaises(fc.ErroFreshhub):
            self.c.eventos([])

    def test_429_tenta_de_novo_pelo_http_retry(self):
        self.get.side_effect = [_resp(429), _resp(200, {"itens": []})]
        self.assertEqual(self.c.listar_pool(), [])
        self.assertEqual(self.get.call_count, 2)


class TestDeConfig(unittest.TestCase):
    def test_sem_chaves_e_none(self):
        self.assertIsNone(fc.de_config({}))
        self.assertIsNone(fc.de_config(None))
        self.assertIsNone(fc.de_config({"freshhub_api": {"url": "http://127.0.0.1:8100"}}))
        self.assertIsNone(fc.de_config({"freshhub_api": {"url": "", "token": "fhs_x"}}))

    def test_com_chaves_monta_o_cliente(self):
        c = fc.de_config({"freshhub_api": {"url": "http://127.0.0.1:8100/", "token": "fhs_x"}})
        self.assertIsInstance(c, fc.FreshhubClient)
        self.assertEqual(c.url, "http://127.0.0.1:8100")


if __name__ == "__main__":
    unittest.main()
