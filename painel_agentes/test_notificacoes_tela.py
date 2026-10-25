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
import unittest
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
                    "/api/notificacoes/historico"]
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


if __name__ == "__main__":
    unittest.main()
