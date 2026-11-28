"""
Etiqueta de volume pra todo pedido finalizado na Stokki (dados): tabela expedicao_volumes,
codigo do pedido, de onde vem o que vai na etiqueta, etiquetar/reimprimir e o PDF.
Rodar (de dentro de painel_agentes): py -3.11 -m unittest test_expedicao_volumes -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_fila_impressao as fila  # noqa: E402
import wms_separacao as sep  # noqa: E402
import expedicao_volumes as ev  # noqa: E402

ANA = {"id": "1", "nome": "Ana"}


def _nucleo(conn):
    conn.execute("CREATE TABLE nucleo_pedidos (codigo TEXT PRIMARY KEY, destinatario_nome TEXT, endereco TEXT, "
                 "remetente_nome TEXT, status TEXT, excluido_em TEXT)")


def _vigia(conn):
    conn.execute("CREATE TABLE vigia_stokki_abertos (codigo TEXT PRIMARY KEY, id_stokki INTEGER, embarcador TEXT, "
                 "status_stokki TEXT, fonte TEXT, primeira_vez_em TEXT, ultima_vez_em TEXT)")


def _conferidos(conn):
    conn.execute("CREATE TABLE volumes_conferidos (id_pedido INTEGER PRIMARY KEY, volumes INTEGER, fonte TEXT, lido_em TEXT)")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.conn = sep.conectar(Path(self._tmp.name) / "dados.db")
        self.addCleanup(self.conn.close)


class TestCodigo(Base):
    def test_codigo_base(self):
        for entrada in ("#ps-45352", "45352", " PS-45352-R1 ", "ps45352",
                        "FV|PS-45352|2|5|ABCD2345", "FV|PS-45352|2|5"):
            self.assertEqual(ev.codigo_base(entrada), "PS-45352")
        for ruim in ("", None, "abc", "PS-", "PS-1, PS-2", "FV|X|1|1"):
            with self.assertRaises(wms.ErroWMS):
                ev.codigo_base(ruim)


class TestDadosDoPedido(Base):
    def test_sem_nenhuma_fonte_nao_quebra(self):
        d = ev.dados_do_pedido(self.conn, "PS-45352")
        self.assertEqual((d["conhecido"], d["cancelado"], d["sugestao"], d["destinatario"]), (False, False, None, ""))

    def test_fontes_em_ordem_e_completando(self):
        _nucleo(self.conn)
        _vigia(self.conn)
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-45352','Mercado Bom','Rua A, 10 - Barueri/SP','FABENNE','ABERTO',NULL)")
        self.conn.execute("INSERT INTO wms_pedidos (id_stokki, codigo_ps, embarcador, situacao, lido_em, atualizado_em, "
                          "destinatario, endereco_destino, cidade, uf) VALUES (45352,'#PS-45352','OUTRO','Open','x','x',"
                          "'Outro nome','Rua B','Barueri','SP')")
        d = ev.dados_do_pedido(self.conn, "PS-45352")
        self.assertTrue(d["conhecido"])
        self.assertEqual((d["destinatario"], d["embarcador"], d["endereco"]), ("Mercado Bom", "FABENNE", "Rua A, 10 - Barueri/SP"))
        self.assertEqual((d["cidade"], d["uf"]), ("Barueri", "SP"))      # veio do wms_pedidos

    def test_so_o_retrato_da_stokki_da_o_embarcador(self):
        _vigia(self.conn)
        self.conn.execute("INSERT INTO vigia_stokki_abertos VALUES ('PS-7',7,'MARCHEF','Waiting for Carrier','prioritario','a','b')")
        d = ev.dados_do_pedido(self.conn, "PS-7")
        self.assertEqual((d["conhecido"], d["embarcador"], d["destinatario"]), (True, "MARCHEF", ""))

    def test_cancelado(self):
        _nucleo(self.conn)
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-9','X','Y','Z','CANCELADO',NULL)")
        self.assertTrue(ev.dados_do_pedido(self.conn, "PS-9")["cancelado"])

    def test_sugestao_so_com_conferencia_e_aguardando_transportador(self):
        # regra do Hugo (28/11): sugere o total CONFERIDO quando a Stokki ja esta em
        # "aguardando transportador"; fora disso nao sugere nada
        _vigia(self.conn)
        _conferidos(self.conn)
        self.conn.executemany("INSERT INTO vigia_stokki_abertos VALUES (?,?,?,?,?,?,?)", [
            ("PS-9", 9, "A", "Waiting for Carrier", "prioritario", "a", "b"),
            ("PS-10", 10, "A", "Waiting for Carrier", "prioritario", "a", "b"),   # so o numero da NF
            ("PS-11", 11, "A", "Ready to Pack", "em_separacao", "a", "b"),        # ainda em conferencia
            ("PS-13", 13, "A", "Waiting for Carrier", "prioritario", "a", "b")])  # nunca lido
        self.conn.execute("INSERT INTO volumes_conferidos VALUES (9, 7, 'conferencia', 'x'), (10, 4, 'nfe', 'x'), "
                          "(11, 5, 'conferencia', 'x'), (12, 6, 'conferencia', 'x')")       # 12: fora do retrato
        self.assertEqual(ev.dados_do_pedido(self.conn, "PS-9")["sugestao"], 7)
        for sem in ("PS-10", "PS-11", "PS-12", "PS-13"):
            self.assertIsNone(ev.dados_do_pedido(self.conn, sem)["sugestao"], sem)


class TestTabela(Base):
    def test_conectar_do_galpao_cria_a_tabela(self):
        self.assertIsNotNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name = 'expedicao_volumes'").fetchone())


if __name__ == "__main__":
    unittest.main()
