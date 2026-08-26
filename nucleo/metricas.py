# -*- coding: utf-8 -*-
"""
nucleo/metricas.py

Métricas de tempo por parada sobre nucleo_paradas (Hugo, 26/08: "quanto
tempo em média estão demorando para receber").

    tempo_no_local_s      = completed_at - arrived_at   (chegou -> recebeu)
    tempo_deslocamento_s  = arrived_at   - started_at   (saiu -> chegou)

Filtro de plausibilidade (mesmo cuidado de revisar_complexidade_entrega.py):
só entram durações entre MIN_PLAUSIVEL_S e MAX_PLAUSIVEL_S -- abaixo é
confirmação em lote (VUUPT) ou toque errado; acima é esquecimento.
Média E mediana sempre juntas (a mediana resiste a outlier).

COMO USAR:
    py -3.11 nucleo/metricas.py                       # últimos 30 dias, visão geral + por nível
    py -3.11 nucleo/metricas.py --dias 60 --por destinatario
    py -3.11 nucleo/metricas.py --por motorista --por remetente
    py -3.11 nucleo/metricas.py --recalcular           # backfill das durações (paradas antigas)
Agrupamentos: geral | nivel | destinatario | remetente | motorista | origem (APP x VUUPT).
"""
import argparse
import sqlite3
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo import banco, tempos

MIN_PLAUSIVEL_S = 30          # menos que isso = confirmação em lote / toque errado
MAX_PLAUSIVEL_S = 4 * 3600    # mais que isso = esqueceu de registrar

_AGRUPAMENTOS = {
    "geral": ("'Todas as paradas'", ""),
    "nivel": ("COALESCE('Nível ' || p.nivel_dificuldade, 'Sem nível')", ""),
    "destinatario": ("COALESCE(pe.destinatario_nome, p.destinatario_nome, p.titulo, p.codigo)", "LEFT JOIN nucleo_pedidos pe ON pe.codigo = p.codigo"),
    "remetente": ("COALESCE(p.remetente_nome, pe.remetente_nome, 'sender ' || p.sender_id)", "LEFT JOIN nucleo_pedidos pe ON pe.codigo = p.codigo"),
    "motorista": ("COALESCE(r.motorista_nome, 'agent ' || r.agent_id)", ""),
    "origem": ("r.provedor", ""),
}


def _resumo(valores: list[int]) -> dict:
    v = sorted(valores)
    n = len(v)
    return {
        "n": n,
        "media_min": round(sum(v) / n / 60, 1),
        "mediana_min": round(statistics.median(v) / 60, 1),
        "p90_min": round(v[min(n - 1, int(n * 0.9))] / 60, 1),
        "min_min": round(v[0] / 60, 1),
        "max_min": round(v[-1] / 60, 1),
    }


def tempo_por_grupo(conn: sqlite3.Connection, de: date, ate: date, por: str = "geral",
                    campo: str = "tempo_no_local_s", minimo_amostra: int = 1) -> list[dict]:
    """Uma linha por grupo: {grupo, n, media_min, mediana_min, p90_min, min, max}.
    `campo` = tempo_no_local_s (padrão) ou tempo_deslocamento_s."""
    if por not in _AGRUPAMENTOS or campo not in ("tempo_no_local_s", "tempo_deslocamento_s"):
        raise ValueError("agrupamento/campo inválido")
    expr, join = _AGRUPAMENTOS[por]
    rows = conn.execute(f"""
        SELECT {expr} AS grupo, p.{campo} AS t
        FROM nucleo_paradas p
        JOIN nucleo_rotas r ON r.id = p.rota_id
        {join}
        WHERE r.data_rota BETWEEN ? AND ? AND r.status != 'CANCELADA'
          AND p.situacao IN ('ENTREGUE', 'PARCIAL', 'INSUCESSO')
          AND p.{campo} BETWEEN ? AND ?
    """, (de.isoformat(), ate.isoformat(), MIN_PLAUSIVEL_S, MAX_PLAUSIVEL_S)).fetchall()
    grupos: dict[str, list[int]] = {}
    for g, t in rows:
        grupos.setdefault(g or "—", []).append(int(t))
    saida = [{"grupo": g, **_resumo(v)} for g, v in grupos.items() if len(v) >= minimo_amostra]
    saida.sort(key=lambda x: (-x["n"], x["grupo"]))
    return saida


def cobertura(conn: sqlite3.Connection, de: date, ate: date) -> dict:
    """Quantas paradas concluídas têm duração plausível -- pra saber o
    quanto a média representa."""
    row = conn.execute("""
        SELECT COUNT(*),
               SUM(p.tempo_no_local_s IS NOT NULL),
               SUM(p.tempo_no_local_s BETWEEN ? AND ?),
               SUM(p.tempo_no_local_s IS NOT NULL AND p.tempo_no_local_s < ?)
        FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
        WHERE r.data_rota BETWEEN ? AND ? AND r.status != 'CANCELADA'
          AND p.situacao IN ('ENTREGUE', 'PARCIAL', 'INSUCESSO')
    """, (MIN_PLAUSIVEL_S, MAX_PLAUSIVEL_S, MIN_PLAUSIVEL_S, de.isoformat(), ate.isoformat())).fetchone()
    return {"concluidas": row[0] or 0, "com_duracao": row[1] or 0, "plausiveis": row[2] or 0, "abaixo_30s": row[3] or 0}


def _imprimir(titulo: str, linhas: list[dict]):
    print(f"\n== {titulo}")
    if not linhas:
        print("   (sem amostra)")
        return
    print(f"   {'grupo':44s} {'n':>5s} {'média':>7s} {'mediana':>8s} {'p90':>6s}")
    for l in linhas[:40]:
        print(f"   {str(l['grupo'])[:44]:44s} {l['n']:5d} {l['media_min']:6.1f}m {l['mediana_min']:7.1f}m {l['p90_min']:5.1f}m")
    if len(linhas) > 40:
        print(f"   ... +{len(linhas) - 40} grupo(s)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Tempo no local / deslocamento por parada.")
    parser.add_argument("--dias", type=int, default=30)
    parser.add_argument("--por", action="append", choices=sorted(_AGRUPAMENTOS), help="pode repetir")
    parser.add_argument("--campo", default="tempo_no_local_s", choices=["tempo_no_local_s", "tempo_deslocamento_s"])
    parser.add_argument("--minimo", type=int, default=3, help="amostra mínima por grupo (padrão 3)")
    parser.add_argument("--recalcular", action="store_true", help="backfill das durações a partir dos carimbos")
    args = parser.parse_args(argv)

    conn = banco.conectar()
    try:
        if args.recalcular:
            print(f"Durações recalculadas em {tempos.recalcular_todas(conn)} parada(s).")
        ate = date.today()
        de = ate - timedelta(days=args.dias - 1)
        cob = cobertura(conn, de, ate)
        print(f"Período {de} a {ate} · paradas concluídas: {cob['concluidas']} · com duração: {cob['com_duracao']} · "
              f"plausíveis (30s-4h): {cob['plausiveis']} · abaixo de 30s (lote/ruído): {cob['abaixo_30s']}")
        rotulo = "Tempo no local (chegou -> recebeu)" if args.campo == "tempo_no_local_s" else "Tempo de deslocamento (saiu -> chegou)"
        for por in (args.por or ["geral", "nivel", "origem"]):
            _imprimir(f"{rotulo} por {por}", tempo_por_grupo(conn, de, ate, por, args.campo, args.minimo))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
