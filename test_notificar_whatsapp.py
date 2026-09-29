"""Testes das notificacoes internas por WhatsApp (notificar_whatsapp.py).

Rodar: py -3.11 -m unittest test_notificar_whatsapp
"""

import unittest
from datetime import datetime
from unittest.mock import patch

import notificar_whatsapp as nw

AGORA = datetime(2026, 9, 16, 22, 5)
UMA = {"Criação de rotas": {"status": "ok", "detalhe": "12 rotas criadas."}}
VARIAS_OK = {
    "Agendamento": {"status": "ok", "detalhe": "3 pedidos agendados"},
    "Impressão": {"status": "ok", "detalhe": "Nada a imprimir"},
    "Pipeline": {"status": "ok", "detalhe": "40 pedido(s)"},
}
VARIAS_ERRO = dict(VARIAS_OK, Pipeline={"status": "erro", "detalhe": "TimeoutError: <stokki>\nlinha 2"},
                   Retiradas={"status": "erro", "detalhe": "401"})


class TestTextoExecucao(unittest.TestCase):
    def test_sucesso(self):
        self.assertEqual(nw.texto_execucao(UMA, 42, agora=AGORA),
                         "✅ *Criação de rotas* · 16/09 22:05\nEtapa concluída sem erro (42 s)")

    def test_erro_lista_etapas_e_aponta_o_email(self):
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            texto = nw.texto_execucao(VARIAS_ERRO, 138, agora=AGORA)
        self.assertEqual(texto, "\n".join([
            "❌ *Agente Stokki Eventos* · 16/09 22:05",
            "2 de 4 etapas com erro: Pipeline, Retiradas",
            "Pipeline: TimeoutError: <stokki> linha 2",
            "Retiradas: 401",
            "Detalhes completos no e-mail.",
        ]))

    def test_detalhe_longo_e_cortado_em_200(self):
        etapas = {"Pipeline": {"status": "erro", "detalhe": "x" * 500}}
        linha = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")[2]
        self.assertEqual(len(linha), len("Pipeline: ") + 200)
        self.assertTrue(linha.endswith("…"))

    def test_detalhe_perde_formatacao_do_whatsapp(self):
        etapas = {"Pipeline": {"status": "erro", "detalhe": "erro *grave* em _campo_ ~x~ `y`"}}
        self.assertIn("Pipeline: erro grave em campo x y", nw.texto_execucao(etapas, 1, agora=AGORA))

    def test_no_maximo_tres_etapas_com_detalhe(self):
        etapas = {f"E{i}": {"status": "erro", "detalhe": f"d{i}"} for i in range(5)}
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            linhas = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")
        self.assertEqual(linhas[2:5], ["E0: d0", "E1: d1", "E2: d2"])
        self.assertEqual(len(linhas), 6)

    def test_etapa_com_erro_sem_detalhe_nao_gera_linha_vazia(self):
        etapas = {"Pipeline": {"status": "erro", "detalhe": ""}, "Outra": None}
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            linhas = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")
        self.assertEqual(linhas[1:], ["2 de 2 etapas com erro: Pipeline, Outra", "Detalhes completos no e-mail."])

    def test_resumo_vazio_ou_none_nao_quebra(self):
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            for vazio in ({}, None):
                self.assertEqual(nw.texto_execucao(vazio, 0, agora=AGORA),
                                 "✅ *Agente Stokki Eventos* · 16/09 22:05\nNenhuma etapa reportada (0 s)")


class TestTextoFalhaJob(unittest.TestCase):
    def test_formato(self):
        info = {"Result": "exit-code", "ExecMainStatus": "1"}
        self.assertEqual(nw.texto_falha_job("stokki-backup-gcs.service", info, agora=AGORA), "\n".join([
            "🚨 *Job da VPS falhou*",
            "stokki-backup-gcs.service",
            "Resultado: exit-code (código 1) · 16/09 22:05",
            "Log completo no e-mail.",
        ]))

    def test_info_vazio(self):
        self.assertIn("Resultado: ? (código ?)", nw.texto_falha_job("x.service", {}, agora=AGORA))
        self.assertIn("Resultado: ? (código ?)", nw.texto_falha_job("x.service", None, agora=AGORA))


class TestTextoNaoExpedidos(unittest.TestCase):
    def test_completo(self):
        self.assertEqual(nw.texto_nao_expedidos(4, 2, 1), "\n".join([
            "⚠️ *Checagem da expedição*",
            "4 pedidos entregues sem expedição na Stokki",
            "2 rotas sem terminar · 1 retirada parada há mais de 7 dias",
            "Lista completa no e-mail.",
        ]))

    def test_singular_e_plural(self):
        self.assertIn("1 pedido entregue sem expedição", nw.texto_nao_expedidos(1, 0, 0))
        self.assertIn("1 rota sem terminar · 3 retiradas paradas há mais de 7 dias",
                      nw.texto_nao_expedidos(0, 1, 3))

    def test_contagem_zero_e_omitida(self):
        self.assertEqual(nw.texto_nao_expedidos(0, 2, 0), "\n".join([
            "⚠️ *Checagem da expedição*",
            "2 rotas sem terminar",
            "Lista completa no e-mail.",
        ]))
        self.assertNotIn("rota", nw.texto_nao_expedidos(3, 0, 0))


if __name__ == "__main__":
    unittest.main()
