# -*- coding: utf-8 -*-
"""
Modo --prontos do atualizar_volumes_conferidos.py: le o total conferido dos pedidos que
estao em "aguardando transportador" na Stokki e ainda nao tem leitura (sugestao de caixas
da etiqueta de volume, 28/11).
Rodar (da raiz):  py -3.11 -m unittest roteirizacao.test_volumes_prontos
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import atualizar_volumes_conferidos as avc  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self.pasta.cleanup)
        self.db = Path(self.pasta.name) / "t.db"
        self.db_vazio = Path(self.pasta.name) / "vazio.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE vigia_stokki_abertos (codigo TEXT PRIMARY KEY, id_stokki INTEGER, embarcador TEXT, "
                    "status_stokki TEXT, fonte TEXT, primeira_vez_em TEXT, ultima_vez_em TEXT)")
        con.executemany("INSERT INTO vigia_stokki_abertos VALUES (?,?,?,?,?,?,?)", [
            ("PS-1", 1, "A", "Waiting for Carrier", "prioritario", "2026-11-28 09:00:00", "x"),   # ja lido
            ("PS-2", 2, "A", "Waiting for Carrier", "prioritario", "2026-11-28 10:00:00", "x"),
            ("PS-3", 3, "A", "Ready to Pack", "em_separacao", "2026-11-28 11:00:00", "x"),        # em conferencia
            ("PS-4", 4, "A", "Waiting for Carrier", "prioritario", "2026-11-28 12:00:00", "x"),
            ("PS-5", None, "A", "Waiting for Carrier", "prioritario", "2026-11-28 13:00:00", "x")])  # sem id
        con.commit()
        con.close()
        avc.gravar(1, 3, "nfe", self.db)


class TestProntosSemLeitura(Base):
    def test_so_pronto_sem_leitura_mais_novo_primeiro(self):
        self.assertEqual(avc.ids_prontos_sem_leitura(self.db), [4, 2])
        self.assertEqual(avc.ids_prontos_sem_leitura(self.db, limite=1), [4])

    def test_sem_as_tabelas_devolve_vazio(self):
        self.assertEqual(avc.ids_prontos_sem_leitura(self.db_vazio), [])

    def test_modo_prontos_le_e_grava(self):
        detalhe = {"quantidade_volumes": 6, "fonte_volumes": "conferencia"}
        with patch("stokki.pedidos.obter_detalhe", return_value=detalhe) as lido:
            self.assertEqual(avc.main_prontos(self.db, sessao=object(), limite=80), 0)
        self.assertEqual(sorted(c.args[1] for c in lido.call_args_list), [2, 4])
        self.assertEqual(avc.carregar({1, 2, 4}, self.db),
                         {1: (3, "nfe"), 2: (6, "conferencia"), 4: (6, "conferencia")})   # o 1 nao foi relido
        self.assertEqual(avc.ids_prontos_sem_leitura(self.db), [])

    def test_stokki_fora_sai_com_erro_e_nada_pra_ler_sai_bem(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO vigia_stokki_abertos VALUES "
                    "('PS-6', 6, 'A', 'Waiting for Carrier', 'prioritario', '2026-11-28 14:00:00', 'x')")
        con.commit()
        con.close()
        with patch("stokki.pedidos.obter_detalhe", side_effect=RuntimeError("500")):
            self.assertEqual(avc.main_prontos(self.db, sessao=object(), limite=80), 1)   # 3 de 3 falharam
        with patch("stokki.pedidos.obter_detalhe", side_effect=AssertionError("nao era pra ler")):
            self.assertEqual(avc.main_prontos(self.db_vazio, sessao=object(), limite=80), 0)

    def test_um_ou_dois_pedidos_com_erro_nao_disparam_o_alerta_de_falha(self):
        # revisao final: de hora em hora sao 1 a 3 pedidos; um pedido que da erro sempre (e por isso
        # e relido toda rodada) viraria "falhou tudo" e alerta de falha a cada hora
        with patch("stokki.pedidos.obter_detalhe", side_effect=RuntimeError("500")):
            self.assertEqual(avc.main_prontos(self.db, sessao=object(), limite=80), 0)   # 2 de 2
            self.assertEqual(avc.main_prontos(self.db, sessao=object(), limite=1), 0)    # 1 de 1


if __name__ == "__main__":
    unittest.main()
