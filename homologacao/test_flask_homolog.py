import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask, jsonify

from homologacao import ambiente, flask_homolog, guarda, registro


def _app(requer_auth=None, servico="painel"):
    app = Flask(__name__)

    @app.route("/pagina")
    def pagina():
        return "<html><head><title>Painel</title></head><body class='x'><p>oi</p></body></html>"

    @app.route("/dados")
    def dados():
        return jsonify(ok=True)

    @app.route("/api/desligado")
    def api_desligado():
        raise ambiente.DesligadoNaHomologacao("X desligado na homologação")

    @app.route("/tela/bloqueado")
    def tela_bloqueado():
        raise guarda.EnvioBloqueadoHomolog("indisponível na homologação: POST x")

    flask_homolog.instalar(app, servico=servico, requer_auth=requer_auth)
    return app


class TestFlaskHomolog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for p in (mock.patch.object(registro, "CAMINHO_BANCO", Path(self.dir.name) / "d.db"),
                  mock.patch.object(guarda, "INSTALADA", True),
                  mock.patch.object(guarda, "CAMADAS", ["smtp", "requests"]),
                  mock.patch.object(ambiente, "informacao_versao",
                                    return_value=dict(ramo="homologacao", commit="abcdef1234", master="m1",
                                                      quando="q", copia="10/11"))):
            p.start()
            self.addCleanup(p.stop)

    def test_faixa_entra_no_html(self):
        r = _app().test_client().get("/pagina")
        corpo = r.get_data(as_text=True)
        self.assertIn("faixa-homologacao", corpo)
        self.assertIn("<title>[HOMOLOG] Painel", corpo)
        self.assertIn("homologacao @ abcdef1", corpo)
        self.assertEqual(int(r.headers["Content-Length"]), len(r.get_data()))

    def test_faixa_nao_entra_no_json(self):
        r = _app().test_client().get("/dados")
        self.assertNotIn("faixa-homologacao", r.get_data(as_text=True))

    def test_desligado_vira_409_json_na_api(self):
        r = _app().test_client().get("/api/desligado")
        self.assertEqual(r.status_code, 409)
        self.assertIn("desligado na homologação", r.get_json()["erro"])

    def test_bloqueado_vira_503_html_fora_da_api(self):
        r = _app().test_client().get("/tela/bloqueado")
        self.assertEqual(r.status_code, 503)
        self.assertIn("indisponível na homologação", r.get_data(as_text=True))

    def test_saude_responde_sem_login(self):
        r = _app(requer_auth=lambda **kw: (lambda f: __import__("functools").wraps(f)(lambda *a, **k: ("nao", 401)))).test_client().get("/homologacao/saude")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["guarda"], "ativa")
        self.assertEqual(j["ambiente"], "homolog")
        self.assertEqual(j["camadas"], ["smtp", "requests"])

    def test_envios_exige_login_no_painel(self):
        def negar(**kw):
            import functools
            return lambda f: functools.wraps(f)(lambda *a, **k: ("negado", 401))
        r = _app(requer_auth=negar).test_client().get("/homologacao/envios")
        self.assertEqual(r.status_code, 401)

    def test_portal_nao_tem_tela_de_envios(self):
        r = _app(servico="portal").test_client().get("/homologacao/envios")
        self.assertEqual(r.status_code, 404)

    def test_envios_lista_registro(self):
        registro.registrar_envio("email", "SMTP", "a@b.com", "resumo-unico-xyz", "corpo")
        c = _app(requer_auth=lambda **kw: (lambda g: g)).test_client()
        self.assertIn("resumo-unico-xyz", c.get("/homologacao/envios").get_data(as_text=True))
        self.assertNotIn("resumo-unico-xyz", c.get("/homologacao/envios?canal=http").get_data(as_text=True))

    def test_detalhe_email_mostra_html_no_iframe(self):
        import email.message
        m = email.message.EmailMessage()
        m["Subject"] = "oi"
        m.set_content("texto")
        m.add_alternative("<b>negrito-html</b>", subtype="html")
        eid = registro.registrar_envio("email", "SMTP", "a@b.com", "oi", m.as_string())
        c = _app(requer_auth=lambda **kw: (lambda g: g)).test_client()
        corpo = c.get("/homologacao/envios/%d" % eid).get_data(as_text=True)
        self.assertIn("<iframe sandbox", corpo)
        self.assertIn("negrito-html", corpo)
        self.assertEqual(c.get("/homologacao/envios/9999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
