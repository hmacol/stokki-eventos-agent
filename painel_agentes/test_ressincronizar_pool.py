# -*- coding: utf-8 -*-
"""
test_ressincronizar_pool.py

Depois de cancelar / reagendar / editar endereço na Vuupt pela tela, o
espelho do pedido é atualizado NA HORA (Entrega 1, Hugo 24/09) -- sem isso
o pool lido do núcleo ficava até 15 min atrasado.

    py -3.11 -m unittest painel_agentes.test_ressincronizar_pool -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import planejamento_rotas as pr  # noqa: E402


class TestRessincronizaDepoisDeEscrever(unittest.TestCase):
    def setUp(self):
        self.mock_vuupt = mock.Mock()
        self.mock_vuupt.buscar_servico_por_id.return_value = {"id": 111, "customer_id": None}
        p1 = mock.patch.object(pr, "VuuptClient", return_value=self.mock_vuupt)
        p2 = mock.patch.object(pr, "_carregar_config", return_value={"vuupt_api": {"token": "t"}, "google_maps": {}})
        p3 = mock.patch.object(pr, "_ressincronizar")
        p4 = mock.patch.object(pr.rascunhos_rota, "buscar_rascunho", return_value=None)
        self.ressinc = p3.start()
        for p in (p1, p2, p4):
            p.start()
        self.addCleanup(mock.patch.stopall)

    def test_cancelar_pedido(self):
        self.assertEqual(pr.cancelar_pedido(111), {"ok": True})
        self.ressinc.assert_called_once_with(self.mock_vuupt, [111])

    def test_reagendar_pedido(self):
        self.assertEqual(pr.reagendar_pedido(111, "2026-09-25", "08:00", "12:00"), {"ok": True})
        self.ressinc.assert_called_once_with(self.mock_vuupt, [111])

    def test_reagendar_pedidos_em_lote_ressincroniza_so_os_que_deram_certo(self):
        self.mock_vuupt.atualizar_servico.side_effect = [None, Exception("falhou"), None]
        r = pr.reagendar_pedidos([{"service_id": 1}, {"service_id": 2}, {"service_id": 3}], "2026-09-25", "08:00", "12:00")
        self.assertEqual([f["service_id"] for f in r["falhas"]], [2])
        self.ressinc.assert_called_once_with(self.mock_vuupt, [1, 3])

    def test_editar_endereco(self):
        with mock.patch("geocodificacao.geocodificar", return_value=None):
            self.assertEqual(pr.editar_endereco_pedido(111, "Rua Nova, 1"), {"ok": True})
        self.ressinc.assert_called_once_with(self.mock_vuupt, [111])

    def test_falha_na_vuupt_nao_ressincroniza(self):
        self.mock_vuupt.cancelar_servico.side_effect = pr.VuuptAPIError("500")
        self.assertFalse(pr.cancelar_pedido(111)["ok"])
        self.ressinc.assert_not_called()


class TestRessincronizarEmSegundoPlano(unittest.TestCase):
    def test_gancho_roda_fora_da_thread_da_requisicao(self):
        """Revisão 24/09: N GETs à Vuupt dentro da requisição (lote de 20
        reagendamentos) atrasavam a resposta e disputavam a cota de 429."""
        import threading
        visto = {}
        pronto = threading.Event()

        def falso(vuupt, ids, conn=None):
            visto["thread"] = threading.current_thread().name
            visto["ids"] = list(ids)
            pronto.set()

        with mock.patch("nucleo.sincronizar_servicos_vuupt.ressincronizar_ids", falso):
            pr._ressincronizar(object(), [1, 2])
            self.assertTrue(pronto.wait(5))
        self.assertEqual(visto["ids"], [1, 2])
        self.assertNotEqual(visto["thread"], threading.main_thread().name)


class TestSincronizarPoolAgora(unittest.TestCase):
    def test_pulado_quando_a_fonte_e_vuupt(self):
        self.assertEqual(pr.sincronizar_pool_agora({"planejamento": {"fonte_pool": "vuupt"}}), {"pulado": True})

    def test_roda_so_o_incremental_quando_a_fonte_e_nucleo(self):
        """Só o incremental (sem_pool, sem rotas): é o que o spec pede pro
        botão; a reconciliação completa fica com o timer de 15 min."""
        cfg = {"planejamento": {"fonte_pool": "nucleo"}, "vuupt_api": {"token": "t"}}
        conn = mock.Mock()
        with mock.patch.object(pr, "VuuptClient") as vc, \
             mock.patch("nucleo.banco.conectar", return_value=conn), \
             mock.patch("nucleo.sincronizar_servicos_vuupt.executar", return_value={"abertos": 3}) as ex:
            self.assertEqual(pr.sincronizar_pool_agora(cfg), {"abertos": 3})
            ex.assert_called_once_with(vc.return_value, conn, "t", sem_pool=True, limite_rotas=0)
        conn.close.assert_called_once()

    def test_nao_roda_duas_vezes_ao_mesmo_tempo(self):
        """Dois operadores clicando (ou o timer) não podem empilhar rodadas
        dentro do processo do painel: a segunda só lê o núcleo."""
        cfg = {"planejamento": {"fonte_pool": "nucleo"}, "vuupt_api": {"token": "t"}}
        with mock.patch.object(pr, "VuuptClient"), \
             mock.patch("nucleo.banco.conectar", return_value=mock.Mock()), \
             mock.patch("nucleo.sincronizar_servicos_vuupt.executar") as ex:
            pr._TRAVA_SINCRONIZACAO.acquire()
            try:
                self.assertEqual(pr.sincronizar_pool_agora(cfg), {"pulado": "em_andamento"})
            finally:
                pr._TRAVA_SINCRONIZACAO.release()
            ex.assert_not_called()


if __name__ == "__main__":
    unittest.main()
