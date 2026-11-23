# -*- coding: utf-8 -*-
"""
test_expedir_pedidos.py

Regras puras da expedição na Stokki:
- serviço da VUUPT com códigos combinados ("PS-1, PS-2") vira um item por
  pedido (antes só o 1º era expedido);
- falha persistente volta pra fila uma vez por dia (antes saía pra sempre).
    py -3.11 -m unittest test_expedir_pedidos -v
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

import expedir_pedidos as ep


class TestDesmembrarCombinados(unittest.TestCase):
    def test_codigo_simples_fica_igual(self):
        s = {"id": 1, "code": "#PS-37189"}
        self.assertEqual(ep._desmembrar_combinados([s]), [s])

    def test_combinado_vira_um_item_por_pedido(self):
        s = {"id": 7, "code": "PS-37189, PS-37176, #PS-37175-R1", "status": "done"}
        itens = ep._desmembrar_combinados([s])
        self.assertEqual([i["code"] for i in itens], ["PS-37189", "PS-37176", "PS-37175-R1"])
        self.assertTrue(all(i["id"] == 7 and i["status"] == "done" for i in itens))
        self.assertEqual(s["code"], "PS-37189, PS-37176, #PS-37175-R1")   # original intacto


class TestFalhasBloqueadas(unittest.TestCase):
    def test_so_bloqueia_quem_falhou_nas_ultimas_24h(self):
        agora = datetime(2026, 9, 26, 10, 0, 0)
        falhas = {
            "PS-1": {"ultima_em": (agora - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")},
            "PS-2": {"ultima_em": (agora - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")},
            "PS-3": {"ultima_em": None},
        }
        self.assertEqual(set(ep._falhas_bloqueadas(falhas, agora)), {"PS-1"})


class TestItensWhatsappInsucesso(unittest.TestCase):
    EMBARCADORES = {10: {"nome": "Quatro Estrelas"}}

    def test_monta_codigo_destinatario_remetente_e_motivo(self):
        s = {"id": 1, "code": "#PS-1", "title": "Mercado Bom", "sender_id": 10,
             "failed_reason_id": None, "note": "Portão fechado"}
        self.assertEqual(ep._itens_whatsapp_insucesso([s], self.EMBARCADORES), [
            {"codigo": "#PS-1", "destinatario": "Mercado Bom", "remetente": "Quatro Estrelas",
             "motivo": "Portão fechado"}])

    def test_sem_observacao_usa_o_texto_do_motivo_e_remetente_sem_cadastro_fica_vazio(self):
        s = {"id": 2, "code": "PS-2", "title": "Padaria", "sender_id": 99, "failed_reason_id": None}
        item = ep._itens_whatsapp_insucesso([s], self.EMBARCADORES)[0]
        self.assertEqual(item["remetente"], "")
        self.assertEqual(item["motivo"], ep.texto_do_motivo(None))


class TestSoltarDaOe(unittest.TestCase):
    """--tela-pedidos (Hugo, 12/11): pedido preso em Expedicao Programada e solto e expedido."""

    def _rodar(self, situacoes, liberar, expedir="expedido"):
        from stokki import expedicao_programada
        with mock.patch.object(ep, "_situacao_na_stokki", side_effect=situacoes), \
                mock.patch.object(ep.time, "sleep"), \
                mock.patch.object(expedicao_programada, "liberar_pedido", return_value=liberar) as lib, \
                mock.patch.object(ep, "expedir_na_stokki", return_value=expedir) as exp:
            return ep._soltar_da_oe_e_expedir(object(), "PS-43333-R1"), lib, exp

    def test_situacao_que_nao_e_aguardando_transportador_nao_mexe_em_oe(self):
        (resultado, nota), lib, exp = self._rodar(["Enviado"], {})
        self.assertEqual((resultado, nota), ("ja_expedido", ""))
        lib.assert_not_called()
        exp.assert_not_called()

    def test_oe_finalizada_e_pedido_enviado_conta_como_expedido(self):
        (resultado, nota), lib, exp = self._rodar(["Aguardando Transportador", "Enviado"],
                                                 {"acao": "oe_finalizada", "oe": 17424, "erro": ""})
        self.assertEqual((resultado, nota), ("expedido", "OE-17424 finalizada"))
        self.assertEqual(lib.call_args.args[1], "43333")   # id da Stokki, nao o sufixo da reentrega
        exp.assert_not_called()

    def test_espera_a_stokki_virar_o_pedido_depois_de_finalizar_a_oe(self):
        ag = "Aguardando Transportador"
        (resultado, _), _, _ = self._rodar([ag, ag, ag, "Enviado"], {"acao": "oe_finalizada", "oe": 17424, "erro": ""})
        self.assertEqual(resultado, "expedido")
        (resultado, nota), _, _ = self._rodar([ag] * 13, {"acao": "oe_finalizada", "oe": 17424, "erro": ""})
        self.assertEqual(resultado, "ja_expedido")
        self.assertIn("foi finalizada, mas o pedido segue", nota)

    def test_removido_da_oe_expede_em_seguida(self):
        (resultado, nota), _, exp = self._rodar(["Aguardando Transportador"],
                                               {"acao": "removido_da_oe", "oe": 17500, "erro": ""})
        self.assertEqual(resultado, "expedido")
        self.assertIn("OE-17500", nota)
        exp.assert_called_once()

    def test_sem_oe_e_falha_ficam_como_recusa_com_a_explicacao(self):
        (resultado, nota), _, _ = self._rodar(["Aguardando Transportador"], {"acao": "sem_oe", "oe": None, "erro": ""})
        self.assertEqual(resultado, "ja_expedido")
        self.assertIn("nenhuma Expedição Programada", nota)
        (resultado, nota), _, _ = self._rodar(["Aguardando Transportador"],
                                             {"acao": "falha", "oe": 17424, "erro": "Hora invalida"})
        self.assertEqual((resultado, nota), ("ja_expedido", "a OE-17424 não aceitou: Hora invalida"))


class TestAnotarResultado(unittest.TestCase):
    def test_guarda_pelo_codigo_base(self):
        with mock.patch.dict(ep._RESULTADOS, {}, clear=True):
            ep._anotar("#PS-43333-R1", True, "Expedido na Stokki")
            self.assertEqual(ep._RESULTADOS, {"PS-43333": {"ok": True, "mensagem": "Expedido na Stokki"}})


if __name__ == "__main__":
    unittest.main()
