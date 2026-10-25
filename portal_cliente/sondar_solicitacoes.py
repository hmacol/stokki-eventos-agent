# -*- coding: utf-8 -*-
"""
Sonda SOMENTE LEITURA do botao "Pausar" da Stokki (tela outbound/show/{id}).

Roda na VPS como www-data, dentro da trava da sessao Stokki:
  cd /opt/stokki-eventos && sudo -u www-data venv/bin/python sondar_solicitacoes.py [id_stokki]

Faz um unico GET na pagina do pedido (em 'Waiting for Carrier' se nao
informar id), mantem o HTML so em memoria e imprime apenas ids de form,
action, metodo, nomes de campo e caminhos de endpoint. Nenhum POST, nenhuma
escrita, nada em disco. Trava ocupada -> exit 2.
"""
import re
import sqlite3
import sys

sys.path.insert(0, "/opt/stokki-eventos")
sys.path.insert(0, "/opt/stokki-eventos/portal_cliente")

CHAVES = re.compile(r"paus|pause|hold|espera|resume|retom", re.I)


def _escolher_pedido() -> tuple[int, str]:
    con = sqlite3.connect("file:/opt/stokki-eventos/dados/dados.db?mode=ro", uri=True)
    try:
        l = con.execute(
            "SELECT id_stokki, status_stokki FROM vigia_stokki_abertos "
            "WHERE status_stokki LIKE '%Waiting for Carrier%' LIMIT 1").fetchone()
    finally:
        con.close()
    if not l:
        raise SystemExit("nenhum pedido Waiting for Carrier em vigia_stokki_abertos")
    return int(l[0]), l[1]


def _trechos(html: str) -> None:
    """Imprime so ids de form, action, metodo, NOMES de campo e caminhos de
    endpoint -- nunca texto em volta (podia ter dado do destinatario)."""
    print("== formularios (id / method / action / nomes dos campos) ==")
    for f in re.finditer(r"<form\b([^>]*)>(.*?)</form>", html, re.S | re.I):
        attrs, corpo = f.group(1), f.group(2)
        fid = re.search(r'\bid="([^"]*)"', attrs)
        met = re.search(r'\bmethod="([^"]*)"', attrs)
        act = re.search(r'\baction="([^"]*)"', attrs)
        campos = re.findall(r"<(?:input|textarea|select)\b[^>]*name=\"([^\"]+)\"", corpo)
        print(fid.group(1) if fid else "?", "|", met.group(1) if met else "?", "|",
              _caminho(act.group(1)) if act else "(sem action)", "| campos:", campos)

    print("== rotas outbound/ no HTML ==")
    for r in sorted(set(re.findall(r"administrator/inventory/outbound/[\w/{}.-]*", html))):
        print(_caminho(r))

    print("== endpoints chamados em scripts de pausa ==")
    for sc in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I):
        corpo = sc.group(1)
        for m in re.finditer(r"\$\('#(form_\w+)'\)\.submit", corpo):
            if CHAVES.search(m.group(1)):
                trecho = corpo[m.end(): m.end() + 1500]
                u = re.search(r"url:\s*['\"][^'\"]*?(administrator/[\w/.-]*)", trecho)
                t = re.search(r"type:\s*['\"](\w+)['\"]", trecho)
                print(m.group(1), "->", t.group(1) if t else "?", _caminho(u.group(1)) if u else "?")


def _caminho(url: str) -> str:
    """So o caminho, com ids numericos trocados por <ID>."""
    url = re.sub(r"^https?://[^/]+", "", url)
    return re.sub(r"/\d+", "/<ID>", url)


def main() -> int:
    from enviar_stokki import carregar_config
    from stokki import sessao_uso
    from stokki.auth import StokkiSession
    from stokki.cancelar import URL_SHOW

    if len(sys.argv) > 1:
        id_stokki = int(sys.argv[1])
    else:
        id_stokki, st = _escolher_pedido()
        print("pedido escolhido, status:", st)
    config = carregar_config()
    conta = sessao_uso.adquirir("aviso-sonda", ttl_segundos=300, esperar_segundos=300, alternativa=False)
    if not conta:
        print("trava ocupada:", sessao_uso.em_uso())
        return 2
    try:
        s = StokkiSession(config, conta=conta)
        resp = s.get(URL_SHOW.format(id=id_stokki))
        resp.raise_for_status()
        _trechos(resp.text)
    finally:
        sessao_uso.liberar("aviso-sonda")
    return 0


if __name__ == "__main__":
    sys.exit(main())
