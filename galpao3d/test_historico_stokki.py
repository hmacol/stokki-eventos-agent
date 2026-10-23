# -*- coding: utf-8 -*-
import unittest
from datetime import datetime
from pathlib import Path

from galpao3d.historico_stokki import ler_historico

PASTA = Path(__file__).parent / "fixtures"


def _ler(nome):
    return ler_historico((PASTA / nome).read_text(encoding="utf-8"))


class TestHistoricoStokki(unittest.TestCase):
    def test_linha_do_tempo_completa(self):
        h = _ler("show_aguardando.html")
        self.assertEqual(
            [(e["quando"], e["usuario"], e["situacao"]) for e in h["linha_do_tempo"]],
            [
                (datetime(2026, 10, 22, 15, 39), "Cliente", "Aberto"),
                (datetime(2026, 10, 22, 16, 13), "Werlly Oliveira", "Em separação"),
                (datetime(2026, 10, 22, 16, 25), "Werlly Oliveira", "Em Conferência"),
                (datetime(2026, 10, 22, 18, 27), "Felipe Gabriel", "Aguardando Transportador"),
            ],
        )

    def test_rotulos_aguardando(self):
        h = _ler("show_aguardando.html")
        self.assertEqual(h["operador_tarefa"], "Werlly Oliveira")
        self.assertEqual(h["tarefa"], "#TO-23729")
        self.assertEqual(h["codigo_separacao"], "W000000801")

    def test_conferencia(self):
        c = _ler("show_aguardando.html")["conferencia"]
        self.assertEqual(c["estacao"], "Estação de Conferência 2")
        self.assertEqual(c["usuario"], "Felipe Gabriel")
        self.assertEqual(c["inicio"], datetime(2026, 10, 22, 18, 13))
        self.assertEqual(c["fim"], datetime(2026, 10, 22, 18, 27))
        self.assertEqual(c["segundos"], 14 * 60 + 21)
        self.assertIs(c["problema"], False)

    def test_unidades_e_itens(self):
        h = _ler("show_aguardando.html")
        self.assertEqual((h["unidades"], h["itens"]), (540, 9))
        h = _ler("show_conferencia.html")
        self.assertEqual((h["unidades"], h["itens"]), (90, 12))
        h = _ler("show_separando.html")
        self.assertEqual((h["unidades"], h["itens"]), (13, 8))

    def test_conferencia_em_andamento(self):
        h = _ler("show_conferencia.html")
        self.assertEqual(
            [(e["quando"], e["usuario"], e["situacao"]) for e in h["linha_do_tempo"]],
            [
                (datetime(2026, 10, 21, 20, 4), "Cliente", "Aberto"),
                (datetime(2026, 10, 22, 10, 8), "Werlly Oliveira", "Em separação"),
                (datetime(2026, 10, 22, 11, 6), "Werlly Oliveira", "Em Conferência"),
            ],
        )
        c = h["conferencia"]
        self.assertEqual(c["estacao"], "Estação de Conferência 1")
        self.assertEqual(c["usuario"], "Caio")
        self.assertEqual(c["inicio"], datetime(2026, 10, 22, 17, 5))
        self.assertIsNone(c["fim"])
        self.assertIsNone(c["segundos"])
        self.assertEqual(h["tarefa"], "#TO-23674")
        self.assertEqual(h["codigo_separacao"], "Q000000697")

    def test_separando_sem_conferencia(self):
        h = _ler("show_separando.html")
        self.assertIsNone(h["conferencia"])
        self.assertEqual(h["operador_tarefa"], "Werlly Oliveira")
        self.assertEqual(h["tarefa"], "#TO-23573")
        self.assertEqual(
            [(e["quando"], e["usuario"], e["situacao"]) for e in h["linha_do_tempo"]],
            [
                (datetime(2026, 10, 19, 14, 5), "Cliente", "Aberto"),
                (datetime(2026, 10, 20, 13, 56), "Werlly Oliveira", "Em separação"),
            ],
        )

    def test_evento_sem_usuario_e_mantido(self):
        html = (
            '<h4>Histórico do pedido</h4>'
            '<div class="time-label"><span class="bg-red">23/10/2026</span></div>'
            '<div class="timeline-item">'
            '<span class="time"><i class="fas fa-clock"></i> 09:30</span>'
            '<h3 class="timeline-header">Cancelado</h3></div>'
            '<div class="timeline-item">'
            '<span class="time"><i class="fas fa-clock"></i> 09:00</span>'
            '<span class="time"><i class="fas fa-user"></i> Cliente</span>'
            '<h3 class="timeline-header">Aberto</h3></div>'
            '<div class="timeline-item">'
            '<span class="time"><i class="fas fa-clock"></i> 09:00</span>'
            '<h3 class="timeline-header">Data do Pedido</h3></div>'
        )
        self.assertEqual(
            [(e["quando"], e["usuario"], e["situacao"]) for e in ler_historico(html)["linha_do_tempo"]],
            [(datetime(2026, 10, 23, 9, 0), "Cliente", "Aberto"),
             (datetime(2026, 10, 23, 9, 30), "", "Cancelado")],
        )

    def test_html_vazio(self):
        h = ler_historico("")
        self.assertEqual(h["linha_do_tempo"], [])
        self.assertIsNone(h["conferencia"])
        self.assertEqual((h["unidades"], h["itens"]), (0, 0))
        self.assertIsNone(h["operador_tarefa"])


if __name__ == "__main__":
    unittest.main()
