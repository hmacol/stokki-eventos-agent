# -*- coding: utf-8 -*-
"""
test_controle_faltas.py

O relatorio de faltas do recebimento (wms_faltas_recebimento.notificar_faltas)
obedece a tela de Notificacoes (controle_notificacoes.py): a tela vale por
cima do config; sem linha na tela vale o config de hoje. Modo teste ignora a
tela. Nenhum teste manda e-mail de verdade.

Rodar (de dentro de painel_agentes/):
    py -3.11 -m unittest test_controle_faltas -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import controle_notificacoes as cn  # noqa: E402
import test_wms_faltas_recebimento as base  # noqa: E402
import wms_pedidos  # noqa: E402

HUGO = "hugo@freshlogbr.com"
CNPJ = base.CNPJ


class TestFaltasObedecemATela(base.Base):
    def setUp(self):
        super().setUp()
        # recebimento encerrado com divergencia, pronto pra notificar
        self._enderecar(1, 6)
        self._enderecar(2, 0.5)
        wms_pedidos.encerrar_com_divergencia(self.conn, self.rid, "avaria", operador={"nome": "Jonas"})
        # config de envio real (sem forcar_destino): a tela e quem decide
        self.config = dict(self.config, notificacao_faltas_recebimento={"forcar_destino": ""})

    def _controle(self, fn, *args):
        c = cn.conectar(self.db)
        try:
            fn(c, *args, "teste")
        finally:
            c.close()

    def test_faltas_tela_desligado_nao_envia(self):
        self._controle(cn.definir_estado, "faltas_recebimento", "DESLIGADO")
        r, envio = self._notificar()
        self.assertFalse(r["enviado"])
        self.assertEqual(r["motivo"], "bloqueado_pela_tela")
        self.assertEqual(envio.chamadas, [])

    def test_faltas_geral_desligado_nao_envia_mesmo_com_tipo_ligado(self):
        self._controle(cn.definir_estado, "faltas_recebimento", "LIGADO")
        self._controle(cn.definir_geral, False)
        r, envio = self._notificar()
        self.assertEqual(r["motivo"], "bloqueado_pela_tela")
        self.assertEqual(envio.chamadas, [])

    def test_faltas_excecao_bloqueado_nao_envia(self):
        self._controle(cn.definir_estado, "faltas_recebimento", "LIGADO")
        self._controle(cn.definir_excecao, "faltas_recebimento", CNPJ, "BLOQUEADO")
        r, envio = self._notificar()
        self.assertEqual(r["motivo"], "bloqueado_pela_tela")
        self.assertEqual(envio.chamadas, [])

    def test_faltas_piloto_sem_copia_interna(self):
        self._controle(cn.definir_estado, "faltas_recebimento", "PILOTO")
        r, envio = self._notificar()
        self.assertTrue(r["enviado"], r["motivo"])
        self.assertTrue(r["redirecionado"])
        self.assertEqual(envio.chamadas[0]["para"], [HUGO])
        self.assertEqual(envio.chamadas[0]["cc"], [])
        self.assertIn("Redirecionado (piloto)", envio.chamadas[0]["corpo"])
        self.assertIn("compras@mariadolores.com.br", envio.chamadas[0]["corpo"])

    def test_faltas_piloto_usa_o_destino_do_piloto_da_tela(self):
        self._controle(cn.definir_estado, "faltas_recebimento", "PILOTO")
        self._controle(cn.definir_piloto, "outro@freshlogbr.com", "")
        r, envio = self._notificar()
        self.assertEqual(envio.chamadas[0]["para"], ["outro@freshlogbr.com"])

    def test_faltas_piloto_com_excecao_liberado_envia_pro_cliente(self):
        self._controle(cn.definir_estado, "faltas_recebimento", "PILOTO")
        self._controle(cn.definir_excecao, "faltas_recebimento", CNPJ, "LIBERADO")
        r, envio = self._notificar()
        self.assertFalse(r["redirecionado"])
        self.assertEqual(envio.chamadas[0]["para"],
                         ["compras@mariadolores.com.br", "fiscal@mariadolores.com.br"])
        self.assertEqual(envio.chamadas[0]["cc"], ["entregas@freshlogbr.com"])

    def test_faltas_tela_ligado_manda_pro_cliente_mesmo_com_config_redirecionando(self):
        self._controle(cn.definir_estado, "faltas_recebimento", "LIGADO")
        config = dict(self.config, notificacao_faltas_recebimento={"forcar_destino": HUGO},
                      notificacoes_automaticas={"ativo": False})
        r, envio = self._notificar(config=config)
        self.assertTrue(r["enviado"], r["motivo"])
        self.assertFalse(r["redirecionado"])
        self.assertEqual(envio.chamadas[0]["para"],
                         ["compras@mariadolores.com.br", "fiscal@mariadolores.com.br"])
        self.assertEqual(envio.chamadas[0]["cc"], ["entregas@freshlogbr.com"])

    def test_faltas_sem_linha_na_tela_vale_o_config(self):
        config = dict(self.config, notificacoes_automaticas={"ativo": False})
        r, envio = self._notificar(config=config)
        self.assertEqual(r["motivo"], "desativado")
        self.assertEqual(envio.chamadas, [])
        r, envio = self._notificar()   # chave mestra ligada: envio real
        self.assertTrue(r["enviado"], r["motivo"])
        self.assertFalse(r["redirecionado"])

    def test_faltas_modo_teste_ignora_a_tela(self):
        self._controle(cn.definir_estado, "faltas_recebimento", "DESLIGADO")
        self._controle(cn.definir_geral, False)
        r, envio = self._notificar(modo_teste=True)
        self.assertTrue(r["enviado"], r["motivo"])
        self.assertEqual(envio.chamadas[0]["para"], [HUGO])
        self.assertEqual(envio.chamadas[0]["cc"], [])


if __name__ == "__main__":
    unittest.main()
