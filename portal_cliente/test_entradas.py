# -*- coding: utf-8 -*-
"""
Aba Pedidos de Entrada do portal (spec
docs/superpowers/specs/2026-09-24-pedidos-de-entrada-portal-design.md).
Sem rede e sem tocar no dados.db real.

    py -3.11 -m unittest portal_cliente.test_entradas -v
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

CNPJ = "12345678000195"          # o cliente (emitente da remessa)
CNPJ_FRESHLOG = "11222333000181"  # destinatário (galpão)
OUTRO = "99887766000155"
HOJE = date.today()
AMANHA = (HOJE + timedelta(days=1)).isoformat()


def xml_remessa(nf="41221", emitente=CNPJ, dest=CNPJ_FRESHLOG, tp_nf="1", itens=None, chave=None):
    """NF-e de remessa pra armazenagem: emitente = cliente, destinatário =
    Fresh Log. tpNF=1 (saída do ponto de vista de quem emite)."""
    chave = chave or f"3526091234567800019555001000{nf.zfill(6)}1000000017"[:44]
    itens = itens or [("NUU001FD", "7891234567890", "MINI CX PAO DE QUEIJO", 20, "CX", 64.77)]
    dets = "".join(
        f'<det nItem="{i}"><prod><cProd>{sku}</cProd><cEAN>{ean}</cEAN><xProd>{desc}</xProd>'
        f'<qCom>{qtd}</qCom><uCom>{un}</uCom><vUnCom>{vu}</vUnCom></prod></det>'
        for i, (sku, ean, desc, qtd, un, vu) in enumerate(itens, start=1))
    return f"""<?xml version="1.0"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{chave}">
      <ide><nNF>{nf}</nNF><serie>1</serie><tpNF>{tp_nf}</tpNF><dhEmi>2026-09-24T10:00:00-03:00</dhEmi></ide>
      <emit><CNPJ>{emitente}</CNPJ><xNome>Cliente Teste</xNome></emit>
      <dest><CNPJ>{dest}</CNPJ><xNome>FRESHLOG</xNome><enderDest><xLgr>Rua G</xLgr><nro>1</nro><xBairro>B</xBairro>
      <xMun>São Paulo</xMun><UF>SP</UF><CEP>01310100</CEP></enderDest></dest>
      {dets}
      <transp><vol><qVol>3</qVol><pesoB>12.5</pesoB></vol></transp><total><ICMSTot><vNF>1295.40</vNF></ICMSTot></total>
    </infNFe></NFe></nfeProc>""".encode("utf-8")


class TestLerNfeEntrada(unittest.TestCase):
    def test_envios_continua_recusando_tpnf_zero(self):
        with self.assertRaises(ep.ErroEnvio):
            ep.ler_nfe(xml_remessa(tp_nf="0"), "x.xml")

    def test_permitir_entrada_aceita_tpnf_zero_e_um(self):
        for tp in ("0", "1"):
            nfe = ep.ler_nfe(xml_remessa(tp_nf=tp), "x.xml", permitir_entrada=True)
            self.assertEqual(nfe["tipo_nf"], tp)
            self.assertEqual(nfe["numero_nf"], "41221")

    def test_itens_lista_vem_do_det(self):
        nfe = ep.ler_nfe(xml_remessa(itens=[("A", "7890000000001", "Prod A", "2.5", "CX", "10.00"),
                                            ("B", "SEM GTIN", "Prod B", 1, "UN", 3)]), "x.xml")
        self.assertEqual(nfe["itens"], 2)
        self.assertEqual(nfe["itens_lista"][0], {"linha": 1, "sku": "A", "ean": "7890000000001", "descricao": "Prod A",
                                                 "quantidade": 2.5, "unidade": "CX", "valor_unitario": 10.0})
        self.assertEqual(nfe["itens_lista"][1]["ean"], "")   # 'SEM GTIN' vira vazio
        self.assertEqual(nfe["itens_lista"][1]["quantidade"], 1.0)


if __name__ == "__main__":
    unittest.main()
