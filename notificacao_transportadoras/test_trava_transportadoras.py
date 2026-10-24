# -*- coding: utf-8 -*-
"""
Trava própria do e-mail às transportadoras (Hugo, 23/10): ligar a
chave-mestra notificacoes_automaticas.ativo pros embarcadores NÃO pode
ligar junto o envio às transportadoras. Envio real exige também
notificacao_transportadoras.ativo: true (ausente = desligado).

Rodar (da raiz):
    py -3.11 -m unittest notificacao_transportadoras.test_trava_transportadoras -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import email_utils
import notificar_transportadoras as nt


class TravaTransportadorasTestCase(unittest.TestCase):

    def test_ausente_e_desligada(self):
        self.assertFalse(email_utils.notificacao_transportadoras_ativa({}))
        self.assertFalse(email_utils.notificacao_transportadoras_ativa({"notificacao_transportadoras": {}}))

    def test_ligada_so_com_true_explicito(self):
        self.assertTrue(email_utils.notificacao_transportadoras_ativa(
            {"notificacao_transportadoras": {"ativo": True}}))

    def _rodar_main(self, config, modo_teste=False):
        grupos = {"transp": ["PS-1"]}
        with mock.patch.object(nt, "_carregar_config", return_value=config), \
             mock.patch.object(nt.CatalogoTransportadoras, "carregar", return_value=mock.Mock()), \
             mock.patch.object(nt, "buscar_rotas_do_dia", return_value=[]), \
             mock.patch.object(nt, "identificar_pedidos_transportadora", return_value=(grupos, {}, {})), \
             mock.patch.object(nt, "_carregar_embarcadores_por_sender_id",
                               side_effect=AssertionError("passou da trava")) as embarcadores, \
             mock.patch.object(nt, "notificar_execucao") as notificar:
            nt.main(modo_teste=modo_teste, data_str="hoje")
        return embarcadores, notificar

    def test_mestra_ligada_sem_trava_propria_nao_envia(self):
        embarcadores, notificar = self._rodar_main({"notificacoes_automaticas": {"ativo": True}})
        embarcadores.assert_not_called()
        resumo = notificar.call_args[0][0]
        self.assertIn("notificacao_transportadoras.ativo", resumo["Resumo geral"]["detalhe"])

    def test_trava_propria_ligada_sem_mestra_nao_envia(self):
        embarcadores, _ = self._rodar_main({"notificacoes_automaticas": {"ativo": False},
                                            "notificacao_transportadoras": {"ativo": True}})
        embarcadores.assert_not_called()


if __name__ == "__main__":
    unittest.main()
