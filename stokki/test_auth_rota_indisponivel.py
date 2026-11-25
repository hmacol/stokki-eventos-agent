# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_auth_rota_indisponivel

24/11: a Stokki tirou do ar .../outbound/table e passou a responder 302
pro /login (ela faz isso com qualquer rota que nao existe). O _executar
lia como sessao expirada e relogava: 891 logins no dia, contra 55 na
vespera. Com a sessao viva, logar de novo nao resolve -- tem que falhar
na hora, dizendo a rota."""
import unittest
from unittest import mock

import requests

from stokki import auth

BASE = "https://freshlog.stokki.com.br/pt-br"
ROTA_MORTA = f"{BASE}/administrator/inventory/outbound/table"
PAINEL = f"{BASE}/administrator"


class _Resp:
    def __init__(self, status_code, url):
        self.status_code, self.url = status_code, url


class _Requests:
    """requests.Session falsa. `viva`: a sessao abre o painel (200) ou e
    mandada pro login (302). `rotas_mortas`: caem no /login sempre, como a
    Stokki faz com rota que nao existe."""
    def __init__(self, viva=True, rotas_mortas=()):
        self.viva, self.rotas_mortas, self.chamadas = viva, set(rotas_mortas), []

    def request(self, metodo, url, **kwargs):
        self.chamadas.append((metodo, url))
        if url in self.rotas_mortas or not self.viva:
            return _Resp(200, f"{BASE}/login")  # o requests seguiu o 302
        return _Resp(200, url)

    def get(self, url, **kwargs):
        self.chamadas.append(("GET", url))
        assert kwargs.get("allow_redirects") is False, "a checagem nao pode seguir o redirect pro login"
        return _Resp(200 if self.viva else 302, url)


def _sessao(falsa):
    """StokkiSession sem fila, sem cookies e sem Playwright. O login falso
    conta as chamadas e deixa a sessao viva, como um login de verdade."""
    sess = object.__new__(auth.StokkiSession)
    sess._session = falsa
    sess._conta = "principal"
    sess.logins = 0

    def logar():
        sess.logins += 1
        falsa.viva = True
    sess._fazer_login_playwright = logar
    return sess


class TestRotaIndisponivel(unittest.TestCase):
    def test_rota_morta_com_sessao_viva_falha_sem_logar(self):
        sess = _sessao(_Requests(viva=True, rotas_mortas=[ROTA_MORTA]))
        with self.assertRaises(auth.RotaIndisponivelError) as erro:
            sess.get(ROTA_MORTA)
        self.assertEqual(sess.logins, 0)
        self.assertIn("/pt-br/administrator/inventory/outbound/table", str(erro.exception))

    def test_erro_de_rota_continua_sendo_pego_por_quem_trata_sessao_expirada(self):
        self.assertTrue(issubclass(auth.RotaIndisponivelError, auth.SessaoExpiradaError))

    def test_sessao_expirada_e_rota_morta_loga_uma_vez_so(self):
        sess = _sessao(_Requests(viva=False, rotas_mortas=[ROTA_MORTA]))
        with self.assertRaises(auth.RotaIndisponivelError):
            sess.get(ROTA_MORTA)
        self.assertEqual(sess.logins, 1)

    def test_sessao_expirada_em_rota_boa_renova_e_responde(self):
        falsa = _Requests(viva=False)
        sess = _sessao(falsa)
        resp = sess.get(PAINEL + "/inventory/outbound/show/1")
        self.assertEqual(resp.url, PAINEL + "/inventory/outbound/show/1")
        self.assertEqual(sess.logins, 1)

    def test_checagem_que_falha_na_rede_cai_no_login_de_antes(self):
        falsa = _Requests(viva=True, rotas_mortas=[ROTA_MORTA])
        falsa.get = mock.Mock(side_effect=requests.ConnectionError("rede"))
        sess = _sessao(falsa)
        with self.assertRaises(auth.SessaoExpiradaError) as erro:
            sess.get(ROTA_MORTA)
        self.assertNotIsInstance(erro.exception, auth.RotaIndisponivelError)
        self.assertEqual(sess.logins, auth.MAX_TENTATIVAS_RENOVACAO)


if __name__ == "__main__":
    unittest.main()
