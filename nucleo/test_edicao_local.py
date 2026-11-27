# -*- coding: utf-8 -*-
"""
Edicao local do pedido sem servico na Vuupt (pool direto da Stokki, Task 6): o
Planejamento grava endereco/coordenadas/agendamento nas colunas do nucleo, a rodada
seguinte do pipeline nao sobrescreve o que foi editado e a materializacao manda o
valor editado pra Vuupt. Banco temporario, sem rede. Rodar (da raiz):
    py -3.11 -m unittest nucleo.test_edicao_local -v
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nucleo import banco, materializar, pedidos
from nucleo.test_materializar import FakeVuupt, _payload


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(banco, "DB_PATH", Path(tmp.name) / "t.db")
        p.start()
        self.addCleanup(p.stop)
        self.conn = banco.conectar()
        self.addCleanup(self.conn.close)

    def _pipeline(self, codigo="PS-1", **extra):
        pedidos.registrar_importacao(_payload(codigo, **extra), None, "nucleo_sem_servico", conn=self.conn)

    def _id(self, codigo="PS-1") -> int:
        return pedidos.id_sintetico(pedidos.buscar_pedido(codigo, conn=self.conn))

    def _linha(self, codigo="PS-1"):
        return pedidos.buscar_pedido(codigo, conn=self.conn)


class EditarSemServicoTest(_Base):
    def test_grava_as_colunas_e_marca_os_campos(self):
        self._pipeline()
        codigo = pedidos.editar_sem_servico(self._id(), {"endereco": "RUA NOVA 2", "latitude": -23.6,
                                                         "longitude": -46.7}, conn=self.conn)
        self.assertEqual(codigo, "PS-1")
        linha = self._linha()
        self.assertEqual((linha["endereco"], linha["latitude"], linha["longitude"]), ("RUA NOVA 2", -23.6, -46.7))
        self.assertEqual(set(json.loads(linha["edicao_local_json"])), {"endereco", "latitude", "longitude"})

    def test_marcas_se_acumulam(self):
        self._pipeline()
        pedidos.editar_sem_servico(self._id(), {"endereco": "RUA NOVA 2"}, conn=self.conn)
        pedidos.editar_sem_servico(self._id(), {"agendamento_inicio": "2026-12-01 08:00:00",
                                                "agendamento_fim": "2026-12-01 12:00:00"}, conn=self.conn)
        self.assertEqual(set(json.loads(self._linha()["edicao_local_json"])),
                         {"endereco", "agendamento_inicio", "agendamento_fim"})

    def test_recusa_com_motivo(self):
        self._pipeline()
        with self.assertRaisesRegex(ValueError, "nao e sintetico"):
            pedidos.editar_sem_servico(5, {"endereco": "X"}, conn=self.conn)
        with self.assertRaisesRegex(ValueError, "nao encontrado"):
            pedidos.editar_sem_servico(-999, {"endereco": "X"}, conn=self.conn)
        with self.assertRaisesRegex(ValueError, "nao editavel"):
            pedidos.editar_sem_servico(self._id(), {"titulo": "X"}, conn=self.conn)
        self.conn.execute("UPDATE nucleo_pedidos SET status = 'CANCELADO' WHERE codigo = 'PS-1'")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "CANCELADO"):
            pedidos.editar_sem_servico(self._id(), {"endereco": "X"}, conn=self.conn)
        self.conn.execute("UPDATE nucleo_pedidos SET status = 'ABERTO', vuupt_service_id = 900 WHERE codigo = 'PS-1'")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "900"):
            pedidos.editar_sem_servico(-1, {"endereco": "X"}, conn=self.conn)
        self.assertEqual(self._linha()["endereco"], "RUA A 1, Sao Paulo - SP")


class ProtecaoContraSobrescritaTest(_Base):
    def test_rodada_do_pipeline_nao_sobrescreve_campo_editado(self):
        self._pipeline()
        pedidos.editar_sem_servico(self._id(), {"endereco": "RUA NOVA 2", "latitude": -23.6, "longitude": -46.7,
                                                "agendamento_inicio": "2026-12-01 08:00:00",
                                                "agendamento_fim": "2026-12-01 12:00:00"}, conn=self.conn)
        # rodada seguinte: a Stokki manda o de sempre e mudou as caixas
        self._pipeline(dimension_3=5, latitude=-23.5, longitude=-46.6)
        linha = self._linha()
        self.assertEqual(linha["endereco"], "RUA NOVA 2")
        self.assertEqual((linha["latitude"], linha["longitude"]), (-23.6, -46.7))
        self.assertEqual((linha["agendamento_inicio"], linha["agendamento_fim"]),
                         ("2026-12-01 08:00:00", "2026-12-01 12:00:00"))
        self.assertEqual(linha["caixas"], 5)   # o que nao foi editado segue a Stokki

    def test_pedido_com_servico_segue_o_pipeline(self):
        self._pipeline()
        pedidos.editar_sem_servico(self._id(), {"endereco": "RUA NOVA 2"}, conn=self.conn)
        # ganhou servico (materializado): o pipeline volta a ser a fonte, como hoje com a Vuupt
        pedidos.registrar_importacao(_payload(), {"service": {"id": 900}}, "atualizado", conn=self.conn)
        pedidos.registrar_importacao(_payload(customer={"name": "DEST", "code": "123", "address": "RUA B 3"}),
                                     {"service": {"id": 900}}, "atualizado", conn=self.conn)
        self.assertEqual(self._linha()["endereco"], "RUA B 3")

    def test_edicao_local_chega_ao_payload_da_materializacao(self):
        self._pipeline()
        pedidos.editar_sem_servico(self._id(), {"endereco": "RUA NOVA 2", "latitude": -23.6, "longitude": -46.7,
                                                "agendamento_inicio": "2026-12-01 08:00:00",
                                                "agendamento_fim": "2026-12-01 12:00:00"}, conn=self.conn)
        self._pipeline()   # rodada do pipeline no meio
        payload = materializar.montar_payload(self._linha(), FakeVuupt())
        self.assertEqual(payload["customer"]["address"], "RUA NOVA 2")
        self.assertEqual((payload["latitude"], payload["longitude"]), (-23.6, -46.7))
        self.assertEqual((payload["scheduled_start"], payload["scheduled_end"]),
                         ("2026-12-01T08:00:00-03:00", "2026-12-01T12:00:00-03:00"))


class MigracaoTest(unittest.TestCase):
    def test_coluna_nova_em_banco_antigo(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "t.db"
            conn = banco.conectar(db)
            conn.execute("ALTER TABLE nucleo_pedidos DROP COLUMN edicao_local_json")
            conn.commit()
            conn.close()
            conn = banco.conectar(db)
            try:
                colunas = {r[1] for r in conn.execute("PRAGMA table_info(nucleo_pedidos)")}
            finally:
                conn.close()
            self.assertIn("edicao_local_json", colunas)


if __name__ == "__main__":
    unittest.main()
