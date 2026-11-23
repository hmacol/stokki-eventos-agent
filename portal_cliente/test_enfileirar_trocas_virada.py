# -*- coding: utf-8 -*-
"""Virada do fluxo Cross: pedidos já abertos na Stokki entram na fila de troca de NF.

    py -3.11 -m unittest portal_cliente.test_enfileirar_trocas_virada -v
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

import enfileirar_trocas_virada as virada  # noqa: E402
import envio_pedidos as ep  # noqa: E402

CNPJ = "66259821000155"


def _chave(n: int) -> str:
    return f"35261066259821000155550010000{n:05d}1000000010"[:44]


def _nfe(chave: str, nf: str, emit: str = CNPJ) -> str:
    return (f'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{chave}"><ide><nNF>{nf}</nNF></ide>'
            f'<emit><CNPJ>{emit}</CNPJ></emit></infNFe></NFe></nfeProc>')


class Planejar(unittest.TestCase):
    def test_planejar_separa_com_e_sem_original(self):
        pedidos = [{"id_stokki": 1, "codigo": "PS-1", "numero_nf": "100"},
                   {"id_stokki": 2, "codigo": "PS-2", "numero_nf": "200"}]
        originais = {"100": (_chave(100), Path("a.xml"))}
        fila, manuais = virada.planejar(pedidos, originais)
        self.assertEqual(fila, [{"id_stokki": 1, "codigo": "PS-1", "numero_nf": "100", "chave_nfe": _chave(100),
                                 "caminho": Path("a.xml")}])
        self.assertEqual(manuais, [pedidos[1]])


class Rodar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self.raiz = raiz
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "d.db"), mock.patch.object(ep, "_RAIZ", raiz)]
        for p in self._patches:
            p.start()
        self.conn = ep.conectar()
        self.conn.executescript(f"""
            CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER,
                                  atualizado_em TEXT, fluxos TEXT);
            INSERT INTO interno VALUES ('{CNPJ}', 'DOURADO', NULL, 18, NULL, 'cross');
            INSERT INTO interno VALUES ('35539067000183', 'MUAI', NULL, 99, NULL, 'full_cross');
        """)
        # original do portal (NF 100) e do importador por e-mail (NF 200); NF 300 sem original
        rel = Path("x") / f"{_chave(100)}.xml"
        (raiz / rel).parent.mkdir(parents=True)
        (raiz / rel).write_text(_nfe(_chave(100), "100"), encoding="utf-8")
        self.conn.execute("INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, numero_nf, xml_path, status, origem, "
                          "criado_em, atualizado_em) VALUES (?, ?, '100', ?, 'CRIADO', 'xml', 'a', 'a')",
                          (CNPJ, _chave(100), str(rel)))
        self.conn.commit()
        self.extraidos = raiz / "importador" / "clientes" / "dourado" / "dados" / "xmls_extraidos"
        self.extraidos.mkdir(parents=True)
        (self.extraidos / f"{_chave(200)}.xml").write_text(_nfe(_chave(200), "200"), encoding="utf-8")
        (self.extraidos / f"{_chave(201)}.xml").write_text(_nfe(_chave(201), "200", emit="99999999000199"), encoding="utf-8")
        self.listados = []

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _listar(self, sessao, stkkc):
        self.listados.append(stkkc)
        return [{"id_stokki": 10, "codigo": "PS-10", "numero_nf": "100"},
                {"id_stokki": 20, "codigo": "PS-20", "numero_nf": "200"},
                {"id_stokki": 30, "codigo": "PS-30", "numero_nf": "300"}]

    def _rodar(self, executar):
        with mock.patch.object(virada, "listar_abertos", side_effect=self._listar):
            return virada.rodar(self.conn, object(), [self.raiz / "importador" / "clientes"], executar=executar)

    def _fila(self):
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'trocas_nf_pendentes'").fetchone():
            return []
        return [dict(r) for r in self.conn.execute("SELECT * FROM trocas_nf_pendentes ORDER BY numero_nf")]

    def test_originais_do_portal_e_do_importador(self):
        originais = virada.originais_do_embarcador(self.conn, CNPJ, [self.raiz / "importador" / "clientes"])
        self.assertEqual(originais["100"][0], _chave(100))
        self.assertEqual(originais["200"], (_chave(200), self.extraidos / f"{_chave(200)}.xml"))  # emitente de outro nao conta

    def test_xml_do_importador_exige_emitente_e_chave_do_embarcador(self):
        # emitente com CPF e o CNPJ do embarcador so no destinatario: nao e dele
        falso = (f'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{_chave(400)}"><ide><nNF>400</nNF></ide>'
                 f'<emit><CPF>12345678901</CPF></emit><dest><CNPJ>{CNPJ}</CNPJ></dest></infNFe></NFe></nfeProc>')
        (self.extraidos / "falso.xml").write_text(falso, encoding="utf-8")
        # emitente certo mas chave de outro CNPJ (posicoes 7-20): descartado
        outra = "35261099999999000199550010000004011000000010"
        (self.extraidos / "chave_outra.xml").write_text(_nfe(outra, "401"), encoding="utf-8")
        originais = virada.originais_do_embarcador(self.conn, CNPJ, [self.raiz / "importador" / "clientes"])
        self.assertNotIn("400", originais)
        self.assertNotIn("401", originais)

    def test_mesma_nf_com_chaves_diferentes_vira_troca_manual(self):
        (self.extraidos / "serie2.xml").write_text(_nfe(_chave(202).replace("0010000", "0020000", 1), "200"), encoding="utf-8")
        originais = virada.originais_do_embarcador(self.conn, CNPJ, [self.raiz / "importador" / "clientes"])
        self.assertIsNone(originais["200"])
        fila, manuais = virada.planejar([{"id_stokki": 20, "codigo": "PS-20", "numero_nf": "200"}], originais)
        self.assertEqual((fila, [m["codigo"] for m in manuais]), ([], ["PS-20"]))

    def test_so_embarcador_cross_e_listado(self):
        self._rodar(executar=False)
        self.assertEqual(self.listados, [18])

    def test_modo_teste_nao_grava(self):
        r = self._rodar(executar=False)
        self.assertEqual(self._fila(), [])
        self.assertEqual((r["enfileirados"], r["manuais"]), (2, 1))

    def test_executar_enfileira_com_id_e_origem_virada(self):
        r = self._rodar(executar=True)
        fila = self._fila()
        self.assertEqual([(f["numero_nf"], f["id_stokki"], f["origem"], f["client_id"]) for f in fila],
                         [("100", 10, "virada", "18"), ("200", 20, "virada", "18")])
        self.assertEqual(r["manuais_lista"][0]["codigo"], "PS-30")


if __name__ == "__main__":
    unittest.main()
