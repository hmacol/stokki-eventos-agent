# -*- coding: utf-8 -*-
"""
Rotas do Galpao 3D (/galpao-3d e /api/galpao-3d/estado), so nivel total.

    cd painel_agentes && py -3.11 -m unittest test_galpao_3d_tela
"""
import sys
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


CONFIG_PAINEL = {
    "usuario": "u_total", "senha": "s_total",
    "usuario_operador": "u_op", "senha_operador": "s_op",
}


class TestGalpao3d(unittest.TestCase):
    def setUp(self):
        config = {**painel_agentes._carregar_config(), "painel_agentes": CONFIG_PAINEL}
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=config)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def test_total_abre(self):
        self._logar("total")
        r = self.cliente.get("/galpao-3d")
        self.assertEqual(r.status_code, 200)
        self.assertIn("galpao_3d.js", r.get_data(as_text=True))

    def test_operador_bloqueado(self):
        self._logar("operador")
        self.assertNotEqual(self.cliente.get("/galpao-3d").status_code, 200)
        self.assertIn(self.cliente.get("/api/galpao-3d/estado").status_code, (401, 403))

    def test_api_devolve_estado(self):
        self._logar("total")
        with mock.patch("galpao3d.estado.montar_estado", return_value={"placar": {}}), \
                mock.patch("galpao3d.banco.conectar", return_value=mock.MagicMock()):
            r = self.cliente.get("/api/galpao-3d/estado")
        self.assertEqual(r.status_code, 200)
        self.assertIn("placar", r.get_json())


if __name__ == "__main__":
    unittest.main()
