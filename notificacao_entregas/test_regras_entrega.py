# -*- coding: utf-8 -*-
"""Tabela de estados do DOC_EXECUCAO_CLAUDE_NOTIFICACAO_ENTREGAS.md, linha a linha.

    py -3.11 -m unittest notificacao_entregas.test_regras_entrega
"""
import unittest
from datetime import datetime, timedelta, timezone

from notificacao_entregas import regras_entrega as r

AGORA = datetime(2026, 9, 17, 15, 0, 0, tzinfo=timezone.utc)


def _utc(minutos_atras: int) -> str:
    """completed_at no formato da Vuupt: UTC, sem fuso."""
    return (AGORA - timedelta(minutes=minutos_atras)).strftime("%Y-%m-%d %H:%M:%S")


def _servico(status_done="success", fotos=2, minutos_atras=5, **extra) -> dict:
    s = {
        "id": 111, "code": "#PS-36327", "title": "NF 123 / Mercado X", "sender_id": 900,
        "status": "done", "status_done": status_done, "completed_at": _utc(minutos_atras),
        "checklistAnswers": {"data": [{"id": 77, "images_quantity": fotos}]} if fotos is not None else {"data": []},
    }
    s.update(extra)
    return s


EMBARCADOR = {"nome": "Fruta Boa", "emails": ["log@frutaboa.com"], "notificar_email": 1, "fator_ponderado": 1.0}
CFG = r.ConfigEntregas(ativo=True)


class TestDecidir(unittest.TestCase):
    def test_estado_final_nao_faz_nada(self):
        for estado in ("ENVIADO", "IGNORADO", "SEM_DESTINATARIO", "ERRO_ENVIO"):
            d = r.decidir(_servico(), EMBARCADOR, CFG, AGORA, estado_atual=estado)
            self.assertEqual(d.acao, r.NADA, estado)

    def test_flag_desligada_ignora_pra_nao_ter_rajada_retroativa(self):
        d = r.decidir(_servico(), EMBARCADOR, r.ConfigEntregas(ativo=False), AGORA)
        self.assertEqual((d.acao, d.motivo), (r.IGNORAR, "desligado"))

    def test_codigo_fora_do_padrao_ps(self):
        d = r.decidir(_servico(code="COLETA-9"), EMBARCADOR, CFG, AGORA)
        self.assertEqual(d.acao, r.IGNORAR)

    def test_retirada_no_galpao_envia_na_hora_sem_esperar_canhoto(self):
        # retirada e fechada por API (acompanhar_retiradas): nunca tem checklist
        d = r.decidir(_servico(title="[RETIRADA] #PS-1 / X", fotos=None, minutos_atras=1), EMBARCADOR, CFG, AGORA)
        self.assertEqual((d.acao, d.com_canhoto), (r.ENVIAR, False))

    def test_retirada_sem_sucesso_nao_notifica(self):
        d = r.decidir(_servico(title="[RETIRADA] #PS-1 / X", status_done="failed"), EMBARCADOR, CFG, AGORA)
        self.assertEqual((d.acao, d.motivo), (r.IGNORAR, "retirada sem sucesso"))

    def test_retirada_respeita_as_mesmas_travas(self):
        s = _servico(title="[RETIRADA] #PS-1 / X", fotos=None)
        self.assertEqual(r.decidir(s, EMBARCADOR, r.ConfigEntregas(ativo=False), AGORA).acao, r.IGNORAR)
        self.assertEqual(r.decidir(s, {**EMBARCADOR, "desligado": True}, CFG, AGORA).acao, r.SEM_DESTINATARIO)

    def test_sem_sender_id(self):
        d = r.decidir(_servico(sender_id=None), None, CFG, AGORA)
        self.assertEqual(d.acao, r.IGNORAR)

    def test_concluido_ha_mais_de_12h_nao_dispara_email_velho(self):
        d = r.decidir(_servico(minutos_atras=13 * 60), EMBARCADOR, CFG, AGORA)
        self.assertEqual((d.acao, d.motivo), (r.IGNORAR, "antigo"))

    def test_fora_do_piloto(self):
        cfg = r.ConfigEntregas(ativo=True, embarcadores_piloto=[1, 2])
        self.assertEqual(r.decidir(_servico(), EMBARCADOR, cfg, AGORA).motivo, "fora do piloto")
        cfg = r.ConfigEntregas(ativo=True, embarcadores_piloto=[900])
        self.assertEqual(r.decidir(_servico(), EMBARCADOR, cfg, AGORA).acao, r.ENVIAR)

    def test_sem_destinatario(self):
        self.assertEqual(r.decidir(_servico(), None, CFG, AGORA).acao, r.SEM_DESTINATARIO)
        self.assertEqual(r.decidir(_servico(), {**EMBARCADOR, "emails": []}, CFG, AGORA).acao, r.SEM_DESTINATARIO)
        self.assertEqual(r.decidir(_servico(), {**EMBARCADOR, "notificar_email": 0}, CFG, AGORA).acao,
                         r.SEM_DESTINATARIO)

    def test_desligado_pelo_cliente_no_portal(self):
        d = r.decidir(_servico(), {**EMBARCADOR, "desligado": True}, CFG, AGORA)
        self.assertEqual(d.acao, r.SEM_DESTINATARIO)

    def test_falha_envia_na_hora_mesmo_sem_foto(self):
        d = r.decidir(_servico(status_done="failed", fotos=0, minutos_atras=1), EMBARCADOR, CFG, AGORA)
        self.assertEqual((d.acao, d.com_canhoto), (r.ENVIAR, False))

    def test_sucesso_com_canhoto_envia_com_anexo(self):
        d = r.decidir(_servico(fotos=1), EMBARCADOR, CFG, AGORA)
        self.assertEqual((d.acao, d.com_canhoto), (r.ENVIAR, True))

    def test_sucesso_sem_canhoto_espera_ate_30_min(self):
        d = r.decidir(_servico(fotos=0, minutos_atras=29), EMBARCADOR, CFG, AGORA)
        self.assertEqual(d.acao, r.ESPERAR)
        d = r.decidir(_servico(fotos=None, minutos_atras=29), EMBARCADOR, CFG, AGORA, estado_atual="AGUARDANDO_CANHOTO")
        self.assertEqual(d.acao, r.ESPERAR)

    def test_sucesso_sem_canhoto_vencido_envia_sem_anexo(self):
        d = r.decidir(_servico(fotos=0, minutos_atras=31), EMBARCADOR, CFG, AGORA, estado_atual="AGUARDANDO_CANHOTO")
        self.assertEqual((d.acao, d.com_canhoto), (r.ENVIAR, False))

    def test_canhoto_que_chega_durante_a_espera_envia_com_anexo(self):
        d = r.decidir(_servico(fotos=1, minutos_atras=12), EMBARCADOR, CFG, AGORA, estado_atual="AGUARDANDO_CANHOTO")
        self.assertEqual((d.acao, d.com_canhoto), (r.ENVIAR, True))

    def test_falha_de_envio_anterior_tenta_de_novo(self):
        d = r.decidir(_servico(), EMBARCADOR, CFG, AGORA, estado_atual="FALHA_ENVIO")
        self.assertEqual(d.acao, r.ENVIAR)

    def test_sem_completed_at_ignora(self):
        d = r.decidir(_servico(completed_at=None), EMBARCADOR, CFG, AGORA)
        self.assertEqual(d.acao, r.IGNORAR)


class TestHelpers(unittest.TestCase):
    def test_completed_at_da_vuupt_e_utc_sem_fuso(self):
        self.assertEqual(r.concluido_em_local("2026-09-17 12:22:19"), "17/09/2026 09:22")
        self.assertEqual(r.concluido_em_local("2026-09-17T12:22:19Z"), "17/09/2026 09:22")
        self.assertEqual(r.concluido_em_local("2026-09-18 01:10:00"), "17/09/2026 22:10")
        self.assertEqual(r.concluido_em_local(None), "")

    def test_codigo_limpo(self):
        self.assertEqual(r.codigo_limpo("#PS-36327"), "PS-36327")
        self.assertEqual(r.codigo_limpo(" #ps-36327-r2 "), "PS-36327-R2")

    def test_codigo_base_pra_buscar_nf(self):
        self.assertEqual(r.codigo_base("#PS-36327-R2"), "PS-36327")

    def test_volumes_desfaz_o_ponderado(self):
        self.assertEqual(r.volumes_reais(12, 1.0), 12)
        self.assertEqual(r.volumes_reais(12, 0.5), 24)
        self.assertEqual(r.volumes_reais("18.0", 6.0), 3)

    def test_volumes_omitidos_quando_nao_da_inteiro_ou_sem_fator(self):
        self.assertIsNone(r.volumes_reais(10, 6.0))
        self.assertIsNone(r.volumes_reais(10, None))
        self.assertIsNone(r.volumes_reais(10, 0))
        self.assertIsNone(r.volumes_reais(0, 1.0))
        self.assertIsNone(r.volumes_reais("x", 1.0))

    def test_checklist_id_so_com_foto(self):
        self.assertEqual(r.checklist_id_com_foto(_servico(fotos=2)), 77)
        self.assertIsNone(r.checklist_id_com_foto(_servico(fotos=0)))
        self.assertIsNone(r.checklist_id_com_foto(_servico(fotos=None)))
        # a foto pode estar no 2o checklist do servico
        s = _servico()
        s["checklistAnswers"] = {"data": [{"id": 1, "images_quantity": 0}, {"id": 2, "images_quantity": 3}]}
        self.assertEqual(r.checklist_id_com_foto(s), 2)

    def test_config_defaults_sao_os_seguros(self):
        cfg = r.carregar_cfg({})
        self.assertFalse(cfg.ativo)
        self.assertEqual(cfg.forcar_destino, "hugo@freshlogbr.com")
        self.assertEqual((cfg.espera_canhoto_min, cfg.max_atraso_horas), (30, 12))

    def test_config_forcar_destino_vazio_explicito_libera_envio_real(self):
        cfg = r.carregar_cfg({"notificacao_entregas": {"ativo": True, "forcar_destino": "",
                                                       "embarcadores_piloto": ["900"]}})
        self.assertTrue(cfg.ativo)
        self.assertEqual(cfg.forcar_destino, "")
        self.assertEqual(cfg.embarcadores_piloto, [900])

    def test_destinos_finais(self):
        cfg = r.ConfigEntregas(ativo=True, forcar_destino="hugo@freshlogbr.com", cc=["ops@freshlogbr.com"])
        self.assertEqual(r.destinos_finais(EMBARCADOR, cfg), (["hugo@freshlogbr.com"], []))
        cfg = r.ConfigEntregas(ativo=True, forcar_destino="", cc=["ops@freshlogbr.com"])
        self.assertEqual(r.destinos_finais(EMBARCADOR, cfg), (["log@frutaboa.com"], ["ops@freshlogbr.com"]))


if __name__ == "__main__":
    unittest.main()
