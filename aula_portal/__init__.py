# -*- coding: utf-8 -*-
"""Gerador dos videos da aula do portal (/cliente/aula). Ver
docs/superpowers/specs/2026-11-06-aula-portal-cliente-design.md."""
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# Os modulos do portal se importam pelo nome curto (auth_cliente, envio_pedidos...).
for _p in (RAIZ, RAIZ / "portal_cliente"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
