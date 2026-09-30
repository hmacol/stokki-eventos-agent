# -*- coding: utf-8 -*-
"""
Testes do marcar_clientes_agenda.py.

Rodar: py -3.11 -m unittest test_marcar_clientes_agenda
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import openpyxl

import marcar_clientes_agenda as mca


class TestMarcarClientesAgenda(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        pasta = Path(self._tmp.name)
        self.planilha = pasta / "BD_CLIENTES.xlsx"
        self.banco = pasta / "dados.db"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Destinatário - Código", "Destinatário - Nome ", "ALERTAS CLIENTES"])
        ws.append([49749452000160, "VERO PANE", None])
        ws.append([21590391000111, "NUTRICAR", "AGENDAMENTO"])
        wb.save(self.planilha)

        conn = sqlite3.connect(self.banco)
        conn.execute(
            "CREATE TABLE agendamentos_pedido (pedido TEXT, cnpj_destinatario TEXT, "
            "nome_destinatario TEXT, status TEXT, data_agendada TEXT, origem TEXT)")
        conn.commit()
        conn.close()

        patch = mock.patch.object(mca, "DB_PATH", self.banco)
        patch.start()
        self.addCleanup(patch.stop)

    def _agendar(self, pedido, cnpj, nome, status="RESPONDIDO", data="30/09/2026", origem=None):
        conn = sqlite3.connect(self.banco)
        conn.execute("INSERT INTO agendamentos_pedido VALUES (?, ?, ?, ?, ?, ?)",
                     (pedido, cnpj, nome, status, data, origem))
        conn.commit()
        conn.close()

    def _alertas(self):
        wb = openpyxl.load_workbook(self.planilha, read_only=True, data_only=True)
        alertas = {str(l[0]): l[2] for l in wb.active.iter_rows(min_row=2, values_only=True)}
        wb.close()
        return alertas

    def _limpar_alerta(self, documento):
        wb = openpyxl.load_workbook(self.planilha)
        for linha in wb.active.iter_rows(min_row=2):
            if str(linha[0].value) == documento:
                linha[2].value = None
        wb.save(self.planilha)

    def test_so_entra_quem_tem_data_informada_e_documento(self):
        self._agendar("PS-1", "49.749.452/0001-60", "VERO PANE")
        self._agendar("PS-2", "59708718000180", "REAL BREAD", status="PENDENTE", data=None)
        self._agendar("PS-3", "", "", origem="PLANILHA_NUU")
        self._agendar("PS-4", "49749452000160", "VERO PANE PRODUTOS")
        self.assertEqual(list(mca.buscar_candidatos()), ["49749452000160"])

    def test_marca_na_planilha_quem_teve_data_informada(self):
        self._agendar("PS-1", "49749452000160", "VERO PANE")
        self._agendar("PS-2", "59708718000180", "REAL BREAD", origem="PLANILHA_NUU")
        resultado = mca.processar(self.planilha, modo_teste=False)
        self.assertEqual(resultado["marcados"], ["49749452000160"])
        self.assertEqual(resultado["incluidos"], ["59708718000180"])
        alertas = self._alertas()
        self.assertEqual(alertas["49749452000160"], "AGENDA")
        self.assertEqual(alertas["59708718000180"], "AGENDA")
        self.assertEqual(alertas["21590391000111"], "AGENDAMENTO")

    def test_quem_o_hugo_desmarcou_nao_volta(self):
        self._agendar("PS-1", "49749452000160", "VERO PANE")
        mca.processar(self.planilha, modo_teste=False)
        self._limpar_alerta("49749452000160")
        self._agendar("PS-9", "49749452000160", "VERO PANE")

        resultado = mca.processar(self.planilha, modo_teste=False)
        self.assertEqual(resultado["marcados"], [])
        self.assertIsNone(self._alertas()["49749452000160"])

    def test_quem_ja_estava_marcado_a_mao_tambem_pode_ser_desmarcado(self):
        self._agendar("PS-1", "21590391000111", "NUTRICAR")
        mca.processar(self.planilha, modo_teste=False)
        self._limpar_alerta("21590391000111")

        mca.processar(self.planilha, modo_teste=False)
        self.assertIsNone(self._alertas()["21590391000111"])

    def test_nao_marcar_tira_o_cliente_da_rotina(self):
        self._agendar("PS-1", "49749452000160", "VERO PANE")
        self._agendar("PS-2", "59708718000180", "REAL BREAD")
        self.assertEqual(mca.registrar_nao_marcar(["49.749.452/0001-60", "abc"]), ["49749452000160"])

        resultado = mca.processar(self.planilha, modo_teste=False)
        self.assertEqual(resultado["marcados"], [])
        self.assertEqual(resultado["incluidos"], ["59708718000180"])
        self.assertIsNone(self._alertas()["49749452000160"])

    def test_modo_teste_nao_grava_nada(self):
        self._agendar("PS-1", "49749452000160", "VERO PANE")
        resultado = mca.processar(self.planilha, modo_teste=True)
        self.assertEqual(resultado["marcados"], ["49749452000160"])
        self.assertIsNone(self._alertas()["49749452000160"])
        # nada registrado: a rodada de verdade ainda marca
        self.assertEqual(mca.processar(self.planilha, modo_teste=False)["marcados"], ["49749452000160"])


if __name__ == "__main__":
    unittest.main()
