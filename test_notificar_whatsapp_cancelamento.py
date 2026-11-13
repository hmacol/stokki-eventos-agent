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


ITENS_DEV = [{"sku": "FABMLT", "nome": "Fabenne Merlot", "movido": 4.0, "destino": "FABENNE-1"},
             {"sku": "FABMO1", "nome": "Fabenne Moscato", "movido": 2.5, "destino": "FABENNE-1"}]
CFG_DEV = {"whatsapp_notificacoes": {"ativo": True, "grupo_id": "g@g.us", "grupo_atendimento_id": "atend@g.us",
                                     "grupo_cancelados_id": "canc@g.us"}}


class TextoDevolucaoGalpao(unittest.TestCase):
    def test_um_item_uma_mensagem_sem_indice(self):
        ts = nw.textos_devolucao_galpao(43101, "FABENNE", ITENS_DEV)
        self.assertEqual(len(ts), 1)
        t = ts[0]
        self.assertTrue(t.startswith("Pedido cancelado PS-43101 · FABENNE — guardar de volta:"))
        self.assertIn("• FABMLT Fabenne Merlot — 4 → FABENNE-1", t)
        self.assertIn("• FABMO1 Fabenne Moscato — 2,5 → FABENNE-1", t)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)

    def _itens(self, n):
        return [{"sku": f"SKU{i:03d}", "nome": "Produto", "movido": i + 1, "destino": f"FABENNE-{i}"} for i in range(n)]

    def test_muitos_itens_em_partes_numeradas_sem_perder_nenhum(self):
        for n in (6, 15):
            ts = nw.textos_devolucao_galpao(43101, "FABENNE", self._itens(n))
            self.assertGreater(len(ts), 1)
            for k, t in enumerate(ts, 1):
                self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
                self.assertTrue(t.startswith(f"Pedido cancelado PS-43101 · FABENNE ({k}/{len(ts)}) — guardar de volta:"))
                self.assertNotIn("e mais", t)
            todo = "\n".join(ts)
            for i in range(n):
                self.assertIn(f"SKU{i:03d}", todo)
                self.assertIn(f"→ FABENNE-{i}", todo)

    def test_embarcador_e_produto_enormes_cabem_e_preservam_sku_qtd_endereco(self):
        itens = [{"sku": "SKU1", "nome": "P" * 120, "movido": 7, "destino": "FABENNE-1"}]
        ts = nw.textos_devolucao_galpao(43101, "E" * 80, itens)
        for t in ts:
            self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        todo = "\n".join(ts)
        self.assertIn("SKU1", todo)
        self.assertIn("— 7 → FABENNE-1", todo)
        self.assertIn("…", todo)


class AvisarDevolucaoGalpao(unittest.TestCase):
    def test_sem_grupo_cancelados_desligado(self):
        config = {"whatsapp_notificacoes": {"ativo": True, "grupo_id": "g@g.us"}}
        with mock.patch.object(nw, "despachar") as d:
            self.assertEqual(nw.avisar_devolucao_galpao(43101, "FABENNE", ITENS_DEV, config), "desligado")
        d.assert_not_called()

    def test_vai_pro_grupo_cancelados(self):
        with mock.patch.object(nw, "despachar", return_value="enviado") as d:
            self.assertEqual(nw.avisar_devolucao_galpao(43101, "FABENNE", ITENS_DEV, CFG_DEV), "enviado")
        self.assertEqual(d.call_args.kwargs["grupo_id"], "canc@g.us")
        self.assertEqual(d.call_args.args[1:3], ("devolucao_estoque", "devolucao"))
        self.assertTrue(d.call_args.args[4].startswith("devolucao:43101:"))

    def test_assinatura_distingue_segundo_aviso_do_mesmo_pedido(self):
        ass = []
        with mock.patch.object(nw, "despachar", side_effect=lambda *a, **k: ass.append(a[4]) or "enviado"):
            nw.avisar_devolucao_galpao(43101, "F", ITENS_DEV[:1], CFG_DEV)
            nw.avisar_devolucao_galpao(43101, "F", ITENS_DEV[1:], CFG_DEV)
            nw.avisar_devolucao_galpao(43101, "F", ITENS_DEV[:1], CFG_DEV)
        self.assertNotEqual(ass[0], ass[1])
        self.assertEqual(ass[0], ass[2])

    def test_partes_tem_assinatura_propria_e_param_na_primeira_que_nao_saiu(self):
        itens = [{"sku": f"SKU{i:03d}", "nome": "Produto", "movido": 1, "destino": f"FABENNE-{i}"} for i in range(15)]
        retornos = iter(["enviado", "nao_enviado", "enviado"])
        ass = []
        with mock.patch.object(nw, "despachar", side_effect=lambda *a, **k: ass.append(a[4]) or next(retornos)):
            self.assertEqual(nw.avisar_devolucao_galpao(43101, "F", itens, CFG_DEV), "nao_enviado")
        self.assertEqual(len(ass), 2)
        self.assertNotEqual(ass[0], ass[1])
        self.assertTrue(ass[0].endswith(":1") and ass[1].endswith(":2"))

    def test_todas_enviadas_devolve_enviado(self):
        itens = [{"sku": f"SKU{i:03d}", "nome": "Produto", "movido": 1, "destino": "FABENNE-1"} for i in range(15)]
        with mock.patch.object(nw, "despachar", return_value="enviado") as d:
            self.assertEqual(nw.avisar_devolucao_galpao(43101, "F", itens, CFG_DEV), "enviado")
        self.assertGreater(d.call_count, 1)

    def test_nunca_levanta(self):
        with mock.patch.object(nw, "despachar", side_effect=RuntimeError("x")):
            self.assertEqual(nw.avisar_devolucao_galpao(1, "F", ITENS_DEV, CFG_DEV), "falhou")
        self.assertEqual(nw.avisar_devolucao_galpao(1, "F", None, CFG_DEV), "falhou")


class AvisarDevolucaoPendente(unittest.TestCase):
    def test_texto_truncado(self):
        t = nw.texto_devolucao_pendente(43101, "FABENNE", "motivo " * 100)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        self.assertTrue(t.startswith("Devolução de estoque do PS-43101 · FABENNE precisa de conferência: "))
        self.assertTrue(t.endswith("…"))

    def test_vai_pro_grupo_do_atendimento(self):
        with mock.patch.object(nw, "despachar", return_value="enviado") as d:
            self.assertEqual(nw.avisar_devolucao_pendente(43101, "FABENNE", "duvida", CFG_DEV), "enviado")
        self.assertEqual(d.call_args.kwargs["grupo_id"], "atend@g.us")
        self.assertEqual(d.call_args.args[1:3], ("devolucao_estoque", "devolucao_pendente"))
        self.assertTrue(d.call_args.args[4].startswith("devolucao_pendente:43101:"))

    def test_assinatura_distingue_motivos_e_repete_o_mesmo(self):
        ass = []
        with mock.patch.object(nw, "despachar", side_effect=lambda *a, **k: ass.append(a[4]) or "enviado"):
            nw.avisar_devolucao_pendente(43101, "F", "parcial", CFG_DEV)
            nw.avisar_devolucao_pendente(43101, "F", "duvida", CFG_DEV)
            nw.avisar_devolucao_pendente(43101, "F", "parcial", CFG_DEV)
        self.assertNotEqual(ass[0], ass[1])
        self.assertEqual(ass[0], ass[2])

    def test_sem_grupo_atendimento_desligado(self):
        with mock.patch.object(nw, "despachar") as d:
            self.assertEqual(nw.avisar_devolucao_pendente(1, "F", "x", {"whatsapp_notificacoes": {}}), "desligado")
        d.assert_not_called()


if __name__ == "__main__":
    unittest.main()
