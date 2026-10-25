# -*- coding: utf-8 -*-
"""
Tela /notificacoes do painel (controle dos envios externos, so nivel total).

    py -3.11 -m unittest test_notificacoes_tela   (de dentro de painel_agentes/)
"""
import builtins
import importlib.util
import io
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_CONFIG_FALSO = "painel_agentes:\n  usuario: u\n  senha: s\n  secret_key: chave-de-teste\n"
_open_real = builtins.open


def _open_sem_config(arq, *a, **kw):
    # O worktree nao tem config.yaml (segredo): o painel so precisa dele pra subir.
    if Path(str(arq)).name == "config.yaml" and not Path(str(arq)).exists():
        return io.StringIO(_CONFIG_FALSO)
    return _open_real(arq, *a, **kw)


_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
with mock.patch.object(builtins, "open", _open_sem_config):
    _spec.loader.exec_module(painel_agentes)

import acesso_telas  # noqa: E402
import controle_notificacoes as cn  # noqa: E402
import notificacoes_tela as nt  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402
import transportadoras_cadastro as tc  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}
CNPJ_A = "11111111000111"
CNPJ_B = "22222222000122"
CNPJ_C = "33333333000133"   # sem e-mail: nao recebe


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        for alvo, attr in ((cn, "DB_PATH"), (pn, "DB_PATH"), (tc, "DB_PATH"), (acesso_telas, "DB_PATH")):
            p = mock.patch.object(alvo, attr, self.db)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "_carregar_config",
                              return_value={"painel_agentes": {"usuario": "u", "senha": "s"}})
        p.start()
        self.addCleanup(p.stop)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
        conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?)", [
            (CNPJ_A, "Cliente A Ltda", "Cliente A", "log@a.com; fin@a.com", 1, 101, 9001),
            (CNPJ_B, "Cliente B Ltda", "", "b@b.com", 1, 102, 9002),
            (CNPJ_C, "Cliente C Ltda", "Cliente C", "", 1, 103, 9003),
        ])
        conn.commit()
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel="total"):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def _controle(self):
        return cn.conectar(self.db)


class TestRotas(_Base):
    def test_so_total_abre(self):
        urls_get = ["/notificacoes", "/api/notificacoes/tipo/nfs_em_rota", f"/api/notificacoes/embarcador/{CNPJ_A}",
                    "/api/notificacoes/historico", f"/api/notificacoes/embarcador/{CNPJ_A}/situacao"]
        corpos = {"/api/notificacoes/geral": {"ligado": False},
                  "/api/notificacoes/estado": {"tipo": "nfs_em_rota", "estado": "PILOTO"},
                  "/api/notificacoes/excecao": {"tipo": "nfs_em_rota", "destino": CNPJ_A, "regra": "BLOQUEADO"},
                  "/api/notificacoes/piloto": {"email": "x@y.com", "whatsapp": ""},
                  "/api/notificacoes/desfazer": {"id": 1},
                  f"/api/notificacoes/embarcador/{CNPJ_A}": {"emails": ["a@a.com"]}}
        for nivel in ("operador", "leitura", "logistica", "atendimento"):
            self._logar(nivel)
            for url in urls_get:
                r = self.cliente.get(url)
                self.assertIn(r.status_code, (302, 403), (nivel, url))
            for url, corpo in corpos.items():
                r = self.cliente.post(url, json=corpo, headers=ORIGEM)
                self.assertIn(r.status_code, (302, 403), (nivel, url))
        conn = self._controle()
        try:
            self.assertEqual(cn.historico(conn), [])
        finally:
            conn.close()
        self._logar("total")
        for url in urls_get:
            self.assertEqual(self.cliente.get(url).status_code, 200, url)

    def test_post_sem_origem_recusado(self):
        self._logar("total")
        for url, corpo in (("/api/notificacoes/geral", {"ligado": False}),
                           ("/api/notificacoes/estado", {"tipo": "nfs_em_rota", "estado": "PILOTO"}),
                           ("/api/notificacoes/excecao", {"tipo": "nfs_em_rota", "destino": CNPJ_A, "regra": "BLOQUEADO"}),
                           ("/api/notificacoes/piloto", {"email": "x@y.com", "whatsapp": ""}),
                           ("/api/notificacoes/desfazer", {"id": 1}),
                           (f"/api/notificacoes/embarcador/{CNPJ_A}", {"emails": ["a@a.com"]})):
            self.assertEqual(self.cliente.post(url, json=corpo).status_code, 403, url)
        conn = self._controle()
        try:
            self.assertEqual(cn.historico(conn), [])
        finally:
            conn.close()

    def test_ligar_pede_confirmacao_com_contagem(self):
        self._logar("total")
        r = self.cliente.post("/api/notificacoes/estado", json={"tipo": "nfs_em_rota", "estado": "LIGADO"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 409)
        # A (2 e-mails) e B recebem; C nao tem e-mail
        self.assertEqual(r.get_json()["destinatarios"], 2)
        conn = self._controle()
        try:
            self.assertIsNone(cn.estado_tipo(conn, "nfs_em_rota"))
        finally:
            conn.close()
        r = self.cliente.post("/api/notificacoes/estado",
                              json={"tipo": "nfs_em_rota", "estado": "LIGADO", "confirmado": True}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        conn = self._controle()
        try:
            self.assertEqual(cn.estado_tipo(conn, "nfs_em_rota"), "LIGADO")
        finally:
            conn.close()

    def test_piloto_e_desligado_nao_pedem_confirmacao(self):
        self._logar("total")
        for estado in ("PILOTO", "DESLIGADO"):
            r = self.cliente.post("/api/notificacoes/estado", json={"tipo": "resumo_diario", "estado": estado},
                                  headers=ORIGEM)
            self.assertEqual(r.status_code, 200, estado)
        r = self.cliente.post("/api/notificacoes/estado", json={"tipo": "nao_existe", "estado": "PILOTO"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        r = self.cliente.post("/api/notificacoes/estado", json={"tipo": "resumo_diario", "estado": "TALVEZ"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 400)

    def test_ligar_o_geral_pede_confirmacao(self):
        self._logar("total")
        r = self.cliente.post("/api/notificacoes/geral", json={"ligado": False}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        r = self.cliente.post("/api/notificacoes/geral", json={"ligado": True}, headers=ORIGEM)
        self.assertEqual(r.status_code, 409)
        conn = self._controle()
        try:
            self.assertEqual(cn.estado_geral(conn), "DESLIGADO")
        finally:
            conn.close()
        r = self.cliente.post("/api/notificacoes/geral", json={"ligado": True, "confirmado": True}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        conn = self._controle()
        try:
            self.assertEqual(cn.estado_geral(conn), "LIGADO")
        finally:
            conn.close()

    def test_excecao_e_desfazer_pela_api(self):
        self._logar("total")
        r = self.cliente.post("/api/notificacoes/excecao",
                              json={"tipo": "nfs_em_rota", "destino": "11.111.111/0001-11", "regra": "BLOQUEADO"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        conn = self._controle()
        try:
            self.assertEqual(cn.excecoes(conn, "nfs_em_rota"), {CNPJ_A: "BLOQUEADO"})
        finally:
            conn.close()
        detalhe = self.cliente.get("/api/notificacoes/tipo/nfs_em_rota").get_json()
        self.assertEqual(detalhe["estado"], "config")
        destinos = {d["chave"]: d for d in detalhe["destinos"]}
        self.assertEqual(destinos[CNPJ_A]["excecao"], "BLOQUEADO")
        self.assertIsNone(destinos[CNPJ_B]["excecao"])
        self.assertIs(destinos[CNPJ_B]["preferencia_cliente"], True)
        # o bloqueado sai da contagem de quem receberia
        r = self.cliente.post("/api/notificacoes/estado", json={"tipo": "nfs_em_rota", "estado": "LIGADO"},
                              headers=ORIGEM)
        self.assertEqual(r.get_json()["destinatarios"], 1)
        # desfazer a excecao pelo historico
        hist = self.cliente.get("/api/notificacoes/historico").get_json()
        self.assertEqual(hist[0]["destino"], CNPJ_A)
        r = self.cliente.post("/api/notificacoes/desfazer", json={"id": hist[0]["id"]}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        conn = self._controle()
        try:
            self.assertEqual(cn.excecoes(conn, "nfs_em_rota"), {})
        finally:
            conn.close()
        r = self.cliente.post("/api/notificacoes/desfazer", json={"id": 999}, headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        r = self.cliente.post("/api/notificacoes/excecao",
                              json={"tipo": "nfs_em_rota", "destino": CNPJ_A, "regra": "TALVEZ"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 400)

    def test_piloto_pela_api(self):
        self._logar("total")
        r = self.cliente.post("/api/notificacoes/piloto", json={"email": "novo@x.com", "whatsapp": "(11) 99999-0000"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        conn = self._controle()
        try:
            self.assertEqual(cn.piloto(conn), {"email": "novo@x.com", "whatsapp": "11999990000"})
        finally:
            conn.close()
        self.assertIn("novo@x.com", self.cliente.get("/notificacoes").get_data(as_text=True))

    def test_emails_do_embarcador_sao_os_do_portal(self):
        self._logar("total")
        r = self.cliente.get(f"/api/notificacoes/embarcador/{CNPJ_A}")
        self.assertEqual(r.get_json()["emails_cadastro"], ["log@a.com", "fin@a.com"])
        r = self.cliente.post(f"/api/notificacoes/embarcador/{CNPJ_A}",
                              json={"emails": ["Novo@A.com", "outro@a.com"], "whatsapp": "(11) 99999-0000"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        conn = sqlite3.connect(self.db)
        try:
            pref = pn.ler(conn, CNPJ_A)
            por = conn.execute("SELECT atualizado_por FROM preferencias_notificacao").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(pref["emails"], ["novo@a.com", "outro@a.com"])
        self.assertEqual(pref["whatsapp"], "5511999990000")
        self.assertEqual(por, "painel:teste")
        self.assertEqual(self.cliente.get(f"/api/notificacoes/embarcador/{CNPJ_A}").get_json()["emails"],
                         ["novo@a.com", "outro@a.com"])
        # e-mail invalido e CNPJ inexistente voltam 400 sem gravar
        r = self.cliente.post(f"/api/notificacoes/embarcador/{CNPJ_A}", json={"emails": ["sem-arroba"]}, headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        r = self.cliente.post("/api/notificacoes/embarcador/99999999000199", json={"emails": ["a@a.com"]},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        # so o whatsapp: nao mexe nos e-mails (corpo sem "emails")
        r = self.cliente.post(f"/api/notificacoes/embarcador/{CNPJ_A}", json={"whatsapp": ""}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cliente.get(f"/api/notificacoes/embarcador/{CNPJ_A}").get_json()["emails"],
                         ["novo@a.com", "outro@a.com"])

    def test_desfazer_que_liga_pede_confirmacao_com_contagem(self):
        self._logar("total")
        post = lambda url, corpo: self.cliente.post(url, json=corpo, headers=ORIGEM)
        post("/api/notificacoes/estado", {"tipo": "nfs_em_rota", "estado": "LIGADO", "confirmado": True})
        post("/api/notificacoes/estado", {"tipo": "nfs_em_rota", "estado": "DESLIGADO"})
        ultimo = self.cliente.get("/api/notificacoes/historico").get_json()[0]
        self.assertEqual((ultimo["de"], ultimo["para"]), ("LIGADO", "DESLIGADO"))
        r = post("/api/notificacoes/desfazer", {"id": ultimo["id"]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual((r.get_json()["tipo"], r.get_json()["destinatarios"]), ("nfs_em_rota", 2))
        conn = self._controle()
        try:
            self.assertEqual(cn.estado_tipo(conn, "nfs_em_rota"), "DESLIGADO")
        finally:
            conn.close()
        r = post("/api/notificacoes/desfazer", {"id": ultimo["id"], "confirmado": True})
        self.assertEqual(r.status_code, 200)
        conn = self._controle()
        try:
            self.assertEqual(cn.estado_tipo(conn, "nfs_em_rota"), "LIGADO")
        finally:
            conn.close()

    def test_desfazer_que_liga_o_geral_pede_confirmacao(self):
        self._logar("total")
        post = lambda url, corpo: self.cliente.post(url, json=corpo, headers=ORIGEM)
        post("/api/notificacoes/geral", {"ligado": True, "confirmado": True})
        post("/api/notificacoes/geral", {"ligado": False})
        ultimo = self.cliente.get("/api/notificacoes/historico").get_json()[0]
        r = post("/api/notificacoes/desfazer", {"id": ultimo["id"]})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["geral"])
        self.assertIn("tipos_ligados", r.get_json())
        conn = self._controle()
        try:
            self.assertEqual(cn.estado_geral(conn), "DESLIGADO")
        finally:
            conn.close()
        self.assertEqual(post("/api/notificacoes/desfazer", {"id": ultimo["id"], "confirmado": True}).status_code, 200)
        conn = self._controle()
        try:
            self.assertEqual(cn.estado_geral(conn), "LIGADO")
        finally:
            conn.close()

    def test_desfazer_que_nao_liga_nao_pede_confirmacao(self):
        self._logar("total")
        post = lambda url, corpo: self.cliente.post(url, json=corpo, headers=ORIGEM)
        post("/api/notificacoes/estado", {"tipo": "resumo_diario", "estado": "PILOTO"})
        ultimo = self.cliente.get("/api/notificacoes/historico").get_json()[0]
        self.assertEqual(post("/api/notificacoes/desfazer", {"id": ultimo["id"]}).status_code, 200)
        conn = self._controle()
        try:
            self.assertIsNone(cn.estado_tipo(conn, "resumo_diario"))
        finally:
            conn.close()

    def test_edicao_de_emails_vai_pro_historico_e_nao_tem_desfazer(self):
        self._logar("total")
        post = lambda url, corpo: self.cliente.post(url, json=corpo, headers=ORIGEM)
        r = post(f"/api/notificacoes/embarcador/{CNPJ_A}", {"emails": ["novo@a.com"], "whatsapp": "(11) 99999-0000"})
        self.assertEqual(r.status_code, 200)
        hist = self.cliente.get("/api/notificacoes/historico").get_json()
        linha = hist[0]
        self.assertEqual((linha["alvo"], linha["destino"]), ("_emails", CNPJ_A))
        self.assertIn("novo@a.com", linha["para"])
        self.assertIn("5511999990000", linha["para"])
        self.assertEqual(linha["destino_nome"], "Cliente A")
        self.assertIs(linha["desfazivel"], False)
        # salvar igual nao gera linha nova
        post(f"/api/notificacoes/embarcador/{CNPJ_A}", {"emails": ["novo@a.com"]})
        self.assertEqual(len(self.cliente.get("/api/notificacoes/historico").get_json()), len(hist))
        # e-mail invalido nao grava nem registra
        self.assertEqual(post(f"/api/notificacoes/embarcador/{CNPJ_A}", {"emails": ["x"]}).status_code, 400)
        self.assertEqual(len(self.cliente.get("/api/notificacoes/historico").get_json()), len(hist))
        # nao desfaz: 400 e nada muda (nem vira excecao "_emails")
        self.assertEqual(post("/api/notificacoes/desfazer", {"id": linha["id"]}).status_code, 400)
        self.assertEqual(self.cliente.get(f"/api/notificacoes/embarcador/{CNPJ_A}").get_json()["emails"], ["novo@a.com"])
        conn = self._controle()
        try:
            self.assertEqual(cn.excecoes(conn, "_emails"), {})
        finally:
            conn.close()

    def test_situacao_do_embarcador_por_tipo(self):
        self._logar("total")
        post = lambda url, corpo: self.cliente.post(url, json=corpo, headers=ORIGEM)
        post("/api/notificacoes/estado", {"tipo": "entrega_concluida", "estado": "PILOTO"})
        post("/api/notificacoes/excecao", {"tipo": "entrega_concluida", "destino": CNPJ_B, "regra": "LIBERADO"})
        conn = sqlite3.connect(self.db)
        try:
            pn.salvar(conn, CNPJ_B, [], {"resumo_diario": True, "chamado_sem_resposta": False}, "t")
        finally:
            conn.close()
        r = self.cliente.get(f"/api/notificacoes/embarcador/{CNPJ_B}/situacao")
        self.assertEqual(r.status_code, 200)
        dados = r.get_json()
        self.assertEqual((dados["cnpj"], dados["nome"]), (CNPJ_B, "Cliente B Ltda"))
        linhas = {l["tipo"]: l for l in dados["situacao"]}
        esperados = [t for t, i in cn.TIPOS.items() if i["destino"] == "embarcador"]
        self.assertEqual([l["tipo"] for l in dados["situacao"]], esperados)
        self.assertNotIn("transportadoras", linhas)
        self.assertEqual(linhas["entrega_concluida"]["estado"], "PILOTO")
        self.assertEqual(linhas["entrega_concluida"]["excecao"], "LIBERADO")
        self.assertEqual(linhas["nfs_em_rota"]["estado"], "config")
        self.assertIsNone(linhas["nfs_em_rota"]["excecao"])
        self.assertIs(linhas["resumo_diario"]["preferencia_cliente"], True)
        self.assertIs(linhas["whatsapp_sem_resposta"]["preferencia_cliente"], False)
        self.assertIsNone(linhas["chamados"]["preferencia_cliente"])
        self.assertEqual(linhas["chamados"]["rotulo"], cn.TIPOS["chamados"]["rotulo"])
        self.assertEqual(self.cliente.get("/api/notificacoes/embarcador/99999999000199/situacao").status_code, 404)

    def test_pagina_traz_a_lista_pra_busca_de_embarcador(self):
        self._logar("total")
        html = self.cliente.get("/notificacoes").get_data(as_text=True)
        self.assertIn('id="busca-embarcador"', html)
        self.assertIn("Cliente B Ltda", html)


    def test_pagina_renderiza_lista_e_geral(self):
        self._logar("total")
        html = self.cliente.get("/notificacoes").get_data(as_text=True)
        for tipo, info in cn.TIPOS.items():
            self.assertIn(info["rotulo"], html)
        self.assertIn("segue o config", html)
        self.cliente.post("/api/notificacoes/geral", json={"ligado": False}, headers=ORIGEM)
        html = self.cliente.get("/notificacoes").get_data(as_text=True)
        self.assertIn('data-geral="DESLIGADO"', html)


class TestMontagem(_Base):
    def test_lista_resumo_e_ultimo_envio(self):
        conn = self._controle()
        try:
            cn.definir_estado(conn, "entrega_concluida", "PILOTO", "t")
            cn.definir_excecao(conn, "entrega_concluida", CNPJ_A, "BLOQUEADO", "t")
            cn.definir_excecao(conn, "entrega_concluida", CNPJ_B, "LIBERADO", "t")
            cn.definir_excecao(conn, "entrega_concluida", CNPJ_C, "LIBERADO", "t")
            conn.execute("CREATE TABLE notificacoes_enviadas (tipo TEXT, chave TEXT, enviado_em TEXT, "
                         "PRIMARY KEY(tipo, chave))")
            conn.executemany("INSERT INTO notificacoes_enviadas VALUES (?,?,?)", [
                ("nfs_em_rota", "a", "2026-10-23 07:01:00"), ("nfs_em_rota", "b", "2026-10-24 07:02:00")])
            conn.commit()
            linhas = {l["tipo"]: l for l in nt.montar_lista(conn)}
        finally:
            conn.close()
        self.assertEqual(list(linhas), list(cn.TIPOS))
        ent = linhas["entrega_concluida"]
        self.assertEqual((ent["estado"], ent["resumo_excecoes"]), ("PILOTO", "1 bloqueado · 2 liberados"))
        self.assertEqual(linhas["resumo_diario"]["estado"], "config")
        self.assertEqual(linhas["resumo_diario"]["resumo_excecoes"], "—")
        self.assertEqual(linhas["nfs_em_rota"]["ultimo_envio"], "2026-10-24 07:02:00")
        self.assertEqual(linhas["resumo_diario"]["ultimo_envio"], "—")
        self.assertEqual(linhas["nfs_em_rota"]["quando"], "timer 07h")

    def test_preferencia_do_cliente_por_tipo(self):
        conn = sqlite3.connect(self.db)
        try:
            pn.salvar(conn, CNPJ_B, [], {"entrega_concluida": False, "chamado_sem_resposta": False}, "t",
                      whatsapp="(11) 99999-0000")
        finally:
            conn.close()
        conn = self._controle()
        try:
            ent = {d["chave"]: d for d in nt.detalhe_tipo(conn, "entrega_concluida")["destinos"]}
            zap = {d["chave"]: d for d in nt.detalhe_tipo(conn, "whatsapp_sem_resposta")["destinos"]}
            cham = {d["chave"]: d for d in nt.detalhe_tipo(conn, "chamados")["destinos"]}
            self.assertIs(ent[CNPJ_B]["preferencia_cliente"], False)
            self.assertIs(ent[CNPJ_A]["preferencia_cliente"], True)
            self.assertIs(zap[CNPJ_B]["preferencia_cliente"], False)   # chamado_sem_resposta
            self.assertIsNone(cham[CNPJ_A]["preferencia_cliente"])      # tipo sem coluna
            # B desligou entrega_concluida: nao conta; A conta
            self.assertEqual(nt.contar_destinatarios(conn, "entrega_concluida"), 1)
            self.assertEqual(nt.contar_destinatarios(conn, "chamados"), 2)
            # WhatsApp: so quem tem numero valido e a preferencia ligada (B desligou)
            self.assertEqual(nt.contar_destinatarios(conn, "whatsapp_sem_resposta"), 0)
        finally:
            conn.close()

    def test_transportadoras_nao_enviar_e_somente_leitura(self):
        conn = tc.conectar(self.db)
        try:
            comum = {"tipo": "TERCEIROS", "situacao": "ATIVA", "uf": "SP", "municipio": "Sao Paulo",
                     "bairro": "Centro", "logradouro": "Rua Um"}
            tc.criar(conn, dict(comum, nome="ALFA LOG", cep="01001000", numero="10", email="alfa@x.com"), "TELA", "t")
            tc.criar(conn, dict(comum, nome="BETA LOG", cep="02002000", numero="20", logradouro="Rua Dois",
                                email="NÃO ENVIAR"), "TELA", "t")
            conn.commit()
        finally:
            conn.close()
        conn = self._controle()
        try:
            cn.definir_excecao(conn, "transportadoras", "ALFA LOG", "LIBERADO", "t")
            detalhe = nt.detalhe_tipo(conn, "transportadoras")
            self.assertEqual(nt.contar_destinatarios(conn, "transportadoras"), 1)
        finally:
            conn.close()
        por_nome = {d["nome"]: d for d in detalhe["destinos"]}
        self.assertIs(por_nome["ALFA LOG"]["nao_enviar"], False)
        self.assertEqual(por_nome["ALFA LOG"]["excecao"], "LIBERADO")
        self.assertIs(por_nome["BETA LOG"]["nao_enviar"], True)
        self.assertEqual(por_nome["ALFA LOG"]["chave"], "alfa log")
        # embarcador nao traz o campo nao_enviar
        conn = self._controle()
        try:
            self.assertNotIn("nao_enviar", nt.detalhe_tipo(conn, "nfs_em_rota")["destinos"][0])
        finally:
            conn.close()

    def test_transportadoras_sem_cadastro_nao_estoura(self):
        conn = self._controle()
        try:
            self.assertEqual(nt.detalhe_tipo(conn, "transportadoras")["destinos"], [])
            self.assertEqual(nt.contar_destinatarios(conn, "transportadoras"), 0)
        finally:
            conn.close()


class TestDisparo(_Base):
    """Disparar agora: o subprocesso NUNCA roda de verdade (subprocess.run mockado)."""

    def setUp(self):
        super().setUp()
        self.solta = threading.Event()
        self.chamadas = []

        def falso_run(cmd, **kw):
            self.chamadas.append((cmd, kw))
            self.solta.wait(5)
            return mock.Mock(returncode=0, stdout="\n".join(f"linha {i}" for i in range(50)), stderr="")

        p = mock.patch.object(nt.subprocess, "run", side_effect=falso_run)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.solta.set)

    def _esperar(self, disparo_id, segundos=5):
        fim = time.time() + segundos
        while time.time() < fim:
            conn = self._controle()
            try:
                row = conn.execute("SELECT terminado_em, codigo_saida, saida FROM notificacoes_disparos WHERE id = ?",
                                   (disparo_id,)).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return row
            time.sleep(0.02)
        self.fail("disparo nao terminou")

    def test_montar_comando(self):
        cmd = nt.montar_comando("nfs_em_rota", "teste", None, False)
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("notificar_nfs_em_rota.py"))
        self.assertEqual(cmd[2:], ["--modo-teste"])
        cmd = nt.montar_comando("nfs_em_rota", "um", 42, True)
        self.assertEqual(cmd[2:], ["--sender-id", "42", "--reenviar"])
        self.assertEqual(nt.montar_comando("resumo_diario", "um", 42, False)[2:], ["--sender-id", "42"])
        self.assertEqual(nt.montar_comando("transportadoras", "todos", None, False)[2:], [])
        with self.assertRaises(ValueError):
            nt.montar_comando("insucesso", "teste", None, False)
        with self.assertRaises(ValueError):
            nt.montar_comando("consulta_transportadora", "teste", None, False)
        with self.assertRaises(ValueError):
            nt.montar_comando("entrega_concluida", "um", 42, False)   # so nfs e resumo aceitam "um"
        with self.assertRaises(ValueError):
            nt.montar_comando("nfs_em_rota", "um", None, False)       # "um" pede o sender_id

    def test_ja_recebeu_hoje_ignora_piloto(self):
        hoje = date.today().isoformat()
        conn = self._controle()
        try:
            conn.execute("CREATE TABLE notificacoes_enviadas (tipo TEXT NOT NULL, chave TEXT NOT NULL, "
                         "enviado_em TEXT NOT NULL, PRIMARY KEY (tipo, chave))")
            conn.executemany("INSERT INTO notificacoes_enviadas VALUES (?,?,?)", [
                ("nfs_em_rota", f"42|{hoje}", f"{hoje} 07:02:10"),
                ("nfs_em_rota", f"43|{hoje}|redirecionado", f"{hoje} 07:03:00"),
                ("nfs_em_rota", "44|2020-01-01", "2020-01-01 07:00:00"),
                ("resumo_diario", f"45|{hoje}", f"{hoje} 20:05:00"),
            ])
            conn.commit()
            self.assertEqual(nt.ja_recebeu_hoje(conn, "nfs_em_rota"), {42: "07:02"})
            self.assertEqual(nt.ja_recebeu_hoje(conn, "resumo_diario"), {45: "20:05"})
            self.assertEqual(nt.ja_recebeu_hoje(conn, "transportadoras"), {})
        finally:
            conn.close()

    def test_ja_recebeu_hoje_sem_tabela(self):
        conn = self._controle()
        try:
            self.assertEqual(nt.ja_recebeu_hoje(conn, "nfs_em_rota"), {})
        finally:
            conn.close()

    def test_disparo_grava_resultado(self):
        conn = self._controle()
        try:
            disparo_id = nt.iniciar_disparo(conn, "nfs_em_rota", "teste", None, False, "teste")
        finally:
            conn.close()
        self.solta.set()
        _terminado, codigo, saida = self._esperar(disparo_id)
        self.assertEqual(codigo, 0)
        linhas = saida.splitlines()
        self.assertEqual((len(linhas), linhas[0], linhas[-1]), (40, "linha 10", "linha 49"))   # so as ultimas 40
        cmd, kw = self.chamadas[0]
        self.assertEqual(cmd[2:], ["--modo-teste"])
        self.assertEqual(kw["cwd"], nt._RAIZ)
        self.assertEqual(kw["timeout"], 1800)
        self.assertTrue(kw["capture_output"])

    def test_disparo_com_timeout(self):
        self.solta.set()
        with mock.patch.object(nt.subprocess, "run", side_effect=nt.subprocess.TimeoutExpired("x", 1800)):
            conn = self._controle()
            try:
                disparo_id = nt.iniciar_disparo(conn, "pedidos_em_espera", "teste", None, False, "teste")
            finally:
                conn.close()
            _t, codigo, saida = self._esperar(disparo_id)
        self.assertNotEqual(codigo, 0)
        self.assertIn("tempo", saida.lower())

    def test_disparo_em_andamento_recusa_segundo(self):
        conn = self._controle()
        try:
            primeiro = nt.iniciar_disparo(conn, "nfs_em_rota", "teste", None, False, "teste")
            with self.assertRaises(RuntimeError):
                nt.iniciar_disparo(conn, "nfs_em_rota", "todos", None, False, "teste")
            # outro tipo nao e bloqueado
            outro = nt.iniciar_disparo(conn, "pedidos_em_espera", "teste", None, False, "teste")
        finally:
            conn.close()
        self._logar("total")
        r = self.cliente.post("/api/notificacoes/disparo", json={"tipo": "nfs_em_rota", "alvo": "teste"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 409)
        self.assertIn("erro", r.get_json())
        self.solta.set()
        self._esperar(primeiro)
        self._esperar(outro)
        # terminou: libera
        r = self.cliente.post("/api/notificacoes/disparo", json={"tipo": "nfs_em_rota", "alvo": "teste"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self._esperar(r.get_json()["id"])

    def test_disparo_antigo_sem_termino_nao_bloqueia(self):
        conn = self._controle()
        try:
            nt._garantir_disparos(conn)
            antigo = (datetime.now() - timedelta(minutes=31)).isoformat(timespec="seconds")
            conn.execute("INSERT INTO notificacoes_disparos (tipo, alvo, por, iniciado_em) VALUES (?,?,?,?)",
                         ("nfs_em_rota", "teste", "x", antigo))
            conn.commit()
            self.solta.set()
            disparo_id = nt.iniciar_disparo(conn, "nfs_em_rota", "teste", None, False, "teste")
        finally:
            conn.close()
        self._esperar(disparo_id)

    def test_api_valida_pedido(self):
        self._logar("total")
        for corpo in ({"tipo": "insucesso", "alvo": "teste"}, {"tipo": "nfs_em_rota", "alvo": "ninguem"},
                      {"tipo": "nfs_em_rota", "alvo": "um"}, {"tipo": "entrega_concluida", "alvo": "um", "sender_id": 1},
                      {"tipo": "nfs_em_rota", "alvo": "um", "sender_id": "abc"}, {}):
            r = self.cliente.post("/api/notificacoes/disparo", json=corpo, headers=ORIGEM)
            self.assertEqual(r.status_code, 400, corpo)
        self.assertEqual(self.chamadas, [])
        r = self.cliente.get("/api/notificacoes/disparo/insucesso")
        self.assertEqual(r.status_code, 404)

    def test_api_um_embarcador_e_dados_da_janela(self):
        hoje = date.today().isoformat()
        conn = self._controle()
        try:
            conn.execute("CREATE TABLE notificacoes_enviadas (tipo TEXT NOT NULL, chave TEXT NOT NULL, "
                         "enviado_em TEXT NOT NULL, PRIMARY KEY (tipo, chave))")
            conn.execute("INSERT INTO notificacoes_enviadas VALUES ('nfs_em_rota', ?, ?)",
                         (f"101|{hoje}", f"{hoje} 07:02:10"))
            conn.commit()
        finally:
            conn.close()
        self._logar("total")
        dados = self.cliente.get("/api/notificacoes/disparo/nfs_em_rota").get_json()
        self.assertEqual(dados["alvos"], ["teste", "todos", "um"])
        self.assertEqual(dados["ja_recebeu_hoje"], {"101": "07:02"})
        self.assertEqual({e["sender_id"]: e["nome"] for e in dados["embarcadores"]},
                         {101: "Cliente A", 102: "Cliente B Ltda", 103: "Cliente C"})
        self.assertEqual(dados["ultimos"], [])
        self.assertEqual(self.cliente.get("/api/notificacoes/disparo/entrega_concluida").get_json()["alvos"],
                         ["teste", "todos"])
        r = self.cliente.post("/api/notificacoes/disparo",
                              json={"tipo": "nfs_em_rota", "alvo": "um", "sender_id": 101, "reenviar": True},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.solta.set()
        self._esperar(r.get_json()["id"])
        self.assertEqual(self.chamadas[0][0][2:], ["--sender-id", "101", "--reenviar"])
        ultimo = self.cliente.get("/api/notificacoes/disparo/nfs_em_rota").get_json()["ultimos"][0]
        self.assertEqual((ultimo["alvo"], ultimo["por"], ultimo["codigo_saida"]), ("um", "teste", 0))
        self.assertIn("Cliente A", ultimo["destino"])

    def test_disparo_so_total(self):
        for nivel in ("operador", "leitura", "logistica", "atendimento"):
            self._logar(nivel)
            r = self.cliente.get("/api/notificacoes/disparo/nfs_em_rota")
            self.assertIn(r.status_code, (302, 403), nivel)
            r = self.cliente.post("/api/notificacoes/disparo", json={"tipo": "nfs_em_rota", "alvo": "teste"},
                                  headers=ORIGEM)
            self.assertIn(r.status_code, (302, 403), nivel)
        self.assertEqual(self.chamadas, [])
        self._logar("total")
        # sem Origin: recusado, e nada roda
        self.assertEqual(self.cliente.post("/api/notificacoes/disparo",
                                           json={"tipo": "nfs_em_rota", "alvo": "teste"}).status_code, 403)
        self.assertEqual(self.chamadas, [])
        self.assertEqual(self.cliente.get("/api/notificacoes/disparo/nfs_em_rota").status_code, 200)


if __name__ == "__main__":
    unittest.main()
