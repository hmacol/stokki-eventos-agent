# -*- coding: utf-8 -*-
"""
Rotas do portal para o cliente alterar endereco, data e horario de um pedido
(GET/POST /api/pedidos/<id>/alteracao) e a flag em /api/dia.

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

    def _fakes(self, servico=None, nucleo=None, area=()):
        deps = mock.Mock(name="deps")
        aplicar = self.aplicar = mock.Mock(return_value={"ok": True, "mensagem": "Alteração feita.", "feito": []})
        return [
            mock.patch.object(self.portal.dados, "buscar_servico", return_value=servico or _servico()),
            mock.patch.object(self.portal.dados, "invalidar_cache"),
            mock.patch.object(self.portal.cancelamento, "servicos_do_pedido", return_value=nucleo or _nucleo()),
            mock.patch.object(self.portal.alterar_entrega, "dependencias_reais", return_value=deps),
            mock.patch.object(self.portal.alterar_entrega, "aplicar", aplicar),
            mock.patch.object(self.portal.envios, "_identificar_area", return_value=list(area)),
            mock.patch.object(self.portal, "_mapa_horarios", return_value={}),
            mock.patch("regras.complexidade_entrega.carregar_ajustes_manuais", return_value={}),
            mock.patch("registro_dia_fixo.conectar", return_value=self.conn),
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

    def test_post_em_rota_pede_confirmacao_e_depois_aplica(self):
        fakes = self._fakes()
        aplicar = self.aplicar
        self._com(fakes)
        r = self.cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA}, base_url=HTTPS)
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(j["ok"])
        self.assertTrue(j["precisa_confirmar"])
        self.assertEqual([a["codigo"] for a in j["avisos"]], ["atraso"])
        aplicar.assert_not_called()
        r = self.cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA, "confirmar": True}, base_url=HTTPS)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        aplicar.assert_called_once()
        contexto = aplicar.call_args.args[4]
        self.assertEqual((contexto["cnpj_embarcador"], contexto["sender_id"], contexto["por"]), (PRINCIPAL, 100, "cliente"))
        self.assertEqual((contexto["destinatario_doc"], contexto["destinatario_nome"]), ("12345678000190", "Loja X"))
        self.portal.dados.invalidar_cache.assert_called_with(100)

    def test_post_so_horario_aplica_direto_sem_aviso(self):
        fakes = self._fakes()
        aplicar = self.aplicar
        self._com(fakes)
        r = self.cli.post("/api/pedidos/10/alteracao", json={"horario": {"inicio": "08:00", "fim": "12:00"}}, base_url=HTTPS)
        self.assertTrue(r.get_json()["ok"])
        aplicar.assert_called_once()

    def test_post_bloqueado_409(self):
        fakes = self._fakes(servico=_servico(status="done"))
        aplicar = self.aplicar
        self._com(fakes)
        r = self.cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA, "confirmar": True}, base_url=HTTPS)
        self.assertEqual(r.status_code, 409)
        aplicar.assert_not_called()

    def test_post_invalido_400(self):
        self._com(self._fakes())
        r = self.cli.post("/api/pedidos/10/alteracao", json={"data": "2020-01-01"}, base_url=HTTPS)
        self.assertEqual(r.status_code, 400)

    def test_post_falha_do_aplicar_502(self):
        fakes = self._fakes()
        aplicar = self.aplicar
        self._com(fakes)
        aplicar.return_value = {"ok": False, "mensagem": "Não conseguimos concluir.", "feito": []}
        r = self.cli.post("/api/pedidos/10/alteracao", json={"horario": {"inicio": "08:00", "fim": "12:00"}}, base_url=HTTPS)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()["erro"], "Não conseguimos concluir.")

    def test_post_endereco_fora_da_area_400(self):
        fakes = self._fakes(area=[({"id": 0}, "sp_nao_atendido")])
        aplicar = self.aplicar
        self._com(fakes)
        corpo = {"endereco": {"cep": "01310-100", "numero": "10"}, "confirmar": True}
        with mock.patch("cotacao.consultar_cep", return_value=CEP_OK):
            r = self.cli.post("/api/pedidos/10/alteracao", json=corpo, base_url=HTTPS)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"],
                         "Esse endereço fica fora da área atendida. Fale com a Fresh Log pelo chat do portal.")
        aplicar.assert_not_called()

    def test_post_endereco_dentro_da_area_aplica(self):
        fakes = self._fakes()
        aplicar = self.aplicar
        self._com(fakes)
        corpo = {"endereco": {"cep": "01310-100", "numero": "10"}, "confirmar": True}
        with mock.patch("cotacao.consultar_cep", return_value=CEP_OK):
            r = self.cli.post("/api/pedidos/10/alteracao", json=corpo, base_url=HTTPS)
        self.assertEqual(r.status_code, 200)
        aplicar.assert_called_once()

    def test_post_equipe_so_leitura_403(self):
        fakes = self._fakes()
        aplicar = self.aplicar
        self._com(fakes)
        cli = self._equipe_leitura()
        r = cli.post("/api/pedidos/10/alteracao", json={"data": AMANHA, "confirmar": True}, base_url=HTTPS)
        self.assertEqual(r.status_code, 403)
        aplicar.assert_not_called()

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
