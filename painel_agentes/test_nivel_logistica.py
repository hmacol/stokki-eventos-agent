# -*- coding: utf-8 -*-
"""
Nivel "logistica" do painel (19/10, Hugo): so as telas da logistica, com
os mesmos poderes do operador nelas. Login so por usuarios_extras.

    py -3.11 -m unittest test_nivel_logistica   (de dentro de painel_agentes/)
"""
import re
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

MENU_ESPERADO = {
    "Início", "Torre de Controle", "Canhotos pendentes", "Pedidos Parados", "Tratativas", "Consulta",
    "Planejamento", "Atendimento", "Motoristas", "Pedágios", "Canhotos", "Clientes AGENDA", "Adesão ao portal",
}


class TestNivelLogistica(unittest.TestCase):

    def setUp(self):
        base = painel_agentes._carregar_config()
        cfg = {**base, "painel_agentes": {**base["painel_agentes"], "usuarios_extras": [
            {"usuario": "quecia", "senha": "s_q", "nivel": "logistica"}]}}
        self._patch = mock.patch.object(painel_agentes, "_carregar_config", return_value=cfg)
        self._patch.start()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        r = self.cliente.post("/login", data={"usuario": "quecia", "senha": "s_q"})
        self.assertEqual(r.status_code, 302)

    def tearDown(self):
        self._patch.stop()

    def test_menu_mostra_so_as_telas_da_logistica(self):
        r = self.cliente.get("/inicio")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        nav = html[html.index('aria-label="Telas do painel"'):]
        nav = nav[:nav.index("</nav>")]
        self.assertEqual(set(re.findall(r'<span class="rotulo">([^<]+)</span>', nav)), MENU_ESPERADO)

    def test_telas_fora_da_lista_dao_403(self):
        for url in ("/", "/historico", "/vigia", "/mapa-rotas", "/laboratorio-rotas", "/expedicao",
                    "/expedicao/consulta", "/wms", "/wms/estoque", "/baixa-sem-app"):
            self.assertEqual(self.cliente.get(url).status_code, 403, url)

    def test_aprovar_cadastro_de_motorista_continua_so_do_total(self):
        r = self.cliente.post("/api/motoristas/cadastros/1/aprovar", json={})
        self.assertEqual(r.status_code, 403)

    def test_telas_da_lista_abrem(self):
        for url in ("/canhotos-pendentes", "/historico-tratativas", "/consulta", "/clientes-agenda",
                    "/atendimento", "/motoristas", "/financeiro/pedagios", "/canhotos", "/adesao-portal"):
            self.assertEqual(self.cliente.get(url).status_code, 200, url)


if __name__ == "__main__":
    unittest.main()
