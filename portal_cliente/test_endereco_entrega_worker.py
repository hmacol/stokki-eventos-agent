# -*- coding: utf-8 -*-
"""
Worker do portal insere o <entrega> escolhido no XML do lote que vai pra
Stokki (spec 2026-10-27); o XML original do cliente nunca muda.

    py -3.11 -m unittest portal_cliente.test_endereco_entrega_worker -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import enviar_stokki as es  # noqa: E402
import envio_pedidos as ep  # noqa: E402
from test_endereco_entrega_xml import ENTREGA, _xml  # noqa: E402

END = {"cep": "01414001", "logradouro": "Rua Haddock Lobo", "numero": "595", "complemento": "loja 2",
       "bairro": "Cerqueira César", "municipio": "São Paulo", "uf": "SP", "cmun": "3550308",
       "documento": "51322677000187", "nome": "DESTINATARIO TESTE"}


class AplicarNoLote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        self.original = raiz / "original.xml"
        self.original.write_bytes(_xml(""))
        self.destino = raiz / "lote" / "x.xml"
        self.destino.parent.mkdir()

    def _copia(self):
        shutil.copyfile(self.original, self.destino)

    def test_origem_cliente_insere_e_original_intacto(self):
        antes = self.original.read_bytes()
        self._copia()
        es.aplicar_entrega_no_lote({"endereco_entrega_origem": "cliente", "endereco_entrega_json": json.dumps(END)}, self.destino)
        nfe = ep.ler_nfe(self.destino.read_bytes())
        self.assertEqual((nfe["destinatario_endereco"], nfe["destinatario_cep"]), ("Rua Haddock Lobo, 595 - loja 2", "01414001"))
        self.assertEqual(self.original.read_bytes(), antes)

    def test_origem_ultima_entrega_insere(self):
        self._copia()
        es.aplicar_entrega_no_lote({"endereco_entrega_origem": "ultima_entrega", "endereco_entrega_json": json.dumps(END)},
                                   self.destino)
        self.assertIn(b"<entrega><CNPJ>51322677000187</CNPJ><xNome>DESTINATARIO TESTE</xNome>", self.destino.read_bytes())

    def test_origem_fiscal_nota_e_vazia_nao_mexem(self):
        for e in ({"endereco_entrega_origem": "fiscal"}, {"endereco_entrega_origem": "nota"},
                  {"endereco_entrega_origem": None}, {}):
            self._copia()
            es.aplicar_entrega_no_lote(e, self.destino)
            self.assertEqual(self.destino.read_bytes(), self.original.read_bytes())

    def test_reenvio_de_erro_insere_de_novo(self):
        e = {"endereco_entrega_origem": "cliente", "endereco_entrega_json": json.dumps(END)}
        self._copia()
        es.aplicar_entrega_no_lote(e, self.destino)
        primeira = self.destino.read_bytes()
        self._copia()
        es.aplicar_entrega_no_lote(e, self.destino)
        self.assertEqual(self.destino.read_bytes(), primeira)

    def test_xml_que_ja_tem_entrega_falha(self):
        self.original.write_bytes(_xml(ENTREGA))
        self._copia()
        with self.assertRaises(ValueError):
            es.aplicar_entrega_no_lote({"endereco_entrega_origem": "cliente", "endereco_entrega_json": json.dumps(END)},
                                       self.destino)


if __name__ == "__main__":
    unittest.main()
