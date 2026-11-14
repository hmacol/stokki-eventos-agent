# -*- coding: utf-8 -*-
"""Gerador dos videos da aula interna da equipe (/painel/aula): o mesmo
motor da aula do cliente (aula_portal), rodando o painel de agentes com
config e banco ficticios."""
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# Os modulos do painel se importam pelo nome curto (retornos, acesso_telas...).
# painel_agentes/ vem antes da raiz: senao "import painel_agentes" pega o pacote.
for _p in (RAIZ, RAIZ / "painel_agentes"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
