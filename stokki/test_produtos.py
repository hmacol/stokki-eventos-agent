# -*- coding: utf-8 -*-
"""
test_produtos.py

Parser da tabela de produtos da Stokki (client/product/table). A linha
abaixo é a primeira linha REAL capturada na VPS em 22/09/2026, quando a
Stokki trocou as células HTML por JSON estruturado (selection/product/
state...) e a sincronização passou a descartar as 1.847 linhas.

Rodar (da raiz):
    py -3.11 -m unittest stokki.test_produtos -v
"""
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from stokki.produtos import _parsear_linha  # noqa: E402

_LINHA = {
    "selection": {"id": 1, "status": True, "sku": "MKCA2022"},
    "image": {"url": "https://3pl-freshlog-files-nv.s3.amazonaws.com/users/clients/inventories/products/1.png", "id": 1},
    "product": {
        "text": "MOMBAK CAPPUCCINO - Cafe leite e chocolate",
        "details": [
            {"label": "DEPOSITANTE", "value": "MOMBAK COMERCIO DE ALIMENTOS E BEBIDAS LTDA", "sub": "#stkkc-22", "subMono": True, "line": 1},
            {"label": "CATEGORIA", "value": "COLD BREW COFFEE", "line": 1},
            {"label": "SKU", "value": "MKCA2022", "copy": "MKCA2022", "mono": True, "line": 2},
            {"label": "GTIN/EAN", "value": "7898966259166", "copy": "7898966259166", "mono": True, "line": 2},
        ],
        "markers": [{"key": "lot", "on": True}, {"key": "dun", "on": True}],
    },
    "created": {"text": "25/11/2024", "sub": "15:18"},
    "updated": {"text": "01/06/2026", "sub": "08:40"},
    "origin": "Bling",
    "state": {"key": "Active", "label": "Ativo", "color": "success"},
    "actions": [],
}


class TestParsearLinha(unittest.TestCase):
    def test_linha_real(self):
        self.assertEqual(_parsear_linha(_LINHA), {
            "stokki_id": 1,
            "descricao": "MOMBAK CAPPUCCINO - Cafe leite e chocolate",
            "embarcador": "MOMBAK COMERCIO DE ALIMENTOS E BEBIDAS LTDA",
            "sku": "MKCA2022",
            "ean": "7898966259166",
            "categoria": "COLD BREW COFFEE",
            "controla_lote": 1,
            "ativo": 1,
            "estado_stokki": "Ativo",
            "origem_stokki": "Bling",
            "stokki_atualizado_em": "2026-06-01 08:40:00",
        })

    def test_sem_lote_e_inativo(self):
        linha = dict(_LINHA, state={"key": "Inactive", "label": "Inativo", "color": "danger"},
                     product=dict(_LINHA["product"], markers=[{"key": "lot", "on": False}]))
        p = _parsear_linha(linha)
        self.assertEqual((p["controla_lote"], p["ativo"]), (0, 0))

    def test_sem_id_descarta(self):
        self.assertIsNone(_parsear_linha(dict(_LINHA, selection={})))


if __name__ == "__main__":
    unittest.main()
