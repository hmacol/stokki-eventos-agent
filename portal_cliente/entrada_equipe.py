# -*- coding: utf-8 -*-
"""
portal_cliente/entrada_equipe.py

Passe de entrada do painel interno pro portal (Hugo, 16/11): o botao
"Cotacao" da Torre abria /cliente/cotacao e caia no login do cliente
(CNPJ + PIN). Agora o painel, com a pessoa ja logada, assina um passe
curto e o portal abre a sessao de equipe sem pedir senha de novo.

O passe so prova "este usuario esta logado no painel agora". Quem pode
entrar e com que nivel continua sendo decidido pelo portal
(app.py: _equipe_do_passe), pelas mesmas regras do login /equipe.

O passe viaja na URL (redirect), entao alem de vencer em 60 s ele leva um
numero unico e so vale uma vez: o portal guarda os ja usados
(app.py: _passe_ja_usado), pra quem achar a URL num log nao entrar com ela.

Importado pelos dois apps (painel: `from portal_cliente import ...`;
portal: `import entrada_equipe`), por isso nao depende de mais nada.
"""
import secrets

from itsdangerous import BadData, URLSafeTimedSerializer

_SALT = "entrada-equipe-painel"
VALIDADE_SEGUNDOS = 60


def gerar(segredo: str, usuario: str, nivel: str) -> str:
    return URLSafeTimedSerializer(segredo).dumps({"u": usuario, "n": nivel, "j": secrets.token_urlsafe(16)}, salt=_SALT)


def ler(segredo: str, passe: str) -> dict | None:
    """{"usuario", "nivel", "unico"} ou None (assinatura errada, vencido, torto)."""
    try:
        dados = URLSafeTimedSerializer(segredo).loads(passe or "", salt=_SALT, max_age=VALIDADE_SEGUNDOS)
    except BadData:
        return None
    if not isinstance(dados, dict) or not dados.get("u") or not dados.get("n") or not dados.get("j"):
        return None
    return {"usuario": str(dados["u"]), "nivel": str(dados["n"]), "unico": str(dados["j"])}
