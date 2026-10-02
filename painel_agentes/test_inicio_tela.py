# -*- coding: utf-8 -*-
"""
Rotas da pagina inicial do painel (/inicio e /api/inicio/dados) e o
destino do login por nivel (29/09).

    py -3.11 -m unittest painel_agentes.test_inicio_tela
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# "painel_agentes" e o nome da pasta: o modulo do app precisa ser carregado pelo caminho
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes  # o Flask acha a pasta templates/ pelo modulo registrado
_spec.loader.exec_module(painel_agentes)

CONFIG_PAINEL = {
    "usuario": "u_total", "senha": "s_total",
    "usuario_operador": "u_op", "senha_operador": "s_op",
    "usuario_leitura": "u_le", "senha_leitura": "s_le",
    "usuario_expedicao": "u_ex", "senha_expedicao": "s_ex",
    "usuario_galpao": "u_ga", "senha_galpao": "s_ga",
    "usuario_atendimento": "u_at", "senha_atendimento": "s_at",
}

DADOS_FALSOS = {
    "pendencias": [{"chave": "torre", "rotulo": "Torre de Controle", "url": "/torre",
                    "apoio": "ocorrencias", "apoio_critico": "criticas", "qtd": 2, "criticas": 1}],
    "operacao": None,
}


class _Base(unittest.TestCase):
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


class TestTelaInicio(_Base):

    def test_abre_para_os_quatro_niveis(self):
        for nivel in ("total", "operador", "leitura", "atendimento"):
            self._logar(nivel)
            r = self.cliente.get("/inicio")
            self.assertEqual(r.status_code, 200, nivel)
            self.assertIn("Esperando voc", r.get_data(as_text=True))

    def test_403_para_expedicao_e_galpao(self):
        for nivel in ("expedicao", "galpao"):
            self._logar(nivel)
            self.assertEqual(self.cliente.get("/inicio").status_code, 403, nivel)

    def test_bloco_rotinas_so_no_html_do_total(self):
        self._logar("total")
        self.assertIn('id="rotinas"', self.cliente.get("/inicio").get_data(as_text=True))
        self._logar("operador")
        self.assertNotIn('id="rotinas"', self.cliente.get("/inicio").get_data(as_text=True))

    def test_menu_mostra_inicio(self):
        self._logar("leitura")
        html = self.cliente.get("/inicio").get_data(as_text=True)
        self.assertIn('aria-current="page"', html)
        self.assertIn(">Início<", html)


class TestApiInicioDados(_Base):

    def test_passa_nivel_e_url_for_para_a_montagem(self):
        self._logar("atendimento")
        with mock.patch.object(painel_agentes.pagina_inicial, "montar_dados",
                               return_value=DADOS_FALSOS) as m:
            r = self.cliente.get("/api/inicio/dados")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), DADOS_FALSOS)
        nivel, url_de = m.call_args.args
        self.assertEqual(nivel, "atendimento")
        with painel_agentes.app.test_request_context():
            self.assertEqual(url_de("torre"), "/torre")
            self.assertEqual(url_de("execucao", execucao_id=7), "/execucao/7")

    def test_401_sem_sessao(self):
        self.assertEqual(self.cliente.get("/api/inicio/dados").status_code, 401)

    def test_403_para_expedicao(self):
        self._logar("expedicao")
        self.assertEqual(self.cliente.get("/api/inicio/dados").status_code, 403)


class TestDestinoDoLogin(_Base):

    def _destino(self, usuario, senha):
        r = self.cliente.post("/login", data={"usuario": usuario, "senha": senha})
        self.assertEqual(r.status_code, 302)
        return r.headers["Location"]

    def test_niveis_do_painel_caem_no_inicio(self):
        for usuario, senha in (("u_total", "s_total"), ("u_op", "s_op"), ("u_le", "s_le"), ("u_at", "s_at")):
            self.assertTrue(self._destino(usuario, senha).endswith("/inicio"), usuario)

    def test_expedicao_e_galpao_continuam_na_propria_tela(self):
        self.assertTrue(self._destino("u_ex", "s_ex").endswith("/expedicao"))
        self.assertTrue(self._destino("u_ga", "s_ga").endswith("/wms"))

    def test_proximo_continua_vencendo(self):
        r = self.cliente.post("/login", data={"usuario": "u_op", "senha": "s_op", "proximo": "/planejamento"})
        self.assertTrue(r.headers["Location"].endswith("/planejamento"))


if __name__ == "__main__":
    unittest.main()
