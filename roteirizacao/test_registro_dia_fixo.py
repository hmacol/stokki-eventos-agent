# -*- coding: utf-8 -*-
"""
Origem da data agendada e controle de avisos (dias fixos v2, Hugo 03/10).
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_registro_dia_fixo -v
"""
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import registro_dia_fixo as reg
import regioes_dia_fixo as rdf

SOROCABA = "Rua XV de Novembro 10, Centro, Sorocaba - SP, 18010-080, Brasil"
QUINTA = date(2026, 10, 8)


class FakeVuupt:
    def __init__(self):
        self.chamadas = []

    def atualizar_servico(self, service_id, payload):
        self.chamadas.append((service_id, payload))


class TestRegistro(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = reg.conectar(self.db)
        self.addCleanup(self.conn.close)

    def test_origem_registrada_so_vale_pra_mesma_data(self):
        s = {"id": 1, "code": "#PS-1001"}
        self.assertEqual(reg.registrar_origem(self.conn, s, QUINTA, reg.ORIGEM_DIA_FIXO), 1)
        self.assertTrue(reg.data_nao_e_do_cliente(self.conn, s, QUINTA))
        self.assertFalse(reg.data_nao_e_do_cliente(self.conn, s, QUINTA + timedelta(days=1)))

    def test_equipe_por_service_id_sem_codigo(self):
        # a tela do Planejamento só manda o service_id
        reg.registrar_origem(self.conn, {"id": 55}, QUINTA, reg.ORIGEM_EQUIPE)
        self.assertTrue(reg.data_nao_e_do_cliente(self.conn, {"id": 55, "code": "#PS-1001"}, QUINTA))
        self.assertFalse(reg.data_nao_e_do_cliente(self.conn, {"id": 56, "code": "#PS-1002"}, QUINTA))

    def test_codigo_combinado_e_reentrega(self):
        reg.registrar_origem(self.conn, {"id": 1, "code": "#PS-1001, PS-2002"}, QUINTA, reg.ORIGEM_DIA_FIXO)
        self.assertTrue(reg.data_nao_e_do_cliente(self.conn, {"id": 9, "code": "PS-2002"}, QUINTA))
        self.assertTrue(reg.data_nao_e_do_cliente(self.conn, {"id": 8, "code": "#PS-1001-R1"}, QUINTA))

    def test_origem_desconhecida_conta_como_cliente(self):
        reg.registrar_origem(self.conn, {"id": 1, "code": "PS-1001"}, QUINTA, "CLIENTE")
        self.assertFalse(reg.data_nao_e_do_cliente(self.conn, {"id": 1, "code": "PS-1001"}, QUINTA))

    def test_servico_sem_codigo_e_sem_id(self):
        self.assertFalse(reg.data_nao_e_do_cliente(self.conn, {}, QUINTA))
        self.assertEqual(reg.registrar_origem(self.conn, {}, QUINTA, reg.ORIGEM_EQUIPE), 0)

    def test_registrar_duas_vezes_nao_duplica(self):
        s = {"id": 1, "code": "PS-1001"}
        reg.registrar_origem(self.conn, s, QUINTA, reg.ORIGEM_EQUIPE)
        self.assertEqual(reg.registrar_origem(self.conn, s, QUINTA, reg.ORIGEM_EQUIPE), 0)

    def test_aviso_uma_vez_por_pedido(self):
        s = {"id": 1, "code": "#PS-1001"}
        self.assertFalse(reg.ja_avisado(self.conn, s))
        reg.registrar_aviso(self.conn, s, QUINTA, ["email"], agora=datetime(2026, 10, 6, 18, 5))
        reg.registrar_aviso(self.conn, s, QUINTA, ["email", "whatsapp"])
        self.assertTrue(reg.ja_avisado(self.conn, s))
        linha = self.conn.execute("SELECT * FROM avisos_fora_dia_fixo").fetchall()
        self.assertEqual(len(linha), 1)
        self.assertEqual((linha[0]["codigo"], linha[0]["data"], linha[0]["canais"], linha[0]["enviado_em"]),
                         ("PS-1001", "2026-10-08", "email", "2026-10-06 18:05:00"))

    def test_aviso_sem_canal_grava_nenhum(self):
        reg.registrar_aviso(self.conn, {"id": 1, "code": "PS-7"}, QUINTA, [])
        self.assertEqual(self.conn.execute("SELECT canais FROM avisos_fora_dia_fixo").fetchone()[0], "nenhum")

    def test_registrar_origens_tolerante(self):
        ruim = Path(self.tmp.name) / "nao_existe" / "t.db"
        itens = [{"servico": {"id": 1, "code": "PS-1"}, "data": QUINTA}]
        self.assertEqual(reg.registrar_origens(itens, reg.ORIGEM_DIA_FIXO, ruim), 0)


class TestAplicarRegistra(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"

    def test_sorocaba_cai_so_em_terca_de_semana_valida_e_registra(self):
        hoje = date(2026, 10, 3)
        servico = {"id": 1, "code": "#PS-1001", "address": SOROCABA}
        vuupt = FakeVuupt()
        feitos = rdf.aplicar_regioes_dia_fixo([servico], vuupt, hoje=hoje, db_path=self.db)
        data = feitos[0]["data"]
        regra = rdf.regra_dia_fixo_do_servico(servico)
        self.assertEqual(data.weekday(), rdf.TERCA)
        self.assertTrue(rdf.data_valida_na_regiao(regra, data))
        self.assertFalse(rdf.data_valida_na_regiao(regra, data - timedelta(days=7)))
        self.assertLessEqual((data - hoje).days, 14)
        self.assertTrue(vuupt.chamadas[0][1]["scheduled_start"].startswith(data.isoformat()))
        conn = reg.conectar(self.db)
        try:
            self.assertTrue(reg.data_nao_e_do_cliente(conn, servico, data))
            self.assertEqual(conn.execute("SELECT origem FROM agendamentos_origem").fetchone()[0], reg.ORIGEM_DIA_FIXO)
        finally:
            conn.close()

    def test_nao_mexe_em_quem_ja_tem_data(self):
        servico = {"id": 1, "code": "#PS-1001", "address": SOROCABA, "scheduled_start": "2026-10-09T08:00:00-03:00"}
        self.assertEqual(rdf.aplicar_regioes_dia_fixo([servico], FakeVuupt(), hoje=date(2026, 10, 3), db_path=self.db), [])

    def test_falha_no_registro_nao_desfaz_o_agendamento(self):
        ruim = Path(self.tmp.name) / "nao_existe" / "t.db"
        servico = {"id": 1, "code": "#PS-1001", "address": SOROCABA}
        feitos = rdf.aplicar_regioes_dia_fixo([servico], FakeVuupt(), hoje=date(2026, 10, 3), db_path=ruim)
        self.assertEqual(len(feitos), 1)
        self.assertTrue(servico["scheduled_start"])


if __name__ == "__main__":
    unittest.main()
