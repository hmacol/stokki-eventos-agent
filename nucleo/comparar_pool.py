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

`--fonte freshhub` (plano 1.2 do sistema novo): compara a Vuupt com GET
/api/pool da API nova (nucleo/freshhub_client), em sombra; as rodadas ficam
na mesma tabela com a coluna `fonte` e o placar é por fonte. Portão da 1.2:
3 dias úteis seguidos com 0 divergências próprias na fonte freshhub, antes
de `planejamento.fonte_pool: freshhub`.

Nunca escreve na Vuupt. Grava no banco só com --salvar/--email.

COMO USAR (VPS, como www-data):
    venv/bin/python nucleo/comparar_pool.py                  # imprime as diferenças
    venv/bin/python nucleo/comparar_pool.py --salvar --email # o que o timer horário roda
    venv/bin/python nucleo/comparar_pool.py --salvar --email --fonte freshhub   # idem, Vuupt x API nova
    venv/bin/python nucleo/comparar_pool.py --historico 10 [--fonte freshhub]   # placar por dia
Saída 0 = pools iguais; 2 = houve divergência.
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo import banco, freshhub_client, pool
from nucleo.normalizacao import normalizar_codigo
from regioes_dia_fixo import regra_dia_fixo_do_servico
from roteirizacao_dados import resolver_janela

logger = logging.getLogger("nucleo.comparar_pool")

CAMPOS = ("id", "address", "latitude", "longitude", "dimension_3", "sender_id", "customer_code",
          "agendado_para", "janela", "dia_fixo")


def _num(valor):
    """Coordenada com 4 casas (~10 m). Sombra 25-30/09: o pipeline de hora em
    hora regrava a coordenada do Google (6+ casas) e o sync regrava a da
    Vuupt (5 casas); a 5ª casa oscilava entre os dois (1 m) e contava como
    divergência."""
    try:
        return round(float(valor), 4)
    except (TypeError, ValueError):
        return None


def _so_digitos(valor) -> str | None:
    """Documento do destinatário só com dígitos: é assim que a tela consulta
    nível e horário (regras.complexidade_entrega._so_digitos). A Vuupt
    devolve '35064514000357' e o pipeline grava '35.064.514/0003-57'."""
    digitos = "".join(c for c in str(valor or "") if c.isdigit())
    return digitos or None


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
        "customer_code": _so_digitos((s.get("customer") or {}).get("code")),
        "agendado_para": (s.get("scheduled_start") or "")[:10] or None,
        "janela": (ini, fim) if ini else None,
        "dia_fixo": regra["nome"] if regra else None,
    }


def _utc(texto) -> datetime | None:
    """'AAAA-MM-DD HH:MM:SS' (Vuupt, UTC sem fuso) ou ISO com fuso/Z (dados_de da API nova) -> datetime UTC."""
    t = str(texto or "").strip()
    if not t:
        return None
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(t.replace(" ", "T", 1))
    except ValueError:
        return None
    return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d).astimezone(timezone.utc)


def comparar(pool_vuupt: list[dict], pool_nucleo: list[dict], dados_de=None, buscar_servico=None) -> dict:
    """Puro (é o que os testes exercitam). `dados_de` (snapshot da carga que gerou o pool do freshhub) e
    `buscar_servico(id)` (serviço atual na Vuupt) ligam a classe 'atraso' (plano 1.2, Focus 4): o que só
    difere porque o pool do novo é da carga de ate 30 min atras fica fora do placar dos 3 dias, como os
    duplicados -- serviço criado na Vuupt depois de `dados_de`, ou serviço que a Vuupt já atribuiu/apagou
    e o novo ainda mostra no pool. Sem os dois, nada muda (fonte núcleo)."""
    def por_codigo(itens):
        mapa, brutos, repetidos = {}, {}, []
        for s in itens:
            codigo = normalizar_codigo(s.get("code"))
            if not codigo:
                continue
            if codigo in mapa:
                repetidos.append(codigo)
            mapa[codigo] = projetar(s)
            brutos[codigo] = s
        return mapa, brutos, sorted(set(repetidos))

    # Achado 24/09 (PS-39958): a Vuupt pode ter DOIS serviços not_assigned
    # com o mesmo código (duplicado que verificar_pedidos_duplicados_vuupt.py
    # cancela às 18h). O núcleo é chaveado por código e mescla os dois numa
    # linha -- a tela mostraria 2 cards pela Vuupt e 1 pelo núcleo. Conta
    # como divergência, com nome próprio pra explicar o placar.
    v, v_bruto, duplicados = por_codigo(pool_vuupt)
    n, n_bruto, _ = por_codigo(pool_nucleo)
    diferentes = []
    # Código duplicado fica fora da comparação de campos: com duas cópias
    # (uma agendada, outra não) não há "valor certo" pro núcleo ter -- a
    # diferença é consequência do duplicado, não do espelho (produção 24/09).
    for codigo in sorted((set(v) & set(n)) - set(duplicados)):
        campos = {c: [v[codigo][c], n[codigo][c]] for c in CAMPOS if v[codigo][c] != n[codigo][c]}
        if campos:
            diferentes.append({"code": codigo, "campos": campos})
    so_v, so_n = sorted(set(v) - set(n)), sorted(set(n) - set(v))
    atraso = []
    limite = _utc(dados_de)
    if limite is not None:
        for codigo in list(so_v):
            criado = _utc(v_bruto[codigo].get("created_at"))
            if criado is not None and criado > limite:
                atraso.append(codigo)
                so_v.remove(codigo)
    if buscar_servico is not None:
        for codigo in list(so_n):
            sid = n_bruto[codigo].get("id")
            try:
                atual = buscar_servico(sid) if sid is not None else None
            except Exception as e:  # noqa: BLE001 - sem resposta, a divergência fica
                logger.warning(f"atraso: nao consegui conferir o servico {sid} ({codigo}) na Vuupt: {e}")
                continue
            if atual is None or (atual.get("status") or "") != "not_assigned":
                atraso.append(codigo)
                so_n.remove(codigo)
    atraso = sorted(atraso)
    # "próprias" = o que é do espelho; o duplicado é da Vuupt (Hugo, 24/09:
    # não some sozinho, entra no relatório mas não no placar dos 3 dias).
    proprias = len(so_v) + len(so_n) + len(diferentes)
    return {"total_vuupt": len(v), "total_vuupt_bruto": len(pool_vuupt), "total_nucleo": len(n),
            "so_na_vuupt": so_v, "so_no_nucleo": so_n, "diferentes": diferentes, "duplicados_na_vuupt": duplicados,
            "atraso": atraso, "divergencias_proprias": proprias,
            "total_divergencias": proprias + len(duplicados) + len(atraso)}


def resumir(r: dict, exemplos: int = 5, fonte: str = "nucleo") -> str:
    nome = _nome_fonte(fonte)
    linhas = [f"Pool: Vuupt {r['total_vuupt']} código(s) em {r.get('total_vuupt_bruto', r['total_vuupt'])} "
              f"serviço(s) × {nome} {r['total_nucleo']} -- {r['divergencias_proprias']} divergência(s) própria(s), "
              f"{len(r['duplicados_na_vuupt'])} duplicado(s) na Vuupt"]
    if r.get("duplicados_na_vuupt"):
        linhas.append(f"  código duplicado na Vuupt ({len(r['duplicados_na_vuupt'])}): "
                      f"{', '.join(r['duplicados_na_vuupt'][:exemplos])}")
    if r.get("atraso"):
        linhas.append(f"  atraso do pool do {nome} ({len(r['atraso'])}, fora do placar): "
                      f"{', '.join(r['atraso'][:exemplos])}")
    if r["so_na_vuupt"]:
        linhas.append(f"  só na Vuupt   ({len(r['so_na_vuupt'])}): {', '.join(r['so_na_vuupt'][:exemplos])}")
    if r["so_no_nucleo"]:
        linhas.append(f"  só no {nome:<8}({len(r['so_no_nucleo'])}): {', '.join(r['so_no_nucleo'][:exemplos])}")
    for d in r["diferentes"][:exemplos]:
        linhas.append(f"  {d['code']}: " + "; ".join(f"{c}: {a!r} × {b!r}" for c, (a, b) in d["campos"].items()))
    if len(r["diferentes"]) > exemplos:
        linhas.append(f"  ... e mais {len(r['diferentes']) - exemplos} com campo diferente")
    return "\n".join(linhas)


# ── Placar ────────────────────────────────────────────────────────────────────

FONTE_NUCLEO = pool.FONTE_NUCLEO
FONTE_FRESHHUB = pool.FONTE_FRESHHUB
FONTES = (FONTE_NUCLEO, FONTE_FRESHHUB)
_DDL = """CREATE TABLE {nome} (
              rodado_em             TEXT NOT NULL,
              total_vuupt           INTEGER NOT NULL DEFAULT 0,
              total_nucleo          INTEGER NOT NULL DEFAULT 0,
              total_divergencias    INTEGER NOT NULL DEFAULT 0,
              divergencias_proprias INTEGER NOT NULL DEFAULT 0,
              duplicados            INTEGER NOT NULL DEFAULT 0,
              detalhes_json         TEXT,
              fonte                 TEXT NOT NULL DEFAULT 'nucleo',
              PRIMARY KEY (rodado_em, fonte))"""


def _garantir_tabela(conn: sqlite3.Connection):
    existentes = {row[1] for row in conn.execute("PRAGMA table_info(nucleo_reconciliacoes_pool)")}
    if not existentes:
        conn.execute(_DDL.format(nome="nucleo_reconciliacoes_pool"))
        conn.commit()
        return
    for coluna in ("divergencias_proprias", "duplicados"):
        if coluna not in existentes:
            conn.execute(f"ALTER TABLE nucleo_reconciliacoes_pool ADD COLUMN {coluna} INTEGER NOT NULL DEFAULT 0")
    if "fonte" not in existentes:
        # 1.2 (25/10): duas fontes (núcleo e freshhub) na mesma rodada -> a chave passa a (rodado_em, fonte);
        # as rodadas antigas são todas do núcleo. O freshhub lê esta tabela pelo snapshot (nome mantido).
        conn.executescript(
            "BEGIN;"
            "ALTER TABLE nucleo_reconciliacoes_pool RENAME TO nucleo_reconciliacoes_pool_antiga;"
            + _DDL.format(nome="nucleo_reconciliacoes_pool") + ";"
            "INSERT INTO nucleo_reconciliacoes_pool (rodado_em, total_vuupt, total_nucleo, total_divergencias,"
            " divergencias_proprias, duplicados, detalhes_json, fonte)"
            " SELECT rodado_em, total_vuupt, total_nucleo, total_divergencias, divergencias_proprias, duplicados,"
            " detalhes_json, 'nucleo' FROM nucleo_reconciliacoes_pool_antiga;"
            "DROP TABLE nucleo_reconciliacoes_pool_antiga;"
            "COMMIT;")
    conn.commit()


def salvar(r: dict, conn: sqlite3.Connection, rodado_em: str | None = None, fonte: str = FONTE_NUCLEO):
    if fonte not in FONTES:
        raise ValueError(f"fonte desconhecida: {fonte}")
    _garantir_tabela(conn)
    conn.execute("""INSERT OR REPLACE INTO nucleo_reconciliacoes_pool
                    (rodado_em, total_vuupt, total_nucleo, total_divergencias, divergencias_proprias, duplicados,
                     detalhes_json, fonte)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (rodado_em or banco.agora(), r["total_vuupt"], r["total_nucleo"], r["total_divergencias"],
                  r["divergencias_proprias"], len(r["duplicados_na_vuupt"]),
                  json.dumps({k: r.get(k) for k in ("so_na_vuupt", "so_no_nucleo", "diferentes", "duplicados_na_vuupt",
                                                    "atraso")},
                             ensure_ascii=False, default=str), fonte))
    conn.commit()


def historico(conn: sqlite3.Connection, limite: int = 20, fonte: str = FONTE_NUCLEO) -> list[dict]:
    """Uma linha por DIA da fonte: pior rodada do dia (máximo de divergências
    PRÓPRIAS); `duplicados` é o máximo de códigos duplicados na Vuupt visto
    no dia, só pra explicar."""
    _garantir_tabela(conn)
    return [dict(r) for r in conn.execute(
        """SELECT substr(rodado_em, 1, 10) AS dia, COUNT(*) AS rodadas, MAX(total_vuupt) AS total_vuupt,
                  MAX(divergencias_proprias) AS pior, SUM(divergencias_proprias) AS soma, MAX(duplicados) AS duplicados
           FROM nucleo_reconciliacoes_pool WHERE fonte = ? GROUP BY dia ORDER BY dia DESC LIMIT ?""",
        (fonte, limite))]


def dias_limpos_seguidos(conn: sqlite3.Connection, fonte: str = FONTE_NUCLEO) -> int:
    """Dias COM pool, do mais recente pra trás, em que NENHUMA rodada da fonte
    achou divergência. Dia sem pool (fim de semana vazio) nem conta nem quebra."""
    seguidos = 0
    for linha in historico(conn, limite=60, fonte=fonte):
        if not linha["total_vuupt"]:
            continue
        if linha["pior"]:
            break
        seguidos += 1
    return seguidos


def _enviar_email(r: dict, conn: sqlite3.Connection, config: dict, exemplos: int, fonte: str = FONTE_NUCLEO):
    from email_utils import COR_ERRO, envelope_html, enviar_email
    limpos = dias_limpos_seguidos(conn, fonte=fonte)
    destino = (config.get("notificacao_execucao", {}) or {}).get("destinatario") or "hugo@freshlogbr.com"
    nome = _nome_fonte(fonte)
    titulo = (f"Pool Vuupt × {nome}: {r['divergencias_proprias']} divergência(s)"
              + (f" (+{len(r['duplicados_na_vuupt'])} duplicado(s) na Vuupt)" if r["duplicados_na_vuupt"] else ""))
    criterio = ("Portão da 1.2 (freshhub): 3 dias úteis seguidos sem divergência própria" if fonte == FONTE_FRESHHUB
                else "Critério da Entrega 1: 3 dias úteis seguidos sem divergência")
    corpo = [f"<h2 style='margin:0 0 12px'>{titulo}</h2>",
             f"<p>{criterio}. Hoje: <b>{limpos}</b>.</p>",
             "<pre style='white-space:pre-wrap;font-size:12px;background:#F3F4F6;padding:12px;border-radius:6px'>",
             resumir(r, exemplos, fonte=fonte), "</pre>"]
    enviar_email([destino], titulo, envelope_html("".join(corpo), cor_acento=COR_ERRO), config.get("email", {}))


def _nome_fonte(fonte: str) -> str:
    return "freshhub" if fonte == FONTE_FRESHHUB else "núcleo"


def _carregar_config() -> dict | None:
    import yaml
    caminho_config = next((c for c in (_RAIZ / "config.yaml", Path.cwd() / "config.yaml") if c.exists()), None)
    if caminho_config is None:
        return None
    return yaml.safe_load(caminho_config.read_text(encoding="utf-8")) or {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compara o pool Vuupt × núcleo (somente leitura na Vuupt).")
    parser.add_argument("--exemplos", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", help="caminho do banco (padrão: dados/dados.db)")
    parser.add_argument("--salvar", action="store_true", help="grava a rodada em nucleo_reconciliacoes_pool")
    parser.add_argument("--email", action="store_true", help="manda e-mail SÓ quando há divergência")
    parser.add_argument("--historico", type=int, metavar="N", help="placar dos últimos N dias (não vai na Vuupt)")
    parser.add_argument("--fonte", choices=FONTES, default=FONTE_NUCLEO,
                        help="o que comparar com a Vuupt: nucleo (padrão) ou freshhub (API nova, plano 1.2)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    caminho = Path(args.db) if args.db else banco.DB_PATH
    conn = banco.conectar(caminho)
    try:
        if args.historico:
            for linha in historico(conn, args.historico, fonte=args.fonte):
                marca = "OK " if not linha["pior"] else "!! "
                print(f"{marca}{linha['dia']}  rodadas={linha['rodadas']:2d} pool={linha['total_vuupt']:3d} "
                      f"pior={linha['pior']:3d} soma={linha['soma']:4d} duplicados_vuupt={linha['duplicados']:2d}")
            print(f"\nDias com pool, seguidos, sem divergência ({_nome_fonte(args.fonte)}): "
                  f"{dias_limpos_seguidos(conn, fonte=args.fonte)} (critério: 3 úteis)")
            return 0

        config = _carregar_config()
        if config is None:
            logger.error("config.yaml não encontrado.")
            return 1
        token = (config.get("vuupt_api") or {}).get("token", "")
        if not token:
            logger.error("vuupt_api.token ausente no config.yaml.")
            return 1
        cliente = None
        if args.fonte == FONTE_FRESHHUB:
            cliente = freshhub_client.de_config(config)
            if cliente is None:
                # a sombra só liga quando o Hugo põe freshhub_api.url/token no config da VPS
                logger.warning("fonte freshhub sem freshhub_api.url/token no config.yaml: sombra desligada, nada a comparar")
                return 0

        from vuupt_client import VuuptClient
        vuupt = VuuptClient(token)
        pool_vuupt = vuupt.listar_servicos([{"field": "status", "operator": "eq", "value": "not_assigned"}],
                                           per_page=100, include=["customer"])
        if cliente is not None:
            corpo = cliente.obter_pool()
            logger.info("pool do freshhub: %d item(ns), dados de %s", len(corpo.get("itens") or []),
                        corpo.get("dados_de"))
            pool_fonte = list(corpo.get("itens") or [])
            # o pool do novo e do snapshot da ultima carga: o que so difere por isso e 'atraso', nao divergencia
            r = comparar(pool_vuupt, pool_fonte, dados_de=corpo.get("dados_de"),
                         buscar_servico=vuupt.buscar_servico_por_id)
        else:
            pool_fonte = pool.listar_pool(conn)
            r = comparar(pool_vuupt, pool_fonte)
        if args.salvar:
            salvar(r, conn, fonte=args.fonte)
        # E-mail e saída 2 só por divergência PRÓPRIA: um duplicado na Vuupt
        # (que fica dias até alguém cancelar) não pode mandar 14 e-mails/dia.
        if args.email and r["divergencias_proprias"]:
            _enviar_email(r, conn, config, args.exemplos, fonte=args.fonte)
    finally:
        conn.close()

    print(json.dumps(r, ensure_ascii=False, indent=1, default=str) if args.json
          else resumir(r, args.exemplos, fonte=args.fonte))
    return 2 if r["divergencias_proprias"] else 0


if __name__ == "__main__":
    sys.exit(main())
