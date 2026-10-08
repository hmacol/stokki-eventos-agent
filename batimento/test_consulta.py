# -*- coding: utf-8 -*-
"""Testes de batimento/consulta.py (sqlite em arquivo temporario). Rodar da raiz:
py -3.11 -m unittest batimento.test_consulta"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from batimento import banco, consulta


def ped(codigo, caixa, rotulo, **kw):
    return {"codigo": codigo, "caixa": caixa, "rotulo": rotulo, "embarcador": "EMB", "evidencias": "a | b", **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"

    def gravar(self, pedidos, quando, fecha=True):
        conn = banco.conectar(self.db)
        try:
            banco.gravar_rodada(conn, pedidos, {"lancados": len(pedidos), "equacao_fecha": fecha}, 1, quando)
        finally:
            conn.close()


class Vencida(unittest.TestCase):
    def test_um_dia_util(self):
        self.assertFalse(consulta.vencida("2026-10-05 07:25:00", datetime(2026, 10, 6, 7, 24)))   # seg -> ter
        self.assertTrue(consulta.vencida("2026-10-05 07:25:00", datetime(2026, 10, 6, 7, 25)))

    def test_sexta_so_vence_no_proximo_dia_util(self):
        self.assertFalse(consulta.vencida("2026-10-02 07:25:00", datetime(2026, 10, 4, 23, 0)))   # domingo
        self.assertTrue(consulta.vencida("2026-10-02 07:25:00", datetime(2026, 10, 5, 7, 25)))    # segunda

    def test_feriado_nao_conta_como_dia_util(self):
        # sexta 09/10 -> 12/10 e feriado (regras/feriados.py) -> vence terca 13/10
        self.assertFalse(consulta.vencida("2026-10-09 07:25:00", datetime(2026, 10, 12, 7, 25)))
        self.assertTrue(consulta.vencida("2026-10-09 07:25:00", datetime(2026, 10, 13, 7, 25)))

    def test_desde_invalido_nao_vence(self):
        self.assertFalse(consulta.vencida(None, datetime(2026, 10, 12)))


class Fechamento(Base):
    def test_sem_rodada_volta_vazio(self):
        d = consulta.fechamento(db_path=self.db)
        self.assertEqual((d["linhas"], d["por_motivo"], d["total_abertas"], d["ultima"], d["rodadas"]),
                         ([], [], 0, None, []))

    def test_lista_so_divergencias_com_filtros(self):
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA"),
                     ped("PS-2", "DIVERGENCIA", "REDESPACHO_SEM_COMPROVANTE"),
                     ped("PS-3", "DESTINO", "ENTREGUE")], datetime(2026, 10, 5, 7, 25))
        agora = datetime(2026, 10, 7, 9, 0)
        d = consulta.fechamento(db_path=self.db, agora=agora)
        self.assertEqual([l["codigo"] for l in d["linhas"]], ["PS-1", "PS-2"])
        self.assertEqual(d["total_abertas"], 2)
        self.assertEqual(d["por_motivo"][0]["motivo"], "EXPEDIDO_SEM_ENTREGA")   # reais primeiro
        self.assertTrue(d["linhas"][0]["real"] and d["linhas"][0]["vencida"])
        self.assertEqual(d["linhas"][0]["evidencias"], ["a", "b"])
        self.assertEqual(d["ultima"]["lancados"], 3)
        self.assertEqual(len(d["rodadas"]), 1)
        self.assertEqual([l["codigo"] for l in consulta.fechamento(so_reais=True, db_path=self.db)["linhas"]],
                         ["PS-1"])
        self.assertEqual([l["codigo"] for l in consulta.fechamento(
            motivo="REDESPACHO_SEM_COMPROVANTE", db_path=self.db)["linhas"]], ["PS-2"])
        self.assertEqual([l["codigo"] for l in consulta.fechamento(busca="ps-2", db_path=self.db)["linhas"]],
                         ["PS-2"])

    def test_tratada_sai_da_lista_salvo_se_pedir(self):
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], datetime(2026, 10, 5, 7, 25))
        consulta.tratar("PS-1", "hugo", "resolvido", db_path=self.db)
        self.assertEqual(consulta.fechamento(db_path=self.db)["linhas"], [])
        d = consulta.fechamento(incluir_tratadas=True, db_path=self.db)
        self.assertTrue(d["linhas"][0]["tratada"])
        self.assertEqual(d["total_abertas"], 0)


class Novidades(Base):
    def test_so_quem_entrou_em_divergencia_nesta_rodada(self):
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], datetime(2026, 10, 5, 7, 25))
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA"),
                     ped("PS-2", "DIVERGENCIA", "RETIRADA_SEM_COMPROVANTE"),
                     ped("PS-3", "EM_ANDAMENTO", "NO_POOL")], datetime(2026, 10, 6, 7, 25))
        conn = banco.conectar(self.db)
        try:
            novas = consulta.novidades(conn, "2026-10-06 07:25:00")
        finally:
            conn.close()
        self.assertEqual([n["codigo"] for n in novas], ["PS-2"])


class Oscilacao(Base):
    def setUp(self):
        super().setUp()
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], datetime(2026, 10, 5, 7, 25))
        self.gravar([ped("PS-1", "EM_ANDAMENTO", "NO_POOL")], datetime(2026, 10, 6, 7, 25))
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], datetime(2026, 10, 7, 7, 25))

    def test_volta_ao_mesmo_motivo_nao_e_novidade(self):
        conn = banco.conectar(self.db)
        try:
            self.assertEqual(consulta.novidades(conn, "2026-10-07 07:25:00"), [])
        finally:
            conn.close()

    def test_prazo_da_torre_conta_da_primeira_vez(self):
        itens = consulta.excecoes_torre("2026-10-07", db_path=self.db, agora=datetime(2026, 10, 7, 9, 0))
        self.assertEqual([x["id"] for x in itens], ["batimento:PS-1:EXPEDIDO_SEM_ENTREGA"])
        self.assertIn("desde 05/10", itens[0]["descricao"])
        d = consulta.fechamento(db_path=self.db, agora=datetime(2026, 10, 7, 9, 0))
        self.assertEqual(d["linhas"][0]["div_desde"], "2026-10-05 07:25:00")


class RodadaAtrasada(Base):
    def test_rodada_com_mais_de_26h_gera_critico_e_marca_a_aba(self):
        self.gravar([ped("PS-1", "EM_ANDAMENTO", "NO_POOL")], datetime(2026, 10, 5, 7, 25))
        agora = datetime(2026, 10, 6, 9, 26)
        itens = consulta.excecoes_torre("2026-10-06", db_path=self.db, agora=agora)
        self.assertEqual([(x["id"], x["severidade"]) for x in itens],
                         [("batimento:sem-rodada:2026-10-05 07:25:00", "critico")])
        self.assertIn("sem rodada desde 05/10 07:25", itens[0]["descricao"])
        self.assertTrue(consulta.fechamento(db_path=self.db, agora=agora)["atrasada"])

    def test_rodada_de_hoje_nao_esta_atrasada(self):
        self.gravar([ped("PS-1", "EM_ANDAMENTO", "NO_POOL")], datetime(2026, 10, 5, 7, 25))
        agora = datetime(2026, 10, 6, 9, 24)
        self.assertEqual(consulta.excecoes_torre("2026-10-06", db_path=self.db, agora=agora), [])
        self.assertFalse(consulta.fechamento(db_path=self.db, agora=agora)["atrasada"])


class ExcecoesTorre(Base):
    def test_sem_rodada_nao_gera_nada(self):
        self.assertEqual(consulta.excecoes_torre("2026-10-07", db_path=self.db), [])

    def test_so_reais_vencidas_e_nao_tratadas(self):
        self.gravar([ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA"),
                     ped("PS-2", "DIVERGENCIA", "REDESPACHO_SEM_COMPROVANTE"),
                     ped("PS-3", "DIVERGENCIA", "ENTREGUE_NAO_EXPEDIDO")], datetime(2026, 10, 5, 7, 25))
        consulta.tratar("PS-3", "hugo", "ok", db_path=self.db)
        itens = [x for x in consulta.excecoes_torre("2026-10-07", db_path=self.db, agora=datetime(2026, 10, 7, 9, 0))
                 if not x["id"].startswith("batimento:sem-rodada:")]   # rodada de 2 dias atras: coberto em RodadaAtrasada
        self.assertEqual([x["id"] for x in itens], ["batimento:PS-1:EXPEDIDO_SEM_ENTREGA"])
        x = itens[0]
        self.assertEqual((x["tipo"], x["severidade"]), ("Batimento", "atencao"))
        self.assertEqual(x["acao"]["url"], "/vigia?aba=fechamento&busca=PS-1")
        self.assertIn("Expedido sem entrega", x["descricao"])
        # ainda no prazo: nada
        self.assertEqual(consulta.excecoes_torre("2026-10-05", db_path=self.db,
                                                 agora=datetime(2026, 10, 5, 12, 0)), [])

    def test_rodada_que_nao_fecha_gera_critico(self):
        self.gravar([ped("PS-1", "EM_ANDAMENTO", "NO_POOL")], datetime(2026, 10, 5, 7, 25), fecha=False)
        itens = consulta.excecoes_torre("2026-10-05", db_path=self.db, agora=datetime(2026, 10, 5, 8, 0))
        self.assertEqual(len(itens), 1)
        self.assertEqual((itens[0]["severidade"], itens[0]["id"]),
                         ("critico", "batimento:nao-fecha:2026-10-05 07:25:00"))


if __name__ == "__main__":
    unittest.main()
