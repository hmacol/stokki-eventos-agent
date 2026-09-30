"""Testes do e-mail de resumo das rotinas (notificar_execucao_agente.py).

Rodar: py -3.11 -m unittest test_notificar_execucao_agente
"""

import unittest
from datetime import datetime
from unittest.mock import patch

import notificar_execucao_agente as nea

AGORA = datetime(2026, 9, 16, 22, 5)
UMA = {"Criação de rotas": {"status": "ok", "detalhe": "12 rotas criadas."}}
VARIAS_OK = {
    "Agendamento": {"status": "ok", "detalhe": "3 pedidos agendados"},
    "Impressão": {"status": "ok", "detalhe": "Nada a imprimir"},
    "Pipeline": {"status": "ok", "detalhe": "40 pedido(s) — criados: 12, atualizados: 28"},
}
VARIAS_ERRO = dict(VARIAS_OK, Pipeline={"status": "erro", "detalhe": "TimeoutError: <stokki>\nlinha 2"},
                   Retiradas={"status": "erro", "detalhe": "401"})


class TestTexto(unittest.TestCase):
    def test_titulo_uma_etapa_e_o_nome_dela(self):
        self.assertEqual(nea.titulo_da_rotina(UMA), "Criação de rotas")

    def test_titulo_explicito_vence(self):
        self.assertEqual(nea.titulo_da_rotina(UMA, "Outro"), "Outro")

    def test_titulo_varias_etapas_usa_o_script(self):
        with patch.object(nea.sys, "argv", ["/opt/x/executar_tudo.py"]):
            self.assertTrue(nea.titulo_da_rotina(VARIAS_OK).startswith("Execução completa"))
        with patch.object(nea.sys, "argv", ["desconhecido.py"]):
            self.assertEqual(nea.titulo_da_rotina(VARIAS_OK), "Agente Stokki Eventos")

    def test_duracao(self):
        self.assertEqual(nea.formatar_duracao(42), "42 s")
        self.assertEqual(nea.formatar_duracao(138), "2,3 min")
        self.assertEqual(nea.formatar_duracao(2 * 3600), "2,0 h")

    def test_resumo(self):
        self.assertEqual(nea.resumir_execucao(UMA), "Etapa concluída sem erro.")
        self.assertEqual(nea.resumir_execucao(VARIAS_OK), "3 etapas concluídas sem erro.")
        self.assertEqual(nea.resumir_execucao(VARIAS_ERRO), "2 de 4 etapas com erro: Pipeline, Retiradas.")


class TestAssunto(unittest.TestCase):
    def test_uma_etapa_ok(self):
        self.assertEqual(nea.montar_assunto(UMA, False, agora=AGORA),
                         "Agente Stokki Eventos · Criação de rotas — ✅ OK (16/09 22:05)")

    def test_varias_com_erro_nomeia_as_etapas(self):
        with patch.object(nea.sys, "argv", ["desconhecido.py"]):
            self.assertEqual(nea.montar_assunto(VARIAS_ERRO, False, agora=AGORA),
                             "Agente Stokki Eventos — ❌ erro em Pipeline, Retiradas (16/09 22:05)")

    def test_modo_teste_tem_prefixo(self):
        self.assertTrue(nea.montar_assunto(UMA, True, agora=AGORA).startswith("[MODO TESTE] "))


class TestHtml(unittest.TestCase):
    def test_ok_tem_logo_titulo_e_etapas(self):
        html = nea.montar_html(VARIAS_OK, 138, False, titulo="Sequência", agora=AGORA)
        self.assertIn("cid:logo_freshlog", html)
        self.assertIn("Sequência", html)
        self.assertIn("16/09/2026 às 22:05", html)
        self.assertIn("2,3 min", html)
        self.assertIn("3 etapas concluídas sem erro.", html)
        self.assertEqual(html.count(">OK<"), 3)
        self.assertNotIn(">ERRO<", html)

    def test_erro_escapa_e_quebra_linha(self):
        html = nea.montar_html(VARIAS_ERRO, 10, False, titulo="X", agora=AGORA)
        self.assertEqual(html.count(">ERRO<"), 2)
        self.assertNotIn("<stokki>", html)
        self.assertIn("&lt;stokki&gt;<br>linha 2", html)
        self.assertIn("Com erro", html)

    def test_modo_teste_tem_pilula(self):
        self.assertIn("Modo teste", nea.montar_html(UMA, 1, True, agora=AGORA))


class TestEnvio(unittest.TestCase):
    def test_desligado_nao_envia(self):
        with patch.object(nea, "enviar_email") as env:
            nea.notificar_execucao(UMA, 1, False, {"notificacao_execucao": {"ativo": False}})
        env.assert_not_called()

    def test_envia_pro_destinatario_configurado(self):
        cfg = {"notificacao_execucao": {"destinatario": "a@b.c"}, "email": {"remetente": "x@y.z"}}
        with patch.object(nea, "enviar_email", return_value=True) as env:
            nea.notificar_execucao(UMA, 1, False, cfg)
        destinatarios, assunto, corpo, config_email = env.call_args.args
        self.assertEqual(destinatarios, ["a@b.c"])
        self.assertIn("Criação de rotas", assunto)
        self.assertIn("12 rotas criadas.", corpo)
        self.assertIs(config_email, cfg["email"])

    def test_excecao_no_envio_nao_propaga(self):
        with patch.object(nea, "enviar_email", side_effect=RuntimeError("smtp")):
            nea.notificar_execucao(UMA, 1, False, {"email": {}})


if __name__ == "__main__":
    unittest.main()
