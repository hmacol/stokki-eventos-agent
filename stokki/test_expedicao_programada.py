# -*- coding: utf-8 -*-
"""
Expedicao Programada (OE) da Stokki: decisao de finalizar a OE ou tirar so o pedido.

    py -3.11 -m unittest stokki.test_expedicao_programada
"""
import unittest
from unittest import mock

from stokki import expedicao_programada as oe


class PaginaFalsa:
    def __init__(self, respostas=None):
        self.url = ""
        self.posts = []
        self.respostas = respostas or {}

    def goto(self, url, **_):
        self.url = url

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, _js, args=None):
        url, campos = args
        self.posts.append((url, dict(campos)))
        return self.respostas.get(url, {"status": 200, "body": "{}"})


class TestLiberarPedido(unittest.TestCase):
    def _liberar(self, pagina, oe_do_pedido, ids, total):
        with mock.patch.object(oe, "oe_do_pedido", return_value=oe_do_pedido), \
                mock.patch.object(oe, "pedidos_da_oe", return_value=(ids, total)):
            return oe.liberar_pedido(pagina, "43333", "11:40")

    def test_pedido_fora_de_oe_nao_posta_nada(self):
        pagina = PaginaFalsa()
        self.assertEqual(self._liberar(pagina, None, [], 0), {"acao": "sem_oe", "oe": None, "erro": ""})
        self.assertEqual(pagina.posts, [])

    def test_oe_so_com_o_pedido_e_finalizada(self):
        pagina = PaginaFalsa()
        r = self._liberar(pagina, 17424, [43333], 1)
        self.assertEqual(r, {"acao": "oe_finalizada", "oe": 17424, "erro": ""})
        url, campos = pagina.posts[0]
        self.assertEqual(url, oe.URL_FINALIZAR)
        self.assertEqual((campos["shipment_id[]"], campos["hour"], campos["action"], campos["save_signature"]),
                         ("17424", "11:40", "finish", "no"))

    def test_oe_com_outros_pedidos_so_tira_o_pedido(self):
        pagina = PaginaFalsa()
        r = self._liberar(pagina, 17500, [43333, 43400], 2)
        self.assertEqual(r["acao"], "removido_da_oe")
        self.assertEqual(pagina.posts, [(oe.URL_REMOVER, {"shipment_id": "17500", "provider_outbound_id": "43333",
                                                          "row": "0"})])

    def test_oe_paginada_com_o_pedido_fora_da_primeira_pagina_tira_so_ele(self):
        pagina = PaginaFalsa()
        self.assertEqual(self._liberar(pagina, 17500, list(range(1, 11)), 25)["acao"], "removido_da_oe")
        self.assertEqual(pagina.posts[0][0], oe.URL_REMOVER)

    def test_pedido_que_nao_esta_na_lista_da_oe_nao_posta(self):
        pagina = PaginaFalsa()
        r = self._liberar(pagina, 17500, [43400], 1)
        self.assertEqual(r["acao"], "falha")
        self.assertEqual(pagina.posts, [])

    def test_recusa_da_stokki_vira_falha_com_o_texto_dela(self):
        pagina = PaginaFalsa({oe.URL_FINALIZAR: {"status": 422, "body": '{"errors": {"hour": ["Hora invalida"]}}'}})
        r = self._liberar(pagina, 17424, [43333], 1)
        self.assertEqual((r["acao"], r["erro"]), ("falha", "Hora invalida"))


if __name__ == "__main__":
    unittest.main()
