# -*- coding: utf-8 -*-
"""
Testes das travas de acesso do portal do cliente (revisao de seguranca de
15/09, achados 2 e 3): contador de tentativas por escopo/chave usado pelo
"Primeiro acesso" (limite de links por CNPJ e por IP) e pelo login da
equipe (/equipe, 5 erros -> 15 min).

    py -3.11 -m unittest portal_cliente.test_acesso_travas
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import auth_cliente as auth  # noqa: E402


class TravaTentativasTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(auth, "DB_PATH", Path(self._tmp.name) / "dados.db")
        self._patch.start()
        self.conn = auth.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def test_sem_registro_nao_conta(self):
        self.assertEqual(auth.contar_tentativas(self.conn, "link_pin_cnpj", "12345678000195", 60), 0)

    def test_conta_por_escopo_e_chave(self):
        auth.registrar_tentativa(self.conn, "link_pin_cnpj", "12345678000195")
        auth.registrar_tentativa(self.conn, "link_pin_cnpj", "12345678000195")
        auth.registrar_tentativa(self.conn, "link_pin_cnpj", "99999999000199")
        auth.registrar_tentativa(self.conn, "equipe_ip", "12345678000195")
        self.assertEqual(auth.contar_tentativas(self.conn, "link_pin_cnpj", "12345678000195", 60), 2)
        self.assertEqual(auth.contar_tentativas(self.conn, "link_pin_cnpj", "99999999000199", 60), 1)
        self.assertEqual(auth.contar_tentativas(self.conn, "equipe_ip", "12345678000195", 60), 1)

    def test_fora_da_janela_nao_conta(self):
        velho = (datetime.now() - timedelta(minutes=61)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("INSERT INTO portal_tentativas (escopo, chave, criado_em) VALUES (?, ?, ?)",
                          ("link_pin_cnpj", "12345678000195", velho))
        self.conn.commit()
        self.assertEqual(auth.contar_tentativas(self.conn, "link_pin_cnpj", "12345678000195", 60), 0)
        self.assertEqual(auth.contar_tentativas(self.conn, "link_pin_cnpj", "12345678000195", 120), 1)

    def test_registrar_limpa_o_que_passou_de_um_dia(self):
        velho = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("INSERT INTO portal_tentativas (escopo, chave, criado_em) VALUES (?, ?, ?)",
                          ("equipe_ip", "1.2.3.4", velho))
        self.conn.commit()
        auth.registrar_tentativa(self.conn, "equipe_ip", "5.6.7.8")
        total = self.conn.execute("SELECT count(*) FROM portal_tentativas").fetchone()[0]
        self.assertEqual(total, 1)

    def test_limpar_tentativas_zera_so_a_chave(self):
        auth.registrar_tentativa(self.conn, "equipe_ip", "1.2.3.4")
        auth.registrar_tentativa(self.conn, "equipe_ip", "5.6.7.8")
        auth.limpar_tentativas(self.conn, "equipe_ip", "1.2.3.4")
        self.assertEqual(auth.contar_tentativas(self.conn, "equipe_ip", "1.2.3.4", 60), 0)
        self.assertEqual(auth.contar_tentativas(self.conn, "equipe_ip", "5.6.7.8", 60), 1)


class RotasDeAcessoTest(unittest.TestCase):
    """Rotas /primeiro-acesso e /equipe pelo test client do Flask, com o
    banco num temporario e sem mandar e-mail de verdade."""

    CNPJ = "12345678000195"
    CNPJ_SEM_EMAIL = "11111111000111"

    @classmethod
    def setUpClass(cls):
        import app as portal   # le o config.yaml local (precisa de portal_cliente.secret_key)
        cls.portal = portal

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(auth, "DB_PATH", Path(self._tmp.name) / "dados.db"),
            mock.patch.object(self.portal, "_enviar_link_pin", return_value=True),
            mock.patch.object(self.portal, "_nivel_equipe",
                              side_effect=lambda u, s: "total" if (u, s) == ("hugo", "certa") else None),
        ]
        self._patches[0].start()
        self.envio = self._patches[1].start()
        self._patches[2].start()
        conn = auth.conectar()
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                     "apelido TEXT, email TEXT)")
        conn.execute("INSERT INTO interno VALUES (?, 1, 'CLIENTE A', NULL, 'a@cliente.com')", (self.CNPJ,))
        conn.execute("INSERT INTO interno VALUES (?, 2, 'CLIENTE B', NULL, '')", (self.CNPJ_SEM_EMAIL,))
        conn.commit()
        conn.close()
        self.cli = self.portal.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _pedir_link(self, cnpj, ip="10.0.0.1"):
        return self.cli.post("/primeiro-acesso", data={"cnpj": cnpj}, environ_base={"REMOTE_ADDR": ip})

    def test_resposta_igual_pra_cadastrado_desconhecido_e_sem_email(self):
        corpos = [self._pedir_link(c).get_data(as_text=True)
                  for c in (self.CNPJ, "99999999000199", self.CNPJ_SEM_EMAIL)]
        self.assertEqual(corpos[0], corpos[1])
        self.assertEqual(corpos[0], corpos[2])
        self.assertIn("Se esse CNPJ estiver no nosso cadastro", corpos[0])
        self.assertNotIn("@", corpos[0].split("<h1>")[1])   # nem e-mail mascarado
        self.assertEqual(self.envio.call_count, 1)           # so o cadastrado com e-mail recebe

    def test_cnpj_incompleto_pede_correcao_sem_consultar(self):
        r = self._pedir_link("123")
        self.assertIn("14 dígitos", r.get_data(as_text=True))
        self.envio.assert_not_called()

    def test_limite_de_links_por_cnpj(self):
        for i in range(self.portal._MAX_LINKS_POR_CNPJ_HORA + 2):
            r = self._pedir_link(self.CNPJ, ip=f"10.0.1.{i}")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.envio.call_count, self.portal._MAX_LINKS_POR_CNPJ_HORA)

    def test_limite_de_pedidos_por_ip(self):
        conn = auth.conectar()
        for _ in range(self.portal._MAX_PEDIDOS_LINK_POR_IP_HORA):
            auth.registrar_tentativa(conn, "link_pin_ip", "10.0.0.9")
        conn.close()
        self._pedir_link(self.CNPJ, ip="10.0.0.9")
        self.envio.assert_not_called()
        self._pedir_link(self.CNPJ, ip="10.0.0.10")
        self.assertEqual(self.envio.call_count, 1)

    def _login_equipe(self, senha, ip="10.0.2.1"):
        return self.cli.post("/equipe", data={"usuario": "hugo", "senha": senha}, environ_base={"REMOTE_ADDR": ip})

    def test_equipe_bloqueia_depois_de_5_erros_mesmo_com_senha_certa(self):
        for _ in range(auth.MAX_TENTATIVAS):
            self.assertEqual(self._login_equipe("errada").status_code, 200)
        self.assertEqual(self._login_equipe("certa").status_code, 429)
        self.assertEqual(self._login_equipe("certa", ip="10.0.2.2").status_code, 302)   # outro IP segue livre

    def test_equipe_login_certo_zera_o_contador(self):
        for _ in range(auth.MAX_TENTATIVAS - 1):
            self._login_equipe("errada")
        self.assertEqual(self._login_equipe("certa").status_code, 302)
        for _ in range(auth.MAX_TENTATIVAS - 1):
            self.assertEqual(self._login_equipe("errada").status_code, 200)


if __name__ == "__main__":
    unittest.main()
