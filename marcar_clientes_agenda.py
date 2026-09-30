# -*- coding: utf-8 -*-
"""
marcar_clientes_agenda.py

Pedido do Hugo, 29/09: todo destinatário que já teve pedido com data de
agendamento INFORMADA (portal, resposta de e-mail, planilha NUU -- ou
seja, que não veio do nosso dia fixo por região) passa a ser marcado
como AGENDA na BD_CLIENTES. A partir daí o resto do sistema já trata:
pedido dele sem agendamento gera o aviso urgente ao remetente
(roteirizacao/notificar_agendamento_pendente.py).

Fonte: agendamentos_pedido com status RESPONDIDO e data preenchida. Data
de dia fixo nunca entra nessa tabela, então tudo que está lá foi
informado por alguém.

Cada documento é tratado UMA vez (tabela clientes_agenda_marcados): se o
Hugo apagar o AGENDA de um cliente na planilha, a rotina não marca de
novo.

Roda nas sequências das 18h e das 22h, depois do
atualizar_agendamentos_confirmados.py. Só manda e-mail de resumo quando
marcou alguém ou deu erro.

COMO USAR:
    py -3.11 marcar_clientes_agenda.py
    py -3.11 marcar_clientes_agenda.py --modo-teste
"""
import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger("marcar_clientes_agenda")

import yaml

from regras.clientes_agendamento import marcar_agendamento

DB_PATH = _RAIZ / "dados" / "dados.db"
TITULO = "Clientes com agendamento"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _so_digitos(s) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _garantir_tabela(conn: sqlite3.Connection):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS clientes_agenda_marcados ("
        " documento TEXT PRIMARY KEY,"
        " nome TEXT,"
        " tratado_em TEXT DEFAULT (datetime('now','localtime')))"
    )
    conn.commit()


def buscar_candidatos() -> dict[str, str]:
    """{documento: nome} dos destinatários com data de agendamento
    informada que a rotina ainda não tratou."""
    conn = sqlite3.connect(DB_PATH)
    try:
        _garantir_tabela(conn)
        ja_tratados = {r[0] for r in conn.execute("SELECT documento FROM clientes_agenda_marcados")}
        rows = conn.execute(
            "SELECT cnpj_destinatario, nome_destinatario FROM agendamentos_pedido "
            "WHERE status = 'RESPONDIDO' AND data_agendada IS NOT NULL AND data_agendada <> ''"
        ).fetchall()
    finally:
        conn.close()

    candidatos: dict[str, str] = {}
    for documento, nome in rows:
        doc = _so_digitos(documento)
        if doc and doc not in ja_tratados and not candidatos.get(doc):
            candidatos[doc] = (nome or "").strip()
    return candidatos


def _registrar_tratados(candidatos: dict[str, str]):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO clientes_agenda_marcados (documento, nome) VALUES (?, ?)",
            list(candidatos.items()),
        )
        conn.commit()
    finally:
        conn.close()


def processar(caminho_planilha, modo_teste: bool) -> dict:
    """Marca os candidatos na planilha. Retorna o resultado de
    marcar_agendamento mais "nomes" ({documento: nome})."""
    candidatos = buscar_candidatos()
    if not candidatos:
        return {"marcados": [], "incluidos": [], "backup": None, "nomes": {}}

    resultado = marcar_agendamento(caminho_planilha, candidatos, gravar=not modo_teste)
    if not modo_teste:
        # registra TODO candidato, inclusive quem já estava marcado à mão:
        # se o Hugo desmarcar depois, a rotina respeita
        _registrar_tratados(candidatos)
    return {**resultado, "nomes": candidatos}


def _descrever(documentos: list[str], nomes: dict[str, str]) -> str:
    return "\n".join(f"{nomes.get(d) or '(sem nome)'} ({d})" for d in documentos)


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
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Marcação de clientes com agendamento iniciada.")

    config = _carregar_config()
    caminho = config.get("clientes_agendamento", {}).get("planilha", "")
    resumo_etapas = {}

    try:
        if not caminho or not Path(caminho).exists():
            raise FileNotFoundError(f"Planilha de clientes não encontrada: '{caminho}' "
                                    f"(config.yaml, clientes_agendamento.planilha).")
        resultado = processar(caminho, modo_teste)
        novos = resultado["marcados"] + resultado["incluidos"]
        for doc in resultado["marcados"]:
            logger.info(f"  {prefixo}{resultado['nomes'].get(doc)} ({doc}): marcado como AGENDA.")
        for doc in resultado["incluidos"]:
            logger.info(f"  {prefixo}{resultado['nomes'].get(doc)} ({doc}): não estava na planilha -- "
                        f"incluído como AGENDA.")
        if novos:
            resumo_etapas[TITULO] = {
                "status": "ok",
                "detalhe": f"{prefixo}{len(novos)} destinatário(s) com data de agendamento informada "
                           f"passaram a exigir agendamento (AGENDA na BD_CLIENTES):\n"
                           f"{_descrever(novos, resultado['nomes'])}",
            }
        else:
            logger.info("Nenhum destinatário novo pra marcar.")
    except Exception as e:
        logger.exception(f"Erro ao marcar clientes com agendamento: {e}")
        resumo_etapas[TITULO] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"Marcação de clientes com agendamento finalizada em {duracao:.1f}s.")

    if resumo_etapas:
        try:
            from notificar_execucao_agente import notificar_execucao
            notificar_execucao(resumo_etapas, duracao, modo_teste, config)
        except Exception as e:
            logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Marca como AGENDA na BD_CLIENTES quem já teve "
                                                 "data de agendamento informada")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Lista quem seria marcado, sem gravar na planilha nem no banco")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
