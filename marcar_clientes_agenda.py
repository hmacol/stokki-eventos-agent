# -*- coding: utf-8 -*-
"""
marcar_clientes_agenda.py

Pedido do Hugo, 29/09: todo destinatário que já teve pedido com data de
agendamento INFORMADA (portal, resposta de e-mail, planilha NUU -- ou
seja, que não veio do nosso dia fixo por região) é candidato a AGENDA
na BD_CLIENTES. A partir daí o resto do sistema já trata: pedido dele
sem agendamento gera o aviso urgente ao remetente
(roteirizacao/notificar_agendamento_pendente.py).

30/09 (Hugo): data informada nem sempre é agendamento de verdade, então
a rotina NÃO grava mais sozinha. Ela deixa o destinatário PENDENTE na
tabela clientes_agenda_marcados, avisa no WhatsApp (grupo interno) com
o link da tela do painel (/clientes-agenda), e o Hugo decide lá:
Autorizar grava AGENDA na planilha (regras/clientes_agendamento.
marcar_agendamento, com backup); Não marcar tira o cliente da fila pra
sempre. Quem já está como AGENDA na planilha entra direto como
AUTORIZADO (se o Hugo apagar depois, a rotina respeita).

Fonte: agendamentos_pedido com status RESPONDIDO e data preenchida. Data
de dia fixo nunca entra nessa tabela, então tudo que está lá foi
informado por alguém.

Roda nas sequências das 18h e das 22h, depois do
atualizar_agendamentos_confirmados.py. Só manda aviso quando entrou
pendente novo ou deu erro.

COMO USAR:
    py -3.11 marcar_clientes_agenda.py
    py -3.11 marcar_clientes_agenda.py --modo-teste
    py -3.11 marcar_clientes_agenda.py --nao-marcar 48178686000131 51322677000187
"""
import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger("marcar_clientes_agenda")

import yaml

from regras.clientes_agendamento import (
    carregar_clientes_agendamento, marcar_agendamento, tem_agendamento,
)

DB_PATH = _RAIZ / "dados" / "dados.db"
TITULO = "Clientes com agendamento"
URL_PAINEL_PADRAO = "https://app.freshhub.com.br/painel"

PENDENTE, AUTORIZADO, NAO_MARCAR = "PENDENTE", "AUTORIZADO", "NAO_MARCAR"
ROTULOS = {PENDENTE: "Aguardando decisão", AUTORIZADO: "Autorizado", NAO_MARCAR: "Não marcar"}
ORIGENS = {"PLANILHA_NUU": "planilha NUU"}
ORIGEM_PADRAO = "portal ou e-mail"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _so_digitos(s) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def formatar_documento(doc: str) -> str:
    d = _so_digitos(doc)
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d


def link_tela(config: dict) -> str:
    base = ((config or {}).get("painel_agentes") or {}).get("url_base") or URL_PAINEL_PADRAO
    return base.rstrip("/") + "/clientes-agenda"


# --- Banco --------------------------------------------------------------------

def _conectar() -> sqlite3.Connection:
    """Abre o banco garantindo a tabela. A tabela nasceu em 29/09 só com
    documento/nome (a rotina gravava direto); as colunas de situação
    vieram em 30/09, e as linhas antigas ganham situação aqui:
    "(não marcar)" era a exclusão do Hugo, o resto já estava marcado."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS clientes_agenda_marcados ("
        " documento TEXT PRIMARY KEY,"
        " nome TEXT,"
        " tratado_em TEXT DEFAULT (datetime('now','localtime')))"
    )
    colunas = {r[1] for r in conn.execute("PRAGMA table_info(clientes_agenda_marcados)")}
    for coluna in ("situacao TEXT", "pedidos TEXT", "decidido_em TEXT"):
        if coluna.split()[0] not in colunas:
            conn.execute(f"ALTER TABLE clientes_agenda_marcados ADD COLUMN {coluna}")
    conn.execute("UPDATE clientes_agenda_marcados SET situacao = ? WHERE situacao IS NULL AND nome = '(não marcar)'",
                 (NAO_MARCAR,))
    conn.execute("UPDATE clientes_agenda_marcados SET situacao = ? WHERE situacao IS NULL", (AUTORIZADO,))
    conn.commit()
    return conn


def buscar_candidatos() -> dict[str, dict]:
    """{documento: {"nome", "pedidos": [{codigo, data, origem, embarcador}]}}
    dos destinatários com data de agendamento informada que ainda não
    estão na tabela (em nenhuma situação)."""
    conn = _conectar()
    try:
        ja_tratados = {r[0] for r in conn.execute("SELECT documento FROM clientes_agenda_marcados")}
        rows = conn.execute(
            "SELECT pedido, cnpj_destinatario, nome_destinatario, cnpj_embarcador, data_agendada, origem "
            "FROM agendamentos_pedido "
            "WHERE status = 'RESPONDIDO' AND data_agendada IS NOT NULL AND data_agendada <> '' "
            "ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()

    candidatos: dict[str, dict] = {}
    for r in rows:
        doc = _so_digitos(r["cnpj_destinatario"])
        if not doc or doc in ja_tratados:
            continue
        item = candidatos.setdefault(doc, {"nome": "", "pedidos": []})
        item["nome"] = item["nome"] or (r["nome_destinatario"] or "").strip()
        item["pedidos"].append({
            "codigo": (r["pedido"] or "").strip(),
            "data": (r["data_agendada"] or "").strip(),
            "origem": ORIGENS.get(r["origem"] or "", ORIGEM_PADRAO),
            "embarcador": _so_digitos(r["cnpj_embarcador"]),
        })
    return candidatos


def _registrar(candidatos: dict[str, dict], situacao: str) -> None:
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    decidido_em = agora if situacao != PENDENTE else None
    conn = _conectar()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO clientes_agenda_marcados "
            "(documento, nome, tratado_em, situacao, pedidos, decidido_em) VALUES (?, ?, ?, ?, ?, ?)",
            [(doc, c.get("nome", ""), agora, situacao, json.dumps(c.get("pedidos", []), ensure_ascii=False),
              decidido_em) for doc, c in candidatos.items()],
        )
        conn.commit()
    finally:
        conn.close()


def registrar_nao_marcar(documentos: list[str]) -> list[str]:
    """Tira destinatários da rotina pela linha de comando (decisão do
    Hugo): nunca serão marcados. Não mexe na planilha."""
    docs = [d for d in (_so_digitos(x) for x in documentos) if d]
    _registrar({d: {"nome": "(não marcar)", "pedidos": []} for d in docs}, NAO_MARCAR)
    return docs


def listar(situacao: str | None = None) -> list[dict]:
    """Linhas da tabela pra tela do painel (pendentes primeiro, mais
    recentes primeiro), com os pedidos que motivaram cada uma."""
    conn = _conectar()
    try:
        sql = "SELECT * FROM clientes_agenda_marcados"
        params: tuple = ()
        if situacao:
            sql += " WHERE situacao = ?"
            params = (situacao,)
        sql += " ORDER BY CASE situacao WHEN 'PENDENTE' THEN 0 ELSE 1 END, tratado_em DESC, documento"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    saida = []
    for r in rows:
        try:
            pedidos = json.loads(r["pedidos"] or "[]")
        except ValueError:
            pedidos = []
        saida.append({
            "documento": r["documento"], "documento_formatado": formatar_documento(r["documento"]),
            "nome": r["nome"] or "", "situacao": r["situacao"], "rotulo": ROTULOS.get(r["situacao"], r["situacao"]),
            "pedidos": pedidos, "tratado_em": r["tratado_em"], "decidido_em": r["decidido_em"],
        })
    return saida


def decidir(documento: str, autorizar: bool, caminho_planilha) -> dict:
    """Decisão do Hugo na tela. Autorizar grava AGENDA na planilha na hora
    (com backup); Não marcar só muda a situação. Só vale pra PENDENTE."""
    doc = _so_digitos(documento)
    conn = _conectar()
    try:
        row = conn.execute("SELECT nome, situacao FROM clientes_agenda_marcados WHERE documento = ?", (doc,)).fetchone()
    finally:
        conn.close()
    if not row or row["situacao"] != PENDENTE:
        raise ValueError(f"{formatar_documento(doc)} não está aguardando decisão.")

    resultado = {"documento": doc, "nome": row["nome"] or "", "backup": None}
    if autorizar:
        gravacao = marcar_agendamento(caminho_planilha, {doc: row["nome"] or ""})
        resultado["backup"] = gravacao["backup"]
        resultado["incluido"] = doc in gravacao["incluidos"]
        situacao = AUTORIZADO
    else:
        situacao = NAO_MARCAR

    conn = _conectar()
    try:
        conn.execute("UPDATE clientes_agenda_marcados SET situacao = ?, decidido_em = datetime('now','localtime') "
                     "WHERE documento = ?", (situacao, doc))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"{row['nome']} ({doc}): {ROTULOS[situacao]}" + (f" -- backup {resultado['backup']}" if resultado["backup"] else ""))
    resultado["situacao"] = situacao
    return resultado


# --- Rotina -------------------------------------------------------------------

def processar(caminho_planilha, modo_teste: bool) -> dict:
    """Separa os candidatos novos: quem já é AGENDA na planilha entra como
    AUTORIZADO; o resto fica PENDENTE esperando o Hugo. Em modo teste só
    informa, sem registrar nada."""
    candidatos = buscar_candidatos()
    conjunto = carregar_clientes_agendamento(caminho_planilha)
    ja_marcados = {d: c for d, c in candidatos.items() if tem_agendamento(d, conjunto)}
    pendentes = {d: c for d, c in candidatos.items() if d not in ja_marcados}

    if not modo_teste:
        if ja_marcados:
            _registrar(ja_marcados, AUTORIZADO)
        if pendentes:
            _registrar(pendentes, PENDENTE)
    total_pendentes = len(listar(PENDENTE)) + (len(pendentes) if modo_teste else 0)
    return {
        "pendentes_novos": list(pendentes), "ja_marcados": list(ja_marcados),
        "nomes": {d: c["nome"] for d, c in candidatos.items()}, "total_pendentes": total_pendentes,
    }


def _descrever(documentos: list[str], nomes: dict[str, str]) -> str:
    return "\n".join(f"{nomes.get(d) or '(sem nome)'} ({formatar_documento(d)})" for d in documentos)


def main(modo_teste: bool = False):
    inicio = time.time()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_RAIZ / "dados" / "marcar_clientes_agenda.log", encoding="utf-8"),
        ],
    )
    prefixo = "[Teste] " if modo_teste else ""
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Clientes com agendamento: rodada iniciada.")

    config = _carregar_config()
    caminho = config.get("clientes_agendamento", {}).get("planilha", "")
    resumo_etapas = {}
    resultado = None

    try:
        if not caminho or not Path(caminho).exists():
            raise FileNotFoundError(f"Planilha de clientes não encontrada: '{caminho}' "
                                    f"(config.yaml, clientes_agendamento.planilha).")
        resultado = processar(caminho, modo_teste)
        for doc in resultado["ja_marcados"]:
            logger.info(f"  {prefixo}{resultado['nomes'].get(doc)} ({doc}): já era AGENDA na planilha.")
        for doc in resultado["pendentes_novos"]:
            logger.info(f"  {prefixo}{resultado['nomes'].get(doc)} ({doc}): aguardando autorização do Hugo.")
        if resultado["pendentes_novos"]:
            link = link_tela(config)
            resumo_etapas[TITULO] = {
                "status": "ok",
                "detalhe": f"{prefixo}{len(resultado['pendentes_novos'])} destinatário(s) com data de agendamento "
                           f"informada aguardam autorização pra virar AGENDA na BD_CLIENTES "
                           f"({resultado['total_pendentes']} no total). Decidir em {link}\n"
                           f"{_descrever(resultado['pendentes_novos'], resultado['nomes'])}",
            }
        else:
            logger.info("Nenhum destinatário novo.")
    except Exception as e:
        logger.exception(f"Erro na rodada de clientes com agendamento: {e}")
        resumo_etapas[TITULO] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"Clientes com agendamento: rodada finalizada em {duracao:.1f}s.")

    if resumo_etapas:
        try:
            from notificar_execucao_agente import notificar_execucao
            notificar_execucao(resumo_etapas, duracao, modo_teste, config)
        except Exception as e:
            logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")
    if resultado and resultado["pendentes_novos"]:
        try:
            import notificar_whatsapp
            notificar_whatsapp.avisar_clientes_agenda(
                len(resultado["pendentes_novos"]), resultado["total_pendentes"], link_tela(config), config,
                modo_teste=modo_teste)
        except Exception as e:
            logger.warning(f"Falha no aviso por WhatsApp (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Põe na fila de autorização do Hugo os destinatários com data "
                                                 "de agendamento informada (AGENDA na BD_CLIENTES)")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Lista quem entraria na fila, sem gravar no banco nem avisar")
    parser.add_argument("--nao-marcar", nargs="+", metavar="DOCUMENTO",
                        help="Só registra esses CNPJ/CPF como fora da rotina (nunca serão marcados) e sai")
    args = parser.parse_args()
    if args.nao_marcar:
        print("Fora da rotina:", ", ".join(registrar_nao_marcar(args.nao_marcar)))
    else:
        main(modo_teste=args.modo_teste)
