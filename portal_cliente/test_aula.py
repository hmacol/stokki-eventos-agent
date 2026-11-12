# -*- coding: utf-8 -*-
"""
Pagina publica /aula (videos de treinamento do portal, 06/11): lista do
manifesto aula.json e entrega dos mp4/jpg com Range.

    py -3.11 -m unittest portal_cliente.test_aula
"""
import builtins
import io
import json
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

import auth_cliente as auth  # noqa: E402
import aula  # noqa: E402

HTTPS = "https://localhost"
CNPJ = "54993021000184"


def _manifesto(*videos):
    return {"gerado_em": "2026-11-12T10:00", "videos": list(videos)}


def _video(numero, slug, titulo):
    return {"numero": numero, "slug": slug, "titulo": titulo, "resumo": f"Resumo de {titulo}",
            "duracao_s": 95, "video": f"{slug}.mp4", "capa": f"{slug}.jpg"}


class AulaTest(unittest.TestCase):
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
        self._tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self._tmp.name) / "aula"
        self.pasta.mkdir()
        db = Path(self._tmp.name) / "dados.db"
        self._patches = [mock.patch.object(aula, "PASTA_AULA", self.pasta),
                         mock.patch.object(auth, "DB_PATH", db),
                         mock.patch.object(self.portal.envios, "DB_PATH", db)]
        for p in self._patches:
            p.start()
        self.cli = self.portal.app.test_client()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _gravar_manifesto(self, texto):
        (self.pasta / "aula.json").write_text(texto, encoding="utf-8")

    def test_aula_abre_sem_login(self):
        self._gravar_manifesto(json.dumps(_manifesto(_video(2, "acompanhar-o-dia", "Acompanhar o dia"),
                                                     _video(1, "primeiro-acesso", "Primeiro acesso"))))
        r = self.cli.get("/aula", base_url=HTTPS)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Primeiro acesso", html)
        self.assertIn("Acompanhar o dia", html)
        self.assertLess(html.index("Primeiro acesso"), html.index("Acompanhar o dia"))

    def test_aula_sem_manifesto(self):
        r = self.cli.get("/aula", base_url=HTTPS)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Os vídeos estão sendo preparados", r.get_data(as_text=True))

    def test_manifesto_quebrado(self):
        self._gravar_manifesto("{")
        r = self.cli.get("/aula", base_url=HTTPS)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Os vídeos estão sendo preparados", r.get_data(as_text=True))

    def test_arquivo_do_manifesto_serve_com_range(self):
        self._gravar_manifesto(json.dumps(_manifesto(_video(2, "acompanhar-o-dia", "Acompanhar o dia"))))
        (self.pasta / "acompanhar-o-dia.mp4").write_bytes(bytes(range(100)))
        r = self.cli.get("/aula/arquivo/acompanhar-o-dia.mp4", headers={"Range": "bytes=0-9"}, base_url=HTTPS)
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.get_data(), bytes(range(10)))
        self.assertIn("Content-Range", r.headers)

    def test_arquivo_fora_do_manifesto_404(self):
        self._gravar_manifesto(json.dumps(_manifesto(_video(2, "acompanhar-o-dia", "Acompanhar o dia"))))
        (self.pasta / "outro.mp4").write_bytes(b"x")
        self.assertEqual(self.cli.get("/aula/arquivo/outro.mp4", base_url=HTTPS).status_code, 404)
        self.assertEqual(self.cli.get("/aula/arquivo/..%2Fconfig.yaml", base_url=HTTPS).status_code, 404)
        self.assertEqual(self.cli.get("/aula/arquivo/aula.json", base_url=HTTPS).status_code, 404)

    def test_cliente_com_troca_pendente_abre_aula(self):
        conn = auth.conectar()
        conn.execute("CREATE TABLE interno (cnpj_embarcador TEXT, sender_id INTEGER, nome_remetente TEXT, "
                     "apelido TEXT, email TEXT)")
        conn.execute("INSERT INTO interno VALUES (?, 100, 'LOJA', NULL, 'a@b.c')", (CNPJ,))
        conn.commit()
        conta = auth.definir_pin(conn, CNPJ, "135790", trocar_pin=True)
        conn.close()
        with self.cli.session_transaction(base_url=HTTPS) as s:
            s["cnpj"], s["v"] = CNPJ, auth.versao_conta(conta)
        self.assertEqual(self.cli.get("/", base_url=HTTPS).status_code, 302)  # a trava da troca vale
        self.assertEqual(self.cli.get("/aula", base_url=HTTPS).status_code, 200)

    def test_login_tem_link_da_aula(self):
        html = self.cli.get("/login", base_url=HTTPS).get_data(as_text=True)
        with self.portal.app.test_request_context(base_url=HTTPS):
            self.assertIn(f'href="{self.portal.url_for("aula")}"', html)


if __name__ == "__main__":
    unittest.main()
