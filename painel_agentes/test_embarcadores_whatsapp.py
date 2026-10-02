# -*- coding: utf-8 -*-
"""
Cadastro do grupo de WhatsApp por embarcador (tela /embarcadores/whatsapp,
Hugo 30/09/2026).

    py -3.11 -m unittest painel_agentes.test_embarcadores_whatsapp
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import avisar_fora_area as afa  # noqa: E402
import embarcadores_whatsapp as ew  # noqa: E402

# "painel_agentes" e o nome da pasta: o modulo do app precisa ser carregado pelo caminho
if "painel_agentes_app" in sys.modules:
    pa = sys.modules["painel_agentes_app"]
else:
    _spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
    pa = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = pa  # o Flask acha a pasta templates/ pelo modulo registrado
    _spec.loader.exec_module(pa)


def _banco(caminho=":memory:"):
    conn = afa.conectar(caminho)
    conn.executescript("""
        CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, email TEXT, sender_id INTEGER);
        INSERT INTO interno VALUES ('11', 'ZETA LTDA', NULL, 'z@z.com', 1), ('22', 'ACME LTDA', 'ACME', '', 2);""")
    afa.garantir_coluna_grupo(conn)
    return conn


class Nucleo(unittest.TestCase):
    def setUp(self):
        self.conn = _banco()
        self.addCleanup(self.conn.close)

    def test_listar_ordenado_por_nome(self):
        self.assertEqual([e["nome"] for e in ew.listar(self.conn)], ["ACME", "ZETA LTDA"])
        self.assertEqual(ew.listar(self.conn)[0], {"cnpj": "22", "nome": "ACME", "email": "", "whatsapp_grupo_id": None})

    def test_salvar_e_remover(self):
        ew.salvar_grupo(self.conn, "22", "123@g.us")
        self.assertEqual(ew.listar(self.conn)[0]["whatsapp_grupo_id"], "123@g.us")
        ew.salvar_grupo(self.conn, "22", "")
        self.assertIsNone(ew.listar(self.conn)[0]["whatsapp_grupo_id"])

    def test_formato_invalido(self):
        for ruim in ("https://chat.whatsapp.com/abc", "123", "123@c.us", " 123@g.us "):
            with self.assertRaises(ValueError, msg=ruim):
                ew.salvar_grupo(self.conn, "22", ruim)
        self.assertIsNone(ew.listar(self.conn)[0]["whatsapp_grupo_id"])

    def test_cnpj_inexistente(self):
        with self.assertRaises(ValueError):
            ew.salvar_grupo(self.conn, "99", "123@g.us")


class Rotas(unittest.TestCase):
    def setUp(self):
        pa.app.config["TESTING"] = True
        self.cliente = pa.app.test_client()
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "operador"
            sess["usuario"] = "maria"
        # arquivo temporario (nao :memory:): a rota abre e FECHA a conexao
        # dela, e o teste le o resultado por outra
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        caminho = str(Path(self._tmp.name) / "dados.db")
        self.conn = _banco(caminho)
        self.addCleanup(self.conn.close)
        conectar_real = afa.conectar
        self.p_conn = patch.object(afa, "conectar", lambda db_path=None: conectar_real(caminho))
        self.p_conn.start(); self.addCleanup(self.p_conn.stop)
        self.p_grupos = patch.object(ew.integracao_openwa, "listar_grupos", return_value=[{"id": "1@g.us", "nome": "G1"}])
        self.grupos = self.p_grupos.start(); self.addCleanup(self.p_grupos.stop)

    def test_tela_abre(self):
        resp = self.cliente.get("/embarcadores/whatsapp")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ACME", resp.get_data(as_text=True))

    def test_grupos(self):
        self.assertEqual(self.cliente.get("/api/embarcadores/whatsapp/grupos").get_json(),
                         {"grupos": [{"id": "1@g.us", "nome": "G1"}], "indisponivel": False})
        self.grupos.return_value = None
        self.assertEqual(self.cliente.get("/api/embarcadores/whatsapp/grupos").get_json(), {"grupos": [], "indisponivel": True})

    def test_salvar(self):
        resp = self.cliente.post("/api/embarcadores/22/whatsapp-grupo", json={"grupo_id": "1@g.us"}, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(ew.listar(self.conn)[0]["whatsapp_grupo_id"], "1@g.us")

    def test_salvar_invalido_e_400(self):
        resp = self.cliente.post("/api/embarcadores/22/whatsapp-grupo", json={"grupo_id": "x"}, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 400)

    def test_nivel_leitura_nao_acessa(self):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "leitura"
        self.assertEqual(self.cliente.get("/embarcadores/whatsapp").status_code, 403)


if __name__ == "__main__":
    unittest.main()
