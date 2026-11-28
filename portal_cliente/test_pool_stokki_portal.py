# -*- coding: utf-8 -*-
"""
Portal com o pool direto da Stokki (planejamento.fonte_pool = stokki, Task 7): o pedido que
ainda nao tem servico na Vuupt so existe no nucleo (id sintetico -id_pool). O portal lista esse
pedido em "aguardando" e cancela/retoma/reagenda/tira da rota no NUCLEO, sem chamar a Vuupt.
Com a chave desligada tudo igual a hoje. Banco temporario, nada de rede.

    py -3.11 -m unittest portal_cliente.test_pool_stokki_portal
"""
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cancelamento as cm  # noqa: E402
import dados_cliente as dc  # noqa: E402
import envio_pedidos as ep  # noqa: E402
import pedidos_pausados_cliente as ppc  # noqa: E402
import retomada  # noqa: E402
import solicitacoes_execucao as se  # noqa: E402
from nucleo import banco, pedidos  # noqa: E402

STOKKI = {"planejamento": {"fonte_pool": "stokki"}}
HOJE = date(2026, 11, 27)


def _payload(codigo="PS-1", sender_id=100, **extra):
    p = {"title": f"#{codigo} - NF1 / EMB / DEST", "code": f"#{codigo}", "type": "delivery", "dimension_3": 2,
         "sender_id": sender_id, "address": "RUA A 1, Sao Paulo - SP",
         "customer": {"name": "DEST " + codigo, "code": "123", "address": "RUA A 1, Sao Paulo - SP"},
         "scheduled_start": "2026-11-27T07:00:00-03:00", "scheduled_end": "2026-11-27T10:00:00-03:00"}
    p.update(extra)
    return p


class _NucleoTemporario(unittest.TestCase):
    """nucleo_pedidos de verdade (banco.conectar) num arquivo temporario."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(banco, "DB_PATH", Path(tmp.name) / "t.db")
        p.start()
        self.addCleanup(p.stop)
        self.nucleo = banco.conectar()
        self.addCleanup(self.nucleo.close)

    def sem_servico(self, codigo="PS-1", **kw) -> int:
        pedidos.registrar_importacao(_payload(codigo, **kw), None, "nucleo_sem_servico", conn=self.nucleo)
        return pedidos.id_sintetico(pedidos.buscar_pedido(codigo, conn=self.nucleo))


# ── Lista do portal (dados_cliente) ────────────────────────────────────────────

class ListaDoPortal(_NucleoTemporario):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(dc, "nf_por_codigo", return_value={})
        p.start()
        self.addCleanup(p.stop)
        self.vuupt = mock.Mock()
        self.vuupt.listar_servicos.return_value = [
            {"id": 501, "code": "#PS-9", "sender_id": 100, "status": "not_assigned", "address": "Rua V 1",
             "customer": {"name": "COM SERVICO"}}]

    def test_portal_lista_pedido_sem_servico(self):
        sid = self.sem_servico("PS-1")
        self.sem_servico("PS-2", sender_id=999)          # de outro embarcador: nao aparece
        do_dia, futuros = dc._coletar_sem_rota(self.vuupt, 100, HOJE, {}, STOKKI)
        por_codigo = {l["codigo"]: l for l in do_dia}
        self.assertEqual(set(por_codigo), {"#PS-9", "#PS-1"})
        self.assertEqual(futuros, [])
        linha = por_codigo["#PS-1"]
        self.assertEqual(linha["situacao"], "aguardando_saida")
        self.assertEqual(linha["service_id"], sid)
        self.assertLess(sid, 0)
        self.assertEqual(linha["destinatario"], "DEST PS-1")
        self.assertEqual(linha["agendado_para"], "2026-11-27")   # 07:00 local, nao escorrega de dia

    def test_agendado_pra_frente_vai_pros_futuros(self):
        self.sem_servico("PS-3", scheduled_start="2026-12-02T08:00:00-03:00",
                         scheduled_end="2026-12-02T12:00:00-03:00")
        _, futuros = dc._coletar_sem_rota(self.vuupt, 100, HOJE, {}, STOKKI)
        self.assertEqual([(f["codigo"], f["detalhe"]) for f in futuros], [("#PS-3", "Agendado qua 02/12 · 08:00")])

    def test_chave_desligada_igual_a_hoje(self):
        self.sem_servico("PS-1")
        with mock.patch("nucleo.pool.listar_pool", side_effect=AssertionError("nao le o nucleo")):
            do_dia, _ = dc._coletar_sem_rota(self.vuupt, 100, HOJE, {}, {})
            do_dia_sem_config, _ = dc._coletar_sem_rota(self.vuupt, 100, HOJE, {})
        self.assertEqual([l["codigo"] for l in do_dia], ["#PS-9"])
        self.assertEqual([l["codigo"] for l in do_dia_sem_config], ["#PS-9"])

    def test_servico_do_nucleo_pelo_id_sintetico(self):
        sid = self.sem_servico("PS-1")
        s = dc.servico_sem_servico(sid)
        self.assertEqual((s["id"], s["code"], s["sender_id"], s["status"]), (sid, "#PS-1", 100, "not_assigned"))
        self.assertIsNone(dc.servico_sem_servico(-99999))
        self.assertIsNone(dc.servico_sem_servico(501))           # id real: nao e daqui

    def test_buscar_servico_ou_nucleo_nunca_vai_na_vuupt_com_id_sintetico(self):
        sid = self.sem_servico("PS-1")
        with mock.patch.object(dc, "buscar_servico", return_value={"id": 501}) as vuupt:
            self.assertEqual(dc.buscar_servico_ou_nucleo("t", sid, "customer", config=STOKKI)["code"], "#PS-1")
            vuupt.assert_not_called()
            self.assertEqual(dc.buscar_servico_ou_nucleo("t", 501, "customer", config=STOKKI), {"id": 501})
        vuupt.assert_called_once_with("t", 501, "customer")

    def test_chave_desligada_nao_serve_id_sintetico(self):
        """Revisao final M2: sem a chave o id negativo nao existe pro portal (nem le o nucleo)."""
        sid = self.sem_servico("PS-1")
        with mock.patch.object(dc, "buscar_servico") as vuupt, \
                mock.patch.object(dc, "servico_sem_servico", side_effect=AssertionError("nao le o nucleo")):
            self.assertIsNone(dc.buscar_servico_ou_nucleo("t", sid, "customer", config={}))
            self.assertIsNone(dc.buscar_servico_ou_nucleo("t", sid, "customer"))
        vuupt.assert_not_called()

    def test_linha_orfa_nao_e_servida_pelo_id_sintetico(self):
        """Onda 4 (M1): servico renomeado na Vuupt deixou a linha antiga ABERTA sem id; nao e pedido sem servico."""
        from nucleo import sincronizar_servicos_vuupt as sinc
        pedidos.registrar_importacao(_payload("PS-10"), {"id": 5010}, "criado", conn=self.nucleo)
        sinc.sincronizar_servicos([{"id": 5010, "code": "#PS-10, #PS-20", "title": "x", "status": "assigned",
                                    "sender_id": 100, "route_id": 77, "customer": {}}], self.nucleo)
        sid = pedidos.id_sintetico(pedidos.buscar_pedido("PS-10", conn=self.nucleo))
        self.assertIsNone(dc.servico_sem_servico(sid))
        self.assertIsNone(dc.buscar_servico_ou_nucleo("t", sid, "customer", config=STOKKI))

    def test_id_negativo_gigante_nao_estoura(self):
        """Onda 4 (L3): -99999999999999999999 dava OverflowError (500) no SQLite."""
        self.assertIsNone(dc.servico_sem_servico(-99999999999999999999))
        self.assertIsNone(pedidos.codigo_por_id_sintetico(self.nucleo, -99999999999999999999))

    def test_pedido_fechado_nao_e_servido_pelo_id_sintetico(self):
        sid = self.sem_servico("PS-1")
        self.nucleo.execute("UPDATE nucleo_pedidos SET status = 'CANCELADO' WHERE codigo = 'PS-1'")
        self.nucleo.commit()
        self.assertIsNone(dc.servico_sem_servico(sid))
        self.assertIsNone(dc.buscar_servico_ou_nucleo("t", sid, "customer", config=STOKKI))


# ── Worker do cancelamento (cancelamento.py) ───────────────────────────────────

COLUNAS_ENVIO = ("id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, status TEXT, criado_em TEXT, atualizado_em TEXT, "
                 "criado_stokki_em TEXT, emitida_em TEXT, numero_nf TEXT, referencia TEXT, destinatario_doc TEXT, "
                 "destinatario_nome TEXT, destinatario_endereco TEXT, destinatario_bairro TEXT, "
                 "destinatario_municipio TEXT, codigo_pedido TEXT, requer_agendamento INTEGER, "
                 "agendamento_pendente INTEGER, agendamento_data TEXT, agendamento_hora_inicio TEXT, "
                 "agendamento_hora_fim TEXT, agendamento_aplicado_em TEXT, horario_inicio TEXT, horario_fim TEXT, "
                 "data_expedicao TEXT, xml_path TEXT, itens_json TEXT, erro TEXT, bloqueio_motivo TEXT, origem TEXT")


def conn_dados(status=ep.STATUS_CRIADO, sem_servico=True, id_pool=7, nasceu=1, composto=None):
    """dados.db em memoria: portal_envios + nucleo_pedidos (com id_pool) + agendamentos_pedido. `nasceu` =
    nasceu_sem_servico da linha PS-39959 (1 = pedido sem servico legitimo); `composto` = codigo de um servico
    agrupado vivo na Vuupt que contem o pedido (linha orfa do sync)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(f"""
        CREATE TABLE portal_envios ({COLUNAS_ENVIO});
        CREATE TABLE portal_solicitacoes (id INTEGER PRIMARY KEY, envio_id, cnpj_embarcador, tipo, detalhes, status,
                                          solicitado_por, criado_em, concluido_em, resposta);
        CREATE TABLE nucleo_pedidos (codigo TEXT, vuupt_service_id INTEGER, status TEXT, status_provedor TEXT,
                                     vuupt_route_id INTEGER, excluido_em TEXT, id_pool INTEGER,
                                     nasceu_sem_servico INTEGER);
        CREATE TABLE nucleo_rotas (vuupt_route_id INTEGER, status TEXT, status_provedor TEXT, iniciada_em TEXT,
                                   agent_id INTEGER);
        CREATE TABLE agendamentos_pedido (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido TEXT UNIQUE, cnpj_destinatario TEXT,
            nome_destinatario TEXT, cnpj_embarcador TEXT, numero_nf TEXT, email_embarcador TEXT, status TEXT,
            data_agendada TEXT, horario_inicio_agendado TEXT, horario_fim_agendado TEXT, resposta_texto TEXT,
            solicitado_em TEXT, respondido_em TEXT, aplicado_vuupt_em TEXT);
    """)
    conn.execute("INSERT INTO portal_envios (id, cnpj_embarcador, status, criado_em, atualizado_em, numero_nf, "
                 "codigo_pedido, requer_agendamento, agendamento_pendente, destinatario_nome) "
                 "VALUES (50, '111', ?, datetime('now','localtime'), datetime('now','localtime'), '9959', 'PS-39959', "
                 "0, 0, 'DESTINO LTDA')", (status,))
    if sem_servico:
        conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-39959', NULL, 'ABERTO', NULL, NULL, NULL, ?, ?)",
                     (id_pool, nasceu))
    if composto:
        conn.execute("INSERT INTO nucleo_pedidos VALUES (?, 900, 'EM_ROTA', 'assigned', 77, NULL, 8, NULL)", (composto,))
    conn.commit()
    return conn


def envio(conn):
    return dict(conn.execute("SELECT * FROM portal_envios WHERE id = 50").fetchone())


class CancelamentoDoPortal(unittest.TestCase):
    def setUp(self):
        self.vuupt, self.stokki, self.avisos = [], [], []

    def _rodar(self, conn, config):
        ep.aplicar_acao(conn, envio(conn), "cancelar", {"motivo": "cliente desistiu"}, "cliente")

        def cancelar_vuupt(sid):
            self.vuupt.append(sid)
            return {"ok": True, "erro": ""}

        def cancelar_stokki(id_stokki, motivo):
            self.stokki.append(id_stokki)
            return {"ok": True, "erro": ""}

        def buscar_vuupt(codigo):
            raise AssertionError("nao pergunta a Vuupt: o nucleo tem a linha")

        return cm.processar_cancelamentos(conn, config, cancelar_vuupt, cancelar_stokki,
                                          lambda e, m, x: self.avisos.append(m), buscar_vuupt=buscar_vuupt)

    def test_cancelamento_portal_sem_servico_local(self):
        conn = conn_dados()
        r = self._rodar(conn, STOKKI)
        self.assertEqual(r, {"cancelados": 1, "operacao": 0, "falhas": 0}, self.avisos)
        # o id sintetico vai pro cancelar de producao (cancelar_servico_completo), que cancela NO NUCLEO
        self.assertEqual(self.vuupt, [-7])
        self.assertEqual(self.stokki, [39959])
        self.assertEqual(envio(conn)["status"], ep.STATUS_CANCELADO)

    def test_chave_desligada_igual_a_hoje(self):
        conn = conn_dados()
        r = self._rodar(conn, {})
        self.assertEqual(r["operacao"], 1)
        self.assertEqual(self.vuupt + self.stokki, [])
        self.assertIn("sem id da Vuupt", self.avisos[0])

    def test_linha_orfa_sem_marca_continua_indo_pra_operacao(self):
        """Onda 4 (M1): o estado real depois do backfill e linha COM id_pool; sem a marca nasceu_sem_servico
        (servico renomeado/apagado, pulado antigo) ela nao e pedido sem servico: vai pra operacao."""
        conn = conn_dados(nasceu=None)
        r = self._rodar(conn, STOKKI)
        self.assertEqual(r["operacao"], 1, self.avisos)
        self.assertEqual(self.vuupt + self.stokki, [])
        self.assertIn("sem id da Vuupt", self.avisos[0])

    def test_orfa_agrupada_em_rota_iniciada_vai_pra_operacao_e_nada_e_cancelado(self):
        """Onda 4 (M1, cenario da revisao): PS-39959 foi agrupado na Vuupt em "PS-39959, PS-40000" (rota iniciada);
        o sync zerou o id da linha antiga. O cancelamento do cliente NAO pode virar cancelamento local + Stokki."""
        conn = conn_dados(nasceu=None, composto="PS-39959, PS-40000")
        r = self._rodar(conn, STOKKI)
        self.assertEqual(r, {"cancelados": 0, "operacao": 1, "falhas": 0}, self.avisos)
        self.assertEqual(self.vuupt + self.stokki, [])
        self.assertEqual(conn.execute("SELECT status FROM nucleo_pedidos WHERE codigo = 'PS-39959'").fetchone()[0],
                         "ABERTO")
        self.assertNotEqual(envio(conn)["status"], ep.STATUS_CANCELADO)

    def test_marcada_mas_agrupada_tambem_vai_pra_operacao(self):
        conn = conn_dados(nasceu=1, composto="PS-39959, PS-40000")
        self.assertEqual(self._rodar(conn, STOKKI)["operacao"], 1)
        self.assertEqual(self.vuupt + self.stokki, [])


class CancelarServicoCompletoComMotivo(unittest.TestCase):
    def test_motivo_do_portal_vai_pro_evento_local(self):
        from roteirizacao import cancelar_servico
        with mock.patch.object(cancelar_servico, "cancelar_sem_servico_local",
                               return_value={"ok": True, "ja_estava": False, "erro": ""}) as local, \
                mock.patch.object(cancelar_servico, "VuuptClient", side_effect=AssertionError("Vuupt")):
            r = cancelar_servico.cancelar_servico_completo("t", -7, motivo_sem_servico="cancelado pelo cliente no portal")
        self.assertTrue(r["ok"])
        self.assertEqual(local.call_args.args[:2], (-7, "cancelado pelo cliente no portal"))


# ── Retomada (retomada.py) ─────────────────────────────────────────────────────

class RetomadaDoPortal(unittest.TestCase):
    def _montar(self, modo="agendamento"):
        conn = conn_dados(status=ep.STATUS_RETOMANDO)
        ep._garantir_colunas(conn, "portal_envios", ep.COLUNAS_RETOMAR)
        conn.execute("UPDATE portal_envios SET retomar_modo=?, retomar_data='2026-12-03', retomar_hora_inicio='09:00', "
                     "retomar_hora_fim='12:00' WHERE id=50", (modo,))
        ppc.garantir_tabela(conn)
        ppc.abrir(conn, "PS-39959", 39959, 50, [], "2026-11-26 09:00:00")
        conn.commit()
        return conn

    def _rodar(self, conn, config, proxima=None):
        self.puts, self.avisos = [], []

        def aplicar(sid, dia, ini, fim):
            self.puts.append((sid, dia, ini, fim))
            return {"ok": True, "erro": ""}
        return retomada.processar_retomadas(conn, config, aplicar, lambda s: proxima,
                                            lambda e, m, x: self.avisos.append(x),
                                            agora=datetime(2026, 11, 27, 10, 0))

    def test_retomada_sem_servico_vai_pelo_id_sintetico(self):
        conn = self._montar()
        self.assertEqual(self._rodar(conn, STOKKI), {"retomados": 1, "falhas": 0}, self.avisos)
        self.assertEqual(self.puts, [(-7, "2026-12-03", "09:00", "12:00")])
        self.assertEqual(envio(conn)["status"], ep.STATUS_CRIADO)
        self.assertTrue(conn.execute("SELECT aplicado_vuupt_em FROM agendamentos_pedido").fetchone()[0])

    def test_proxima_rota_sem_servico(self):
        conn = self._montar(modo="proxima_rota")
        self.assertEqual(self._rodar(conn, STOKKI, proxima=date(2026, 12, 1)), {"retomados": 1, "falhas": 0})
        self.assertEqual(self.puts[0][:2], (-7, "2026-12-01"))

    def test_chave_desligada_igual_a_hoje(self):
        conn = self._montar()
        self.assertEqual(self._rodar(conn, {}), {"retomados": 0, "falhas": 1})
        self.assertEqual(self.puts, [])
        self.assertIn("sem id da Vuupt", self.avisos[0])


# ── Execucao de solicitacao pela operacao (solicitacoes_execucao.py) ───────────

class SolicitacaoDoPortal(unittest.TestCase):
    def setUp(self):
        self.chamadas = []

    def _deps(self, local=True):
        def anota(nome):
            def f(*args):
                self.chamadas.append((nome, *args))
                return {"ok": True, "erro": "", "rota_id": None}
            return f

        def buscar(codigo):
            raise AssertionError("nao pergunta a Vuupt: o nucleo tem a linha")

        return se.Dependencias(buscar_servico=buscar, cancelar_vuupt=anota("cancelar"),
                               cancelar_stokki=anota("cancelar_stokki"), tirar_da_rota_vuupt=anota("tirar"),
                               reagendar_vuupt=anota("reagendar"), abrir_stokki=lambda: True,
                               fechar_stokki=lambda: None, sem_servico_local=local)

    def _solicitacao(self, conn, tipo):
        cur = conn.execute("INSERT INTO portal_solicitacoes (envio_id, cnpj_embarcador, tipo, detalhes, status, "
                           "solicitado_por, criado_em) VALUES (50, '111', ?, 'cliente pediu', 'PENDENTE', 'cliente', "
                           "datetime('now','localtime'))", (tipo,))
        conn.commit()
        return cur.lastrowid

    def test_cancelar_sem_servico_local(self):
        conn = conn_dados()
        r = se.executar(conn, self._solicitacao(conn, "cancelar"), self._deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual([c[:2] for c in self.chamadas], [("cancelar", -7), ("cancelar_stokki", 39959)])

    def test_em_espera_sem_servico_tira_do_rascunho_e_pausa(self):
        conn = conn_dados()
        r = se.executar(conn, self._solicitacao(conn, "em_espera"), self._deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.chamadas, [("tirar", -7)])
        self.assertEqual(envio(conn)["status"], ep.STATUS_EM_ESPERA_CLIENTE)

    def test_reagendar_sem_servico_grava_pelo_id_sintetico(self):
        conn = conn_dados()
        dia = (date.today() + timedelta(days=3)).isoformat()
        conn.execute("UPDATE portal_envios SET agendamento_data=?, agendamento_hora_inicio='08:00', "
                     "agendamento_hora_fim='12:00' WHERE id=50", (dia,))
        conn.commit()
        r = se.executar(conn, self._solicitacao(conn, "reagendar"), self._deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.chamadas, [("tirar", -7), ("reagendar", -7, dia, "08:00", "12:00")])

    def test_chave_desligada_igual_a_hoje(self):
        conn = conn_dados()
        r = se.executar(conn, self._solicitacao(conn, "em_espera"), self._deps(local=False))
        self.assertFalse(r["ok"])
        self.assertIn("sem id da Vuupt", r["mensagem"])
        self.assertEqual(self.chamadas, [])

    def test_dependencias_reais_ligam_pela_chave(self):
        self.assertTrue(se.dependencias_reais(STOKKI).sem_servico_local)
        self.assertFalse(se.dependencias_reais({}).sem_servico_local)


# ── Acoes de producao com id sintetico: nunca chamam a Vuupt ───────────────────

class AcoesReaisSemServico(_NucleoTemporario):
    def _sem_vuupt(self):
        se._caminhos_painel()
        import vuupt_client
        return mock.patch.object(vuupt_client, "VuuptClient", side_effect=AssertionError("chamou a Vuupt"))

    def test_tirar_da_rota_real_sem_servico_so_mexe_no_rascunho(self):
        with self._sem_vuupt():
            from roteirizacao import cancelar_servico
            import rascunhos_rota
            with mock.patch.object(cancelar_servico, "rascunho_do_servico", return_value=(5, "RASCUNHO")), \
                    mock.patch.object(rascunhos_rota, "remover_parada") as remover:
                r = se._tirar_da_rota_real("t", -7, 0, "ana")
        self.assertEqual(r, {"ok": True, "erro": "", "rota_id": None, "rota_cancelada": False})
        remover.assert_called_once_with(5, -7)

    def test_reagendar_servico_vuupt_sem_servico_grava_no_nucleo(self):
        sid = self.sem_servico("PS-1")
        with self._sem_vuupt():
            r = se.reagendar_servico_vuupt("t", sid, "2026-12-01", "08:00", "12:00")
        self.assertEqual(r, {"ok": True, "erro": ""})
        linha = pedidos.buscar_pedido("PS-1", conn=self.nucleo)
        self.assertEqual((linha["agendamento_inicio"], linha["agendamento_fim"]),
                         ("2026-12-01 08:00:00", "2026-12-01 12:00:00"))
        self.assertIn("agendamento_inicio", pedidos.campos_editados(linha["edicao_local_json"]))

    def test_reagendar_sem_servico_fechado_recusa_com_motivo(self):
        sid = self.sem_servico("PS-1")
        pedidos.cancelar_sem_servico("PS-1", "teste", banco.ORIGEM_PAINEL, conn=self.nucleo)
        with self._sem_vuupt():
            r = se.reagendar_servico_vuupt("t", sid, "2026-12-01", "08:00", "12:00")
        self.assertFalse(r["ok"])
        self.assertIn("CANCELADO", r["erro"])

    def test_proxima_data_da_retomada_le_o_nucleo(self):
        import enviar_stokki as wk
        sid = self.sem_servico("PS-1")
        fake = types.ModuleType("vuupt_client")
        fake.VuuptClient = mock.Mock(side_effect=AssertionError("chamou a Vuupt"))
        capt = {}
        with mock.patch.dict(sys.modules, {"vuupt_client": fake}), \
                mock.patch.object(wk.rt, "processar_retomadas", side_effect=lambda c, cfg, a, p, av: capt.update(p=p)):
            conn = conn_dados(status=ep.STATUS_RETOMANDO)
            wk.processar_retomadas_do_ciclo(conn, {})
            with mock.patch("roteirizacao.regioes_dia_fixo.regra_dia_fixo_do_servico", return_value=None) as regra:
                self.assertIsNone(capt["p"]({"codigo": "PS-1", "vuupt_service_id": sid}))
        self.assertEqual(regra.call_args.args[0]["address"], "RUA A 1, Sao Paulo - SP")
        fake.VuuptClient.assert_not_called()


if __name__ == "__main__":
    unittest.main()
