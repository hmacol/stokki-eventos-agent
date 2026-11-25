# -*- coding: utf-8 -*-
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest import mock

from nucleo import auth_motorista as auth
from nucleo import fila_app_motoristas as fila


def _pessoa(nome, cpf, ativo=True):
    return {"principal_id": 1, "nome": nome, "cpf": cpf, "ativo": ativo, "linhas": []}


class FilaTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE motoristas (cpf TEXT PRIMARY KEY, app_versao TEXT, ultimo_login_em TEXT)")
        self.pessoas = [_pessoa("ANA SOUZA", "1"), _pessoa("BRUNO LIMA", "2"), _pessoa("CARLA NOVA", "3"),
                        _pessoa("DUDA TESTE", "4"), _pessoa("EVA SEM FONE", "5")]
        self.conn.execute("INSERT INTO motoristas VALUES ('1', NULL, '2026-11-10 08:00:00')")   # APK antigo
        self.conn.execute("INSERT INTO motoristas VALUES ('2', '1.2.0', '2026-11-20 08:00:00')")  # ja no novo
        self.conn.execute("INSERT INTO motoristas VALUES ('3', NULL, NULL)")                      # PIN criado, nunca entrou
        p = mock.patch.object(fila.atu, "pessoas", side_effect=lambda c: self.pessoas)
        t = mock.patch.object(fila, "_telefone", side_effect=lambda c, pessoa: None if pessoa["cpf"] == "5" else "11999990000")
        p.start(); t.start()
        self.addCleanup(p.stop); self.addCleanup(t.stop)

    def test_classifica_e_pula_quem_ja_adotou_teste_e_sem_telefone(self):
        r = fila.enfileirar(self.conn)
        self.assertEqual(sorted(r["novos"]), [("ANA SOUZA", "atualizar"), ("CARLA NOVA", "adotar")])
        self.assertEqual(r["fora"], [("EVA SEM FONE", "sem telefone")])
        self.assertEqual(fila.enfileirar(self.conn)["novos"], [])   # idempotente

    def test_sem_conta_fica_pausado_ate_ter_pin(self):
        self.pessoas.append(_pessoa("FABIO SEM PIN", "6"))
        fila.enfileirar(self.conn)
        self.assertNotIn("6", [d["cpf"] for d in fila.lote_do_dia(self.conn, 10)])
        self.assertEqual(self.conn.execute("SELECT situacao FROM motoristas_fila_app WHERE cpf='6'").fetchone()[0], "pendente")
        self.conn.execute("INSERT INTO motoristas VALUES ('6', NULL, NULL)")                      # conta criada
        self.assertIn("6", [d["cpf"] for d in fila.lote_do_dia(self.conn, 10)])

    def test_lote_respeita_limite_e_adocao_tira_da_fila(self):
        fila.enfileirar(self.conn)
        self.assertEqual(len(fila.lote_do_dia(self.conn, 1)), 1)
        self.conn.execute("UPDATE motoristas SET app_versao = '1.2.0' WHERE cpf = '1'")
        self.assertEqual([d["cpf"] for d in fila.lote_do_dia(self.conn, 5)], ["3"])
        self.assertEqual(self.conn.execute("SELECT situacao FROM motoristas_fila_app WHERE cpf='1'").fetchone()[0], "adotou")

    def test_envio_real_marca_e_lembrete_so_apos_3_dias_e_uma_vez(self):
        fila.enfileirar(self.conn)
        avisar = mock.Mock(return_value=("enviado", False))
        cfg = {"atualizacao_motoristas": {"forcar_destino_whatsapp": ""}}
        lote = fila.lote_do_dia(self.conn, 5)
        res = fila.enviar(self.conn, cfg, lote, real=True, avisar=avisar, dormir=lambda s: None)
        self.assertEqual([r["situacao"] for r in res], ["enviado", "enviado"])
        self.assertEqual(avisar.call_args_list[0].kwargs["assinatura"], "fila_app:1:1")
        self.assertIn("versão antiga", avisar.call_args_list[0].args[3])
        self.assertEqual(fila.lote_do_dia(self.conn, 5), [])                      # acabou de receber
        depois = datetime.now() + timedelta(days=4)
        lembrete = fila.lote_do_dia(self.conn, 5, agora=depois)
        self.assertEqual(len(lembrete), 2)
        fila.enviar(self.conn, cfg, lembrete, real=True, avisar=avisar, dormir=lambda s: None)
        self.assertEqual(fila.lote_do_dia(self.conn, 5, agora=depois + timedelta(days=9)), [])   # max 2 mensagens

    def test_piloto_nao_muda_a_fila_e_teto_para_o_lote(self):
        fila.enfileirar(self.conn)
        lote = fila.lote_do_dia(self.conn, 5)
        avisar = mock.Mock(side_effect=[("teto", False), ("enviado", False)])
        cfg = {"atualizacao_motoristas": {"forcar_destino_whatsapp": "11988887777"}}
        res = fila.enviar(self.conn, cfg, lote, real=False, avisar=avisar, dormir=lambda s: None)
        self.assertEqual([r["situacao"] for r in res], ["teto", "nao_tentado"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM motoristas_fila_app WHERE situacao='enviado'").fetchone()[0], 0)

    def test_desligado_sem_a_chave(self):
        fila.enfileirar(self.conn)
        res = fila.enviar(self.conn, {}, fila.lote_do_dia(self.conn, 5), real=True, avisar=mock.Mock())
        self.assertTrue(all(r["situacao"] == "desligado" for r in res))


class VersaoAppTest(unittest.TestCase):
    def test_registrar_versao(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE motoristas (cpf TEXT PRIMARY KEY, app_versao TEXT)")
        conn.execute("INSERT INTO motoristas VALUES ('123', NULL)")
        auth.registrar_versao_app(conn, "123", "1.2.0")
        self.assertEqual(conn.execute("SELECT app_versao FROM motoristas").fetchone()[0], "1.2.0")
        auth.registrar_versao_app(conn, "123", "lixo; drop")     # invalido ignora
        auth.registrar_versao_app(conn, "123", None)
        self.assertEqual(conn.execute("SELECT app_versao FROM motoristas").fetchone()[0], "1.2.0")


if __name__ == "__main__":
    unittest.main()
