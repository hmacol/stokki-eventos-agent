# -*- coding: utf-8 -*-
"""
Grava os videos da aula interna da equipe (/painel/aula).

    py -3.11 -m aula_equipe.gravar --video 1
    py -3.11 -m aula_equipe.gravar --todos
    py -3.11 -m aula_equipe.gravar --manifesto

Mesmo motor da aula do cliente (aula_portal.gravar: marca de cor por passo,
narracao da Francisca, ffmpeg), com o painel no lugar do portal e dois
formatos: "retrato" (720x1280, a tela de celular, pra assistir no WhatsApp
em tela cheia) e "paisagem" (1280x720, a tela do computador, com a parte do
celular da doca numa moldura de celular no meio). Saida em
aula_equipe/saida/ (<slug>.mp4, .jpg, .json e aula.json).
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import imageio_ffmpeg

import aula_equipe  # noqa: F401  (poe painel_agentes/ no sys.path)
from aula_equipe import RAIZ, ambiente, roteiro
from aula_equipe.roteiro import Video
from aula_portal import narracao, servidor
from aula_portal.gravar import (ANTES_DO_CLIQUE, ARGS_NAVEGADOR, FOLGA_FALA, FPS, MARCA_LADO, MARCA_X, MARCA_Y,
                                PALETA_MARCAS, TECLA_MS, GravacaoFalhou, achar_marcas, classificar_url, comando_ffmpeg,
                                conferir_bloqueios, cores_do_canto, linha_do_tempo, momento_capa)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SAIDA = Path(__file__).resolve().parent / "saida"
OVERLAY = RAIZ / "aula_portal" / "overlay.js"
EXTRA = Path(__file__).resolve().parent / "extra.js"
FORMATOS = {   # viewport (pixel da pagina), escala do video, onde a legenda fica
    "retrato": {"viewport": (360, 640), "escala": 2, "legenda_baixo": 96},
    # computador com zoom de 125% (tela de 1024 px num video 1280x720): o texto le no celular
    "paisagem": {"viewport": (1024, 576), "escala": 1.25, "legenda_baixo": None},
}
ACOES = ("ir", "clicar", "digitar", "preencher", "escolher", "arquivo", "esperar", "rolar", "destacar", "cartao",
         "fechar_cartao", "pausa", "js", "py", "celular", "computador", "assinar")


def tracos_assinatura(x: float, y: float, largura: float, altura: float) -> list[list[tuple[float, float]]]:
    """Uma rubrica cursiva (lacos que andam pra direita) e o risco embaixo,
    dentro da caixa do canvas. Cada traco e uma lista de pontos."""
    import math
    s = altura / 180
    x0, y0 = x + largura * .14, y + altura * .5
    alturas = [1.0, .5, .72, .42, .62, .36, .5]   # um laco alto (a "inicial") e os outros variando
    lacos = []
    for t in range(0, 105):
        a = alturas[min(t // 15, len(alturas) - 1)]
        lacos.append((x0 + t * 2.3 * s + 9 * s * math.cos(t * .42) + (12 * s if t < 15 else 0) * math.sin(t * .21),
                      y0 - 34 * a * s * math.sin(t * .42) - t * .18 * s))
    risco = [(x0 + 8 * s + t * 3.6 * s, y0 + 30 * s - t * .35 * s + 2 * s * math.sin(t * .3)) for t in range(0, 62)]
    return [lacos, risco]


def tamanho_video(formato: str) -> tuple[int, int]:
    f = FORMATOS[formato]
    return round(f["viewport"][0] * f["escala"]), round(f["viewport"][1] * f["escala"])


def escrever_manifesto(videos: list[Video], saida: Path = SAIDA) -> Path:
    itens = []
    for v in sorted(videos, key=lambda v: v.numero):
        meta = saida / f"{v.slug}.json"
        if not v.publicar or not (saida / f"{v.slug}.mp4").exists() or not meta.exists():
            continue
        largura, altura = tamanho_video(v.formato)
        itens.append({"numero": v.numero, "slug": v.slug, "titulo": v.titulo, "resumo": v.resumo,
                      "duracao_s": int(json.loads(meta.read_text(encoding="utf-8"))["duracao_s"]),
                      "video": f"{v.slug}.mp4", "capa": f"{v.slug}.jpg", "largura": largura, "altura": altura})
    caminho = saida / "aula.json"
    caminho.write_text(json.dumps({"gerado_em": datetime.now().strftime("%Y-%m-%dT%H:%M"), "videos": itens},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
    return caminho


# ── Executor ───────────────────────────────────────────────────────────────────

class _Executor:
    """Roda as acoes na pagina ou, depois de ("celular",), dentro da tela da
    moldura de celular. A camada da aula (legenda, cursor, anel) fica sempre
    na pagina de cima: o destaque usa a caixa do elemento medida pelo
    Playwright, que ja vem nas coordenadas da pagina de cima."""

    def __init__(self, page, base: str):
        self.page, self.base, self.alvo = page, base, page

    def _destacar(self, sel: str, anel: bool = True):
        loc = self.alvo.locator(sel).first
        # no meio da tela: no pe dela ficam a legenda e o botao fixo de confirmar
        fora = loc.evaluate("e => { const r = e.getBoundingClientRect();"
                            " return r.top < 60 || r.bottom > innerHeight * 0.66; }")
        if fora:
            loc.evaluate("e => e.scrollIntoView({block: 'center', behavior: 'smooth'})")
            time.sleep(0.7)
        time.sleep(0.15)
        caixa = loc.bounding_box()
        if not caixa:
            raise GravacaoFalhou(f"elemento sem caixa (escondido?): {sel}")
        r = {"left": caixa["x"], "top": caixa["y"], "width": caixa["width"], "height": caixa["height"]}
        x, y = self.page.evaluate("([r, a]) => window.aula.destacarCaixa(r, a)", [r, anel])
        time.sleep(ANTES_DO_CLIQUE)
        return x, y

    def repintar(self, js: str) -> None:
        from playwright.sync_api import Error as ErroPlaywright
        for tentativa in range(5):
            try:
                self.page.evaluate(js)
                return
            except ErroPlaywright:
                if tentativa == 4:
                    raise
                time.sleep(0.3)
                self.page.wait_for_load_state("load")

    def _assentar(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001  (tela que consulta sozinha nunca "assenta"; segue)
            pass

    def rodar(self, acao: tuple) -> None:
        tipo, *args = acao
        p, a = self.page, self.alvo
        if tipo == "ir":
            self.alvo = p
            p.goto(self.base + args[0])
            self._assentar()
        elif tipo == "celular":       # dai em diante, as acoes sao na tela dentro da moldura
            p.wait_for_selector("iframe#celular")
            self.alvo = p.frame(name="celular")
            self.alvo.wait_for_load_state("load")
            time.sleep(0.4)
        elif tipo == "computador":
            self.alvo = p
        elif tipo == "clicar":
            sel = args[0]
            a.evaluate("s => { const e = document.querySelector(s); if (e && e.target === '_blank') e.removeAttribute('target'); }", sel)
            x, y = self._destacar(sel, anel=False)
            p.evaluate("([x, y]) => window.aula.clique(x, y)", [x, y])
            a.locator(sel).first.click()
            self._assentar()
        elif tipo == "digitar":
            sel, texto = args
            self._destacar(sel, anel=False)
            a.locator(sel).first.click()
            a.locator(sel).first.press_sequentially(texto, delay=TECLA_MS)
        elif tipo == "preencher":
            sel, valor = args
            self._destacar(sel, anel=False)
            a.locator(sel).first.fill(valor)
        elif tipo == "escolher":
            sel, valor = args
            self._destacar(sel)
            a.locator(sel).first.select_option(valor)
            time.sleep(0.8)
        elif tipo == "arquivo":
            sel_input, caminhos, *visivel = args
            if visivel:
                x, y = self._destacar(visivel[0])
                p.evaluate("([x, y]) => window.aula.clique(x, y)", [x, y])
            a.locator(sel_input).first.set_input_files([str(c) for c in caminhos])
            time.sleep(0.8)
        elif tipo == "esperar":
            a.wait_for_selector(args[0], timeout=15000)
        elif tipo == "rolar":
            a.evaluate("s => document.querySelector(s).scrollIntoView({behavior: 'smooth', block: 'center'})", args[0])
            time.sleep(0.9)
        elif tipo == "destacar":
            self._destacar(args[0])
        elif tipo == "assinar":       # assinatura com o mouse (pointer events) no canvas
            self._destacar(args[0], anel=False)
            caixa = a.locator(args[0]).first.bounding_box()
            for traco in tracos_assinatura(caixa["x"], caixa["y"], caixa["width"], caixa["height"]):
                p.mouse.move(*traco[0])
                p.mouse.down()
                for ponto in traco[1:]:
                    p.mouse.move(*ponto, steps=2)
                p.mouse.up()
            time.sleep(0.3)
        elif tipo == "cartao":
            p.evaluate("([t, s]) => window.aula.cartao(t, s)", list(args))
        elif tipo == "fechar_cartao":
            p.evaluate("() => window.aula.fecharCartao()")
            time.sleep(1.0)
        elif tipo == "pausa":
            time.sleep(float(args[0]))
        elif tipo == "js":
            p.evaluate(args[0])
        elif tipo == "py":
            args[0]()
        else:
            raise ValueError(f"acao desconhecida: {acao}")


def _entrar(page, base: str, nivel: str) -> None:
    """Login fora do video, pela API do contexto (o cookie vale pra pagina):
    a tela inicial do login (Galpao, Expedicao) nem abre. A Expedicao consulta
    a Vuupt ao abrir, e o route() nao pega o redirecionamento do login."""
    r = page.context.request.post(base + "/login", form={"usuario": ambiente.USUARIOS[nivel], "senha": ambiente.SENHA},
                                  max_redirects=0)
    if r.status != 302 or "/login" in r.headers.get("location", ""):
        raise GravacaoFalhou(f"login {nivel} recusado na gravacao ({r.status})")


def gravar_video(video: Video, raiz: Path = RAIZ, porta: int = ambiente.PORTA_PADRAO, saida: Path = SAIDA) -> dict:
    from playwright.sync_api import sync_playwright

    saida.mkdir(parents=True, exist_ok=True)
    falas = [narracao.gerar(p.fala, saida / "voz") for p in video.passos]
    duracoes = [narracao.duracao(f) for f in falas]
    fmt = FORMATOS[video.formato]
    largura, altura = tamanho_video(video.formato)

    ambiente.escrever_config(raiz)
    ambiente.semear_banco(raiz, video.estado)
    servidor.ARQ_BLOQUEIOS.unlink(missing_ok=True)
    proc = ambiente.subir_painel(raiz, porta)
    base = f"http://127.0.0.1:{porta}"
    proibidas: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="aula_equipe_"))
    try:
        with sync_playwright() as pw:
            args = ARGS_NAVEGADOR + ([f"--force-device-scale-factor={fmt['escala']}"] if fmt["escala"] != 1 else [])
            nav = pw.chromium.launch(args=args)
            ctx = nav.new_context(viewport={"width": fmt["viewport"][0], "height": fmt["viewport"][1]},
                                  record_video_dir=str(tmp), record_video_size={"width": largura, "height": altura},
                                  locale="pt-BR", timezone_id="America/Sao_Paulo")
            cfg = {"semDica": True, "paleta": PALETA_MARCAS, "marca": [MARCA_X, MARCA_Y, MARCA_LADO],
                   "legendaBaixo": fmt["legenda_baixo"]}
            ctx.add_init_script(f"window.__aulaCfg = {json.dumps(cfg)};")
            ctx.add_init_script(OVERLAY.read_text(encoding="utf-8"))
            ctx.add_init_script(EXTRA.read_text(encoding="utf-8"))
            for padrao, handler in video.intercepta.items():
                ctx.route(padrao, handler)
            ctx.on("request", lambda req: proibidas.append(req.url)
                   if classificar_url(req.url, porta) == "proibido" else None)
            page = ctx.new_page()
            page.on("dialog", lambda d: d.accept())   # "Confirmar a saida de N pedidos?" e afins
            _entrar(page, base, video.login)
            page.goto(base + video.inicio)
            page.wait_for_load_state("networkidle")
            t0 = time.monotonic()
            exe = _Executor(page, base)
            for n, (passo, dur) in enumerate(zip(video.passos, duracoes)):
                inicio = time.monotonic() - t0
                legenda = json.dumps(passo.legenda or passo.fala)
                marca = f"window.aula && window.aula.marca({n})"
                exe.repintar(f"window.aula && window.aula.legenda({legenda})")
                exe.repintar(marca)
                for acao in passo.acoes:
                    exe.rodar(acao)
                    exe.repintar(marca)
                    if acao[0] in ("ir", "clicar", "js"):
                        exe.repintar(f"window.aula && !document.getElementById('aula-cartao') && window.aula.legenda({legenda})")
                resta = inicio + dur + FOLGA_FALA - (time.monotonic() - t0)
                if resta > 0:
                    time.sleep(resta)
            time.sleep(0.6)
            webm = Path(page.video.path())
            ctx.close()
            nav.close()
    finally:
        ambiente.parar_painel(proc)

    try:
        conferir_bloqueios(video, proibidas, servidor.ARQ_BLOQUEIOS)
    except GravacaoFalhou:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    escala = fmt["escala"]
    marcas = achar_marcas(cores_do_canto(webm, escala), FPS, len(video.passos))
    corte = marcas[0]
    tempos = linha_do_tempo([m - corte for m in marcas], duracoes)
    mp4 = saida / f"{video.slug}.mp4"
    total = min(narracao.duracao(webm) - corte, tempos[-1] + duracoes[-1] + FOLGA_FALA + 0.6)
    subprocess.run(comando_ffmpeg(webm, list(zip(falas, tempos)), mp4, corte, total, escala), check=True)
    capa = min(momento_capa(tempos, total) + 1.5, total - 0.5)
    lado_capa = "scale=640:-2" if largura >= altura else "scale=-2:640"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", f"{capa:.2f}", "-i", str(mp4),
                    "-frames:v", "1", "-vf", lado_capa, "-q:v", "3", str(saida / f"{video.slug}.jpg")], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    meta = {"numero": video.numero, "slug": video.slug, "duracao_s": round(total), "formato": video.formato,
            "gravado_em": datetime.now().isoformat(timespec="seconds"), "passos": len(video.passos)}
    (saida / f"{video.slug}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grava os videos da aula da equipe.")
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--video", type=int, action="append", help="numero do video (pode repetir)")
    grupo.add_argument("--todos", action="store_true")
    grupo.add_argument("--manifesto", action="store_true", help="so reescreve aula.json com o que ja foi gravado")
    ap.add_argument("--porta", type=int, default=ambiente.PORTA_PADRAO)
    args = ap.parse_args(argv)

    if not args.manifesto:
        alvo = roteiro.VIDEOS if args.todos else [v for v in roteiro.VIDEOS if v.numero in set(args.video)]
        if not alvo:
            ap.error("nenhum video com esse numero")
        for v in sorted(alvo, key=lambda v: v.numero):
            print(f"gravando {v.numero} {v.slug}...", flush=True)
            meta = gravar_video(v, porta=args.porta)
            print(f"  ok: {SAIDA / (v.slug + '.mp4')} ({meta['duracao_s']} s)", flush=True)
    print(f"manifesto: {escrever_manifesto(roteiro.VIDEOS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
