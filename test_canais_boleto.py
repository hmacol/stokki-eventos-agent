# -*- coding: utf-8 -*-
"""
Rodar:  py -3.11 -m unittest test_canais_boleto
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "portal_cliente"))

import notificar_whatsapp as nw  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402
import chamados as ch  # noqa: E402


class Preferencia(unittest.TestCase):
    def test_tipo_novo_antes_do_whatsapp(self):
        self.assertIn("boleto_faltando", pn.TIPOS)
        self.assertTrue(pn.TIPOS["boleto_faltando"]["default"])
        self.assertEqual(list(pn.TIPOS)[-1], "chamado_sem_resposta")

    def test_whatsapp_por_tipo(self):
        c = sqlite3.connect(":memory:")
        pn._garantir_tabela(c)
        c.execute("INSERT INTO preferencias_notificacao (cnpj_embarcador, whatsapp, boleto_faltando, "
                  "chamado_sem_resposta) VALUES ('1', '5511987654321', 0, 1)")
        self.assertIsNone(pn.whatsapp_do_embarcador(c, "1", tipo="boleto_faltando"))
        self.assertEqual(pn.whatsapp_do_embarcador(c, "1"), "5511987654321")


class TextoWhatsApp(unittest.TestCase):
    def test_cabe_e_corta_codigos(self):
        cods = [f"PS-4{i:04d}" for i in range(40)]
        t = nw.texto_boleto_faltando(cods, "amanhã")
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        self.assertIn("40 pedidos de amanhã sem boleto", t)
        self.assertIn("PS-40000", t)
        self.assertIn("+", t)

    def test_um_pedido(self):
        t = nw.texto_boleto_faltando(["PS-1"], "hoje")
        self.assertIn("1 pedido de hoje sem boleto (PS-1)", t)


class AvisoWhatsApp(unittest.TestCase):
    def _cfg(self, forcar=""):
        # clientes_ligado tambem exige o gateway configurado (base_url, api_key, sessao)
        return {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                          "clientes": {"ativo": True, "forcar_destino": forcar}}}

    def test_desligado_sem_telefone(self):
        self.assertEqual(nw.avisar_cliente_boleto_faltando("k", "", "t", self._cfg()), "desligado")

    def test_forcar_destino_prefixa_e_redireciona(self):
        with patch.object(nw.integracao_openwa, "numero_existe", return_value=True), \
             patch.object(nw, "despachar", return_value="enviado") as d:
            r = nw.avisar_cliente_boleto_faltando("boleto:11:2026-10-07", "5511911112222", "texto",
                                                  self._cfg("5511999990000"))
        self.assertEqual(r, "enviado")
        self.assertTrue(d.call_args.args[3].startswith("[teste → +5511911112222]"))
        self.assertEqual(d.call_args.kwargs["grupo_id"], "5511999990000@c.us")
        self.assertEqual(d.call_args.args[4], "boleto:11:2026-10-07")


class AreaChamado(unittest.TestCase):
    def test_area_boleto(self):
        self.assertEqual(ch.AREAS["boleto"], "Boleto")


if __name__ == "__main__":
    unittest.main()
