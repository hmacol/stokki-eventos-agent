# -*- coding: utf-8 -*-
"""py -3.11 -m unittest notificacao_entregas.test_montar_email_entrega"""
import unittest

from notificacao_entregas import montar_email_entrega as m

EMBARCADOR = {"nome": "Fruta Boa", "emails": ["log@frutaboa.com"], "fator_ponderado": 0.5}
PORTAL = "https://app.freshhub.com.br/cliente"


def _servico(**extra) -> dict:
    s = {
        "id": 111, "code": "#PS-36327", "title": "NF 123 / titulo", "sender_id": 900,
        "status": "done", "status_done": "success", "completed_at": "2026-09-17 12:22:19",
        "address": "Rua das Flores 120, Vila P, Santo Andre - SP, 09190-250, Brasil",
        "address_complement": "Doca 3", "dimension_3": "6.0", "note": "Entregar na doca",
        "customer": {"data": {"name": "Mercado Bom Preço"}},
        "failed_reason_id": None,
    }
    s.update(extra)
    return s


class TestDadosDoServico(unittest.TestCase):
    def test_sucesso(self):
        d = m.dados_do_servico(_servico(), EMBARCADOR, nf="4567", com_canhoto=True)
        self.assertEqual(d["codigo"], "PS-36327")
        self.assertTrue(d["sucesso"])
        self.assertEqual(d["destinatario"], "Mercado Bom Preço")
        self.assertEqual(d["concluido_em"], "17/09/2026 09:22")
        self.assertEqual(d["volumes"], 12)          # 6.0 ponderado / fator 0.5
        self.assertEqual(d["nf"], "4567")
        self.assertIn("Doca 3", d["endereco"])
        self.assertFalse(d["reentrega"])

    def test_destinatario_cai_no_titulo_sem_customer(self):
        d = m.dados_do_servico(_servico(customer=None), EMBARCADOR, nf="", com_canhoto=False)
        self.assertEqual(d["destinatario"], "NF 123 / titulo")

    def test_falha_motivo_do_de_para(self):
        d = m.dados_do_servico(_servico(status_done="failed", failed_reason_id=8490), EMBARCADOR, "", False)
        self.assertFalse(d["sucesso"])
        self.assertEqual(d["motivo"], "Cliente ausente")

    def test_falha_motivo_novo_usa_descricao_da_vuupt_sem_texto_interno(self):
        s = _servico(status_done="failed", failed_reason_id=999999,
                     failedReason={"data": {"description": "Portaria recusou"}})
        d = m.dados_do_servico(s, EMBARCADOR, "", False)
        self.assertEqual(d["motivo"], "Portaria recusou")
        s = _servico(status_done="failed", failed_reason_id=999999, failedReason={"description": "Direto"})
        self.assertEqual(m.dados_do_servico(s, EMBARCADOR, "", False)["motivo"], "Direto")

    def test_falha_sem_motivo(self):
        d = m.dados_do_servico(_servico(status_done="failed", failed_reason_id=None), EMBARCADOR, "", False)
        self.assertEqual(d["motivo"], "Motivo não informado")
        d = m.dados_do_servico(_servico(status_done="failed", failed_reason_id=999998), EMBARCADOR, "", False)
        self.assertEqual(d["motivo"], "Motivo não informado")

    def test_reentrega(self):
        d = m.dados_do_servico(_servico(code="#PS-36327-R2"), EMBARCADOR, "", False)
        self.assertTrue(d["reentrega"])
        self.assertEqual(d["codigo"], "PS-36327-R2")


class TestAssunto(unittest.TestCase):
    def test_sucesso(self):
        d = m.dados_do_servico(_servico(), EMBARCADOR, "4567", True)
        self.assertEqual(m.montar_assunto(d), "Pedido PS-36327 entregue · Mercado Bom Preço")

    def test_falha(self):
        d = m.dados_do_servico(_servico(status_done="failed", failed_reason_id=5431), EMBARCADOR, "", False)
        self.assertEqual(m.montar_assunto(d), "Pedido PS-36327 não entregue · Local fechado")

    def test_destinatario_longo_e_cortado(self):
        d = m.dados_do_servico(_servico(customer={"data": {"name": "X" * 200}}), EMBARCADOR, "", True)
        self.assertLessEqual(len(m.montar_assunto(d)), 110)


class TestHtml(unittest.TestCase):
    def test_sucesso_com_canhoto(self):
        d = m.dados_do_servico(_servico(), EMBARCADOR, "4567", com_canhoto=True)
        h = m.montar_html(d, PORTAL)
        for trecho in ("ENTREGUE", "PS-36327", "4567", "Mercado Bom Preço", "17/09/2026 09:22",
                       "Rua das Flores 120", "Entregar na doca", "anexo", PORTAL, "cid:logo_freshlog"):
            self.assertIn(trecho, h)
        self.assertIn(">12<", h)                 # volumes reais
        self.assertNotIn("NÃO ENTREGUE", h)
        self.assertNotIn("Motivo", h)

    def test_sucesso_sem_canhoto_avisa_do_portal(self):
        d = m.dados_do_servico(_servico(), EMBARCADOR, "", com_canhoto=False)
        h = m.montar_html(d, PORTAL)
        self.assertIn("ainda não foi processado", h)
        self.assertNotIn("segue em anexo", h)

    def test_linhas_vazias_somem(self):
        d = m.dados_do_servico(_servico(note="", dimension_3=None), EMBARCADOR, "", True)
        h = m.montar_html(d, PORTAL)
        self.assertNotIn("Nota fiscal", h)
        self.assertNotIn("Volumes", h)
        self.assertNotIn("Observações", h)

    def test_falha_mostra_motivo_e_faixa_vermelha(self):
        d = m.dados_do_servico(_servico(status_done="failed", failed_reason_id=8490), EMBARCADOR, "", False)
        h = m.montar_html(d, PORTAL, promete_email_reenvio=True)
        self.assertIn("NÃO ENTREGUE", h)
        self.assertIn("Cliente ausente", h)
        self.assertIn(m.COR_ERRO, h)
        self.assertIn("decidir sobre o reenvio", h)
        self.assertNotIn("canhoto", h.lower())

    def test_falha_nao_promete_email_de_reenvio_quando_ele_esta_desligado(self):
        d = m.dados_do_servico(_servico(status_done="failed", failed_reason_id=8490), EMBARCADOR, "", False)
        h = m.montar_html(d, PORTAL, promete_email_reenvio=False)
        self.assertNotIn("decidir sobre o reenvio", h)
        self.assertIn("responda este e-mail", h)

    def test_escapa_html_de_dado_externo(self):
        s = _servico(customer={"data": {"name": "<script>x</script>"}}, note="a <b>b</b>")
        h = m.montar_html(m.dados_do_servico(s, EMBARCADOR, "", True), PORTAL)
        self.assertNotIn("<script>", h)
        self.assertNotIn("<b>b</b>", h)

    def test_aviso_de_teste_no_topo(self):
        d = m.dados_do_servico(_servico(), EMBARCADOR, "", True)
        h = m.montar_html(d, PORTAL, aviso_topo="iria para log@frutaboa.com")
        self.assertIn("iria para log@frutaboa.com", h)

    def test_nao_cita_motorista(self):
        s = _servico(agent={"data": {"name": "Joao Motorista"}}, driver_id=5)
        h = m.montar_html(m.dados_do_servico(s, EMBARCADOR, "", True), PORTAL)
        self.assertNotIn("Joao", h)
        self.assertNotIn("otorista", h)


NOTA_RETIRADA = ('RETIRADA NO GALPÃO -- não roteirizar. Quem retira: TRANSPORTES S.A. LTDA (CNPJ 12345678000190). Dest'
                 "inatário final: MERCADO BOM (98.765.432/0001-10). Fechado automaticamente quando a Stokki marcar 'Enviado'.")


def _retirada(**extra) -> dict:
    base = dict(title="[RETIRADA] #PS-36327 - REF / Fruta Boa / MERCADO BOM / via TRANSPORTES S.A. LTDA",
                note=NOTA_RETIRADA, address="Av do Galpao 1, Sao Paulo - SP", address_complement="",
                customer={"data": {"name": "GALPAO FRESHLOG"}}, checklistAnswers={"data": []})
    return _servico(**{**base, **extra})


class TestRetirada(unittest.TestCase):
    def test_dados_vem_da_nota_e_nao_do_customer_que_e_o_galpao(self):
        d = m.dados_do_servico(_retirada(), EMBARCADOR, "4567", com_canhoto=False)
        self.assertEqual(d["tipo"], "retirada")
        self.assertEqual(d["destinatario"], "MERCADO BOM")
        self.assertEqual(d["quem_retira"], "TRANSPORTES S.A. LTDA")
        self.assertEqual(d["endereco"], "")
        self.assertEqual(d["observacoes"], "")     # a nota da retirada e texto interno
        self.assertEqual(d["volumes"], 12)

    def test_assunto(self):
        d = m.dados_do_servico(_retirada(), EMBARCADOR, "", False)
        self.assertEqual(m.montar_assunto(d), "Pedido PS-36327 retirado · TRANSPORTES S.A. LTDA")

    def test_html(self):
        h = m.montar_html(m.dados_do_servico(_retirada(), EMBARCADOR, "4567", False), PORTAL)
        for trecho in ("RETIRADO", "foi retirado", "TRANSPORTES S.A. LTDA", "MERCADO BOM", "4567", "Retirado por",
                       "Destinatário final", "17/09/2026 09:22"):
            self.assertIn(trecho, h)
        for proibido in ("não roteirizar", "GALPAO FRESHLOG", "Av do Galpao", "canhoto", "Fechado automaticamente",
                         "12345678000190"):
            self.assertNotIn(proibido, h)

    def test_nome_generico_de_quem_retira_nao_aparece(self):
        # na planilha BD_TRANSPORTADORAS esses "transportadores" sao rotulos, nao empresas
        for generico in ("CLIENTE RETIRA", "cliente", "Retirada Pessoal", "COLETA FABRICA", "TRANSPORTADORA COLETA"):
            nota = f"Quem retira: {generico}. Destinatário final: LOJA Y. Fechado automaticamente"
            d = m.dados_do_servico(_retirada(note=nota), EMBARCADOR, "", False)
            self.assertEqual(d["quem_retira"], "", generico)
            self.assertEqual(m.montar_assunto(d), "Pedido PS-36327 retirado · LOJA Y")
            h = m.montar_html(d, PORTAL)
            self.assertNotIn("Retirado por", h)
            self.assertIn("foi retirado no nosso galpão, com saída", h)   # sem "por <quem>"

    def test_nota_fora_do_formato_ainda_monta_email(self):
        d = m.dados_do_servico(_retirada(note="outra coisa"), EMBARCADOR, "", False)
        self.assertEqual((d["quem_retira"], d["destinatario"]), ("", ""))
        self.assertEqual(m.montar_assunto(d), "Pedido PS-36327 retirado")
        self.assertIn("foi retirado", m.montar_html(d, PORTAL))

    def test_entrega_normal_tem_tipo_entrega(self):
        self.assertEqual(m.dados_do_servico(_servico(), EMBARCADOR, "", True)["tipo"], "entrega")


if __name__ == "__main__":
    unittest.main()
