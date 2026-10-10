# -*- coding: utf-8 -*-
"""
Aprovação e recusa de auto-cadastro de motorista pelo painel (/motoristas,
08/10/2026): só o nível total vê e age; aprovar grava na planilha (duplo)
e cria o login; documento é servido só pra quem pode aprovar.

    cd painel_agentes && py -3.11 -m unittest test_cadastros_motoristas
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

from nucleo import banco, cadastro_motorista as cad, auth_motorista as auth  # noqa: E402

CONFIG_PAINEL = {"usuario": "u_total", "senha": "s_total", "usuario_operador": "u_op", "senha_operador": "s_op",
                 "usuario_leitura": "u_le", "senha_leitura": "s_le"}

CADASTRO = {"cpf": "98765432100", "nome": "Maria da Silva", "telefone": "11998765432", "chave_pix": "maria@x.com",
            "placa": "ABC1D23", "tipo_veiculo": "FIORINO", "zonas": ["ZONA SUL"], "dias": ["SEGUNDA"], "aceita_viagens": True}


class TestCadastrosMotoristas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        raiz = Path(self._tmp.name)
        self._patch_db = mock.patch.object(banco, "DB_PATH", raiz / "t.db")
        self._patch_db.start()
        config = {**painel_agentes._carregar_config(), "painel_agentes": CONFIG_PAINEL,
                  "motoristas": {"planilha": str(raiz / "BD.xlsx")}}
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=config)
        p.start()
        self.addCleanup(p.stop)
        self.planilha = []
        p2 = mock.patch("regras.cadastro_motoristas.cadastrar_motorista", side_effect=lambda cfg, d: self.planilha.append(d))
        p2.start()
        self.addCleanup(p2.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

        conn = banco.conectar()
        self.cid = cad.criar_cadastro(conn, CADASTRO)["id"]
        chave = conn.execute("SELECT chave_envio FROM motoristas_cadastros WHERE id = ?", (self.cid,)).fetchone()[0]
        cad.guardar_documento(conn, self.cid, chave, "cnh", "cnh.jpg", b"\xff\xd8" * 20)
        cad.guardar_documento(conn, self.cid, chave, "crlv", "crlv.jpg", b"\xff\xd8" * 20)
        conn.close()

    def tearDown(self):
        self._patch_db.stop()
        self._tmp.cleanup()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "hugo"

    def _post(self, url, body):
        return self.cliente.post(url, json=body, headers={"Origin": "http://localhost", "Referer": "http://localhost/"})

    def test_tela_mostra_pendentes_so_pro_total(self):
        self._logar("total")
        html = self.cliente.get("/motoristas").get_data(as_text=True)
        self.assertIn('id="titulo-pendentes"', html)
        self.assertIn("esperando aprovação (1)", html)
        self.assertIn("Maria da Silva", html)
        self._logar("operador")
        html = self.cliente.get("/motoristas").get_data(as_text=True)
        self.assertNotIn('id="titulo-pendentes"', html)
        self.assertNotIn("Maria da Silva", html)

    def test_documento_so_pro_total(self):
        url = f"/api/motoristas/cadastros/{self.cid}/documento/cnh"
        self._logar("operador")
        self.assertIn(self.cliente.get(url).status_code, (302, 403))
        self._logar("total")
        r = self.cliente.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cliente.get(f"/api/motoristas/cadastros/{self.cid}/documento/rg").status_code, 404)

    def test_aprovar_e_recusar(self):
        self._logar("operador")
        self.assertIn(self._post(f"/api/motoristas/cadastros/{self.cid}/aprovar", {"agent_id": 77}).status_code, (302, 403))

        self._logar("total")
        r = self._post(f"/api/motoristas/cadastros/{self.cid}/aprovar", {"agent_id": None})
        self.assertEqual(r.status_code, 400)
        r = self._post(f"/api/motoristas/cadastros/{self.cid}/aprovar",
                       {"agent_id": 77, "zonas": ["ZONA SUL", "CENTRO"], "dias": ["SEGUNDA", "SEXTA"], "tipo_veiculo": "VAN_HR", "aceita_viagens": False})
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertRegex(d["pin"], r"^\d{6}$")
        self.assertEqual(d["cadastro"]["revisado_por"], "hugo")
        self.assertEqual(self.planilha[0]["agent_id"], 77)
        self.assertEqual(self.planilha[0]["tipo_veiculo"], "VAN_HR")
        conn = banco.conectar()
        m = auth.buscar_motorista(conn, "98765432100")
        self.assertEqual((m["agent_id"], m["tipo_veiculo"], m["trocar_pin"], m["placa"]), (77, "VAN_HR", 1, "ABC1D23"))
        self.assertEqual(cad.contar_pendentes(conn), 0)
        # segundo cadastro: recusa
        cid2 = cad.criar_cadastro(conn, {**CADASTRO, "cpf": "11122233344"})["id"]
        conn.close()
        r = self._post(f"/api/motoristas/cadastros/{cid2}/recusar", {"motivo": "CNH vencida"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["cadastro"]["status"], "RECUSADO")
        self.assertEqual(self._post(f"/api/motoristas/cadastros/{cid2}/recusar", {"motivo": "x"}).status_code, 409)

    def test_chave_ve_financeiro(self):
        self._logar("total")
        self._post(f"/api/motoristas/cadastros/{self.cid}/aprovar", {"agent_id": 77})
        r = self._post("/api/motoristas/98765432100/ve-financeiro", {"ve": False})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertFalse(r.get_json()["ve_financeiro"])
        self.assertEqual(self._post("/api/motoristas/00000000000/ve-financeiro", {"ve": True}).status_code, 404)
        self._logar("operador")
        self.assertIn(self._post("/api/motoristas/98765432100/ve-financeiro", {"ve": True}).status_code, (302, 403))


if __name__ == "__main__":
    unittest.main()
