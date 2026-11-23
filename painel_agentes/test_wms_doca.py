# -*- coding: utf-8 -*-
"""
Testes da posicao DOCA (espera do recebimento por etapas).
Rodar (da raiz): py -3.11 -m unittest painel_agentes.test_wms_doca -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_doca  # noqa: E402
import wms_pedidos  # noqa: E402


class TestDoca(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        self.conn = wms_doca.conectar(self.db)
        self.pid = 1
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, dun, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (1, 900, '72400017', "
            "'COXINHA FESTA', 'MARIA DOLORES', 48, '724000170000', NULL, 6, 'UN', 1, '2026-09-21 10:00:00')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_garantir_doca_cria_uma_vez(self):
        self.assertEqual(wms_doca.garantir_doca(self.conn), "DOC1-P1")
        self.assertEqual(wms_doca.garantir_doca(self.conn), "DOC1-P1")
        self.assertEqual(wms.obter_area(self.conn, "DOC1")["tipo"], "DOCA")
        self.assertEqual(len(wms.listar_posicoes(self.conn, area="DOC1")), 1)

    def test_e_doca(self):
        self.assertTrue(wms_doca.e_doca("doc1-p1"))
        self.assertFalse(wms_doca.e_doca("C1-E1-N1"))
        self.assertFalse(wms_doca.e_doca(None))

    def test_saldo_na_doca_atende_pedido(self):
        # decisao do Hugo 22/11: a doca vale como posicao comum na reserva
        wms_doca.garantir_doca(self.conn)
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=10,
                                lote="L1", validade="2027-01-01", destino="DOC1-P1")
        lotes = wms_pedidos.disponivel_por_lote(self.conn, self.pid)
        self.assertEqual([(l["posicao"], l["disponivel"]) for l in lotes], [("DOC1-P1", 10.0)])

    def test_temperatura_do_cadastro(self):
        wms.definir_temperatura_produto(self.conn, self.pid, "REFRIGERADO")
        self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "REFRIGERADO")

    def test_temperatura_deduzida_de_onde_esta(self):
        wms.criar_area(self.conn, "CG1", "CAM_CONGELADA"); wms.criar_posicao(self.conn, "CG1-P1")
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=5, destino="CG1-P1", validade="2027-01-01")
        self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "CONGELADO")

    def test_temperatura_desconhecida_e_doca_nao_conta(self):
        wms_doca.garantir_doca(self.conn)
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=self.pid, quantidade=5, destino="DOC1-P1", validade="2027-01-01")
        self.assertEqual(wms_doca.temperatura_do_produto(self.conn, self.pid), "")

    def test_temperatura_invalida(self):
        with self.assertRaises(wms.ErroWMS):
            wms.definir_temperatura_produto(self.conn, self.pid, "MORNO")

    def test_temperatura_vazia_limpa(self):
        wms.definir_temperatura_produto(self.conn, self.pid, "CONGELADO")
        self.assertEqual(wms.definir_temperatura_produto(self.conn, self.pid, "")["temperatura"], "")


class TestSugerirPosicoes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = wms_doca.conectar(Path(self._tmp.name) / "dados.db")
        self.pid = 1
        self._produto(1, 900, 48)
        wms.criar_area(self.conn, "C1", "CONTAINER")
        wms.gerar_posicoes(self.conn, "C1", estantes=3, niveis=3)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _produto(self, pid, stokki, emb):
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, dun, "
            "qtd_por_caixa, unidade, controla_validade, atualizado_em) VALUES (?, ?, ?, 'PROD', 'EMB', ?, NULL, NULL, "
            "6, 'UN', 1, '2026-09-21 10:00:00')", (pid, stokki, f"SKU{pid}", emb))
        self.conn.commit()

    def _entrada(self, destino, qtd=10, lote="L2611", validade="2027-06-30", pid=None):
        wms.registrar_movimento(self.conn, tipo="ENTRADA", produto_id=pid or self.pid, quantidade=qtd,
                                lote=lote, validade=validade, destino=destino)

    def _sug(self, lote="L2611", validade="2027-06-30", limite=3):
        return wms_doca.sugerir_posicoes(self.conn, self.pid, lote, validade, limite)

    def test_sugere_mesmo_lote_primeiro(self):
        self._entrada("C1-E1-N1", 80)
        s = self._sug()
        self.assertEqual(s[0], {"posicao": "C1-E1-N1", "criterio": "MESMO_LOTE",
                                "motivo": "Já tem este produto aqui (lote L2611, 80 UN)"})
        self.assertEqual(s[1]["criterio"], "AO_LADO")
        self.assertLessEqual(len(s), 3)

    def test_lote_diferente_cai_para_ao_lado(self):
        self._entrada("C1-E1-N1", lote="OUTRO")
        s = self._sug()
        self.assertNotIn("MESMO_LOTE", [x["criterio"] for x in s])
        self.assertEqual(s[0]["criterio"], "AO_LADO")
        self.assertEqual(s[0]["motivo"], "Ao lado do mesmo produto")

    def test_ao_lado_prefere_mesma_estante(self):
        self._entrada("C1-E2-N2", lote="OUTRO")
        s = self._sug(limite=4)
        self.assertEqual([x["posicao"] for x in s],
                         ["C1-E2-N1", "C1-E2-N3", "C1-E1-N2", "C1-E3-N2"])

    def test_embarcador_quando_produto_e_novo(self):
        self._produto(2, 901, 48)
        self._produto(3, 902, 99)
        wms.criar_area(self.conn, "C2", "CONTAINER"); wms.gerar_posicoes(self.conn, "C2", pallets=2)
        self._entrada("C1-E1-N1", pid=2)
        self._entrada("C1-E1-N2", pid=2, lote="X")
        self._entrada("C2-P1", pid=3)
        s = self._sug()
        self.assertEqual(s[0]["criterio"], "EMBARCADOR")
        self.assertTrue(s[0]["posicao"].startswith("C1-"))
        self.assertEqual(s[0]["motivo"], "Perto de outros produtos do mesmo embarcador")

    def test_vazia_como_ultimo_recurso(self):
        s = self._sug()
        self.assertEqual(s[0], {"posicao": "C1-E1-N1", "criterio": "VAZIA",
                                "motivo": "Posição vazia na área certa"})

    def test_respeita_temperatura(self):
        wms.criar_area(self.conn, "CG1", "CAM_CONGELADA"); wms.criar_posicao(self.conn, "CG1-P1")
        wms.definir_temperatura_produto(self.conn, self.pid, "CONGELADO")
        self.assertEqual([x["posicao"] for x in self._sug()], ["CG1-P1"])

    def test_sem_temperatura_nao_filtra(self):
        wms.criar_area(self.conn, "CG1", "CAM_CONGELADA"); wms.criar_posicao(self.conn, "CG1-P1")
        pos = [x["posicao"] for x in self._sug(limite=100)]
        self.assertIn("CG1-P1", pos)
        self.assertIn("C1-E1-N1", pos)

    def test_nunca_sugere_doca(self):
        wms_doca.garantir_doca(self.conn)
        self._entrada("DOC1-P1")
        pos = [x["posicao"] for x in self._sug(limite=100)]
        self.assertNotIn("DOC1-P1", pos)
        self.assertTrue(pos)

    def test_sem_posicao_vazia_devolve_lista_vazia(self):
        wms.criar_area(self.conn, "C9", "CONTAINER"); wms.criar_posicao(self.conn, "C9-P1")
        self.conn.execute("UPDATE wms_posicoes SET ativo = 0 WHERE codigo <> 'C9-P1'")
        self.conn.commit()
        self._entrada("C9-P1", lote="OUTRO")
        self.assertEqual(self._sug(), [])

    def test_lote_vazio_e_validade_nula(self):
        self.conn.execute("UPDATE wms_produtos SET controla_validade = 0 WHERE id = ?", (self.pid,))
        self.conn.commit()
        self._entrada("C1-E1-N1", 5, lote="", validade=None)
        s = self._sug(lote="", validade=None)
        self.assertEqual(s[0], {"posicao": "C1-E1-N1", "criterio": "MESMO_LOTE",
                                "motivo": "Já tem este produto aqui (5 UN)"})


if __name__ == "__main__":
    unittest.main()
