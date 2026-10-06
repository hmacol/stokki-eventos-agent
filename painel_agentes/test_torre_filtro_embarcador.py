"""Opções do filtro por embarcador da Torre (05/10)."""
import unittest

from torre_controle import montar_opcoes_embarcador


class TestOpcoesEmbarcador(unittest.TestCase):
    def test_grupo_com_filiais_ganha_opcao_do_grupo_e_de_cada_uma(self):
        opcoes = montar_opcoes_embarcador([
            (21849438, "MARCHEF - JEE"),
            (18164790, "MARCHEF - ALMAZ"),
            (16362183, "NUU"),
        ])
        self.assertEqual([o["chave"] for o in opcoes],
                         ["g:MARCHEF", "s:18164790", "s:21849438", "s:16362183"])
        grupo = opcoes[0]
        self.assertTrue(grupo["grupo"])
        self.assertEqual(sorted(grupo["sender_ids"]), [18164790, 21849438])
        self.assertEqual(grupo["rotulo"], "MARCHEF (grupo, 2)")
        self.assertEqual(opcoes[3], {"chave": "s:16362183", "rotulo": "NUU",
                                     "sender_ids": [16362183], "grupo": False})

    def test_sem_sender_ou_sem_nome_fica_de_fora(self):
        opcoes = montar_opcoes_embarcador([(None, "CAK ALIMENTOS LTDA - ZANKY"), (123, None), (456, "  FARM ")])
        self.assertEqual(opcoes, [{"chave": "s:456", "rotulo": "FARM", "sender_ids": [456], "grupo": False}])


if __name__ == "__main__":
    unittest.main()
