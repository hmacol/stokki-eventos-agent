# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from datetime import date, datetime, time

import ciencia_dedicado as cd

AGORA = datetime(2026, 11, 17, 10, 0, 0)


def _item(codigo="#PS-1", sid=10, sender=5, data=date(2026, 11, 20)):
    return {
        "servico": {"id": sid, "code": codigo, "sender_id": sender,
                    "title": f"{codigo} - 123 / EMB / Padaria do Ze",
                    "address": "Rua A, 10, Campinas - SP, 13000-000, Brasil"},
        "regra": {"nome": "Campinas", "regiao": "Interior", "dias": [1, 3]},
        "data": data, "valor": 250.0, "valor_pendente": False,
    }


class TestCienciaDedicado(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = cd.conectar(self.db)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db)

    def test_abrir_cria_pendente_com_lembrete_imediato(self):
        cid = cd.abrir(self.conn, _item(), "12345678000199", AGORA)
        c = cd.buscar(self.conn, cid)
        self.assertEqual(c["situacao"], cd.PENDENTE)
        self.assertEqual(c["codigo"], "PS-1")
        self.assertEqual(c["cnpj"], "12345678000199")
        self.assertEqual(c["data_entrega"], "2026-11-20")
        self.assertEqual(c["proximo_lembrete_em"], "2026-11-17 10:00:00")

    def test_abrir_guarda_regiao_regra_dias_destinatario_e_cidade(self):
        c = cd.buscar(self.conn, cd.abrir(self.conn, _item(), None, AGORA))
        self.assertEqual(c["regiao"], "Interior")
        self.assertEqual(c["regra_nome"], "Campinas")
        self.assertEqual(c["dias_visita"], "Terças e Quintas")
        self.assertEqual(c["destinatario"], "Padaria do Ze")
        self.assertEqual(c["cidade"], "Campinas")

    def test_existe_para_servico_por_codigo_ou_service_id_em_qualquer_situacao(self):
        self.assertFalse(cd.existe_para_servico(self.conn, _item()["servico"]))
        cid = cd.abrir(self.conn, _item(), None, AGORA)
        cd.responder(self.conn, cid, cd.REAGENDADO, "Ana", "1.1.1.1", AGORA, date(2026, 11, 25))
        self.assertTrue(cd.existe_para_servico(self.conn, _item()["servico"]))
        self.assertTrue(cd.existe_para_servico(self.conn, {"id": 10, "code": "#PS-99"}))
        self.assertTrue(cd.existe_para_servico(self.conn, {"id": 77, "code": "PS-1, PS-2"}))
        self.assertFalse(cd.existe_para_servico(self.conn, {"id": 77, "code": "#PS-2"}))

    def test_abrir_nao_duplica_pendente_do_mesmo_codigo(self):
        self.assertIsNotNone(cd.abrir(self.conn, _item(), None, AGORA))
        self.assertIsNone(cd.abrir(self.conn, _item(), None, AGORA))
        self.assertEqual(len(cd.listar(self.conn)), 1)

    def test_abrir_apos_reagendado_cria_linha_nova_e_mantem_a_antiga(self):
        antigo = cd.abrir(self.conn, _item(), None, AGORA)
        cd.responder(self.conn, antigo, cd.REAGENDADO, "Ana", "1.1.1.1", AGORA, date(2026, 11, 25))
        novo = cd.abrir(self.conn, _item(), None, AGORA)
        self.assertNotEqual(novo, antigo)
        self.assertEqual(cd.buscar(self.conn, antigo)["situacao"], cd.REAGENDADO)
        self.assertEqual(cd.buscar(self.conn, novo)["situacao"], cd.PENDENTE)

    def test_codigo_combinado_usa_o_primeiro_e_reentrega_nao_confunde(self):
        a = cd.abrir(self.conn, _item("#PS-1, PS-2"), None, AGORA)
        self.assertEqual(cd.buscar(self.conn, a)["codigo"], "PS-1")
        b = cd.abrir(self.conn, _item("PS-1-R2", sid=11), None, AGORA)
        self.assertIsNotNone(b)
        self.assertEqual(cd.buscar(self.conn, b)["codigo"], "PS-1-R2")

    def test_responder_so_muda_pendente(self):
        cid = cd.abrir(self.conn, _item(), None, AGORA)
        self.assertTrue(cd.responder(self.conn, cid, cd.CIENTE, "Ana", "1.1.1.1", AGORA))
        depois = datetime(2026, 11, 17, 11, 0, 0)
        self.assertFalse(cd.responder(self.conn, cid, cd.REAGENDADO, "Bia", "2.2.2.2", depois,
                                      date(2026, 11, 25)))
        c = cd.buscar(self.conn, cid)
        self.assertEqual((c["situacao"], c["respondido_por"], c["respondido_ip"], c["respondido_em"]),
                         (cd.CIENTE, "Ana", "1.1.1.1", "2026-11-17 10:00:00"))
        self.assertIsNone(c["data_nova"])

    def test_do_sender_filtra_respondidos_pela_data_da_resposta_e_sempre_traz_pendente(self):
        antigo = datetime(2026, 9, 1, 10, 0, 0)   # 77 dias antes de AGORA
        pendente_antigo = cd.abrir(self.conn, _item("#PS-1", 1), None, antigo)
        respondido_agora = cd.abrir(self.conn, _item("#PS-2", 2), None, antigo)
        cd.responder(self.conn, respondido_agora, cd.CIENTE, "Ana", "1.1.1.1", datetime(2026, 11, 10, 9, 0, 0))
        respondido_antes = cd.abrir(self.conn, _item("#PS-3", 3), None, AGORA)
        cd.responder(self.conn, respondido_antes, cd.CIENTE, "Bia", "1.1.1.1", datetime(2026, 10, 1, 9, 0, 0))
        encerrado_agora = cd.abrir(self.conn, _item("#PS-4", 4), None, antigo)
        cd.encerrar(self.conn, encerrado_agora, "data passou", datetime(2026, 11, 16, 9, 0, 0))
        cd.abrir(self.conn, _item("#PS-5", 5, sender=6), None, AGORA)   # outro embarcador
        ids = [c["id"] for c in cd.do_sender(self.conn, 5, agora=AGORA)]
        self.assertEqual(ids, [encerrado_agora, respondido_agora, pendente_antigo])

    def test_pendentes_vencidos_respeita_proximo_lembrete(self):
        cid = cd.abrir(self.conn, _item(), None, AGORA)
        self.assertEqual([c["id"] for c in cd.pendentes_vencidos(self.conn, AGORA)], [cid])
        cd.agendar_proximo(self.conn, cid, AGORA, 4)
        self.assertEqual(cd.pendentes_vencidos(self.conn, datetime(2026, 11, 17, 13, 59)), [])
        self.assertEqual(len(cd.pendentes_vencidos(self.conn, datetime(2026, 11, 17, 14, 0))), 1)

    def test_agendar_proximo_soma_4_horas(self):
        cid = cd.abrir(self.conn, _item(), None, AGORA)
        cd.agendar_proximo(self.conn, cid, AGORA, 4)
        self.assertEqual(cd.buscar(self.conn, cid)["proximo_lembrete_em"], "2026-11-17 14:00:00")

    def test_proximo_envio_permitido(self):
        j = (time(8, 0), time(20, 0))
        self.assertEqual(cd.proximo_envio_permitido(datetime(2026, 11, 17, 7, 59), j),
                         datetime(2026, 11, 17, 8, 0))
        self.assertEqual(cd.proximo_envio_permitido(datetime(2026, 11, 17, 12, 0), j),
                         datetime(2026, 11, 17, 12, 0))
        self.assertEqual(cd.proximo_envio_permitido(datetime(2026, 11, 17, 20, 1), j),
                         datetime(2026, 11, 18, 8, 0))

    def test_envios_guardam_canal_resultado_motivo_e_rodada(self):
        cid = cd.abrir(self.conn, _item(), None, AGORA)
        cd.registrar_envio(self.conn, cid, "chat", "enviado", destino="chamado 9", rodada=0, agora=AGORA)
        cd.registrar_envio(self.conn, cid, "whatsapp", "nao_enviado", motivo="teto", rodada=1, agora=AGORA)
        e = cd.envios(self.conn, cid)
        self.assertEqual([(x["canal"], x["resultado"], x["motivo"], x["rodada"], x["destino"]) for x in e],
                         [("chat", "enviado", "", 0, "chamado 9"), ("whatsapp", "nao_enviado", "teto", 1, "")])
        self.assertEqual(e[0]["codigo"], "PS-1")
        self.assertEqual(cd.rodadas_enviadas(self.conn, cid), 1)

    def test_rodadas_nao_contam_piloto_e_contam_rodada_sem_canal(self):
        cid = cd.abrir(self.conn, _item(), None, AGORA)
        hora = lambda h: datetime(2026, 11, 17, h, 0, 0)
        # rodada so de piloto (com um canal sem cadastro): nao conta
        cd.registrar_envio(self.conn, cid, "email", "piloto", rodada=0, agora=hora(10))
        cd.registrar_envio(self.conn, cid, "whatsapp", "nao_enviado", motivo="sem_whatsapp", rodada=0, agora=hora(10))
        self.assertEqual(cd.rodadas_enviadas(self.conn, cid), 0)
        # rodada em que nenhum canal saiu (sem cadastro): conta
        cd.registrar_envio(self.conn, cid, "email", "nao_enviado", motivo="sem_canal", rodada=0, agora=hora(14))
        self.assertEqual(cd.rodadas_enviadas(self.conn, cid), 1)
        # rodada com e-mail real e WhatsApp de piloto: conta
        cd.registrar_envio(self.conn, cid, "email", "enviado", rodada=1, agora=hora(18))
        cd.registrar_envio(self.conn, cid, "whatsapp", "piloto", rodada=1, agora=hora(18))
        self.assertEqual(cd.rodadas_enviadas(self.conn, cid), 2)
        self.assertEqual(cd.rodadas_piloto(self.conn, cid), 1)

    def test_rodada_so_de_desligado_ou_falha_nao_conta_e_teto_conta(self):
        cid = cd.abrir(self.conn, _item(), None, AGORA)
        hora = lambda h: datetime(2026, 11, 17, h, 0, 0)
        for canal in ("email", "whatsapp", "chat"):
            cd.registrar_envio(self.conn, cid, canal, "nao_enviado", motivo="desligado", agora=hora(8))
        cd.registrar_envio(self.conn, cid, "email", "nao_enviado", motivo="falha", agora=hora(12))
        cd.registrar_envio(self.conn, cid, "chat", "nao_enviado", motivo="desligado", agora=hora(12))
        self.assertEqual((cd.rodadas_enviadas(self.conn, cid), cd.rodadas_piloto(self.conn, cid)), (0, 0))
        cd.registrar_envio(self.conn, cid, "email", "nao_enviado", motivo="falha", agora=hora(16))
        cd.registrar_envio(self.conn, cid, "whatsapp", "nao_enviado", motivo="teto", agora=hora(16))
        self.assertEqual(cd.rodadas_enviadas(self.conn, cid), 1)

    def test_cfg_padrao_desligado_4h_janela_08_20(self):
        for vazio in (None, {}, {"ciencia_dedicado": None}):
            c = cd.cfg(vazio)
            self.assertFalse(c["ativo"])
            self.assertEqual(c["intervalo_horas"], 4)
            self.assertEqual(c["janela"], (time(8, 0), time(20, 0)))
        c = cd.cfg({"ciencia_dedicado": {"ativo": True, "janela": {"inicio": "09:30", "fim": "18:00"},
                                         "forcar_destino": "a@b.c"}})
        self.assertTrue(c["ativo"])
        self.assertEqual(c["janela"], (time(9, 30), time(18, 0)))
        self.assertEqual(c["forcar_destino"], "a@b.c")


if __name__ == "__main__":
    unittest.main()
