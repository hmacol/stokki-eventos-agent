# -*- coding: utf-8 -*-
"""Link publico da liberacao de recebimento fora da faixa (/liberacao/<token>).

App Flask minimo (o app.py do portal le o config.yaml ao importar), com link_liberacao.registrar
e `enviar` falso: nenhum e-mail sai.

Rodar (da raiz): py -3.11 -m unittest portal_cliente.test_link_liberacao
"""
import builtins
import io
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from flask import Flask
from itsdangerous import URLSafeTimedSerializer

_AQUI = Path(__file__).resolve().parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import auth_cliente  # noqa: E402
import link_liberacao  # noqa: E402
from painel_agentes import wms_chegada  # noqa: E402

HTTPS = "https://localhost"
CNPJ = "54993021000184"
SEGREDO = "segredo-de-teste"
CONFIG = {"notificacao_chegada_recebimento": {"email_escritorio": "escritorio@teste.local"}}


class TokenLiberacao(unittest.TestCase):
    def test_token_ida_e_volta(self):
        token = link_liberacao.gerar(42, SEGREDO)
        self.assertEqual(link_liberacao.ler(token, SEGREDO), 42)
        url = link_liberacao.url(42, SEGREDO, "https://app.freshhub.com.br/cliente/")
        self.assertTrue(url.startswith("https://app.freshhub.com.br/cliente/liberacao/"))
        self.assertEqual(link_liberacao.ler(url.rsplit("/", 1)[1], SEGREDO), 42)

    def test_token_adulterado_ou_vencido_404(self):
        token = link_liberacao.gerar(42, SEGREDO)
        self.assertIsNone(link_liberacao.ler(token[:-2] + "xx", SEGREDO))
        self.assertIsNone(link_liberacao.ler(token, "outro-segredo"))
        self.assertIsNone(link_liberacao.ler("lixo", SEGREDO))
        self.assertIsNone(link_liberacao.ler(token, SEGREDO, validade_dias=-1))
        # outra finalidade (outro salt) nao vale
        outro = URLSafeTimedSerializer(SEGREDO).dumps({"c": 42}, salt="ciencia-dedicado")
        self.assertIsNone(link_liberacao.ler(outro, SEGREDO))
        # vencido: assinado ha mais de 7 dias
        antigo = URLSafeTimedSerializer(SEGREDO).dumps({"c": 42}, salt=link_liberacao._SALT)
        with mock.patch.object(time, "time", return_value=time.time() + 8 * 86400):
            self.assertIsNone(link_liberacao.ler(antigo, SEGREDO))


class PaginaLiberacao(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "dados.db"
        self.conn = wms_chegada.conectar(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE wms_recebimentos (id INTEGER PRIMARY KEY, codigo TEXT, embarcador TEXT)")
        self.conn.execute("INSERT INTO wms_recebimentos VALUES (1, 'REC-1', 'MARIA')")
        self.conn.execute("INSERT INTO wms_recebimentos VALUES (2, 'REC-2', 'MARIA')")
        self.enviados = []
        self.envio_ok = True

    def _chegada(self, uuid="u1", estado="AGUARDANDO_ESCRITORIO", recebimentos=(1,)):
        cur = self.conn.execute(
            "INSERT INTO wms_chegadas (uuid, estado, chegada_em, criado_em) VALUES (?, ?, '2026-11-22 08:30:00', "
            "'2026-11-22 08:30:00')", (uuid, estado))
        cid = cur.lastrowid
        for rid in recebimentos:
            self.conn.execute("INSERT INTO wms_chegada_recebimentos VALUES (?, ?)", (cid, rid))
        self.conn.execute("INSERT INTO wms_chegada_temperaturas VALUES (?, 'REFRIGERADO', 9.5, 0, 7, 1)", (cid,))
        self.conn.execute("INSERT INTO wms_chegada_temperaturas VALUES (?, 'CONGELADO', -15, -18, -12, 0)", (cid,))
        self.conn.commit()
        return cid

    def _foto(self, cid, conteudo=b"jpg-falso"):
        pasta = self.db.parent / "chegadas"
        pasta.mkdir(exist_ok=True)
        nome = f"CHG-{cid}-x{time.monotonic_ns()}.jpg"
        (pasta / nome).write_bytes(conteudo)
        cur = self.conn.execute("INSERT INTO wms_chegada_fotos (chegada_id, tipo, arquivo) VALUES (?, 'TERMOMETRO', ?)",
                                (cid, nome))
        self.conn.commit()
        return cur.lastrowid

    def _enviar(self, destinatarios, assunto, corpo, config_email, **kw):
        self.enviados.append((destinatarios, assunto, corpo))
        return self.envio_ok

    def _cliente(self):
        app = Flask("teste_liberacao", root_path=str(_AQUI), template_folder="templates")
        app.config["TESTING"] = True

        @app.route("/logo")
        def regioes_logo():
            return ""

        link_liberacao.registrar(app, config=CONFIG, secret=SEGREDO, db_path=self.db, enviar=self._enviar)
        return app.test_client()

    def _url(self, cid, resto=""):
        return f"/liberacao/{link_liberacao.gerar(cid, SEGREDO)}{resto}"

    def _estado(self, cid):
        return wms_chegada.detalhe(self.conn, cid)

    def test_pagina_mostra_medicao_e_faixa(self):
        cid = self._chegada(recebimentos=(1, 2))
        foto = self._foto(cid)
        r = self._cliente().get(self._url(cid))
        self.assertEqual(r.status_code, 200)
        texto = r.get_data(as_text=True)
        for esperado in ("REC-1, REC-2", "22/11/2026 08:30", "9,5 °C", "0 °C a 7 °C", "-15 °C", "fora da faixa",
                         "Autorizo o recebimento", "Não autorizo", 'name="nome"', f"/foto/{foto}"):
            self.assertIn(esperado, texto)

    def test_autorizar_libera_e_avisa_escritorio(self):
        cid = self._chegada()
        c = self._cliente()
        r = c.post(self._url(cid, "/responder"), data={"nome": "Maria Souza", "resposta": "autorizo"})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["Location"].endswith(self._url(cid)))
        d = self._estado(cid)
        self.assertEqual((d["estado"], d["resposta_cliente"], d["resposta_cliente_nome"], d["liberacao_via"]),
                         ("LIBERADA", "AUTORIZADO", "Maria Souza", "CLIENTE"))
        self.assertEqual(len(self.enviados), 1)
        destinos, assunto, corpo = self.enviados[0]
        self.assertEqual(destinos, ["escritorio@teste.local"])
        self.assertEqual(assunto, "Cliente autorizou o recebimento: REC-1")
        self.assertIn("Maria Souza", corpo)
        self.assertTrue(wms_chegada.ja_enviado(self.conn, f"escritorio-RESPOSTA_CLIENTE-{cid}"))
        self.assertIn("Recebimento autorizado por Maria Souza", c.get(self._url(cid)).get_data(as_text=True))

    def test_nao_autorizar_registra_e_avisa(self):
        cid = self._chegada()
        r = self._cliente().post(self._url(cid, "/responder"), data={"nome": "Maria Souza", "resposta": "nao_autorizo"})
        self.assertEqual(r.status_code, 303)
        d = self._estado(cid)
        self.assertEqual((d["estado"], d["resposta_cliente"]), ("AGUARDANDO_ESCRITORIO", "NAO_AUTORIZADO"))
        self.assertEqual(self.enviados[0][1], "Cliente não autorizou o recebimento: REC-1")

    def test_falha_de_email_nao_desfaz_a_resposta(self):
        cid = self._chegada()
        self.envio_ok = False
        r = self._cliente().post(self._url(cid, "/responder"), data={"nome": "Maria Souza", "resposta": "autorizo"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._estado(cid)["resposta_cliente"], "AUTORIZADO")
        self.assertFalse(wms_chegada.ja_enviado(self.conn, f"escritorio-RESPOSTA_CLIENTE-{cid}"))

    def test_responder_duas_vezes(self):
        cid = self._chegada()
        c = self._cliente()
        c.post(self._url(cid, "/responder"), data={"nome": "Maria Souza", "resposta": "nao_autorizo"})
        r = c.post(self._url(cid, "/responder"), data={"nome": "Outra Pessoa", "resposta": "autorizo"})
        self.assertEqual(r.status_code, 303)
        d = self._estado(cid)
        self.assertEqual((d["resposta_cliente"], d["resposta_cliente_nome"], d["estado"]),
                         ("NAO_AUTORIZADO", "Maria Souza", "AGUARDANDO_ESCRITORIO"))
        self.assertEqual(len(self.enviados), 1)
        pagina = c.get(self._url(cid)).get_data(as_text=True)
        self.assertIn("Recebimento não autorizado por Maria Souza", pagina)
        self.assertNotIn("Autorizo o recebimento", pagina)

    def test_responder_depois_do_escritorio_decidir(self):
        cid = self._chegada()
        wms_chegada.liberar_por_fora(self.conn, cid, "Cliente autorizou por telefone", "ana")
        c = self._cliente()
        r = c.post(self._url(cid, "/responder"), data={"nome": "Maria Souza", "resposta": "nao_autorizo"})
        self.assertEqual(r.status_code, 303)
        d = self._estado(cid)
        self.assertEqual((d["estado"], d["resposta_cliente"]), ("LIBERADA", ""))
        self.assertEqual(self.enviados, [])
        pagina = c.get(self._url(cid)).get_data(as_text=True)
        self.assertIn("Liberação registrada pela Fresh Log", pagina)
        self.assertNotIn("Não autorizo", pagina)

    def test_nao_recebida_mostra_decisao_da_fresh_log(self):
        cid = self._chegada()
        wms_chegada.nao_receber(self.conn, cid, "Carga recusada", "ana")
        pagina = self._cliente().get(self._url(cid)).get_data(as_text=True)
        self.assertIn("A Fresh Log decidiu não receber esta carga", pagina)

    def test_nome_vazio_volta_com_erro(self):
        cid = self._chegada()
        for nome in ("", "  ", "Jo"):
            r = self._cliente().post(self._url(cid, "/responder"), data={"nome": nome, "resposta": "autorizo"})
            self.assertEqual(r.status_code, 400, nome)
            self.assertIn("Informe o seu nome", r.get_data(as_text=True))
        d = self._estado(cid)
        self.assertEqual((d["estado"], d["resposta_cliente"]), ("AGUARDANDO_ESCRITORIO", ""))
        self.assertEqual(self.enviados, [])

    def test_resposta_invalida_400(self):
        cid = self._chegada()
        r = self._cliente().post(self._url(cid, "/responder"), data={"nome": "Maria Souza", "resposta": "talvez"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._estado(cid)["resposta_cliente"], "")

    def test_token_invalido_404_em_todas_as_rotas(self):
        c = self._cliente()
        for r in (c.get("/liberacao/lixo"), c.post("/liberacao/lixo/responder", data={}),
                  c.get("/liberacao/lixo/foto/1")):
            self.assertEqual(r.status_code, 404)
        self.assertIn("Link inválido ou vencido", c.get("/liberacao/lixo").get_data(as_text=True))

    def test_foto_serve_a_da_chegada(self):
        cid = self._chegada()
        foto = self._foto(cid, b"conteudo-da-foto")
        r = self._cliente().get(self._url(cid, f"/foto/{foto}"))
        self.addCleanup(r.close)
        self.assertEqual((r.status_code, r.data), (200, b"conteudo-da-foto"))

    def test_foto_de_outra_chegada_404(self):
        cid = self._chegada()
        outra = self._chegada(uuid="u2", recebimentos=(2,))
        foto_outra = self._foto(outra)
        c = self._cliente()
        self.assertEqual(c.get(self._url(cid, f"/foto/{foto_outra}")).status_code, 404)
        self.assertEqual(c.get(self._url(cid, "/foto/99999")).status_code, 404)

    def test_token_adulterado_ou_vencido_na_rota_404(self):
        cid = self._chegada()
        c = self._cliente()
        token = link_liberacao.gerar(cid, SEGREDO)
        trocado = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.assertEqual(c.get(f"/liberacao/{trocado}").status_code, 404)
        self.assertEqual(c.post(f"/liberacao/{trocado}/responder", data={"nome": "Maria Souza",
                                                                        "resposta": "autorizo"}).status_code, 404)
        with mock.patch.object(time, "time", return_value=time.time() + 8 * 86400):
            self.assertEqual(c.get(f"/liberacao/{token}").status_code, 404)
        self.assertEqual(self._estado(cid)["resposta_cliente"], "")

    def test_ja_respondida_com_form_invalido_mostra_leitura(self):
        cid = self._chegada()
        c = self._cliente()
        c.post(self._url(cid, "/responder"), data={"nome": "Maria Souza", "resposta": "autorizo"})
        r = c.post(self._url(cid, "/responder"), data={"nome": "", "resposta": "x"})
        self.assertEqual(r.status_code, 303)

    def test_foto_com_arquivo_fora_da_pasta_404(self):
        cid = self._chegada()
        (self.db.parent / "fora.jpg").write_bytes(b"segredo")
        (self.db.parent / "chegadas").mkdir(exist_ok=True)
        cur = self.conn.execute("INSERT INTO wms_chegada_fotos (chegada_id, tipo, arquivo) VALUES (?, 'CARGA', ?)",
                                (cid, "../fora.jpg"))
        self.conn.commit()
        self.assertEqual(self._cliente().get(self._url(cid, f"/foto/{cur.lastrowid}")).status_code, 404)


class RotaLivreNoAppReal(unittest.TestCase):
    """Sem login e com a troca de PIN pendente, os tres endpoints passam pelo before_request do app real."""

    @classmethod
    def setUpClass(cls):
        real_open = builtins.open

        def abrir(arq, *a, **k):  # worktree sem config.yaml (segredo): so o import do app ve um de teste
            if str(arq).endswith("config.yaml") and not Path(arq).exists():
                return io.StringIO("portal_cliente: {secret_key: teste-teste-teste-teste-teste-teste-1234}")
            return real_open(arq, *a, **k)

        try:
            with mock.patch.object(builtins, "open", abrir):
                import app as portal
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(f"app do portal nao importa neste ambiente ({e})")
        cls.portal = portal

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "dados.db"
        original = wms_chegada.conectar
        for p in (mock.patch.object(auth_cliente, "DB_PATH", db),   # nada grava no dados.db do repo
                  mock.patch.object(wms_chegada, "conectar", lambda *_: original(db))):
            p.start()
            self.addCleanup(p.stop)
        conn = original(db)
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE wms_recebimentos (id INTEGER PRIMARY KEY, codigo TEXT, embarcador TEXT)")
        conn.execute("INSERT INTO wms_chegadas (uuid, estado, chegada_em, criado_em) VALUES "
                     "('u', 'AGUARDANDO_ESCRITORIO', '2026-11-22 08:30:00', '2026-11-22 08:30:00')")
        conn.execute("INSERT INTO wms_chegada_fotos (chegada_id, tipo, arquivo) VALUES (1, 'CARGA', 'nao-existe.jpg')")
        conn.commit()
        c = auth_cliente.conectar()
        c.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                  "apelido TEXT, email TEXT)")
        c.execute("INSERT INTO interno VALUES (?, 100, 'LOJA', NULL, 'a@b.c')", (CNPJ,))
        c.commit()
        conta = auth_cliente.definir_pin(c, CNPJ, "135790", trocar_pin=True)
        c.close()
        self.cli = self.portal.app.test_client()
        with self.cli.session_transaction(base_url=HTTPS) as s:
            s["cnpj"], s["v"] = CNPJ, auth_cliente.versao_conta(conta)
        self.token = link_liberacao.gerar(1, self.portal._SECRET)

    def test_endpoints_abrem_com_troca_de_pin_pendente(self):
        self.assertEqual(self.cli.get("/", base_url=HTTPS).status_code, 302)   # a trava da troca vale
        r = self.cli.get(f"/liberacao/{self.token}", base_url=HTTPS)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Autorizo o recebimento", r.get_data(as_text=True))
        # nome vazio: a propria pagina responde 400 (sem gravar nem mandar e-mail), nunca 302
        r = self.cli.post(f"/liberacao/{self.token}/responder", data={"nome": "", "resposta": "autorizo"},
                          base_url=HTTPS)
        self.assertEqual(r.status_code, 400)
        # foto cadastrada sem arquivo: 404 da propria rota, nunca 302
        self.assertEqual(self.cli.get(f"/liberacao/{self.token}/foto/1", base_url=HTTPS).status_code, 404)

    def test_endpoints_abrem_sem_sessao(self):
        cli = self.portal.app.test_client()
        self.assertEqual(cli.get(f"/liberacao/{self.token}", base_url=HTTPS).status_code, 200)
        self.assertEqual(cli.get(f"/liberacao/{self.token}/foto/1", base_url=HTTPS).status_code, 404)
        self.assertEqual(cli.post(f"/liberacao/{self.token}/responder", data={"nome": ""},
                                  base_url=HTTPS).status_code, 400)


if __name__ == "__main__":
    unittest.main()
