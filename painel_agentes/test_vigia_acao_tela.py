# -*- coding: utf-8 -*-
"""
Aba "Não entregues" da /vigia e POST /api/vigia/decisao (Hugo, 21/11).

    py -3.11 -m unittest test_vigia_acao_tela   (de dentro de painel_agentes/)
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
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
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)

import acesso_telas  # noqa: E402
import tratativas  # noqa: E402
from vigia import banco, regras  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}
RETORNO = (date.today() + timedelta(days=3)).isoformat()   # dentro do limite de 60 dias


class TestVigiaAcao(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        cfg = {"painel_agentes": {"usuario": "u", "senha": "s"}}
        for p in (mock.patch.object(banco, "DB_PATH", self.db),
                  mock.patch.object(tratativas, "DB_PATH", self.db),
                  mock.patch.object(acesso_telas, "DB_PATH", self.db),
                  mock.patch.object(painel_agentes, "_carregar_config", return_value=cfg)):
            p.start()
            self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        conn = banco.conectar(self.db)
        linhas = [
            ("PS-1", regras.ROTA_PASSADA, "rota de 13/11 não terminou", "EMB A | CLIENTE 1"),
            ("PS-2", regras.INSUCESSO, "Não coletado", "EMB B | CLIENTE 2"),
            ("PS-3", regras.INSUCESSO, "Problema fiscal", "EMB C | CLIENTE 3"),
            ("PS-4", regras.RECUSADO, "embarcador pediu pra não reenviar", "EMB D | CLIENTE 4"),
        ]
        for cod, est, motivo, det in linhas:
            conn.execute("INSERT INTO vigia_pedidos (codigo, estado, motivo, desde, vence_em, vencido, detalhe, visto_em) "
                         "VALUES (?, ?, ?, '2026-11-10 08:00:00', '2026-11-10 08:00:00', 1, ?, '2026-11-19 09:45:00')",
                         (cod, est, motivo, det))
        conn.commit()
        conn.close()

    def _logar(self, nivel, usuario="teste"):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = usuario
            sess["rodada_sessoes"] = acesso_telas.rodada_sessoes()["rodada"]

    def _decidir(self, **corpo):
        base = {"codigo": "PS-1", "estado": regras.ROTA_PASSADA, "decisao": "nova_data",
                "ate": RETORNO, "obs": ""}
        return self.cliente.post("/api/vigia/decisao", json={**base, **corpo}, headers=ORIGEM)

    def test_logistica_abre_na_propria_equipe(self):
        self._logar("logistica")
        html = self.cliente.get("/vigia").get_data(as_text=True)
        self.assertIn("Confirmar a baixa com o motorista", html)
        self.assertIn("Criar a reentrega", html)            # não coletado é da logística
        self.assertNotIn("Cancelar e devolver o pedido na Stokki", html)   # recusado é do atendimento
        self.assertIn("btn-tratar", html)

    def test_atendimento_abre_na_propria_equipe_e_todas_mostra_tudo(self):
        self._logar("atendimento")
        html = self.cliente.get("/vigia").get_data(as_text=True)
        self.assertIn("Cancelar e devolver o pedido na Stokki", html)
        self.assertIn("Decidir com o embarcador", html)
        self.assertNotIn("Confirmar a baixa com o motorista", html)
        html = self.cliente.get("/vigia?equipe=").get_data(as_text=True)
        self.assertIn("Confirmar a baixa com o motorista", html)
        self.assertIn("Cancelar e devolver o pedido na Stokki", html)

    def test_total_ve_todas_e_a_soma_dos_grupos_bate(self):
        self._logar("total")
        html = self.cliente.get("/vigia").get_data(as_text=True)
        for pedido in ("PS-1", "PS-2", "PS-3", "PS-4"):
            self.assertIn(f"#{pedido}", html)
        self.assertIn("Todas · 4", html)

    def test_leitura_ve_mas_nao_age_nem_trata(self):
        self._logar("leitura")
        html = self.cliente.get("/vigia").get_data(as_text=True)
        self.assertIn("#PS-1", html)
        self.assertNotIn("btn-tratar", html)
        self.assertNotIn("/pedidos?q=", html)
        self.assertEqual(self._decidir().status_code, 403)

    def test_expedicao_nao_abre_a_vigia(self):
        self._logar("expedicao")
        self.assertEqual(self.cliente.get("/vigia").status_code, 403)

    def test_tratar_some_da_fila_e_vai_pra_com_decisao_e_pro_historico(self):
        self._logar("logistica", "carla")
        r = self._decidir(decisao="outro", obs="motorista volta segunda", ate=RETORNO)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        html = self.cliente.get("/vigia?equipe=").get_data(as_text=True)
        self.assertIn("Todas · 3", html)                     # saiu da fila
        self.assertIn("Com decisão", html)
        self.assertIn("motorista volta segunda", html)
        self.assertIn("btn-desfazer", html)
        conn = tratativas._conectar()
        ev = conn.execute("SELECT evento, decisao, texto FROM tratativas_pedido WHERE pedido_code = 'PS-1'").fetchone()
        conn.close()
        self.assertEqual(ev["evento"], "VIGIA_DECISAO")
        self.assertIn(f"até {RETORNO}", ev["texto"])
        self.assertIn("por carla", ev["texto"])

    def test_desfazer_devolve_pra_fila(self):
        self._logar("operador")
        self._decidir()
        r = self.cliente.post("/api/vigia/decisao/desfazer", json={"codigo": "PS-1"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Todas · 4", self.cliente.get("/vigia?equipe=").get_data(as_text=True))
        r = self.cliente.post("/api/vigia/decisao/desfazer", json={"codigo": "PS-1"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 400)

    def test_recusa_decisao_invalida_com_mensagem(self):
        self._logar("operador")
        for corpo, trecho in (({"ate": ""}, "data de retorno"), ({"decisao": "x"}, "o que foi decidido"),
                              ({"decisao": "outro", "obs": ""}, "Outro"), ({"ate": "2000-01-01"}, "passado"),
                              ({"estado": regras.INSUCESSO}, "mudou de situação")):
            r = self._decidir(**corpo)
            self.assertEqual(r.status_code, 400, corpo)
            self.assertIn(trecho, r.get_json()["erro"])

    def test_post_de_outra_origem_e_barrado(self):
        self._logar("operador")
        r = self.cliente.post("/api/vigia/decisao", json={"codigo": "PS-1"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_abas_antigas_continuam(self):
        self._logar("atendimento")
        html = self.cliente.get("/vigia?aba=abertos").get_data(as_text=True)
        self.assertIn("Todos os pedidos abertos", html)
        self.assertIn("O que fazer", html)
        self.assertIn("#PS-3", html)
        self.assertEqual(self.cliente.get("/vigia?estado=RECUSADO&vencidos=1").status_code, 200)
        self.assertEqual(self.cliente.get("/vigia?aba=fechamento").status_code, 200)

    def test_nada_vencido_mostra_mensagem(self):
        conn = banco.conectar(self.db)
        conn.execute("DELETE FROM vigia_pedidos")
        conn.commit()
        conn.close()
        self._logar("total")
        self.assertIn("Nada vencido sem decisão", self.cliente.get("/vigia").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
