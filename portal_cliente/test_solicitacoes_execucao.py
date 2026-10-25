# -*- coding: utf-8 -*-
"""
Executar/recusar solicitação do portal pela tela da operação (aviso de
pendências, Task 6). Tudo que fala com Stokki/Vuupt é falso (Dependencias).

    py -3.11 -m unittest portal_cliente.test_solicitacoes_execucao
"""
import sqlite3
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
import solicitacoes_execucao as se  # noqa: E402

COLUNAS_ENVIO = ("id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT, atualizado_em TEXT, "
                 "numero_nf TEXT, referencia TEXT, origem TEXT, destinatario_nome TEXT, destinatario_doc TEXT, "
                 "codigo_pedido TEXT, horario_inicio TEXT, horario_fim TEXT, requer_agendamento INTEGER, "
                 "agendamento_pendente INTEGER, agendamento_data TEXT, agendamento_hora_inicio TEXT, "
                 "agendamento_hora_fim TEXT, agendamento_aplicado_em TEXT")


def conn_portal(com_nucleo=False):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(f"CREATE TABLE portal_envios ({COLUNAS_ENVIO});"
                       "CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, "
                       "status, solicitado_por, criado_em, concluido_em, resposta);"
                       # mesmas colunas que envio_pedidos.registrar_agendamento_pedido grava
                       "CREATE TABLE agendamentos_pedido (pedido TEXT PRIMARY KEY, cnpj_destinatario, nome_destinatario, "
                       "cnpj_embarcador, numero_nf, email_embarcador, status, data_agendada, horario_inicio_agendado, "
                       "horario_fim_agendado, resposta_texto, solicitado_em, respondido_em, aplicado_vuupt_em);")
    if com_nucleo:
        conn.executescript("CREATE TABLE nucleo_pedidos (codigo, vuupt_service_id, status, status_provedor, vuupt_route_id, excluido_em);"
                           "CREATE TABLE nucleo_rotas (vuupt_route_id, status, status_provedor, iniciada_em, agent_id);")
    return conn


def novo_envio(conn, id_=50, status=ep.STATUS_CRIADO, codigo="PS-39959", ag_data=None, ini=None, fim=None):
    conn.execute("INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, atualizado_em, numero_nf, codigo_pedido, "
                 "agendamento_data, agendamento_hora_inicio, agendamento_hora_fim) "
                 "VALUES (?, '111', ?, datetime('now','localtime'), datetime('now','localtime'), '9959', ?, ?, ?, ?)",
                 (id_, status, codigo, ag_data, ini, fim))
    conn.commit()


def nova_solicitacao(conn, tipo, envio_id=50, status="PENDENTE", detalhes="cliente pediu"):
    cur = conn.execute("INSERT INTO portal_solicitacoes (envio_id, cnpj_embarcador, tipo, detalhes, status, solicitado_por, criado_em) "
                       "VALUES (?, '111', ?, ?, ?, 'cliente', datetime('now','localtime'))", (envio_id, tipo, detalhes, status))
    conn.commit()
    return cur.lastrowid


def solicitacao(conn, id_):
    return dict(conn.execute("SELECT * FROM portal_solicitacoes WHERE id = ?", (id_,)).fetchone())


def envio(conn, id_=50):
    return dict(conn.execute("SELECT * FROM portal_envios WHERE id = ?", (id_,)).fetchone())


class Fakes:
    """Dependencias falsas que anotam cada chamada, na ordem, em `chamadas`."""
    def __init__(self, servico=None, stokki_livre=True, **respostas):
        self.chamadas = []
        self.servico = {"id": 777, "status": "assigned"} if servico is None else servico
        self.stokki_livre = stokki_livre
        self.respostas = respostas

    PADRAO = {"tirar_da_rota_vuupt": {"ok": True, "erro": "", "rota_id": 9001, "rota_cancelada": False}}

    def _r(self, nome):
        r = self.respostas.get(nome, self.PADRAO.get(nome, {"ok": True, "erro": ""}))
        if isinstance(r, Exception):
            raise r
        return r() if callable(r) else r

    def deps(self, agent_lalamove=0):
        def anota(nome, resp=True):
            def f(*args):
                self.chamadas.append((nome, *args))
                return self._r(nome) if resp else None
            return f

        def buscar(codigo):
            self.chamadas.append(("buscar_servico", codigo))
            return self.servico or None

        def abrir():
            self.chamadas.append(("abrir_stokki",))
            if isinstance(self.stokki_livre, Exception):
                raise self.stokki_livre
            return self.stokki_livre

        return se.Dependencias(
            buscar_servico=buscar, cancelar_vuupt=anota("cancelar_vuupt"), cancelar_stokki=anota("cancelar_stokki"),
            pausar_stokki=anota("pausar_stokki"), tirar_da_rota_vuupt=anota("tirar_da_rota_vuupt"),
            reagendar_vuupt=anota("reagendar_vuupt"), abrir_stokki=abrir, fechar_stokki=anota("fechar_stokki", resp=False),
            agent_lalamove=agent_lalamove)

    def nomes(self):
        return [c[0] for c in self.chamadas]


class Executar(unittest.TestCase):
    def test_cancelar_ok_conclui(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "cancelar", detalhes="cliente desistiu")
        f = Fakes()
        r = se.executar(conn, sid, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual(f.nomes(), ["abrir_stokki", "buscar_servico", "cancelar_vuupt", "cancelar_stokki", "fechar_stokki"])
        self.assertIn(("cancelar_vuupt", 777), f.chamadas)
        self.assertIn(("cancelar_stokki", 39959, "cliente desistiu"), f.chamadas)
        s = solicitacao(conn, sid)
        self.assertEqual(s["status"], "CONCLUIDA")
        self.assertTrue(s["resposta"])
        self.assertEqual(envio(conn)["status"], ep.STATUS_CANCELADO)

    def test_cancelar_vuupt_recusa_nao_conclui(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "cancelar")
        f = Fakes(cancelar_vuupt={"ok": False, "erro": "servico 777 esta 'on_route' na Vuupt: nao cancela sozinho"})
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertIn("on_route", r["mensagem"])
        self.assertNotIn("cancelar_stokki", f.nomes())
        self.assertEqual(f.nomes()[-1], "fechar_stokki")
        self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")
        self.assertEqual(envio(conn)["status"], ep.STATUS_CRIADO)

    def test_cancelar_stokki_falha_diz_o_que_foi_feito(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "cancelar")
        f = Fakes(cancelar_stokki={"ok": False, "erro": "pedido ja expedido na Stokki (Enviado)"})
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertIn("expedido", r["mensagem"])
        self.assertIn("Vuupt", r["mensagem"])
        self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")
        self.assertEqual(envio(conn)["status"], ep.STATUS_CRIADO)

    def test_cancelar_rota_lalamove_nao_executa(self):
        conn = conn_portal(com_nucleo=True)
        novo_envio(conn)
        conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-39959', 777, 'EM_ROTA', 'assigned', 9001, NULL)")
        conn.execute("INSERT INTO nucleo_rotas VALUES (9001, 'PLANEJADA', 'not_started', NULL, 555)")
        sid = nova_solicitacao(conn, "cancelar")
        f = Fakes()
        r = se.executar(conn, sid, f.deps(agent_lalamove=555))
        self.assertFalse(r["ok"])
        self.assertIn("Lalamove", r["mensagem"])
        self.assertFalse({"cancelar_vuupt", "cancelar_stokki"} & set(f.nomes()))
        self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")

    def test_cancelar_enquanto_worker_cancela_nao_executa(self):
        conn = conn_portal()
        novo_envio(conn, status=ep.STATUS_CANCELANDO)
        sid = nova_solicitacao(conn, "cancelar")
        f = Fakes()
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertFalse({"cancelar_vuupt", "cancelar_stokki"} & set(f.nomes()))

    def test_em_espera_tira_da_rota_e_pausa(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "em_espera", detalhes="segurar ate segunda")
        f = Fakes()
        r = se.executar(conn, sid, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual(f.nomes(), ["abrir_stokki", "buscar_servico", "tirar_da_rota_vuupt", "pausar_stokki", "fechar_stokki"])
        self.assertIn(("tirar_da_rota_vuupt", 777), f.chamadas)
        self.assertIn(("pausar_stokki", 39959, "segurar ate segunda"), f.chamadas)
        self.assertEqual(solicitacao(conn, sid)["status"], "CONCLUIDA")
        self.assertEqual(envio(conn)["status"], ep.STATUS_CRIADO)
        self.assertIn("tirado da rota 9001", r["mensagem"])
        self.assertIn(se.AVISO_REROTEIRIZACAO, r["mensagem"])
        self.assertNotIn("roteirização", solicitacao(conn, sid)["resposta"])  # aviso é só pro operador

    def test_em_espera_ultima_parada_avisa_rota_cancelada(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "em_espera")
        f = Fakes(tirar_da_rota_vuupt={"ok": True, "erro": "", "rota_id": 9001, "rota_cancelada": True})
        r = se.executar(conn, sid, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertIn("a rota 9001 ficou vazia e foi cancelada", r["mensagem"])

    def test_abrir_stokki_com_excecao_vira_ok_false(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "em_espera")
        f = Fakes(stokki_livre=sqlite3.OperationalError("database is locked"))
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertIn("database is locked", r["mensagem"])
        self.assertEqual(f.nomes(), ["abrir_stokki"])
        self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")

    def test_em_espera_falha_no_pausar_nao_conclui(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "em_espera")
        f = Fakes(pausar_stokki={"ok": False, "erro": "Stokki aceitou mas a situacao ficou 'Aguardando Transportador'"})
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertIn("fora da rota", r["mensagem"])
        self.assertIn("Aguardando Transportador", r["mensagem"])
        self.assertEqual(f.nomes()[-1], "fechar_stokki")
        self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")

    def test_em_espera_vuupt_recusa_nao_pausa(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "em_espera")
        f = Fakes(tirar_da_rota_vuupt={"ok": False, "erro": "Rota #9001 não pode ser alterada por aqui"})
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertIn("9001", r["mensagem"])
        self.assertNotIn("pausar_stokki", f.nomes())
        self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")

    def test_em_espera_sem_servico_na_vuupt_so_pausa(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "em_espera")
        f = Fakes(servico={})
        r = se.executar(conn, sid, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertNotIn("tirar_da_rota_vuupt", f.nomes())
        self.assertIn("pausar_stokki", f.nomes())

    def test_excecao_numa_etapa_nao_conclui_e_libera_a_stokki(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "em_espera")
        f = Fakes(pausar_stokki=ConnectionError("timeout"))
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertIn("timeout", r["mensagem"])
        self.assertIn("fora da rota", r["mensagem"])
        self.assertEqual(f.nomes()[-1], "fechar_stokki")
        self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")

    def test_reagendar_aplica_data_e_conclui(self):
        conn = conn_portal()
        dia = (date.today() + timedelta(days=2)).isoformat()
        novo_envio(conn, ag_data=dia, ini="09:00", fim="12:00")
        sid = nova_solicitacao(conn, "reagendar")
        f = Fakes()
        r = se.executar(conn, sid, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual(f.nomes(), ["buscar_servico", "tirar_da_rota_vuupt", "reagendar_vuupt"])
        self.assertIn(("reagendar_vuupt", 777, dia, "09:00", "12:00"), f.chamadas)
        self.assertTrue(envio(conn)["agendamento_aplicado_em"])
        s = solicitacao(conn, sid)
        self.assertEqual(s["status"], "CONCLUIDA")
        self.assertIn("09:00", s["resposta"])

    def test_reagendar_mantem_origem_do_cliente(self):
        conn = conn_portal()
        dia = date.today() + timedelta(days=2)
        novo_envio(conn, ag_data=dia.isoformat(), ini="09:00", fim="12:00")
        sid = nova_solicitacao(conn, "reagendar")
        r = se.executar(conn, sid, Fakes().deps())
        self.assertTrue(r["ok"], r)
        a = dict(conn.execute("SELECT * FROM agendamentos_pedido WHERE pedido = 'PS-39959'").fetchone())
        self.assertEqual((a["status"], a["data_agendada"], a["horario_inicio_agendado"]),
                         ("RESPONDIDO", dia.strftime("%d/%m/%Y"), "09:00"))
        self.assertIn("portal do cliente", a["resposta_texto"])
        self.assertTrue(a["aplicado_vuupt_em"])  # já aplicado aqui: o job não reaplica
        # nenhuma origem EQUIPE/DIA_FIXO gravada (sem linha = data do cliente)
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'agendamentos_origem'").fetchone())

    def test_reagendar_servico_em_rota_tira_da_rota_antes(self):
        conn = conn_portal()
        novo_envio(conn, ag_data=(date.today() + timedelta(days=2)).isoformat(), ini="09:00", fim="12:00")
        sid = nova_solicitacao(conn, "reagendar")
        f = Fakes()
        r = se.executar(conn, sid, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertLess(f.nomes().index("tirar_da_rota_vuupt"), f.nomes().index("reagendar_vuupt"))
        self.assertIn("tirado da rota 9001", r["mensagem"])
        self.assertIn("data nova", r["mensagem"])

    def test_reagendar_servico_fora_de_rota_so_aplica_data(self):
        conn = conn_portal()
        novo_envio(conn, ag_data=(date.today() + timedelta(days=2)).isoformat(), ini="09:00", fim="12:00")
        sid = nova_solicitacao(conn, "reagendar")
        f = Fakes(tirar_da_rota_vuupt={"ok": True, "erro": "", "rota_id": None, "rota_cancelada": False})
        r = se.executar(conn, sid, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertNotIn("tirado da rota", r["mensagem"])
        self.assertIn("reagendar_vuupt", f.nomes())

    def test_reagendar_rota_ja_saiu_nao_reagenda(self):
        conn = conn_portal()
        novo_envio(conn, ag_data=(date.today() + timedelta(days=2)).isoformat(), ini="09:00", fim="12:00")
        sid = nova_solicitacao(conn, "reagendar")
        f = Fakes(tirar_da_rota_vuupt={"ok": False, "erro": "Rota #9001 não pode ser alterada por aqui"})
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertNotIn("reagendar_vuupt", f.nomes())
        self.assertIsNone(conn.execute("SELECT 1 FROM agendamentos_pedido").fetchone())
        self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")

    def test_reagendar_rota_lalamove_recusa(self):
        conn = conn_portal(com_nucleo=True)
        novo_envio(conn, ag_data=(date.today() + timedelta(days=2)).isoformat(), ini="09:00", fim="12:00")
        conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-39959', 777, 'EM_ROTA', 'assigned', 9001, NULL)")
        conn.execute("INSERT INTO nucleo_rotas VALUES (9001, 'PLANEJADA', 'not_started', NULL, 555)")
        sid = nova_solicitacao(conn, "reagendar")
        f = Fakes()
        r = se.executar(conn, sid, f.deps(agent_lalamove=555))
        self.assertFalse(r["ok"])
        self.assertIn("Lalamove", r["mensagem"])
        self.assertFalse({"tirar_da_rota_vuupt", "reagendar_vuupt"} & set(f.nomes()))

    def test_fechada_durante_a_execucao_nao_conclui(self):
        conn = conn_portal()
        novo_envio(conn, ag_data=(date.today() + timedelta(days=2)).isoformat(), ini="09:00", fim="12:00")
        sid = nova_solicitacao(conn, "reagendar")

        def outro_fecha():
            conn.execute("UPDATE portal_solicitacoes SET status = 'RECUSADA', resposta = 'outro' WHERE id = ?", (sid,))
            return {"ok": True, "erro": ""}
        f = Fakes(reagendar_vuupt=outro_fecha)
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertIn("RECUSADA", r["mensagem"])
        self.assertEqual(solicitacao(conn, sid)["resposta"], "outro")

    def test_reagendar_data_passada_nao_executa(self):
        conn = conn_portal()
        novo_envio(conn, ag_data=(date.today() - timedelta(days=1)).isoformat(), ini="09:00", fim="12:00")
        sid = nova_solicitacao(conn, "reagendar")
        f = Fakes()
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertNotIn("reagendar_vuupt", f.nomes())
        self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")

    def test_stokki_ocupada_nao_conclui(self):
        for tipo in ("cancelar", "em_espera"):
            conn = conn_portal()
            novo_envio(conn)
            sid = nova_solicitacao(conn, tipo)
            f = Fakes(stokki_livre=False)
            r = se.executar(conn, sid, f.deps())
            self.assertFalse(r["ok"], tipo)
            self.assertIn("Stokki ocupada", r["mensagem"])
            self.assertEqual(f.nomes(), ["abrir_stokki"], tipo)
            self.assertEqual(solicitacao(conn, sid)["status"], "PENDENTE")

    def test_solicitacao_ja_concluida_recusa_executar(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "cancelar", status="CONCLUIDA")
        f = Fakes()
        r = se.executar(conn, sid, f.deps())
        self.assertFalse(r["ok"])
        self.assertEqual(f.chamadas, [])

    def test_solicitacao_inexistente(self):
        f = Fakes()
        r = se.executar(conn_portal(), 999, f.deps())
        self.assertFalse(r["ok"])
        self.assertEqual(f.chamadas, [])


class Recusar(unittest.TestCase):
    def test_recusar_marca_recusada(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "cancelar")
        se.recusar(conn, sid, "Motorista já saiu com o pedido.")
        s = solicitacao(conn, sid)
        self.assertEqual((s["status"], s["resposta"]), ("RECUSADA", "Motorista já saiu com o pedido."))
        self.assertEqual(envio(conn)["status"], ep.STATUS_CRIADO)

    def test_recusar_ja_fechada_levanta(self):
        conn = conn_portal()
        novo_envio(conn)
        sid = nova_solicitacao(conn, "cancelar", status="CONCLUIDA")
        with self.assertRaises(ValueError):
            se.recusar(conn, sid, "tarde demais")


class DependenciasReais(unittest.TestCase):
    """Fiação das dependências de produção, com Vuupt/Stokki/expedição falsas (nada de rede)."""
    def test_trava_pega_e_libera_com_o_mesmo_dono(self):
        from unittest import mock
        from stokki import sessao_uso
        with mock.patch.object(sessao_uso, "adquirir", return_value="principal") as adq, \
                mock.patch.object(sessao_uso, "liberar") as lib:
            d = se.dependencias_reais({"vuupt_api": {"token": "x"}, "lalamove": {"agent_id_vuupt": 555}})
            self.assertTrue(d.abrir_stokki())
            d.fechar_stokki()
        self.assertEqual(d.agent_lalamove, 555)
        self.assertEqual(adq.call_args.kwargs["esperar_segundos"], 60)
        self.assertEqual(lib.call_args.args[0], adq.call_args.args[0])

    def test_sem_vez_nao_libera_trava_alheia(self):
        from unittest import mock
        from stokki import sessao_uso
        with mock.patch.object(sessao_uso, "adquirir", return_value=False), \
                mock.patch.object(sessao_uso, "liberar") as lib:
            d = se.dependencias_reais({})
            self.assertFalse(d.abrir_stokki())
            d.fechar_stokki()
        lib.assert_not_called()

    def _modulos_reais(self):
        se._caminhos_painel()
        import expedicao
        import rotas_client
        import vuupt_client
        from roteirizacao import cancelar_servico
        return expedicao, rotas_client, vuupt_client, cancelar_servico

    def test_tirar_da_rota_usa_a_data_da_rota(self):
        from unittest import mock
        expedicao, rotas_client, vuupt_client, cancelar_servico = self._modulos_reais()
        amanha = date.today() + timedelta(days=1)
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_id.return_value = {"id": 777, "status": "assigned", "route_id": 9001}
        # start_at em UTC sem fuso (como a Vuupt devolve): 11:00 UTC = 08:00 em SP, mesmo dia
        with mock.patch.object(vuupt_client, "VuuptClient", return_value=vuupt), \
                mock.patch.object(rotas_client, "buscar_rota", return_value={"route": {"start_at": f"{amanha} 11:00:00"}}), \
                mock.patch.object(cancelar_servico, "rascunho_do_servico", return_value=(None, None)), \
                mock.patch.object(expedicao, "excluir_pedido_da_rota", return_value={"ok": True, "rota_cancelada": True}) as exc:
            r = se.dependencias_reais({"vuupt_api": {"token": "x"}}, usuario="ana").tirar_da_rota_vuupt(777)
        self.assertEqual(r, {"ok": True, "erro": "", "rota_id": 9001, "rota_cancelada": True})
        args = exc.call_args.args
        self.assertEqual((args[0], args[1], args[2], args[3]), (amanha, 9001, 777, "Outros"))
        self.assertIn(args[3], expedicao.MOTIVOS_EXCLUSAO)
        self.assertNotIn("permitir_rota_em_andamento", exc.call_args.kwargs)  # só rota que não saiu

    def test_tirar_da_rota_de_ontem_usa_hoje(self):
        from unittest import mock
        expedicao, rotas_client, vuupt_client, cancelar_servico = self._modulos_reais()
        ontem = date.today() - timedelta(days=1)
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_id.return_value = {"id": 777, "status": "assigned", "route_id": 9001}
        with mock.patch.object(vuupt_client, "VuuptClient", return_value=vuupt), \
                mock.patch.object(rotas_client, "buscar_rota", return_value={"start_at": f"{ontem} 11:00:00"}), \
                mock.patch.object(cancelar_servico, "rascunho_do_servico", return_value=(None, None)), \
                mock.patch.object(expedicao, "excluir_pedido_da_rota", return_value={"ok": True}) as exc:
            se.dependencias_reais({}).tirar_da_rota_vuupt(777)
        self.assertEqual(exc.call_args.args[0], date.today())

    def test_tirar_da_rota_servico_sem_rota_nao_mexe(self):
        from unittest import mock
        expedicao, rotas_client, vuupt_client, cancelar_servico = self._modulos_reais()
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_id.return_value = {"id": 777, "status": "not_assigned", "route_id": None}
        with mock.patch.object(vuupt_client, "VuuptClient", return_value=vuupt), \
                mock.patch.object(cancelar_servico, "rascunho_do_servico", return_value=(None, None)), \
                mock.patch.object(expedicao, "excluir_pedido_da_rota") as exc:
            r = se.dependencias_reais({}).tirar_da_rota_vuupt(777)
        self.assertEqual((r["ok"], r["rota_id"]), (True, None))
        exc.assert_not_called()

    def test_reagendar_real_put_direto_sem_origem_equipe(self):
        from unittest import mock
        _, _, vuupt_client, _ = self._modulos_reais()
        import planejamento_rotas
        import registro_dia_fixo
        vuupt = mock.Mock()
        with mock.patch.object(vuupt_client, "VuuptClient", return_value=vuupt), \
                mock.patch("nucleo.sincronizar_servicos_vuupt.ressincronizar_ids"), \
                mock.patch.object(planejamento_rotas, "reagendar_pedido") as plan, \
                mock.patch.object(registro_dia_fixo, "registrar_origens") as origens:
            r = se.dependencias_reais({}).reagendar_vuupt(777, "2026-11-03", "09:00", "12:00")
        self.assertTrue(r["ok"], r)
        vuupt.atualizar_servico.assert_called_once_with(
            777, {"scheduled_start": "2026-11-03T09:00:00-03:00", "scheduled_end": "2026-11-03T12:00:00-03:00"})
        plan.assert_not_called()
        origens.assert_not_called()


if __name__ == "__main__":
    unittest.main()
