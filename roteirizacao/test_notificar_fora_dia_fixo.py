# -*- coding: utf-8 -*-
"""
Aviso ao embarcador de data fora do dia de visita (dias fixos v2, Hugo
03/10) e o ponto de chamada tratar_fora_dia_fixo.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_notificar_fora_dia_fixo -v
"""
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

    def test_notificacoes_desligadas_nao_envia_nem_registra(self):
        config = {"notificacoes_automaticas": {"ativo": False}, "email": {}}
        r = nfdf.avisar([_item()], config, db_path=self.db, embarcadores=EMBS)
        self.assertEqual(r["desligado"], 1)
        self.email.assert_not_called()
        self.assertFalse(self._avisado())

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


class TestTratar(Base):
    def setUp(self):
        super().setUp()
        for alvo, obj, valor in (("DETECCAO_A_PARTIR_DE", fdf, date(2026, 10, 6)),
                                 ("calcular_valor", fdf, lambda s, c, t: 784.09)):
            p = mock.patch.object(obj, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(nfdf.preferencias_notificacao, "carregar_embarcadores", return_value=EMBS)
        p.start()
        self.addCleanup(p.stop)

    def _tratar(self, **kw):
        return fdf.tratar_fora_dia_fixo([_servico()], CONFIG, hoje=HOJE, db_path=self.db, **kw)

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
        with mock.patch.object(fdf, "marcar_fora_dia_fixo", side_effect=RuntimeError("banco travado")):
            self.assertIn("erro", self._tratar())


if __name__ == "__main__":
    unittest.main()
