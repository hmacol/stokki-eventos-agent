# -*- coding: utf-8 -*-
"""
test_prontidao_stokki.py -- prontidao do pedido na Stokki pelo retrato do pipeline (vigia_stokki_abertos),
reutilizavel pelo pool da tela, pelo envio do rascunho e pelo incremento das 22h (plano 1.2 do freshhub,
Task 7). Antes vivia so em painel_agentes/planejamento_rotas.py.

    py -3.11 -m unittest nucleo.test_prontidao_stokki -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import prontidao_stokki as ps  # noqa: E402
from vigia import banco as vigia_banco  # noqa: E402

RETRATO = {"PS-40001": "Open", "PS-40002": "Separating", "PS-40003": "Ready to Pack", "PS-40004": "On hold",
           "PS-40005": "Em espera", "PS-40006": "Waiting for Carrier"}


class TestSituacao(unittest.TestCase):
    def test_rotulo_por_status_e_none_quando_pronto_ou_fora_do_retrato(self):
        self.assertEqual([ps.situacao_nao_pronta(c, RETRATO) for c in
                          ("#PS-40001", "#PS-40002", "#PS-40003", "#PS-40004", "#PS-40005", "#PS-40006", "#PS-9")],
                         ["Em aberto", "Em separação", "Em conferência", "Em espera", "Em espera", None, None])

    def test_reentrega_e_codigo_combinado_casam_pelo_codigo_base(self):
        retrato = {"PS-41010": "Waiting for Carrier", "PS-41011": "Separating"}
        self.assertIsNone(ps.situacao_nao_pronta("#PS-41010-R1", retrato))
        self.assertEqual(ps.situacao_nao_pronta("#PS-41010, #PS-41011", retrato), "Em separação")
        self.assertEqual(ps.situacao_nao_pronta("PS-41011-R2", retrato), "Em separação")
        self.assertIsNone(ps.situacao_nao_pronta("", retrato))
        self.assertIsNone(ps.situacao_nao_pronta(None, {}))

    def test_separar_prontos_por_chave(self):
        itens = [{"code": "#PS-40006"}, {"code": "#PS-40003"}, {"code": "#PS-7"}]
        prontos, nao = ps.separar_prontos(itens, RETRATO, chave="code")
        self.assertEqual([p["code"] for p in prontos], ["#PS-40006", "#PS-7"])
        self.assertEqual(nao, [{"code": "#PS-40003", "situacao_stokki": "Em conferência"}])
        paradas = [{"codigo": "#PS-40001", "service_id": 1}]
        self.assertEqual(ps.separar_prontos(paradas, RETRATO), ([], [{"codigo": "#PS-40001", "service_id": 1,
                                                                        "situacao_stokki": "Em aberto"}]))
        self.assertEqual(ps.separar_prontos(itens, {}, chave="code"), (itens, []))

    def test_anotar_situacao_no_pool(self):
        pool = [{"codigo": "#PS-40003"}, {"codigo": "#PS-40006"}]
        ps.anotar_situacao_stokki(pool, RETRATO)
        self.assertEqual([p["situacao_stokki"] for p in pool], ["Em conferência", None])


class TestCarregarRetrato(unittest.TestCase):
    def test_le_codigos_e_hora_da_ultima_listagem(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "dados.db"
            conn = vigia_banco.conectar(db)
            conn.execute("INSERT INTO vigia_stokki_abertos (codigo, status_stokki, primeira_vez_em, ultima_vez_em) "
                         "VALUES ('PS-40001', 'Open', '2026-10-19 08:05:00', '2026-10-19 22:18:28')")
            conn.execute("INSERT INTO vigia_listagens (concluida_em, completa, qtd) VALUES ('2026-10-19 22:18:28', 1, 1)")
            conn.commit()
            conn.close()
            self.assertEqual(ps.carregar_retrato_stokki(db), ({"PS-40001": "Open"}, "19/10 22:18"))
            self.assertEqual(ps.carregar_retrato_stokki(Path(d) / "vazio.db"), ({}, None))


if __name__ == "__main__":
    unittest.main()
