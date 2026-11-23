"""Carregado pelo Python na subida de todo processo com PYTHONPATH=<raiz>/homologacao/site.

Com FRESH_AMBIENTE=homolog instala a guarda de rede antes de qualquer import do projeto; os
subprocessos herdam o ambiente e nascem com a guarda. Sem a variavel, nao faz nada.
Falha fechada: se a instalacao falhar, o processo nao sobe (SystemExit escapa do site.py,
que so engole Exception e seguiria sem guarda).
"""
import os
import sys


def _instalar_guarda():
    if not os.environ.get("FRESH_AMBIENTE"):
        return
    try:
        from pathlib import Path
        raiz = str(Path(__file__).resolve().parents[2])  # <raiz>/homologacao/site/sitecustomize.py
        sys.path.insert(0, raiz)
        from homologacao import ambiente
        if not ambiente.homolog_ativo():
            sys.path.remove(raiz)
            return
        from homologacao import guarda
        guarda.instalar()
    except BaseException as erro:
        raise SystemExit("Homologacao: guarda de rede nao instalou (%s: %s), abortando."
                         % (type(erro).__name__, erro))


_instalar_guarda()
