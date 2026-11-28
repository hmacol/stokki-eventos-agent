# -*- coding: utf-8 -*-
"""
Guarda unica do pedido "sem servico legitimo" (onda 4, revisao de seguranca M1/L1): so a linha
que NASCEU sem servico (pipeline, fonte stokki), sem vuupt_service_id e fora de servico
agrupado da Vuupt pode ser cancelada/reagendada/editada LOCALMENTE pelo id sintetico. Linha
orfa (servico agrupado/renomeado/apagado na Vuupt, pulado antigo) volta pro fluxo de sempre
(operacao / Vuupt). Banco temporario, sem rede.

    py -3.11 -m unittest nucleo.test_sem_servico_legitimo
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nucleo import banco, pedidos, sincronizar_servicos_vuupt as sinc


def _payload(codigo):
    return {"title": f"#{codigo} - NF1 / EMB / DEST", "code": f"#{codigo}", "type": "delivery", "sender_id": 11,
            "customer": {"name": "DEST", "address": "RUA A 1"},
            "scheduled_start": "2026-11-27T07:00:00-03:00", "scheduled_end": "2026-11-27T10:00:00-03:00"}


def _servico(sid, code, status="assigned", route_id=77):
    return {"id": sid, "code": code, "title": code, "status": status, "sender_id": 11, "route_id": route_id,
            "address": "RUA A 1", "customer": {"name": "DEST"}}


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(banco, "DB_PATH", Path(tmp.name) / "t.db")
        p.start()
        self.addCleanup(p.stop)
        self.conn = banco.conectar()
        self.addCleanup(self.conn.close)

    def legitimo(self, codigo="PS-11") -> int:
        pedidos.registrar_importacao(_payload(codigo), None, "nucleo_sem_servico", conn=self.conn)
        return pedidos.id_sintetico(pedidos.buscar_pedido(codigo, conn=self.conn))

    def orfa_agrupada(self, codigo="PS-10") -> int:
        """PS-10 tinha servico 5010; alguem agrupou na Vuupt ("PS-10, PS-20", em rota iniciada) e o sync
        zerou o id da linha antiga: ABERTO, id_pool setado, nasceu_sem_servico NULL."""
        pedidos.registrar_importacao(_payload(codigo), {"id": 5010}, "criado", conn=self.conn)
        sinc.sincronizar_servicos([_servico(5010, f"#{codigo}, #PS-20")], self.conn)
        linha = pedidos.buscar_pedido(codigo, conn=self.conn)
        assert linha["vuupt_service_id"] is None and linha["status"] == banco.PEDIDO_ABERTO and linha["id_pool"]
        return pedidos.id_sintetico(linha)


class GuardaUnicaTest(_Base):
    def test_so_a_linha_que_nasceu_sem_servico_e_legitima(self):
        self.legitimo("PS-11")
        self.orfa_agrupada("PS-10")
        pedidos.registrar_importacao(_payload("PS-12"), None, "pulado_sem_alteracao", conn=self.conn)  # legado
        self.assertTrue(pedidos.eh_sem_servico_legitimo(self.conn, "PS-11"))
        self.assertTrue(pedidos.eh_sem_servico_legitimo(self.conn, "#ps-11"))
        self.assertFalse(pedidos.eh_sem_servico_legitimo(self.conn, "PS-10"))
        self.assertFalse(pedidos.eh_sem_servico_legitimo(self.conn, "PS-12"))
        self.assertFalse(pedidos.eh_sem_servico_legitimo(self.conn, "PS-99"))   # nao existe
        linha = pedidos.buscar_pedido("PS-11", conn=self.conn)
        self.assertTrue(pedidos.eh_sem_servico_legitimo(self.conn, linha))   # aceita a linha tambem

    def test_marcada_mas_dentro_de_composto_vivo_nao_e_legitima(self):
        self.legitimo("PS-30")
        sinc.sincronizar_servicos([_servico(6000, "#PS-30, #PS-31")], self.conn)
        self.assertFalse(pedidos.eh_sem_servico_legitimo(self.conn, "PS-30"))

    def test_com_servico_nao_e_legitima(self):
        self.legitimo("PS-11")
        pedidos.upsert_pedido(self.conn, "PS-11", {}, banco.ORIGEM_VUUPT_SYNC, vuupt_service_id=900)
        self.assertFalse(pedidos.eh_sem_servico_legitimo(self.conn, "PS-11"))

    def test_sem_a_coluna_falha_fechado(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE nucleo_pedidos (codigo TEXT, vuupt_service_id INTEGER, status TEXT, excluido_em TEXT)")
        c.execute("INSERT INTO nucleo_pedidos VALUES ('PS-11', NULL, 'ABERTO', NULL)")
        self.assertFalse(pedidos.eh_sem_servico_legitimo(c, "PS-11"))


class AcoesNaLinhaOrfaTest(_Base):
    def test_cancelar_local_recusa_a_orfa(self):
        self.orfa_agrupada("PS-10")
        self.assertFalse(pedidos.cancelar_sem_servico("PS-10", "teste", banco.ORIGEM_PAINEL, conn=self.conn))
        self.assertEqual(pedidos.buscar_pedido("PS-10", conn=self.conn)["status"], banco.PEDIDO_ABERTO)
        n = self.conn.execute("SELECT COUNT(*) FROM nucleo_eventos WHERE tipo = 'PEDIDO_CANCELADO'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_cancelar_local_da_legitima_continua(self):
        self.legitimo("PS-11")
        self.assertTrue(pedidos.cancelar_sem_servico("PS-11", "teste", banco.ORIGEM_PAINEL, conn=self.conn))

    def test_reagendar_e_editar_a_orfa_levantam_com_motivo(self):
        sid = self.orfa_agrupada("PS-10")
        with self.assertRaises(ValueError) as erro:
            pedidos.gravar_agendamento_sem_servico(sid, "2026-12-01T08:00:00-03:00", None, conn=self.conn)
        self.assertIn("operacao", str(erro.exception))
        with self.assertRaises(ValueError) as erro:
            pedidos.editar_sem_servico(sid, {"endereco": "RUA NOVA"}, conn=self.conn)
        self.assertIn("operacao", str(erro.exception))
        linha = pedidos.buscar_pedido("PS-10", conn=self.conn)
        self.assertEqual((linha["endereco"], linha["agendamento_inicio"]), ("RUA A 1", "2026-11-27 07:00:00"))

    def test_reagendar_a_legitima_continua(self):
        sid = self.legitimo("PS-11")
        self.assertTrue(pedidos.gravar_agendamento_sem_servico(sid, "2026-12-01T08:00:00-03:00", None, conn=self.conn))


class ChaveDesligadaTest(_Base):
    """L1: as acoes que recebem id sintetico nao leem a chave; a guarda vale com ela desligada."""

    def test_cancelar_servico_completo_com_id_da_orfa_nao_fecha_nada(self):
        from roteirizacao import cancelar_servico
        sid = self.orfa_agrupada("PS-10")
        with mock.patch.object(cancelar_servico, "VuuptClient", side_effect=AssertionError("Vuupt")), \
                mock.patch("roteirizacao.conferir_pool.tirar_dos_rascunhos", side_effect=AssertionError("rascunho")):
            r = cancelar_servico.cancelar_servico_completo("t", sid)
        self.assertFalse(r["ok"])
        self.assertIn("operacao", r["erro"])
        self.assertEqual(pedidos.buscar_pedido("PS-10", conn=self.conn)["status"], banco.PEDIDO_ABERTO)

    def test_reagendar_feriado_com_id_da_orfa_nao_grava(self):
        sid = self.orfa_agrupada("PS-10")
        with self.assertRaises(ValueError):
            pedidos.editar_sem_servico(sid, {"agendamento_inicio": "2026-12-01 08:00:00",
                                             "agendamento_fim": "2026-12-01 12:00:00"})


if __name__ == "__main__":
    unittest.main()
