# -*- coding: utf-8 -*-
"""
test_lote_ativo_enviadas.py

Rota enviada nao some da tela (Hugo, 18/11): em 17/11 o botao "Criar
Rotas Diarias (Rascunho)" abriu um lote novo as 12h41 e as 18 rotas ja
ENVIADAS do lote anterior sumiram do /planejamento (a tela mostra so o
lote ativo). Agora listar_rascunhos_do_dia traz o lote ativo + as
ENVIADAS de qualquer lote da data; rota criada/duplicada na tela vai
pro lote ativo (nao pro lote da enviada antiga).

Rodar (de painel_agentes/):
    py -3.11 -m unittest test_lote_ativo_enviadas -v
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

DIA = date(2026, 11, 17)


def _servico(i):
    return {"id": i, "code": f"PS-{i}", "address": f"Rua {i}", "latitude": -23.5, "longitude": -46.6,
            "dimension_3": 1}


def _rota(nome, ids):
    return {"nome": nome, "start_location_base_id": 1, "start_at": "2026-11-17T09:00:00Z",
            "sublote": [_servico(i) for i in ids]}


class TestLoteAtivoEnviadas(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            mock.patch.object(rascunhos_rota, "DB_PATH", Path(self.tmp.name) / "dados.db"),
            mock.patch("mapa_util.carregar_remetentes_por_sender_id", lambda: {}),
            mock.patch.object(rascunhos_rota, "_recalcular_km_silencioso", lambda conn, rid: None),
            mock.patch.object(rascunhos_rota, "_avisar_freshhub", lambda *a, **k: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # lote antigo (sequencia da tarde): #1 enviada, #2 ainda rascunho
        self.lote_antigo = rascunhos_rota.criar_lote_rascunhos(DIA, [
            _rota("Planejamento - 17/11/2026 - #1", [1, 2]),
            _rota("Planejamento - 17/11/2026 - #2", [3]),
        ])
        self._sql("UPDATE rascunhos_rota SET status = 'ENVIADO', vuupt_route_id = 99, "
                  "criado_em = '2026-11-16 16:58:00' WHERE nome LIKE '%#1'")
        self._sql("UPDATE rascunhos_rota SET criado_em = '2026-11-16 16:58:00' WHERE nome LIKE '%#2'")
        # lote novo (botao do painel as 12h41): passa a ser o ativo
        self.lote_novo = rascunhos_rota.criar_lote_rascunhos(DIA, [_rota("Planejamento - 17/11/2026 - #5", [4])])
        self._sql("UPDATE rascunhos_rota SET criado_em = '2026-11-17 12:42:00' WHERE lote_id = ?",
                  (self.lote_novo,))

    def _sql(self, sql, params=()):
        conn = rascunhos_rota._conectar()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _nomes(self):
        return [r["nome"] for r in rascunhos_rota.listar_rascunhos_do_dia(DIA)]

    def test_enviada_de_lote_anterior_continua_na_tela(self):
        self.assertEqual(self._nomes(), ["Planejamento - 17/11/2026 - #1", "Planejamento - 17/11/2026 - #5"])

    def test_rascunho_de_lote_anterior_continua_fora(self):
        self.assertNotIn("Planejamento - 17/11/2026 - #2", self._nomes())

    def test_enviada_descartada_nao_volta(self):
        self._sql("UPDATE rascunhos_rota SET status = 'DESCARTADO' WHERE nome LIKE '%#1'")
        self.assertEqual(self._nomes(), ["Planejamento - 17/11/2026 - #5"])

    def test_rota_nova_da_tela_vai_pro_lote_ativo(self):
        # a #1 (lote antigo) vem primeiro na lista; a referencia tem que ser do lote ativo
        self.assertEqual(rascunhos_rota.referencia_para_rascunho_manual(DIA)["lote_id"], self.lote_novo)

    def test_duplicar_enviada_antiga_vai_pro_lote_ativo(self):
        enviada = next(r for r in rascunhos_rota.listar_rascunhos_do_dia(DIA) if r["status"] == "ENVIADO")
        novo_id = rascunhos_rota.duplicar_rascunho(enviada["id"])
        self.assertEqual(rascunhos_rota.buscar_rascunho(novo_id)["lote_id"], self.lote_novo)
        # e o lote novo continua ativo: a #5 nao some
        self.assertIn("Planejamento - 17/11/2026 - #5", self._nomes())


if __name__ == "__main__":
    unittest.main()
