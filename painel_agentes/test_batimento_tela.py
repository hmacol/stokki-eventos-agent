# -*- coding: utf-8 -*-
"""
Aba Fechamento do /vigia e POST /api/batimento/tratar (05/10).

    py -3.11 -m unittest painel_agentes.test_batimento_tela
"""
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

from batimento import banco  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)


class TestAbaFechamento(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        p = mock.patch.object(banco, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def _gravar(self):
        conn = banco.conectar(self.db)
        banco.gravar_rodada(conn, [
            {"codigo": "PS-1", "caixa": "DIVERGENCIA", "rotulo": "EXPEDIDO_SEM_ENTREGA", "embarcador": "EMB A",
             "evidencias": "stokki=EXPEDIDO"},
            {"codigo": "PS-2", "caixa": "DESTINO", "rotulo": "ENTREGUE", "evidencias": ""},
        ], {"lancados": 2, "equacao_fecha": True}, 1, datetime(2026, 10, 5, 7, 25))
        conn.close()

    def test_sem_rodada_renderiza_vazio(self):
        self._logar("leitura")
        r = self.cliente.get("/vigia?aba=fechamento")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Nenhuma rodada", r.get_data(as_text=True))

    def test_aba_mostra_conta_e_divergencia(self):
        self._gravar()
        self._logar("operador")
        html = self.cliente.get("/vigia?aba=fechamento").get_data(as_text=True)
        for trecho in ("PS-1", "Expedido sem entrega", "EMB A", "fecha", "btn-tratar"):
            self.assertIn(trecho, html)

    def test_leitura_nao_ve_botao_nem_trata(self):
        self._gravar()
        self._logar("leitura")
        html = self.cliente.get("/vigia?aba=fechamento").get_data(as_text=True)
        self.assertNotIn("btn-tratar", html)
        r = self.cliente.post("/api/batimento/tratar", json={"codigo": "PS-1", "obs": "x"},
                              headers={"Origin": "http://localhost"})
        self.assertIn(r.status_code, (302, 401, 403))

    def test_operador_trata(self):
        self._gravar()
        self._logar("operador")
        r = self.cliente.post("/api/batimento/tratar", json={"codigo": "PS-1", "obs": "falei com o motorista"},
                              headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        conn = banco.conectar(self.db)
        row = conn.execute("SELECT tratado_por, tratado_obs FROM batimento_pedidos WHERE codigo='PS-1'").fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("teste", "falei com o motorista"))
        r = self.cliente.post("/api/batimento/tratar", json={"codigo": "PS-2", "obs": "x"},
                              headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 400)

    def test_rodada_atrasada_aparece_na_aba(self):
        self._gravar()   # rodada de 05/10 07:25, bem mais de 26h atras
        self._logar("leitura")
        self.assertIn("rodada atrasada", self.cliente.get("/vigia?aba=fechamento").get_data(as_text=True))

    def test_aba_padrao_continua_sendo_pedidos_abertos(self):
        self._logar("leitura")
        r = self.cliente.get("/vigia")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Vigia de pedidos abertos", r.get_data(as_text=True))


class TestTorreBatimento(unittest.TestCase):
    def test_torre_chama_o_batimento(self):
        src = (_AQUI / "torre_controle.py").read_text(encoding="utf-8")
        self.assertIn("from batimento.consulta import excecoes_torre as batimento_excecoes", src)


if __name__ == "__main__":
    unittest.main()
