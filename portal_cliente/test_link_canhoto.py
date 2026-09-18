"""Link publico do canhoto (sem login) usado no e-mail de resumo diario.

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_link_canhoto
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from itsdangerous import URLSafeTimedSerializer

_AQUI = Path(__file__).parent
sys.path.insert(0, str(_AQUI.parent))
sys.path.insert(0, str(_AQUI))

import link_canhoto

SEGREDO = "segredo-de-teste"


class TestToken(unittest.TestCase):
    def test_ida_e_volta(self):
        token = link_canhoto.gerar(555, 101, SEGREDO)
        self.assertEqual(link_canhoto.ler(token, SEGREDO), {"service_id": 555, "sender_id": 101})

    def test_adulterado_ou_de_outro_segredo_nao_vale(self):
        token = link_canhoto.gerar(555, 101, SEGREDO)
        self.assertIsNone(link_canhoto.ler(token[:-2] + "xx", SEGREDO))
        self.assertIsNone(link_canhoto.ler(token, "outro-segredo"))
        self.assertIsNone(link_canhoto.ler("lixo", SEGREDO))

    def test_vencido_nao_vale(self):
        token = link_canhoto.gerar(555, 101, SEGREDO)
        self.assertIsNone(link_canhoto.ler(token, SEGREDO, validade_dias=-1))

    def test_token_de_outra_finalidade_do_portal_nao_vale(self):
        # mesmo segredo do portal, outro salt (ex.: link de definir PIN)
        outro = URLSafeTimedSerializer(SEGREDO).dumps({"service_id": 555, "sender_id": 101}, salt="portal-cliente-definir-pin")
        self.assertIsNone(link_canhoto.ler(outro, SEGREDO))

    def test_url_completa(self):
        url = link_canhoto.url(555, 101, SEGREDO, "https://app.freshhub.com.br/cliente/")
        self.assertTrue(url.startswith("https://app.freshhub.com.br/cliente/c/"))
        self.assertEqual(link_canhoto.ler(url.rsplit("/", 1)[1], SEGREDO)["service_id"], 555)


class TestRota(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as portal
        cls.portal = portal
        portal.app.config["TESTING"] = True

    def setUp(self):
        self.tc = self.portal.app.test_client()   # sem sessao: a rota e publica
        self.token = link_canhoto.gerar(555, 101, self.portal._SECRET)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pdf = Path(self._tmp.name) / "canhoto.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 teste")

    def _abrir(self, token, servico, checklist_id=9, pdf="padrao"):
        dados = self.portal.dados
        with patch.object(dados, "buscar_servico", return_value=servico) as buscar, \
             patch.object(dados, "checklist_id_do_servico", return_value=checklist_id), \
             patch.object(dados, "baixar_canhoto_pdf", return_value=self.pdf if pdf == "padrao" else pdf):
            resposta = self.tc.get(f"/c/{token}")
            resposta.get_data()   # fecha o arquivo antes de apagar o tmp
        return resposta, buscar

    def test_token_valido_serve_o_pdf_sem_login(self):
        r, buscar = self._abrir(self.token, {"id": 555, "sender_id": 101, "code": "#PS-1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertEqual(buscar.call_args.args[1], 555)

    def test_servico_de_outro_embarcador_da_404(self):
        r, _ = self._abrir(self.token, {"id": 555, "sender_id": 999, "code": "#PS-1"})
        self.assertEqual(r.status_code, 404)

    def test_token_invalido_da_404_sem_consultar_a_vuupt(self):
        r, buscar = self._abrir("token-falso", {"id": 555, "sender_id": 101})
        self.assertEqual(r.status_code, 404)
        buscar.assert_not_called()

    def test_canhoto_ainda_nao_enviado_explica_em_vez_de_quebrar(self):
        r, _ = self._abrir(self.token, {"id": 555, "sender_id": 101, "code": "#PS-1"}, checklist_id=None)
        self.assertEqual(r.status_code, 404)
        self.assertTrue("ainda não disponível" in r.get_data(as_text=True))

    def test_rota_logada_antiga_continua_funcionando(self):
        # api_canhoto usa o mesmo miolo; sem sessao segue exigindo login
        self.assertEqual(self.tc.get("/api/canhoto/555").status_code, 401)


if __name__ == "__main__":
    unittest.main()
