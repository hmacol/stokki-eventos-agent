# -*- coding: utf-8 -*-
"""Realocação de pedido em dia que o cliente não recebe (Hugo, 27/11)."""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import realocar_dia_fechado as rdf  # noqa: E402
import registro_dia_fixo  # noqa: E402

DOC = "11222333000144"
SEG_A_SEX = {DOC: {"dias_semana": "0,1,2,3,4", "horario_atendimento_inicio": "08:00",
                   "horario_atendimento_fim": "17:00"}}
HOJE = date(2026, 11, 26)          # quinta
SEX, SAB, SEG = date(2026, 11, 27), date(2026, 11, 28), date(2026, 11, 30)
ENDERECO_SP = "Rua Augusta, 100 - Consolacao, Sao Paulo - SP, 01305-000"


def serv(sid, agendado=None, fim=None, doc=DOC):
    return {"id": sid, "code": f"#PS-{sid}", "customer": {"code": doc}, "address": ENDERECO_SP,
            "scheduled_start": agendado, "scheduled_end": fim}


class Base(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "t.db"
        self.conn = registro_dia_fixo.conectar(self.db)

    def tearDown(self):
        self.conn.close()


class Regra(Base):
    def test_data_em_utc_vira_local(self):
        # 01:00 UTC de domingo = 22:00 de sábado em SP
        self.assertEqual(rdf.data_agendada(serv(1, "2026-11-29T01:00:00")), SAB)

    def test_dia_do_pedido(self):
        self.assertEqual(rdf.dia_do_pedido(serv(1), SAB), SAB)                         # sem data
        self.assertEqual(rdf.dia_do_pedido(serv(1, "2026-11-25T11:00:00"), SAB), SAB)  # atrasado
        self.assertEqual(rdf.dia_do_pedido(serv(1, "2026-12-01T11:00:00"), SAB), date(2026, 12, 1))

    def test_nova_data_pula_fim_de_semana_e_nunca_hoje(self):
        self.assertEqual(rdf.nova_data(serv(1), SAB, SEG_A_SEX, HOJE), SEG)
        self.assertEqual(rdf.nova_data(serv(1), date(2026, 11, 20), SEG_A_SEX, HOJE), SEX)

    def test_nova_data_respeita_dia_fixo_da_regiao(self):
        so_quarta = {"nome": "Americana", "dias": [2]}
        with mock.patch.object(rdf, "regra_dia_fixo_do_servico", return_value=so_quarta), \
             mock.patch.object(rdf, "data_valida_na_regiao", side_effect=lambda r, d: d.weekday() == 2):
            self.assertEqual(rdf.nova_data(serv(1), SAB, SEG_A_SEX, HOJE), date(2026, 12, 2))

    def test_sem_data_valida(self):
        so_sab = {DOC: {"dias_semana": "5"}}
        self.assertIsNone(rdf.nova_data(serv(1), SAB, so_sab, HOJE))

    def test_janela(self):
        self.assertEqual(rdf.janela(serv(1, "2026-11-28T12:00:00", "2026-11-28T15:00:00"), SEG_A_SEX), ("09:00", "12:00"))
        self.assertEqual(rdf.janela(serv(1), SEG_A_SEX), ("08:00", "17:00"))
        self.assertEqual(rdf.janela(serv(1), {}), rdf.DIA_TODO)

    def test_classificar(self):
        cliente = serv(2, "2026-11-28T12:00:00")                     # sem linha de origem = do cliente
        nossa = serv(3, "2026-11-28T12:00:00")
        registro_dia_fixo.registrar_origem(self.conn, nossa, SAB, registro_dia_fixo.ORIGEM_AUTOMATICO)
        outro_cliente = serv(4, doc="999")
        livres, realocar, mantidos = rdf.classificar([serv(1), cliente, nossa, outro_cliente], SAB, SEG_A_SEX, self.conn)
        self.assertEqual([s["id"] for s in livres], [4])
        self.assertEqual([s["id"] for s in realocar], [1, 3])
        self.assertEqual([s["id"] for s in mantidos], [2])
        self.assertIn("sábado", rdf.texto_dia_fechado(serv(1), SAB, SEG_A_SEX))


class Lote(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(rdf, "tirar_de_rascunhos", return_value=[])
        self.tirar = p.start()
        self.addCleanup(p.stop)

    def test_realoca_grava_e_tira_da_rodada(self):
        vuupt = mock.Mock()
        cliente = serv(2, "2026-11-28T12:00:00")
        self.tirar.return_value = ["Planejamento - 28/11/2026 - #3"]
        entram, resumo = rdf.realocar_lote([serv(1), cliente, serv(4, doc="999")], SAB, SEG_A_SEX, vuupt,
                                           hoje=HOJE, db_path=self.db)
        self.assertEqual(sorted(s["id"] for s in entram), [2, 4])  # data do cliente vence e sai normal
        self.assertEqual(resumo["realocados"], [{"codigo": "#PS-1", "de": "2026-11-28", "para": "2026-11-30",
                                                 "rotas": ["Planejamento - 28/11/2026 - #3"]}])
        self.tirar.assert_called_once_with(1, HOJE)
        sid, corpo = vuupt.atualizar_servico.call_args[0]
        self.assertEqual(sid, 1)
        self.assertTrue(corpo["scheduled_start"].startswith("2026-11-30T08:00"))
        self.assertTrue(registro_dia_fixo.data_e_automatica(self.conn, serv(1), SEG))

    def test_modo_teste_nao_grava(self):
        vuupt = mock.Mock()
        entram, resumo = rdf.realocar_lote([serv(1)], SAB, SEG_A_SEX, vuupt, modo_teste=True, hoje=HOJE, db_path=self.db)
        self.assertEqual(entram, [])
        self.assertEqual(len(resumo["realocados"]), 1)
        vuupt.atualizar_servico.assert_not_called()
        self.tirar.assert_not_called()
        self.assertFalse(registro_dia_fixo.data_e_automatica(self.conn, serv(1), SEG))

    def test_falha_na_vuupt_fica_fora_da_rota(self):
        vuupt = mock.Mock()
        vuupt.atualizar_servico.side_effect = RuntimeError("429")
        entram, resumo = rdf.realocar_lote([serv(1)], SAB, SEG_A_SEX, vuupt, hoje=HOJE, db_path=self.db)
        self.assertEqual(entram, [])
        self.assertEqual(resumo["falhas"][0]["codigo"], "#PS-1")

    def test_pedido_sem_servico_grava_no_nucleo_e_nunca_chama_a_vuupt(self):
        """Merge com o pool direto da Stokki: com fonte_pool = stokki o pool traz id negativo (-id_pool); a
        realocacao nunca manda esse id pra Vuupt: a data vai pras colunas do nucleo (edicao local)."""
        from nucleo import banco, pedidos
        with mock.patch.object(banco, "DB_PATH", self.db):
            pedidos.registrar_importacao({"code": "#PS-1", "title": "x", "customer": {"code": DOC}}, None,
                                         "nucleo_sem_servico")
            neg = pedidos.id_sintetico(pedidos.buscar_pedido("PS-1"))
            vuupt = mock.Mock()
            entram, resumo = rdf.realocar_lote([dict(serv(1), id=neg)], SAB, SEG_A_SEX, vuupt, hoje=HOJE,
                                               db_path=self.db)
            linha = pedidos.buscar_pedido("PS-1")
        vuupt.atualizar_servico.assert_not_called()
        self.assertEqual(entram, [])
        self.assertEqual(resumo["falhas"], [])
        self.assertTrue(linha["agendamento_inicio"].startswith("2026-11-30 08:00"), linha["agendamento_inicio"])
        self.tirar.assert_called_once_with(neg, HOJE)

    def test_linha_orfa_sem_servico_nao_e_realocada_local(self):
        from nucleo import banco, pedidos
        with mock.patch.object(banco, "DB_PATH", self.db):
            pedidos.registrar_importacao({"code": "#PS-1", "title": "x"}, None, "pulado_sem_alteracao")
            neg = pedidos.id_sintetico(pedidos.buscar_pedido("PS-1"))
            vuupt = mock.Mock()
            _, resumo = rdf.realocar_lote([dict(serv(1), id=neg)], SAB, SEG_A_SEX, vuupt, hoje=HOJE, db_path=self.db)
        vuupt.atualizar_servico.assert_not_called()
        self.assertIn("operacao", resumo["falhas"][0]["erro"])

    def test_sem_ninguem_com_dias_nao_mexe(self):
        vuupt = mock.Mock()
        lista = [serv(1)]
        entram, _ = rdf.realocar_lote(lista, SAB, {DOC: {"dias_semana": None}}, vuupt, hoje=HOJE, db_path=self.db)
        self.assertIs(entram, lista)
        vuupt.atualizar_servico.assert_not_called()


if __name__ == "__main__":
    unittest.main()
