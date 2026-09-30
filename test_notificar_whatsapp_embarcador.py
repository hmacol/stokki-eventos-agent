"""Testes do WhatsApp ao embarcador (notificar_whatsapp_embarcador.py).

Rodar: py -3.11 -m unittest test_notificar_whatsapp_embarcador
"""

import sqlite3
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import notificar_whatsapp as nw
import notificar_whatsapp_embarcador as nwe

AGORA = datetime(2026, 9, 30, 18, 10)
EMB = {"nome": "Alfa", "cnpj": "11111111000111", "emails": ["a@alfa.com"], "celulares": ["11988887777"],
       "desligado": False}


def _config(emb=None, **wa_extra):
    wa = {"ativo": True, "base_url": "http://x/api", "api_key": "k", "sessao": "s", "grupo_id": "1@g.us",
          "teto_diario": 2, "intervalo_min_seg": 20, "janela_repeticao_min": 120, "falhas_para_alerta": 3,
          "embarcadores": {"ativo": True, "teto_diario": 30, "teto_por_embarcador": 6, **(emb or {})}}
    wa.update(wa_extra)
    return {"whatsapp_notificacoes": wa, "email": {"remetente": "a@b.com"}}


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        envio = patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1"))
        self.enviar = envio.start()
        self.addCleanup(envio.stop)
        self.addCleanup(self.conn.close)

    def avisar(self, emb=EMB, chave="insucesso:7:PS-1", config=None, agora=AGORA, **kw):
        return nwe.avisar(emb, "insucesso", chave, "texto", config=_config() if config is None else config,
                          conn=self.conn,
                          agora=agora, dormir=MagicMock(), **kw)

    def linhas(self):
        return self.conn.execute(
            "SELECT origem, assinatura, situacao, motivo FROM notificacoes_whatsapp ORDER BY id").fetchall()


class TestChaves(_ComBanco):
    def test_envia_pro_celular_no_formato_do_whatsapp(self):
        self.assertEqual(self.avisar(), ["enviado"])
        self.assertEqual(self.enviar.call_args.args[1], "5511988887777@c.us")
        self.assertEqual(self.linhas(), [("embarcador:insucesso", "11111111000111|insucesso:7:PS-1|11988887777",
                                          "enviado", None)])

    def test_desligado_por_padrao(self):
        for config in (_config(emb={"ativo": False}), _config(ativo=False), {}, {"whatsapp_notificacoes": {}}):
            with self.subTest(config=config):
                self.assertEqual(self.avisar(config=config), ["desligado"])
        self.enviar.assert_not_called()

    def test_sem_celular_nao_envia(self):
        self.assertEqual(self.avisar(emb={**EMB, "celulares": []}), ["sem_celular"])
        self.enviar.assert_not_called()

    def test_modo_teste_so_loga(self):
        with self.assertLogs(nwe.logger, level="INFO"):
            self.assertEqual(self.avisar(modo_teste=True), ["modo_teste"])
        self.enviar.assert_not_called()

    def test_um_envio_por_celular(self):
        emb = {**EMB, "celulares": ["11988887777", "21977776666"]}
        self.assertEqual(self.avisar(emb=emb), ["enviado", "enviado"])
        self.assertEqual([c.args[1] for c in self.enviar.call_args_list],
                         ["5511988887777@c.us", "5521977776666@c.us"])

    def test_forcar_destino_manda_tudo_pro_numero_de_teste_avisando_o_original(self):
        emb = {**EMB, "celulares": ["11988887777", "21977776666"]}
        config = _config(emb={"forcar_destino": "+55 (11) 91234-5678"})
        self.assertEqual(self.avisar(emb=emb, config=config), ["enviado"])
        destino, texto = self.enviar.call_args.args[1:]
        self.assertEqual(destino, "5511912345678@c.us")
        self.assertTrue(texto.startswith("[TESTE · iria para Alfa: 11988887777, 21977776666]\n"))

    def test_nunca_levanta(self):
        with patch.object(nwe, "_avisar", side_effect=RuntimeError("x")):
            self.assertEqual(nwe.avisar(EMB, "insucesso", "c", "t", config=_config()), ["falhou"])

    def test_config_ausente_desliga(self):
        with patch.object(nwe, "CONFIG_PATH", nwe.CONFIG_PATH.with_name("nao-existe.yaml")):
            self.assertEqual(nwe.avisar(EMB, "insucesso", "c", "t", conn=self.conn), ["desligado"])


class TestVolume(_ComBanco):
    def test_mesmo_aviso_nao_repete_dentro_da_janela(self):
        self.avisar()
        self.assertEqual(self.avisar(agora=AGORA + timedelta(minutes=30)), ["nao_enviado"])
        self.assertEqual(self.linhas()[-1][2:], ("nao_enviado", "repetida dentro da janela"))

    def test_teto_por_embarcador(self):
        config = _config(emb={"teto_por_embarcador": 2})
        for i in range(2):
            self.assertEqual(self.avisar(chave=f"c{i}", config=config, agora=AGORA + timedelta(minutes=i)),
                             ["enviado"])
        self.assertEqual(self.avisar(chave="c9", config=config, agora=AGORA + timedelta(minutes=5)),
                         ["nao_enviado"])
        self.assertEqual(self.linhas()[-1][3], "teto diario do embarcador atingido")
        outro = {**EMB, "cnpj": "22222222000122"}
        self.assertEqual(self.avisar(emb=outro, chave="c9", config=config, agora=AGORA + timedelta(minutes=6)),
                         ["enviado"])

    def test_teto_geral_dos_embarcadores(self):
        config = _config(emb={"teto_diario": 1})
        self.avisar(config=config)
        outro = {**EMB, "cnpj": "22222222000122"}
        self.assertEqual(self.avisar(emb=outro, config=config, agora=AGORA + timedelta(minutes=1)),
                         ["nao_enviado"])
        self.assertEqual(self.linhas()[-1][3], "teto diario de embarcadores atingido")

    def test_mensagens_a_embarcador_nao_gastam_o_teto_dos_avisos_internos(self):
        for i in range(3):  # teto interno do _config e 2
            self.avisar(chave=f"c{i}", agora=AGORA + timedelta(minutes=i))
        situacao = nw.despachar(_config(), "rotina", "execucao", "t", conn=self.conn,
                                agora=AGORA + timedelta(minutes=10), dormir=MagicMock())
        self.assertEqual(situacao, "enviado")

    def test_avisos_internos_nao_gastam_o_teto_dos_embarcadores(self):
        config = _config(emb={"teto_diario": 1})
        nw.despachar(config, "rotina", "execucao", "t", conn=self.conn, agora=AGORA, dormir=MagicMock())
        self.assertEqual(self.avisar(config=config, agora=AGORA + timedelta(minutes=1)), ["enviado"])


class TestTextos(unittest.TestCase):
    def test_insucesso_leva_codigos_motivo_e_link(self):
        pedidos = [{"code": "#PS-1"}, {"code": "PS-2"}]
        texto = nwe.texto_insucesso("Alfa", "Destinatário ausente", pedidos, "https://x/r/tok")
        self.assertIn("Não conseguimos entregar 2 pedidos (Destinatário ausente):", texto)
        self.assertIn("\nPS-1, PS-2\n", texto)
        self.assertIn("\nhttps://x/r/tok\n", texto)
        self.assertTrue(texto.endswith(nwe.RODAPE))

    def test_lista_longa_e_resumida(self):
        pedidos = [{"code": f"PS-{i}"} for i in range(13)]
        self.assertIn("PS-9 e mais 3", nwe.texto_agendamento_pendente("Alfa", pedidos))

    def test_singular_e_plural(self):
        self.assertIn("Este pedido tem", nwe.texto_agendamento_pendente("Alfa", [{"code": "PS-1"}]))
        self.assertIn("Estes 2 pedidos têm",
                      nwe.texto_agendamento_pendente("Alfa", [{"code": "PS-1"}, {"code": "PS-2"}]))

    def test_dia_fixo_leva_a_data(self):
        itens = [{"servico": {"code": "#PS-1"}, "data": date(2026, 10, 6)}]
        texto = nwe.texto_agendamento_dia_fixo("Alfa", itens)
        self.assertIn("PS-1 (06/10)", texto)
        self.assertIn("Este pedido foi agendado", texto)

    def test_sem_nome_nao_fica_virgula_solta(self):
        self.assertIn("\nOlá. Este pedido", nwe.texto_agendamento_pendente("", [{"code": "PS-1"}]))

    def test_nome_perde_formatacao_do_whatsapp(self):
        self.assertIn("Olá, Alfa bold.", nwe.texto_agendamento_pendente("Alfa *bold*", [{"code": "PS-1"}]))


if __name__ == "__main__":
    unittest.main()
