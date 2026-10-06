# -*- coding: utf-8 -*-
"""
Pedido com cancelamento solicitado nao pode continuar cobrando agendamento
(Hugo, 06/10/2026, caso PS-39959 da Maria Dolores).

    py -3.11 -m unittest portal_cliente.test_cancelamento_agendamento
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

_COLUNAS = ("id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT, atualizado_em TEXT, "
            "criado_stokki_em TEXT, emitida_em TEXT, numero_nf TEXT, referencia TEXT, destinatario_doc TEXT, "
            "destinatario_nome TEXT, destinatario_endereco TEXT, destinatario_bairro TEXT, destinatario_municipio TEXT, "
            "codigo_pedido TEXT, requer_agendamento INTEGER, agendamento_pendente INTEGER, agendamento_data TEXT, "
            "data_expedicao TEXT, xml_path TEXT, itens_json TEXT")


def _conn(cancelamento_pendente: bool):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(f"CREATE TABLE portal_envios ({_COLUNAS});"
                       "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);")
    conn.execute("INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, codigo_pedido, requer_agendamento, agendamento_pendente) "
                 "VALUES (50, '111', ?, datetime('now','localtime'), '9959', 'PS-39959', 1, 1)", (ep.STATUS_CRIADO,))
    if cancelamento_pendente:
        conn.execute("INSERT INTO portal_solicitacoes (envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em) "
                     "VALUES (50, '111', 'cancelar', '', 'PENDENTE', 'cliente', '2026-09-23 15:47:30')")
    conn.commit()
    return conn


class CancelamentoSolicitado(unittest.TestCase):
    def test_sem_cancelamento_continua_pedindo_agendamento(self):
        envios = ep.listar_envios(_conn(False), "111")
        self.assertTrue(envios[0]["agendamento_pendente"])
        self.assertTrue(envios[0]["pode_reagendar"])
        self.assertEqual(ep.resumo_envios(envios)["agendamento_pendente"], 1)

    def test_com_cancelamento_pendente_para_de_pedir_agendamento(self):
        envios = ep.listar_envios(_conn(True), "111")
        self.assertFalse(envios[0]["agendamento_pendente"])
        self.assertFalse(envios[0]["pode_reagendar"])
        self.assertEqual(ep.resumo_envios(envios)["agendamento_pendente"], 0)
        # a pendencia que fica e a do cancelamento, aguardando a Fresh Log
        self.assertEqual([s["tipo"] for s in envios[0]["solicitacoes_pendentes"]], ["cancelar"])


if __name__ == "__main__":
    unittest.main()
