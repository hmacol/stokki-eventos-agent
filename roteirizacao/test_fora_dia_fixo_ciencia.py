# -*- coding: utf-8 -*-
"""
Com ciencia_dedicado.ativo, o pedido marcado fora do dia fixo abre ciencia e o
aviso unico antigo nao sai. Rodar (da raiz):
    py -3.11 -m unittest roteirizacao.test_fora_dia_fixo_ciencia -v
"""
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import ciencia_dedicado as cd
import fora_dia_fixo as fdf
import notificar_ciencia_dedicado
import notificar_fora_dia_fixo
import pedidos_dedicados

CAMPINAS = "Rua Barão de Jaguara 900, Centro, Campinas - SP, 13015-001, Brasil"   # só quarta
HOJE = date(2026, 10, 6)
QUINTA = "2026-10-08"
LIGADO = {"ciencia_dedicado": {"ativo": True}}


def _servico(i=1):
    return {"id": i, "code": f"#PS-{1000 + i}", "address": CAMPINAS, "sender_id": 7, "dimension_3": 5,
            "latitude": -22.905, "longitude": -47.060, "created_at": "2026-10-06 15:00:00",
            "title": f"Cliente {i}", "scheduled_start": f"{QUINTA}T08:00:00-03:00"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "t.db"
        for alvo, valor in (("DETECCAO_A_PARTIR_DE", date(2026, 10, 5)), ("calcular_valor", lambda s, c, t: 784.09)):
            p = mock.patch.object(fdf, alvo, valor)
            p.start()
            self.addCleanup(p.stop)
        self.executar = mock.patch.object(notificar_ciencia_dedicado, "executar", return_value={}).start()
        self.avisar = mock.patch.object(notificar_fora_dia_fixo, "avisar",
                                        return_value={"registrados": 0, "falhas": 0}).start()
        self.addCleanup(mock.patch.stopall)

    def _tratar(self, servicos, config=LIGADO, **kw):
        return fdf.tratar_fora_dia_fixo(servicos, config, hoje=HOJE, db_path=self.db, **kw)

    def _ciencias(self):
        conn = cd.conectar(self.db)
        try:
            return cd.listar(conn)
        finally:
            conn.close()


class TestLigacaoCiencia(Base):
    def test_desligado_segue_o_aviso_antigo_e_nao_abre_ciencia(self):
        r = self._tratar([_servico()], config={})
        self.assertEqual(r["marcados"], 1)
        self.assertIn("avisos", r)
        self.avisar.assert_called_once()
        self.executar.assert_not_called()
        self.assertEqual(self._ciencias(), [])

    def test_ligado_abre_ciencia_e_nao_chama_o_aviso_antigo(self):
        with mock.patch("preferencias_notificacao.carregar_embarcadores",
                        return_value={7: {"cnpj": "12345678000190"}}):
            r = self._tratar([_servico()])
        self.assertEqual((r["marcados"], r["ciencias"]), (1, 1))
        self.assertNotIn("avisos", r)
        self.avisar.assert_not_called()
        self.executar.assert_called_once()
        self.assertEqual(self.executar.call_args.kwargs["db_path"], self.db)
        linhas = self._ciencias()
        self.assertEqual(len(linhas), 1)
        # a roteirizacao so manda o aviso do que acabou de abrir (os lembretes vencidos sao do timer)
        self.assertEqual(self.executar.call_args.kwargs["somente_ids"], {linhas[0]["id"]})
        self.assertEqual((linhas[0]["codigo"], linhas[0]["cnpj"], linhas[0]["data_entrega"]),
                         ("PS-1001", "12345678000190", QUINTA))

    def test_ligado_sem_cnpj_abre_mesmo_assim(self):
        r = self._tratar([_servico()])   # banco temporario sem preferencias: carregar falha
        self.assertEqual(r["ciencias"], 1)
        self.assertIsNone(self._ciencias()[0]["cnpj"])

    def test_ligado_sem_marcado_novo_nao_roda_a_rotina(self):
        r = self._tratar([])
        self.assertEqual((r["marcados"], r["ciencias"]), (0, 0))
        self.executar.assert_not_called()

    def test_ligado_falha_na_ciencia_nao_derruba_a_rodada(self):
        with mock.patch.object(cd, "abrir", side_effect=[RuntimeError("banco travado"), 5]):
            r = self._tratar([_servico(1), _servico(2)])
        self.assertEqual((r["marcados"], r["ciencias"], r["ciencias_falhas"]), (2, 1, 1))
        self.assertNotIn("erro", r)
        self.avisar.assert_not_called()
        self.executar.assert_called_once()
        self.assertEqual(len(self._ativos()), 2)

    def test_proxima_rodada_reabre_ciencia_do_orfao_recente(self):
        with mock.patch.object(cd, "abrir", side_effect=RuntimeError("banco travado")):
            r = self._tratar([_servico()])
        self.assertEqual((r["ciencias"], r["ciencias_falhas"]), (0, 1))
        self.executar.assert_not_called()
        self.assertEqual(self._ciencias(), [])
        r = self._tratar([_servico()])   # marcar pula (ja marcado), mas o orfao entra
        self.assertEqual((r["marcados"], r["ciencias"]), (0, 1))
        self.assertEqual(len(self._ciencias()), 1)
        self.executar.assert_called_once()

    def _envelhecer_marcacao(self, horas):
        conn = pedidos_dedicados.conectar(self.db)
        conn.execute("UPDATE pedidos_dedicados SET marcado_em = datetime('now','localtime', ?)", (f"-{horas} hours",))
        conn.commit()
        conn.close()

    def test_orfao_marcado_ha_mais_de_48h_nao_entra(self):
        with mock.patch.object(cd, "abrir", side_effect=RuntimeError("x")):
            self._tratar([_servico()])
        self._envelhecer_marcacao(49)
        r = self._tratar([_servico()])
        self.assertEqual(r["ciencias"], 0)
        self.assertEqual(self._ciencias(), [])
        self.executar.assert_not_called()

    def test_pedido_com_ciencia_respondida_nao_reabre(self):
        self._tratar([_servico()])
        conn = cd.conectar(self.db)
        cd.responder(conn, self._ciencias()[0]["id"], cd.CIENTE, "Ana", "1.1.1.1", datetime(2026, 10, 6, 10, 0))
        conn.close()
        r = self._tratar([_servico()])
        self.assertEqual(r["ciencias"], 0)
        self.assertEqual(len(self._ciencias()), 1)

    def test_falha_ao_conectar_nao_derruba_a_rodada(self):
        with mock.patch.object(cd, "conectar", side_effect=RuntimeError("sem banco")):
            r = self._tratar([_servico()])
        self.assertEqual((r["marcados"], r["ciencias"], r["ciencias_falhas"]), (1, 0, 1))
        self.assertNotIn("erro", r)

    def test_modo_teste_nao_abre_ciencia(self):
        r = self._tratar([_servico()], modo_teste=True)
        self.assertEqual(r, {"marcados": 0, "candidatos": 1})
        self.executar.assert_not_called()
        self.assertEqual(self._ciencias(), [])

    def _reagendar_pela_pagina(self, minutos_atras):
        conn = cd.conectar(self.db)
        cd.encerrar(conn, self._ciencias()[0]["id"], "reagendado", datetime(2026, 10, 6, 10, 0))
        conn.close()
        conn = pedidos_dedicados.conectar(self.db)
        pedidos_dedicados.remover(conn, codigo_pedido="PS-1001", por=pedidos_dedicados.POR_CIENCIA_REAGENDADO)
        conn.execute("UPDATE pedidos_dedicados SET removido_em = datetime('now','localtime', ?)",
                     (f"-{minutos_atras} minutes",))
        conn.commit()
        conn.close()

    def test_reagendado_pela_pagina_ha_menos_de_60_min_nao_e_remarcado(self):
        # a roteirizacao que buscou os servicos antes do reagendamento ainda ve a data velha
        self._tratar([_servico()])
        self._reagendar_pela_pagina(10)
        r = self._tratar([_servico()])
        self.assertEqual((r["marcados"], r["ciencias"]), (0, 0))
        self.assertEqual(self._ativos(), {})
        self.assertEqual(len(self._ciencias()), 1)
        # mesmo servico com outro codigo tambem espera
        outro = dict(_servico(), code="#PS-7777")
        self.assertEqual(self._tratar([outro])["marcados"], 0)

    def test_remarcado_apos_reagendar_abre_ciencia_nova(self):
        self._tratar([_servico()])
        self._reagendar_pela_pagina(61)
        r = self._tratar([_servico()])
        self.assertEqual((r["marcados"], r["ciencias"]), (1, 1))
        self.assertEqual(len(self._ciencias()), 2)
        self.assertEqual(self.executar.call_count, 2)

    def _ativos(self):
        conn = pedidos_dedicados.conectar(self.db)
        try:
            return pedidos_dedicados.ativos_por_codigo(conn)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
