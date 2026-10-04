# -*- coding: utf-8 -*-
"""
Aviso ao embarcador de data fora do dia de visita (dias fixos v2, Hugo
03/10) e o ponto de chamada tratar_fora_dia_fixo.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_notificar_fora_dia_fixo -v
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import fora_dia_fixo as fdf
import notificar_fora_dia_fixo as nfdf
import pedidos_dedicados
import registro_dia_fixo as reg
from regioes_dia_fixo import regra_dia_fixo_do_servico

CAMPINAS = "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"
HOJE = date(2026, 10, 6)
CONFIG = {"notificacoes_automaticas": {"ativo": True}, "email": {}}
EMBS = {7: {"nome": "EMB TESTE", "emails": ["cliente@emb.com"], "cnpj": "12345678000195", "desligado": False}}


def _servico(i=1, data="2026-10-08"):
    return {"id": i, "code": f"#PS-{1000 + i}", "address": CAMPINAS, "sender_id": 7, "dimension_3": 5,
            "latitude": -22.905, "longitude": -47.060, "created_at": "2026-10-06 15:00:00",
            "title": f"Cliente {i}", "scheduled_start": f"{data}T08:00:00-03:00"}


def _item(i=1, valor=784.09, pendente=False):
    s = _servico(i)
    return {"servico": s, "regra": regra_dia_fixo_do_servico(s), "data": date(2026, 10, 8),
            "valor": valor, "valor_pendente": pendente}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        p = mock.patch.object(nfdf, "enviar_email", return_value=True)
        self.email = p.start()
        self.addCleanup(p.stop)

    def _avisado(self, i=1):
        conn = reg.conectar(self.db)
        try:
            return reg.ja_avisado(conn, _servico(i))
        finally:
            conn.close()

    def _canais(self, i=1):
        conn = reg.conectar(self.db)
        try:
            return conn.execute("SELECT canais FROM avisos_fora_dia_fixo WHERE codigo = ?", (f"PS-{1000 + i}",)).fetchone()[0]
        finally:
            conn.close()


class TestAvisar(Base):
    def test_um_email_por_embarcador_vai_pro_hugo_no_piloto_e_registra(self):
        r = nfdf.avisar([_item(1), _item(2)], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertEqual((r["emails"], r["registrados"]), (1, 2))
        self.assertEqual(self.email.call_count, 1)
        self.assertEqual(self.email.call_args[0][0], [nfdf.EMAIL_TESTE])
        self.assertIn("iria para cliente@emb.com", self.email.call_args[0][2])
        self.assertTrue(self._avisado(1) and self._avisado(2))
        self.assertEqual(self._canais(1), "email")

    def test_forcar_destino_vazio_vai_pro_embarcador(self):
        config = {**CONFIG, "fora_dia_fixo": {"forcar_destino": ""}}
        nfdf.avisar([_item()], config, db_path=self.db, embarcadores=EMBS)
        self.assertEqual(self.email.call_args[0][0], ["cliente@emb.com"])
        self.assertNotIn("iria para", self.email.call_args[0][2])

    def test_corpo_tem_data_dias_regiao_e_valor(self):
        nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=EMBS)
        corpo = self.email.call_args[0][2]
        for trecho in ("#PS-1001", "08/10/2026", "Campinas", "Quartas", "R$ 784,09", "envio dedicado"):
            self.assertIn(trecho, corpo)

    def test_valor_pendente_aparece_a_confirmar(self):
        nfdf.avisar([_item(valor=0.0, pendente=True)], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertIn("a confirmar", self.email.call_args[0][2])

    def test_falha_de_email_nao_registra(self):
        self.email.return_value = False
        r = nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertEqual((r["falhas"], r["registrados"]), (1, 0))
        self.assertFalse(self._avisado())

    def test_envio_real_com_notificacoes_desligadas_nao_envia_nem_registra(self):
        config = {"notificacoes_automaticas": {"ativo": False}, "email": {}, "fora_dia_fixo": {"forcar_destino": ""}}
        with mock.patch.object(nfdf.preferencias_notificacao, "whatsapp_do_embarcador", return_value="5511988887777"), \
             mock.patch.object(nfdf.notificar_whatsapp, "avisar_cliente_fora_dia_fixo", return_value="enviado") as wpp:
            r = nfdf.avisar([_item()], config, db_path=self.db, embarcadores=EMBS)
        self.assertEqual(r["desligado"], 1)
        self.email.assert_not_called()
        wpp.assert_not_called()
        self.assertFalse(self._avisado())   # tenta de novo quando a chave geral ligar

    def test_piloto_sai_pro_hugo_mesmo_com_notificacoes_desligadas(self):
        # producao tem notificacoes_automaticas.ativo = False; o piloto nao depende dela
        config = {"notificacoes_automaticas": {"ativo": False}, "email": {}}
        r = nfdf.avisar([_item()], config, db_path=self.db, embarcadores=EMBS)
        self.assertEqual((r["emails"], r["registrados"], r["desligado"]), (1, 1, 0))
        self.assertEqual(self.email.call_args[0][0], [nfdf.EMAIL_TESTE])
        self.assertTrue(self._avisado())

    def test_piloto_com_forcar_destino_preenchido_nao_depende_da_chave_geral(self):
        config = {"notificacoes_automaticas": {"ativo": False}, "email": {},
                  "fora_dia_fixo": {"forcar_destino": "outro@freshlogbr.com"}}
        r = nfdf.avisar([_item()], config, db_path=self.db, embarcadores=EMBS)
        self.assertEqual(r["registrados"], 1)
        self.assertEqual(self.email.call_args[0][0], ["outro@freshlogbr.com"])

    def test_embarcador_sem_email_registra_nenhum(self):
        nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores={})
        self.email.assert_not_called()
        self.assertEqual(self._canais(), "nenhum")

    def test_embarcador_que_desligou_nao_recebe_email(self):
        embs = {7: {**EMBS[7], "desligado": True}}
        nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=embs)
        self.email.assert_not_called()

    def test_whatsapp_quando_tem_numero(self):
        with mock.patch.object(nfdf.preferencias_notificacao, "whatsapp_do_embarcador", return_value="5511988887777"), \
             mock.patch.object(nfdf.notificar_whatsapp, "avisar_cliente_fora_dia_fixo", return_value="enviado") as wpp:
            r = nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=EMBS)
        codigo, telefone, texto, _config = wpp.call_args[0]
        self.assertEqual((codigo, telefone), ("PS-1001", "5511988887777"))
        self.assertLessEqual(len(texto), 200)
        self.assertEqual(r["whatsapp"], 1)
        self.assertEqual(self._canais(), "email,whatsapp")

    def test_email_falhou_mas_whatsapp_saiu_registra(self):
        self.email.return_value = False
        with mock.patch.object(nfdf.preferencias_notificacao, "whatsapp_do_embarcador", return_value="5511988887777"), \
             mock.patch.object(nfdf.notificar_whatsapp, "avisar_cliente_fora_dia_fixo", return_value="enviado"):
            nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertEqual(self._canais(), "whatsapp")

    def test_falha_ao_carregar_embarcadores_nao_registra_e_tenta_de_novo(self):
        with mock.patch.object(nfdf.preferencias_notificacao, "carregar_embarcadores",
                               side_effect=sqlite3.OperationalError("database is locked")),              self.assertLogs(nfdf.logger, "WARNING"):
            r = nfdf.avisar([_item()], CONFIG, db_path=self.db)
        self.assertEqual((r["falhas"], r["registrados"]), (1, 0))
        self.email.assert_not_called()
        self.assertFalse(self._avisado())
        with mock.patch.object(nfdf.preferencias_notificacao, "carregar_embarcadores", return_value=EMBS):
            nfdf.avisar([_item()], CONFIG, db_path=self.db)
        self.assertEqual(self.email.call_count, 1)
        self.assertTrue(self._avisado())

    def test_falha_ao_ler_whatsapp_sem_email_nao_registra(self):
        embs = {7: {**EMBS[7], "emails": []}}
        with mock.patch.object(nfdf.preferencias_notificacao, "whatsapp_do_embarcador",
                               side_effect=sqlite3.OperationalError("database is locked")),              self.assertLogs(nfdf.logger, "WARNING"):
            r = nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=embs)
        self.assertEqual(r["registrados"], 0)
        self.assertFalse(self._avisado())

    def test_falha_ao_ler_whatsapp_com_email_enviado_registra_email(self):
        with mock.patch.object(nfdf.preferencias_notificacao, "whatsapp_do_embarcador",
                               side_effect=sqlite3.OperationalError("database is locked")),              self.assertLogs(nfdf.logger, "WARNING"):
            nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertEqual(self._canais(), "email")

    def test_falha_no_pedido_2_registra_o_1(self):
        original = reg.registrar_aviso

        def registrar(conn, servico, *a, **kw):
            if "1002" in servico["code"]:
                raise sqlite3.OperationalError("database is locked")
            return original(conn, servico, *a, **kw)

        with mock.patch.object(reg, "registrar_aviso", side_effect=registrar), self.assertLogs(nfdf.logger, "WARNING"):
            r = nfdf.avisar([_item(1), _item(2)], CONFIG, db_path=self.db, embarcadores=EMBS)
        self.assertEqual((r["registrados"], r["falhas"]), (1, 1))
        self.assertTrue(self._avisado(1))
        self.assertFalse(self._avisado(2))


class TestTratar(Base):
    def setUp(self):
        super().setUp()
        for alvo, obj, valor in (("DETECCAO_A_PARTIR_DE", fdf, date(2026, 10, 6)),
                                 ("calcular_valor", fdf, lambda s, c, t: 784.09)):
            p = mock.patch.object(obj, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        for obj, alvo, valor in ((nfdf.preferencias_notificacao, "carregar_embarcadores", EMBS),
                                 (fdf, "carregar_tipos_carga", {})):
            p = mock.patch.object(obj, alvo, return_value=valor)
            p.start()
            self.addCleanup(p.stop)

    def _tratar(self, servicos=None, **kw):
        return fdf.tratar_fora_dia_fixo(servicos or [_servico()], CONFIG, hoje=HOJE, db_path=self.db, **kw)

    def test_tratar_duas_vezes_marca_e_avisa_uma_vez(self):
        self.assertEqual(self._tratar()["marcados"], 1)
        self.assertEqual(self._tratar()["marcados"], 0)
        self._tratar()
        self.assertEqual(self.email.call_count, 1)
        conn = pedidos_dedicados.conectar(self.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM pedidos_dedicados").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_aviso_que_falhou_sai_na_rodada_seguinte(self):
        self.email.return_value = False
        self._tratar()
        self.assertFalse(self._avisado())
        self.email.return_value = True
        self._tratar()
        self.assertTrue(self._avisado())
        self._tratar()
        self.assertEqual(self.email.call_count, 2)

    def test_modo_teste_nao_marca_nem_avisa(self):
        with mock.patch.object(nfdf, "avisar") as avisar, \
             mock.patch.object(nfdf.notificar_whatsapp, "avisar_cliente_fora_dia_fixo") as wpp:
            r = self._tratar(modo_teste=True)
        self.assertEqual((r["marcados"], r["candidatos"]), (0, 1))
        self.email.assert_not_called()
        avisar.assert_not_called()
        wpp.assert_not_called()
        self.assertFalse(self._avisado())
        conn = pedidos_dedicados.conectar(self.db)
        try:
            self.assertEqual(pedidos_dedicados.ativos_por_codigo(conn), {})
        finally:
            conn.close()

    def test_nunca_levanta(self):
        with mock.patch.object(fdf, "marcar_fora_dia_fixo", side_effect=RuntimeError("banco travado")),              self.assertLogs(fdf.logger, "ERROR"):
            self.assertIn("erro", self._tratar())

    def test_avisar_que_levanta_mantem_os_marcados(self):
        with mock.patch.object(nfdf, "avisar", side_effect=RuntimeError("banco travado")),              self.assertLogs(fdf.logger, "ERROR"):
            r = self._tratar()
        self.assertEqual(r["marcados"], 1)
        self.assertIn("erro", r)

    def test_falha_no_pedido_2_nao_reenvia_o_1_na_rodada_seguinte(self):
        servicos = [_servico(1), _servico(2)]
        original = reg.registrar_aviso

        def registrar(conn, servico, *a, **kw):
            if "1002" in servico["code"]:
                raise sqlite3.OperationalError("database is locked")
            return original(conn, servico, *a, **kw)

        with mock.patch.object(reg, "registrar_aviso", side_effect=registrar), self.assertLogs(nfdf.logger, "WARNING"):
            self._tratar(servicos)
        self.assertEqual(self.email.call_count, 1)
        self._tratar(servicos)
        self.assertEqual(self.email.call_count, 2)
        corpo = self.email.call_args[0][2]
        self.assertIn("#PS-1002", corpo)
        self.assertNotIn("#PS-1001", corpo)
        self._tratar(servicos)
        self.assertEqual(self.email.call_count, 2)
        self.assertTrue(self._avisado(1) and self._avisado(2))

    def test_codigo_combinado_avisado_nao_avisa_de_novo_pelo_outro_codigo(self):
        s1 = {**_servico(1), "code": "#PS-1001, PS-1002"}
        self._tratar([s1])
        self.assertEqual(self.email.call_count, 1)
        s2 = {**_servico(1), "code": "PS-1002"}
        self._tratar([s2])
        self.assertEqual(self.email.call_count, 1)


if __name__ == "__main__":
    unittest.main()
