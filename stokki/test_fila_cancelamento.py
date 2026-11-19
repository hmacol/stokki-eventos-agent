# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_fila_cancelamento"""
import sqlite3
import unittest
from unittest import mock

from stokki import fila_cancelamento as fc

OK = {"ok": True, "situacao": "Cancelado", "situacao_antes": "Aguardando Transportador", "ja_estava": False, "erro": ""}


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row   # igual ao worker e ao painel
        self.evento = mock.Mock()
        self.devolucao = mock.Mock()

    def processar(self, cancelar, **kw):
        return fc.processar(self.conn, "sessao", cancelar=cancelar, registrar_evento=self.evento,
                            registrar_devolucao=self.devolucao, **kw)

    def status(self, fila_id):
        return fc.andamento(self.conn, [fila_id])[0]


class TestEnfileirar(_Base):
    def test_um_por_ps_e_nao_duplica_o_que_esta_aberto(self):
        a = fc.enfileirar(self.conn, "PS-1, PS-2", [1, 2], "cliente desistiu do pedido", "valeria")
        self.assertEqual([(x["id_stokki"], x["novo"]) for x in a], [(1, True), (2, True)])
        b = fc.enfileirar(self.conn, "PS-1", [1], "outro motivo qualquer aqui", "hugo")
        self.assertEqual(b, [{"fila_id": a[0]["fila_id"], "id_stokki": 1, "novo": False}])
        self.assertEqual(fc.pendentes(self.conn), 2)

    def test_depois_de_terminar_pode_pedir_de_novo(self):
        a = fc.enfileirar(self.conn, "PS-1", [1], "cliente desistiu do pedido", "valeria")
        self.processar(mock.Mock(return_value=OK))
        b = fc.enfileirar(self.conn, "PS-1", [1], "cliente desistiu do pedido", "valeria")
        self.assertTrue(b[0]["novo"])
        self.assertNotEqual(a[0]["fila_id"], b[0]["fila_id"])


class TestProcessar(_Base):
    def test_ok_grava_situacao_registra_tratativa_e_devolucao(self):
        f = fc.enfileirar(self.conn, "#PS-1", [1], "cliente desistiu do pedido", "valeria")[0]
        canc = mock.Mock(return_value=OK)
        self.assertEqual(self.processar(canc), {"ok": 1, "erro": 0, "de_novo": 0})
        canc.assert_called_once_with("sessao", 1, "cliente desistiu do pedido", prefixo="Fresh Hub Pedidos (valeria)")
        st = self.status(f["fila_id"])
        self.assertEqual((st["status"], st["situacao"], st["ja_estava"]), ("OK", "Cancelado", False))
        self.devolucao.assert_called_once_with(1, False)
        args, kw = self.evento.call_args
        self.assertEqual(args, ("#PS-1", "PEDIDOS", "CANCELADO_STOKKI"))
        for trecho in ("valeria: ", "Aguardando Transportador → Cancelado", "cliente desistiu do pedido"):
            self.assertIn(trecho, kw["texto"])
        self.assertEqual(fc.pendentes(self.conn), 0)

    def test_ja_estava_cancelado(self):
        f = fc.enfileirar(self.conn, "PS-1", [1], "cliente desistiu do pedido", "valeria")[0]
        self.processar(mock.Mock(return_value={**OK, "situacao_antes": "Cancelado", "ja_estava": True}))
        self.assertTrue(self.status(f["fila_id"])["ja_estava"])
        self.devolucao.assert_called_once_with(1, True)
        self.assertIn("já estava cancelado", self.evento.call_args.kwargs["texto"])

    def test_recusa_da_stokki_vira_erro_na_hora(self):
        f = fc.enfileirar(self.conn, "PS-1", [1], "cliente desistiu do pedido", "valeria")[0]
        self.processar(mock.Mock(return_value={"ok": False, "situacao": "Enviado", "erro": "pedido ja expedido"}))
        st = self.status(f["fila_id"])
        self.assertEqual((st["status"], st["erro"]), ("ERRO", "pedido ja expedido"))
        self.devolucao.assert_not_called()
        self.assertEqual(self.evento.call_args.args[2], "CANCELAMENTO_STOKKI_FALHOU")

    def test_falha_de_rede_tenta_de_novo_ate_o_limite(self):
        f = fc.enfileirar(self.conn, "PS-1", [1], "cliente desistiu do pedido", "valeria")[0]
        canc = mock.Mock(side_effect=ConnectionError("caiu"))
        for _ in range(fc.MAX_TENTATIVAS - 1):
            self.assertEqual(self.processar(canc)["de_novo"], 1)
            self.assertEqual(self.status(f["fila_id"])["status"], "NA_FILA")
        self.evento.assert_not_called()
        self.assertEqual(self.processar(canc)["erro"], 1)
        st = self.status(f["fila_id"])
        self.assertEqual(st["status"], "ERRO")
        self.assertIn("caiu", st["erro"])
        self.assertEqual(canc.call_count, fc.MAX_TENTATIVAS)

    def test_um_que_falha_nao_para_os_outros_e_devolucao_que_levanta_nao_muda_nada(self):
        fila = fc.enfileirar(self.conn, "PS-1 PS-2", [1, 2], "cliente desistiu do pedido", "valeria")
        self.devolucao.side_effect = RuntimeError("banco")
        canc = mock.Mock(side_effect=lambda s, i, m, prefixo: OK if i == 2 else {"ok": False, "erro": "x"})
        self.assertEqual(self.processar(canc), {"ok": 1, "erro": 1, "de_novo": 0})
        self.assertEqual([x["status"] for x in fc.andamento(self.conn, [f["fila_id"] for f in fila])], ["ERRO", "OK"])

    def test_limite_por_ciclo(self):
        fc.enfileirar(self.conn, "", [1, 2, 3], "cliente desistiu do pedido", "valeria")
        self.processar(mock.Mock(return_value=OK), limite=2)
        self.assertEqual(fc.pendentes(self.conn), 1)

    def test_rodando_esquecido_volta_pra_fila(self):
        f = fc.enfileirar(self.conn, "PS-1", [1], "cliente desistiu do pedido", "valeria")[0]
        self.conn.execute("UPDATE stokki_cancelamentos_fila SET status = 'RODANDO', atualizado_em = '2026-01-01 00:00:00'")
        self.assertEqual(fc.pendentes(self.conn), 1)
        self.assertEqual(self.status(f["fila_id"])["status"], "NA_FILA")

    def test_rodando_recente_nao_e_mexido(self):
        f = fc.enfileirar(self.conn, "PS-1", [1], "cliente desistiu do pedido", "valeria")[0]
        self.conn.execute("UPDATE stokki_cancelamentos_fila SET status = 'RODANDO'")
        self.assertEqual(fc.pendentes(self.conn), 0)
        self.assertEqual(self.status(f["fila_id"])["status"], "RODANDO")


if __name__ == "__main__":
    unittest.main()
