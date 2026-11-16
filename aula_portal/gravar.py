# -*- coding: utf-8 -*-
"""
Grava os videos da aula do portal (/cliente/aula).

    py -3.11 -m aula_portal.gravar --video 11
    py -3.11 -m aula_portal.gravar --todos
    py -3.11 -m aula_portal.gravar --manifesto

Para cada video: semeia o banco ficticio, sobe o portal deste checkout
(ambiente.py), roda o roteiro no Chromium gravando a tela, narra cada passo
com a Francisca e junta tudo no ffmpeg. Saida em aula_portal/saida/
(<slug>.mp4, <slug>.jpg, <slug>.json e aula.json). Qualquer requisicao pra
fora da maquina (Stokki, Vuupt, freshhub...) e bloqueada e FALHA a gravacao.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import imageio_ffmpeg

from aula_portal import RAIZ, ambiente, dados_teste as dt, narracao, roteiro, servidor
from aula_portal.roteiro import Video

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SAIDA = Path(__file__).resolve().parent / "saida"
OVERLAY = Path(__file__).resolve().parent / "overlay.js"
HOSTS_PERMITIDOS = ("fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.cloudflare.com", "cdn.jsdelivr.net")
ACOES = ("ir", "clicar", "digitar", "preencher", "escolher", "arquivo", "enviar_nfe", "esperar", "rolar", "destacar", "cartao", "fechar_cartao",
         "pausa", "js", "py")
FOLGA_FALA = 0.4      # respiro depois de cada fala
TECLA_MS = 70         # digitacao "humana"
ANTES_DO_CLIQUE = 0.75  # o cursor chega e o anel aparece antes do clique
# Saida da maquina bloqueada no proprio navegador: nenhum nome de host resolve
# (so 127.0.0.1 fica de fora da regra). Nao da pra interceptar tudo com route() -- o Chromium
# perde o corpo do upload (multipart com arquivo) -- nem usar proxy: o
# Playwright manda ate 127.0.0.1 por ele.
ARGS_NAVEGADOR = ["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"]


class GravacaoFalhou(RuntimeError):
    pass


# ── Pecas puras (testadas) ─────────────────────────────────────────────────────

def classificar_url(url: str, porta: int) -> str:
    if url.startswith(("data:", "blob:", "about:")):
        return "local"
    u = urlparse(url)
    if u.hostname in ("127.0.0.1", "localhost") and u.port == porta:
        return "local"
    if u.hostname in HOSTS_PERMITIDOS:
        return "permitido"
    return "proibido"


# Sincronia pela imagem: cada passo pinta esta cor (ciclo) num quadrado no canto
# (MARCA_*) e a fala vai pro instante em que ela aparece no video bruto -- o
# video do Playwright atrasa em relacao ao relogio. A montagem apaga o quadrado.
PALETA_MARCAS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255)]
MARCA_X, MARCA_Y, MARCA_LADO = 3, 3, 16
FPS = 25


def classificar_cor(cor) -> int | None:
    """Indice da cor da paleta mais perto (ou None se nada esta perto)."""
    dist, idx = min((sum((a - b) ** 2 for a, b in zip(cor, c)), i) for i, c in enumerate(PALETA_MARCAS))
    return idx if dist < 100 ** 2 else None


def achar_marcas(cores: list, fps: int, n: int) -> list[float]:
    """Segundo (no video bruto) em que cada passo 0..n-1 aparece, lendo as
    cores do canto quadro a quadro, em ordem."""
    tempos, i = [], 0
    for quadro, cor in enumerate(cores):
        if i == n:
            break
        if cor is not None and classificar_cor(cor) == i % len(PALETA_MARCAS):
            tempos.append(round(quadro / fps, 3))
            i += 1
    if i < n:
        raise GravacaoFalhou(f"marca do passo {i} nao apareceu no video bruto")
    return tempos


def cores_do_canto(webm: Path, escala: float = 1) -> list[tuple]:
    """Cor media do quadrado da marca em cada quadro (FPS) do video bruto.
    escala = pixels do video por pixel da pagina (2 no video em pe, 1,25 no
    deitado da aula da equipe)."""
    m, lado = round((MARCA_X + 2) * escala), round((MARCA_LADO - 4) * escala)
    r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", str(webm), "-vf",
                        f"fps={FPS},crop={lado}:{lado}:{m}:{m},scale=1:1:flags=area", "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return [tuple(r.stdout[k:k + 3]) for k in range(0, len(r.stdout) - 2, 3)]


def linha_do_tempo(inicios: list[float], duracoes: list[float]) -> list[float]:
    """Cada fala comeca no seu passo, mas nunca antes de a anterior acabar."""
    saida, fim = [], 0.0
    for ini, dur in zip(inicios, duracoes):
        comeco = max(ini, fim)
        saida.append(comeco)
        fim = comeco + dur
    return saida


def momento_capa(tempos: list[float], total: float) -> float:
    """Inicio do passo em ~40% do video (a tela do assunto, nao o cartao de
    titulo do 1o passo); nunca antes do 2o passo."""
    alvo = total * 0.4
    return max([t for t in tempos if t <= alvo] + [tempos[min(1, len(tempos) - 1)]])


def comando_ffmpeg(webm: Path, falas: list[tuple[Path, float]], saida: Path, corte: float, total: float,
                   escala: float = 1) -> list[str]:
    """Video cortado em `corte` s + cada fala atrasada pro seu instante
    (relativo ao corte), misturadas sem normalizar o volume."""
    mx, my, lado = (round((MARCA_X - 2) * escala), round((MARCA_Y - 2) * escala),
                    round((MARCA_LADO + 4) * escala))
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", f"{corte:.3f}", "-i", str(webm)]
    for mp3, _ in falas:
        cmd += ["-i", str(mp3)]
    filtros = [f"[{i + 1}:a]adelay={int(round(t * 1000))}|{int(round(t * 1000))}[a{i}]" for i, (_, t) in enumerate(falas)]
    filtros.append("".join(f"[a{i}]" for i in range(len(falas))) + f"amix=inputs={len(falas)}:normalize=0[aout]")
    filtros.append(f"[0:v]delogo=x={mx}:y={my}:w={lado}:h={lado}[vout]")
    cmd += ["-filter_complex", ";".join(filtros), "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p", "-r", "25",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(saida)]
    return cmd


def _dias_da_url(url: str) -> int | None:
    valor = parse_qs(urlparse(url).query).get("dias", [""])[0]
    return int(valor) if valor.isdigit() else None


def interceptadores(video: Video) -> dict:
    """O dia ficticio vale em todo video (sem ele a tela tentaria a Vuupt e
    mostraria erro); o que o video declara vem antes e sobrepoe."""
    padrao = {"**/api/dia*": roteiro.json_fixo(lambda r: dt.dia(date.today(), _dias_da_url(r.url)))}
    return {**video.intercepta, **{k: v for k, v in padrao.items() if k not in video.intercepta}}


def conferir_bloqueios(video: Video, proibidas: list[str], arquivo: Path) -> None:
    """Reprova o video se o navegador tentou sair (proibidas) ou se o portal
    da gravacao registrou SMTP/socket pra fora (servidor.guarda_rede)."""
    servidor_tentou = arquivo.read_text(encoding="utf-8").splitlines() if arquivo.exists() else []
    servidor_tentou = [linha for linha in servidor_tentou if linha.strip()]
    if proibidas or servidor_tentou:
        raise GravacaoFalhou(f"video {video.numero}: tentativas de sair bloqueadas -- navegador: "
                             f"{sorted(set(proibidas))}; portal: {servidor_tentou}")


def escrever_manifesto(videos: list[Video], saida: Path = SAIDA) -> Path:
    itens = []
    for v in sorted(videos, key=lambda v: v.numero):
        meta = saida / f"{v.slug}.json"
        if not v.publicar or not (saida / f"{v.slug}.mp4").exists() or not meta.exists():
            continue
        itens.append({"numero": v.numero, "slug": v.slug, "titulo": v.titulo, "resumo": v.resumo,
                      "duracao_s": int(json.loads(meta.read_text(encoding="utf-8"))["duracao_s"]),
                      "video": f"{v.slug}.mp4", "capa": f"{v.slug}.jpg"})
    caminho = saida / "aula.json"
    caminho.write_text(json.dumps({"gerado_em": datetime.now().strftime("%Y-%m-%dT%H:%M"), "videos": itens},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
    return caminho


# ── Executor ───────────────────────────────────────────────────────────────────

class _Executor:
    def __init__(self, page, base: str):
        self.page, self.base = page, base

    def _destacar(self, sel: str, anel: bool = True):
        self.page.locator(sel).first.scroll_into_view_if_needed()
        x, y = self.page.evaluate("([s, a]) => window.aula.destacar(s, a)", [sel, anel])
        time.sleep(ANTES_DO_CLIQUE)
        return x, y

    def repintar(self, js: str) -> None:
        """Legenda/marca de volta depois de uma acao. Se a acao disparou uma
        navegacao (ex.: history.back()), espera a pagina nova e tenta de novo."""
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

    def rodar(self, acao: tuple) -> None:
        tipo, *args = acao
        p = self.page
        if tipo == "ir":
            p.goto(self.base + args[0])
            p.wait_for_load_state("networkidle")
        elif tipo == "clicar":
            sel = args[0]
            p.evaluate("s => { const e = document.querySelector(s); if (e && e.target === '_blank') e.removeAttribute('target'); }", sel)
            x, y = self._destacar(sel, anel=False)
            p.evaluate("([x, y]) => window.aula.clique(x, y)", [x, y])
            p.locator(sel).first.click()
            try:
                p.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001  (polling da tela nunca "assenta"; segue)
                pass
        elif tipo == "digitar":
            sel, texto = args
            self._destacar(sel, anel=False)
            p.locator(sel).first.click()
            p.locator(sel).first.press_sequentially(texto, delay=TECLA_MS)
        elif tipo == "preencher":
            sel, valor = args
            self._destacar(sel, anel=False)
            p.locator(sel).first.fill(valor)
        elif tipo == "enviar_nfe":
            # as 3 NF-e (2 boas + 1 de outro CNPJ) e o boleto da NF 5201, gerados agora
            sel_input, visivel = args
            pasta = Path(tempfile.mkdtemp(prefix="aula_nfe_"))
            self._destacar(visivel)
            p.locator(sel_input).first.set_input_files([str(c) for c in dt.xmls_nfe(pasta) + [dt.boleto_pdf(pasta)]])
        elif tipo == "escolher":
            sel, valor = args
            self._destacar(sel)
            p.locator(sel).first.select_option(valor)
            time.sleep(0.8)
        elif tipo == "arquivo":
            sel_input, caminhos, *visivel = args
            if visivel:
                self._destacar(visivel[0])
            p.locator(sel_input).first.set_input_files([str(c) for c in caminhos])
        elif tipo == "esperar":
            p.wait_for_selector(args[0], timeout=15000)
        elif tipo == "rolar":
            p.evaluate("s => document.querySelector(s).scrollIntoView({behavior: 'smooth', block: 'center'})", args[0])
            time.sleep(0.9)
        elif tipo == "destacar":
            self._destacar(args[0])
        elif tipo == "cartao":
            p.evaluate("([t, s]) => window.aula.cartao(t, s)", list(args))
        elif tipo == "fechar_cartao":
            p.evaluate("() => window.aula.fecharCartao()")
            time.sleep(1.0)  # quem assiste ve a tela antes do primeiro clique
        elif tipo == "pausa":
            time.sleep(float(args[0]))
        elif tipo == "js":
            p.evaluate(args[0])
        elif tipo == "py":  # mexe no banco ficticio no meio do video (ex.: a equipe responde o chat)
            args[0]()
        else:
            raise ValueError(f"acao desconhecida: {acao}")


def _entrar(page, base: str) -> None:
    page.goto(base + "/login")
    page.fill("#cnpj", dt.CNPJ)
    page.fill("#pin", dt.PIN_GRAVACAO)
    page.click("button.principal")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1200)


def gravar_video(video: Video, raiz: Path = RAIZ, porta: int = ambiente.PORTA_PADRAO, saida: Path = SAIDA) -> dict:
    from playwright.sync_api import sync_playwright

    saida.mkdir(parents=True, exist_ok=True)
    cache_voz = saida / "voz"
    falas = [narracao.gerar(p.fala, cache_voz) for p in video.passos]
    duracoes = [narracao.duracao(f) for f in falas]

    ambiente.escrever_config(raiz)
    ambiente.semear_banco(raiz, video.estado)
    servidor.ARQ_BLOQUEIOS.unlink(missing_ok=True)
    proc = ambiente.subir_portal(raiz, porta)
    base = f"http://127.0.0.1:{porta}"
    proibidas: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="aula_"))
    try:
        with sync_playwright() as pw:
            nav = pw.chromium.launch(args=ARGS_NAVEGADOR)
            ctx = nav.new_context(viewport={"width": 1280, "height": 720}, record_video_dir=str(tmp),
                                  record_video_size={"width": 1280, "height": 720},
                                  locale="pt-BR", timezone_id="America/Sao_Paulo")
            ctx.add_init_script(f"window.__aulaCfg = {json.dumps({'semDica': not video.mostrar_chat, 'paleta': PALETA_MARCAS, 'marca': [MARCA_X, MARCA_Y, MARCA_LADO]})};")
            ctx.add_init_script(OVERLAY.read_text(encoding="utf-8"))

            for padrao, handler in interceptadores(video).items():
                ctx.route(padrao, handler)
            ctx.on("request", lambda req: proibidas.append(req.url)
                   if classificar_url(req.url, porta) == "proibido" else None)
            page = ctx.new_page()
            t0 = time.monotonic()
            if video.entrar:
                _entrar(page, base)
            else:
                page.goto(base + video.inicio)
                page.wait_for_load_state("networkidle")
            exe = _Executor(page, base)
            for n, (passo, dur) in enumerate(zip(video.passos, duracoes)):
                inicio = time.monotonic() - t0
                marca = f"window.aula && window.aula.marca({n})"
                exe.repintar(f"window.aula && window.aula.legenda({json.dumps(passo.legenda or passo.fala)})")
                exe.repintar(marca)
                for acao in passo.acoes:
                    exe.rodar(acao)
                    exe.repintar(marca)  # navegou, abriu <dialog> ou cartao: a marca volta pra cima
                    if acao[0] in ("ir", "clicar", "js"):  # pode ter navegado: a legenda volta na pagina nova
                        exe.repintar(f"window.aula && !document.getElementById('aula-cartao') && "
                                     f"window.aula.legenda({json.dumps(passo.legenda or passo.fala)})")
                resta = inicio + dur + FOLGA_FALA - (time.monotonic() - t0)
                if resta > 0:
                    time.sleep(resta)
            time.sleep(0.6)
            webm = Path(page.video.path())
            ctx.close()
            nav.close()
    finally:
        ambiente.parar_portal(proc)

    try:
        conferir_bloqueios(video, proibidas, servidor.ARQ_BLOQUEIOS)
    except GravacaoFalhou:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    marcas = achar_marcas(cores_do_canto(webm), FPS, len(video.passos))
    corte = marcas[0]
    tempos = linha_do_tempo([m - corte for m in marcas], duracoes)
    mp4 = saida / f"{video.slug}.mp4"
    total = min(narracao.duracao(webm) - corte, tempos[-1] + duracoes[-1] + FOLGA_FALA + 0.6)
    subprocess.run(comando_ffmpeg(webm, list(zip(falas, tempos)), mp4, corte, total), check=True)
    capa = min(momento_capa(tempos, total) + 1.5, total - 0.5)   # 1,5 s depois: a acao do passo ja na tela
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", f"{capa:.2f}", "-i", str(mp4),
                    "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(saida / f"{video.slug}.jpg")], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    meta = {"numero": video.numero, "slug": video.slug, "duracao_s": round(total),
            "gravado_em": datetime.now().isoformat(timespec="seconds"), "passos": len(video.passos)}
    (saida / f"{video.slug}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grava os videos da aula do portal.")
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
    caminho = escrever_manifesto(roteiro.VIDEOS)
    print(f"manifesto: {caminho}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
