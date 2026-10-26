# -*- coding: utf-8 -*-
"""Worker do portal no fluxo Cross: converte pro item XD e enfileira a troca de NF.

    py -3.11 -m unittest portal_cliente.test_enviar_stokki_cross -v
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

import enviar_stokki as es  # noqa: E402
import envio_pedidos as ep  # noqa: E402

CNPJ = "66259821000155"


def _chave(n: int) -> str:
    return f"35261066259821000155550010000{n:05d}1000000010"[:44]


class _PxFalso:
    def __init__(self):
        self.chamadas = []
        self.TRANSFORMACOES = {"cross": self._cross, "muai": self._muai}

    def _cross(self, origem, destino, codigo_xd, nome_item):
        self.chamadas.append(("cross", codigo_xd, nome_item))
        Path(destino).write_bytes(Path(origem).read_bytes())

    def _muai(self, origem, destino):
        self.chamadas.append(("muai",))
        Path(destino).write_bytes(Path(origem).read_bytes())


class TransformarXml(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.o, self.d = Path(self.tmp.name) / "o.xml", Path(self.tmp.name) / "d.xml"
        self.o.write_bytes(b"<x/>")

    def test_transformar_xml_cross_usa_regra_parametrizada(self):
        px = _PxFalso()
        es.transformar_xml("muai", self.o, self.d, px, fluxo="cross", xd={"codigo_xd": "XDDOURADO", "nome_item": "Mercadoria DOURADO"})
        self.assertEqual(px.chamadas, [("cross", "XDDOURADO", "Mercadoria DOURADO")])

    def test_transformar_xml_cross_sem_xd_levanta_value_error(self):
        with self.assertRaises(ValueError) as c:
            es.transformar_xml("nenhuma", self.o, self.d, _PxFalso(), fluxo="cross",
                               xd={"codigo_xd": None, "erro_xd": "nenhum produto XD no catálogo do embarcador"})
        self.assertIn("XD não encontrado", str(c.exception))

    def test_armazenagem_segue_a_regra_do_embarcador(self):
        px = _PxFalso()
        es.transformar_xml("muai", self.o, self.d, px, fluxo="armazenagem")
        self.assertEqual(px.chamadas, [("muai",)])

    def test_id_do_codigo(self):
        self.assertEqual(es._id_do_codigo("PS-42584"), 42584)
        self.assertIsNone(es._id_do_codigo("PS-36327-R1"))
        self.assertIsNone(es._id_do_codigo(None))


class ProcessarLote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self.raiz = raiz
        self.px = _PxFalso()
        self.wizard = mock.Mock()
        self._patches = [mock.patch.object(ep, "DB_PATH", raiz / "d.db"), mock.patch.object(ep, "_RAIZ", raiz),
                         mock.patch.object(ep, "PASTA_XMLS", raiz / "x"),
                         mock.patch.object(es, "PASTA_LOTES", raiz / "lotes"),
                         mock.patch.object(es, "carregar_importador", return_value=(object(), self.px)),
                         mock.patch.object(es, "executar_wizard", self.wizard),
                         mock.patch.object(es, "_credenciais", return_value=("u", "s")),
                         mock.patch.object(es, "_avisar_erros"),
                         mock.patch.object(es.sessao_uso, "adquirir", return_value="principal"),
                         mock.patch.object(es.sessao_uso, "liberar")]
        for p in self._patches:
            p.start()
        self.conn = ep.conectar()
        self.conn.executescript(f"""
            CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER,
                                  sender_id INTEGER, email TEXT, atualizado_em TEXT, fluxos TEXT);
            INSERT INTO interno VALUES ('{CNPJ}', 'DOURADO', 'LATICINIOS DOURADO', 18, 11426239, '', NULL, 'armazenagem,cross');
            CREATE TABLE wms_produtos (sku TEXT, embarcador_id INTEGER, ativo INTEGER);
            INSERT INTO wms_produtos VALUES ('XDDOURADO', 18, 1);
        """)

    def tearDown(self):
        self.conn.close()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _envio(self, n: int, fluxo: str) -> dict:
        chave = _chave(n)
        rel = Path("x") / CNPJ / f"{chave}.xml"
        (self.raiz / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.raiz / rel).write_bytes(b"<nfeProc/>")
        self.conn.execute("""INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, numero_nf, xml_path, status, fluxo,
                             criado_em, atualizado_em) VALUES (?, ?, ?, ?, 'NA_FILA', ?, 'a', 'a')""",
                          (CNPJ, chave, str(n), str(rel), fluxo))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM portal_envios WHERE chave_nfe = ?", (chave,)).fetchone())

    def _wizard_devolve(self, resultados, codigos=None):
        self.wizard.return_value = (resultados, {}, codigos or {})

    def _fila(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM trocas_nf_pendentes ORDER BY id")] \
            if self.conn.execute("SELECT name FROM sqlite_master WHERE name='trocas_nf_pendentes'").fetchone() else []

    def test_envio_cross_criado_enfileira_troca_com_id_e_marca_pendente(self):
        e = self._envio(1, "cross")
        self._wizard_devolve([{"arquivo": f"{e['chave_nfe']}.xml", "criado": True, "erro": ""}], {"1": "PS-42584"})
        es.processar_lote(self.conn, CNPJ, [e], {})
        self.assertEqual(self.px.chamadas, [("cross", "XDDOURADO", "Mercadoria DOURADO")])
        fila = self._fila()
        self.assertEqual(len(fila), 1)
        self.assertEqual((fila[0]["id_stokki"], fila[0]["codigo_pedido"], fila[0]["origem"], fila[0]["envio_id"]),
                         (42584, "PS-42584", "portal", e["id"]))
        self.assertEqual((fila[0]["client_id"], fila[0]["caminho_xml_original"]), ("18", e["xml_path"]))
        env = dict(self.conn.execute("SELECT status, troca_nf_status FROM portal_envios WHERE id = ?", (e["id"],)).fetchone())
        self.assertEqual((env["status"], env["troca_nf_status"]), ("CRIADO", "PENDENTE"))

    def test_envio_cross_duplicado_enfileira_troca_sem_id(self):
        e = self._envio(2, "cross")
        self._wizard_devolve([{"arquivo": f"{e['chave_nfe']}.xml", "criado": False,
                               "erro": "Chave da NFe já utilizada no sistema, verifique os pedidos em andamento."}])
        es.processar_lote(self.conn, CNPJ, [e], {})
        fila = self._fila()
        self.assertEqual((len(fila), fila[0]["id_stokki"]), (1, None))
        st = self.conn.execute("SELECT status FROM portal_envios WHERE id = ?", (e["id"],)).fetchone()[0]
        self.assertEqual(st, "DUPLICADO")

    def test_envio_armazenagem_nao_enfileira(self):
        e = self._envio(3, "armazenagem")
        self._wizard_devolve([{"arquivo": f"{e['chave_nfe']}.xml", "criado": True, "erro": ""}], {"3": "PS-42590"})
        es.processar_lote(self.conn, CNPJ, [e], {})
        self.assertEqual(self._fila(), [])
        self.assertEqual(self.px.chamadas, [])   # regra "nenhuma" do embarcador: copia o XML

    def test_cross_sem_xd_vira_erro_e_nao_chama_a_stokki(self):
        self.conn.execute("DELETE FROM wms_produtos")
        e = self._envio(4, "cross")
        es.processar_lote(self.conn, CNPJ, [e], {})
        self.wizard.assert_not_called()
        linha = dict(self.conn.execute("SELECT status, erro FROM portal_envios WHERE id = ?", (e["id"],)).fetchone())
        self.assertEqual(linha["status"], "ERRO")
        self.assertIn("XD não encontrado", linha["erro"])

    def test_simular_nao_enfileira(self):
        e = self._envio(5, "cross")
        es.processar_lote(self.conn, CNPJ, [e], {}, simular=True)
        self.assertEqual(self._fila(), [])


if __name__ == "__main__":
    unittest.main()
