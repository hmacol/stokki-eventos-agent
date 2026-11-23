# -*- coding: utf-8 -*-
"""Testes do e-mail do batimento/bater.py (envio mockado). Rodar da raiz:
py -3.11 -m unittest batimento.test_bater"""
import unittest
from unittest import mock

from batimento import bater

RODADA = {"rodada_em": "2026-10-06 07:25:00", "lancados": 10, "destino": 7, "em_andamento": 2,
          "divergencia": 1, "fecha": 1}
NOVA = {"codigo": "PS-1", "embarcador": "EMB", "motivo_txt": "Expedido sem entrega", "real": True,
        "evidencias": ["stokki=EXPEDIDO", "nucleo=None"]}
POR_MOTIVO = [{"motivo": "EXPEDIDO_SEM_ENTREGA", "rotulo": "Expedido sem entrega", "total": 1, "real": True}]


class MontarEmail(unittest.TestCase):
    def test_assunto_e_corpo_com_novidade(self):
        assunto, html = bater.montar_email([NOVA], RODADA, POR_MOTIVO)
        self.assertEqual(assunto, "[Freshlog] Batimento: 1 divergência(s) nova(s)")
        for trecho in ("PS-1", "Expedido sem entrega", "10 lançados", "vigia?aba=fechamento"):
            self.assertIn(trecho, html)

    def test_assunto_quando_nao_fecha(self):
        assunto, _ = bater.montar_email([], {**RODADA, "fecha": 0}, POR_MOTIVO)
        self.assertTrue(assunto.startswith("[Freshlog] Batimento NÃO FECHA"))


class Avisar(unittest.TestCase):
    def test_sem_novidade_e_fechando_nao_manda(self):
        with mock.patch("email_utils.enviar_email") as env:
            self.assertFalse(bater.avisar({}, [], RODADA, POR_MOTIVO))
        env.assert_not_called()

    def test_manda_pro_destinatario_interno(self):
        config = {"notificacao_execucao": {"destinatario": "x@freshlogbr.com"}, "email": {"remetente": "r"}}
        with mock.patch("email_utils.enviar_email", return_value=True) as env:
            self.assertTrue(bater.avisar(config, [NOVA], RODADA, POR_MOTIVO))
        self.assertEqual(env.call_args.args[0], ["x@freshlogbr.com"])
        self.assertEqual(env.call_args.args[3], {"remetente": "r"})

    def test_fallback_e_falha_no_envio_nao_levanta(self):
        with mock.patch("email_utils.enviar_email", side_effect=RuntimeError("smtp fora")) as env:
            self.assertFalse(bater.avisar({}, [NOVA], RODADA, POR_MOTIVO))
        self.assertEqual(env.call_args.args[0], ["hugo@freshlogbr.com"])


if __name__ == "__main__":
    unittest.main()
