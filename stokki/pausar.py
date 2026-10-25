# -*- coding: utf-8 -*-
"""
stokki/pausar.py

Coloca um pedido de saida "Em espera" na Stokki pelo botao "Pausar" da
pagina do pedido (sonda so leitura de 25/10/2026, task-5-report da feature
aviso-pendencias):

  GET  /administrator/inventory/outbound/show/{id}  -> "Situação" + _token e page do form_pause
  POST /administrator/inventory/outbound/pause      (FormData: _token, provider_outbound_id, page)
  GET  /administrator/inventory/outbound/show/{id}  -> confere que a Situação virou "Em espera"

Acao sincrona (sem poll, diferente do cancelamento); erro de validacao vem
em `errors` (Laravel 422). Sem campo de motivo: quem chama registra o porque
no nosso banco. Ainda NAO provado com escrita real: o texto exato da
Situação depois de pausar e o valor de `page` nao foram observados -- por
isso o resultado so e ok se a Situação relida contiver "espera"/"paus".
Desfazer = form_open (POST outbound/open, mesmos campos), nao implementado.

Mesmas regras de stokki/cancelar.py: StokkiSession (requests) e trava
stokki/sessao_uso.py com quem chama.
"""
import logging
import re

from stokki.cancelar import URL_SHOW, _RE_SITUACAO, _limpar
from stokki.pedidos import BASE_URL

logger = logging.getLogger(__name__)

URL_PAUSE = f"{BASE_URL}/pt-br/administrator/inventory/outbound/pause"

_RE_FORM = re.compile(r'<form[^>]*id="form_pause".*?</form>', re.S)
_RE_INPUT = r'<input[^>]*name="{nome}"[^>]*>'
_RE_VALOR = re.compile(r'value="([^"]*)"')
_PALAVRAS_EM_ESPERA = ("espera", "paus", "on hold")


def _valor_do_input(form_html: str, nome: str) -> str:
    m = re.search(_RE_INPUT.format(nome=re.escape(nome)), form_html)
    v = _RE_VALOR.search(m.group(0)) if m else None
    return v.group(1) if v else ""


def ler_pagina(sessao, id_stokki: int) -> tuple[str, str, str]:
    """(Situação, _token do form_pause, page do form_pause)."""
    resp = sessao.get(URL_SHOW.format(id=id_stokki))
    if hasattr(resp, "raise_for_status"):
        resp.raise_for_status()
    html = resp.text
    m = _RE_SITUACAO.search(html)
    situacao = _limpar(m.group(1)) if m else ""
    f = _RE_FORM.search(html)
    if not f:
        return situacao, "", ""
    return situacao, _valor_do_input(f.group(0), "_token"), _valor_do_input(f.group(0), "page")


def _em_espera(situacao: str) -> bool:
    s = situacao.lower()
    return any(p in s for p in _PALAVRAS_EM_ESPERA)


def pausar_pedido(sessao, id_stokki: int) -> dict:
    """Devolve {"ok", "situacao", "ja_estava", "erro"}. Nunca levanta por
    resposta da Stokki (so por falha de rede do requests)."""
    situacao, token, page = ler_pagina(sessao, id_stokki)
    sit = situacao.lower()
    if _em_espera(situacao):
        return {"ok": True, "situacao": situacao, "ja_estava": True, "erro": ""}
    if "cancel" in sit:
        return {"ok": False, "situacao": situacao, "ja_estava": False, "erro": f"pedido cancelado na Stokki ({situacao})"}
    if any(p in sit for p in ("enviado", "sent", "expedido")):
        return {"ok": False, "situacao": situacao, "ja_estava": False, "erro": f"pedido ja expedido na Stokki ({situacao})"}
    if not token:
        return {"ok": False, "situacao": situacao, "ja_estava": False,
                "erro": f"formulario de pausa nao encontrado na pagina (situacao {situacao!r})"}

    resp = sessao.post(
        URL_PAUSE,
        files={"_token": (None, token), "provider_outbound_id": (None, str(id_stokki)), "page": (None, page or "show")},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": URL_SHOW.format(id=id_stokki)},
    )
    try:
        corpo = resp.json()
    except ValueError:
        corpo = {}
    if not isinstance(corpo, dict):
        corpo = {}
    if resp.status_code != 200 or not corpo.get("success", True):
        erros = corpo.get("errors") or corpo.get("message") or resp.text[:200]
        return {"ok": False, "situacao": situacao, "ja_estava": False, "erro": f"Stokki {resp.status_code}: {erros}"}

    depois, _, _ = ler_pagina(sessao, id_stokki)
    ok = _em_espera(depois)
    logger.info(f"Stokki: pedido {id_stokki} pausado -> {depois!r} ({'ok' if ok else 'NAO ficou em espera'})")
    return {"ok": ok, "situacao": depois, "ja_estava": False,
            "erro": "" if ok else f"Stokki aceitou mas a situacao ficou {depois!r}"}
