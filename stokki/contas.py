# -*- coding: utf-8 -*-
"""
stokki/contas.py

Contas da Stokki que as rotinas usam (Hugo, 05/10/2026: "segunda
alternativa de login para o caso de congestionamento"):

  - "principal": stokki.usuario / stokki.senha -- conta ADMIN (StokkiSession:
    importação, WMS, retiradas, telas do painel; portal);
  - "reserva":   stokki.reserva.usuario / senha -- segunda conta ADMIN,
    opcional. Quando a principal está com outro processo, quem pede
    alternativa entra por ela;
  - "provider":  stokki.provider.usuario / senha -- só a área /provider/
    (expedição, documentos, Estação de Impressão). Sem alternativa:
    testado em 05/10, o wms@ (provider) não entra em /pt-br/administrator
    (302 pro login), então não serve de reserva da principal.

A Stokki só derruba a sessão de quem usa o MESMO usuário, então cada
conta tem a sua trava (stokki/sessao_uso.py) e o seu arquivo de cookies
(stokki/auth.py).
"""
from pathlib import Path

import yaml

PRINCIPAL = "principal"
RESERVA = "reserva"
PROVIDER = "provider"
CONTAS = (PRINCIPAL, RESERVA, PROVIDER)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def credenciais(config: dict, conta: str) -> tuple[str, str]:
    """(usuario, senha) da conta. Provider sem usuário/senha cai no
    principal (mesmo fallback que expedição/documentos/estação já tinham)."""
    stokki = config.get("stokki", {}) or {}
    if conta in (PROVIDER, RESERVA):
        sub = stokki.get(conta, {}) or {}
        if conta == RESERVA:
            return sub.get("usuario", ""), sub.get("senha", "")
        return (sub.get("usuario") or stokki.get("usuario", ""),
                sub.get("senha") or stokki.get("senha", ""))
    return stokki.get("usuario", ""), stokki.get("senha", "")


def _ler_config() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def reserva_disponivel(config: dict | None = None) -> bool:
    """True se stokki.reserva tem usuário e senha e é um usuário diferente
    da principal e da provider. config=None lê o config.yaml da raiz; sem
    arquivo, False (comportamento antigo)."""
    config = _ler_config() if config is None else config
    usuario, senha = credenciais(config, RESERVA)
    if not (usuario and senha):
        return False
    outros = {credenciais(config, c)[0].strip().lower() for c in (PRINCIPAL, PROVIDER)}
    return usuario.strip().lower() not in outros


def ordem(conta: str, alternativa: bool) -> list[str]:
    """Contas que podem atender quem prefere `conta`, na ordem de
    preferência. Só a principal tem alternativa (a reserva)."""
    if alternativa and conta == PRINCIPAL and reserva_disponivel():
        return [PRINCIPAL, RESERVA]
    return [conta]
