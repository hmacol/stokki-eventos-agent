# -*- coding: utf-8 -*-
"""
test_pool_freshhub.py -- pool do planejamento lido da API do sistema novo (plano 1.2 do freshhub, Task 5):
com `planejamento.fonte_pool: freshhub` o pool vem de GET /api/pool; se a API cair, a rodada nao para
(volta pra Vuupt, loga e avisa por e-mail, uma vez por hora).

    py -3.11 -m unittest nucleo.test_pool_freshhub -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import pool  # noqa: E402
from nucleo.test_pool import _Base, _servico  # noqa: E402


def _config(com_api=True):
    cfg = {"planejamento": {"fonte_pool": "freshhub"}, "email": {}}
    if com_api:
        cfg["freshhub_api"] = {"url": "http://127.0.0.1:8100", "token": "fhs_teste"}
    return cfg


def _item_api(**extra):
    return {**_servico(), "estado": "aguardando_transporte", "prontidao": "aguardando_transporte",
            "marcas": {"dedicado": False}, "_fonte": "freshhub", "_origem": "LEGADO", **extra}


class TestFonteFreshhub(_Base):
    def test_fonte_freshhub_le_a_api_e_nao_vai_na_vuupt(self):
        vuupt = mock.Mock()
        item = _item_api()
        with mock.patch.object(pool.freshhub_client.FreshhubClient, "listar_pool", return_value=[item]):
            itens = pool.listar_pool_not_assigned(_config(), vuupt)
        self.assertEqual(itens, [item])
        vuupt.listar_servicos.assert_not_called()

    def test_api_fora_cai_pra_vuupt_loga_e_avisa(self):
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = [{"id": 2}]
        erro = pool.freshhub_client.ErroFreshhub("GET /api/pool: HTTP 503")
        with mock.patch.object(pool.freshhub_client.FreshhubClient, "listar_pool", side_effect=erro), \
                mock.patch.object(pool, "_alertar_freshhub_indisponivel") as alerta, \
                self.assertLogs("nucleo.pool", level="ERROR") as logs:
            itens = pool.listar_pool_not_assigned(_config(), vuupt)
        self.assertEqual(itens, [{"id": 2}])
        vuupt.listar_servicos.assert_called_once_with(
            [{"field": "status", "operator": "eq", "value": "not_assigned"}], per_page=100, include=["customer"])
        self.assertIn("pool.freshhub_indisponivel", logs.output[0])
        alerta.assert_called_once()

    def test_sem_freshhub_api_no_config_cai_pra_vuupt_e_avisa(self):
        vuupt = mock.Mock()
        vuupt.listar_servicos.return_value = []
        with mock.patch.object(pool, "_alertar_freshhub_indisponivel") as alerta, \
                self.assertLogs("nucleo.pool", level="ERROR"):
            pool.listar_pool_not_assigned(_config(com_api=False), vuupt)
        vuupt.listar_servicos.assert_called_once()
        self.assertIn("freshhub_api", alerta.call_args.args[1])

    def test_alerta_sai_uma_vez_por_hora(self):
        pool._ULTIMO_ALERTA.clear()
        try:
            with mock.patch("email_utils.enviar_email", return_value=True) as enviar:
                self.assertTrue(pool._alertar_freshhub_indisponivel(_config(), "HTTP 503"))
                self.assertFalse(pool._alertar_freshhub_indisponivel(_config(), "HTTP 503"))
            enviar.assert_called_once()
            destinos, assunto, corpo, _cfg_email = enviar.call_args.args
            self.assertEqual(destinos, ["hugo@freshlogbr.com"])
            self.assertIn("freshhub", assunto.lower())
            self.assertIn("HTTP 503", corpo)
        finally:
            pool._ULTIMO_ALERTA.clear()

    def test_item_da_api_vira_o_mesmo_item_da_tela_que_o_servico_da_vuupt(self):
        # mesma tecnica de test_pool.TestEquivalenciaComATela: o item da API tem as chaves do servico da Vuupt
        extra = str(_RAIZ / "painel_agentes")
        ja_tinha = extra in sys.path
        if not ja_tinha:
            sys.path.append(extra)
        try:
            import planejamento_rotas
        finally:
            if not ja_tinha:
                sys.path.remove(extra)
        remetentes = {11: "BRAZO"}
        esperado = planejamento_rotas._servico_para_pool(_servico(), remetentes)
        self.assertEqual(planejamento_rotas._servico_para_pool(_item_api(), remetentes), esperado)


if __name__ == "__main__":
    unittest.main()
