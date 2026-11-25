import sqlite3
import unittest

from nucleo import nf_pedido as n


def _banco():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE documentos_pedido (id_pedido, tipo_doc, status, numero_nf)")
    c.execute("CREATE TABLE documentos_processados (codigo_pedido, tipo, status, numero_nf)")
    c.executemany("INSERT INTO documentos_pedido VALUES (?,?,?,?)", [
        ("#PS-1", "nf", "casado", "100"),
        ("#PS-2", "nf", "casado", "300"), ("#PS-2", "nf", "baixado", "200"),
        ("#PS-4", "nf", "em_revisao", "999"),
        ("#PS-5", "boleto", "casado", "888"),
    ])
    c.executemany("INSERT INTO documentos_processados VALUES (?,?,?,?)", [
        ("#PS-1", "Nota Fiscal", "ENVIADO", "777"),   # principal vence
        ("#PS-3", "Nota Fiscal", "ENVIADO", "400"),   # so no apoio
    ])
    return c


class NfPedidoTest(unittest.TestCase):
    def test_codigo_base(self):
        self.assertEqual(n.codigo_base("#ps-36327-r1"), "PS-36327")
        self.assertEqual(n.codigos_base_lista("#PS-1, PS-2-R1"), ["PS-1", "PS-2"])

    def test_principal_apoio_varias_e_sem(self):
        m = n.nf_por_codigo(["#PS-1", "PS-2", "PS-3-R2", "PS-4", "PS-5", "PS-6"], _banco())
        self.assertEqual(m["PS-1"], "100")          # nao mistura com o apoio
        self.assertEqual(m["PS-2"], "200, 300")     # mais de uma: virgula
        self.assertEqual(m["PS-3"], "400")          # apoio
        for sem in ("PS-4", "PS-5", "PS-6"):        # em_revisao, boleto, inexistente
            self.assertNotIn(sem, m)

    def test_anotar(self):
        itens = [{"codigo": "#PS-2-R1"}, {"codigo": "#PS-6"}, {"codigo": "#PS-1, #PS-3"}]
        n.anotar_nf(itens, conn=_banco())
        self.assertEqual([i["nf"] for i in itens], ["200, 300", "", "100, 400"])

    def test_falha_de_banco_nao_quebra(self):
        c = sqlite3.connect(":memory:")  # sem tabelas
        self.assertEqual(n.nf_por_codigo(["PS-1"], c), {})


if __name__ == "__main__":
    unittest.main()
