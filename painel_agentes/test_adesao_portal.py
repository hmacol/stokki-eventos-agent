# -*- coding: utf-8 -*-
"""
Tela /adesao-portal do painel (quem entrou e quem importa pelo portal, 21/10).

    py -3.11 -m unittest test_adesao_portal      (de dentro de painel_agentes/)
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import adesao_portal  # noqa: E402

# "painel_agentes" e o nome da pasta: o modulo do app precisa ser carregado pelo caminho
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)

AGORA = datetime(2026, 10, 21, 18, 0)


class TestAdesaoPortal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.banco = Path(self._tmp.name) / "dados.db"
        conn = sqlite3.connect(self.banco)
        conn.executescript("""
            CREATE TABLE clientes_portal (cnpj TEXT, nome TEXT, ativo INTEGER, trocar_pin INTEGER, ultimo_login_em TEXT);
            CREATE TABLE portal_envios (cnpj_embarcador TEXT, status TEXT, criado_em TEXT);
            CREATE TABLE portal_grupos (cnpj_login TEXT, cnpj_membro TEXT);
            INSERT INTO clientes_portal VALUES
              ('11111111000111', 'ATIVO', 1, 0, '2026-10-01 10:00:00'),
              ('22222222000122', 'ANTIGO', 1, 0, '2026-09-01 10:00:00'),
              ('33333333000133', 'SO OLHA', 1, 1, '2026-10-03 10:00:00'),
              ('44444444000144', 'SUMIDO', 1, 1, NULL),
              ('55555555000155', 'GRUPO', 1, 0, '2026-10-02 10:00:00'),
              ('66666666000166', 'DESATIVADO', 0, 0, NULL),
              ('00000000000191', 'CLIENTE TESTE', 1, 0, '2026-10-16 10:00:00');
            INSERT INTO portal_envios VALUES
              ('11111111000111', 'CRIADO', '2026-10-20 09:00:00'),
              ('11111111000111', 'DUPLICADO', '2026-10-21 09:00:00'),
              ('22222222000122', 'CRIADO', '2026-09-10 09:00:00'),
              ('77777777000177', 'CRIADO', '2026-10-19 09:00:00'),
              ('00000000000191', 'CRIADO', '2026-10-20 09:00:00');
            INSERT INTO portal_grupos VALUES ('55555555000155', '77777777000177');
        """)
        conn.commit()
        conn.close()

    def _por_nome(self):
        return {l["nome"]: l for l in adesao_portal.listar(self.banco, agora=AGORA)}

    def test_classifica_cada_conta(self):
        linhas = self._por_nome()
        self.assertEqual(linhas["ATIVO"]["situacao"], adesao_portal.IMPORTANDO)
        self.assertEqual(linhas["ATIVO"]["pedidos_total"], 1)  # DUPLICADO nao conta
        self.assertEqual(linhas["ATIVO"]["ultimo_envio"], "2026-10-20 09:00:00")
        self.assertEqual(linhas["ANTIGO"]["situacao"], adesao_portal.PAROU)
        self.assertEqual(linhas["SO OLHA"]["situacao"], adesao_portal.SO_ENTROU)
        self.assertTrue(linhas["SO OLHA"]["pin_pendente"])
        self.assertEqual(linhas["SUMIDO"]["situacao"], adesao_portal.NUNCA_ENTROU)

    def test_grupo_soma_envio_do_membro(self):
        self.assertEqual(self._por_nome()["GRUPO"]["situacao"], adesao_portal.IMPORTANDO)

    def test_fora_teste_e_desativado(self):
        linhas = self._por_nome()
        self.assertNotIn("CLIENTE TESTE", linhas)
        self.assertNotIn("DESATIVADO", linhas)

    def test_ordem_e_resumo(self):
        linhas = adesao_portal.listar(self.banco, agora=AGORA)
        self.assertEqual([l["situacao"] for l in linhas][-1], adesao_portal.NUNCA_ENTROU)
        self.assertEqual([r["qtd"] for r in adesao_portal.resumo(linhas)], [2, 1, 1, 1])

    def _cliente(self, nivel=None):
        p = mock.patch.object(adesao_portal, "DB_PATH", self.banco)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        cliente = painel_agentes.app.test_client()
        if nivel:
            with cliente.session_transaction() as sess:
                sess["nivel_acesso"] = nivel
                sess["usuario"] = "teste"
        return cliente

    def test_tela_abre_pro_atendimento(self):
        resp = self._cliente("atendimento").get("/adesao-portal")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("SUMIDO", html)
        self.assertIn("44.444.444/0001-44", html)
        self.assertIn("Nunca entrou", html)

    def test_expedicao_nao_entra(self):
        self.assertEqual(self._cliente("expedicao").get("/adesao-portal").status_code, 403)

    def test_sem_login_vai_pro_login(self):
        resp = self._cliente().get("/adesao-portal")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
