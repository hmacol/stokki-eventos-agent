# -*- coding: utf-8 -*-
"""
stokki/sondar_avancar_status.py -- SONDA SO LEITURA: como levar um pedido
Cross (Quatro Estrelas, item XD) de "Aberto" ate "Aguardando Transportador".

Hoje a equipe faz isso pelo coletor (separa e confere o item XD). Esta sonda
descobre as telas/acoes da Stokki envolvidas, sem clicar em nada:
  1. acha o embarcador pelo nome no diretorio (outbound/select);
  2. conta e lista os pedidos "Open" dele (so o codigo PS);
  3. no primeiro, le a pagina show: Situacao, formularios (id, action,
     nomes dos campos) e links/botoes com cara de separacao/conferencia;
  4. lista os links do menu com cara de coletor/separacao/conferencia.

So GET. Usa os cookies JA salvos (stokki/sessao_stokki.json); NAO faz
login: se a sessao nao valer, para. Nao imprime cookies, token, endereco
nem dado de cliente. Rodar na VPS, como www-data:
    sudo -u www-data venv/bin/python stokki/sondar_avancar_status.py [nome]
"""
import json
import re
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from stokki import pedidos as sp  # noqa: E402
from stokki.auth import COOKIES_PATH  # noqa: E402

PALAVRAS = ("separ", "picking", "pick", "confer", "check", "pack", "embal", "wave", "onda", "collect",
            "coletor", "mobile", "expedi", "shipment", "romaneio", "carrier", "transportador", "liberar",
            "finaliz", "avanc", "start", "iniciar")


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


def _tem_palavra(texto: str) -> bool:
    t = (texto or "").lower()
    return any(p in t for p in PALAVRAS)


def _caminho(href: str) -> str:
    return re.sub(r"^https?://[^/]+", "", href or "")


def main() -> int:
    nome = (sys.argv[1] if len(sys.argv) > 1 else "quatro estrelas").lower()
    s = _sessao_sem_login()

    try:
        embarcadores = sp.listar_embarcadores(s)
    except Exception as e:
        print(f"sessao nao valeu ou diretorio falhou: {type(e).__name__}")
        return 1
    lista = embarcadores if isinstance(embarcadores, list) else embarcadores.get("clients") or embarcadores.get("data") or []
    print(f"== diretorio: {len(lista)} embarcador(es); chaves do item: {sorted(lista[0].keys()) if lista and isinstance(lista[0], dict) else type(lista[0]).__name__ if lista else '-'}")
    achados = [e for e in lista if nome in json.dumps(e, ensure_ascii=False).lower()]
    for e in achados:
        print("   embarcador:", {k: v for k, v in e.items() if k in ("id", "name", "nome", "text", "value")} if isinstance(e, dict) else e)
    if not achados:
        print("nenhum embarcador com esse nome")
        return 1
    e0 = achados[0]
    cliente = str(e0.get("id") or e0.get("value") or "") if isinstance(e0, dict) else str(e0)

    contagem = sp.listar_pedidos(s, status="Open", cliente=cliente, por_pagina=100)
    linhas = contagem.get("aaData") or contagem.get("data") or []
    print(f"== pedidos Open do embarcador (filtro client={cliente}): recordsFiltered={contagem.get('recordsFiltered')} nesta pagina={len(linhas)}")
    ids = []
    for ln in linhas[:100]:
        pid = sp.extrair_id_da_linha(ln)
        ids.append(pid)
    print("   codigos:", ", ".join(f"PS-{i}" for i in ids if i))
    if linhas:
        acoes = linhas[0][-1] if isinstance(linhas[0], list) else (linhas[0].get("action") if isinstance(linhas[0], dict) else "")
        sa = BeautifulSoup(str(acoes or ""), "html.parser")
        print("== acoes na linha da listagem:")
        for a in sa.find_all(["a", "button"]):
            print("   ", a.name, _caminho(a.get("href") or a.get("data-url") or a.get("data-target") or ""),
                  "|", " ".join(a.get_text(" ", strip=True).split())[:60], "| title:", a.get("title") or "")
    if not ids or not ids[0]:
        return 0

    r = s.get(f"{sp.BASE_URL}/pt-br/administrator/inventory/outbound/show/{ids[0]}", headers={"Accept": "text/html"})
    print(f"== show PS-{ids[0]}: HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    rotulo = soup.find(string=re.compile(r"Situa"))
    if rotulo:
        print("   situacao:", " ".join(rotulo.find_parent().find_parent().get_text(" ", strip=True).split())[:120])
    print("== formularios:")
    for f in soup.find_all("form"):
        campos = [i.get("name") for i in f.find_all(["input", "select", "textarea"]) if i.get("name") and i.get("name") != "_token"]
        print("   ", f.get("id"), f.get("method"), _caminho(f.get("action") or ""), campos)
    print("== links/botoes da pagina com cara de separacao/conferencia/expedicao:")
    vistos = set()
    for a in soup.find_all(["a", "button"]):
        alvo = _caminho(a.get("href") or a.get("data-url") or a.get("data-action") or a.get("data-target") or "")
        texto = " ".join(a.get_text(" ", strip=True).split())[:60]
        if (_tem_palavra(alvo) or _tem_palavra(texto) or _tem_palavra(a.get("onclick") or "")) and (alvo, texto) not in vistos:
            vistos.add((alvo, texto))
            print("   ", a.name, a.get("id") or "", alvo, "|", texto, "| onclick:", (a.get("onclick") or "")[:60])
    print("== rotas citadas nos scripts da pagina (pick/separ/confer/pack/carrier):")
    rotas = set(re.findall(r"""['"](/pt-br/[a-z0-9_/\-{}]*(?:pick|separ|confer|check|pack|carrier|ship|wave|collect|mobile)[a-z0-9_/\-{}]*)['"]""",
                           r.text, re.IGNORECASE))
    for rota in sorted(rotas):
        print("   ", rota)
    return 0


if __name__ == "__main__":
    sys.exit(main())
