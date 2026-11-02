# -*- coding: utf-8 -*-
"""
test_api_cadastro_link.py

Página e rotas públicas do cadastro de motorista pelo link (motorista-api):
chave geral, fluxo criar -> documentos -> situação -> concluir, processar que
falha sem derrubar o concluir e freio por IP. SQLite temporário, sem rede.
    py -3.11 -m unittest nucleo.test_api_cadastro_link -v
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import api_motorista, banco  # noqa: E402

MSG_OFF = "Cadastro indisponível no momento."


def _cpf_valido(base9: str) -> str:
    d = [int(c) for c in base9]
    for n in (9, 10):
        soma = sum(d[i] * (n + 1 - i) for i in range(n))
        d.append((soma * 10 % 11) % 10)
    return "".join(map(str, d))


def _dados(cpf="52998224725", **extra):
    d = {
        "nome": "João Pedro Santos", "cpf": cpf, "data_nascimento": "1990-05-17", "telefone": "(11) 99876-5432",
        "chave_pix": "joao@exemplo.com", "emite_nf": True, "dias": ["SEGUNDA", "TERCA"], "zonas": ["ZONA SUL"],
        "posicao": "FIXO", "celular": "ANDROID", "tipo_veiculo": "fiorino", "placa": "abc-1d23",
        "veiculo_modelo": "Fiat Fiorino", "veiculo_ano": 2020, "refrigerado": False, "dono_veiculo": True,
    }
    d.update(extra)
    return d


def _app_dados(cpf="52998224725"):
    return {"nome": "Maria Souza Lima", "cpf": cpf, "telefone": "11988887777", "chave_pix": "maria@exemplo.com",
            "placa": "ABC1D23", "tipo_veiculo": "fiorino", "zonas": ["ZONA SUL"], "dias": ["SEGUNDA"]}


class TestApiCadastroLink(unittest.TestCase):
    def _app(self, ativo=True):
        cfg = {"api_motorista": {"secret_key": "x", "pasta_comprovantes": str(Path(self._tmp.name) / "f"), "gcs_ativo": False}}
        if ativo:
            cfg["cadastro_motorista_link"] = {"ativo": True}
        return api_motorista.criar_app(cfg).test_client()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch_db = mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db")
        self._patch_db.start()
        self._patch_zap = mock.patch("notificar_whatsapp.despachar")
        self._patch_zap.start()
        self.cli = self._app()

    def tearDown(self):
        mock.patch.stopall()
        self._tmp.cleanup()

    def _criar(self, **kw):
        r = self.cli.post("/api/cadastro-link", json=_dados(**kw))
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def _subir(self, cid, chave, tipo):
        return self.cli.post(f"/api/cadastro/{cid}/documentos",
                             data={"chave_envio": chave, "tipo": tipo, "arquivo": (io.BytesIO(b"\xff\xd8\xff fake"), f"{tipo}.jpg")},
                             content_type="multipart/form-data")

    def test_desligado_devolve_503(self):
        cli = self._app(ativo=False)
        r = cli.get("/cadastro")
        self.assertEqual(r.status_code, 503)
        self.assertIn(MSG_OFF, r.get_data(as_text=True))
        for r in (cli.post("/api/cadastro-link", json=_dados()),
                  cli.get("/api/cadastro/1/situacao?chave_envio=x"),
                  cli.post("/api/cadastro/1/concluir", json={"chave_envio": "x"})):
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.get_json(), {"erro": MSG_OFF})

    def test_desligado_nao_derruba_rotas_antigas(self):
        cli = self._app(ativo=False)
        r = cli.post("/api/cadastro", json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "CPF precisa ter 11 dígitos.")
        self.assertEqual(cli.post("/api/cadastro", json=_app_dados()).status_code, 201)

    def test_pagina_ligada(self):
        r = self.cli.get("/cadastro")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("ZONA SUL", html)
        self.assertIn("Fiorino", html)
        self.assertIn("setAttribute('capture', 'user')", html)

    def test_fluxo_completo(self):
        c = self._criar()
        cid, chave = c["id"], c["chave_envio"]
        s = self.cli.get(f"/api/cadastro/{cid}/situacao?chave_envio={chave}").get_json()
        self.assertEqual(s["primeiro_nome"], "João")
        self.assertFalse(any(s["documentos"].values()))
        for tipo in ("cnh", "crlv", "selfie", "antt", "residencia"):
            self.assertEqual(self._subir(cid, chave, tipo).status_code, 200)
        s = self.cli.get(f"/api/cadastro/{cid}/situacao?chave_envio={chave}").get_json()
        self.assertTrue(all(s["documentos"].values()))
        with mock.patch("nucleo.cadastro_motorista_link.processar") as proc:
            r = self.cli.post(f"/api/cadastro/{cid}/concluir", json={"chave_envio": chave})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "primeiro_nome": "João"})
        proc.assert_called_once()
        self.assertEqual(proc.call_args.args[2], cid)

    def test_processar_com_erro_nao_vira_500(self):
        c = self._criar()
        for tipo in ("cnh", "crlv", "selfie", "antt", "residencia"):
            self._subir(c["id"], c["chave_envio"], tipo)
        with mock.patch("nucleo.cadastro_motorista_link.processar", side_effect=RuntimeError("vuupt fora")):
            r = self.cli.post(f"/api/cadastro/{c['id']}/concluir", json={"chave_envio": c["chave_envio"]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_concluir_sem_documentos_400(self):
        c = self._criar()
        with mock.patch("nucleo.cadastro_motorista_link.processar") as proc:
            r = self.cli.post(f"/api/cadastro/{c['id']}/concluir", json={"chave_envio": c["chave_envio"]})
        self.assertEqual(r.status_code, 400)
        proc.assert_not_called()

    def test_link_nao_avisa_cadastro_novo_no_upload_e_app_avisa(self):
        import notificar_whatsapp
        c = self._criar()
        for tipo in ("cnh", "crlv", "selfie", "antt", "residencia"):
            self._subir(c["id"], c["chave_envio"], tipo)
        tipos = [ch.args[2] for ch in notificar_whatsapp.despachar.call_args_list]
        self.assertNotIn("cadastro_novo", tipos)
        notificar_whatsapp.despachar.reset_mock()
        a = self.cli.post("/api/cadastro", json=_app_dados(_cpf_valido("000000019"))).get_json()
        for tipo in ("cnh", "crlv"):
            self._subir(a["id"], a["chave_envio"], tipo)
        tipos = [ch.args[2] for ch in notificar_whatsapp.despachar.call_args_list]
        self.assertEqual(tipos.count("cadastro_novo"), 1)

    def test_cadastro_do_app_nao_usa_concluir_nem_situacao(self):
        a = self.cli.post("/api/cadastro", json=_app_dados()).get_json()
        for tipo in ("cnh", "crlv"):
            self._subir(a["id"], a["chave_envio"], tipo)
        with mock.patch("nucleo.cadastro_motorista_link.processar") as proc:
            r = self.cli.post(f"/api/cadastro/{a['id']}/concluir", json={"chave_envio": a["chave_envio"]})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["erro"], "Cadastro não encontrado.")
        proc.assert_not_called()
        r = self.cli.get(f"/api/cadastro/{a['id']}/situacao?chave_envio={a['chave_envio']}")
        self.assertEqual(r.status_code, 404)
        row = banco.conectar().execute("SELECT status, enviado_em FROM motoristas_cadastros WHERE id = ?", (a["id"],)).fetchone()
        self.assertEqual(row["status"], "PENDENTE")
        self.assertFalse(row["enviado_em"])

    def test_chave_errada_404(self):
        c = self._criar()
        r = self.cli.get(f"/api/cadastro/{c['id']}/situacao?chave_envio=errada")
        self.assertEqual(r.status_code, 404)

    def test_erro_de_validacao_mostra_texto(self):
        r = self.cli.post("/api/cadastro-link", json=_dados(cpf="11111111111"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["erro"], "CPF inválido.")

    def test_sexto_cadastro_do_mesmo_ip_leva_429(self):
        for i in range(5):
            self._criar(cpf=_cpf_valido(f"{i + 1:09d}"))
        r = self.cli.post("/api/cadastro-link", json=_dados(cpf=_cpf_valido("000000099")))
        self.assertEqual(r.status_code, 429)


if __name__ == "__main__":
    unittest.main()
