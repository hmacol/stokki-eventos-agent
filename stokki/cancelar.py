# -*- coding: utf-8 -*-
"""
stokki/cancelar.py

Cancela um pedido de saida na Stokki pelo mesmo fluxo da tela (sonda de
06/10/2026, provado no PS-39959 -- memoria reference_stokki_cancelar_pedido_endpoint):

  GET  /administrator/inventory/outbound/show/{id}      -> "Situação" + _token do form_cancel
  POST /administrator/inventory/outbound/cancel         -> {"success":true,"processing":true}
  GET  /administrator/inventory/outbound/cancel/status/{id} a cada 2 s ate {"state":"Canceled"}

Sem Playwright: usa a StokkiSession (requests) e exige que quem chama
esteja com a trava stokki/sessao_uso.py (login concorrente derruba a
outra sessao). Motivo tem minimo de 15 caracteres na Stokki.
"""
import logging
import re
import time

from stokki.pedidos import BASE_URL

logger = logging.getLogger(__name__)

URL_SHOW = f"{BASE_URL}/pt-br/administrator/inventory/outbound/show/{{id}}"
URL_CANCEL = f"{BASE_URL}/pt-br/administrator/inventory/outbound/cancel"
URL_STATUS = f"{BASE_URL}/pt-br/administrator/inventory/outbound/cancel/status/{{id}}"
PREFIXO_MOTIVO = "Portal Fresh Hub"
MOTIVO_PADRAO = f"{PREFIXO_MOTIVO}: cancelado pelo embarcador"
TENTATIVAS_POLL = 15
INTERVALO_POLL = 2

_RE_SITUACAO = re.compile(r"Situa[çc][ãa]o:\s*</th>\s*<td>(.*?)</td>", re.S)
_RE_TOKEN = re.compile(r'<form id="form_cancel".*?name="_token" value="([^"]+)"', re.S)


def _limpar(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def situacao_e_token(sessao, id_stokki: int) -> tuple[str, str]:
    """Texto da "Situação" e o _token do formulario de cancelamento."""
    resp = sessao.get(URL_SHOW.format(id=id_stokki))
    if hasattr(resp, "raise_for_status"):
        resp.raise_for_status()
    html = resp.text
    m = _RE_SITUACAO.search(html)
    t = _RE_TOKEN.search(html)
    return (_limpar(m.group(1)) if m else "", t.group(1) if t else "")


def montar_motivo(motivo_cliente: str) -> str:
    texto = (motivo_cliente or "").strip()
    if not texto:
        return MOTIVO_PADRAO
    if not texto.startswith(PREFIXO_MOTIVO):
        texto = f"{PREFIXO_MOTIVO}: {texto}"
    return texto[:200]


def cancelar_pedido(sessao, id_stokki: int, motivo: str, dormir=time.sleep) -> dict:
    """Devolve {"ok", "situacao", "ja_estava", "erro"}. Nunca levanta por
    resposta da Stokki (so por falha de rede do requests)."""
    situacao, token = situacao_e_token(sessao, id_stokki)
    sit = situacao.lower()
    if "cancel" in sit:
        return {"ok": True, "situacao": situacao, "ja_estava": True, "erro": ""}
    if any(p in sit for p in ("enviado", "sent", "expedido")):
        return {"ok": False, "situacao": situacao, "ja_estava": False, "erro": f"pedido ja expedido na Stokki ({situacao})"}
    if not token:
        return {"ok": False, "situacao": situacao, "ja_estava": False, "erro": "formulario de cancelamento nao encontrado na pagina"}

    resp = sessao.post(
        URL_CANCEL,
        files={"_token": (None, token), "reason": (None, montar_motivo(motivo)),
               "provider_outbound_id": (None, str(id_stokki)), "page": (None, "show")},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": URL_SHOW.format(id=id_stokki)},
    )
    try:
        corpo = resp.json()
    except ValueError:
        corpo = {}
    if resp.status_code != 200 or not corpo.get("success", True):
        erros = corpo.get("errors") or corpo.get("message") or resp.text[:200]
        return {"ok": False, "situacao": situacao, "ja_estava": False, "erro": f"Stokki {resp.status_code}: {erros}"}

    if corpo.get("processing"):
        concluiu = False
        for _ in range(TENTATIVAS_POLL):
            dormir(INTERVALO_POLL)
            st = sessao.get(URL_STATUS.format(id=id_stokki))
            try:
                if st.json().get("state") == "Canceled":
                    concluiu = True
                    break
            except ValueError:
                pass
        if not concluiu:
            return {"ok": False, "situacao": situacao, "ja_estava": False,
                    "erro": f"cancelamento ainda em processamento depois de {TENTATIVAS_POLL * INTERVALO_POLL}s"}

    depois, _ = situacao_e_token(sessao, id_stokki)
    ok = "cancel" in depois.lower()
    logger.info(f"Stokki: pedido {id_stokki} -> {depois!r} ({'ok' if ok else 'NAO cancelou'})")
    return {"ok": ok, "situacao": depois, "ja_estava": False,
            "erro": "" if ok else f"Stokki aceitou mas a situacao ficou {depois!r}"}
