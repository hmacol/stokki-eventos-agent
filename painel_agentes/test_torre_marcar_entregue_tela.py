# -*- coding: utf-8 -*-
"""
Endpoint POST /api/torre/marcar-entregue e os templates que o chamam
(Torre e Planejamento), Hugo 08/10. A funcao de negocio e testada em
test_torre_marcar_entregue.py; aqui so o contrato HTTP (nivel, CSRF,
corpo) e a presenca do botao/modal nas telas.

    py -3.11 -m unittest painel_agentes.test_torre_marcar_entregue_tela
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

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = painel_agentes
_spec.loader.exec_module(painel_agentes)

CONFIG = {
    "painel_agentes": {
        "usuario": "u_total", "senha": "s_total",
        "usuario_operador": "u_op", "senha_operador": "s_op",
        "usuario_leitura": "u_le", "senha_leitura": "s_le",
        "usuario_expedicao": "u_ex", "senha_expedicao": "s_ex",
        "usuario_galpao": "u_ga", "senha_galpao": "s_ga",
        "usuario_atendimento": "u_at", "senha_atendimento": "s_at",
    },
    "google_maps": {},
}


class _Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(painel_agentes, "_carregar_config", return_value=CONFIG)
        p.start()
        self.addCleanup(p.stop)
        painel_agentes.app.config["TESTING"] = True
        self.cliente = painel_agentes.app.test_client()

    def _logar(self, nivel):
        with self.cliente.session_transaction() as sess:
            sess["nivel_acesso"] = nivel
            sess["usuario"] = "teste"

    def _post(self, corpo, origem=True, arquivos=None):
        import io
        headers = {"Origin": "http://localhost"} if origem else {}
        data = dict(corpo)
        for nome, conteudo in (arquivos or {}).items():
            data[nome] = (io.BytesIO(conteudo), "foto.jpg")
        return self.cliente.post("/api/torre/marcar-entregue", data=data,
                                 content_type="multipart/form-data", headers=headers)


class TestEndpoint(_Base):

    def test_api_multipart_ok(self):
        self._logar("operador")
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual",
                               return_value={"ok": True, "codigo": "#PS-77", "checklist_id": 9}) as fn:
            r = self._post({"service_id": "77", "justificativa": "outro", "texto_outro": "porteiro recebeu"},
                           arquivos={"foto_canhoto": b"canhoto", "foto_nota_retorno": b"nota"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()["checklist_id"], 9)
        fn.assert_called_once_with(77, "teste", b"canhoto", b"nota", "outro", "porteiro recebeu")

    def test_api_sem_arquivo_manda_none(self):
        self._logar("total")
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual",
                               return_value={"ok": True, "codigo": "#PS-77"}) as fn:
            r = self._post({"service_id": "77", "justificativa": "sem_bateria"})
        self.assertEqual(r.status_code, 200, r.data)
        fn.assert_called_once_with(77, "teste", None, None, "sem_bateria", None)

    def test_api_arquivo_grande_413(self):
        self._logar("operador")
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual") as fn:
            # o test_client recalcula o Content-Length: baixa o limite em vez de mandar 32 MB
            with mock.patch.object(painel_agentes, "LIMITE_REQUISICAO_ENTREGUE", 5):
                r = self._post({"service_id": "77"}, arquivos={"foto_canhoto": b"x" * 100})
            self.assertEqual(r.status_code, 413)
            # arquivo acima de 15 MB dentro do limite da requisicao
            with mock.patch("nucleo.canhotos_manuais.LIMITE_BYTES", 5):
                r = self._post({"service_id": "77"}, arquivos={"foto_canhoto": b"x" * 100})
            self.assertEqual(r.status_code, 413)
        fn.assert_not_called()

    def test_erro_de_negocio_vira_400(self):
        self._logar("total")
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual",
                               return_value={"ok": False, "erro": "sem motorista"}):
            r = self._post({"service_id": "77", "justificativa": "sem_bateria"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "sem motorista")

    def test_service_id_invalido_400_sem_chamar(self):
        self._logar("total")
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual") as fn:
            r = self._post({"service_id": "abc"})
        self.assertEqual(r.status_code, 400)
        fn.assert_not_called()

    def test_json_antigo_nao_vale_mais(self):
        self._logar("total")
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual") as fn:
            r = self.cliente.post("/api/torre/marcar-entregue", json={"service_id": 77, "observacao": "x"},
                                  headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 400)
        fn.assert_not_called()

    def test_leitura_e_atendimento_nao_podem(self):
        for nivel in ("leitura", "atendimento"):
            self._logar(nivel)
            with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual") as fn:
                r = self._post({"service_id": "77", "justificativa": "sem_bateria"})
            self.assertEqual(r.status_code, 403, nivel)
            fn.assert_not_called()

    def test_api_exige_mesma_origem(self):
        self._logar("total")
        with mock.patch.object(painel_agentes.torre_controle, "marcar_entregue_manual") as fn:
            r = self._post({"service_id": "77", "justificativa": "sem_bateria"}, origem=False)
        self.assertEqual(r.status_code, 403)
        fn.assert_not_called()


class TestTemplates(_Base):
    """Os templates sao grandes e carregam dados ao vivo; aqui so se
    garante que compilam (Jinja) e que o botao/modal novos estao la."""

    def _fonte(self, nome):
        env = painel_agentes.app.jinja_env
        fonte, _, _ = env.loader.get_source(env, nome)
        env.parse(fonte)  # estoura em erro de sintaxe Jinja
        return fonte

    def test_torre_tem_menu_modal_e_url(self):
        fonte = self._fonte("torre_controle.html")
        self.assertIn("Marcar como entregue", fonte)
        self.assertIn('id="modal-entregue"', fonte)
        self.assertIn("url_for('api_torre_marcar_entregue')", fonte)
        self.assertIn("function confirmarEntregueChip", fonte)

    def test_planejamento_tem_menu_e_modal(self):
        fonte = self._fonte("planejamento_rotas.html")
        self.assertIn('rotulo: "Marcar como entregue"', fonte)
        self.assertIn('id="modal-entregue"', fonte)
        self.assertIn('"/api/torre/marcar-entregue"', fonte)
        # em massa (23/10): botao na barra de selecao das rotas
        self.assertIn('id="botao-entregue-selecao-rotas"', fonte)
        self.assertIn("function abrirModalEntregueLote", fonte)

    def test_torre_renderiza_para_operador(self):
        self._logar("operador")
        r = self.cliente.get("/torre")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("/api/torre/marcar-entregue", html)
        self.assertIn("Marcar como entregue", html)


if __name__ == "__main__":
    unittest.main()
