# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from datetime import datetime

from regras import alteracoes_entrega as ae


class AlteracoesEntregaTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = ae.conectar(self.path)

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def _reg(self, codigo="#PS-1234", endereco=None, status="APLICADA", lat=None, lon=None, sender_id=1):
        return ae.registrar(
            self.conn, cnpj_embarcador="00000000000191", sender_id=sender_id, service_id=10,
            codigo=codigo, por="teste", antes={}, depois={}, endereco=endereco,
            latitude=lat, longitude=lon, rota_id_tirada=None, status=status)

    def test_codigo_base_tira_hash_e_reentrega(self):
        self.assertEqual(ae.codigo_base("#PS-1234-R1"), "PS-1234")
        self.assertEqual(ae.codigo_base("ps-1234"), "PS-1234")
        self.assertEqual(ae.codigo_base("PS-1234-C2"), "PS-1234", 1)
        self.assertIsNone(ae.codigo_base(""))

    def test_endereco_vigente_pega_a_ultima_aplicada(self):
        self._reg(endereco={"rua": "A"}, lat=1.0, lon=2.0)
        self._reg(endereco={"rua": "B"}, lat=3.0, lon=4.0)
        self._reg(endereco={"rua": "C"}, status="FALHOU")
        self.assertEqual(ae.endereco_vigente(self.conn, "#PS-1234", 1),
                         {"endereco": {"rua": "B"}, "latitude": 3.0, "longitude": 4.0})

    def test_endereco_vigente_vale_pra_reentrega(self):
        self._reg(codigo="#PS-1234", endereco={"rua": "A"})
        self.assertEqual(ae.endereco_vigente(self.conn, "#PS-1234-R1", 1)["endereco"], {"rua": "A"})

    def test_endereco_vigente_ignora_alteracao_so_de_data(self):
        self._reg(endereco=None)
        self.assertIsNone(ae.endereco_vigente(self.conn, "PS-1234", 1))

    def test_endereco_vigente_nao_mistura_embarcadores(self):
        self._reg(endereco={"rua": "A"}, sender_id=1)
        self.assertIsNone(ae.endereco_vigente(self.conn, "#PS-1234", 2))
        self.assertEqual(ae.endereco_vigente(self.conn, "#PS-1234", 1)["endereco"], {"rua": "A"})

    def test_houve_alteracao_desde_tabela_vazia_e_so_falhou(self):
        agora = datetime(2026, 10, 29, 15, 50)
        self.assertFalse(ae.houve_alteracao_desde(self.conn, 60, agora=agora))
        self._reg(status="FALHOU")
        self.conn.execute("UPDATE alteracoes_entrega_portal SET criado_em='2026-10-29 15:10:00'")
        self.conn.commit()
        self.assertFalse(ae.houve_alteracao_desde(self.conn, 60, agora=agora))

    def test_houve_alteracao_desde(self):
        self._reg()
        self.conn.execute("UPDATE alteracoes_entrega_portal SET criado_em='2026-10-29 15:10:00'")
        self.conn.commit()
        self.assertTrue(ae.houve_alteracao_desde(self.conn, 60, agora=datetime(2026, 10, 29, 15, 50)))
        self.assertFalse(ae.houve_alteracao_desde(self.conn, 60, agora=datetime(2026, 10, 29, 16, 20)))


if __name__ == "__main__":
    unittest.main()
