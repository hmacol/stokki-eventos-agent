# -*- coding: utf-8 -*-
"""
Sonda SOMENTE LEITURA do botao "Pausar" da Stokki (tela outbound/show/{id}).

Roda na VPS como www-data, dentro da trava da sessao Stokki:
  cd /opt/stokki-eventos && sudo -u www-data venv/bin/python sondar_solicitacoes.py [id_stokki]

Faz um unico GET na pagina do pedido (em 'Waiting for Carrier' se nao
informar id), salva o HTML em /tmp, imprime so os trechos do Pausar
(formularios, rotas e scripts que mencionam pausa) com _token e valores
mascarados, e apaga o /tmp. Nenhum POST, nenhuma escrita.
"""
import os
import re
import sqlite3
import sys

sys.path.insert(0, "/opt/stokki-eventos")
sys.path.insert(0, "/opt/stokki-eventos/portal_cliente")

CHAVES = re.compile(r"paus|pause|hold|espera|resume|retom", re.I)


def _mascarar(txt: str) -> str:
    txt = re.sub(r'(name="_token"[^>]*value=")[^"]+', r"\1<REDIGIDO>", txt)
    txt = re.sub(r'(value=")[^"]{25,}(")', r"\1<REDIGIDO>\2", txt)
    txt = re.sub(r"(_token['\"]?\s*[:=]\s*['\"])[^'\"]+", r"\1<REDIGIDO>", txt)
    txt = re.sub(r"(csrf[-_]token['\"]?\s*(?:content)?=?\s*['\"])[^'\"]+", r"\1<REDIGIDO>", txt, flags=re.I)
    return txt


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
    print("== situacao ==")
    m = re.search(r"Situa[çc][ãa]o:\s*</th>\s*<td>(.*?)</td>", html, re.S)
    print(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else "?")

    print("== formularios (action/method/campos) ==")
    for f in re.finditer(r"<form\b([^>]*)>(.*?)</form>", html, re.S | re.I):
        attrs, corpo = f.group(1), f.group(2)
        campos = re.findall(r"<(?:input|textarea|select)\b[^>]*name=\"([^\"]+)\"", corpo)
        print(_mascarar(re.sub(r"\s+", " ", attrs))[:250], "| campos:", campos)

    print("== elementos com pausa (botao/modal) ==")
    for m in re.finditer(r"<(?:button|a|div|h\d)\b[^>]*>(?:(?!</).){0,120}", html, re.S | re.I):
        s = m.group(0)
        if CHAVES.search(s):
            print(_mascarar(re.sub(r"\s+", " ", s))[:300])

    print("== rotas outbound/ no HTML ==")
    rotas = sorted(set(re.findall(r"[\w/.:-]*administrator/inventory/outbound/[\w/{}.-]*", html)))
    for r in rotas:
        print(re.sub(r"/\d+", "/<ID>", r))

    print("== scripts com pausa (linhas) ==")
    for sc in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I):
        linhas = sc.group(1).splitlines()
        for i, ln in enumerate(linhas):
            if CHAVES.search(ln):
                ini, fim = max(0, i - 6), min(len(linhas), i + 14)
                print("---")
                for x in linhas[ini:fim]:
                    print(_mascarar(x.strip())[:240])


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
        return 0
    tmp = f"/tmp/sonda_pausar_{os.getpid()}.html"
    try:
        s = StokkiSession(config, conta=conta)
        resp = s.get(URL_SHOW.format(id=id_stokki))
        resp.raise_for_status()
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(resp.text)
        _trechos(resp.text)
    finally:
        sessao_uso.liberar("aviso-sonda")
        if os.path.exists(tmp):
            os.remove(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
