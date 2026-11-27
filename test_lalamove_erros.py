# -*- coding: utf-8 -*-
import unittest

from lalamove_client import LalamoveAPIError
from lalamove_erros import classificar, formatar


class TestLalamoveErros(unittest.TestCase):
    def cod(self, erro):
        return classificar(erro)["codigo"]

    def test_classifica_por_texto_e_status(self):
        casos = {
            "HTTP 422: ERR_INVALID_PHONE_NUMBER: invalid phone": "LAL-04",
            "HTTP 402: ERR_INSUFFICIENT_CREDIT": "LAL-07",
            "HTTP 401: Unauthorized": "LAL-08",
            "HTTP 429: too many requests": "LAL-09",
            "HTTP 503: unavailable": "LAL-10",
            "HTTP 422: ERR_OUT_OF_SERVICE_AREA": "LAL-03",
            "Lalamove: não consegui geocodificar a base (Rua Zilda).": "LAL-03",
            "HTTP 400: ERR_INVALID_SPECIAL_REQUEST": "LAL-05",
            "Cotação devolveu 3 stopIds pra 4 paradas.": "LAL-06",
            "Rascunho sem paradas.": "LAL-02",
            "lalamove.remetente_telefone precisa estar em E.164": "LAL-01",
            "Título não atualizado: PS-1: HTTP 500": "LAL-11",
            "algo estranho": "LAL-99",
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(self.cod(texto), esperado)

    def test_aceita_excecao(self):
        self.assertEqual(self.cod(LalamoveAPIError("HTTP 429: x", 429)), "LAL-09")

    def test_formatar_guarda_detalhe_e_e_idempotente(self):
        f = formatar("HTTP 401: Unauthorized")
        self.assertTrue(f.startswith("LAL-08 "))
        self.assertTrue(f.endswith("[HTTP 401: Unauthorized]"))
        self.assertEqual(formatar(f), f)


if __name__ == "__main__":
    unittest.main()
