# -*- coding: utf-8 -*-
"""
test_exportar_historico.py

Testes do exportador do histórico da Vuupt (Etapa 1) sem rede e sem GCS:
janelas mensais, paginação, o alerta quando o que foi baixado não bate com
o total da API, e a retomada (janela pronta não baixa de novo).

    python -m unittest nucleo.test_exportar_historico -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import exportar_historico_vuupt as exportador


class TestJanelas(unittest.TestCase):
    def test_meses_do_periodo(self):
        self.assertEqual(exportador.meses("2026-11", "2027-02"), ["2026-11", "2026-12", "2027-01", "2027-02"])
        self.assertEqual(exportador.meses("2026-09", "2026-09"), ["2026-09"])

    def test_limites_do_mes_viram_filtro(self):
        self.assertEqual(exportador._limites("2026-09"), ("2026-09-01 00:00:00", "2026-10-01 00:00:00"))
        self.assertEqual(exportador._limites("2026-12"), ("2026-12-01 00:00:00", "2027-01-01 00:00:00"))


class _RespostaFalsa:
    """Paginação no formato da Vuupt: {'data': [...], 'meta': {'pagination': {...}}}."""

    def __init__(self, paginas):
        self.paginas = paginas
        self.chamadas = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.chamadas.append(dict(params or {}))
        pagina = (params or {}).get("page", 1)
        dados = self.paginas[pagina - 1]
        total = sum(len(p) for p in self.paginas)
        corpo = {"data": dados, "meta": {"pagination": {"total": total, "total_pages": len(self.paginas)}}}
        return mock.Mock(status_code=200, json=lambda: corpo, raise_for_status=lambda: None)


class TestBaixar(unittest.TestCase):
    def test_pagina_ate_o_fim_e_manda_o_filtro_do_mes(self):
        falsa = _RespostaFalsa([[{"id": 1}, {"id": 2}], [{"id": 3}]])
        with mock.patch.object(exportador.requests, "get", falsa.get):
            linhas, total = exportador.baixar_tudo("tok", "rotas", "2026-09", rps=0)
        self.assertEqual([l["id"] for l in linhas], [1, 2, 3])
        self.assertEqual(total, 3)
        primeiro = falsa.chamadas[0]
        self.assertEqual(primeiro["filter[0][field]"], "start_at")
        self.assertEqual(primeiro["filter[0][value]"], "2026-09-01 00:00:00")
        self.assertEqual(primeiro["filter[1][value]"], "2026-10-01 00:00:00")
        self.assertEqual([c["page"] for c in falsa.chamadas], [1, 2])

    def test_entidade_sem_data_vai_sem_filtro_e_com_include(self):
        falsa = _RespostaFalsa([[{"id": 9}]])
        with mock.patch.object(exportador.requests, "get", falsa.get):
            exportador.baixar_tudo("tok", "clientes", None, rps=0)
        self.assertNotIn("filter[0][field]", falsa.chamadas[0])
        falsa2 = _RespostaFalsa([[{"id": 9}]])
        with mock.patch.object(exportador.requests, "get", falsa2.get):
            exportador.baixar_tudo("tok", "servicos", "2026-09", rps=0)
        self.assertEqual(falsa2.chamadas[0]["include"], "customer,failedReason")


class TestExportarEntidades(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.estado_path = Path(self._tmp.name) / "estado.json"
        self._p1 = mock.patch.object(exportador, "ESTADO", self.estado_path)
        self._p1.start()
        self.enviados = []
        self._p2 = mock.patch.object(exportador, "enviar_jsonl",
                                     lambda config, linhas, caminho: self.enviados.append((caminho, len(linhas))) or 10)
        self._p2.start()

    def tearDown(self):
        self._p2.stop()
        self._p1.stop()
        self._tmp.cleanup()

    def test_exporta_janela_marca_estado_e_nao_repete(self):
        falsa = _RespostaFalsa([[{"id": 1}, {"id": 2}]])
        estado = exportador.ler_estado()
        with mock.patch.object(exportador.requests, "get", falsa.get):
            exportador.exportar_entidades("tok", {}, estado, "2026-09", "2026-09", rps=0,
                                          modo_teste=False, apenas=["rotas"])
            chamadas_primeira = len(falsa.chamadas)
            exportador.exportar_entidades("tok", {}, estado, "2026-09", "2026-09", rps=0,
                                          modo_teste=False, apenas=["rotas"])
        self.assertEqual(self.enviados, [("arquivo_vuupt/rotas/2026-09.jsonl.gz", 2)])
        self.assertEqual(len(falsa.chamadas), chamadas_primeira)          # 2ª vez não foi na API
        self.assertEqual(estado["janelas"]["rotas/2026-09"]["linhas"], 2)

    def test_avisa_quando_baixado_nao_bate_com_o_total_da_api(self):
        corpo = {"data": [{"id": 1}], "meta": {"pagination": {"total": 5, "total_pages": 1}}}
        falso_get = lambda *a, **k: mock.Mock(status_code=200, json=lambda: corpo, raise_for_status=lambda: None)  # noqa: E731
        estado = exportador.ler_estado()
        with mock.patch.object(exportador.requests, "get", falso_get):
            resumo = exportador.exportar_entidades("tok", {}, estado, "2026-09", "2026-09", rps=0,
                                                   modo_teste=False, apenas=["rotas"])
        self.assertIn("ATENCAO", resumo["rotas/2026-09"])

    def test_modo_teste_nao_grava_estado(self):
        falsa = _RespostaFalsa([[{"id": 1}]])
        estado = exportador.ler_estado()
        with mock.patch.object(exportador.requests, "get", falsa.get):
            exportador.exportar_entidades("tok", {}, estado, "2026-09", "2026-09", rps=0,
                                          modo_teste=True, apenas=["rotas"])
        self.assertEqual(estado["janelas"], {})
        self.assertEqual(self.enviados, [])


if __name__ == "__main__":
    unittest.main()
