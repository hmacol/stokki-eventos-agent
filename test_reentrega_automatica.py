# -*- coding: utf-8 -*-
"""
test_reentrega_automatica.py

Reentrega automática de insucesso sem resposta em 12h (Hugo, 28/09) e a
reentrega copiando as caixas do original.

Rodar (da raiz):
    python -m unittest test_reentrega_automatica -v
"""
import unittest
from datetime import datetime, timezone
from unittest import mock

import expedir_pedidos as ep

AGORA = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
LIVRE = dict(duplicado=False, agendado=False, respondido=False, tratado_na_torre=False)
ABERTO = dict(aberto_stokki=True)


def _servico(horas_atras=13, **extra):
    concluido = AGORA.timestamp() - horas_atras * 3600
    s = {"id": 1, "code": "#PS-100", "title": "Cliente X", "failed_reason_id": 8490,
         "completed_at": datetime.fromtimestamp(concluido, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    s.update(extra)
    return s


class TestDecidirReentregaAuto(unittest.TestCase):
    def test_passou_12h_sem_decisao_reentrega(self):
        ok, _ = ep.decidir_reentrega_auto(_servico(13), AGORA, **LIVRE, **ABERTO)
        self.assertTrue(ok)

    def test_antes_de_12h_espera(self):
        ok, motivo = ep.decidir_reentrega_auto(_servico(11), AGORA, **LIVRE, **ABERTO)
        self.assertFalse(ok)
        self.assertIn("12h", motivo)

    def test_decisao_humana_ou_do_cliente_prevalece(self):
        for chave in LIVRE:
            with self.subTest(chave=chave):
                ok, _ = ep.decidir_reentrega_auto(_servico(30), AGORA, **{**LIVRE, chave: True}, **ABERTO)
                self.assertFalse(ok)

    def test_motivo_que_nao_reenvia_sozinho(self):
        ok, motivo = ep.decidir_reentrega_auto(_servico(30, failed_reason_id=8366), AGORA, **LIVRE, **ABERTO)
        self.assertFalse(ok)
        self.assertIn("não reenvia sozinho", motivo)

    def test_teto_da_cadeia(self):
        ok, _ = ep.decidir_reentrega_auto(_servico(30, code="#PS-100-R1"), AGORA, **LIVRE, **ABERTO)
        self.assertTrue(ok)
        ok, motivo = ep.decidir_reentrega_auto(_servico(30, code="#PS-100-R2"), AGORA, **LIVRE, **ABERTO)
        self.assertFalse(ok)
        self.assertIn("decisão humana", motivo)

    def test_trava_de_seguranca_da_revisao(self):
        casos = {
            "velho demais": (_servico(50), {}),
            "anterior à regra": (_servico(13, completed_at="2026-09-28 20:00:00"), {}),
            "combinado": (_servico(20, code="#PS-1, #PS-2"), {}),
            "fechado na Stokki": (_servico(20), {"aberto_stokki": False}),
            "sem retrato": (_servico(20), {"aberto_stokki": None}),
            "recriado à mão": (_servico(20), {"reentrega_na_vuupt": True}),
            "motivo fora do de-para": (_servico(20, failed_reason_id=99999), {}),
            "não reconheceu": (_servico(20, failed_reason_id=5563), {}),
        }
        for nome, (serv, extra) in casos.items():
            with self.subTest(nome):
                ok, _ = ep.decidir_reentrega_auto(serv, AGORA, **LIVRE, **{**ABERTO, **extra})
                self.assertFalse(ok)

    def test_retirada_nao_reenvia(self):
        ok, _ = ep.decidir_reentrega_auto(_servico(30, title="[RETIRADA] X"), AGORA, **LIVRE, **ABERTO)
        self.assertFalse(ok)


class TestDuplicarCopiaCaixas(unittest.TestCase):
    def test_reentrega_leva_caixas_nota_e_skills(self):
        vuupt = mock.Mock()
        vuupt.buscar_servico_por_code.return_value = None
        vuupt.criar_servico.return_value = {"id": 9}
        original = {"id": 1, "code": "#PS-100", "title": "T", "dimension_3": 7, "note": "portaria",
                    "skills": [{"id": 11, "name": "Seco-2"}], "customer_id": 5, "sender_id": 6}
        ep.duplicar_servico_por_insucesso(vuupt, original)
        payload = vuupt.criar_servico.call_args[0][0]
        self.assertEqual(payload["dimension_3"], 7)
        self.assertEqual(payload["note"], "portaria")
        self.assertEqual(payload["skills"], [{"id": 11}])
        self.assertEqual(payload["code"], "#PS-100-R1")


class TestReentregarOrquestracao(unittest.TestCase):
    def test_cria_so_quem_venceu_e_registra(self):
        vencido, novo = _servico(20, id=1, code="#PS-1"), _servico(2, id=2, code="#PS-2")
        marcados = {}
        with mock.patch.object(ep.fingerprint_duplicacao_insucesso, "ja_duplicado", side_effect=lambda sid: sid in marcados), \
             mock.patch.object(ep.fingerprint_duplicacao_insucesso, "marcar_duplicado", side_effect=marcados.__setitem__), \
             mock.patch.object(ep.fingerprint_duplicacao_agendada, "ja_agendado", return_value=False), \
             mock.patch("fingerprint_aguardando_resposta.ja_respondido", return_value=False), \
             mock.patch.object(ep, "_ids_tratados_na_torre", return_value=set()), \
             mock.patch.object(ep, "_fatos_do_banco", return_value=({"PS-1", "PS-2"}, set())), \
             mock.patch("aplicar_resposta_insucesso._adquirir_trava", return_value=True), \
             mock.patch("aplicar_resposta_insucesso._liberar_trava"), \
             mock.patch.object(ep, "VuuptClient"), \
             mock.patch.object(ep, "duplicar_servico_por_insucesso", return_value={"code": "#PS-1-R1"}) as dup, \
             mock.patch.object(ep.tratativas, "registrar_evento") as evento:
            r = ep.reentregar_insucessos_sem_resposta("t", [vencido, novo], modo_teste=False, agora=AGORA)
        self.assertEqual(r["criadas"], ["PS-1 -> #PS-1-R1"])
        dup.assert_called_once()
        self.assertEqual(marcados, {1: "#PS-1-R1"})
        self.assertEqual(evento.call_args[0][2], "REENVIO_AUTOMATICO")

    def test_trava_ocupada_adia(self):
        with mock.patch.object(ep.fingerprint_duplicacao_insucesso, "ja_duplicado", return_value=False), \
             mock.patch.object(ep.fingerprint_duplicacao_agendada, "ja_agendado", return_value=False), \
             mock.patch("fingerprint_aguardando_resposta.ja_respondido", return_value=False), \
             mock.patch.object(ep, "_ids_tratados_na_torre", return_value=set()), \
             mock.patch.object(ep, "_fatos_do_banco", return_value=({"PS-100"}, set())), \
             mock.patch("aplicar_resposta_insucesso._adquirir_trava", return_value=False), \
             mock.patch.object(ep, "VuuptClient"), \
             mock.patch.object(ep, "duplicar_servico_por_insucesso") as dup:
            r = ep.reentregar_insucessos_sem_resposta("t", [_servico(20)], modo_teste=False, agora=AGORA)
        dup.assert_not_called()
        self.assertEqual(r["criadas"], [])


if __name__ == "__main__":
    unittest.main()
