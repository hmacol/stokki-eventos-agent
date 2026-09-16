# -*- coding: utf-8 -*-
"""
nucleo/migrar_espelho_15_09.py

Migração ÚNICA do que já está gravado no núcleo, junto com as correções do
espelho (Etapa 2 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md). O código novo já
grava certo daqui pra frente; isto conserta o passado:

  1. codigo_paradas   nucleo_paradas.codigo -> sem '#', maiúsculo
  2. codigo_pedidos   o mesmo em nucleo_pedidos, MESCLANDO o par
                      '#PS-x' + 'PS-x' (22 pares em 12/09) numa linha só
  3. fuso_rotas       start_at / iniciada_em / concluida_em / cancelada_em
                      das rotas VUUPT: UTC -> hora local (e data_rota junto)
  4. fuso_paradas     started_at / arrived_at / completed_at idem
  5. fuso_eventos     ocorrido_em dos eventos VUUPT_SYNC idem
  6. fuso_agendamento agendamento_inicio/fim que vieram do serviço da VUUPT
  7. status_pedidos   pedido ABERTO/EM_ROTA que já tinha resultado numa rota
                      real (15 casos em 12/09, causados pelo bug do upsert)

Cada passo é registrado em `nucleo_migracoes` e NÃO roda duas vezes -- o
que importa aqui, porque converter fuso duas vezes tiraria mais 3 h.
Nada é apagado sem deixar rastro: a linha duplicada some, mas o conteúdo
dela vai pra um evento PEDIDO_CODIGO_MESCLADO.

COMO USAR (com backup do dia conferido antes):
    venv/bin/python nucleo/migrar_espelho_15_09.py --modo-teste     # mostra o que faria
    venv/bin/python nucleo/migrar_espelho_15_09.py --db /tmp/copia.db
    sudo -u www-data venv/bin/python nucleo/migrar_espelho_15_09.py
"""
import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo import banco
from nucleo.normalizacao import vuupt_para_local

logger = logging.getLogger("nucleo.migrar_espelho")

_SQL_NORMALIZA = "upper(trim(ltrim(trim(codigo), '#')))"

_COLUNAS_PEDIDO = [
    "vuupt_service_id", "titulo", "tipo", "destinatario_nome", "destinatario_codigo", "destinatario_telefone",
    "endereco", "latitude", "longitude", "horario_inicio", "horario_fim", "remetente_nome", "remetente_codigo",
    "sender_id", "caixas", "agendamento_inicio", "agendamento_fim",
]

# Situação da parada -> status do pedido (mesma tabela do sync e da API).
_RESULTADO_PARA_STATUS = {
    banco.PARADA_ENTREGUE: banco.PEDIDO_ENTREGUE,
    banco.PARADA_PARCIAL: banco.PEDIDO_ENTREGUE,
    banco.PARADA_INSUCESSO: banco.PEDIDO_INSUCESSO,
}


def _ja_aplicada(conn: sqlite3.Connection, nome: str) -> bool:
    conn.execute("""CREATE TABLE IF NOT EXISTS nucleo_migracoes (
                        nome TEXT PRIMARY KEY,
                        aplicada_em TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                        detalhes_json TEXT)""")
    return conn.execute("SELECT 1 FROM nucleo_migracoes WHERE nome = ?", (nome,)).fetchone() is not None


def _registrar(conn: sqlite3.Connection, nome: str, detalhes: dict):
    conn.execute("INSERT OR REPLACE INTO nucleo_migracoes (nome, detalhes_json) VALUES (?, ?)",
                 (nome, json.dumps(detalhes, ensure_ascii=False, default=str)))


def passo_codigo_paradas(conn: sqlite3.Connection) -> dict:
    n = conn.execute(f"SELECT COUNT(*) FROM nucleo_paradas WHERE codigo IS NOT NULL AND codigo <> {_SQL_NORMALIZA}"
                     ).fetchone()[0]
    conn.execute(f"UPDATE nucleo_paradas SET codigo = {_SQL_NORMALIZA}, atualizado_em = atualizado_em "
                 f"WHERE codigo IS NOT NULL AND codigo <> {_SQL_NORMALIZA}")
    return {"paradas_normalizadas": n}


def passo_codigo_pedidos(conn: sqlite3.Connection) -> dict:
    pares = conn.execute(f"""
        SELECT a.codigo AS com_cerquilha, b.codigo AS sem_cerquilha
          FROM nucleo_pedidos a JOIN nucleo_pedidos b ON b.codigo = {_SQL_NORMALIZA.replace('codigo', 'a.codigo')}
         WHERE a.codigo <> b.codigo
    """).fetchall()
    mesclados = 0
    for par in pares:
        linhas = conn.execute("SELECT * FROM nucleo_pedidos WHERE codigo IN (?, ?) ORDER BY atualizado_em DESC",
                              (par["com_cerquilha"], par["sem_cerquilha"])).fetchall()
        if len(linhas) != 2:
            continue
        novo, velho = dict(linhas[0]), dict(linhas[1])          # novo = atualizado por último
        valores = {c: (novo.get(c) if novo.get(c) is not None else velho.get(c)) for c in _COLUNAS_PEDIDO}
        dados = {}
        for linha in (velho, novo):                              # o mais novo por cima
            try:
                dados.update(json.loads(linha.get("dados_json") or "{}"))
            except ValueError:
                pass
        sets = ", ".join(f"{c} = ?" for c in _COLUNAS_PEDIDO)
        conn.execute(f"""UPDATE nucleo_pedidos SET {sets}, status = ?, origem = ?, dados_json = ?,
                                criado_em = ?, atualizado_em = ? WHERE codigo = ?""",
                     [valores[c] for c in _COLUNAS_PEDIDO]
                     + [novo["status"], velho.get("origem") or novo.get("origem"),
                        json.dumps(dados, ensure_ascii=False, default=str),
                        min(velho.get("criado_em") or "", novo.get("criado_em") or "") or novo.get("criado_em"),
                        max(velho.get("atualizado_em") or "", novo.get("atualizado_em") or ""),
                        par["sem_cerquilha"]])
        conn.execute("""INSERT INTO nucleo_eventos (tipo, origem, ocorrido_em, dados_json)
                        VALUES ('PEDIDO_CODIGO_MESCLADO', ?, ?, ?)""",
                     (banco.ORIGEM_PAINEL, banco.agora(),
                      json.dumps({"codigo": par["sem_cerquilha"], "linha_removida": novo if novo["codigo"].startswith("#") else velho},
                                 ensure_ascii=False, default=str)))
        conn.execute("DELETE FROM nucleo_pedidos WHERE codigo = ?", (par["com_cerquilha"],))
        mesclados += 1

    n = conn.execute(f"SELECT COUNT(*) FROM nucleo_pedidos WHERE codigo <> {_SQL_NORMALIZA}").fetchone()[0]
    conn.execute(f"UPDATE nucleo_pedidos SET codigo = {_SQL_NORMALIZA} WHERE codigo <> {_SQL_NORMALIZA}")
    return {"pares_mesclados": mesclados, "pedidos_renomeados": n}


def passo_fuso_rotas(conn: sqlite3.Connection) -> dict:
    """Converte só o que É carimbo da VUUPT: o valor gravado tem que bater
    com o bruto do dados_json (ou com o min/max das paradas, ainda crus
    nesta altura). Quando o sync usou a hora do servidor (fallback 'agora'),
    o valor já é local e fica como está."""
    mudadas = {"start_at": 0, "iniciada_em": 0, "concluida_em": 0, "cancelada_em": 0, "data_rota": 0}
    for rota in conn.execute("SELECT * FROM nucleo_rotas WHERE provedor = ?", (banco.PROVEDOR_VUUPT,)).fetchall():
        try:
            bruto = json.loads(rota["dados_json"] or "{}")
        except ValueError:
            bruto = {}
        extremos = conn.execute("""SELECT MIN(started_at), MAX(completed_at) FROM nucleo_paradas
                                   WHERE rota_id = ?""", (rota["id"],)).fetchone()
        campos, valores = [], []
        # start_at: o sync sobrescreve a cada rodada, então quem tem status
        # bruto da VUUPT tem start_at da VUUPT.
        if rota["status_provedor"] and rota["start_at"]:
            local = vuupt_para_local(rota["start_at"])
            if local and local != rota["start_at"]:
                campos += ["start_at = ?"]
                valores += [local]
                mudadas["start_at"] += 1
                if local[:10] != rota["data_rota"]:
                    campos += ["data_rota = ?"]
                    valores += [local[:10]]
                    mudadas["data_rota"] += 1
        for coluna, candidatos in (
            ("iniciada_em", {bruto.get("started_at"), bruto.get("done_started_at"), extremos[0]}),
            ("concluida_em", {bruto.get("finished_at"), bruto.get("done_finished_at"), extremos[1]}),
            ("cancelada_em", {bruto.get("canceled_at")}),
        ):
            atual = rota[coluna]
            if atual and atual in {c for c in candidatos if c}:
                local = vuupt_para_local(atual)
                if local and local != atual:
                    campos.append(f"{coluna} = ?")
                    valores.append(local)
                    mudadas[coluna] += 1
        if campos:
            conn.execute(f"UPDATE nucleo_rotas SET {', '.join(campos)} WHERE id = ?", valores + [rota["id"]])
    return mudadas


def passo_fuso_paradas(conn: sqlite3.Connection) -> dict:
    """Converte só o valor que ainda é IGUAL ao bruto do serviço guardado em
    dados_json. Parada que o sync novo já regravou (em hora local) tem valor
    diferente do bruto e fica quieta -- é o que impede tirar 3 h duas vezes
    se a migração rodar depois de uma rodada do sync."""
    n = 0
    puladas = 0
    linhas = conn.execute("""SELECT p.id, p.started_at, p.arrived_at, p.completed_at, p.dados_json
                             FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
                             WHERE r.provedor = ? AND (p.started_at IS NOT NULL OR p.arrived_at IS NOT NULL
                                                       OR p.completed_at IS NOT NULL)""",
                          (banco.PROVEDOR_VUUPT,)).fetchall()
    for p in linhas:
        try:
            bruto = json.loads(p["dados_json"] or "{}")
        except ValueError:
            bruto = {}
        bruto = bruto.get("service", bruto) if isinstance(bruto, dict) else {}
        novos = {}
        for coluna in ("started_at", "arrived_at", "completed_at"):
            atual = p[coluna]
            if not atual or atual != bruto.get(coluna):
                puladas += 1 if atual else 0
                continue
            local = vuupt_para_local(atual)
            if local and local != atual:
                novos[coluna] = local
        if novos:
            conn.execute(f"UPDATE nucleo_paradas SET {', '.join(f'{c} = ?' for c in novos)} WHERE id = ?",
                         list(novos.values()) + [p["id"]])
            n += 1
    return {"paradas_convertidas": n, "paradas_olhadas": len(linhas), "carimbos_ja_locais": puladas}


def passo_fuso_eventos(conn: sqlite3.Connection) -> dict:
    """ocorrido_em do evento VUUPT_SYNC é o completed_at cru (UTC) ou a hora
    do próprio sync (local). Diferença de mais de 1 h pro recebido_em só
    acontece no primeiro caso."""
    linhas = conn.execute("""SELECT id, ocorrido_em FROM nucleo_eventos
                             WHERE origem = ? AND ocorrido_em IS NOT NULL
                               AND ABS(strftime('%s', ocorrido_em) - strftime('%s', recebido_em)) > 3600""",
                          (banco.ORIGEM_VUUPT_SYNC,)).fetchall()
    n = 0
    for e in linhas:
        local = vuupt_para_local(e["ocorrido_em"])
        if local and local != e["ocorrido_em"]:
            conn.execute("UPDATE nucleo_eventos SET ocorrido_em = ? WHERE id = ?", (local, e["id"]))
            n += 1
    return {"eventos_convertidos": n}


def passo_fuso_agendamento(conn: sqlite3.Connection) -> dict:
    linhas = conn.execute("""SELECT codigo, agendamento_inicio, agendamento_fim FROM nucleo_pedidos
                             WHERE json_valid(dados_json)
                               AND (json_extract(dados_json, '$.service.scheduled_start') = agendamento_inicio
                                 OR json_extract(dados_json, '$.service.scheduled_end') = agendamento_fim)""").fetchall()
    n = 0
    for p in linhas:
        novos = {c: vuupt_para_local(p[c]) for c in ("agendamento_inicio", "agendamento_fim") if p[c]}
        novos = {c: v for c, v in novos.items() if v and v != p[c]}
        if novos:
            conn.execute(f"UPDATE nucleo_pedidos SET {', '.join(f'{c} = ?' for c in novos)} WHERE codigo = ?",
                         list(novos.values()) + [p["codigo"]])
            n += 1
    return {"pedidos_convertidos": n}


def passo_status_pedidos(conn: sqlite3.Connection) -> dict:
    """Pedido ABERTO/EM_ROTA que já tem resultado numa rota real. Rota de
    teste (réplica) fica de fora -- ela copia o código do pedido de verdade."""
    linhas = conn.execute("""
        SELECT pe.codigo, pe.status, p.situacao, r.data_rota, r.id AS rota_id
          FROM nucleo_pedidos pe
          JOIN nucleo_paradas p ON p.codigo = pe.codigo
          JOIN nucleo_rotas r ON r.id = p.rota_id
         WHERE pe.status IN (?, ?) AND r.status <> ? AND p.situacao IN (?, ?, ?)
           AND COALESCE(r.dados_json, '') NOT LIKE '%replica_de%'
         ORDER BY pe.codigo, r.data_rota, p.id
    """, (banco.PEDIDO_ABERTO, banco.PEDIDO_EM_ROTA, banco.ROTA_CANCELADA,
          banco.PARADA_ENTREGUE, banco.PARADA_PARCIAL, banco.PARADA_INSUCESSO)).fetchall()
    ultimo = {l["codigo"]: l for l in linhas}      # ordenado: fica o mais recente
    corrigidos = {}
    for codigo, l in ultimo.items():
        novo = _RESULTADO_PARA_STATUS.get(l["situacao"])
        if not novo or novo == l["status"]:
            continue
        conn.execute("UPDATE nucleo_pedidos SET status = ?, atualizado_em = ? WHERE codigo = ?",
                     (novo, banco.agora(), codigo))
        conn.execute("""INSERT INTO nucleo_eventos (rota_id, tipo, origem, ocorrido_em, dados_json)
                        VALUES (?, 'PEDIDO_STATUS_CORRIGIDO', ?, ?, ?)""",
                     (l["rota_id"], banco.ORIGEM_PAINEL, banco.agora(),
                      json.dumps({"codigo": codigo, "antes": l["status"], "depois": novo,
                                  "motivo": "migracao 15/09: upsert gravava ABERTO com status None"},
                                 ensure_ascii=False)))
        corrigidos[codigo] = f'{l["status"]}->{novo}'
    return {"pedidos_corrigidos": len(corrigidos), "exemplos": list(corrigidos.items())[:10]}


PASSOS = [
    ("codigo_paradas", passo_codigo_paradas),
    ("codigo_pedidos", passo_codigo_pedidos),
    ("fuso_rotas", passo_fuso_rotas),
    ("fuso_paradas", passo_fuso_paradas),
    ("fuso_eventos", passo_fuso_eventos),
    ("fuso_agendamento", passo_fuso_agendamento),
    ("status_pedidos", passo_status_pedidos),
]


def migrar(conn: sqlite3.Connection, modo_teste: bool = False, apenas: list[str] | None = None) -> dict:
    resumo = {}
    for nome, funcao in PASSOS:
        if apenas and nome not in apenas:
            continue
        if _ja_aplicada(conn, nome):
            resumo[nome] = "já aplicada"
            continue
        detalhes = funcao(conn)
        if not modo_teste:
            _registrar(conn, nome, detalhes)
        resumo[nome] = detalhes
    if modo_teste:
        conn.rollback()
    else:
        conn.commit()
    return resumo


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Migração única do espelho do núcleo (15/09).")
    parser.add_argument("--db", help="caminho do banco (padrão: dados/dados.db)")
    parser.add_argument("--modo-teste", action="store_true", help="roda tudo e desfaz no fim (só mostra os números)")
    parser.add_argument("--passo", action="append", help="roda só este passo (pode repetir)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    conn = banco.conectar(Path(args.db) if args.db else None)
    try:
        resumo = migrar(conn, modo_teste=args.modo_teste, apenas=args.passo)
    finally:
        conn.close()
    for nome, detalhes in resumo.items():
        logger.info(f"{nome}: {detalhes}")
    if args.modo_teste:
        logger.info("--modo-teste: nada foi gravado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
