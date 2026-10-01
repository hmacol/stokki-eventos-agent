# -*- coding: utf-8 -*-
"""
Texto do e-mail de area nao atendida, compartilhado entre a roteirizacao
(notificar_remetentes) e o botao "Avisar clientes" do planejamento
(avisar_fora_area.py), 30/09/2026.

    py -3.11 -m unittest roteirizacao.test_notificar_area_nao_atendida
"""
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
for _p in (_AQUI.parent, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import notificar_area_nao_atendida as na  # noqa: E402

PEDIDOS = [{"id": 1, "code": "#PS-1", "address": "Rua A, 1 - Centro, Curitiba - PR, 80000-000"},
           {"id": 2, "code": "PS-2", "address": "Rua B, 2 - Centro, Blumenau - SC, 89000-000"}]


class AssuntoEConteudo(unittest.TestCase):
    def test_fora_sp(self):
        assunto, html = na.assunto_e_conteudo("ACME", na.TIPO_FORA_SP, PEDIDOS)
        self.assertEqual(assunto, "[Freshlog] 2 pedido(s) — fora de SP — confirmar redespacho")
        self.assertIn("Entrega fora do estado de São Paulo", html)
        self.assertIn("Olá, ACME.", html)
        self.assertIn("#PS-1", html)
        self.assertIn("#PS-2", html)
        self.assertIn("Curitiba - PR", html)

    def test_sp_nao_atendido(self):
        assunto, html = na.assunto_e_conteudo("", na.TIPO_SP_NAO_ATENDIDO, PEDIDOS[:1])
        self.assertEqual(assunto, "[Freshlog] 1 pedido(s) — cotação necessária")
        self.assertIn("Região fora da área de atendimento", html)

    def test_escapa_html(self):
        _, html = na.assunto_e_conteudo("<b>x</b>", na.TIPO_FORA_SP, PEDIDOS[:1])
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)


if __name__ == "__main__":
    unittest.main()
