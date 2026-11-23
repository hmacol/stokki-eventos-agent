# -*- coding: utf-8 -*-
"""
Testes da etapa Receber (chegada do caminhao): faixas de temperatura.
Rodar (de dentro de painel_agentes/): py -3.11 -m unittest test_wms_chegada -v
"""
import io
import sqlite3
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

    def test_faixa_propria_substitui_a_da_classe(self):
        # sorvete: faixa propria fora do padrao da classe
        wms.definir_temperatura_produto(self.conn, 2, "CONGELADO", temp_min=-25, temp_max=-20)
        rec = self._receb(10, [2])
        self.conn.commit()
        self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [rec], "CONGELADO", {}), (-25.0, -20.0))
        self.assertIs(wms_chegada.medida_dentro(self.conn, [rec], "CONGELADO", -22, {}), True)
        self.assertIs(wms_chegada.medida_dentro(self.conn, [rec], "CONGELADO", -15, {}), False)

    def test_faixas_disjuntas_na_mesma_carga(self):
        wms.definir_temperatura_produto(self.conn, 1, "REFRIGERADO", temp_min=0, temp_max=3)
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, unidade, "
            "temperatura, temp_min, temp_max, atualizado_em) VALUES (5, 905, 'R2', 'PROD R2', 'MARIA', 48, 'UN', "
            "'REFRIGERADO', 5, 7, '2026-09-21 10:00:00')")
        rec = self._receb(11, [1, 5])
        self.conn.commit()
        self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [rec], "REFRIGERADO", {}), (0.0, 7.0))
        for t in (2, 6, 4):
            self.assertIs(wms_chegada.medida_dentro(self.conn, [rec], "REFRIGERADO", t, {}), False)

    def test_medida_nos_limites_esta_dentro(self):
        for t, esperado in ((0.0, True), (7.0, True), (-0.1, False), (7.1, False)):
            self.assertIs(wms_chegada.medida_dentro(self.conn, [self.rec], "REFRIGERADO", t, {}), esperado)

    def test_faixa_de_outra_classe_nao_interfere(self):
        wms.definir_temperatura_produto(self.conn, 2, "CONGELADO", temp_min=-25, temp_max=-20)
        self.assertEqual(wms_chegada.faixa_da_carga(self.conn, [self.rec], "REFRIGERADO", {}), (0.0, 7.0))

    def test_temperatura_nao_fria_zera_a_faixa(self):
        p = wms.definir_temperatura_produto(self.conn, 1, "AMBIENTE", temp_min=2, temp_max=5)
        self.assertIsNone(p["temp_min"])
        self.assertIsNone(p["temp_max"])

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

    def test_abrir_mesmo_embarcador_por_stkkc_id(self):
        self.conn.execute("UPDATE wms_recebimentos SET stkkc_id = '48', embarcador = 'A' WHERE id = ?", (self.rec,))
        self.conn.execute("UPDATE wms_recebimentos SET stkkc_id = '48', embarcador = 'B' WHERE id = ?", (self.rec_seco,))
        self.conn.commit()
        self.assertEqual(len(self._abrir(ids=[self.rec, self.rec_seco])["recebimentos"]), 2)

    def test_abrir_embarcadores_diferentes_recusa_sem_gravar(self):
        self.conn.execute("UPDATE wms_recebimentos SET stkkc_id = '48' WHERE id = ?", (self.rec,))
        self.conn.execute("UPDATE wms_recebimentos SET stkkc_id = '22' WHERE id = ?", (self.rec_seco,))
        self.conn.commit()
        with self.assertRaises(wms_chegada.ErroChegada) as cm:
            self._abrir(ids=[self.rec, self.rec_seco])
        self.assertEqual(str(cm.exception), "Recebimentos de embarcadores diferentes: "
                                            "faça um Receber para cada embarcador.")
        self.assertEqual((self.n("wms_chegadas"), self.n("wms_chegada_recebimentos")), (0, 0))

    def test_abrir_sem_stkkc_id_compara_o_nome_do_embarcador(self):
        self.conn.execute("UPDATE wms_recebimentos SET embarcador = ' maria ' WHERE id = ?", (self.rec,))
        self.conn.execute("UPDATE wms_recebimentos SET embarcador = 'MARIA' WHERE id = ?", (self.rec_seco,))
        self.conn.commit()
        self.assertEqual(len(self._abrir(ids=[self.rec, self.rec_seco])["recebimentos"]), 2)
        self.conn.execute("UPDATE wms_recebimentos SET embarcador = 'OUTRO' WHERE id = ?", (self.rec_seco,))
        self.conn.commit()
        with self.assertRaises(wms_chegada.ErroChegada):
            self._abrir("u2", ids=[self.rec, self.rec_seco])

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

    def test_abrir_e_atomico_com_um_recebimento_invalido(self):
        with self.assertRaises(wms_chegada.ErroChegada):
            self._abrir(ids=[self.rec, 9999])
        self.assertEqual(self.n("wms_chegadas"), 0)
        self.assertEqual(self.n("wms_chegada_recebimentos"), 0)

    def test_foto_save_falhando_nao_deixa_rastro(self):
        from unittest import mock
        c = self._abrir()
        png = self._png((100, 100))
        with mock.patch("PIL.Image.Image.save", side_effect=OSError("disco cheio")):
            with self.assertRaises(wms_chegada.ErroChegada) as cm:
                wms_chegada.salvar_foto(self.conn, c["id"], "PLACA", png, pasta=self.pasta)
        self.assertEqual(str(cm.exception), "Não consegui gravar a foto.")
        self.assertEqual(self.n("wms_chegada_fotos"), 0)
        self.assertEqual(list(self.pasta.glob("*")), [])

    def test_foto_insert_falhando_apaga_o_arquivo(self):
        c = self._abrir()
        self.conn.execute("DROP TABLE wms_chegada_fotos")
        with self.assertRaises(sqlite3.OperationalError):
            wms_chegada.salvar_foto(self.conn, c["id"], "PLACA", self._png((100, 100)), pasta=self.pasta)
        self.assertEqual(list(self.pasta.glob("*")), [])

    def test_foto_que_nao_e_imagem(self):
        self._recusa_foto("PLACA", b"isso nao e uma imagem",
                          "Não consegui ler a foto. Tire de novo pela câmera do aparelho.")

    def test_foto_grande_demais(self):
        self._recusa_foto("PLACA", b"0" * (wms_chegada.MAX_FOTO_BYTES + 1))

    def test_foto_tipo_invalido(self):
        self._recusa_foto("SELFIE", self._png((100, 100)))

    def test_foto_so_em_rascunho(self):
        for estado, rec in (("LIBERADA", self.rec), ("AGUARDANDO_ESCRITORIO", self.rec_seco)):
            c = self._abrir(uuid="u-" + estado, ids=[rec])
            self.conn.execute("UPDATE wms_chegadas SET estado = ? WHERE id = ?", (estado, c["id"]))
            self.conn.commit()
            with self.assertRaises(wms_chegada.ErroChegada) as cm:
                wms_chegada.salvar_foto(self.conn, c["id"], "CARGA", self._png((100, 100)), pasta=self.pasta)
            self.assertEqual(str(cm.exception), "Esse checklist já foi concluído: não dá para mudar as fotos.")
        self.assertEqual(self.n("wms_chegada_fotos"), 0)
        self.assertEqual(list(self.pasta.glob("*")), [])

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

    def test_descartar_rascunho_apaga_tudo(self):
        outra = self._abrir("u2", ids=[self.rec_seco])
        f_outra = wms_chegada.salvar_foto(self.conn, outra["id"], "CARGA", self._png((100, 100)), pasta=self.pasta)
        c = self._abrir("u1")
        for tipo in ("CAMINHAO_ABERTO", "CARGA"):
            wms_chegada.salvar_foto(self.conn, c["id"], tipo, self._png((100, 100)), pasta=self.pasta)
        self.conn.execute("INSERT INTO wms_chegada_temperaturas (chegada_id, classe, medida_c, faixa_min, faixa_max) "
                          "VALUES (?, 'REFRIGERADO', 5, 0, 7)", (c["id"],))
        self.conn.commit()
        self.assertIsNone(wms_chegada.descartar(self.conn, c["id"], self.OP, pasta=self.pasta))
        with self.assertRaises(wms_chegada.ErroChegada):
            wms_chegada.detalhe(self.conn, c["id"])
        for tabela, n in (("wms_chegadas", 1), ("wms_chegada_fotos", 1), ("wms_chegada_recebimentos", 1),
                          ("wms_chegada_temperaturas", 0)):
            self.assertEqual(self.n(tabela), n, tabela)
        self.assertEqual([p.name for p in self.pasta.glob("*")], [f_outra["arquivo"]])   # sem orfaos
        self.assertEqual(wms_chegada.estado_do_recebimento(self.conn, self.rec), "SEM_CHEGADA")
        self.assertEqual(self._abrir("u3")["estado"], "RASCUNHO")   # o recebimento volta para o Receber
        self.assertFalse(self.conn.in_transaction)

    def test_descartar_so_rascunho(self):
        for estado, rec in (("LIBERADA", self.rec), ("AGUARDANDO_ESCRITORIO", self.rec_seco)):
            c = self._abrir(uuid="u-" + estado, ids=[rec])
            wms_chegada.salvar_foto(self.conn, c["id"], "CARGA", self._png((100, 100)), pasta=self.pasta)
            self.conn.execute("UPDATE wms_chegadas SET estado = ? WHERE id = ?", (estado, c["id"]))
            self.conn.commit()
            with self.assertRaises(wms_chegada.ErroChegada) as cm:
                wms_chegada.descartar(self.conn, c["id"], self.OP, pasta=self.pasta)
            self.assertEqual(str(cm.exception), "Esse checklist já foi concluído: não dá para descartar.")
        self.assertEqual((self.n("wms_chegadas"), self.n("wms_chegada_fotos")), (2, 2))
        self.assertEqual(len(list(self.pasta.glob("*"))), 2)
        with self.assertRaises(wms_chegada.ErroChegada):
            wms_chegada.descartar(self.conn, 9999, self.OP, pasta=self.pasta)




class TestConcluir(_Base):
    OP = {"id": 7, "nome": "Ana"}

    def setUp(self):
        super().setUp()
        self.pasta = Path(self._tmp.name) / "fotos"

    def _png(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (200, 10, 10)).save(buf, format="PNG")
        return buf.getvalue()

    def _chegada(self, ids=None, fotos=("CAMINHAO_ABERTO", "CARGA", "TERMOMETRO"), uuid="u1"):
        c = wms_chegada.abrir(self.conn, uuid=uuid, recebimento_ids=ids or [self.rec], operador=self.OP)
        for tipo in fotos:
            wms_chegada.salvar_foto(self.conn, c["id"], tipo, self._png(), pasta=self.pasta)
        return c["id"]

    def _dados(self, **mudancas):
        d = {"motorista": "Jose Silva", "placa": "abc-1d23", "transportadora": "TransX", "volumes": "12",
             "lacre": "L99", "integra": True, "observacao": "",
             "temperaturas": {"REFRIGERADO": "5", "CONGELADO": "-15"}}
        d.update(mudancas)
        return d

    def _concluir(self, cid, dados=None):
        return wms_chegada.concluir(self.conn, cid, dados if dados is not None else self._dados(), {}, self.OP)

    def _recusa(self, cid, dados, msg):
        with self.assertRaises(wms_chegada.ErroChegada) as cm:
            self._concluir(cid, dados)
        self.assertEqual(str(cm.exception), msg)
        # nada gravado
        c = wms_chegada.detalhe(self.conn, cid)
        self.assertEqual(c["estado"], "RASCUNHO")
        self.assertEqual(c["motorista"], "")
        self.assertEqual(c["temperaturas"], [])

    def test_motorista_curto(self):
        self._recusa(self._chegada(), self._dados(motorista=" Jo "), "Informe o nome do motorista.")

    def test_placa_invalida(self):
        cid = self._chegada()
        for placa in ("ABC123", "ABC-12345", "", "AB#1234"):
            self._recusa(cid, self._dados(placa=placa), "Placa inválida.")

    def test_falta_foto_caminhao(self):
        self._recusa(self._chegada(fotos=("CARGA", "TERMOMETRO")), self._dados(), "Falta a foto do caminhão aberto.")

    def test_falta_foto_carga(self):
        self._recusa(self._chegada(fotos=("CAMINHAO_ABERTO", "TERMOMETRO")), self._dados(),
                     "Falta pelo menos uma foto da carga.")

    def test_integra_indefinida(self):
        cid = self._chegada()
        self._recusa(cid, self._dados(integra=None), "Diga se a carga chegou inteira.")
        self._recusa(cid, self._dados(integra="sim"), "Diga se a carga chegou inteira.")

    def test_avaria_sem_foto(self):
        self._recusa(self._chegada(), self._dados(integra=False, observacao="caixas amassadas"),
                     "Avaria precisa de foto e de observação.")

    def test_avaria_sem_observacao(self):
        cid = self._chegada(ids=[self.rec_seco], fotos=("CAMINHAO_ABERTO", "CARGA", "AVARIA"))
        self._recusa(cid, self._dados(integra=False, observacao="ruim", temperaturas={}),
                     "Avaria precisa de foto e de observação.")

    def test_avaria_completa_conclui(self):
        cid = self._chegada(ids=[self.rec_seco], fotos=("CAMINHAO_ABERTO", "CARGA", "AVARIA"))
        r = self._concluir(cid, self._dados(integra=False, observacao="caixas amassadas", temperaturas={}))
        self.assertIs(r["avaria"], True)
        self.assertEqual(r["chegada"]["integra"], 0)
        self.assertEqual(r["chegada"]["estado"], "LIBERADA")

    def test_temperatura_faltando_por_classe(self):
        cid = self._chegada()
        self._recusa(cid, self._dados(temperaturas={"CONGELADO": "-15"}), "Informe a temperatura do refrigerado.")
        self._recusa(cid, self._dados(temperaturas={"REFRIGERADO": "5"}), "Informe a temperatura do congelado.")

    def test_temperatura_vazia_ou_texto(self):
        cid = self._chegada()
        for ruim in ("", "  ", "abc", None, "nan", "inf", "5 graus"):
            self._recusa(cid, self._dados(temperaturas={"REFRIGERADO": ruim, "CONGELADO": "-15"}),
                         "Informe a temperatura do refrigerado.")

    def test_falta_foto_termometro(self):
        self._recusa(self._chegada(fotos=("CAMINHAO_ABERTO", "CARGA")), self._dados(), "Falta a foto do termômetro.")

    def test_concluir_na_faixa_libera(self):
        cid = self._chegada()
        r = self._concluir(cid)
        c = r["chegada"]
        self.assertEqual(c["estado"], "LIBERADA")
        self.assertIs(r["fora_da_faixa"], False)
        self.assertIs(r["avaria"], False)
        self.assertEqual((c["motorista"], c["placa"], c["transportadora"], c["volumes"], c["lacre"]),
                         ("Jose Silva", "ABC1D23", "TransX", "12", "L99"))
        self.assertEqual((c["operador_id"], c["operador_nome"]), (7, "Ana"))
        self.assertTrue(c["concluida_em"])
        self.assertEqual([(t["classe"], t["medida_c"], t["faixa_min"], t["faixa_max"], t["fora"])
                          for t in c["temperaturas"]],
                         [("CONGELADO", -15.0, -18.0, -12.0, 0), ("REFRIGERADO", 5.0, 0.0, 7.0, 0)])

    def test_limite_da_faixa_esta_dentro(self):
        for i, (r, c) in enumerate((("7,0", "-12"), ("0,0", "-18"))):
            rec = self._receb(20 + i, [1, 2])
            self.conn.commit()
            cid = self._chegada(uuid=f"l{i}", ids=[rec])
            res = self._concluir(cid, self._dados(temperaturas={"REFRIGERADO": r, "CONGELADO": c}))
            self.assertEqual(res["chegada"]["estado"], "LIBERADA", (r, c))

    def test_fora_da_faixa_aguarda_escritorio(self):
        cid = self._chegada()
        r = self._concluir(cid, self._dados(temperaturas={"REFRIGERADO": "5", "CONGELADO": "-9,0"}))
        self.assertEqual(r["chegada"]["estado"], "AGUARDANDO_ESCRITORIO")
        self.assertIs(r["fora_da_faixa"], True)
        fora = {t["classe"]: t["fora"] for t in r["chegada"]["temperaturas"]}
        self.assertEqual(fora, {"CONGELADO": 1, "REFRIGERADO": 0})

    def test_faixas_disjuntas_vao_para_o_escritorio(self):
        wms.definir_temperatura_produto(self.conn, 1, "REFRIGERADO", temp_min=0, temp_max=3)
        self.conn.execute(
            "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, unidade, "
            "temperatura, temp_min, temp_max, atualizado_em) VALUES (5, 905, 'R2', 'PROD R2', 'MARIA', 48, 'UN', "
            "'REFRIGERADO', 5, 7, '2026-09-21 10:00:00')")
        rec = self._receb(11, [1, 5])
        self.conn.commit()
        cid = self._chegada(ids=[rec])
        r = self._concluir(cid, self._dados(temperaturas={"REFRIGERADO": "2"}))
        self.assertEqual(r["chegada"]["estado"], "AGUARDANDO_ESCRITORIO")
        (t,) = r["chegada"]["temperaturas"]
        self.assertEqual(t["fora"], 1)
        self.assertEqual((t["faixa_min"], t["faixa_max"]), (0.0, 7.0))

    def test_temperatura_com_virgula_e_negativa(self):
        cid = self._chegada()
        c = self._concluir(cid, self._dados(temperaturas={"REFRIGERADO": "6,5", "CONGELADO": "-18"}))["chegada"]
        self.assertEqual({t["classe"]: t["medida_c"] for t in c["temperaturas"]},
                         {"REFRIGERADO": 6.5, "CONGELADO": -18.0})

    def test_carga_seca_nao_pede_temperatura(self):
        cid = self._chegada(ids=[self.rec_seco], fotos=("CAMINHAO_ABERTO", "CARGA"))
        r = self._concluir(cid, self._dados(temperaturas={}))
        self.assertEqual(r["chegada"]["estado"], "LIBERADA")
        self.assertEqual(r["chegada"]["temperaturas"], [])

    def test_volumes_e_texto_livre(self):
        for i, (digitado, esperado) in enumerate((("4 pallets", "4 pallets"), ("12", "12"), (None, ""),
                                                  ("x" * 80, "x" * 60))):
            rec = self._receb(40 + i, [3])
            self.conn.commit()
            cid = self._chegada(ids=[rec], uuid="v%d" % i, fotos=("CAMINHAO_ABERTO", "CARGA"))
            c = self._concluir(cid, self._dados(volumes=digitado, temperaturas={}))["chegada"]
            self.assertEqual(c["volumes"], esperado)

    def test_carga_mista_pede_as_duas(self):
        cid = self._chegada()
        self.assertEqual(len(self._concluir(cid)["chegada"]["temperaturas"]), 2)

    def test_concluir_duas_vezes_nao_muda(self):
        cid = self._chegada()
        primeira = self._concluir(cid)
        segunda = self._concluir(cid, self._dados(motorista="Outro Nome",
                                                  temperaturas={"REFRIGERADO": "30", "CONGELADO": "0"}))
        self.assertEqual(primeira, segunda)

    def test_pode_conferir_so_liberada(self):
        self.assertEqual(wms_chegada.estado_do_recebimento(self.conn, self.rec), "SEM_CHEGADA")
        self.assertIs(wms_chegada.pode_conferir(self.conn, self.rec), False)
        cid = self._chegada()
        self.assertEqual(wms_chegada.estado_do_recebimento(self.conn, self.rec), "RASCUNHO")
        self.assertIs(wms_chegada.pode_conferir(self.conn, self.rec), False)
        self._concluir(cid, self._dados(temperaturas={"REFRIGERADO": "5", "CONGELADO": "-9"}))
        self.assertEqual(wms_chegada.estado_do_recebimento(self.conn, self.rec), "AGUARDANDO_ESCRITORIO")
        self.assertIs(wms_chegada.pode_conferir(self.conn, self.rec), False)
        self.conn.execute("UPDATE wms_chegadas SET estado = 'LIBERADA' WHERE id = ?", (cid,))
        self.assertIs(wms_chegada.pode_conferir(self.conn, self.rec), True)

    def test_chegada_nao_recebida_nao_conta(self):
        cid = self._chegada()
        self.conn.execute("UPDATE wms_chegadas SET estado = 'NAO_RECEBIDA' WHERE id = ?", (cid,))
        self.assertEqual(wms_chegada.estado_do_recebimento(self.conn, self.rec), "SEM_CHEGADA")

    # --- decisoes sobre carga fora da faixa ---
    def _aguardando(self, uuid="u1"):
        cid = self._chegada(uuid=uuid)
        self._concluir(cid, self._dados(temperaturas={"REFRIGERADO": "5", "CONGELADO": "-9"}))
        return cid

    def _erro(self, msg, fn, *args):
        with self.assertRaises(wms_chegada.ErroChegada) as cm:
            fn(self.conn, *args)
        self.assertEqual(str(cm.exception), msg)

    def test_cliente_autoriza_libera(self):
        cid = self._aguardando()
        c = wms_chegada.responder_cliente(self.conn, cid, "Maria Souza", True)
        self.assertEqual(c["estado"], "LIBERADA")
        self.assertEqual((c["liberacao_via"], c["liberacao_nome"]), ("CLIENTE", "Maria Souza"))
        self.assertEqual((c["resposta_cliente"], c["resposta_cliente_nome"]), ("AUTORIZADO", "Maria Souza"))
        self.assertTrue(c["liberacao_em"])
        self.assertTrue(c["resposta_cliente_em"])
        self.assertIs(wms_chegada.pode_conferir(self.conn, self.rec), True)

    def test_cliente_nao_autoriza_continua_aguardando(self):
        cid = self._aguardando()
        c = wms_chegada.responder_cliente(self.conn, cid, "Maria Souza", False)
        self.assertEqual(c["estado"], "AGUARDANDO_ESCRITORIO")
        self.assertEqual((c["resposta_cliente"], c["resposta_cliente_nome"]), ("NAO_AUTORIZADO", "Maria Souza"))
        self.assertTrue(c["resposta_cliente_em"])
        self.assertEqual(c["liberacao_via"], "")

    def test_cliente_nome_curto(self):
        cid = self._aguardando()
        self._erro("Informe o seu nome.", wms_chegada.responder_cliente, cid, " Jo ", True)
        self.assertEqual(wms_chegada.detalhe(self.conn, cid)["estado"], "AGUARDANDO_ESCRITORIO")

    def test_cliente_responde_duas_vezes(self):
        cid = self._aguardando()
        wms_chegada.responder_cliente(self.conn, cid, "Maria Souza", False)
        self._erro("Esse pedido de liberação já foi respondido.", wms_chegada.responder_cliente, cid, "Outro Nome", True)
        c = wms_chegada.detalhe(self.conn, cid)
        self.assertEqual((c["estado"], c["resposta_cliente"], c["resposta_cliente_nome"]),
                         ("AGUARDANDO_ESCRITORIO", "NAO_AUTORIZADO", "Maria Souza"))

    def test_cliente_responde_depois_do_escritorio_decidir(self):
        cid = self._aguardando()
        wms_chegada.liberar_por_fora(self.conn, cid, "Joao autorizou por telefone", "Ana")
        self._erro("Essa carga não está mais aguardando decisão.", wms_chegada.responder_cliente, cid, "Maria Souza", False)
        c = wms_chegada.detalhe(self.conn, cid)
        self.assertEqual((c["estado"], c["liberacao_via"], c["resposta_cliente"]), ("LIBERADA", "ESCRITORIO", ""))

    def test_liberar_por_fora_exige_texto(self):
        cid = self._aguardando()
        self._erro("Descreva quem autorizou e por onde.", wms_chegada.liberar_por_fora, cid, "  curto  ", "Ana")
        self.assertEqual(wms_chegada.detalhe(self.conn, cid)["estado"], "AGUARDANDO_ESCRITORIO")
        c = wms_chegada.liberar_por_fora(self.conn, cid, "Joao autorizou por telefone", "Ana")
        self.assertEqual(c["estado"], "LIBERADA")
        self.assertEqual((c["liberacao_via"], c["liberacao_nome"], c["liberacao_texto"]),
                         ("ESCRITORIO", "Ana", "Joao autorizou por telefone"))
        self.assertTrue(c["liberacao_em"])

    def test_liberar_por_fora_depois_de_nao_autorizado_e_permitido(self):
        cid = self._aguardando()
        wms_chegada.responder_cliente(self.conn, cid, "Maria Souza", False)
        c = wms_chegada.liberar_por_fora(self.conn, cid, "Diretor autorizou por e-mail", "Ana")
        self.assertEqual(c["estado"], "LIBERADA")
        self.assertEqual(c["resposta_cliente"], "NAO_AUTORIZADO")

    def test_nao_receber_exige_motivo(self):
        cid = self._aguardando()
        self._erro("Informe o motivo.", wms_chegada.nao_receber, cid, " abc ", "Ana")
        self.assertEqual(wms_chegada.detalhe(self.conn, cid)["estado"], "AGUARDANDO_ESCRITORIO")

    def test_nao_receber_devolve_recebimento_para_a_lista(self):
        cid = self._aguardando()
        c = wms_chegada.nao_receber(self.conn, cid, "Carga quente demais", "Ana")
        self.assertEqual(c["estado"], "NAO_RECEBIDA")
        self.assertEqual((c["nao_recebida_motivo"], c["nao_recebida_por"]), ("Carga quente demais", "Ana"))
        self.assertEqual(wms_chegada.estado_do_recebimento(self.conn, self.rec), "SEM_CHEGADA")

    def test_decisao_em_chegada_liberada_recusa(self):
        cid = self._chegada()
        self._concluir(cid)
        msg = "Essa carga não está mais aguardando decisão."
        self._erro(msg, wms_chegada.responder_cliente, cid, "Maria Souza", True)
        self._erro(msg, wms_chegada.liberar_por_fora, cid, "Joao autorizou por telefone", "Ana")
        self._erro(msg, wms_chegada.nao_receber, cid, "Carga quente demais", "Ana")
        self.assertEqual(wms_chegada.detalhe(self.conn, cid)["estado"], "LIBERADA")


if __name__ == "__main__":
    unittest.main()
