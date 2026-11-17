# -*- coding: utf-8 -*-
"""
test_api_atualizacao_motorista.py

Página e rotas públicas do link de atualização de motorista que já existe
(motorista-api): chave geral, fluxo abrir -> dados -> documentos -> concluir,
token inválido sem detalhe e upload recusado com o link morto. SQLite
temporário, sem rede.
    py -3.11 -m unittest nucleo.test_api_atualizacao_motorista -v
"""
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import api_motorista, atualizacao_motorista as atu, banco  # noqa: E402

CPF = "52998224725"
JPG = b"\xff\xd8\xff\xe0" + b"0" * 50
MSG_OFF = "Cadastro indisponível no momento."


def _dados(**extra):
    d = {"nome": "Fulano de Tal", "data_nascimento": "1990-05-17", "telefone": "(11) 99876-5432",
         "email": "fulano@exemplo.com", "chave_pix": "pix@exemplo.com", "tipo_veiculo": "vuc",
         "placa": "abc-1d23", "veiculo_modelo": "Fiat Fiorino", "veiculo_ano": 2020, "refrigerado": False,
         "emite_nf": True, "dono_veiculo": True}
    d.update(extra)
    return d


class TestApiAtualizacaoMotorista(unittest.TestCase):
    def _app(self, ativo=True, link_novo=False):
        cfg = {"api_motorista": {"secret_key": "x", "pasta_comprovantes": str(Path(self._tmp.name) / "f"),
                                 "gcs_ativo": False}}
        if ativo:
            cfg["atualizacao_motoristas"] = {"ativo": True}
        if link_novo:
            cfg["cadastro_motorista_link"] = {"ativo": True}
        return api_motorista.criar_app(cfg).test_client()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        mock.patch.object(banco, "DB_PATH", Path(self._tmp.name) / "t.db").start()
        self._zap = mock.patch("notificar_whatsapp.despachar").start()
        self.conn = banco.conectar()
        agora = banco.agora()
        self.conn.execute("INSERT INTO bd_motoristas (NOME_MOTORISTA, CPF_MOTORISTA, ATIVO, criado_em, atualizado_em) "
                          "VALUES ('Fulano', ?, 'SIM', ?, ?)", (CPF, agora, agora))
        self.conn.commit()
        self.lid = self.conn.execute("SELECT id FROM bd_motoristas").fetchone()[0]
        self.cli = self._app()

    def tearDown(self):
        self.conn.close()
        mock.patch.stopall()
        self._tmp.cleanup()

    def _link(self):
        return atu.gerar_link(self.conn, self.lid, "hugo")

    def _abrir(self, token, cpf=CPF):
        return self.cli.post(f"/api/atualizacao/{token}/abrir", json={"cpf": cpf})

    def _subir(self, cid, chave, tipo):
        return self.cli.post(f"/api/cadastro/{cid}/documentos",
                             data={"chave_envio": chave, "tipo": tipo, "arquivo": (io.BytesIO(JPG), f"{tipo}.jpg")},
                             content_type="multipart/form-data")

    def test_desligado_503(self):
        token = self._link()["token"]
        cli = self._app(ativo=False)
        r = cli.get(f"/atualizar/{token}")
        self.assertEqual(r.status_code, 503)
        self.assertIn(MSG_OFF, r.get_data(as_text=True))
        for r in (cli.post(f"/api/atualizacao/{token}/abrir", json={"cpf": CPF}),
                  cli.post(f"/api/atualizacao/{token}/dados", json={"chave_envio": "x"}),
                  cli.post(f"/api/atualizacao/{token}/concluir", json={"chave_envio": "x"})):
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.get_json(), {"erro": MSG_OFF})

    def test_pagina_em_modo_atualizar_sem_campos_da_operacao(self):
        token = self._link()["token"]
        r = self.cli.get(f"/atualizar/{token}")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn(f"/api/atualizacao/{token}/abrir", html)
        for operacional in ('name="zonas"', 'name="dias"', 'name="posicao"', 'name="celular"'):
            self.assertNotIn(operacional, html)
        # o cadastro novo continua com os campos
        self.assertIn('name="zonas"', self._app(ativo=False, link_novo=True).get("/cadastro").get_data(as_text=True))

    def test_fluxo_completo(self):
        token = self._link()["token"]
        r = self._abrir(token)
        self.assertEqual(r.status_code, 200, r.get_json())
        ficha = r.get_json()
        cid, chave = ficha["cadastro_id"], ficha["chave_envio"]
        r = self.cli.post(f"/api/atualizacao/{token}/dados", json={"chave_envio": chave, **_dados()})
        self.assertEqual(r.status_code, 200, r.get_json())
        for tipo in ("cnh", "crlv", "selfie"):
            r = self._subir(cid, chave, tipo)
            self.assertEqual(r.status_code, 200, r.get_json())
        r = self.cli.post(f"/api/atualizacao/{token}/concluir", json={"chave_envio": chave})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json(), {"ok": True, "pendentes": ["antt", "residencia"], "pix_em_analise": False})
        status = self.conn.execute("SELECT status FROM motoristas_cadastros WHERE id = ?", (cid,)).fetchone()[0]
        self.assertEqual(status, "APROVADO")

    def test_token_invalido_404_sem_detalhe(self):
        r = self.cli.get("/atualizar/nao-existe")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Link inválido ou vencido", r.get_data(as_text=True))
        for r in (self._abrir("nao-existe"),
                  self.cli.post("/api/atualizacao/nao-existe/dados", json={"chave_envio": "x"}),
                  self.cli.post("/api/atualizacao/nao-existe/concluir", json={"chave_envio": "x"})):
            self.assertEqual(r.status_code, 404)
            self.assertEqual(r.get_json(), {"erro": atu.MSG_LINK_INVALIDO})

    def test_cpf_errado_403(self):
        token = self._link()["token"]
        r = self._abrir(token, cpf="11144477735")
        self.assertEqual(r.status_code, 403)

    def test_documento_de_atualizacao_nao_avisa_grupo(self):
        token = self._link()["token"]
        ficha = self._abrir(token).get_json()
        for tipo in ("cnh", "crlv", "selfie"):
            self.assertEqual(self._subir(ficha["cadastro_id"], ficha["chave_envio"], tipo).status_code, 200)
        self._zap.assert_not_called()

    def test_documento_recusado_com_link_cancelado(self):
        token = self._link()["token"]
        ficha = self._abrir(token).get_json()
        cid, chave = ficha["cadastro_id"], ficha["chave_envio"]
        self.assertEqual(self._subir(cid, chave, "cnh").status_code, 200)
        atu.gerar_link(self.conn, self.lid, "hugo")   # link novo cancela o antigo
        r = self._subir(cid, chave, "crlv")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json(), {"erro": atu.MSG_LINK_INVALIDO})
        self.assertIsNone(self.conn.execute("SELECT crlv_arquivo FROM motoristas_cadastros WHERE id = ?",
                                            (cid,)).fetchone()[0])

    def test_documento_recusado_com_link_vencido(self):
        token = self._link()["token"]
        ficha = self._abrir(token).get_json()
        vencido = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("UPDATE motoristas_links_atualizacao SET expira_em = ?", (vencido,))
        self.conn.commit()
        self.assertEqual(self._subir(ficha["cadastro_id"], ficha["chave_envio"], "cnh").status_code, 410)

    def test_documento_recusado_com_link_concluido(self):
        token = self._link()["token"]
        ficha = self._abrir(token).get_json()
        cid, chave = ficha["cadastro_id"], ficha["chave_envio"]
        self.cli.post(f"/api/atualizacao/{token}/dados", json={"chave_envio": chave, **_dados()})
        for tipo in ("cnh", "crlv", "selfie"):
            self._subir(cid, chave, tipo)
        self.assertEqual(self.cli.post(f"/api/atualizacao/{token}/concluir", json={"chave_envio": chave}).status_code, 200)
        self.assertEqual(self._subir(cid, chave, "antt").status_code, 410)

    def test_documento_recusado_com_atualizacao_desligada(self):
        token = self._link()["token"]
        ficha = self._abrir(token).get_json()
        r = self._app(ativo=False).post(
            f"/api/cadastro/{ficha['cadastro_id']}/documentos",
            data={"chave_envio": ficha["chave_envio"], "tipo": "cnh", "arquivo": (io.BytesIO(JPG), "cnh.jpg")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 503)

    def test_token_nao_vai_para_o_log(self):
        token = self._link()["token"]
        ficha = self._abrir(token).get_json()
        chave = ficha["chave_envio"]
        with self.assertLogs("nucleo.api_motorista", level="INFO") as cm:
            # CadastroInvalido (dados inválidos), LinkInvalido (token que não existe) e 503 (desligado)
            self.assertEqual(self.cli.post(f"/api/atualizacao/{token}/dados",
                                           json={"chave_envio": chave, "nome": ""}).status_code, 400)
            self.assertEqual(self.cli.post(f"/api/atualizacao/{token}x/concluir",
                                           json={"chave_envio": chave}).status_code, 404)
            self.assertEqual(self._app(ativo=False).post(f"/api/atualizacao/{token}/abrir",
                                                         json={"cpf": CPF}).status_code, 503)
        saida = "\n".join(cm.output)
        self.assertIn("recusou", saida)
        self.assertNotIn(token, saida)

    def test_link_novo_continua_503_independente(self):
        # atualizacao ligada e cadastro_motorista_link ausente: o link de cadastro novo segue desligado
        r = self.cli.get("/cadastro")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.cli.post("/api/cadastro-link", json={}).status_code, 503)
        # e o contrário: cadastro novo ligado não liga a atualização
        cli = self._app(ativo=False, link_novo=True)
        self.assertEqual(cli.get("/cadastro").status_code, 200)
        self.assertEqual(cli.get(f"/atualizar/{self._link()['token']}").status_code, 503)

    def test_ativo_entre_aspas_segue_o_criterio_do_painel(self):
        """M3: "false" entre aspas no config não liga a página (mesmo _parse_bool do painel)."""
        token = self._link()["token"]
        for valor, ligado in (("false", False), ("nao", False), ("0", False), ("sim", True), ("true", True)):
            cfg = {"api_motorista": {"secret_key": "x", "pasta_comprovantes": str(Path(self._tmp.name) / "f"),
                                     "gcs_ativo": False},
                   "atualizacao_motoristas": {"ativo": valor}}
            cli = api_motorista.criar_app(cfg).test_client()
            self.assertEqual(cli.get(f"/atualizar/{token}").status_code, 200 if ligado else 503, valor)
            if not ligado:
                self.assertEqual(cli.post(f"/api/atualizacao/{token}/abrir", json={"cpf": CPF}).status_code, 503, valor)


if __name__ == "__main__":
    unittest.main()
