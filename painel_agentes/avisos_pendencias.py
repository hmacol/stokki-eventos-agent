# -*- coding: utf-8 -*-
"""
painel_agentes/avisos_pendencias.py

Catalogo das pendencias que viram faixa no topo das telas do painel
(spec 2026-10-24-aviso-pendencias-painel) e as contagens delas. Modulo
puro: nao conhece Flask nem sessao -- quem chama (API, Task 3) entrega o
nivel, os contadores do menu ja calculados e as permissoes.

REGRA: o aviso nunca pode deixar nada lento nem derrubar a resposta.
Contagem barata e COUNT em SQLite local; a que pode demorar passa por
contadores_menu._caro (ultimo valor conhecido + renovacao em segundo
plano). Fonte que falha ou ainda sem valor: o aviso dela some, os outros
continuam.
"""
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

import contadores_menu  # noqa: E402

logger = logging.getLogger("painel.avisos_pendencias")

IGNORAR_MINUTOS = 30


def _item(chave, texto, niveis, destino, endpoint_destino, vermelho=False):
    return {"chave": chave, "texto": texto, "niveis": tuple(niveis), "destino": destino,
            "endpoint_destino": endpoint_destino, "vermelho": vermelho}


# Ordem FIXA (vermelhas -- alguem de fora esperando -- primeiro, depois a
# tabela da spec 5). `endpoint_destino` e o nome real da rota Flask e
# tambem a chave da tela em acesso_telas.TELAS (login personalizado).
CATALOGO = [
    _item("chamados", "{n} chamados esperando a Fresh Log",
          ("total", "operador", "logistica", "atendimento"), "/atendimento", "atendimento", True),
    _item("solicitacoes_portal", "{n} pedidos do portal com cancelamento, reagendamento ou reenvio pendente",
          ("total", "operador"), "/solicitacoes-portal", "solicitacoes_portal", True),
    _item("retornos", "{n} retornos esperados não recebidos",
          ("total", "galpao", "expedicao", "operador", "logistica"), "/retornos", "retornos_tela", True),
    _item("cadastros_embarcador", "{n} cadastros de embarcador para aprovar",
          ("total",), "/inicio", "inicio", True),
    _item("motoristas", "{n} motoristas novos para aprovar",
          ("total",), "/motoristas", "motoristas", True),
    _item("pedagios", "{n} pedágios para aprovar",
          ("total", "operador", "logistica"), "/financeiro/pedagios", "pedagios"),
    _item("canhotos", "{n} canhotos reprovados pela IA para revisar",
          ("total", "operador", "logistica"), "/canhotos", "canhotos"),
    _item("canhotos_pendentes", "{n} entregas sem canhoto",
          ("total", "operador", "logistica"), "/canhotos-pendentes", "canhotos_pendentes"),
    _item("transportadoras", "{n} transportadoras desconhecidas",
          ("total", "operador", "logistica"), "/transportadoras", "transportadoras"),
    _item("clientes_agenda", "{n} clientes AGENDA aguardando decisão",
          ("total", "logistica"), "/clientes-agenda", "clientes_agenda"),
    _item("pedidos_parados", "{n} pedidos parados sem classificação",
          ("total", "operador", "logistica", "atendimento"), "/pedidos-parados", "pedidos_parados"),
    _item("torre", "{n} ocorrências na fila da Torre",
          ("total", "operador", "logistica", "atendimento"), "/torre", "torre"),
    _item("rascunhos_erro", "{n} rascunhos de rota com erro de envio",
          ("total", "operador", "logistica"), "/planejamento", "planejamento"),
    _item("batimento", "{n} divergências do batimento sem tratar",
          ("total", "operador"), "/vigia?aba=fechamento", "vigia_pedidos"),
    _item("vigia_vencidos", "{n} pedidos com prazo vencido",
          ("total", "operador"), "/vigia", "vigia_pedidos"),
    _item("rotinas_falhas", "{n} rotinas falharam nas últimas 24 h",
          ("total",), "/historico", "historico"),
    _item("wms_pendencias", "{n} itens do WMS com pendência ou falta",
          ("total", "galpao", "operador"), "/wms/estoque", "wms_estoque"),
    _item("wms_divergencias", "{n} recebimentos com divergência sem aviso",
          ("total", "galpao"), "/wms", "wms"),
]

_SINGULAR = {
    "chamados": "{n} chamado esperando a Fresh Log",
    "solicitacoes_portal": "{n} pedido do portal com cancelamento, reagendamento ou reenvio pendente",
    "retornos": "{n} retorno esperado não recebido",
    "cadastros_embarcador": "{n} cadastro de embarcador para aprovar",
    "motoristas": "{n} motorista novo para aprovar",
    "pedagios": "{n} pedágio para aprovar",
    "canhotos": "{n} canhoto reprovado pela IA para revisar",
    "canhotos_pendentes": "{n} entrega sem canhoto",
    "transportadoras": "{n} transportadora desconhecida",
    "clientes_agenda": "{n} cliente AGENDA aguardando decisão",
    "pedidos_parados": "{n} pedido parado sem classificação",
    "torre": "{n} ocorrência na fila da Torre",
    "rascunhos_erro": "{n} rascunho de rota com erro de envio",
    "batimento": "{n} divergência do batimento sem tratar",
    "vigia_vencidos": "{n} pedido com prazo vencido",
    "rotinas_falhas": "{n} rotina falhou nas últimas 24 h",
    "wms_pendencias": "{n} item do WMS com pendência ou falta",
    "wms_divergencias": "{n} recebimento com divergência sem aviso",
}

# Chave do aviso -> chave em contadores_menu.contadores(...) (ja calculado
# pelo chamador; aqui nada disso e recalculado).
_DO_MENU = {"chamados": "atendimento", "pedagios": "pedagios", "canhotos": "canhotos",
            "transportadoras": "transportadoras", "motoristas": "motoristas",
            "pedidos_parados": "pedidos_parados", "torre": "torre"}


# ── Contagens (so as que NAO estao em contadores_menu) ────────────────────────

def _conectar():
    from nucleo import banco as nucleo_banco
    return nucleo_banco.conectar()


def _contar_sql(sql: str) -> int:
    conn = _conectar()
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def _contar_solicitacoes_portal() -> int:
    return _contar_sql("SELECT COUNT(*) FROM portal_solicitacoes WHERE status = 'PENDENTE'")


def _contar_clientes_agenda() -> int:
    return _contar_sql("SELECT COUNT(*) FROM clientes_agenda_marcados WHERE situacao = 'PENDENTE'")


def _contar_rascunhos_erro() -> int:
    return _contar_sql("SELECT COUNT(*) FROM rascunhos_rota WHERE status = 'ERRO_ENVIO'")


def _contar_cadastros_embarcador() -> int:
    return _contar_sql("SELECT COUNT(*) FROM cadastro_embarcador WHERE situacao = 'PREENCHIDO'")


def _calc_canhotos_pendentes() -> dict:
    from nucleo import canhotos_manuais
    desde = (date.today() - timedelta(days=60)).isoformat()   # mesma janela da tela
    conn = _conectar()
    try:
        return {"qtd": int(canhotos_manuais.pendentes(conn, desde, por_pagina=1)["total"])}
    finally:
        conn.close()


def _calc_retornos() -> dict:
    import retornos
    try:
        import yaml
        with open(_RAIZ / ("config" + ".yaml"), encoding="utf-8") as f:
            excluir = retornos.agent_ids_lalamove(yaml.safe_load(f) or {})
    except Exception:
        excluir = frozenset()
    conn = retornos.conectar()
    try:
        grupos = retornos.listar_pendentes(conn, date.today(), agent_ids_excluir=excluir)
    finally:
        conn.close()
    return {"qtd": sum(len(g["pedidos"]) for g in grupos)}


def _calc_batimento() -> dict:
    from batimento import consulta
    return {"qtd": int(consulta.fechamento()["total_abertas"])}


def _calc_vigia_vencidos() -> dict:
    from vigia import consulta
    return {"qtd": int(consulta.listar()["vencidos"])}


def _contar_rotinas_falhas(execucoes: list[dict], agora: datetime | None = None) -> int:
    """Mesma regra de pagina_inicial.montar_rotinas, sem o teto de exibicao:
    status fora de SUCESSO/RODANDO/NA_FILA nas ultimas 24 h."""
    import pagina_inicial as pi
    agora = agora or datetime.now()
    limite = agora - pi.JANELA_FALHAS
    n = 0
    for ex in execucoes:
        if ex["status"] in ("SUCESSO", "RODANDO", "NA_FILA"):
            continue
        try:
            quando = datetime.strptime(ex.get("finalizado_em") or ex["iniciado_em"], pi._FORMATO_SQLITE)
        except (TypeError, ValueError):
            continue
        if quando >= limite:
            n += 1
    return n


def _calc_rotinas_falhas() -> dict:
    import executor
    return {"qtd": _contar_rotinas_falhas(executor.listar_execucoes_recentes(300))}


def _calc_wms_pendencias() -> dict:
    import wms_pedidos
    conn = _conectar()
    try:
        return {"qtd": len(wms_pedidos.pendencias(conn, limite=100000))}
    finally:
        conn.close()


def _contar_wms_divergencias() -> int:
    import wms_faltas_recebimento
    conn = _conectar()
    try:
        return len(wms_faltas_recebimento.pendentes_de_envio(conn))
    finally:
        conn.close()


def _via_cache(chave: str, calcular):
    """Fonte que pode demorar: ultimo valor conhecido, nunca bloqueia
    (contadores_menu._caro). None = ainda sem valor."""
    valor = contadores_menu._caro("aviso_" + chave, calcular)
    return None if valor is None else int(valor["qtd"])


_FONTES = {
    "solicitacoes_portal": _contar_solicitacoes_portal,
    "retornos": lambda: _via_cache("retornos", _calc_retornos),
    "cadastros_embarcador": _contar_cadastros_embarcador,
    "canhotos_pendentes": lambda: _via_cache("canhotos_pendentes", _calc_canhotos_pendentes),
    "clientes_agenda": _contar_clientes_agenda,
    "rascunhos_erro": _contar_rascunhos_erro,
    "batimento": lambda: _via_cache("batimento", _calc_batimento),
    "vigia_vencidos": lambda: _via_cache("vigia_vencidos", _calc_vigia_vencidos),
    "rotinas_falhas": lambda: _via_cache("rotinas_falhas", _calc_rotinas_falhas),
    "wms_pendencias": lambda: _via_cache("wms_pendencias", _calc_wms_pendencias),
    "wms_divergencias": _contar_wms_divergencias,
}


def contar(chave: str) -> int | None:
    """Quantidade da pendencia `chave` (so as que nao vem do contadores_menu).
    KeyError se a chave nao e daqui; None se a fonte cara ainda nao tem valor."""
    return _FONTES[chave]()


# ── Montagem ──────────────────────────────────────────────────────────────────

def _texto(chave: str, texto_plural: str, n: int) -> str:
    return (_SINGULAR[chave] if n == 1 else texto_plural).format(n=n)


def montar(nivel: str, contadores: dict, telas_liberadas: set | None,
           pode_aprovar_embarcador: bool) -> list[dict]:
    """Avisos desse perfil, na ordem do catalogo, sem os zerados.
    `contadores` e o dict de contadores_menu.contadores(...). Fonte com
    erro ou sem valor: aquele aviso nao vem, os outros sim."""
    if nivel == "leitura":
        return []
    avisos = []
    for item in CATALOGO:
        chave = item["chave"]
        if chave == "cadastros_embarcador":
            # total sempre; os demais (exceto leitura) so com a permissao aprovar_embarcadores
            permitido = nivel == "total" or pode_aprovar_embarcador
        else:
            permitido = nivel in item["niveis"]
        if not permitido:
            continue
        if telas_liberadas is not None and item["endpoint_destino"] not in telas_liberadas:
            continue
        try:
            if chave in _DO_MENU:
                bruto = (contadores or {}).get(_DO_MENU[chave])
                qtd = None if bruto is None else int(bruto["qtd"])
            else:
                qtd = contar(chave)
        except Exception as e:
            logger.warning(f"[avisos] Falha em '{chave}': {e}")
            continue
        if not qtd:
            continue
        avisos.append({"chave": chave, "texto": _texto(chave, item["texto"], qtd), "qtd": qtd,
                       "destino": item["destino"], "vermelho": item["vermelho"]})
    return avisos


# ── Ignorar por pessoa ────────────────────────────────────────────────────────

DB_PATH = _RAIZ / "dados" / "dados.db"
_FMT = "%Y-%m-%d %H:%M:%S"


def conectar(caminho=None) -> sqlite3.Connection:
    conn = sqlite3.connect(caminho or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS painel_avisos_ignorados (
            pessoa TEXT NOT NULL,
            chave  TEXT NOT NULL,
            ate    TEXT NOT NULL,
            qtd    INTEGER NOT NULL,
            PRIMARY KEY (pessoa, chave)
        )""")
    conn.commit()
    return conn


def ignorar(conn, pessoa: str, chave: str, qtd: int, agora: datetime) -> None:
    """Esconde o aviso `chave` pra `pessoa` por IGNORAR_MINUTOS (renova se ja existe)."""
    ate = (agora + timedelta(minutes=IGNORAR_MINUTOS)).strftime(_FMT)
    conn.execute(
        "INSERT INTO painel_avisos_ignorados (pessoa, chave, ate, qtd) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(pessoa, chave) DO UPDATE SET ate = excluded.ate, qtd = excluded.qtd",
        (pessoa, chave, ate, int(qtd)))
    conn.commit()


def filtrar_ignorados(conn, pessoa: str, avisos: list[dict], agora: datetime) -> list[dict]:
    """Tira os avisos ignorados ainda validos. Vermelho volta na hora se a
    quantidade atual passou da guardada; laranja espera o `ate`."""
    linhas = conn.execute("SELECT chave, ate, qtd FROM painel_avisos_ignorados WHERE pessoa = ?",
                          (pessoa,)).fetchall()
    ignorados = {r["chave"]: (r["ate"], r["qtd"]) for r in linhas}
    agora_s = agora.strftime(_FMT)
    saida = []
    for a in avisos:
        reg = ignorados.get(a["chave"])
        if reg and reg[0] > agora_s and not (a["vermelho"] and a["qtd"] > reg[1]):
            continue
        saida.append(a)
    return saida
