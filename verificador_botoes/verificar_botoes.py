# -*- coding: utf-8 -*-
"""
verificar_botoes.py -- verificador automatico dos botoes do painel interno.

Sobe o painel_agentes numa porta livre (8099) com o banco local, abre cada
tela num Chromium headless logado como nivel total e clica em cada botao
visivel. Toda chamada de rede que nao seja a carga da pagina ou arquivo
estatico e interceptada NO NAVEGADOR e respondida com JSON falso: nenhum
clique roda codigo no servidor (nem login na Stokki, nem escrita na Vuupt).
O que se prova e "o clique dispara a acao certa" -- rota existente, sem
erro de JavaScript, alguma reacao na tela.

COMO USAR (da raiz do projeto):
    py -3.11 -m verificador_botoes.verificar_botoes
    py -3.11 -m verificador_botoes.verificar_botoes --tela /torre --tela /planejamento
    py -3.11 -m verificador_botoes.verificar_botoes --sem-subir --porta 8099   (painel ja no ar)

Saida: verificador_botoes/relatorios/relatorio_<data>_<hora>.md e .json
(o JSON e o formato que a fase 2, --consertar, vai consumir).
"""
import argparse
import json
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml
from playwright.sync_api import Error as ErroPlaywright, sync_playwright

from verificador_botoes.classificador import (
    classificar, decidir_intercepcao, separar_erros, telas_do_mapa,
)
from verificador_botoes.relatorio import gerar_markdown, resumir

PASTA_RELATORIOS = Path(__file__).resolve().parent / "relatorios"
PASTA_PAINEL = _RAIZ / "painel_agentes"

# Coleta os botoes visiveis com um "caminho DOM" reproduzivel apos reload.
_JS_LISTAR_BOTOES = r"""
() => {
  const sel = 'button, input[type=submit], input[type=button], a[href], a[onclick], [role=button]';
  const visivel = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const caminho = el => {
    const partes = [];
    while (el && el.nodeType === 1 && el.tagName !== 'BODY') {
      if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
        partes.unshift('#' + CSS.escape(el.id));
        return partes.join(' > ');
      }
      let n = 1, irmao = el;
      while ((irmao = irmao.previousElementSibling)) if (irmao.tagName === el.tagName) n++;
      partes.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + n + ')');
      el = el.parentElement;
    }
    return 'body > ' + partes.join(' > ');
  };
  // Campo de texto vazio no mesmo form ou num container proximo (ate 3
  // niveis acima): sinal de que o botao precisa de dados pra fazer algo.
  const campoVazioPerto = el => {
    const tipos = 'input[type=text], input[type=number], input[type=search], input:not([type]), textarea';
    let raiz = el.form;
    if (!raiz) {
      raiz = el.parentElement;
      for (let i = 0; i < 3 && raiz && !raiz.querySelector(tipos); i++) raiz = raiz.parentElement;
    }
    if (!raiz) return null;
    for (const c of raiz.querySelectorAll(tipos)) {
      if (!c.disabled && visivel(c) && !(c.value || '').trim()) return c.id ? '#' + c.id : (c.name || c.placeholder || 'input');
    }
    return null;
  };
  const saida = [];
  for (const el of document.querySelectorAll(sel)) {
    if (!visivel(el)) continue;
    // Marcadores, popups e zoom do Leaflet sao da biblioteca, nao nossos.
    if (el.closest('.leaflet-container')) continue;
    const dados = {};
    for (const a of el.attributes) if (a.name.startsWith('data-')) dados[a.name] = a.value;
    const texto = (el.innerText || el.value || el.getAttribute('title') || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 50);
    saida.push({
      tag: el.tagName.toLowerCase(),
      texto,
      id: el.id || null,
      classes: el.className && typeof el.className === 'string' ? el.className.trim().slice(0, 80) : '',
      onclick: el.getAttribute('onclick'),
      href: el.getAttribute('href'),
      tipo: el.getAttribute('type'),
      dados,
      desabilitado: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      form_metodo: el.form ? (el.form.getAttribute('method') || 'get').toLowerCase() : null,
      form_invalido: !!(el.form && typeof el.form.checkValidity === 'function' && !el.form.checkValidity()),
      campo_vazio_perto: campoVazioPerto(el),
      caminho: caminho(el),
    });
  }
  return saida;
}
"""

# Contador de alteracoes no DOM + registro de window.open/print, instalado
# antes de cada pagina carregar.
_JS_INIT = r"""
window.__vb = { mutacoes: 0, abertos: [] };
// Observa `document` (sempre existe no init script; documentElement pode
// ainda nao existir e o observer morreria em silencio).
new MutationObserver(m => { window.__vb.mutacoes += m.length; })
  .observe(document, { childList: true, subtree: true, attributes: true, characterData: true });
const _open = window.open;
window.open = (url, ...r) => { window.__vb.abertos.push(String(url)); return { focus(){}, close(){}, closed: true, document: { write(){}, close(){} } }; };
window.print = () => { window.__vb.abertos.push('print()'); };
"""

_CORPO_FALSO_JSON = json.dumps({"ok": True, "sucesso": True, "falso_verificador": True})
_CORPO_FALSO_HTML = "<!doctype html><html><body><p>bloqueado pelo verificador de botoes</p></body></html>"


def _porta_livre(porta: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", porta)) != 0


def _esperar_porta(porta: int, segundos: int = 40):
    limite = time.time() + segundos
    while time.time() < limite:
        if not _porta_livre(porta):
            return
        time.sleep(0.5)
    raise RuntimeError(f"painel nao subiu na porta {porta} em {segundos}s")


def escolher_porta(porta: int | None) -> int:
    """Porta pedida, ou a primeira livre a partir da 8199 (a 8099 e a porta
    de teste manual do CLAUDE.md e costuma estar ocupada por outra sessao)."""
    if porta:
        if not _porta_livre(porta):
            raise RuntimeError(f"porta {porta} ja ocupada; use --sem-subir ou outra --porta")
        return porta
    for candidata in range(8199, 8149, -1):
        if _porta_livre(candidata):
            return candidata
    raise RuntimeError("nenhuma porta livre entre 8150 e 8199")


def subir_painel(porta: int) -> subprocess.Popen:
    """Sobe o painel_agentes local numa porta separada da 8070 (que pode ter
    uma instancia de verdade). Cookie de sessao sem Secure porque aqui e
    http://localhost. Filho nosso: e morto no final."""
    codigo = (
        "import painel_agentes; "
        "painel_agentes.app.config['SESSION_COOKIE_SECURE'] = False; "
        f"painel_agentes.app.run(host='127.0.0.1', port={porta}, threaded=True)"
    )
    log = open(PASTA_PAINEL / "dados" / "verificador_botoes_painel.log", "a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", codigo], cwd=str(PASTA_PAINEL),
                            stdout=log, stderr=subprocess.STDOUT)
    _esperar_porta(porta)
    return proc


def carregar_mapa_e_credenciais():
    """Importa o app do painel so pra ler o url_map (rotas + metodos) e le
    usuario/senha de nivel total do config.yaml. Os valores nunca sao
    impressos. Devolve tambem as rotas GET de /api/ que tocam a Stokki
    (fonte da view menciona "stokki"): essas nunca passam, porque login
    concorrente derruba a sessao da VPS."""
    import inspect
    sys.path.insert(0, str(PASTA_PAINEL))
    import painel_agentes  # noqa: E402
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        cfg = (yaml.safe_load(f) or {}).get("painel_agentes", {})
    if not cfg.get("usuario") or not cfg.get("senha"):
        raise RuntimeError("config.yaml sem painel_agentes.usuario/senha")
    app = painel_agentes.app
    proibidas = set()
    for regra in app.url_map.iter_rules():
        if not regra.rule.startswith("/api/") or "GET" not in (regra.methods or ()):
            continue
        try:
            fonte = inspect.getsource(app.view_functions[regra.endpoint])
        except (OSError, TypeError, KeyError):
            fonte = ""
        if "stokki" in fonte.lower():
            proibidas.add(regra.rule)
    return app.url_map, cfg["usuario"], cfg["senha"], proibidas


def _assinatura(botao: dict) -> str:
    """Botoes iguais repetidos em lista (um por pedido/rota) sao clicados so
    uma vez: mesma tag, texto, onclick sem numeros, classes e chaves data-*."""
    onclick = re.sub(r"['\"]?[\w#-]*\d[\w#-]*['\"]?", "N", botao.get("onclick") or "")
    texto = re.sub(r"\d+", "N", botao.get("texto") or "")
    return "|".join([botao["tag"], texto, onclick, botao.get("classes") or "",
                     ",".join(sorted(botao.get("dados", {}).keys())), botao.get("href") or ""])


def _descrever(botao: dict) -> str:
    partes = [botao["tag"]]
    if botao.get("texto"):
        partes.append(f"'{botao['texto']}'")
    elif botao.get("id"):
        partes.append(f"#{botao['id']}")
    elif botao.get("onclick"):
        partes.append(botao["onclick"][:40])
    elif botao.get("classes"):
        partes.append("." + botao["classes"].split()[0])
    return " ".join(partes)


def _e_link_puro(botao: dict) -> bool:
    href = botao.get("href")
    return bool(href) and not botao.get("onclick") and not href.startswith(("#", "javascript:"))


class Verificador:
    def __init__(self, mapa, base_url: str, espera_ms: int, profundidade: int, max_filhos: int,
                 api_leitura: bool = True, proibidas: set[str] | None = None):
        self.mapa = mapa
        self.base_url = base_url
        self.host = urlsplit(base_url).netloc
        self.espera_ms = espera_ms
        self.profundidade = profundidade
        self.max_filhos = max_filhos
        self.api_leitura = api_leitura
        self.proibidas = proibidas or set()
        self.chamadas: list[dict] = []
        self.erros: list[dict] = []
        self.dialogos: list[str] = []

    # -- navegador ---------------------------------------------------------
    def _rotear(self, route):
        req = route.request
        partes = urlsplit(req.url)
        local = partes.netloc == self.host
        decisao = decidir_intercepcao(partes.path, req.method, req.resource_type,
                                      navegacao=req.is_navigation_request(),
                                      api_leitura=self.api_leitura and local, proibidas=self.proibidas)
        caminho = partes.path + (("?" + partes.query) if partes.query else "") if local else req.url
        if decisao == "PASSAR" and (local or req.resource_type in ("stylesheet", "script", "font", "image")):
            if local and (partes.path.startswith("/api/") or req.is_navigation_request()):
                # Leitura real da API local ou navegacao (form GET, link):
                # conta como chamada do botao mesmo que o servidor demore
                # mais que a espera pra responder.
                self.chamadas.append({"t": time.time(), "metodo": req.method, "caminho": caminho,
                                      "externo": False, "tipo": req.resource_type, "passou": True,
                                      "navegacao": req.is_navigation_request()})
            return route.continue_()
        self.chamadas.append({"t": time.time(), "metodo": req.method, "caminho": caminho,
                              "externo": not local, "tipo": req.resource_type, "passou": False})
        if req.is_navigation_request():
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=_CORPO_FALSO_HTML)
        return route.fulfill(status=200, content_type="application/json", body=_CORPO_FALSO_JSON)

    def preparar_contexto(self, contexto):
        contexto.add_init_script(_JS_INIT)
        contexto.on("dialog", lambda d: (self.dialogos.append(f"{d.type}: {d.message[:80]}"), d.accept()))
        contexto.on("page", lambda p: p.on("pageerror", self._registrar_erro))
        contexto.route("**/*", self._rotear)

    def _registrar_erro(self, erro):
        self.erros.append({"t": time.time(), "msg": str(erro).split("\n")[0][:200]})

    def logar(self, pagina, usuario: str, senha: str):
        pagina.goto(self.base_url + "/login", wait_until="load")
        pagina.fill("input[name=usuario]", usuario)
        pagina.fill("input[name=senha]", senha)
        pagina.click("button[type=submit], input[type=submit]")
        pagina.wait_for_load_state("load")
        if "/login" in pagina.url:
            raise RuntimeError("login no painel falhou (confira painel_agentes.usuario/senha)")

    # -- telas -------------------------------------------------------------
    def _abrir(self, pagina, caminho: str):
        # Tema, menu recolhido e afins ficam no localStorage e vazariam de um
        # clique pro seguinte; cada botao parte do mesmo estado.
        try:
            pagina.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        except ErroPlaywright:
            pass
        resposta = pagina.goto(self.base_url + caminho, wait_until="load")
        self._esperar_sossego(pagina)
        return resposta

    def _esperar_sossego(self, pagina, max_ms: int = 12000):
        """Espera a tela parar de chamar a API (dados renderizados) antes de
        listar ou clicar: a Torre leva ~8 s pra montar os cartoes e, sem
        isso, botoes gerados por JS apareciam como 'filhos' de outro clique
        e depois como NAO_ENCONTRADO."""
        passado = 0
        while passado < max_ms:
            antes = len(self.chamadas)
            pagina.wait_for_timeout(800)
            passado += 800
            if len(self.chamadas) == antes:
                return

    def _limpar(self, pagina):
        self.chamadas.clear()
        self.erros.clear()
        self.dialogos.clear()
        try:
            pagina.evaluate("() => { window.__vb.mutacoes = 0; window.__vb.abertos = []; }")
        except ErroPlaywright:
            pass

    def _listar(self, pagina) -> list[dict]:
        try:
            return pagina.evaluate(_JS_LISTAR_BOTOES)
        except ErroPlaywright:
            return []

    def _clicar(self, pagina, botao: dict) -> str | None:
        """Devolve None se clicou, ou o motivo de nao ter conseguido."""
        alvo = pagina.locator(botao["caminho"]).first
        try:
            if alvo.count() == 0:
                return "NAO_ENCONTRADO"
        except ErroPlaywright as e:
            return f"NAO_ENCONTRADO ({str(e).splitlines()[0][:80]})"
        try:
            alvo.scroll_into_view_if_needed(timeout=2000)
        except ErroPlaywright:
            pass
        try:
            alvo.click(timeout=3000, no_wait_after=True)
        except ErroPlaywright:
            try:
                alvo.click(timeout=3000, no_wait_after=True, force=True)
            except ErroPlaywright:
                # Ultimo recurso (elemento fora da viewport num layout fixo):
                # dispara o evento direto, sem mover o mouse.
                try:
                    alvo.dispatch_event("click", timeout=2000)
                except ErroPlaywright as e:
                    return f"NAO_CLICAVEL ({str(e).splitlines()[0][:80]})"
        return None

    def _verificar_botao(self, pagina, caminho_tela: str, botao: dict, pai: dict | None) -> dict:
        registro = {"descricao": _descrever(botao), "seletor": botao["caminho"],
                    "pai": _descrever(pai) if pai else None, "onclick": botao.get("onclick"),
                    "href": botao.get("href"), "repeticoes": botao.get("repeticoes", 1)}
        if botao.get("desabilitado"):
            registro.update(status="DESABILITADO", detalhe="")
            return registro
        if _e_link_puro(botao):
            registro.update(classificar(self.mapa, {"href": botao["href"], "onclick": None}))
            return registro

        self._abrir(pagina, caminho_tela)
        if pai:
            motivo = self._clicar(pagina, pai)
            if motivo:
                registro.update(status="NAO_ENCONTRADO", detalhe=f"pai nao clicavel: {motivo}")
                return registro
            pagina.wait_for_timeout(self.espera_ms)
        self._limpar(pagina)
        url_antes = pagina.url.split("#")[0]

        motivo = self._clicar(pagina, botao)
        if motivo:
            registro.update(status=motivo.split(" ")[0], detalhe=motivo)
            return registro
        pagina.wait_for_timeout(self.espera_ms)

        try:
            estado = pagina.evaluate("() => window.__vb")
        except ErroPlaywright:
            estado = {"mutacoes": 0, "abertos": []}
        url_depois = pagina.url.split("#")[0]
        erros_do_botao, erros_pos = separar_erros(list(self.erros), list(self.chamadas))
        evento = {
            "chamadas": [{"metodo": c["metodo"], "caminho": c["caminho"], "externo": c["externo"]}
                         for c in self.chamadas],
            "erros_js": erros_do_botao,
            "navegou": url_depois != url_antes,
            "url_depois": url_depois.replace(self.base_url, ""),
            "mutacoes": (estado or {}).get("mutacoes", 0) + len((estado or {}).get("abertos", [])),
            "href": botao.get("href"), "onclick": botao.get("onclick"),
            "dialogos": list(self.dialogos),
            "form_invalido": bool(botao.get("form_invalido")),
            "campo_vazio_perto": botao.get("campo_vazio_perto"),
        }
        navegacoes = [c for c in self.chamadas if c.get("navegacao")]
        if navegacoes and not evento["navegou"]:
            evento["navegou"] = True
            evento["url_depois"] = navegacoes[-1]["caminho"] + " (ainda carregando)"
        registro.update(classificar(self.mapa, evento))
        avisos = []
        if (estado or {}).get("abertos"):
            avisos.append("abriu: " + ", ".join(estado["abertos"])[:120])
        if self.dialogos and "dialogo:" not in registro["detalhe"]:
            avisos.append("dialogo: " + "; ".join(self.dialogos)[:120])
        if erros_pos:
            avisos.append("erro JS apos resposta falsa (provavel artefato da trava): " + "; ".join(erros_pos)[:160])
        if avisos:
            registro["detalhe"] = (registro["detalhe"] + " | " if registro["detalhe"] else "") + " | ".join(avisos)
        registro["chamadas"] = evento["chamadas"]
        registro["erros_js"] = erros_do_botao
        registro["erros_pos_resposta"] = erros_pos

        # Botoes que so existem depois deste clique (modal, menu): 1 nivel.
        if self.profundidade > 0 and pai is None and not evento["navegou"]:
            registro["filhos"] = self._listar(pagina)
        return registro

    def _deduplicar(self, botoes: list[dict]) -> list[dict]:
        vistos: dict[str, dict] = {}
        for b in botoes:
            chave = _assinatura(b)
            if chave in vistos:
                vistos[chave]["repeticoes"] = vistos[chave].get("repeticoes", 1) + 1
            else:
                vistos[chave] = dict(b, repeticoes=1)
        return list(vistos.values())

    def verificar_tela(self, pagina, caminho: str, endpoint: str) -> dict:
        tela = {"caminho": caminho, "endpoint": endpoint, "botoes": []}
        self._limpar(pagina)
        resposta = self._abrir(pagina, caminho)
        tipo = (resposta.headers.get("content-type", "") if resposta else "")
        if resposta and resposta.status != 200:
            tela["pulada"] = f"HTTP {resposta.status}"
            return tela
        if "text/html" not in tipo:
            tela["pulada"] = f"nao e HTML ({tipo.split(';')[0] or '?'})"
            return tela
        if "/login" in pagina.url:
            tela["pulada"] = "redirecionou pro login"
            return tela

        iniciais = self._deduplicar(self._listar(pagina))
        assinaturas_iniciais = {_assinatura(b) for b in iniciais}
        print(f"  {caminho}: {len(iniciais)} botoes distintos")
        for botao in iniciais:
            registro = self._verificar_botao(pagina, caminho, botao, None)
            filhos = registro.pop("filhos", None) or []
            tela["botoes"].append(registro)
            print(f"    {registro['status']:16} {registro['descricao']}")
            novos = [f for f in self._deduplicar(filhos) if _assinatura(f) not in assinaturas_iniciais]
            for filho in novos[: self.max_filhos]:
                registro_filho = self._verificar_botao(pagina, caminho, filho, botao)
                tela["botoes"].append(registro_filho)
                print(f"      {registro_filho['status']:14} {registro_filho['descricao']} (via {registro['descricao']})")
        return tela


def main():
    parser = argparse.ArgumentParser(description="Verificador automatico dos botoes do painel interno.")
    parser.add_argument("--tela", action="append", help="caminho da tela (repetivel); padrao: todas as GET sem parametro")
    parser.add_argument("--exceto", action="append", help="tela a pular (repetivel), pra dividir a rodada")
    parser.add_argument("--porta", type=int, default=None, help="padrao: primeira livre a partir da 8199")
    parser.add_argument("--sem-subir", action="store_true", help="nao sobe o painel; usa o que ja estiver na porta")
    parser.add_argument("--espera", type=int, default=1500, help="ms de espera depois de cada clique")
    parser.add_argument("--profundidade", type=int, default=1, help="0 = so botoes da tela; 1 = tambem os que aparecem apos um clique")
    parser.add_argument("--max-filhos", type=int, default=12, help="maximo de botoes novos verificados por clique-pai")
    parser.add_argument("--visivel", action="store_true", help="abre o Chromium com janela (depuracao)")
    parser.add_argument("--api-falsa", action="store_true",
                        help="falsifica tambem os GET de /api/ (padrao: leituras passam pro painel local, menos as que tocam a Stokki)")
    args = parser.parse_args()

    mapa, usuario, senha, proibidas = carregar_mapa_e_credenciais()
    print(f"Rotas GET de API que nunca passam (tocam a Stokki): {sorted(proibidas) or 'nenhuma'}")
    telas = telas_do_mapa(mapa)
    if args.tela:
        pedidas = {t.rstrip("/") or "/" for t in args.tela}
        telas = [t for t in telas if t[0] in pedidas]
        faltando = pedidas - {t[0] for t in telas}
        if faltando:
            raise SystemExit(f"tela(s) nao encontrada(s) no mapa de rotas: {sorted(faltando)}")
    if args.exceto:
        puladas = {t.rstrip("/") or "/" for t in args.exceto}
        telas = [t for t in telas if t[0] not in puladas]

    porta = args.porta if args.sem_subir else escolher_porta(args.porta)
    if args.sem_subir and not porta:
        raise SystemExit("--sem-subir exige --porta")
    base_url = f"http://localhost:{porta}"
    proc = None if args.sem_subir else subir_painel(porta)
    print(f"Painel em {base_url}")
    inicio = datetime.now()
    resultado = {"gerado_em": inicio.strftime("%Y-%m-%d %H:%M"), "servico": "painel_agentes",
                 "base_url": base_url, "telas": []}
    try:
        with sync_playwright() as pw:
            # Camera falsa: a tela do WMS abre a camera pra ler codigo de
            # barras; sem isso o headless devolve "Not supported" e vira
            # ERRO_JS falso.
            navegador = pw.chromium.launch(headless=not args.visivel, args=[
                "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            contexto = navegador.new_context(viewport={"width": 1400, "height": 900},
                                             permissions=["camera"])
            verificador = Verificador(mapa, base_url, args.espera, args.profundidade, args.max_filhos,
                                      api_leitura=not args.api_falsa, proibidas=proibidas)
            pagina = contexto.new_page()
            pagina.on("pageerror", verificador._registrar_erro)
            verificador.logar(pagina, usuario, senha)
            verificador.preparar_contexto(contexto)
            print(f"Logado. {len(telas)} tela(s) a verificar.")
            for caminho, endpoint in telas:
                try:
                    resultado["telas"].append(verificador.verificar_tela(pagina, caminho, endpoint))
                except ErroPlaywright as e:
                    resultado["telas"].append({"caminho": caminho, "endpoint": endpoint, "botoes": [],
                                               "pulada": f"erro do navegador: {str(e).splitlines()[0][:120]}"})
                    pagina = contexto.new_page()
            navegador.close()
    finally:
        if proc:
            proc.kill()

    resultado["duracao_s"] = round((datetime.now() - inicio).total_seconds())
    PASTA_RELATORIOS.mkdir(exist_ok=True)
    nome = f"relatorio_{inicio.strftime('%Y-%m-%d_%H%M')}"
    (PASTA_RELATORIOS / f"{nome}.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
    (PASTA_RELATORIOS / f"{nome}.md").write_text(gerar_markdown(resultado), encoding="utf-8")
    resumo = resumir(resultado)
    print(f"\n{resumo['total']} botoes, {resumo['falhas']} falhas, {resultado['duracao_s']}s. "
          f"Por status: {resumo['por_status']}")
    print(f"Relatorio: {PASTA_RELATORIOS / (nome + '.md')}")
    return 1 if resumo["falhas"] else 0


if __name__ == "__main__":
    sys.exit(main())
