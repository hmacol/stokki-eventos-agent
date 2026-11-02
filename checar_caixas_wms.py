# -*- coding: utf-8 -*-
"""
checar_caixas_wms.py

Checagem diaria: compara o saldo do WMS (wms_saldos) com a soma das caixas
em estoque, por posicao/produto/lote/validade, so do embarcador piloto. Se
houver divergencia, manda UM e-mail com a tabela. Nao corrige nada.

Sai com 0 quando rodou (com ou sem divergencia); 1 so em excecao (ex.: banco).

COMO USAR (timer systemd 06h40, infra/stokki-wms-checar-caixas.*):
    venv/bin/python checar_caixas_wms.py
    venv/bin/python checar_caixas_wms.py --modo-teste     # imprime, nao manda e-mail
    venv/bin/python checar_caixas_wms.py --db caminho.db  # banco alternativo (teste)
"""
import argparse
import html
import logging
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

import email_utils
import wms_caixas

logger = logging.getLogger("checar_caixas_wms")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _piloto_id(config: dict) -> int:
    return int(str((config.get("wms", {}) or {}).get("embarcador_piloto_id", "48")).strip())


def _destinatarios(config: dict) -> list[str]:
    alertas = (config.get("wms", {}) or {}).get("alertas_email")
    if isinstance(alertas, str):
        alertas = [alertas]
    lista = [str(a).strip() for a in (alertas or []) if str(a).strip()]
    if lista:
        return lista
    remetente = str((config.get("email", {}) or {}).get("remetente") or "").strip()
    return [remetente] if remetente else []


def _corpo_html(linhas: list[dict]) -> str:
    cab = "".join(f"<th style='text-align:left;padding:4px 8px'>{h}</th>"
                  for h in ("Posicao", "SKU", "Lote", "Validade", "Saldo", "Caixas"))
    corpo = ""
    for d in linhas:
        cels = [d["posicao"], d["sku"], d["lote"], d["validade"], d["saldo"], d["caixas"]]
        corpo += "<tr>" + "".join(
            f"<td style='padding:4px 8px;border-top:1px solid #ddd'>{html.escape(str(c if c is not None else ''))}</td>"
            for c in cels) + "</tr>"
    return ("<p>O saldo do WMS nao bate com a soma das caixas nestas linhas:</p>"
            "<table style='border-collapse:collapse;font-family:sans-serif;font-size:13px'>"
            f"<tr>{cab}</tr>{corpo}</table>")


def checar(conn, config: dict, modo_teste: bool = False) -> dict:
    """Devolve {"divergencias": n, "email": "enviado"|"teste"|"nada"|"falhou"}."""
    embarcador_id = _piloto_id(config)
    linhas = wms_caixas.divergencias(conn, embarcador_id)
    resultado = {"divergencias": len(linhas), "email": "nada"}
    if not linhas:
        return resultado
    for d in linhas:
        logger.info("Divergencia: %s", d)
    if modo_teste:
        resultado["email"] = "teste"
        return resultado
    destinatarios = _destinatarios(config)
    if not destinatarios:
        logger.warning("Sem destinatario (wms.alertas_email / email.remetente): e-mail nao enviado.")
        return resultado
    assunto = f"WMS: {len(linhas)} divergencia(s) caixas x saldo ({embarcador_id})"
    ok = email_utils.enviar_email(destinatarios, assunto, _corpo_html(linhas), config.get("email", {}) or {})
    if ok:
        resultado["email"] = "enviado"
    else:
        logger.error("Falha ao enviar o e-mail de divergencias.")
        resultado["email"] = "falhou"
    return resultado


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modo-teste", action="store_true", help="nao manda e-mail, so imprime")
    ap.add_argument("--db", help="caminho do banco (padrao: dados/dados.db)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    try:
        try:
            config = _carregar_config()
        except FileNotFoundError:
            if not args.modo_teste:
                raise
            config = {}
        conn = wms_caixas.conectar(Path(args.db) if args.db else None)
        try:
            res = checar(conn, config, args.modo_teste)
        finally:
            conn.close()
    except Exception:
        logger.exception("Falha na checagem de caixas x saldo")
        return 1
    logger.info("Checagem: %s", res)
    print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
