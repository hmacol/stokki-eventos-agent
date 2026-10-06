# -*- coding: utf-8 -*-
"""
vigia_documentos.py

Vigia diário do fluxo de documentos (pedido do Hugo, 05/10/2026:
"garantir que esse processo está perfeito"). Roda 1x por dia às 06:43
(stokki-vigia-documentos.timer), depois do romaneio das 04h e antes da
saída dos motoristas.

Só LÊ: banco (rotas do dia, documentos_processados) e o log do
processar_documentos. Não toca Stokki, Vuupt nem GCS. A régua de
"o que falta" é a mesma da capa do romaneio
(gerar_pdf_romaneios.avaliar_documentos_pedido).

Resumo vai por notificar_execucao: e-mail todo dia; WhatsApp ALERTAS
FRESH só quando alguma checagem fica "erro".

COMO USAR:
    py -3.11 documentos_pedido/vigia_documentos.py --sem-notificar
    py -3.11 documentos_pedido/vigia_documentos.py --data 2026-10-06 --modo-teste
"""
import argparse
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))
sys.path.append(str(Path(__file__).parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = _RAIZ / "dados" / "dados.db"
LOG_DOCUMENTOS = Path(__file__).parent / "dados" / "processar_documentos.log"
JANELA_HORAS = 24
MAX_CODIGOS_POR_EMBARCADOR = 12

_RE_FIM_RODADA = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Processamento de documentos finalizado.*Contadores: (\{.*\})")
_RE_ERRO_GRAVE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*(Erro no processamento de documentos|Erro ao buscar PDFs)")
_RE_CONTADOR_ERRO = re.compile(r"'ERRO':\s*(\d+)")


def _quando(texto: str) -> datetime:
    return datetime.strptime(texto, "%Y-%m-%d %H:%M:%S")


def checar_rodadas(linhas_log: list[str], agora: datetime) -> dict:
    """Rodadas reais das últimas 24h: pelo menos uma, nenhuma com ERRO,
    nenhuma falha geral/IMAP. Rodada [MODO TESTE] não conta."""
    limite = agora - timedelta(hours=JANELA_HORAS)
    reais, com_erro, graves = 0, 0, []
    for linha in linhas_log:
        m = _RE_FIM_RODADA.match(linha)
        if m and _quando(m.group(1)) >= limite and "[MODO TESTE]" not in linha:
            reais += 1
            m_erro = _RE_CONTADOR_ERRO.search(m.group(2))
            if m_erro and int(m_erro.group(1)) > 0:
                com_erro += 1
            continue
        g = _RE_ERRO_GRAVE.match(linha)
        if g and _quando(g.group(1)) >= limite:
            graves.append(f"{g.group(1)[11:16]} {g.group(2)}")
    problemas = []
    if not reais:
        problemas.append(f"nenhuma rodada terminou nas últimas {JANELA_HORAS}h")
    if com_erro:
        problemas.append(f"{com_erro} rodada(s) com documento em ERRO")
    problemas.extend(graves[:5])
    if problemas:
        return {"status": "erro", "detalhe": "; ".join(problemas)}
    return {"status": "ok", "detalhe": f"{reais} rodada(s) sem erro nas últimas {JANELA_HORAS}h"}


def servicos_do_dia(con: sqlite3.Connection, data_iso: str) -> list[dict]:
    """Entregas das rotas do dia (espelho do núcleo), sem rota
    cancelada, sem serviço excluído, sem coleta."""
    rows = con.execute("""
        SELECT p.codigo, p.sender_id, p.remetente_nome
        FROM nucleo_rotas r JOIN nucleo_pedidos p ON p.vuupt_route_id = r.vuupt_route_id
        WHERE r.data_rota = ? AND r.cancelada_em IS NULL AND COALESCE(r.status, '') != 'CANCELADA'
          AND p.excluido_em IS NULL AND p.tipo = 'delivery'
        ORDER BY p.codigo
    """, (data_iso,)).fetchall()
    return [{"codigo": (c or "").lstrip("#"), "sender_id": s, "embarcador": (n or "?").split(" - ")[-1].strip()}
            for c, s, n in rows]


def _avaliar(servico: dict, docs_por_pedido: dict) -> dict:
    import gerar_pdf_romaneios as gpr
    return gpr.avaliar_documentos_pedido(servico["codigo"], servico["sender_id"], docs_por_pedido)


def _detalhe_por_embarcador(itens: list[tuple[str, str]]) -> str:
    por_emb: dict[str, list[str]] = defaultdict(list)
    for emb, codigo in itens:
        por_emb[emb].append(codigo)
    partes = []
    for emb, codigos in sorted(por_emb.items(), key=lambda kv: -len(kv[1])):
        extra = len(codigos) - MAX_CODIGOS_POR_EMBARCADOR
        lista = ", ".join(codigos[:MAX_CODIGOS_POR_EMBARCADOR]) + (f" (+{extra})" if extra > 0 else "")
        partes.append(f"{emb}: {lista}")
    return " | ".join(partes)


def checar_pendencias(servicos: list[dict], docs_por_pedido: dict) -> dict:
    grupos = {"Nota fiscal (rotas do dia)": [], "Boleto (rotas do dia)": [], "Arquivos (rotas do dia)": []}
    for s in servicos:
        for falta in _avaliar(s, docs_por_pedido)["faltas"]:
            if falta == "sem nota fiscal":
                chave = "Nota fiscal (rotas do dia)"
            elif falta == "sem boleto":
                chave = "Boleto (rotas do dia)"
            else:
                chave = "Arquivos (rotas do dia)"
            grupos[chave].append((s["embarcador"], s["codigo"]))
    resultado = {}
    for nome, itens in grupos.items():
        if itens:
            resultado[nome] = {"status": "erro",
                               "detalhe": f"{len(itens)} pedido(s) -- {_detalhe_por_embarcador(itens)}"}
        else:
            resultado[nome] = {"status": "ok", "detalhe": f"nada faltando em {len(servicos)} entrega(s)"}
    return resultado


def checar_revisoes(con: sqlite3.Connection, agora: datetime) -> dict:
    desde = (agora - timedelta(hours=JANELA_HORAS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute("SELECT motivo FROM documentos_processados "
                       "WHERE status = 'REVISAO_MANUAL' AND processado_em >= ?", (desde,)).fetchall()
    if not rows:
        return {"status": "ok", "detalhe": "nenhuma revisão manual nova"}
    motivos = Counter((m or "?")[:60] for (m,) in rows)
    topo = "; ".join(f"{n}x {m}" for m, n in motivos.most_common(4))
    return {"status": "ok", "detalhe": f"{len(rows)} nova(s) -- {topo}"}


def main(data_iso: str | None = None, modo_teste: bool = False, notificar: bool = True) -> dict:
    inicio = time.time()
    agora = datetime.now()
    data_iso = data_iso or date.today().isoformat()
    resumo: dict = {}

    try:
        linhas = LOG_DOCUMENTOS.read_text(encoding="utf-8", errors="ignore").splitlines()[-20000:]
    except OSError as e:
        linhas = []
        resumo["Log do processar_documentos"] = {"status": "erro", "detalhe": f"não deu pra ler: {e}"}
    resumo["Rodadas do fluxo"] = checar_rodadas(linhas, agora)

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        servicos = servicos_do_dia(con, data_iso)
        resumo["Rotas do dia"] = {"status": "ok", "detalhe": f"{len(servicos)} entrega(s) em {data_iso}"}
        import gerar_pdf_romaneios as gpr
        codigos = {c for s in servicos for c in gpr._codigos_base_lista(s["codigo"])}
        docs_por_pedido, _ = gpr.carregar_documentos_por_pedido(codigos)
        resumo.update(checar_pendencias(servicos, docs_por_pedido))
        resumo["Revisão manual (24h)"] = checar_revisoes(con, agora)
    finally:
        con.close()

    duracao = time.time() - inicio
    for nome, info in resumo.items():
        print(f"[{info['status'].upper():4}] {nome}: {info['detalhe']}")
    if notificar:
        import yaml
        from notificar_execucao_agente import notificar_execucao
        with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        notificar_execucao(resumo, duracao, modo_teste, config, titulo="Vigia de documentos")
    return resumo


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vigia diário do fluxo de documentos (só leitura)")
    parser.add_argument("--data", default=None, help="Data das rotas (AAAA-MM-DD). Padrão: hoje.")
    parser.add_argument("--modo-teste", action="store_true", help="Assunto do e-mail com [MODO TESTE]")
    parser.add_argument("--sem-notificar", action="store_true", help="Só imprime, não manda e-mail/WhatsApp")
    a = parser.parse_args()
    main(a.data, modo_teste=a.modo_teste, notificar=not a.sem_notificar)
