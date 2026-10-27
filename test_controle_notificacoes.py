# -*- coding: utf-8 -*-
"""Testes do controle_notificacoes (rodar da raiz: py -3.11 -m unittest test_controle_notificacoes -v)."""
import tempfile
import unittest
from pathlib import Path

import controle_notificacoes as cn


class BaseTeste(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "teste.db"
        self.conn = cn.conectar(self.db)
        self.addCleanup(self.conn.close)


class TestDecidir(BaseTeste):
    def test_sem_linha_usa_fallback(self):
        d = cn.decidir("nfs_em_rota", "123", fallback_acao="PILOTO", fallback_piloto="x@y", db_path=self.db)
        self.assertEqual((d.acao, d.destino_piloto, d.origem), ("PILOTO", "x@y", "config"))

    def test_sem_linha_fallback_nao_leva_piloto_se_nao_for_piloto(self):
        d = cn.decidir("nfs_em_rota", "123", fallback_acao="ENVIAR", fallback_piloto="x@y", db_path=self.db)
        self.assertEqual((d.acao, d.destino_piloto), ("ENVIAR", ""))

    def test_geral_desligado_cala_tipo_sem_linha(self):
        cn.definir_geral(self.conn, False, "hugo")
        d = cn.decidir("nfs_em_rota", "123", fallback_acao="ENVIAR", db_path=self.db)
        self.assertEqual((d.acao, d.origem), ("NAO", "tela"))

    def test_estados(self):
        esperado = {"DESLIGADO": "NAO", "LIGADO": "ENVIAR", "PILOTO": "PILOTO"}
        for estado, acao in esperado.items():
            cn.definir_estado(self.conn, "nfs_em_rota", estado, "hugo")
            d = cn.decidir("nfs_em_rota", "123", fallback_acao="ENVIAR", db_path=self.db)
            self.assertEqual(d.acao, acao, estado)
            self.assertEqual(d.origem, "tela")
            if estado == "PILOTO":
                self.assertEqual(d.destino_piloto, "hugo@freshlogbr.com")

    def test_liberado_fura_piloto_e_bloqueado_vence_ligado(self):
        cn.definir_estado(self.conn, "nfs_em_rota", "PILOTO", "hugo")
        cn.definir_excecao(self.conn, "nfs_em_rota", "11.222.333/0001-81", "LIBERADO", "hugo")
        d = cn.decidir("nfs_em_rota", "11222333000181", fallback_acao="NAO", db_path=self.db)
        self.assertEqual(d.acao, "ENVIAR")
        # outro destinatario continua so no piloto
        d2 = cn.decidir("nfs_em_rota", "99999999000100", fallback_acao="NAO", db_path=self.db)
        self.assertEqual(d2.acao, "PILOTO")
        # destino digitado com pontuacao tambem casa
        d3 = cn.decidir("nfs_em_rota", "11.222.333/0001-81", fallback_acao="NAO", db_path=self.db)
        self.assertEqual(d3.acao, "ENVIAR")

        cn.definir_estado(self.conn, "nfs_em_rota", "LIGADO", "hugo")
        cn.definir_excecao(self.conn, "nfs_em_rota", "11222333000181", "BLOQUEADO", "hugo")
        d4 = cn.decidir("nfs_em_rota", "11222333000181", fallback_acao="ENVIAR", db_path=self.db)
        self.assertEqual(d4.acao, "NAO")

    def test_bloqueado_vence_mesmo_com_estado_desligado_e_destino_none_ignora_excecao(self):
        cn.definir_estado(self.conn, "nfs_em_rota", "LIGADO", "hugo")
        cn.definir_excecao(self.conn, "nfs_em_rota", "123", "BLOQUEADO", "hugo")
        d = cn.decidir("nfs_em_rota", None, fallback_acao="NAO", db_path=self.db)
        self.assertEqual(d.acao, "ENVIAR")

    def test_piloto_whatsapp_sem_numero_vira_nao(self):
        cn.definir_estado(self.conn, "convite_embarcador", "PILOTO", "hugo")
        d = cn.decidir("convite_embarcador", "123", fallback_acao="ENVIAR", canal="whatsapp", db_path=self.db)
        self.assertEqual(d.acao, "NAO")
        self.assertIn("piloto sem destino", d.motivo)

    def test_piloto_whatsapp_com_numero(self):
        cn.definir_estado(self.conn, "convite_embarcador", "PILOTO", "hugo")
        cn.definir_piloto(self.conn, "hugo@freshlogbr.com", "5511999990000", "hugo")
        d = cn.decidir("convite_embarcador", "123", fallback_acao="ENVIAR", canal="whatsapp", db_path=self.db)
        self.assertEqual((d.acao, d.destino_piloto), ("PILOTO", "5511999990000"))

    def test_erro_de_banco_usa_fallback(self):
        # um diretorio no lugar do arquivo: abrir o banco falha com sqlite3.Error
        d = cn.decidir("nfs_em_rota", "123", fallback_acao="PILOTO", fallback_piloto="x@y",
                       db_path=self._tmp.name)
        self.assertEqual((d.acao, d.destino_piloto, d.origem), ("PILOTO", "x@y", "config"))

    def test_transportadora_destino_normalizado_sem_digitos(self):
        cn.definir_estado(self.conn, "transportadoras", "PILOTO", "hugo")
        cn.definir_excecao(self.conn, "transportadoras", " TAFF ", "LIBERADO", "hugo")
        d = cn.decidir("transportadoras", "taff", fallback_acao="NAO", db_path=self.db)
        self.assertEqual(d.acao, "ENVIAR")

    def test_tipo_desconhecido_levanta(self):
        with self.assertRaises(ValueError):
            cn.decidir("inexistente", "1", fallback_acao="NAO", db_path=self.db)


class TestConsome(BaseTeste):
    """`consome`: o NAO tira o evento da fila (geral desligado, tipo DESLIGADO,
    BLOQUEADO). Piloto sem destino e NAO do config nao consomem."""

    def test_geral_desligado_consome(self):
        cn.definir_geral(self.conn, False, "hugo")
        d = cn.decidir("nfs_em_rota", "123", fallback_acao="ENVIAR", db_path=self.db)
        self.assertTrue(d.consome)
        self.assertEqual(d.motivo, cn.MOTIVO_GERAL_DESLIGADO)

    def test_tipo_desligado_consome(self):
        cn.definir_estado(self.conn, "nfs_em_rota", "DESLIGADO", "hugo")
        d = cn.decidir("nfs_em_rota", "123", fallback_acao="ENVIAR", db_path=self.db)
        self.assertEqual(d.acao, "NAO")
        self.assertTrue(d.consome)

    def test_bloqueado_consome(self):
        cn.definir_estado(self.conn, "nfs_em_rota", "LIGADO", "hugo")
        cn.definir_excecao(self.conn, "nfs_em_rota", "123", "BLOQUEADO", "hugo")
        d = cn.decidir("nfs_em_rota", "123", fallback_acao="ENVIAR", db_path=self.db)
        self.assertEqual(d.acao, "NAO")
        self.assertTrue(d.consome)

    def test_piloto_sem_destino_nao_consome(self):
        cn.definir_estado(self.conn, "whatsapp_sem_resposta", "PILOTO", "hugo")
        d = cn.decidir("whatsapp_sem_resposta", "123", fallback_acao="ENVIAR", canal="whatsapp",
                       db_path=self.db)
        self.assertEqual((d.acao, d.origem), ("NAO", "tela"))
        self.assertFalse(d.consome)

    def test_config_nao_consome(self):
        d = cn.decidir("nfs_em_rota", "123", fallback_acao="NAO", db_path=self.db)
        self.assertEqual((d.acao, d.origem), ("NAO", "config"))
        self.assertFalse(d.consome)
        d2 = cn.decidir("nfs_em_rota", "123", fallback_acao="NAO", db_path=self._tmp.name)
        self.assertFalse(d2.consome)

    def test_enviar_e_piloto_nao_consomem(self):
        for estado in ("LIGADO", "PILOTO"):
            cn.definir_estado(self.conn, "nfs_em_rota", estado, "hugo")
            d = cn.decidir("nfs_em_rota", "123", fallback_acao="NAO", db_path=self.db)
            self.assertFalse(d.consome, estado)


class TestConfig(unittest.TestCase):
    def test_acao_do_config(self):
        self.assertEqual(cn.acao_do_config(False, ""), "NAO")
        self.assertEqual(cn.acao_do_config(False, "h@x"), "NAO")
        self.assertEqual(cn.acao_do_config(True, "h@x"), "PILOTO")
        self.assertEqual(cn.acao_do_config(True, ""), "ENVIAR")


class TestTipos(unittest.TestCase):
    def test_canais_e_destinos(self):
        self.assertEqual(set(cn.TIPOS), {
            "nfs_em_rota", "entrega_concluida", "resumo_diario", "insucesso", "pedidos_em_espera",
            "agendamento", "fora_dia_fixo", "faltas_recebimento", "whatsapp_sem_resposta", "chamados",
            "area_nao_atendida", "transportadoras", "convite_embarcador", "redespacho",
            "consulta_transportadora", "rota_cancelada"})
        self.assertEqual(len(cn.TIPOS), 16)
        for t in ("fora_dia_fixo", "convite_embarcador", "consulta_transportadora"):
            self.assertEqual(cn.TIPOS[t]["canais"], ("email", "whatsapp"))
        self.assertEqual(cn.TIPOS["redespacho"]["canais"], ("email",))
        self.assertEqual(cn.TIPOS["whatsapp_sem_resposta"]["canais"], ("whatsapp",))
        self.assertEqual(cn.TIPOS["nfs_em_rota"]["canais"], ("email",))
        self.assertEqual(cn.TIPOS["transportadoras"]["destino"], "transportadora")
        self.assertEqual(cn.TIPOS["nfs_em_rota"]["destino"], "embarcador")
        for info in cn.TIPOS.values():
            self.assertEqual(set(info), {"rotulo", "destino", "canais", "quando"})


class TestMutacoes(BaseTeste):
    def test_estado_invalido_recusa(self):
        with self.assertRaises(ValueError):
            cn.definir_estado(self.conn, "nfs_em_rota", "QUALQUER", "h")
        with self.assertRaises(ValueError):
            cn.definir_estado(self.conn, "tipo_que_nao_existe", "LIGADO", "h")
        with self.assertRaises(ValueError):
            cn.definir_excecao(self.conn, "tipo_que_nao_existe", "1", "LIBERADO", "h")
        with self.assertRaises(ValueError):
            cn.definir_excecao(self.conn, "nfs_em_rota", "1", "TALVEZ", "h")
        self.assertIsNone(cn.estado_tipo(self.conn, "nfs_em_rota"))

    def test_transportadora_nao_aceita_bloqueado(self):
        with self.assertRaises(ValueError):
            cn.definir_excecao(self.conn, "transportadoras", "taff", "BLOQUEADO", "h")
        cn.definir_excecao(self.conn, "transportadoras", "taff", "LIBERADO", "h")
        self.assertEqual(cn.excecoes(self.conn, "transportadoras"), {"taff": "LIBERADO"})

    def test_leituras_e_piloto_padrao(self):
        self.assertIsNone(cn.estado_geral(self.conn))
        self.assertIsNone(cn.estado_tipo(self.conn, "nfs_em_rota"))
        self.assertEqual(cn.excecoes(self.conn, "nfs_em_rota"), {})
        self.assertEqual(cn.piloto(self.conn), {"email": "hugo@freshlogbr.com", "whatsapp": ""})
        cn.definir_geral(self.conn, True, "hugo")
        self.assertEqual(cn.estado_geral(self.conn), "LIGADO")
        cn.definir_geral(self.conn, False, "hugo")
        self.assertEqual(cn.estado_geral(self.conn), "DESLIGADO")
        cn.definir_piloto(self.conn, "outro@x.com", "5511988887777", "hugo")
        self.assertEqual(cn.piloto(self.conn), {"email": "outro@x.com", "whatsapp": "5511988887777"})

    def test_historico_e_desfazer(self):
        cn.definir_estado(self.conn, "nfs_em_rota", "LIGADO", "hugo")
        cn.definir_estado(self.conn, "nfs_em_rota", "PILOTO", "hugo")
        hist = cn.historico(self.conn)
        self.assertEqual(len(hist), 2)
        segunda = hist[0]  # mais recente primeiro
        self.assertEqual((segunda["alvo"], segunda["de"], segunda["para"]), ("nfs_em_rota", "LIGADO", "PILOTO"))
        self.assertEqual(hist[1]["de"], None)

        cn.desfazer(self.conn, segunda["id"], "gabriel")
        self.assertEqual(cn.estado_tipo(self.conn, "nfs_em_rota"), "LIGADO")
        hist = cn.historico(self.conn)
        self.assertEqual(len(hist), 3)
        self.assertEqual((hist[0]["de"], hist[0]["para"], hist[0]["por"]), ("PILOTO", "LIGADO", "gabriel"))

        # desfazer a primeira (estado antes inexistente) remove a linha do tipo
        cn.desfazer(self.conn, hist[2]["id"], "gabriel")
        self.assertIsNone(cn.estado_tipo(self.conn, "nfs_em_rota"))

    def test_desfazer_excecao_criada_remove(self):
        cn.definir_excecao(self.conn, "nfs_em_rota", "11.222.333/0001-81", "LIBERADO", "hugo")
        self.assertEqual(cn.excecoes(self.conn, "nfs_em_rota"), {"11222333000181": "LIBERADO"})
        item = cn.historico(self.conn)[0]
        self.assertEqual(item["destino"], "11222333000181")
        cn.desfazer(self.conn, item["id"], "hugo")
        self.assertEqual(cn.excecoes(self.conn, "nfs_em_rota"), {})

    def test_remover_excecao_e_desfazer_recria(self):
        cn.definir_excecao(self.conn, "nfs_em_rota", "123", "BLOQUEADO", "hugo")
        cn.definir_excecao(self.conn, "nfs_em_rota", "123", None, "hugo")
        self.assertEqual(cn.excecoes(self.conn, "nfs_em_rota"), {})
        item = cn.historico(self.conn)[0]
        self.assertEqual((item["de"], item["para"]), ("BLOQUEADO", None))
        cn.desfazer(self.conn, item["id"], "hugo")
        self.assertEqual(cn.excecoes(self.conn, "nfs_em_rota"), {"123": "BLOQUEADO"})

    def test_desfazer_geral_e_piloto(self):
        cn.definir_geral(self.conn, False, "hugo")
        cn.desfazer(self.conn, cn.historico(self.conn)[0]["id"], "hugo")
        self.assertIsNone(cn.estado_geral(self.conn))

        cn.definir_piloto(self.conn, "a@b.com", "5511999990000", "hugo")
        for item in cn.historico(self.conn)[:2]:
            if item["alvo"] == "_piloto":
                cn.desfazer(self.conn, item["id"], "hugo")
        self.assertEqual(cn.piloto(self.conn), {"email": "hugo@freshlogbr.com", "whatsapp": ""})

    def test_sem_mudanca_nao_grava_historico(self):
        cn.definir_estado(self.conn, "nfs_em_rota", "LIGADO", "hugo")
        cn.definir_estado(self.conn, "nfs_em_rota", "LIGADO", "hugo")
        self.assertEqual(len(cn.historico(self.conn)), 1)

    def test_desfazer_id_inexistente(self):
        with self.assertRaises(ValueError):
            cn.desfazer(self.conn, 999, "hugo")


if __name__ == "__main__":
    unittest.main()
