# -*- coding: utf-8 -*-
"""
nucleo/comparar_pool.py

Comparador do POOL: Vuupt (`not_assigned` ao vivo) × núcleo (nucleo/pool.py)
-- a sombra da Entrega 1 do spec
docs/superpowers/specs/2026-09-24-pool-pelo-nucleo-pedidos-portal-rascunho-14h-design.md.
O planejamento só pode trocar de fonte (planejamento.fonte_pool: nucleo)
depois de 3 dias úteis seguidos sem divergência sem explicação.

Compara código a código o que a tela enxerga: id, endereço, coordenadas,
caixas, remetente, CNPJ do destinatário, data agendada, janela resolvida
(roteirizacao_dados.resolver_janela) e regra de dia fixo. Nível, área e NF
derivam desses campos, então não precisam entrar.

Nunca escreve na Vuupt. Grava no banco só com --salvar/--email.

COMO USAR (VPS, como www-data):
    venv/bin/python nucleo/comparar_pool.py                  # imprime as diferenças
    venv/bin/python nucleo/comparar_pool.py --salvar --email # o que o timer horário roda
    venv/bin/python nucleo/comparar_pool.py --historico 10   # placar por dia (não vai na Vuupt)
Saída 0 = pools iguais; 2 = houve divergência.
"""
import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo import banco, pool
from nucleo.normalizacao import normalizar_codigo
from regioes_dia_fixo import regra_dia_fixo_do_servico
from roteirizacao_dados import resolver_janela

logger = logging.getLogger("nucleo.comparar_pool")

CAMPOS = ("id", "address", "latitude", "longitude", "dimension_3", "sender_id", "customer_code",
          "agendado_para", "janela", "dia_fixo")


def _num(valor):
    try:
        return round(float(valor), 5)
    except (TypeError, ValueError):
        return None


def projetar(s: dict) -> dict:
    ini, fim, _fonte = resolver_janela(s)
    regra = regra_dia_fixo_do_servico(s)
    return {
        "id": s.get("id"),
        "address": (s.get("address") or "").strip(),
        "latitude": _num(s.get("latitude")),
        "longitude": _num(s.get("longitude")),
        "dimension_3": s.get("dimension_3"),
        "sender_id": s.get("sender_id"),
        "customer_code": (s.get("customer") or {}).get("code"),
        "agendado_para": (s.get("scheduled_start") or "")[:10] or None,
        "janela": (ini, fim) if ini else None,
        "dia_fixo": regra["nome"] if regra else None,
    }


def comparar(pool_vuupt: list[dict], pool_nucleo: list[dict]) -> dict:
    """Puro (é o que os testes exercitam)."""
    def por_codigo(itens):
        mapa, repetidos = {}, []
        for s in itens:
            codigo = normalizar_codigo(s.get("code"))
            if not codigo:
                continue
            if codigo in mapa:
                repetidos.append(codigo)
            mapa[codigo] = projetar(s)
        return mapa, sorted(set(repetidos))

    # Achado 24/09 (PS-39958): a Vuupt pode ter DOIS serviços not_assigned
    # com o mesmo código (duplicado que verificar_pedidos_duplicados_vuupt.py
    # cancela às 18h). O núcleo é chaveado por código e mescla os dois numa
    # linha -- a tela mostraria 2 cards pela Vuupt e 1 pelo núcleo. Conta
    # como divergência, com nome próprio pra explicar o placar.
    v, duplicados = por_codigo(pool_vuupt)
    n, _ = por_codigo(pool_nucleo)
    diferentes = []
    for codigo in sorted(set(v) & set(n)):
        campos = {c: [v[codigo][c], n[codigo][c]] for c in CAMPOS if v[codigo][c] != n[codigo][c]}
        if campos:
            diferentes.append({"code": codigo, "campos": campos})
    so_v, so_n = sorted(set(v) - set(n)), sorted(set(n) - set(v))
    # "próprias" = o que é do espelho; o duplicado é da Vuupt (Hugo, 24/09:
    # não some sozinho, entra no relatório mas não no placar dos 3 dias).
    proprias = len(so_v) + len(so_n) + len(diferentes)
    return {"total_vuupt": len(v), "total_vuupt_bruto": len(pool_vuupt), "total_nucleo": len(n),
            "so_na_vuupt": so_v, "so_no_nucleo": so_n, "diferentes": diferentes, "duplicados_na_vuupt": duplicados,
            "divergencias_proprias": proprias, "total_divergencias": proprias + len(duplicados)}


def resumir(r: dict, exemplos: int = 5) -> str:
    linhas = [f"Pool: Vuupt {r['total_vuupt']} código(s) em {r.get('total_vuupt_bruto', r['total_vuupt'])} "
              f"serviço(s) × núcleo {r['total_nucleo']} -- {r['divergencias_proprias']} divergência(s) própria(s), "
              f"{len(r['duplicados_na_vuupt'])} duplicado(s) na Vuupt"]
    if r.get("duplicados_na_vuupt"):
        linhas.append(f"  código duplicado na Vuupt ({len(r['duplicados_na_vuupt'])}): "
                      f"{', '.join(r['duplicados_na_vuupt'][:exemplos])}")
    if r["so_na_vuupt"]:
        linhas.append(f"  só na Vuupt   ({len(r['so_na_vuupt'])}): {', '.join(r['so_na_vuupt'][:exemplos])}")
    if r["so_no_nucleo"]:
        linhas.append(f"  só no núcleo  ({len(r['so_no_nucleo'])}): {', '.join(r['so_no_nucleo'][:exemplos])}")
    for d in r["diferentes"][:exemplos]:
        linhas.append(f"  {d['code']}: " + "; ".join(f"{c}: {a!r} × {b!r}" for c, (a, b) in d["campos"].items()))
    if len(r["diferentes"]) > exemplos:
        linhas.append(f"  ... e mais {len(r['diferentes']) - exemplos} com campo diferente")
    return "\n".join(linhas)


# ── Placar ────────────────────────────────────────────────────────────────────

def _garantir_tabela(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE IF NOT EXISTS nucleo_reconciliacoes_pool (
                        rodado_em             TEXT PRIMARY KEY,
                        total_vuupt           INTEGER NOT NULL DEFAULT 0,
                        total_nucleo          INTEGER NOT NULL DEFAULT 0,
                        total_divergencias    INTEGER NOT NULL DEFAULT 0,
                        divergencias_proprias INTEGER NOT NULL DEFAULT 0,
                        duplicados            INTEGER NOT NULL DEFAULT 0,
                        detalhes_json         TEXT)""")
    existentes = {row[1] for row in conn.execute("PRAGMA table_info(nucleo_reconciliacoes_pool)")}
    for coluna in ("divergencias_proprias", "duplicados"):
        if coluna not in existentes:
            conn.execute(f"ALTER TABLE nucleo_reconciliacoes_pool ADD COLUMN {coluna} INTEGER NOT NULL DEFAULT 0")


def salvar(r: dict, conn: sqlite3.Connection, rodado_em: str | None = None):
    _garantir_tabela(conn)
    conn.execute("""INSERT OR REPLACE INTO nucleo_reconciliacoes_pool
                    (rodado_em, total_vuupt, total_nucleo, total_divergencias, divergencias_proprias, duplicados,
                     detalhes_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (rodado_em or banco.agora(), r["total_vuupt"], r["total_nucleo"], r["total_divergencias"],
                  r["divergencias_proprias"], len(r["duplicados_na_vuupt"]),
                  json.dumps({k: r.get(k) for k in ("so_na_vuupt", "so_no_nucleo", "diferentes", "duplicados_na_vuupt")},
                             ensure_ascii=False, default=str)))
    conn.commit()


def historico(conn: sqlite3.Connection, limite: int = 20) -> list[dict]:
    """Uma linha por DIA: pior rodada do dia (máximo de divergências
    PRÓPRIAS); `duplicados` é o máximo de códigos duplicados na Vuupt visto
    no dia, só pra explicar."""
    _garantir_tabela(conn)
    return [dict(r) for r in conn.execute(
        """SELECT substr(rodado_em, 1, 10) AS dia, COUNT(*) AS rodadas, MAX(total_vuupt) AS total_vuupt,
                  MAX(divergencias_proprias) AS pior, SUM(divergencias_proprias) AS soma, MAX(duplicados) AS duplicados
           FROM nucleo_reconciliacoes_pool GROUP BY dia ORDER BY dia DESC LIMIT ?""", (limite,))]


def dias_limpos_seguidos(conn: sqlite3.Connection) -> int:
    """Dias COM pool, do mais recente pra trás, em que NENHUMA rodada achou
    divergência. Dia sem pool (fim de semana vazio) nem conta nem quebra."""
    seguidos = 0
    for linha in historico(conn, limite=60):
        if not linha["total_vuupt"]:
            continue
        if linha["pior"]:
            break
        seguidos += 1
    return seguidos


def _enviar_email(r: dict, conn: sqlite3.Connection, config: dict, exemplos: int):
    from email_utils import COR_ERRO, envelope_html, enviar_email
    limpos = dias_limpos_seguidos(conn)
    destino = (config.get("notificacao_execucao", {}) or {}).get("destinatario") or "hugo@freshlogbr.com"
    titulo = (f"Pool Vuupt × núcleo: {r['divergencias_proprias']} divergência(s)"
              + (f" (+{len(r['duplicados_na_vuupt'])} duplicado(s) na Vuupt)" if r["duplicados_na_vuupt"] else ""))
    corpo = [f"<h2 style='margin:0 0 12px'>{titulo}</h2>",
             f"<p>Critério da Entrega 1: 3 dias úteis seguidos sem divergência. Hoje: <b>{limpos}</b>.</p>",
             "<pre style='white-space:pre-wrap;font-size:12px;background:#F3F4F6;padding:12px;border-radius:6px'>",
             resumir(r, exemplos), "</pre>"]
    enviar_email([destino], titulo, envelope_html("".join(corpo), cor_acento=COR_ERRO), config.get("email", {}))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compara o pool Vuupt × núcleo (somente leitura na Vuupt).")
    parser.add_argument("--exemplos", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", help="caminho do banco (padrão: dados/dados.db)")
    parser.add_argument("--salvar", action="store_true", help="grava a rodada em nucleo_reconciliacoes_pool")
    parser.add_argument("--email", action="store_true", help="manda e-mail SÓ quando há divergência")
    parser.add_argument("--historico", type=int, metavar="N", help="placar dos últimos N dias (não vai na Vuupt)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    caminho = Path(args.db) if args.db else banco.DB_PATH
    conn = banco.conectar(caminho)
    try:
        if args.historico:
            for linha in historico(conn, args.historico):
                marca = "OK " if not linha["pior"] else "!! "
                print(f"{marca}{linha['dia']}  rodadas={linha['rodadas']:2d} pool={linha['total_vuupt']:3d} "
                      f"pior={linha['pior']:3d} soma={linha['soma']:4d} duplicados_vuupt={linha['duplicados']:2d}")
            print(f"\nDias com pool, seguidos, sem divergência: {dias_limpos_seguidos(conn)} (critério: 3 úteis)")
            return 0

        import yaml
        caminho_config = next((c for c in (_RAIZ / "config.yaml", Path.cwd() / "config.yaml") if c.exists()), None)
        if caminho_config is None:
            logger.error("config.yaml não encontrado.")
            return 1
        config = yaml.safe_load(caminho_config.read_text(encoding="utf-8")) or {}
        token = (config.get("vuupt_api") or {}).get("token", "")
        if not token:
            logger.error("vuupt_api.token ausente no config.yaml.")
            return 1

        from vuupt_client import VuuptClient
        vuupt = VuuptClient(token)
        pool_vuupt = vuupt.listar_servicos([{"field": "status", "operator": "eq", "value": "not_assigned"}],
                                           per_page=100, include=["customer"])
        pool_nucleo = pool.listar_pool(conn)
        r = comparar(pool_vuupt, pool_nucleo)
        if args.salvar:
            salvar(r, conn)
        # E-mail e saída 2 só por divergência PRÓPRIA: um duplicado na Vuupt
        # (que fica dias até alguém cancelar) não pode mandar 14 e-mails/dia.
        if args.email and r["divergencias_proprias"]:
            _enviar_email(r, conn, config, args.exemplos)
    finally:
        conn.close()

    print(json.dumps(r, ensure_ascii=False, indent=1, default=str) if args.json else resumir(r, args.exemplos))
    return 2 if r["divergencias_proprias"] else 0


if __name__ == "__main__":
    sys.exit(main())
