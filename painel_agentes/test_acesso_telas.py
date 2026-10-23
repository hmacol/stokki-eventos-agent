# -*- coding: utf-8 -*-
"""
Acesso personalizado por usuario (21/10, acesso_telas.py).

    py -3.11 -m unittest test_acesso_telas   (de dentro de painel_agentes/)
"""
import re
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

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)
import acesso_telas  # noqa: E402

# Rotas sem login de painel (ou so do total de verdade) -- fora do catalogo de proposito.
FORA_DO_CATALOGO = {"static", "login", "logout", "api_romaneios_pendentes", "api_romaneio_pdf",
                    "wms_manifest", "wms_service_worker", "acessos", "api_acessos", "api_acessos_novo",
                    "api_acessos_senha", "api_acessos_ativo", "trocar_senha",
                    "galpao_3d", "api_galpao_3d_estado"}


def _caminho_exemplo(regra: str) -> str:
    regra = re.sub(r"<int:[^>]+>", "1", regra)
    regra = re.sub(r"<path:[^>]+>", "a/b.pdf", regra)
    return re.sub(r"<[^>]+>", "x", regra)


class TestCatalogo(unittest.TestCase):

    def test_toda_rota_do_painel_cai_em_alguma_tela(self):
        soltas = [r.rule for r in painel_agentes.app.url_map.iter_rules()
                  if r.endpoint not in FORA_DO_CATALOGO
                  and not acesso_telas.caminho_liberado(_caminho_exemplo(r.rule), acesso_telas.CHAVES)]
        self.assertEqual(soltas, [], "rota nova precisa entrar numa tela de acesso_telas.TELAS")

    def test_chave_de_cada_tela_e_uma_pagina(self):
        with painel_agentes.app.test_request_context():
            for chave in acesso_telas.PAGINAS:
                painel_agentes.url_for(chave)

    def test_inicio_nao_aprova_embarcador_sem_a_permissao(self):
        self.assertFalse(acesso_telas.caminho_liberado("/api/cadastro-embarcador/123/aprovar", ["inicio"]))
        self.assertTrue(acesso_telas.caminho_liberado("/api/cadastro-embarcador/123/aprovar", ["aprovar_embarcadores"]))

    def test_galpao_nao_abre_estoque_nem_expedicao_abre_consulta(self):
        self.assertFalse(acesso_telas.caminho_liberado("/wms/estoque", ["wms"]))
        self.assertTrue(acesso_telas.caminho_liberado("/wms/etiquetas.pdf", ["wms"]))
        self.assertFalse(acesso_telas.caminho_liberado("/expedicao/consulta", ["expedicao"]))
        self.assertTrue(acesso_telas.caminho_liberado("/api/wms/produtos/7/saldo", ["wms_estoque"]))
        self.assertFalse(acesso_telas.caminho_liberado("/api/wms/movimentos", ["wms_estoque"]))


class TestAcessoPersonalizado(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = painel_agentes._carregar_config()
        self.cfg_painel = {**base["painel_agentes"], "usuario": "u_total", "senha": "s_total", "usuarios_extras": [
            {"usuario": "ana", "senha": "s_ana", "nivel": "leitura"},
            {"usuario": "chefe", "senha": "s_chefe", "nivel": "total"}]}
        cfg = {**base, "painel_agentes": self.cfg_painel}
        self._patches = [mock.patch.object(painel_agentes, "_carregar_config", return_value=cfg),
                         mock.patch.object(acesso_telas, "DB_PATH", Path(self._tmp.name) / "t.db")]
        for p in self._patches:
            p.start()
        painel_agentes.app.config["TESTING"] = True
        painel_agentes._login_falhas.clear()
        conn = acesso_telas.conectar()
        try:
            acesso_telas.importar_do_config(conn, self.cfg_painel)
        finally:
            conn.close()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _gravar(self, usuario, telas):
        conn = acesso_telas.conectar()
        try:
            acesso_telas.salvar(conn, usuario, telas, "u_total")
        finally:
            conn.close()

    def _logar(self, usuario, senha):
        cliente = painel_agentes.app.test_client()
        return cliente, cliente.post("/login", data={"usuario": usuario, "senha": senha})

    def test_menu_e_rotas_seguem_a_lista(self):
        self._gravar("ana", ["index", "historico_tratativas"])
        cliente, r = self._logar("ana", "s_ana")
        self.assertEqual(r.headers["Location"], "/historico-tratativas")   # primeira tela na ordem do menu
        r = cliente.get("/")                                      # tela so do total: liberada completa
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        nav = html[html.index('aria-label="Telas do painel"'):]
        nav = nav[:nav.index("</nav>")]
        self.assertEqual(set(re.findall(r'<span class="rotulo">([^<]+)</span>', nav)), {"Agentes", "Tratativas"})
        self.assertIn("personalizado", html)
        for url in ("/inicio", "/torre", "/acessos", "/historico"):
            self.assertEqual(cliente.get(url).status_code, 403, url)

    def test_mudanca_vale_sem_novo_login(self):
        self._gravar("ana", ["index"])
        cliente, _ = self._logar("ana", "s_ana")
        self.assertEqual(cliente.get("/").status_code, 200)
        self._gravar("ana", None)                                 # volta ao nivel leitura do config
        self.assertEqual(cliente.get("/").status_code, 403)

    def test_inicio_esconde_cadastros_sem_a_permissao(self):
        dados = {"pendencias": [], "operacao": None, "cadastros_embarcador": [{"cnpj": "1"}]}
        with mock.patch.object(painel_agentes.pagina_inicial, "montar_dados", side_effect=lambda *a, **k: dict(dados)):
            self._gravar("ana", ["inicio"])
            cliente, _ = self._logar("ana", "s_ana")
            self.assertNotIn("cadastros_embarcador", cliente.get("/api/inicio/dados").get_json())
            self._gravar("ana", ["inicio", "aprovar_embarcadores"])
            self.assertIn("cadastros_embarcador", cliente.get("/api/inicio/dados").get_json())

    def test_so_permissao_sem_tela_nao_entra(self):
        self._gravar("ana", ["aprovar_embarcadores"])
        _, r = self._logar("ana", "s_ana")
        self.assertIn("nenhuma tela liberada", r.get_data(as_text=True))

    def test_lista_vazia_nao_entra(self):
        self._gravar("ana", [])
        _, r = self._logar("ana", "s_ana")
        self.assertEqual(r.status_code, 200)
        self.assertIn("nenhuma tela liberada", r.get_data(as_text=True))

    def test_par_fixo_nunca_e_personalizado(self):
        self.assertIsNone(acesso_telas.telas_do_usuario("u_total"))
        conn = acesso_telas.conectar()
        try:
            with self.assertRaises(ValueError):
                acesso_telas.salvar(conn, "u_total", ["index"], "chefe")
        finally:
            conn.close()

    def test_api_salva_e_nao_deixa_mudar_o_proprio(self):
        cliente, _ = self._logar("chefe", "s_chefe")
        hdr = {"Origin": "http://localhost"}
        r = cliente.post("/api/acessos", json={"usuario": "ana", "telas": ["torre", "consulta"]}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(acesso_telas.telas_do_usuario("ana"), ["torre", "consulta"])
        r = cliente.post("/api/acessos", json={"usuario": "chefe", "telas": ["torre"]}, headers=hdr)
        self.assertEqual(r.status_code, 400)
        r = cliente.post("/api/acessos", json={"usuario": "ana", "telas": ["nao_existe"]}, headers=hdr)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(cliente.get("/acessos").status_code, 200)

    def test_personalizado_nao_abre_a_tela_de_acessos(self):
        self._gravar("chefe", ["index"])
        cliente, _ = self._logar("chefe", "s_chefe")
        self.assertEqual(cliente.get("/acessos").status_code, 403)
        r = cliente.post("/api/acessos", json={"usuario": "ana", "telas": None}, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 403)
        r = cliente.post("/api/acessos/novo", json={"usuario": "zé", "nivel": "total", "senha": "123456"},
                         headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 403)


class TestCadastroPelaTela(TestAcessoPersonalizado):
    """Cadastro, nome, nivel e senha pela /acessos (23/10)."""

    def _chefe(self):
        cliente, _ = self._logar("chefe", "s_chefe")
        return cliente, {"Origin": "http://localhost"}

    def test_cadastro_com_senha_provisoria_obriga_troca(self):
        cliente, hdr = self._chefe()
        r = cliente.post("/api/acessos/novo", json={"usuario": " Maria.Souza ", "nome": "Maria Souza",
                                                     "nivel": "operador", "senha": "prov123"}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["usuario"], "maria.souza")
        maria, r = self._logar("maria.souza", "prov123")
        self.assertTrue(r.headers["Location"].endswith("/trocar-senha"))
        self.assertTrue(maria.get("/torre").headers["Location"].endswith("/trocar-senha"))
        self.assertEqual(maria.get("/api/torre/dados").status_code, 403)
        r = maria.post("/trocar-senha", data={"nova": "prov123", "repetir": "prov123"}, headers=hdr)
        self.assertIn("diferente", r.get_data(as_text=True))
        r = maria.post("/trocar-senha", data={"nova": "minha99", "repetir": "minha99"}, headers=hdr)
        self.assertEqual(r.status_code, 302)
        self.assertIn("Maria Souza", maria.get("/torre").get_data(as_text=True))
        self.assertIsNone(painel_agentes._nivel_das_credenciais("maria.souza", "prov123", self.cfg_painel))
        self.assertEqual(painel_agentes._nivel_das_credenciais("maria.souza", "minha99", self.cfg_painel), "operador")

    def test_cadastro_recusa_repetido_par_fixo_e_senha_curta(self):
        cliente, hdr = self._chefe()
        for corpo in ({"usuario": "ana", "nivel": "leitura", "senha": "123456"},
                      {"usuario": "u_total", "nivel": "leitura", "senha": "123456"},
                      {"usuario": "novo", "nivel": "leitura", "senha": "123"},
                      {"usuario": "novo", "nivel": "admin", "senha": "123456"},
                      {"usuario": "com espaco", "nivel": "leitura", "senha": "123456"}):
            self.assertEqual(cliente.post("/api/acessos/novo", json=corpo, headers=hdr).status_code, 400, corpo)

    def test_mudar_nivel_e_nome_vale_sem_novo_login(self):
        ana, _ = self._logar("ana", "s_ana")
        self.assertEqual(ana.get("/").status_code, 403)
        cliente, hdr = self._chefe()
        r = cliente.post("/api/acessos", json={"usuario": "ana", "telas": None, "nome": "Ana Lima", "nivel": "total"},
                         headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = ana.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ana Lima", r.get_data(as_text=True))
        r = cliente.post("/api/acessos", json={"usuario": "ana", "telas": None, "nome": "", "nivel": "admin"},
                         headers=hdr)
        self.assertEqual(r.status_code, 400)

    def test_redefinir_senha_de_outro_e_nao_a_propria(self):
        cliente, hdr = self._chefe()
        self.assertEqual(cliente.post("/api/acessos/senha", json={"usuario": "ana", "senha": "nova123"},
                                      headers=hdr).status_code, 200)
        _, r = self._logar("ana", "nova123")
        self.assertTrue(r.headers["Location"].endswith("/trocar-senha"))
        self.assertEqual(cliente.post("/api/acessos/senha", json={"usuario": "chefe", "senha": "nova123"},
                                      headers=hdr).status_code, 400)

    def test_sessao_aberta_antes_de_redefinir_cai_e_nao_escolhe_a_senha(self):
        ana_velha, _ = self._logar("ana", "s_ana")
        self.assertEqual(ana_velha.get("/historico-tratativas").status_code, 200)
        cliente, hdr = self._chefe()
        cliente.post("/api/acessos/senha", json={"usuario": "ana", "senha": "nova123"}, headers=hdr)
        r = ana_velha.post("/trocar-senha", data={"nova": "invasor1", "repetir": "invasor1"}, headers=hdr)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertTrue(ana_velha.get("/historico-tratativas").headers["Location"].startswith("/login"))
        self.assertIsNone(painel_agentes._nivel_das_credenciais("ana", "invasor1", self.cfg_painel))
        ana, _ = self._logar("ana", "nova123")
        self.assertEqual(ana.post("/trocar-senha", data={"nova": "minha99", "repetir": "minha99"},
                                  headers=hdr).status_code, 302)
        self.assertEqual(ana.get("/historico-tratativas").status_code, 200)

    def test_troca_com_versao_velha_nao_grava(self):
        cliente, hdr = self._chefe()
        cliente.post("/api/acessos/senha", json={"usuario": "ana", "senha": "nova123"}, headers=hdr)
        conn = acesso_telas.conectar()
        try:
            self.assertIsNone(acesso_telas.trocar_senha(conn, "ana", "invasor1", 0))
            self.assertEqual(acesso_telas.trocar_senha(conn, "ana", "minha99", 1), 2)
        finally:
            conn.close()

    def test_desativar_derruba_sessao_e_reativar_devolve(self):
        ana, _ = self._logar("ana", "s_ana")
        self.assertEqual(ana.get("/historico-tratativas").status_code, 200)
        cliente, hdr = self._chefe()
        r = cliente.post("/api/acessos/ativo", json={"usuario": "ana", "ativo": False}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(ana.get("/historico-tratativas").headers["Location"].startswith("/login"))
        _, r = self._logar("ana", "s_ana")
        self.assertIn("incorretos", r.get_data(as_text=True))
        self.assertIn("desativado", cliente.get("/acessos").get_data(as_text=True))
        self.assertEqual(cliente.post("/api/acessos/novo", json={"usuario": "ana", "nivel": "leitura", "senha": "123456"},
                                      headers=hdr).status_code, 400)
        cliente.post("/api/acessos/ativo", json={"usuario": "ana", "ativo": True}, headers=hdr)
        _, r = self._logar("ana", "s_ana")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(cliente.post("/api/acessos/ativo", json={"usuario": "chefe", "ativo": False},
                                      headers=hdr).status_code, 400)

    def test_login_bloqueia_depois_de_5_erros_so_aquele_login(self):
        for _ in range(5):
            _, r = self._logar("ana", "errada")
            self.assertEqual(r.status_code, 200)
        _, r = self._logar("ana", "s_ana")                         # ate a senha certa espera
        self.assertEqual(r.status_code, 429)
        self.assertIn("15 minutos", r.get_data(as_text=True))
        _, r = self._logar("chefe", "s_chefe")                     # outro login do mesmo IP entra
        self.assertEqual(r.status_code, 302)
        painel_agentes._login_falhas.clear()
        _, r = self._logar("ana", "s_ana")
        self.assertEqual(r.status_code, 302)

    def test_tabela_antiga_ganha_versao_senha(self):
        conn = acesso_telas.conectar()
        conn.execute("DROP TABLE painel_usuarios")
        conn.execute("CREATE TABLE painel_usuarios (usuario TEXT PRIMARY KEY, nome TEXT NOT NULL DEFAULT '', "
                     "senha_hash TEXT NOT NULL, nivel TEXT NOT NULL, trocar_senha INTEGER NOT NULL DEFAULT 0, "
                     "criado_por TEXT, criado_em TEXT NOT NULL, atualizado_por TEXT, atualizado_em TEXT)")
        conn.commit()
        conn.close()
        conn = acesso_telas.conectar()
        try:
            acesso_telas.importar_do_config(conn, self.cfg_painel)
            self.assertEqual(acesso_telas.conta("ana", conn)["versao_senha"], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
