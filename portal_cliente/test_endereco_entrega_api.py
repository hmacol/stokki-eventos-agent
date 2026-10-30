# -*- coding: utf-8 -*-
"""
Rotas do envio com o endereco de entrega (spec 2026-10-27): a previa devolve
o caso de cada NF, o confirmar passa o sender da empresa do envio e a tela
consulta o CEP.

    py -3.11 -m unittest portal_cliente.test_endereco_entrega_api -v
"""
import builtins
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

import cotacao  # noqa: E402
import envio_pedidos as ep  # noqa: E402
from test_endereco_entrega_envio import _xml_nf  # noqa: E402
from test_endereco_entrega_xml import ENTREGA  # noqa: E402

CNPJ = "22135070000190"
CLIENTE = {"cnpj": CNPJ, "sender_id": 22812827, "nome": "EMB TESTE", "cnpj_formatado": "22.135.070/0001-90",
           "versao": "x", "sender_ids": [22812827],
           "empresas": [{"cnpj": CNPJ, "cnpj_formatado": "22.135.070/0001-90", "sender_id": 22812827, "nome": "EMB TESTE"}]}
LIGADA = {"portal_cliente": {"envios": {"endereco_entrega_ativo": True}}}


def _importar_app():
    """app.py lê o arquivo de configuração de segredos ao carregar; no teste
    ele vira um config mínimo de teste (a worktree não tem o arquivo, e o teste não deve lê-lo)."""
    if "app" in sys.modules:
        return sys.modules["app"]
    abrir = builtins.open

    def falso(arquivo, *a, **kw):
        if str(arquivo).endswith("config.yaml"):
            return io.StringIO("portal_cliente: {secret_key: teste-local-nao-e-segredo-0123456789abcdef}")
        return abrir(arquivo, *a, **kw)

    with mock.patch("builtins.open", falso):
        import app
    return app


class Rotas(unittest.TestCase):
    def setUp(self):
        self.app = app = _importar_app()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        (raiz / "x" / "_temporarios").mkdir(parents=True)
        self.config = dict(LIGADA)
        patches = [
            mock.patch.object(ep, "DB_PATH", raiz / "d.db"), mock.patch.object(ep, "PASTA_XMLS", raiz / "x"),
            mock.patch.object(ep, "PASTA_TEMP", raiz / "x" / "_temporarios"),
            mock.patch.object(ep, "validar_item", return_value={"ok": True, "erros": [], "avisos": [], "envio_existente": None}),
            mock.patch.object(ep, "config_stokki_cliente", return_value={"regra_xml": "nenhuma", "envio_ativo": True, "sender_id": 1}),
            mock.patch.object(ep, "fluxos_para_envio", return_value=(["armazenagem"], {"erro": None})),
            mock.patch.object(ep, "info_destinatarios", return_value={}),
            mock.patch.object(ep, "_tipo_transportadora", return_value=None),
            mock.patch.object(app.auth, "sessao_valida", lambda c, cnpj, v: dict(CLIENTE)),
            mock.patch.object(app, "_CONFIG", self.config),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        app.app.config["TESTING"] = True
        self.tc = app.app.test_client()
        with self.tc.session_transaction() as s:
            s["cnpj"] = CNPJ
            s["v"] = "x"

    def _analisar(self):
        arquivos = [(io.BytesIO(_xml_nf(1)), "nf1.xml"), (io.BytesIO(_xml_nf(2, ENTREGA)), "nf2.xml")]
        return self.tc.post("/api/envios/analisar", data={"arquivos": arquivos}, content_type="multipart/form-data")

    def test_analisar_devolve_modo_por_item(self):
        r = self._analisar()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertIs(j["entrega_ativa"], True)
        modos = {it["numero_nf"]: it["entrega"]["modo"] for it in j["itens"]}
        self.assertEqual(modos, {"1": "livre", "2": "nota"})

    def test_analisar_usa_sender_da_empresa_do_envio(self):
        with mock.patch("endereco_entrega.classificar_entrega",
                        return_value={"modo": "livre", "transportadora": "", "sugestao": None}) as caso:
            self._analisar()
        self.assertEqual({c.args[3] for c in caso.call_args_list}, {22812827})

    def test_analisar_sem_chave_nao_devolve_entrega(self):
        self.config.clear()
        j = self._analisar().get_json()
        self.assertIs(j["entrega_ativa"], False)
        self.assertTrue(all("entrega" not in it for it in j["itens"]))

    def test_cep_ok_e_404(self):
        dados = {"cep": "01414-001", "logradouro": "Rua Haddock Lobo", "bairro": "Cerqueira César",
                 "cidade": "São Paulo", "uf": "SP", "ibge": "3550308"}
        with mock.patch.object(cotacao, "consultar_cep", side_effect=lambda c: dados if c == "01414001" else None):
            ok = self.tc.get("/api/envios/cep/01414-001")
            nao = self.tc.get("/api/envios/cep/99999999")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json(), {k: v for k, v in dados.items() if k != "ibge"})
        self.assertEqual((nao.status_code, nao.get_json()), (404, {"erro": "CEP não encontrado"}))

    def test_confirmar_passa_sender_id_e_config(self):
        with mock.patch.object(ep, "confirmar_envios", return_value=[]) as conf, \
             mock.patch.object(ep, "avisos_fora_dia_fixo", return_value=[]) as fora:
            r = self.tc.post("/api/envios/confirmar", json={"itens": [{"token": "tokenteste1"}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(conf.call_args.kwargs["sender_id"], 22812827)
        self.assertIs(fora.call_args.args[1], self.config)


if __name__ == "__main__":
    unittest.main()
