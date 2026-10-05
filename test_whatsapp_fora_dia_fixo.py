# -*- coding: utf-8 -*-
"""
WhatsApp ao embarcador: data fora do dia de visita da região (dias fixos v2,
Hugo 03/10). Rodar (da raiz): py -3.11 -m unittest test_whatsapp_fora_dia_fixo -v
"""
import sqlite3
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import notificar_whatsapp as nw

DATA = date(2026, 10, 8)
CONFIG = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                    "grupo_id": "g@g.us", "intervalo_min_seg": 0,
                                    "clientes": {"ativo": True, "teto_diario": 30, "forcar_destino": ""}}}


class TestTexto(unittest.TestCase):
    def test_texto_completo(self):
        t = nw.texto_fora_dia_fixo("PS-40316", DATA, "Campinas", "Quartas", 784.09)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        for trecho in ("PS-40316", "08/10", "Campinas", "Quartas", "envio dedicado", "R$ 784,09", "portal"):
            self.assertIn(trecho, t)

    def test_texto_longo_encurta_e_mantem_o_essencial(self):
        t = nw.texto_fora_dia_fixo("#PS-40316-R12", DATA, "Vale do Paraíba", "Segundas, Quartas e Sextas", 12345.67)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        for trecho in ("PS-40316-R12", "08/10", "envio dedicado", "R$ 12.345,67"):
            self.assertIn(trecho, t)
        self.assertNotIn("#", t)

    def test_valor_pendente(self):
        self.assertIn("valor a confirmar", nw.texto_fora_dia_fixo("PS-1", DATA, "Campinas", "Quartas", None))


class TestEnvio(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        for alvo, valor in (("configurado", lambda cfg: True), ("numero_existe", lambda cfg, n: True)):
            p = mock.patch.object(nw.integracao_openwa, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1"))
        self.enviar = p.start()
        self.addCleanup(p.stop)

    def _avisar(self, config=CONFIG, **kw):
        return nw.avisar_cliente_fora_dia_fixo("PS-1", "5511988887777", "texto", config, conn=self.conn,
                                               agora=datetime(2026, 10, 6, 18, 0), dormir=lambda s: None, **kw)

    def test_desligado_sem_canal_de_clientes(self):
        config = {"whatsapp_notificacoes": {**CONFIG["whatsapp_notificacoes"], "clientes": {"ativo": False}}}
        self.assertEqual(self._avisar(config), "desligado")
        self.enviar.assert_not_called()

    def test_envia_pro_numero_do_embarcador(self):
        self.assertEqual(self._avisar(), "enviado")
        self.assertEqual(self.enviar.call_args[0][1:], ("5511988887777@c.us", "texto"))
        tipo = self.conn.execute("SELECT origem, tipo, assinatura FROM notificacoes_whatsapp").fetchone()
        self.assertEqual(tipo, (nw.ORIGEM_CLIENTE, "fora_dia_fixo", "fora_dia_fixo:PS-1"))

    def test_forcar_destino_desvia_e_mostra_o_destino_real(self):
        cfg = {"whatsapp_notificacoes": {**CONFIG["whatsapp_notificacoes"],
                                         "clientes": {"ativo": True, "forcar_destino": "5511999990000"}}}
        self.assertEqual(self._avisar(cfg), "enviado")
        destino, texto = self.enviar.call_args[0][1:]
        self.assertEqual(destino, "5511999990000@c.us")
        self.assertTrue(texto.startswith("[teste → +5511988887777]"))

    def test_teto_diario_dos_clientes(self):
        cfg = {"whatsapp_notificacoes": {**CONFIG["whatsapp_notificacoes"],
                                         "clientes": {"ativo": True, "teto_diario": 0, "forcar_destino": ""}}}
        self.assertEqual(self._avisar(cfg), "nao_enviado")
        self.enviar.assert_not_called()

    def test_numero_sem_whatsapp(self):
        with mock.patch.object(nw.integracao_openwa, "numero_existe", lambda cfg, n: False):
            self.assertEqual(self._avisar(), "numero_sem_whatsapp")
        self.enviar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
