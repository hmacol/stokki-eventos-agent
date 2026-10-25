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
                       "status, solicitado_por, criado_em, concluido_em, resposta);")
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

    def _r(self, nome):
        r = self.respostas.get(nome, {"ok": True, "erro": ""})
        if isinstance(r, Exception):
            raise r
        return r

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
        self.assertEqual(f.nomes(), ["buscar_servico", "reagendar_vuupt"])
        self.assertIn(("reagendar_vuupt", 777, dia, "09:00", "12:00"), f.chamadas)
        self.assertTrue(envio(conn)["agendamento_aplicado_em"])
        s = solicitacao(conn, sid)
        self.assertEqual(s["status"], "CONCLUIDA")
        self.assertIn("09:00", s["resposta"])

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
    """Só a fiação da trava da Stokki (nada de rede)."""
    def test_trava_pega_e_libera_com_o_mesmo_dono(self):
        from unittest import mock
        from stokki import sessao_uso
        with mock.patch.object(sessao_uso, "adquirir", return_value="principal") as adq,                 mock.patch.object(sessao_uso, "liberar") as lib:
            d = se.dependencias_reais({"vuupt_api": {"token": "x"}, "lalamove": {"agent_id_vuupt": 555}})
            self.assertTrue(d.abrir_stokki())
            d.fechar_stokki()
        self.assertEqual(d.agent_lalamove, 555)
        self.assertEqual(adq.call_args.kwargs["esperar_segundos"], 60)
        self.assertEqual(lib.call_args.args[0], adq.call_args.args[0])

    def test_sem_vez_nao_libera_trava_alheia(self):
        from unittest import mock
        from stokki import sessao_uso
        with mock.patch.object(sessao_uso, "adquirir", return_value=False),                 mock.patch.object(sessao_uso, "liberar") as lib:
            d = se.dependencias_reais({})
            self.assertFalse(d.abrir_stokki())
            d.fechar_stokki()
        lib.assert_not_called()


if __name__ == "__main__":
    unittest.main()
