# -*- coding: utf-8 -*-
"""
Tela /transportadoras do painel (cadastro de transportadoras + pendencias).

    py -3.11 -m unittest test_transportadoras_tela   (de dentro de painel_agentes/)
"""
import builtins
import importlib.util
import io
import sys
import tempfile
import unittest
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
import consulta_transportadora as ct  # noqa: E402
import contadores_menu  # noqa: E402
import transportadoras_cadastro as tc  # noqa: E402

ORIGEM = {"Origin": "http://localhost"}


class TestTelaTransportadoras(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        banco = Path(self._tmp.name) / "dados.db"
        for alvo, attr, valor in ((tc, "DB_PATH", banco), (ct, "DB_PATH", banco), (acesso_telas, "DB_PATH", banco)):
            p = mock.patch.object(alvo, attr, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(painel_agentes, "_carregar_config",
                              return_value={"painel_agentes": {"usuario": "u", "senha": "s"}})
        p.start()
        self.addCleanup(p.stop)
        # a liberacao das NFs bloqueadas le o dados.db real: aqui so conferimos a chamada
        p = mock.patch("portal_cliente.bloqueio_area.liberar_por_transportadora", return_value=[])
        self.liberar = p.start()
        self.addCleanup(p.stop)
        conn = ct.conectar_consultas(banco)
        self.ativa = tc.criar(conn, {"nome": "ALFA", "tipo": "ENTREGA", "situacao": "ATIVA"}, "PLANILHA", "t")
        r = ct.registrar_desconhecida(conn, "BETA LOG", "11222333000181", "PS-1", "12345678000199", None,
                                      {"destinatario": "X", "cidade": "Sao Paulo", "uf": "SP"})
        self.pendente = r["transportadora"]
        conn.close()
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def test_leitura_ve_mas_nao_edita(self):
        self._logar("leitura")
        r = self.cliente.get("/transportadoras")
        self.assertEqual(r.status_code, 200)
        self.assertIn("BETA LOG", r.get_data(as_text=True))
        r = self.cliente.post("/api/transportadoras", json={"nome": "Z"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 403)
        r = self.cliente.post(f"/api/transportadoras/{self.ativa['id']}", json={"nome": "Z"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 403)

    def test_leitura_e_atendimento_abrem_ficha_mas_nao_escrevem(self):
        for nivel in ("leitura", "atendimento"):
            self._logar(nivel)
            tid = self.pendente["id"]
            r = self.cliente.get(f"/api/transportadoras/{tid}")
            self.assertEqual(r.status_code, 200, nivel)
            self.assertEqual(len(r.get_json()["parados"]), 1)
            html = self.cliente.get("/transportadoras").get_data(as_text=True)
            self.assertNotIn('data-responder="', html)
            self.assertNotIn('id="botao-nova"', html)
            for url in ("/api/transportadoras", f"/api/transportadoras/{tid}", f"/api/transportadoras/{tid}/responder"):
                self.assertEqual(self.cliente.post(url, json={"nome": "Z", "tipo": "RETIRADA"}, headers=ORIGEM).status_code,
                                 403, (nivel, url))
            self.assertEqual(self.cliente.get("/api/transportadoras/cep/01001000").status_code, 403)

    def test_corpo_que_nao_e_objeto_devolve_400(self):
        self._logar("operador")
        tid = self.pendente["id"]
        for url in ("/api/transportadoras", f"/api/transportadoras/{tid}", f"/api/transportadoras/{tid}/responder"):
            for corpo in ([1, 2], "x", 5):
                self.assertEqual(self.cliente.post(url, json=corpo, headers=ORIGEM).status_code, 400, (url, corpo))
        r = self.cliente.post(f"/api/transportadoras/{tid}/responder", json={"tipo": "RETIRADA", "email": 5}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)

    def test_dias_uteis_e_rotulo_do_embarcador(self):
        import transportadoras_tela as tt
        from datetime import date
        # sexta 16/10/2026 -> segunda 19/10: 1 dia util
        self.assertEqual(tt._dias("2026-10-16T09:00:00", date(2026, 10, 19)), 1)
        self.assertEqual(tt._dias("2026-10-16T09:00:00", date(2026, 10, 20)), 2)
        conn = ct.conectar_consultas(tc.DB_PATH)
        try:
            self.assertEqual(tt.rotulo_embarcador(conn, "12345678000199"), "12.345.678/0001-99")
            self.assertEqual(tt.rotulo_embarcador(conn, "stkkc:77"), "embarcador sem cadastro (stkkc 77)")
        finally:
            conn.close()

    def test_criar_conflito_devolve_400(self):
        self._logar("operador")
        r = self.cliente.post("/api/transportadoras", json={"nome": "ALFA", "tipo": "RETIRADA", "situacao": "ATIVA"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Conflito", r.get_json()["erro"])

    def test_criar_e_editar_pela_tela(self):
        self._logar("logistica")
        r = self.cliente.post("/api/transportadoras", json={"nome": "GAMA", "tipo": "retirada", "situacao": "ATIVA"},
                              headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        nova = r.get_json()
        self.assertEqual((nova["origem"], nova["atualizado_por"], nova["tipo"]), ("TELA", "teste", "RETIRADA"))
        r = self.cliente.post(f"/api/transportadoras/{nova['id']}", json={"email": "a@b.com"}, headers=ORIGEM)
        self.assertEqual(r.get_json()["email"], "a@b.com")
        ficha = self.cliente.get(f"/api/transportadoras/{nova['id']}").get_json()
        self.assertEqual(ficha["historico"][0]["campo"], "email")
        self.assertEqual([c.args[0]["id"] for c in self.liberar.call_args_list], [nova["id"], nova["id"]])

    def test_sem_origem_da_403(self):
        self._logar("total")
        self.assertEqual(self.cliente.post("/api/transportadoras", json={"nome": "Z"}).status_code, 403)

    def test_responder_pela_tela_libera(self):
        self._logar("operador")
        tid = self.pendente["id"]
        ficha = self.cliente.get(f"/api/transportadoras/{tid}").get_json()
        self.assertEqual(len(ficha["parados"]), 1)
        r = self.cliente.post(f"/api/transportadoras/{tid}/responder", json={"tipo": "RETIRADA"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["situacao"], "ATIVA")
        # Hugo 29/10: classificar solta as NFs do portal presas no bloqueio de area
        transp, por = self.liberar.call_args.args
        self.assertEqual((transp["id"], transp["tipo"], por), (tid, "RETIRADA", "teste"))
        conn = ct.conectar_consultas(tc.DB_PATH)
        try:
            self.assertEqual(ct.pedidos_parados(conn, tid), [])
            c = conn.execute("SELECT respondido_por, canal_resposta FROM transportadora_consultas").fetchone()
            self.assertEqual((c[0], c[1]), ("teste", "tela"))
        finally:
            conn.close()
        r = self.cliente.post(f"/api/transportadoras/{tid}/responder", json={"tipo": "RETIRADA"}, headers=ORIGEM)
        self.assertEqual(r.status_code, 400)
        self.assertIn("ja respondida", r.get_json()["erro"])

    def test_ficha_tira_de_pendente_e_encerra_a_consulta(self):
        # Mudar a Situacao pela ficha (sem os botoes de resposta) tambem libera
        # os parados e fecha a consulta: senao o embarcador seguiria recebendo lembrete.
        self._logar("operador")
        for corpo in ({"situacao": "ATIVA", "tipo": "ENTREGA"}, {"situacao": "INATIVA"}):
            conn = ct.conectar_consultas(tc.DB_PATH)
            r = ct.registrar_desconhecida(conn, "DELTA " + corpo["situacao"], "", "PS-" + corpo["situacao"],
                                          "12345678000199", None)
            conn.close()
            tid = r["transportadora"]["id"]
            resp = self.cliente.post(f"/api/transportadoras/{tid}", json=corpo, headers=ORIGEM)
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertEqual(resp.get_json()["situacao"], corpo["situacao"])
            conn = ct.conectar_consultas(tc.DB_PATH)
            try:
                self.assertEqual(ct.pedidos_parados(conn, tid), [])
                c = conn.execute("SELECT respondido_por, canal_resposta FROM transportadora_consultas "
                                 "WHERE transportadora_id=?", (tid,)).fetchone()
                self.assertEqual((c[0], c[1]), ("teste", "tela"))
            finally:
                conn.close()

    def test_aba_pendentes_mostra_resumo(self):
        self._logar("total")
        html = self.cliente.get("/transportadoras?aba=pendentes").get_data(as_text=True)
        self.assertIn("BETA LOG", html)
        self.assertNotIn(">ALFA<", html)
        self.assertIn("A transportadora vem retirar no galp", html)

    def test_cep_usa_consultar_cep(self):
        self._logar("total")
        achado = {"cep": "01001-000", "logradouro": "Praca da Se", "bairro": "Se", "cidade": "Sao Paulo", "uf": "SP"}
        with mock.patch("portal_cliente.cotacao.consultar_cep", return_value=achado):
            r = self.cliente.get("/api/transportadoras/cep/01001000")
        self.assertEqual(r.get_json()["logradouro"], "Praca da Se")
        with mock.patch("portal_cliente.cotacao.consultar_cep", return_value=None):
            self.assertEqual(self.cliente.get("/api/transportadoras/cep/00000000").status_code, 404)

    def test_tela_em_acesso_telas(self):
        self.assertIn("transportadoras", acesso_telas.CHAVES)

    def test_contador_pendentes(self):
        c = contadores_menu.contadores("operador", {"transportadoras": ("operador",)})
        self.assertEqual(c["transportadoras"]["qtd"], 1)
        self.assertEqual(contadores_menu.contadores("atendimento", {"transportadoras": ("operador",)}), {})
        self.assertEqual(contadores_menu.NIVEIS_POR_CONTADOR["transportadoras"], ("total", "operador", "logistica"))


if __name__ == "__main__":
    unittest.main()
