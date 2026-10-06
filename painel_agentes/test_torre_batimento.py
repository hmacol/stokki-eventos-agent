# -*- coding: utf-8 -*-
"""
"Tratar" e "Desfazer" da Torre em item do batimento tambem gravam/limpam a
tratativa em batimento_pedidos (Hugo, 06/10). Banco temporario, sem VUUPT.

    py -3.11 -m unittest painel_agentes.test_torre_batimento
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import torre_controle  # noqa: E402
from batimento import banco  # noqa: E402

ID = "batimento:PS-1:EXPEDIDO_SEM_ENTREGA"


class TestTorreTrataBatimento(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "batimento.db"
        for p in (mock.patch.object(torre_controle, "_RAIZ", Path(self._tmp.name)),
                  mock.patch.object(torre_controle, "tratativas"),
                  mock.patch.object(banco, "DB_PATH", self.db)):
            p.start()
            self.addCleanup(p.stop)
        conn = banco.conectar(self.db)
        banco.gravar_rodada(conn, [
            {"codigo": "PS-1", "caixa": "DIVERGENCIA", "rotulo": "EXPEDIDO_SEM_ENTREGA", "evidencias": ""},
            {"codigo": "PS-2", "caixa": "DIVERGENCIA", "rotulo": "ENTREGUE_NAO_EXPEDIDO", "evidencias": ""},
            {"codigo": "PS-3", "caixa": "DESTINO", "rotulo": "ENTREGUE", "evidencias": ""},
        ], {"lancados": 3, "equacao_fecha": True}, 1, datetime(2026, 10, 5, 7, 25))
        conn.close()

    def tratativa(self, codigo):
        conn = banco.conectar(self.db)
        try:
            return tuple(conn.execute("SELECT tratado_por, tratado_obs FROM batimento_pedidos WHERE codigo = ?",
                                      (codigo,)).fetchone())
        finally:
            conn.close()

    def test_tratar_na_torre_grava_no_batimento(self):
        torre_controle.marcar_excecao_tratada(ID, "2026-10-06", "Batimento", "PS-1 ...", "falei com o motorista",
                                              por="ana")
        self.assertEqual(self.tratativa("PS-1"), ("ana", "[Torre] falei com o motorista"))

    def test_motivo_vazio_vira_texto_padrao(self):
        torre_controle.marcar_excecao_tratada(ID, "2026-10-06", "Batimento", "", "")
        self.assertEqual(self.tratativa("PS-1"), ("torre", "Tratado na Torre"))

    def test_lote(self):
        n = torre_controle.marcar_excecoes_tratadas(
            [{"id": ID, "tipo": "Batimento"}, {"id": "batimento:PS-2:ENTREGUE_NAO_EXPEDIDO", "tipo": "Batimento"}],
            "2026-10-06", "lote", por="ana")
        self.assertEqual(n, 2)
        self.assertEqual(self.tratativa("PS-2"), ("ana", "[Torre] lote"))

    def test_desfazer_na_torre_limpa_o_batimento(self):
        torre_controle.marcar_excecao_tratada(ID, "2026-10-06", "Batimento", "", "x", por="ana")
        torre_controle.desfazer_excecao_tratada(ID)
        self.assertEqual(self.tratativa("PS-1"), (None, None))

    def test_pedido_fora_de_divergencia_nao_quebra_o_tratar_da_torre(self):
        torre_controle.marcar_excecao_tratada("batimento:PS-3:EXPEDIDO_SEM_ENTREGA", "2026-10-06", "Batimento",
                                              "", "x", por="ana")
        self.assertEqual(self.tratativa("PS-3"), (None, None))
        self.assertIn("batimento:PS-3:EXPEDIDO_SEM_ENTREGA",
                      torre_controle._buscar_tratadas(["batimento:PS-3:EXPEDIDO_SEM_ENTREGA"]))

    def test_item_nao_fecha_fica_so_na_torre(self):
        torre_controle.marcar_excecao_tratada("batimento:nao-fecha:2026-10-06 07:25:00", "2026-10-06",
                                              "Batimento", "", "x", por="ana")
        self.assertEqual(self.tratativa("PS-1"), (None, None))


if __name__ == "__main__":
    unittest.main()
