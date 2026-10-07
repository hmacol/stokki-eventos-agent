# -*- coding: utf-8 -*-
"""
Cancelamento de pedido pelo portal (DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md).

    py -3.11 -m unittest portal_cliente.test_cancelamento
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cancelamento as cm  # noqa: E402
import envio_pedidos as ep  # noqa: E402

COLUNAS_ENVIO = ("id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT, atualizado_em TEXT, "
                 "criado_stokki_em TEXT, emitida_em TEXT, numero_nf TEXT, referencia TEXT, destinatario_doc TEXT, "
                 "destinatario_nome TEXT, destinatario_endereco TEXT, destinatario_bairro TEXT, destinatario_municipio TEXT, "
                 "codigo_pedido TEXT, requer_agendamento INTEGER, agendamento_pendente INTEGER, agendamento_data TEXT, "
                 "data_expedicao TEXT, xml_path TEXT, itens_json TEXT, erro TEXT, bloqueio_motivo TEXT")


def conn_portal(envios=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(f"CREATE TABLE portal_envios ({COLUNAS_ENVIO});"
                       "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em, concluido_em, resposta);")
    for e in envios:
        conn.execute("INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, numero_nf, codigo_pedido, requer_agendamento, agendamento_pendente, destinatario_nome) "
                     "VALUES (?, '111', ?, datetime('now','localtime'), ?, ?, 1, 1, 'DESTINO LTDA')", e)
    conn.commit()
    return conn


def envio(conn, id_):
    return dict(conn.execute("SELECT * FROM portal_envios WHERE id = ?", (id_,)).fetchone())


class AplicarAcaoCancelar(unittest.TestCase):
    def test_criado_vira_cancelando_com_solicitacao_pendente(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        r = ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {"motivo": "cliente desistiu"}, "cliente")
        self.assertEqual((r["aplicado"], r["precisa_operacao"], r["em_andamento"]), (False, False, True))
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELANDO)
        s = conn.execute("SELECT tipo, status, detalhes FROM portal_solicitacoes").fetchall()
        self.assertEqual([tuple(x) for x in s], [("cancelar", "PENDENTE", "cliente desistiu")])

    def test_duplicado_tambem_vira_cancelando(self):
        conn = conn_portal([(51, ep.STATUS_DUPLICADO, "9960", "PS-39960")])
        ep.aplicar_acao(conn, envio(conn, 51), "cancelar", {}, "cliente")
        self.assertEqual(envio(conn, 51)["status"], ep.STATUS_CANCELANDO)

    def test_cancelar_duas_vezes_recusa(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM portal_solicitacoes").fetchone()[0], 1)

    def test_na_fila_continua_cancelando_na_hora(self):
        conn = conn_portal([(52, ep.STATUS_NA_FILA, "9961", None)])
        r = ep.aplicar_acao(conn, envio(conn, 52), "cancelar", {}, "cliente")
        self.assertTrue(r["aplicado"])
        self.assertEqual(envio(conn, 52)["status"], ep.STATUS_CANCELADO)


class LinhaCancelando(unittest.TestCase):
    def test_cancelando_nao_oferece_acoes_nem_cobra_agendamento(self):
        conn = conn_portal([(50, ep.STATUS_CANCELANDO, "9959", "PS-39959")])
        conn.execute("INSERT INTO portal_solicitacoes (envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em) "
                     "VALUES (50, '111', 'cancelar', '', 'PENDENTE', 'cliente', '2026-10-06 10:00:00')")
        conn.commit()
        l = ep.listar_envios(conn, "111")[0]
        self.assertEqual(l["status_rotulo"], "Cancelando")
        self.assertFalse(l["pode_cancelar"] or l["pode_reagendar"] or l["pode_em_espera"] or l["pode_reenviar"])
        self.assertFalse(l["agendamento_pendente"])
        self.assertEqual(ep.resumo_envios([l])["agendamento_pendente"], 0)


class ConcluirSolicitacao(unittest.TestCase):
    def test_concluir_cancelamento_marca_envio_cancelado(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        conn.execute("UPDATE portal_envios SET status = ? WHERE id = 50", (ep.STATUS_CRIADO,))  # worker devolveu pra operacao
        sid = conn.execute("SELECT id FROM portal_solicitacoes").fetchone()[0]
        ep.concluir_solicitacao(conn, sid, "cancelado na Stokki pela equipe")
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELADO)
        self.assertEqual(tuple(conn.execute("SELECT status, resposta FROM portal_solicitacoes").fetchone()), ("CONCLUIDA", "cancelado na Stokki pela equipe"))

    def test_recusar_cancelamento_nao_mexe_no_envio(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        conn.execute("UPDATE portal_envios SET status = ? WHERE id = 50", (ep.STATUS_CRIADO,))
        sid = conn.execute("SELECT id FROM portal_solicitacoes").fetchone()[0]
        ep.concluir_solicitacao(conn, sid, "ja entregue", recusada=True)
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)

    def test_buscar_solicitacao_cancelamento(self):
        conn = conn_portal([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        self.assertIsNone(ep.buscar_solicitacao_cancelamento(conn, 50))
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {"motivo": "x"}, "cliente")
        self.assertEqual(ep.buscar_solicitacao_cancelamento(conn, 50)["detalhes"], "x")


def _serv(codigo, status, provedor="not_assigned", route=None, sid=1, excluido=None):
    return {"codigo": codigo, "vuupt_service_id": sid, "status": status, "status_provedor": provedor,
            "vuupt_route_id": route, "excluido_em": excluido}


class Decidir(unittest.TestCase):
    def test_sem_servico_cancela_sozinho(self):
        self.assertEqual(cm.decidir([], {})[0], cm.SOZINHO)

    def test_no_pool_sozinho(self):
        self.assertEqual(cm.decidir([_serv("PS-1", "ABERTO")], {})[0], cm.SOZINHO)

    def test_rota_planejada_nao_iniciada_sozinho(self):
        rotas = {10: {"status": "PLANEJADA", "status_provedor": "assigned", "iniciada_em": None, "agent_id": 5}}
        self.assertEqual(cm.decidir([_serv("PS-1", "EM_ROTA", "assigned", 10)], rotas)[0], cm.SOZINHO)

    def test_rota_iniciada_vai_pra_operacao(self):
        rotas = {10: {"status": "EM_ROTA", "status_provedor": "started", "iniciada_em": "2026-10-06 08:00:00", "agent_id": 5}}
        d, motivo = cm.decidir([_serv("PS-1", "EM_ROTA", "assigned", 10)], rotas)
        self.assertEqual(d, cm.OPERACAO)
        self.assertIn("em rota", motivo)

    def test_rota_lalamove_vai_pra_operacao(self):
        rotas = {10: {"status": "PLANEJADA", "status_provedor": "assigned", "iniciada_em": None, "agent_id": 99}}
        self.assertEqual(cm.decidir([_serv("PS-1", "EM_ROTA", "assigned", 10)], rotas, agent_lalamove=99)[0], cm.OPERACAO)

    def test_entregue_ou_insucesso_vai_pra_operacao(self):
        self.assertEqual(cm.decidir([_serv("PS-1", "ENTREGUE", "done")], {})[0], cm.OPERACAO)
        self.assertEqual(cm.decidir([_serv("PS-1", "INSUCESSO", "done")], {})[0], cm.OPERACAO)

    def test_servico_cancelado_ou_excluido_nao_conta(self):
        self.assertEqual(cm.decidir([_serv("PS-1", "CANCELADO", "canceled"), _serv("PS-1-R1", "ABERTO", sid=2)], {})[0], cm.SOZINHO)
        self.assertEqual(cm.decidir([_serv("PS-1", "ENTREGUE", "done", excluido="2026-10-01 00:00:00")], {})[0], cm.SOZINHO)


class IdStokki(unittest.TestCase):
    def test_codigos(self):
        self.assertEqual(cm.id_stokki_do_codigo("PS-39959"), 39959)
        self.assertEqual(cm.id_stokki_do_codigo("#PS-39959"), 39959)
        self.assertIsNone(cm.id_stokki_do_codigo(None))
        self.assertIsNone(cm.id_stokki_do_codigo("ABC"))


def conn_completo(envios=()):
    conn = conn_portal(envios)
    conn.executescript("""
        CREATE TABLE nucleo_pedidos (codigo TEXT, vuupt_service_id INTEGER, status TEXT, status_provedor TEXT, vuupt_route_id INTEGER, excluido_em TEXT);
        CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, status TEXT, status_provedor TEXT, iniciada_em TEXT, agent_id INTEGER);
    """)
    return conn


class Processar(unittest.TestCase):
    def setUp(self):
        self.vuupt_chamadas, self.stokki_chamadas, self.avisos = [], [], []
        self.vuupt_ok, self.stokki_ok = True, True

    def cancelar_vuupt(self, sid):
        self.vuupt_chamadas.append(sid)
        return {"ok": self.vuupt_ok, "erro": "" if self.vuupt_ok else "Vuupt: 409"}

    def cancelar_stokki(self, id_stokki, motivo):
        self.stokki_chamadas.append((id_stokki, motivo))
        return {"ok": self.stokki_ok, "erro": "" if self.stokki_ok else "Stokki 500"}

    def avisar(self, envio, motivo, erro):
        self.avisos.append((envio["id"], motivo, erro))

    def rodar(self, conn):
        return cm.processar_cancelamentos(conn, {"lalamove": {"agent_id_vuupt": 99}}, self.cancelar_vuupt, self.cancelar_stokki, self.avisar)

    def preparar(self, status_nucleo="ABERTO", route=None, rota=None, codigo="PS-39959"):
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", codigo)])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {"motivo": "cliente desistiu"}, "cliente")
        if status_nucleo:
            conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-39959', 111, ?, 'not_assigned', ?, NULL)", (status_nucleo, route))
        if rota:
            conn.execute("INSERT INTO nucleo_rotas VALUES (?, ?, ?, ?, ?)", rota)
        conn.commit()
        return conn

    def test_sozinho_cancela_nas_duas_pontas_e_conclui(self):
        conn = self.preparar()
        r = self.rodar(conn)
        self.assertEqual(r, {"cancelados": 1, "operacao": 0, "falhas": 0})
        self.assertEqual(self.vuupt_chamadas, [111])
        self.assertEqual(self.stokki_chamadas[0][0], 39959)
        self.assertIn("cliente desistiu", self.stokki_chamadas[0][1])
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELADO)
        s = conn.execute("SELECT status, resposta FROM portal_solicitacoes").fetchone()
        self.assertEqual(s["status"], "CONCLUIDA")
        self.assertIn("automaticamente", s["resposta"])
        self.assertEqual(self.avisos, [])

    def test_sem_servico_no_nucleo_cancela_so_na_stokki(self):
        conn = self.preparar(status_nucleo=None)
        self.assertEqual(self.rodar(conn)["cancelados"], 1)
        self.assertEqual(self.vuupt_chamadas, [])
        self.assertEqual(len(self.stokki_chamadas), 1)

    def test_em_rota_volta_pra_criado_e_avisa(self):
        conn = self.preparar("EM_ROTA", 10, (10, "EM_ROTA", "started", "2026-10-06 08:00:00", 5))
        r = self.rodar(conn)
        self.assertEqual(r, {"cancelados": 0, "operacao": 1, "falhas": 0})
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)
        self.assertEqual(conn.execute("SELECT status FROM portal_solicitacoes").fetchone()[0], "PENDENTE")
        self.assertEqual(self.vuupt_chamadas + self.stokki_chamadas, [])
        self.assertEqual(self.avisos[0][0], 50)
        self.assertIn("em rota", self.avisos[0][1])

    def test_falha_na_vuupt_nao_toca_a_stokki(self):
        self.vuupt_ok = False
        conn = self.preparar()
        r = self.rodar(conn)
        self.assertEqual(r["falhas"], 1)
        self.assertEqual(self.stokki_chamadas, [])
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)
        self.assertIn("409", self.avisos[0][2])

    def test_falha_na_stokki_volta_pra_criado(self):
        self.stokki_ok = False
        conn = self.preparar()
        self.assertEqual(self.rodar(conn)["falhas"], 1)
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)
        self.assertIn("Stokki 500", self.avisos[0][2])

    def test_sem_codigo_vai_pra_operacao(self):
        conn = self.preparar(status_nucleo=None, codigo=None)
        self.assertEqual(self.rodar(conn)["operacao"], 1)
        self.assertIn("não identificado", self.avisos[0][1])

    def test_cancela_todas_as_reentregas_vivas(self):
        conn = self.preparar("CANCELADO")
        conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-39959-R1', 222, 'ABERTO', 'not_assigned', NULL, NULL)")
        conn.commit()
        self.assertEqual(self.rodar(conn)["cancelados"], 1)
        self.assertEqual(self.vuupt_chamadas, [222])

    def test_nada_a_fazer(self):
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        self.assertEqual(self.rodar(conn), {"cancelados": 0, "operacao": 0, "falhas": 0})


class AvisarOperacao(unittest.TestCase):
    def test_manda_email_e_whatsapp_e_nunca_levanta(self):
        from unittest import mock
        config = {"email": {"email_atendimento": "atendimento@x.com"}, "whatsapp_notificacoes": {}}
        e = {"id": 50, "numero_nf": "9959", "codigo_pedido": "PS-39959", "destinatario_nome": "D <script>x</script>", "cnpj_embarcador": "111"}
        with mock.patch.object(cm, "enviar_email", return_value=True) as em, \
             mock.patch("notificar_whatsapp.avisar_cancelamento_pendente", return_value="modo_teste") as wa:
            cm.avisar_operacao(config, e, "motorista em rota", "Stokki <b>500</b>")
        self.assertEqual(em.call_args.args[0], ["atendimento@x.com"])
        self.assertIn("PS-39959", em.call_args.args[1])
        self.assertIn("motorista em rota", em.call_args.args[2])
        # texto vindo do XML do cliente / da Stokki entra escapado no HTML do e-mail
        self.assertNotIn("<script>", em.call_args.args[2])
        self.assertIn("&lt;script&gt;", em.call_args.args[2])
        self.assertIn("Stokki &lt;b&gt;500", em.call_args.args[2])
        wa.assert_called_once()
        with mock.patch.object(cm, "enviar_email", side_effect=RuntimeError("smtp fora")):
            cm.avisar_operacao(config, e, "x", "")  # nao levanta


class Worker(unittest.TestCase):
    def test_ciclo_processa_cancelando_sob_a_trava(self):
        from unittest import mock
        import enviar_stokki as es
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        with mock.patch.object(es.sessao_uso, "adquirir", return_value=True) as adq, \
             mock.patch.object(es.sessao_uso, "liberar") as lib, \
             mock.patch.object(es, "StokkiSession") as sess, \
             mock.patch.object(es.cm, "processar_cancelamentos", return_value={"cancelados": 1, "operacao": 0, "falhas": 0}) as proc:
            r = es.processar_cancelamentos_do_ciclo(conn, {"vuupt_api": {"token": "t"}})
        self.assertEqual(r["cancelados"], 1)
        adq.assert_called_once()
        lib.assert_called_once_with(es.DONO_TRAVA)
        sess.assert_called_once()
        self.assertEqual(proc.call_args.args[0], conn)

    def test_sem_cancelando_nao_abre_sessao(self):
        from unittest import mock
        import enviar_stokki as es
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        with mock.patch.object(es, "StokkiSession") as sess, mock.patch.object(es.sessao_uso, "adquirir") as adq:
            self.assertEqual(es.processar_cancelamentos_do_ciclo(conn, {}), {"cancelados": 0, "operacao": 0, "falhas": 0})
        sess.assert_not_called()
        adq.assert_not_called()

    def test_trava_ocupada_deixa_pro_proximo_ciclo(self):
        from unittest import mock
        import enviar_stokki as es
        conn = conn_completo([(50, ep.STATUS_CRIADO, "9959", "PS-39959")])
        ep.aplicar_acao(conn, envio(conn, 50), "cancelar", {}, "cliente")
        with mock.patch.object(es.sessao_uso, "adquirir", return_value=False), mock.patch.object(es.sessao_uso, "em_uso", return_value="pipeline"):
            es.processar_cancelamentos_do_ciclo(conn, {})
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CANCELANDO)

    def test_cancelando_orfao_volta_pra_criado(self):
        import enviar_stokki as es
        conn = conn_completo([(50, ep.STATUS_CANCELANDO, "9959", "PS-39959")])
        conn.execute("UPDATE portal_envios SET atualizado_em = datetime('now','localtime','-31 minutes') WHERE id = 50")
        conn.commit()
        es.resetar_orfaos(conn)
        self.assertEqual(envio(conn, 50)["status"], ep.STATUS_CRIADO)


if __name__ == "__main__":
    unittest.main()
