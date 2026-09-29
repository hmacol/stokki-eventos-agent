"""Testes das notificacoes internas por WhatsApp (notificar_whatsapp.py).

Rodar: py -3.11 -m unittest test_notificar_whatsapp
"""

import unittest
from datetime import datetime
from unittest.mock import patch
import sqlite3
from datetime import timedelta
from unittest.mock import MagicMock

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


def _config(**extra):
    wa = {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s",
          "grupo_id": "1@g.us", "sempre_avisar": ["criar_rotas_diarias"], "teto_diario": 3,
          "intervalo_min_seg": 20, "janela_repeticao_min": 120, "falhas_para_alerta": 3}
    wa.update(extra)
    return {"whatsapp_notificacoes": wa, "email": {"remetente": "a@b.com"}}


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.dormir = MagicMock()
        self.envio = patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1"))
        self.enviar = self.envio.start()
        self.addCleanup(self.envio.stop)
        self.addCleanup(self.conn.close)

    def despachar(self, config=None, origem="rotina", assinatura=None, agora=AGORA, **kw):
        return nw.despachar(config or _config(), origem, "execucao", "texto", assinatura,
                            conn=self.conn, agora=agora, dormir=self.dormir, **kw)

    def linhas(self):
        return self.conn.execute(
            "SELECT origem, situacao, motivo, id_mensagem FROM notificacoes_whatsapp ORDER BY id").fetchall()


class TestDespacharChave(_ComBanco):
    def test_envia_e_registra(self):
        self.assertEqual(self.despachar(), "enviado")
        self.enviar.assert_called_once_with(_config()["whatsapp_notificacoes"], "1@g.us", "texto")
        self.assertEqual(self.linhas(), [("rotina", "enviado", None, "m1")])

    def test_desligado_nao_envia_nem_cria_tabela(self):
        for config in (_config(ativo=False), _config(grupo_id=""), _config(api_key=""), {}, None,
                       {"whatsapp_notificacoes": None}):
            self.assertEqual(nw.despachar(config, "r", "execucao", "t", conn=self.conn), "desligado")
        self.enviar.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)

    def test_modo_teste_so_loga(self):
        with self.assertLogs(nw.logger, level="INFO") as logs:
            self.assertEqual(self.despachar(modo_teste=True), "modo_teste")
        self.assertIn("texto", "\n".join(logs.output))
        self.enviar.assert_not_called()


class TestDespacharVolume(_ComBanco):
    def test_repeticao_dentro_da_janela(self):
        self.assertEqual(self.despachar(assinatura="erro:Pipeline"), "enviado")
        depois = AGORA + timedelta(minutes=119)
        self.assertEqual(self.despachar(assinatura="erro:Pipeline", agora=depois), "nao_enviado")
        self.assertEqual(self.linhas()[-1][1:3], ("nao_enviado", "repetida dentro da janela"))

    def test_repeticao_fora_da_janela_ou_de_outra_origem_envia(self):
        self.despachar(assinatura="erro:Pipeline")
        self.assertEqual(self.despachar(assinatura="erro:Pipeline", origem="outra",
                                        agora=AGORA + timedelta(minutes=5)), "enviado")
        self.assertEqual(self.despachar(assinatura="erro:Pipeline",
                                        agora=AGORA + timedelta(minutes=121)), "enviado")

    def test_sem_assinatura_nao_tem_regra_de_repeticao(self):
        self.despachar()
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=5)), "enviado")

    def test_teto_diario(self):
        for i in range(3):
            self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=i)), "enviado")
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=10)), "nao_enviado")
        self.assertEqual(self.linhas()[-1][2], "teto diario atingido")
        amanha = AGORA + timedelta(days=1)
        self.assertEqual(self.despachar(agora=amanha), "enviado")

    def test_intervalo_minimo_espera_a_diferenca(self):
        self.despachar()
        self.dormir.assert_not_called()
        self.despachar(agora=AGORA + timedelta(seconds=5))
        self.dormir.assert_called_once_with(15.0)

    def test_intervalo_ja_cumprido_nao_espera(self):
        self.despachar()
        self.despachar(agora=AGORA + timedelta(seconds=25))
        self.dormir.assert_not_called()

    def test_numeros_malformados_caem_no_padrao(self):
        config = _config(teto_diario="vinte", intervalo_min_seg=None, janela_repeticao_min="x",
                         falhas_para_alerta=[])
        self.assertEqual(self.despachar(config=config), "enviado")


class TestDespacharFalhas(_ComBanco):
    def setUp(self):
        super().setUp()
        self.enviar.return_value = (False, None)
        self.email = patch("email_utils.enviar_email", return_value=True)
        self.enviar_email = self.email.start()
        self.addCleanup(self.email.stop)

    def falhar(self, n, inicio=0):
        for i in range(n):
            self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=inicio + i)), "falhou")

    def test_falha_registra_e_nao_reenvia(self):
        self.falhar(1)
        self.assertEqual(self.enviar.call_count, 1)
        self.assertEqual(self.linhas()[0][1], "falhou")

    def test_email_unico_na_terceira_falha_seguida(self):
        self.falhar(2)
        self.enviar_email.assert_not_called()
        self.falhar(1, inicio=2)
        self.enviar_email.assert_called_once()
        self.assertEqual(self.enviar_email.call_args[0][0], ["a@b.com"])
        self.falhar(3, inicio=3)
        self.enviar_email.assert_called_once()

    def test_sucesso_no_meio_zera_a_contagem(self):
        self.falhar(2)
        self.enviar.return_value = (True, "m")
        self.despachar(agora=AGORA + timedelta(minutes=5))
        self.enviar.return_value = (False, None)
        self.falhar(2, inicio=6)
        self.enviar_email.assert_not_called()
        self.falhar(1, inicio=8)
        self.enviar_email.assert_called_once()

    def test_falha_nao_conta_para_o_teto(self):
        self.falhar(5)
        self.enviar.return_value = (True, "m")
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=30)), "enviado")


class TestDespacharNuncaLevanta(unittest.TestCase):
    def test_banco_indisponivel(self):
        conn = MagicMock()
        conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        with patch.object(nw.integracao_openwa, "enviar_texto") as enviar:
            self.assertEqual(nw.despachar(_config(), "r", "execucao", "t", conn=conn, agora=AGORA), "falhou")
        enviar.assert_not_called()

    def test_excecao_no_transporte(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with patch.object(nw.integracao_openwa, "enviar_texto", side_effect=RuntimeError("bug")):
            self.assertEqual(nw.despachar(_config(), "r", "execucao", "t", conn=conn, agora=AGORA,
                                          dormir=MagicMock()), "falhou")


class TestAvisarExecucao(_ComBanco):
    def avisar(self, etapas, script, config=None, modo_teste=False):
        with patch.object(nw.sys, "argv", [script]), \
                patch("notificar_execucao_agente.sys.argv", [script]):
            return nw.avisar_execucao(etapas, 10, modo_teste, config or _config(),
                                      conn=self.conn, agora=AGORA, dormir=self.dormir)

    def test_sucesso_fora_da_lista_nao_envia_nem_registra(self):
        self.assertEqual(self.avisar(UMA, "/opt/x/expedir_pedidos.py"), "nao_relevante")
        self.enviar.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)

    def test_sucesso_de_script_da_lista_envia(self):
        self.assertEqual(self.avisar(UMA, "/opt/x/roteirizacao/criar_rotas_diarias.py"), "enviado")
        self.assertEqual(self.linhas()[0][0], "criar_rotas_diarias")
        self.assertTrue(self.enviar.call_args[0][2].startswith("✅ *Criação de rotas*"))

    def test_erro_de_qualquer_script_envia_com_assinatura(self):
        self.assertEqual(self.avisar(VARIAS_ERRO, "expedir_pedidos.py"), "enviado")
        assinatura = self.conn.execute("SELECT assinatura FROM notificacoes_whatsapp").fetchone()[0]
        self.assertEqual(assinatura, "erro:Pipeline,Retiradas")

    def test_mesmo_erro_de_novo_nao_repete(self):
        self.avisar(VARIAS_ERRO, "expedir_pedidos.py")
        self.assertEqual(self.avisar(VARIAS_ERRO, "expedir_pedidos.py"), "nao_enviado")

    def test_desligado_vence_a_relevancia(self):
        self.assertEqual(self.avisar(VARIAS_ERRO, "x.py", config=_config(ativo=False)), "desligado")

    def test_modo_teste(self):
        self.assertEqual(self.avisar(VARIAS_ERRO, "x.py", modo_teste=True), "modo_teste")
        self.enviar.assert_not_called()

    def test_sempre_avisar_ausente_ou_none(self):
        for valor in (None, "", []):
            self.assertEqual(self.avisar(UMA, "criar_rotas_diarias.py",
                                         config=_config(sempre_avisar=valor)), "nao_relevante")

    def test_argv_vazio_e_resumo_none(self):
        with patch.object(nw.sys, "argv", [""]), patch("notificar_execucao_agente.sys.argv", [""]):
            self.assertEqual(nw.avisar_execucao(None, 0, False, _config(), conn=self.conn, agora=AGORA),
                             "nao_relevante")
            self.assertEqual(nw.avisar_execucao(VARIAS_ERRO, 0, False, _config(), conn=self.conn,
                                                agora=AGORA, dormir=self.dormir), "enviado")
        self.assertEqual(self.linhas()[0][0], "desconhecido")


class TestAvisarOutros(_ComBanco):
    def test_falha_job(self):
        situacao = nw.avisar_falha_job("stokki-backup-gcs.service", {"Result": "exit-code"}, _config(),
                                       conn=self.conn, agora=AGORA, dormir=self.dormir)
        self.assertEqual(situacao, "enviado")
        self.assertEqual(self.conn.execute("SELECT origem, tipo FROM notificacoes_whatsapp").fetchone(),
                         ("stokki-backup-gcs.service", "falha_job"))
        self.assertIn("Job da VPS falhou", self.enviar.call_args[0][2])

    def test_nao_expedidos(self):
        situacao = nw.avisar_nao_expedidos(4, 2, 1, _config(), conn=self.conn, agora=AGORA,
                                           dormir=self.dormir)
        self.assertEqual(situacao, "enviado")
        self.assertEqual(self.conn.execute("SELECT origem, tipo FROM notificacoes_whatsapp").fetchone(),
                         ("verificar_entregues_nao_expedidos", "nao_expedidos"))

    def test_nenhuma_levanta_com_entrada_ruim(self):
        with patch.object(nw, "despachar", side_effect=RuntimeError("bug")):
            self.assertEqual(nw.avisar_falha_job("u", None, _config()), "falhou")
            self.assertEqual(nw.avisar_nao_expedidos(1, 1, 1, _config()), "falhou")
            with patch.object(nw.sys, "argv", ["x.py"]):
                self.assertEqual(nw.avisar_execucao(VARIAS_ERRO, 1, False, _config()), "falhou")


class TestChamadaNoResumoDasRotinas(unittest.TestCase):
    def test_notificar_execucao_chama_o_whatsapp_depois_do_email(self):
        import notificar_execucao_agente as nea
        ordem = []
        with patch.object(nea, "enviar_email", side_effect=lambda *a, **k: ordem.append("email") or True), \
                patch.object(nw, "avisar_execucao", side_effect=lambda *a, **k: ordem.append("whatsapp")) as avisar:
            nea.notificar_execucao(UMA, 12.0, False, _config(), titulo="T")
        self.assertEqual(ordem, ["email", "whatsapp"])
        avisar.assert_called_once_with(UMA, 12.0, False, _config(), "T")

    def test_falha_no_whatsapp_nao_derruba_a_rotina(self):
        import notificar_execucao_agente as nea
        with patch.object(nea, "enviar_email", return_value=True), \
                patch.object(nw, "avisar_execucao", side_effect=RuntimeError("bug")):
            nea.notificar_execucao(UMA, 12.0, False, _config())

    def test_email_de_execucao_desligado_desliga_tudo(self):
        import notificar_execucao_agente as nea
        config = dict(_config(), notificacao_execucao={"ativo": False})
        with patch.object(nea, "enviar_email") as email, patch.object(nw, "avisar_execucao") as avisar:
            nea.notificar_execucao(UMA, 12.0, False, config)
        email.assert_not_called()
        avisar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
