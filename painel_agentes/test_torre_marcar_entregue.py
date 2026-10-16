# -*- coding: utf-8 -*-
"""
test_torre_marcar_entregue.py

"Marcar como entregue" pelo painel (Hugo, 08/10): fecha o servico na
Vuupt por API (accept -> start -> check-in -> check-out) sem passar pelo
app do motorista, e deixa auditoria em torre_entregas_manuais +
tratativas_pedido.

Nenhum teste toca na VUUPT nem no dados.db real: o banco vai pra uma
pasta temporaria e o cliente da VUUPT e um duble.
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_torre_marcar_entregue -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import torre_controle  # noqa: E402


class _VuuptFalso:
    """Devolve o servico com o status configurado em `status` e anota as
    chamadas de concluir_como_agente. `falha` faz o check-out estourar."""
    status = "on_route"
    servico = True
    falha = None
    chamadas = []

    def __init__(self, token):
        self.token = token

    def buscar_servico_por_id(self, service_id):
        if not _VuuptFalso.servico:
            return None
        return {"id": service_id, "code": "#PS-77", "status": _VuuptFalso.status,
                "title": "Cliente Exemplo"}

    def concluir_como_agente(self, service_id, sucesso=True, failed_reason_id=None, status_atual=""):
        if _VuuptFalso.falha:
            raise _VuuptFalso.falha
        _VuuptFalso.chamadas.append((service_id, sucesso, status_atual))


class TestMarcarEntregueManual(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(torre_controle, "_RAIZ", Path(self._tmp.name)),
            mock.patch.object(torre_controle, "VuuptClient", _VuuptFalso),
            mock.patch.object(torre_controle, "_carregar_config", lambda: {"vuupt_api": {"token": "x"}}),
            mock.patch.object(torre_controle, "tratativas"),
        ]
        for p in self._patches:
            p.start()
        _VuuptFalso.status = "on_route"
        _VuuptFalso.servico = True
        _VuuptFalso.falha = None
        _VuuptFalso.chamadas = []

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _entregas(self):
        return torre_controle._buscar_entregas_manuais([77])

    def test_em_rota_fecha_na_vuupt_e_registra(self):
        resultado = torre_controle.marcar_entregue_manual(77, "hugo", "Cliente confirmou por WhatsApp")

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["codigo"], "#PS-77")
        self.assertFalse(resultado.get("ja_estava"))
        self.assertEqual(_VuuptFalso.chamadas, [(77, True, "on_route")])

        registro = self._entregas()[77]
        self.assertEqual(registro["codigo"], "#PS-77")
        self.assertEqual(registro["status_anterior"], "on_route")
        self.assertEqual(registro["usuario"], "hugo")
        self.assertEqual(registro["observacao"], "Cliente confirmou por WhatsApp")

        chamadas = torre_controle.tratativas.registrar_evento.call_args_list
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(chamadas[0].args[:3], ("#PS-77", "TORRE", "ENTREGUE_MANUAL"))
        self.assertEqual(chamadas[0].kwargs["service_id"], 77)
        self.assertIn("hugo", chamadas[0].kwargs["texto"])
        self.assertIn("WhatsApp", chamadas[0].kwargs["texto"])

    def test_assigned_tambem_fecha(self):
        # assigned = tem motorista mas ele nem aceitou; concluir_como_agente
        # percorre as etapas que faltam a partir do status atual.
        _VuuptFalso.status = "assigned"
        resultado = torre_controle.marcar_entregue_manual(77, "hugo", "ok")
        self.assertTrue(resultado["ok"])
        self.assertEqual(_VuuptFalso.chamadas, [(77, True, "assigned")])

    def test_ja_entregue_nao_chama_vuupt_nem_registra(self):
        _VuuptFalso.status = "done"
        resultado = torre_controle.marcar_entregue_manual(77, "hugo", "ok")
        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["ja_estava"])
        self.assertEqual(_VuuptFalso.chamadas, [])
        self.assertEqual(self._entregas(), {})
        torre_controle.tratativas.registrar_evento.assert_not_called()

    def test_sem_motorista_recusa(self):
        _VuuptFalso.status = "not_assigned"
        resultado = torre_controle.marcar_entregue_manual(77, "hugo", "ok")
        self.assertFalse(resultado["ok"])
        self.assertIn("motorista", resultado["erro"].lower())
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_cancelado_recusa(self):
        _VuuptFalso.status = "canceled"
        resultado = torre_controle.marcar_entregue_manual(77, "hugo", "ok")
        self.assertFalse(resultado["ok"])
        self.assertIn("cancelad", resultado["erro"].lower())
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_insucesso_recusa(self):
        # Insucesso ja aconteceu: o caminho e a reentrega (Duplicar), nao
        # transformar o failed em done por cima.
        _VuuptFalso.status = "failed"
        resultado = torre_controle.marcar_entregue_manual(77, "hugo", "ok")
        self.assertFalse(resultado["ok"])
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_nao_encontrado_recusa(self):
        _VuuptFalso.servico = False
        resultado = torre_controle.marcar_entregue_manual(77, "hugo", "ok")
        self.assertFalse(resultado["ok"])
        self.assertIn("encontrado", resultado["erro"].lower())

    def test_observacao_obrigatoria(self):
        resultado = torre_controle.marcar_entregue_manual(77, "hugo", "   ")
        self.assertFalse(resultado["ok"])
        self.assertEqual(_VuuptFalso.chamadas, [])

    def test_falha_na_vuupt_nao_registra(self):
        _VuuptFalso.falha = RuntimeError("check-out do servico 77: status 500")
        resultado = torre_controle.marcar_entregue_manual(77, "hugo", "ok")
        self.assertFalse(resultado["ok"])
        self.assertIn("500", resultado["erro"])
        self.assertEqual(self._entregas(), {})
        torre_controle.tratativas.registrar_evento.assert_not_called()


if __name__ == "__main__":
    unittest.main()
