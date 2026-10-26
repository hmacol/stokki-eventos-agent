# -*- coding: utf-8 -*-
"""
Tela /solicitacoes-portal: lista, executa e recusa solicitacoes do cliente
(spec 2026-10-24-aviso-pendencias-painel, Task 7).

    py -3.11 -m unittest test_solicitacoes_portal_tela   (de dentro de painel_agentes/)
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
from portal_cliente import envio_pedidos, solicitacoes_execucao  # noqa: E402

CONFIG_PAINEL = {"usuario": "u_total", "senha": "s_total"}
ORIGEM = {"Origin": "http://localhost"}
SOLIC = {"id": 7, "tipo": "cancelar", "cnpj_embarcador": "123", "criado_em": "2026-10-24T09:05:00",
         "detalhes": "cliente desistiu", "numero_nf": "55", "referencia": "R1",
         "origem": "xml", "destinatario_nome": "Loja X", "codigo_pedido": "PS-100", "status_envio": "ENVIADO"}


class _Base(unittest.TestCase):
    def setUp(self):
        for alvo in (
            mock.patch.object(painel_agentes, "_carregar_config", return_value={"painel_agentes": CONFIG_PAINEL}),
            mock.patch.object(painel_agentes.contadores_menu, "contadores", return_value={}),
            mock.patch.object(painel_agentes.avisos_pendencias, "montar", return_value=[]),
            mock.patch.object(envio_pedidos, "conectar", return_value=mock.MagicMock()),
            mock.patch.object(envio_pedidos, "listar_solicitacoes_pendentes", return_value=[dict(SOLIC)]),
        ):
            alvo.start()
            self.addCleanup(alvo.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()
        self._logar("total")

    def _logar(self, nivel, usuario="maria"):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = usuario


class TestSolicitacoesPortal(_Base):

    def test_lista(self):
        r = self.cliente.get("/api/solicitacoes-portal")
        self.assertEqual(r.status_code, 200)
        itens = r.get_json()["solicitacoes"]
        self.assertEqual(len(itens), 1)
        i = itens[0]
        self.assertEqual((i["id"], i["codigo_pedido"], i["tipo"]), (7, "PS-100", "cancelar"))
        self.assertEqual(i["rotulo"], envio_pedidos.ROTULOS_SOLICITACAO["cancelar"])
        self.assertEqual(i["criado_em"], "24/10 09:05")
        self.assertEqual(i["detalhe"], "cliente desistiu")
        self.assertEqual(self.cliente.get("/solicitacoes-portal").status_code, 200)

    def test_envio_cancelando_fica_fora_da_lista(self):
        cancelando = dict(SOLIC, id=8, status_envio="CANCELANDO")
        with mock.patch.object(envio_pedidos, "listar_solicitacoes_pendentes", return_value=[dict(SOLIC), cancelando]):
            itens = self.cliente.get("/api/solicitacoes-portal").get_json()["solicitacoes"]
        self.assertEqual([i["id"] for i in itens], [7])

    def test_executar_chama_executor(self):
        deps = object()
        with mock.patch.object(solicitacoes_execucao, "dependencias_reais", return_value=deps) as d, \
                mock.patch.object(solicitacoes_execucao, "executar",
                                  return_value={"ok": False, "mensagem": "Rota já saiu"}) as e:
            r = self.cliente.post("/api/solicitacoes-portal/7/executar", headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": False, "mensagem": "Rota já saiu"})
        self.assertEqual(d.call_args.kwargs.get("usuario"), "maria")
        self.assertEqual(e.call_args.args[1:], (7, deps))

    def test_executar_excecao_vira_500_json(self):
        with mock.patch.object(solicitacoes_execucao, "dependencias_reais", side_effect=RuntimeError("boom")):
            r = self.cliente.post("/api/solicitacoes-portal/7/executar", headers=ORIGEM)
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.get_json()["ok"])

    def test_recusar_exige_resposta(self):
        with mock.patch.object(solicitacoes_execucao, "recusar") as rec:
            for corpo in ({}, {"resposta": "  "}, {"resposta": "x" * 501}, {"resposta": 5}):
                r = self.cliente.post("/api/solicitacoes-portal/7/recusar", json=corpo, headers=ORIGEM)
                self.assertEqual(r.status_code, 400, corpo)
            rec.assert_not_called()
            r = self.cliente.post("/api/solicitacoes-portal/7/recusar", json={"resposta": " Fora do prazo "}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(rec.call_args.args[1:], (7, "Fora do prazo"))

    def test_recusar_nao_pendente_da_400(self):
        with mock.patch.object(solicitacoes_execucao, "recusar", side_effect=ValueError("já tratada")):
            r = self.cliente.post("/api/solicitacoes-portal/7/recusar", json={"resposta": "ok"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        self.assertIn("já tratada", r.get_json()["mensagem"])

    def test_post_exige_mesma_origem(self):
        r = self.cliente.post("/api/solicitacoes-portal/7/recusar", json={"resposta": "ok"},
                              headers={"Origin": "http://outro.com"})
        self.assertEqual(r.status_code, 403)

    def test_nivel_atendimento_bloqueado(self):
        self._logar("atendimento")
        self.assertNotEqual(self.cliente.get("/api/solicitacoes-portal").status_code, 200)
        self.assertNotEqual(self.cliente.get("/solicitacoes-portal").status_code, 200)
        r = self.cliente.post("/api/solicitacoes-portal/7/executar", headers=ORIGEM)
        self.assertNotEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
