# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_auth_sessao_ativa"""
import unittest

from stokki.auth import aguardar_saida_do_login

BASE = "https://freshlog.stokki.com.br/pt-br"


class _Pagina:
    """Pagina falsa: a URL muda pro destino do clique; wait_for_url estoura se
    a condicao nao vale na URL atual (como um timeout do Playwright)."""
    def __init__(self, url, destino_clique=None, texto=""):
        self.url, self.destino_clique, self.texto, self.cliques = url, destino_clique, texto, []

    def wait_for_url(self, condicao, timeout=None):
        if not condicao(self.url):
            raise TimeoutError(f"Timeout {timeout}ms exceeded.")

    def click(self, seletor):
        self.cliques.append(seletor)
        self.url = self.destino_clique

    def evaluate(self, _js):
        return self.texto


class TestAguardarSaidaDoLogin(unittest.TestCase):
    def test_login_direto_nao_clica_em_nada(self):
        p = _Pagina(f"{BASE}/administrator")
        aguardar_saida_do_login(p)
        self.assertEqual(p.cliques, [])

    def test_sessao_ativa_encerra_a_outra_e_entra(self):
        p = _Pagina(f"{BASE}/login/active-session", destino_clique=f"{BASE}/administrator",
                    texto="Chrome · Windows\nIP\n2600:1f18::1\nÚltimo acesso\nhoje às 13:23\nAtiva agora")
        with self.assertLogs("stokki.auth", "WARNING") as logs:
            aguardar_saida_do_login(p)
        self.assertEqual(p.cliques, ["form[action$='/login/active-session'] button[type='submit']"])
        self.assertEqual(p.url, f"{BASE}/administrator")
        self.assertIn("IP 2600:1f18::1, último acesso hoje às 13:23", logs.output[0])

    def test_continua_no_login_estoura(self):
        p = _Pagina(f"{BASE}/login")
        with self.assertRaises(TimeoutError):
            aguardar_saida_do_login(p)

    def test_clique_que_volta_pro_login_estoura(self):
        p = _Pagina(f"{BASE}/login/active-session", destino_clique=f"{BASE}/login")
        with self.assertRaises(TimeoutError):
            aguardar_saida_do_login(p)


if __name__ == "__main__":
    unittest.main()
