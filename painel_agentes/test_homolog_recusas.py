# -*- coding: utf-8 -*-
"""
Homologacao (Task 5): agentes e disparo recusados, telas que liam a Stokki sem 500.

    py -3.11 -m unittest test_homolog_recusas -v   (de dentro de painel_agentes/)
"""
import importlib.util
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from homologacao import ambiente  # noqa: E402
from homologacao.ambiente import DesligadoNaHomologacao  # noqa: E402
from homologacao.guarda import EnvioBloqueadoHomolog  # noqa: E402

_patchers = []
painel_agentes = None


def setUpModule():
    global painel_agentes
    for p in (mock.patch.dict(os.environ, {"FRESH_AMBIENTE": "homolog"}),
              mock.patch.object(ambiente, "exigir_coerencia", lambda raiz: None)):
        p.start()
        _patchers.append(p)
    spec = importlib.util.spec_from_file_location("painel_agentes_homolog_recusas", _AQUI / "painel_agentes.py")
    painel_agentes = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = painel_agentes
    spec.loader.exec_module(painel_agentes)


def tearDownModule():
    for p in reversed(_patchers):
        p.stop()


class TestRecusas(unittest.TestCase):
    def test_executor_recusa_os_tres(self):
        import executor
        agente = {"id": "x", "nome": "X"}
        with mock.patch.object(executor.subprocess, "Popen") as popen, \
                mock.patch.object(executor, "_conectar", side_effect=AssertionError("tocou no banco")):
            with self.assertRaises(DesligadoNaHomologacao):
                executor.iniciar_execucao(agente, True)
            with self.assertRaises(DesligadoNaHomologacao):
                executor.iniciar_sequencia([{"agente": agente}])
            with self.assertRaises(DesligadoNaHomologacao):
                executor.iniciar_execucao_com_fila(agente, True, ("x",))
            popen.assert_not_called()

    def test_disparo_recusa(self):
        import notificacoes_tela
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE notificacoes_disparos (id INTEGER PRIMARY KEY, tipo TEXT)")
        with self.assertRaises(DesligadoNaHomologacao):
            notificacoes_tela.iniciar_disparo(conn, "resumo_diario", "todos", None, False, "teste")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notificacoes_disparos").fetchone()[0], 0)


class TestTelasStokki(unittest.TestCase):
    def setUp(self):
        import consulta_expedicao
        import pedidos_parados_triagem as triagem
        import torre_controle
        from portal_cliente import envio_pedidos, solicitacoes_execucao
        bloqueio = EnvioBloqueadoHomolog("indisponível na homologação: teste")
        vuupt = mock.MagicMock()
        vuupt.buscar_servico_por_code.return_value = None
        for alvo in (
            mock.patch.object(painel_agentes, "_carregar_config",
                              return_value={"painel_agentes": {"usuario": "u", "senha": "s"}}),
            mock.patch.object(painel_agentes.contadores_menu, "contadores", return_value={}),
            mock.patch.object(painel_agentes.avisos_pendencias, "montar", return_value=[]),
            mock.patch("stokki.auth.StokkiSession", side_effect=bloqueio),
            mock.patch.object(consulta_expedicao, "StokkiSession", side_effect=bloqueio),
            mock.patch.object(consulta_expedicao, "_SessaoSemLogin", side_effect=bloqueio),
            mock.patch.object(consulta_expedicao, "_carregar_config", return_value={}),
            mock.patch.object(triagem, "_vuupt", return_value=vuupt),
            mock.patch.object(triagem, "_carregar_config", return_value={}),
            mock.patch.object(triagem, "_painel_tem_execucao_rodando", return_value=False),
            mock.patch.object(triagem, "_base_externa_com_cache", return_value={}),
            mock.patch.object(triagem, "_catalogo_transportadoras", return_value=None),
            mock.patch.object(torre_controle, "_carregar_config", return_value={}),
            mock.patch.object(torre_controle, "VuuptClient"),
            mock.patch.object(torre_controle, "_coletar_backlog", side_effect=bloqueio),
            mock.patch.object(envio_pedidos, "conectar", return_value=mock.MagicMock()),
            mock.patch.object(solicitacoes_execucao, "dependencias_reais", return_value=mock.MagicMock()),
            mock.patch.object(solicitacoes_execucao, "executar", side_effect=bloqueio),
        ):
            alvo.start()
            self.addCleanup(alvo.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = "total"
            sess["usuario"] = "maria"

    def _confere(self, resp):
        corpo = json.dumps(resp.get_json(silent=True) or resp.get_data(as_text=True), ensure_ascii=False)
        self.assertNotEqual(resp.status_code, 500, corpo[:300])
        self.assertTrue("indisponível na homologação" in corpo or "conferida" in corpo
                        or "Stokki" in corpo or "Vuupt" in corpo, corpo[:300])

    def test_consulta_expedicao(self):
        self._confere(self.cliente.get("/api/expedicao/consulta?codigo=PS-12345"))

    def test_verificar_na_stokki(self):
        self._confere(self.cliente.post(
            "/api/pedidos-parados/verificar-stokki", json={"pedidos": [{"order_number": "PS-1"}]},
            headers={"Origin": "http://localhost"}))

    def test_contar_sem_rota(self):
        self._confere(self.cliente.get("/api/planejamento/sem-rota-hoje"))

    def test_executar_solicitacao(self):
        self._confere(self.cliente.post("/api/solicitacoes-portal/7/executar",
                                        headers={"Origin": "http://localhost"}))


if __name__ == "__main__":
    unittest.main()
