# -*- coding: utf-8 -*-
"""
Transportadora escolhida pelo embarcador na previa do envio: o confirmar grava
a escolha (e a area usa o tipo dela), o worker reescreve o <transporta> so na
copia do lote.

    py -3.11 -m unittest portal_cliente.test_transportadora_envio -v
"""
import json
import shutil
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

import enviar_stokki as es  # noqa: E402
import envio_pedidos as ep  # noqa: E402
import transportadora_envio as te  # noqa: E402
from test_endereco_entrega_envio import Base  # noqa: E402
from test_endereco_entrega_xml import _xml  # noqa: E402

COM_TRANSPORTA = _xml("").replace(
    b"<modFrete>0</modFrete>",
    b"<modFrete>0</modFrete><transporta><CNPJ>11222333000181</CNPJ><xNome>ERRADA LTDA</xNome><IE>123</IE></transporta>")


class TrocarXml(unittest.TestCase):
    def test_substitui_o_grupo_existente(self):
        novo = te.trocar_transporta_xml(COM_TRANSPORTA, "Rapido & Cia", "44.555.666/0001-81")
        nfe = ep.ler_nfe(novo)
        self.assertEqual((nfe["transportadora_nome"], nfe["transportadora_cnpj"]), ("Rapido & Cia", "44555666000181"))
        self.assertNotIn(b"ERRADA", novo)
        self.assertEqual(novo.count(b"<transporta>"), 1)

    def test_insere_quando_a_nota_nao_tem(self):
        novo = te.trocar_transporta_xml(_xml(""), "Rapida SA", "")
        nfe = ep.ler_nfe(novo)
        self.assertEqual((nfe["transportadora_nome"], nfe["transportadora_cnpj"]), ("Rapida SA", ""))
        self.assertEqual(nfe["volumes"], 2)

    def test_grupo_vazio_nao_duplica(self):
        xml = _xml("").replace(b"</modFrete>", b"</modFrete><transporta/>")
        self.assertEqual(te.trocar_transporta_xml(xml, "X", "").count(b"<transporta>"), 1)

    def test_sem_transp_levanta(self):
        with self.assertRaises(ValueError):
            te.trocar_transporta_xml(b"<nfeProc></nfeProc>", "X", "")


class NoLote(unittest.TestCase):
    def test_copia_trocada_e_original_intacto(self):
        with tempfile.TemporaryDirectory() as t:
            original = Path(t) / "o.xml"
            original.write_bytes(COM_TRANSPORTA)
            destino = Path(t) / "lote.xml"
            shutil.copyfile(original, destino)
            e = {"transportadora_json": json.dumps({"id": 1, "nome": "RAPIDA SA", "cnpj": "44555666000181"})}
            es.aplicar_transportadora_no_lote(e, destino)
            self.assertEqual(ep.ler_nfe(destino.read_bytes())["transportadora_nome"], "RAPIDA SA")
            self.assertEqual(original.read_bytes(), COM_TRANSPORTA)

    def test_sem_escolha_nao_mexe(self):
        with tempfile.TemporaryDirectory() as t:
            destino = Path(t) / "lote.xml"
            destino.write_bytes(COM_TRANSPORTA)
            es.aplicar_transportadora_no_lote({"transportadora_json": None}, destino)
            self.assertEqual(destino.read_bytes(), COM_TRANSPORTA)


class Confirmar(Base):
    def setUp(self):
        super().setUp()
        self.conn.execute("CREATE TABLE transportadoras (id INTEGER PRIMARY KEY, nome TEXT, tipo TEXT, situacao TEXT, cnpj TEXT)")
        self.conn.executemany("INSERT INTO transportadoras VALUES (?,?,?,?,?)", [
            (1, "RAPIDA SA", "ENTREGA", "ATIVA", "44555666000181"),
            (2, "RETIRA NO GALPAO", "RETIRADA", "ATIVA", ""),
            (3, "ANTIGA", "ENTREGA", "INATIVA", "")])
        self.conn.commit()

    def test_grava_escolha_e_area_usa_a_nova(self):
        with mock.patch.object(ep, "classificar_area_envio", return_value=None) as area:
            self.confirmar([self.item(transportadora_id=2)])
        self.assertEqual(area.call_args[0][0]["transportadora_nome"], "RETIRA NO GALPAO")
        r = self.linhas()[0]
        self.assertEqual(json.loads(r["transportadora_json"])["nome"], "RETIRA NO GALPAO")
        self.assertEqual(ep.ler_nfe_do_envio(dict(r))["transportadora_nome"], "RETIRA NO GALPAO")

    def test_sem_escolha_nada_gravado(self):
        self.confirmar([self.item()])
        self.assertIsNone(self.linhas()[0]["transportadora_json"])

    def test_inativa_ou_inexistente_recusa(self):
        for tid in (3, 99, "abc"):
            with self.assertRaises(ep.ErroEnvio):
                self.confirmar([self.item(transportadora_id=tid)])
        self.assertEqual(self.linhas(), [])

    def test_opcoes_so_ativas(self):
        self.assertEqual([o["id"] for o in te.opcoes(self.conn)], [1, 2])


class ColunaDaPlanilha(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE transportadoras (id INTEGER, nome TEXT, tipo TEXT, situacao TEXT, cnpj TEXT)")
        self.conn.executemany("INSERT INTO transportadoras VALUES (?,?,?,?,?)", [
            (1, "RAPIDA SA", "ENTREGA", "ATIVA", "44.555.666/0001-81"),
            (2, "RAPIDA SUL LTDA", "ENTREGA", "ATIVA", ""),
            (3, "João & Cia", "ENTREGA", "ATIVA", ""),
            (4, "ANTIGA", "ENTREGA", "INATIVA", "")])

    def test_por_nome_sem_acento_e_caixa_e_por_cnpj(self):
        self.assertEqual(te.achar_por_texto(self.conn, "joao cia")["id"], 3)
        self.assertEqual(te.achar_por_texto(self.conn, "44555666000181")["id"], 1)
        self.assertEqual(te.achar_por_texto(self.conn, "rapida sa")["id"], 1)

    def test_nao_acha_inativa_nem_desconhecida_e_ambigua_pede_cnpj(self):
        for txt in ("antiga", "ninguem"):
            with self.assertRaisesRegex(te.ErroTransportadora, "não encontrada"):
                te.achar_por_texto(self.conn, txt)
        with self.assertRaisesRegex(te.ErroTransportadora, "mais de uma"):
            te.achar_por_texto(self.conn, "rapida")

    def test_casar_opcao_da_stokki_nunca_chuta(self):
        ops = [("", "Selecione"), ("10", "Rapida SA"), ("11", "Rapida Sul Ltda"), ("12", "Joao e Cia")]
        self.assertEqual(te.casar_opcao_stokki(ops, {"nome": "RAPIDA SA"}), "10")
        self.assertEqual(te.casar_opcao_stokki(ops, {"nome": "Outra"}), "")
        self.assertEqual(te.casar_opcao_stokki([("10", "Rapida SA"), ("11", "Rapida SA Filial")], {"nome": "Rapida"}), "")


if __name__ == "__main__":
    unittest.main()
