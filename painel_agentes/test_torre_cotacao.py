# -*- coding: utf-8 -*-
"""
Botao "Cotacao" da Torre (Hugo, 16/11): /torre/cotacao manda pro portal com
o passe assinado de quem esta logado, em vez do link direto que caia no
login do cliente (CNPJ + PIN).

    py -3.11 -m unittest test_torre_cotacao   (de dentro de painel_agentes/)
"""
import builtins
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

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
painel_agentes = sys.modules.get(_spec.name)
if painel_agentes is None:
    painel_agentes = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = painel_agentes
    with mock.patch.object(builtins, "open", _open_sem_config):
        _spec.loader.exec_module(painel_agentes)

import acesso_telas  # noqa: E402
from portal_cliente import entrada_equipe  # noqa: E402


class TestTorreCotacao(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(acesso_telas, "DB_PATH", Path(self._tmp.name) / "dados.db")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value={
            "painel_agentes": {"usuario": "u", "senha": "s"},
            "portal_cliente": {"secret_key": "segredo-do-portal", "url_base": "https://exemplo.test/cliente/"}})
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel, usuario="teste"):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = usuario

    def test_sem_login_vai_pro_login_do_painel(self):
        r = self.cliente.get("/torre/cotacao")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_logado_vai_pro_portal_com_o_passe_dele(self):
        self._logar("operador", "oper")
        r = self.cliente.get("/torre/cotacao")
        self.assertEqual(r.status_code, 302)
        destino = urlparse(r.headers["Location"])
        self.assertEqual(f"{destino.scheme}://{destino.netloc}{destino.path}", "https://exemplo.test/cliente/equipe/entrar")
        q = parse_qs(destino.query)
        self.assertEqual(q["proximo"], ["/cotacao"])
        lido = entrada_equipe.ler("segredo-do-portal", q["t"][0])
        self.assertEqual((lido["usuario"], lido["nivel"]), ("oper", "operador"))

    def test_nivel_sem_torre_nao_gera_passe(self):
        self._logar("expedicao")
        self.assertEqual(self.cliente.get("/torre/cotacao").status_code, 403)

    def test_botoes_da_torre_apontam_pra_rota_nova(self):
        self._logar("operador")
        for caminho in ("/torre", "/torre/mobile"):
            html = self.cliente.get(caminho).get_data(as_text=True)
            self.assertIn('id="btn-cotacao" href="/torre/cotacao"', html)
            self.assertNotIn("/cliente/cotacao", html)


if __name__ == "__main__":
    unittest.main()
