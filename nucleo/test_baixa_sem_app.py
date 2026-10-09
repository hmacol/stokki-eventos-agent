# -*- coding: utf-8 -*-
"""Testes de nucleo/baixa_sem_app.py (banco temporario, Vuupt falsa). Rodar da raiz:
py -3.11 -m unittest nucleo.test_baixa_sem_app"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from nucleo import baixa_sem_app as b
from nucleo import banco

HOJE = date(2026, 10, 9)


class VuuptFalsa:
    def __init__(self, status):
        self.status = dict(status)          # service_id -> status
        self.concluidos = []

    def buscar_servico_por_id(self, sid):
        if self.status[sid] == "sem_resposta":
            return None
        return {"id": sid, "status": self.status[sid], "route_id": 77}

    def concluir_como_agente(self, sid, sucesso=True, failed_reason_id=None, status_atual=""):
        if self.status[sid] == "erro":
            raise RuntimeError("409 conflito")
        self.concluidos.append((sid, sucesso, failed_reason_id))
        self.status[sid] = "done"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = banco.conectar(self.db)
        self.addCleanup(self.conn.close)

    def rota(self, data_rota, agent_id, situacoes, status="PLANEJADA", vuupt_id=5000):
        cur = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, nome, provedor, vuupt_route_id, agent_id, motorista_nome, status) "
            "VALUES (?, 'Rota X', 'VUUPT', ?, ?, 'Iago', ?)", (data_rota, vuupt_id, agent_id, status))
        for i, s in enumerate(situacoes):
            self.conn.execute("INSERT INTO nucleo_paradas (rota_id, ordem, codigo, situacao) VALUES (?, ?, ?, ?)",
                              (cur.lastrowid, i, f"PS-{i}", s))
        self.conn.commit()


class Torre(Base):
    def test_rota_de_ontem_com_pendente_aparece(self):
        self.rota("2026-10-08", 50191, ["PENDENTE", "PENDENTE", "ENTREGUE"])
        itens = b.excecoes_torre("2026-10-09", db_path=self.db, hoje=HOJE, sem_app={50191})
        self.assertEqual([x["id"] for x in itens], ["semapp:5000"])
        self.assertIn("2 pedido(s)", itens[0]["descricao"])
        self.assertEqual(itens[0]["acao"]["url"], "/baixa-sem-app?rota=5000")

    def test_filtros(self):
        self.rota("2026-10-09", 50191, ["PENDENTE"], vuupt_id=1)                       # hoje
        self.rota("2026-10-08", 50191, ["ENTREGUE"], vuupt_id=2)                       # nada pendente
        self.rota("2026-10-08", 50191, ["PENDENTE"], status="CANCELADA", vuupt_id=3)   # cancelada
        self.rota("2026-10-08", 999, ["PENDENTE"], vuupt_id=4)                         # motorista com app
        self.rota("2026-10-08", 50191, ["PENDENTE"], vuupt_id=None)                    # sem rota na Vuupt
        self.assertEqual(b.excecoes_torre("2026-10-09", db_path=self.db, hoje=HOJE, sem_app={50191}), [])

    def test_sem_marcados_nao_gera_nada(self):
        self.rota("2026-10-08", 50191, ["PENDENTE"])
        self.assertEqual(b.excecoes_torre("2026-10-09", db_path=self.db, hoje=HOJE, sem_app=set()), [])


class Lote(Base):
    def itens(self):
        return [{"service_id": 1, "codigo": "PS-1", "entregue": True, "failed_reason_id": None},
                {"service_id": 2, "codigo": "PS-2", "entregue": False, "failed_reason_id": 5433},
                {"service_id": 3, "codigo": "PS-3", "entregue": True, "failed_reason_id": None}]

    def executar(self, vuupt, lote):
        with mock.patch.object(b, "_liberar_rota_agendada"), \
             mock.patch.object(b, "_registrar_tratativa"), \
             mock.patch.object(b, "_ressincronizar"):
            return b.executar_lote(lote, vuupt, db_path=self.db)

    def test_entregue_e_insucesso_com_motivo(self):
        vuupt = VuuptFalsa({1: "assigned", 2: "assigned", 3: "done"})
        lote = b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens())
        r = self.executar(vuupt, lote)
        self.assertEqual(vuupt.concluidos, [(1, True, None), (2, False, 5433)])
        self.assertEqual([i["status"] for i in r["itens"]], ["entregue", "insucesso", "ja_fechado"])
        self.assertTrue(r["terminado"])

    def test_repetir_nao_reconclui(self):
        vuupt = VuuptFalsa({1: "assigned", 2: "assigned", 3: "assigned"})
        self.executar(vuupt, b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens()))
        r = self.executar(vuupt, b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens()))
        self.assertEqual(len(vuupt.concluidos), 3)
        self.assertEqual({i["status"] for i in r["itens"]}, {"ja_fechado"})

    def test_erro_num_item_nao_para_os_outros(self):
        vuupt = VuuptFalsa({1: "erro", 2: "assigned", 3: "assigned"})
        r = self.executar(vuupt, b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens()))
        self.assertEqual([i["status"] for i in r["itens"]], ["erro", "insucesso", "entregue"])
        self.assertIn("409", r["itens"][0]["erro"])
        self.assertEqual(b.ler_lote(self.conn, r["id"])["itens"][0]["status"], "erro")

    def test_vuupt_sem_resposta_vira_erro_sem_escrever(self):
        vuupt = VuuptFalsa({1: "sem_resposta", 2: "assigned", 3: "assigned"})
        vuupt.atribuir_agente = mock.Mock()
        r = self.executar(vuupt, b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens()))
        self.assertEqual(r["itens"][0]["status"], "erro")
        vuupt.atribuir_agente.assert_not_called()
        self.assertNotIn(1, [c[0] for c in vuupt.concluidos])

    def test_lote_em_andamento(self):
        self.assertFalse(b.lote_em_andamento(self.conn, 5000))
        lote = b.criar_lote(self.conn, 5000, 50191, "hugo", self.itens())
        self.assertTrue(b.lote_em_andamento(self.conn, 5000))
        self.assertFalse(b.lote_em_andamento(self.conn, 6000))
        self.conn.execute("UPDATE baixas_sem_app SET criado_em = '2026-01-01 00:00:00' WHERE id = ?", (lote,))
        self.conn.commit()
        self.assertFalse(b.lote_em_andamento(self.conn, 5000))   # lote velho/morto libera nova tentativa


if __name__ == "__main__":
    unittest.main()
