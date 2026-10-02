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
                         "✅ *Criação de rotas* · 16/09 22:05\nTerminou sem problemas (42 s)")

    def test_sucesso_com_varias_etapas(self):
        with patch("notificar_execucao_agente.sys.argv", ["executar_tudo.py"]):
            texto = nw.texto_execucao(VARIAS_OK, 138, agora=AGORA)
        self.assertEqual(texto, "✅ *Rotina de pedidos* · 16/09 22:05\n"
                                "As 3 etapas terminaram sem problemas (2,3 min)")

    def test_erro_explica_em_linguagem_simples_e_aponta_o_email(self):
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            texto = nw.texto_execucao(VARIAS_ERRO, 138, agora=AGORA)
        self.assertEqual(texto, "\n".join([
            "❌ *Rotina automática* · 16/09 22:05",
            "2 das 4 etapas falharam:",
            "• Importação de pedidos: o sistema demorou a responder",
            "• Retiradas: o login no sistema caiu",
            "Detalhes no e-mail.",
        ]))

    def test_detalhe_longo_e_cortado_para_a_mensagem_caber(self):
        etapas = {"Pipeline": {"status": "erro", "detalhe": "x" * 500}}
        texto = nw.texto_execucao(etapas, 1, agora=AGORA)
        self.assertEqual(len(texto), 200)
        self.assertTrue(texto.endswith("…\nDetalhes no e-mail."))

    def test_detalhe_perde_formatacao_do_whatsapp(self):
        etapas = {"Pipeline": {"status": "erro", "detalhe": "erro *grave* em _campo_ ~x~ `y`"}}
        self.assertIn("Não funcionou: erro grave em campo x y", nw.texto_execucao(etapas, 1, agora=AGORA))

    def test_no_maximo_tres_etapas_com_detalhe(self):
        etapas = {f"E{i}": {"status": "erro", "detalhe": f"d{i}"} for i in range(5)}
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            linhas = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")
        self.assertEqual(linhas[2:6], ["• E0: d0", "• E1: d1", "• E2: d2", "• e mais 2"])
        self.assertEqual(len(linhas), 7)

    def test_etapa_com_erro_sem_detalhe_mostra_so_o_nome(self):
        etapas = {"Pipeline": {"status": "erro", "detalhe": ""}, "Outra": None}
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            linhas = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")
        self.assertEqual(linhas[1:], ["2 das 2 etapas falharam:", "• Importação de pedidos",
                                      "• Outra", "Detalhes no e-mail."])

    def test_resumo_vazio_ou_none_nao_quebra(self):
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            for vazio in ({}, None):
                self.assertEqual(nw.texto_execucao(vazio, 0, agora=AGORA),
                                 "✅ *Rotina automática* · 16/09 22:05\nRodou, mas não informou o que fez (0 s)")


class TestCabeEm200(unittest.TestCase):
    """Pedido do Hugo (29/09/2026): a mensagem inteira em 200 caracteres,
    completa -- sem reticencias cortando o rodape."""

    def conferir(self, texto):
        self.assertLessEqual(len(texto), nw.MAX_MENSAGEM, texto)
        self.assertRegex(texto.split("\n")[-1], r"(e-mail\.|\))$", texto)

    def test_falha_de_qualquer_tarefa_com_qualquer_resultado(self):
        for chave in nw.NOMES_DAS_TAREFAS:
            for resultado in list(nw.RESULTADOS_SIMPLES) + ["desconhecido"]:
                self.conferir(nw.texto_falha_job(f"stokki-{chave}.service", {"Result": resultado}, agora=AGORA))

    def test_falha_de_tarefa_sem_nome_cadastrado(self):
        info = {"Description": "Stokki Eventos - " + "descricao comprida " * 20, "Result": "oom-kill"}
        self.conferir(nw.texto_falha_job("stokki-nova.service", info, agora=AGORA))
        self.conferir(nw.texto_falha_job("u" * 300 + ".service", {}, agora=AGORA))

    def test_conferencia_com_numeros_grandes(self):
        self.conferir(nw.texto_nao_expedidos(9999, 9999, 9999))

    def test_rotina_com_nomes_e_detalhes_compridos(self):
        for qtd in (1, 2, 3, 20, 150):
            etapas = {f"Etapa {i} " + "n" * 80: {"status": "erro", "detalhe": "d" * 500} for i in range(qtd)}
            for titulo in (None, "t" * 300):
                with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
                    self.conferir(nw.texto_execucao(etapas, 99999, titulo, agora=AGORA))
            ok = {nome: {"status": "ok"} for nome in etapas}
            self.conferir(nw.texto_execucao(ok, 99999, "t" * 300, agora=AGORA))

    def test_o_que_nao_cabe_vai_pra_conta_do_e_mais(self):
        etapas = {f"Etapa {i} " + "n" * 80: {"status": "erro", "detalhe": "d" * 500} for i in range(5)}
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            linhas = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")
        mostradas = [l for l in linhas if l.startswith("• Etapa")]
        self.assertGreaterEqual(len(mostradas), 1)
        self.assertEqual(linhas[-2], f"• e mais {5 - len(mostradas)}")


class TestMotivoSimples(unittest.TestCase):
    def test_erros_conhecidos_viram_frase(self):
        casos = {
            "TimeoutError: Page.goto: Timeout 30000ms exceeded": "o sistema demorou a responder",
            "401": "o login no sistema caiu",
            "HTTP 403 Forbidden": "o login no sistema caiu",
            "Erro 502 na Vuupt": "o outro sistema estava fora do ar",
            "ConnectionError: HTTPSConnectionPool(host='x')": "falha de conexão",
            "sqlite3.OperationalError: database is locked": "banco de dados ocupado",
            "x" * 300 + " TimeoutError": "o sistema demorou a responder",
        }
        for detalhe, frase in casos.items():
            self.assertEqual(nw._motivo_simples(detalhe), frase, detalhe)

    def test_excecao_desconhecida_vira_erro_tecnico(self):
        self.assertEqual(nw._motivo_simples("KeyError: 'sender_id'"), "erro técnico")
        self.assertEqual(nw._motivo_simples("<Response [418]>"), "erro técnico")

    def test_texto_escrito_por_gente_passa_como_esta(self):
        self.assertEqual(nw._motivo_simples("3 devolvidos ao pool, 1 com erro."), "3 devolvidos ao pool, 1 com erro.")
        self.assertEqual(nw._motivo_simples("500 pedidos lidos, 401 importados"), "500 pedidos lidos, 401 importados")
        self.assertEqual(nw._motivo_simples(None), "")


class TestTextoFalhaJob(unittest.TestCase):
    def test_formato(self):
        info = {"Result": "exit-code", "ExecMainStatus": "1"}
        self.assertEqual(nw.texto_falha_job("stokki-backup-gcs.service", info, agora=AGORA), "\n".join([
            "🚨 *Tarefa automática falhou* · 16/09 22:05",
            "Cópia de segurança diária dos dados: parou com erro.",
            "Detalhes no e-mail.",
        ]))

    def test_nao_mostra_nome_de_unit_nem_codigo(self):
        texto = nw.texto_falha_job("stokki-sequencia-noite.service", {"Result": "timeout", "ExecMainStatus": "15"},
                                   agora=AGORA)
        self.assertIn("Rotina da noite: demorou demais e foi interrompida.", texto)
        for jargao in ("stokki-", ".service", "timeout", "15", "VPS", "Job"):
            self.assertNotIn(jargao, texto.replace("16/09 22:05", ""))

    def test_tarefa_sem_nome_cadastrado_usa_a_descricao_limpa(self):
        info = {"Description": "Stokki Eventos - Tarefa nova (tarefa_nova.py --hoje)"}
        self.assertEqual(nw.texto_falha_job("stokki-tarefa-nova.service", info, agora=AGORA).split("\n")[1],
                         "Tarefa nova: não terminou como deveria.")
        self.assertEqual(nw.texto_falha_job("x.service", {}, agora=AGORA).split("\n")[1],
                         "x.service: não terminou como deveria.")

    def test_info_none(self):
        self.assertIn("não terminou como deveria.", nw.texto_falha_job("x.service", None, agora=AGORA))

    def test_toda_tarefa_do_infra_tem_nome_simples(self):
        from pathlib import Path
        units = {p.name[len("stokki-"):-len(".service")]
                 for p in (Path(__file__).resolve().parent / "infra").glob("stokki-*.service")
                 if "@" not in p.name}
        self.assertEqual(units - set(nw.NOMES_DAS_TAREFAS), set())


class TestTextoNaoExpedidos(unittest.TestCase):
    def test_completo(self):
        self.assertEqual(nw.texto_nao_expedidos(4, 2, 1), "\n".join([
            "⚠️ *Conferência das entregas*",
            "• 4 pedidos entregues sem baixa na Stokki",
            "• 2 rotas antigas não encerradas",
            "• 1 retirada no galpão há mais de 7 dias",
            "Lista no e-mail.",
        ]))

    def test_singular_e_plural(self):
        self.assertIn("• 1 pedido entregue sem baixa na Stokki", nw.texto_nao_expedidos(1, 0, 0))
        texto = nw.texto_nao_expedidos(0, 1, 3)
        self.assertIn("• 1 rota antiga não encerrada", texto)
        self.assertIn("• 3 retiradas no galpão há mais de 7 dias", texto)

    def test_contagem_zero_e_omitida(self):
        self.assertEqual(nw.texto_nao_expedidos(0, 2, 0), "\n".join([
            "⚠️ *Conferência das entregas*",
            "• 2 rotas antigas não encerradas",
            "Lista no e-mail.",
        ]))
        self.assertNotIn("rota", nw.texto_nao_expedidos(3, 0, 0))


def _insucesso(n, remetente="Quatro Estrelas", motivo="Destinatário ausente"):
    return {"codigo": f"#PS-{n}", "destinatario": f"Mercado {n}", "remetente": remetente, "motivo": motivo}


class TestTextoInsucessos(unittest.TestCase):
    def test_um_pedido(self):
        self.assertEqual(nw.texto_insucessos([_insucesso(1)], agora=AGORA), "\n".join([
            "⚠️ *Insucesso na entrega* · 16/09 22:05",
            "1 pedido novo",
            "PS-1 · Mercado 1 (Quatro Estrelas): Destinatário ausente",
            "Detalhes no e-mail e na Torre.",
        ]))

    def test_varios_pedidos(self):
        linhas = nw.texto_insucessos([_insucesso(1), _insucesso(2, "Muai", "Recusado")], agora=AGORA).split("\n")
        self.assertEqual(linhas[1], "2 pedidos novos")
        self.assertEqual(linhas[3], "PS-2 · Mercado 2 (Muai): Recusado")

    def test_mais_de_dez_lista_dez_e_conta_o_resto(self):
        linhas = nw.texto_insucessos([_insucesso(i) for i in range(13)], agora=AGORA).split("\n")
        self.assertEqual(linhas[1], "13 pedidos novos")
        self.assertTrue(linhas[11].startswith("PS-9 "))
        self.assertEqual(linhas[12:], ["e mais 3", "Detalhes no e-mail e na Torre."])

    def test_sem_remetente_ou_destinatario_ou_motivo(self):
        itens = [{"codigo": "PS-1", "destinatario": "Mercado", "remetente": "", "motivo": "Ausente"},
                 {"codigo": "PS-2", "destinatario": None, "remetente": "Muai", "motivo": None},
                 {"codigo": "PS-3"}]
        self.assertEqual(nw.texto_insucessos(itens, agora=AGORA).split("\n")[2:5], [
            "PS-1 · Mercado: Ausente",
            "PS-2 (Muai): motivo não informado",
            "PS-3: motivo não informado",
        ])

    def test_nomes_perdem_formatacao_e_sao_cortados(self):
        item = {"codigo": "PS-1", "destinatario": "*Mercado*\n_Bom_ " + "x" * 100, "remetente": "~Muai~",
                "motivo": "m" * 300}
        linha = nw.texto_insucessos([item], agora=AGORA).split("\n")[2]
        self.assertTrue(linha.startswith("PS-1 · Mercado Bom xxx"))
        self.assertIn("… (Muai): mmm", linha)
        self.assertNotIn("*", linha)
        self.assertLessEqual(len(linha), 200)


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
        # canal em pausa: nem tenta; depois da pausa tenta 1 vez e nao repete o e-mail
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=10)), "nao_enviado")
        self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=63)), "falhou")
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
        self.falhar(2)
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
        self.assertIn("Tarefa automática falhou", self.enviar.call_args[0][2])

    def test_nao_expedidos(self):
        situacao = nw.avisar_nao_expedidos(4, 2, 1, _config(), conn=self.conn, agora=AGORA,
                                           dormir=self.dormir)
        self.assertEqual(situacao, "enviado")
        self.assertEqual(self.conn.execute("SELECT origem, tipo FROM notificacoes_whatsapp").fetchone(),
                         ("verificar_entregues_nao_expedidos", "nao_expedidos"))

    def test_insucessos(self):
        situacao = nw.avisar_insucessos([_insucesso(1)], _config(), conn=self.conn, agora=AGORA,
                                        dormir=self.dormir)
        self.assertEqual(situacao, "enviado")
        self.assertEqual(self.conn.execute("SELECT origem, tipo FROM notificacoes_whatsapp").fetchone(),
                         ("expedir_pedidos", "insucesso"))
        self.assertIn("PS-1 · Mercado 1 (Quatro Estrelas)", self.enviar.call_args[0][2])

    def test_insucessos_lista_vazia_nao_envia_nem_registra(self):
        for vazio in ([], None):
            self.assertEqual(nw.avisar_insucessos(vazio, _config(), conn=self.conn), "nao_relevante")
        self.enviar.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)

    def test_insucessos_modo_teste_so_loga(self):
        self.assertEqual(nw.avisar_insucessos([_insucesso(1)], _config(), modo_teste=True, conn=self.conn,
                                              agora=AGORA), "modo_teste")
        self.enviar.assert_not_called()

    def test_nenhuma_levanta_com_entrada_ruim(self):
        with patch.object(nw, "despachar", side_effect=RuntimeError("bug")):
            self.assertEqual(nw.avisar_falha_job("u", None, _config()), "falhou")
            self.assertEqual(nw.avisar_nao_expedidos(1, 1, 1, _config()), "falhou")
            self.assertEqual(nw.avisar_insucessos([_insucesso(1)], _config()), "falhou")
            self.assertEqual(nw.avisar_insucessos(["lixo"], _config()), "falhou")
            with patch.object(nw.sys, "argv", ["x.py"]):
                self.assertEqual(nw.avisar_execucao(VARIAS_ERRO, 1, False, _config()), "falhou")


LINK = "https://app.freshhub.com.br/painel/atendimento?chamado=21"


def _chamado(**extra):
    chamado = {"id": 21, "tipo": "CLIENTE", "nome_cliente": "QUATRO ESTRELAS", "area_rotulo": "Entrega",
               "pedido_ref": "PS-40316", "assunto": "Pedido nao chegou"}
    chamado.update(extra)
    return chamado


class TestTextoChamado(unittest.TestCase):
    def test_cliente(self):
        self.assertEqual(nw.texto_chamado(_chamado(), LINK, agora=AGORA), "\n".join([
            "🙋 *Atendimento precisa de gente* · 16/09 22:05",
            "Chamado #21 · QUATRO ESTRELAS",
            "Área: Entrega · Pedido: PS-40316",
            LINK,
        ]))

    def test_motorista_sai_sem_nome(self):
        texto = nw.texto_chamado(_chamado(tipo="MOTORISTA", nome_cliente="João da Silva", pedido_ref=""),
                                 LINK, agora=AGORA)
        self.assertEqual(texto.split("\n")[1:3], ["Chamado #21 · Motorista (app)", "Área: Entrega"])
        self.assertNotIn("João", texto)

    def test_sem_area_nem_pedido_nao_deixa_linha_vazia(self):
        texto = nw.texto_chamado(_chamado(area_rotulo="", pedido_ref=None), LINK, agora=AGORA)
        self.assertEqual(texto.split("\n")[1:], ["Chamado #21 · QUATRO ESTRELAS", LINK])

    def test_cabe_em_200_sem_cortar_o_link(self):
        link = "https://app.freshhub.com.br/painel/atendimento?chamado=123456"
        texto = nw.texto_chamado(_chamado(id=123456, nome_cliente="N" * 300, area_rotulo="a" * 300,
                                          pedido_ref="p" * 300), link, agora=AGORA)
        self.assertLessEqual(len(texto), nw.MAX_MENSAGEM)
        self.assertEqual(texto.split("\n")[-1], link)
        self.assertIn("Área: ", texto)

    def test_assunto_e_texto_do_cliente_ficam_de_fora(self):
        self.assertNotIn("nao chegou", nw.texto_chamado(_chamado(), LINK, agora=AGORA))


class TestAvisarChamado(_ComBanco):
    def avisar(self, config=None, chamado=None, agora=AGORA):
        return nw.avisar_chamado(chamado or _chamado(), LINK,
                                 config or _config(grupo_atendimento_id="2@g.us"),
                                 conn=self.conn, agora=agora, dormir=self.dormir)

    def test_vai_pro_grupo_do_atendimento(self):
        self.assertEqual(self.avisar(), "enviado")
        self.assertEqual(self.enviar.call_args[0][1], "2@g.us")
        self.assertIn(LINK, self.enviar.call_args[0][2])
        self.assertEqual(self.conn.execute("SELECT origem, tipo, assinatura FROM notificacoes_whatsapp").fetchone(),
                         ("atendimento", "chamado", "chamado:21"))

    def test_sem_grupo_do_atendimento_nao_cai_no_grupo_de_alertas(self):
        self.assertEqual(self.avisar(config=_config()), "desligado")
        self.enviar.assert_not_called()

    def test_vale_mesmo_sem_o_grupo_de_alertas(self):
        self.assertEqual(self.avisar(config=_config(grupo_id="", grupo_atendimento_id="2@g.us")), "enviado")

    def test_chave_propria_desliga_so_este_aviso(self):
        config = _config(grupo_atendimento_id="2@g.us", avisar_chamados=False)
        self.assertEqual(self.avisar(config=config), "desligado")
        self.assertEqual(self.despachar(config=config), "enviado")

    def test_chave_mestra_desligada(self):
        self.assertEqual(self.avisar(config=_config(ativo=False, grupo_atendimento_id="2@g.us")), "desligado")
        self.enviar.assert_not_called()

    def test_um_aviso_por_chamado_dentro_da_janela(self):
        self.assertEqual(self.avisar(), "enviado")
        self.assertEqual(self.avisar(agora=AGORA + timedelta(minutes=30)), "nao_enviado")
        self.assertEqual(self.avisar(chamado=_chamado(id=22), agora=AGORA + timedelta(minutes=31)), "enviado")
        self.assertEqual(self.enviar.call_count, 2)

    def test_nao_levanta_com_entrada_ruim(self):
        self.assertEqual(nw.avisar_chamado(None, LINK, _config(grupo_atendimento_id="2@g.us")), "falhou")


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


class TestCorrecoesDaRevisao(_ComBanco):
    def test_intervalo_vale_a_partir_do_horario_real_do_envio(self):
        self.despachar()
        self.despachar(agora=AGORA + timedelta(seconds=5))          # espera 15 s, sai em +20
        self.dormir.assert_called_once_with(15.0)
        horarios = [l[0] for l in self.conn.execute("SELECT criado_em FROM notificacoes_whatsapp ORDER BY id")]
        self.assertEqual(horarios[1], (AGORA + timedelta(seconds=20)).isoformat(timespec="seconds"))
        self.dormir.reset_mock()
        self.despachar(agora=AGORA + timedelta(seconds=22))
        self.dormir.assert_called_once_with(18.0)

    def test_canal_em_pausa_depois_de_falhas_seguidas(self):
        self.enviar.return_value = (False, None)
        with patch("email_utils.enviar_email", return_value=True):
            for i in range(3):
                self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=i)), "falhou")
            self.assertEqual(self.enviar.call_count, 3)
            for minuto in (3, 30, 61):
                self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=minuto)), "nao_enviado")
            self.assertEqual(self.enviar.call_count, 3)
            self.assertEqual(self.linhas()[-1][2], "canal em pausa")
            self.enviar.return_value = (True, "m")
            self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=63)), "enviado")
            self.assertEqual(self.despachar(agora=AGORA + timedelta(minutes=64)), "enviado")

    def test_pausa_configuravel(self):
        self.enviar.return_value = (False, None)
        config = _config(pausa_canal_min=10)
        with patch("email_utils.enviar_email", return_value=True):
            for i in range(3):
                self.despachar(config=config, agora=AGORA + timedelta(minutes=i))
            self.assertEqual(self.despachar(config=config, agora=AGORA + timedelta(minutes=5)), "nao_enviado")
            self.assertEqual(self.despachar(config=config, agora=AGORA + timedelta(minutes=13)), "falhou")


class TestResumoDeErroLimpo(unittest.TestCase):
    def test_muitas_etapas_lista_tres_e_conta_o_resto(self):
        etapas = {f"Rota {i} — Motorista {i}": {"status": "erro", "detalhe": "x"} for i in range(20)}
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            linhas = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")
        self.assertEqual(linhas[1:6], ["20 das 20 etapas falharam:", "• Rota 0 — Motorista 0: x",
                                       "• Rota 1 — Motorista 1: x", "• Rota 2 — Motorista 2: x", "• e mais 17"])

    def test_nome_de_etapa_perde_formatacao(self):
        etapas = {"rota_1 *x*": {"status": "erro", "detalhe": "d"}, "ok": {"status": "ok"}}
        with patch("notificar_execucao_agente.sys.argv", ["desconhecido.py"]):
            linhas = nw.texto_execucao(etapas, 1, agora=AGORA).split("\n")
        self.assertEqual(linhas[1:3], ["1 das 2 etapas falharam:", "• rota1 x: d"])

    def test_uma_etapa_so_com_erro(self):
        linhas = nw.texto_execucao({"Pipeline": {"status": "erro", "detalhe": "d"}}, 1, agora=AGORA).split("\n")
        self.assertEqual(linhas[:2], ["❌ *Importação de pedidos* · 16/09 22:05", "Não funcionou: d"])

    def test_rotas_sem_motorista_tem_titulo_proprio(self):
        etapas = {"Cancelamento de rotas sem motorista": {"status": "ok", "detalhe": "2 canceladas"},
                  "Pedidos presos em rotas de dias anteriores": {"status": "ok", "detalhe": "0"}}
        with patch("notificar_execucao_agente.sys.argv", ["/opt/x/roteirizacao/cancelar_rotas_sem_motorista.py"]):
            self.assertTrue(nw.texto_execucao(etapas, 1, agora=AGORA).startswith("✅ *Rotas sem motorista*"))


if __name__ == "__main__":
    unittest.main()


LINK_AGENDA = "https://app.freshhub.com.br/painel/clientes-agenda"


class TestTextoClientesAgenda(unittest.TestCase):
    def test_contagens_e_link_sem_nome_de_cliente(self):
        self.assertEqual(nw.texto_clientes_agenda(3, 8, LINK_AGENDA, agora=AGORA), "\n".join([
            "📅 *Clientes para marcar como AGENDA* · 16/09 22:05",
            "3 novos aguardam sua autorização (8 no total).",
            LINK_AGENDA,
        ]))

    def test_singular(self):
        self.assertEqual(nw.texto_clientes_agenda(1, 1, LINK_AGENDA, agora=AGORA).split("\n")[1],
                         "1 novo aguarda sua autorização (1 no total).")

    def test_cabe_em_200(self):
        self.assertLessEqual(len(nw.texto_clientes_agenda(999, 9999, LINK_AGENDA, agora=AGORA)), nw.MAX_MENSAGEM)


class TestAvisarClientesAgenda(_ComBanco):
    def avisar(self, novos=3, total=8, config=None, agora=AGORA, **kw):
        return nw.avisar_clientes_agenda(novos, total, LINK_AGENDA, config or _config(),
                                         conn=self.conn, agora=agora, dormir=self.dormir, **kw)

    def test_vai_pro_grupo_de_alertas(self):
        self.assertEqual(self.avisar(), "enviado")
        self.assertEqual(self.enviar.call_args[0][1], "1@g.us")
        self.assertIn(LINK_AGENDA, self.enviar.call_args[0][2])
        self.assertEqual(self.conn.execute("SELECT origem, tipo FROM notificacoes_whatsapp").fetchone(),
                         ("marcar_clientes_agenda", "clientes_agenda"))

    def test_sem_novos_nao_avisa(self):
        self.assertEqual(self.avisar(novos=0), "nao_relevante")
        self.enviar.assert_not_called()

    def test_modo_teste_nao_envia(self):
        self.assertEqual(self.avisar(modo_teste=True), "modo_teste")
        self.enviar.assert_not_called()

    def test_desligado(self):
        self.assertEqual(self.avisar(config=_config(ativo=False)), "desligado")


import datetime as _dt


class TestRotasFracas(unittest.TestCase):
    ALVO = _dt.date(2026, 9, 30)

    def _resumo(self, **extra):
        base = {"juntadas": 0, "seguradas": 0, "pedidos_segurados": 0, "data_nova": _dt.date(2026, 10, 1),
                "sobraram": 0, "pedidos_sobraram": 0, "caixas_sobraram": 0}
        base.update(extra)
        return base

    def test_completo(self):
        r = self._resumo(juntadas=2, seguradas=1, pedidos_segurados=3,
                         sobraram=1, pedidos_sobraram=2, caixas_sobraram=9)
        self.assertEqual(nw.texto_rotas_fracas(self.ALVO, r), "\n".join([
            "⚠️ *Rotas fracas* · rotas de 30/09",
            "• 2 juntadas em rotas vizinhas",
            "• 1 segurada para 01/10 (3 pedidos)",
            "• 1 saiu fraca (2 pedidos, 9 caixas)",
            "Veja no Planejamento.",
        ]))

    def test_linha_zerada_e_omitida(self):
        texto = nw.texto_rotas_fracas(self.ALVO, self._resumo(juntadas=1))
        self.assertEqual(texto, "\n".join([
            "⚠️ *Rotas fracas* · rotas de 30/09",
            "• 1 juntada em rota vizinha",
            "Veja no Planejamento.",
        ]))

    def test_cabe_em_200_com_numeros_grandes(self):
        r = self._resumo(juntadas=9999, seguradas=9999, pedidos_segurados=9999,
                         sobraram=9999, pedidos_sobraram=9999, caixas_sobraram=9999)
        self.assertLessEqual(len(nw.texto_rotas_fracas(self.ALVO, r)), nw.MAX_MENSAGEM)

    def test_sem_rota_fraca_nao_envia(self):
        with patch.object(nw, "despachar") as despachar:
            self.assertEqual(nw.avisar_rotas_fracas(self.ALVO, self._resumo(), {}), "nao_relevante")
        despachar.assert_not_called()

    def test_envia_uma_por_data_alvo(self):
        with patch.object(nw, "despachar", return_value="enviado") as despachar:
            saida = nw.avisar_rotas_fracas(self.ALVO, self._resumo(sobraram=1, pedidos_sobraram=2, caixas_sobraram=9),
                                           {"x": 1}, modo_teste=True)
        self.assertEqual(saida, "enviado")
        args, kwargs = despachar.call_args
        self.assertEqual(args[:3], ({"x": 1}, "criar_rotas_diarias", "rotas_fracas"))
        self.assertEqual(args[4], "rotas_fracas:2026-09-30")
        self.assertTrue(kwargs["modo_teste"])

    def test_falha_no_envio_nao_levanta(self):
        with patch.object(nw, "despachar", side_effect=RuntimeError("boom")):
            self.assertEqual(nw.avisar_rotas_fracas(self.ALVO, self._resumo(juntadas=1), {}), "falhou")
