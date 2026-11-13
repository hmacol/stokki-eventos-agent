# -*- coding: utf-8 -*-
"""
Aula da equipe (/aula, 13/11): videos das telas, so com login.

    py -3.11 -m unittest test_aula_equipe   (de dentro de painel_agentes/)
"""
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

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("painel_agentes_app", _AQUI / "painel_agentes.py")
painel_agentes = sys.modules.get(_spec.name)
if painel_agentes is None:
    painel_agentes = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = painel_agentes
    _spec.loader.exec_module(painel_agentes)
import acesso_telas  # noqa: E402


def _video(numero, slug, largura=720, altura=1280):
    return {"numero": numero, "slug": slug, "titulo": slug.title(), "resumo": "r", "duracao_s": 90,
            "video": f"{slug}.mp4", "capa": f"{slug}.jpg", "largura": largura, "altura": altura}


class TestAulaEquipe(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.pasta = tmp / "aula_equipe"
        self.pasta.mkdir()
        base = painel_agentes._carregar_config()
        cfg_painel = {**base.get("painel_agentes", {}), "secret_key": "x" * 32, "usuario": "u_total", "senha": "s_total",
                      "usuario_galpao": "galpao", "senha_galpao": "s_galpao", "usuario_operador": "op",
                      "senha_operador": "s_op"}
        cfg = {**base, "painel_agentes": cfg_painel}
        self._patches = [mock.patch.object(painel_agentes, "_carregar_config", return_value=cfg),
                         mock.patch.object(painel_agentes, "PASTA_AULA_EQUIPE", self.pasta),
                         mock.patch.object(acesso_telas, "DB_PATH", tmp / "t.db")]
        for p in self._patches:
            p.start()
        painel_agentes.app.config["TESTING"] = True
        self.cli = painel_agentes.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _manifesto(self, *videos):
        (self.pasta / "aula.json").write_text(json.dumps({"videos": list(videos)}), encoding="utf-8")

    def _entrar(self, usuario="galpao", senha="s_galpao", **query):
        return self.cli.post("/login", query_string=query, data={"usuario": usuario, "senha": senha})

    def test_sem_login_vai_pro_login_e_volta_pra_aula(self):
        r = self.cli.get("/aula")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login?proximo=aula", r.headers["Location"])
        r = self._entrar(proximo="aula")
        self.assertEqual(r.headers["Location"], "/aula")

    def test_login_normal_continua_caindo_na_tela_do_nivel(self):
        self.assertEqual(self._entrar().headers["Location"], "/wms")
        self.assertEqual(self._entrar(proximo="https://fora.com").headers["Location"], "/wms")   # so "aula" muda o destino

    def test_galpao_ve_os_videos_em_pe_e_deitado(self):
        self._manifesto(_video(2, "coletas", 1280, 720), _video(1, "retornos"))
        self._entrar()
        html = self.cli.get("/aula").get_data(as_text=True)
        self.assertLess(html.index("Retornos"), html.index("Coletas"))   # ordem do numero
        self.assertIn('class="em-pe"', html)                              # capa do video em pe
        self.assertIn('href="/wms"', html)                                # "Voltar ao painel" vai pra tela do nivel

    def test_sem_manifesto_mostra_preparando(self):
        self._entrar()
        self.assertIn("sendo preparados", self.cli.get("/aula").get_data(as_text=True))

    def test_arquivo_do_manifesto_com_range_e_o_resto_404(self):
        self._manifesto(_video(1, "retornos"))
        (self.pasta / "retornos.mp4").write_bytes(bytes(range(100)))
        (self.pasta / "outro.mp4").write_bytes(b"x")
        self._entrar()
        with self.cli.get("/aula/arquivo/retornos.mp4", headers={"Range": "bytes=0-9"}) as r:
            self.assertEqual(r.status_code, 206)
            self.assertEqual(r.get_data(), bytes(range(10)))
            self.assertIn("private", r.headers["Cache-Control"])
        for nome in ("outro.mp4", "aula.json", "..%2Fconfig.yaml"):
            self.assertEqual(self.cli.get(f"/aula/arquivo/{nome}").status_code, 404, nome)

    def test_arquivo_sem_login_nao_sai(self):
        self._manifesto(_video(1, "retornos"))
        (self.pasta / "retornos.mp4").write_bytes(b"x")
        self.assertEqual(self.cli.get("/aula/arquivo/retornos.mp4").status_code, 302)

    def test_acesso_personalizado_abre_a_aula_e_ve_no_menu(self):
        conn = acesso_telas.conectar()
        try:
            acesso_telas.importar_do_config(conn, {"usuarios_extras": [{"usuario": "ana", "senha": "s_ana", "nivel": "leitura"}]})
            acesso_telas.salvar(conn, "ana", ["historico_tratativas"], "u_total")
        finally:
            conn.close()
        self._manifesto(_video(1, "retornos"))
        (self.pasta / "retornos.mp4").write_bytes(b"x")
        self._entrar("ana", "s_ana")
        self.assertEqual(self.cli.get("/aula").status_code, 200)
        with self.cli.get("/aula/arquivo/retornos.mp4") as r:
            self.assertEqual(r.status_code, 200)
        self.assertIn("Aula (vídeos)", self.cli.get("/historico-tratativas").get_data(as_text=True))

    def test_caminhos_da_aula_sao_comuns_a_todas_as_telas(self):
        self.assertTrue(acesso_telas.caminho_liberado("/aula", []))
        self.assertTrue(acesso_telas.caminho_liberado("/aula/arquivo/retornos.mp4", []))
        self.assertFalse(acesso_telas.caminho_liberado("/aula/arquivo/a/b", []))


if __name__ == "__main__":
    unittest.main()
