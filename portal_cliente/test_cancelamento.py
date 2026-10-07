# -*- coding: utf-8 -*-
"""
Cancelamento de pedido pelo portal (DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md).

    py -3.11 -m unittest portal_cliente.test_cancelamento
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

COLUNAS_ENVIO = ("id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT, atualizado_em TEXT, "
                 "criado_stokki_em TEXT, emitida_em TEXT, numero_nf TEXT, referencia TEXT, destinatario_doc TEXT, "
                 "destinatario_nome TEXT, destinatario_endereco TEXT, destinatario_bairro TEXT, destinatario_municipio TEXT, "
                 "codigo_pedido TEXT, requer_agendamento INTEGER, agendamento_pendente INTEGER, agendamento_data TEXT, "
                 "data_expedicao TEXT, xml_path TEXT, itens_json TEXT, erro TEXT, bloqueio_motivo TEXT")


def conn_portal(envios=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(f"CREATE TABLE portal_envios ({COLUNAS_ENVIO});"
                       "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);")
    for e in envios:
        conn.execute("INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, codigo_pedido, requer_agendamento, agendamento_pendente, destinatario_nome) "
                     "VALUES (?, '111', ?, datetime('now','localtime'), ?, ?, 1, 1, 'DESTINO LTDA')", e)
    conn.commit()
    return conn


def envio(conn, id_):
    return dict(conn.execute("SELECT * FROM portal_envios WHERE id = ?", (id_,)).fetchone())


class AplicarAcaoCancelar(unittest.TestCase):
    def test_criado_vira_cancelando_com_solicitacao_pendente(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        r = ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {"motivo": "cliente desistiu"}, "cliente")
        self.assertEqual((r["aplicado"], r["precisa_operacao"], r["em_andamento"]), (False, False, True))
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELANDO)
        s = conn.execute("SELECT tipo, status, detalhes FROM portal_solicitacoes").fetchall()
        self.assertEqual([tuple(x) for x in s], [("cancelar", "PENDENTE", "cliente desistiu")])

    def test_duplicado_tambem_vira_cancelando(self):
        conn = conn_portal([(51, ep.STATUS_DUPLICADO, "9960", "PS-39960")])
        ep.aplicar_acao(conn, envio(conn, 51), "cancelar", {}, "cliente")
        self.assertEqual(envio(conn, 51)["status"], ep.STATUS_CANCELANDO)

    def test_cancelar_duas_vezes_recusa(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM portal_solicitacoes").fetchone()[0], 1)

    def test_na_fila_continua_cancelando_na_hora(self):
        conn = conn_portal([(52, ep.STATUS_NA_FILA, "9961", None)])
        r = ep.aplicar_acao(conn, envio(conn, 52), "cancelar", {}, "cliente")
        self.assertTrue(r["aplicado"])
        self.assertEqual(envio(conn, 52)["status"], ep.STATUS_CANCELADO)


class LinhaCancelando(unittest.TestCase):
    def test_cancelando_nao_oferece_acoes_nem_cobra_agendamento(self):
        conn = conn_portal([(50, ep.STATUS_CANCELANDO, "9959", "PS-39959")])
        conn.execute("INSERT INTO portal_solicitacoes (envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em) "
                     "VALUES (50, '111', 'cancelar', '', 'PENDENTE', 'cliente', '2026-10-06 10:00:00')")
        conn.commit()
        l = ep.listar_envios(conn, "111")[0]
        self.assertEqual(l["status_rotulo"], "Cancelando")
        self.assertFalse(l["pode_cancelar"] or l["pode_reagendar"] or l["pode_em_espera"] or l["pode_reenviar"])
        self.assertFalse(l["agendamento_pendente"])
        self.assertEqual(ep.resumo_envios([l])["agendamento_pendente"], 0)


class ConcluirSolicitacao(unittest.TestCase):
    def test_concluir_cancelamento_marca_envio_cancelado(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        conn.execute("UPDATE portal_envios SET status = ? WHERE id = 50", (ep.STATUS_CRIADO,))  # worker devolveu pra operacao
        sid = conn.execute("SELECT id FROM portal_solicitacoes").fetchone()[0]
        ep.concluir_solicitacao(conn, sid, "cancelado na Stokki pela equipe")
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELADO)
        self.assertEqual(tuple(conn.execute("SELECT status, resposta FROM portal_solicitacoes").fetchone()), ("CONCLUIDA", "cancelado na Stokki pela equipe"))

    def test_recusar_cancelamento_nao_mexe_no_envio(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        conn.execute("UPDATE portal_envios SET status = ? WHERE id = 50", (ep.STATUS_CRIADO,))
        sid = conn.execute("SELECT id FROM portal_solicitacoes").fetchone()[0]
        ep.concluir_solicitacao(conn, sid, "ja entregue", recusada=True)
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)

    def test_buscar_solicitacao_cancelamento(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        self.assertIsNone(ep.buscar_solicitacao_cancelamento(conn, 50))
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {"motivo": "x"}, "cliente")
        self.assertEqual(ep.buscar_solicitacao_cancelamento(conn, 50)["detalhes"], "x")


if __name__ == "__main__":
    unittest.main()
