# -*- coding: utf-8 -*-
"""
test_incrementar_rotas_prontidao.py -- no corte das 22h (incrementar_rotas) pedido do pool que ainda nao
aprontou na Stokki (retrato do pipeline) nao entra em rota nem em rascunho e vira evento nao_aprontou no
sistema novo; pedido que entra numa rota existente vira roteirizado (plano 1.2 do freshhub, Task 7).

Rodar (da raiz):  py -3.11 -m unittest roteirizacao.test_incrementar_rotas_prontidao -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import incrementar_rotas as inc  # noqa: E402
from test_incrementar_rotas import TestMainComRascunho  # noqa: E402


class TestProntidaoNoIncremento(TestMainComRascunho):
    def setUp(self):
        self.registrar = mock.Mock(return_value={"aceitos": 1, "repetidos": 0, "desconhecidos": []})
        self.retrato = ({"PS-2": "Ready to Pack"}, "25/10 21:40")
        for p in (mock.patch.object(inc.eventos_freshhub, "registrar", self.registrar),
                  mock.patch.object(inc.prontidao_stokki, "carregar_retrato_stokki",
                                    side_effect=lambda *a, **k: self.retrato)):
            p.start()
            self.addCleanup(p.stop)

    def _eventos(self, tipo):
        return [e for c in self.registrar.call_args_list for e in c.args[1] if e["tipo"] == tipo]

    def test_pedido_nao_pronto_fica_fora_do_rascunho_e_vira_nao_aprontou(self):
        resumo = self._rodar([], [{"id": 7, "status": "RASCUNHO", "paradas": []}])
        self.assertEqual(resumo["status"], "ok")
        paradas = self.inc_rascunho.call_args[0][1]
        self.assertEqual([p["service_id"] for p in paradas], [1])
        nao = self._eventos("nao_aprontou")
        self.assertEqual([e["codigo"] for e in nao], ["PS-2"])
        self.assertEqual(nao[0]["dados"]["situacao_stokki"], "Em conferência")
        self.assertEqual(nao[0]["dados"]["origem"], "incremento")
        self.assertIn("1 ainda em separação na Stokki", resumo["detalhe"])

    def test_modo_teste_repassa_pro_registrar_que_nao_manda(self):
        # eventos_freshhub.registrar e quem segura em modo teste (nucleo/test_eventos_freshhub); aqui so a flag
        self._rodar([], [{"id": 7, "status": "RASCUNHO", "paradas": []}], modo_teste=True)
        self.assertTrue(self.registrar.call_args_list)
        self.assertTrue(all(c.kwargs.get("modo_teste") is True for c in self.registrar.call_args_list))

    def test_sem_retrato_ninguem_e_barrado(self):
        self.retrato = ({}, None)
        self._rodar([], [{"id": 7, "status": "RASCUNHO", "paradas": []}])
        self.assertEqual([p["service_id"] for p in self.inc_rascunho.call_args[0][1]], [1, 2])
        self.assertEqual(self._eventos("nao_aprontou"), [])


class TestEventoDoIncremento(unittest.TestCase):
    def test_pedido_que_entra_em_rota_existente_vira_roteirizado(self):
        registrar = mock.Mock()
        with mock.patch.object(inc.eventos_freshhub, "registrar", registrar):
            inc._registrar_roteirizado_incremento({}, {"id": 5, "code": "#PS-7"},
                                                  {"id": 900, "nome": "Planejamento - 27/10/2026 - #2"},
                                                  "2026-10-27", modo_teste=False)
        (e,) = registrar.call_args.args[1]
        self.assertEqual((e["codigo"], e["tipo"]), ("PS-7", "roteirizado"))
        self.assertEqual(e["dados"], {"vuupt_route_id": 900, "rota_nome": "Planejamento - 27/10/2026 - #2",
                                      "data": "2026-10-27", "service_id": 5, "origem": "incremento"})
        self.assertTrue(registrar.call_args.kwargs["modo_teste"] is False)


if __name__ == "__main__":
    unittest.main()
