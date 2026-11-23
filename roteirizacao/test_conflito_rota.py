# -*- coding: utf-8 -*-
"""
test_conflito_rota.py

criar_rota_removendo_conflitos: pedido que "já faz parte de uma rota" sai
do lote e o resto segue. Achado 28/09: com reentrega (PS-1-R1) a regex
só pegava "PS-1", nada casava e a rota inteira caía em erro.

Rodar (da raiz):
    python -m unittest roteirizacao.test_conflito_rota -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import rotas_client


def _falso_criar_rota(conflitos: list[str]):
    """criar_rota que recusa enquanto o próximo código de `conflitos`
    estiver entre os serviços enviados."""
    chamadas = []

    def criar(token, nome, start_at, start_location_base_id, service_ids, **_):
        chamadas.append(list(service_ids))
        if conflitos and conflitos[0][1] in service_ids:
            codigo, _sid = conflitos.pop(0)
            raise Exception(f"Serviço já faz parte de uma rota: #{codigo}")
        return {"id": 999, "services": service_ids}

    return criar, chamadas


class TestConflitoRota(unittest.TestCase):
    def _rodar(self, sublote, conflitos):
        criar, chamadas = _falso_criar_rota(conflitos)
        with mock.patch.object(rotas_client, "criar_rota", side_effect=criar):
            rota, final, removidos = rotas_client.criar_rota_removendo_conflitos(
                "t", "Rota X", "2026-09-29 08:00:00", sublote, start_location_base_id=1)
        return rota, final, removidos, chamadas

    def test_reentrega_com_sufixo_sai_do_lote(self):
        sublote = [{"id": 1, "code": "#PS-100"}, {"id": 2, "code": "#PS-100-R1"}, {"id": 3, "code": "PS-200"}]
        rota, final, removidos, _ = self._rodar(sublote, [("PS-100-R1", 2)])
        self.assertIsNotNone(rota)
        self.assertEqual([s["id"] for s in final], [1, 3])
        self.assertEqual(removidos, ["PS-100-R1"])

    def test_original_nao_derruba_a_reentrega(self):
        sublote = [{"id": 1, "code": "#PS-100"}, {"id": 2, "code": "#PS-100-R1"}]
        _, final, removidos, _ = self._rodar(sublote, [("PS-100", 1)])
        self.assertEqual([s["id"] for s in final], [2])
        self.assertEqual(removidos, ["PS-100"])

    def test_servico_combinado(self):
        sublote = [{"id": 1, "code": "#PS-1, #PS-2"}, {"id": 3, "code": "PS-3"}]
        _, final, _, _ = self._rodar(sublote, [("PS-2", 1)])
        self.assertEqual([s["id"] for s in final], [3])

    def test_codigo_desconhecido_propaga(self):
        sublote = [{"id": 1, "code": "PS-1"}]
        with mock.patch.object(rotas_client, "criar_rota",
                               side_effect=Exception("Serviço já faz parte de uma rota: #PS-9")):
            with self.assertRaises(Exception):
                rotas_client.criar_rota_removendo_conflitos(
                    "t", "Rota X", "2026-09-29 08:00:00", sublote, start_location_base_id=1)


if __name__ == "__main__":
    unittest.main()
