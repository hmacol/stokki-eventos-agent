# -*- coding: utf-8 -*-
"""
Worker da retomada: cliente retoma pedido em espera a pedido dele (spec
2026-10-25, secao 4 item 3). Nada na Stokki.

    py -3.11 -m unittest portal_cliente.test_retomada
"""
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cancelamento as cm  # noqa: E402
import enviar_stokki as wk  # noqa: E402
import envio_pedidos as ep  # noqa: E402
import pedidos_pausados_cliente as ppc  # noqa: E402
import retomada  # noqa: E402
from test_cancelamento import conn_portal, envio  # noqa: E402

AGORA = datetime(2026, 10, 28, 10, 0, 0)
AMANHA = date(2026, 10, 29)


def montar(status=ep.STATUS_RETOMANDO, modo="proxima_rota", data=None, ini=None, fim=None, servicos=((901, "PS-39960"),)):
    conn = conn_portal([(60, status, "9960", "PS-39960")])
    ep._garantir_colunas(conn, "portal_envios", {**ep.COLUNAS_RETOMAR, "agendamento_hora_inicio": "TEXT",
                                                 "agendamento_hora_fim": "TEXT", "agendamento_aplicado_em": "TEXT",
                                                 "horario_inicio": "TEXT", "horario_fim": "TEXT"})
    conn.execute("UPDATE portal_envios SET retomar_modo=?, retomar_data=?, retomar_hora_inicio=?, retomar_hora_fim=?, "
                 "cnpj_embarcador='111' WHERE id=60", (modo, data, ini, fim))
    conn.executescript("""
        CREATE TABLE nucleo_pedidos (codigo TEXT, vuupt_service_id INTEGER, status TEXT, status_provedor TEXT,
                                     vuupt_route_id INTEGER, excluido_em TEXT);
        CREATE TABLE nucleo_rotas (vuupt_route_id INTEGER, status TEXT, status_provedor TEXT, iniciada_em TEXT, agent_id INTEGER);
        CREATE TABLE agendamentos_pedido (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido TEXT UNIQUE, cnpj_destinatario TEXT,
            nome_destinatario TEXT, cnpj_embarcador TEXT, numero_nf TEXT, email_embarcador TEXT, status TEXT, data_agendada TEXT,
            horario_inicio_agendado TEXT, horario_fim_agendado TEXT, resposta_texto TEXT, solicitado_em TEXT,
            respondido_em TEXT, aplicado_vuupt_em TEXT);
    """)
    for sid, cod in servicos:
        conn.execute("INSERT INTO nucleo_pedidos VALUES (?, ?, 'ABERTO', 'pending', NULL, NULL)", (cod, sid))
    ppc.garantir_tabela(conn)
    if status in (ep.STATUS_RETOMANDO, ep.STATUS_EM_ESPERA_CLIENTE):
        ppc.abrir(conn, "PS-39960", 39960, 60, [s for s, _ in servicos], "2026-10-27 09:00:00")
    conn.commit()
    return conn


class Falsos:
    def __init__(self, proxima=AMANHA, ok=True):
        self.puts, self.avisos, self.proxima, self.ok = [], [], proxima, ok

    def aplicar(self, sid, dia, ini, fim):
        self.puts.append((sid, dia, ini, fim))
        return {"ok": True, "erro": ""} if self.ok else {"ok": False, "erro": "Vuupt 500"}

    def proxima_data(self, servico):
        return self.proxima

    def avisar(self, e, motivo, erro):
        self.avisos.append((e["id"], motivo, erro))

    def rodar(self, conn):
        return retomada.processar_retomadas(conn, {}, self.aplicar, self.proxima_data, self.avisar, agora=AGORA)


def pausa(conn):
    return dict(conn.execute("SELECT retomado_em, fim FROM pedidos_pausados_cliente WHERE codigo='PS-39960'").fetchone())


class Retomada(unittest.TestCase):
    def test_retomar_proxima_rota_aplica_dia_fixo_e_fecha(self):
        conn = montar()
        f = Falsos()
        r = f.rodar(conn)
        self.assertEqual(r, {"retomados": 1, "falhas": 0})
        self.assertEqual(len(f.puts), 1)
        self.assertEqual(f.puts[0][:2], (901, "2026-10-29"))
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_CRIADO)
        self.assertEqual(pausa(conn), {"retomado_em": "2026-10-28 10:00:00", "fim": "retomado"})
        # dia fixo e da regiao, nao escolha do cliente: nada em agendamentos_pedido
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM agendamentos_pedido").fetchone()[0], 0)

    def test_retomar_agendamento_aplica_data_e_fecha(self):
        conn = montar(modo="agendamento", data="2026-11-03", ini="09:00", fim="12:00")
        f = Falsos()
        self.assertEqual(f.rodar(conn), {"retomados": 1, "falhas": 0})
        self.assertEqual(f.puts, [(901, "2026-11-03", "09:00", "12:00")])
        ag = dict(conn.execute("SELECT * FROM agendamentos_pedido").fetchone())
        self.assertEqual((ag["pedido"], ag["status"], ag["data_agendada"], ag["horario_inicio_agendado"]),
                         ("PS-39960", "RESPONDIDO", "03/11/2026", "09:00"))
        self.assertTrue(ag["aplicado_vuupt_em"])  # ja aplicado na Vuupt acima
        e = envio(conn, 60)
        self.assertEqual((e["status"], e["agendamento_data"], e["agendamento_pendente"]), (ep.STATUS_CRIADO, "2026-11-03", 0))
        self.assertEqual(pausa(conn)["fim"], "retomado")

    def test_agendamento_sem_servico_na_vuupt_so_registra(self):
        conn = montar(modo="agendamento", data="2026-11-03", ini="09:00", fim="12:00", servicos=())
        f = Falsos()
        self.assertEqual(f.rodar(conn), {"retomados": 1, "falhas": 0})
        self.assertEqual(f.puts, [])
        self.assertIsNone(conn.execute("SELECT aplicado_vuupt_em FROM agendamentos_pedido").fetchone()[0])

    def test_regiao_sem_dia_fixo_nao_mexe_na_vuupt(self):
        conn = montar()
        f = Falsos(proxima=None)
        self.assertEqual(f.rodar(conn), {"retomados": 1, "falhas": 0})
        self.assertEqual(f.puts, [])
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_CRIADO)
        self.assertEqual(pausa(conn)["fim"], "retomado")

    def test_vuupt_falha_volta_em_espera_e_avisa(self):
        conn = montar()
        f = Falsos(ok=False)
        self.assertEqual(f.rodar(conn), {"retomados": 0, "falhas": 1})
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_EM_ESPERA_CLIENTE)
        self.assertIsNone(pausa(conn)["retomado_em"])
        self.assertEqual(len(f.avisos), 1)
        self.assertIn("Vuupt 500", f.avisos[0][2])

    def test_excecao_volta_em_espera_e_avisa(self):
        conn = montar()
        f = Falsos()

        def estoura(*a):
            raise RuntimeError("rede")
        r = retomada.processar_retomadas(conn, {}, estoura, f.proxima_data, f.avisar, agora=AGORA)
        self.assertEqual(r, {"retomados": 0, "falhas": 1})
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_EM_ESPERA_CLIENTE)
        self.assertEqual(len(f.avisos), 1)

    def test_retomar_de_novo_e_idempotente(self):
        # falha, volta pra espera, cliente retoma outra vez, desta vez funciona
        conn = montar()
        Falsos(ok=False).rodar(conn)
        ep.aplicar_acao(conn, envio(conn, 60), "retomar", {"modo": "proxima_rota"}, "cliente")
        f = Falsos()
        self.assertEqual(f.rodar(conn), {"retomados": 1, "falhas": 0})
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_CRIADO)
        # e rodar de novo sem RETOMANDO nao faz nada
        self.assertEqual(f.rodar(conn), {"retomados": 0, "falhas": 0})
        self.assertEqual(len(f.puts), 1)

    def test_pausa_ja_fechada_nao_impede_concluir(self):
        # worker caiu entre fechar e mudar o status: a repeticao conclui
        conn = montar()
        ppc.fechar(conn, "PS-39960", "retomado", "2026-10-28 09:59:00")
        self.assertEqual(Falsos().rodar(conn), {"retomados": 1, "falhas": 0})
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_CRIADO)
        self.assertEqual(pausa(conn)["retomado_em"], "2026-10-28 09:59:00")

    def test_status_mudou_no_meio_nao_e_sobrescrito(self):
        conn = montar()
        f = Falsos()

        def aplicar_e_cancela(sid, dia, ini, fim):
            conn.execute("UPDATE portal_envios SET status = ? WHERE id = 60", (ep.STATUS_CANCELADO,))
            conn.commit()
            return {"ok": True, "erro": ""}
        retomada.processar_retomadas(conn, {}, aplicar_e_cancela, f.proxima_data, f.avisar, agora=AGORA)
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_CANCELADO)
        self.assertIsNone(pausa(conn)["retomado_em"])


class Orfaos(unittest.TestCase):
    def test_orfao_retomando_volta(self):
        conn = montar()
        conn.execute("UPDATE portal_envios SET atualizado_em = datetime('now','localtime','-31 minutes') WHERE id = 60")
        conn.commit()
        avisos = []
        wk.resetar_orfaos(conn, avisar=lambda e, m, err, acao="cancelamento": avisos.append((e["id"], m, acao)))
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_EM_ESPERA_CLIENTE)
        self.assertEqual(avisos, [(60, "retomada não concluída em 30 min (worker parado)", "retomada")])

    def test_retomando_recente_nao_e_orfao(self):
        conn = montar()
        conn.execute("UPDATE portal_envios SET atualizado_em = datetime('now','localtime') WHERE id = 60")
        conn.commit()
        wk.resetar_orfaos(conn)
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_RETOMANDO)

    def test_cancelando_orfao_de_pedido_em_espera_volta_pra_espera(self):
        conn = montar(status=ep.STATUS_CANCELANDO)
        ppc.abrir(conn, "PS-39960", 39960, 60, [901], "2026-10-27 09:00:00")
        conn.execute("UPDATE portal_envios SET atualizado_em = datetime('now','localtime','-31 minutes') WHERE id = 60")
        conn.commit()
        wk.resetar_orfaos(conn)
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_EM_ESPERA_CLIENTE)

    def test_cancelando_orfao_comum_volta_pra_criado(self):
        conn = montar(status=ep.STATUS_CANCELANDO)
        conn.execute("UPDATE portal_envios SET atualizado_em = datetime('now','localtime','-31 minutes') WHERE id = 60")
        conn.commit()
        wk.resetar_orfaos(conn)
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_CRIADO)


class CancelamentoDoPedidoEmEspera(unittest.TestCase):
    def test_cancelamento_concluido_fecha_pausa(self):
        conn = montar(status=ep.STATUS_CANCELANDO)
        ppc.abrir(conn, "PS-39960", 39960, 60, [901], "2026-10-27 09:00:00")
        r = cm.processar_cancelamentos(conn, {}, lambda sid: {"ok": True}, lambda i, m: {"ok": True},
                                       lambda *a: None, agora=AGORA)
        self.assertEqual(r["cancelados"], 1)
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_CANCELADO)
        self.assertEqual(pausa(conn), {"retomado_em": "2026-10-28 10:00:00", "fim": "cancelado"})

    def test_motorista_ja_saiu_devolve_pedido_em_espera_pra_espera(self):
        conn = montar(status=ep.STATUS_CANCELANDO)
        ppc.abrir(conn, "PS-39960", 39960, 60, [901], "2026-10-27 09:00:00")
        conn.execute("UPDATE nucleo_pedidos SET status_provedor='on_route'")
        conn.execute("UPDATE nucleo_pedidos SET vuupt_route_id=7")
        conn.execute("INSERT INTO nucleo_rotas VALUES (7, 'EM_ROTA', 'started', '2026-10-28 08:00', 1)")
        conn.commit()
        avisos = []
        r = cm.processar_cancelamentos(conn, {}, lambda sid: {"ok": True}, lambda i, m: {"ok": True},
                                       lambda *a: avisos.append(a), agora=AGORA)
        self.assertEqual(r["operacao"], 1)
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_EM_ESPERA_CLIENTE)
        self.assertEqual(len(avisos), 1)

    def test_cancelamento_concluido_pela_operacao_fecha_pausa(self):
        conn = montar(status=ep.STATUS_CANCELANDO)
        ppc.abrir(conn, "PS-39960", 39960, 60, [901], "2026-10-27 09:00:00")
        conn.execute("INSERT INTO portal_solicitacoes (id, envio_id, cnpj_embarcador, tipo, status, criado_em) "
                     "VALUES (5, 60, '111', 'cancelar', 'PENDENTE', '2026-10-28 09:00:00')")
        conn.commit()
        ep.concluir_solicitacao(conn, 5, "cancelado pela operacao")
        self.assertEqual(pausa(conn)["fim"], "cancelado")


class GuardasDoPortal(unittest.TestCase):
    def test_retomar_com_status_desatualizado_recusa(self):
        conn = montar(status=ep.STATUS_EM_ESPERA_CLIENTE, modo=None)
        velho = envio(conn, 60)  # lido quando ainda estava em espera
        conn.execute("UPDATE portal_envios SET status = ? WHERE id = 60", (ep.STATUS_CANCELANDO,))
        conn.commit()
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, velho, "retomar", {"modo": "proxima_rota"}, "cliente")
        self.assertEqual(envio(conn, 60)["status"], ep.STATUS_CANCELANDO)

    def test_retomar_duas_vezes_recusa_a_segunda(self):
        conn = montar(status=ep.STATUS_EM_ESPERA_CLIENTE, modo=None)
        velho = envio(conn, 60)
        ep.aplicar_acao(conn, velho, "retomar", {"modo": "proxima_rota"}, "cliente")
        with self.assertRaises(ep.ErroEnvio):
            ep.aplicar_acao(conn, velho, "retomar", {"modo": "proxima_rota"}, "cliente")


if __name__ == "__main__":
    unittest.main()
