# -*- coding: utf-8 -*-
"""
Motor de gravacao da aula: guarda de rede, linha do tempo, ffmpeg,
manifesto, narracao e as regras dos roteiros.

    py -3.11 -m unittest aula_portal.test_motor
"""
import fnmatch
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aula_portal import gravar, narracao, roteiro
from aula_portal.roteiro import Passo, Video


def _video(numero, slug, publicar=True):
    return Video(numero=numero, slug=slug, titulo=f"Titulo {numero}", resumo=f"Resumo {numero}", estado="padrao",
                 passos=[Passo("Ola.", [("pausa", 1)])], publicar=publicar)


class GuardaRedeTest(unittest.TestCase):
    def test_classificar_url(self):
        self.assertEqual(gravar.classificar_url("http://127.0.0.1:8097/api/dia?dias=14", 8097), "local")
        self.assertEqual(gravar.classificar_url("http://localhost:8097/", 8097), "local")
        self.assertEqual(gravar.classificar_url("data:image/png;base64,AAA", 8097), "local")
        self.assertEqual(gravar.classificar_url("blob:http://127.0.0.1:8097/x", 8097), "local")
        self.assertEqual(gravar.classificar_url("https://fonts.gstatic.com/s/x.woff2", 8097), "permitido")
        self.assertEqual(gravar.classificar_url("https://cdn.jsdelivr.net/npm/x@1/x.js", 8097), "permitido")
        for url in ("https://api.vuupt.com/api/v1/services", "https://freshlog.stokki.com.br/pt-br/",
                    "https://app.freshhub.com.br/cliente/", "http://127.0.0.1:8100/x", "https://api.anthropic.com/v1"):
            self.assertEqual(gravar.classificar_url(url, 8097), "proibido", url)


class InterceptacaoTest(unittest.TestCase):
    def test_dia_ficticio_em_todo_video(self):
        h = gravar.interceptadores(_video(1, "um"))
        self.assertTrue(any(fnmatch.fnmatch("http://127.0.0.1:8097/api/dia?dias=14", p) for p in h))

    def test_padroes_no_formato_do_playwright(self):
        # "*" do Playwright nao passa de "/": padrao de URL completa precisa de "**"
        for padrao in gravar.interceptadores(_video(1, "um")):
            self.assertTrue(padrao.startswith("**/"), padrao)
        for v in roteiro.VIDEOS:
            for padrao in v.intercepta:
                self.assertTrue(padrao.startswith("**/"), f"video {v.numero}: {padrao}")

    def test_navegador_sem_dns_pra_fora(self):
        # bloqueio de saida sem interceptar tudo (interceptar tudo tira o corpo do upload no Chromium);
        # proxy nao serve: o Playwright manda ate 127.0.0.1 pelo proxy
        regra = next(a for a in gravar.ARGS_NAVEGADOR if a.startswith("--host-resolver-rules="))
        self.assertIn("MAP * ~NOTFOUND", regra)
        self.assertIn("EXCLUDE 127.0.0.1", regra)

    def test_dia_segue_o_periodo_da_url(self):
        handler = next(f for p, f in gravar.interceptadores(_video(1, "um")).items() if "api/dia" in p)
        respostas = []

        class Rota:
            def fulfill(self, **kw):
                respostas.append(json.loads(kw["body"]))

        handler(Rota(), mock.Mock(url="http://127.0.0.1:8097/api/dia?dias=30"))
        handler(Rota(), mock.Mock(url="http://127.0.0.1:8097/api/dia"))
        self.assertEqual([r["periodo"]["dias"] for r in respostas], [30, 14])

    def test_video_sobrepoe_o_padrao(self):
        proprio = object()
        v = _video(1, "um")
        v.intercepta = {"**/api/dia*": proprio}
        h = gravar.interceptadores(v)
        self.assertIs(next(f for p, f in h.items() if fnmatch.fnmatch("http://x:1/api/dia?dias=1", p)), proprio)


class ConferenciaTest(unittest.TestCase):
    def test_bloqueio_no_servidor_reprova(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "bloqueios.log"
            gravar.conferir_bloqueios(_video(1, "um"), [], log)  # sem arquivo: passa
            log.write_text("smtp ('smtp.gmail.com', 587)", encoding="utf-8")
            with self.assertRaises(gravar.GravacaoFalhou):
                gravar.conferir_bloqueios(_video(1, "um"), [], log)

    def test_url_proibida_reprova(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(gravar.GravacaoFalhou):
                gravar.conferir_bloqueios(_video(1, "um"), ["https://api.vuupt.com/x"], Path(tmp) / "nada.log")


class MarcasTest(unittest.TestCase):
    """Sincronia pela imagem: cada passo pinta uma cor no canto e a fala vai
    pro instante em que a cor aparece no video bruto (o video do Playwright
    atrasa em relacao ao relogio; visto 12/11, fecho sumindo do fim)."""

    def test_classificar_cor(self):
        self.assertEqual(gravar.classificar_cor((250, 12, 5)), 0)
        self.assertEqual(gravar.classificar_cor(gravar.PALETA_MARCAS[3]), 3)
        self.assertIsNone(gravar.classificar_cor((128, 128, 128)))

    def test_achar_marcas(self):
        p = gravar.PALETA_MARCAS
        cores = [None] * 5 + [p[0]] * 10 + [None] * 3 + [p[1]] * 20 + [p[2]] * 5
        self.assertEqual(gravar.achar_marcas(cores, 25, 3), [0.2, 0.72, 1.52])

    def test_marca_repetida_depois_de_navegar_nao_conta_de_novo(self):
        p = gravar.PALETA_MARCAS
        cores = [p[0]] * 4 + [None] * 2 + [p[0]] * 4 + [p[1]] * 4
        self.assertEqual(gravar.achar_marcas(cores, 25, 2), [0.0, 0.4])

    def test_marca_que_nao_aparece_reprova(self):
        p = gravar.PALETA_MARCAS
        with self.assertRaises(gravar.GravacaoFalhou):
            gravar.achar_marcas([p[0]] * 5, 25, 2)

    def test_montagem_apaga_a_marca(self):
        cmd = " ".join(gravar.comando_ffmpeg(Path("v.webm"), [(Path("a.mp3"), 0.0)], Path("s.mp4"), corte=0.0, total=5.0))
        self.assertIn("delogo=x=1:y=1:w=20:h=20", cmd)


class LinhaDoTempoTest(unittest.TestCase):
    def test_linha_do_tempo_sem_sobreposicao(self):
        self.assertEqual(gravar.linha_do_tempo([0, 2, 3], [4, 1, 1]), [0, 4, 5])
        self.assertEqual(gravar.linha_do_tempo([0, 10], [2, 2]), [0, 10])


class FfmpegTest(unittest.TestCase):
    def test_comando_ffmpeg(self):
        cmd = gravar.comando_ffmpeg(Path("v.webm"), [(Path("a.mp3"), 0.0), (Path("b.mp3"), 4.0)], Path("s.mp4"),
                                    corte=1.5, total=9.0)
        texto = " ".join(cmd)
        self.assertIn("adelay=4000|4000", texto)
        self.assertIn("amix=inputs=2", texto)
        self.assertIn("libx264", texto)
        self.assertIn("aac", texto)
        self.assertIn("+faststart", texto)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "1.500")
        self.assertEqual(cmd[-1], "s.mp4")


class ManifestoTest(unittest.TestCase):
    def test_manifesto_so_publicados(self):
        with tempfile.TemporaryDirectory() as tmp:
            saida = Path(tmp)
            for slug in ("um", "dois"):
                (saida / f"{slug}.mp4").write_bytes(b"x")
                (saida / f"{slug}.jpg").write_bytes(b"x")
                (saida / f"{slug}.json").write_text(json.dumps({"duracao_s": 101}), encoding="utf-8")
            videos = [_video(2, "dois", publicar=False), _video(1, "um"), _video(3, "tres")]
            caminho = gravar.escrever_manifesto(videos, saida)
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        self.assertEqual([v["slug"] for v in dados["videos"]], ["um"])
        self.assertEqual(dados["videos"][0], {"numero": 1, "slug": "um", "titulo": "Titulo 1", "resumo": "Resumo 1",
                                              "duracao_s": 101, "video": "um.mp4", "capa": "um.jpg"})
        self.assertIn("gerado_em", dados)


class NarracaoTest(unittest.TestCase):
    def test_narracao_cache(self):
        chamadas = []

        class FalsoCommunicate:
            def __init__(self, texto, voz):
                chamadas.append((texto, voz))

            async def save(self, caminho):
                Path(caminho).write_bytes(b"mp3")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(narracao.edge_tts, "Communicate", FalsoCommunicate):
            a = narracao.gerar("Bom dia.", Path(tmp))
            b = narracao.gerar("Bom dia.", Path(tmp))
        self.assertEqual(a, b)
        self.assertEqual(chamadas, [("Bom dia.", "pt-BR-ThalitaMultilingualNeural")])

    def test_narracao_tenta_de_novo(self):
        falhas = [narracao.edge_tts.exceptions.NoAudioReceived("sem audio")]

        class FalhaUmaVez:
            def __init__(self, texto, voz):
                pass

            async def save(self, caminho):
                if falhas:
                    raise falhas.pop()
                Path(caminho).write_bytes(b"mp3")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(narracao.edge_tts, "Communicate", FalhaUmaVez), \
                mock.patch.object(narracao.time, "sleep"):
            self.assertEqual(narracao.gerar("Oi.", Path(tmp)).read_bytes(), b"mp3")


class RoteirosTest(unittest.TestCase):
    def _textos(self):
        for v in roteiro.VIDEOS:
            yield v.titulo
            yield v.resumo
            for p in v.passos:
                yield p.fala
                yield p.legenda or ""
                for a in p.acoes:
                    yield from (str(x) for x in a[1:])

    def test_roteiros_sem_pin_padrao(self):
        for t in self._textos():
            self.assertNotIn("123456", t)

    def test_roteiros_numeracao(self):
        numeros = [v.numero for v in roteiro.VIDEOS]
        self.assertEqual(sorted(numeros), list(range(1, 12)))
        self.assertEqual(len({v.slug for v in roteiro.VIDEOS}), len(roteiro.VIDEOS))

    def test_video3_pede_nao_altera(self):
        v3 = next(v for v in roteiro.VIDEOS if v.numero == 3)
        falas = " ".join(p.fala for p in v3.passos).lower()
        self.assertTrue("pedido" in falas or "pedir" in falas)
        self.assertNotIn("você altera", falas)
        self.assertNotIn("alterar sozinho", falas)
        self.assertFalse(v3.publicar)

    def test_fecho_anuncia_o_proximo_publicado(self):
        # a pagina so lista os publicados: anunciar o 3 ou o 10 antes da liberacao confunde
        videos = sorted(roteiro.VIDEOS, key=lambda v: v.numero)
        for v in videos:
            seguinte = next((o for o in videos if o.numero > v.numero and o.publicar), None)
            fala = v.passos[-1].fala
            if seguinte:
                self.assertEqual(fala, f"No próximo vídeo: {seguinte.titulo}.", f"video {v.numero}")
            else:
                self.assertTrue(fala.startswith("Pronto!"), f"video {v.numero}")

    def test_video4_canhoto_abre_em_nova_aba(self):
        # no portal real o link do canhoto tem target=_blank (so a gravacao abre na mesma aba)
        falas = " ".join(p.fala for p in next(v for v in roteiro.VIDEOS if v.numero == 4).passos).lower()
        self.assertIn("nova aba", falas)
        self.assertNotIn("botão voltar", falas)

    def test_video10_espera_liberacao(self):
        self.assertFalse(next(v for v in roteiro.VIDEOS if v.numero == 10).publicar)

    def test_acoes_conhecidas(self):
        for v in roteiro.VIDEOS:
            for p in v.passos:
                for a in p.acoes:
                    self.assertIn(a[0], gravar.ACOES, f"video {v.numero}: {a}")


if __name__ == "__main__":
    unittest.main()


class OverlayTest(unittest.TestCase):
    """A camada da gravacao no Chromium de verdade: cartao em tela cheia e
    legenda embaixo, inclusive com um <dialog> modal aberto por cima."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls._pw = sync_playwright().start()
        cls._nav = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls._nav.close()
        cls._pw.stop()

    def _pagina(self):
        pg = self._nav.new_page(viewport={"width": 1280, "height": 720})
        pg.add_init_script("window.__aulaCfg = {};")
        pg.add_init_script(gravar.OVERLAY.read_text(encoding="utf-8"))
        pg.goto("data:text/html,<html><head></head><body><dialog id='d' style='width:1200px;height:680px;max-width:none;max-height:none'>janela</dialog></body></html>")
        self.addCleanup(pg.close)
        return pg

    def test_cartao_em_tela_cheia(self):
        pg = self._pagina()
        pg.evaluate("window.aula.cartao('Titulo', 'Sub')")
        caixa = pg.evaluate("(() => { const r = document.getElementById('aula-cartao').getBoundingClientRect(); "
                            "return [r.left, r.top, r.width, r.height]; })()")
        self.assertEqual(caixa, [0, 0, 1280, 720])

    def test_legenda_embaixo_e_acima_do_dialog(self):
        pg = self._pagina()
        pg.evaluate("document.getElementById('d').showModal()")
        pg.evaluate("window.aula.legenda('Oi')")
        r = pg.evaluate("(() => { const r = document.getElementById('aula-legenda').getBoundingClientRect(); "
                        "return [r.left + r.width / 2, r.bottom]; })()")
        self.assertAlmostEqual(r[0], 640, delta=2)
        self.assertAlmostEqual(r[1], 720 - 28, delta=2)
        # elementFromPoint ignora pointer-events:none (a legenda): confere o pixel desenhado
        import io as _io
        from PIL import Image
        ponto = pg.evaluate("(() => { const r = document.getElementById('aula-legenda').getBoundingClientRect(); "
                            "return [r.left + 6, r.top + r.height / 2]; })()")
        pg.wait_for_timeout(400)  # a legenda entra com transicao de opacidade (0,25 s)
        png = pg.screenshot(clip={"x": ponto[0], "y": ponto[1], "width": 1, "height": 1})
        self.assertLess(sum(Image.open(_io.BytesIO(png)).convert("RGB").getpixel((0, 0))), 200)  # escuro = legenda por cima

    def test_marca_no_canto_por_cima_de_tudo(self):
        import io as _io
        from PIL import Image
        pg = self._nav.new_page(viewport={"width": 1280, "height": 720})
        self.addCleanup(pg.close)
        cfg = {"paleta": gravar.PALETA_MARCAS, "marca": [gravar.MARCA_X, gravar.MARCA_Y, gravar.MARCA_LADO]}
        pg.add_init_script(f"window.__aulaCfg = {json.dumps(cfg)};")
        pg.add_init_script(gravar.OVERLAY.read_text(encoding="utf-8"))
        pg.goto("data:text/html,<html><head></head><body><dialog id='d' style='width:1270px;height:710px;"
                "max-width:none;max-height:none'>x</dialog></body></html>")
        pg.evaluate("window.aula.cartao('T', 'S')")
        pg.evaluate("document.getElementById('d').showModal()")
        pg.evaluate("window.aula.marca(2)")
        c = gravar.MARCA_X + gravar.MARCA_LADO // 2
        png = pg.screenshot(clip={"x": c, "y": c, "width": 1, "height": 1})
        self.assertEqual(gravar.classificar_cor(Image.open(_io.BytesIO(png)).convert("RGB").getpixel((0, 0))), 2)

    def test_repintar_sobrevive_a_navegacao(self):
        pg = self._nav.new_page()
        self.addCleanup(pg.close)
        pg.add_init_script(gravar.OVERLAY.read_text(encoding="utf-8"))
        pg.goto("data:text/html,<html><head></head><body>A</body></html>")
        pg.goto("data:text/html,<html><head></head><body>B</body></html>")
        exe = gravar._Executor(pg, "")
        exe.rodar(("js", "history.back()"))
        exe.repintar("window.aula.marca(1)")   # nao pode cair com "Execution context was destroyed"
        self.assertIn("A", pg.content())
