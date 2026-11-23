# -*- coding: utf-8 -*-
"""Pagina publica de ciencia do dedicado (/ciencia/<token>): o embarcador
confirma a ciencia do carro dedicado ou reagenda pro dia de visita.

App Flask minimo (o app.py do portal le o config.yaml ao importar), com
link_ciencia.registrar e dependencias falsas: nada sai pra Vuupt.

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_link_ciencia_pagina
"""
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from flask import Flask

_AQUI = Path(__file__).resolve().parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import alterar_entrega as ae  # noqa: E402
import ciencia_dedicado as cd  # noqa: E402
import controle_notificacoes as cn  # noqa: E402
import link_ciencia  # noqa: E402
import pedidos_dedicados  # noqa: E402
import regioes_dia_fixo as rdf  # noqa: E402
from regras import complexidade_entrega as ce  # noqa: E402

SEGREDO = "segredo-de-teste"
CNPJ_REAL = "11222333000181"
CNPJ_TESTE = "00000000000191"
SANTOS = "Rua A, 10, Centro, Santos - SP, 11010-000, Brasil"   # Baixada Santista: terca
IP = "200.1.2.3"
# alteracao de entrega desligada no portal: a pagina tem chave propria
CONFIG = {"portal_cliente": {"alteracao_entrega": {"ativo": False}}, "vuupt_api": {"token": "x"}}


def _servico(sid=1001, code="#PS-1", sender_id=101, **extra):
    base = {"id": sid, "code": code, "sender_id": sender_id, "status": "not_assigned", "address": SANTOS,
            "scheduled_start": "2026-11-21 12:00:00", "customer_id": 900,
            "customer": {"code": "12.345.678/0001-90", "name": "Padaria Boa"}}
    base.update(extra)
    return base


class FakeDeps:
    def __init__(self, falha_em=None):
        self.chamadas, self.falha_em = [], falha_em

    def _r(self, nome, args):
        self.chamadas.append((nome, *args))
        return {"ok": False, "erro": "x"} if self.falha_em == nome else {"ok": True, "erro": ""}

    def fabrica(self, config, por):
        self.por = por
        return ae.Dependencias(
            tirar_da_rota=lambda sid: self._r("tirar", (sid,)),
            gravar_endereco=lambda sid, texto, dados: self._r("endereco", (sid, texto)),
            reagendar=lambda sid, dia, ini, fim: self._r("reagendar", (sid, dia, ini, fim)),
            atualizar_horario_contato=lambda cid, ini, fim: self._r("contato", (cid, ini, fim)),
            geocodificar=lambda texto: None,
            avisar_equipe=lambda info: self.chamadas.append(("avisar", info["codigo"])))


class PaginaCiencia(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "dados.db"
        patcher = mock.patch.object(ce, "_DB_PATH", Path(tmp.name) / "ajustes.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = cd.conectar(self.db)
        self.addCleanup(self.conn.close)
        self.conn.executescript(
            "CREATE TABLE agendamentos_pedido (pedido TEXT PRIMARY KEY, cnpj_destinatario, nome_destinatario, "
            "cnpj_embarcador, numero_nf, email_embarcador, status, data_agendada, horario_inicio_agendado, "
            "horario_fim_agendado, resposta_texto, solicitado_em, respondido_em, aplicado_vuupt_em);"
            "CREATE TABLE portal_destinatarios (cnpj_embarcador TEXT NOT NULL, documento TEXT NOT NULL, nome TEXT, "
            "horario_inicio TEXT, horario_fim TEXT, requer_agendamento INTEGER NOT NULL DEFAULT 0, "
            "atualizado_em TEXT NOT NULL, PRIMARY KEY (cnpj_embarcador, documento));")
        self.deps = FakeDeps()
        self.servicos = {}
        self.buscas = []
        self.chat = []
        self.alertas = []
        self.alerta_ok = True
        self.regra = rdf.regra_dia_fixo_do_servico(_servico())

    # ── montagem ──────────────────────────────────────────────────────────────

    def _cliente(self):
        app = Flask("teste_ciencia", root_path=str(_AQUI), template_folder="templates")
        app.config["TESTING"] = True

        @app.route("/logo")
        def regioes_logo():
            return ""

        def buscar(sid):
            self.buscas.append(sid)
            return self.servicos.get(sid)

        link_ciencia.registrar(app, config=CONFIG, secret=SEGREDO, db_path=self.db, deps_fabrica=self.deps.fabrica,
                               buscar_servico=buscar,
                               chat_mensagem=lambda chamado, texto: self.chat.append((chamado, texto)) or True,
                               alerta_equipe=lambda texto: self.alertas.append(texto) or self.alerta_ok)
        return app.test_client()

    def _ciencia(self, sid=1001, code="#PS-1", sender_id=101, cnpj=CNPJ_REAL, data=date(2026, 11, 21)):
        servico = _servico(sid, code, sender_id)
        self.servicos[sid] = servico
        conn_ded = pedidos_dedicados.conectar(self.db)
        pedidos_dedicados.marcar(conn_ded, [{"codigo_pedido": pedidos_dedicados.codigos_do_servico(servico)[0],
                                             "service_id": sid, "sender_id": sender_id}], 350.0,
                                 pedidos_dedicados.POR_FORA_DIA_FIXO)
        conn_ded.close()
        return cd.abrir(self.conn, {"servico": servico, "regra": self.regra, "data": data, "valor": 350.0,
                                    "valor_pendente": False}, cnpj, datetime.now() - timedelta(hours=1))

    def _tela_ligada(self):
        conn = cn.conectar(self.db)
        cn.definir_estado(conn, "ciencia_dedicado", "LIGADO", "teste")
        conn.close()

    def _url(self, sender_id=101, acao="", ids=None, origem="email"):
        """Sem `ids`, o link cobre todas as ciencias ja abertas (a de outro sender cai na checagem do sender)."""
        if ids is None:
            ids = [r[0] for r in self.conn.execute("SELECT id FROM ciencias_dedicado")] or [999999]
        return f"/ciencia/{link_ciencia.gerar(sender_id, SEGREDO, ids, origem)}" + (f"/{acao}" if acao else "")

    def _post(self, tc, acao, form, sender_id=101, ids=None, origem="email", headers=None):
        return tc.post(self._url(sender_id, acao, ids, origem), data=form, headers=headers or {},
                       environ_base={"REMOTE_ADDR": IP})

    def _dedicado_ativo(self, sid=1001) -> bool:
        conn = pedidos_dedicados.conectar(self.db)
        try:
            return conn.execute("SELECT 1 FROM pedidos_dedicados WHERE service_id = ? AND removido_em IS NULL",
                                (sid,)).fetchone() is not None
        finally:
            conn.close()

    def _datas(self):
        return link_ciencia.datas_de_visita(self.regra, date.today())

    def _nfs(self):
        self.conn.executescript(
            "CREATE TABLE documentos_pedido (id_pedido, tipo_doc, status, numero_nf);"
            "CREATE TABLE documentos_processados (codigo_pedido, tipo, status, numero_nf);"
            "INSERT INTO documentos_pedido VALUES ('PS-1','nf','casado','777'),"
            "('PS-2','nf','casado','888'),('PS-2','nf','casado','889');")
        self.conn.commit()

    def test_pagina_mostra_ps_e_nf(self):
        self._nfs()
        self._ciencia()                                   # PS-1: uma NF
        respondido = self._ciencia(sid=1002, code="#PS-2")  # duas NFs
        self._ciencia(sid=1003, code="#PS-4")             # sem NF
        cd.responder(self.conn, respondido, cd.CIENTE, "Joana Lima", IP, datetime.now())
        corpo = self._cliente().get(self._url(101)).get_data(as_text=True)
        self.assertIn("PS-1 · NF 777", corpo)
        self.assertIn("PS-4 · NF —", corpo)
        self.assertIn("PS-2 · NF 888, 889", corpo)

    # ── token e lista ─────────────────────────────────────────────────────────

    def test_token_invalido_ou_expirado_da_404_sem_listar_pedido(self):
        self._ciencia()
        tc = self._cliente()
        with mock.patch("time.time", return_value=time.time() - 31 * 86400):
            vencido = link_ciencia.gerar(101, SEGREDO, [1], "email")
        for token in ("lixo", vencido, link_ciencia.gerar(101, "outro-segredo", [1], "email")):
            r = tc.get(f"/ciencia/{token}")
            self.assertEqual(r.status_code, 404, token)
            corpo = r.get_data(as_text=True)
            self.assertIn("Link inválido ou vencido", corpo)
            self.assertNotIn("PS-1", corpo)
            self.assertNotIn("Padaria", corpo)
        r = tc.post(f"/ciencia/{vencido}/ciente", data={"ciencia_id": "1", "nome": "Maria"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(cd.buscar(self.conn, 1)["situacao"], cd.PENDENTE)

    def test_lista_so_os_pedidos_do_sender_do_token(self):
        meu = self._ciencia()
        respondido = self._ciencia(sid=1002, code="#PS-2")
        cd.responder(self.conn, respondido, cd.CIENTE, "Joana Lima", IP, datetime.now())
        self._ciencia(sid=2001, code="#PS-9", sender_id=202, cnpj="99888777000166")
        r = self._cliente().get(self._url(101))
        self.assertEqual(r.status_code, 200)
        corpo = r.get_data(as_text=True)
        self.assertIn("PS-1", corpo)
        self.assertIn("Padaria Boa", corpo)
        self.assertIn("Santos", corpo)
        self.assertIn("21/11/2026", corpo)
        self.assertIn("R$ 350,00", corpo)
        self.assertIn(f'value="{meu}"', corpo)
        # respondido aparece embaixo, com quem respondeu e sem formulario
        self.assertIn("PS-2", corpo)
        self.assertIn("Joana Lima", corpo)
        self.assertNotIn(f'value="{respondido}"', corpo)
        self.assertNotIn("PS-9", corpo)
        # as datas oferecidas sao as de visita da regiao
        for d in self._datas():
            self.assertIn(f'value="{d.isoformat()}"', corpo)

    # ── estou ciente ──────────────────────────────────────────────────────────

    def test_ciente_grava_nome_hora_ip_e_para_o_lembrete(self):
        self._tela_ligada()
        cid = self._ciencia()
        cd.registrar_envio(self.conn, cid, "chat", "enviado", destino="77")
        tc = self._cliente()
        r = self._post(tc, "ciente", {"ciencia_id": str(cid), "nome": "  Maria   Souza "})
        self.assertEqual(r.status_code, 303)
        linha = cd.buscar(self.conn, cid)
        self.assertEqual((linha["situacao"], linha["respondido_por"], linha["respondido_ip"]),
                         (cd.CIENTE, "Maria Souza", IP))
        self.assertTrue(linha["respondido_em"].startswith(date.today().isoformat()))
        self.assertEqual(cd.pendentes_vencidos(self.conn, datetime.now() + timedelta(days=30)), [])
        self.assertTrue(self._dedicado_ativo())   # ciencia nao mexe no dedicado
        self.assertEqual(self.deps.chamadas, [])
        # mensagem do sistema na conversa do chat em que o pedido foi avisado
        self.assertEqual(len(self.chat), 1)
        self.assertEqual(self.chat[0][0], 77)
        self.assertIn("Maria Souza confirmou", self.chat[0][1])
        corpo = tc.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("ciência registrada", corpo)
        self.assertNotIn("Modo piloto", corpo)

    def test_ciente_sem_nome_pede_o_nome_e_nao_grava(self):
        self._tela_ligada()
        cid = self._ciencia()
        tc = self._cliente()
        for nome in ("", "   ", "M", "x" * 81):
            r = self._post(tc, "ciente", {"ciencia_id": str(cid), "nome": nome})
            self.assertEqual(r.status_code, 400, repr(nome))
            self.assertIn("Informe seu nome", r.get_data(as_text=True))
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.PENDENTE)
        self.assertEqual(self.chat, [])

    def test_ciencia_de_outro_sender_e_recusada(self):
        self._tela_ligada()
        alheia = self._ciencia(sid=2001, code="#PS-9", sender_id=202)
        tc = self._cliente()
        for acao, form in (("ciente", {"ciencia_id": str(alheia), "nome": "Maria"}),
                           ("reagendar", {"ciencia_id": str(alheia), "nome": "Maria",
                                          "data": self._datas()[0].isoformat()}),
                           ("ciente", {"ciencia_id": "abc", "nome": "Maria"})):
            r = self._post(tc, acao, form, sender_id=101)
            self.assertEqual(r.status_code, 303)
            corpo = tc.get(r.headers["Location"]).get_data(as_text=True)
            self.assertIn("Este pedido já foi respondido", corpo)
            self.assertNotIn("PS-9", corpo)
        self.assertEqual(cd.buscar(self.conn, alheia)["situacao"], cd.PENDENTE)
        self.assertTrue(self._dedicado_ativo(2001))
        self.assertEqual((self.deps.chamadas, self.buscas), ([], []))

    def test_segunda_resposta_nao_sobrescreve_a_primeira(self):
        self._tela_ligada()
        cid = self._ciencia()
        tc = self._cliente()
        self._post(tc, "ciente", {"ciencia_id": str(cid), "nome": "Maria"})
        primeira = cd.buscar(self.conn, cid)
        for acao, form in (("ciente", {"ciencia_id": str(cid), "nome": "Pedro"}),
                           ("reagendar", {"ciencia_id": str(cid), "nome": "Pedro",
                                          "data": self._datas()[0].isoformat()})):
            r = self._post(tc, acao, form)
            self.assertIn("Este pedido já foi respondido", tc.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual(cd.buscar(self.conn, cid), primeira)
        self.assertEqual(self.deps.chamadas, [])

    # ── reagendar ─────────────────────────────────────────────────────────────

    def test_datas_de_visita_devolve_4_a_partir_de_amanha_pulando_feriado(self):
        # Santos visita as tercas; 09/02/2027 e terca de Carnaval (feriado): a visita passa pra quarta 10/02
        self.assertEqual(link_ciencia.datas_de_visita(self.regra, date(2027, 2, 1)),
                         [date(2027, 2, 2), date(2027, 2, 10), date(2027, 2, 16), date(2027, 2, 23)])
        # hoje ja e dia de visita: comeca no proximo
        self.assertEqual(link_ciencia.datas_de_visita(self.regra, date(2027, 2, 2), n=2),
                         [date(2027, 2, 10), date(2027, 2, 16)])

    def test_reagendar_aplica_data_remove_dedicado_e_grava_reagendado(self):
        self._tela_ligada()
        cid = self._ciencia()
        cd.registrar_envio(self.conn, cid, "chat", "enviado", destino="77")
        data = self._datas()[1]
        tc = self._cliente()
        r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria Souza", "data": data.isoformat()})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.buscas, [1001])
        self.assertIn(("reagendar", 1001, data.isoformat(), "08:00", "18:00"), self.deps.chamadas)
        self.assertIn("Maria Souza", self.deps.por)
        linha = cd.buscar(self.conn, cid)
        self.assertEqual((linha["situacao"], linha["data_nova"], linha["respondido_por"], linha["respondido_ip"]),
                         (cd.REAGENDADO, data.isoformat(), "Maria Souza", IP))
        self.assertFalse(self._dedicado_ativo())
        removido = self.conn.execute("SELECT removido_por FROM pedidos_dedicados WHERE service_id = 1001").fetchone()
        self.assertEqual(removido[0], pedidos_dedicados.POR_CIENCIA_REAGENDADO)
        # data nova e do cliente (agendamentos_pedido), num dia de visita: a roteirizacao nao marca de novo
        agendada = self.conn.execute("SELECT data_agendada FROM agendamentos_pedido WHERE pedido = 'PS-1'").fetchone()
        self.assertEqual(agendada[0], data.strftime("%d/%m/%Y"))
        self.assertEqual(len(self.chat), 1)
        self.assertIn(data.strftime("%d/%m/%Y"), self.chat[0][1])
        self.assertEqual(self.alertas, [])
        corpo = tc.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn(f"reagendada para {data.strftime('%d/%m/%Y')}", corpo)

    def test_reagendar_com_data_fora_da_lista_e_recusado(self):
        self._tela_ligada()
        cid = self._ciencia()
        tc = self._cliente()
        datas = self._datas()
        fora = [datas[0] - timedelta(days=1), datas[-1] + timedelta(days=7), date.today()]
        for valor in [d.isoformat() for d in fora] + ["", "21/11/2026"]:
            r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria", "data": valor})
            self.assertEqual(r.status_code, 400, valor)
            self.assertIn("Escolha uma das datas", r.get_data(as_text=True))
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.PENDENTE)
        self.assertTrue(self._dedicado_ativo())
        self.assertEqual((self.deps.chamadas, self.buscas), ([], []))

    def test_reagendar_recusado_pelo_alterar_entrega_mantem_pendente_e_mostra_a_mensagem(self):
        self._tela_ligada()
        cid = self._ciencia()
        tc = self._cliente()
        data = self._datas()[0].isoformat()
        # avaliacao bloqueada: pedido ja saiu pra entrega
        self.servicos[1001]["status"] = "on_route"
        r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria", "data": data})
        self.assertEqual(r.status_code, 409)
        corpo = r.get_data(as_text=True)
        self.assertIn("Esse pedido já está em entrega ou foi entregue.", corpo)
        self.assertIn("chat do portal", corpo)
        self.assertEqual(self.deps.chamadas, [])
        # aplicar sem ok: a Vuupt recusou a data
        self.servicos[1001]["status"] = "not_assigned"
        self.deps.falha_em = "reagendar"
        with self.assertLogs("portal_alterar_entrega", level="ERROR"):
            r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria", "data": data})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Não conseguimos concluir a alteração", r.get_data(as_text=True))
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.PENDENTE)
        self.assertTrue(self._dedicado_ativo())
        self.assertEqual(self.chat, [])

    # ── piloto e chave ────────────────────────────────────────────────────────

    def test_piloto_simula_e_nao_grava_nem_chama_vuupt(self):
        cid = self._ciencia()   # sem linha na tela de Notificacoes = PILOTO
        cd.registrar_envio(self.conn, cid, "chat", "enviado", destino="77")
        tc = self._cliente()
        self.assertIn("Modo piloto: nada é gravado", tc.get(self._url()).get_data(as_text=True))
        r = self._post(tc, "ciente", {"ciencia_id": str(cid), "nome": "Maria"})
        self.assertEqual(r.status_code, 303)
        corpo = tc.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Modo piloto: nada é gravado", corpo)
        self.assertIn("ciência simulada", corpo)
        data = self._datas()[0]
        r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria", "data": data.isoformat()})
        self.assertEqual(r.status_code, 303)
        self.assertIn(f"reagendamento simulado para {data.strftime('%d/%m/%Y')}",
                      tc.get(r.headers["Location"]).get_data(as_text=True))
        # piloto valida tudo: nome e data continuam exigidos
        self.assertEqual(self._post(tc, "ciente", {"ciencia_id": str(cid), "nome": ""}).status_code, 400)
        self.assertEqual(self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria",
                                                      "data": "2020-01-01"}).status_code, 400)
        linha = cd.buscar(self.conn, cid)
        self.assertEqual((linha["situacao"], linha["respondido_em"]), (cd.PENDENTE, None))
        self.assertTrue(self._dedicado_ativo())
        self.assertEqual((self.deps.chamadas, self.buscas, self.chat), ([], [], []))

    def test_piloto_com_cliente_teste_age_de_verdade(self):
        cid = self._ciencia(cnpj=CNPJ_TESTE)   # sem linha na tela: piloto, mas o cliente teste age
        outro = self._ciencia(sid=1002, code="#PS-2", cnpj=CNPJ_TESTE)
        tc = self._cliente()
        self.assertNotIn("Modo piloto", tc.get(self._url()).get_data(as_text=True))
        self._post(tc, "ciente", {"ciencia_id": str(cid), "nome": "Hugo"})
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.CIENTE)
        data = self._datas()[0]
        self._post(tc, "reagendar", {"ciencia_id": str(outro), "nome": "Hugo", "data": data.isoformat()})
        self.assertEqual(cd.buscar(self.conn, outro)["situacao"], cd.REAGENDADO)
        self.assertIn(("reagendar", 1002, data.isoformat(), "08:00", "18:00"), self.deps.chamadas)
        self.assertFalse(self._dedicado_ativo(1002))

    def _tela(self, *comandos):
        """Zera a tela de Notificacoes e aplica os comandos (funcoes que recebem a conexao)."""
        conn = cn.conectar(self.db)
        for tabela in ("notificacoes_controle", "notificacoes_controle_excecao", "notificacoes_controle_param"):
            conn.execute(f"DELETE FROM {tabela}")
        conn.commit()
        for comando in comandos:
            comando(conn)
        conn.commit()
        conn.close()

    def _assert_simulou(self, tc, cid, caso):
        self.assertIn("Modo piloto: nada é gravado", tc.get(self._url()).get_data(as_text=True), caso)
        r = self._post(tc, "ciente", {"ciencia_id": str(cid), "nome": "Hugo"})
        self.assertIn("ciência simulada", tc.get(r.headers["Location"]).get_data(as_text=True), caso)
        r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Hugo", "data": self._datas()[0].isoformat()})
        self.assertIn("reagendamento simulado", tc.get(r.headers["Location"]).get_data(as_text=True), caso)
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.PENDENTE, caso)
        self.assertTrue(self._dedicado_ativo(), caso)
        self.assertEqual((self.deps.chamadas, self.buscas, self.chat), ([], [], []), caso)

    def test_tela_que_nao_manda_enviar_simula(self):
        # so ENVIAR age de verdade: NAO (geral desligado, tipo desligado, embarcador bloqueado,
        # piloto sem e-mail de destino) simula como o piloto
        cid = self._ciencia()
        tc = self._cliente()
        casos = {
            "geral desligado": [lambda c: cn.definir_estado(c, "ciencia_dedicado", "LIGADO", "t"),
                                lambda c: cn.definir_geral(c, False, "t")],
            "tipo desligado": [lambda c: cn.definir_estado(c, "ciencia_dedicado", "DESLIGADO", "t")],
            "embarcador bloqueado": [lambda c: cn.definir_estado(c, "ciencia_dedicado", "LIGADO", "t"),
                                     lambda c: cn.definir_excecao(c, "ciencia_dedicado", CNPJ_REAL, "BLOQUEADO", "t")],
            "piloto sem e-mail": [lambda c: cn.definir_estado(c, "ciencia_dedicado", "PILOTO", "t"),
                                  lambda c: c.execute("INSERT INTO notificacoes_controle_param VALUES ('piloto_email', '')")],
        }
        for caso, comandos in casos.items():
            self._tela(*comandos)
            self.assertEqual(cn.decidir("ciencia_dedicado", CNPJ_REAL, fallback_acao="PILOTO", db_path=self.db).acao,
                             "NAO", caso)
            self._assert_simulou(tc, cid, caso)

    def test_erro_ao_decidir_simula(self):
        self._tela_ligada()
        cid = self._ciencia()
        tc = self._cliente()
        import notificar_ciencia_dedicado as ncd
        with mock.patch.object(ncd, "decisao_email", side_effect=RuntimeError("tela quebrada")), \
                self.assertLogs("portal_cliente.link_ciencia", level="WARNING"):
            self._assert_simulou(tc, cid, "erro")

    def _reagendar_com_falha(self, alvo, metodo):
        self._tela_ligada()
        cid = self._ciencia()
        alertas = self.alertas
        tc = self._cliente()
        data = self._datas()[0]
        with mock.patch.object(alvo, metodo, side_effect=RuntimeError("banco travado")), \
                self.assertLogs("portal_cliente.link_ciencia", level="ERROR"):
            r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria", "data": data.isoformat()})
        self.assertEqual(r.status_code, 303)
        self.assertIn("A data foi alterada. Nossa equipe vai concluir o ajuste do pedido.",
                      tc.get(r.headers["Location"]).get_data(as_text=True))
        self.assertIn(("reagendar", 1001, data.isoformat(), "08:00", "18:00"), self.deps.chamadas)
        self.assertEqual(len(alertas), 1)
        self.assertIn("PS-1", alertas[0])
        self.assertIn(CNPJ_REAL, alertas[0])
        self.assertIn(data.strftime("%d/%m/%Y"), alertas[0])
        return cid, alertas[0]

    def test_reagendar_aplicado_com_falha_ao_tirar_o_dedicado_alerta_a_equipe(self):
        cid, alerta = self._reagendar_com_falha(pedidos_dedicados, "remover")
        self.assertIn("data alterada, mas o dedicado não foi removido", alerta)
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.REAGENDADO)   # a resposta e gravada antes
        self.assertTrue(self._dedicado_ativo())

    def test_reagendar_aplicado_com_falha_ao_gravar_a_resposta_alerta_e_ainda_tira_o_dedicado(self):
        cid, alerta = self._reagendar_com_falha(cd, "responder")
        self.assertIn("data alterada, mas a resposta não foi gravada", alerta)
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.PENDENTE)
        self.assertFalse(self._dedicado_ativo())

    def test_alerta_da_equipe_que_nao_saiu_fica_no_log_de_erro(self):
        self._tela_ligada()
        cid = self._ciencia()
        self.alerta_ok = False
        tc = self._cliente()
        data = self._datas()[0]
        with mock.patch.object(pedidos_dedicados, "remover", side_effect=RuntimeError("banco travado")),                 self.assertLogs("portal_cliente.link_ciencia", level="ERROR") as logs:
            r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria", "data": data.isoformat()})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(self.alertas), 1)
        linhas = [l for l in logs.output if "ERROR" in l and "alerta" in l]
        self.assertTrue(linhas, logs.output)
        for trecho in ("PS-1", data.strftime("%d/%m/%Y"), "o dedicado não foi removido"):
            self.assertIn(trecho, linhas[-1])

    def test_encerrado_pelo_sistema_nao_aparece_pro_cliente(self):
        encerrada = self._ciencia()
        cd.encerrar(self.conn, encerrada, "data em dia de visita", datetime.now())
        respondida = self._ciencia(sid=1002, code="#PS-2")
        cd.responder(self.conn, respondida, cd.CIENTE, "Joana Lima", IP, datetime.now())
        corpo = self._cliente().get(self._url(101)).get_data(as_text=True)
        self.assertIn("PS-2", corpo)
        self.assertIn("Joana Lima", corpo)
        self.assertNotIn("PS-1", corpo)
        self.assertNotIn("data em dia de visita", corpo)
        self.assertNotIn("Encerrado", corpo)

    def test_reagendar_com_ciencia_encerrada_no_meio_alerta_a_equipe_e_tira_o_dedicado(self):
        # a Vuupt ja mudou a data e a ciencia nao estava mais PENDENTE: o registro diverge, a equipe confere
        self._tela_ligada()
        cid = self._ciencia()
        tc = self._cliente()
        data = self._datas()[0]
        with mock.patch.object(cd, "responder", return_value=False), \
                self.assertLogs("portal_cliente.link_ciencia", level="ERROR") as logs:
            r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria", "data": data.isoformat()})
        self.assertTrue(any("ERROR" in linha and "PS-1" in linha for linha in logs.output), logs.output)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Nossa equipe vai concluir o ajuste", tc.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual(len(self.alertas), 1)
        self.assertIn("a resposta não foi gravada", self.alertas[0])
        self.assertFalse(self._dedicado_ativo())

    # ── revisao de seguranca 19/11 ────────────────────────────────────────────

    def test_link_so_alcanca_as_ciencias_que_cobre(self):
        self._tela_ligada()
        antiga = self._ciencia()
        nova = self._ciencia(sid=1002, code="#PS-2")   # aberta depois do link sair
        tc = self._cliente()
        corpo = tc.get(self._url(ids=[antiga])).get_data(as_text=True)
        self.assertIn("PS-1", corpo)
        self.assertNotIn("PS-2", corpo)
        for acao, form in (("ciente", {"ciencia_id": str(nova), "nome": "Maria"}),
                           ("reagendar", {"ciencia_id": str(nova), "nome": "Maria",
                                          "data": self._datas()[0].isoformat()})):
            r = self._post(tc, acao, form, ids=[antiga])
            self.assertIn("r=ja_respondido", r.headers["Location"])
        self.assertEqual(cd.buscar(self.conn, nova)["situacao"], cd.PENDENTE)
        self.assertEqual((self.deps.chamadas, self.buscas), ([], []))
        # o aviso pos-POST tambem nao fala de ciencia fora do link
        self.assertNotIn("PS-2", tc.get(self._url(ids=[antiga]) + f"?r=ciente&c={nova}").get_data(as_text=True))

    def test_resposta_grava_de_qual_link_veio_e_o_navegador(self):
        self._tela_ligada()
        ciente, reagendado = self._ciencia(), self._ciencia(sid=1002, code="#PS-2")
        tc = self._cliente()
        self._post(tc, "ciente", {"ciencia_id": str(ciente), "nome": "Maria"}, origem="whatsapp",
                   headers={"User-Agent": "Celular X" + "y" * 400})
        self._post(tc, "reagendar", {"ciencia_id": str(reagendado), "nome": "Joana",
                                     "data": self._datas()[0].isoformat()}, origem="painel:joana")
        linha = cd.buscar(self.conn, ciente)
        self.assertEqual(linha["respondido_origem"], "whatsapp")
        self.assertTrue(linha["respondido_navegador"].startswith("Celular X"))
        self.assertEqual(len(linha["respondido_navegador"]), 300)
        linha = cd.buscar(self.conn, reagendado)
        self.assertEqual((linha["situacao"], linha["respondido_origem"]), (cd.REAGENDADO, "painel:joana"))

    def test_resposta_em_andamento_trava_a_outra(self):
        # reagendar esperando a Vuupt: o ciente (ou outra aba) nao grava por cima nem chama a Vuupt de novo
        self._tela_ligada()
        cid = self._ciencia()
        self.assertTrue(cd.reivindicar_resposta(self.conn, cid, datetime.now()))
        tc = self._cliente()
        for acao, form in (("ciente", {"ciencia_id": str(cid), "nome": "Pedro"}),
                           ("reagendar", {"ciencia_id": str(cid), "nome": "Pedro",
                                          "data": self._datas()[0].isoformat()})):
            r = self._post(tc, acao, form)
            self.assertIn("r=em_andamento", r.headers["Location"])
            self.assertIn("está sendo registrada agora", tc.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.PENDENTE)
        self.assertEqual((self.deps.chamadas, self.buscas), ([], []))

    def test_link_de_copia_de_piloto_simula_mesmo_com_o_embarcador_em_envio_real(self):
        self._tela_ligada()   # embarcador ja em ENVIAR: o link do cliente age, a copia que foi pro piloto nao
        cid = self._ciencia()
        tc = self._cliente()
        for origem in ("email-piloto", "whatsapp-piloto"):
            self.assertIn("Modo piloto", tc.get(self._url(origem=origem)).get_data(as_text=True))
            r = self._post(tc, "ciente", {"ciencia_id": str(cid), "nome": "Hugo"}, origem=origem)
            self.assertIn("ciência simulada", tc.get(r.headers["Location"]).get_data(as_text=True))
            r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Hugo",
                                             "data": self._datas()[0].isoformat()}, origem=origem)
            self.assertIn("simulado", tc.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.PENDENTE)
        self.assertEqual((self.deps.chamadas, self.buscas), ([], []))

    def test_cliente_teste_age_de_verdade_tambem_pelo_link_de_piloto(self):
        cid = self._ciencia(cnpj="00.000.000/0001-91")
        self._post(self._cliente(), "ciente", {"ciencia_id": str(cid), "nome": "Hugo"}, origem="email-piloto")
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.CIENTE)

    def test_reagendar_recusado_solta_a_trava(self):
        self._tela_ligada()
        cid = self._ciencia()
        self.servicos[1001]["status"] = "on_route"
        tc = self._cliente()
        r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria",
                                         "data": self._datas()[0].isoformat()})
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(cd.buscar(self.conn, cid)["processando_desde"])
        self._post(tc, "ciente", {"ciencia_id": str(cid), "nome": "Maria"})
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.CIENTE)

    def test_ids_que_nao_sao_inteiro_positivo_pequeno_nao_tocam_o_banco(self):
        self._tela_ligada()
        cid = self._ciencia()
        tc = self._cliente()
        for valor in ("99999999999999999999999", str(2 ** 31), "-1", "0", "1.0", " 1", "1e3", "abc", ""):
            r = self._post(tc, "ciente", {"ciencia_id": valor, "nome": "Maria"})
            self.assertEqual(r.status_code, 303, valor)
            self.assertIn("r=ja_respondido", r.headers["Location"], valor)
            r = tc.get(self._url() + f"?r=ciente&c={valor}")
            self.assertEqual(r.status_code, 200, valor)
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.PENDENTE)

    def test_pagina_funciona_com_alteracao_entrega_desligada(self):
        self.assertFalse(ae.ativa_para(CONFIG, CNPJ_REAL))
        self._tela_ligada()
        cid = self._ciencia()
        tc = self._cliente()
        self.assertEqual(tc.get(self._url()).status_code, 200)
        data = self._datas()[0]
        r = self._post(tc, "reagendar", {"ciencia_id": str(cid), "nome": "Maria", "data": data.isoformat()})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(cd.buscar(self.conn, cid)["situacao"], cd.REAGENDADO)


if __name__ == "__main__":
    unittest.main()
