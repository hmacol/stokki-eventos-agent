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

Duas seções lidas do núcleo (26/09), porque o cruzamento acima só enxerga
o que a Vuupt já marcou como entregue:
  - rotas de dias anteriores que não terminaram (paradas pendentes);
  - retiradas no galpão abertas há mais de DIAS_RETIRADA dias.
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
DIAS_ROTAS          = 60          # rota de dia anterior sem terminar: olha até aqui
DIAS_RETIRADA       = 7           # retirada no galpão aberta há mais que isso = alerta
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


# ── Núcleo: rotas que não terminaram e retiradas esquecidas ───────────────────
# O cruzamento acima só vê o que a Vuupt marcou como `done`. Parada de rota
# que o motorista nunca iniciou/fechou (Rafael/Iago, 14/09: ~91 paradas;
# de novo 16-24/09) e retirada no galpão que ninguém fecha ficam invisíveis
# pra expedição e pra este e-mail. Lido do núcleo, que espelha a Vuupt.

def listar_rotas_paradas(conn, hoje, agent_lalamove: int = 0) -> list[dict]:
    """Rotas de dias anteriores (até DIAS_ROTAS) não concluídas nem
    canceladas que ainda têm parada sem resultado. Rota [TESTE] fica de fora."""
    rows = conn.execute("""
        SELECT r.id, r.data_rota, r.nome, r.agent_id, r.motorista_nome, r.status_provedor,
               SUM(p.situacao NOT IN ('ENTREGUE', 'PARCIAL', 'INSUCESSO', 'CANCELADA')) AS pendentes,
               COUNT(*) AS total
        FROM nucleo_rotas r JOIN nucleo_paradas p ON p.rota_id = r.id
        WHERE r.data_rota < ? AND r.data_rota >= ?
          AND r.status NOT IN ('CONCLUIDA', 'CANCELADA')
          AND COALESCE(r.nome, '') NOT LIKE '[TESTE]%'
        GROUP BY r.id HAVING pendentes > 0
        ORDER BY r.data_rota, r.id
    """, (hoje.isoformat(), (hoje - timedelta(days=DIAS_ROTAS)).isoformat())).fetchall()
    rotas = []
    for id_, data_rota, nome, agent_id, motorista, status_prov, pendentes, total in rows:
        if agent_lalamove and agent_id == agent_lalamove:
            motivo = "Lalamove: conferir a entrega e fechar a rota na Vuupt"
        else:
            motivo = "confirmar com o motorista se entregou; fechar na Vuupt ou expedir com --forcar"
        rotas.append({"id": id_, "data": data_rota, "nome": nome or "", "motorista": motorista or f"agente {agent_id}",
                      "status_provedor": status_prov or "", "pendentes": int(pendentes), "total": total,
                      "motivo": motivo})
    return rotas


def listar_retiradas_velhas(conn, agora: datetime) -> list[dict]:
    """Retiradas no galpão abertas há mais de DIAS_RETIRADA, agrupadas por
    embarcador (são dezenas; uma linha por pedido afogaria o e-mail)."""
    limite = (agora - timedelta(days=DIAS_RETIRADA)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("""
        SELECT COALESCE(remetente_nome, '(sem embarcador)'), codigo, criado_em_provedor
        FROM nucleo_pedidos
        WHERE fluxo = 'RETIRADA' AND status NOT IN ('ENTREGUE', 'CANCELADO', 'INSUCESSO')
          AND excluido_em IS NULL AND criado_em_provedor < ?
        ORDER BY criado_em_provedor
    """, (limite,)).fetchall()
    grupos: dict[str, dict] = {}
    for embarcador, codigo, criado in rows:
        g = grupos.setdefault(embarcador, {"embarcador": embarcador, "qtd": 0, "codigos": [],
                                           "mais_antiga": datetime.strptime(criado[:10], "%Y-%m-%d").strftime("%d/%m")})
        g["qtd"] += 1
        g["codigos"].append(codigo)
    return sorted(grupos.values(), key=lambda g: -g["qtd"])


# ── E-mail ────────────────────────────────────────────────────────────────────

_TD = "padding:6px 10px;border-bottom:1px solid #E5E7EB;font-size:12px;"
_TH = "padding:8px 10px;text-align:left;"


def _secao_rotas_e_retiradas(rotas: list[dict], retiradas: list[dict]) -> str:
    partes = []
    if rotas:
        linhas = "".join(
            f"<tr><td style='{_TD}font-weight:600;'>{html.escape(r['data'][8:10] + '/' + r['data'][5:7])}</td>"
            f"<td style='{_TD}'>{html.escape(r['motorista'])}</td>"
            f"<td style='{_TD}'>{html.escape(r['nome'])}</td>"
            f"<td style='{_TD}text-align:right;'>{r['pendentes']}/{r['total']}</td>"
            f"<td style='{_TD}'>{html.escape(r['status_provedor'])}</td>"
            f"<td style='{_TD}'>{html.escape(r['motivo'])}</td></tr>"
            for r in rotas)
        partes.append(f"""
<h2 style="margin:24px 0 8px;font-size:18px;color:#141428;">Rotas de dias anteriores que não terminaram</h2>
<p style="margin:0 0 12px;font-size:14px;color:#374151;">{len(rotas)} rota(s) com
{sum(r['pendentes'] for r in rotas)} parada(s) sem resultado. Enquanto não fecharem, esses pedidos
não são expedidos na Stokki nem aparecem na lista acima.</p>
<table style="width:100%;border-collapse:collapse;">
<thead><tr style="background:#141428;color:#fff;font-size:12px;">
<th style="{_TH}">Dia</th><th style="{_TH}">Motorista</th><th style="{_TH}">Rota</th>
<th style="{_TH}text-align:right;">Pendentes</th><th style="{_TH}">Vuupt</th><th style="{_TH}">Ação</th>
</tr></thead><tbody>{linhas}</tbody></table>""")
    if retiradas:
        linhas = "".join(
            f"<tr><td style='{_TD}font-weight:600;'>{html.escape(g['embarcador'])}</td>"
            f"<td style='{_TD}text-align:right;'>{g['qtd']}</td>"
            f"<td style='{_TD}'>{html.escape(g['mais_antiga'])}</td>"
            f"<td style='{_TD}'>{html.escape(', '.join(g['codigos'][:10]) + (' ...' if g['qtd'] > 10 else ''))}</td></tr>"
            for g in retiradas)
        partes.append(f"""
<h2 style="margin:24px 0 8px;font-size:18px;color:#141428;">Retiradas no galpão abertas há mais de {DIAS_RETIRADA} dias</h2>
<p style="margin:0 0 12px;font-size:14px;color:#374151;">{sum(g['qtd'] for g in retiradas)} retirada(s).
Se o cliente já retirou, expedir na Stokki (o serviço fecha sozinho na Vuupt); se não vai retirar, cancelar.</p>
<table style="width:100%;border-collapse:collapse;">
<thead><tr style="background:#141428;color:#fff;font-size:12px;">
<th style="{_TH}">Embarcador</th><th style="{_TH}text-align:right;">Qtd</th>
<th style="{_TH}">Mais antiga</th><th style="{_TH}">Pedidos</th>
</tr></thead><tbody>{linhas}</tbody></table>""")
    return "".join(partes)


def montar_email(alertas: list[dict], so_insucesso: int, total_stokki: int,
                 rotas: list[dict] = (), retiradas: list[dict] = ()) -> str:
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
""" if alertas else ""
    conteudo += _secao_rotas_e_retiradas(rotas, retiradas)
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

    conn = sqlite3.connect(DB_PATH)
    try:
        agent_lalamove = int((config.get("lalamove") or {}).get("agent_id_vuupt") or 0)
        rotas = listar_rotas_paradas(conn, datetime.now().date(), agent_lalamove)
        # criado_em_provedor vem da Vuupt em UTC sem fuso
        retiradas = listar_retiradas_velhas(conn, datetime.now(timezone.utc).replace(tzinfo=None))
    except sqlite3.OperationalError as e:   # núcleo ausente: segue só com a parte da Stokki
        logger.warning(f"Nucleo indisponivel ({e}) -- secoes de rotas e retiradas puladas.")
        rotas, retiradas = [], []
    finally:
        conn.close()
    logger.info(f"ROTAS SEM TERMINAR: {len(rotas)} ({sum(r['pendentes'] for r in rotas)} parada(s) pendente(s)).")
    for r in rotas:
        logger.info(f"  {r['data']} {r['motorista']:<22} {r['nome']} -- {r['pendentes']}/{r['total']} pendente(s)")
    logger.info(f"RETIRADAS > {DIAS_RETIRADA} DIAS: {sum(g['qtd'] for g in retiradas)}.")
    for g in retiradas:
        logger.info(f"  {g['embarcador'][:40]:<40} {g['qtd']:>4} (mais antiga {g['mais_antiga']})")

    if (alertas or rotas or retiradas) and not modo_teste:
        config_email = config.get("email", {})
        destino = config_email.get("email_responsavel", "")
        if destino:
            n_ret = sum(g["qtd"] for g in retiradas)
            assunto = (f"[Freshlog] Expedicao: {len(alertas)} entregue(s) sem expedir, "
                       f"{len(rotas)} rota(s) sem terminar, {n_ret} retirada(s) antiga(s)")
            ok = enviar_email([destino], assunto,
                              montar_email(alertas, so_insucesso, len(stokki), rotas, retiradas), config_email)
            logger.info(f"E-mail {'enviado' if ok else 'FALHOU'} para {destino}.")
        else:
            logger.warning("email.email_responsavel nao configurado -- e-mail pulado.")
    return len(alertas)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--modo-teste", action="store_true", help="só loga, não manda e-mail")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
