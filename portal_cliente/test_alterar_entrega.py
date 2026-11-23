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
        # a data nova vai pro PUT: o pedido tem de sair da rota/rascunho mesmo fora de rota na Vuupt
        self.assertTrue(p.tirar_da_rota)

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


# ── aplicar ────────────────────────────────────────────────────────────────

import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

from regras import alteracoes_entrega as store  # noqa: E402
from regras import complexidade_entrega as ce  # noqa: E402

DOC = "12345678000190"
CTX = {"cnpj_embarcador": "11222333000181", "sender_id": 7, "por": "Maria",
       "destinatario_doc": DOC, "destinatario_nome": "Padaria Boa"}
END_NOVO = {"logradouro": "Rua X", "numero": "10", "complemento": "", "bairro": "Centro",
            "municipio": "Santos", "uf": "SP", "cep": "11010000", "cmun": "3548500"}


class FakeDeps:
    def __init__(self, falha_em=None):
        self.chamadas, self.falha_em, self.avisos = [], falha_em, []

    def _r(self, nome, args, extra=None):
        self.chamadas.append((nome, *args))
        if self.falha_em == nome:
            return {"ok": False, "erro": "x"}
        return dict({"ok": True, "erro": ""}, **(extra or {}))

    def _geo(self, texto):
        self.chamadas.append(("geocodificar", texto))
        return (-23.9, -46.3)

    def deps(self):
        return ae.Dependencias(
            tirar_da_rota=lambda sid: self._r("tirar", (sid,), {"rota_id": 55}),
            gravar_endereco=lambda sid, texto, dados: self._r("endereco", (sid, texto, dados)),
            reagendar=lambda sid, dia, ini, fim: self._r("reagendar", (sid, dia, ini, fim)),
            atualizar_horario_contato=lambda cid, ini, fim: self._r("contato", (cid, ini, fim)),
            geocodificar=self._geo,
            avisar_equipe=lambda info: self.avisos.append(info))

    def nomes(self):
        return [c[0] for c in self.chamadas]


class Aplicar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(ce, "_DB_PATH", Path(self.tmp.name) / "ajustes.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = store.conectar(Path(self.tmp.name) / "dados.db")
        self.addCleanup(self.conn.close)
        self.conn.executescript(
            "CREATE TABLE agendamentos_pedido (pedido TEXT PRIMARY KEY, cnpj_destinatario, nome_destinatario, "
            "cnpj_embarcador, numero_nf, email_embarcador, status, data_agendada, horario_inicio_agendado, "
            "horario_fim_agendado, resposta_texto, solicitado_em, respondido_em, aplicado_vuupt_em);"
            "CREATE TABLE portal_destinatarios (cnpj_embarcador TEXT NOT NULL, documento TEXT NOT NULL, nome TEXT, "
            "horario_inicio TEXT, horario_fim TEXT, requer_agendamento INTEGER NOT NULL DEFAULT 0, "
            "atualizado_em TEXT NOT NULL, PRIMARY KEY (cnpj_embarcador, documento));"
            "CREATE TABLE agendamentos_origem (codigo TEXT NOT NULL DEFAULT '', service_id INTEGER NOT NULL DEFAULT 0,"
            " data TEXT NOT NULL, origem TEXT NOT NULL, gravado_em TEXT NOT NULL,"
            " PRIMARY KEY (codigo, service_id, data, origem));")
        self.s = servico(code="#PS-1234567", customer_id=900)

    def registros(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM alteracoes_entrega_portal")]

    def test_aplicar_ordem_completa(self):
        f = FakeDeps()
        plano = ae.Plano(tirar_da_rota=True, endereco_str=ae.endereco_vuupt(END_NOVO),
                         data_final=date(2026, 11, 12), origem_data="CLIENTE")
        r = ae.aplicar(self.conn, self.s, validado(endereco=END_NOVO, data=date(2026, 11, 12)), plano, CTX, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual(f.nomes(), ["tirar", "geocodificar", "endereco", "reagendar"])
        self.assertEqual(f.chamadas[2][3], {"address": plano.endereco_str, "latitude": -23.9, "longitude": -46.3})
        reg = self.registros()
        self.assertEqual(len(reg), 1)
        self.assertEqual((reg[0]["status"], reg[0]["rota_id_tirada"], reg[0]["service_id"]), ("APLICADA", 55, 1001))
        self.assertEqual(len(f.avisos), 1)
        self.assertIn("saiu da rota", r["mensagem"])

    def test_aplicar_servico_ja_fora_da_rota_nao_tira_de_novo(self):
        f = FakeDeps()
        plano = ae.Plano(tirar_da_rota=False, data_final=date(2026, 11, 12), origem_data="CLIENTE")
        r = ae.aplicar(self.conn, self.s, validado(data=date(2026, 11, 12)), plano, CTX, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertNotIn("tirar", f.nomes())
        # toda alteracao concluida avisa a equipe (WhatsApp no grupo; Hugo, 12/11), mesmo sem sair de rota
        self.assertEqual(len(f.avisos), 1)
        self.assertIsNone(f.avisos[0]["rota_id"])
        self.assertEqual(f.avisos[0]["falha"], "")
        self.assertEqual(f.avisos[0]["mudancas"], ["data 12/11"])
        self.assertIsNone(self.registros()[0]["rota_id_tirada"])
        self.assertNotIn("saiu da rota", r["mensagem"])

    def test_aplicar_falha_depois_de_tirar(self):
        f = FakeDeps(falha_em="endereco")
        plano = ae.Plano(tirar_da_rota=True, endereco_str="Rua X", data_final=date(2026, 11, 12), origem_data="CLIENTE")
        r = ae.aplicar(self.conn, self.s, validado(endereco=END_NOVO, data=date(2026, 11, 12)), plano, CTX, f.deps())
        self.assertFalse(r["ok"])
        self.assertIn("já saiu da rota", r["mensagem"])
        self.assertNotIn("reagendar", f.nomes())
        reg = self.registros()
        self.assertEqual((reg[0]["status"], reg[0]["erro"]), ("FALHOU", "x"))
        self.assertEqual(len(f.avisos), 1)

    def test_aplicar_falha_ao_tirar_nao_avisa_nem_diz_que_saiu(self):
        f = FakeDeps(falha_em="tirar")
        plano = ae.Plano(tirar_da_rota=True, data_final=date(2026, 11, 12), origem_data="CLIENTE")
        r = ae.aplicar(self.conn, self.s, validado(data=date(2026, 11, 12)), plano, CTX, f.deps())
        self.assertFalse(r["ok"])
        self.assertNotIn("já saiu da rota", r["mensagem"])
        self.assertEqual(f.avisos, [])
        self.assertEqual(self.registros()[0]["status"], "FALHOU")

    def test_aplicar_data_usa_horario_do_destinatario(self):
        ce.definir_ajuste_manual(DOC, 1, "13:00", "17:00")
        f = FakeDeps()
        plano = ae.Plano(tirar_da_rota=True, data_final=date(2026, 11, 3), origem_data="CLIENTE")
        ae.aplicar(self.conn, self.s, validado(data=date(2026, 11, 3)), plano, CTX, f.deps())
        self.assertIn(("reagendar", 1001, "2026-11-03", "13:00", "17:00"), f.chamadas)
        row = self.conn.execute("SELECT * FROM agendamentos_pedido WHERE pedido='PS-1234567'").fetchone()
        self.assertEqual(row["data_agendada"], "03/11/2026")
        self.assertIsNotNone(row["aplicado_vuupt_em"])
        self.assertEqual(row["cnpj_embarcador"], CTX["cnpj_embarcador"])

    def test_aplicar_data_sem_ajuste_usa_8_as_18(self):
        f = FakeDeps()
        plano = ae.Plano(tirar_da_rota=False, data_final=date(2026, 11, 3), origem_data="CLIENTE")
        ae.aplicar(self.conn, self.s, validado(data=date(2026, 11, 3)), plano, CTX, f.deps())
        self.assertIn(("reagendar", 1001, "2026-11-03", "08:00", "18:00"), f.chamadas)

    def test_aplicar_data_do_dia_fixo_grava_origem_dia_fixo(self):
        f = FakeDeps()
        plano = ae.Plano(tirar_da_rota=True, endereco_str="Rua X", data_final=date(2026, 11, 11), origem_data="DIA_FIXO")
        ae.aplicar(self.conn, self.s, validado(endereco=END_NOVO), plano, CTX, f.deps())
        linhas = self.conn.execute("SELECT codigo, service_id, data, origem FROM agendamentos_origem").fetchall()
        self.assertEqual([tuple(l) for l in linhas], [("PS-1234567", 1001, "2026-11-11", "DIA_FIXO")])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM agendamentos_pedido").fetchone())

    def test_aplicar_horario_nao_mexe_em_scheduled(self):
        self.conn.execute("INSERT INTO portal_destinatarios VALUES (?,?,?,?,?,?,?)",
                          (CTX["cnpj_embarcador"], DOC, "Padaria Boa", "08:00", "12:00", 1, "2026-10-01"))
        self.conn.commit()
        f = FakeDeps()
        r = ae.aplicar(self.conn, self.s, validado(horario=("14:00", "18:00")), ae.Plano(tirar_da_rota=False), CTX, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertNotIn("reagendar", f.nomes())
        self.assertEqual(f.chamadas[-1], ("contato", 900, "14:00", "18:00"))
        row = self.conn.execute("SELECT * FROM portal_destinatarios").fetchone()
        self.assertEqual((row["horario_inicio"], row["horario_fim"], row["requer_agendamento"]), ("14:00", "18:00", 1))
        ajuste = ce.carregar_ajustes_manuais()[DOC]
        self.assertEqual((ajuste["horario_atendimento_inicio"], ajuste["horario_atendimento_fim"]), ("14:00", "18:00"))

    def test_aplicar_horario_sem_linha_previa_nao_exige_agendamento(self):
        f = FakeDeps()
        ae.aplicar(self.conn, self.s, validado(horario=("14:00", "18:00")), ae.Plano(tirar_da_rota=False), CTX, f.deps())
        self.assertEqual(self.conn.execute("SELECT requer_agendamento FROM portal_destinatarios").fetchone()[0], 0)

    def test_aplicar_falha_do_contato_vira_aviso_e_nao_derruba(self):
        f = FakeDeps(falha_em="contato")
        r = ae.aplicar(self.conn, self.s, validado(horario=("14:00", "18:00")), ae.Plano(tirar_da_rota=False), CTX, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.registros()[0]["status"], "APLICADA")

    def test_registrar_agendamento_sem_envio_id(self):
        import envio_pedidos as ep
        ok = ep.registrar_agendamento_pedido(
            self.conn, {"codigo_pedido": "PS-1234567", "agendamento_data": "2026-11-03",
                        "agendamento_hora_inicio": "08:00", "agendamento_hora_fim": "18:00",
                        "destinatario_doc": DOC, "destinatario_nome": "X",
                        "cnpj_embarcador": CTX["cnpj_embarcador"], "numero_nf": None}, "Maria")
        self.assertTrue(ok)
        self.assertEqual(self.conn.execute("SELECT status FROM agendamentos_pedido").fetchone()[0], "RESPONDIDO")

    def test_registrar_falha_na_aplicada_ainda_avisa_e_devolve_ok(self):
        f = FakeDeps()
        plano = ae.Plano(tirar_da_rota=True, data_final=date(2026, 11, 12), origem_data="CLIENTE")
        with mock.patch.object(store, "registrar", side_effect=sqlite3.OperationalError("database is locked")):
            r = ae.aplicar(self.conn, self.s, validado(data=date(2026, 11, 12)), plano, CTX, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(f.avisos), 1)
        self.assertIn("Alteração feita", r["mensagem"])

    def test_falha_local_depois_do_reagendar_nao_vira_falhou(self):
        f = FakeDeps()
        plano = ae.Plano(tirar_da_rota=False, data_final=date(2026, 11, 11), origem_data="DIA_FIXO")
        import registro_dia_fixo
        with mock.patch.object(registro_dia_fixo, "registrar_origem", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs("portal_alterar_entrega", "WARNING"):
            r = ae.aplicar(self.conn, self.s, validado(), plano, CTX, f.deps())
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.registros()[0]["status"], "APLICADA")

    def test_horario_sem_documento_falha_antes_de_tudo(self):
        f = FakeDeps()
        plano = ae.Plano(tirar_da_rota=True, endereco_str="Rua X")
        ctx = dict(CTX, destinatario_doc="")
        r = ae.aplicar(self.conn, self.s, validado(endereco=END_NOVO, horario=("14:00", "18:00")), plano, ctx, f.deps())
        self.assertFalse(r["ok"])
        self.assertEqual(f.chamadas, [])
        self.assertTrue(r["mensagem"].endswith(CHAT))

    def test_cnpj_formatado_grava_digitos(self):
        f = FakeDeps()
        plano = ae.Plano(tirar_da_rota=False, data_final=date(2026, 11, 3), origem_data="CLIENTE")
        ctx = dict(CTX, cnpj_embarcador="11.222.333/0001-81")
        ae.aplicar(self.conn, self.s, validado(data=date(2026, 11, 3), horario=("14:00", "18:00")), plano, ctx, f.deps())
        self.assertEqual(self.conn.execute("SELECT cnpj_embarcador FROM agendamentos_pedido").fetchone()[0], "11222333000181")
        self.assertEqual(self.conn.execute("SELECT cnpj_embarcador FROM portal_destinatarios").fetchone()[0], "11222333000181")

    def test_excecao_inesperada_nao_vaza_para_o_cliente(self):
        f = FakeDeps()
        deps = f.deps()
        deps.reagendar = lambda *a: (_ for _ in ()).throw(KeyError("x"))
        plano = ae.Plano(tirar_da_rota=True, data_final=date(2026, 11, 3), origem_data="CLIENTE")
        with self.assertLogs("portal_alterar_entrega", "ERROR"):
            r = ae.aplicar(self.conn, self.s, validado(data=date(2026, 11, 3)), plano, CTX, deps)
        self.assertFalse(r["ok"])
        self.assertNotIn("KeyError", r["mensagem"])
        self.assertIn("erro inesperado", r["mensagem"])
        self.assertIn("KeyError", self.registros()[0]["erro"])


if __name__ == "__main__":
    unittest.main()
