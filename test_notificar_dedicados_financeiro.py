# -*- coding: utf-8 -*-
"""E-mail quinzenal do financeiro com os pedidos dedicados (Hugo, 23/09/2026).

    py -3.11 -m unittest test_notificar_dedicados_financeiro
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import pedidos_dedicados as pd  # noqa: E402
import notificar_dedicados_financeiro as nf  # noqa: E402


def _conn():
    conn = pd.conectar(":memory:")
    for cod, nome, valor, quando, nfe in [("PS-1", "ACME", 33.34, "2026-09-03 10:00:00", "10"),
                                          ("PS-2", "ACME", 33.33, "2026-09-03 10:00:00", "11"),
                                          ("PS-3", "Beta", 200, "2026-09-14 10:00:00", None),
                                          ("PS-4", "Beta", 5, "2026-09-16 00:00:00", None)]:
        conn.execute("INSERT INTO pedidos_dedicados (codigo_pedido, remetente_nome, numero_nf, valor, grupo_id, valor_total_grupo, marcado_em, marcado_por) "
                     "VALUES (?,?,?,?,'g',0,?,'a')", (cod, nome, nfe, valor, quando))
    conn.commit()
    return conn


class MontarEmail(unittest.TestCase):
    def test_subtotais_e_total(self):
        _, _, linhas = pd.listar_quinzena(_conn(), date(2026, 9, 16))
        assunto, html = nf.montar_email(date(2026, 9, 1), date(2026, 9, 15), linhas, None)
        self.assertIn("01/09", assunto)
        self.assertIn("15/09/2026", assunto)
        for trecho in ("ACME", "PS-1", "NF 10", "R$ 66,67", "Beta", "R$ 200,00", "R$ 266,67"):
            self.assertIn(trecho, html, trecho)
        self.assertNotIn("PS-4", html)
        self.assertNotIn("Redirecionado", html)

    def test_vazio(self):
        _, html = nf.montar_email(date(2026, 9, 1), date(2026, 9, 15), [], None)
        self.assertIn("Nenhum pedido dedicado", html)

    def test_aviso_de_redirecionamento(self):
        _, html = nf.montar_email(date(2026, 9, 1), date(2026, 9, 15), [], "financeiro@x")
        self.assertIn("Redirecionado", html)
        self.assertIn("financeiro@x", html)


class Executar(unittest.TestCase):
    def test_redireciona_por_padrao(self):
        with mock.patch.object(nf, "enviar_email", return_value=True) as env:
            r = nf.executar({"email": {}}, date(2026, 9, 16), modo_teste=False, conn=_conn())
        self.assertEqual(env.call_args.args[0], ["hugo@freshlogbr.com"])
        self.assertEqual(r["pedidos"], 3)
        self.assertTrue(r["enviado"])
        self.assertIn("financeiro@freshlogbr.com", env.call_args.args[2])  # aviso de destino real

    def test_envio_real_com_forcar_vazio(self):
        with mock.patch.object(nf, "enviar_email", return_value=True) as env:
            nf.executar({"email": {}, "financeiro": {"forcar_destino": "", "email": "fin@x"}}, date(2026, 9, 16), modo_teste=False, conn=_conn())
        self.assertEqual(env.call_args.args[0], ["fin@x"])
        self.assertNotIn("Redirecionado", env.call_args.args[2])

    def test_modo_teste_sempre_pro_hugo(self):
        with mock.patch.object(nf, "enviar_email", return_value=True) as env:
            nf.executar({"email": {}, "financeiro": {"forcar_destino": "", "email": "fin@x"}}, date(2026, 9, 16), modo_teste=True, conn=_conn())
        self.assertEqual(env.call_args.args[0], ["hugo@freshlogbr.com"])
        self.assertTrue(env.call_args.args[1].startswith("[MODO TESTE]"))

    def test_desligado(self):
        with mock.patch.object(nf, "enviar_email") as env:
            r = nf.executar({"financeiro": {"ativo": False}}, date(2026, 9, 16), modo_teste=False, conn=_conn())
        env.assert_not_called()
        self.assertEqual(r["desativado"], "financeiro.ativo=false")

    def test_data_ref_dia_1_bissexto(self):
        with mock.patch.object(nf, "enviar_email", return_value=True):
            r = nf.executar({"email": {}}, date(2028, 3, 1), modo_teste=True, conn=_conn())
        self.assertEqual((r["inicio"], r["fim"]), ("2028-02-16", "2028-02-29"))


if __name__ == "__main__":
    unittest.main()
