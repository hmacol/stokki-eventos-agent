# -*- coding: utf-8 -*-
"""
stokki/sondar_trocar_nf.py -- SONDA SO LEITURA do fluxo Cross (passo 0 do
roteiro docs/superpowers/plans/2026-10-25-importacao-fluxo-cross-piloto.md).

Prova, com as funcoes de stokki/trocar_nf.py, o que a troca de NF precisa e
nao foi possivel provar em 25/10:
  1. o _token do formulario "Informar Nota Fiscal" na listagem;
  2. o download do XML do pedido por /pt-br/xml/nfe/<base64 do caminho>
     (sem ele a conferencia cai para "o caminho mudou").

Usa os cookies JA salvos (stokki/sessao_stokki.json). NAO faz login: se a
sessao nao valer, para. Nao imprime cookies, token nem conteudo do XML.
Rodar na VPS, como www-data:
    sudo -u www-data venv/bin/python stokki/sondar_trocar_nf.py
"""
import json
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import requests  # noqa: E402

from stokki import pedidos as sp  # noqa: E402
from stokki import trocar_nf as tn  # noqa: E402
from stokki.auth import COOKIES_PATH  # noqa: E402


def _sessao_sem_login() -> requests.Session:
    dados = json.loads(Path(COOKIES_PATH).read_text(encoding="utf-8"))
    cookies = dados if isinstance(dados, list) else dados.get("cookies", [])
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                      "Accept-Language": "pt-BR,pt;q=0.9", "X-Requested-With": "XMLHttpRequest",
                      "Accept": "application/json, text/javascript, */*; q=0.01"})
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain") or "freshlog.stokki.com.br", path=c.get("path", "/"))
    return s


def main() -> int:
    s = _sessao_sem_login()
    r = s.get(tn.URL_LISTAGEM, headers={"Accept": "text/html", "X-Requested-With": ""}, allow_redirects=False, timeout=30)
    if r.status_code != 200 or "logout" not in r.text:
        print(f"Sessao salva nao vale (HTTP {r.status_code}). Nao vou logar. Parando.")
        return 2
    print("1. _token do form Informar Nota Fiscal na listagem:", bool(tn.token_csrf(s)))
    linhas = (sp.listar_pedidos(s, status="Waiting for Carrier", por_pagina=3) or {}).get("aaData") or []
    if not linhas:
        print("Nenhum pedido Aguardando Transportador pra testar o download.")
        return 1
    for linha in linhas:
        id_stokki, caminho = sp.extrair_id_da_linha(linha), tn._caminho_da_linha(linha)
        xml = tn.baixar_xml_por_caminho(s, caminho)
        print(f"2. pedido {id_stokki}: data-url {'ok' if caminho else 'AUSENTE'} | download "
              f"{'OK, ' + str(tn.contar_itens(xml)) + ' item(ns), chave ' + ('ok' if tn.chave_do_xml(xml) else 'AUSENTE') if xml else 'FALHOU'}")
    print("Conclusao: conferencia por", "itens (download funciona)" if any(
        tn.baixar_xml_por_caminho(s, tn._caminho_da_linha(l)) for l in linhas) else "caminho (download nao funciona)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
