# -*- coding: utf-8 -*-
"""
Decisões do "Tratar", lista de ação, placar e linha por equipe da Torre
(Hugo, 21/11). Rodar (da raiz):
    py -3.11 -m unittest vigia.test_decisoes -v
"""
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vigia import banco, consulta, regras, vigiar

AGORA = datetime(2026, 11, 19, 10, 0)  # quinta
HOJE = AGORA.date()

ESQUEMA_EXTRA = """
CREATE TABLE nucleo_rotas (id INTEGER PRIMARY KEY, data_rota TEXT, cancelada_em TEXT);
CREATE TABLE nucleo_paradas (id INTEGER PRIMARY KEY, rota_id INTEGER, codigo TEXT, situacao TEXT);
CREATE TABLE batimento_pedidos (codigo TEXT PRIMARY KEY, embarcador TEXT, caixa TEXT, rotulo TEXT,
    desde TEXT, tratado_em TEXT);
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = banco.conectar(self.db)
        self.conn.executescript(ESQUEMA_EXTRA)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def vigiado(self, codigo, estado, motivo="", desde="2026-11-12 08:00:00", vencido=1, detalhe="EMB X"):
        self.conn.execute(
            "INSERT INTO vigia_pedidos (codigo, estado, motivo, desde, vence_em, vencido, detalhe, visto_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-11-19 09:45:00')",
            (codigo, estado, motivo, desde, desde, vencido, detalhe))
        self.conn.commit()


class TestDecisao(Base):
    def test_grava_e_troca_a_decisao(self):
        self.vigiado("PS-1", regras.ROTA_PASSADA)
        banco.registrar_decisao(self.conn, "#ps-1", regras.ROTA_PASSADA, "confirmado_motorista",
                                date(2026, 11, 20), "", "ana", hoje=HOJE)
        banco.registrar_decisao(self.conn, "PS-1", regras.ROTA_PASSADA, "outro",
                                date(2026, 11, 23), "motorista só volta segunda", "bia", hoje=HOJE)
        linhas = self.conn.execute("SELECT * FROM vigia_decisoes").fetchall()
        self.assertEqual(len(linhas), 1)
        self.assertEqual((linhas[0]["decisao"], linhas[0]["ate"], linhas[0]["por"]), ("outro", "2026-11-23", "bia"))

    def test_recusa_o_que_nao_vale(self):
        self.vigiado("PS-1", regras.ROTA_PASSADA)
        ok = dict(conn=self.conn, codigo="PS-1", estado=regras.ROTA_PASSADA, decisao="nova_data",
                  ate=date(2026, 11, 20), observacao="", por="ana", hoje=HOJE)
        for mudanca in ({"decisao": "inventada"}, {"decisao": "outro", "observacao": "ok"},
                        {"ate": date(2026, 11, 18)}, {"ate": date(2027, 3, 1)},
                        {"estado": regras.INSUCESSO}, {"codigo": "PS-999"}, {"codigo": ""}):
            with self.assertRaises(ValueError, msg=str(mudanca)):
                banco.registrar_decisao(**{**ok, **mudanca})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vigia_decisoes").fetchone()[0], 0)

    def test_data_padrao_e_o_proximo_dia_util(self):
        self.assertEqual(consulta.data_padrao_retorno(date(2026, 11, 20)), date(2026, 11, 23))  # sexta -> segunda
        self.assertEqual(consulta.data_padrao_retorno(date(2026, 11, 19)), date(2026, 11, 23))  # 20/11 é feriado
        self.assertEqual(consulta.data_padrao_retorno(date(2026, 11, 18)), date(2026, 11, 19))

    def test_desfazer(self):
        self.vigiado("PS-1", regras.ROTA_PASSADA)
        consulta.decidir("PS-1", regras.ROTA_PASSADA, "nova_data", "2026-11-20", "", "ana", self.db, HOJE)
        self.assertTrue(consulta.desfazer("PS-1", self.db))
        self.assertFalse(consulta.desfazer("PS-1", self.db))

    def test_decidir_sem_data_valida_da_mensagem_de_tela(self):
        self.vigiado("PS-1", regras.ROTA_PASSADA)
        with self.assertRaisesRegex(ValueError, "data de retorno"):
            consulta.decidir("PS-1", regras.ROTA_PASSADA, "nova_data", "", "", "ana", self.db, HOJE)


class TestListaAcao(Base):
    def setUp(self):
        super().setUp()
        self.vigiado("PS-1", regras.ROTA_PASSADA, "rota de 13/11 não terminou")
        self.vigiado("PS-2", regras.ROTA_PASSADA, "rota de 13/11 não terminou", desde="2026-11-14 00:00:00")
        self.vigiado("PS-3", regras.INSUCESSO, "Não coletado")
        self.vigiado("PS-4", regras.INSUCESSO, "Problema fiscal")
        self.vigiado("PS-5", regras.NO_POOL, "aguardando roteirização", vencido=0)      # no prazo: fora da fila
        self.vigiado("PS-6", regras.AGUARDANDO_RETIRADA, "retirada", vencido=0)
        self.conn.execute("INSERT INTO batimento_pedidos VALUES ('PS-7', 'EMB Y', 'DIVERGENCIA', "
                          "'ENTREGUE_NAO_EXPEDIDO', '2026-11-17 07:00:00', NULL)")
        self.conn.execute("INSERT INTO batimento_pedidos VALUES ('PS-8', 'EMB Y', 'DIVERGENCIA', "
                          "'ENTREGUE_NAO_EXPEDIDO', '2026-11-17 07:00:00', '2026-11-18 09:00:00')")  # já tratado
        self.conn.commit()

    def lista(self, **kw):
        return consulta.lista_acao(db_path=self.db, agora=AGORA, **kw)

    def test_so_vencido_entra_na_fila_e_cada_grupo_diz_o_que_fazer(self):
        d = self.lista()
        self.assertEqual(d["total_acao"], 5)
        self.assertEqual(sum(g["total"] for g in d["grupos"]), d["total_acao"])
        por_acao = {g["acao"]: g for g in d["grupos"]}
        self.assertEqual(por_acao[regras.acao_e_dono(regras.ROTA_PASSADA)[0]]["total"], 2)
        self.assertEqual(por_acao[regras.acao_e_dono(regras.INSUCESSO, "Não coletado")[0]]["dono"], regras.LOGISTICA)
        self.assertEqual(por_acao[regras.acao_e_dono(regras.INSUCESSO, "x")[0]]["dono"], regras.ATENDIMENTO)
        self.assertEqual(por_acao[regras.acao_e_dono(regras.ENTREGUE_NAO_EXPEDIDO)[0]]["dono"], regras.EXPEDICAO)
        codigos = {l["codigo"] for g in d["grupos"] for l in g["linhas"]}
        self.assertEqual(codigos, {"PS-1", "PS-2", "PS-3", "PS-4", "PS-7"})   # PS-5/6 no prazo, PS-8 tratado

    def test_filtro_de_equipe_e_busca(self):
        d = self.lista(equipe=regras.LOGISTICA)
        self.assertEqual({g["dono"] for g in d["grupos"]}, {regras.LOGISTICA})
        self.assertEqual(d["total_acao"], 5)                      # o total não depende do filtro
        self.assertEqual({e["equipe"]: e["total"] for e in d["equipes"]},
                         {regras.LOGISTICA: 3, regras.ATENDIMENTO: 1, regras.EXPEDICAO: 1})
        d = self.lista(busca="ps-4")
        self.assertEqual([l["codigo"] for g in d["grupos"] for l in g["linhas"]], ["PS-4"])

    def test_decisao_valida_tira_da_fila_e_volta_sozinha(self):
        consulta.decidir("PS-1", regras.ROTA_PASSADA, "confirmado_motorista", "2026-11-20", "", "ana", self.db, HOJE)
        d = self.lista()
        self.assertEqual(d["total_acao"], 4)
        self.assertEqual([l["codigo"] for l in d["com_decisao"]], ["PS-1"])
        self.assertEqual(d["com_decisao"][0]["decisao"]["rotulo"], "Confirmado com o motorista")
        # dois dias depois a decisão venceu: volta pra fila, marcada, na frente do grupo
        depois = consulta.lista_acao(db_path=self.db, agora=datetime(2026, 11, 23, 10, 0))
        self.assertEqual(depois["decisoes_vencidas"], 1)
        grupo = [g for g in depois["grupos"] if g["estado"] == regras.ROTA_PASSADA][0]
        self.assertEqual([l["codigo"] for l in grupo["linhas"]][0], "PS-1")
        self.assertTrue(grupo["linhas"][0]["decisao_vencida"])
        self.assertEqual(depois["com_decisao"], [])

    def test_decisao_de_outro_estado_nao_vale(self):
        consulta.decidir("PS-1", regras.ROTA_PASSADA, "nova_data", "2026-11-25", "", "ana", self.db, HOJE)
        self.conn.execute("UPDATE vigia_pedidos SET estado = ? WHERE codigo = 'PS-1'", (regras.INSUCESSO,))
        self.conn.commit()
        d = self.lista()
        self.assertEqual(d["com_decisao"], [])
        self.assertEqual(d["total_acao"], 5)

    def test_banco_sem_batimento_nem_rotas_nao_quebra(self):
        self.conn.executescript("DROP TABLE batimento_pedidos; DROP TABLE nucleo_paradas; DROP TABLE nucleo_rotas;")
        d = self.lista()
        self.assertEqual(d["total_acao"], 4)
        self.assertIsNone(d["placar"])


class TestPlacar(Base):
    def test_fechamento_do_ultimo_dia_de_rota(self):
        # segunda 16/11 teve rota; hoje é quinta -> "ontem" = último dia com rota antes de hoje (quarta 18)
        self.conn.execute("INSERT INTO nucleo_rotas VALUES (1, '2026-11-18', NULL)")
        self.conn.execute("INSERT INTO nucleo_rotas VALUES (2, '2026-11-17', NULL)")
        paradas = [("PS-10", "ENTREGUE"), ("PS-11", "ENTREGUE"), ("PS-12", "ENTREGUE"),
                   ("#PS-13", "INSUCESSO"), ("PS-14", "INSUCESSO"), ("PS-15", "PENDENTE"),
                   ("PS-16", "INSUCESSO"), ("PS-17", "CANCELADA")]
        for cod, sit in paradas:
            self.conn.execute("INSERT INTO nucleo_paradas (rota_id, codigo, situacao) VALUES (1, ?, ?)", (cod, sit))
        self.conn.execute("INSERT INTO nucleo_paradas (rota_id, codigo, situacao) VALUES (2, 'PS-99', 'ENTREGUE')")
        self.vigiado("PS-13", regras.INSUCESSO, "Não coletado")                         # sem decisão
        self.vigiado("PS-14", regras.INSUCESSO, "Problema fiscal")                      # vai ter decisão
        self.vigiado("PS-15", regras.ROTA_PASSADA, "", desde="2026-11-19 00:00:00", vencido=0)  # no prazo
        # PS-16 já saiu do vigia (reentrega criada): resolvido
        self.conn.commit()
        consulta.decidir("PS-14", regras.INSUCESSO, "aguardando_embarcador", "2026-11-20", "", "ana", self.db, HOJE)
        p = consulta.lista_acao(db_path=self.db, agora=AGORA)["placar"]
        self.assertEqual(p["dia"], "2026-11-18")
        self.assertEqual((p["programados"], p["entregues"], p["nao_entregues"]), (7, 3, 4))
        self.assertEqual((p["sem_decisao"], p["com_decisao"], p["no_prazo"], p["resolvidos"]), (1, 1, 1, 1))


class TestTorrePorEquipe(Base):
    def setUp(self):
        super().setUp()
        banco.registrar_listagem_stokki([{"codigo": "PS-1"}], completa=True,
                                        agora=datetime(2026, 11, 19, 9, 0), conn=self.conn)
        self.vigiado("PS-1", regras.ROTA_PASSADA)
        self.vigiado("PS-2", regras.ROTA_PASSADA, desde="2026-11-10 00:00:00")
        self.vigiado("PS-3", regras.RECUSADO, "embarcador pediu pra não reenviar")

    def test_uma_linha_por_equipe_com_pedido_vencido_sem_decisao(self):
        ex = [x for x in consulta.excecoes_torre("2026-11-19", db_path=self.db, agora=AGORA)
              if x["id"].startswith("vigia:equipe:")]
        self.assertEqual([x["tipo"] for x in ex], ["Vigia", "Vigia"])
        log, aten = ex
        self.assertIn("Logística: 2 pedido(s)", log["descricao"])
        self.assertIn("Mais antigo: PS-2", log["descricao"])
        self.assertEqual(log["severidade"], "critico")            # rota de dia anterior é crítica
        self.assertIn("Atendimento: 1 pedido(s)", aten["descricao"])
        self.assertEqual(aten["severidade"], "atencao")
        self.assertIn("equipe=", log["acao"]["url"])

    def test_pedido_com_decisao_valida_sai_da_contagem(self):
        consulta.decidir("PS-2", regras.ROTA_PASSADA, "nova_data", "2026-11-20", "", "ana", self.db, HOJE)
        ex = consulta.excecoes_torre("2026-11-19", db_path=self.db, agora=AGORA)
        log = [x for x in ex if "Logística" in x["descricao"]][0]
        self.assertIn("Logística: 1 pedido(s)", log["descricao"])
        self.assertIn("Mais antigo: PS-1", log["descricao"])

    def test_equipe_toda_decidida_some_da_torre(self):
        consulta.decidir("PS-3", regras.RECUSADO, "cancelar_devolver", "2026-11-20", "", "ana", self.db, HOJE)
        ex = consulta.excecoes_torre("2026-11-19", db_path=self.db, agora=AGORA)
        self.assertFalse(any("Atendimento:" in x["descricao"] for x in ex))


class TestRetiradaNaRodada(unittest.TestCase):
    def test_retirada_do_pipeline_vira_aguardando_retirada_sem_prazo(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = banco.conectar(Path(tmp) / "t.db")
            from vigia.test_vigiar import ESQUEMA
            conn.executescript(ESQUEMA)
            banco.registrar_listagem_stokki([
                {"codigo": "PS-20", "acao": "ignorado_retirada", "observacao": "Retirada no galpão via J3 FLEX",
                 "embarcador": "EMB Z"}], completa=True, agora=datetime(2026, 11, 19, 8, 0), conn=conn)
            vigiar.rodar(conn, agora=AGORA)
            r = dict(conn.execute("SELECT * FROM vigia_pedidos WHERE codigo = 'PS-20'").fetchone())
            conn.close()
        self.assertEqual(r["estado"], regras.AGUARDANDO_RETIRADA)
        self.assertEqual(r["vencido"], 0)
        self.assertIsNone(r["vence_em"])
        self.assertIn("J3 FLEX", r["motivo"])


if __name__ == "__main__":
    unittest.main()
