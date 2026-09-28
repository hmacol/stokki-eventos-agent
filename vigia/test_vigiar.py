# -*- coding: utf-8 -*-
"""
Rodada completa do vigia num banco temporário. Rodar (da raiz):
    python -m unittest vigia.test_vigiar -v
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vigia import banco, vigiar

AGORA = datetime(2026, 9, 29, 10, 0)  # terça

ESQUEMA = """
CREATE TABLE nucleo_pedidos (codigo TEXT PRIMARY KEY, vuupt_service_id INTEGER, status TEXT,
    agendamento_inicio TEXT, vuupt_route_id INTEGER, criado_em_provedor TEXT, atualizado_em_provedor TEXT,
    criado_em TEXT, remetente_nome TEXT, destinatario_nome TEXT, fluxo TEXT, excluido_em TEXT,
    reentrega_de_service_id INTEGER);
CREATE TABLE nucleo_rotas (vuupt_route_id INTEGER, data_rota TEXT);
CREATE TABLE nucleo_paradas (service_id INTEGER, completed_at TEXT, motivo_texto TEXT);
CREATE TABLE rascunhos_rota (id INTEGER PRIMARY KEY, data_alvo TEXT, lote_id TEXT, nome TEXT,
    status TEXT, criado_em TEXT);
CREATE TABLE rascunhos_parada (rascunho_id INTEGER, service_id INTEGER);
CREATE TABLE insucessos_duplicados (service_id_original INTEGER, novo_code TEXT, cancelado_em TEXT);
"""


class TestRodada(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = banco.conectar(Path(self.tmp.name) / "t.db")
        self.conn.executescript(ESQUEMA)
        ped = lambda *a: self.conn.execute(  # noqa: E731
            "INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, vuupt_route_id, "
            "criado_em_provedor, fluxo) VALUES (?, ?, ?, ?, ?, ?)", a)
        ped("PS-1", 1, "ABERTO", None, "2026-09-25 09:00:00", "ENTREGA")        # pool há dias
        ped("PS-2", 2, "EM_ROTA", 50, "2026-09-26 09:00:00", "ENTREGA")         # rota de ontem
        ped("PS-3", 3, "INSUCESSO", None, "2026-09-26 09:00:00", "ENTREGA")     # insucesso sem reentrega
        ped("PS-4", 4, "INSUCESSO", None, "2026-09-26 09:00:00", "ENTREGA")     # já tem R1
        ped("PS-4-R1", 5, "ABERTO", None, "2026-09-29 08:00:00", "ENTREGA")
        ped("PS-6", 6, "ABERTO", None, "2026-09-28 17:00:00", "ENTREGA")        # em rascunho de amanhã
        ped("PS-9", 9, "ABERTO", None, "2026-09-20 09:00:00", "RETIRADA")       # retirada: fora
        self.conn.execute("INSERT INTO nucleo_rotas VALUES (50, '2026-09-28')")
        self.conn.execute("INSERT INTO nucleo_paradas VALUES (3, '2026-09-28 15:00:00', 'Cliente ausente')")
        self.conn.execute("INSERT INTO insucessos_duplicados VALUES (4, '#PS-4-R1', NULL)")
        self.conn.execute("INSERT INTO rascunhos_rota VALUES (1, '2026-09-30', 'L1', 'Rota A', 'RASCUNHO', "
                          "'2026-09-29 08:00:00')")
        self.conn.execute("INSERT INTO rascunhos_parada VALUES (1, 6)")
        self.conn.commit()
        banco.registrar_listagem_stokki([
            {"codigo": "PS-3"}, {"codigo": "PS-4"},
            {"codigo": "PS-7", "acao": "aguardando_redespacho", "embarcador": "EMB X"},  # sem serviço
        ], completa=True, agora=datetime(2026, 9, 29, 5, 0), conn=self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _estados(self):
        return {r["codigo"]: dict(r) for r in self.conn.execute("SELECT * FROM vigia_pedidos")}

    def test_classifica_cada_caso(self):
        vigiar.rodar(self.conn, agora=AGORA)
        e = self._estados()
        self.assertEqual(e["PS-1"]["estado"], "NO_POOL")
        self.assertEqual(e["PS-1"]["vencido"], 1)
        self.assertEqual(e["PS-2"]["estado"], "ROTA_PASSADA")
        self.assertEqual(e["PS-3"]["estado"], "INSUCESSO")
        self.assertEqual(e["PS-3"]["vencido"], 1)  # 15:00 de ontem + 13h
        self.assertNotIn("PS-4", e)                # já tem reentrega
        self.assertEqual(e["PS-4-R1"]["estado"], "NO_POOL")
        self.assertEqual(e["PS-4-R1"]["vencido"], 0)
        self.assertEqual(e["PS-6"]["estado"], "EM_RASCUNHO")
        self.assertEqual(e["PS-6"]["vencido"], 0)  # vence hoje 19h
        self.assertEqual(e["PS-7"]["estado"], "SEM_SERVICO")
        self.assertIn("aguardando_redespacho", e["PS-7"]["motivo"])
        self.assertEqual(e["PS-7"]["vencido"], 1)  # visto 05:00, prazo 4h
        self.assertNotIn("PS-9", e)

    def test_desde_se_mantem_e_historico_registra_saida(self):
        vigiar.rodar(self.conn, agora=AGORA)
        desde = self._estados()["PS-1"]["desde"]
        self.conn.execute("UPDATE nucleo_pedidos SET status = 'ENTREGUE' WHERE codigo = 'PS-2'")
        self.conn.commit()
        vigiar.rodar(self.conn, agora=datetime(2026, 9, 29, 11, 0))
        e = self._estados()
        self.assertEqual(e["PS-1"]["desde"], desde)
        self.assertNotIn("PS-2", e)
        hist = self.conn.execute("SELECT estado FROM vigia_historico WHERE codigo = 'PS-2'").fetchall()
        self.assertEqual([h["estado"] for h in hist], ["ROTA_PASSADA"])

    def test_retrato_velho_nao_acusa_sem_servico_novo(self):
        vigiar.rodar(self.conn, agora=datetime(2026, 10, 3, 10, 0))
        e = self._estados()
        self.assertNotIn("PS-7", e)
        self.assertNotIn("PS-3", e)
        self.assertIn("PS-1", e)

    def test_torre_avisa_retrato_velho(self):
        from vigia import consulta
        vigiar.rodar(self.conn, agora=datetime(2026, 10, 3, 10, 0))
        db = Path(self.tmp.name) / "t.db"
        ex = consulta.excecoes_torre("2026-10-03", db_path=db, agora=datetime(2026, 10, 3, 10, 0))
        self.assertTrue(any(x["id"].startswith("vigia:retrato:") for x in ex))
        ex = consulta.excecoes_torre("2026-09-29", db_path=db, agora=AGORA)
        self.assertFalse(any(x["id"].startswith("vigia:retrato:") for x in ex))

    def test_listagem_incompleta_nao_fecha_ninguem(self):
        abertos = lambda: {r["codigo"] for r in self.conn.execute("SELECT codigo FROM vigia_stokki_abertos")}  # noqa: E731
        banco.registrar_listagem_stokki([{"codigo": "PS-3"}], completa=False, conn=self.conn)
        self.assertEqual(abertos(), {"PS-3", "PS-4", "PS-7"})
        # 1 ausência numa listagem completa ainda não fecha (fonte pode voltar vazia sem erro)
        banco.registrar_listagem_stokki([{"codigo": "PS-3"}], completa=True, conn=self.conn)
        self.assertEqual(abertos(), {"PS-3", "PS-4", "PS-7"})
        banco.registrar_listagem_stokki([{"codigo": "PS-3"}], completa=True, conn=self.conn)
        self.assertEqual(abertos(), {"PS-3"})

    def test_reaparecer_zera_as_ausencias_e_mantem_o_desde(self):
        primeira = self.conn.execute(
            "SELECT primeira_vez_em FROM vigia_stokki_abertos WHERE codigo = 'PS-7'").fetchone()[0]
        banco.registrar_listagem_stokki([{"codigo": "PS-3"}], completa=True, conn=self.conn)
        banco.registrar_listagem_stokki([{"codigo": "PS-7"}], completa=True, conn=self.conn)
        banco.registrar_listagem_stokki([{"codigo": "PS-3"}], completa=True, conn=self.conn)
        row = self.conn.execute(
            "SELECT primeira_vez_em FROM vigia_stokki_abertos WHERE codigo = 'PS-7'").fetchone()
        self.assertEqual(row[0], primeira)

    def test_retirada_aberta_nao_vira_sem_servico(self):
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, fluxo) "
                          "VALUES ('PS-7', 70, 'EM_ROTA', 'RETIRADA')")
        self.conn.commit()
        vigiar.rodar(self.conn, agora=AGORA)
        self.assertNotIn("PS-7", self._estados())

    def test_reentrega_feita_a_mao_na_vuupt_resolve_o_insucesso(self):
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, fluxo, "
                          "reentrega_de_service_id) VALUES ('PS-3-R1', 30, 'ABERTO', 'ENTREGA', 3)")
        self.conn.commit()
        vigiar.rodar(self.conn, agora=AGORA)
        self.assertNotIn("PS-3", self._estados())

    def test_combinado_reentregue_deixa_o_segundo_pedido_sem_servico(self):
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, fluxo) "
                          "VALUES ('PS-20, PS-21', 20, 'INSUCESSO', 'ENTREGA')")
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, status, fluxo) "
                          "VALUES ('PS-20-R1', 22, 'ABERTO', 'ENTREGA')")
        self.conn.execute("INSERT INTO insucessos_duplicados VALUES (20, '#PS-20-R1', NULL)")
        self.conn.commit()
        banco.registrar_listagem_stokki([{"codigo": c} for c in ("PS-3", "PS-4", "PS-7", "PS-20", "PS-21")],
                                        completa=True, agora=datetime(2026, 9, 29, 5, 0), conn=self.conn)
        vigiar.rodar(self.conn, agora=AGORA)
        e = self._estados()
        self.assertEqual(e["PS-21"]["estado"], "SEM_SERVICO")
        self.assertNotIn("PS-20", e)

    def test_retrato_velho_mantem_o_que_se_sabia(self):
        vigiar.rodar(self.conn, agora=AGORA)
        vigiar.rodar(self.conn, agora=datetime(2026, 10, 3, 10, 0))
        e = self._estados()
        self.assertEqual(e["PS-7"]["estado"], "SEM_SERVICO")
        self.assertEqual(e["PS-3"]["estado"], "INSUCESSO")


if __name__ == "__main__":
    unittest.main()
