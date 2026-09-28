# -*- coding: utf-8 -*-
"""
test_pedidos_parados_reenvio.py

"Reenvio" da triagem segue a cadeia de reentregas (achado 28/09): se a
R1 também falhou, gera a R2 em vez de responder "já duplicado → R1" e
dar a tratativa por concluída. E a sugestão via Vuupt não chama mais de
"Em Rota" o pedido que está no pool.

Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_pedidos_parados_reenvio -v
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import pedidos_parados_triagem as ppt  # noqa: E402

ORIGINAL = {"id": 1, "code": "#PS-100", "status": "done", "completed_at": "x", "failed_reason_id": 7}


class _Fingerprint:
    def __init__(self, duplicados: dict):
        self.duplicados = dict(duplicados)  # service_id -> novo_code

    def ja_duplicado(self, sid):
        return sid in self.duplicados

    def buscar_novo_code(self, sid):
        return self.duplicados.get(sid)

    def marcar_duplicado(self, sid, code):
        self.duplicados[sid] = code


class TestReenvio(unittest.TestCase):
    def _rodar(self, servicos_por_code: dict, duplicados: dict):
        fp = _Fingerprint(duplicados)
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_code.side_effect = lambda c: servicos_por_code.get(c.lstrip("#"))
        expedir = mock.Mock()
        expedir.duplicar_servico_por_insucesso.side_effect = lambda v, s: {"code": "#PS-100-R2"}
        acoes = []
        with mock.patch.dict(sys.modules, {"fingerprint_duplicacao_insucesso": fp}), \
             mock.patch.object(ppt, "_vuupt", return_value=vuupt), \
             mock.patch.object(ppt, "_resolver_pedido", return_value=(ORIGINAL, "PS-100")), \
             mock.patch.object(ppt, "_expedir_pedidos_raiz", return_value=expedir), \
             mock.patch.object(ppt, "_marcar_acao", side_effect=lambda *a: acoes.append(a)), \
             mock.patch.object(ppt.tratativas, "registrar_evento"):
            resultado = ppt.duplicar("100", "teste")
        return resultado, expedir, fp, acoes

    def test_r1_falhou_gera_r2(self):
        r1 = {"id": 2, "code": "#PS-100-R1", "status": "done", "completed_at": "x", "failed_reason_id": 7}
        resultado, expedir, fp, _ = self._rodar({"PS-100-R1": r1}, {1: "#PS-100-R1"})
        self.assertFalse(resultado["ja_existia"])
        self.assertEqual(resultado["novo_code"], "#PS-100-R2")
        expedir.duplicar_servico_por_insucesso.assert_called_once()
        self.assertIs(expedir.duplicar_servico_por_insucesso.call_args[0][1], r1)
        self.assertEqual(fp.duplicados[2], "#PS-100-R2")

    def test_r1_ativa_nao_duplica(self):
        r1 = {"id": 2, "code": "#PS-100-R1", "status": "not_assigned"}
        resultado, expedir, _, acoes = self._rodar({"PS-100-R1": r1}, {1: "#PS-100-R1"})
        self.assertTrue(resultado["ja_existia"])
        expedir.duplicar_servico_por_insucesso.assert_not_called()
        self.assertIn("já ativa", acoes[-1][2])

    def test_r1_entregue_nao_duplica(self):
        r1 = {"id": 2, "code": "#PS-100-R1", "status": "done", "completed_at": "x"}
        resultado, expedir, _, _ = self._rodar({"PS-100-R1": r1}, {1: "#PS-100-R1"})
        self.assertTrue(resultado["ja_existia"])
        expedir.duplicar_servico_por_insucesso.assert_not_called()

    def test_original_sem_reentrega_duplica(self):
        resultado, expedir, _, _ = self._rodar({}, {})
        self.assertFalse(resultado["ja_existia"])
        expedir.duplicar_servico_por_insucesso.assert_called_once()


class TestSugestaoVuupt(unittest.TestCase):
    """28/09: not_assigned está no POOL, não em rota -- sem sugestão."""
    HOJE = __import__("datetime").date(2026, 9, 28)

    def test_pool_sem_agendamento_nao_vira_em_rota(self):
        alvo, motivo = ppt._sugestao_vuupt({"status": "not_assigned"}, self.HOJE)
        self.assertIsNone(alvo)
        self.assertTrue(motivo.startswith(ppt.MOTIVO_NO_POOL))

    def test_pool_agendado_hoje_nao_vira_em_rota(self):
        alvo, _ = ppt._sugestao_vuupt({"status": "not_assigned", "scheduled_start": "2026-09-28 12:00:00"}, self.HOJE)
        self.assertIsNone(alvo)

    def test_atribuido_segue_em_rota(self):
        alvo, _ = ppt._sugestao_vuupt({"status": "assigned"}, self.HOJE)
        self.assertEqual(alvo, "Em Rota")


if __name__ == "__main__":
    unittest.main()
