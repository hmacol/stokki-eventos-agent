# -*- coding: utf-8 -*-
"""Testes do e-mail diario "entregas de hoje" por embarcador.

  py -3.11 -m unittest test_notificar_nfs_em_rota
"""
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import notificar_nfs_em_rota as mod
from portal_cliente import dados_cliente

DATA = date(2026, 9, 17)
SENDER_A, SENDER_B = 111, 222


def _servico(sid, code, sender_id, status="assigned", nome="Mercado X", **extra):
    return {"id": sid, "code": code, "sender_id": sender_id, "status": status,
            "address": "Rua A 10, Centro, Sao Paulo - SP, 01000-000, Brasil",
            "customer": {"data": {"name": nome}}, **extra}


def _rota(rid, numero, servicos, status="assigned", agente="Joao"):
    return {"id": rid, "name": f"Planejamento - 17/09/2026 - #{numero}", "status": status,
            "agent_id": None, "agent": {"data": {"name": agente}},
            "start_at": "2026-09-17 06:00:00", "services": {"data": servicos}}


ROTAS = [
    _rota(1, 12, [
        _servico(10, "#PS-100", SENDER_A),
        _servico(11, "#PS-200", SENDER_B),
        _servico(12, "#PS-101", SENDER_A, status="on_route"),
        _servico(13, "#PS-102", SENDER_A, status="canceled"),
        _servico(14, "#PS-103", SENDER_A, status="done", status_done="success"),
    ]),
    _rota(2, 3, [_servico(20, "#PS-104-R1", SENDER_A, nome="Padaria <b>Y</b>")], agente="Maria"),
    _rota(3, 9, [_servico(30, "#PS-105", SENDER_A)], status="canceled"),
]
NFS = {"PS-100": "5001", "PS-101": "5002", "PS-200": "9001"}


def _pedidos(sender_id):
    with mock.patch.object(dados_cliente, "nf_por_codigo", side_effect=lambda cods: {c: NFS[c] for c in cods if c in NFS}):
        return mod.pedidos_saindo_hoje(ROTAS, sender_id, {})


class TestPedidosSaindoHoje(unittest.TestCase):
    def test_so_do_embarcador_e_so_o_que_ainda_vai_sair(self):
        codigos = [p["codigo"] for p in _pedidos(SENDER_A)]
        # cancelado, entregue e rota cancelada ficam de fora; PS-200 e de outro embarcador
        self.assertEqual(sorted(codigos), ["#PS-100", "#PS-101", "#PS-104-R1"])

    def test_ordem_da_parada_ignora_cancelados(self):
        por_codigo = {p["codigo"]: p for p in _pedidos(SENDER_A)}
        self.assertEqual((por_codigo["#PS-101"]["ordem"], por_codigo["#PS-101"]["total_paradas"]), (3, 4))
        self.assertEqual(por_codigo["#PS-101"]["situacao"], "em_rota")
        self.assertEqual(por_codigo["#PS-100"]["situacao"], "programado")

    def test_nf_vem_de_documentos_processados(self):
        por_codigo = {p["codigo"]: p for p in _pedidos(SENDER_A)}
        self.assertEqual(por_codigo["#PS-100"]["nf"], "5001")
        self.assertEqual(por_codigo["#PS-104-R1"]["nf"], "")


class TestMontarEmail(unittest.TestCase):
    def test_sem_pedidos_nao_gera_email(self):
        self.assertIsNone(mod.montar_email("Cliente A", [], DATA))

    def test_assunto_conta_as_notas(self):
        assunto, _ = mod.montar_email("Cliente A", _pedidos(SENDER_A), DATA)
        self.assertIn("17/09", assunto)
        self.assertIn("3 notas", assunto)

    def test_assunto_no_singular(self):
        assunto, _ = mod.montar_email("Cliente B", _pedidos(SENDER_B), DATA)
        self.assertIn("1 nota ", assunto + " ")
        self.assertNotIn("1 notas", assunto)

    def test_corpo_agrupa_por_rota_na_ordem_da_parada(self):
        _, corpo = mod.montar_email("Cliente A", _pedidos(SENDER_A), DATA)
        self.assertLess(corpo.index("Rota 3"), corpo.index("Rota 12"))
        self.assertLess(corpo.index("#PS-100"), corpo.index("#PS-101"))
        self.assertIn("Joao", corpo)
        self.assertIn("Maria", corpo)
        self.assertIn("5002", corpo)

    def test_pedido_sem_nf_aparece_como_pendente(self):
        _, corpo = mod.montar_email("Cliente A", _pedidos(SENDER_A), DATA)
        self.assertIn("#PS-104-R1", corpo)
        self.assertIn("NF pendente", corpo)

    def test_escapa_html_de_dado_externo(self):
        _, corpo = mod.montar_email("Cliente <A>", _pedidos(SENDER_A), DATA)
        self.assertNotIn("<b>Y</b>", corpo)
        self.assertIn("Padaria &lt;b&gt;Y&lt;/b&gt;", corpo)
        self.assertIn("Cliente &lt;A&gt;", corpo)

    def test_redirecionado_cita_o_destino_original(self):
        _, corpo = mod.montar_email("Cliente A", _pedidos(SENDER_A), DATA, destino_original=["log@cliente.com"])
        self.assertIn("log@cliente.com", corpo)


class TestDestinos(unittest.TestCase):
    def test_modo_teste_vai_pro_email_de_teste(self):
        self.assertEqual(mod.resolver_destinos(["a@x.com"], True, ""), ([mod.EMAIL_TESTE], True))

    def test_forcar_destino_redireciona(self):
        self.assertEqual(mod.resolver_destinos(["a@x.com"], False, "piloto@f.com"), (["piloto@f.com"], True))

    def test_envio_real(self):
        self.assertEqual(mod.resolver_destinos(["a@x.com", "b@x.com"], False, ""), (["a@x.com", "b@x.com"], False))

    def test_sem_secao_no_config_redireciona_por_padrao(self):
        # envio real pra cliente so com forcar_destino: "" explicito no config.yaml
        self.assertEqual(mod.forcar_destino_do_config({}), mod.EMAIL_TESTE)
        self.assertEqual(mod.forcar_destino_do_config({"notificacao_nfs_em_rota": {"forcar_destino": ""}}), "")


class TestExecutar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT PRIMARY KEY, sender_id INTEGER, "
                     "nome_remetente TEXT, apelido TEXT, email TEXT, notificar_email INTEGER)")
        conn.executemany("INSERT INTO interno VALUES (?,?,?,?,?,?)", [
            ("1", SENDER_A, "Cliente A Ltda", "Cliente A", "log@a.com; fin@a.com", 1),
            ("2", SENDER_B, "Cliente B Ltda", None, "b@b.com", 0),       # notificar_email desligado
            ("3", 333, "Cliente C", None, "c@c.com", 1),                  # nada saindo hoje
            ("4", None, "Sem sender", None, "d@d.com", 1),
        ])
        conn.commit()
        conn.close()
        self.enviados = []
        self._patch = mock.patch.object(dados_cliente, "nf_por_codigo", return_value={})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _enviar(self, destinos, assunto, corpo, config_email, **kw):
        self.enviados.append((destinos, assunto))
        return True

    def _executar(self, config=None, **kw):
        config = config if config is not None else {"notificacao_nfs_em_rota": {"forcar_destino": ""}}
        return mod.executar(config, DATA, rotas=ROTAS, motoristas={}, enviar=self._enviar, db_path=self.db, **kw)

    def test_envia_so_pra_quem_tem_nota_e_notificar_ligado(self):
        r = self._executar()
        self.assertEqual(self.enviados, [(["log@a.com", "fin@a.com"], mock.ANY)])
        self.assertEqual((r["enviados"], r["falhas"]), (1, 0))

    def test_segunda_rodada_no_mesmo_dia_nao_repete(self):
        self._executar()
        r = self._executar()
        self.assertEqual(len(self.enviados), 1)
        self.assertEqual(r["ja_enviados"], 1)

    def test_modo_teste_redireciona_e_nao_marca_como_enviado(self):
        self._executar(modo_teste=True)
        self.assertEqual(self.enviados[0][0], [mod.EMAIL_TESTE])
        self._executar()
        self.assertEqual(self.enviados[1][0], ["log@a.com", "fin@a.com"])

    def test_chave_mestra_desligada_bloqueia_envio_real(self):
        r = self._executar(config={"notificacoes_automaticas": {"ativo": False},
                                   "notificacao_nfs_em_rota": {"forcar_destino": ""}})
        self.assertEqual(self.enviados, [])
        self.assertTrue(r["desativado"])

    def test_flag_proprio_desligado(self):
        r = self._executar(config={"notificacao_nfs_em_rota": {"ativo": False}})
        self.assertEqual(self.enviados, [])
        self.assertTrue(r["desativado"])

    def test_falha_num_embarcador_nao_derruba_os_outros(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE interno SET notificar_email = 1 WHERE sender_id = ?", (SENDER_B,))
        conn.commit()
        conn.close()

        def enviar(destinos, assunto, corpo, config_email, **kw):
            if destinos == ["b@b.com"]:
                raise RuntimeError("smtp caiu")
            return self._enviar(destinos, assunto, corpo, config_email)

        r = mod.executar({"notificacao_nfs_em_rota": {"forcar_destino": ""}}, DATA, rotas=ROTAS,
                         motoristas={}, enviar=enviar, db_path=self.db)
        self.assertEqual((r["enviados"], r["falhas"]), (1, 1))

    def test_cliente_que_desligou_no_portal_nao_recebe(self):
        import preferencias_notificacao
        conn = sqlite3.connect(self.db)
        preferencias_notificacao.salvar(conn, "1", [], {"nfs_em_rota": False}, "cliente")
        conn.close()
        r = self._executar()
        self.assertEqual(self.enviados, [])
        self.assertEqual(r["enviados"], 0)

    def test_email_de_notificacoes_do_portal_vence_o_do_cadastro(self):
        import preferencias_notificacao
        conn = sqlite3.connect(self.db)
        preferencias_notificacao.salvar(conn, "1", ["avisos@a.com"], {}, "cliente")
        conn.close()
        self._executar()
        self.assertEqual(self.enviados[0][0], ["avisos@a.com"])

    def test_filtro_por_sender_id(self):
        r = self._executar(sender_id=333)
        self.assertEqual(self.enviados, [])
        self.assertEqual(r["sem_notas"], 1)


if __name__ == "__main__":
    unittest.main()
