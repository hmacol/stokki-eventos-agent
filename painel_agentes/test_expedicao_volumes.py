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
import wms_etiqueta_volume as vol  # noqa: E402
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


ALFABETO = r"^[2-9A-HJ-NP-Z]{8}$"


class TestEtiquetar(Base):
    def ativos(self, codigo="PS-45352"):
        return self.conn.execute("SELECT * FROM expedicao_volumes WHERE codigo = ? AND anulado_em IS NULL ORDER BY numero",
                                 (codigo,)).fetchall()

    def test_cria_as_caixas_e_um_trabalho_na_fila(self):
        r = ev.etiquetar(self.conn, "45352", 3, ANA)
        self.assertEqual((r["codigo"], r["total"], r["repetido"], r["anuladas"], r["conhecido"]), ("PS-45352", 3, False, 0, False))
        linhas = self.ativos()
        self.assertEqual([l["numero"] for l in linhas], [1, 2, 3])
        self.assertEqual(len({l["id_caixa"] for l in linhas}), 3)
        for l in linhas:
            self.assertRegex(l["id_caixa"], ALFABETO)
            self.assertEqual(l["criado_por"], "Ana")
        t = fila.obter(self.conn, r["impressao_id"])
        self.assertEqual((t["tipo"], t["tamanho"], t["pedido_por"], t["titulo"]), ("VOLUME", "10x15", "Ana", "PS-45352 · 3 volume(s)"))
        self.assertEqual(t["itens"], [ev.conteudo_qr("PS-45352", l["numero"], 3, l["id_caixa"]) for l in linhas])
        self.assertEqual(t["itens"][1], f"FV|PS-45352|2|3|{linhas[1]['id_caixa']}")

    def test_repetir_mesmo_total(self):
        a = ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        b = ev.etiquetar(self.conn, "#ps-45352", 3, ANA)
        self.assertTrue(b["repetido"])
        self.assertEqual(b["impressao_id"], a["impressao_id"])
        self.assertEqual(len(fila.listar(self.conn)), 1)

    def test_trocar_total(self):
        a = ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        antigos = {l["id_caixa"] for l in self.ativos()}
        pede = ev.etiquetar(self.conn, "PS-45352", 4, ANA)
        self.assertEqual(pede, {"confirmar": ["PS-45352 ja tem 3 etiquetas. As 3 antigas deixam de valer: troque todas pelas 4 novas."]})
        self.assertEqual(len(self.ativos()), 3)                       # nada mudou
        r = ev.etiquetar(self.conn, "PS-45352", 4, {"id": "2", "nome": "Bia"}, confirmado=True)
        self.assertEqual((r["total"], r["anuladas"]), (4, 3))
        self.assertEqual(len(self.ativos()), 4)
        self.assertFalse(antigos & {l["id_caixa"] for l in self.ativos()})
        anuladas = self.conn.execute("SELECT anulado_por FROM expedicao_volumes WHERE anulado_em IS NOT NULL").fetchall()
        self.assertEqual([x[0] for x in anuladas], ["Bia"] * 3)
        self.assertEqual(fila.obter(self.conn, a["impressao_id"])["situacao"], "CANCELADO")

    def test_trabalho_ja_impresso_nao_vira_cancelado(self):
        a = ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        fila.marcar_impresso(self.conn, a["impressao_id"], "estacao")
        ev.etiquetar(self.conn, "PS-45352", 2, ANA, confirmado=True)
        self.assertEqual(fila.obter(self.conn, a["impressao_id"])["situacao"], "IMPRESSO")

    def test_mais_de_99_e_cancelado_pedem_confirmacao(self):
        _nucleo(self.conn)
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-9','X','Y','Z','CANCELADO',NULL)")
        self.assertEqual(ev.etiquetar(self.conn, "PS-9", 120, ANA)["confirmar"],
                         ["Sao mesmo 120 caixas?", "PS-9 esta CANCELADO no nosso sistema. Etiquetar mesmo assim?"])
        self.assertEqual(self.ativos("PS-9"), [])
        self.assertEqual(ev.etiquetar(self.conn, "PS-9", 120, ANA, confirmado=True)["total"], 120)
        self.assertEqual(ev.etiquetar(self.conn, "PS-8", 99, ANA)["total"], 99)       # 99 nao pergunta
        self.assertEqual(ev.etiquetar(self.conn, "PS-7", "300", ANA, confirmado=True)["total"], 300)

    def test_pedido_grande_vai_pra_fila_em_lotes_de_50(self):
        # revisao final: o PDF monta todas as artes na memoria (300 de uma vez = ~700 MB no painel)
        r = ev.etiquetar(self.conn, "PS-45352", 120, ANA, confirmado=True)
        trabalhos = fila.listar(self.conn)
        self.assertEqual([len(t["itens"]) for t in trabalhos], [50, 50, 20])
        self.assertEqual([t["titulo"] for t in trabalhos],
                         ["PS-45352 · volumes 1 a 50 de 120", "PS-45352 · volumes 51 a 100 de 120",
                          "PS-45352 · volumes 101 a 120 de 120"])
        self.assertEqual((r["impressao_id"], r["trabalhos"]), (trabalhos[0]["id"], 3))
        linhas = self.ativos()
        self.assertEqual([linhas[0]["impressao_id"], linhas[50]["impressao_id"], linhas[119]["impressao_id"]],
                         [t["id"] for t in trabalhos])
        self.assertEqual(ev.etiquetar(self.conn, "PS-45352", 120, ANA)["trabalhos"], 3)        # repetido
        self.assertEqual([len(fila.obter(self.conn, i)["itens"])
                          for i in ev.reimprimir(self.conn, "PS-45352", ANA)["impressao_ids"]], [50, 50, 20])
        ev.etiquetar(self.conn, "PS-45352", 2, ANA, confirmado=True)
        self.assertEqual({fila.obter(self.conn, t["id"])["situacao"] for t in trabalhos}, {"CANCELADO"})

    def test_quantidade_invalida(self):
        for ruim in (0, -1, 301, 5.5, "abc", "", None, "5,0"):
            with self.assertRaises(wms.ErroWMS):
                ev.etiquetar(self.conn, "PS-45352", ruim, ANA, confirmado=True)
        self.assertEqual(self.ativos(), [])

    def test_situacao(self):
        self.assertIsNone(ev.situacao(self.conn, "45352")["volumes"])
        ev.etiquetar(self.conn, "45352", 3, ANA)
        v = ev.situacao(self.conn, "FV|PS-45352|1|3")["volumes"]
        self.assertEqual((v["total"], v["criado_por"], v["reimpressoes"]), (3, "Ana", 0))


class TestReimprimir(Base):
    def test_todas_e_uma_so(self):
        ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        originais = fila.listar(self.conn)[0]["itens"]
        r = ev.reimprimir(self.conn, "PS-45352", ANA)
        self.assertEqual((r["quantidade"], fila.obter(self.conn, r["impressao_id"])["itens"]), (3, originais))
        r2 = ev.reimprimir(self.conn, "PS-45352", ANA, numero=2)
        self.assertEqual(fila.obter(self.conn, r2["impressao_id"])["itens"], [originais[1]])
        self.assertEqual([x[0] for x in self.conn.execute("SELECT reimpressoes FROM expedicao_volumes ORDER BY numero")], [1, 2, 1])

    def test_sem_etiqueta_ou_numero_que_nao_existe(self):
        with self.assertRaises(wms.ErroWMS):
            ev.reimprimir(self.conn, "PS-45352", ANA)
        ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        with self.assertRaises(wms.ErroWMS):
            ev.reimprimir(self.conn, "PS-45352", ANA, numero=9)


class TestProntos(Base):
    def test_so_pronto_na_stokki_e_sem_etiqueta(self):
        self.assertEqual(ev.prontos_sem_etiqueta(self.conn), {"total": 0, "itens": []})    # sem a tabela
        _vigia(self.conn)
        self.conn.executemany("INSERT INTO vigia_stokki_abertos VALUES (?,?,?,?,?,?,?)", [
            ("PS-1", 1, "FABENNE", "Waiting for Carrier", "aguardando_transportador", "2026-11-28 10:00:00", "x"),
            ("PS-2", 2, "FABENNE", "Open", "prioritario", "2026-11-28 11:00:00", "x"),
            ("PS-3", 3, "MARCHEF", "Waiting for Carrier", "prioritario", "2026-11-28 12:00:00", "x"),
            ("PS-4", 4, "MARCHEF", "Waiting for Carrier", "prioritario", "2026-11-28 13:00:00", "x")])
        ev.etiquetar(self.conn, "PS-3", 2, ANA)
        r = ev.prontos_sem_etiqueta(self.conn, limite=1)
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["itens"], [{"codigo": "PS-4", "embarcador": "MARCHEF", "desde": "2026-11-28 13:00:00"}])


class TestPdf(Base):
    def test_dados_da_etiqueta(self):
        _nucleo(self.conn)
        self.conn.execute("INSERT INTO nucleo_pedidos VALUES ('PS-45352','Mercado Bom','Rua A, 10','FABENNE','ABERTO',NULL)")
        r = ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        item = fila.obter(self.conn, r["impressao_id"])["itens"][1]
        d = ev.dados_da_etiqueta(self.conn, item)
        self.assertEqual((d["codigo_ps"], d["numero"], d["total"], d["qr"], d["destinatario"], d["embarcador"]),
                         ("PS-45352", 2, 3, item, "Mercado Bom", "FABENNE"))
        self.assertIsNone(ev.dados_da_etiqueta(self.conn, "FV|PS-45352|2|3|ZZZZZZZZ"))
        self.assertIsNone(ev.dados_da_etiqueta(self.conn, "FV|PS-45352|2|3"))
        outro = item.replace("PS-45352", "PS-1")
        self.assertIsNone(ev.dados_da_etiqueta(self.conn, outro))          # id de outro pedido

    def test_pdf_mistura_piloto_e_novo(self):
        r = ev.etiquetar(self.conn, "PS-45352", 2, ANA)
        itens = fila.obter(self.conn, r["impressao_id"])["itens"] + ["FV|PS-44009|1|1"]
        self.assertTrue(vol.gerar_pdf_volumes(self.conn, itens).startswith(b"%PDF"))

    def test_pdf_recusa_anulada(self):
        a = ev.etiquetar(self.conn, "PS-45352", 2, ANA)
        itens = fila.obter(self.conn, a["impressao_id"])["itens"]
        ev.etiquetar(self.conn, "PS-45352", 3, ANA, confirmado=True)
        with self.assertRaisesRegex(wms.ErroWMS, "anulada"):
            vol.gerar_pdf_volumes(self.conn, itens)


class TestPdfNaTela(Base):
    """Hugo, 28/11: a tela e usada no computador e o PDF abre direto depois do Imprimir, com o
    login do galpao (que nao entra na fila /etiquetas/fila). Abrir pela tela marca como impresso,
    pra o trabalho nao ficar pendente na fila e alguem imprimir de novo."""

    def test_etiquetar_e_reimprimir_devolvem_todos_os_trabalhos(self):
        r = ev.etiquetar(self.conn, "PS-45352", 3, ANA)
        self.assertEqual(r["impressao_ids"], [r["impressao_id"]])
        self.assertEqual(ev.etiquetar(self.conn, "PS-45352", 3, ANA)["impressao_ids"], r["impressao_ids"])   # repetido
        grande = ev.etiquetar(self.conn, "PS-7", 120, ANA, confirmado=True)
        self.assertEqual((len(grande["impressao_ids"]), grande["impressao_ids"][0]), (3, grande["impressao_id"]))

    def test_pdf_do_trabalho_so_serve_o_que_e_da_tela(self):
        r = ev.etiquetar(self.conn, "PS-45352", 2, ANA)
        self.assertTrue(ev.pdf_do_trabalho(self.conn, r["impressao_id"]).startswith(b"%PDF"))
        piloto = fila.enfileirar(self.conn, tipo="VOLUME", tamanho="10x15", itens=["FV|PS-44009|1|1"], titulo="piloto")
        caixa = fila.enfileirar(self.conn, tipo="CAIXA", tamanho="10x5", itens=["FL|X|L1||1"], titulo="caixa")
        self.conn.commit()
        for alheio in (piloto, caixa, 99999):
            with self.assertRaisesRegex(wms.ErroWMS, "nao e de etiqueta de volume"):
                ev.pdf_do_trabalho(self.conn, alheio)
        ev.etiquetar(self.conn, "PS-45352", 3, ANA, confirmado=True)          # o total mudou: o antigo foi cancelado
        with self.assertRaisesRegex(wms.ErroWMS, "cancelado"):
            ev.pdf_do_trabalho(self.conn, r["impressao_id"])

    def test_marcar_impresso_so_o_que_e_da_tela(self):
        r = ev.etiquetar(self.conn, "PS-45352", 2, ANA)
        m = ev.marcar_impresso(self.conn, r["impressao_id"], ANA)
        self.assertEqual((m["impressao_id"], m["situacao"]), (r["impressao_id"], "IMPRESSO"))
        self.assertEqual(fila.obter(self.conn, r["impressao_id"])["impresso_por"], "Ana")
        piloto = fila.enfileirar(self.conn, tipo="VOLUME", tamanho="10x15", itens=["FV|PS-44009|1|1"], titulo="piloto")
        self.conn.commit()
        with self.assertRaises(wms.ErroWMS):
            ev.marcar_impresso(self.conn, piloto, ANA)
        self.assertEqual(fila.obter(self.conn, piloto)["situacao"], "PENDENTE")


if __name__ == "__main__":
    unittest.main()
