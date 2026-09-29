# -*- coding: utf-8 -*-
"""
test_usuarios_painel.py

Testes dos usuarios cadastrados do painel (Hugo, 29/09): cadastro,
validacoes, senha e a regra de acesso por tela (GET = leitura, resto =
total, rota fora de tela = negada).

O banco vai pra uma pasta temporaria; nada toca no dados.db real.
Rodar (da raiz):
    py -3.11 -m unittest painel_agentes.test_usuarios_painel -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import usuarios_painel as up  # noqa: E402


class _ComBanco(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = up.conectar(Path(self._tmp.name) / "dados.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def criar(self, **extra):
        dados = {"usuario_id": None, "usuario": "Maria.Silva", "nome": "Maria", "senha": "senha-forte",
                 "ativo": True, "permissoes": {"torre": "total", "consulta": "leitura"}, "por": "hugo"}
        dados.update(extra)
        return up.salvar_usuario(self.conn, **dados)


class TestCadastro(_ComBanco):

    def test_cria_normaliza_login_e_grava_permissoes(self):
        uid = self.criar()
        u = up.buscar_usuario(self.conn, uid)
        self.assertEqual(u["usuario"], "maria.silva")
        self.assertEqual(u["permissoes"], {"torre": "total", "consulta": "leitura"})
        self.assertNotIn("senha_hash", u)

    def test_descarta_tela_e_acesso_desconhecidos(self):
        uid = self.criar(permissoes={"torre": "admin", "inventada": "total", "mapa": "", "vigia": "leitura"})
        self.assertEqual(up.buscar_usuario(self.conn, uid)["permissoes"], {"vigia": "leitura"})

    def test_autentica_so_com_senha_certa_e_ativo(self):
        uid = self.criar()
        self.assertIsNone(up.autenticar(self.conn, "maria.silva", "errada"))
        self.assertEqual(up.autenticar(self.conn, " MARIA.silva ", "senha-forte")["id"], uid)
        up.salvar_usuario(self.conn, usuario_id=uid, usuario="maria.silva", nome="Maria", senha="",
                          ativo=False, permissoes={}, por="hugo")
        self.assertIsNone(up.autenticar(self.conn, "maria.silva", "senha-forte"))

    def test_edicao_sem_senha_mantem_a_atual_e_nao_derruba_sessao(self):
        uid = self.criar()
        versao = up.buscar_usuario(self.conn, uid)["versao"]
        up.salvar_usuario(self.conn, usuario_id=uid, usuario="maria.silva", nome="Maria S.", senha="",
                          ativo=True, permissoes={"torre": "leitura"}, por="hugo")
        u = up.buscar_usuario(self.conn, uid)
        self.assertEqual(u["versao"], versao)
        self.assertEqual(u["permissoes"], {"torre": "leitura"})
        self.assertIsNotNone(up.autenticar(self.conn, "maria.silva", "senha-forte"))

    def test_trocar_senha_ou_desativar_incrementa_versao(self):
        uid = self.criar()
        v1 = up.buscar_usuario(self.conn, uid)["versao"]
        up.salvar_usuario(self.conn, usuario_id=uid, usuario="maria.silva", nome="Maria", senha="outra-senha",
                          ativo=True, permissoes={}, por="hugo")
        v2 = up.buscar_usuario(self.conn, uid)["versao"]
        self.assertGreater(v2, v1)
        up.salvar_usuario(self.conn, usuario_id=uid, usuario="maria.silva", nome="Maria", senha="",
                          ativo=False, permissoes={}, por="hugo")
        self.assertGreater(up.buscar_usuario(self.conn, uid)["versao"], v2)

    def test_validacoes(self):
        self.criar()
        casos = [
            ({"usuario": "ab"}, "Login inválido"),
            ({"usuario": "com espaco"}, "Login inválido"),
            ({"usuario": "MARIA.SILVA"}, "Já existe"),
            ({"usuario": "admin", "logins_reservados": ["Admin"]}, "acesso fixo"),
            ({"usuario": "joao", "nome": "  "}, "nome"),
            ({"usuario": "joao", "senha": "curta"}, "pelo menos"),
            ({"usuario": "joao", "senha": ""}, "senha"),
        ]
        for extra, trecho in casos:
            with self.subTest(extra=extra):
                with self.assertRaises(up.ErroUsuario) as ctx:
                    self.criar(**extra)
                self.assertIn(trecho, str(ctx.exception))

    def test_nao_se_tranca_pra_fora(self):
        uid = self.criar(permissoes={"usuarios": "total"})
        with self.assertRaises(up.ErroUsuario):
            up.salvar_usuario(self.conn, usuario_id=uid, usuario="maria.silva", nome="Maria", senha="",
                              ativo=True, permissoes={"usuarios": "leitura"}, por="maria.silva", editor_id=uid)
        with self.assertRaises(up.ErroUsuario):
            up.salvar_usuario(self.conn, usuario_id=uid, usuario="maria.silva", nome="Maria", senha="",
                              ativo=False, permissoes={"usuarios": "total"}, por="maria.silva", editor_id=uid)
        # Outra pessoa pode.
        up.salvar_usuario(self.conn, usuario_id=uid, usuario="maria.silva", nome="Maria", senha="",
                          ativo=True, permissoes={}, por="hugo", editor_id=None)


class TestRegraDeAcesso(unittest.TestCase):

    def test_rotas_caem_na_tela_certa(self):
        casos = {
            ("/torre", "torre"): ["torre"],
            ("/torre/mobile", "torre_mobile"): ["torre"],
            ("/api/torre/tratar", "api_torre_tratar"): ["torre"],
            ("/historico", "historico"): ["execucoes"],
            ("/historico-tratativas", "historico_tratativas"): ["tratativas"],
            ("/", "index"): ["agentes"],
            ("/execucao/5", "execucao"): ["agentes", "execucoes"],
            ("/wms", "wms"): ["galpao"],
            ("/wms/estoque", "wms_estoque"): ["estoque"],
            ("/api/wms/pedidos", "api_wms_pedidos"): ["galpao", "estoque"],
            ("/api/wms/movimentos", "api_wms_registrar_movimento"): ["galpao"],
            ("/canhotos/3/foto", "canhoto_foto"): ["consulta", "canhotos"],
            ("/usuarios/salvar", "usuarios_salvar"): ["usuarios"],
            ("/torreX", None): [],
        }
        for (caminho, endpoint), esperado in casos.items():
            with self.subTest(caminho=caminho):
                self.assertEqual(sorted(up.paginas_da_rota(caminho, endpoint)), sorted(esperado))

    def test_leitura_so_get_total_tudo_sem_acesso_nada(self):
        perms = {"torre": "leitura", "planejamento": "total"}
        self.assertEqual(up.pode(perms, "/torre", "torre", "GET"), (True, "leitura"))
        self.assertEqual(up.pode(perms, "/api/torre/tratar", "api_torre_tratar", "POST"), (False, "leitura"))
        self.assertEqual(up.pode(perms, "/api/planejamento/nova-rota", "x", "POST"), (True, "total"))
        self.assertEqual(up.pode(perms, "/api/planejamento/dedicado", "x", "DELETE"), (True, "total"))
        self.assertEqual(up.pode(perms, "/consulta", "consulta", "GET"), (False, None))
        self.assertEqual(up.pode({}, "/rota-que-ninguem-mapeou", "nova", "GET"), (False, None))
        self.assertEqual(up.pode({}, "/api/sidebar/contadores", "api_sidebar_contadores", "GET"), (True, None))

    def test_rota_de_duas_telas_usa_o_maior_acesso(self):
        perms = {"estoque": "total", "galpao": "leitura"}
        self.assertEqual(up.pode(perms, "/api/wms/pedidos/1/liberar-reservas", "api_wms_liberar_reservas", "POST"),
                         (True, "total"))
        self.assertEqual(up.pode(perms, "/api/wms/movimentos", "api_wms_registrar_movimento", "POST"),
                         (False, "leitura"))

    def test_tela_inicial_e_contadores(self):
        self.assertEqual(up.endpoint_inicial({"canhotos": "leitura", "consulta": "total"}), "consulta")
        self.assertIsNone(up.endpoint_inicial({}))
        self.assertEqual(up.contadores_permitidos({"torre": "leitura", "mapa": "total"}), {"torre"})

    def test_menu_lateral_tem_todas_as_telas(self):
        menu = (Path(__file__).parent / "templates" / "_menu_lateral_nav.html").read_text(encoding="utf-8")
        for chave in up.CHAVES_PAGINAS:
            with self.subTest(chave=chave):
                self.assertIn(f"'pagina': '{chave}'", menu)


if __name__ == "__main__":
    unittest.main()
