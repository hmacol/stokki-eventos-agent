# -*- coding: utf-8 -*-
"""Aprovacao do pre-cadastro de embarcador pelo painel (10/10): so o nivel
total grava interno.habilidade.

    cd painel_agentes && py -3.11 -m unittest test_cadastro_embarcador_painel
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

import cadastro_embarcador as cad  # noqa: E402
from nucleo import banco  # noqa: E402

CONFIG_PAINEL = {"usuario": "u_total", "senha": "s_total", "usuario_operador": "u_op", "senha_operador": "s_op",
                 "usuario_leitura": "u_le", "senha_leitura": "s_le"}
CNPJ = "49874051000131"


class TestAprovarCadastroEmbarcador(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch_db = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch_db.start()
        config = {**painel_agentes._carregar_config(), "painel_agentes": CONFIG_PAINEL}
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=config)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        conn = banco.conectar()
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
                     "habilidade TEXT, atualizado_em TEXT)")
        conn.execute("INSERT INTO interno VALUES (?, 'NONNA', 'FABENNE', NULL, NULL)", (CNPJ,))
        conn.commit()
        cad.registrar(conn, CNPJ, 100, cad.CONVIDADO)
        cad.salvar_preenchimento(conn, CNPJ, "Refrigerado")
        conn.close()

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "hugo"

    def _post(self, body=None, origem="http://localhost"):
        return self.cliente.post(f"/api/cadastro-embarcador/{CNPJ}/aprovar", json=body or {},
                                 headers={"Origin": origem, "Referer": origem + "/"})

    def _habilidade(self):
        conn = banco.conectar()
        try:
            return conn.execute("SELECT habilidade FROM interno WHERE cnpj_embarcador = ?", (CNPJ,)).fetchone()[0]
        finally:
            conn.close()

    def test_so_total_aprova(self):
        self._logar("operador")
        self.assertIn(self._post().status_code, (302, 403))
        self.assertIsNone(self._habilidade())

    def test_aprovar_grava_e_some_da_lista(self):
        self._logar("total")
        r = self._post()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["cadastro"]["aprovado_por"], "hugo")
        self.assertEqual(self._habilidade(), "Refrigerado")
        conn = banco.conectar()
        try:
            self.assertEqual(cad.listar_abertos(conn), [])
        finally:
            conn.close()
        self.assertEqual(self._post().status_code, 409)

    def test_corrigir_tipo_e_tipo_invalido(self):
        self._logar("total")
        self.assertEqual(self._post({"habilidade": "Quente"}).status_code, 400)
        self.assertEqual(self._post({"habilidade": "Seco"}).status_code, 200)
        self.assertEqual(self._habilidade(), "Seco")

    def test_origem_estranha(self):
        self._logar("total")
        self.assertEqual(self._post(origem="http://outro.site").status_code, 403)
        self.assertIsNone(self._habilidade())


if __name__ == "__main__":
    unittest.main()
