# -*- coding: utf-8 -*-
"""
Grupos internos da equipe no /atendimento (Hugo, 19/10/2026).

    py -3.11 -m unittest test_grupos_equipe   (de dentro de painel_agentes/)
"""
import sys
import unittest
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _RAIZ / "portal_cliente", _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import grupos_equipe as ge  # noqa: E402


class Grupos(unittest.TestCase):
    def setUp(self):
        self.conn = ge.conectar(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_criar_e_listar(self):
        g = ge.criar_grupo(self.conn, "  Operação  ", "ana", "Ana")
        self.assertEqual(g["nome"], "Operação")
        self.assertEqual(g["criado_por"], "Ana")
        lista = ge.listar_grupos(self.conn, "bia", "Bia")
        self.assertEqual([x["nome"] for x in lista], ["Operação"])
        self.assertEqual(lista[0]["nao_lidas"], 0)

    def test_nome_invalido_ou_repetido(self):
        ge.criar_grupo(self.conn, "Clientes", "ana", "Ana")
        for ruim in ("", " ", "a", "x" * 61, "clientes", " CLIENTES "):
            with self.assertRaises(ge.ErroGrupo, msg=repr(ruim)):
                ge.criar_grupo(self.conn, ruim, "ana", "Ana")

    def test_mensagem_e_nao_lidas(self):
        g = ge.criar_grupo(self.conn, "Operação", "ana", "Ana")
        m1 = ge.postar(self.conn, g["id"], "ana", "Ana", "  PS-41186 atrasou  ")
        self.assertEqual(m1["texto"], "PS-41186 atrasou")
        ge.postar(self.conn, g["id"], "ana", "Ana", "alguém vê?")
        # quem escreveu não tem não lidas; os outros têm
        self.assertEqual(ge.listar_grupos(self.conn, "ana", "Ana")[0]["nao_lidas"], 0)
        bia = ge.listar_grupos(self.conn, "bia", "Bia")[0]
        self.assertEqual(bia["nao_lidas"], 2)
        self.assertEqual(bia["ultima_texto"], "alguém vê?")
        self.assertEqual(bia["ultima_autor"], "Ana")
        # Bia lê: zera; Ana responde depois: Bia tem 1 de novo
        msgs = ge.mensagens(self.conn, g["id"])
        ge.marcar_lido(self.conn, g["id"], "bia", "Bia", msgs[-1]["id"])
        self.assertEqual(ge.listar_grupos(self.conn, "bia", "Bia")[0]["nao_lidas"], 0)
        ge.postar(self.conn, g["id"], "ana", "Ana", "?")
        self.assertEqual(ge.listar_grupos(self.conn, "bia", "Bia")[0]["nao_lidas"], 1)

    def test_login_compartilhado_conta_pelo_nome(self):
        """Login por nível é compartilhado (ex.: 'atendimento'): mensagem de
        outra pessoa no mesmo login também é não lida."""
        g = ge.criar_grupo(self.conn, "Operação", "atendimento", "Ana")
        ge.postar(self.conn, g["id"], "atendimento", "Carla", "oi")
        self.assertEqual(ge.listar_grupos(self.conn, "atendimento", "Ana")[0]["nao_lidas"], 1)
        self.assertEqual(ge.listar_grupos(self.conn, "atendimento", "Carla")[0]["nao_lidas"], 0)

    def test_mensagens_desde_e_eu(self):
        g = ge.criar_grupo(self.conn, "Operação", "ana", "Ana")
        a = ge.postar(self.conn, g["id"], "ana", "Ana", "um")
        ge.postar(self.conn, g["id"], "bia", "Bia", "dois")
        novas = ge.mensagens(self.conn, g["id"], desde=a["id"], usuario="ana", autor="Ana")
        self.assertEqual([m["texto"] for m in novas], ["dois"])
        self.assertFalse(novas[0]["eu"])
        self.assertTrue(ge.mensagens(self.conn, g["id"], usuario="ana", autor="Ana")[0]["eu"])

    def test_mensagem_invalida(self):
        g = ge.criar_grupo(self.conn, "Operação", "ana", "Ana")
        with self.assertRaises(ge.ErroGrupo):
            ge.postar(self.conn, g["id"], "ana", "Ana", "   ")
        with self.assertRaises(ge.ErroGrupo):
            ge.postar(self.conn, g["id"], "ana", "Ana", "x" * 4001)
        with self.assertRaises(ge.ErroGrupo):
            ge.postar(self.conn, 999, "ana", "Ana", "oi")

    def test_ordem_pela_ultima_atividade(self):
        a = ge.criar_grupo(self.conn, "Antigo", "ana", "Ana")
        ge.criar_grupo(self.conn, "Novo", "ana", "Ana")
        ge.postar(self.conn, a["id"], "ana", "Ana", "subiu")
        self.assertEqual([x["nome"] for x in ge.listar_grupos(self.conn, "ana", "Ana")], ["Antigo", "Novo"])

    def test_enviar_chamado(self):
        g = ge.criar_grupo(self.conn, "Clientes", "ana", "Ana")
        chamado = {"id": 77, "solicitante": "ACME Ltda", "assunto": "Entrega atrasada", "pedido_ref": "PS-41186",
                   "status_rotulo": "Em atendimento", "de_motorista": False}
        m = ge.postar(self.conn, g["id"], "ana", "Ana", ge.texto_chamado(chamado, "  vejam isso  "), chamado_id=77)
        self.assertEqual(m["chamado_id"], 77)
        self.assertIn("Chamado #77", m["texto"])
        self.assertIn("ACME Ltda", m["texto"])
        self.assertIn("PS-41186", m["texto"])
        self.assertTrue(m["texto"].startswith("vejam isso"))
        sem = ge.texto_chamado({"id": 5, "solicitante": "João", "de_motorista": True}, "")
        self.assertIn("Chamado #5", sem)
        self.assertIn("motorista João", sem)


if __name__ == "__main__":
    unittest.main()
