# -*- coding: utf-8 -*-
"""
Criacao direta de rota (criar_rotas_diarias sem --gerar-rascunho) com o pool direto da
Stokki (Task 5): pedido de id sintetico ganha o servico na Vuupt antes da rota; quem falha
fica fora com o motivo no log; lista so com id real nem chama a materializacao.

Rodar (da raiz):  py -3.11 -m unittest roteirizacao.test_criar_rotas_sem_servico -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import criar_rotas_diarias as crd  # noqa: E402


class ComServicoTest(unittest.TestCase):
    def test_troca_o_id_e_tira_quem_falhou(self):
        sublote = [{"id": 5, "code": "#PS-5"}, {"id": -1, "code": "#PS-1"}, {"id": -2, "code": "#PS-2"}]
        retorno = ([{"id": 5, "code": "#PS-5"}, {"id": 900, "code": "#PS-1"}], {"PS-2": "pedido PS-2 esta CANCELADO"},
                   {})
        with mock.patch.object(crd.materializar, "materializar_lote", return_value=retorno) as m, \
                self.assertLogs(crd.logger, "ERROR") as logs:
            saida = crd._com_servico(sublote, "vuupt", {}, "Planejamento - 27/11/2026 - #1")
        m.assert_called_once_with(sublote, "vuupt", {})
        self.assertEqual([s["id"] for s in saida], [5, 900])
        self.assertTrue(any("PS-2" in l and "CANCELADO" in l for l in logs.output))

    def test_so_id_real_nao_chama_a_vuupt(self):
        sublote = [{"id": 5, "code": "#PS-5"}]
        vuupt = mock.Mock()
        self.assertEqual(crd._com_servico(sublote, vuupt, {}, "x"), sublote)
        self.assertEqual(vuupt.mock_calls, [])


class NaoSaiParcialTest(unittest.TestCase):
    """Onda 3 (Hugo: "nao sair parcial"): falha TECNICA num pedido do lote = a rota NAO e criada nesta rodada
    (todos ficam no pool); recusa PERMANENTE tira so aquele pedido e a rota sai com o resto."""

    def setUp(self):
        import tempfile
        from nucleo import banco, pedidos
        from nucleo.test_materializar import _payload
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(banco, "DB_PATH", Path(tmp.name) / "t.db")
        p.start()
        self.addCleanup(p.stop)
        self.pedidos, self.banco = pedidos, banco
        self.ids = {}
        for c in ("PS-11", "PS-12", "PS-13"):
            pedidos.registrar_importacao(_payload(c), None, "nucleo_sem_servico")
            self.ids[c] = pedidos.id_sintetico(pedidos.buscar_pedido(c))
        self.sublote = [{"id": 5, "code": "#PS-5"}] + [{"id": i, "code": f"#{c}"} for c, i in self.ids.items()]

    def _pool(self):
        from nucleo import pool
        return {s["code"]: s["id"] for s in pool.listar_pool(incluir_sem_servico=True)}

    def test_falha_tecnica_num_pedido_nao_cria_a_rota_e_todos_ficam_no_pool(self):
        from nucleo.test_materializar import FakeVuupt
        vuupt = FakeVuupt(falhar_em={"PS-12"})
        with self.assertRaises(crd.RotaAdiada) as erro, self.assertLogs(crd.logger, "ERROR"):
            crd._com_servico(self.sublote, vuupt, {}, "Planejamento - 27/11/2026 - #1")
        self.assertIn("PS-12", str(erro.exception))
        pool = self._pool()
        self.assertEqual(set(pool), {"#PS-11", "#PS-12", "#PS-13"})   # ninguem saiu do pool
        for c in ("PS-11", "PS-12", "PS-13"):
            self.assertEqual(self.pedidos.buscar_pedido(c)["status"], self.banco.PEDIDO_ABERTO)
        # rodada seguinte, Vuupt de volta: a rota sai COMPLETA (quem ja tinha servico e adotado)
        vuupt.falhar_em = set()
        prontos = crd._com_servico(self.sublote, vuupt, {}, "Planejamento - 27/11/2026 - #1")
        self.assertEqual([s["code"] for s in prontos], ["#PS-5", "#PS-11", "#PS-12", "#PS-13"])
        self.assertTrue(all(s["id"] > 0 for s in prontos))
        self.assertEqual(len(vuupt.criados), 3)   # nenhum servico em dobro

    def test_recusa_permanente_tira_so_o_pedido_e_a_rota_sai(self):
        from nucleo.test_materializar import _VuuptQueRecusa
        with self.assertLogs(crd.logger, "ERROR") as logs:
            prontos = crd._com_servico(self.sublote, _VuuptQueRecusa({"PS-12": 422}), {}, "Planejamento - 27/11/2026 - #1")
        self.assertEqual([s["code"] for s in prontos], ["#PS-5", "#PS-11", "#PS-13"])
        self.assertTrue(any("PS-12" in m and "Status 422" in m for m in logs.output))

    def test_resumo_avisa_rota_nao_criada(self):
        frase = crd._frase_rotas_adiadas(["Planejamento - 27/11/2026 - #1 (PS-12: falha tecnica)"])
        self.assertIn("[ALERTA_ROTA_NAO_CRIADA]", frase)
        self.assertIn("#1", frase)
        self.assertEqual(crd._frase_rotas_adiadas([]), "")


if __name__ == "__main__":
    unittest.main()
