# -*- coding: utf-8 -*-
"""py -3.11 -m unittest nucleo.test_canhotos_manuais"""
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from nucleo import banco
from nucleo import canhotos_manuais as cm


def _jpeg(orientacao=None, tamanho=(40, 20)):
    img = Image.new("RGB", tamanho, "white")
    buf = io.BytesIO()
    if orientacao:
        exif = Image.Exif()
        exif[0x0112] = orientacao
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


class ParaPdf(unittest.TestCase):
    def test_jpeg_vira_pdf(self):
        self.assertTrue(cm.para_pdf(_jpeg()).startswith(b"%PDF-"))

    def test_pdf_passa_igual(self):
        pdf = b"%PDF-1.4\n%fake\n"
        self.assertEqual(cm.para_pdf(pdf), pdf)

    def test_recusa_grande_heic_e_lixo(self):
        with self.assertRaises(cm.CanhotoInvalido):
            cm.para_pdf(b"%PDF-" + b"0" * (cm.LIMITE_BYTES + 1))
        with self.assertRaises(cm.CanhotoInvalido):
            cm.para_pdf(b"\x00\x00\x00\x18ftypheic" + b"0" * 50)
        with self.assertRaises(cm.CanhotoInvalido):
            cm.para_pdf(b"nao sou imagem")

    def test_imagem_gigante_e_recusada(self):
        with mock.patch.object(cm, "MAX_PIXELS", 100):
            with self.assertRaises(cm.CanhotoInvalido):
                cm.para_pdf(_jpeg(tamanho=(40, 20)))          # 800 px > 100

    def test_foto_grande_e_reduzida(self):
        with mock.patch.object(cm, "LADO_MAXIMO", 20):
            pdf = cm.para_pdf(_jpeg(tamanho=(80, 40)))
        mb = pdf.split(b"/MediaBox")[1].split(b"]")[0]
        nums = [float(x) for x in mb.replace(b"[", b" ").split()]
        self.assertLessEqual(max(nums[2], nums[3]), 20 * 72 / 150 + 1)

    def test_orientacao_exif_aplicada(self):
        pdf = cm.para_pdf(_jpeg(orientacao=6, tamanho=(40, 20)))   # 6 = girar 90: vira retrato
        self.assertIn(b"/MediaBox", pdf)
        mb = pdf.split(b"/MediaBox")[1].split(b"]")[0]
        nums = [float(x) for x in mb.replace(b"[", b" ").split()]
        self.assertGreater(nums[3], nums[2])                       # altura > largura


class Salvar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "dados.db"
        p = mock.patch.object(banco, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)

    def test_grava_busca_e_substitui(self):
        conn = banco.conectar(self.db)
        try:
            r = cm.salvar(conn, "#ps-1-r1", 10, b"%PDF-1 a", "hugo")
            self.assertEqual(r["codigo"], "PS-1-R1")
            cm.salvar(conn, "PS-1-R1", 10, b"%PDF-1 b", "hugo")
        finally:
            conn.close()
        caminho = cm.caminho_canhoto_manual("#PS-1-R1", db_path=self.db)
        self.assertEqual(caminho.read_bytes(), b"%PDF-1 b")
        self.assertEqual(caminho.parent, self.db.parent / "canhotos_manuais")
        self.assertIsNone(cm.caminho_canhoto_manual("PS-2", db_path=self.db))

    def test_codigo_invalido_e_recusado(self):
        conn = banco.conectar(self.db)
        try:
            for ruim in ("../../X", "/var/www/x", "", "PS-1/../2"):
                with self.assertRaises(cm.CanhotoInvalido, msg=ruim):
                    cm.salvar(conn, ruim, 1, b"%PDF-1", "hugo")
        finally:
            conn.close()
        self.assertEqual(list((self.db.parent / "canhotos_manuais").glob("*")) if (self.db.parent / "canhotos_manuais").exists() else [], [])

    def test_servico_com_dois_pedidos_grava_os_dois(self):
        conn = banco.conectar(self.db)
        try:
            cm.salvar(conn, "PS-37189, PS-37176", 1, b"%PDF-1", "hugo")
        finally:
            conn.close()
        self.assertIsNotNone(cm.caminho_canhoto_manual("PS-37189", db_path=self.db))
        self.assertIsNotNone(cm.caminho_canhoto_manual("#PS-37176", db_path=self.db))

    def test_gcs_falha_nao_impede(self):
        conn = banco.conectar(self.db)
        try:
            with mock.patch("documentos_pedido.storage_gcs.enviar_documento", side_effect=RuntimeError("403")):
                r = cm.salvar(conn, "PS-3", 1, b"%PDF-1", "hugo", config={"gcs": {"bucket_name": "x"}})
        finally:
            conn.close()
        self.assertIsNone(r["caminho_gcs"])
        self.assertIsNotNone(cm.caminho_canhoto_manual("PS-3", db_path=self.db))


if __name__ == "__main__":
    unittest.main()
