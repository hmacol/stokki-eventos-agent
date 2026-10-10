# -*- coding: utf-8 -*-
"""
/api/torre/rodar aceita os campos declarados em agentes.py["parametros"]
(hoje: "data" do Gerar PDFs de Romaneio -> --data). Pedido do Hugo,
07/10: o "Reexecutar" da Torre gerava sempre os romaneios de hoje, sem
perguntar a data.

    py -3.11 -m unittest painel_agentes.test_torre_rodar_data
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

CONFIG_PAINEL = {"usuario": "u_total", "senha": "s_total"}
CABECALHOS = {"Origin": "http://localhost"}


class TestTorreRodarComData(unittest.TestCase):

    def setUp(self):
        config = {**painel_agentes._carregar_config(), "painel_agentes": CONFIG_PAINEL}
        p_cfg = mock.patch.object(painel_agentes, "_carregar_config", return_value=config)
        p_rod = mock.patch.object(painel_agentes, "ha_execucao_rodando", return_value=False)
        self.iniciar = mock.patch.object(painel_agentes, "iniciar_execucao", return_value=77)
        for p in (p_cfg, p_rod, self.iniciar):
            self.addCleanup(p.stop)
        p_cfg.start()
        p_rod.start()
        self.iniciar_mock = self.iniciar.start()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "total"
            sess["usuario"] = "teste"

    def _rodar(self, body):
        return self.cliente.post("/api/torre/rodar", json=body, headers=CABECALHOS)

    def test_data_preenchida_vira_flag_data(self):
        r = self._rodar({"agente_id": "gerar_romaneios", "data": "2026-10-08"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json(), {"ok": True, "execucao_id": 77})
        agente = painel_agentes.buscar_agente("gerar_romaneios")
        self.iniciar_mock.assert_called_once_with(agente, modo_teste=False, args_extra=["--data", "2026-10-08"])

    def test_data_em_branco_roda_sem_flag(self):
        r = self._rodar({"agente_id": "gerar_romaneios", "data": ""})
        self.assertEqual(r.status_code, 200)
        self.iniciar_mock.assert_called_once()
        self.assertIsNone(self.iniciar_mock.call_args.kwargs.get("args_extra"))

    def test_data_invalida_devolve_400_sem_rodar(self):
        r = self._rodar({"agente_id": "gerar_romaneios", "data": "08/10/2026"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Data das rotas", r.get_json()["erro"])
        self.iniciar_mock.assert_not_called()

    def test_agente_sem_parametros_ignora_data(self):
        r = self._rodar({"agente_id": "somente_importacao", "data": "2026-10-08"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.iniciar_mock.call_args.kwargs.get("args_extra"))


if __name__ == "__main__":
    unittest.main()
