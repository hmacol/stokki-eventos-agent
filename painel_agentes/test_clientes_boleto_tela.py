# -*- coding: utf-8 -*-
"""
Tela /clientes-boleto do painel (06/10).

    py -3.11 -m unittest painel_agentes.test_clientes_boleto_tela
"""
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

from regras import clientes_boleto as cb  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)


class TestTelaClientesBoleto(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        banco = Path(self._tmp.name) / "dados.db"
        c = sqlite3.connect(banco)
        c.executescript("""
            CREATE TABLE nucleo_pedidos (codigo TEXT, sender_id INTEGER, destinatario_codigo TEXT,
                                         destinatario_nome TEXT, remetente_nome TEXT);
            CREATE TABLE documentos_processados (hash_conteudo TEXT PRIMARY KEY, tipo TEXT, codigo_pedido TEXT,
                                                 status TEXT, cobranca INTEGER, processado_em TEXT);
            INSERT INTO nucleo_pedidos VALUES ('PS-1', 11, '49749452000160', 'VERO PANE', 'MARIA DOLORES - NUU');
            INSERT INTO documentos_processados VALUES ('h1', 'Nota Fiscal', 'PS-1', 'ENVIADO', 1, '2099-01-01 10:00:00');
        """)
        c.commit()
        c.close()
        p = mock.patch.object(cb, "DB_PATH", banco)
        p.start()
        self.addCleanup(p.stop)
        cb.sugerir()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def test_total_ve_o_pendente_com_evidencia(self):
        self._logar("total")
        resp = self.cliente.get("/clientes-boleto")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("VERO PANE", html)
        self.assertIn("49.749.452/0001-60", html)
        self.assertIn("NUU", html)
        self.assertIn('data-acao="tem"', html)

    def test_outros_niveis_nao_entram(self):
        for nivel in ("operador", "leitura", "atendimento"):
            self._logar(nivel)
            self.assertEqual(self.cliente.get("/clientes-boleto").status_code, 403, nivel)

    def test_decidir_tem_e_trocar(self):
        self._logar("total")
        corpo = {"sender_id": 11, "documento": "49749452000160", "acao": "tem"}
        resp = self.cliente.post("/api/clientes-boleto/decidir", json=corpo, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["situacao"], cb.TEM_BOLETO)
        corpo["acao"] = "nao_tem"
        resp = self.cliente.post("/api/clientes-boleto/decidir", json=corpo, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.get_json()["situacao"], cb.NAO_TEM_BOLETO)
        self.assertEqual(cb.listar()[0]["decidido_por"], "teste")

    def test_acao_invalida_origem_estranha_e_par_inexistente(self):
        self._logar("total")
        h = {"Origin": "http://localhost"}
        self.assertEqual(self.cliente.post("/api/clientes-boleto/decidir",
                                           json={"sender_id": 11, "documento": "49749452000160", "acao": "x"},
                                           headers=h).status_code, 400)
        self.assertEqual(self.cliente.post("/api/clientes-boleto/decidir",
                                           json={"sender_id": 11, "documento": "49749452000160", "acao": "tem"},
                                           headers={"Origin": "http://outro.site"}).status_code, 403)
        self.assertEqual(self.cliente.post("/api/clientes-boleto/decidir",
                                           json={"sender_id": 99, "documento": "1", "acao": "tem"},
                                           headers=h).status_code, 400)


if __name__ == "__main__":
    unittest.main()
