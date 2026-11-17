# -*- coding: utf-8 -*-
"""
portal_cliente/link_ciencia.py

Link publico da pagina de ciencia do dedicado fora do dia fixo, sem login
(spec docs/superpowers/specs/2026-11-17-ciencia-dedicado-dia-fixo-design.md,
secao 4). Mesmo padrao de link_canhoto.py: itsdangerous, segredo do portal
com salt proprio. O token e por embarcador (carrega so o sender_id) e vale
30 dias; a pagina lista os pedidos PENDENTE daquele sender_id.
"""
from itsdangerous import BadData, URLSafeTimedSerializer

_SALT = "ciencia-dedicado"
VALIDADE_DIAS = 30


def gerar(sender_id: int, segredo: str) -> str:
    return URLSafeTimedSerializer(segredo).dumps({"r": int(sender_id)}, salt=_SALT)


def ler(token: str, segredo: str, validade_dias: int = VALIDADE_DIAS) -> int | None:
    """sender_id, ou None se adulterado, vencido ou de outra finalidade (outro salt)."""
    try:
        dados = URLSafeTimedSerializer(segredo).loads(token, salt=_SALT, max_age=validade_dias * 86400)
        return int(dados["r"])
    except (BadData, KeyError, TypeError, ValueError):
        return None


def url(sender_id: int, segredo: str, url_base: str) -> str:
    return f"{url_base.rstrip('/')}/ciencia/{gerar(sender_id, segredo)}"
