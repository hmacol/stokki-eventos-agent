# -*- coding: utf-8 -*-
"""
painel_agentes/retornos.py

Recebimento de retornos (Hugo, 23/10/2026): o motorista volta ao galpao
com os pedidos que nao entregou, descarrega no pallet e o recebedor confere
pelo celular (tela /retornos). Substitui o lancamento que era feito no
Fresh Hub do Lovable, agora ligado ao nosso fluxo.

Regras combinadas com o Hugo (spec 2026-10-23-recebimento-retornos-design.md):
- entra todo insucesso, de qualquer origem da baixa, e toda entrega
  parcial; so fica de fora o "Nao coletado";
- o recebedor confere caixas, avaria (com foto), confirma ou corrige o
  motivo e pode deixar observacao; foto da carga e assinatura do motorista
  sao obrigatorias por descarga;
- a confirmacao so registra e avisa: nao segura nem libera reentrega;
- divergencia (caixas diferentes, avaria, motivo corrigido) e parcial
  recebida viram mensagem no grupo "Alertas" do /atendimento;
- retorno nao recebido ate 07h do proximo dia util vira alerta no mesmo
  grupo (alertar_retornos_atrasados.py).

Fonte dos retornos: nucleo_paradas + nucleo_rotas (espelho da Vuupt a cada
30 min e rotas do app proprio). A chave do recebimento e a parada
(nucleo_paradas.id), nunca so o codigo: reentrega reaproveita codigo.
"""
import json
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import grupos_equipe
from insucesso_entrega.motivos_falha import MOTIVOS_FALHA
from nucleo.normalizacao import normalizar_codigo

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
NOME_GRUPO = "Alertas"
AUTOR_ALERTA = "Recebimento de retornos"
JANELA_DIAS = 7
NAO_COLETADO_VUUPT = 5744
TEXTO_PARCIAL = "PARCIAL: avisar o cliente e solicitar a entrada no estoque"


class ErroRetorno(Exception):
    """Mensagem pro usuario (vira 400 na API)."""


def conectar(caminho=None) -> sqlite3.Connection:
    conn = sqlite3.connect(caminho or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS retornos_descargas (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            motorista_nome   TEXT,
            motorista_cpf    TEXT,
            recebedor        TEXT,
            usuario          TEXT,
            recebido_em      TEXT,
            fotos_carga      TEXT,
            assinatura       TEXT,
            tem_divergencia  INTEGER NOT NULL DEFAULT 0,
            criado_em        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retornos_recebidos (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            descarga_id       INTEGER NOT NULL,
            parada_id         INTEGER NOT NULL UNIQUE,
            rota_id           INTEGER,
            service_id        INTEGER,
            codigo            TEXT,
            embarcador        TEXT,
            parcial           INTEGER NOT NULL DEFAULT 0,
            caixas_esperadas  INTEGER,
            caixas_recebidas  INTEGER,
            avaria            INTEGER NOT NULL DEFAULT 0,
            fotos_avaria      TEXT,
            motivo_original   TEXT,
            motivo_confirmado TEXT,
            motivo_corrigido  INTEGER NOT NULL DEFAULT 0,
            observacao        TEXT,
            divergente        INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_retornos_recebidos_service ON retornos_recebidos (service_id);
        CREATE INDEX IF NOT EXISTS idx_retornos_recebidos_codigo ON retornos_recebidos (codigo);
        CREATE TABLE IF NOT EXISTS retornos_alertas_atraso (
            parada_id   INTEGER PRIMARY KEY,
            alertado_em TEXT NOT NULL
        );
    """)
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalizar_texto(texto) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(sem_acento.lower().split())


def motivos_validos() -> list[str]:
    """Motivos que o recebedor pode escolher ao corrigir (sem "Nao coletado")."""
    return sorted({m["texto"] for k, m in MOTIVOS_FALHA.items() if k != NAO_COLETADO_VUUPT})


def _ids_nao_coletado(conn) -> set[int]:
    try:
        return {r[0] for r in conn.execute(
            "SELECT id FROM motivos_ocorrencia WHERE vuupt_failed_reason_id = ?", (NAO_COLETADO_VUUPT,))}
    except sqlite3.OperationalError:
        return set()  # tabela do app proprio ausente


def eh_nao_coletado(failed_reason_id, motivo_id, motivo_texto, ids_nao_coletado: set[int]) -> bool:
    return (failed_reason_id == NAO_COLETADO_VUUPT
            or (motivo_id is not None and motivo_id in ids_nao_coletado)
            or _normalizar_texto(motivo_texto) == "nao coletado")


# Paradas que sao retorno esperado. O filtro de "Nao coletado" fica em Python.
_SQL_RETORNOS = """
    SELECT p.id AS parada_id, p.rota_id, p.service_id, p.codigo, p.destinatario_nome, p.remetente_nome,
           p.volume_caixas, p.situacao, p.motivo_id, p.motivo_texto, p.failed_reason_id, p.completed_at,
           r.motorista_nome, r.motorista_cpf, r.nome AS rota_nome,
           COALESCE(substr(p.completed_at, 1, 10), r.data_rota) AS dia_insucesso
    FROM nucleo_paradas p
    JOIN nucleo_rotas r ON r.id = p.rota_id
    LEFT JOIN retornos_recebidos rr ON rr.parada_id = p.id
    WHERE p.situacao IN ('INSUCESSO', 'PARCIAL')
"""


def _pedido(row) -> dict:
    parcial = row["situacao"] == "PARCIAL"
    return {"parada_id": row["parada_id"], "rota_id": row["rota_id"], "service_id": row["service_id"],
            "codigo": row["codigo"], "destinatario": row["destinatario_nome"] or "",
            "embarcador": row["remetente_nome"] or "", "parcial": parcial,
            "caixas_esperadas": None if parcial else row["volume_caixas"],
            "motivo": row["motivo_texto"] or "", "baixa_em": row["completed_at"] or "",
            "dia_insucesso": row["dia_insucesso"], "rota_nome": row["rota_nome"] or "",
            "motorista_nome": row["motorista_nome"] or "Sem motorista", "motorista_cpf": row["motorista_cpf"] or ""}


def _agrupar(pedidos: list[dict], hoje: date) -> list[dict]:
    grupos: dict[str, dict] = {}
    for p in pedidos:
        chave = p["motorista_cpf"] or p["motorista_nome"]
        g = grupos.setdefault(chave, {"chave": chave, "motorista_nome": p["motorista_nome"],
                                      "motorista_cpf": p["motorista_cpf"], "pedidos": []})
        g["pedidos"].append(p)
    for g in grupos.values():
        g["pedidos"].sort(key=lambda p: (p["dia_insucesso"], p["codigo"] or ""))
        g["total_caixas"] = sum(p["caixas_esperadas"] or 0 for p in g["pedidos"])
        g["mais_antigo"] = g["pedidos"][0]["dia_insucesso"]
        g["desde_ontem"] = g["mais_antigo"] < hoje.isoformat()
    return sorted(grupos.values(), key=lambda g: (g["mais_antigo"], g["motorista_nome"]))


def _retornos_pendentes(conn, hoje: date, dias: int, extra_sql: str = "") -> list[dict]:
    ids_nc = _ids_nao_coletado(conn)
    inicio = (hoje - timedelta(days=dias)).isoformat()
    rows = conn.execute(_SQL_RETORNOS + " AND rr.id IS NULL" + extra_sql, ()).fetchall()
    return [_pedido(r) for r in rows
            if r["dia_insucesso"] and inicio <= r["dia_insucesso"] <= hoje.isoformat()
            and not eh_nao_coletado(r["failed_reason_id"], r["motivo_id"], r["motivo_texto"], ids_nc)]


def listar_pendentes(conn, hoje: date, dias: int = JANELA_DIAS) -> list[dict]:
    """Retornos esperados ainda nao recebidos, agrupados por motorista."""
    return _agrupar(_retornos_pendentes(conn, hoje, dias), hoje)


def reservar_descarga(conn) -> int:
    """Cria a descarga vazia so pra ter o id (pasta das fotos) antes de gravar."""
    cur = conn.execute("INSERT INTO retornos_descargas (criado_em) VALUES (?)", (_agora(),))
    conn.commit()
    return cur.lastrowid


def descartar_descarga(conn, descarga_id: int) -> None:
    """Apaga uma descarga reservada que nao chegou a ser confirmada."""
    conn.execute("DELETE FROM retornos_descargas WHERE id = ? AND recebido_em IS NULL", (descarga_id,))
    conn.commit()


def _paradas_por_id(conn, ids: list[int]) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
    marcas = ",".join("?" * len(ids))
    sql = _SQL_RETORNOS.replace("WHERE p.situacao", f"WHERE p.id IN ({marcas}) AND p.situacao")
    sql = sql.replace("SELECT p.id AS parada_id", "SELECT rr.id AS recebido_id, p.id AS parada_id")
    return {r["parada_id"]: r for r in conn.execute(sql, ids)}


def registrar_descarga(conn, *, descarga_id: int, itens: list[dict], recebedor: str, usuario: str,
                       fotos_carga: list[str], assinatura: str) -> dict:
    """Grava a conferencia de uma descarga. Esperado, motivo original e
    motorista vem sempre do nucleo, nunca do que o celular mandou."""
    recebedor = " ".join((recebedor or "").split())
    if not recebedor:
        raise ErroRetorno("Informe o nome de quem recebeu.")
    if not itens:
        raise ErroRetorno("Marque pelo menos um pedido que veio nesta descarga.")
    if not fotos_carga:
        raise ErroRetorno("Tire pelo menos uma foto da carga.")
    if not assinatura:
        raise ErroRetorno("Falta a assinatura do motorista.")

    ids = [int(i["parada_id"]) for i in itens]
    if len(set(ids)) != len(ids):
        raise ErroRetorno("Pedido repetido na descarga.")
    paradas = _paradas_por_id(conn, ids)
    ids_nc = _ids_nao_coletado(conn)
    validos = set(motivos_validos())
    linhas, motoristas = [], set()
    for item in itens:
        row = paradas.get(int(item["parada_id"]))
        if row is None or eh_nao_coletado(row["failed_reason_id"], row["motivo_id"], row["motivo_texto"], ids_nc):
            raise ErroRetorno("Pedido não está na lista de retornos.")
        p = _pedido(row)
        if row["recebido_id"] is not None:
            raise ErroRetorno(f"{p['codigo']} já foi recebido.")
        motoristas.add(p["motorista_cpf"] or p["motorista_nome"])
        caixas = item.get("caixas_recebidas")
        if caixas is None or caixas == "":
            raise ErroRetorno(f"Informe quantas caixas de {p['codigo']} voltaram.")
        caixas = int(caixas)
        if caixas < 0:
            raise ErroRetorno(f"Caixas de {p['codigo']} não pode ser negativo.")
        avaria = bool(item.get("avaria"))
        fotos_avaria = list(item.get("fotos_avaria") or [])
        if avaria and not fotos_avaria:
            raise ErroRetorno(f"Tire a foto da avaria de {p['codigo']}.")
        motivo = (item.get("motivo") or "").strip() or p["motivo"]
        if motivo != p["motivo"] and motivo not in validos:
            raise ErroRetorno(f"Motivo inválido em {p['codigo']}.")
        corrigido = _normalizar_texto(motivo) != _normalizar_texto(p["motivo"])
        caixas_diferentes = p["caixas_esperadas"] is not None and caixas != p["caixas_esperadas"]
        linhas.append({**p, "caixas_recebidas": caixas, "avaria": avaria, "fotos_avaria": fotos_avaria,
                       "motivo_confirmado": motivo, "motivo_corrigido": corrigido,
                       "observacao": (item.get("observacao") or "").strip(),
                       "divergente": caixas_diferentes or avaria or corrigido})
    if len(motoristas) > 1:
        raise ErroRetorno("A descarga é de um motorista só.")

    primeiro = linhas[0]
    tem_divergencia = any(x["divergente"] for x in linhas)
    try:
        for x in linhas:
            conn.execute("""
                INSERT INTO retornos_recebidos (descarga_id, parada_id, rota_id, service_id, codigo, embarcador,
                    parcial, caixas_esperadas, caixas_recebidas, avaria, fotos_avaria, motivo_original,
                    motivo_confirmado, motivo_corrigido, observacao, divergente)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (descarga_id, x["parada_id"], x["rota_id"], x["service_id"], x["codigo"], x["embarcador"],
                  int(x["parcial"]), x["caixas_esperadas"], x["caixas_recebidas"], int(x["avaria"]),
                  json.dumps(x["fotos_avaria"]), x["motivo"], x["motivo_confirmado"], int(x["motivo_corrigido"]),
                  x["observacao"], int(x["divergente"])))
        conn.execute("""
            UPDATE retornos_descargas SET motorista_nome = ?, motorista_cpf = ?, recebedor = ?, usuario = ?,
                recebido_em = ?, fotos_carga = ?, assinatura = ?, tem_divergencia = ?
            WHERE id = ?
        """, (primeiro["motorista_nome"], primeiro["motorista_cpf"], recebedor, usuario, _agora(),
              json.dumps(list(fotos_carga)), assinatura, int(tem_divergencia), descarga_id))
        conn.commit()
    except sqlite3.IntegrityError:
        # Outra pessoa recebeu o mesmo pedido entre a leitura e a gravacao.
        conn.rollback()
        raise ErroRetorno("Um dos pedidos acabou de ser recebido por outra pessoa. Atualize a tela.")

    return {"descarga_id": descarga_id, "motorista_nome": primeiro["motorista_nome"],
            "divergentes": [x for x in linhas if x["divergente"]],
            "parciais": [x for x in linhas if x["parcial"]]}


def _problemas(x: dict) -> list[str]:
    partes = []
    if x["caixas_esperadas"] is not None and x["caixas_recebidas"] != x["caixas_esperadas"]:
        partes.append(f"{x['caixas_recebidas']} de {x['caixas_esperadas']} cx")
    if x["avaria"]:
        partes.append("avaria")
    if x["motivo_corrigido"]:
        partes.append(f"motivo {x['motivo']} → {x['motivo_confirmado']}")
    return partes


def texto_alerta_descarga(resultado: dict, recebedor: str) -> str | None:
    """Mensagem do grupo Alertas pra uma descarga, ou None se nao ha o que avisar."""
    if not resultado["divergentes"] and not resultado["parciais"]:
        return None
    titulo = "Retorno com divergência" if resultado["divergentes"] else "Retorno parcial recebido"
    linhas = [f"{titulo} · {resultado['motorista_nome']}", f"Recebido por {recebedor}"]
    vistos = set()
    for x in resultado["divergentes"] + resultado["parciais"]:
        if x["parada_id"] in vistos:
            continue
        vistos.add(x["parada_id"])
        partes = _problemas(x)
        if x["parcial"]:
            partes.append(f"{x['caixas_recebidas']} cx voltaram · {TEXTO_PARCIAL}")
        linha = f"{x['codigo']} · {x['embarcador'] or 'sem embarcador'} · " + ", ".join(partes)
        if x["observacao"]:
            linha += f" · obs: {x['observacao']}"
        linhas.append(linha)
    linhas.append(f"Descarga #{resultado['descarga_id']} (Retornos > Recebidos)")
    return "\n".join(linhas)


def postar_alerta(conn_grupos, texto: str) -> None:
    """Posta no grupo "Alertas" do /atendimento (cria o grupo se faltar).
    conn_grupos vem de grupos_equipe.conectar."""
    row = conn_grupos.execute("SELECT id FROM equipe_grupos WHERE lower(nome) = lower(?)", (NOME_GRUPO,)).fetchone()
    grupo_id = row["id"] if row else grupos_equipe.criar_grupo(conn_grupos, NOME_GRUPO, "sistema", AUTOR_ALERTA)["id"]
    grupos_equipe.postar(conn_grupos, grupo_id, "sistema", AUTOR_ALERTA, texto[:grupos_equipe.TEXTO_MAX])


_SQL_STATUS = """
    SELECT rr.service_id, rr.codigo, rr.divergente, rr.caixas_recebidas, rr.caixas_esperadas, rr.parcial,
           rr.avaria, rr.motivo_confirmado, rr.observacao, d.id AS descarga_id, d.recebido_em, d.recebedor
    FROM retornos_recebidos rr JOIN retornos_descargas d ON d.id = rr.descarga_id
"""


def _status(row) -> dict:
    return {"descarga_id": row["descarga_id"], "recebido_em": row["recebido_em"], "recebedor": row["recebedor"],
            "divergente": bool(row["divergente"]), "codigo": row["codigo"], "parcial": bool(row["parcial"]),
            "caixas_recebidas": row["caixas_recebidas"], "caixas_esperadas": row["caixas_esperadas"],
            "avaria": bool(row["avaria"]), "motivo": row["motivo_confirmado"], "observacao": row["observacao"]}


def status_por_service_ids(conn, ids: list[int]) -> dict[int, dict]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    marcas = ",".join("?" * len(ids))
    return {r["service_id"]: _status(r) for r in conn.execute(_SQL_STATUS + f" WHERE rr.service_id IN ({marcas})", ids)}


def status_por_codigo(conn, codigo: str) -> list[dict]:
    cod = normalizar_codigo(codigo)
    return [_status(r) for r in conn.execute(_SQL_STATUS + " WHERE upper(rr.codigo) = ? ORDER BY d.recebido_em",
                                             (cod,))]


def listar_descargas(conn, limite: int = 50) -> list[dict]:
    rows = conn.execute("""
        SELECT d.id, d.motorista_nome, d.recebedor, d.recebido_em, d.tem_divergencia,
               COUNT(rr.id) AS pedidos, SUM(rr.parcial) AS parciais
        FROM retornos_descargas d LEFT JOIN retornos_recebidos rr ON rr.descarga_id = d.id
        WHERE d.recebido_em IS NOT NULL
        GROUP BY d.id ORDER BY d.recebido_em DESC, d.id DESC LIMIT ?
    """, (limite,)).fetchall()
    return [{**dict(r), "tem_divergencia": bool(r["tem_divergencia"]), "parciais": r["parciais"] or 0} for r in rows]


def detalhar_descarga(conn, descarga_id: int) -> dict | None:
    d = conn.execute("SELECT * FROM retornos_descargas WHERE id = ? AND recebido_em IS NOT NULL",
                     (descarga_id,)).fetchone()
    if not d:
        return None
    itens = conn.execute("SELECT * FROM retornos_recebidos WHERE descarga_id = ? ORDER BY codigo",
                         (descarga_id,)).fetchall()
    return {"id": d["id"], "motorista_nome": d["motorista_nome"], "recebedor": d["recebedor"],
            "recebido_em": d["recebido_em"], "tem_divergencia": bool(d["tem_divergencia"]),
            "fotos_carga": json.loads(d["fotos_carga"] or "[]"), "assinatura": d["assinatura"],
            "itens": [{**dict(i), "fotos_avaria": json.loads(i["fotos_avaria"] or "[]"),
                       "avaria": bool(i["avaria"]), "parcial": bool(i["parcial"]),
                       "divergente": bool(i["divergente"]), "motivo_corrigido": bool(i["motivo_corrigido"])}
                      for i in itens]}
