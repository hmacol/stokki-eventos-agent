# -*- coding: utf-8 -*-
"""
test_ler_respostas_agendamento.py

Falha da API da IA não pode virar "não entendida" (achado 28/09): o
e-mail era marcado como processado e a resposta do embarcador se perdia.

Rodar (da raiz):
    python -m unittest test_ler_respostas_agendamento -v
"""
import unittest
from unittest import mock

import ler_respostas_agendamento as lra


class TestChamarClaude(unittest.TestCase):
    def test_falha_de_rede_marca_falha_api(self):
        with mock.patch.object(lra.requests, "post", side_effect=ConnectionError("fora do ar")):
            r = lra._chamar_claude("p", "k", {"agendamentos": [], "nao_entendido": True})
        self.assertTrue(r.get("_falha_api"))
        self.assertTrue(r["nao_entendido"])

    def test_resposta_valida_sem_flag(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"content": [{"text": '{"data": "01/10/2026", "nao_entendido": false}'}]}
        with mock.patch.object(lra.requests, "post", return_value=resp):
            r = lra._chamar_claude("p", "k", {"data": "", "nao_entendido": True})
        self.assertNotIn("_falha_api", r)
        self.assertEqual(r["data"], "01/10/2026")


if __name__ == "__main__":
    unittest.main()
