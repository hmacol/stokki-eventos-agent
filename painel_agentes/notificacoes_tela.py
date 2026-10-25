# -*- coding: utf-8 -*-
"""
painel_agentes/notificacoes_tela.py

Dados da tela /notificacoes (24/10): lista de tipos, painel de um tipo e
contagem de quem receberia. Quem decide e grava e controle_notificacoes;
a preferencia do cliente vem de preferencias_notificacao. Sem Flask.
"""
import sqlite3
from pathlib import Path

import controle_notificacoes as cn
import preferencias_notificacao as pn
from regras.transportadoras import CatalogoTransportadoras

# Coluna de preferencias_notificacao de cada tipo da tela (a do portal): o
# mesmo nome, menos o chamado sem resposta, que la se chama diferente.
_COLUNA_PREF = {t: t for t in cn.TIPOS if t in pn.TIPOS}
_COLUNA_PREF["whatsapp_sem_resposta"] = "chamado_sem_resposta"

_ROTULO_ALVO = {"_geral": "Geral", "_piloto": "Destino do piloto"}


def _db_path(conn: sqlite3.Connection) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def conectar_preferencias() -> sqlite3.Connection:
    """Mesmo arquivo do controle, pra ler/gravar a lista de e-mails do portal."""
    return sqlite3.connect(cn.DB_PATH, timeout=10)


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _resumo_excecoes(excecoes: dict) -> str:
    bloqueados = sum(1 for r in excecoes.values() if r == "BLOQUEADO")
    liberados = sum(1 for r in excecoes.values() if r == "LIBERADO")
    partes = []
    if bloqueados:
        partes.append(_plural(bloqueados, "bloqueado", "bloqueados"))
    if liberados:
        partes.append(_plural(liberados, "liberado", "liberados"))
    return " · ".join(partes) or "—"


def _ultimo_envio(conn: sqlite3.Connection, tipo: str) -> str:
    try:
        row = conn.execute("SELECT MAX(enviado_em) FROM notificacoes_enviadas WHERE tipo = ?", (tipo,)).fetchone()
    except sqlite3.OperationalError:    # a tabela so nasce quando a primeira rotina envia
        return "—"
    return row[0] if row and row[0] else "—"


def montar_lista(conn: sqlite3.Connection) -> list[dict]:
    """Uma linha por tipo, na ordem de cn.TIPOS. estado "config" = sem linha
    na tela (vale o config de hoje)."""
    linhas = []
    for tipo, info in cn.TIPOS.items():
        linhas.append({
            "tipo": tipo,
            "rotulo": info["rotulo"],
            "estado": cn.estado_tipo(conn, tipo) or "config",
            "resumo_excecoes": _resumo_excecoes(cn.excecoes(conn, tipo)),
            "quando": info["quando"],
            "ultimo_envio": _ultimo_envio(conn, tipo),
        })
    return linhas


def _embarcadores(conn: sqlite3.Connection, tipo: str) -> list[dict]:
    """Embarcadores do cadastro (um por CNPJ) com a preferencia do cliente
    para o tipo (None quando o tipo nao tem escolha no portal)."""
    coluna = _COLUNA_PREF.get(tipo)
    # carregar_embarcadores so conhece os tipos do portal; pros outros qualquer
    # um serve (so o nome, os e-mails e o CNPJ interessam).
    todos = pn.carregar_embarcadores(coluna or "nfs_em_rota", db_path=_db_path(conn))
    vistos = {}
    for emb in todos.values():
        cnpj = emb["cnpj"]
        if cnpj and cnpj not in vistos:
            vistos[cnpj] = {"chave": cnpj, "nome": emb["nome"] or cnpj, "emails": emb["emails"],
                            "preferencia_cliente": (not emb["desligado"]) if coluna else None}
    return sorted(vistos.values(), key=lambda e: e["nome"].lower())


def _pontos(conn: sqlite3.Connection) -> list:
    try:
        return CatalogoTransportadoras.carregar_do_banco(_db_path(conn)).pontos_redespacho()
    except FileNotFoundError:   # sem cadastro ativo (ou sem a tabela): nada a listar
        return []


def detalhe_tipo(conn: sqlite3.Connection, tipo: str) -> dict:
    """Painel lateral de um tipo: estado e um destino por embarcador (ou ponto
    de transportadora). nao_enviar so existe pra transportadoras e e somente
    leitura aqui (quem edita e o /transportadoras)."""
    info = cn.TIPOS[tipo]
    estado = cn.estado_tipo(conn, tipo)
    excecoes = cn.excecoes(conn, tipo)
    if info["destino"] == "transportadora":
        destinos = [{"chave": p.nome_normalizado.strip().lower(), "nome": p.nome, "excecao": None,
                     "preferencia_cliente": None, "nao_enviar": bool(p.nao_enviar)} for p in _pontos(conn)]
    else:
        destinos = [{"chave": e["chave"], "nome": e["nome"], "excecao": None,
                     "preferencia_cliente": e["preferencia_cliente"]} for e in _embarcadores(conn, tipo)]
    conhecidos = set()
    for d in destinos:
        d["excecao"] = excecoes.get(d["chave"])
        conhecidos.add(d["chave"])
    for chave, regra in excecoes.items():   # excecao de destino que saiu do cadastro continua visivel
        if chave not in conhecidos:
            extra = {"chave": chave, "nome": f"{chave} (fora do cadastro)", "excecao": regra,
                     "preferencia_cliente": None}
            if info["destino"] == "transportadora":
                extra["nao_enviar"] = False
            destinos.append(extra)
    return {"tipo": tipo, "rotulo": info["rotulo"], "destino": info["destino"], "canais": list(info["canais"]),
            "quando": info["quando"], "estado": estado or "config", "origem": "tela" if estado else "config",
            "destinos": destinos}


def contar_destinatarios(conn: sqlite3.Connection, tipo: str) -> int:
    """Quantos destinatarios receberiam de verdade se o tipo fosse LIGADO:
    embarcadores com contato no canal e preferencia ligada, menos os
    BLOQUEADOS; transportadoras = pontos sem NAO ENVIAR."""
    info = cn.TIPOS[tipo]
    if info["destino"] == "transportadora":
        return sum(1 for p in _pontos(conn) if not p.nao_enviar)
    bloqueados = {d for d, r in cn.excecoes(conn, tipo).items() if r == "BLOQUEADO"}
    so_whatsapp = tuple(info["canais"]) == ("whatsapp",)
    total = 0
    for emb in _embarcadores(conn, tipo):
        if emb["chave"] in bloqueados or emb["preferencia_cliente"] is False:
            continue
        if so_whatsapp:
            # numero valido e preferencia ligada (a regra do proprio aviso)
            if not pn.whatsapp_do_embarcador(conn, emb["chave"]):
                continue
        elif not emb["emails"]:
            continue
        total += 1
    return total


def historico_legivel(conn: sqlite3.Connection, limite: int = 50) -> list[dict]:
    """cn.historico com rotulo do alvo e nome do destino pra tela."""
    nomes = {e["chave"]: e["nome"] for e in _embarcadores(conn, "nfs_em_rota")}
    saida = []
    for h in cn.historico(conn, limite):
        h["alvo_rotulo"] = _ROTULO_ALVO.get(h["alvo"]) or cn.TIPOS.get(h["alvo"], {}).get("rotulo", h["alvo"])
        h["destino_nome"] = nomes.get(h["destino"]) if h["destino"] else None
        saida.append(h)
    return saida
