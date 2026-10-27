# -*- coding: utf-8 -*-
"""Fluxos por embarcador (Armazenagem / Full cross / Cross) e descoberta do XD.

    py -3.11 -m unittest nucleo.test_fluxo_embarcador -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import fluxo_embarcador as fe  # noqa: E402


def _banco():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, apelido TEXT, nome_remetente TEXT,
                              stkkc_id INTEGER, atualizado_em TEXT);
        CREATE TABLE wms_produtos (sku TEXT, embarcador_id INTEGER, ativo INTEGER);
        INSERT INTO interno VALUES ('66259821000155', 'DOURADO', 'LATICINIOS DOURADO LTDA', 18, NULL);
        INSERT INTO interno VALUES ('68146976000100', 'QUATRO ESTRELAS', 'EMPORIO QUATRO ESTRELAS', 98, NULL);
    """)
    fe.garantir_coluna(conn)
    return conn


class Normalizar(unittest.TestCase):
    def test_normalizar_vazio_e_armazenagem(self):
        self.assertEqual(fe.normalizar_fluxos(None), ["armazenagem"])
        self.assertEqual(fe.normalizar_fluxos(""), ["armazenagem"])
        self.assertEqual(fe.normalizar_fluxos(" cross , full_cross"), ["cross", "full_cross"])
        self.assertEqual(fe.normalizar_fluxos("xpto"), ["armazenagem"])

    def test_definir_fluxos_invalido_levanta(self):
        conn = _banco()
        with self.assertRaises(ValueError):
            fe.definir_fluxos(conn, "66259821000155", ["xpto"])
        with self.assertRaises(ValueError):
            fe.definir_fluxos(conn, "00000000000000", ["cross"])

    def test_definir_fluxos_grava(self):
        conn = _banco()
        fe.definir_fluxos(conn, "66259821000155", ["cross", "armazenagem"])
        valor = conn.execute("SELECT fluxos FROM interno WHERE cnpj_embarcador='66259821000155'").fetchone()[0]
        self.assertEqual(fe.normalizar_fluxos(valor), ["armazenagem", "cross"])


class ResolverXd(unittest.TestCase):
    def test_resolver_xd_unico(self):
        conn = _banco()
        conn.executemany("INSERT INTO wms_produtos VALUES (?,?,?)", [("XDDOURADO", 18, 1), ("QJ-01", 18, 1)])
        self.assertEqual(fe.resolver_codigo_xd(conn, 18, "DOURADO"), ("XDDOURADO", None))

    def test_resolver_xd_varios_casa_apelido(self):
        conn = _banco()
        conn.executemany("INSERT INTO wms_produtos VALUES (?,?,?)", [("XDQE", 98, 1), ("XDQUATROESTRELAS", 98, 1)])
        self.assertEqual(fe.resolver_codigo_xd(conn, 98, "QUATRO ESTRELAS"), ("XDQUATROESTRELAS", None))

    def test_resolver_xd_varios_sem_casamento_erro(self):
        conn = _banco()
        conn.executemany("INSERT INTO wms_produtos VALUES (?,?,?)", [("XDA", 18, 1), ("XDB", 18, 1)])
        codigo, erro = fe.resolver_codigo_xd(conn, 18, "DOURADO")
        self.assertIsNone(codigo)
        self.assertTrue(erro.startswith("mais de um produto XD no catálogo"), erro)
        self.assertIn("XDA", erro)

    def test_resolver_xd_nenhum(self):
        conn = _banco()
        conn.executemany("INSERT INTO wms_produtos VALUES (?,?,?)", [("XDDOURADO", 18, 0), ("XDOUTRO", 77, 1)])
        self.assertEqual(fe.resolver_codigo_xd(conn, 18, "DOURADO"),
                         (None, "nenhum produto XD no catálogo do embarcador"))


class NomesTrocados(unittest.TestCase):
    """Em produção o "apelido" muitas vezes é a razão social e o nome curto
    está em nome_remetente (ex. Dourado, stkkc 18)."""
    def _banco(self):
        conn = _banco()
        conn.execute("UPDATE interno SET apelido = 'LATICINIOS DOURADO - INDUSTRIA E COMERCIO LTDA', "
                     "nome_remetente = 'DOURADO' WHERE stkkc_id = 18")
        return conn

    def test_varios_xd_casa_pelo_nome_curto(self):
        conn = self._banco()
        conn.executemany("INSERT INTO wms_produtos VALUES (?,?,?)", [("XDDOURADO", 18, 1), ("XDOUTRO", 18, 1)])
        fe.definir_fluxos(conn, "66259821000155", ["cross"])
        self.assertEqual(fe.fluxos_do_embarcador(conn, stkkc=18)["codigo_xd"], "XDDOURADO")

    def test_nome_item_usa_o_nome_mais_curto(self):
        conn = self._banco()
        self.assertEqual(fe.fluxos_do_embarcador(conn, stkkc=18)["nome_item"], "Mercadoria DOURADO")


class FluxosDoEmbarcador(unittest.TestCase):
    def test_fluxos_do_embarcador_por_stkkc_sem_cross_nao_resolve_xd(self):
        conn = _banco()
        conn.execute("INSERT INTO wms_produtos VALUES ('XDDOURADO', 18, 1)")
        r = fe.fluxos_do_embarcador(conn, stkkc=18)
        self.assertEqual(r["fluxos"], ["armazenagem"])
        self.assertIsNone(r["codigo_xd"])
        self.assertIsNone(r["erro_xd"])
        self.assertEqual(r["cnpj"], "66259821000155")

    def test_fluxos_do_embarcador_cross_traz_xd_e_nome_item(self):
        conn = _banco()
        conn.execute("INSERT INTO wms_produtos VALUES ('XDDOURADO', 18, 1)")
        fe.definir_fluxos(conn, "66259821000155", ["cross"])
        r = fe.fluxos_do_embarcador(conn, cnpj="66.259.821/0001-55")
        self.assertEqual((r["fluxos"], r["codigo_xd"], r["erro_xd"]), (["cross"], "XDDOURADO", None))
        self.assertEqual(r["nome_item"], "Mercadoria DOURADO")
        self.assertEqual(r["stkkc"], 18)

    def test_fluxos_do_embarcador_inexistente_levanta(self):
        with self.assertRaises(ValueError):
            fe.fluxos_do_embarcador(_banco(), stkkc=4242)


class Carga(unittest.TestCase):
    def test_carga_json_full_cross_em_outro(self):
        docs = [
            {"cnpj": "35539067000183", "armazenagem": False, "cross": False, "outro": "Full cross"},
            {"cnpj": "23155000000166", "armazenagem": True, "cross": True, "outro": ""},
            {"cnpj": "29920346000190", "armazenagem": True, "cross": False, "outro": ""},
            {"cnpj": "99999999000199", "armazenagem": False, "cross": False, "outro": ""},
        ]
        self.assertEqual(fe.carga_de_marcacoes(docs), {
            "35539067000183": ["full_cross"],
            "23155000000166": ["armazenagem", "cross"],
            "29920346000190": ["armazenagem"],
        })


if __name__ == "__main__":
    unittest.main()
