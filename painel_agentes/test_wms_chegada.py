# -*- coding: utf-8 -*-
"""
Testes da etapa Receber (chegada do caminhao): faixas de temperatura.
Rodar (de dentro de painel_agentes/): py -3.11 -m unittest test_wms_chegada -v
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import wms  # noqa: E402
import wms_caixas  # noqa: E402
import wms_chegada  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "dados.db"
        wms_caixas.conectar(self.db).close()  # cria wms_produtos e recebimentos
        self.conn = wms_chegada.conectar(self.db)
        for pid, sku, temp in ((1, "R1", "REFRIGERADO"), (2, "C1", "CONGELADO"), (3, "S1", ""), (4, "A1", "AMBIENTE")):
            self.conn.execute(
                "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, unidade, "
                "temperatura, atualizado_em) VALUES (?, ?, ?, ?, 'MARIA', 48, 'UN', ?, '2026-09-21 10:00:00')",
                (pid, 900 + pid, sku, "PROD " + sku, temp))
        self.p_refri = 1
        self.rec = self._receb(1, [1, 2, 3])
        self.rec_seco = self._receb(2, [3, 4])
        self.conn.commit()

    def _receb(self, id_stokki, produtos):
        cur = self.conn.execute(
            "INSERT INTO wms_recebimentos (id_stokki, lido_em, atualizado_em) VALUES (?, '2026-11-22', '2026-11-22')",
            (id_stokki,))
        rid = cur.lastrowid
        for i, pid in enumerate(produtos, 1):
            self.conn.execute(
                "INSERT INTO wms_recebimento_itens (recebimento_id, linha, qtd_embalagem, produto_id) "
                "VALUES (?, ?, 1, ?)", (rid, i, pid))
        return rid

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestFaixas(_Base):
    def test_faixas_padrao_e_config(self):
        self.assertEqual(wms_chegada.faixas({}), {"REFRIGERADO": (0.0, 7.0), "CONGELADO": (-18.0, -12.0)})
        f = wms_chegada.faixas({"wms": {"temperatura_faixas": {"REFRIGERADO": [2, 8], "CONGELADO": "x"}}})
        self.assertEqual(f, {"REFRIGERADO": (2.0, 8.0), "CONGELADO": (-18.0, -12.0)})

    def test_classes_da_carga(self):
        self.assertEqual(wms_chegada.classes_da_carga(self.conn, [self.rec]), ["REFRIGERADO", "CONGELADO"])

    def test_carga_sem_temperatura_nao_tem_classe(self):
        self.assertEqual(wms_chegada.classes_da_carga(self.conn, [self.rec_seco]), [])
        self.assertEqual(wms_chegada.classes_da_carga(self.conn, []), [])

    def test_faixa_propria_do_produto_estreita(self):
        wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=2, temp_max=5)
        self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [self.rec], "REFRIGERADO", {}), (2.0, 5.0))
        # congelado sem faixa propria continua no padrao
        self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [self.rec], "CONGELADO", {}), (-18.0, -12.0))

    def test_faixa_do_produto_nao_alarga_a_da_classe(self):
        wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=-5, temp_max=20)
        self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [self.rec], "REFRIGERADO", {}), (0.0, 7.0))

    def test_limpar_faixa(self):
        wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=2, temp_max=5)
        p = wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=None, temp_max=None)
        self.assertIsNone(p["temp_min"])
        self.assertIsNone(p["temp_max"])

    def test_faixa_invertida_ou_pela_metade_recusa(self):
        with self.assertRaises(wms.ErroWMS):
            wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=6, temp_max=2)
        with self.assertRaises(wms.ErroWMS):
            wms.definir_temperatura_produto(self.conn, self.p_refri, "REFRIGERADO", temp_min=2, temp_max=None)


class TestChegada(_Base):
    OP = {"id": 7, "nome": "Ana"}

    def setUp(self):
        super().setUp()
        self.pasta = Path(self._tmp.name) / "fotos"

    def n(self, tabela):
        return self.conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]

    def _abrir(self, uuid="u1", ids=None):
        return wms_chegada.abrir(self.conn, uuid=uuid, recebimento_ids=ids or [self.rec], operador=self.OP)

    def _png(self, tam=(3000, 2000)):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", tam, (200, 10, 10)).save(buf, format="PNG")
        return buf.getvalue()

    def test_abrir_cria_rascunho(self):
        c = self._abrir()
        self.assertEqual(c["estado"], "RASCUNHO")
        self.assertEqual(c["uuid"], "u1")
        self.assertEqual(c["operador_id"], 7)
        self.assertEqual(c["operador_nome"], "Ana")
        self.assertRegex(c["chegada_em"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(c["recebimentos"][0]["id"], self.rec)

    def test_abrir_mesmo_uuid_devolve_a_mesma(self):
        a = self._abrir()
        b = self._abrir()
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.n("wms_chegadas"), 1)
        self.assertEqual(self.n("wms_chegada_recebimentos"), 1)

    def test_abrir_com_dois_recebimentos(self):
        c = self._abrir(ids=[self.rec, self.rec_seco])
        self.assertEqual(sorted(r["id"] for r in c["recebimentos"]), [self.rec, self.rec_seco])

    def test_abrir_recebimento_ja_em_outra_chegada_recusa(self):
        self._abrir("u1")
        with self.assertRaises(wms_chegada.ErroChegada) as cm:
            self._abrir("u2")
        self.assertEqual(str(cm.exception), "Esse recebimento já tem chegada registrada.")
        self.assertEqual(self.n("wms_chegadas"), 1)

    def test_abrir_de_novo_depois_de_nao_recebida(self):
        a = self._abrir("u1")
        self.conn.execute("UPDATE wms_chegadas SET estado = 'NAO_RECEBIDA' WHERE id = ?", (a["id"],))
        self.conn.commit()
        b = self._abrir("u2")
        self.assertNotEqual(a["id"], b["id"])

    def test_abrir_recebimento_encerrado_recusa(self):
        self.conn.execute("UPDATE wms_recebimentos SET estado = 'ENDERECADO' WHERE id = ?", (self.rec,))
        self.conn.commit()
        with self.assertRaises(wms_chegada.ErroChegada):
            self._abrir()
        with self.assertRaises(wms_chegada.ErroChegada):
            self._abrir("u2", ids=[9999])
        with self.assertRaises(wms_chegada.ErroChegada):
            self._abrir("u3", ids=[])
        self.assertEqual(self.n("wms_chegadas"), 0)

    def test_salvar_foto_regrava_jpeg(self):
        from PIL import Image
        c = self._abrir()
        f = wms_chegada.salvar_foto(self.conn, c["id"], "PLACA", self._png(), pasta=self.pasta)
        self.assertEqual(f["tipo"], "PLACA")
        self.assertRegex(f["arquivo"], rf"^CHG-{c['id']}-[0-9a-f]{{8}}\.jpg$")
        with Image.open(self.pasta / f["arquivo"]) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(max(img.size), 2200)
        self.assertEqual(self.n("wms_chegada_fotos"), 1)
        self.assertEqual(wms_chegada.caminho_foto(self.conn, c["id"], f["id"], pasta=self.pasta), self.pasta / f["arquivo"])
        self.assertEqual(len(wms_chegada.detalhe(self.conn, c["id"])["fotos"]), 1)

    def _recusa_foto(self, tipo, conteudo, msg=None):
        c = self._abrir()
        with self.assertRaises(wms_chegada.ErroChegada) as cm:
            wms_chegada.salvar_foto(self.conn, c["id"], tipo, conteudo, pasta=self.pasta)
        if msg:
            self.assertEqual(str(cm.exception), msg)
        self.assertEqual(self.n("wms_chegada_fotos"), 0)
        self.assertEqual(list(self.pasta.glob("*")), [])

    def test_foto_que_nao_e_imagem(self):
        self._recusa_foto("PLACA", b"isso nao e uma imagem",
                          "Não consegui ler a foto. Tire de novo pela câmera do aparelho.")

    def test_foto_grande_demais(self):
        self._recusa_foto("PLACA", b"0" * (wms_chegada.MAX_FOTO_BYTES + 1))

    def test_foto_tipo_invalido(self):
        self._recusa_foto("SELFIE", self._png((100, 100)))

    def test_remover_foto_so_em_rascunho(self):
        c = self._abrir()
        f = wms_chegada.salvar_foto(self.conn, c["id"], "CARGA", self._png((100, 100)), pasta=self.pasta)
        self.conn.execute("UPDATE wms_chegadas SET estado = 'CONCLUIDA' WHERE id = ?", (c["id"],))
        self.conn.commit()
        with self.assertRaises(wms_chegada.ErroChegada):
            wms_chegada.remover_foto(self.conn, c["id"], f["id"], pasta=self.pasta)
        self.assertEqual(self.n("wms_chegada_fotos"), 1)
        self.conn.execute("UPDATE wms_chegadas SET estado = 'RASCUNHO' WHERE id = ?", (c["id"],))
        self.conn.commit()
        wms_chegada.remover_foto(self.conn, c["id"], f["id"], pasta=self.pasta)
        self.assertEqual(self.n("wms_chegada_fotos"), 0)
        self.assertEqual(list(self.pasta.glob("*")), [])

    def test_caminho_foto_de_outra_chegada_recusa(self):
        a = self._abrir("u1")
        b = self._abrir("u2", ids=[self.rec_seco])
        f = wms_chegada.salvar_foto(self.conn, a["id"], "CARGA", self._png((100, 100)), pasta=self.pasta)
        with self.assertRaises(wms_chegada.ErroChegada):
            wms_chegada.caminho_foto(self.conn, b["id"], f["id"], pasta=self.pasta)


if __name__ == "__main__":
    unittest.main()
