# -*- coding: utf-8 -*-
"""py -3.11 -m unittest stokki.test_devolucao_estoque"""
import unittest
from datetime import datetime

from stokki import devolucao_estoque as de

T = datetime(2026, 11, 3, 12, 30)   # cancelamento


def mov(tipo, ref, q, h):
    return {"tipo": tipo, "movimento": "Interno", "referencia": ref, "quantidade": q,
            "quando": datetime(2026, 11, 3, h, 0)}


class TestRegras(unittest.TestCase):
    def test_bloqueado_soma_so_o_pedido(self):
        ms = [mov("Bloqueio", "#PS-43101", 4, 12), mov("Bloqueio", "#PE-2609", 20, 11),
              mov("Bloqueio", "#PS-43102", 6, 12)]
        self.assertEqual(de.bloqueado_pelo_pedido(ms, 43101), 4)

    def test_bloqueado_nao_confunde_prefixo(self):   # PS-4310 x PS-43101
        self.assertEqual(de.bloqueado_pelo_pedido([mov("Bloqueio", "#PS-43101", 4, 12)], 4310), 0)

    def test_bloqueado_tolera_espaco_e_sem_cerquilha(self):
        ms = [mov("Bloqueio", " PS-43101 ", 4, 12), mov("Bloqueio", "#PS-43101", 1, 12)]
        self.assertEqual(de.bloqueado_pelo_pedido(ms, 43101), 5)

    def test_sem_desbloqueio_nao_devolvido(self):
        self.assertEqual(de.ja_devolvido([mov("Bloqueio", "#PS-43101", 4, 12)], 4, T), "nao")

    def test_desbloqueio_igual_sem_referencia_depois_do_cancelamento(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 4, 13)], 4, T), "sim")

    def test_desbloqueio_antes_do_cancelamento_nao_conta(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 4, 11)], 4, T), "nao")

    def test_desbloqueio_de_outra_quantidade_e_duvida(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 3, 13)], 4, T), "duvida")

    def test_desbloqueio_com_referencia_nao_conta(self):   # "#stkkadj-" e ajuste, nao devolucao
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "#stkkadj-1097", 4, 13)], 4, T), "nao")

    def test_quanto_mover(self):
        self.assertEqual((de.quanto_mover(4, 12), de.quanto_mover(4, 3), de.quanto_mover(4, 0)), (4, 3, 0))
        self.assertEqual(de.quanto_mover(-1, 5), 0)

    def test_destino_origem_primeiro(self):
        r = de.escolher_destinos(4, [("FABENNE-2", 4)], [{"endereco": "FABENNE-1", "estoque": 5}], [])
        self.assertEqual(r[0], ("FABENNE-2", 4))

    def test_destino_origem_em_dois_enderecos(self):
        r = de.escolher_destinos(4, [("A-1", 3), ("A-2", 1)], [], [])
        self.assertEqual(r[:2], [("A-1", 3), ("A-2", 1)])

    def test_destino_origem_parcial_resto_vai_pro_melhor_do_produto(self):
        r = de.escolher_destinos(4, [("A-1", 3)], [{"endereco": "B-1", "estoque": 9}], [])
        self.assertEqual(r[:2], [("A-1", 3), ("B-1", 1)])

    def test_destino_sem_origem_maior_saldo_do_produto(self):
        r = de.escolher_destinos(4, [], [{"endereco": "Virtual", "estoque": 28},
                                         {"endereco": "FABENNE-1", "estoque": 5},
                                         {"endereco": "FABENNE-3", "estoque": 9}], [])
        self.assertEqual(r[0], ("FABENNE-3", 4))

    def test_destino_sem_produto_maior_saldo_do_embarcador(self):
        r = de.escolher_destinos(4, [], [{"endereco": "Virtual", "estoque": 28}],
                                 [{"endereco": "Ajuste", "estoque": 99}, {"endereco": "FABENNE-1", "estoque": 40}])
        self.assertEqual(r, [("FABENNE-1", 4)])

    def test_destino_nenhum(self):
        self.assertEqual(de.escolher_destinos(4, [], [], [{"endereco": "VIRTUAL", "estoque": 9}]), [])

    def test_origem_de_sistema_e_ignorada(self):
        self.assertEqual(de.escolher_destinos(4, [("Sobra", 4)], [{"endereco": "X-1", "estoque": 1}], [])[0],
                         ("X-1", 4))

    def test_empate_ordem_alfabetica(self):
        r = de.escolher_destinos(4, [], [{"endereco": "B-1", "estoque": 5}, {"endereco": "A-1", "estoque": 5}], [])
        self.assertEqual(r[0], ("A-1", 4))

    def test_antigo(self):
        agora = datetime(2026, 11, 5, 12, 0)
        self.assertTrue(de.cancelamento_antigo(datetime(2026, 11, 3, 12, 0), agora, True))
        self.assertFalse(de.cancelamento_antigo(datetime(2026, 11, 5, 9, 0), agora, True))
        self.assertFalse(de.cancelamento_antigo(datetime(2026, 11, 1, 9, 0), agora, False))
        self.assertTrue(de.cancelamento_antigo(None, agora, True))

    def test_bloqueado_ignora_movimento_nao_interno(self):
        externo = dict(mov("Bloqueio", "#PS-43101", 4, 12), movimento="Externo")
        self.assertEqual(de.bloqueado_pelo_pedido([externo, mov("Bloqueio", "#PS-43101", 2, 12)], 43101), 2)

    def test_desbloqueio_nao_interno_e_ignorado(self):
        externo = dict(mov("Desbloqueio", "", 4, 13), movimento="Externo")
        self.assertEqual(de.ja_devolvido([externo], 4, T), "nao")

    def test_desbloqueio_no_mesmo_minuto_conta(self):
        m = dict(mov("Desbloqueio", "", 4, 12), quando=T)
        self.assertEqual(de.ja_devolvido([m], 4, T), "sim")

    def test_sem_data_de_cancelamento_e_duvida(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 4, 13)], 4, None), "duvida")

    def test_linha_sem_data_conta_como_posterior(self):
        m = dict(mov("Desbloqueio", "", 4, 13), quando=None)
        self.assertEqual(de.ja_devolvido([m], 4, T), "sim")

    def test_quantidade_com_tolerancia(self):
        self.assertEqual(de.ja_devolvido([mov("Desbloqueio", "", 0.1 + 0.2, 13)], 0.3, T), "sim")

    def test_endereco_vazio_descartado(self):
        self.assertEqual(de.escolher_destinos(4, [("", 4), ("  ", 4)], [{"endereco": "", "estoque": 5}], []), [])

    def test_origem_que_passa_de_qtd_e_cortada(self):
        r = de.escolher_destinos(4, [("A-1", 3), ("A-2", 3)], [], [])
        self.assertEqual(r, [("A-1", 3), ("A-2", 1)])

    def test_antigo_limite_exato_24h(self):
        self.assertTrue(de.cancelamento_antigo(datetime(2026, 11, 4, 12, 0), datetime(2026, 11, 5, 12, 0), True))


if __name__ == "__main__":
    unittest.main()
