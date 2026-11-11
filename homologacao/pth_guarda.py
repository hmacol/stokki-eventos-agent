"""Ativada pelo infra/homolog/homolog-guarda.pth, instalado (do root, 644) no site-packages do venv da
homologacao pelo criar.sh/subir.sh.

Todo processo do venv /opt/stokki-homolog/venv nasce com a guarda de rede, inclusive script rodado a mao
(`sudo -u homolog venv/bin/python pipeline.py`) sem o PYTHONPATH e o FRESH_AMBIENTE dos units, e com -E ou -I
(eles nao desligam o site-packages do venv). So -S (sem o modulo site) pula o .pth: nesse caso sobra o nft
do usuario homolog. O sitecustomize dos units continua (guarda.instalar() e idempotente).

Falha fechada: se a guarda nao instalar, o processo morre aqui (os._exit(1)). O site.py so mostraria o
erro da linha do .pth e seguiria sem guarda.
Excecao unica: o pip do root (comum.sh, pip_do_venv) com HOMOLOG_PIP_SEM_GUARDA=1 precisa do PyPI. Vale so
para euid 0, que de todo jeito pode apagar o .pth; o usuario homolog nunca pula a guarda.
"""
import os
import sys

VARIAVEL_PIP = "HOMOLOG_PIP_SEM_GUARDA"


def _pip_do_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return os.environ.get(VARIAVEL_PIP) == "1" and geteuid is not None and geteuid() == 0


def _abortar(erro) -> None:
    try:
        sys.stderr.write("Homologacao: guarda de rede nao instalou (%s: %s), abortando.\n"
                         % (type(erro).__name__, erro))
        sys.stderr.flush()
    except BaseException:
        pass
    os._exit(1)


def ativar(raiz: str) -> None:
    """Chamada pela linha do .pth, que pos `raiz` no comeco do sys.path para achar este modulo."""
    try:
        if _pip_do_root():
            return
        # forca (nao so "se vazio"): o venv e da homologacao; outro valor desligaria as recusas do app
        os.environ["FRESH_AMBIENTE"] = "homolog"
        from homologacao import guarda
        guarda.instalar()
    except BaseException as erro:
        _abortar(erro)
    finally:
        # a raiz entrou so para importar a guarda (ja em sys.modules); nao muda o import do resto
        try:
            sys.path.remove(raiz)
        except ValueError:
            pass
