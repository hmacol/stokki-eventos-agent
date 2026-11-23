# -*- coding: utf-8 -*-
"""
Posicao DOCA: espera do recebimento por etapas (a conferencia lanca aqui,
a fila "Guardar" leva dali para a posicao final).
"""
import sqlite3
from pathlib import Path

import wms
import wms_caixas
from wms import ErroWMS

DOCA_AREA = "DOC1"
DOCA_POSICAO = "DOC1-P1"


class TemperaturaDiferente(ErroWMS):
    """Produto de uma temperatura numa area de outra: a tela pede confirmacao
    e reenvia com confirmar_temperatura=True (mesmo uuid)."""

    def __init__(self, mensagem: str, dados: dict):
        super().__init__(mensagem)
        self.dados = dados


def conectar(caminho: Path | None = None):
    return wms_caixas.conectar(caminho)


def e_doca(codigo: str | None) -> bool:
    return bool(codigo) and wms.normalizar_codigo(codigo) == DOCA_POSICAO


def temperatura_do_produto(conn, produto_id: int) -> str:
    """Cadastro; senao a temperatura da area onde ha mais saldo (sem a doca); senao ''."""
    row = conn.execute("SELECT temperatura FROM wms_produtos WHERE id = ?", (produto_id,)).fetchone()
    if row and row["temperatura"]:
        return row["temperatura"]
    row = conn.execute(
        "SELECT a.temperatura FROM wms_saldos s "
        "JOIN wms_posicoes p ON p.codigo = s.posicao JOIN wms_areas a ON a.codigo = p.area_codigo "
        "WHERE s.produto_id = ? AND s.quantidade > 0 AND a.codigo <> ? "
        "GROUP BY a.temperatura ORDER BY SUM(s.quantidade) DESC LIMIT 1",
        (produto_id, DOCA_AREA)).fetchone()
    return row["temperatura"] if row else ""


def garantir_doca(conn) -> str:
    """Cria (ou reativa) a area e a posicao da doca. Devolve o codigo da posicao."""
    area = wms.obter_area(conn, DOCA_AREA)
    if not area or not area["ativo"]:
        wms.criar_area(conn, DOCA_AREA, "DOCA")
    wms.criar_posicao(conn, DOCA_POSICAO)
    return DOCA_POSICAO


def _distancia(a, b):
    """Chave de proximidade entre duas posicoes da mesma area (menor = mais perto)."""
    if a["pallet"] is not None and b["pallet"] is not None:
        return (0, abs(a["pallet"] - b["pallet"]), 0)
    if a["estante"] is not None and b["estante"] is not None:
        de = abs(a["estante"] - b["estante"])
        dn = abs(a["nivel"] - b["nivel"])
        return (0, dn, 0) if de == 0 else (1, de, dn)
    return (2, 0, 0)


def sugerir_posicoes(conn, produto_id: int, lote: str, validade: str | None, limite: int = 3) -> list[dict]:
    """Ate `limite` posicoes para guardar o produto, com o motivo. So leitura.
    Ordem fixa: MESMO_LOTE, AO_LADO, EMBARCADOR, VAZIA."""
    lote, validade = lote or "", validade or ""
    temperatura = temperatura_do_produto(conn, produto_id)
    sql = ("SELECT p.codigo, p.area_codigo, p.estante, p.nivel, p.pallet FROM wms_posicoes p "
           "JOIN wms_areas a ON a.codigo = p.area_codigo "
           "WHERE p.ativo = 1 AND a.ativo = 1 AND p.codigo <> ?")
    args = [DOCA_POSICAO]
    if temperatura:
        sql += " AND a.temperatura = ?"
        args.append(temperatura)
    posicoes = {r["codigo"]: r for r in conn.execute(sql + " ORDER BY p.codigo", args)}

    ocupadas, do_produto, mesmo_lote = set(), set(), {}
    emb = conn.execute("SELECT embarcador_id FROM wms_produtos WHERE id = ?", (produto_id,)).fetchone()
    emb_id = emb["embarcador_id"] if emb else None
    por_area_emb = {}
    for s in conn.execute(
            "SELECT s.posicao, s.produto_id, s.lote, s.validade, s.quantidade, pr.embarcador_id "
            "FROM wms_saldos s JOIN wms_produtos pr ON pr.id = s.produto_id WHERE s.quantidade > 0"):
        ocupadas.add(s["posicao"])
        pos = posicoes.get(s["posicao"])
        if not pos:
            continue
        if s["produto_id"] == produto_id:
            do_produto.add(s["posicao"])
            if s["lote"] == lote and s["validade"] == validade:
                mesmo_lote[s["posicao"]] = mesmo_lote.get(s["posicao"], 0) + s["quantidade"]
        if emb_id is not None and s["embarcador_id"] == emb_id:
            por_area_emb.setdefault(pos["area_codigo"], set()).add(s["posicao"])

    vazias = [p for c, p in posicoes.items() if c not in ocupadas]
    resultado, usadas = [], set()

    def adicionar(codigo, criterio, motivo):
        if codigo not in usadas:
            usadas.add(codigo)
            resultado.append({"posicao": codigo, "criterio": criterio, "motivo": motivo})

    for codigo in sorted(mesmo_lote):
        qtd = mesmo_lote[codigo]
        motivo = (f"Já tem este produto aqui (lote {lote}, {qtd:g} UN)" if lote
                  else f"Já tem este produto aqui ({qtd:g} UN)")
        adicionar(codigo, "MESMO_LOTE", motivo)

    candidatas = []
    for v in vazias:
        dists = [_distancia(v, posicoes[c]) for c in do_produto
                 if posicoes[c]["area_codigo"] == v["area_codigo"]]
        if dists:
            candidatas.append((min(dists), v["codigo"]))
    for _, codigo in sorted(candidatas):
        adicionar(codigo, "AO_LADO", "Ao lado do mesmo produto")

    por_emb = sorted((v for v in vazias if v["area_codigo"] in por_area_emb),
                     key=lambda v: (-len(por_area_emb[v["area_codigo"]]), v["codigo"]))
    for v in por_emb:
        adicionar(v["codigo"], "EMBARCADOR", "Perto de outros produtos do mesmo embarcador")

    for v in vazias:
        adicionar(v["codigo"], "VAZIA", "Posição vazia na área certa")

    return resultado[:limite]


# ---------------------------------------------------------------------------
# Guardar: da doca para a posicao, com a reserva acompanhando
# ---------------------------------------------------------------------------
# Os movimentos vem primeiro (cada um faz o proprio commit); a reserva e levada
# depois, numa transacao curta, e esse passo roda sempre: se a queda for entre
# os dois, repetir com o mesmo uuid acha os movimentos (duplicados) e so
# completa a reserva.

def _levar_reservas(conn, produto_id: int, lote: str, validade: str, destino: str) -> int:
    """Reservas ATIVAS da chave na doca que passam do saldo que sobrou la vao para o
    destino, por id crescente (a ultima e dividida). Reserva de item separado nunca vai:
    a caixa dele nao sai da doca e a baixa tira dali. Devolve quantas linhas foram
    reapontadas ou criadas."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        saldo = wms._saldo_atual(conn, DOCA_POSICAO, produto_id, lote, validade)
        reservas = conn.execute(
            "SELECT * FROM wms_reservas WHERE estado = 'ATIVA' AND posicao = ? AND produto_id = ? AND lote = ? "
            "AND COALESCE(validade, '') = ? ORDER BY id", (DOCA_POSICAO, produto_id, lote, validade)).fetchall()
        try:
            presas = {r[0] for r in conn.execute(
                "SELECT reserva_id FROM wms_separacao_itens WHERE reserva_id IS NOT NULL "
                "AND etapa IN ('SEPARADA', 'DISTRIBUIDA', 'CONFERIDA')")}
        except sqlite3.OperationalError:   # banco sem a separacao
            presas = set()
        excedente = round(sum(float(r["quantidade_un"]) for r in reservas) - saldo, 3)
        ts, n = wms.agora(), 0
        for r in reservas:
            if excedente <= 0.0004:
                break
            if r["id"] in presas:
                continue
            q = round(float(r["quantidade_un"]), 3)
            if q <= excedente + 0.0004:
                conn.execute("UPDATE wms_reservas SET posicao = ?, atualizado_em = ? WHERE id = ?",
                             (destino, ts, r["id"]))
                excedente = round(excedente - q, 3)
            else:
                conn.execute("UPDATE wms_reservas SET quantidade_un = ?, atualizado_em = ? WHERE id = ?",
                             (round(q - excedente, 3), ts, r["id"]))
                conn.execute(
                    "INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade, quantidade_un, "
                    "estado, origem, movimento_uuid, criado_em, atualizado_em) "
                    "VALUES (?,?,?,?,?,?,?, 'ATIVA', ?, '', ?, ?)",
                    (r["pedido_id"], r["item_id"], r["produto_id"], destino, r["lote"], r["validade"], excedente,
                     r["origem"], ts, ts))
                excedente = 0.0
            n += 1
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise


def guardar(conn, *, produto_id: int, lote: str, validade: str | None, destino: str, uuid: str,
            operador: dict | None = None, quantidade: float | None = None, seriais: list[str] | None = None,
            confirmar_temperatura: bool = False) -> dict:
    """Leva produto + lote + validade da doca para `destino`. Com caixa: todas as da chave
    na doca (ou so `seriais`), cada uma por mover_caixa; sem caixa: `quantidade` (None = tudo)
    numa TRANSFERENCIA. Repetir com o mesmo uuid nao move de novo e vale o destino ja gravado."""
    uuid = str(uuid or "").strip()
    if not uuid:
        raise ErroWMS("uuid do aparelho obrigatorio para guardar.")
    wms_caixas._fechar_transacao(conn)
    produto_id = int(produto_id)
    lote = " ".join(str(lote or "").split()).upper()
    validade = wms._validar_validade(validade)
    val = validade or ""
    prefixo = f"g-{uuid}-"
    feitos = conn.execute("SELECT * FROM wms_movimentos WHERE uuid = ? OR substr(uuid, 1, ?) = ?",
                          (f"g-{uuid}", len(prefixo), prefixo)).fetchall()
    for m in feitos:
        if (m["posicao_origem"] != DOCA_POSICAO or m["produto_id"] != produto_id or m["lote"] != lote
                or (m["validade"] or "") != val):
            raise ErroWMS(f"uuid {uuid} ja foi usado por outro movimento.")

    sufixo = ""
    if feitos:
        destino = feitos[0]["posicao_destino"]   # resposta perdida: vale o destino ja gravado
    else:
        destino = wms.normalizar_codigo(destino)
        if e_doca(destino):
            raise ErroWMS("Bipe a posição de destino, não a doca.")
        pos = wms._posicao_ativa(conn, destino, "destino")
        temp = temperatura_do_produto(conn, produto_id)
        if temp and temp != pos["temperatura"]:
            if not confirmar_temperatura:
                sug = sugerir_posicoes(conn, produto_id, lote, validade, limite=1)
                raise TemperaturaDiferente(
                    f"{destino} é {pos['temperatura'].lower()} e o produto é {temp.lower()}.",
                    {"produto": temp, "area": pos["temperatura"], "sugerida": sug[0]["posicao"] if sug else None})
            sufixo = " · temperatura diferente confirmada"

    # Caixa em qualquer situacao que ainda esta no saldo da doca (inclusive a separada, que
    # nao sai): sem isso a chave cairia no caminho sem caixa e levaria o saldo dela.
    ja_caixas = {m["uuid"][len(prefixo):] for m in feitos if m["uuid"].startswith(prefixo)}
    marcas = ",".join("?" * len(wms_caixas.NO_SALDO))
    na_doca = [r["serial"] for r in conn.execute(
        f"SELECT serial FROM wms_caixas WHERE posicao = ? AND produto_id = ? AND lote = ? "
        f"AND COALESCE(validade, '') = ? AND situacao IN ({marcas}) ORDER BY serial",
        (DOCA_POSICAO, produto_id, lote, val, *wms_caixas.NO_SALDO))]
    com_caixa = seriais is not None or bool(na_doca or ja_caixas)
    if com_caixa:
        if seriais is None:
            alvo = sorted(set(na_doca) | ja_caixas)
        else:
            alvo = []
            for s in seriais:
                s = str(s or "").strip().upper()
                if s not in na_doca and s not in ja_caixas:
                    raise ErroWMS(f"Caixa {s} não é deste produto, lote e validade na doca.")
                if s not in alvo:
                    alvo.append(s)
            if not alvo:
                raise ErroWMS("Bipe ao menos uma caixa.")
    elif not feitos:
        q = wms._saldo_atual(conn, DOCA_POSICAO, produto_id, lote, val) if quantidade is None else quantidade
        if quantidade is None and q <= 0.0004:
            raise ErroWMS("Nada deste produto na doca.")

    movidas, nao_movidas = [], []
    try:
        if com_caixa:
            for s in alvo:
                if s not in ja_caixas and wms_caixas._prometido(conn, s) > 0.0004:
                    nao_movidas.append({"serial": s, "motivo": "unidades separadas para pedido"})
                    continue
                # levar_reserva so vale saindo da doca (outro aparelho pode ter movido a caixa agora)
                if s not in ja_caixas and wms_caixas.obter_caixa(conn, s)["posicao"] != DOCA_POSICAO:
                    raise ErroWMS(f"Caixa {s} saiu da doca durante a guarda.")
                wms_caixas.mover_caixa(conn, s, destino, uuid=f"{prefixo}{s}", operador=operador,
                                       levar_reserva=True)
                movidas.append(s)
        elif not feitos:
            wms.registrar_movimento(conn, tipo="TRANSFERENCIA", produto_id=produto_id, quantidade=q, lote=lote,
                                    validade=validade, origem=DOCA_POSICAO, destino=destino, operador=operador,
                                    uuid=f"g-{uuid}", observacao="Guarda da doca" + sufixo)
    finally:
        levadas = _levar_reservas(conn, produto_id, lote, val, destino)

    total = 0.0
    for u in ([f"{prefixo}{s}" for s in movidas] if com_caixa else [f"g-{uuid}"]):
        row = conn.execute("SELECT quantidade FROM wms_movimentos WHERE uuid = ?", (u,)).fetchone()
        total += float(row["quantidade"]) if row else 0.0
    return {"movidas": movidas, "quantidade": round(total, 3),
            "restante_na_doca": round(wms._saldo_atual(conn, DOCA_POSICAO, produto_id, lote, val), 3),
            "nao_movidas": nao_movidas, "reservas_levadas": levadas, "destino": destino}
