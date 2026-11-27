# -*- coding: utf-8 -*-
"""Testes das regras puras de conferir_pool.py (sem Vuupt nem Stokki)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import conferir_pool as cp


class TestCodigoBase(unittest.TestCase):
    def test_tira_cerquilha_e_sufixo_de_reentrega(self):
        self.assertEqual(cp.codigo_base("#PS-40426-R1"), "PS-40426")
        self.assertEqual(cp.codigo_base("PS-40426"), "PS-40426")
        self.assertEqual(cp.codigo_base("#PS-40426-R1-R2"), "PS-40426")

    def test_sem_codigo_reconhecivel(self):
        self.assertIsNone(cp.codigo_base("RETIRADA X"))
        self.assertIsNone(cp.codigo_base(None))


class TestDecidir(unittest.TestCase):
    def test_vuupt_e_stokki_concordam_cancela(self):
        self.assertEqual(cp.decidir("Enviado", vuupt_entregue=True, lalamove_entregue=False), (cp.CANCELAR, None))

    def test_lalamove_entregue_e_stokki_enviado_cancela(self):
        self.assertEqual(cp.decidir("Enviado", vuupt_entregue=False, lalamove_entregue=True), (cp.CANCELAR, None))

    def test_so_a_stokki_diz_enviado_avisa(self):
        acao, motivo = cp.decidir("Enviado", vuupt_entregue=False, lalamove_entregue=False)
        self.assertEqual(acao, cp.AVISAR)
        self.assertIn("Stokki", motivo)

    def test_cancelado_na_stokki_so_avisa(self):
        acao, motivo = cp.decidir("Cancelado", vuupt_entregue=False, lalamove_entregue=False)
        self.assertEqual(acao, cp.AVISAR)
        self.assertIn("ancelado", motivo)

    def test_aguardando_na_stokki_nada(self):
        self.assertEqual(cp.decidir("Aguardando Transportador", vuupt_entregue=False, lalamove_entregue=False),
                         (None, None))

    def test_entregue_na_vuupt_mas_stokki_aberta_nao_cancela(self):
        acao, _ = cp.decidir("Aguardando Transportador", vuupt_entregue=True, lalamove_entregue=False)
        self.assertNotEqual(acao, cp.CANCELAR)


class TestDuplicados(unittest.TestCase):
    def test_original_e_reentrega_no_pool(self):
        pool = [{"id": 1, "code": "#PS-40146"}, {"id": 2, "code": "#PS-40146-R1"}, {"id": 3, "code": "PS-40147"}]
        self.assertEqual(cp.duplicados_no_pool(pool), {"PS-40146": ["#PS-40146", "#PS-40146-R1"]})


class TestEntregueEmIrmao(unittest.TestCase):
    def test_outro_servico_concluido_com_sucesso(self):
        irmaos = [{"id": 9, "status": "done", "completed_at": "2026-10-01 13:53:18", "failed_reason_id": None}]
        self.assertTrue(cp.entregue_em_outro_servico(irmaos, proprio_id=1))

    def test_insucesso_nao_conta(self):
        irmaos = [{"id": 9, "status": "done", "completed_at": "2026-09-30 13:00:20", "failed_reason_id": 5744}]
        self.assertFalse(cp.entregue_em_outro_servico(irmaos, proprio_id=1))

    def test_o_proprio_servico_nao_conta(self):
        irmaos = [{"id": 1, "status": "done", "completed_at": "2026-10-01", "failed_reason_id": None}]
        self.assertFalse(cp.entregue_em_outro_servico(irmaos, proprio_id=1))


class TestTextoWhatsapp(unittest.TestCase):
    def test_cabe_em_200(self):
        avisos = [{"codigo": f"PS-{40000 + i}"} for i in range(30)]
        texto = cp.texto_whatsapp(cancelados=[{"codigo": "PS-1"}] * 3, avisos=avisos, duplicados={"PS-2": ["a", "b"]})
        self.assertLessEqual(len(texto), 200)
        self.assertIn("3", texto)


class TestCancelarJaEntregue(unittest.TestCase):
    """Fonte stokki: pedido do pool sem servico na Vuupt (id negativo) ja entregue."""

    def setUp(self):
        import tempfile
        from unittest import mock
        from nucleo import banco, pedidos
        self.banco, self.pedidos, self.mock = banco, pedidos, mock
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        mock.patch.object(banco, "DB_PATH", Path(tmp.name) / "t.db").start()
        self.addCleanup(mock.patch.stopall)
        self.tirar = mock.patch.object(cp, "tirar_dos_rascunhos", return_value=[7]).start()

    def test_conferir_pool_entregue_sem_servico_cancela_local_e_nunca_materializa(self):
        from nucleo import pool
        conn = self.banco.conectar()
        try:
            # como o pipeline grava com a chave ligada (so linha que nasceu sem servico entra no pool)
            self.pedidos.registrar_importacao({"code": "#PS-40426", "title": "x"}, None, "nucleo_sem_servico",
                                              conn=conn)
        finally:
            conn.close()
        [s] = pool.listar_pool(incluir_sem_servico=True)
        self.assertTrue(pool.eh_sem_servico(s["id"]))
        vuupt = self.mock.Mock()
        self.assertEqual(cp.cancelar_ja_entregue(vuupt, s), [7])
        self.assertEqual(vuupt.mock_calls, [])                    # nunca chama a Vuupt com id negativo
        self.tirar.assert_called_once_with(s["id"])               # sai do rascunho: nao materializa no envio
        self.assertEqual(self.pedidos.buscar_pedido("PS-40426")["status"], self.banco.PEDIDO_CANCELADO)
        self.assertEqual(pool.listar_pool(incluir_sem_servico=True), [])   # sai do pool

    def test_sem_servico_ja_fechado_nao_conta_como_cancelado(self):
        from nucleo import pool
        conn = self.banco.conectar()
        try:
            # como o pipeline grava com a chave ligada (so linha que nasceu sem servico entra no pool)
            self.pedidos.registrar_importacao({"code": "#PS-40426", "title": "x"}, None, "nucleo_sem_servico",
                                              conn=conn)
        finally:
            conn.close()
        [s] = pool.listar_pool(incluir_sem_servico=True)
        self.pedidos.cancelar_sem_servico("PS-40426", "outro caminho", self.banco.ORIGEM_PIPELINE)
        vuupt = self.mock.Mock()
        self.assertIsNone(cp.cancelar_ja_entregue(vuupt, s))     # None = main nao conta nem loga CANCELADO
        self.assertEqual(vuupt.mock_calls, [])
        self.tirar.assert_called_once_with(s["id"])              # mesmo assim a parada nao fica

    def test_com_servico_cancela_na_vuupt_como_hoje(self):
        vuupt = self.mock.Mock()
        self.assertEqual(cp.cancelar_ja_entregue(vuupt, {"id": 55, "code": "#PS-1"}), [7])
        vuupt.cancelar_servico_oficial.assert_called_once_with(55)
        self.tirar.assert_called_once_with(55)

    def test_falha_na_vuupt_nao_tira_do_rascunho(self):
        from vuupt_client import VuuptAPIError
        vuupt = self.mock.Mock()
        vuupt.cancelar_servico_oficial.side_effect = VuuptAPIError("500")
        self.assertIsNone(cp.cancelar_ja_entregue(vuupt, {"id": 55, "code": "#PS-1"}))
        self.tirar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
