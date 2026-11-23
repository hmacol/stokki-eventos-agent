# -*- coding: utf-8 -*-
"""
nucleo/acoes_motorista.py

Ações da tela /motoristas sobre uma linha da bd_motoristas: inativar/reativar,
excluir e resetar o PIN do login do app. Nada aqui chama Vuupt, WhatsApp ou Stokki.
"""
import logging
import shutil
import sqlite3

from nucleo import atualizacao_motorista as atualizacao, auth_motorista as auth, banco, cadastro_motorista as cad
from regras import motoristas_banco
from regras.cadastro_motoristas import MotoristaNaoEncontrado, _espelhar, editar_motorista
from regras.preferencias_motoristas import _parse_bool

logger = logging.getLogger("nucleo.acoes_motorista")

# Tabelas com a coluna `agent_id` (conferidas no DDL do código). `rotas` é a tabela antiga, sem DDL
# no código: em produção existe, vazia, só com `motorista_cpf`.
_TABELAS_COM_AGENTE = ("nucleo_rotas", "rascunhos_rota", "baixas_sem_app")
MOTIVO_EXCLUIDO = "excluído no painel"


class AcaoRecusada(Exception):
    def __init__(self, mensagem: str, codigo: int = 400):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.codigo = codigo


def rotas_do_agente(conn: sqlite3.Connection, agent_id: int, cpf: str | None = None) -> int:
    """Linhas de rota/rascunho/baixa com esse agente (e `rotas` antiga pelo CPF).
    Tabela que não existe conta 0; tabela conhecida SEM `agent_id` recusa (a
    guarda da exclusão não pode falhar aberta)."""
    total = 0
    for tabela in _TABELAS_COM_AGENTE:
        colunas = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        if not colunas:
            continue
        if "agent_id" not in colunas:
            raise AcaoRecusada(f"não deu pra conferir as rotas: a tabela {tabela} está sem a coluna agent_id", 409)
        total += conn.execute(f"SELECT COUNT(*) FROM {tabela} WHERE agent_id = ?", (agent_id,)).fetchone()[0]
    cpf = auth.normalizar_cpf(cpf)
    if cpf and "motorista_cpf" in {r["name"] for r in conn.execute("PRAGMA table_info(rotas)")}:
        total += conn.execute("SELECT COUNT(*) FROM rotas WHERE motorista_cpf = ?", (cpf,)).fetchone()[0]
    return total


def _linha(conn, motorista_id: int) -> dict:
    linha = next((l for l in motoristas_banco.listar(conn) if l["id"] == motorista_id), None)
    if linha is None:
        raise MotoristaNaoEncontrado(f"Motorista id={motorista_id} nao existe.")
    return linha


def _pessoa(conn, motorista_id: int) -> dict:
    return next(p for p in atualizacao.pessoas(conn) if motorista_id in p["ids"])


def _cancelar_links(conn, ids: list[int], commit: bool = True) -> list[int]:
    """Cancela os links em aberto da pessoa; o rascunho PENDENTE de cada um vira SUBSTITUIDO.
    Devolve os ids dos rascunhos (com commit=False, quem chama apaga as pastas depois do commit)."""
    marcas = ",".join("?" * len(ids))
    onde = f"concluido_em IS NULL AND cancelado_em IS NULL AND bd_motorista_id IN ({marcas})"
    links = [r[0] for r in conn.execute(f"SELECT id FROM motoristas_links_atualizacao WHERE {onde}", ids)]
    conn.execute(f"UPDATE motoristas_links_atualizacao SET cancelado_em = ? WHERE {onde}", (banco.agora(), *ids))
    rascunhos = atualizacao.descartar_rascunhos(conn, links)
    if commit:
        conn.commit()
        atualizacao.apagar_pastas(rascunhos)
    return rascunhos


def definir_ativo(conn, config: dict, motorista_id: int, ativo: bool, usuario: str) -> dict:
    resultado = editar_motorista(config, motorista_id, {"ativo": bool(ativo)}, usuario, conn=conn)
    if not ativo:
        pessoa = _pessoa(conn, motorista_id)
        if not pessoa["ativo"]:
            _cancelar_links(conn, pessoa["ids"])
    return {"ok": True, **resultado}


def excluir(conn, config: dict, motorista_id: int, usuario: str) -> dict:
    linha = _linha(conn, motorista_id)
    pessoa = _pessoa(conn, motorista_id)
    n = rotas_do_agente(conn, linha["AGENT_ID_VUUPT"], pessoa["cpf"]) if linha["AGENT_ID_VUUPT"] is not None else 0
    if n:
        raise AcaoRecusada(f"tem {n} rotas no histórico: inative em vez de excluir", 409)

    restantes = [i for i in pessoa["ids"] if i != motorista_id]
    login_apagado, fichas, ids_fichas, rascunhos = False, 0, [], []
    try:   # a linha e tudo que depende dela saem numa transação só (commit único no fim)
        motoristas_banco.apagar(conn, motorista_id, f"painel:{usuario}", commit=False)
        if restantes:
            # vínculos que apontavam pra linha apagada passam pra principal que sobrou
            novo = min(restantes)
            conn.execute("UPDATE motoristas_links_atualizacao SET bd_motorista_id = ? WHERE bd_motorista_id = ?",
                         (novo, motorista_id))
            conn.execute("UPDATE OR IGNORE motoristas_grupos_whatsapp SET bd_motorista_id = ? WHERE bd_motorista_id = ?",
                         (novo, motorista_id))
            conn.execute("DELETE FROM motoristas_grupos_whatsapp WHERE bd_motorista_id = ?", (motorista_id,))
            conn.execute("UPDATE motoristas_cadastros SET bd_motorista_id = ? WHERE bd_motorista_id = ?",
                         (novo, motorista_id))
        else:
            ids = pessoa["ids"]
            marcas = ",".join("?" * len(ids))
            rascunhos = _cancelar_links(conn, ids, commit=False)
            conn.execute(f"DELETE FROM motoristas_grupos_whatsapp WHERE bd_motorista_id IN ({marcas})", ids)
            if pessoa["cpf"]:
                login_apagado = conn.execute("DELETE FROM motoristas WHERE cpf = ?", (pessoa["cpf"],)).rowcount > 0
            # só as fichas desta pessoa: o CPF dela; sem CPF, a ficha cujo CPF não é de outra linha
            # (a linha apagada já saiu da tabela). Ficha de outro CPF nunca é tocada.
            ligadas = [r["id"] for r in conn.execute(
                f"SELECT id FROM motoristas_cadastros WHERE bd_motorista_id IN ({marcas})", ids)]
            ids_fichas = [r["id"] for r in conn.execute(
                f"SELECT id, cpf FROM motoristas_cadastros WHERE bd_motorista_id IN ({marcas})", ids)
                if (auth.normalizar_cpf(r["cpf"]) == pessoa["cpf"] if pessoa["cpf"]
                    else not motoristas_banco.por_cpf(conn, r["cpf"]))]
            # a de outro CPF solta o vínculo com a linha apagada: o vincular_fichas religa pelo CPF
            for ficha_id in (i for i in ligadas if i not in ids_fichas):
                conn.execute("UPDATE motoristas_cadastros SET bd_motorista_id = NULL WHERE id = ?", (ficha_id,))
            agora = banco.agora()
            for ficha_id in ids_fichas:
                # concluido_em: o retentar do link não filtra status (mesmo critério do desfazer_cadastro_link)
                conn.execute("UPDATE motoristas_cadastros SET status = ?, motivo = ?, "
                             "concluido_em = COALESCE(concluido_em, ?) WHERE id = ?",
                             (cad.STATUS_RECUSADO, MOTIVO_EXCLUIDO, agora, ficha_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    for ficha_id in ids_fichas:
        shutil.rmtree(cad.pasta_cadastros() / str(ficha_id), ignore_errors=True)
    atualizacao.apagar_pastas([i for i in rascunhos if i not in ids_fichas])
    fichas = len(ids_fichas)
    _espelhar(conn)
    logger.info("Motorista id=%s excluido por %s (login_apagado=%s, fichas=%s).", motorista_id, usuario,
                login_apagado, fichas)
    return {"ok": True, "login_apagado": login_apagado, "fichas": fichas}


def resetar_pin(conn, motorista_id: int, usuario: str) -> dict:
    """PIN novo sorteado (provisório: o app pede a troca) para o CPF da linha.
    Sem login ainda, cria. O PIN só volta no retorno; o histórico não o guarda."""
    linha = _linha(conn, motorista_id)
    cpf = auth.normalizar_cpf(linha.get("CPF_MOTORISTA"))
    if not cpf:
        raise AcaoRecusada("falta o CPF", 400)
    pin = cad.sortear_pin()
    login = auth.buscar_motorista(conn, cpf)
    if login:
        auth.criar_ou_atualizar_motorista(conn, cpf, login["nome"], pin=pin, agent_id=login.get("agent_id"),
                                          perfil=login.get("perfil") or auth.PERFIL_MOTORISTA,
                                          ativo=bool(login.get("ativo")), trocar_pin=True)
    else:
        auth.criar_ou_atualizar_motorista(conn, cpf, linha["NOME_MOTORISTA"], pin=pin,
                                          agent_id=linha.get("AGENT_ID_VUUPT"),
                                          vehicle_id=linha.get("VEHICLE_ID_VUUPT"),
                                          telefone=linha.get("TELEFONE_MOTORISTA"),
                                          email=linha.get("EMAIL_MOTORISTA"),
                                          tipo_veiculo=linha.get("TIPO_VEICULO"),
                                          ativo=_parse_bool(linha.get("ATIVO")), trocar_pin=True)
    motoristas_banco._historico(conn, motorista_id, linha["AGENT_ID_VUUPT"], "PIN_RESETADO", None, None,
                                f"painel:{usuario}", banco.agora())
    conn.commit()
    logger.info("PIN do motorista id=%s resetado por %s.", motorista_id, usuario)
    return {"cpf": cpf, "pin": pin, "criado": login is None}
