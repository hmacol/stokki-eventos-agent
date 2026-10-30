"""Testes do resumo diario do Gmail no WhatsApp (resumo_email_whatsapp.py e
notificar_whatsapp.avisar_resumo_email).

Rodar: py -3.11 -m unittest test_resumo_email_whatsapp
"""

import sqlite3
import unittest
from datetime import date, datetime
from unittest.mock import patch

import notificar_whatsapp as nw
from resumo_email_whatsapp import escolher_resumo

HOJE = date(2026, 10, 29)
AGORA = datetime(2026, 10, 29, 7, 15)
CONFIG = {"whatsapp_notificacoes": {
    "ativo": True, "base_url": "http://127.0.0.1:2785/api", "api_key": "k", "sessao": "s",
    "grupo_id": "alertas@g.us", "grupo_resumo_email_id": "resumo@g.us", "teto_diario": 50}}


class TestEscolherResumo(unittest.TestCase):
    def test_pega_o_de_hoje_mais_recente(self):
        msgs = [("Resumo do e-mail · 28/10", "ontem"),
                ("Resumo do e-mail · 29/10", "primeiro"),
                ("Resumo do e-mail · 29/10", "segundo")]
        self.assertEqual(escolher_resumo(msgs, HOJE), "segundo")

    def test_ignora_outro_dia_outro_assunto_e_corpo_vazio(self):
        msgs = [("Resumo do e-mail · 28/10", "ontem"),
                ("Agente Stokki Eventos · Resumo geral — OK (29/10 04:10)", "x"),
                ("Resumo do e-mail · 29/10", "   ")]
        self.assertIsNone(escolher_resumo(msgs, HOJE))


class TestTextoResumoEmail(unittest.TestCase):
    def test_arruma_espacos(self):
        self.assertEqual(nw.texto_resumo_email("  *Ler*  \r\n\r\n\r\n\r\n- item  \n"), "*Ler*\n\n- item")

    def test_corta_no_limite_e_avisa(self):
        texto = nw.texto_resumo_email("a" * 3000)
        self.assertEqual(len(texto), nw.MAX_RESUMO_EMAIL)
        self.assertTrue(texto.endswith("O resto está no e-mail."))


class TestAvisarResumoEmail(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def _avisar(self, config=CONFIG):
        return nw.avisar_resumo_email("*Ler / agir*\n- D4Sign", HOJE, config, conn=self.conn, agora=AGORA,
                                      dormir=lambda _s: None)

    @patch("notificar_whatsapp.integracao_openwa.enviar_texto", return_value=(True, "m1"))
    def test_envia_no_grupo_do_resumo_uma_vez_por_dia(self, enviar):
        self.assertEqual(self._avisar(), "enviado")
        self.assertEqual(enviar.call_args.args[1], "resumo@g.us")
        self.assertEqual(self._avisar(), "ja_enviado")
        self.assertEqual(enviar.call_count, 1)

    @patch("notificar_whatsapp.integracao_openwa.enviar_texto", return_value=(True, "m1"))
    def test_sem_grupo_fica_desligado(self, enviar):
        config = {"whatsapp_notificacoes": dict(CONFIG["whatsapp_notificacoes"], grupo_resumo_email_id="")}
        self.assertEqual(self._avisar(config), "desligado")
        enviar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
