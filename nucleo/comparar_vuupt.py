# -*- coding: utf-8 -*-
"""
nucleo/comparar_vuupt.py

Comparador Vuupt × núcleo -- a prova da Etapa 2 do
DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md ("espelho fiel e comprovado"). Enquanto
a Vuupt for a fonte de verdade das rotas dela, o núcleo só serve pra
substituí-la se bater com ela, pedido a pedido. Este script mede isso.

Pra cada dia (LOCAL de São Paulo), lista as rotas da Vuupt com os serviços
e confere contra nucleo_rotas / nucleo_paradas / nucleo_pedidos:

  rota_ausente_no_nucleo     rota da Vuupt que o núcleo não tem
  rota_sobrando_no_nucleo    rota VUUPT do núcleo naquele dia que a Vuupt não lista (excluída/remarcada)
  status_rota_diferente      status derivado da Vuupt × status do núcleo
  motorista_diferente        agent_id
  data_rota_diferente        data local do start_at × data_rota
  parada_ausente             serviço da rota sem parada no núcleo
  parada_sobrando            parada ativa no núcleo que a rota da Vuupt não tem mais
  situacao_parada_diferente  situação esperada × situação gravada
  horario_parada_em_utc      completed_at gravado cru (UTC), sem converter pra hora local
  horario_parada_diferente   completed_at diferente por outro motivo
  pedido_ausente             código da rota sem linha em nucleo_pedidos
  pedido_duplicado_codigo    o mesmo pedido gravado com e sem '#'
  status_pedido_diferente    status esperado pelo resultado da rota × nucleo_pedidos.status

Nunca escreve em lugar nenhum: Vuupt só GET, banco em modo somente leitura.

COMO USAR (VPS, como www-data):
    venv/bin/python nucleo/comparar_vuupt.py                    # ontem
    venv/bin/python nucleo/comparar_vuupt.py --dias 7           # últimos 7 dias até ontem
    venv/bin/python nucleo/comparar_vuupt.py --data 2026-09-11 --exemplos 10
    venv/bin/python nucleo/comparar_vuupt.py --dias 7 --json > comparacao.json
Saída 0 = nenhuma divergência; 2 = houve divergência.
"""
import argparse
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo import banco
from nucleo.normalizacao import janela_utc_do_dia, normalizar_codigo, vuupt_para_local
from nucleo.sincronizar_vuupt import (_SITUACAO_PARA_PEDIDO, _STATUS_ROTA_VUUPT, _derivar_status_rota,
                                      extrair_servicos, situacao_do_servico)

logger = logging.getLogger("nucleo.comparar_vuupt")

CATEGORIAS = [
    "rota_ausente_no_nucleo", "rota_sobrando_no_nucleo", "status_rota_diferente", "motorista_diferente",
    "data_rota_diferente", "parada_ausente", "parada_sobrando", "situacao_parada_diferente",
    "horario_parada_em_utc", "horario_parada_diferente", "pedido_ausente", "pedido_duplicado_codigo",
    "status_pedido_diferente",
]


def _data_local(ts) -> str | None:
    local = vuupt_para_local(ts)
    return local[:10] if local else None


def comparar_dia(dia: date, rotas_vuupt: list[dict], conn: sqlite3.Connection) -> dict:
    """Puro em relação à rede (é o que os testes exercitam). `conn` com
    row_factory=sqlite3.Row."""
    divergencias: dict[str, list[dict]] = defaultdict(list)
    contagem = {"rotas": 0, "paradas": 0, "pedidos": 0}
    ids_vuupt = set()
    esperado_pedido: dict[str, str | None] = {}

    for rota in rotas_vuupt:
        rid = rota.get("id")
        if rid is None:
            continue
        ids_vuupt.add(rid)
        contagem["rotas"] += 1
        servicos = extrair_servicos(rota)
        cancelada = _STATUS_ROTA_VUUPT.get((rota.get("status") or "").lower()) == banco.ROTA_CANCELADA
        esperado = {}
        for s in servicos:
            if s.get("id") is None:
                continue
            esperado[s["id"]] = (banco.PARADA_CANCELADA if cancelada else situacao_do_servico(s), s)
        status_esperado = _derivar_status_rota(rota.get("status"), [sit for sit, _ in esperado.values()])

        n = conn.execute("SELECT * FROM nucleo_rotas WHERE vuupt_route_id = ?", (rid,)).fetchone()
        base = {"vuupt_route_id": rid, "nome": rota.get("name")}
        if n is None:
            divergencias["rota_ausente_no_nucleo"].append({**base, "status_vuupt": rota.get("status")})
            continue
        base["rota_id"] = n["id"]
        if n["status"] != status_esperado:
            divergencias["status_rota_diferente"].append({**base, "nucleo": n["status"], "vuupt": status_esperado,
                                                          "status_bruto": rota.get("status")})
        if (n["agent_id"] or None) != (rota.get("agent_id") or None):
            divergencias["motorista_diferente"].append({**base, "nucleo": n["agent_id"], "vuupt": rota.get("agent_id")})
        data_esperada = _data_local(rota.get("start_at"))
        if data_esperada and n["data_rota"] != data_esperada:
            divergencias["data_rota_diferente"].append({**base, "nucleo": n["data_rota"], "vuupt": data_esperada})

        paradas = {p["service_id"]: p for p in conn.execute(
            "SELECT * FROM nucleo_paradas WHERE rota_id = ? AND service_id IS NOT NULL", (n["id"],))}
        for sid, (situacao, s) in esperado.items():
            contagem["paradas"] += 1
            p = paradas.get(sid)
            ref = {**base, "service_id": sid, "codigo": s.get("code")}
            if p is None:
                divergencias["parada_ausente"].append(ref)
                continue
            if p["situacao"] != situacao:
                divergencias["situacao_parada_diferente"].append({**ref, "nucleo": p["situacao"], "vuupt": situacao})
            bruto = s.get("completed_at")
            if bruto and not cancelada:
                local = vuupt_para_local(bruto)
                if p["completed_at"] != local:
                    categoria = "horario_parada_em_utc" if p["completed_at"] == str(bruto) else "horario_parada_diferente"
                    divergencias[categoria].append({**ref, "nucleo": p["completed_at"], "esperado": local})
        for sid, p in paradas.items():
            if sid not in esperado and p["situacao"] != banco.PARADA_CANCELADA:
                divergencias["parada_sobrando"].append({**base, "service_id": sid, "codigo": p["codigo"],
                                                        "situacao_nucleo": p["situacao"]})

        for situacao, s in esperado.values():
            codigo = normalizar_codigo(s.get("code"))
            if not codigo:
                continue
            if cancelada:
                esperado_pedido.setdefault(codigo, None)        # só presença: rota cancelada não diz o status
            else:
                esperado_pedido[codigo] = _SITUACAO_PARA_PEDIDO.get(situacao)

    for n in conn.execute("SELECT * FROM nucleo_rotas WHERE provedor = ? AND data_rota = ?",
                          (banco.PROVEDOR_VUUPT, dia.isoformat())):
        if n["vuupt_route_id"] not in ids_vuupt:
            divergencias["rota_sobrando_no_nucleo"].append({"vuupt_route_id": n["vuupt_route_id"], "rota_id": n["id"],
                                                            "nome": n["nome"], "status_nucleo": n["status"]})

    for codigo, status in esperado_pedido.items():
        contagem["pedidos"] += 1
        linhas = conn.execute("SELECT codigo, status FROM nucleo_pedidos WHERE codigo IN (?, ?)",
                              (codigo, "#" + codigo)).fetchall()
        if not linhas:
            divergencias["pedido_ausente"].append({"codigo": codigo})
            continue
        if len(linhas) > 1:
            divergencias["pedido_duplicado_codigo"].append({"codigo": codigo, "linhas": [dict(l) for l in linhas]})
        if status is None or any(l["status"] == status for l in linhas):
            continue
        # O pedido pode ter ido pra outra rota DEPOIS desse dia -- aí o status atual é outro, e está certo.
        mudou_depois = conn.execute("""
            SELECT 1 FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
            WHERE p.codigo IN (?, ?) AND r.data_rota > ? AND r.status <> 'CANCELADA' LIMIT 1
        """, (codigo, "#" + codigo, dia.isoformat())).fetchone()
        if not mudou_depois:
            divergencias["status_pedido_diferente"].append({"codigo": codigo, "nucleo": [l["status"] for l in linhas],
                                                            "esperado": status})

    return {"dia": dia.isoformat(), "contagem": contagem,
            "divergencias": {c: divergencias.get(c, []) for c in CATEGORIAS}}


def resumir(resultados: list[dict], exemplos: int) -> str:
    linhas = []
    total_rotas = sum(r["contagem"]["rotas"] for r in resultados)
    total_paradas = sum(r["contagem"]["paradas"] for r in resultados)
    total_pedidos = sum(r["contagem"]["pedidos"] for r in resultados)
    dias = ", ".join(r["dia"] for r in resultados)
    linhas.append(f"Dias: {dias}")
    linhas.append(f"Comparado: {total_rotas} rota(s), {total_paradas} parada(s), {total_pedidos} pedido(s)")
    total_div = 0
    for c in CATEGORIAS:
        itens = [i for r in resultados for i in r["divergencias"][c]]
        total_div += len(itens)
        if itens:
            linhas.append(f"  {c:28s} {len(itens):5d}")
            for i in itens[:exemplos]:
                linhas.append(f"      ex: {json.dumps(i, ensure_ascii=False, default=str)[:220]}")
    linhas.append(f"TOTAL de divergências: {total_div}")
    return "\n".join(linhas)


def _listar_rotas_dia_local(token: str, dia: date) -> list[dict]:
    from rotas_client import listar_rotas
    inicio, fim = janela_utc_do_dia(dia)
    filtro = [{"field": "start_at", "operator": "gte", "value": inicio},
              {"field": "start_at", "operator": "lt", "value": fim}]
    return listar_rotas(token, include=["services"], filtro=filtro)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compara Vuupt × núcleo (somente leitura).")
    parser.add_argument("--data", help="um dia (YYYY-MM-DD)")
    parser.add_argument("--dias", type=int, default=1, help="quantos dias até ONTEM (padrão 1)")
    parser.add_argument("--exemplos", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="imprime o resultado completo em JSON")
    parser.add_argument("--db", help="caminho do banco (padrão: dados/dados.db) -- pra comparar uma cópia")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import yaml
    # Aceita rodar de uma cópia fora do repo (ex.: /tmp) desde que o
    # diretório de trabalho seja o do projeto -- é como a VPS roda.
    caminho_config = next((c for c in (_RAIZ / "config.yaml", Path.cwd() / "config.yaml") if c.exists()), None)
    if caminho_config is None:
        logger.error("config.yaml não encontrado (nem em %s nem no diretório atual).", _RAIZ)
        return 1
    config = yaml.safe_load(caminho_config.read_text(encoding="utf-8")) or {}
    token = (config.get("vuupt_api") or {}).get("token", "")
    if not token:
        logger.error("vuupt_api.token ausente no config.yaml.")
        return 1

    if args.data:
        dias = [date.fromisoformat(args.data)]
    else:
        ontem = date.today() - timedelta(days=1)
        dias = [ontem - timedelta(days=i) for i in range(args.dias)][::-1]

    caminho = Path(args.db) if args.db else banco.DB_PATH
    conn = sqlite3.connect(f"{caminho.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        resultados = [comparar_dia(d, _listar_rotas_dia_local(token, d), conn) for d in dias]
    finally:
        conn.close()

    if args.json:
        print(json.dumps(resultados, ensure_ascii=False, indent=1, default=str))
    else:
        print(resumir(resultados, args.exemplos))
    tem_divergencia = any(itens for r in resultados for itens in r["divergencias"].values())
    return 2 if tem_divergencia else 0


if __name__ == "__main__":
    sys.exit(main())
