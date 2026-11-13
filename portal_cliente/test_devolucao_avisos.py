# -*- coding: utf-8 -*-
"""Avisos da devolucao de estoque no worker do portal (galpao e operacao).

    py -3.11 -m unittest discover -s portal_cliente -p "test_devolucao_avisos.py"
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import enviar_stokki as es  # noqa: E402

ITENS = [{"sku": "FABMLT", "nome": "Fabenne Merlot", "movido": 4.0, "destino": "FABENNE-1"}]
CONFIG = {"email": {"email_atendimento": "atend@x.com", "email_responsavel": "resp@x.com"}}


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE interno (cnpj_embarcador TEXT, apelido TEXT, nome_remetente TEXT, stkkc_id INTEGER)")
    c.execute("INSERT INTO interno VALUES ('1', 'FABENNE', 'Fabenne Vinhos Ltda', 777)")
    c.execute("INSERT INTO interno VALUES ('2', NULL, 'Sem Apelido Ltda', 888)")
    return c


class NomeEmbarcador(unittest.TestCase):
    def test_apelido_nome_e_fallbacks(self):
        c = _conn()
        self.assertEqual(es._nome_embarcador_stokki(c, 777), "FABENNE")
        self.assertEqual(es._nome_embarcador_stokki(c, "888"), "Sem Apelido Ltda")
        self.assertEqual(es._nome_embarcador_stokki(c, 999), "#stkkc-999")
        self.assertEqual(es._nome_embarcador_stokki(c, 0), "embarcador não identificado")
        self.assertEqual(es._nome_embarcador_stokki(c, None), "embarcador não identificado")

    def test_erro_de_banco_cai_no_id(self):
        self.assertEqual(es._nome_embarcador_stokki(sqlite3.connect(":memory:"), 5), "#stkkc-5")


class AvisoGalpao(unittest.TestCase):
    def _chamar(self, retorno):
        with mock.patch("notificar_whatsapp.avisar_devolucao_galpao", return_value=retorno) as a:
            return es._avisar_devolucao_galpao(_conn(), CONFIG, 43101, 777, ITENS), a

    def test_enviado_e_modo_teste_sao_ok(self):
        for ret in ("enviado", "modo_teste"):
            r, a = self._chamar(ret)
            self.assertTrue(r)
        self.assertEqual(a.call_args.args[:3], (43101, "FABENNE", ITENS))

    def test_desligado_loga_ps_e_endereco_sem_levantar(self):
        with self.assertLogs("portal_envios", level="WARNING") as lg:
            r, _ = self._chamar("desligado")
        self.assertTrue(r)
        self.assertIn("PS-43101", lg.output[0])
        self.assertIn("FABENNE-1", lg.output[0])

    def test_falhou_e_nao_enviado_levantam_pra_nao_marcar_avisado(self):
        for ret in ("falhou", "nao_enviado"):
            with self.assertLogs("portal_envios", level="WARNING") as lg:
                with self.assertRaises(RuntimeError):
                    self._chamar(ret)
            self.assertIn("FABENNE-1", lg.output[0])


class AvisoOperacao(unittest.TestCase):
    def test_email_e_whatsapp_com_escape(self):
        with mock.patch.object(es, "enviar_email") as em, \
                mock.patch("notificar_whatsapp.avisar_devolucao_pendente") as wa:
            es._avisar_devolucao_operacao(CONFIG, 43101, "FABENNE", "duvida <b>x</b> | FABMLT: movido 2 de 4")
        self.assertEqual(em.call_args.args[0], ["atend@x.com"])
        self.assertEqual(em.call_args.args[1], "[Stokki] Devolução de estoque precisa de conferência · PS-43101")
        html = em.call_args.args[2]
        self.assertIn("PS-43101", html)
        self.assertIn("FABENNE", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("<li>", html)
        self.assertIn("Virtual", html)
        wa.assert_called_once_with(43101, "FABENNE", "duvida <b>x</b> | FABMLT: movido 2 de 4", CONFIG)

    def test_sem_atendimento_usa_responsavel(self):
        with mock.patch.object(es, "enviar_email") as em, mock.patch("notificar_whatsapp.avisar_devolucao_pendente"):
            es._avisar_devolucao_operacao({"email": {"email_responsavel": "resp@x.com"}}, 1, "E", "m")
        self.assertEqual(em.call_args.args[0], ["resp@x.com"])

    def test_nunca_levanta_e_whatsapp_sai_mesmo_com_email_quebrado(self):
        with mock.patch.object(es, "enviar_email", side_effect=RuntimeError("smtp")), \
                mock.patch("notificar_whatsapp.avisar_devolucao_pendente") as wa:
            es._avisar_devolucao_operacao(CONFIG, 1, "E", "m")
        wa.assert_called_once()
        with mock.patch.object(es, "enviar_email"), \
                mock.patch("notificar_whatsapp.avisar_devolucao_pendente", side_effect=RuntimeError("x")):
            es._avisar_devolucao_operacao(CONFIG, 1, "E", "m")


if __name__ == "__main__":
    unittest.main()
