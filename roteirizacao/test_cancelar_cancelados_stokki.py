# -*- coding: utf-8 -*-
"""py -3.11 -m unittest roteirizacao.test_cancelar_cancelados_stokki"""
import sqlite3
import unittest
from unittest import mock

from roteirizacao import cancelar_cancelados_stokki as ccs


def _banco() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE galpao_pedidos (id_stokki INTEGER PRIMARY KEY, codigo TEXT, embarcador TEXT, situacao TEXT);
        CREATE TABLE nucleo_pedidos (codigo TEXT PRIMARY KEY, vuupt_service_id INTEGER, status TEXT,
                                     excluido_em TEXT);
    """)
    ccs.garantir_tabela(conn)
    return conn


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = _banco()
        self.vuupt = mock.Mock()
        self.status = {}
        self.vuupt.buscar_servico_por_id.side_effect = lambda sid: (
            {"id": sid, "status": self.status[sid]} if sid in self.status else None)
        self.rascunho = mock.patch.object(ccs.cs, "rascunho_do_servico", return_value=(None, None)).start()
        self.cancelar = mock.patch.object(ccs.cs, "cancelar_servico_completo",
                                          return_value={"ok": True, "ja_estava": False, "erro": ""}).start()
        self.addCleanup(mock.patch.stopall)

    def pedido(self, codigo, sid, nucleo="ABERTO", galpao="Cancelado", vuupt: str | None = "not_assigned",
               excluido=None):
        base = codigo.split("-R")[0]
        self.conn.execute("INSERT OR IGNORE INTO galpao_pedidos VALUES (?, ?, 'EMB', ?)",
                          (int(base[3:]), base, galpao))
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES (?, ?, ?, ?)", (codigo, sid, nucleo, excluido))
        if vuupt:
            self.status[sid] = vuupt

    def rodar(self, **kw):
        return ccs.processar(self.conn, self.vuupt, "tok", **kw)


class Candidatos(Base):
    def test_so_cancelado_no_galpao_e_vivo_no_nucleo(self):
        self.pedido("PS-1", 11)
        self.pedido("PS-2", 22, galpao="Aberto")
        self.pedido("PS-3", 33, nucleo="INSUCESSO")
        self.pedido("PS-4", 44, excluido="2026-11-18")
        self.pedido("PS-5-R1", 55, nucleo="EM_ROTA")
        self.assertEqual([c["codigo"] for c in ccs.candidatos(self.conn)], ["PS-1", "PS-5-R1"])

    def test_base_do_codigo(self):
        self.assertEqual(ccs.base_do_codigo("#PS-44486-R2"), "PS-44486")
        self.assertEqual(ccs.base_do_codigo("ps-1-C1"), "PS-1")
        self.assertIsNone(ccs.base_do_codigo("#PS-1, #PS-2"))
        self.assertIsNone(ccs.base_do_codigo("LANC-9"))


class Processar(Base):
    def test_no_pool_cancela_e_registra(self):
        self.pedido("PS-1", 11)
        r = self.rodar()
        self.cancelar.assert_called_once_with("tok", 11, vuupt=self.vuupt)
        self.assertEqual([i["codigo"] for i in r["cancelados"]], ["PS-1"])
        self.assertEqual(ccs._ja_registrado(self.conn, 11), ccs.CANCELADO)

    def test_em_rota_nao_cancela_e_avisa_uma_vez(self):
        self.pedido("PS-1", 11, nucleo="EM_ROTA", vuupt="assigned")
        r1 = self.rodar()
        r2 = self.rodar()
        self.cancelar.assert_not_called()
        self.assertEqual(len(r1["em_rota"]), 1)
        self.assertEqual(r2["em_rota"], [])

    def test_not_assigned_em_rascunho_enviado_ou_ofertado_e_rota(self):
        self.pedido("PS-1", 11)
        for st in ("ENVIADO", "OFERTADA"):
            self.conn.execute("DELETE FROM cancelados_stokki_vuupt")
            self.rascunho.return_value = (7, st)
            r = self.rodar()
            self.assertEqual(len(r["em_rota"]), 1, st)
        self.cancelar.assert_not_called()

    def test_not_assigned_em_rascunho_nao_enviado_cancela(self):
        self.pedido("PS-1", 11)
        self.rascunho.return_value = (7, "RASCUNHO")
        self.assertEqual(len(self.rodar()["cancelados"]), 1)

    def test_done_ou_cancelado_ou_sem_resposta_nao_faz_nada(self):
        self.pedido("PS-1", 11, vuupt="done")
        self.pedido("PS-2", 22, vuupt="canceled")
        self.pedido("PS-3", 33, vuupt=None)
        r = self.rodar()
        self.cancelar.assert_not_called()
        self.assertEqual((r["cancelados"], r["em_rota"], r["falhas"], r["vistos"]), ([], [], [], 3))

    def test_falha_avisa_uma_vez_e_tenta_de_novo(self):
        self.pedido("PS-1", 11)
        self.cancelar.return_value = {"ok": False, "ja_estava": False, "erro": "409"}
        r1 = self.rodar()
        r2 = self.rodar()
        self.assertEqual(self.cancelar.call_count, 2)
        self.assertEqual((len(r1["falhas"]), len(r2["falhas"])), (1, 0))
        self.cancelar.return_value = {"ok": True, "ja_estava": False, "erro": ""}
        self.assertEqual(len(self.rodar()["cancelados"]), 1)
        self.assertEqual(ccs._ja_registrado(self.conn, 11), ccs.CANCELADO)

    def test_saiu_da_rota_e_voltou_ao_pool_cancela(self):
        self.pedido("PS-1", 11, vuupt="assigned")
        self.rodar()
        self.status[11] = "not_assigned"
        self.assertEqual(len(self.rodar()["cancelados"]), 1)

    def test_modo_teste_nao_cancela_nem_grava(self):
        self.pedido("PS-1", 11)
        self.pedido("PS-2", 22, vuupt="assigned")
        r = self.rodar(modo_teste=True)
        self.cancelar.assert_not_called()
        self.assertEqual((len(r["cancelados"]), len(r["em_rota"])), (1, 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM cancelados_stokki_vuupt").fetchone()[0], 0)


class SemServicoNaVuupt(unittest.TestCase):
    """Fonte stokki: pedido do nucleo sem servico na Vuupt tambem fecha, so que LOCAL."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from nucleo import banco, pedidos
        self.banco, self.pedidos = banco, pedidos
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        mock.patch.object(banco, "DB_PATH", Path(tmp.name) / "t.db").start()
        self.addCleanup(mock.patch.stopall)
        self.conn = banco.conectar()
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE galpao_pedidos (id_stokki INTEGER PRIMARY KEY, codigo TEXT, "
                          "embarcador TEXT, situacao TEXT)")
        ccs.garantir_tabela(self.conn)
        self.vuupt = mock.Mock()
        self.rascunho = mock.patch.object(ccs.cs, "rascunho_do_servico", return_value=(None, None)).start()
        self.cancelar = mock.patch.object(ccs.cs, "cancelar_servico_completo").start()
        self.remover = mock.patch.object(ccs.cs.rascunhos_rota, "remover_parada").start()
        self.conn.execute("INSERT INTO galpao_pedidos VALUES (1, 'PS-1', 'EMB', 'Cancelado')")
        pedidos.upsert_pedido(self.conn, "PS-1", {"titulo": "x"}, banco.ORIGEM_PIPELINE)
        self.conn.commit()
        self.neg = -self.conn.execute("SELECT id_pool FROM nucleo_pedidos WHERE codigo='PS-1'").fetchone()[0]

    def _status(self):
        return self.conn.execute("SELECT status FROM nucleo_pedidos WHERE codigo='PS-1'").fetchone()[0]

    def test_chave_desligada_nao_e_candidato(self):
        self.assertEqual(ccs.candidatos(self.conn), [])
        r = ccs.processar(self.conn, self.vuupt, "tok")
        self.assertEqual(r["vistos"], 0)
        self.assertEqual(self._status(), "ABERTO")

    def test_cancelado_na_stokki_sem_servico_cancela_local(self):
        self.rascunho.return_value = (7, "RASCUNHO")
        r = ccs.processar(self.conn, self.vuupt, "tok", incluir_sem_servico=True)
        self.assertEqual([i["codigo"] for i in r["cancelados"]], ["PS-1"])
        self.assertEqual(self._status(), "CANCELADO")
        self.assertEqual(self.vuupt.mock_calls, [])
        self.cancelar.assert_not_called()
        self.rascunho.assert_called_once_with(self.neg)
        self.remover.assert_called_once_with(7, self.neg)
        self.assertEqual(ccs._ja_registrado(self.conn, self.neg), ccs.CANCELADO)
        # rodada seguinte: ja fechado, nao volta
        self.assertEqual(ccs.processar(self.conn, self.vuupt, "tok", incluir_sem_servico=True)["vistos"], 0)

    def test_sem_servico_em_rascunho_ofertado_so_avisa(self):
        self.rascunho.return_value = (7, "OFERTADA")
        r = ccs.processar(self.conn, self.vuupt, "tok", incluir_sem_servico=True)
        self.assertEqual(len(r["em_rota"]), 1)
        self.assertEqual(self._status(), "ABERTO")
        self.remover.assert_not_called()

    def test_modo_teste_nao_cancela(self):
        r = ccs.processar(self.conn, self.vuupt, "tok", modo_teste=True, incluir_sem_servico=True)
        self.assertEqual(len(r["cancelados"]), 1)
        self.assertEqual(self._status(), "ABERTO")
        self.assertEqual(self.vuupt.mock_calls, [])


class Email(unittest.TestCase):
    def test_sem_novidade_nao_manda(self):
        with mock.patch("email_utils.enviar_email") as env:
            self.assertFalse(ccs.avisar({}, {"cancelados": [], "em_rota": [], "falhas": [], "vistos": 3}))
        env.assert_not_called()

    def test_assunto_e_escape(self):
        item = {"codigo": "PS-1", "embarcador": "<b>X</b>", "service_id": 11, "detalhe": "status assigned"}
        assunto, corpo = ccs.montar_email({"cancelados": [item], "em_rota": [item], "falhas": []})
        self.assertEqual(assunto, "[Freshlog] Cancelados na Stokki: 1 cancelado(s) na Vuupt, 1 em rota")
        self.assertIn("&lt;b&gt;X&lt;/b&gt;", corpo)
        self.assertNotIn("<b>X</b>", corpo)


if __name__ == "__main__":
    unittest.main()
