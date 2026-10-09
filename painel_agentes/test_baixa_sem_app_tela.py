# -*- coding: utf-8 -*-
"""
Tela /baixa-sem-app e Torre (motorista sem app, 08/10).

    py -3.11 -m unittest painel_agentes.test_baixa_sem_app_tela
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

from nucleo import banco  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)


class TestTorreSemApp(unittest.TestCase):
    def test_torre_chama_o_modulo(self):
        src = (_AQUI / "torre_controle.py").read_text(encoding="utf-8")
        self.assertIn("from nucleo.baixa_sem_app import excecoes_torre as sem_app_excecoes", src)


ROTA = {"id": 5000, "name": "Planejamento - 07/10/2026 - #18", "agent_id": 50191, "start_at": "2026-10-07 08:00:00",
        "services": {"data": [{"id": 1, "code": "#PS-1", "status": "assigned", "title": "Cliente A"},
                              {"id": 2, "code": "#PS-2", "status": "done", "title": "Cliente B"}]}}


class TestTelaBaixa(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for p in (mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "dados.db"),
                  mock.patch("rotas_client.buscar_rota", return_value=ROTA),
                  mock.patch("regras.preferencias_motoristas.agent_ids_sem_app", return_value={50191}),
                  mock.patch("nucleo.baixa_sem_app.executar_lote")):
            p.start()
            self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as s:
            s["nivel_acesso"] = nivel
            s["usuario"] = "teste"

    def _post(self, body):
        return self.cliente.post("/api/baixa-sem-app", json=body, headers={"Origin": "http://localhost"})

    def test_tela_lista_pedidos_e_motivos(self):
        self._logar("operador")
        html = self.cliente.get("/baixa-sem-app?rota=5000").get_data(as_text=True)
        for trecho in ("PS-1", "PS-2", "Cliente A", "Endereço Incorreto", "Confirmar baixa"):
            self.assertIn(trecho, html)

    def test_leitura_nao_entra(self):
        self._logar("leitura")
        self.assertIn(self.cliente.get("/baixa-sem-app?rota=5000").status_code, (302, 401, 403))
        self.assertIn(self._post({"rota": 5000, "itens": []}).status_code, (302, 401, 403))

    def test_post_cria_lote_e_dispara(self):
        self._logar("operador")
        r = self._post({"rota": 5000, "itens": [
            {"service_id": 1, "codigo": "PS-1", "entregue": False, "failed_reason_id": 5433}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        lote = r.get_json()["lote"]
        st = self.cliente.get(f"/api/baixa-sem-app/{lote}").get_json()
        self.assertEqual(st["itens"][0]["failed_reason_id"], 5433)

    def test_segundo_lote_da_mesma_rota_e_recusado(self):
        self._logar("operador")
        body = {"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": True}]}
        self.assertEqual(self._post(body).status_code, 200)
        self.assertEqual(self._post(body).status_code, 409)   # executar_lote mockado: o 1o segue "em andamento"

    def test_post_recusa_sem_motivo_e_motorista_com_app(self):
        self._logar("operador")
        r = self._post({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": False}]})
        self.assertEqual(r.status_code, 400)
        with mock.patch("regras.preferencias_motoristas.agent_ids_sem_app", return_value=set()):
            r = self._post({"rota": 5000, "itens": [{"service_id": 1, "codigo": "PS-1", "entregue": True}]})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
