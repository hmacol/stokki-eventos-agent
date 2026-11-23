# -*- coding: utf-8 -*-
"""
Endereco de entrega do XML (Hugo, 24/09/2026, caso NF 41240 da Maria
Dolores): quando a NF-e traz o grupo <entrega> (local de entrega diferente
do endereco fiscal do destinatario), o portal passa a usar ESSE endereco em
tudo -- gravacao, checagem de area, tela. O fiscal fica guardado so pra
informacao.

    py -3.11 -m unittest portal_cliente.test_endereco_entrega_xml
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

CHAVE = "31260922135070000190550010000412401256412410"
DEST = ("<dest><CNPJ>51322677000187</CNPJ><xNome>DESTINATARIO TESTE</xNome><enderDest><xLgr>RUA IVAI</xLgr><nro>207</nro>"
        "<xBairro>TATUAPE</xBairro><xMun>SAO PAULO</xMun><UF>SP</UF><CEP>03080010</CEP><fone>1133334444</fone></enderDest></dest>")
ENTREGA = ("<entrega><CNPJ>51322677000187</CNPJ><xNome>Destinatario</xNome><xLgr>Rua Aguapei</xLgr><nro>110</nro>"
           "<xBairro>Vila Santo Estevao</xBairro><xMun>Sao Paulo</xMun><UF>SP</UF><CEP>03325000</CEP><fone>11964885514</fone></entrega>")


def _xml(entrega: str = ENTREGA) -> bytes:
    return f"""<?xml version="1.0"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{CHAVE}">
<ide><nNF>41240</nNF><serie>1</serie><tpNF>1</tpNF></ide><emit><CNPJ>22135070000190</CNPJ><xNome>Emit</xNome></emit>
{DEST}{entrega}<transp><modFrete>0</modFrete><vol><qVol>2</qVol></vol></transp></infNFe></NFe></nfeProc>""".encode("utf-8")


class LerNfeLocalDeEntrega(unittest.TestCase):
    def test_usa_o_endereco_de_entrega(self):
        nfe = ep.ler_nfe(_xml())
        self.assertEqual(nfe["destinatario_endereco"], "Rua Aguapei, 110")
        self.assertEqual(nfe["destinatario_bairro"], "Vila Santo Estevao")
        self.assertEqual((nfe["destinatario_municipio"], nfe["destinatario_uf"]), ("Sao Paulo", "SP"))
        self.assertEqual(nfe["destinatario_cep"], "03325000")
        self.assertEqual(nfe["destinatario_telefone"], "11964885514")
        # quem recebe continua sendo o destinatario fiscal
        self.assertEqual((nfe["destinatario_nome"], nfe["destinatario_doc"]), ("DESTINATARIO TESTE", "51322677000187"))

    def test_guarda_o_fiscal_pra_informacao(self):
        self.assertEqual(ep.ler_nfe(_xml())["endereco_fiscal"], "RUA IVAI, 207, TATUAPE, SAO PAULO/SP, CEP 03080-010")

    def test_sem_grupo_entrega_nada_muda(self):
        nfe = ep.ler_nfe(_xml(""))
        self.assertEqual(nfe["destinatario_endereco"], "RUA IVAI, 207")
        self.assertEqual(nfe["destinatario_cep"], "03080010")
        self.assertEqual(nfe["destinatario_telefone"], "1133334444")
        self.assertEqual(nfe["endereco_fiscal"], "")

    def test_entrega_sem_fone_usa_o_do_destinatario(self):
        nfe = ep.ler_nfe(_xml(ENTREGA.replace("<fone>11964885514</fone>", "")))
        self.assertEqual(nfe["destinatario_telefone"], "1133334444")

    def test_entrega_sem_logradouro_e_ignorada(self):
        nfe = ep.ler_nfe(_xml("<entrega><CNPJ>51322677000187</CNPJ><xMun>Sao Paulo</xMun><UF>SP</UF></entrega>"))
        self.assertEqual(nfe["destinatario_endereco"], "RUA IVAI, 207")
        self.assertEqual(nfe["endereco_fiscal"], "")


class AreaPeloLocalDeEntrega(unittest.TestCase):
    def test_classificacao_geocodifica_o_endereco_de_entrega(self):
        with mock.patch.object(ep, "_identificar_area", return_value=[]) as ident:
            ep.classificar_area_envio(ep.ler_nfe(_xml()), {"google_maps": {"api_key": "k"}})
        endereco = ident.call_args[0][0][0]["address"]
        self.assertIn("Rua Aguapei, 110", endereco)
        self.assertIn("03325000", endereco)
        self.assertNotIn("IVAI", endereco)

    def test_entrega_fora_da_area_bloqueia_mesmo_com_fiscal_em_sp(self):
        fora = ENTREGA.replace("<xMun>Sao Paulo</xMun><UF>SP</UF>", "<xMun>Curitiba</xMun><UF>PR</UF>")
        with mock.patch.object(ep, "_identificar_area",
                               side_effect=lambda s, k: [(s[0], "fora_sp")] if "Curitiba - PR" in s[0]["address"] else []):
            self.assertEqual(ep.classificar_area_envio(ep.ler_nfe(_xml(fora)), {"google_maps": {"api_key": "k"}}), "fora_sp")


class GravaELista(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "d.db"),
                         mock.patch.object(ep, "PASTA_XMLS", raiz / "x"),
                         mock.patch.object(ep, "PASTA_TEMP", raiz / "x" / "_temporarios"),
                         mock.patch.object(ep, "_RAIZ", raiz)]
        for p in self._patches:
            p.start()
        (raiz / "x" / "_temporarios").mkdir(parents=True)
        self.conn = ep.conectar()

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_confirmar_grava_entrega_e_fiscal(self):
        (ep.PASTA_TEMP / "tokenteste41240.xml").write_bytes(_xml())
        with mock.patch.object(ep, "validar_item", return_value={"ok": True, "erros": [], "envio_existente": None}), \
             mock.patch.object(ep, "info_destinatarios", return_value={}), \
             mock.patch.object(ep, "gravar_destinatario"), \
             mock.patch.object(ep, "classificar_area_envio", return_value=None):
            ep.confirmar_envios(self.conn, "22135070000190",
                                [{"token": "tokenteste41240", "horario_inicio": "09:00", "horario_fim": "17:30"}],
                                "cliente", {}, "nenhuma")
        r = self.conn.execute("SELECT * FROM portal_envios").fetchone()
        self.assertEqual((r["destinatario_endereco"], r["destinatario_cep"]), ("Rua Aguapei, 110", "03325000"))
        self.assertEqual(r["endereco_fiscal"], "RUA IVAI, 207, TATUAPE, SAO PAULO/SP, CEP 03080-010")
        linha = ep._linha(r, {})
        self.assertEqual(linha["endereco_curto"], "Rua Aguapei, 110, Vila Santo Estevao, Sao Paulo")
        self.assertEqual(linha["endereco_fiscal"], "RUA IVAI, 207, TATUAPE, SAO PAULO/SP, CEP 03080-010")


if __name__ == "__main__":
    unittest.main()
