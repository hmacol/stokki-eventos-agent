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
        token = link_ciencia.gerar(101, SEGREDO, [7, 3, 7], "email")
        self.assertEqual(link_ciencia.ler(token, SEGREDO), (101, frozenset({3, 7}), "email"))
        url = link_ciencia.url(101, SEGREDO, "https://app.freshhub.com.br/cliente/", [3], "painel:joana")
        self.assertTrue(url.startswith("https://app.freshhub.com.br/cliente/ciencia/"))
        lido = link_ciencia.ler(url.rsplit("/", 1)[1], SEGREDO)
        self.assertEqual((lido.sender_id, lido.ids, lido.origem), (101, frozenset({3}), "painel:joana"))

    def test_token_sem_ciencias_ou_do_formato_antigo_devolve_none(self):
        # formato de antes de 19/11 (so o sender_id): alcancaria todo pedido do embarcador
        antigo = URLSafeTimedSerializer(SEGREDO).dumps({"r": 101}, salt="ciencia-dedicado")
        self.assertIsNone(link_ciencia.ler(antigo, SEGREDO))
        self.assertIsNone(link_ciencia.ler(link_ciencia.gerar(101, SEGREDO, [], "email"), SEGREDO))
        texto = URLSafeTimedSerializer(SEGREDO).dumps({"r": 101, "c": "12", "o": "email"}, salt="ciencia-dedicado")
        self.assertIsNone(link_ciencia.ler(texto, SEGREDO))

    def test_token_adulterado_ou_expirado_devolve_none(self):
        token = link_ciencia.gerar(101, SEGREDO, [1], "email")
        self.assertIsNone(link_ciencia.ler(token[:-2] + "xx", SEGREDO))
        self.assertIsNone(link_ciencia.ler(token, "outro-segredo"))
        self.assertIsNone(link_ciencia.ler("lixo", SEGREDO))
        self.assertIsNone(link_ciencia.ler(token, SEGREDO, validade_dias=-1))
        # mesmo segredo do portal, outra finalidade (link do canhoto)
        outro = URLSafeTimedSerializer(SEGREDO).dumps({"r": 101}, salt="portal-cliente-canhoto-email")
        self.assertIsNone(link_ciencia.ler(outro, SEGREDO))


if __name__ == "__main__":
    unittest.main()
