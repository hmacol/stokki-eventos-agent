# -*- coding: utf-8 -*-
"""
API do aviso de pendencias: avisos no /api/sidebar/contadores e o
POST /api/avisos/ignorar (spec 2026-10-24-aviso-pendencias-painel, Task 3).

    py -3.11 -m unittest test_aviso_pendencias_tela   (de dentro de painel_agentes/)
"""
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
import avisos_pendencias  # noqa: E402

_MONTAR_REAL = avisos_pendencias.montar
CONFIG_PAINEL ={"usuario": "u_total", "senha": "s_total"}
AVISO = {"chave": "torre", "texto": "2 ocorrências na fila da Torre", "qtd": 2,
         "destino": "/torre", "vermelho": False}
CONTA = {"usuario": "maria", "nome": "Maria", "nivel": "total", "ativo": True, "versao_senha": 0,
         "trocar_senha": False, "telas": None}
ORIGEM = {"Origin": "http://localhost"}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for alvo in (
            mock.patch.object(painel_agentes, "_carregar_config", return_value={"painel_agentes": CONFIG_PAINEL}),
            mock.patch.object(avisos_pendencias, "DB_PATH", Path(self._tmp.name) / "avisos.db"),
            mock.patch.object(painel_agentes.contadores_menu, "contadores", return_value={}),
            mock.patch.object(avisos_pendencias, "montar", return_value=[dict(AVISO)]),
        ):
            alvo.start()
            self.addCleanup(alvo.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel="total", usuario="teste"):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = usuario

    def _como_nominal(self):
        p = mock.patch.object(acesso_telas, "conta", return_value=dict(CONTA))
        p.start()
        self.addCleanup(p.stop)
        self._logar("total", "maria")


class TestAvisosNoContadores(_Base):

    def test_contadores_traz_avisos(self):
        self._logar()
        r = self.cliente.get("/api/sidebar/contadores")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["contadores"], {})
        self.assertEqual([a["chave"] for a in d["avisos"]], ["torre"])
        self.assertEqual(set(d["avisos"][0]), {"chave", "texto", "qtd", "destino", "vermelho"})
        self.assertEqual(d["avisos"][0]["destino"], "/torre")

    def test_batimento_destino_leva_a_aba(self):
        self._logar()
        batimento = {**AVISO, "chave": "batimento"}
        with mock.patch.object(avisos_pendencias, "montar", return_value=[batimento]):
            d = self.cliente.get("/api/sidebar/contadores").get_json()
        self.assertTrue(d["avisos"][0]["destino"].endswith("?aba=fechamento"), d["avisos"][0]["destino"])

    def test_endpoint_inexistente_some_sem_derrubar(self):
        self._logar()
        sem_rota = {**AVISO, "chave": "solicitacoes_portal"}
        item = next(i for i in avisos_pendencias.CATALOGO if i["chave"] == "solicitacoes_portal")
        with mock.patch.dict(item, {"endpoint_destino": "tela_que_nao_existe"}), \
                mock.patch.object(avisos_pendencias, "montar", return_value=[sem_rota, dict(AVISO)]):
            r = self.cliente.get("/api/sidebar/contadores")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([a["chave"] for a in r.get_json()["avisos"]], ["torre"])

    def test_aparelho_sem_cookie_ganha_um(self):
        self._logar()
        r = self.cliente.get("/api/sidebar/contadores")
        cookies = r.headers.getlist("Set-Cookie")
        self.assertTrue(any(c.startswith("aviso_aparelho=") for c in cookies), cookies)
        c = [c for c in cookies if c.startswith("aviso_aparelho=")][0]
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Lax", c)

    def test_login_nominal_nao_ganha_cookie_e_usa_login(self):
        self._como_nominal()
        with mock.patch.object(avisos_pendencias, "filtrar_ignorados", side_effect=lambda c, p, a, n: a) as f:
            r = self.cliente.get("/api/sidebar/contadores")
        self.assertFalse(any(c.startswith("aviso_aparelho=") for c in r.headers.getlist("Set-Cookie")))
        self.assertEqual(f.call_args.args[1], "login:maria")

    def test_aparelho_com_cookie_usa_o_cookie(self):
        self._logar()
        uid = "11111111-2222-4333-8444-555555555555"
        self.cliente.set_cookie("aviso_aparelho", uid)
        with mock.patch.object(avisos_pendencias, "filtrar_ignorados", side_effect=lambda c, p, a, n: a) as f:
            r = self.cliente.get("/api/sidebar/contadores")
        self.assertEqual(f.call_args.args[1], f"aparelho:{uid}")
        self.assertFalse(any(c.startswith("aviso_aparelho=") for c in r.headers.getlist("Set-Cookie")))

    def test_montar_recebe_permissoes_do_login_personalizado(self):
        self._logar("total", "maria")
        conta = {**CONTA, "telas": ["torre", "aprovar_embarcadores"]}
        with mock.patch.object(acesso_telas, "conta", return_value=conta), \
                mock.patch.object(avisos_pendencias, "montar", return_value=[]) as m:
            self.cliente.get("/api/sidebar/contadores")
        args = m.call_args.args
        self.assertEqual(set(args[2]), {"torre", "aprovar_embarcadores"})
        self.assertTrue(args[3])


class TestCadastroEmbarcadorPersonalizado(unittest.TestCase):
    """montar real (nao mockado): login personalizado vira nivel total, entao
    so a permissao aprovar_embarcadores decide o aviso."""

    def _chaves(self, telas, pode):
        with mock.patch.object(avisos_pendencias, "contar", return_value=3):
            return [a["chave"] for a in _MONTAR_REAL("total", {}, telas, pode)]

    def test_sem_permissao_nao_avisa(self):
        self.assertNotIn("cadastros_embarcador", self._chaves({"inicio"}, False))

    def test_com_permissao_avisa(self):
        self.assertIn("cadastros_embarcador", self._chaves({"inicio", "aprovar_embarcadores"}, True))

    def test_total_sem_personalizacao_avisa(self):
        self.assertIn("cadastros_embarcador", self._chaves(None, False))


class TestIgnorar(_Base):

    def _post(self, corpo, headers=ORIGEM):
        return self.cliente.post("/api/avisos/ignorar", json=corpo, headers=headers)

    def test_ignorar_grava_e_some(self):
        self._como_nominal()
        self.assertEqual(len(self.cliente.get("/api/sidebar/contadores").get_json()["avisos"]), 1)
        r = self._post({"chave": "torre", "qtd": 2})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(r.get_json()["ate"])
        self.assertEqual(self.cliente.get("/api/sidebar/contadores").get_json()["avisos"], [])

    def test_ignorar_chave_invalida_400(self):
        self._como_nominal()
        self.assertEqual(self._post({"chave": "nao_existe", "qtd": 1}).status_code, 400)

    def test_ignorar_qtd_invalida_400(self):
        self._como_nominal()
        for qtd in (-1, "2", None, 1.5, True):
            self.assertEqual(self._post({"chave": "torre", "qtd": qtd}).status_code, 400, qtd)

    def test_ignorar_exige_mesma_origem(self):
        self._como_nominal()
        self.assertEqual(self._post({"chave": "torre", "qtd": 2}, headers={}).status_code, 403)
        self.assertEqual(self._post({"chave": "torre", "qtd": 2},
                                    headers={"Origin": "http://outro.com"}).status_code, 403)

    def test_ignorar_liberado_pra_login_personalizado(self):
        self.assertTrue(acesso_telas.caminho_liberado("/api/avisos/ignorar", ["torre"]))


if __name__ == "__main__":
    unittest.main()
