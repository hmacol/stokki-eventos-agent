# -*- coding: utf-8 -*-
"""
Liberacao de NFs bloqueadas por area nao atendida, pelo chamado do
/atendimento (Hugo, 23/09/2026).

    py -3.11 -m unittest painel_agentes.test_atendimento_liberar
"""
import sqlite3
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _RAIZ / "portal_cliente", _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pedidos_dedicados as pd  # noqa: E402
from atendimento_chamados import liberar_envios, valor_brl, envios_do_chamado  # noqa: E402


def _envios():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE portal_envios (id INTEGER PRIMARY KEY, cnpj_embarcador TEXT, numero_nf TEXT, referencia TEXT, destinatario_nome TEXT,
            destinatario_municipio TEXT, destinatario_uf TEXT, status TEXT, bloqueio_chamado_id INTEGER, atualizado_em TEXT, codigo_pedido TEXT);
        INSERT INTO portal_envios VALUES (1,'1','10',NULL,'D1','Curitiba','PR','AGUARDANDO_LIBERACAO',77,NULL,NULL),
                                         (2,'1','11',NULL,'D2','Curitiba','PR','AGUARDANDO_LIBERACAO',77,NULL,NULL),
                                         (3,'1','12',NULL,'D3','Curitiba','PR','CANCELADO',77,NULL,NULL),
                                         (4,'1','13',NULL,'D4','Sao Paulo','SP','NA_FILA',NULL,NULL,NULL);""")
    return conn


class ValorBrl(unittest.TestCase):
    def test_formatos(self):
        self.assertEqual(valor_brl("150,50"), 150.5)
        self.assertEqual(valor_brl("1.234,56"), 1234.56)
        self.assertEqual(valor_brl("R$ 99"), 99.0)
        self.assertEqual(valor_brl(100), 100.0)
        # revisao 23/09: ponto de milhar sem virgula nao e decimal
        self.assertEqual(valor_brl("1.500"), 1500.0)
        self.assertEqual(valor_brl("12.345.678"), 12345678.0)
        self.assertEqual(valor_brl("1.5"), 1.5)      # decimal com ponto (1 ou 2 casas) continua valendo
        self.assertEqual(valor_brl("1.50"), 1.5)
        with self.assertRaises(ValueError):
            valor_brl("0,001")  # arredonda pra 0,00 -> invalido
        for ruim in ("abc", 0, "", None, -5):
            with self.assertRaises(ValueError, msg=repr(ruim)):
                valor_brl(ruim)


class Liberar(unittest.TestCase):
    def setUp(self):
        self.env = _envios()
        self.ded = pd.conectar(":memory:")
        self.msgs = []
        self.chamado = {"id": 77, "sender_id": 5, "nome_cliente": "ACME"}

    def _msg(self, texto):
        self.msgs.append(texto)

    def test_envios_do_chamado(self):
        envs = envios_do_chamado(self.env, 77)
        self.assertEqual([e["id"] for e in envs], [1, 2, 3])
        self.assertEqual(envs[2]["status_rotulo"], "Cancelado")

    def test_dedicado_divide_e_libera(self):
        r = liberar_envios(self.env, self.ded, self.chamado, [1, 2], "dedicado", "100", "hugo", self._msg)
        self.assertEqual(r["liberados"], [1, 2])
        self.assertEqual(r["valores"], {1: 50.0, 2: 50.0})
        self.assertEqual([x[0] for x in self.env.execute("SELECT status FROM portal_envios WHERE id IN (1,2)")], ["NA_FILA", "NA_FILA"])
        self.assertEqual(self.ded.execute("SELECT COUNT(*) FROM pedidos_dedicados").fetchone()[0], 2)
        self.assertEqual(tuple(self.ded.execute("SELECT numero_nf, sender_id, remetente_nome FROM pedidos_dedicados WHERE envio_id=1").fetchone()),
                         ("10", 5, "ACME"))
        self.assertIn("R$ 50,00", self.msgs[0])
        self.assertIn("NF 10", self.msgs[0])

    def test_dedicado_tres_nfs_sobra_no_primeiro(self):
        self.env.execute("INSERT INTO portal_envios VALUES (5,'1','14',NULL,'D5','Curitiba','PR','AGUARDANDO_LIBERACAO',77,NULL,NULL)")
        r = liberar_envios(self.env, self.ded, self.chamado, [1, 2, 5], "dedicado", "100", "hugo", self._msg)
        self.assertEqual(r["valores"], {1: 33.34, 2: 33.33, 5: 33.33})

    def test_simples(self):
        r = liberar_envios(self.env, self.ded, self.chamado, [1], "simples", None, "hugo", self._msg)
        self.assertEqual(r["liberados"], [1])
        self.assertEqual(self.ded.execute("SELECT COUNT(*) FROM pedidos_dedicados").fetchone()[0], 0)
        self.assertEqual(self.env.execute("SELECT status FROM portal_envios WHERE id=1").fetchone()[0], "NA_FILA")
        self.assertEqual(self.env.execute("SELECT status FROM portal_envios WHERE id=2").fetchone()[0], "AGUARDANDO_LIBERACAO")

    def test_cancelar(self):
        liberar_envios(self.env, self.ded, self.chamado, [2], "cancelar", None, "hugo", self._msg)
        self.assertEqual(self.env.execute("SELECT status FROM portal_envios WHERE id=2").fetchone()[0], "CANCELADO")
        self.assertIn("cancelada", self.msgs[0])

    def test_id_fora_do_chamado_ou_ja_tratado(self):
        with self.assertRaises(ValueError):
            liberar_envios(self.env, self.ded, self.chamado, [3], "simples", None, "hugo", self._msg)
        with self.assertRaises(ValueError):
            liberar_envios(self.env, self.ded, self.chamado, [4], "simples", None, "hugo", self._msg)
        with self.assertRaises(ValueError):
            liberar_envios(self.env, self.ded, {"id": 99}, [1], "simples", None, "hugo", self._msg)
        self.assertEqual(self.msgs, [])

    def test_dedicado_exige_valor_e_ids(self):
        with self.assertRaises(ValueError):
            liberar_envios(self.env, self.ded, self.chamado, [1], "dedicado", "", "hugo", self._msg)
        with self.assertRaises(ValueError):
            liberar_envios(self.env, self.ded, self.chamado, [], "simples", None, "hugo", self._msg)
        with self.assertRaises(ValueError):
            liberar_envios(self.env, self.ded, self.chamado, [1], "outro", None, "hugo", self._msg)


if __name__ == "__main__":
    unittest.main()
