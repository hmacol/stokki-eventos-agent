# -*- coding: utf-8 -*-
"""
Testes do Relatorio de Recebimento em PDF.
Rodar (de dentro de painel_agentes/): py -3.11 -m unittest test_wms_chegada_pdf -v
"""
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import wms_chegada  # noqa: E402
import wms_chegada_pdf  # noqa: E402
from test_wms_chegada import _Base  # noqa: E402


def _jpeg(cor=(200, 10, 10), tam=(400, 300)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", tam, cor).save(buf, format="JPEG")
    return buf.getvalue()


def _paginas(pdf: bytes) -> int:
    return len(__import__("re").findall(rb"/Type\s*/Page\b(?!s)", pdf))


class TestPdf(unittest.TestCase):
    def setUp(self):
        self.jpeg = _jpeg()
        self.detalhe = {
            "id": 1, "estado": "CONCLUIDA", "motorista": "Jose Silva", "placa": "ABC1D23",
            "transportadora": "TransX", "volumes": "12", "lacre": "L99", "integra": 0,
            "observacao": "caixas amassadas " * 30, "chegada_em": "2026-11-22 08:15:00",
            "operador_nome": "Ana", "concluida_em": "2026-11-22 08:40:00",
            "liberacao_via": "ESCRITORIO", "liberacao_nome": "hugo", "liberacao_texto": "liberado pelo cliente " * 20,
            "liberacao_em": "2026-11-22 09:00:00",
            "recebimentos": [{"id": 1, "codigo": "REC-1", "embarcador": "MARIA"}],
            "fotos": [], "temperaturas": [
                {"classe": "REFRIGERADO", "medida_c": 5.0, "faixa_min": 0.0, "faixa_max": 7.0, "fora": 0},
                {"classe": "CONGELADO", "medida_c": -9.0, "faixa_min": -18.0, "faixa_max": -12.0, "fora": 1}]}
        self.detalhe_seco = dict(self.detalhe, temperaturas=[], integra=1, observacao="", transportadora="",
                                 lacre="", volumes="", liberacao_via="")

    def test_gera_pdf(self):
        pdf = wms_chegada_pdf.gerar(self.detalhe, [("CAMINHAO_ABERTO", self.jpeg), ("CARGA", self.jpeg)])
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_muitas_fotos_viram_mais_paginas(self):
        um = wms_chegada_pdf.gerar(self.detalhe, [("CARGA", self.jpeg)])
        muitas = wms_chegada_pdf.gerar(self.detalhe, [("CARGA", self.jpeg)] * 14)
        self.assertGreater(_paginas(muitas), _paginas(um))

    def test_sem_foto_e_sem_temperatura_nao_quebra(self):
        self.assertTrue(wms_chegada_pdf.gerar(self.detalhe_seco, []).startswith(b"%PDF"))

    def test_foto_corrompida_e_pulada(self):
        with self.assertLogs("wms_chegada_pdf", level="WARNING"):
            pdf = wms_chegada_pdf.gerar(self.detalhe, [("CARGA", b"nao e imagem"), ("CARGA", self.jpeg)])
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_liberacao_via_cliente(self):
        d = dict(self.detalhe, liberacao_via="CLIENTE", liberacao_nome="Maria", liberacao_texto="")
        self.assertTrue(wms_chegada_pdf.gerar(d, []).startswith(b"%PDF"))

    def textos(self, detalhe):
        with mock.patch.object(wms_chegada_pdf, "_texto", wraps=wms_chegada_pdf._texto) as m:
            self.assertTrue(wms_chegada_pdf.gerar(detalhe, []).startswith(b"%PDF"))
        return [c.args[3] for c in m.call_args_list]

    def test_recusa_do_cliente_aparece_mesmo_com_liberacao_por_fora(self):
        d = dict(self.detalhe, liberacao_texto="Diretor autorizou por e-mail", resposta_cliente="NAO_AUTORIZADO",
                 resposta_cliente_nome="Maria Souza", resposta_cliente_em="2026-11-22 08:50:00")
        t = self.textos(d)
        self.assertIn("Resposta do cliente pelo link: não autorizado por Maria Souza em 22/11/2026 08:50", t)
        self.assertIn("Registrado por hugo em 22/11/2026 09:00: Diretor autorizou por e-mail", t)

    def test_resposta_autorizada_do_cliente(self):
        d = dict(self.detalhe, liberacao_via="CLIENTE", liberacao_nome="Maria Souza", resposta_cliente="AUTORIZADO",
                 resposta_cliente_nome="Maria Souza", resposta_cliente_em="2026-11-22 08:50:00")
        t = self.textos(d)
        self.assertIn("Resposta do cliente pelo link: autorizado por Maria Souza em 22/11/2026 08:50", t)
        self.assertFalse([x for x in t if x.startswith("Registrado por")])

    def test_rascunho_sem_integra_mostra_traco(self):
        t = self.textos(dict(self.detalhe_seco, integra=None))
        self.assertNotIn("Com avaria", t)
        self.assertNotIn("Íntegra", t)
        self.assertIn("Com avaria", self.textos(dict(self.detalhe_seco, integra=0)))

    def test_texto_da_temperatura(self):
        self.assertEqual(wms_chegada_pdf._graus(-9.0), "-9,0 °C")
        self.assertEqual(wms_chegada_pdf._data("2026-11-22 08:15:00"), "22/11/2026 08:15")
        self.assertEqual(wms_chegada_pdf._data(""), "—")


class TestDoBanco(_Base):
    def test_do_banco_pula_foto_que_sumiu_do_disco(self):
        pasta = Path(self._tmp.name) / "fotos"
        c = wms_chegada.abrir(self.conn, uuid="u1", recebimento_ids=[self.rec], operador={"id": 7, "nome": "Ana"})
        for tipo in ("CAMINHAO_ABERTO", "CARGA", "CARGA"):
            wms_chegada.salvar_foto(self.conn, c["id"], tipo, _jpeg(), pasta=pasta)
        fotos = wms_chegada.detalhe(self.conn, c["id"])["fotos"]
        wms_chegada.caminho_foto(self.conn, c["id"], fotos[0]["id"], pasta).unlink()
        with self.assertLogs("wms_chegada_pdf", level="WARNING"):
            sem = wms_chegada_pdf.do_banco(self.conn, c["id"], pasta)
        self.assertTrue(sem.startswith(b"%PDF"))
        # nenhuma foto no disco: continua gerando
        for f in fotos[1:]:
            wms_chegada.caminho_foto(self.conn, c["id"], f["id"], pasta).unlink()
        with self.assertLogs("wms_chegada_pdf", level="WARNING"):
            self.assertTrue(wms_chegada_pdf.do_banco(self.conn, c["id"], pasta).startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
