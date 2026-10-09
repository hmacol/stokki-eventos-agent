# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_notificar_whatsapp_cancelamento"""
import unittest
from unittest import mock

import notificar_whatsapp as nw

ENVIO = {"id": 50, "numero_nf": "9959", "codigo_pedido": "PS-39959", "destinatario_nome": "LOURENCO DISTRIBUIDORA",
         "cnpj_embarcador": "22135070000190", "nome_embarcador": "MARIA DOLORES"}


class TextoCancelamento(unittest.TestCase):
    def test_texto_curto_com_motivo(self):
        t = nw.texto_cancelamento_pendente(ENVIO, "motorista em rota (PS-39959, rota 10)", "")
        self.assertIn("NF 9959", t)
        self.assertIn("PS-39959", t)
        self.assertIn("MARIA DOLORES", t)
        self.assertIn("motorista em rota", t)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)

    def test_texto_com_erro(self):
        t = nw.texto_cancelamento_pendente(ENVIO, "falha técnica no cancelamento automático", "Stokki 500: x" * 20)
        self.assertIn("falhou", t)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)


class AvisarCancelamento(unittest.TestCase):
    def test_vai_pro_grupo_do_atendimento_com_assinatura(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "grupo_id": "g@g.us", "grupo_atendimento_id": "atend@g.us"}}
        with mock.patch.object(nw, "despachar", return_value="enviado") as d:
            self.assertEqual(nw.avisar_cancelamento_pendente(ENVIO, "motorista em rota", "", config), "enviado")
        kw = d.call_args.kwargs
        self.assertEqual(kw["grupo_id"], "atend@g.us")
        self.assertEqual(d.call_args.args[1:3], ("portal_cancelamento", "cancelamento"))
        self.assertEqual(d.call_args.args[4], "cancelamento:50")

    def test_sem_grupo_do_atendimento_nao_envia(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "grupo_id": "g@g.us"}}
        with mock.patch.object(nw, "despachar") as d:
            self.assertEqual(nw.avisar_cancelamento_pendente(ENVIO, "x", "", config), "desligado")
        d.assert_not_called()


if __name__ == "__main__":
    unittest.main()
