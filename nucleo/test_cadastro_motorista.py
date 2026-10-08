# -*- coding: utf-8 -*-
"""
test_cadastro_motorista.py

Cadastro do motorista pelo app (Hugo, 08/10/2026): "meus dados" valendo
na hora com histórico e espelho na planilha, auto-cadastro com CNH/CRLV,
aviso no WhatsApp quando completa, aprovação (planilha + login provisório)
e recusa. SQLite temporário, planilha e WhatsApp substituídos por duplos.
    python -m unittest nucleo.test_cadastro_motorista -v
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from nucleo import api_motorista, auth_motorista as auth, banco, cadastro_motorista as cad  # noqa: E402

CPF, PIN, AGENT = "12345678901", "482913", 4242

CADASTRO_OK = {
    "cpf": "98765432100", "nome": "Maria da Silva", "telefone": "(11) 99876-5432", "email": "Maria@Exemplo.com",
    "chave_pix": "maria@exemplo.com", "placa": "abc-1d23", "tipo_veiculo": "fiorino",
    "zonas": ["ZONA SUL", "ABCD"], "dias": ["SEGUNDA", "TERCA", "QUARTA"], "aceita_viagens": True,
}


def _foto(nome="cnh.jpg"):
    return (io.BytesIO(b"\xff\xd8\xff\xe0" + b"0" * 100), nome)


class TestCadastroMotorista(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        raiz = Path(self._tmp.name)
        self._patches = [mock.patch.object(banco, "DB_PATH", raiz / "t.db")]
        for p in self._patches:
            p.start()
        self.config = {"api_motorista": {"secret_key": "segredo-de-teste", "gcs_ativo": False},
                       "motoristas": {"planilha": str(raiz / "BD.xlsx")}}
        self.app = api_motorista.criar_app(self.config)
        self.cli = self.app.test_client()
        conn = banco.conectar()
        auth.criar_ou_atualizar_motorista(conn, CPF, "Motorista Teste", PIN, agent_id=AGENT, telefone="11911111111")
        conn.close()
        self.planilha = mock.patch("regras.cadastro_motoristas.atualizar_contato_planilha", return_value=True)
        self.planilha_mock = self.planilha.start()
        self.whats = mock.patch("notificar_whatsapp.despachar", return_value="enviado")
        self.whats_mock = self.whats.start()

    def tearDown(self):
        self.whats.stop()
        self.planilha.stop()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _auth(self, cpf=CPF, pin=PIN):
        r = self.cli.post("/api/login", json={"cpf": cpf, "pin": pin})
        self.assertEqual(r.status_code, 200, r.get_json())
        return {"Authorization": f"Bearer {r.get_json()['acesso']}"}

    # ── meus dados ────────────────────────────────────────────────────────────
    def test_meus_dados_vale_na_hora_com_historico_planilha_e_aviso(self):
        h = self._auth()
        eu = self.cli.get("/api/eu", headers=h).get_json()
        self.assertIn("chave_pix", eu)
        self.assertIsNone(eu["placa"])

        r = self.cli.put("/api/eu", json={"placa": "AB1234"}, headers=h)
        self.assertEqual(r.status_code, 400)
        r = self.cli.put("/api/eu", json={"telefone": "123"}, headers=h)
        self.assertEqual(r.status_code, 400)

        r = self.cli.put("/api/eu", json={"telefone": "+55 (11) 98888-7777", "chave_pix": " 123.456.789-01 ",
                                           "placa": "abc1d23", "email": ""}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["motorista"]["telefone"], "11988887777")
        self.assertEqual(d["motorista"]["chave_pix"], "123.456.789-01")
        self.assertEqual(d["motorista"]["placa"], "ABC1D23")
        self.assertEqual({m["campo"] for m in d["mudancas"]}, {"telefone", "chave_pix", "placa"})
        self.planilha_mock.assert_called_once()
        kw = self.planilha_mock.call_args.kwargs
        self.assertEqual((kw["telefone"], kw["placa"]), ("11988887777", "ABC1D23"))
        self.assertNotIn("email", kw)   # e-mail não mudou (já era vazio): não mexe na coluna
        self.whats_mock.assert_called_once()
        self.assertIn("PIX", self.whats_mock.call_args.args[3])

        # Repetir os mesmos dados: nada muda, nada avisa
        self.planilha_mock.reset_mock(); self.whats_mock.reset_mock()
        r = self.cli.put("/api/eu", json={"placa": "ABC-1D23"}, headers=h)
        self.assertEqual(r.get_json()["mudancas"], [])
        self.planilha_mock.assert_not_called()
        self.whats_mock.assert_not_called()

        conn = banco.conectar()
        hist = cad.historico(conn, CPF)
        conn.close()
        self.assertEqual(len(hist), 3)
        self.assertEqual({(x["campo"], x["valor_novo"]) for x in hist} & {("placa", "ABC1D23")}, {("placa", "ABC1D23")})

    def test_meus_dados_bloqueado_com_pin_provisorio(self):
        conn = banco.conectar()
        auth.criar_ou_atualizar_motorista(conn, "11122233344", "Novato", "123456", agent_id=7, trocar_pin=True)
        conn.close()
        h = self._auth(cpf="11122233344", pin="123456")
        self.assertEqual(self.cli.put("/api/eu", json={"placa": "ABC1D23"}, headers=h).status_code, 403)

    # ── auto-cadastro ─────────────────────────────────────────────────────────
    def test_cadastro_valida_campos_e_duplicidade(self):
        r = self.cli.post("/api/cadastro", json={**CADASTRO_OK, "nome": "Maria"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("nome completo", r.get_json()["erro"])
        r = self.cli.post("/api/cadastro", json={**CADASTRO_OK, "zonas": ["MARTE"]})
        self.assertEqual(r.status_code, 400)
        r = self.cli.post("/api/cadastro", json={**CADASTRO_OK, "tipo_veiculo": "carroca"})
        self.assertEqual(r.status_code, 400)
        r = self.cli.post("/api/cadastro", json={**CADASTRO_OK, "cpf": CPF})
        self.assertEqual(r.status_code, 409)   # já tem login

        r = self.cli.post("/api/cadastro", json=CADASTRO_OK)
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertTrue(r.get_json()["chave_envio"])
        r2 = self.cli.post("/api/cadastro", json=CADASTRO_OK)
        self.assertEqual(r2.status_code, 409)  # já em análise
        self.whats_mock.assert_not_called()    # só avisa quando chegam os documentos

        conn = banco.conectar()
        c = cad.buscar_cadastro(conn, r.get_json()["id"])
        conn.close()
        self.assertEqual((c["telefone"], c["email"], c["placa"], c["tipo_veiculo"]), ("11998765432", "maria@exemplo.com", "ABC1D23", "FIORINO"))
        self.assertEqual(c["zonas"], ["ZONA SUL", "ABCD"])
        self.assertFalse(c["completo"])
        self.assertNotIn("chave_envio", c)

    def test_cadastro_tem_freio_por_ip(self):
        # 5 por hora por IP; o 6º leva 429 mesmo com dados válidos
        for i in range(5):
            r = self.cli.post("/api/cadastro", json={**CADASTRO_OK, "cpf": f"1000000000{i}"}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
            self.assertEqual(r.status_code, 201, r.get_json())
        r = self.cli.post("/api/cadastro", json={**CADASTRO_OK, "cpf": "10000000009"}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
        self.assertEqual(r.status_code, 429)
        # outro IP segue normal
        r = self.cli.post("/api/cadastro", json={**CADASTRO_OK, "cpf": "10000000009"}, environ_base={"REMOTE_ADDR": "10.0.0.10"})
        self.assertEqual(r.status_code, 201)

    def _criar_cadastro_completo(self):
        r = self.cli.post("/api/cadastro", json=CADASTRO_OK).get_json()
        cid, chave = r["id"], r["chave_envio"]
        url = f"/api/cadastro/{cid}/documentos"
        # chave errada: como se não existisse
        x = self.cli.post(url, data={"tipo": "cnh", "chave_envio": "errada", "arquivo": _foto()}, content_type="multipart/form-data")
        self.assertEqual(x.status_code, 404)
        x = self.cli.post(url, data={"tipo": "cnh", "chave_envio": chave, "arquivo": _foto("cnh.gif")}, content_type="multipart/form-data")
        self.assertEqual(x.status_code, 400)
        x = self.cli.post(url, data={"tipo": "cnh", "chave_envio": chave, "arquivo": _foto()}, content_type="multipart/form-data")
        self.assertEqual(x.status_code, 200, x.get_json())
        self.assertFalse(x.get_json()["completo"])
        self.whats_mock.assert_not_called()
        x = self.cli.post(url, data={"tipo": "crlv", "chave_envio": chave, "arquivo": _foto("crlv.png")}, content_type="multipart/form-data")
        self.assertTrue(x.get_json()["completo"])
        return cid, chave

    def test_documentos_completam_o_cadastro_e_avisam_uma_vez(self):
        cid, chave = self._criar_cadastro_completo()
        self.whats_mock.assert_called_once()
        texto = self.whats_mock.call_args.args[3]
        self.assertIn("Maria da Silva", texto)
        self.assertIn("/motoristas", texto)
        # reenviar a CNH troca a foto mas não avisa de novo
        x = self.cli.post(f"/api/cadastro/{cid}/documentos", data={"tipo": "cnh", "chave_envio": chave, "arquivo": _foto()},
                          content_type="multipart/form-data")
        self.assertEqual(x.status_code, 200)
        self.whats_mock.assert_called_once()
        conn = banco.conectar()
        self.assertEqual(cad.contar_pendentes(conn), 1)
        self.assertTrue(cad.caminho_documento(conn, cid, "cnh").is_file())
        self.assertIsNone(cad.caminho_documento(conn, cid, "rg"))
        conn.close()

    def test_aprovar_grava_planilha_cria_login_provisorio_e_recusar_fecha(self):
        cid, _ = self._criar_cadastro_completo()
        conn = banco.conectar()
        planilha = []
        with self.assertRaises(cad.CadastroInvalido):   # sem agente Vuupt
            cad.aprovar(conn, cid, self.config, agent_id=None, zonas=None, dias=None, tipo_veiculo=None,
                        aceita_viagens=True, revisado_por="hugo", gravar_planilha=lambda cfg, d: planilha.append(d))
        r = cad.aprovar(conn, cid, self.config, agent_id=5151, zonas=["CENTRO"], dias=None, tipo_veiculo=None,
                        aceita_viagens=False, revisado_por="hugo", gravar_planilha=lambda cfg, d: planilha.append(d))
        self.assertRegex(r["pin"], r"^\d{6}$")
        self.assertNotEqual(r["pin"], "123456")   # provisório sorteado, não o do lote
        pin = r["pin"]
        self.assertEqual(r["cadastro"]["status"], "APROVADO")
        self.assertEqual(planilha[0]["agent_id"], 5151)
        self.assertEqual(planilha[0]["zonas_preferidas"], ["CENTRO"])
        self.assertEqual(planilha[0]["dias_disponiveis"], ["SEGUNDA", "TERCA", "QUARTA"])
        self.assertEqual((planilha[0]["placa"], planilha[0]["cpf"]), ("ABC1D23", "98765432100"))
        m = auth.buscar_motorista(conn, "98765432100")
        self.assertEqual((m["agent_id"], m["chave_pix"], m["placa"], m["trocar_pin"]), (5151, "maria@exemplo.com", "ABC1D23", 1))
        with self.assertRaises(cad.CadastroInvalido):   # já avaliado
            cad.recusar(conn, cid, "x", "hugo")
        self.assertEqual(cad.contar_pendentes(conn), 0)
        conn.close()

        # Login com o PIN provisório funciona e cai no gate da troca
        h = self._auth(cpf="98765432100", pin=pin)
        self.assertEqual(self.cli.get("/api/rotas", headers=h).status_code, 403)

        # Recusa de outro cadastro
        r = self.cli.post("/api/cadastro", json={**CADASTRO_OK, "cpf": "55566677788"}).get_json()
        conn = banco.conectar()
        c = cad.recusar(conn, r["id"], "sem CNH válida", "hugo")
        self.assertEqual((c["status"], c["motivo"]), ("RECUSADO", "sem CNH válida"))
        conn.close()
        # recusado pode pedir de novo
        self.assertEqual(self.cli.post("/api/cadastro", json={**CADASTRO_OK, "cpf": "55566677788"}).status_code, 201)


if __name__ == "__main__":
    unittest.main()
