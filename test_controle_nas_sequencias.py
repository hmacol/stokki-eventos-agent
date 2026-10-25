# -*- coding: utf-8 -*-
"""Os envios que moram dentro das sequencias (insucesso na expedicao,
agendamento pendente/dia fixo na roteirizacao, solicitacao de agendamento e de
redespacho no pipeline) obedecem a tela de Notificacoes
(controle_notificacoes.py): a tela vale por cima do config; sem linha na tela
vale o config de hoje. O fora do dia fixo tem o proprio arquivo
(roteirizacao/test_controle_fora_dia_fixo.py).

Rodar (da raiz): py -3.11 -m unittest test_controle_nas_sequencias -v
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "insucesso_entrega"))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import agendamento_confirmacao as ag_conf
import controle_notificacoes as cn
import preferencias_notificacao as pn
import redespacho_confirmacao as red_conf

import notificar_insucesso_aguardando_resposta as insucesso
import notificar_agendamento_pendente as ag_pendente
import notificar_agendamento_dia_fixo as ag_dia_fixo

HUGO = "hugo@freshlogbr.com"
CNPJ_A = "11111111000111"
CONFIG_RESPOSTA = {"url_base": "https://exemplo.test/insucesso", "token_secret": "segredo-de-teste"}


class _ComControle(unittest.TestCase):
    """Banco temporario com um embarcador (sender 101, e-mail proprio) e as
    tabelas do controle."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
            "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)")
        conn.execute("INSERT INTO interno VALUES (?,?,?,?,?,?,?)",
                     (CNPJ_A, "Alfa LTDA", "Alfa", "cadastro@alfa.com", 1, 101, 9001))
        conn.commit()
        pn.salvar(conn, CNPJ_A, ["avisos@alfa.com"], {}, "cliente")
        conn.close()
        patcher = patch.object(pn, "DB_PATH", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _controle(self, fn, *args):
        conn = cn.conectar(self.db)
        try:
            fn(conn, *args, "teste")
        finally:
            conn.close()

    def _estado(self, tipo, estado):
        self._controle(cn.definir_estado, tipo, estado)

    def _piloto(self, email=HUGO, whatsapp=""):
        self._controle(cn.definir_piloto, email, whatsapp)


class TestInsucesso(_ComControle):
    def _pendentes(self):
        return [{"id": 1, "code": "#PS-1", "sender_id": 101, "failed_reason_id": 7, "title": "Cliente A"}]

    def _rodar(self, **kwargs):
        with patch.object(insucesso, "enviar_email", return_value=True) as enviar, \
             patch("fingerprint_aguardando_resposta.marcar_notificado") as marcar, \
             patch.object(insucesso.tratativas, "registrar_evento") as registrar:
            resultado = insucesso.notificar_remetentes(
                self._pendentes(), {}, CONFIG_RESPOSTA, db_path=self.db, **kwargs)
        return resultado, enviar, marcar, registrar

    def test_insucesso_tela_ligado_com_mestra_desligada_envia(self):
        self._estado("insucesso", "LIGADO")
        resultado, enviar, marcar, _ = self._rodar(mestra=False)
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(enviar.call_args.args[0], ["avisos@alfa.com"])
        self.assertEqual(marcar.call_count, 1)

    def test_insucesso_sem_linha_na_tela_vale_a_mestra(self):
        resultado, enviar, marcar, _ = self._rodar(mestra=False)
        self.assertEqual(resultado["enviados"], 0)
        enviar.assert_not_called()
        marcar.assert_not_called()
        resultado, enviar, _, _ = self._rodar(mestra=True)
        self.assertEqual(resultado["enviados"], 1)

    def test_insucesso_tela_desligado_nao_envia_nem_marca(self):
        self._estado("insucesso", "DESLIGADO")
        resultado, enviar, marcar, registrar = self._rodar()
        self.assertEqual(resultado["enviados"], 0)
        enviar.assert_not_called()
        marcar.assert_not_called()
        registrar.assert_not_called()

    def test_insucesso_excecao_bloqueado_nao_envia(self):
        self._estado("insucesso", "LIGADO")
        self._controle(cn.definir_excecao, "insucesso", CNPJ_A, "BLOQUEADO")
        resultado, enviar, _, _ = self._rodar()
        self.assertEqual(resultado["enviados"], 0)
        enviar.assert_not_called()

    def test_insucesso_piloto_nao_marca_notificado(self):
        self._estado("insucesso", "PILOTO")
        self._piloto()
        resultado, enviar, marcar, registrar = self._rodar()
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(enviar.call_args.args[0], [HUGO])
        self.assertIn("Piloto: iria para avisos@alfa.com", enviar.call_args.args[2])
        marcar.assert_not_called()
        registrar.assert_not_called()

    def test_insucesso_modo_teste_ignora_a_tela(self):
        self._estado("insucesso", "DESLIGADO")
        resultado, enviar, marcar, _ = self._rodar(modo_teste=True)
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(enviar.call_args.args[0], [HUGO])
        marcar.assert_not_called()

    def test_insucesso_modo_teste_com_mestra_desligada_nao_envia_como_hoje(self):
        resultado, enviar, _, _ = self._rodar(modo_teste=True, mestra=False)
        self.assertEqual(resultado["enviados"], 0)
        enviar.assert_not_called()

    def test_insucesso_botao_da_torre_ignora_a_tela(self):
        self._estado("insucesso", "DESLIGADO")
        resultado, enviar, _, _ = self._rodar(ignorar_preferencia=True)
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(enviar.call_args.args[0], ["avisos@alfa.com"])


class TestAgendamentoPendente(_ComControle):
    def _pendentes(self):
        return [{"id": 1, "code": "#PS-1", "sender_id": 101, "title": "Cliente A"}]

    def _rodar(self, **kwargs):
        with patch.object(ag_pendente, "enviar_email", return_value=True) as enviar, \
             patch.object(ag_pendente, "_registrar_pendentes") as registrar:
            resultado = ag_pendente.notificar_remetentes(self._pendentes(), {}, db_path=self.db, **kwargs)
        return resultado, enviar, registrar

    def test_agendamento_pendente_desligado_nao_envia(self):
        self._estado("agendamento", "DESLIGADO")
        resultado, enviar, registrar = self._rodar()
        self.assertEqual(resultado["enviados"], 0)
        enviar.assert_not_called()
        registrar.assert_not_called()

    def test_agendamento_pendente_tela_ligado_com_mestra_desligada_envia(self):
        self._estado("agendamento", "LIGADO")
        resultado, enviar, registrar = self._rodar(mestra=False)
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(registrar.call_count, 1)

    def test_agendamento_pendente_sem_linha_na_tela_vale_a_mestra(self):
        resultado, enviar, _ = self._rodar(mestra=False)
        self.assertEqual(resultado["enviados"], 0)
        enviar.assert_not_called()

    def test_agendamento_pendente_piloto_manda_pro_piloto_sem_registrar(self):
        self._estado("agendamento", "PILOTO")
        self._piloto()
        resultado, enviar, registrar = self._rodar()
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(enviar.call_args.args[0], [HUGO])
        self.assertIn("Piloto: iria para avisos@alfa.com", enviar.call_args.args[2])
        registrar.assert_not_called()


class TestAgendamentoDiaFixo(_ComControle):
    def _itens(self):
        return [{"servico": {"code": "#PS-1", "sender_id": 101, "title": "Cliente"},
                 "regiao": "Sorocaba", "dias": [1], "data": date(2026, 9, 22)}]

    def _rodar(self, **kwargs):
        with patch.object(ag_dia_fixo, "enviar_email", return_value=True) as enviar:
            resultado = ag_dia_fixo.notificar_agendamentos_dia_fixo(self._itens(), {}, db_path=self.db, **kwargs)
        return resultado, enviar

    def test_dia_fixo_piloto_vai_pro_piloto(self):
        self._estado("agendamento", "PILOTO")
        self._piloto("piloto@freshlogbr.com")
        resultado, enviar = self._rodar()
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(enviar.call_args.args[0], ["piloto@freshlogbr.com"])
        self.assertIn("Piloto: iria para avisos@alfa.com", enviar.call_args.args[2])

    def test_dia_fixo_desligado_nao_envia(self):
        self._estado("agendamento", "DESLIGADO")
        resultado, enviar = self._rodar()
        self.assertEqual(resultado["enviados"], 0)
        enviar.assert_not_called()

    def test_dia_fixo_tela_ligado_com_mestra_desligada_envia(self):
        self._estado("agendamento", "LIGADO")
        resultado, enviar = self._rodar(mestra=False)
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(enviar.call_args.args[0], ["avisos@alfa.com"])

    def test_dia_fixo_sem_linha_na_tela_vale_a_mestra(self):
        resultado, enviar = self._rodar(mestra=False)
        self.assertEqual(resultado["enviados"], 0)
        enviar.assert_not_called()

    def test_dia_fixo_modo_teste_ignora_a_tela(self):
        self._estado("agendamento", "DESLIGADO")
        resultado, enviar = self._rodar(modo_teste=True)
        self.assertEqual(resultado["enviados"], 1)
        self.assertEqual(enviar.call_args.args[0], [HUGO])


class TestSolicitacoes(_ComControle):
    """agendamento_confirmacao.enviar_solicitacao e redespacho_confirmacao.
    enviar_solicitacao: sem linha na tela envia como hoje (fallback ENVIAR)."""

    def _agendamento(self, **kw):
        with patch.object(ag_conf, "enviar_email", return_value=True) as enviar, \
             patch.object(ag_conf, "_pode_notificar", return_value=(True, "")), \
             patch.object(ag_conf, "_registrar_solicitacao") as registrar:
            retorno = ag_conf.enviar_solicitacao("PS-1", "Dest", "123", "99999999000199", CNPJ_A,
                                                 "avisos@alfa.com", {}, db_path=self.db, **kw)
        return retorno, enviar, registrar

    def _redespacho(self, **kw):
        with patch.object(red_conf, "enviar_email", return_value=True) as enviar, \
             patch.object(red_conf, "_pode_notificar", return_value=(True, "")), \
             patch.object(red_conf, "_registrar_solicitacao") as registrar:
            retorno = red_conf.enviar_solicitacao("PS-1", "Dest", "Campinas", "SP", "Transp",
                                                  "avisos@alfa.com", {}, cnpj_emb=CNPJ_A, db_path=self.db, **kw)
        return retorno, enviar, registrar

    def test_solicitacao_agendamento_sem_linha_envia_como_hoje(self):
        (enviado, motivo), enviar, registrar = self._agendamento()
        self.assertTrue(enviado)
        self.assertEqual(enviar.call_args.args[0], ["avisos@alfa.com"])
        self.assertEqual(registrar.call_count, 1)

    def test_solicitacao_agendamento_desligado_nao_envia(self):
        self._estado("agendamento", "DESLIGADO")
        (enviado, motivo), enviar, registrar = self._agendamento()
        self.assertFalse(enviado)
        self.assertIn("tela", motivo)
        enviar.assert_not_called()
        registrar.assert_not_called()

    def test_solicitacao_agendamento_piloto_vai_pro_piloto_sem_registrar(self):
        self._estado("agendamento", "PILOTO")
        self._piloto()
        (enviado, motivo), enviar, registrar = self._agendamento()
        self.assertEqual(enviar.call_args.args[0], [HUGO])
        self.assertIn("Piloto: iria para avisos@alfa.com", enviar.call_args.args[2])
        self.assertNotIn("[[PEDIDO:", enviar.call_args.args[2])
        registrar.assert_not_called()
        self.assertFalse(enviado)   # o embarcador ainda nao foi perguntado

    def test_solicitacao_agendamento_sem_email_continua_sem_email(self):
        with patch.object(ag_conf, "enviar_email", return_value=True) as enviar:
            enviado, _ = ag_conf.enviar_solicitacao("PS-1", "D", "1", "9", CNPJ_A, "", {}, db_path=self.db)
        self.assertFalse(enviado)
        enviar.assert_not_called()

    def test_solicitacao_redespacho_sem_linha_envia_como_hoje(self):
        (enviado, _), enviar, registrar = self._redespacho()
        self.assertTrue(enviado)
        self.assertEqual(enviar.call_args.args[0], ["avisos@alfa.com"])
        self.assertEqual(registrar.call_count, 1)

    def test_redespacho_bloqueado_nao_envia(self):
        self._estado("redespacho", "LIGADO")
        self._controle(cn.definir_excecao, "redespacho", CNPJ_A, "BLOQUEADO")
        (enviado, motivo), enviar, registrar = self._redespacho()
        self.assertFalse(enviado)
        self.assertIn("tela", motivo)
        enviar.assert_not_called()
        registrar.assert_not_called()

    def test_redespacho_piloto_vai_pro_piloto_sem_registrar(self):
        self._estado("redespacho", "PILOTO")
        self._piloto()
        _, enviar, registrar = self._redespacho()
        self.assertEqual(enviar.call_args.args[0], [HUGO])
        self.assertIn("Piloto: iria para avisos@alfa.com", enviar.call_args.args[2])
        registrar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
