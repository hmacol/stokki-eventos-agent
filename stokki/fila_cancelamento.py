# -*- coding: utf-8 -*-
"""
stokki/fila_cancelamento.py

Fila do "Cancelar Stokki" da tela /pedidos do painel (Hugo, 18/11: "incluir
solicitacao na fila sempre"). O clique nunca mais fala com a Stokki: grava
uma linha por PS em stokki_cancelamentos_fila (NA_FILA) e volta na hora. O
worker do portal (portal_cliente/enviar_stokki.py, ciclo de 20 s) pega a
trava da Stokki e executa (processar); Stokki ocupada = a linha espera o
proximo ciclo, sem prazo. A tela acompanha por andamento().

Status: NA_FILA -> RODANDO -> OK | ERRO. Recusa da Stokki (ja expedido...)
vira ERRO na hora; falha de rede volta pra NA_FILA ate MAX_TENTATIVAS. Uma
RODANDO esquecida (worker morreu) volta pra fila depois de ORFAO_SEGUNDOS:
cancelar de novo e seguro (a Stokki responde "ja estava cancelado").
Cada fim registra a tratativa do pedido (CANCELADO_STOKKI ou
CANCELAMENTO_STOKKI_FALHOU) com quem pediu e o motivo.
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MAX_TENTATIVAS = 3
ORFAO_SEGUNDOS = 15 * 60
MAX_POR_CICLO = 10          # cada PS leva ~30 s; o resto fica pro ciclo seguinte
_FMT = "%Y-%m-%d %H:%M:%S"
ABERTOS = ("NA_FILA", "RODANDO")


def _agora() -> str:
    return datetime.now().strftime(_FMT)


def garantir_tabela(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stokki_cancelamentos_fila ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, id_stokki INTEGER NOT NULL, codigo TEXT, motivo TEXT, "
        "usuario TEXT, status TEXT NOT NULL, situacao_antes TEXT, situacao TEXT, ja_estava INTEGER, "
        "erro TEXT, tentativas INTEGER DEFAULT 0, criado_em TEXT, atualizado_em TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stokki_canc_fila_status ON stokki_cancelamentos_fila (status)")


def enfileirar(conn, codigo: str, ids_stokki, motivo: str, usuario: str) -> list[dict]:
    """Uma linha NA_FILA por PS. PS que ja esta na fila (NA_FILA/RODANDO) nao duplica: devolve a linha
    que ja existe. [{fila_id, id_stokki, novo}]."""
    garantir_tabela(conn)
    saida = []
    for id_stokki in ids_stokki:
        aberta = conn.execute(
            f"SELECT id FROM stokki_cancelamentos_fila WHERE id_stokki = ? AND status IN ({','.join('?' * len(ABERTOS))}) "
            "ORDER BY id LIMIT 1", (int(id_stokki), *ABERTOS)).fetchone()
        if aberta:
            saida.append({"fila_id": aberta[0], "id_stokki": int(id_stokki), "novo": False})
            continue
        agora = _agora()
        cur = conn.execute(
            "INSERT INTO stokki_cancelamentos_fila (id_stokki, codigo, motivo, usuario, status, tentativas, "
            "criado_em, atualizado_em) VALUES (?, ?, ?, ?, 'NA_FILA', 0, ?, ?)",
            (int(id_stokki), (codigo or "").strip(), motivo, usuario, agora, agora))
        saida.append({"fila_id": cur.lastrowid, "id_stokki": int(id_stokki), "novo": True})
    conn.commit()
    return saida


def andamento(conn, fila_ids) -> list[dict]:
    """Linhas pedidas, na ordem dos ids: [{fila_id, id_stokki, status, situacao, ja_estava, erro}]."""
    garantir_tabela(conn)
    ids = [int(i) for i in fila_ids]
    if not ids:
        return []
    linhas = conn.execute(
        f"SELECT id, id_stokki, status, situacao, ja_estava, erro FROM stokki_cancelamentos_fila "
        f"WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    por_id = {l[0]: l for l in linhas}
    return [{"fila_id": l[0], "id_stokki": l[1], "status": l[2], "situacao": l[3] or "",
             "ja_estava": bool(l[4]), "erro": l[5] or ""} for l in (por_id.get(i) for i in ids) if l]


def pendentes(conn) -> int:
    """Quantas linhas esperam o worker (NA_FILA + RODANDO orfa). Sem nenhuma, o worker nem pega a trava."""
    garantir_tabela(conn)
    _soltar_orfaos(conn)
    return conn.execute("SELECT COUNT(*) FROM stokki_cancelamentos_fila WHERE status = 'NA_FILA'").fetchone()[0]


def _soltar_orfaos(conn) -> None:
    limite = (datetime.now() - timedelta(seconds=ORFAO_SEGUNDOS)).strftime(_FMT)
    n = conn.execute("UPDATE stokki_cancelamentos_fila SET status = 'NA_FILA', atualizado_em = ? "
                     "WHERE status = 'RODANDO' AND atualizado_em < ?", (_agora(), limite)).rowcount
    conn.commit()
    if n:
        logger.warning(f"fila_cancelamento: {n} cancelamento(s) RODANDO esquecido(s) voltaram pra fila")


def _gravar(conn, fila_id: int, **campos) -> None:
    campos["atualizado_em"] = _agora()
    sets = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(f"UPDATE stokki_cancelamentos_fila SET {sets} WHERE id = ?", (*campos.values(), fila_id))
    conn.commit()


def _texto_ok(l: dict, r: dict) -> str:
    if r.get("ja_estava"):
        return f"{l['usuario']}: cancelado na Stokki (PS-{l['id_stokki']}, já estava cancelado): {l['motivo']}"
    return (f"{l['usuario']}: cancelado na Stokki (PS-{l['id_stokki']}, situação: "
            f"{r.get('situacao_antes') or '?'} → {r.get('situacao') or '?'}): {l['motivo']}")


def processar(conn, sessao, *, cancelar, registrar_evento, registrar_devolucao, limite: int = MAX_POR_CICLO) -> dict:
    """Executa ate `limite` linhas NA_FILA (mais antigas primeiro). Quem chama ja esta com a trava da Stokki.
    cancelar(sessao, id_stokki, motivo, prefixo=) = stokki.cancelar.cancelar_pedido;
    registrar_evento = tratativas.registrar_evento; registrar_devolucao(id_stokki, ja_estava)."""
    garantir_tabela(conn)
    total = {"ok": 0, "erro": 0, "de_novo": 0}
    linhas = conn.execute(
        "SELECT id, id_stokki, codigo, motivo, usuario, tentativas FROM stokki_cancelamentos_fila "
        "WHERE status = 'NA_FILA' ORDER BY id LIMIT ?", (int(limite),)).fetchall()
    for linha in linhas:
        l = dict(zip(("id", "id_stokki", "codigo", "motivo", "usuario", "tentativas"), tuple(linha)))
        _gravar(conn, l["id"], status="RODANDO")
        tentativas = int(l["tentativas"] or 0) + 1
        try:
            r = cancelar(sessao, l["id_stokki"], l["motivo"], prefixo=f"Fresh Hub Pedidos ({l['usuario']})")
        except Exception as e:  # noqa: BLE001 -- rede/sessao: tenta de novo no proximo ciclo
            erro = f"falha ao falar com a Stokki: {e}"
            logger.warning(f"fila_cancelamento: PS-{l['id_stokki']} tentativa {tentativas}: {e}")
            if tentativas < MAX_TENTATIVAS:
                _gravar(conn, l["id"], status="NA_FILA", tentativas=tentativas, erro=erro)
                total["de_novo"] += 1
                continue
            r = {"ok": False, "erro": erro}
        if r.get("ok"):
            _gravar(conn, l["id"], status="OK", situacao=r.get("situacao") or "",
                    situacao_antes=r.get("situacao_antes") or "", ja_estava=int(bool(r.get("ja_estava"))),
                    erro="", tentativas=tentativas)
            total["ok"] += 1
            try:
                registrar_devolucao(l["id_stokki"], bool(r.get("ja_estava")))
            except Exception as e:  # noqa: BLE001 -- a fila de devolucao nunca trava o cancelamento
                logger.warning(f"fila_cancelamento: PS-{l['id_stokki']} nao registrou na fila de devolucao: {e}")
            registrar_evento(l["codigo"], "PEDIDOS", "CANCELADO_STOKKI", texto=_texto_ok(l, r))
        else:
            erro = r.get("erro") or "a Stokki não cancelou"
            _gravar(conn, l["id"], status="ERRO", situacao=r.get("situacao") or "", erro=erro, tentativas=tentativas)
            total["erro"] += 1
            registrar_evento(l["codigo"], "PEDIDOS", "CANCELAMENTO_STOKKI_FALHOU",
                             texto=f"{l['usuario']}: Stokki não cancelou PS-{l['id_stokki']} ({erro}): {l['motivo']}")
    return total
