# -*- coding: utf-8 -*-
"""WhatsApp de chamado sem resposta, convite de embarcador novo e consulta de
transportadora obedecem a tela de Notificacoes (controle_notificacoes.py): a
tela vale por cima do config; sem linha na tela vale o config de hoje.

Rodar: py -3.11 -m unittest test_controle_whatsapp_convite -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

import cadastro_embarcador as cad
import controle_notificacoes as cn
import embarcadores_stokki as emb
import notificar_whatsapp as nw
import test_avisar_cliente_sem_resposta as base_sr
import test_consulta_transportadora as base_ct
import test_embarcadores_stokki as base_emb

PILOTO_WPP = "5511977776666"
PILOTO_EMAIL = "piloto@freshlogbr.com"


def _controle(db, tipo=None, estado=None, geral=None, piloto=True):
    conn = cn.conectar(db)
    try:
        if geral is not None:
            cn.definir_geral(conn, geral, "teste")
        if tipo and estado:
            cn.definir_estado(conn, tipo, estado, "teste")
        if piloto:
            cn.definir_piloto(conn, PILOTO_EMAIL, PILOTO_WPP, "teste")
    finally:
        conn.close()


class TestSemResposta(base_sr._ComBanco):
    """Banco do chamado e do controle e o mesmo arquivo temporario (ch.DB_PATH)."""

    def _rodar(self, config=None, **kw):
        return self.executar(config, db_path=base_sr.ch.DB_PATH, **kw)

    def _chamado_em_silencio(self):
        c = self.chamado()
        return self.equipe_respondeu(c, 11)

    def test_sem_resposta_tela_desligado_consome_msg(self):
        _controle(base_sr.ch.DB_PATH, "whatsapp_sem_resposta", "DESLIGADO")
        msg = self._chamado_em_silencio()
        r = self._rodar()
        self.assertEqual((r["avaliados"], r["enviados"]), (1, 0))
        self.enviar.assert_not_called()
        self.assertEqual([(a, s) for a, s, _ in self.registros()], [(f"msg:{msg}", "bloqueado")])
        # consumido: ligar depois nao faz sair o aviso velho
        _controle(base_sr.ch.DB_PATH, "whatsapp_sem_resposta", "LIGADO")
        r = self._rodar()
        self.assertEqual((r["avaliados"], r["enviados"]), (0, 0))
        self.enviar.assert_not_called()

    def test_sem_resposta_piloto_vai_pro_numero_do_piloto(self):
        _controle(base_sr.ch.DB_PATH, "whatsapp_sem_resposta", "PILOTO")
        msg = self._chamado_em_silencio()
        r = self._rodar()
        self.assertEqual(r["enviados"], 0)
        self.assertEqual(self.enviar.call_args[0][1], f"{PILOTO_WPP}@c.us")
        self.assertTrue(self.enviar.call_args[0][2].startswith(f"[teste → +{base_sr.TEL}]"))
        # nao conta como recebido pelo cliente: origem propria, msg segue elegivel
        origens = [tuple(x) for x in self.conn.execute("SELECT origem, assinatura, situacao FROM notificacoes_whatsapp")]
        self.assertEqual(origens, [("cliente_sem_resposta_piloto", f"msg:{msg}", "enviado")])
        self.assertEqual(self.registros()[0][1], "enviado")
        # a copia de piloto sai uma vez so por mensagem
        self._rodar()
        self.assertEqual(self.enviar.call_count, 1)

    def test_sem_resposta_tela_ligado_envia_real_mesmo_com_forcar_no_config(self):
        _controle(base_sr.ch.DB_PATH, "whatsapp_sem_resposta", "LIGADO")
        self._chamado_em_silencio()
        r = self._rodar(base_sr._config(forcar_destino="5511900001111"))
        self.assertEqual(r["enviados"], 1)
        self.assertEqual(self.enviar.call_args[0][1], f"{base_sr.TEL}@c.us")

    def test_sem_resposta_tela_ligado_vale_com_clientes_ativo_desligado_no_config(self):
        _controle(base_sr.ch.DB_PATH, "whatsapp_sem_resposta", "LIGADO")
        self._chamado_em_silencio()
        r = self._rodar(base_sr._config(ativo=False))
        self.assertEqual(r["enviados"], 1)

    def test_sem_linha_na_tela_config_desligado_nao_consome(self):
        self._chamado_em_silencio()
        r = self._rodar(base_sr._config(ativo=False))
        self.assertEqual(r["enviados"], 0)
        self.enviar.assert_not_called()
        self.assertEqual(self.registros(), [])

    def test_sem_linha_na_tela_config_ligado_envia_como_hoje(self):
        self._chamado_em_silencio()
        self.assertEqual(self._rodar()["enviados"], 1)
        self.assertEqual(self.enviar.call_args[0][1], f"{base_sr.TEL}@c.us")

    def test_modo_teste_ignora_a_tela(self):
        _controle(base_sr.ch.DB_PATH, "whatsapp_sem_resposta", "DESLIGADO")
        self._chamado_em_silencio()
        r = self._rodar(modo_teste=True)
        self.assertEqual([s for _, s in r["situacoes"]], ["modo_teste"])
        self.assertEqual(self.registros(), [])

    def test_main_nao_retorna_cedo_quando_ha_linha_na_tela(self):
        config = base_sr._config(ativo=False)
        for tem_linha, esperado in ((False, 0), (True, 1)):
            with self.subTest(tem_linha=tem_linha):
                with mock.patch.object(base_sr.rot, "executar", return_value={}) as executar, \
                        mock.patch.object(base_sr.rot, "_tela_tem_linha", return_value=tem_linha), \
                        mock.patch.object(base_sr.rot.yaml, "safe_load", return_value=config), \
                        mock.patch("builtins.open", mock.mock_open(read_data="")):
                    self.assertEqual(base_sr.rot.main([]), 0)
                self.assertEqual(executar.call_count, esperado)


class _ConviteComControle(base_emb._ComCadastro):
    def setUp(self):
        super().setUp()
        self.ctl = Path(self._tmp.name) / "controle.db"

    def convidar(self, cli, config=None, **kw):
        return super().convidar(cli, config=config, db_path=self.ctl, **kw)

    def situacao(self, cli):
        return cad.buscar(self.conn, cli["cnpj"])["situacao"]


class TestConvite(_ConviteComControle):
    def test_convite_piloto_nao_marca_convidado(self):
        _controle(self.ctl, "convite_embarcador", "PILOTO")
        self.preparar(base_emb.NONNA)
        self.assertEqual(self.convidar(base_emb.NONNA), "piloto")
        self.assertEqual(self.enviar.call_args.args[1], f"{PILOTO_WPP}@c.us")
        self.assertIn("/definir-pin/", self.enviar.call_args.args[2])
        c = cad.buscar(self.conn, base_emb.NONNA["cnpj"])
        self.assertEqual(c["situacao"], "PENDENTE")
        self.assertFalse(c["convite_enviado_em"])
        # o numero do cliente nao e guardado pelo piloto
        self.assertEqual(emb.pn.ler(self.conn, base_emb.NONNA["cnpj"])["whatsapp"], "")
        # no maximo 1 copia por dia
        self.assertEqual(self.convidar(base_emb.NONNA), "piloto")
        self.assertEqual(self.enviar.call_count, 1)

    def test_convite_piloto_por_email(self):
        _controle(self.ctl, "convite_embarcador", "PILOTO")
        self.preparar(base_emb.RIC)
        self.assertEqual(self.convidar(base_emb.RIC), "piloto")
        self.assertEqual(self.email.call_args.args[0], [PILOTO_EMAIL])
        self.assertEqual(self.situacao(base_emb.RIC), "PENDENTE")
        self.assertEqual(self.convidar(base_emb.RIC), "piloto")
        self.assertEqual(self.email.call_count, 1)

    def test_convite_ligado_marca_convidado(self):
        _controle(self.ctl, "convite_embarcador", "LIGADO")
        self.preparar(base_emb.NONNA)
        config = base_emb._config()
        config["cadastro_embarcador"]["forcar_destino_whatsapp"] = "5511900001111"
        self.assertEqual(self.convidar(base_emb.NONNA, config=config), "whatsapp")
        self.assertEqual(self.enviar.call_args.args[1], "5511988887777@c.us")   # real, apesar do forcar do config
        self.assertEqual(self.situacao(base_emb.NONNA), "CONVIDADO")

    def test_convite_ligado_email_real_apesar_do_forcar_do_config(self):
        _controle(self.ctl, "convite_embarcador", "LIGADO")
        self.preparar(base_emb.RIC)
        config = base_emb._config()
        config["cadastro_embarcador"]["forcar_destino_email"] = "hugo@freshlogbr.com"
        self.assertEqual(self.convidar(base_emb.RIC, config=config), "email")
        self.assertEqual(self.email.call_args.args[0], ["contato@ric.com"])
        self.assertEqual(self.situacao(base_emb.RIC), "CONVIDADO")

    def test_convite_desligado_pela_tela_nao_envia_e_continua_pendente(self):
        _controle(self.ctl, "convite_embarcador", "DESLIGADO")
        self.preparar(base_emb.NONNA)
        self.preparar(base_emb.RIC, sender=22872019)
        self.assertEqual(self.convidar(base_emb.NONNA), "bloqueado")
        self.assertEqual(self.convidar(base_emb.RIC), "bloqueado")
        self.enviar.assert_not_called()
        self.email.assert_not_called()
        self.assertEqual(self.situacao(base_emb.NONNA), "PENDENTE")
        self.assertEqual(self.situacao(base_emb.RIC), "PENDENTE")
        # ao ligar, o convite pendente sai
        _controle(self.ctl, "convite_embarcador", "LIGADO")
        self.assertEqual(self.convidar(base_emb.NONNA), "whatsapp")
        self.assertEqual(self.situacao(base_emb.NONNA), "CONVIDADO")

    def test_convite_excecao_bloqueado_nao_envia(self):
        _controle(self.ctl, "convite_embarcador", "LIGADO")
        conn = cn.conectar(self.ctl)
        cn.definir_excecao(conn, "convite_embarcador", base_emb.NONNA["cnpj"], "BLOQUEADO", "teste")
        conn.close()
        self.preparar(base_emb.NONNA)
        self.assertEqual(self.convidar(base_emb.NONNA), "bloqueado")
        self.enviar.assert_not_called()

    def test_sem_linha_na_tela_vale_o_config(self):
        self.preparar(base_emb.NONNA)
        self.assertEqual(self.convidar(base_emb.NONNA), "whatsapp")
        self.assertEqual(self.situacao(base_emb.NONNA), "CONVIDADO")

    def test_modo_teste_ignora_a_tela(self):
        _controle(self.ctl, "convite_embarcador", "DESLIGADO")
        self.preparar(base_emb.NONNA)
        self.assertEqual(self.convidar(base_emb.NONNA, modo_teste=True), "whatsapp")


class TestConsultaTransportadora(base_ct.AvisoTest):
    def _ctl(self, estado):
        _controle(self.db, "consulta_transportadora", estado)

    def _marcas(self):
        return tuple(self.conn.execute("SELECT enviado_em, ultimo_lembrete_em FROM transportadora_consultas").fetchone())

    def test_consulta_transportadora_bloqueado_consome(self):
        self._ctl("DESLIGADO")
        r = base_ct.ct.enviar_aviso(self.conn, self.consulta, self.config, False, False, db_path=self.db)
        self.email.assert_not_called()
        self.zap.assert_not_called()
        self.assertFalse(r["email"])
        self.assertEqual(r["whatsapp"], "bloqueado")
        enviado_em, lembrete = self._marcas()
        self.assertTrue(enviado_em)
        self.assertIsNone(lembrete)

    def test_bloqueado_no_lembrete_marca_o_lembrete(self):
        self._ctl("DESLIGADO")
        base_ct.ct.enviar_aviso(self.conn, self.consulta, self.config, True, False, db_path=self.db)
        enviado_em, lembrete = self._marcas()
        self.assertIsNone(enviado_em)
        self.assertTrue(lembrete)

    def test_piloto_manda_copia_ao_piloto_e_nao_marca(self):
        self._ctl("PILOTO")
        r = base_ct.ct.enviar_aviso(self.conn, self.consulta, self.config, False, False, db_path=self.db)
        self.assertEqual(self.email.call_args[0][0], [PILOTO_EMAIL])
        self.assertEqual(self.zap.call_args.kwargs["destino_forcado"], PILOTO_WPP)
        self.assertEqual(self._marcas(), (None, None))
        # no maximo uma copia por consulta por dia
        base_ct.ct.enviar_aviso(self.conn, self.consulta, self.config, False, False, db_path=self.db)
        self.assertEqual(self.email.call_count, 1)
        self.assertEqual(self.zap.call_count, 1)

    def test_ligado_envia_real_apesar_do_forcar_do_config_e_marca(self):
        self._ctl("LIGADO")
        self.config["consulta_transportadora"]["forcar_destino_email"] = "hugo@x.com"
        r = base_ct.ct.enviar_aviso(self.conn, self.consulta, self.config, False, False, db_path=self.db)
        self.assertEqual(self.email.call_args[0][0], ["a@x.com", "b@x.com"])
        self.assertEqual(self.zap.call_args.kwargs["destino_forcado"], "")
        self.assertTrue(self._marcas()[0])

    def test_ligado_pela_tela_vale_com_config_desligado(self):
        self._ctl("LIGADO")
        self.config["consulta_transportadora"]["ativo"] = False
        base_ct.ct.enviar_aviso(self.conn, self.consulta, self.config, False, False, db_path=self.db)
        self.email.assert_called_once()

    def test_sem_linha_na_tela_vale_o_config(self):
        base_ct.ct.enviar_aviso(self.conn, self.consulta, self.config, False, False, db_path=self.db)
        self.assertEqual(self.email.call_args[0][0], ["a@x.com", "b@x.com"])
        self.assertIsNone(self.zap.call_args.kwargs.get("destino_forcado"))

    def test_modo_teste_ignora_a_tela(self):
        self._ctl("DESLIGADO")
        base_ct.ct.enviar_aviso(self.conn, self.consulta, self.config, False, True, db_path=self.db)
        self.assertEqual(self._marcas(), (None, None))
        self.assertIsNone(self.zap.call_args.kwargs.get("destino_forcado"))


class TestFuncoesDoWhatsapp(unittest.TestCase):
    """destino_forcado das funcoes de notificar_whatsapp (mesma semantica de avisar_cliente_fora_dia_fixo)."""

    CONFIG = {"whatsapp_notificacoes": {"ativo": True, "base_url": "http://x", "api_key": "k", "sessao": "s",
                                        "grupo_id": "g@g.us", "intervalo_min_seg": 0,
                                        "clientes": {"ativo": True, "teto_diario": 30, "forcar_destino": "5511900001111"}},
              "cadastro_embarcador": {"forcar_destino_whatsapp": "5511900002222"},
              "consulta_transportadora": {"forcar_destino_whatsapp": "5511900003333"}}

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        for alvo, valor in (("configurado", lambda cfg: True), ("numero_existe", lambda cfg, n: True)):
            p = mock.patch.object(nw.integracao_openwa, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(nw.integracao_openwa, "enviar_texto", return_value=(True, "m1"))
        self.enviar = p.start()
        self.addCleanup(p.stop)

    def _chamar(self, fn, **kw):
        from datetime import datetime
        base = dict(conn=self.conn, agora=datetime(2026, 10, 6, 18, 0), dormir=lambda s: None)
        if fn is nw.avisar_cliente_sem_resposta:
            return fn(1, 2, "5511988887777", "http://l", self.CONFIG, **base, **kw)
        return fn("11111111000111", "5511988887777", "texto", "ass:1", self.CONFIG, **base, **kw)

    def test_destino_forcado_troca_o_forcar_do_config_so_na_chamada(self):
        for fn in (nw.avisar_cliente_sem_resposta, nw.avisar_cliente_cadastro, nw.avisar_cliente_transportadora):
            with self.subTest(fn=fn.__name__):
                self.enviar.reset_mock()
                self.assertEqual(self._chamar(fn, destino_forcado="5511977776666"), "enviado")
                self.assertEqual(self.enviar.call_args[0][1], "5511977776666@c.us")
                self.assertTrue(self.enviar.call_args[0][2].startswith("[teste → +5511988887777]"))

    def test_destino_forcado_vazio_e_envio_real(self):
        for fn in (nw.avisar_cliente_sem_resposta, nw.avisar_cliente_cadastro, nw.avisar_cliente_transportadora):
            with self.subTest(fn=fn.__name__):
                self.enviar.reset_mock()
                self.assertEqual(self._chamar(fn, destino_forcado=""), "enviado")
                self.assertEqual(self.enviar.call_args[0][1], "5511988887777@c.us")
                self.assertFalse(self.enviar.call_args[0][2].startswith("[teste"))

    def test_none_vale_o_config(self):
        esperados = {nw.avisar_cliente_sem_resposta: "5511900001111", nw.avisar_cliente_cadastro: "5511900002222",
                     nw.avisar_cliente_transportadora: "5511900003333"}
        for fn, numero in esperados.items():
            with self.subTest(fn=fn.__name__):
                self.enviar.reset_mock()
                self.assertEqual(self._chamar(fn), "enviado")
                self.assertEqual(self.enviar.call_args[0][1], f"{numero}@c.us")


if __name__ == "__main__":
    unittest.main()
