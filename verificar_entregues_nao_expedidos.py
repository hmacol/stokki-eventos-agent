# -*- coding: utf-8 -*-
"""
verificar_entregues_nao_expedidos.py

Checagem diária de fechamento da expedição (28/08, item 4 do plano
aceito pelo Hugo: "nunca deixar acumular pedido entregue sem expedir"):
cruza a Stokki (pedidos em "Aguardando Transportador"/"Em espera") com a
Vuupt (serviços `done` + `status_done=success`) e lista todo pedido
ENTREGUE há mais de 24h que ainda não avançou na Stokki, com o motivo
provável:

  - marcado como expedido (fingerprint) mas a Stokki não avançou
    -> conferir na Stokki (caso PS-37718/34818: "Expedição #OE" criada
       por humano sem concluir);
  - em expedicoes_falhas (Stokki recusou N vezes) -> ação manual;
  - fora da janela de 30 dias do expedir_pedidos.py -> `--forcar`;
  - na fila (ainda vai ser expedido na próxima rodada de 30 min).

Independe da causa: se está entregue e não expedido, aparece. E-mail
interno pro `email.email_responsavel` (config.yaml) só quando há algo.
Roda às 07:15 pelo timer `stokki-verificar-entregues-nao-expedidos.timer`
(antes da 1ª expedição das 08h, janela sem outra sessão Stokki aberta --
login concorrente derruba a sessão de quem estiver rodando).

Execute:
  py -3.11 verificar_entregues_nao_expedidos.py
  py -3.11 verificar_entregues_nao_expedidos.py --modo-teste   (sem e-mail)
"""
import argparse
import html
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from email_utils import envelope_html, enviar_email
from stokki.auth import StokkiSession
from stokki import pedidos as stokki_pedidos

(_RAIZ / "dados").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ / "dados" / "verificar_entregues_nao_expedidos.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("verificar_entregues")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CONFIG_PATH = _RAIZ / "config.yaml"
DB_PATH     = _RAIZ / "dados" / "dados.db"
VUUPT_BASE  = "https://api.vuupt.com/api/v1/services"

DIAS_VUUPT          = 60          # quanto do histórico `done` da Vuupt é lido
HORAS_MINIMAS       = 24          # entregue há mais que isso sem expedir = alerta
HORAS_JANELA_EXPED  = 24 * 30     # mesma HORAS_ENTREGUES do expedir_pedidos.py
# Situações da Stokki que significam "ainda não expedido" (valor da URL da listagem)
STATUS_STOKKI = [stokki_pedidos.STATUS_AGUARDANDO_TRANSPORTADOR, "On hold"]

_PS = re.compile(r"#?PS-\d+(?:-R\d+)*", re.IGNORECASE)


def _carregar_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _base(code: str) -> str:
    return re.sub(r"-R\d+$", "", (code or "").strip().lstrip("#").upper())


def _parse(d: str):
    if not d:
        return None
    t = d.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Stokki ────────────────────────────────────────────────────────────────────

def listar_stokki_nao_expedidos(sessao) -> dict[str, str]:
    """{codigo_ps: situacao} de tudo que ainda não foi expedido na Stokki."""
    resultado = {}
    for status in STATUS_STOKKI:
        n = 0
        try:
            for linha in stokki_pedidos.iterar_todos_pedidos(sessao, status=status):
                valores = linha.values() if isinstance(linha, dict) else linha
                texto = " | ".join(re.sub(r"<[^>]+>", " ", str(v)) for v in valores)
                m = _PS.search(texto)
                if m:
                    resultado[m.group(0).upper().lstrip("#")] = status
                    n += 1
        except Exception as e:
            logger.warning(f"Stokki: falha ao listar status '{status}': {e}")
        logger.info(f"Stokki '{status}': {n} pedido(s).")
    return resultado


# ── Vuupt ─────────────────────────────────────────────────────────────────────

def listar_vuupt_done(token: str, dias: int) -> dict[str, list[dict]]:
    """{codigo_base: [serviços done]} dos últimos `dias` (sucesso e insucesso)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    limite = datetime.now(timezone.utc) - timedelta(days=dias)
    por_base: dict[str, list[dict]] = {}
    page = 1
    while True:
        resp = requests.get(VUUPT_BASE, headers=headers, timeout=60,
                            params={"page": page, "per_page": 100, "sort": "-completed_at",
                                    "include": "checklistAnswers"})
        resp.raise_for_status()
        body = resp.json()
        registros = body.get("data", [])
        if not registros:
            break
        parar = False
        for s in registros:
            dt = _parse(s.get("completed_at"))
            if dt and dt < limite:
                parar = True
                break
            if s.get("status") != "done" or not _PS.match(s.get("code") or ""):
                continue
            por_base.setdefault(_base(s.get("code")), []).append(s)
        if parar:
            break
        pag = body.get("meta", {}).get("pagination", {})
        if page >= pag.get("total_pages", page):
            break
        page += 1
        time.sleep(0.5)
    logger.info(f"Vuupt: {sum(len(v) for v in por_base.values())} serviço(s) done em {dias} dias.")
    return por_base


# ── Cruzamento ────────────────────────────────────────────────────────────────

def _tem_foto(s: dict) -> bool:
    cl = ((s.get("checklistAnswers") or {}).get("data") or [None])[0]
    return bool(cl) and int(cl.get("images_quantity") or 0) > 0


def cruzar(stokki: dict[str, str], vuupt: dict[str, list[dict]]) -> tuple[list[dict], int]:
    """Retorna (alertas, qtd_so_insucesso). Alerta = entregue com sucesso
    há mais de HORAS_MINIMAS e ainda não expedido na Stokki."""
    conn = sqlite3.connect(DB_PATH)
    fingerprint = {r[0] for r in conn.execute("SELECT codigo_ps FROM expedicoes_processadas")}
    try:
        falhas = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT codigo_ps, motivo, tentativas FROM expedicoes_falhas")}
    except sqlite3.OperationalError:
        falhas = {}
    conn.close()

    agora = datetime.now(timezone.utc)
    alertas, so_insucesso = [], 0
    for codigo, situacao in stokki.items():
        servicos = vuupt.get(_base(codigo))
        if not servicos:
            continue
        sucesso = [s for s in servicos if s.get("status_done") == "success"]
        if not sucesso:
            so_insucesso += 1
            continue
        s = max(sucesso, key=lambda x: x.get("completed_at") or "")
        entregue_em = _parse(s.get("completed_at"))
        horas = (agora - entregue_em).total_seconds() / 3600 if entregue_em else 0
        if horas < HORAS_MINIMAS:
            continue
        code_vuupt = (s.get("code") or "").strip().lstrip("#").upper()
        if situacao == "On hold":
            # "Em espera" na Stokki = nunca passou pela Estação de Impressão;
            # a Stokki recusa expedir nesse estado (situação inválida), não
            # adianta --forcar. Caso real: 15 entregas de 26/07.
            motivo = "'Em espera' na Stokki (não passou pela impressão) -- resolver na Stokki"
        elif code_vuupt in fingerprint or codigo in fingerprint:
            motivo = "marcado como expedido (fingerprint) mas a Stokki não avançou -- conferir na Stokki"
        elif code_vuupt in falhas or codigo in falhas:
            m, n = falhas.get(code_vuupt) or falhas.get(codigo)
            motivo = f"Stokki recusou {n}x ({m}) -- ação manual na Stokki"
        elif horas > HORAS_JANELA_EXPED:
            motivo = "fora da janela de 30 dias da expedição automática -- expedir com --forcar"
        else:
            motivo = "na fila da expedição automática (próxima rodada)"
        if not _tem_foto(s):
            motivo += " | sem foto de canhoto"
        alertas.append({
            "codigo": codigo, "code_vuupt": s.get("code"), "situacao_stokki": situacao,
            "entregue_em": entregue_em.astimezone().strftime("%d/%m %H:%M") if entregue_em else "",
            "dias": round(horas / 24, 1), "motivo": motivo,
        })
    alertas.sort(key=lambda a: -a["dias"])
    return alertas, so_insucesso


# ── E-mail ────────────────────────────────────────────────────────────────────

def montar_email(alertas: list[dict], so_insucesso: int, total_stokki: int) -> str:
    linhas = "".join(
        f"<tr>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #E5E7EB;font-weight:600;font-size:13px;'>{html.escape(a['codigo'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #E5E7EB;font-size:12px;'>{html.escape(a['situacao_stokki'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #E5E7EB;font-size:12px;'>{html.escape(a['entregue_em'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #E5E7EB;font-size:12px;text-align:right;'>{a['dias']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #E5E7EB;font-size:12px;'>{html.escape(a['motivo'])}</td>"
        f"</tr>"
        for a in alertas
    )
    conteudo = f"""
<h2 style="margin:0 0 8px;font-size:20px;color:#141428;">Entregues sem expedição na Stokki</h2>
<p style="margin:0 0 16px;font-size:14px;color:#374151;">
{len(alertas)} pedido(s) entregue(s) há mais de {HORAS_MINIMAS}h que ainda não avançaram na Stokki
(de {total_stokki} não expedidos; outros {so_insucesso} têm só insucesso na Vuupt e seguem a tratativa normal).</p>
<table style="width:100%;border-collapse:collapse;">
<thead><tr style="background:#141428;color:#fff;font-size:12px;">
<th style="padding:8px 10px;text-align:left;">Pedido</th>
<th style="padding:8px 10px;text-align:left;">Stokki</th>
<th style="padding:8px 10px;text-align:left;">Entregue</th>
<th style="padding:8px 10px;text-align:right;">Dias</th>
<th style="padding:8px 10px;text-align:left;">Motivo / ação</th>
</tr></thead><tbody>{linhas}</tbody></table>
"""
    return envelope_html(conteudo, rodape="Checagem diária de fechamento da expedição (verificar_entregues_nao_expedidos.py).")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(modo_teste: bool = False) -> int:
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    if not token:
        raise SystemExit("Token VUUPT nao configurado em config.yaml")
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Checagem de entregues não expedidos iniciada.")

    sessao = StokkiSession(config)
    stokki = listar_stokki_nao_expedidos(sessao)
    vuupt = listar_vuupt_done(token, DIAS_VUUPT)
    alertas, so_insucesso = cruzar(stokki, vuupt)

    logger.info(f"RESULTADO: {len(alertas)} entregue(s) sem expedição há >{HORAS_MINIMAS}h | "
                f"{so_insucesso} só com insucesso | {len(stokki)} não expedidos na Stokki.")
    for a in alertas:
        logger.info(f"  {a['codigo']:<16} {a['situacao_stokki']:<22} entregue {a['entregue_em']} "
                    f"({a['dias']}d) -- {a['motivo']}")

    if alertas and not modo_teste:
        config_email = config.get("email", {})
        destino = config_email.get("email_responsavel", "")
        if destino:
            ok = enviar_email([destino], f"[Freshlog] {len(alertas)} pedido(s) entregue(s) sem expedicao na Stokki",
                              montar_email(alertas, so_insucesso, len(stokki)), config_email)
            logger.info(f"E-mail {'enviado' if ok else 'FALHOU'} para {destino}.")
        else:
            logger.warning("email.email_responsavel nao configurado -- e-mail pulado.")
    return len(alertas)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--modo-teste", action="store_true", help="só loga, não manda e-mail")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
