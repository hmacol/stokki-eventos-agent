# -*- coding: utf-8 -*-
"""Testes da tabela agente_acoes. Rodar: py -3.11 -m unittest agente.test_banco"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agente import banco, regras  # noqa: E402


class TestAcoes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = banco.conectar(Path(self.tmp.name) / "t.db")
        self.acao = regras.Acao(regras.AVISAR, regras.AVISO_FALHA_ENTREGA, "PS-1", "vigia:INSUCESSO:PS-1:x",
                                False, {"codigo": "PS-1"})

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_registra_uma_vez_so(self):
        i1 = banco.registrar(self.conn, self.acao, banco.ENVIADA, destinatario="5511", texto="oi")
        i2 = banco.registrar(self.conn, self.acao, banco.ENVIADA, destinatario="5511", texto="oi")
        self.assertIsNotNone(i1)
        self.assertIsNone(i2)
        self.assertTrue(banco.ja_existe(self.conn, "PS-1", "vigia:INSUCESSO:PS-1:x", regras.AVISO_FALHA_ENTREGA))
        self.assertEqual(len(banco.listar(self.conn)), 1)

    def test_mesmo_pedido_outro_fato_entra(self):
        outra = regras.Acao(regras.AVISAR, regras.AVISO_FALHA_ENTREGA, "PS-1", "vigia:INSUCESSO:PS-1:y", False)
        banco.registrar(self.conn, self.acao, banco.ENVIADA)
        self.assertIsNotNone(banco.registrar(self.conn, outra, banco.ENVIADA))

    def test_status_e_listagem(self):
        prop = regras.Acao(regras.PROPOR, regras.PROPOSTA_EXPEDIR_STOKKI, "PS-2", "batimento:X:PS-2", True)
        pid = banco.registrar(self.conn, prop, banco.PROPOSTA, destinatario="torre", texto="expedir")
        self.assertEqual(banco.contar_propostas(self.conn), 1)
        banco.atualizar_status(self.conn, pid, banco.APROVADA, aprovado_por="hugo",
                               agora=datetime(2026, 10, 5, 9, 0))
        l = banco.listar(self.conn, status=banco.APROVADA)[0]
        self.assertEqual(l["aprovado_por"], "hugo")
        self.assertIsNone(l["executado_em"])
        self.assertEqual(banco.contar_propostas(self.conn), 0)
        banco.atualizar_status(self.conn, pid, banco.EXECUTADA, resultado="ok", executado=True,
                               agora=datetime(2026, 10, 5, 9, 5))
        l = banco.listar(self.conn, codigo="PS-2")[0]
        self.assertEqual(l["status"], banco.EXECUTADA)
        self.assertEqual(l["executado_em"], "2026-10-05 09:05:00")
        self.assertEqual(l["resultado"], "ok")
        self.assertEqual(l["dados"], {})

    def test_rodada(self):
        rid = banco.abrir_rodada(self.conn, modo_teste=True)
        banco.fechar_rodada(self.conn, rid, {"fatos": 3, "acoes_novas": 1, "enviadas": 1, "falhas": 0})
        r = self.conn.execute("SELECT * FROM agente_rodadas WHERE id = ?", (rid,)).fetchone()
        self.assertEqual((r["fatos"], r["acoes_novas"], r["modo_teste"]), (3, 1, 1))
        self.assertIsNotNone(r["concluida_em"])


if __name__ == "__main__":
    unittest.main()
