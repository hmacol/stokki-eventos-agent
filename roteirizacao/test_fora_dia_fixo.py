# -*- coding: utf-8 -*-
"""
Data do embarcador fora do dia de visita da região vira dedicado (dias
fixos v2, Hugo 03/10). Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_fora_dia_fixo -v
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import fora_dia_fixo as fdf
import km_rodoviario
import pedidos_dedicados
import registro_dia_fixo as reg

CAMPINAS = "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"   # só quarta
SAO_PAULO = "Rua Augusta 100, Consolação, São Paulo - SP, 01304-000, Brasil"
HOJE = date(2026, 10, 6)        # terça
QUARTA, QUINTA = "2026-10-07", "2026-10-08"


def _servico(i=1, endereco=CAMPINAS, data=QUINTA, criado="2026-10-06 15:00:00", code=None, **extra):
    s = {"id": i, "code": code or f"#PS-{1000 + i}", "address": endereco, "sender_id": 7, "dimension_3": 5,
         "latitude": -22.905, "longitude": -47.060, "created_at": criado, "title": f"Cliente {i}"}
    if data:
        s["scheduled_start"] = f"{data}T08:00:00-03:00"
    s.update(extra)
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        for alvo, valor in (("DETECCAO_A_PARTIR_DE", date(2026, 10, 6)), ("calcular_valor", lambda s, c, t: 784.09)):
            p = mock.patch.object(fdf, alvo, valor)
            p.start()
            self.addCleanup(p.stop)

    def _marcar(self, servicos):
        return fdf.marcar_fora_dia_fixo(servicos, {}, hoje=HOJE, db_path=self.db)

    def _ativos(self):
        conn = pedidos_dedicados.conectar(self.db)
        try:
            return pedidos_dedicados.ativos_por_codigo(conn)
        finally:
            conn.close()


class TestDeteccaoEMarcacao(Base):
    def test_data_do_embarcador_fora_do_dia_marca(self):
        marcados = self._marcar([_servico()])
        self.assertEqual(len(marcados), 1)
        self.assertEqual((marcados[0]["data"], marcados[0]["valor"], marcados[0]["valor_pendente"]),
                         (date(2026, 10, 8), 784.09, False))
        self.assertEqual(marcados[0]["regra"]["regiao"], "Campinas")
        linha = self._ativos()["PS-1001"]
        self.assertEqual((linha["valor"], linha["marcado_por"], linha["service_id"], linha["sender_id"]),
                         (784.09, fdf.POR, 1, 7))
        self.assertEqual(fdf.POR, pedidos_dedicados.POR_FORA_DIA_FIXO)

    def test_data_no_dia_de_visita_nao_marca(self):
        self.assertEqual(self._marcar([_servico(data=QUARTA)]), [])
        self.assertEqual(self._ativos(), {})

    def test_data_gravada_pelo_dia_fixo_nao_marca(self):
        s = _servico()
        conn = reg.conectar(self.db)
        reg.registrar_origem(conn, s, date(2026, 10, 8), reg.ORIGEM_DIA_FIXO)
        conn.close()
        self.assertEqual(self._marcar([s]), [])

    def test_data_reagendada_pela_equipe_nao_marca(self):
        # Hugo, 03/10: data posta pela equipe no Planejamento (só service_id) não vira dedicado
        conn = reg.conectar(self.db)
        reg.registrar_origem(conn, {"id": 1}, date(2026, 10, 8), reg.ORIGEM_EQUIPE)
        conn.close()
        self.assertEqual(self._marcar([_servico()]), [])
        self.assertEqual(self._ativos(), {})

    def test_equipe_em_outra_data_nao_protege_a_data_nova_do_cliente(self):
        conn = reg.conectar(self.db)
        reg.registrar_origem(conn, {"id": 1}, date(2026, 10, 7), reg.ORIGEM_EQUIPE)
        conn.close()
        self.assertEqual(len(self._marcar([_servico()])), 1)   # cliente trocou pra 08/10 depois

    def test_sem_regiao_de_dia_fixo_nao_marca(self):
        self.assertEqual(self._marcar([_servico(endereco=SAO_PAULO)]), [])

    def test_sem_agendamento_nao_marca(self):
        self.assertEqual(self._marcar([_servico(data=None)]), [])

    def test_ja_dedicado_nao_remarca(self):
        conn = pedidos_dedicados.conectar(self.db)
        pedidos_dedicados.marcar(conn, [{"codigo_pedido": "PS-1001"}], 500.0, "hugo")
        antes = pedidos_dedicados.ativos_por_codigo(conn)["PS-1001"]
        conn.close()
        self.assertEqual(self._marcar([_servico()]), [])
        depois = self._ativos()["PS-1001"]
        self.assertEqual((depois["valor"], depois["marcado_por"], depois["marcado_em"]),
                         (500.0, "hugo", antes["marcado_em"]))

    def test_codigo_combinado_com_um_ja_dedicado(self):
        conn = pedidos_dedicados.conectar(self.db)
        pedidos_dedicados.marcar(conn, [{"codigo_pedido": "PS-2002"}], 300.0, "hugo")
        conn.close()
        self.assertEqual(self._marcar([_servico(code="#PS-1001, PS-2002")]), [])
        self.assertEqual(set(self._ativos()), {"PS-2002"})

    def test_falha_da_calculadora_marca_com_zero_e_valor_pendente(self):
        with mock.patch.object(fdf, "calcular_valor", lambda s, c, t: None):
            marcados = self._marcar([_servico()])
        self.assertTrue(marcados[0]["valor_pendente"])
        linha = self._ativos()["PS-1001"]
        self.assertEqual((linha["valor"], linha["marcado_por"]), (0.0, fdf.POR_VALOR_PENDENTE))

    def test_pedido_criado_antes_do_corte_nao_marca(self):
        # dia fixo antigo gravou datas que a tabela nova nao conhece
        self.assertEqual(self._marcar([_servico(criado="2026-10-05 15:00:00")]), [])

    def test_created_at_em_utc_conta_o_dia_de_brasilia(self):
        # 02:30 UTC de terca = 23:30 de segunda em Brasilia -> antes do corte
        self.assertEqual(self._marcar([_servico(criado="2026-10-06 02:30:00")]), [])

    def test_data_vencida_ou_malformada_nao_marca(self):
        self.assertEqual(self._marcar([_servico(data="2026-10-01")]), [])
        self.assertEqual(self._marcar([_servico(i=2, data=None, scheduled_start="amanha")]), [])

    def test_scheduled_sem_fuso_e_utc_e_vale_o_dia_de_brasilia(self):
        # 01:00 UTC de quinta = 22:00 de quarta em Brasilia -> dia de visita de Campinas
        s = _servico(data=None, scheduled_start="2026-10-08 01:00:00")
        self.assertEqual(fdf.data_agendada(s), date(2026, 10, 7))
        self.assertEqual(self._marcar([s]), [])

    def test_scheduled_com_offset_explicito_continua_valendo(self):
        s = _servico(data=None, scheduled_start="2026-10-08T23:30:00-03:00")
        self.assertEqual(fdf.data_agendada(s), date(2026, 10, 8))
        marcados = self._marcar([s])
        self.assertEqual([m["data"] for m in marcados], [date(2026, 10, 8)])

    def test_registro_de_origem_compara_com_o_dia_de_brasilia(self):
        # 01:00 UTC de sexta 09/10 = 22:00 de quinta 08/10; registro DIA_FIXO de 08/10 protege
        s = _servico(data=None, scheduled_start="2026-10-09 01:00:00")
        conn = reg.conectar(self.db)
        reg.registrar_origem(conn, s, date(2026, 10, 8), reg.ORIGEM_DIA_FIXO)
        conn.close()
        self.assertEqual(self._marcar([s]), [])

    def test_reentrega_segue_a_mesma_regra(self):
        marcados = self._marcar([_servico(code="#PS-1001-R1")])
        self.assertEqual(len(marcados), 1)
        self.assertIn("PS-1001", self._ativos())

    def _linhas_dedicados(self):
        conn = pedidos_dedicados.conectar(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM pedidos_dedicados").fetchone()[0]
        finally:
            conn.close()

    def test_mesmo_service_id_com_outro_codigo_nao_duplica(self):
        # "#PS-1001, PS-1002" grava so PS-1001; depois o servico volta como "PS-1002"
        self.assertEqual(len(self._marcar([_servico(code="#PS-1001, PS-1002")])), 1)
        self.assertEqual(self._marcar([_servico(code="PS-1002")]), [])
        self.assertEqual(self._linhas_dedicados(), 1)

    def test_mesmo_service_id_duas_vezes_na_mesma_rodada_marca_uma(self):
        marcados = self._marcar([_servico(code="#PS-1001"), _servico(code="#PS-1002")])
        self.assertEqual(len(marcados), 1)
        self.assertEqual(self._linhas_dedicados(), 1)

    def test_outro_service_id_com_codigo_novo_marca(self):
        self._marcar([_servico(code="#PS-1001, PS-1002")])
        self.assertEqual(len(self._marcar([_servico(i=2, code="PS-3003")])), 1)
        self.assertEqual(self._linhas_dedicados(), 2)

    def test_rodado_duas_vezes_marca_uma(self):
        self._marcar([_servico()])
        self.assertEqual(self._marcar([_servico()]), [])

    def _remover(self, **kw):
        conn = pedidos_dedicados.conectar(self.db)
        try:
            pedidos_dedicados.remover(conn, por="hugo", **kw)
        finally:
            conn.close()

    def test_dedicado_removido_pela_equipe_nao_e_remarcado(self):
        # "Remover dedicado" no Planejamento: a decisao da equipe vale nas rodadas seguintes
        self.assertEqual(len(self._marcar([_servico()])), 1)
        self._remover(codigo_pedido="PS-1001")
        self.assertEqual(self._marcar([_servico()]), [])
        self.assertEqual(self._ativos(), {})
        self.assertEqual(len(self._marcar([_servico(i=2)])), 1)   # outro pedido continua marcando

    def test_servico_removido_volta_com_outro_codigo_e_nao_e_remarcado(self):
        self._marcar([_servico(code="#PS-1001, PS-1002")])
        self._remover(service_id=1)
        self.assertEqual(self._marcar([_servico(code="PS-1002")]), [])
        self.assertEqual(self._ativos(), {})


class TestPendentesDeAviso(Base):
    def test_marcado_e_nao_avisado_e_pendente(self):
        self._marcar([_servico()])
        pend = fdf.pendentes_de_aviso([_servico(), _servico(i=2, endereco=SAO_PAULO)], db_path=self.db)
        self.assertEqual([p["servico"]["id"] for p in pend], [1])
        self.assertEqual((pend[0]["valor"], pend[0]["valor_pendente"], pend[0]["data"]), (784.09, False, date(2026, 10, 8)))

    def test_avisado_sai_da_lista(self):
        self._marcar([_servico()])
        conn = reg.conectar(self.db)
        reg.registrar_aviso(conn, _servico(), date(2026, 10, 8), ["email"])
        conn.close()
        self.assertEqual(fdf.pendentes_de_aviso([_servico()], db_path=self.db), [])

    def test_dedicado_manual_nao_e_pendente(self):
        conn = pedidos_dedicados.conectar(self.db)
        pedidos_dedicados.marcar(conn, [{"codigo_pedido": "PS-1001"}], 500.0, "hugo")
        conn.close()
        self.assertEqual(fdf.pendentes_de_aviso([_servico()], db_path=self.db), [])

    def test_valor_pendente_vem_marcado(self):
        with mock.patch.object(fdf, "calcular_valor", lambda s, c, t: None):
            self._marcar([_servico()])
        self.assertTrue(fdf.pendentes_de_aviso([_servico()], db_path=self.db)[0]["valor_pendente"])


class TestCalcularValor(unittest.TestCase):
    def test_usa_a_calculadora_com_km_em_linha_reta_ida_e_volta(self):
        from portal_cliente import cotacao
        s = _servico()
        regras = cotacao.regras_de({})
        km = km_rodoviario.calcular_trajeto(tuple(regras["origem_coords"]), [(-22.905, -47.060)], None, voltar=True).km_total
        esperado = cotacao.calcular({"caixas": 5, "peso_kg": 0, "tipo_carga": "REFRIGERADO", "urgente": False,
                                     "valor_nf": None, "km_total": km, "pedagio": None}, regras)["total"]
        with mock.patch.object(km_rodoviario.requests, "post", side_effect=AssertionError("nao chama a Routes")):
            self.assertEqual(fdf.calcular_valor(s, {}, {7: "Refrigerado"}), esperado)

    def test_sem_coordenada(self):
        self.assertIsNone(fdf.calcular_valor(_servico(latitude=None), {}, {}))

    def test_carga_acima_da_tabela(self):
        self.assertIsNone(fdf.calcular_valor(_servico(dimension_3=5000), {}, {}))

    def test_erro_inesperado_da_calculadora_vira_none(self):
        from portal_cliente import cotacao
        with mock.patch.object(cotacao, "regras_de", side_effect=ValueError("config quebrada")):
            self.assertIsNone(fdf.calcular_valor(_servico(), {}, {}))


if __name__ == "__main__":
    unittest.main()
