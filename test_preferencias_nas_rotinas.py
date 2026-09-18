"""As rotinas que mandam e-mail ao embarcador respeitam as preferencias do
portal (preferencias_notificacao.py): tipo desligado nao envia, nao marca
como notificado e conta em "desligados"; o e-mail de notificacoes vence o
do cadastro.

Rodar: py -3.11 -m unittest test_preferencias_nas_rotinas
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "insucesso_entrega"))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import preferencias_notificacao as pn

import notificar_insucesso_aguardando_resposta as insucesso
import notificar_agendamento_pendente as ag_pendente
import notificar_agendamento_dia_fixo as ag_dia_fixo
import notificar_pedidos_em_espera as em_espera
import aplicar_resposta_insucesso as aplicar_resposta

CNPJ_LIGADO = "11111111000111"
CNPJ_DESLIGADO = "22222222000122"
CONFIG_RESPOSTA = {"url_base": "https://exemplo.test/insucesso", "token_secret": "segredo-de-teste"}


class _ComBanco(unittest.TestCase):
    """Banco temporario com 2 embarcadores: 101/9001 (ligado, com e-mail de
    notificacoes proprio) e 102/9002 (desligou o `TIPO` do teste)."""

    TIPO = ""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, nome_remetente TEXT, apelido TEXT, "
            "email TEXT, notificar_email INTEGER NOT NULL DEFAULT 1, sender_id INTEGER, stkkc_id INTEGER)"
        )
        conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?,?)", [
            (CNPJ_LIGADO, "Alfa LTDA", "Alfa", "cadastro@alfa.com", 1, 101, 9001),
            (CNPJ_DESLIGADO, "Beta SA", "Beta", "cadastro@beta.com", 1, 102, 9002),
        ])
        conn.commit()
        pn.salvar(conn, CNPJ_LIGADO, ["avisos@alfa.com"], {}, "cliente")
        pn.salvar(conn, CNPJ_DESLIGADO, [], {self.TIPO: False}, "cliente")
        conn.close()

        patcher = patch.object(pn, "DB_PATH", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class TestInsucesso(_ComBanco):
    TIPO = "insucesso"

    def _pendentes(self):
        return [
            {"id": 1, "code": "#PS-1", "sender_id": 101, "failed_reason_id": 7, "title": "Cliente A"},
            {"id": 2, "code": "#PS-2", "sender_id": 102, "failed_reason_id": 7, "title": "Cliente B"},
        ]

    def _rodar(self, **kwargs):
        with patch.object(insucesso, "enviar_email", return_value=True) as enviar, \
             patch("fingerprint_aguardando_resposta.marcar_notificado") as marcar, \
             patch.object(insucesso.tratativas, "registrar_evento") as registrar:
            resultado = insucesso.notificar_remetentes(self._pendentes(), {}, CONFIG_RESPOSTA, **kwargs)
        return resultado, enviar, marcar, registrar

    def test_desligado_nao_envia_nem_marca_como_notificado(self):
        resultado, enviar, marcar, registrar = self._rodar()
        self.assertEqual(resultado, {"enviados": 1, "falhas": 0, "sem_email": 0, "desligados": 1})
        self.assertEqual([c.args[0] for c in enviar.call_args_list], [["avisos@alfa.com"]])
        self.assertEqual([c.args[0] for c in marcar.call_args_list], [1])
        self.assertEqual([c.args[0] for c in registrar.call_args_list], ["#PS-1"])

    def test_botao_manual_da_torre_ignora_a_chave(self):
        resultado, enviar, _, _ = self._rodar(ignorar_preferencia=True)
        self.assertEqual(resultado["enviados"], 2)
        self.assertEqual(resultado["desligados"], 0)
        self.assertIn(["cadastro@beta.com"], [c.args[0] for c in enviar.call_args_list])

    def test_confirmacao_da_resposta_usa_o_email_de_notificacoes_e_ignora_a_chave(self):
        self.assertEqual(aplicar_resposta._email_do_remetente(101), "avisos@alfa.com")
        self.assertEqual(aplicar_resposta._email_do_remetente(102), "cadastro@beta.com")
        self.assertEqual(aplicar_resposta._email_do_remetente(999), "")


class TestAgendamentoPendente(_ComBanco):
    TIPO = "agendamento"

    def test_desligado_nao_envia_nem_registra_solicitacao(self):
        pendentes = [
            {"id": 1, "code": "#PS-1", "sender_id": 101, "title": "Cliente A"},
            {"id": 2, "code": "#PS-2", "sender_id": 102, "title": "Cliente B"},
        ]
        with patch.object(ag_pendente, "enviar_email", return_value=True) as enviar, \
             patch.object(ag_pendente, "_registrar_pendentes") as registrar:
            resultado = ag_pendente.notificar_remetentes(pendentes, {})
        self.assertEqual(resultado, {"enviados": 1, "falhas": 0, "sem_email": 0, "desligados": 1})
        self.assertEqual([c.args[0] for c in enviar.call_args_list], [["avisos@alfa.com"]])
        self.assertEqual(len(registrar.call_args_list), 1)
        self.assertEqual(registrar.call_args.args[1]["cnpj"], CNPJ_LIGADO)


class TestAgendamentoDiaFixo(_ComBanco):
    TIPO = "agendamento"

    def test_desligado_nao_envia(self):
        def item(sender_id, code):
            return {"servico": {"code": code, "sender_id": sender_id, "title": "Cliente"},
                    "regiao": "Sorocaba", "dias": [1], "data": date(2026, 9, 22)}

        with patch.object(ag_dia_fixo, "enviar_email", return_value=True) as enviar:
            resultado = ag_dia_fixo.notificar_agendamentos_dia_fixo([item(101, "#PS-1"), item(102, "#PS-2")], {})
        self.assertEqual(resultado, {"enviados": 1, "falhas": 0, "sem_email": 0, "desligados": 1})
        self.assertEqual([c.args[0] for c in enviar.call_args_list], [["avisos@alfa.com"]])


class TestPedidosEmEspera(_ComBanco):
    TIPO = "pedidos_em_espera"

    def test_desligado_nao_envia(self):
        def pedido(stkkc_id, codigo):
            return {"codigo_ps": codigo, "id_stokki": 1, "data": "18/09/2026", "destino": "Cliente",
                    "marker": "Aguardando faturamento", "stkkc_id": stkkc_id}

        execucao = MagicMock()
        with patch.object(em_espera, "_carregar_config", return_value={"email": {}}), \
             patch.object(em_espera, "StokkiSession"), \
             patch.object(em_espera, "_buscar_pedidos_em_espera",
                          return_value=[pedido(9001, "#PS-1"), pedido(9002, "#PS-2")]), \
             patch.object(em_espera, "notificacao_pedidos_em_espera_ativa", return_value=True), \
             patch.object(em_espera, "notificar_execucao", execucao), \
             patch.object(em_espera, "enviar_email", return_value=True) as enviar:
            em_espera.main(modo_teste=False)

        self.assertEqual([c.args[0] for c in enviar.call_args_list], [["avisos@alfa.com"]])
        resumo = execucao.call_args.args[0]["Notificador de Faturamento"]
        self.assertEqual(resumo["status"], "ok")
        self.assertIn("1 desligado(s) pelo cliente", resumo["detalhe"])


if __name__ == "__main__":
    unittest.main()
