# -*- coding: utf-8 -*-
"""
Consulta de pedido na Expedicao (02/10): regra do veredito, busca nas
duas fontes e rotas do painel.

    py -3.11 -m unittest painel_agentes.test_consulta_expedicao
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import consulta_expedicao as ce  # noqa: E402

SP = ZoneInfo("America/Sao_Paulo")
AGORA = datetime(2026, 10, 2, 10, 0, tzinfo=SP)

STOKKI_AGUARDANDO = {"estado": "ok", "status": "Aguardando Transportador", "transportadora": "FRESHLOG", "retira": False}
STOKKI_ENVIADO = {"estado": "ok", "status": "Enviado", "transportadora": "FRESHLOG", "retira": False}
STOKKI_NAO_CONFERIDA = {"estado": "nao_conferida"}
ROTA_PARADA = {"id": 9, "nome": "Planejamento - 02/10 - #3", "status": "accepted", "iniciada": False,
               "motorista": "Iago Mendes", "placa": "ABC1D23"}
ROTA_RODANDO = {**ROTA_PARADA, "status": "started", "iniciada": True}


class TestNormalizarCodigo(unittest.TestCase):

    def test_formatos_aceitos(self):
        for entrada, esperado in (
            ("38123", "PS-38123"), ("PS-38123", "PS-38123"), ("#PS-38123", "PS-38123"),
            ("ps 38123", "PS-38123"), (" PS.38123 ", "PS-38123"), ("PS-38123-r1", "PS-38123-R1"),
            ("38123-R1-R1", "PS-38123-R1-R1"),
        ):
            self.assertEqual(ce.normalizar_codigo(entrada), esperado, entrada)

    def test_invalidos(self):
        for entrada in ("", "   ", "abc", "123", "PS-", None):
            self.assertIsNone(ce.normalizar_codigo(entrada), entrada)


class TestDecidirVeredito(unittest.TestCase):

    def _v(self, servico, rota=None, stokki=STOKKI_AGUARDANDO):
        return ce.decidir_veredito(servico, rota, stokki, AGORA)

    def test_pool_sem_agendamento(self):
        r = self._v({"status": "not_assigned"})
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("sem rota", r["contexto"].lower())
        self.assertIsNone(r["divergencia"])

    def test_pool_agendado(self):
        r = self._v({"status": "not_assigned", "scheduled_start": "2026-10-05 08:00:00"})
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("05/10", r["contexto"])

    def test_atribuido_ao_agente_de_retirada_espera_no_galpao(self):
        # Achado no teste real (PS-40637): 'assigned' sem rota, driver_id = agente de retirada
        r = ce.decidir_veredito({"status": "assigned", "driver_id": 50259}, None, STOKKI_AGUARDANDO, AGORA,
                                agente_retirada=50259)
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("retirada", r["contexto"].lower())
        self.assertNotIn("pool", r["contexto"].lower())

    def test_retirada_pelo_titulo_em_on_route_continua_no_galpao(self):
        r = self._v({"status": "on_route", "title": "[RETIRADA] #PS-1 - X"})
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("retirada", r["contexto"].lower())

    def test_retirada_fechada_sem_stokki_e_retirado(self):
        r = ce.decidir_veredito({"status": "done", "status_done": "success", "driver_id": "50259",
                                 "completed_at": "2026-10-02 12:00:00"}, None, STOKKI_NAO_CONFERIDA, AGORA,
                                agente_retirada=50259)
        self.assertEqual(r["veredito"], "retirado")

    def test_situacao_stokki_conhecida_de_galpao(self):
        r = self._v(None, stokki={"estado": "ok", "status": "Em Conferência", "transportadora": "", "retira": False})
        self.assertEqual(r["veredito"], "galpao")

    def test_situacao_stokki_desconhecida_sem_vuupt_nao_vira_galpao(self):
        r = self._v(None, stokki={"estado": "ok", "status": "Finalizado", "transportadora": "", "retira": False})
        self.assertEqual(r["veredito"], "nao_encontrado")
        self.assertIn("Finalizado", r["contexto"])

    def test_situacao_stokki_desconhecida_com_vuupt_no_pool_e_divergencia(self):
        r = self._v({"status": "not_assigned"},
                    stokki={"estado": "ok", "status": "Finalizado", "transportadora": "", "retira": False})
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("Finalizado", r["divergencia"])

    def test_rota_nao_iniciada_mostra_rota_e_motorista(self):
        r = self._v({"status": "accepted", "route_id": 9}, ROTA_PARADA)
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("#3", r["contexto"])
        self.assertIn("Iago Mendes", r["contexto"])

    def test_on_route_saiu(self):
        r = self._v({"status": "on_route", "route_id": 9}, ROTA_RODANDO)
        self.assertEqual(r["veredito"], "saiu")
        self.assertIn("ABC1D23", r["contexto"])

    def test_rota_antiga_ja_nao_parada_conta_como_saiu(self):
        r = self._v({"status": "accepted", "route_id": 9}, {**ROTA_PARADA, "status": "finished", "iniciada": True})
        self.assertEqual(r["veredito"], "saiu")

    def test_entregue(self):
        r = self._v({"status": "done", "status_done": "success", "completed_at": "2026-10-02 12:30:00"},
                    stokki=STOKKI_ENVIADO)
        self.assertEqual(r["veredito"], "entregue")
        self.assertIn("02/10 09:30", r["contexto"])  # completed_at vem em UTC
        self.assertIsNone(r["divergencia"])

    def test_insucesso_deveria_ter_voltado(self):
        with mock.patch.object(ce, "texto_do_motivo", return_value="Cliente ausente"):
            r = self._v({"status": "done", "status_done": "failed", "failed_reason_id": 7,
                         "completed_at": "2026-10-01 18:00:00"})
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("insucesso", r["contexto"].lower())
        self.assertIn("Cliente ausente", r["contexto"])

    def test_cancelado_na_stokki_vence_tudo(self):
        r = self._v({"status": "on_route"}, ROTA_RODANDO,
                    {"estado": "ok", "status": "Cancelado", "transportadora": "", "retira": False})
        self.assertEqual(r["veredito"], "cancelado")

    def test_cancelado_na_vuupt(self):
        r = self._v({"status": "canceled"})
        self.assertEqual(r["veredito"], "cancelado")

    def test_retirado(self):
        r = self._v({"status": "done", "status_done": "success"}, stokki={
            "estado": "ok", "status": "Enviado", "transportadora": "CLIENTE RETIRA", "retira": True})
        self.assertEqual(r["veredito"], "retirado")
        self.assertIn("CLIENTE RETIRA", r["contexto"])

    def test_sem_vuupt_stokki_aguardando(self):
        r = self._v(None)
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("roteiriza", r["contexto"].lower())

    def test_sem_vuupt_stokki_enviado_por_transportadora(self):
        r = self._v(None, stokki={"estado": "ok", "status": "Enviado", "transportadora": "JADLOG", "retira": False})
        self.assertEqual(r["veredito"], "saiu")
        self.assertIn("JADLOG", r["contexto"])

    def test_nao_encontrado(self):
        r = self._v(None, stokki={"estado": "inexistente"})
        self.assertEqual(r["veredito"], "nao_encontrado")

    def test_sem_vuupt_e_stokki_nao_conferida(self):
        r = self._v(None, stokki=STOKKI_NAO_CONFERIDA)
        self.assertEqual(r["veredito"], "nao_encontrado")
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)

    def test_divergencia_pool_mas_stokki_enviado(self):
        r = self._v({"status": "not_assigned"}, stokki=STOKKI_ENVIADO)
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("Enviado", r["divergencia"])

    def test_divergencia_entregue_ha_mais_de_24h_sem_expedir(self):
        r = self._v({"status": "done", "status_done": "success", "completed_at": "2026-09-30 15:00:00"})
        self.assertEqual(r["veredito"], "entregue")
        self.assertIn("Aguardando Transportador", r["divergencia"])

    def test_entregue_ha_pouco_sem_expedir_nao_e_divergencia(self):
        r = self._v({"status": "done", "status_done": "success", "completed_at": "2026-10-02 11:00:00"})
        self.assertIsNone(r["divergencia"])

    def test_stokki_nao_conferida_decide_pela_vuupt_com_aviso(self):
        r = self._v({"status": "accepted", "route_id": 9}, ROTA_PARADA, STOKKI_NAO_CONFERIDA)
        self.assertEqual(r["veredito"], "galpao")
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)
        self.assertIn("não conferida", r["stokki_bruto"])

    def test_linhas_brutas(self):
        r = self._v({"status": "accepted", "route_id": 9}, ROTA_PARADA)
        self.assertEqual(r["stokki_bruto"], "Stokki: Aguardando Transportador · FRESHLOG")
        self.assertIn("accepted", r["vuupt_bruto"])
        self.assertIn("#3", r["vuupt_bruto"])
        self.assertEqual(r["titulo"], ce.TITULOS["galpao"])


class _VuuptFalso:
    def __init__(self, servico=None, erro=None):
        self.servico, self.erro, self.codigos = servico, erro, []

    def buscar_servico_por_code(self, codigo):
        self.codigos.append(codigo)
        if self.erro:
            raise self.erro
        return self.servico


class TestConsultar(unittest.TestCase):

    def setUp(self):
        self.vuupt = _VuuptFalso({"id": 1, "status": "accepted", "route_id": 9})
        self.sessao_real = getattr(ce, "_SessaoSemLogin", None)  # antes do patch abaixo
        patches = [
            mock.patch.object(ce.triagem, "_vuupt", side_effect=lambda: self.vuupt),
            mock.patch.object(ce.triagem, "_servico_mais_recente_da_cadeia", side_effect=lambda v, s: s),
            mock.patch.object(ce, "_carregar_config", return_value={"vuupt_api": {"token": "t"}}),
            mock.patch.object(ce, "buscar_rota", return_value={"route": {
                "id": 9, "name": "Planejamento - 02/10 - #3", "status": "accepted", "agent_id": 50191}}),
            mock.patch.object(ce, "_catalogo_motoristas", return_value={
                50191: mock.Mock(nome="Iago Mendes", placa="ABC1D23")}),
            mock.patch.object(ce.triagem, "_painel_tem_execucao_rodando", return_value=False),
            mock.patch.object(ce, "_SessaoSemLogin", return_value=object()),
            mock.patch.object(ce.triagem, "_status_e_transportadora_stokki",
                              return_value={"status": "Aguardando Transportador", "transportadora": "FRESHLOG"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_fluxo_completo_rota_parada(self):
        r = ce.consultar(" ps 38123 ")
        self.assertEqual(r["codigo"], "PS-38123")
        self.assertEqual(self.vuupt.codigos, ["PS-38123"])
        self.assertEqual(r["veredito"], "galpao")
        self.assertIn("Iago Mendes", r["contexto"])
        self.assertIsNone(r["aviso"])

    def test_stokki_recebe_so_os_digitos(self):
        ce.consultar("PS-38123-R1")
        self.assertEqual(ce.triagem._status_e_transportadora_stokki.call_args.args[1], "38123")

    def test_codigo_invalido(self):
        with self.assertRaises(ValueError):
            ce.consultar("abc")

    def test_vuupt_fora(self):
        self.vuupt.erro = RuntimeError("timeout")
        with self.assertRaises(ce.ConsultaIndisponivel):
            ce.consultar("38123")

    def test_agente_rodando_nao_abre_stokki(self):
        with mock.patch.object(ce.triagem, "_painel_tem_execucao_rodando", return_value=True):
            r = ce.consultar("38123")
        ce.triagem._status_e_transportadora_stokki.assert_not_called()
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)
        self.assertEqual(r["veredito"], "galpao")

    def test_lock_ocupado_nao_abre_stokki(self):
        ce.triagem._lock_consulta_stokki.acquire()
        try:
            r = ce.consultar("38123")
        finally:
            ce.triagem._lock_consulta_stokki.release()
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)

    def test_erro_na_stokki_vira_nao_conferida_e_solta_o_lock(self):
        with mock.patch.object(ce.triagem, "_status_e_transportadora_stokki", side_effect=RuntimeError("401")):
            r = ce.consultar("38123")
        self.assertEqual(r["aviso"], ce.AVISO_STOKKI_NAO_CONFERIDA)
        self.assertTrue(ce.triagem._lock_consulta_stokki.acquire(blocking=False))
        ce.triagem._lock_consulta_stokki.release()

    def test_stokki_inexistente(self):
        self.vuupt.servico = None
        with mock.patch.object(ce.triagem, "_status_e_transportadora_stokki", return_value=None):
            r = ce.consultar("38123")
        self.assertEqual(r["veredito"], "nao_encontrado")

    def test_retira_so_consulta_catalogo_quando_enviado(self):
        with mock.patch.object(ce.triagem, "_catalogo_transportadoras") as cat:
            ce.consultar("38123")
        cat.assert_not_called()

    def test_enviado_cliente_retira(self):
        self.vuupt.servico = {"id": 1, "status": "done", "status_done": "success"}
        with mock.patch.object(ce.triagem, "_status_e_transportadora_stokki",
                               return_value={"status": "Enviado", "transportadora": "CLIENTE RETIRA"}), \
             mock.patch.object(ce.triagem, "_catalogo_transportadoras", return_value=None):
            r = ce.consultar("38123")
        self.assertEqual(r["veredito"], "retirado")

    def test_agente_de_retirada_vem_do_config(self):
        self.vuupt.servico = {"id": 1, "status": "assigned", "driver_id": 50259}
        with mock.patch.object(ce, "_carregar_config", return_value={"retiradas": {"agent_id": 50259}}):
            r = ce.consultar("38123")
        self.assertIn("retirada", r["contexto"].lower())

    def test_sessao_da_consulta_nunca_faz_login(self):
        # Achado da revisao (I1): sem cookie valido, nada de Playwright dentro do painel
        import stokki.auth as auth
        with mock.patch.object(auth, "COOKIES_PATH", _RAIZ / "nao_existe.json"),              mock.patch.object(auth.StokkiSession, "_fazer_login_playwright") as login:
            with self.assertRaises(auth.SessaoExpiradaError):
                self.sessao_real({"stokki": {"usuario": "u", "senha": "s"}})
        login.assert_not_called()

    def test_servico_sem_rota_nao_busca_rota(self):
        self.vuupt.servico = {"id": 1, "status": "not_assigned"}
        ce.consultar("38123")
        ce.buscar_rota.assert_not_called()


import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

CONFIG_PAINEL = {
    "usuario": "u_total", "senha": "s_total",
    "usuario_operador": "u_op", "senha_operador": "s_op",
    "usuario_leitura": "u_le", "senha_leitura": "s_le",
    "usuario_expedicao": "u_ex", "senha_expedicao": "s_ex",
    "usuario_galpao": "u_ga", "senha_galpao": "s_ga",
    "usuario_atendimento": "u_at", "senha_atendimento": "s_at",
}
RESULTADO = {"codigo": "PS-38123", "veredito": "galpao", "titulo": "Deveria estar no galpão",
             "contexto": "Sem rota", "divergencia": None, "aviso": None,
             "stokki_bruto": "Stokki: Em espera", "vuupt_bruto": "Vuupt: not_assigned"}


class _BaseApp(unittest.TestCase):
    def setUp(self):
        config = {**painel_agentes._carregar_config(), "painel_agentes": CONFIG_PAINEL}
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=config)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"


class TestTelaConsulta(_BaseApp):

    def test_abre_para_os_quatro_niveis(self):
        for nivel in ("total", "operador", "expedicao", "galpao"):
            self._logar(nivel)
            r = self.cliente.get("/expedicao/consulta")
            self.assertEqual(r.status_code, 200, nivel)
            self.assertIn('id="codigo"', r.get_data(as_text=True))
            self.assertIn("AbortController", r.get_data(as_text=True))  # timeout no celular (I1)

    def test_403_para_leitura_e_atendimento(self):
        for nivel in ("leitura", "atendimento"):
            self._logar(nivel)
            self.assertEqual(self.cliente.get("/expedicao/consulta").status_code, 403, nivel)

    def test_menu_mostra_item_para_expedicao(self):
        self._logar("expedicao")
        html = self.cliente.get("/expedicao/consulta").get_data(as_text=True)
        self.assertIn(">Consultar pedido<", html)
        self.assertIn('aria-current="page"', html)

    def test_menu_esconde_item_de_leitura(self):
        self._logar("leitura")
        # /expedicao busca as rotas na Vuupt: sem o patch o teste iria à rede
        with mock.patch.object(painel_agentes, "listar_rotas_do_dia", return_value=[]):
            html = self.cliente.get("/expedicao").get_data(as_text=True)
        self.assertNotIn(">Consultar pedido<", html)


class TestApiConsulta(_BaseApp):

    def test_devolve_o_resultado(self):
        self._logar("galpao")
        with mock.patch.object(painel_agentes.consulta_expedicao, "consultar", return_value=RESULTADO) as m:
            r = self.cliente.get("/api/expedicao/consulta?codigo=38123")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), RESULTADO)
        m.assert_called_once_with("38123")

    def test_codigo_invalido_400(self):
        self._logar("expedicao")
        with mock.patch.object(painel_agentes.consulta_expedicao, "consultar", side_effect=ValueError("Código inválido")):
            r = self.cliente.get("/api/expedicao/consulta?codigo=abc")
        self.assertEqual(r.status_code, 400)
        self.assertIn("inválido", r.get_json()["erro"])

    def test_vuupt_fora_503(self):
        self._logar("expedicao")
        with mock.patch.object(painel_agentes.consulta_expedicao, "consultar",
                               side_effect=painel_agentes.consulta_expedicao.ConsultaIndisponivel("Vuupt indisponível")):
            r = self.cliente.get("/api/expedicao/consulta?codigo=38123")
        self.assertEqual(r.status_code, 503)

    def test_401_sem_sessao(self):
        self.assertEqual(self.cliente.get("/api/expedicao/consulta?codigo=1").status_code, 401)

    def test_403_para_leitura(self):
        self._logar("leitura")
        self.assertEqual(self.cliente.get("/api/expedicao/consulta?codigo=38123").status_code, 403)


if __name__ == "__main__":
    unittest.main()
