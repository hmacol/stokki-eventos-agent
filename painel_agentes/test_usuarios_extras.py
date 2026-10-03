# -*- coding: utf-8 -*-
"""
Logins nominais do painel (usuarios_extras no config.yaml, 02/10).

    py -3.11 -m unittest painel_agentes.test_usuarios_extras
"""
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# "painel_agentes" e o nome da pasta: o modulo do app precisa ser carregado pelo caminho
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

CFG = {
    "usuario": "u_total", "senha": "s_total",
    "usuarios_extras": [
        {"usuario": "gabriel", "senha": "s_gab", "nivel": "total"},
        {"usuario": "ana", "senha": "s_ana", "nivel": "leitura"},
        {"usuario": "x", "senha": "s_x", "nivel": "admin"},
    ],
}


class TestUsuariosExtras(unittest.TestCase):

    def _nivel(self, u, s, cfg=CFG):
        return painel_agentes._nivel_das_credenciais(u, s, cfg)

    def test_extra_recebe_o_nivel_dele(self):
        self.assertEqual(self._nivel("gabriel", "s_gab"), "total")
        self.assertEqual(self._nivel("ana", "s_ana"), "leitura")

    def test_senha_errada_ou_trocada_nao_entra(self):
        self.assertIsNone(self._nivel("gabriel", "errada"))
        self.assertIsNone(self._nivel("gabriel", "s_ana"))

    def test_nivel_desconhecido_nao_entra(self):
        self.assertIsNone(self._nivel("x", "s_x"))

    def test_par_fixo_continua_valendo(self):
        self.assertEqual(self._nivel("u_total", "s_total"), "total")

    def test_sem_lista_nao_quebra(self):
        self.assertIsNone(self._nivel("gabriel", "s_gab", {"usuario": "a", "senha": "b"}))

    def test_login_grava_o_nome_do_extra_na_sessao(self):
        from unittest import mock
        config = {**painel_agentes._carregar_config(), "painel_agentes": {**painel_agentes._carregar_config()["painel_agentes"], **CFG}}
        with mock.patch.object(painel_agentes, "_carregar_config", return_value=config):
            painel_agentes.app.config["TESTING"] = True
            cliente = painel_agentes.app.test_client()
            r = cliente.post("/login", data={"usuario": "gabriel", "senha": "s_gab"})
            self.assertEqual(r.status_code, 302)
            with cliente.session_transaction() as sess:
                self.assertEqual(sess["nivel_acesso"], "total")
                self.assertEqual(sess["usuario"], "gabriel")


if __name__ == "__main__":
    unittest.main()
