# -*- coding: utf-8 -*-
"""
Reenvio da NF de pedido cancelado fora do portal (Hugo, 04/11): a equipe
cancela na Stokki/Vuupt, o envio do portal continua CRIADO e o cliente nao
conseguia subir o mesmo XML de novo ("Essa NF-e ja foi enviada"). A Stokki
aceita a chave de pedido cancelado; se o pedido ainda estiver ativo la, ela
mesma recusa e o envio volta a DUPLICADO.

    py -3.11 -m unittest portal_cliente.test_reenvio_cancelado
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

EMB = "22310186000118"
CHAVE = "1" * 44


class ReenvioDePedidoCanceladoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(ep, "DB_PATH", Path(self._tmp.name) / "dados.db")
        p.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(p.stop)
        self.conn = ep.conectar()
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE nucleo_pedidos (codigo TEXT PRIMARY KEY, status TEXT)")
        self.conn.execute("INSERT INTO portal_envios (cnpj_embarcador, chave_nfe, numero_nf, xml_path, status, "
                          "codigo_pedido, criado_em, atualizado_em) VALUES (?, ?, '41389', 'x.xml', 'CRIADO', 'PS-40674', "
                          "'2026-10-02 15:17:53', '2026-10-02 15:17:53')", (EMB, CHAVE))
        self.conn.commit()
        self.item = {"chave_nfe": CHAVE, "emitente_cnpj": EMB, "emitente_nome": "VIDAVEG",
                     "destinatario_doc": "98765432000110", "destinatario_endereco": "Rua X"}

    def _nucleo(self, *linhas):
        self.conn.executemany("INSERT INTO nucleo_pedidos VALUES (?, ?)", linhas)
        self.conn.commit()

    def test_pedido_ativo_continua_bloqueando(self):
        self._nucleo(("PS-40674", "ABERTO"))
        v = ep.validar_item(self.conn, self.item, EMB)
        self.assertFalse(v["ok"])
        self.assertIn("já foi enviada", v["erros"][0])

    def test_pedido_cancelado_fora_do_portal_pode_ser_reenviado(self):
        self._nucleo(("PS-40674", "CANCELADO"))
        v = ep.validar_item(self.conn, self.item, EMB)
        self.assertTrue(v["ok"], v["erros"])
        self.assertIn("PS-40674", v["avisos"][0])
        self.assertIn("será reenviado", v["avisos"][0])
        self.assertEqual(v["envio_existente"]["codigo_pedido"], "PS-40674")

    def test_reentrega_ativa_segura_o_reenvio(self):
        self._nucleo(("PS-40674", "CANCELADO"), ("PS-40674-R1", "EM_ROTA"))
        self.assertFalse(ep.validar_item(self.conn, self.item, EMB)["ok"])

    def test_sem_linha_no_nucleo_continua_bloqueando(self):
        self.assertFalse(ep.validar_item(self.conn, self.item, EMB)["ok"])

    def test_sem_tabela_do_nucleo_continua_bloqueando(self):
        self.conn.execute("DROP TABLE nucleo_pedidos")
        self.assertFalse(ep.validar_item(self.conn, self.item, EMB)["ok"])


if __name__ == "__main__":
    unittest.main()
