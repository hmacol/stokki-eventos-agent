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
import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

_RAIZ = Path(__file__).resolve().parent.parent
for _p in (_RAIZ, _RAIZ / "documentos_pedido"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import stokki_documentos as sd  # noqa: E402
from regras.embarcadores import _normalizar  # noqa: E402
from selecionar_pedidos import EMBARCADORES_SEM_NF  # noqa: E402
from stokki import contas, sessao_uso  # noqa: E402

logger = logging.getLogger("documentos_pedido.coletar_xml_nfe")

PASTA_DADOS = _RAIZ / "dados"
PASTA_XML = PASTA_DADOS / "xml_stokki"
DB_PATH = PASTA_DADOS / "dados.db"
DONO_TRAVA = "coletar-xml-nfe"  # stokki/sessao_uso.py
DESDE = date(2026, 7, 15)       # historico do freshhub (spec 0.4)
_ERROS_SEGUIDOS_MAX = 3
_RENOVAR_A_CADA = 25

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
        primeira_em TEXT, ultima_em TEXT, erro TEXT, erros INTEGER DEFAULT 0)""")
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
    if situacao == Situacao.ERRO:  # so os erros SEGUIDOS contam (revisao: 1 timeout nao abandona ativo)
        return int(estado.get("erros") or 0) < _ERRO_MAX_TENTATIVAS
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
    estados = {r[0]: {"situacao": r[1], "tentativas": r[2], "ultima_em": r[3], "erros": r[4]} for r in conn.execute(
        "SELECT codigo, situacao, tentativas, ultima_em, erros FROM xml_stokki_coleta")}
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
    e_erro = int(situacao == Situacao.ERRO)
    conn.execute("""INSERT INTO xml_stokki_coleta
            (codigo, situacao, chave, tentativas, primeira_em, ultima_em, erro, erros)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(codigo) DO UPDATE SET situacao = excluded.situacao, chave = excluded.chave,
            tentativas = tentativas + 1, ultima_em = excluded.ultima_em, erro = excluded.erro,
            erros = CASE WHEN excluded.erros = 1 THEN coalesce(erros, 0) + 1 ELSE 0 END""",
                 (codigo, str(situacao), chave, quando, quando, (erro or None) and erro[:200], e_erro))
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


def coletar(conn: sqlite3.Connection, codigos: list[str], page: Any, ate: datetime, pausa_s: float = 2.0,
            relogio: Callable[[], datetime] = datetime.now, pasta: Path = PASTA_XML,
            gravar_estado: bool = True) -> dict[str, Any]:
    """Um pedido por vez na pagina /provider/ ja logada. Para no horario `ate` (conferido antes de
    cada pedido) ou depois de 3 erros seguidos (Stokki fora ou barrando robo: nao insiste)."""
    contagens: dict[str, Any] = {str(s): 0 for s in Situacao}
    contagens["parou_por"] = "fim"
    seguidos = 0
    for i, codigo in enumerate(codigos):
        agora = relogio()
        if agora >= ate:
            contagens["parou_por"] = "horario"
            break
        if i and i % _RENOVAR_A_CADA == 0:
            sessao_uso.renovar(DONO_TRAVA, ttl_segundos=600)
        chave = erro = None
        try:
            situacao, chave = _um_pedido(page, codigo, pasta)
            seguidos = 0
        except Exception as e:  # noqa: BLE001 - um pedido ruim nao derruba a noite; 3 seguidos sim
            situacao, erro = Situacao.ERRO, f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
            seguidos += 1
        finally:
            _limpar_downloads(codigo)
        if gravar_estado:
            registrar(conn, codigo, situacao, agora, chave=chave, erro=erro)
        contagens[str(situacao)] += 1
        logger.info(f"  {codigo}: {situacao}")
        if seguidos >= _ERROS_SEGUIDOS_MAX:
            contagens["parou_por"] = "erros"
            logger.error(f"{seguidos} erros seguidos na Stokki -- parando a noite ({(erro or '')[:120]}).")
            break
        if pausa_s:
            time.sleep(pausa_s)
    return contagens


class PaginaInvalida(RuntimeError):
    """A Stokki nao mostrou a pagina do pedido (403, login, aba sumida) ou o download nao e XML."""


def _conferir_pagina(page: Any, resposta: Any) -> bool:
    """True = pagina do pedido; False = pedido inexistente (500). Bloqueio, sessao derrubada ou pagina
    sem a aba Documentos levantam: o goto do Playwright NAO falha com HTTP de erro (revisao 0.4b)."""
    status = getattr(resposta, "status", None) or 200
    if status == 500:
        return False
    if status >= 400 or "/login" in str(getattr(page, "url", "") or ""):
        raise PaginaInvalida(f"Stokki respondeu {status} em {str(getattr(page, 'url', ''))[-40:]}")
    if page.query_selector("#document") is None:
        raise PaginaInvalida("pagina sem a aba Documentos")
    return True


def _um_pedido(page: Any, codigo: str, pasta: Path) -> tuple[Situacao, str | None]:
    resposta = page.goto(f"{sd.URL_PROVIDER_SHW}/{sd._extrair_id(codigo)}", wait_until="networkidle",
                         timeout=30_000)
    page.wait_for_timeout(400)
    if not _conferir_pagina(page, resposta):
        return Situacao.SEM_XML, None
    caminho = sd.baixar_xml_nfe(page, codigo)
    if caminho:
        conteudo = Path(caminho).read_bytes()
        chave = sd._chave_do_xml(Path(caminho))
        if chave:
            salvar_xml(conteudo, codigo, pasta)
            return Situacao.OK, chave
        if not conteudo.lstrip().startswith(b"<?xml") and b"<nfeProc" not in conteudo and b"<NFe" not in conteudo:
            raise PaginaInvalida("download nao e XML de NF-e (bloqueio?)")
        return Situacao.PLACEHOLDER, None
    href = sd.localizar_link_xml_nfe(page)
    if href and not sd.xml_placeholder(href):
        raise PaginaInvalida("link do XML real existe mas o download falhou")
    return (Situacao.PLACEHOLDER if href else Situacao.SEM_XML), None


def _limpar_downloads(codigo: str) -> None:
    """Copias do download na pasta temporaria compartilhada (NF-e tem CPF e endereco): o XML
    bom ja foi pra dados/xml_stokki/."""
    for arq in Path(sd.PASTA_TEMP_DOWNLOADS).glob(f"{codigo}_*.xml"):
        arq.unlink(missing_ok=True)


def _agora() -> datetime:
    return datetime.now()


def _na_janela(agora: datetime, ate: str) -> bool:
    """23:00 ate `ate` (03:40): fora disso a provider e das rotinas do dia (revisao: rodada manual
    de dia seguraria a conta por horas)."""
    h, m = (int(x) for x in ate.split(":"))
    return agora.hour >= 23 or (agora.hour, agora.minute) < (h, m)


def _carregar_config() -> dict:
    import yaml
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@contextmanager
def _navegador(config: dict) -> Iterator[Any]:
    """Chromium com o contexto disfarcado do legado e login provider (a trava ja e deste processo)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = sd.nova_pagina(browser)
            sd._login(page, config)
            yield page
        finally:
            browser.close()


def _limite_do_horario(hhmm: str, agora: datetime) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    ate = agora.replace(hour=h, minute=m, second=0, microsecond=0)
    return ate if ate > agora else ate + timedelta(days=1)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Busca na Stokki (so leitura) o XML da NF-e dos pedidos "
                                            "sem nota no freshhub e grava em dados/xml_stokki/.")
    p.add_argument("--limite", type=int, default=3500)
    p.add_argument("--ate", default="03:40", help="HH:MM: para antes deste horario (padrao 03:40)")
    p.add_argument("--somente", help="PS-1,PS-2: so estes pedidos (ignora a selecao e o estado)")
    p.add_argument("--modo-teste", action="store_true",
                   help="salva os XMLs numa pasta temporaria e nao grava estado")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    agora = _agora()
    if not args.somente and not _na_janela(agora, args.ate):
        logger.warning(f"Fora da janela (23:00-{args.ate}); de dia so com --somente.")
        print(json.dumps({"pedidos": 0, "parou_por": "fora da janela"}))  # noqa: T201
        return 0
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        garantir_tabela(conn)
        codigos = (list(dict.fromkeys(_base(c) or c.strip().upper() for c in args.somente.split(",") if c.strip()))
                   if args.somente else selecionar(conn, DESDE, agora, args.limite))
        resultado: dict[str, Any] = {"pedidos": len(codigos), "parou_por": "nada a fazer"}
        if codigos:
            if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=600, esperar_segundos=1800,
                                       conta=contas.PROVIDER):
                logger.warning(f"Conta provider ocupada por '{sessao_uso.em_uso(conta=contas.PROVIDER)}' "
                               "ha mais de 30 min -- sem login nesta noite.")
                resultado["parou_por"] = "ocupada"
            else:
                pasta = Path(tempfile.mkdtemp(prefix="xml_stokki_teste_")) if args.modo_teste else PASTA_XML
                try:
                    with _navegador(_carregar_config()) as page:
                        resultado.update(coletar(conn, codigos, page, _limite_do_horario(args.ate, agora),
                                                 pasta=pasta, gravar_estado=not args.modo_teste))
                finally:
                    sessao_uso.liberar(DONO_TRAVA)
                if args.modo_teste:
                    resultado["pasta_teste"] = str(pasta)
    finally:
        conn.close()
    print(json.dumps(resultado, ensure_ascii=False))  # noqa: T201
    return 1 if resultado.get("parou_por") == "erros" else 0


if __name__ == "__main__":
    sys.exit(main())
