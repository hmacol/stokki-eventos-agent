# -*- coding: utf-8 -*-
"""
Aviso de data fora do dia fixo obedecendo a tela de Notificacoes
(controle_notificacoes.py): e-mail e WhatsApp decididos separadamente.
Rodar (da raiz): py -3.11 -m unittest roteirizacao.test_controle_fora_dia_fixo -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import controle_notificacoes as cn
import fora_dia_fixo as fdf
import notificar_fora_dia_fixo as nfdf
import registro_dia_fixo as reg
from regioes_dia_fixo import regra_dia_fixo_do_servico

CAMPINAS = "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"
HOJE = date(2026, 10, 6)
CNPJ = "12345678000195"
CONFIG = {"notificacoes_automaticas": {"ativo": True}, "email": {}}
CONFIG_MESTRA_OFF = {"notificacoes_automaticas": {"ativo": False}, "email": {}, "fora_dia_fixo": {"forcar_destino": ""}}
EMBS = {7: {"nome": "EMB TESTE", "emails": ["cliente@emb.com"], "cnpj": CNPJ, "desligado": False}}
FONE = "5511988887777"
PILOTO_WPP = "5511900001111"


def _servico(i=1, data="2026-10-08"):
    return {"id": i, "code": f"#PS-{1000 + i}", "address": CAMPINAS, "sender_id": 7, "dimension_3": 5,
            "latitude": -22.905, "longitude": -47.060, "created_at": "2026-10-06 15:00:00",
            "title": f"Cliente {i}", "scheduled_start": f"{data}T08:00:00-03:00"}


def _item(i=1):
    s = _servico(i)
    return {"servico": s, "regra": regra_dia_fixo_do_servico(s), "data": date(2026, 10, 8),
            "valor": 784.09, "valor_pendente": False}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        patches = (
            mock.patch.object(nfdf, "enviar_email", return_value=True),
            mock.patch.object(nfdf.preferencias_notificacao, "whatsapp_do_embarcador", return_value=FONE),
            mock.patch.object(nfdf.notificar_whatsapp, "avisar_cliente_fora_dia_fixo", return_value="enviado"),
        )
        self.email, self.fone, self.wpp = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def _controle(self, fn, *args):
        conn = cn.conectar(self.db)
        try:
            fn(conn, *args, "teste")
        finally:
            conn.close()

    def _estado(self, estado):
        self._controle(cn.definir_estado, "fora_dia_fixo", estado)

    def _piloto(self, email="hugo@freshlogbr.com", whatsapp=""):
        self._controle(cn.definir_piloto, email, whatsapp)

    def _avisar(self, config=CONFIG, itens=None):
        return nfdf.avisar(itens or [_item()], config, db_path=self.db, embarcadores=EMBS)

    def _canais(self, i=1):
        conn = reg.conectar(self.db)
        try:
            row = conn.execute("SELECT canais FROM avisos_fora_dia_fixo WHERE codigo = ?",
                               (f"PS-{1000 + i}",)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _avisado(self, i=1):
        conn = reg.conectar(self.db)
        try:
            return reg.ja_avisado(conn, _servico(i))
        finally:
            conn.close()


class TestControleForaDiaFixo(Base):
    def test_fora_dia_fixo_ligado_com_mestra_desligada_envia_email_e_whatsapp_reais(self):
        self._estado("LIGADO")
        r = self._avisar(CONFIG_MESTRA_OFF)
        self.assertEqual((r["emails"], r["whatsapp"], r["registrados"]), (1, 1, 1))
        self.assertEqual(self.email.call_args[0][0], ["cliente@emb.com"])
        self.assertNotIn("iria para", self.email.call_args[0][2])
        self.assertEqual(self.wpp.call_args.kwargs["destino_forcado"], "")
        self.assertEqual(self._canais(), "email,whatsapp")

    def test_fora_dia_fixo_desligado_pela_tela_com_mestra_ligada_nao_envia(self):
        self._estado("DESLIGADO")
        self._avisar()
        self.email.assert_not_called()
        self.wpp.assert_not_called()

    def test_fora_dia_fixo_bloqueado_nos_dois_canais_consome(self):
        self._estado("LIGADO")
        self._controle(cn.definir_excecao, "fora_dia_fixo", CNPJ, "BLOQUEADO")
        r = self._avisar()
        self.email.assert_not_called()
        self.wpp.assert_not_called()
        self.assertEqual((r["bloqueados"], r["emails"], r["whatsapp"]), (1, 0, 0))
        self.assertEqual(self._canais(), "bloqueado")
        self.assertTrue(self._avisado())

    def test_fora_dia_fixo_sem_linha_na_tela_com_mestra_desligada_nao_registra(self):
        r = self._avisar(CONFIG_MESTRA_OFF)
        self.assertEqual(r["desligado"], 1)
        self.email.assert_not_called()
        self.wpp.assert_not_called()
        self.assertFalse(self._avisado())   # tenta de novo quando a chave geral ligar

    def test_fora_dia_fixo_piloto_manda_copia_de_email_e_whatsapp_e_grava_piloto(self):
        self._estado("PILOTO")
        self._piloto(whatsapp=PILOTO_WPP)
        r = self._avisar()
        self.assertEqual(self.email.call_args[0][0], ["hugo@freshlogbr.com"])
        self.assertIn("iria para cliente@emb.com", self.email.call_args[0][2])
        self.assertEqual(self.wpp.call_args.kwargs["destino_forcado"], PILOTO_WPP)
        self.assertEqual(self._canais(), "piloto")
        self.assertFalse(self._avisado())   # copia de piloto nao conta como recebido
        self.assertEqual(r["registrados"], 1)

    def test_fora_dia_fixo_whatsapp_piloto_sem_numero_nao_envia_whatsapp(self):
        self._estado("PILOTO")
        self._piloto(whatsapp="")
        self._avisar()
        self.wpp.assert_not_called()
        self.assertEqual(self.email.call_args[0][0], ["hugo@freshlogbr.com"])
        self.assertEqual(self._canais(), "piloto")

    def test_fora_dia_fixo_piloto_nao_repete_a_copia_na_rodada_seguinte(self):
        self._estado("PILOTO")
        self._piloto(whatsapp=PILOTO_WPP)
        self._avisar()
        self._avisar()
        self.assertEqual(self.email.call_count, 1)
        self.assertEqual(self.wpp.call_count, 1)

    def test_fora_dia_fixo_depois_do_piloto_ligar_manda_de_verdade_e_troca_a_linha(self):
        self._estado("PILOTO")
        self._piloto(whatsapp=PILOTO_WPP)
        self._avisar()
        self._estado("LIGADO")
        self._avisar()
        self.assertEqual(self.email.call_args[0][0], ["cliente@emb.com"])
        self.assertEqual(self._canais(), "email,whatsapp")
        self.assertTrue(self._avisado())

    def test_fora_dia_fixo_excecao_liberada_no_piloto_vai_de_verdade(self):
        self._estado("PILOTO")
        self._piloto()
        self._controle(cn.definir_excecao, "fora_dia_fixo", CNPJ, "LIBERADO")
        self._avisar()
        self.assertEqual(self.email.call_args[0][0], ["cliente@emb.com"])
        self.assertEqual(self._canais(), "email,whatsapp")

    def test_fora_dia_fixo_sem_cnpj_nao_quebra_e_vale_o_tipo(self):
        self._estado("LIGADO")
        embs = {7: {**EMBS[7], "cnpj": ""}}
        r = nfdf.avisar([_item()], CONFIG, db_path=self.db, embarcadores=embs)
        self.assertEqual(r["emails"], 1)
        self.assertEqual(self.email.call_args[0][0], ["cliente@emb.com"])


class TestTratarComTela(Base):
    """Pela rotina inteira (marca + avisa): o evento bloqueado pela tela e
    consumido, nao sai quando a tela liberar depois."""

    def setUp(self):
        super().setUp()
        for obj, alvo, valor in ((fdf, "DETECCAO_A_PARTIR_DE", date(2026, 10, 5)),
                                 (fdf, "calcular_valor", lambda s, c, t: 784.09)):
            p = mock.patch.object(obj, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        for obj, alvo, valor in ((nfdf.preferencias_notificacao, "carregar_embarcadores", EMBS),
                                 (fdf, "carregar_tipos_carga", {})):
            p = mock.patch.object(obj, alvo, return_value=valor)
            p.start()
            self.addCleanup(p.stop)

    def _tratar(self):
        return fdf.tratar_fora_dia_fixo([_servico()], CONFIG, hoje=HOJE, db_path=self.db)

    def test_fora_dia_fixo_bloqueado_nos_dois_canais_segunda_chamada_nao_envia(self):
        self._estado("LIGADO")
        self._controle(cn.definir_excecao, "fora_dia_fixo", CNPJ, "BLOQUEADO")
        self.assertEqual(self._tratar()["marcados"], 1)
        self.assertEqual(self._canais(), "bloqueado")
        self._controle(cn.definir_excecao, "fora_dia_fixo", CNPJ, None)
        self._tratar()
        self.email.assert_not_called()
        self.wpp.assert_not_called()

    def test_fora_dia_fixo_piloto_entao_ligado_envia_na_rodada_seguinte(self):
        self._estado("PILOTO")
        self._piloto()
        self._tratar()
        self.assertEqual(self.email.call_args[0][0], ["hugo@freshlogbr.com"])
        self._estado("LIGADO")
        self._tratar()
        self.assertEqual(self.email.call_count, 2)
        self.assertEqual(self.email.call_args[0][0], ["cliente@emb.com"])
        self._tratar()
        self.assertEqual(self.email.call_count, 2)


class TestRegistro(Base):
    def test_ja_avisado_ignora_linha_so_de_piloto(self):
        conn = reg.conectar(self.db)
        try:
            reg.registrar_aviso(conn, _servico(), date(2026, 10, 8), ["piloto"])
            self.assertFalse(reg.ja_avisado(conn, _servico()))
            reg.registrar_aviso(conn, _servico(), date(2026, 10, 8), ["email"])
            self.assertTrue(reg.ja_avisado(conn, _servico()))
            self.assertEqual(conn.execute("SELECT canais FROM avisos_fora_dia_fixo").fetchone()[0], "email")
        finally:
            conn.close()

    def test_piloto_nao_apaga_aviso_real(self):
        conn = reg.conectar(self.db)
        try:
            reg.registrar_aviso(conn, _servico(), date(2026, 10, 8), ["email"])
            reg.registrar_aviso(conn, _servico(), date(2026, 10, 8), ["piloto"])
            self.assertTrue(reg.ja_avisado(conn, _servico()))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
