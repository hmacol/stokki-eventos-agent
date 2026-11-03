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
        self.assertIn('id="recomecar"', html)
        self.assertIn("Seu cadastro anterior foi encerrado. Comece de novo.", html)
        self.assertIn("r.j.status === 'SUBSTITUIDO' || r.j.status === 'RECUSADO'", html)

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
            self._esperar_fundo()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "primeiro_nome": "João"})
        proc.assert_called_once()
        self.assertEqual(proc.call_args.args[2], cid)

    def _esperar_fundo(self):
        t = self.cli.application.config["THREAD_CADASTRO_LINK"]
        t.join(10)
        self.assertFalse(t.is_alive())

    def test_concluir_responde_sem_esperar_o_processar(self):
        import threading
        c = self._criar()
        for tipo in ("cnh", "crlv", "selfie", "antt", "residencia"):
            self._subir(c["id"], c["chave_envio"], tipo)
        liberar, conexoes = threading.Event(), []

        def processar_lento(conn, config, cadastro_id):
            conexoes.append(conn)
            liberar.wait(10)
        with mock.patch("nucleo.cadastro_motorista_link.processar", side_effect=processar_lento):
            r = self.cli.post(f"/api/cadastro/{c['id']}/concluir", json={"chave_envio": c["chave_envio"]})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(self.cli.application.config["THREAD_CADASTRO_LINK"].is_alive())
            liberar.set()
            self._esperar_fundo()
        self.assertEqual(len(conexoes), 1)
        with self.assertRaises(Exception):   # conexão própria da thread, fechada no fim
            conexoes[0].execute("SELECT 1")

    def test_processar_com_erro_nao_vira_500(self):
        c = self._criar()
        for tipo in ("cnh", "crlv", "selfie", "antt", "residencia"):
            self._subir(c["id"], c["chave_envio"], tipo)
        with mock.patch("nucleo.cadastro_motorista_link.processar", side_effect=RuntimeError("vuupt fora")) as proc, \
                self.assertLogs("nucleo.api_motorista", "ERROR"):
            r = self.cli.post(f"/api/cadastro/{c['id']}/concluir", json={"chave_envio": c["chave_envio"]})
            self._esperar_fundo()
        proc.assert_called_once()
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

    # ── Revisão de segurança 02/11 (A1, M1, M2, B2) ─────────────────────────

    def test_freio_do_link_nao_zera_com_restart(self):
        for i in range(5):
            self._criar(cpf=_cpf_valido(f"{i + 1:09d}"))
        cli = self._app()   # app novo = restart do motorista-api
        r = cli.post("/api/cadastro-link", json=_dados(cpf=_cpf_valido("000000099")))
        self.assertEqual(r.status_code, 429)
        r = cli.post("/api/cadastro-link", json=_dados(cpf=_cpf_valido("000000099")), environ_base={"REMOTE_ADDR": "10.9.9.9"})
        self.assertEqual(r.status_code, 201)

    def test_link_nao_gasta_o_freio_do_app(self):
        for i in range(5):
            self._criar(cpf=_cpf_valido(f"{i + 1:09d}"))
        self.assertEqual(self.cli.post("/api/cadastro", json=_app_dados(_cpf_valido("000000019"))).status_code, 201)

    def _pronto(self):
        c = self._criar()
        for tipo in ("cnh", "crlv", "selfie", "antt", "residencia"):
            self._subir(c["id"], c["chave_envio"], tipo)
        return c

    def test_concluir_repetido_processa_uma_vez(self):
        """A1: só a chamada que grava o envio abre a thread."""
        c = self._pronto()
        with mock.patch("nucleo.cadastro_motorista_link.processar") as proc, \
                mock.patch.object(api_motorista.threading, "Thread", wraps=api_motorista.threading.Thread) as th:
            respostas = []
            for _ in range(3):
                respostas.append(self.cli.post(f"/api/cadastro/{c['id']}/concluir", json={"chave_envio": c["chave_envio"]}))
                self._esperar_fundo()
        self.assertEqual([r.status_code for r in respostas], [200, 200, 200])
        self.assertTrue(all(r.get_json() == {"ok": True, "primeiro_nome": "João"} for r in respostas))
        self.assertEqual(th.call_count, 1)
        proc.assert_called_once()

    def test_concluir_repetido_em_conflito_nao_chama_a_vuupt(self):
        c = self._pronto()
        with mock.patch("nucleo.cadastro_motorista_link.processar"):
            self.assertEqual(self.cli.post(f"/api/cadastro/{c['id']}/concluir",
                                           json={"chave_envio": c["chave_envio"]}).status_code, 200)
            self._esperar_fundo()
        conn = banco.conectar()
        conn.execute("UPDATE motoristas_cadastros SET conflito_agent_id = 321, conflito_resolvido = 0 WHERE id = ?", (c["id"],))
        conn.commit()
        conn.close()
        with mock.patch("nucleo.cadastro_motorista_link.VuuptAgentes") as vuupt,                 mock.patch.object(api_motorista.threading, "Thread", wraps=api_motorista.threading.Thread) as th:
            for _ in range(3):
                r = self.cli.post(f"/api/cadastro/{c['id']}/concluir", json={"chave_envio": c["chave_envio"]})
                self.assertEqual(r.status_code, 200)
        th.assert_not_called()
        vuupt.assert_not_called()
        # mesmo que a thread fosse aberta, processar sem forcar recusa o conflito em aberto
        from nucleo import cadastro_motorista_link
        falsa = mock.Mock()
        conn = banco.conectar()
        try:
            r = cadastro_motorista_link.processar(conn, {"cadastro_motorista_link": {"ativo": True}}, c["id"], vuupt=falsa)
        finally:
            conn.close()
        self.assertEqual(r["situacao"], "ignorado")
        falsa.listar_agentes.assert_not_called()

    def test_fundo_com_link_desligado_nao_processa(self):
        """M1(b): a thread confere a chave de novo (desligada entre o concluir e o processar)."""
        c = self._pronto()
        with mock.patch("nucleo.cadastro_motorista_link.processar") as proc,                 mock.patch("nucleo.cadastro_motorista_link.link_ativo", return_value=False):
            r = self.cli.post(f"/api/cadastro/{c['id']}/concluir", json={"chave_envio": c["chave_envio"]})
            self._esperar_fundo()
        self.assertEqual(r.status_code, 200)
        proc.assert_not_called()

    def test_freio_60_por_ip_nas_rotas_da_chave(self):
        c = self._criar()
        url = f"/api/cadastro/{c['id']}/situacao?chave_envio={c['chave_envio']}"
        for _ in range(60):
            self.assertEqual(self.cli.get(url).status_code, 200)
        r = self.cli.get(url)
        self.assertEqual(r.status_code, 429)
        self.assertIn("erro", r.get_json())
        self.assertEqual(self.cli.get(url, environ_base={"REMOTE_ADDR": "10.1.1.1"}).status_code, 200)
        # grupos separados: documentos e concluir ainda passam
        self.assertEqual(self._subir(c["id"], c["chave_envio"], "cnh").status_code, 200)
        for _ in range(59):
            self._subir(c["id"], c["chave_envio"], "cnh")
        self.assertEqual(self._subir(c["id"], c["chave_envio"], "cnh").status_code, 429)
        for _ in range(60):
            self.cli.post(f"/api/cadastro/{c['id']}/concluir", json={"chave_envio": "errada"})
        self.assertEqual(self.cli.post(f"/api/cadastro/{c['id']}/concluir", json={"chave_envio": "errada"}).status_code, 429)

    def test_upload_que_nao_e_foto_400(self):
        c = self._criar()
        r = self.cli.post(f"/api/cadastro/{c['id']}/documentos",
                          data={"chave_envio": c["chave_envio"], "tipo": "cnh", "arquivo": (io.BytesIO(b"MZ\x90\x00" * 10), "cnh.jpg")},
                          content_type="multipart/form-data")
        self.assertEqual((r.status_code, r.get_json()), (400, {"erro": "Arquivo não parece uma foto ou PDF."}))

    def test_log_nao_mostra_a_chave_envio(self):
        c = self._criar()
        with self.assertLogs("nucleo.api_motorista", "INFO") as logs:
            self.cli.get(f"/api/cadastro/{c['id']}/situacao?chave_envio={c['chave_envio']}&x=1")
        texto = "\n".join(logs.output)
        self.assertNotIn(c["chave_envio"], texto)
        self.assertIn(f"/api/cadastro/{c['id']}/situacao?chave_envio=***&x=1", texto)


if __name__ == "__main__":
    unittest.main()
