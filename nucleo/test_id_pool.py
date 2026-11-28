# -*- coding: utf-8 -*-
"""
test_id_pool.py

id_pool estavel em nucleo_pedidos e id sintetico negativo do pool.

    py -3.11 -m unittest nucleo.test_id_pool -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import banco, pedidos


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "t.db"
        self._patch = mock.patch.object(banco, "DB_PATH", self.db)
        self._patch.start()
        self.conn = banco.conectar()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _novo(self, codigo):
        pedidos.upsert_pedido(self.conn, codigo, {"titulo": codigo}, banco.ORIGEM_PIPELINE)
        self.conn.commit()

    def _id(self, codigo):
        return self.conn.execute("SELECT id_pool FROM nucleo_pedidos WHERE codigo=?", (codigo,)).fetchone()[0]


class TestIdPool(_Base):
    def test_backfill_da_id_pool_a_todos_e_e_unico(self):
        # banco "antigo": linhas sem id_pool
        self.conn.executemany(
            "INSERT INTO nucleo_pedidos (codigo, criado_em) VALUES (?, ?)",
            [("PS-3", "2026-01-02 10:00:00"), ("PS-1", "2026-01-03 10:00:00"), ("PS-2", "2026-01-02 10:00:00")])
        self.conn.commit()
        banco.garantir_esquema(self.conn)
        banco.garantir_esquema(self.conn)  # idempotente
        ids = dict(self.conn.execute("SELECT codigo, id_pool FROM nucleo_pedidos").fetchall())
        self.assertEqual(ids, {"PS-2": 1, "PS-3": 2, "PS-1": 3})  # criado_em, codigo
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE nucleo_pedidos SET id_pool = 1 WHERE codigo = 'PS-1'")

    def test_pedido_novo_ganha_id_pool_crescente(self):
        self._novo("PS-10")
        self._novo("PS-11")
        pedidos.upsert_pedido(self.conn, "PS-10", {"titulo": "x"}, banco.ORIGEM_PIPELINE)  # update nao muda
        self.assertEqual((self._id("PS-10"), self._id("PS-11")), (1, 2))

    def test_id_nao_e_reutilizado_apos_apagar(self):
        self._novo("PS-10")
        self._novo("PS-11")
        self.conn.execute("DELETE FROM nucleo_pedidos WHERE codigo = 'PS-11'")
        self.conn.commit()
        self._novo("PS-12")
        self.assertEqual(self._id("PS-12"), 3)

    def test_id_pool_sobrevive_a_vacuum_into(self):
        for c in ("PS-1", "PS-2", "PS-3"):
            self._novo(c)
        self.conn.execute("DELETE FROM nucleo_pedidos WHERE codigo = 'PS-1'")
        self.conn.commit()
        copia = Path(self._tmp.name) / "copia.db"
        self.conn.execute(f"VACUUM INTO '{copia.as_posix()}'")
        c2 = sqlite3.connect(copia)
        try:
            antes = dict(self.conn.execute("SELECT codigo, id_pool FROM nucleo_pedidos").fetchall())
            depois = dict(c2.execute("SELECT codigo, id_pool FROM nucleo_pedidos").fetchall())
        finally:
            c2.close()
        self.assertEqual(antes, depois)
        self.assertEqual(depois, {"PS-2": 2, "PS-3": 3})

    def test_estado_estavel_nao_abre_transacao_de_escrita(self):
        self._novo("PS-1")
        banco.garantir_esquema(self.conn)
        self.assertFalse(self.conn.in_transaction)
        # outra conexao segura o lock de escrita: conectar() nao pode bloquear
        outra = sqlite3.connect(self.db, timeout=0.2, isolation_level=None)
        try:
            outra.execute("BEGIN IMMEDIATE")
            c2 = banco.conectar()
            self.assertFalse(c2.in_transaction)
            c2.close()
            outra.execute("ROLLBACK")
        finally:
            outra.close()

    def test_pedido_existente_com_id_pool_null_recebe_id_no_upsert(self):
        self._novo("PS-1")
        self.conn.execute("DROP INDEX idx_nucleo_pedidos_id_pool")
        self.conn.execute("UPDATE nucleo_pedidos SET id_pool = NULL WHERE codigo = 'PS-1'")
        self.conn.commit()
        pedidos.upsert_pedido(self.conn, "PS-1", {"titulo": "x"}, banco.ORIGEM_PIPELINE)
        self.conn.commit()
        self.assertEqual(self._id("PS-1"), 2)  # proximo do contador (PS-1 original usou 1)

    def test_id_sintetico_ida_e_volta(self):
        self._novo("PS-77")
        row = self.conn.execute("SELECT * FROM nucleo_pedidos WHERE codigo='PS-77'").fetchone()
        n = pedidos.id_sintetico(row)
        self.assertLess(n, 0)
        self.assertEqual(pedidos.codigo_por_id_sintetico(self.conn, n), "PS-77")
        self.assertIsNone(pedidos.codigo_por_id_sintetico(self.conn, 5001))
        self.assertIsNone(pedidos.codigo_por_id_sintetico(self.conn, -999))


class _ConnPragmaVelho:
    """Simula a corrida logo apos o git pull: o PRAGMA deste processo foi lido
    antes de outro processo fazer o ALTER (a coluna ja existe de verdade)."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA table_info"):
            return iter([])
        return self._conn.execute(sql, *args)


class TestMigracaoConcorrente(_Base):
    def test_coluna_ja_criada_por_outro_processo_nao_derruba(self):
        # sem a tolerancia o 2o processo morre 1x com "duplicate column name"
        banco._migrar_colunas(_ConnPragmaVelho(self.conn), "nucleo_pedidos",
                              [("id_pool", "INTEGER"), ("edicao_local_json", "TEXT")])
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(nucleo_pedidos)")}
        self.assertIn("id_pool", cols)

    def test_outro_erro_de_alter_continua_subindo(self):
        with self.assertRaises(sqlite3.OperationalError):
            banco._migrar_colunas(self.conn, "tabela_que_nao_existe", [("x", "TEXT")])


class _ConnComLock:
    """Conexao de verdade, mas o BEGIN IMMEDIATE da migracao do id_pool acha o banco travado (processo
    antigo escrevendo na janela do deploy)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __getattr__(self, nome):
        return getattr(self._conn, nome)


class TestMigracaoComBancoTravado(_Base):
    """Onda 4 (revisao de seguranca M2): linha sem id_pool gravada por processo antigo + banco travado no
    BEGIN IMMEDIATE da migracao nao pode derrubar o conectar() (500 no portal, timer falhando)."""

    def setUp(self):
        super().setUp()
        self.conn.execute("INSERT INTO nucleo_pedidos (codigo, criado_em) VALUES ('PS-77', '2026-11-27 10:00:00')")
        self.conn.commit()   # linha de codigo antigo: id_pool NULL
        banco._AVISOU_ID_POOL_TRAVADO = False

    def test_conectar_segue_com_warning_uma_vez_e_o_upsert_preenche_a_linha(self):
        real = sqlite3.connect
        with mock.patch.object(banco.sqlite3, "connect", side_effect=lambda *a, **k: _ConnComLock(real(*a, **k))):
            with self.assertLogs(banco.logger, "WARNING") as logs:
                c1 = banco.conectar()
                c2 = banco.conectar()   # tenta de novo, mas nao repete o aviso
            try:
                self.assertEqual(sum("id_pool" in m and "travado" in m for m in logs.output), 1)
                self.assertIsNone(c1.execute("SELECT id_pool FROM nucleo_pedidos WHERE codigo='PS-77'").fetchone()[0])
                pedidos.upsert_pedido(c1, "PS-77", {"titulo": "x"}, banco.ORIGEM_PIPELINE)   # por linha
                c1.commit()
                self.assertIsNotNone(self._id("PS-77"))
            finally:
                c1.close()
                c2.close()
        # sem o lock, a proxima conexao migra o que faltar
        banco.conectar().close()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nucleo_pedidos WHERE id_pool IS NULL").fetchone()[0], 0)

    def test_upsert_tolera_contador_ausente_na_janela_do_backfill_adiado(self):
        """Onda 4 round 1: 1a migracao adiada pelo lock = nucleo_contadores pode nao existir (ou estar sem a linha
        'id_pool'); o upsert grava o pedido sem id_pool e a proxima conexao completa."""
        for estrago in ("DROP TABLE nucleo_contadores", "DELETE FROM nucleo_contadores"):
            codigo = "PS-81" if estrago.startswith("DROP") else "PS-82"
            self.conn.execute(estrago)
            self.conn.commit()
            pedidos.upsert_pedido(self.conn, codigo, {"titulo": "x"}, banco.ORIGEM_PIPELINE)
            self.conn.commit()
            self.assertIsNone(self._id(codigo))
            banco.conectar().close()   # sem lock: o backfill cria o contador e preenche
            self.assertIsNotNone(self._id(codigo))

    def test_outro_erro_continua_subindo(self):
        class _Quebrado(_ConnComLock):
            def execute(self, sql, *args):
                if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                    raise sqlite3.OperationalError("disk I/O error")
                return self._conn.execute(sql, *args)
        with self.assertRaises(sqlite3.OperationalError):
            banco._garantir_id_pool(_Quebrado(self.conn))


if __name__ == "__main__":
    unittest.main()
