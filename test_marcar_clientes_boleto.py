# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest test_marcar_clientes_boleto
"""
import unittest
from unittest.mock import patch

import marcar_clientes_boleto as mcb
import notificar_whatsapp as nw


class Rotina(unittest.TestCase):
    def _rodar(self, novos, modo_teste=False):
        with patch.object(mcb, "_carregar_config", return_value={"painel_agentes": {"url_base": "https://x/painel"}}), \
             patch.object(mcb.cb, "sugerir", return_value={"novos": novos, "atualizados": 3}) as sug, \
             patch.object(mcb.cb, "listar", return_value=[{}] * (len(novos) + 2)), \
             patch.object(mcb, "notificar_execucao") as notif, \
             patch.object(mcb.notificar_whatsapp, "avisar_clientes_boleto", return_value="enviado") as zap:
            r = mcb.main(modo_teste=modo_teste)
        return r, sug, notif, zap

    def test_com_pendente_novo_avisa(self):
        r, sug, notif, zap = self._rodar([(11, "123", "MERCADO A")])
        self.assertEqual(r["novos"], 1)
        zap.assert_called_once()
        self.assertEqual(zap.call_args.args[:3], (1, 3, "https://x/painel/clientes-boleto"))
        notif.assert_called_once()
        self.assertIn("https://x/painel/clientes-boleto", notif.call_args.args[0]["Clientes com boleto"]["detalhe"])

    def test_sem_novo_nao_avisa(self):
        r, sug, notif, zap = self._rodar([])
        zap.assert_not_called()
        notif.assert_not_called()

    def test_modo_teste_repassa(self):
        _r, sug, _n, _z = self._rodar([], modo_teste=True)
        self.assertTrue(sug.call_args.kwargs["modo_teste"])


class TextoWhatsApp(unittest.TestCase):
    def test_texto_curto(self):
        t = nw.texto_clientes_boleto(3, 40, "https://app.freshhub.com.br/painel/clientes-boleto")
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        self.assertIn("3 novos", t)
        self.assertIn("/clientes-boleto", t)


if __name__ == "__main__":
    unittest.main()
