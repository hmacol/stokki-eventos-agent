# -*- coding: utf-8 -*-
"""
coletar_xml_nfe.py

Busca na Stokki o XML da NF-e dos pedidos que o sistema novo (freshhub)
carregou sem nota (plano 0.4b do freshhub, 20/10/2026). O freshhub nao
fala com a Stokki: este coletor grava cada XML em dados/xml_stokki/PS-<id>.xml
e o snapshot diario do freshhub (05:10) leva a pasta.

So leitura na Stokki: abre a pagina do pedido na area /provider/ e baixa o
XML (baixar_xml_nfe ja descarta o placeholder e cai no anexo real). Conta
provider, sempre pela trava (stokki/sessao_uso.py): login concorrente
derruba a sessao de quem estiver usando. Janela 23:30-03:40, 2 s por pedido.

Estado por pedido na tabela xml_stokki_coleta (dados.db): OK nunca repete;
ativo sem XML tenta toda noite; finalizado sem XML mais 2 vezes, uma por
semana (pedido finalizado nao aceita anexo); erro ate 5 vezes.
"""
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from regras.embarcadores import _normalizar
from selecionar_pedidos import EMBARCADORES_SEM_NF

PASTA_DADOS = Path(__file__).resolve().parent.parent / "dados"
PASTA_XML = PASTA_DADOS / "xml_stokki"

_ATIVOS = ("ABERTO", "EM_ROTA")
_FINALIZADO_MAX_TENTATIVAS = 3
_FINALIZADO_INTERVALO = timedelta(days=7)
_ERRO_MAX_TENTATIVAS = 5
_FORMATO = "%Y-%m-%d %H:%M:%S"
_CODIGO_BASE = re.compile(r"^(PS-\d+)(?:-R\d+)?$")


class Situacao(StrEnum):
    OK = "OK"                    # XML real salvo
    PLACEHOLDER = "PLACEHOLDER"  # so o rascunho da Stokki, sem anexo real
    SEM_XML = "SEM_XML"          # nenhum XML no pedido
    ERRO = "ERRO"


def garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS xml_stokki_coleta (
        codigo TEXT PRIMARY KEY, situacao TEXT, chave TEXT, tentativas INTEGER,
        primeira_em TEXT, ultima_em TEXT, erro TEXT)""")
    conn.commit()


def _base(codigo: str | None) -> str | None:
    m = _CODIGO_BASE.match(str(codigo or "").strip().upper().lstrip("#"))
    return m.group(1) if m else None


def deve_tentar(estado: dict | None, pedido_ativo: bool, agora: datetime) -> bool:
    if not estado:
        return True
    situacao, tentativas = estado["situacao"], int(estado["tentativas"] or 0)
    if situacao == Situacao.OK:
        return False
    if situacao == Situacao.ERRO:
        return tentativas < _ERRO_MAX_TENTATIVAS
    if pedido_ativo:
        return True
    ultima = datetime.strptime(estado["ultima_em"], _FORMATO) if estado.get("ultima_em") else None
    return tentativas < _FINALIZADO_MAX_TENTATIVAS and (ultima is None or agora - ultima >= _FINALIZADO_INTERVALO)


def selecionar(conn: sqlite3.Connection, desde: date, agora: datetime, limite: int) -> list[str]:
    """Codigos PS-<id> base (o id da Stokki e o numero do PS; reentrega -Rn e o mesmo pedido la),
    ativos primeiro e depois do mais novo pro mais velho."""
    corte = desde.isoformat()
    com_parada = {_base(r[0]) for r in conn.execute(
        "SELECT p.codigo FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id WHERE r.data_rota >= ?",
        (corte,))}
    com_portal = {_base(r[0]) for r in conn.execute(
        "SELECT codigo_pedido, chave_nfe FROM portal_envios WHERE coalesce(chave_nfe, '') != '' "
        "AND coalesce(status, '') NOT IN ('CANCELADO', 'DUPLICADO')") if not str(r[1]).startswith("99")}
    estados = {r[0]: {"situacao": r[1], "tentativas": r[2], "ultima_em": r[3]} for r in conn.execute(
        "SELECT codigo, situacao, tentativas, ultima_em FROM xml_stokki_coleta")}
    pedidos: dict[str, dict] = {}
    for codigo, status, remetente, criado in conn.execute(
            "SELECT codigo, status, remetente_nome, criado_em_provedor FROM nucleo_pedidos"):
        base = _base(codigo)
        if base is None:
            continue
        p = pedidos.setdefault(base, {"ativo": False, "criado": "", "periodo": False, "sem_nf": False})
        p["ativo"] = p["ativo"] or str(status or "") in _ATIVOS
        p["criado"] = max(p["criado"], str(criado or ""))
        p["periodo"] = p["periodo"] or str(criado or "")[:10] >= corte
        nome = _normalizar(str(remetente or ""))
        p["sem_nf"] = p["sem_nf"] or any(n in nome for n in EMBARCADORES_SEM_NF)
    candidatos = [
        (base, p) for base, p in pedidos.items()
        if (p["periodo"] or base in com_parada) and not p["sem_nf"] and base not in com_portal
        and deve_tentar(estados.get(base), p["ativo"], agora)
    ]
    candidatos.sort(key=lambda bp: bp[1]["criado"], reverse=True)
    candidatos.sort(key=lambda bp: not bp[1]["ativo"])  # estavel: ativos na frente, cada grupo do mais novo
    return [base for base, _ in candidatos[:limite]]


def registrar(conn: sqlite3.Connection, codigo: str, situacao: Situacao, agora: datetime,
              chave: str | None = None, erro: str | None = None) -> None:
    quando = agora.strftime(_FORMATO)
    conn.execute("""INSERT INTO xml_stokki_coleta (codigo, situacao, chave, tentativas, primeira_em, ultima_em, erro)
        VALUES (?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(codigo) DO UPDATE SET situacao = excluded.situacao, chave = excluded.chave,
            tentativas = tentativas + 1, ultima_em = excluded.ultima_em, erro = excluded.erro""",
                 (codigo, str(situacao), chave, quando, quando, (erro or None) and erro[:200]))
    conn.commit()


def salvar_xml(conteudo: bytes, codigo: str, pasta: Path = PASTA_XML) -> Path:
    """Escrita atomica: o snapshot das 05:10 nunca ve XML pela metade (so copia *.xml)."""
    pasta.mkdir(parents=True, exist_ok=True)
    final = pasta / f"{codigo}.xml"
    tmp = pasta / f"{codigo}.xml.tmp"
    try:
        tmp.write_bytes(conteudo)
        os.chmod(tmp, 0o640)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final
