# -*- coding: utf-8 -*-
"""
Servico da VUUPT com "code": null (24/10/2026): o matcher quebrava com
AttributeError ('NoneType' object has no attribute 'lstrip') e o documento
ia pra revisao manual como "erro inesperado" (2x por dia desde 17/10).
Rodar:  py -3.11 -m unittest documentos_pedido.test_matcher_code_nulo
"""
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from matcher import casar_documento_com_pedido  # noqa: E402


class VuuptFalso:
    def __init__(self, por_titulo=None, cliente=None, por_cliente=None):
        self.por_titulo, self.cliente, self.por_cliente = por_titulo or [], cliente, por_cliente or []

    def listar_servicos(self, filtros, per_page=50, include=None):
        return self.por_titulo if filtros[0]["field"] == "title" else self.por_cliente

    def buscar_customer_por_code(self, cnpj):
        return self.cliente


class CodeNulo(unittest.TestCase):
    def test_referencia_no_titulo_ignora_servico_sem_code(self):
        vuupt = VuuptFalso(por_titulo=[{"code": None}, {"code": "#PS-1"}])
        r = casar_documento_com_pedido("boleto.pdf", None, "", vuupt, tipo_documento="Boleto",
                                       metadados_boleto={"numero_nf": "41400", "cnpj_pagador": None})
        self.assertEqual(r["codigo_pedido"], "PS-1")

    def test_cnpj_com_unico_servico_sem_code_vai_pra_revisao_com_motivo(self):
        vuupt = VuuptFalso(cliente={"id": 7}, por_cliente=[{"code": None}])
        r = casar_documento_com_pedido("boleto.pdf", None, "", vuupt, tipo_documento="Boleto",
                                       metadados_boleto={"numero_nf": None,
                                                         "cnpj_pagador": "12.345.678/0001-90"})
        self.assertIsNone(r["codigo_pedido"])
        self.assertIn("sem código", r["motivo_falha"])


if __name__ == "__main__":
    unittest.main()
