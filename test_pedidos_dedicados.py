# -*- coding: utf-8 -*-
"""Testes do modulo pedidos_dedicados (Hugo, 23/09/2026).

    py -3.11 -m unittest test_pedidos_dedicados
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pedidos_dedicados as pd  # noqa: E402


def _conn():
    return pd.conectar(":memory:")


class Normalizacao(unittest.TestCase):
    def test_formatos(self):
        for bruto, esperado in [("#PS-1234", "PS-1234"), ("ps 1234", "PS-1234"), ("PS-1234-R2", "PS-1234"),
                                ("PS1234", "PS-1234"), ("  #PS-99999 ", "PS-99999"), ("NF123", "NF123"), (None, "")]:
            self.assertEqual(pd.normalizar_codigo(bruto), esperado, bruto)

    def test_codigos_do_servico(self):
        self.assertEqual(pd.codigos_do_servico({"code": "#PS-37189, PS-37176-R1"}), ["PS-37189", "PS-37176"])
        self.assertEqual(pd.codigos_do_servico({}), [])


class Divisao(unittest.TestCase):
    def test_sobra_no_primeiro(self):
        self.assertEqual(pd.dividir_valor(100, 3), [33.34, 33.33, 33.33])

    def test_um_pedido(self):
        self.assertEqual(pd.dividir_valor(150.5, 1), [150.5])

    def test_centavo(self):
        self.assertEqual(pd.dividir_valor(0.01, 2), [0.01, 0.0])

    def test_invalido(self):
        with self.assertRaises(ValueError):
            pd.dividir_valor(10, 0)
        with self.assertRaises(ValueError):
            pd.dividir_valor(-1, 1)


class Marcacao(unittest.TestCase):
    def test_marcar_divide_e_agrupa(self):
        conn = _conn()
        grupo = pd.marcar(conn, [{"codigo_pedido": "#PS-1", "sender_id": 7, "remetente_nome": "ACME", "numero_nf": "10"},
                                 {"codigo_pedido": "PS-2", "sender_id": 7, "remetente_nome": "ACME"},
                                 {"envio_id": 55, "sender_id": 8, "remetente_nome": "Beta"}], 100, "hugo")
        rows = conn.execute("SELECT * FROM pedidos_dedicados ORDER BY id").fetchall()
        self.assertEqual([r["valor"] for r in rows], [33.34, 33.33, 33.33])
        self.assertEqual({r["grupo_id"] for r in rows}, {grupo})
        self.assertEqual(rows[0]["codigo_pedido"], "PS-1")
        self.assertEqual(rows[0]["valor_total_grupo"], 100)
        self.assertEqual(rows[2]["envio_id"], 55)
        self.assertIsNone(rows[2]["codigo_pedido"])

    def test_marcar_de_novo_atualiza(self):
        conn = _conn()
        pd.marcar(conn, [{"codigo_pedido": "PS-1"}], 50, "a")
        pd.marcar(conn, [{"codigo_pedido": "ps-1"}], 80, "b")
        rows = conn.execute("SELECT valor, marcado_por FROM pedidos_dedicados WHERE removido_em IS NULL").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["valor"], rows[0]["marcado_por"]), (80, "b"))

    def test_marcar_de_novo_mantem_marcado_em_original(self):
        """Revisao 23/09: remarcar (corrigir valor) nao pode mudar a quinzena."""
        conn = _conn()
        pd.marcar(conn, [{"codigo_pedido": "PS-1"}], 50, "a")
        conn.execute("UPDATE pedidos_dedicados SET marcado_em = '2026-09-14 10:00:00'")
        conn.commit()
        pd.marcar(conn, [{"codigo_pedido": "PS-1"}], 80, "b")
        r = conn.execute("SELECT marcado_em, valor FROM pedidos_dedicados WHERE removido_em IS NULL").fetchone()
        self.assertEqual((r["marcado_em"], r["valor"]), ("2026-09-14 10:00:00", 80))

    def test_remover_e_ativos(self):
        conn = _conn()
        pd.marcar(conn, [{"codigo_pedido": "PS-1"}, {"codigo_pedido": "PS-2"}], 10, "a")
        self.assertEqual(pd.remover(conn, codigo_pedido="#PS-1", por="b"), 1)
        ativos = pd.ativos_por_codigo(conn)
        self.assertEqual(list(ativos), ["PS-2"])
        self.assertEqual(ativos["PS-2"]["valor"], 5.0)

    def test_vincular_codigo(self):
        conn = _conn()
        pd.marcar(conn, [{"envio_id": 9}], 30, "a")
        pd.vincular_codigo(conn, 9, "PS-777")
        self.assertIn("PS-777", pd.ativos_por_codigo(conn))
        self.assertEqual(pd.ativo_por_envio(conn, 9)["codigo_pedido"], "PS-777")

    def test_marcar_vazio(self):
        with self.assertRaises(ValueError):
            pd.marcar(_conn(), [], 10, "a")


class Filtro(unittest.TestCase):
    def test_servico_com_dois_codigos_sai_inteiro(self):
        ativos = {"PS-2": {"valor": 1}}
        servicos = [{"id": 1, "code": "#PS-1"}, {"id": 2, "code": "#PS-2, PS-3"}]
        restantes, dedicados = pd.filtrar_dedicados(servicos, ativos)
        self.assertEqual([s["id"] for s in restantes], [1])
        self.assertEqual([s["id"] for s in dedicados], [2])


class Quinzena(unittest.TestCase):
    def _semear(self, conn, quando, codigo):
        conn.execute("INSERT INTO pedidos_dedicados (codigo_pedido, valor, grupo_id, valor_total_grupo, marcado_em, marcado_por) "
                     "VALUES (?, 1, 'g', 1, ?, 'a')", (codigo, quando))

    def test_dia_16(self):
        conn = _conn()
        self._semear(conn, "2026-09-01 00:00:00", "PS-1")
        self._semear(conn, "2026-09-15 23:59:59", "PS-2")
        self._semear(conn, "2026-09-16 00:00:00", "PS-3")
        ini, fim, linhas = pd.listar_quinzena(conn, date(2026, 9, 16))
        self.assertEqual((ini, fim), (date(2026, 9, 1), date(2026, 9, 15)))
        self.assertEqual([l["codigo_pedido"] for l in linhas], ["PS-1", "PS-2"])

    def test_dia_1_mes_anterior_bissexto(self):
        conn = _conn()
        self._semear(conn, "2028-02-29 10:00:00", "PS-1")
        self._semear(conn, "2028-02-15 10:00:00", "PS-2")
        ini, fim, linhas = pd.listar_quinzena(conn, date(2028, 3, 1))
        self.assertEqual((ini, fim), (date(2028, 2, 16), date(2028, 2, 29)))
        self.assertEqual([l["codigo_pedido"] for l in linhas], ["PS-1"])

    def test_outro_dia_usa_quinzena_fechada_anterior(self):
        ini, fim, _ = pd.listar_quinzena(_conn(), date(2026, 9, 23))
        self.assertEqual((ini, fim), (date(2026, 9, 1), date(2026, 9, 15)))
        ini, fim, _ = pd.listar_quinzena(_conn(), date(2026, 9, 10))
        self.assertEqual((ini, fim), (date(2026, 8, 16), date(2026, 8, 31)))

    def test_removido_fica_fora(self):
        conn = _conn()
        self._semear(conn, "2026-09-05 10:00:00", "PS-1")
        conn.execute("UPDATE pedidos_dedicados SET removido_em = '2026-09-06 00:00:00'")
        self.assertEqual(pd.listar_quinzena(conn, date(2026, 9, 16))[2], [])


if __name__ == "__main__":
    unittest.main()
