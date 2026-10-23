# -*- coding: utf-8 -*-
"""py -3.11 -m unittest test_lembrar_consultas_transportadora -v"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import notificar_whatsapp as nw

import consulta_transportadora as ct
import lembrar_consultas_transportadora as lc

CFG = {"consulta_transportadora": {"ativo": True}, "painel_agentes": {"url_base": "https://x.test/painel"}}
SEXTA, SEGUNDA, TERCA = date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)


class LembrarTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = ct.conectar_consultas(Path(self._dir.name) / "t.db")
        r = ct.registrar_desconhecida(self.conn, "ACME LTDA", "", "PS-1", "e1", 1)
        self.cid = r["consulta"]["id"]
        self.conn.execute("UPDATE transportadora_consultas SET criado_em=?, enviado_em=? WHERE id=?",
                          ("2026-10-23T10:00:00", "2026-10-23T10:05:00", self.cid))
        self.conn.commit()
        self.sem_contato = False
        self.avisos = []
        self.alertas = []
        self.p1 = mock.patch.object(ct, "enviar_aviso", side_effect=self._aviso)
        self.p2 = mock.patch.object(lc.notificar_whatsapp, "avisar_alerta_transportadora", side_effect=self._alerta)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.conn.close()
        self._dir.cleanup()

    def _aviso(self, conn, consulta, config, lembrete, modo_teste):
        self.avisos.append(lembrete)
        if self.sem_contato:
            return {"email": False, "whatsapp": "desligado", "sem_contato": True}
        conn.execute("UPDATE transportadora_consultas SET ultimo_lembrete_em=? WHERE id=?",
                     (f"{self._hoje.isoformat()}T08:00:00", consulta["id"]))
        conn.commit()
        return {"email": True, "whatsapp": "desligado", "sem_contato": False}

    def _alerta(self, *a, **kw):
        self.alertas.append((a, kw))
        return "enviado"

    def _rodar(self, hoje, modo_teste=False, config=CFG):
        self._hoje = hoje
        return lc.rodar(self.conn, config, hoje, modo_teste)

    def test_lembra_uma_vez_por_dia(self):
        self.assertEqual(self._rodar(SEGUNDA)["lembretes"], 1)
        self.assertEqual(self._rodar(SEGUNDA)["lembretes"], 0)
        self.assertEqual(self.avisos, [True])

    def test_primeiro_aviso_que_nunca_saiu_vai_como_aviso(self):
        self.conn.execute("UPDATE transportadora_consultas SET enviado_em=NULL WHERE id=?", (self.cid,))
        self.conn.commit()
        self._rodar(SEGUNDA)
        self.assertEqual(self.avisos, [False])

    def test_alerta_apos_dois_dias_uteis(self):
        self.assertEqual(self._rodar(SEGUNDA)["alertas"], 0)
        self.assertEqual(self._rodar(TERCA)["alertas"], 1)
        args, kw = self.alertas[0]
        self.assertIn("https://x.test/painel/transportadoras?aba=pendentes", args + tuple(kw.values()))

    def test_alerta_imediato_sem_contato(self):
        self.sem_contato = True
        self.assertEqual(self._rodar(SEGUNDA)["alertas"], 1)

    def test_alerta_nao_repete(self):
        self._rodar(TERCA)
        self.assertEqual(self._rodar(date(2026, 10, 28))["alertas"], 0)
        self.assertEqual(len(self.alertas), 1)

    def test_fim_de_semana_nao_roda(self):
        r = self._rodar(date(2026, 10, 24))
        self.assertEqual(r, {"lembretes": 0, "alertas": 0})
        self.assertEqual(self.avisos, [])

    def test_desativado_nao_faz_nada(self):
        self.assertEqual(self._rodar(TERCA, config={})["lembretes"], 0)
        self.assertEqual(self.avisos, [])

    def test_sem_pedido_parado_pula(self):
        self.conn.execute("UPDATE transportadora_pedidos_parados SET liberado_em='2026-10-26T01:00:00'")
        self.conn.commit()
        self.assertEqual(self._rodar(TERCA), {"lembretes": 0, "alertas": 0})
        self.assertEqual(self.avisos, [])

    def test_transportadora_que_saiu_de_pendente_nao_lembra_nem_alerta(self):
        # Situacao mudada por fora, consulta e parado ainda abertos: nada sai.
        self.conn.execute("UPDATE transportadoras SET situacao='ATIVA', tipo='ENTREGA'")
        self.conn.commit()
        self.assertEqual(self._rodar(TERCA), {"lembretes": 0, "alertas": 0})
        self.assertEqual((self.avisos, self.alertas), ([], []))

    def test_falha_numa_consulta_nao_derruba_as_outras(self):
        ct.registrar_desconhecida(self.conn, "BETA", "", "PS-9", "e2", 2)
        chamadas = []

        def quebra_a_primeira(conn, consulta, config, lembrete, modo_teste):
            chamadas.append(consulta["id"])
            if len(chamadas) == 1:
                raise KeyError("secret_key")
            return {"email": True, "whatsapp": "desligado", "sem_contato": False}

        with mock.patch.object(ct, "enviar_aviso", side_effect=quebra_a_primeira):
            r = lc.rodar(self.conn, CFG, SEGUNDA, False)
        self.assertEqual(len(chamadas), 2)
        self.assertEqual(r["lembretes"], 1)

    def test_alerta_leva_nome_do_embarcador_e_dias_uteis(self):
        self.conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, stkkc_id INTEGER, apelido TEXT, nome_remetente TEXT)")
        self.conn.execute("INSERT INTO interno VALUES ('e1', NULL, 'Loja Bela', NULL)")
        self.conn.commit()
        self._rodar(TERCA)
        args, _ = self.alertas[0]
        self.assertEqual(args[:4], ("ACME LTDA", "Loja Bela", 1, 2))

    def test_texto_alerta_cabe_e_mantem_o_link(self):
        link = "https://app.freshhub.com.br/painel/transportadoras?aba=pendentes"
        t = nw.texto_alerta_transportadora("T" * 150, "E" * 150, 12, 3, link)
        self.assertLessEqual(len(t), nw.MAX_MENSAGEM)
        self.assertTrue(t.endswith(link))

    def test_modo_teste_nao_marca_alerta(self):
        self._rodar(TERCA, modo_teste=True)
        self.assertIsNone(self.conn.execute(
            "SELECT alerta_equipe_em FROM transportadora_consultas WHERE id=?", (self.cid,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
