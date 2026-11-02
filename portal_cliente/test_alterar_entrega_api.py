# -*- coding: utf-8 -*-
"""
Rotas do portal para o cliente pedir alteracao de endereco, data e horario de
um pedido (GET/POST /api/pedidos/<id>/alteracao; o POST vira chamado pro
atendimento) e a flag em /api/dia.

    py -3.11 -m unittest portal_cliente.test_alterar_entrega_api
"""
import builtins
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

import auth_cliente as auth  # noqa: E402
import alterar_entrega as ae  # noqa: E402

PRINCIPAL, MEMBRO_A, AVULSO = "54993021000184", "48654566000163", "22310186000118"
HTTPS = "https://localhost"
CEP_OK = {"cep": "01310100", "logradouro": "Avenida Paulista", "bairro": "Bela Vista", "cidade": "Sao Paulo", "uf": "SP", "ibge": "3550308"}
AMANHA = (date.today() + timedelta(days=1)).isoformat()


def _servico(**extra):
    base = {"id": 10, "sender_id": 100, "code": "#PS-1234", "status": "assigned", "route_id": 55,
            "address": "Rua A, 1, Sao Paulo - SP, 01000-000, Brasil", "scheduled_start": "2026-11-05 12:00:00",
            "customer_id": 9, "customer": {"code": "12.345.678/0001-90", "name": "Loja X"}}
    base.update(extra)
    return base


def _nucleo(rota_status="PLANEJADA"):
    servicos = [{"codigo": "PS-1234", "vuupt_service_id": 10, "status": "ROTEIRIZADO", "vuupt_route_id": 55}]
    return servicos, {55: {"vuupt_route_id": 55, "status": rota_status, "status_provedor": None,
                           "iniciada_em": None, "agent_id": 1}}


class AlterarEntregaApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        real_open = builtins.open

        def abrir(arq, *a, **k):  # worktree sem config.yaml (segredo): so o import do app ve um de teste
            if str(arq).endswith("config.yaml") and not Path(arq).exists():
                return io.StringIO("portal_cliente: {secret_key: teste-teste-teste-teste-teste-teste-1234}")
            return real_open(arq, *a, **k)

        try:
            with mock.patch.object(builtins, "open", abrir):
                import app as portal
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(f"app do portal nao importa neste ambiente ({e})")
        cls.portal = portal

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = Path(self._tmp.name) / "dados.db"
        self._patches = [mock.patch.object(auth, "DB_PATH", db),
                         mock.patch.object(self.portal.envios, "DB_PATH", db),
                         mock.patch.dict(self.portal._CONFIG, {
                             "portal_cliente": {"secret_key": "x", "alteracao_entrega": {"ativo": True}}})]
        for p in self._patches:
            p.start()
        self.conn = auth.conectar()
        self.conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                          "apelido TEXT, email TEXT)")
        self.conn.executemany("INSERT INTO interno VALUES (?, ?, ?, NULL, ?)", [
            (PRINCIPAL, 100, "MARCHEF", "a@b.c"), (MEMBRO_A, 101, "ITAUEIRA", "a@b.c"), (AVULSO, 200, "VIDAVEG", "a@b.c")])
        self.conn.commit()
        auth.definir_grupo(self.conn, PRINCIPAL, [MEMBRO_A])
        self.cli = self._logar(PRINCIPAL)

    def tearDown(self):
        self.conn.close()
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _logar(self, cnpj):
        conta = auth.definir_pin(self.conn, cnpj, "123456")
        cli = self.portal.app.test_client()
        with cli.session_transaction(base_url=HTTPS) as s:
            s["cnpj"], s["v"] = cnpj, auth.versao_conta(conta)
        return cli

    def _equipe_leitura(self):
        cli = self.portal.app.test_client()
        with cli.session_transaction(base_url=HTTPS) as s:
            s["equipe"] = {"usuario": "ana", "nivel": "leitura"}
            s["cnpj_equipe"] = PRINCIPAL
        return cli

    def _fakes(self, servico=None, nucleo=None):
        self.abrir = mock.Mock(return_value=77)
        return [
            mock.patch.object(self.portal.dados, "buscar_servico", return_value=servico or _servico()),
            mock.patch.object(self.portal.cancelamento, "servicos_do_pedido", return_value=nucleo or _nucleo()),
            mock.patch.object(self.portal.alterar_entrega, "abrir_chamado", self.abrir),
            mock.patch.object(self.portal, "_mapa_horarios", return_value={}),
            mock.patch("regras.complexidade_entrega.carregar_ajustes_manuais", return_value={}),
        ]

    def _com(self, patches):
        stack = []
        for p in patches:
            stack.append(p.start())
            self.addCleanup(p.stop)
        return stack

    def test_chave_desligada_404(self):
        self._com(self._fakes())
        with mock.patch.dict(self.portal._CONFIG, {"portal_cliente": {"secret_key": "x"}}):
            self.assertEqual(self.cli.get("/api/pedidos/10/alteracao", base_url=HTTPS).status_code, 404)
            r = self.cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA}, base_url=HTTPS)
            self.assertEqual(r.status_code, 404)

    def test_servico_de_outro_cliente_404(self):
        self._com(self._fakes(servico=_servico(sender_id=999)))
        self.assertEqual(self.cli.get("/api/pedidos/10/alteracao", base_url=HTTPS).status_code, 404)
        self.assertEqual(self.cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA}, base_url=HTTPS).status_code, 404)

    def test_servico_de_outra_empresa_do_grupo_ok(self):
        self._com(self._fakes(servico=_servico(sender_id=101)))
        r = self.cli.get("/api/pedidos/10/alteracao", base_url=HTTPS)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["pode"])

    def test_chave_so_numa_empresa_do_grupo_vale_pra_empresa_do_servico(self):
        self._com(self._fakes(servico=_servico(sender_id=101)))
        cfg = {"portal_cliente": {"secret_key": "x", "alteracao_entrega": {"ativo": False, "piloto_cnpjs": [PRINCIPAL]}}}
        with mock.patch.dict(self.portal._CONFIG, cfg):
            self.assertEqual(self.cli.get("/api/pedidos/10/alteracao", base_url=HTTPS).status_code, 404)

    def test_get_mostra_o_atual(self):
        self._com(self._fakes())
        j = self.cli.get("/api/pedidos/10/alteracao", base_url=HTTPS).get_json()
        self.assertTrue(j["pode"])
        self.assertTrue(j["em_rota"])
        self.assertEqual(j["destinatario"], "Loja X")
        self.assertEqual(j["atual"]["data"], "2026-11-05")
        self.assertEqual(j["atual"]["horario"], {"inicio": "00:00", "fim": "23:59"})
        self.assertIn("Rua A", j["atual"]["endereco"])

    def test_get_bloqueado_mostra_motivo(self):
        self._com(self._fakes(servico=_servico(status="on_route")))
        j = self.cli.get("/api/pedidos/10/alteracao", base_url=HTTPS).get_json()
        self.assertFalse(j["pode"])
        self.assertIn("Fale com a Fresh Log pelo chat do portal.", j["motivo"])

    def test_post_abre_chamado_com_o_pedido_e_nao_mexe_na_vuupt(self):
        self._com(self._fakes())
        corpo = {"endereco": {"cep": "01310-100", "numero": "10"}, "data": AMANHA,
                 "horario": {"inicio": "08:00", "fim": "12:00"}}
        with mock.patch("cotacao.consultar_cep", return_value=CEP_OK), \
                mock.patch.object(self.portal.dados, "invalidar_cache") as invalidar:
            r = self.cli.post("/api/pedidos/10/alteracao", json=corpo, base_url=HTTPS)
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual((j["ok"], j["chamado_id"]), (True, 77))
        self.assertIn("chamado #77", j["mensagem"])
        cliente, codigo, texto, _cfg = self.abrir.call_args.args
        self.assertEqual(cliente, {"cnpj": PRINCIPAL, "sender_id": 100, "nome": "MARCHEF"})
        self.assertEqual(codigo, "PS-1234")
        self.assertTrue(texto.startswith("Quero alterar a entrega do pedido PS-1234 (Loja X):"))
        self.assertIn("Avenida Paulista, 10", texto)
        self.assertIn("- Horário de recebimento: 08:00 às 12:00 (hoje: dia todo)", texto)
        invalidar.assert_not_called()

    def test_post_empresa_do_grupo_chamado_no_cnpj_do_login(self):
        self._com(self._fakes(servico=_servico(sender_id=101)))
        r = self.cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA}, base_url=HTTPS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.abrir.call_args.args[0], {"cnpj": PRINCIPAL, "sender_id": 101, "nome": "ITAUEIRA"})

    def test_post_bloqueado_409(self):
        self._com(self._fakes(servico=_servico(status="done")))
        r = self.cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA}, base_url=HTTPS)
        self.assertEqual(r.status_code, 409)
        self.abrir.assert_not_called()

    def test_post_invalido_400(self):
        self._com(self._fakes())
        r = self.cli.post("/api/pedidos/10/alteracao", json={"data": "2020-01-01"}, base_url=HTTPS)
        self.assertEqual(r.status_code, 400)
        self.abrir.assert_not_called()

    def test_post_chamado_que_falha_502_sem_detalhe(self):
        self._com(self._fakes())
        self.abrir.side_effect = RuntimeError("boom")
        r = self.cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA}, base_url=HTTPS)
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("boom", r.get_json()["erro"])

    def test_post_corpo_que_nao_e_objeto_400(self):
        self._com(self._fakes())
        r = self.cli.post("/api/pedidos/10/alteracao", json=[1], base_url=HTTPS)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "Nada foi alterado.")

    def test_get_com_falha_interna_devolve_json_502(self):
        self._com(self._fakes())
        with mock.patch.object(self.portal.cancelamento, "servicos_do_pedido", side_effect=RuntimeError("boom")):
            r = self.cli.get("/api/pedidos/10/alteracao", base_url=HTTPS)
        self.assertEqual(r.status_code, 502)
        self.assertTrue(r.get_json()["erro"].endswith("Fale com a Fresh Log pelo chat do portal."))
        self.assertNotIn("boom", r.get_json()["erro"])

    def test_post_de_outra_origem_403(self):
        self._com(self._fakes())
        r = self.cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA}, base_url=HTTPS,
                          headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.abrir.assert_not_called()

    def test_post_equipe_403(self):
        self._com(self._fakes())
        r = self._equipe_leitura().post("/api/pedidos/10/alteracao", json={"data": AMANHA}, base_url=HTTPS)
        self.assertEqual(r.status_code, 403)
        self.abrir.assert_not_called()

    def test_api_dia_informa_chave(self):
        with mock.patch.object(self.portal.dados, "montar_dia", return_value={"pedidos": []}):
            self.assertTrue(self.cli.get("/api/dia", base_url=HTTPS).get_json()["alteracao_entrega"])
            with mock.patch.dict(self.portal._CONFIG, {"portal_cliente": {"secret_key": "x"}}):
                self.assertFalse(self.cli.get("/api/dia", base_url=HTTPS).get_json()["alteracao_entrega"])
            cfg = {"portal_cliente": {"secret_key": "x", "alteracao_entrega": {"piloto_cnpjs": [MEMBRO_A]}}}
            with mock.patch.dict(self.portal._CONFIG, cfg):   # basta UMA empresa do grupo habilitada
                self.assertTrue(self.cli.get("/api/dia", base_url=HTTPS).get_json()["alteracao_entrega"])


if __name__ == "__main__":
    unittest.main()
