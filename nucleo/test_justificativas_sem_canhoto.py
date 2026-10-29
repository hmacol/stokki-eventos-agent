# -*- coding: utf-8 -*-
"""
test_justificativas_sem_canhoto.py -- lista fixa de justificativas de entrega sem canhoto (checklist Vuupt
pelas telas). Codigos, rotulos e ordem vem da spec; "outro" exige texto de 5 a 200 caracteres.

    py -3.11 -m unittest nucleo.test_justificativas_sem_canhoto -v
"""
import sys
import unittest
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import justificativas_sem_canhoto as jus  # noqa: E402


class TestJustificativas(unittest.TestCase):

    def test_lista_na_ordem(self):
        self.assertEqual(
            list(jus.JUSTIFICATIVAS.items()),
            [
                ("canhoto_depois", "Canhoto chega depois (motorista vai trazer)"),
                ("nao_assinou", "Cliente não assinou / recusou assinar"),
                ("confirmado_contato", "Entrega confirmada por telefone ou WhatsApp com o cliente"),
                ("sem_bateria", "Motorista sem celular ou sem bateria na hora da entrega"),
                ("foto_perdida", "Foto do canhoto perdida ou ilegível"),
                ("acordo_embarcador", "Entrega sem canhoto por acordo com o embarcador"),
                ("outro", "Outro"),
            ],
        )
        self.assertEqual(jus.rotulo("nao_assinou"), "Cliente não assinou / recusou assinar")

    def test_codigo_invalido(self):
        for codigo in (None, "", "inexistente", "OUTRO"):
            with self.subTest(codigo=codigo):
                with self.assertRaises(ValueError) as ctx:
                    jus.validar(codigo, "texto qualquer")
                self.assertEqual(str(ctx.exception), "Escolha uma justificativa da lista.")

    def test_outro_exige_texto(self):
        for texto in (None, "", "     "):
            with self.subTest(texto=texto):
                with self.assertRaises(ValueError) as ctx:
                    jus.validar("outro", texto)
                self.assertEqual(str(ctx.exception), "Descreva o motivo em 5 a 200 caracteres.")

    def test_outro_limites_5_200(self):
        self.assertEqual(jus.validar("outro", "abcde"), ("outro", "abcde"))
        self.assertEqual(jus.validar("outro", "a" * 200), ("outro", "a" * 200))
        # o texto e aparado antes de medir: 4 letras com espacos continuam invalidas
        with self.assertRaises(ValueError):
            jus.validar("outro", "   abcd   ")
        with self.assertRaises(ValueError):
            jus.validar("outro", "a" * 201)
        # aparado: devolve o texto sem espacos nas pontas
        self.assertEqual(jus.validar("outro", "  motivo real  "), ("outro", "motivo real"))

    def test_texto_ignorado_fora_do_outro(self):
        self.assertEqual(jus.validar("nao_assinou", "qualquer coisa"), ("nao_assinou", ""))
        self.assertEqual(jus.validar("nao_assinou", None), ("nao_assinou", ""))


if __name__ == "__main__":
    unittest.main()
