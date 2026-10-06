# -*- coding: utf-8 -*-
"""Testes de batimento/banco.py (sqlite em memoria). Rodar da raiz:
py -3.11 -m unittest batimento.test_banco"""
import sqlite3
import unittest
from datetime import datetime

from batimento import banco


def ped(codigo, caixa, rotulo, **kw):
    return {"codigo": codigo, "caixa": caixa, "rotulo": rotulo, "evidencias": "stokki=X | y", **kw}


def resumo(n, fecha=True):
    return {"lancados": n, "equacao_fecha": fecha}


class GravarRodada(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        banco.garantir_esquema(self.conn)
        self.t1 = datetime(2026, 10, 5, 7, 25)
        self.t2 = datetime(2026, 10, 6, 7, 25)

    def linha(self, codigo):
        return self.conn.execute("SELECT * FROM batimento_pedidos WHERE codigo = ?", (codigo,)).fetchone()

    def test_primeira_rodada_insere_e_fecha(self):
        r = banco.gravar_rodada(self.conn, [ped("PS-1", "DESTINO", "ENTREGUE"),
                                            ped("PS-2", "EM_ANDAMENTO", "NO_POOL")], resumo(2), 1, self.t1)
        self.assertEqual(r["novos"], 2)
        self.assertTrue(r["fecha"])
        self.assertEqual(self.linha("PS-1")["fechado_em"], "2026-10-05 07:25:00")
        self.assertIsNone(self.linha("PS-2")["fechado_em"])
        rod = self.conn.execute("SELECT * FROM batimento_rodadas").fetchone()
        self.assertEqual((rod["lancados"], rod["destino"], rod["em_andamento"], rod["fecha"]), (2, 1, 1, 1))

    def test_destino_fica_congelado(self):
        banco.gravar_rodada(self.conn, [ped("PS-1", "DESTINO", "ENTREGUE")], resumo(1), 1, self.t1)
        # dia seguinte a Vuupt ja nao ve a entrega: a regra diria divergencia
        r = banco.gravar_rodada(self.conn, [ped("PS-1", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")],
                                resumo(1), 1, self.t2)
        l = self.linha("PS-1")
        self.assertEqual((l["caixa"], l["rotulo"]), ("DESTINO", "ENTREGUE"))
        self.assertEqual(l["fechado_em"], "2026-10-05 07:25:00")
        self.assertEqual(l["visto_em"], "2026-10-06 07:25:00")
        self.assertEqual(r["congelados"], 1)
        self.assertEqual(r["totais"], {"DESTINO": 1})

    def test_desde_so_muda_quando_troca_de_rotulo(self):
        banco.gravar_rodada(self.conn, [ped("PS-2", "EM_ANDAMENTO", "NO_POOL")], resumo(1), 1, self.t1)
        banco.gravar_rodada(self.conn, [ped("PS-2", "EM_ANDAMENTO", "NO_POOL")], resumo(1), 1, self.t2)
        self.assertEqual(self.linha("PS-2")["desde"], "2026-10-05 07:25:00")
        banco.gravar_rodada(self.conn, [ped("PS-2", "DIVERGENCIA", "EXPEDIDO_SEM_DOCUMENTO")],
                            resumo(1), 1, datetime(2026, 10, 7, 7, 25))
        self.assertEqual(self.linha("PS-2")["desde"], "2026-10-07 07:25:00")

    def test_em_andamento_que_vira_destino_ganha_fechado_em(self):
        banco.gravar_rodada(self.conn, [ped("PS-3", "EM_ANDAMENTO", "EM_ROTA")], resumo(1), 1, self.t1)
        banco.gravar_rodada(self.conn, [ped("PS-3", "DESTINO", "ENTREGUE")], resumo(1), 1, self.t2)
        self.assertEqual(self.linha("PS-3")["fechado_em"], "2026-10-06 07:25:00")

    def test_soma_diferente_dos_lancados_nao_fecha(self):
        r = banco.gravar_rodada(self.conn, [ped("PS-1", "DESTINO", "ENTREGUE")], resumo(2), 1, self.t1)
        self.assertFalse(r["fecha"])
        self.assertEqual(self.conn.execute("SELECT fecha FROM batimento_rodadas").fetchone()[0], 0)

    def test_resumo_que_nao_fecha_nao_fecha(self):
        r = banco.gravar_rodada(self.conn, [ped("PS-1", "DESTINO", "ENTREGUE")], resumo(1, fecha=False), 1, self.t1)
        self.assertFalse(r["fecha"])

    def test_marcar_tratado_grava_quem_quando_obs(self):
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], resumo(1), 1, self.t1)
        banco.marcar_tratado(self.conn, "PS-5", "hugo", "  cliente confirmou recebimento  ", self.t2)
        l = self.linha("PS-5")
        self.assertEqual((l["tratado_por"], l["tratado_obs"], l["tratado_em"]),
                         ("hugo", "cliente confirmou recebimento", "2026-10-06 07:25:00"))

    def test_marcar_tratado_recusa_obs_vazia_e_fora_de_divergencia(self):
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA"),
                                        ped("PS-6", "DESTINO", "ENTREGUE")], resumo(2), 1, self.t1)
        with self.assertRaises(ValueError):
            banco.marcar_tratado(self.conn, "PS-5", "hugo", "   ")
        with self.assertRaises(ValueError):
            banco.marcar_tratado(self.conn, "PS-6", "hugo", "x")
        with self.assertRaises(ValueError):
            banco.marcar_tratado(self.conn, "PS-404", "hugo", "x")

    def test_desmarcar_tratado_limpa_e_ignora_quem_nao_existe(self):
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], resumo(1), 1, self.t1)
        banco.marcar_tratado(self.conn, "PS-5", "hugo", "ok", self.t1)
        self.assertTrue(banco.desmarcar_tratado(self.conn, "PS-5"))
        l = self.linha("PS-5")
        self.assertEqual((l["tratado_em"], l["tratado_por"], l["tratado_obs"]), (None, None, None))
        self.assertFalse(banco.desmarcar_tratado(self.conn, "PS-404"))

    def test_troca_de_motivo_limpa_tratativa(self):
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], resumo(1), 1, self.t1)
        banco.marcar_tratado(self.conn, "PS-5", "hugo", "vendo com o motorista", self.t1)
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_ENTREGA")], resumo(1), 1, self.t2)
        self.assertEqual(self.linha("PS-5")["tratado_por"], "hugo")      # mesmo motivo: mantem
        banco.gravar_rodada(self.conn, [ped("PS-5", "DIVERGENCIA", "EXPEDIDO_SEM_DOCUMENTO")], resumo(1), 1,
                            datetime(2026, 10, 7, 7, 25))
        l = self.linha("PS-5")
        self.assertIsNone(l["tratado_em"])
        self.assertIsNone(l["tratado_por"])
        self.assertIsNone(l["tratado_obs"])

    def test_pedido_de_rodada_antiga_nao_entra_na_conta_de_hoje(self):
        banco.gravar_rodada(self.conn, [ped("PS-1", "DESTINO", "ENTREGUE")], resumo(1), 1, self.t1)
        r = banco.gravar_rodada(self.conn, [ped("PS-2", "EM_ANDAMENTO", "NO_POOL")], resumo(1), 2, self.t2)
        self.assertEqual(r["totais"], {"EM_ANDAMENTO": 1})
        self.assertTrue(r["fecha"])


class _ConexaoAtrasada:
    """Finge que outra thread criou a coluna entre o PRAGMA e o ALTER."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *a):
        if sql.startswith("PRAGMA table_info(batimento_pedidos)"):
            return iter([])
        return self._conn.execute(sql, *a)

    def __getattr__(self, nome):
        return getattr(self._conn, nome)


class EsquemaConcorrente(unittest.TestCase):
    def test_alter_duplicado_por_outra_thread_nao_quebra(self):
        conn = sqlite3.connect(":memory:")
        banco.garantir_esquema(conn)                   # coluna ja existe
        banco.garantir_esquema(_ConexaoAtrasada(conn))  # PRAGMA diz que falta: ALTER duplicado


if __name__ == "__main__":
    unittest.main()
