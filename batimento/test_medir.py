# -*- coding: utf-8 -*-
"""Testes de batimento/medir.py sem rede. Rodar da raiz:
py -3.11 -m unittest batimento.test_medir"""
import unittest
from unittest import mock

from batimento import medir


class _Resp:
    def __init__(self, status, texto=""):
        self.status_code, self.text = status, texto

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _pagina(situacao):
    return ('<tr>\n <th style="width:280px">Situação:</th>\n <td>\n <span class="badge">'
            f'<i class="fa fa-x"></i>{situacao}</span>\n </td></tr>')


class _Sessao:
    def __init__(self, por_id):
        self.por_id = por_id

    def get(self, url):
        return self.por_id[int(url.rsplit("/", 1)[1])]


class CobrirFaixa(unittest.TestCase):
    @mock.patch.object(medir.time, "sleep", lambda s: None)
    def test_faltantes_vao_pro_show(self):
        sessao = _Sessao({11: _Resp(200, _pagina("Importação")), 12: _Resp(500, "Server Error"),
                          13: _Resp(200, _pagina("Devolvido"))})
        linhas = [{"codigo": "PS-10"}, {"codigo": "PS-14"}]
        cob = medir.cobrir_faixa(sessao, 10, {10, 14}, linhas)
        self.assertEqual(cob["importacao"], [11])
        self.assertEqual(cob["inexistentes"], [12])
        self.assertEqual((cob["listados"], cob["pelo_show"], cob["faixa"]), (2, 1, "10..14"))
        self.assertEqual(linhas[-1]["codigo"], "PS-13")
        self.assertEqual(linhas[-1]["status_stokki_bruto"], "Devolvido")

    def test_sem_ids(self):
        self.assertIsNone(medir.cobrir_faixa(_Sessao({}), 10, set(), [])["faixa"])


class IdDeCorte(unittest.TestCase):
    def test_maior_ps_antes_do_corte_mais_um(self):
        import sqlite3
        from datetime import date
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE nucleo_pedidos (codigo TEXT, criado_em_provedor TEXT)")
        conn.executemany("INSERT INTO nucleo_pedidos VALUES (?, ?)", [
            ("PS-100", "2026-09-27 10:00:00"), ("PS-200", "2026-09-27 18:00:00"),
            ("PS-50", "2026-09-29 08:00:00"),      # antigo reimportado depois do corte
            ("PS-201", "2026-09-28 08:00:00")])
        self.assertEqual(medir.id_de_corte(conn, date(2026, 9, 28)), 201)


class CanhotoPainel(unittest.TestCase):
    def test_canhoto_manual_vira_fonte_painel(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE nucleo_pedidos (codigo TEXT, status TEXT, fluxo TEXT, status_provedor TEXT, "
                     "status_done_provedor TEXT, excluido_em TEXT, vuupt_route_id INTEGER, qtd_checklists INTEGER, "
                     "criado_em_provedor TEXT)")
        conn.execute("CREATE TABLE canhotos_manuais (codigo TEXT PRIMARY KEY, service_id INTEGER, caminho TEXT, "
                     "caminho_gcs TEXT, enviado_por TEXT, enviado_em TEXT, origem TEXT)")
        conn.execute("INSERT INTO canhotos_manuais (codigo, caminho, enviado_em) VALUES ('PS-1', 'x', 'y')")
        banco = medir.ler_banco(conn, {"PS-1"}, 0)
        fato = medir.montar_fato({"codigo": "PS-1", "transportadora": ""}, banco["PS-1"], None, None)
        self.assertEqual(fato["canhoto_fonte"], "painel")


if __name__ == "__main__":
    unittest.main()
