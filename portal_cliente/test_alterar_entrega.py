# -*- coding: utf-8 -*-
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alterar_entrega as ae  # noqa: E402

HOJE = date(2026, 11, 9)  # segunda; terca 10/11 e quarta 11/11 sao dias uteis
SANTOS = "Rua A, 10, Centro, Santos - SP, 11010-000, Brasil"  # Baixada Santista: terca
CHAT = "Fale com a Fresh Log pelo chat do portal."


def cep_falso(cep):
    return {"ibge": "3548500", "logradouro": "Rua X", "bairro": "Centro", "cidade": "Santos", "uf": "SP"} \
        if cep == "11010000" else None


def servico(**kw):
    base = {"id": 1001, "title": "#PS-1234567", "status": "not_assigned", "address": SANTOS,
            "scheduled_start": "2026-11-10 15:00:00"}
    base.update(kw)
    return base


class Avaliar(unittest.TestCase):
    def test_avaliar_bloqueia_cada_caso(self):
        casos = [
            (servico(status="canceled"), {}, None),
            (servico(title="[RETIRADA] #PS-1"), {}, None),
            (servico(status="on_route"), {}, None),
            (servico(status="done"), {}, None),
            (servico(status="arrived"), {}, None),
            (servico(status="algo_novo"), {}, None),
            (servico(status=None), {}, None),
            (servico(), {}, "INSUCESSO"),
            (servico(), {}, "ENTREGUE"),
            (servico(route_id=7), {7: {"iniciada_em": "2026-11-10 08:00:00"}}, None),
            (servico(route_id=7), {7: {"status": "EM_ROTA"}}, None),
            (servico(route_id=7), {7: {"status_provedor": "started"}}, None),
            (servico(route_id=7), {}, None),
            (servico(route_id=7), {8: {"agent_id": 3}}, None),
            (servico(route_id=7), {7: {"agent_id": 99}}, None),
        ]
        for s, rotas, nucleo in casos:
            r = ae.avaliar(s, rotas, nucleo, 99)
            self.assertFalse(r.pode, s)
            self.assertTrue(r.motivo.endswith(CHAT), r.motivo)

    def test_avaliar_pool_e_rota_nao_iniciada(self):
        r = ae.avaliar(servico(), {}, "ABERTO", 99)
        self.assertTrue(r.pode)
        self.assertFalse(r.em_rota)
        r = ae.avaliar(servico(route_id=55), {55: {"status": "RASCUNHO", "agent_id": 3}}, "EM_ROTA", 99)
        self.assertTrue(r.pode)
        self.assertTrue(r.em_rota)
        self.assertEqual(r.rota_id, 55)

    def test_avaliar_route_id_texto_acha_rota_do_nucleo(self):
        r = ae.avaliar(servico(status="assigned", route_id="55"), {55: {"status": "RASCUNHO"}}, None, 99)
        self.assertTrue(r.pode)
        self.assertEqual(r.rota_id, 55)
        r = ae.avaliar(servico(route_id="55"), {55: {"iniciada_em": "2026-11-10 08:00:00"}}, None, 99)
        self.assertFalse(r.pode)

    def test_avaliar_rota_fora_do_nucleo_pede_tentar_depois(self):
        r = ae.avaliar(servico(route_id=7), {}, None, 99)
        self.assertEqual(r.motivo, "A rota desse pedido ainda está sendo atualizada, "
                                   "tente de novo em alguns minutos. " + CHAT)


class Validar(unittest.TestCase):
    def test_validar_recusa(self):
        for corpo in ({"data": "2026-11-09"}, {"horario": {"inicio": "08:00", "fim": "25:00"}},
                      {"horario": {"inicio": "10:00", "fim": "09:00"}},
                      {"horario": {"inicio": "10:00", "fim": "10:00"}}):
            with self.assertRaises(ae.ErroAlteracao, msg=corpo):
                ae.validar(corpo, HOJE, cep_falso)
        with self.assertRaises(ae.ErroAlteracao) as c:
            ae.validar({"endereco": {"cep": "99999999", "numero": "1"}}, HOJE, cep_falso)
        self.assertEqual(str(c.exception), "CEP não encontrado")
        with self.assertRaises(ae.ErroAlteracao) as c:
            ae.validar({}, HOJE, cep_falso)
        self.assertEqual(str(c.exception), "Nada foi alterado.")

    def test_validar_bloco_que_nao_e_dict(self):
        for corpo in ({"endereco": "Rua X"}, {"horario": "08:00"}, {"horario": ["08:00", "10:00"]}):
            with self.assertRaises(ae.ErroAlteracao):
                ae.validar(corpo, HOJE, cep_falso)

    def test_validar_aceita(self):
        v = ae.validar({"endereco": {"cep": "11010000", "numero": "10"}, "data": "2026-11-10",
                        "horario": {"inicio": "08:00", "fim": "12:00"}}, HOJE, cep_falso)
        self.assertEqual(v["data"], date(2026, 11, 10))
        self.assertEqual(v["horario"], ("08:00", "12:00"))
        self.assertEqual(v["endereco"]["municipio"], "Santos")

    def test_endereco_vuupt(self):
        end = {"logradouro": "Rua X", "numero": "10", "complemento": "", "bairro": "Centro",
               "municipio": "Santos", "uf": "SP", "cep": "11010000"}
        self.assertEqual(ae.endereco_vuupt(end), "Rua X, 10, Centro, Santos - SP, 11010000, Brasil")


def validado(**kw):
    base = {"endereco": None, "data": None, "horario": None}
    base.update(kw)
    return base


END_CAMPINAS = {"logradouro": "Rua B", "numero": "20", "complemento": "", "bairro": "Centro",
                "municipio": "Campinas", "uf": "SP", "cep": "13010000"}


class AtivaPara(unittest.TestCase):
    def test_ativa_para(self):
        cfg = {"portal_cliente": {"alteracao_entrega": {"ativo": False, "piloto_cnpjs": ["00.000.000/0001-91"]}}}
        self.assertTrue(ae.ativa_para(cfg, "00000000000191"))
        self.assertFalse(ae.ativa_para(cfg, "11222333000181"))
        cfg["portal_cliente"]["alteracao_entrega"]["ativo"] = True
        self.assertTrue(ae.ativa_para(cfg, "11222333000181"))
        self.assertFalse(ae.ativa_para(None, "00000000000191"))


class TextoChamado(unittest.TestCase):
    ATUAL = {"endereco": SANTOS, "data": date(2026, 11, 10), "horario": ("00:00", "23:59")}

    def test_texto_com_os_tres(self):
        t = ae.texto_chamado("PS-1234567", "Padaria Boa", self.ATUAL,
                             validado(endereco=END_CAMPINAS, data=date(2026, 11, 11), horario=("08:00", "12:00")))
        self.assertEqual(t.split("\n"), [
            "Quero alterar a entrega do pedido PS-1234567 (Padaria Boa):",
            f"- Endereço: Rua B, 20, Centro, Campinas - SP, 13010000, Brasil (hoje: {SANTOS})",
            "- Data da entrega: 11/11/2026 (hoje: 10/11/2026)",
            "- Horário de recebimento: 08:00 às 12:00 (hoje: dia todo)",
        ])

    def test_texto_so_horario_sem_destinatario_nem_data(self):
        atual = {"endereco": "", "data": None, "horario": ("08:00:00", "18:00:00")}
        t = ae.texto_chamado("PS-1", "", atual, validado(horario=("09:00", "11:00")))
        self.assertEqual(t, "Quero alterar a entrega do pedido PS-1:\n"
                            "- Horário de recebimento: 09:00 às 11:00 (hoje: 08:00 às 18:00)")
        t = ae.texto_chamado("PS-1", "", atual, validado(data=date(2026, 11, 11)))
        self.assertIn("(hoje: sem data agendada)", t)


import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

import chamados as ch  # noqa: E402

CLIENTE = {"cnpj": "00000000000191", "sender_id": 7, "nome": "Cliente Teste"}
OFFLINE = {"estado": "fora", "texto": "Estamos fora do horário", "horario": "seg a sex, 8h às 18h"}


class AbrirChamado(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch.object(ch, "DB_PATH", Path(tmp.name) / "dados.db"),
                  mock.patch.object(ch, "em_segundo_plano"),
                  mock.patch.object(ch, "whatsapp_para_atendimento"),
                  mock.patch.object(ch, "emails_do_cliente", return_value=["cliente@x.com"])):
            p.start()
            self.addCleanup(p.stop)

    def _chamado(self, cid):
        conn = ch.conectar()
        try:
            return ch.buscar_chamado(conn, cid), ch.mensagens(conn, cid)
        finally:
            conn.close()

    def test_fora_do_horario_fica_aguardando_a_equipe(self):
        with mock.patch.object(ch, "situacao_atendimento", return_value=OFFLINE):
            cid = ae.abrir_chamado(CLIENTE, "PS-1234567", "Quero alterar a entrega do pedido PS-1234567:", {})
        chamado, msgs = self._chamado(cid)
        self.assertEqual((chamado["area"], chamado["pedido_ref"], chamado["status"], chamado["cnpj_embarcador"]),
                         ("entrega", "PS-1234567", ch.STATUS_AGUARDANDO_FL, "00000000000191"))
        self.assertEqual(chamado["assunto"], "Alterar entrega PS-1234567")
        self.assertEqual(msgs[0]["origem"], ch.ORIGEM_CLIENTE)
        self.assertEqual(msgs[0]["texto"], "Quero alterar a entrega do pedido PS-1234567:")
        self.assertIn("Estamos fora do horário", msgs[-1]["texto"])
        fns = [c.args[0] for c in ch.em_segundo_plano.call_args_list]
        self.assertEqual(fns, [ch.email_para_atendimento, ch.email_confirmacao_cliente])
        ch.whatsapp_para_atendimento.assert_called_once()

    def test_equipe_online_vai_pra_fila(self):
        with mock.patch.object(ch, "situacao_atendimento", return_value=dict(OFFLINE, estado="online")):
            cid = ae.abrir_chamado(CLIENTE, "PS-1", "texto", {})
        self.assertEqual(self._chamado(cid)[0]["status"], ch.STATUS_NA_FILA)


if __name__ == "__main__":
    unittest.main()
