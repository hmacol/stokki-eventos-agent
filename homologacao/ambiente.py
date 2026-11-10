"""Ambiente de homologacao: so age com a variavel FRESH_AMBIENTE=homolog."""
import json
import os
import re
import sys
from pathlib import Path

RAIZ_PRODUCAO = Path("/opt/stokki-eventos")
RAIZ_HOMOLOG = Path("/opt/stokki-homolog")
VARIAVEL = "FRESH_AMBIENTE"

_RAIZ_REPO = Path(__file__).resolve().parent.parent
CAMINHO_CONFIG = _RAIZ_REPO / "config.yaml"
_CNPJ_PADRAO = {"00000000000191"}
_cache_cnpjs = None


class DesligadoNaHomologacao(RuntimeError):
    pass


def homolog_ativo() -> bool:
    return os.environ.get(VARIAVEL, "").strip().lower() == "homolog"


def exigir_coerencia(raiz: Path) -> None:
    ativo = homolog_ativo()
    resolvida = raiz.resolve()
    if ativo and resolvida == RAIZ_PRODUCAO:
        raise SystemExit(
            "FRESH_AMBIENTE=homolog na raiz de producao (%s): abortando." % RAIZ_PRODUCAO)
    if ativo:
        guarda = sys.modules.get("homologacao.guarda")
        if guarda is None or not getattr(guarda, "INSTALADA", False):
            raise SystemExit(
                "Homologacao ativa sem a guarda de rede instalada: abortando.")
    if resolvida == RAIZ_HOMOLOG and not ativo:
        raise SystemExit(
            "Raiz de homologacao (%s) sem FRESH_AMBIENTE=homolog: abortando." % RAIZ_HOMOLOG)


def recusar_na_homologacao(o_que: str) -> None:
    if homolog_ativo():
        raise DesligadoNaHomologacao(f"{o_que} desligado na homologação")


def cnpjs_portal_permitidos() -> set:
    global _cache_cnpjs
    if _cache_cnpjs is not None:
        return set(_cache_cnpjs)
    cnpjs = None
    try:
        import yaml
        with open(CAMINHO_CONFIG, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        bruto = (cfg.get("homologacao") or {}).get("portal_cnpjs_permitidos")
        if bruto:
            cnpjs = {re.sub(r"\D", "", str(c)) for c in bruto}
            cnpjs.discard("")
    except Exception:
        cnpjs = None
    _cache_cnpjs = cnpjs or set(_CNPJ_PADRAO)
    return set(_cache_cnpjs)


def informacao_versao(raiz: Path) -> dict:
    info = {"ramo": "?", "commit": "?", "master": "?", "quando": "?", "copia": "?"}
    try:
        dados = json.loads((raiz / "dados" / "HOMOLOG_VERSAO").read_text(encoding="utf-8"))
        for k in ("ramo", "commit", "master", "quando"):
            if dados.get(k):
                info[k] = dados[k]
    except Exception:
        pass
    try:
        texto = (raiz / "dados" / "HOMOLOG_COPIA").read_text(encoding="utf-8").strip()
        if texto:
            info["copia"] = texto
    except Exception:
        pass
    return info
