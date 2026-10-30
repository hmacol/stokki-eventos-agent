# -*- coding: utf-8 -*-
"""
Cliente retoma pedido em espera a pedido dele (spec 2026-10-25, secao 4 item 3).

    py -3.11 -m unittest portal_cliente.test_retomar_em_espera
"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402
from test_cancelamento import conn_portal, envio  # noqa: E402


def _linha(conn, id_):
    return ep._linha(conn.execute("SELECT * FROM portal_envios WHERE id = ?", (id_,)).fetchone(), {})


def _retomada(conn, id_):
    return dict(conn.execute("SELECT retomar_modo, retomar_data, retomar_hora_inicio, retomar_hora_fim "
                             "FROM portal_envios WHERE id = ?", (id_,)).fetchone())


class RetomarEmEspera(unittest.TestCase):
    def setUp(self):
        self.conn = conn_portal([(60, ep.STATUS_EM_ESPERA_CLIENTE, "9960", "PS-39960")])

    def test_linha_em_espera_oferece_retomar_e_cancelar(self):
        l = _linha(self.conn, 60)
        self.assertTrue(l["pode_retomar"])
        self.assertTrue(l["pode_cancelar"])
        self.assertFalse(l["pode_reagendar"])
        self.assertFalse(l["pode_em_espera"])

    def test_linha_retomando_sem_acoes(self):
        self.conn.execute("UPDATE portal_envios SET status = ? WHERE id = 60", (ep.STATUS_RETOMANDO,))
        l = _linha(self.conn, 60)
        for k in ("pode_retomar", "pode_cancelar", "pode_reagendar", "pode_em_espera", "pode_reenviar"):
            self.assertFalse(l[k], k)

    def test_linha_criado_nao_oferece_retomar(self):
        conn = conn_portal([(61, ep.STATUS_CRIADO, "9961", "PS-39961")])
        self.assertFalse(_linha(conn, 61)["pode_retomar"])

    def test_retomar_proxima_rota_vira_retomando(self):
        r = ep.aplicar_acao(self.conn, envio(self.conn, 60), "retomar", {"modo": "proxima_rota"}, "cliente")
        self.assertEqual((r["aplicado"], r["em_andamento"]), (False, True))
        self.assertIn("Retomando", r["mensagem"])
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_RETOMANDO)
        self.assertEqual(_retomada(self.conn, 60), {"retomar_modo": "proxima_rota", "retomar_data": None,
                                                    "retomar_hora_inicio": None, "retomar_hora_fim": None})

    def test_retomar_agendamento_grava_data_e_horas(self):
        data = (date.today() + timedelta(days=3)).isoformat()
        ep.aplicar_acao(self.conn, envio(self.conn, 60), "retomar",
                        {"modo": "agendamento", "data": data, "hora_inicio": "09:00", "hora_fim": "12:30"}, "cliente")
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_RETOMANDO)
        self.assertEqual(_retomada(self.conn, 60), {"retomar_modo": "agendamento", "retomar_data": data,
                                                    "retomar_hora_inicio": "09:00", "retomar_hora_fim": "12:30"})

    def test_retomar_agendamento_valida_data(self):
        ontem = (date.today() - timedelta(days=1)).isoformat()
        for dados in ({"modo": "agendamento"},
                      {"modo": "agendamento", "data": "lixo"},
                      {"modo": "agendamento", "data": ontem}):
            with self.assertRaises(ep.ErroEnvio, msg=str(dados)):
                ep.aplicar_acao(self.conn, envio(self.conn, 60), "retomar", dados, "cliente")
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_EM_ESPERA_CLIENTE)

    def test_retomar_modo_invalido_recusa(self):
        for modo in (None, "", "agora", "<script>"):
            with self.assertRaises(ep.ErroEnvio):
                ep.aplicar_acao(self.conn, envio(self.conn, 60), "retomar", {"modo": modo}, "cliente")
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_EM_ESPERA_CLIENTE)

    def test_retomar_fora_de_em_espera_recusa(self):
        for st in (ep.STATUS_CRIADO, ep.STATUS_NA_FILA, ep.STATUS_CANCELADO, ep.STATUS_RETOMANDO, ep.STATUS_CANCELANDO):
            conn = conn_portal([(70, st, "9970", "PS-39970")])
            with self.assertRaises(ep.ErroEnvio, msg=st):
                ep.aplicar_acao(conn, envio(conn, 70), "retomar", {"modo": "proxima_rota"}, "cliente")
            self.assertEqual(envio(conn, 70)["status"], st)

    def test_cancelar_em_espera_vira_cancelando(self):
        r = ep.aplicar_acao(self.conn, envio(self.conn, 60), "cancelar", {"motivo": "desistiu"}, "cliente")
        self.assertTrue(r["em_andamento"])
        self.assertEqual(envio(self.conn, 60)["status"], ep.STATUS_CANCELANDO)
        self.assertEqual(self.conn.execute("SELECT tipo, status FROM portal_solicitacoes").fetchall()[0][:], ("cancelar", "PENDENTE"))

    def test_cancelar_retomando_recusa(self):
        self.conn.execute("UPDATE portal_envios SET status = ? WHERE id = 60", (ep.STATUS_RETOMANDO,))
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(self.conn, envio(self.conn, 60), "cancelar", {}, "cliente")

    def test_reagendar_em_espera_continua_valendo_so_para_os_antigos(self):
        # regressao: reagendar com data passada ainda recusa (validacao compartilhada)
        conn = conn_portal([(71, ep.STATUS_CRIADO, "9971", "PS-39971")])
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, envio(conn, 71), "reagendar", {"data": "2020-01-01"}, "cliente")


def _amanha(dias=1):
    return (date.today() + timedelta(days=dias)).isoformat()


class RevisaoFinal(unittest.TestCase):
    """Correcoes da revisao final (28/10): listagem, guarda do reagendar, janela e Pendencias."""

    def test_listar_envios_nao_esconde_pausado_antigo(self):
        antigo = "2020-01-01 10:00:00"
        casos = [(80, ep.STATUS_EM_ESPERA_CLIENTE), (81, ep.STATUS_RETOMANDO), (82, ep.STATUS_CANCELANDO),
                 (83, ep.STATUS_CRIADO)]
        conn = conn_portal([(i, st, f"99{i}", f"PS-399{i}") for i, st in casos])
        conn.execute("UPDATE portal_envios SET criado_em = ?", (antigo,))
        conn.commit()
        self.assertEqual(sorted(l["id"] for l in ep.listar_envios(conn, "111")), [80, 81, 82])

    def test_reagendar_recusa_fora_dos_status_da_linha(self):
        for st in (ep.STATUS_EM_ESPERA_CLIENTE, ep.STATUS_RETOMANDO, ep.STATUS_CANCELANDO, ep.STATUS_CANCELADO):
            conn = conn_portal([(90, st, "9990", "PS-39990")])
            with self.assertRaises(ep.ErroEnvio, msg=st):
                ep.aplicar_acao(conn, envio(conn, 90), "reagendar", {"data": _amanha()}, "cliente")
            self.assertEqual(envio(conn, 90)["status"], st)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM portal_solicitacoes").fetchone()[0], 0)

    def test_reagendar_vale_pra_erro_e_aguardando_liberacao(self):
        # o card "Precisa da sua atencao" (Definir data) posta reagendar nesses status
        for st in (ep.STATUS_ERRO, ep.STATUS_AGUARDANDO_LIBERACAO):
            conn = conn_portal([(97, st, "9997", "PS-39997")])
            conn.execute("ALTER TABLE portal_envios ADD COLUMN agendamento_hora_inicio TEXT")
            conn.execute("ALTER TABLE portal_envios ADD COLUMN agendamento_hora_fim TEXT")
            conn.execute("ALTER TABLE portal_envios ADD COLUMN agendamento_aplicado_em TEXT")
            conn.commit()
            ep.aplicar_acao(conn, envio(conn, 97), "reagendar", {"data": _amanha()}, "cliente")
            self.assertEqual(envio(conn, 97)["agendamento_data"], _amanha(), st)

    def test_reagendar_recusa_com_cancelamento_pendente(self):
        conn = conn_portal([(91, ep.STATUS_CRIADO, "9991", "PS-39991")])
        conn.execute("INSERT INTO portal_solicitacoes (envio_id, cnpj_embarcador, tipo, status, solicitado_por, criado_em) "
                     "VALUES (91, '111', 'cancelar', 'PENDENTE', 'cliente', datetime('now'))")
        conn.commit()
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, envio(conn, 91), "reagendar", {"data": _amanha()}, "cliente")

    def test_janela_com_fim_antes_do_inicio_recusa(self):
        conn = conn_portal([(92, ep.STATUS_EM_ESPERA_CLIENTE, "9992", "PS-39992")])
        for hi, hf in (("18:00", "08:00"), ("10:00", "10:00")):
            with self.assertRaises(ep.ErroEnvio, msg=f"{hi}-{hf}"):
                ep.aplicar_acao(conn, envio(conn, 92), "retomar",
                                {"modo": "agendamento", "data": _amanha(), "hora_inicio": hi, "hora_fim": hf}, "cliente")
        conn2 = conn_portal([(93, ep.STATUS_CRIADO, "9993", "PS-39993")])
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn2, envio(conn2, 93), "reagendar",
                            {"data": _amanha(), "hora_inicio": "18:00", "hora_fim": "08:00"}, "cliente")
        self.assertEqual(envio(conn, 92)["status"], ep.STATUS_EM_ESPERA_CLIENTE)

    def test_data_muito_distante_recusa_e_o_limite_vale(self):
        conn = conn_portal([(94, ep.STATUS_EM_ESPERA_CLIENTE, "9994", "PS-39994")])
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, envio(conn, 94), "retomar", {"modo": "agendamento", "data": "9999-12-31"}, "cliente")
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, envio(conn, 94), "retomar",
                            {"modo": "agendamento", "data": _amanha(ep.DIAS_MAX_AGENDAMENTO + 1)}, "cliente")
        ep.aplicar_acao(conn, envio(conn, 94), "retomar",
                        {"modo": "agendamento", "data": _amanha(ep.DIAS_MAX_AGENDAMENTO)}, "cliente")
        self.assertEqual(envio(conn, 94)["status"], ep.STATUS_RETOMANDO)

    def test_resumo_conta_em_espera_como_pendencia(self):
        conn = conn_portal([(95, ep.STATUS_EM_ESPERA_CLIENTE, "9995", "PS-39995"), (96, ep.STATUS_CRIADO, "9996", "PS-39996")])
        conn.execute("UPDATE portal_envios SET agendamento_pendente = 0")
        conn.commit()
        self.assertEqual(ep.resumo_envios(ep.listar_envios(conn, "111"))["em_espera"], 1)

    def test_aba_pendencias_inclui_em_espera(self):
        html = (_AQUI / "templates" / "_envios.html").read_text(encoding="utf-8")
        linha = next(l for l in html.splitlines() if "aba === 'pendencias'" in l)
        self.assertIn("EM_ESPERA_CLIENTE", linha)
        self.assertIn("k.em_espera", html)


class EmailSolicitacaoEscapado(unittest.TestCase):
    def test_texto_do_cliente_nao_vira_html_no_email_interno(self):
        import builtins
        from unittest import mock
        real_open = builtins.open

        def abrir(arq, *a, **k):  # worktree sem config.yaml (segredo): so o import do app ve um de teste
            if str(arq).endswith("config.yaml") and not Path(arq).exists():
                import io
                return io.StringIO("portal_cliente: {secret_key: teste-teste-teste-teste-teste-teste-1234}")
            return real_open(arq, *a, **k)

        try:
            with mock.patch.object(builtins, "open", abrir):
                import app as portal
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"app do portal nao importa neste ambiente ({e})")
        env = {"id": 1, "status": ep.STATUS_CRIADO, "codigo_pedido": "PS-1<b>", "numero_nf": "1",
               "destinatario_nome": "<i>Loja</i>"}
        corpo = {"motivo": '<a href="https://phishing.example">Clique</a>'}
        empresa = {"nome": "ACME <script>", "cnpj_formatado": "00"}
        with portal.app.test_request_context("/"),              mock.patch.object(portal, "_empresa_envio", return_value=empresa),              mock.patch.dict(portal._CONFIG, {"email": {"email_atendimento": "x@y.z"}}, clear=False),              mock.patch.object(portal, "enviar_email") as enviar:
            portal._avisar_operacao_solicitacao(env, "em_espera", corpo)
        html = enviar.call_args[0][2]
        for cru in ("<a href", "<script>", "<i>Loja", "PS-1<b>"):
            self.assertNotIn(cru, html)
        self.assertIn("&lt;a href", html)


if __name__ == "__main__":
    unittest.main()
