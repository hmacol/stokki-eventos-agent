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
                    "wms_manifest", "wms_service_worker", "acessos", "api_acessos"}


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

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _gravar(self, usuario, telas):
        conn = acesso_telas.conectar()
        try:
            acesso_telas.salvar(conn, usuario, telas, "u_total", self.cfg_painel)
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
        self.assertIsNone(acesso_telas.telas_do_usuario("u_total", self.cfg_painel))
        conn = acesso_telas.conectar()
        try:
            with self.assertRaises(ValueError):
                acesso_telas.salvar(conn, "u_total", ["index"], "chefe", self.cfg_painel)
        finally:
            conn.close()

    def test_api_salva_e_nao_deixa_mudar_o_proprio(self):
        cliente, _ = self._logar("chefe", "s_chefe")
        hdr = {"Origin": "http://localhost"}
        r = cliente.post("/api/acessos", json={"usuario": "ana", "telas": ["torre", "consulta"]}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(acesso_telas.telas_do_usuario("ana", self.cfg_painel), ["torre", "consulta"])
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


if __name__ == "__main__":
    unittest.main()
