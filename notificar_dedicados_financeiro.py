# -*- coding: utf-8 -*-
"""
E-mail quinzenal pro financeiro com os pedidos dedicados (Hugo, 23/09/2026):
remetente, pedido e valor da quinzena anterior, pela data da marcacao
(pedidos_dedicados.marcado_em). Roda dia 1 (16..fim do mes anterior) e
dia 16 (1..15) as 08:00 -- infra/stokki-dedicados-financeiro.timer.

    venv/bin/python notificar_dedicados_financeiro.py --modo-teste
    venv/bin/python notificar_dedicados_financeiro.py --data-ref 2026-10-01

Config (config.yaml), tudo opcional:
    financeiro:
      email: financeiro@freshlogbr.com
      forcar_destino: hugo@freshlogbr.com   # ausente = hugo@ (piloto); "" = envio real
      ativo: true
"""
import argparse
import html
import logging
import sys
import time
from datetime import date
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from email_utils import COR_BORDA, COR_TEXTO_SUAVE, enviar_email, envelope_html  # noqa: E402
from notificar_execucao_agente import notificar_execucao  # noqa: E402
import pedidos_dedicados  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dedicados_financeiro")

CONFIG_PATH = _RAIZ / "config.yaml"
EMAIL_TESTE = "hugo@freshlogbr.com"
EMAIL_FINANCEIRO_PADRAO = "financeiro@freshlogbr.com"
RODAPE = "Fresh Log - pedidos dedicados da quinzena (automatico)."


def _carregar_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _secao(config: dict) -> dict:
    return (config or {}).get("financeiro") or {}


def forcar_destino_do_config(config: dict) -> str:
    """Sem a chave no config o padrao e redirecionar pro Hugo (piloto)."""
    secao = _secao(config)
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def _brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _td(conteudo: str, extra: str = "") -> str:
    return f"<td style='padding:5px 6px;{extra}'>{conteudo}</td>"


def montar_email(ini: date, fim: date, linhas: list[dict], destino_original: str | None) -> tuple[str, str]:
    """(assunto, html). destino_original preenchido = e-mail redirecionado
    (piloto): entra um aviso amarelo com o destino real."""
    assunto = f"[Fresh Log] Pedidos dedicados {ini.strftime('%d/%m')} a {fim.strftime('%d/%m/%Y')}"
    aviso = (f"<p style='background:#FFF4D6;padding:8px;border-radius:6px'>Redirecionado (piloto). Destino real: "
             f"{html.escape(destino_original)}</p>" if destino_original else "")
    if not linhas:
        return assunto, envelope_html(aviso + f"<p>Nenhum pedido dedicado marcado entre {_br(ini)} e {_br(fim)}.</p>", RODAPE)
    grupos: dict[str, list[dict]] = {}
    for l in linhas:
        grupos.setdefault(l.get("remetente_nome") or "(remetente nao identificado)", []).append(l)
    cab = f"padding:6px;border-bottom:1px solid {COR_BORDA}"
    partes = [f"<p>Pedidos marcados como <b>envio dedicado</b> entre {_br(ini)} e {_br(fim)} ({len(linhas)} pedido(s)).</p>",
              f"<table style='border-collapse:collapse;width:100%;font-size:13px'><tr style='color:{COR_TEXTO_SUAVE}'>"
              f"<th align='left' style='{cab}'>Remetente</th><th align='left' style='{cab}'>Pedido</th>"
              f"<th align='left' style='{cab}'>Marcado em</th><th align='right' style='{cab}'>Valor</th></tr>"]
    total = 0.0
    for nome, itens in grupos.items():
        sub = 0.0
        for l in itens:
            ped = l.get("codigo_pedido") or f"envio #{l.get('envio_id')}"
            if l.get("numero_nf"):
                ped += f" · NF {l['numero_nf']}"
            quando = f"{l['marcado_em'][8:10]}/{l['marcado_em'][5:7]}"
            partes.append("<tr>" + _td(html.escape(nome)) + _td(html.escape(ped)) + _td(quando)
                          + f"<td align='right' style='padding:5px 6px'>{_brl(l['valor'])}</td></tr>")
            sub += l["valor"]
        partes.append(f"<tr><td colspan='3' style='padding:5px 6px;border-top:1px solid {COR_BORDA}'><b>Subtotal {html.escape(nome)}</b></td>"
                      f"<td align='right' style='padding:5px 6px;border-top:1px solid {COR_BORDA}'><b>{_brl(round(sub, 2))}</b></td></tr>")
        total += sub
    partes.append(f"<tr><td colspan='3' style='padding:8px 6px;border-top:2px solid {COR_BORDA}'><b>Total</b></td>"
                  f"<td align='right' style='padding:8px 6px;border-top:2px solid {COR_BORDA}'><b>{_brl(round(total, 2))}</b></td></tr></table>")
    return assunto, envelope_html(aviso + "".join(partes), RODAPE)


def executar(config: dict, data_ref: date, modo_teste: bool, conn=None) -> dict:
    r = {"inicio": "", "fim": "", "pedidos": 0, "enviado": False}
    secao = _secao(config)
    if not modo_teste and not secao.get("ativo", True):
        r["desativado"] = "financeiro.ativo=false"
        return r
    propria = conn is None
    conn = conn or pedidos_dedicados.conectar()
    try:
        ini, fim, linhas = pedidos_dedicados.listar_quinzena(conn, data_ref)
    finally:
        if propria:
            conn.close()
    r.update({"inicio": ini.isoformat(), "fim": fim.isoformat(), "pedidos": len(linhas)})
    if data_ref.day not in (1, 16):
        logger.info(f"rodando fora do dia 1/16 (data-ref {data_ref}); quinzena {ini}..{fim}")
    destino_real = secao.get("email") or EMAIL_FINANCEIRO_PADRAO
    forcar = EMAIL_TESTE if modo_teste else forcar_destino_do_config(config)
    destinos = [forcar] if forcar else [destino_real]
    assunto, corpo = montar_email(ini, fim, linhas, destino_real if forcar else None)
    if modo_teste:
        assunto = "[MODO TESTE] " + assunto
    r["enviado"] = enviar_email(destinos, assunto, corpo, (config or {}).get("email", {}) or {})
    logger.info(f"quinzena {ini}..{fim}: {len(linhas)} pedido(s), e-mail pra {destinos}: {'ok' if r['enviado'] else 'FALHOU'}")
    return r


def main(modo_teste: bool = False, data_ref: str | None = None) -> None:
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Pedidos dedicados pro financeiro iniciado.")
    inicio = time.time()
    resultado = {"status": "ok", "detalhe": ""}
    config = {}
    try:
        config = _carregar_config()
        r = executar(config, date.fromisoformat(data_ref) if data_ref else date.today(), modo_teste)
        resultado["detalhe"] = f"{r['inicio']} a {r['fim']}: {r['pedidos']} pedido(s)" + (", DESLIGADO" if r.get("desativado") else "")
        if not r.get("desativado") and not r["enviado"]:
            resultado["status"] = "erro"
            resultado["detalhe"] += " -- e-mail nao enviado"
    except Exception as e:
        logger.exception(f"Erro: {e}")
        resultado["status"] = "erro"
        resultado["detalhe"] = str(e)
    finally:
        try:
            notificar_execucao({"Pedidos dedicados (financeiro)": resultado}, time.time() - inicio, modo_teste, config or _carregar_config())
        except Exception as e:
            logger.warning(f"Falha ao notificar execucao (nao afeta o resultado): {e}")
    if resultado["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--modo-teste", action="store_true")
    parser.add_argument("--data-ref", default=None, help="AAAA-MM-DD (default hoje); a quinzena e a anterior fechada")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste, data_ref=args.data_ref)
