# -*- coding: utf-8 -*-
"""
test_plano_original.py

Foto do plano (Hugo, 17/10): criar_lote_rascunhos grava em
rascunhos_plano_original em qual rascunho cada pedido NASCEU. As edicoes
da tela (mover, fundir, descartar) mexem so em rascunhos_parada, entao
a foto continua mostrando o plano que o roteirizador entregou -- e
roteirizacao/aderencia_plano.py compara com o que foi enviado.

Rodar (de painel_agentes/):
    py -3.11 -m unittest test_plano_original -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import rascunhos_rota  # noqa: E402

DIA = date(2026, 10, 17)


def _servico(i):
    return {"id": i, "code": f"PS-{i}", "address": f"Rua {i}", "latitude": -23.5, "longitude": -46.6,
            "dimension_3": 1}


class TestPlanoOriginal(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            mock.patch.object(rascunhos_rota, "DB_PATH", Path(self.tmp.name) / "dados.db"),
            mock.patch("mapa_util.carregar_remetentes_por_sender_id", lambda: {}),
            mock.patch.object(rascunhos_rota, "_recalcular_km_silencioso", lambda conn, rid: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rota(self, nome, ids):
        return {"nome": nome, "start_location_base_id": 1, "start_at": "2026-10-17T09:00:00Z",
                "sublote": [_servico(i) for i in ids]}

    def _plano(self):
        conn = rascunhos_rota._conectar()
        try:
            return {r["service_id"]: r["rascunho_id"] for r in conn.execute(
                "SELECT service_id, rascunho_id FROM rascunhos_plano_original")}
        finally:
            conn.close()

    def test_foto_grava_onde_cada_pedido_nasceu(self):
        lote = rascunhos_rota.criar_lote_rascunhos(DIA, [self._rota("A", [1, 2]), self._rota("B", [3])])
        rotas = {r["nome"]: r["id"] for r in rascunhos_rota.listar_rascunhos_do_dia(DIA)}
        self.assertEqual(self._plano(), {1: rotas["A"], 2: rotas["A"], 3: rotas["B"]})
        conn = rascunhos_rota._conectar()
        try:
            lotes = {r[0] for r in conn.execute("SELECT lote_id FROM rascunhos_plano_original")}
        finally:
            conn.close()
        self.assertEqual(lotes, {lote})

    def test_mover_parada_nao_altera_a_foto(self):
        rascunhos_rota.criar_lote_rascunhos(DIA, [self._rota("A", [1, 2]), self._rota("B", [3])])
        rotas = {r["nome"]: r["id"] for r in rascunhos_rota.listar_rascunhos_do_dia(DIA)}
        rascunhos_rota.mover_parada(2, rotas["A"], rotas["B"], 0)
        self.assertEqual(self._plano()[2], rotas["A"])


if __name__ == "__main__":
    unittest.main()
