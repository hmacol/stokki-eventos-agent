"""Etiqueta de volume 10x15 e a rota de PDF da fila para o tipo VOLUME."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import acesso_telas  # noqa: E402
import wms  # noqa: E402
import wms_etiqueta_volume as vol  # noqa: E402
import wms_fila_impressao as fila  # noqa: E402
import wms_separacao as sep  # noqa: E402
from test_fila_etiquetas_tela import painel_agentes  # noqa: E402


class TestEtiquetaVolume(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        for alvo, valor in ((wms, Path(self._tmp.name) / "dados.db"), (acesso_telas, Path(self._tmp.name) / "a.db")):
            p = mock.patch.object(alvo, "DB_PATH", valor)
            p.start()
            self.addCleanup(p.stop)
        self.conn = sep.conectar()
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO wms_pedidos (id_stokki, codigo_ps, embarcador, situacao, lido_em, atualizado_em, "
            "destinatario, endereco_destino, cidade, uf) VALUES (44009, 'PS-44009', 'MOMBAK', 'Open', 'x', 'x', "
            "'Mercado Bom', 'Rua A, 10', 'Barueri', 'SP')")
        self.conn.commit()

    def test_qr_do_volume(self):
        self.assertEqual(sep.conteudo_qr_volume("#ps-44009", 2, 5), "FV|PS-44009|2|5")

    def test_arte_tem_o_tamanho_da_midia_em_pe(self):
        img = vol.desenhar_etiqueta_volume(sep.dados_do_volume(self.conn, "FV|PS-44009|2|5"))
        self.assertEqual(img.size, (1181, 1772))
        # pedido desconhecido ou campos vazios nao impedem a etiqueta
        vol.desenhar_etiqueta_volume(sep.dados_do_volume(self.conn, "FV|PS-1|1|1"))

    def test_numero_da_nf_vai_pra_etiqueta(self):
        self.assertEqual(sep.dados_do_volume(self.conn, "FV|PS-44009|1|1")["nf"], "")
        self.conn.execute("UPDATE wms_pedidos SET nf = '12345' WHERE codigo_ps = 'PS-44009'")
        self.conn.commit()
        dados = sep.dados_do_volume(self.conn, "FV|PS-44009|1|1")
        self.assertEqual(dados["nf"], "12345")
        com = vol.desenhar_etiqueta_volume(dados)
        sem = vol.desenhar_etiqueta_volume({**dados, "nf": ""})
        self.assertNotEqual(com.tobytes(), sem.tobytes())

    def test_pdf_tem_uma_pagina_por_volume(self):
        pdf = vol.gerar_pdf_volumes(self.conn, ["FV|PS-44009|1|3", "FV|PS-44009|2|3", "FV|PS-44009|3|3"])
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages"), 3)
        for ruim in ([], ["FL|x|y|z"]):
            with self.assertRaises(wms.ErroWMS):
                vol.gerar_pdf_volumes(self.conn, ruim)

    def test_rota_da_fila_serve_o_pdf_do_trabalho_de_volume(self):
        job = fila.enfileirar(self.conn, tipo="VOLUME", tamanho="10x15", itens=["FV|PS-44009|1|1"],
                              titulo="PS-44009 · 1 volume(s)", pedido_por="Bia")
        self.conn.commit()
        cfg = {"painel_agentes": {"usuario": "u", "senha": "s"}, "wms": {}}
        with mock.patch.object(painel_agentes, "_carregar_config", return_value=cfg):
            painel_agentes.app.config["TESTING"] = True
            cliente = painel_agentes.app.test_client()
            with cliente.session_transaction() as s:
                s["nivel_acesso"] = "expedicao"
                s["usuario"] = "teste"
            r = cliente.get(f"/etiquetas/fila/{job}.pdf")
        self.assertEqual((r.status_code, r.mimetype), (200, "application/pdf"))
        self.assertTrue(r.data.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
