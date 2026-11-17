"""Link publico da pagina de ciencia do dedicado (sem login), por embarcador.

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_link_ciencia
"""

import sys
import unittest
from pathlib import Path

from itsdangerous import URLSafeTimedSerializer

_AQUI = Path(__file__).parent
sys.path.insert(0, str(_AQUI.parent))
sys.path.insert(0, str(_AQUI))

import link_ciencia

SEGREDO = "segredo-de-teste"


class TestToken(unittest.TestCase):
    def test_token_ida_e_volta(self):
        token = link_ciencia.gerar(101, SEGREDO)
        self.assertEqual(link_ciencia.ler(token, SEGREDO), 101)
        url = link_ciencia.url(101, SEGREDO, "https://app.freshhub.com.br/cliente/")
        self.assertTrue(url.startswith("https://app.freshhub.com.br/cliente/ciencia/"))
        self.assertEqual(link_ciencia.ler(url.rsplit("/", 1)[1], SEGREDO), 101)

    def test_token_adulterado_ou_expirado_devolve_none(self):
        token = link_ciencia.gerar(101, SEGREDO)
        self.assertIsNone(link_ciencia.ler(token[:-2] + "xx", SEGREDO))
        self.assertIsNone(link_ciencia.ler(token, "outro-segredo"))
        self.assertIsNone(link_ciencia.ler("lixo", SEGREDO))
        self.assertIsNone(link_ciencia.ler(token, SEGREDO, validade_dias=-1))
        # mesmo segredo do portal, outra finalidade (link do canhoto)
        outro = URLSafeTimedSerializer(SEGREDO).dumps({"r": 101}, salt="portal-cliente-canhoto-email")
        self.assertIsNone(link_ciencia.ler(outro, SEGREDO))


if __name__ == "__main__":
    unittest.main()
