# -*- coding: utf-8 -*-
"""Testes do recibo repetido nas despesas (nucleo/duplicidade_recibo.py) e da
origem da foto (CAMERA/GALERIA) em registrar_pedagio. SQLite temporario.
Rodar da raiz: py -3.11 -m unittest nucleo.test_duplicidade_recibo -v
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from PIL import Image, ImageDraw  # noqa: E402

from nucleo import banco, duplicidade_recibo, operacao  # noqa: E402


def _recibo(texto: str, ruido: int = 0) -> bytes:
    img = Image.new("RGB", (400, 600), "white")
    d = ImageDraw.Draw(img)
    for i, linha in enumerate(texto.split("|")):
        d.rectangle([20, 40 + i * 60, 40 + 60 * (i + 1) + len(linha) * 12, 70 + i * 60], fill="black")
    if ruido:
        d.point([(5, 5), (6, 6), (7, 7)], fill="gray")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70 + ruido)
    return buf.getvalue()


class TestDuplicidade(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch.start()
        self.conn = banco.conectar()
        self.rota = self.conn.execute(
            "INSERT INTO nucleo_rotas (data_rota, nome, provedor, agent_id, motorista_nome, status) "
            "VALUES ('2026-11-24', 'R1', 'APP', 7, 'JOAO', 'EM_ROTA')").lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self._tmp.cleanup()

    def _lancar(self, uuid, conteudo, valor=10.0, origem=None, status="PENDENTE"):
        import hashlib
        pid = self.conn.execute(
            "INSERT INTO nucleo_pedagios (uuid, rota_id, agent_id, valor_informado, sha256, hash_visual, status, origem) "
            "VALUES (?, ?, 7, ?, ?, ?, ?, ?)",
            (uuid, self.rota, valor, hashlib.sha256(conteudo).hexdigest(), duplicidade_recibo.hash_visual(conteudo),
             status, origem or "CAMERA")).lastrowid
        self.conn.commit()
        return pid

    def _itens(self):
        itens = [dict(r) for r in self.conn.execute("SELECT * FROM nucleo_pedagios ORDER BY id")]
        duplicidade_recibo.marcar_duplicados(self.conn, itens)
        return {i["uuid"]: i for i in itens}

    def test_hash_visual_estavel_e_distingue(self):
        a = _recibo("a|bb|ccc")
        self.assertEqual(len(duplicidade_recibo.hash_visual(a)), 16)
        self.assertLessEqual(duplicidade_recibo.distancia(duplicidade_recibo.hash_visual(a),
                                                          duplicidade_recibo.hash_visual(_recibo("a|bb|ccc", ruido=15))), 6)
        self.assertGreater(duplicidade_recibo.distancia(duplicidade_recibo.hash_visual(a),
                                                        duplicidade_recibo.hash_visual(_recibo("aaaaaaaa"))), 6)
        self.assertIsNone(duplicidade_recibo.hash_visual(b"nao e imagem"))

    def test_identico_e_parecido_com_mesmo_valor(self):
        base = _recibo("a|bb|ccc")
        self._lancar("u1", base)
        self._lancar("u2", base)                                   # mesmo arquivo
        self._lancar("u3", _recibo("a|bb|ccc", ruido=15))          # parecido, mesmo valor
        self._lancar("u4", _recibo("a|bb|ccc", ruido=15), valor=99)  # parecido, valor diferente
        it = self._itens()
        self.assertEqual({d["motivo"] for d in it["u2"]["duplicados"]}, {"IDENTICO", "PARECIDO"})
        self.assertIn("PARECIDO", {d["motivo"] for d in it["u3"]["duplicados"]})
        self.assertEqual([d["id"] for d in it["u4"]["duplicados"] if d["motivo"] == "PARECIDO"], [])

    def test_cancelado_nao_conta(self):
        base = _recibo("a|bb|ccc")
        self._lancar("u1", base, status="CANCELADO")
        self._lancar("u2", base)
        self.assertEqual(self._itens()["u2"]["duplicados"], [])

    def test_origem_so_aceita_galeria_senao_camera(self):
        for uuid, origem, esperado in (("o1", "galeria", "GALERIA"), ("o2", None, "CAMERA"), ("o3", "xpto", "CAMERA")):
            operacao.registrar_pedagio(self.conn, self.rota, 7, uuid, 5.0, None, "sha" + uuid, 1, "2026-11-24 10:00:00",
                                       origem=origem)
            self.assertEqual(self.conn.execute("SELECT origem FROM nucleo_pedagios WHERE uuid = ?", (uuid,)).fetchone()[0], esperado)


if __name__ == "__main__":
    unittest.main()
