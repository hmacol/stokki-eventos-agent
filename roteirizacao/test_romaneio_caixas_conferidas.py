# -*- coding: utf-8 -*-
"""
Rodar (da raiz):  py -3.11 -m unittest roteirizacao.test_romaneio_caixas_conferidas
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import atualizar_volumes_conferidos as vc  # noqa: E402
import gerar_pdf_romaneios as gpr  # noqa: E402
from regras.tipo_veiculo import tipo_por_codigo  # noqa: E402
from stokki.pedidos import _parsear_pagina_detalhe  # noqa: E402


class FonteDoVolumeNaStokki(unittest.TestCase):
    HTML = ("<strong>Quantidade de volumes mencionado na NF-e:</strong> {nfe}<br>"
            "<strong>Quantidade de volumes informado na conferência:</strong> {conf}<br>")

    def _ler(self, nfe, conf):
        d = _parsear_pagina_detalhe(self.HTML.format(nfe=nfe, conf=conf), 1)
        return d["quantidade_volumes"], d["fonte_volumes"]

    def test_conferencia_vence_a_nfe(self):
        self.assertEqual(self._ler("10", "12"), (12, "conferencia"))

    def test_sem_conferencia_cai_na_nfe(self):
        self.assertEqual(self._ler("10", "Não informado"), (10, "nfe"))

    def test_sem_nenhum(self):
        self.assertEqual(self._ler("Não informado", "Não informado"), (None, None))


class TabelaVolumesConferidos(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self.pasta.cleanup)
        self.db = Path(self.pasta.name) / "t.db"

    def test_ids_do_codigo(self):
        self.assertEqual(vc.ids_do_codigo("#PS-37189, PS-37176-R1"), [37189, 37176])
        self.assertEqual(vc.ids_do_codigo(""), [])

    def test_carregar_sem_tabela_devolve_vazio(self):
        self.assertEqual(vc.carregar({1}, self.db), {})

    def test_grava_e_regrava(self):
        vc.gravar(1, 10, "nfe", self.db)
        vc.gravar(1, 12, "conferencia", self.db)
        vc.gravar(2, None, None, self.db)
        self.assertEqual(vc.carregar({1, 2, 3}, self.db), {1: (12, "conferencia"), 2: (None, None)})

    def test_atualizar_segue_depois_de_falha(self):
        def detalhe(_sessao, id_pedido):
            if id_pedido == 2:
                raise RuntimeError("500")
            return {"quantidade_volumes": 7, "fonte_volumes": "conferencia"}
        with patch("stokki.pedidos.obter_detalhe", side_effect=detalhe):
            cont = vc.atualizar(None, [1, 2, 3], self.db)
        self.assertEqual((cont["conferencia"], cont["falhas"]), (2, 1))
        self.assertEqual(set(vc.carregar({1, 2, 3}, self.db)), {1, 3})


class VolumesDoPedido(unittest.TestCase):
    def test_conferido(self):
        self.assertEqual(gpr._volumes_do_pedido("PS-1", {1: (12, "conferencia")}), (True, 12, True))

    def test_so_nfe_nao_e_conferido(self):
        self.assertEqual(gpr._volumes_do_pedido("PS-1-R1", {1: (10, "nfe")}), (True, 10, False))

    def test_nao_lido(self):
        self.assertEqual(gpr._volumes_do_pedido("PS-1", {}), (False, None, False))

    def test_lido_sem_numero(self):
        self.assertEqual(gpr._volumes_do_pedido("PS-1", {1: (None, None)}), (True, None, False))

    def test_servico_com_dois_pedidos_soma(self):
        conferidos = {1: (3, "conferencia"), 2: (4, "nfe")}
        self.assertEqual(gpr._volumes_do_pedido("PS-1, PS-2", conferidos), (True, 7, False))
        self.assertEqual(gpr._volumes_do_pedido("PS-1, PS-3", conferidos), (False, None, False))


class RomaneioUsaConferido(unittest.TestCase):
    """montar_pdf_rota: caixas do pedido = conferido (nao o dimension_3
    despoderado); a lotacao (normalizada / capacidade do veiculo) vai so
    pro log, nunca pra capa."""

    def _montar(self, conferidos, tipo):
        servicos = [
            # fator 3.0: dimension_3 = 36 normalizadas; o galpao conferiu 12
            {"code": "#PS-1", "title": "PS-1 - x / EMB / CLIENTE A", "sender_id": 999,
             "address": "Rua A, 1", "dimension_3": 36},
            {"code": "#PS-2", "title": "PS-2 - x / EMB / CLIENTE B", "sender_id": 999,
             "address": "Rua B, 2", "dimension_3": 90},
        ]
        capturado = {}

        def capa(rota, itens, *a, **kw):
            capturado["itens"], capturado["kw"] = itens, kw
            return [gpr._pagina_branca(gpr.A4_PAISAGEM)[0]]

        with tempfile.TemporaryDirectory() as pasta, \
                patch.object(gpr.volumes_conferidos, "carregar", return_value=conferidos), \
                patch.object(gpr, "_tipo_veiculo_da_rota", return_value=tipo), \
                patch.object(gpr, "carregar_catalogo_transportadoras", return_value=None), \
                patch.object(gpr, "gerar_capa", side_effect=capa),                 self.assertLogs(gpr.logger, "INFO") as logs:
            gpr.logger.info("inicio")
            gpr.montar_pdf_rota({"name": "Rota #1", "id": 1, "agent_id": 5}, servicos, {}, {999: "EMB"},
                                {999: 3.0}, "MOTORISTA", date(2026, 11, 16), Path(pasta) / "r.pdf")
        capturado["log"] = [m for m in logs.output if "lotacao" in m]
        return capturado

    def test_conferido_e_lotacao(self):
        c = self._montar({1: (12, "conferencia"), 2: (30, "nfe")}, tipo_por_codigo("FIORINO"))
        self.assertEqual([(i["volumes"], i["volumes_conferidos"]) for i in c["itens"]], [(12, True), (30, False)])
        # 36 + 90 normalizadas / Fiorino 100: so no log, como aviso
        self.assertEqual(len(c["log"]), 1)
        self.assertTrue(c["log"][0].startswith("WARNING"))
        self.assertIn("126 caixas normalizadas / 100 (FIORINO) = 126% -- ACIMA", c["log"][0])
        self.assertNotIn("lotacao", c["kw"])

    def test_sem_leitura_cai_no_que_valia_antes(self):
        c = self._montar({}, None)
        self.assertEqual([(i["volumes"], i["volumes_conferidos"]) for i in c["itens"]], [(12, False), (30, False)])
        self.assertEqual(c["log"], [])


class CapaDesenha(unittest.TestCase):
    def test_capa_desenha(self):
        item = {"posicao": 1, "codigo": "PS-1", "embarcador": "EMB", "cliente": "C", "endereco": "Rua A",
                "volumes": 12, "volumes_conferidos": False, "peso": None, "nfs": "", "tem_nf": False,
                "tem_boleto": False}
        paginas = gpr.gerar_capa({"name": "Rota #1", "id": 1}, [item], "M", date(2026, 11, 16), False, None)
        self.assertEqual(len(paginas), 1)


if __name__ == "__main__":
    unittest.main()
