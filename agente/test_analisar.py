# -*- coding: utf-8 -*-
"""Testes da rodada do agente com banco temporario e envios falsos.
Rodar: py -3.11 -m unittest agente.test_analisar"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from agente import analisar, banco, regras  # noqa: E402

AGORA = datetime(2026, 10, 5, 9, 0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE vigia_pedidos (codigo TEXT PRIMARY KEY, estado TEXT, motivo TEXT, desde TEXT,
                vence_em TEXT, vencido INTEGER, service_id INTEGER, detalhe TEXT, visto_em TEXT);
            CREATE TABLE nucleo_pedidos (codigo TEXT PRIMARY KEY, remetente_nome TEXT, sender_id INTEGER,
                destinatario_nome TEXT);
            CREATE TABLE preferencias_notificacao (cnpj_embarcador TEXT PRIMARY KEY, whatsapp TEXT);
        """)
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-1','Padaria Pao',77,'Mercado X')")
        self.conn.execute("INSERT INTO preferencias_notificacao VALUES ('12345678000199','(11) 99999-0000')")
        self.conn.commit()
        self.embs = {77: {"nome": "Padaria Pao", "emails": ["a@b.com"], "cnpj": "12.345.678/0001-99", "desligado": False}}
        self.p_emb = mock.patch.object(analisar, "_embarcadores", return_value=self.embs)
        self.p_trat = mock.patch.object(analisar, "_registrar_tratativa")
        self.p_emb.start(); self.p_trat.start()
        self.wa = mock.Mock(return_value="enviado")
        self.em = mock.Mock(return_value=True)

    def tearDown(self):
        self.p_emb.stop(); self.p_trat.stop()
        self.conn.close(); self.tmp.cleanup()

    def vigia(self, codigo, estado, vencido=0, desde="2026-10-05 08:00:00", motivo="Cliente ausente"):
        self.conn.execute("INSERT OR REPLACE INTO vigia_pedidos VALUES (?,?,?,?,NULL,?,1,'d','2026-10-05 08:59:00')",
                          (codigo, estado, motivo, desde, vencido))
        self.conn.commit()

    def rodar(self, config=None, modo_teste=False):
        return analisar.executar(config or {"agente": {"ativo": True}}, conn=self.conn, agora=AGORA,
                                 modo_teste=modo_teste, db_path=self.db,
                                 enviar_whatsapp=self.wa, enviar_email=self.em)


class TestRodada(Base):
    def test_insucesso_avisa_por_whatsapp_e_email(self):
        self.vigia("PS-1", "INSUCESSO")
        r = self.rodar()
        self.assertEqual((r["acoes_novas"], r["enviadas"], r["falhas"]), (1, 1, 0))
        tel, texto, assinatura = self.wa.call_args[0][:3]
        self.assertEqual(tel, "5511999990000")
        self.assertIn("PS-1", texto)
        self.assertIn("Mercado X", texto)
        self.assertIn("Cliente ausente", texto)
        self.assertIn("https://app.freshhub.com.br/cliente", texto)
        self.assertTrue(assinatura.startswith("agente:"))
        destinos, assunto = self.em.call_args[0][:2]
        self.assertEqual(destinos, ["a@b.com"])
        self.assertIn("PS-1", assunto)
        linha = banco.listar(self.conn)[0]
        self.assertEqual((linha["status"], linha["destinatario"]), (banco.ENVIADA, "5511999990000"))
        self.assertIn("whatsapp=enviado", linha["resultado"])
        self.assertIn("email=enviado", linha["resultado"])

    def test_segunda_rodada_nao_repete(self):
        self.vigia("PS-1", "INSUCESSO")
        self.rodar()
        r = self.rodar()
        self.assertEqual(r["acoes_novas"], 0)
        self.assertEqual(self.wa.call_count, 1)

    def test_desligado_grava_mas_nao_envia(self):
        self.vigia("PS-1", "INSUCESSO")
        r = self.rodar(config={"agente": {"ativo": False}})
        self.assertEqual(r["desligadas"], 1)
        self.wa.assert_not_called(); self.em.assert_not_called()
        linha = banco.listar(self.conn)[0]
        self.assertEqual(linha["status"], banco.DESLIGADA)
        self.assertIn("PS-1", linha["texto"])

    def test_modo_teste_nao_grava(self):
        self.vigia("PS-1", "INSUCESSO")
        r = self.rodar(modo_teste=True)
        self.assertEqual(r["acoes_novas"], 1)
        self.assertEqual(banco.listar(self.conn), [])
        self.assertTrue(self.wa.call_args[0][4])  # modo_teste repassado
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agente_rodadas").fetchone()[0], 0)

    def test_forcar_destino_email_redireciona(self):
        self.vigia("PS-1", "INSUCESSO")
        self.rodar(config={"agente": {"ativo": True, "forcar_destino_email": "hugo@x.com"}})
        destinos, _, corpo = self.em.call_args[0][:3]
        self.assertEqual(destinos, ["hugo@x.com"])
        self.assertIn("PILOTO", corpo)
        self.assertIn("a@b.com", corpo)

    def test_embarcador_que_desligou_nao_recebe_email(self):
        self.embs[77]["desligado"] = True
        self.vigia("PS-1", "INSUCESSO")
        r = self.rodar()
        self.em.assert_not_called()
        self.assertEqual(r["enviadas"], 1)  # whatsapp saiu
        self.assertIn("email=embarcador_desligou", banco.listar(self.conn)[0]["resultado"])

    def test_sem_contato_nenhum_vira_falha(self):
        self.embs.clear()
        self.conn.execute("DELETE FROM preferencias_notificacao"); self.conn.commit()
        self.vigia("PS-1", "INSUCESSO")
        r = self.rodar()
        self.assertEqual(r["falhas"], 1)
        self.assertEqual(banco.listar(self.conn)[0]["status"], banco.FALHOU)

    def test_whatsapp_falhou_mas_email_foi_conta_como_enviada(self):
        self.wa.return_value = "falhou"
        self.vigia("PS-1", "INSUCESSO")
        r = self.rodar()
        self.assertEqual(r["enviadas"], 1)

    def test_terceira_tentativa_gera_dois_avisos(self):
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-1-R2','Padaria Pao',77,'Mercado X')")
        self.vigia("PS-1-R2", "INSUCESSO")
        r = self.rodar()
        self.assertEqual(r["acoes_novas"], 2)
        templates = sorted(l["template"] for l in banco.listar(self.conn))
        self.assertEqual(templates, sorted([regras.AVISO_FALHA_ENTREGA, regras.PEDIR_CONFIRMACAO_DEVOLUCAO]))

    def test_estados_sem_acao_nao_gravam_nada(self):
        self.vigia("PS-1", "NO_POOL", vencido=1)
        self.vigia("PS-2", "EM_ROTA")
        r = self.rodar()
        self.assertEqual((r["fatos"], r["acoes_novas"]), (2, 0))

    def test_sem_tabela_do_vigia(self):
        self.conn.execute("DROP TABLE vigia_pedidos"); self.conn.commit()
        self.assertEqual(self.rodar()["fatos"], 0)

    def test_batimento_vira_proposta(self):
        self.conn.executescript("""
            CREATE TABLE batimento_pedidos (codigo TEXT, destino TEXT, motivo TEXT, evidencias_json TEXT, tratado_em TEXT);
            INSERT INTO batimento_pedidos VALUES ('PS-9','DIVERGENCIA','ENTREGUE_NAO_EXPEDIDO','{"vuupt":"done"}',NULL);
            INSERT INTO batimento_pedidos VALUES ('PS-8','DIVERGENCIA','EXPEDIDO_SEM_ENTREGA','{}','2026-10-01 00:00:00');
            INSERT INTO batimento_pedidos VALUES ('PS-7','ENTREGUE',NULL,'{}',NULL);
        """)
        r = self.rodar()
        self.assertEqual((r["fatos"], r["propostas"]), (1, 1))
        linha = banco.listar(self.conn)[0]
        self.assertEqual((linha["codigo"], linha["status"], linha["destinatario"]), ("PS-9", banco.PROPOSTA, "torre"))
        self.assertIn("Expedir na Stokki", linha["texto"])
        self.assertIn("vuupt=done", linha["texto"])
        self.wa.assert_not_called()

    def test_rodada_registrada(self):
        self.vigia("PS-1", "INSUCESSO")
        self.rodar()
        r = self.conn.execute("SELECT * FROM agente_rodadas").fetchone()
        self.assertEqual((r["fatos"], r["acoes_novas"], r["enviadas"]), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
