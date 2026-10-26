# -*- coding: utf-8 -*-
"""
Tela Demandas (25/10): equipes, visibilidade, permissoes, fluxo, eventos,
avisos (badge + som) e rotas.

    py -3.11 -m unittest test_demandas   (de dentro de painel_agentes/)
"""
import builtins
import importlib.util
import io
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_CONFIG_FALSO = "painel_agentes:\n  usuario: u\n  senha: s\n  secret_key: chave-de-teste\n"
_open_real = builtins.open


def _open_sem_config(arq, *a, **kw):
    # O worktree nao tem config.yaml (segredo): o painel so precisa dele pra subir.
    if Path(str(arq)).name == "config.yaml" and not Path(str(arq)).exists():
        return io.StringIO(_CONFIG_FALSO)
    return _open_real(arq, *a, **kw)


_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
with mock.patch.object(builtins, "open", _open_sem_config):
    _spec.loader.exec_module(painel_agentes)

import acesso_telas  # noqa: E402
import demandas as dm  # noqa: E402
import grupos_equipe  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.banco = Path(self._tmp.name) / "dados.db"
        for alvo in (dm, acesso_telas, grupos_equipe):
            p = mock.patch.object(alvo, "DB_PATH", self.banco)
            p.start()
            self.addCleanup(p.stop)
        c = acesso_telas.conectar()
        for login, nome, nivel in (("ana", "Ana", "operador"), ("bia", "Bia", "operador"),
                                   ("caio", "Caio", "atendimento"), ("davi", "Davi", "operador")):
            acesso_telas.criar(c, login, nome, nivel, "senha123", "teste", ())
        c.execute("UPDATE painel_usuarios SET trocar_senha = 0")
        c.commit()
        c.close()
        self.conn = dm.conectar()
        self.addCleanup(self.conn.close)
        self.admin = self.ident("u", "total")
        # Galpao: ana lider, bia membro; vinculada ao login fixo galpao. Atendimento: caio lider.
        self.galpao = dm.salvar_equipe(self.conn, self.admin, None, {
            "nome": "Galpão", "membros": [{"usuario": "ana", "lider": True}, {"usuario": "bia"}], "niveis": ["galpao"]})
        self.atend = dm.salvar_equipe(self.conn, self.admin, None, {
            "nome": "Atendimento", "membros": [{"usuario": "caio", "lider": True}]})

    def ident(self, usuario, nivel, telas=None, nome=None):
        return dm.identidade(self.conn, usuario, nivel, telas, nome)

    def criar(self, ident=None, **kw):
        dados = {"titulo": "Conferir avaria", "equipe_id": self.galpao["id"], **kw}
        return dm.criar(self.conn, ident or self.admin, dados)


class TestDados(Base):
    def test_identidade(self):
        ana, bia, galpao = self.ident("ana", "operador"), self.ident("bia", "operador"), self.ident("galpao", "galpao")
        self.assertTrue(ana["pode_criar"])
        self.assertFalse(bia["pode_criar"])
        self.assertEqual(ana["autor"], "Ana")
        self.assertFalse(galpao["nominal"])
        self.assertEqual(galpao["equipes"], {self.galpao["id"]})
        self.assertIsNone(galpao["autor"])
        # Acesso personalizado vira "total" dentro da tela, mas nao e admin.
        self.assertFalse(self.ident("davi", "total", telas=["demandas"])["admin"])

    def test_visibilidade(self):
        d_galpao = self.criar()
        d_atend = self.criar(self.ident("caio", "atendimento"), equipe_id=self.atend["id"], titulo="Ligar pro cliente")
        def ids(usuario, nivel, aba="equipe"):
            return {x["id"] for x in dm.listar(self.conn, self.ident(usuario, nivel), aba)}
        self.assertEqual(ids("bia", "operador"), {d_galpao["id"]})
        self.assertEqual(ids("galpao", "galpao"), {d_galpao["id"]})          # login fixo vinculado
        self.assertEqual(ids("expedicao", "expedicao"), set())               # login fixo sem vinculo
        self.assertEqual(ids("davi", "operador"), set())                     # sem equipe
        self.assertEqual(ids("u", "total", "todas"), {d_galpao["id"], d_atend["id"]})
        # quem criou acompanha mesmo sem ser da equipe
        d_cruzada = self.criar(self.ident("caio", "atendimento"), titulo="Separar devolução")
        self.assertEqual(ids("caio", "atendimento", "criadas"), {d_atend["id"], d_cruzada["id"]})
        with self.assertRaises(dm.SemPermissao):
            dm.listar(self.conn, self.ident("ana", "operador"), "todas")
        with self.assertRaises(dm.ErroDemanda):
            dm.detalhar(self.conn, self.ident("davi", "operador"), d_galpao["id"])

    def test_quem_cria(self):
        with self.assertRaises(dm.SemPermissao):
            self.criar(self.ident("bia", "operador"))
        d = self.criar(self.ident("ana", "operador"), equipe_id=self.atend["id"])   # lider manda pra outra equipe
        self.assertEqual(d["equipe"], "Atendimento")
        with self.assertRaises(dm.ErroDemanda):
            self.criar(responsavel="caio")   # nao e membro do Galpao

    def test_equipe_desativada_nao_recebe(self):
        d = self.criar()
        dm.salvar_equipe(self.conn, self.admin, self.galpao["id"],
                         {"nome": "Galpão", "ativo": False, "membros": [{"usuario": "ana", "lider": True}]})
        with self.assertRaises(dm.ErroDemanda):
            self.criar()
        self.assertTrue(dm.detalhar(self.conn, self.ident("ana", "operador"), d["id"]))   # antiga continua visivel

    def test_fluxo_e_eventos(self):
        d = self.criar(prioridade="ALTA")
        bia = self.ident("bia", "operador")
        self.assertIn("iniciar", d["acoes"])
        with self.assertRaises(dm.ErroDemanda):
            dm.mudar_status(self.conn, bia, d["id"], "CONCLUIDA")    # pulando etapa
        d = dm.mudar_status(self.conn, bia, d["id"], "EM_ANDAMENTO")
        self.assertEqual(d["responsavel"], "bia")                    # iniciou sem responsavel: assumiu
        with self.assertRaises(dm.SemPermissao):
            dm.mudar_status(self.conn, bia, d["id"], "CANCELADA", "nao precisa")   # cancelar e do gestor
        d = dm.mudar_status(self.conn, bia, d["id"], "CONCLUIDA")
        self.assertEqual(d["status"], "CONCLUIDA")
        with self.assertRaises(dm.SemPermissao):
            dm.mudar_status(self.conn, bia, d["id"], "ABERTA")
        d = dm.mudar_status(self.conn, self.ident("ana", "operador"), d["id"], "ABERTA")
        tipos = [e["tipo"] for e in dm.detalhar(self.conn, self.admin, d["id"])["eventos"]]
        self.assertEqual(tipos, ["criada", "status", "responsavel", "status", "reaberta"])

    def test_cancelar_pede_motivo(self):
        d = self.criar()
        with self.assertRaises(dm.ErroDemanda):
            dm.mudar_status(self.conn, self.admin, d["id"], "CANCELADA", "")
        d = dm.mudar_status(self.conn, self.admin, d["id"], "CANCELADA", "Cliente desistiu")
        self.assertEqual(d["status"], "CANCELADA")

    def test_editar_registra_de_para_e_troca_de_equipe_limpa_responsavel(self):
        d = self.criar(responsavel="bia")
        with self.assertRaises(dm.SemPermissao):   # membro sem ser lider nao edita
            dm.editar(self.conn, self.ident("bia", "operador"), d["id"], {"titulo": "Outro"})
        d = dm.editar(self.conn, self.admin, d["id"], {"prazo": "2026-10-30", "prioridade": "URGENTE"})
        d = dm.editar(self.conn, self.admin, d["id"], {"equipe_id": self.atend["id"]})
        self.assertIsNone(d["responsavel"])
        eventos = dm.detalhar(self.conn, self.admin, d["id"])["eventos"]
        prazo = next(e for e in eventos if e["tipo"] == "prazo")
        self.assertIn("2026-10-30", prazo["resumo"])
        self.assertTrue(any(e["tipo"] == "equipe" for e in eventos))
        self.assertTrue(any(e["tipo"] == "responsavel" and "—" in e["resumo"] for e in eventos))
        with self.assertRaises(dm.ErroDemanda):    # saiu da equipe dela: nem enxerga mais
            dm.editar(self.conn, self.ident("bia", "operador"), d["id"], {"titulo": "Outro"})

    def test_login_fixo_precisa_de_nome(self):
        d = self.criar()
        with self.assertRaises(dm.PrecisaNome):
            dm.comentar(self.conn, self.ident("galpao", "galpao"), d["id"], "Separado")
        d = dm.comentar(self.conn, self.ident("galpao", "galpao", nome="Zé do galpão"), d["id"], "Separado")
        self.assertEqual(d["eventos"][-1]["autor"], "Zé do galpão")

    def test_normalizar_pedido(self):
        self.assertEqual(dm.normalizar_pedido("#ps 36327"), "PS-36327")
        self.assertEqual(dm.normalizar_pedido("36327"), "PS-36327")
        self.assertEqual(dm.normalizar_pedido("PS.36327"), "PS-36327")
        self.assertEqual(dm.normalizar_pedido("#PS-1-R2"), "PS-1-R2")
        self.assertEqual(dm.normalizar_pedido("NF A12"), "NF A12")
        self.assertIsNone(dm.normalizar_pedido("  "))

    def test_avisos(self):
        bia, caio = self.ident("bia", "operador"), self.ident("caio", "atendimento")
        self.assertEqual(dm.avisos(self.conn, bia, None)["itens"], [])      # primeira vez: so o ponteiro
        inicio = dm.avisos(self.conn, bia, None)["max_id"]
        d = self.criar()
        itens = dm.avisos(self.conn, bia, inicio)["itens"]
        self.assertEqual([i["demanda_id"] for i in itens], [d["id"]])
        self.assertEqual(dm.avisos(self.conn, caio, inicio)["itens"], [])   # outra equipe
        self.assertEqual(dm.avisos(self.conn, self.ident("u", "total"), inicio)["itens"], [])   # proprio evento
        marco = dm.avisos(self.conn, caio, None)["max_id"]
        dm.editar(self.conn, self.admin, d["id"], {"equipe_id": self.atend["id"]})
        self.assertEqual([i["motivo"] for i in dm.avisos(self.conn, caio, marco)["itens"]], ["chegou pra sua equipe"])
        marco = dm.avisos(self.conn, caio, None)["max_id"]
        dm.editar(self.conn, self.admin, d["id"], {"responsavel": "caio"})
        self.assertEqual([i["motivo"] for i in dm.avisos(self.conn, caio, marco)["itens"]], ["ficou com você"])

    def test_badge(self):
        bia = self.ident("bia", "operador")
        self.criar()
        self.criar(prazo=(date.today() - timedelta(days=1)).isoformat())
        self.criar(prioridade="URGENTE")
        self.criar(equipe_id=self.atend["id"])
        self.assertEqual(dm.contar_badge(self.conn, bia), {"qtd": 3, "criticas": 2})

    def test_aviso_no_grupo_sem_titulo(self):
        self.criar(titulo="Assunto sigiloso", prioridade="URGENTE")
        cg = grupos_equipe.conectar()
        try:
            grupo = cg.execute("SELECT id FROM equipe_grupos WHERE nome = 'Demandas'").fetchone()
            textos = [m["texto"] for m in grupos_equipe.mensagens(cg, grupo["id"])]
        finally:
            cg.close()
        self.assertEqual(len(textos), 1)
        self.assertIn("[URGENTE] para Galpão", textos[0])
        self.assertNotIn("sigiloso", textos[0])

    def test_falha_no_grupo_nao_derruba_a_criacao(self):
        with mock.patch.object(grupos_equipe, "postar", side_effect=RuntimeError("fora")):
            d = self.criar()
        self.assertEqual(d["status"], "ABERTA")

    def test_equipes_so_admin_e_membros_validos(self):
        with self.assertRaises(dm.SemPermissao):
            dm.salvar_equipe(self.conn, self.ident("ana", "operador"), None, {"nome": "X"})
        with self.assertRaises(dm.ErroDemanda):
            dm.salvar_equipe(self.conn, self.admin, None, {"nome": "X", "membros": [{"usuario": "ninguem"}]})
        with self.assertRaises(dm.ErroDemanda):
            dm.salvar_equipe(self.conn, self.admin, None, {"nome": "galpão"})   # nome repetido
        with self.assertRaises(dm.ErroDemanda):
            dm.salvar_equipe(self.conn, self.admin, None, {"nome": "Y", "niveis": ["total"]})


class TestRotas(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(painel_agentes, "_carregar_config",
                              return_value={"painel_agentes": {"usuario": "u", "senha": "s"}})
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def logar(self, usuario, nivel):
        with self.cliente.session_transaction() as sess:
            sess.clear()
            sess["nivel_acesso"] = nivel
            sess["usuario"] = usuario
            sess["rodada_sessoes"] = acesso_telas.rodada_sessoes()["rodada"]

    def test_criar_e_ver_pela_api(self):
        self.logar("ana", "operador")
        r = self.cliente.post("/api/demandas", json={"titulo": "Separar PS-1", "equipe_id": self.galpao["id"],
                                                     "pedido_codigo": "1"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()["demanda"]
        self.assertEqual(d["pedido_codigo"], "PS-1")
        self.logar("bia", "operador")
        self.assertEqual([x["id"] for x in self.cliente.get("/api/demandas").get_json()["demandas"]], [d["id"]])
        self.assertEqual(self.cliente.post("/api/demandas", json={"titulo": "abc", "equipe_id": self.galpao["id"]},
                                           headers=ORIGEM).status_code, 403)
        self.assertEqual(self.cliente.post("/api/demandas", json={"titulo": "abc", "equipe_id": self.galpao["id"]})
                         .status_code, 403)   # sem Origin
        self.assertEqual(self.cliente.post("/api/demandas", json=[1], headers=ORIGEM).status_code, 400)

    def test_resposta_diz_quem_esta_logado(self):
        # aba aberta como um usuario + login de outro no mesmo navegador: a tela compara e recarrega
        self.logar("bia", "operador")
        r = self.cliente.post("/api/demandas", json={"titulo": "abc", "equipe_id": self.galpao["id"]}, headers=ORIGEM)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.headers.get("X-Demandas-Usuario"), "bia")
        self.assertEqual(self.cliente.get("/api/demandas/opcoes").headers.get("X-Demandas-Usuario"), "bia")

    def test_login_fixo_pede_nome_pela_api(self):
        d = self.criar()
        self.logar("galpao", "galpao")
        r = self.cliente.post(f"/api/demandas/{d['id']}/comentarios", json={"texto": "ok"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.get_json()["precisa_nome"])
        self.assertEqual(self.cliente.post("/api/demandas/nome", json={"nome": "Zé"}, headers=ORIGEM).status_code, 200)
        r = self.cliente.post(f"/api/demandas/{d['id']}/comentarios", json={"texto": "ok"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)

    def test_equipes_so_total_de_verdade(self):
        self.logar("u", "total")
        self.assertEqual(self.cliente.get("/api/demandas/equipes").status_code, 200)
        c = acesso_telas.conectar()
        acesso_telas.salvar(c, "davi", ["demandas"], "u")
        c.close()
        self.logar("davi", "operador")
        self.assertEqual(self.cliente.get("/demandas").status_code, 200)
        self.assertEqual(self.cliente.get("/api/demandas/equipes").status_code, 403)
        self.assertEqual(self.cliente.get("/api/demandas?aba=todas").status_code, 403)

    def test_contador_e_avisos_em_qualquer_tela(self):
        self.criar()
        self.logar("bia", "operador")
        cont = self.cliente.get("/api/sidebar/contadores").get_json()["contadores"]
        self.assertEqual(cont["demandas"]["qtd"], 1)
        self.assertEqual(self.cliente.get("/api/demandas/avisos").status_code, 200)
        # personalizado sem a tela Demandas ainda recebe o aviso (caminho comum)
        c = acesso_telas.conectar()
        acesso_telas.salvar(c, "davi", ["torre"], "u")
        c.close()
        self.logar("davi", "operador")
        self.assertEqual(self.cliente.get("/api/demandas/avisos").status_code, 200)

    def test_tela_e_aviso_no_wms(self):
        self.logar("u", "total")
        html = self.cliente.get("/demandas").get_data(as_text=True)
        self.assertIn('data-aba="equipes"', html)
        self.assertIn('id="demandas-aviso"', html)
        self.logar("galpao", "galpao")
        r = self.cliente.get("/wms")
        if r.status_code == 200:   # wms depende de dados do galpao; se renderizar, tem o aviso
            self.assertIn('id="demandas-aviso"', r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
