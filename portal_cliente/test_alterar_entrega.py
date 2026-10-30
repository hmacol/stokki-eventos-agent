# -*- coding: utf-8 -*-
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alterar_entrega as ae  # noqa: E402
import regioes_dia_fixo as rdf  # noqa: E402

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


def conn_registro():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE agendamentos_origem (codigo TEXT NOT NULL DEFAULT '', service_id INTEGER NOT NULL DEFAULT 0,"
              " data TEXT NOT NULL, origem TEXT NOT NULL, gravado_em TEXT NOT NULL, PRIMARY KEY (codigo, service_id, data, origem))")
    return c


def validado(**kw):
    base = {"endereco": None, "data": None, "horario": None}
    base.update(kw)
    return base


END_CAMPINAS = {"logradouro": "Rua B", "numero": "20", "complemento": "", "bairro": "Centro",
                "municipio": "Campinas", "uf": "SP", "cep": "13010000"}  # Campinas: quarta


class Planejar(unittest.TestCase):
    def setUp(self):
        self.conn = conn_registro()
        self.em_rota = ae.Avaliacao(True, "", True, 55)
        self.fora = ae.Avaliacao(True, "", False, None)

    def test_planejar_atraso_so_com_endereco_ou_data(self):
        p = ae.planejar(servico(), validado(horario=("08:00", "12:00")), self.em_rota, self.conn, HOJE)
        self.assertFalse(p.tirar_da_rota)
        self.assertEqual(p.avisos, [])
        p = ae.planejar(servico(), validado(data=date(2026, 11, 17)), self.em_rota, self.conn, HOJE)
        self.assertTrue(p.tirar_da_rota)
        self.assertEqual([a["codigo"] for a in p.avisos], ["atraso"])
        self.assertEqual(p.avisos[0]["texto"],
                         "As rotas do dia seguinte são fechadas às 16h e esse pedido já está numa rota. "
                         "Para alterar, ele sai da rota e volta para a programação, e a entrega pode atrasar 1 dia.")
        p = ae.planejar(servico(), validado(data=date(2026, 11, 17)), self.fora, self.conn, HOJE)
        self.assertTrue(p.tirar_da_rota)
        self.assertEqual(p.avisos, [])
        p = ae.planejar(servico(), validado(endereco=END_CAMPINAS), self.fora, self.conn, HOJE)
        self.assertFalse(p.tirar_da_rota)

    def test_planejar_data_fora_do_dia_fixo_avisa_dedicado(self):
        p = ae.planejar(servico(), validado(data=date(2026, 11, 11)), self.fora, self.conn, HOJE)  # quarta em Santos
        self.assertEqual([a["codigo"] for a in p.avisos], ["dedicado"])
        self.assertEqual(p.avisos[0]["texto"],
                         "Essa data não é dia de entrega na região (Terças). A entrega vira dedicada e é cobrada à parte.")
        self.assertEqual((p.data_final, p.origem_data), (date(2026, 11, 11), "CLIENTE"))
        p = ae.planejar(servico(), validado(data=date(2026, 11, 17)), self.fora, self.conn, HOJE)  # terca
        self.assertEqual(p.avisos, [])

    def test_planejar_so_endereco_data_do_dia_fixo_recalcula(self):
        from registro_dia_fixo import ORIGEM_DIA_FIXO, registrar_origem
        s = servico()  # terca 10/11, gravada pelo dia fixo
        registrar_origem(self.conn, s, date(2026, 11, 10), ORIGEM_DIA_FIXO)
        p = ae.planejar(s, validado(endereco=END_CAMPINAS), self.fora, self.conn, HOJE)
        regra = rdf.regra_dia_fixo_do_servico({"address": p.endereco_str})
        self.assertEqual(p.data_final, rdf.proxima_data_valida(regra, date(2026, 11, 10)))
        self.assertEqual(p.data_final, date(2026, 11, 11))
        self.assertEqual(p.origem_data, "DIA_FIXO")
        self.assertEqual(p.avisos, [])

    def test_planejar_so_endereco_amanha_valido_e_escolhido(self):
        from registro_dia_fixo import ORIGEM_DIA_FIXO, registrar_origem
        hoje = date(2026, 11, 10)  # terca; amanha (quarta 11/11) e dia de Campinas
        s = servico()  # terca 10/11, invalida em Campinas
        registrar_origem(self.conn, s, date(2026, 11, 10), ORIGEM_DIA_FIXO)
        p = ae.planejar(s, validado(endereco=END_CAMPINAS), self.fora, self.conn, hoje)
        self.assertEqual(p.data_final, date(2026, 11, 11))

    def test_planejar_so_endereco_data_do_cliente_avisa_dedicado(self):
        p = ae.planejar(servico(), validado(endereco=END_CAMPINAS), self.fora, self.conn, HOJE)
        self.assertIsNone(p.data_final)
        self.assertEqual([a["codigo"] for a in p.avisos], ["dedicado"])


class AtivaPara(unittest.TestCase):
    def test_ativa_para(self):
        cfg = {"portal_cliente": {"alteracao_entrega": {"ativo": False, "piloto_cnpjs": ["00.000.000/0001-91"]}}}
        self.assertTrue(ae.ativa_para(cfg, "00000000000191"))
        self.assertFalse(ae.ativa_para(cfg, "11222333000181"))
        cfg["portal_cliente"]["alteracao_entrega"]["ativo"] = True
        self.assertTrue(ae.ativa_para(cfg, "11222333000181"))
        self.assertFalse(ae.ativa_para(None, "00000000000191"))


if __name__ == "__main__":
    unittest.main()
