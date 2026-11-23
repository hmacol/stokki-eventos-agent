# -*- coding: utf-8 -*-
"""
portal_cliente/trocas_nf.py

Fila de trocas de NF do fluxo Cross (Hugo, 24-25/10/2026 -- spec
docs/superpowers/specs/2026-10-24-importacao-fluxo-cross-design.md, 6.4-6.7):
o pedido Cross nasce na Stokki com o XML convertido (um item XD) e depois
recebe o XML original pela "Informar Nota Fiscal" (stokki/trocar_nf.py).

Quem enfileira: o worker do portal (pedido criado ou duplicado), o importador
por e-mail (repo agente_importacao_stokki) e o script da virada. Quem
processa: o worker do portal (portal-cliente-envios, ciclo de 20 s, dentro da
trava da Stokki). 3 tentativas com pelo menos 10 min entre elas; depois ERRO e
e-mail SO pra equipe (nunca pro cliente). A troca e idempotente: se o pedido
ja tem o XML original, termina FEITA sem POST.
"""
import html
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_AQUI = Path(__file__).parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402
from email_utils import enviar_email, envelope_html  # noqa: E402
from stokki import sessao_uso  # noqa: E402

logger = logging.getLogger("portal_trocas_nf")

STATUS_PENDENTE = "PENDENTE"
STATUS_FEITA = "FEITA"
STATUS_ERRO = "ERRO"
MAX_TENTATIVAS = 3
INTERVALO_MINIMO_MINUTOS = 10
MAX_POR_PASSADA = 15   # ~5 requisicoes por troca: a passada nao segura a Stokki por muitos minutos
DONO_TRAVA = "portal-trocas-nf"
_FMT = "%Y-%m-%d %H:%M:%S"

_DDL = """
CREATE TABLE IF NOT EXISTS trocas_nf_pendentes (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_embarcador       TEXT NOT NULL,
    client_id             TEXT,
    numero_nf             TEXT NOT NULL,
    chave_nfe             TEXT NOT NULL UNIQUE,
    id_stokki             INTEGER,
    codigo_pedido         TEXT,
    caminho_xml_original  TEXT NOT NULL,
    origem                TEXT NOT NULL,          -- portal | email | virada
    envio_id              INTEGER,                -- portal_envios.id, quando veio do portal
    status                TEXT NOT NULL,          -- PENDENTE | FEITA | ERRO
    tentativas            INTEGER NOT NULL DEFAULT 0,
    erro                  TEXT,
    criado_em             TEXT NOT NULL,
    ultima_tentativa_em   TEXT,
    concluido_em          TEXT
)
"""


def garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _agora(agora: datetime | None = None) -> str:
    return (agora or datetime.now()).strftime(_FMT)


def enfileirar(conn: sqlite3.Connection, *, cnpj_embarcador: str, client_id: str, numero_nf: str, chave_nfe: str,
               caminho_xml_original: str, origem: str, envio_id: int | None = None, id_stokki: int | None = None,
               codigo_pedido: str | None = None, reabrir: bool = False) -> int:
    """Uma linha por chave. Repetida: completa o que faltava (id, envio) sem
    reabrir FEITA; ERRO so volta a PENDENTE com reabrir=True (o importador
    reenvia os mesmos XMLs toda rodada e reabrir sozinho mandaria o mesmo
    e-mail de erro 4x por dia). Faz commit. Devolve o id da linha."""
    garantir_tabela(conn)
    row = conn.execute("SELECT id, status FROM trocas_nf_pendentes WHERE chave_nfe = ?", (chave_nfe,)).fetchone()
    if row:
        conn.execute("""UPDATE trocas_nf_pendentes SET id_stokki = COALESCE(id_stokki, ?),
                        codigo_pedido = COALESCE(codigo_pedido, ?), envio_id = COALESCE(envio_id, ?)
                        WHERE id = ?""", (id_stokki, codigo_pedido, envio_id, row[0]))
        if reabrir and row[1] == STATUS_ERRO:
            conn.execute("UPDATE trocas_nf_pendentes SET status = ?, tentativas = 0, erro = NULL, "
                         "caminho_xml_original = ? WHERE id = ?", (STATUS_PENDENTE, str(caminho_xml_original), row[0]))
        conn.commit()
        return row[0]
    cur = conn.execute("""INSERT INTO trocas_nf_pendentes (cnpj_embarcador, client_id, numero_nf, chave_nfe, id_stokki,
                          codigo_pedido, caminho_xml_original, origem, envio_id, status, tentativas, criado_em)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                       (cnpj_embarcador, str(client_id or ""), str(numero_nf), chave_nfe, id_stokki, codigo_pedido,
                        str(caminho_xml_original), origem, envio_id, STATUS_PENDENTE, _agora()))
    conn.commit()
    return cur.lastrowid


def pendentes_elegiveis(conn: sqlite3.Connection, agora: datetime) -> list[dict]:
    garantir_tabela(conn)
    limite = (agora - timedelta(minutes=INTERVALO_MINIMO_MINUTOS)).strftime(_FMT)
    rows = conn.execute("""SELECT * FROM trocas_nf_pendentes WHERE status = ? AND tentativas < ?
                           AND (ultima_tentativa_em IS NULL OR ultima_tentativa_em <= ?) ORDER BY id""",
                        (STATUS_PENDENTE, MAX_TENTATIVAS, limite)).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM trocas_nf_pendentes LIMIT 0").description]
    return [dict(zip(cols, r)) for r in rows]


def espelhar_em_portal_envios(conn: sqlite3.Connection, linha: dict) -> None:
    """Copia o estado da troca pra linha do envio do portal (selo na lista)."""
    if not linha.get("envio_id"):
        return
    colunas = {r[1] for r in conn.execute("PRAGMA table_info(portal_envios)")}
    if "troca_nf_status" not in colunas:
        return
    conn.execute("""UPDATE portal_envios SET troca_nf_status = ?, troca_nf_em = ?, troca_nf_erro = ?,
                    id_stokki = COALESCE(?, id_stokki) WHERE id = ?""",
                 (linha["status"], linha.get("concluido_em") or linha.get("ultima_tentativa_em"), linha.get("erro"),
                  linha.get("id_stokki"), linha["envio_id"]))


def _nome_embarcador(conn: sqlite3.Connection, cnpj: str) -> str:
    try:
        row = conn.execute("SELECT apelido, nome_remetente FROM interno WHERE cnpj_embarcador = ?", (cnpj,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    return (row and (row[0] or row[1])) or cnpj


def avisar_equipe(config: dict, linha: dict, nome: str = "") -> None:
    """E-mail interno (forcar_destino, se houver, senao atendimento). Nunca pro cliente."""
    from bloqueio_area import forcar_destino
    email_cfg = (config or {}).get("email", {}) or {}
    forcar = forcar_destino(config)
    destino = forcar or email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
    if not destino:
        logger.warning(f"troca de NF {linha['numero_nf']} falhou e nao ha e-mail de equipe no config.")
        return
    nome = nome or linha["cnpj_embarcador"]
    corpo = (f"<p>A troca da NF do pedido Cross não deu certo depois de {MAX_TENTATIVAS} tentativas. "
             f"O pedido existe na Stokki com o item XD, mas a NF dele ainda não é a original.</p>"
             f"<p><b>Embarcador:</b> {html.escape(nome)}<br><b>NF:</b> {html.escape(str(linha['numero_nf']))}<br>"
             f"<b>Pedido:</b> {html.escape(str(linha.get('codigo_pedido') or linha.get('id_stokki') or 'não localizado'))}<br>"
             f"<b>Chave:</b> {html.escape(linha['chave_nfe'])}<br><b>Último erro:</b> {html.escape(linha.get('erro') or '')}</p>"
             f"<p>Faça a troca à mão na Stokki: Pedidos de Saída, Opções, Informar Nota Fiscal, com o XML "
             f"original (<code>{html.escape(str(linha['caminho_xml_original']))}</code>).</p>")
    enviar_email([destino], f"[Portal] Troca de NF falhou · NF {linha['numero_nf']} · {nome}",
                 envelope_html(corpo), email_cfg)


def _erros_de_stokki_fora() -> tuple:
    """Sessao expirada sem conseguir relogar, rede fora ou timeout: nao e
    defeito da linha, e a Stokki indisponivel."""
    import requests
    erros = [requests.exceptions.ConnectionError, requests.exceptions.Timeout]
    try:
        from stokki.auth import SessaoExpiradaError
        erros.append(SessaoExpiradaError)
    except Exception:  # noqa: BLE001
        pass
    return tuple(erros)


def _caminho(texto: str) -> Path:
    p = Path(texto)
    return p if p.is_absolute() else ep._RAIZ / p


def processar_pendentes(conn: sqlite3.Connection, config: dict, simular: bool = False, fabrica_sessao=None,
                        trocar=None, localizar=None, agora: datetime | None = None) -> dict:
    """Uma passada na fila. Sem elegivel nao abre sessao nem pega trava."""
    resumo = {"feitas": 0, "pendentes": 0, "erros": 0, "adiados": 0}
    agora = agora or datetime.now()
    linhas = pendentes_elegiveis(conn, agora)
    if not linhas:
        return resumo
    if simular:
        logger.info(f"SIMULACAO: {len(linhas)} troca(s) de NF ficam na fila.")
        return resumo
    if trocar is None or localizar is None:
        from stokki import trocar_nf as tn
        trocar = trocar or tn.trocar_nf
        localizar = localizar or tn.localizar_pedido_por_nf
    if fabrica_sessao is None:
        from stokki.auth import StokkiSession
        fabrica_sessao = lambda: StokkiSession(config)  # noqa: E731
    if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=10 * 60, esperar_segundos=60, alternativa=True):
        logger.info(f"Stokki ocupada por '{sessao_uso.em_uso()}' -- {len(linhas)} troca(s) de NF esperam o proximo ciclo.")
        resumo["adiados"] = len(linhas)
        return resumo
    try:
        try:
            sessao = fabrica_sessao()
        except Exception as e:  # noqa: BLE001 -- Stokki fora: nao gasta tentativa
            logger.warning(f"trocas de NF: sessao da Stokki indisponivel ({e}) -- {len(linhas)} adiada(s).")
            resumo["adiados"] = len(linhas)
            return resumo
        quando = _agora(agora)
        fora = _erros_de_stokki_fora()
        lote = linhas[:MAX_POR_PASSADA]
        resumo["adiados"] += len(linhas) - len(lote)
        for idx, l in enumerate(lote):
            erro = ""
            try:
                if not l["id_stokki"]:
                    achado = localizar(sessao, l["numero_nf"], l["client_id"])
                    if not achado:
                        raise LookupError(f"pedido da NF {l['numero_nf']} não encontrado na Stokki")
                    l["id_stokki"], l["codigo_pedido"] = achado["id_stokki"], achado.get("codigo")
                    conn.execute("UPDATE trocas_nf_pendentes SET id_stokki = ?, codigo_pedido = ? WHERE id = ?",
                                 (l["id_stokki"], l["codigo_pedido"], l["id"]))
                    conn.commit()  # nunca segurar a escrita do dados.db durante as chamadas a Stokki
                caminho = _caminho(l["caminho_xml_original"])
                if not caminho.is_file():
                    raise FileNotFoundError(f"XML original não encontrado: {l['caminho_xml_original']}")
                r = trocar(sessao, int(l["id_stokki"]), caminho, l["numero_nf"], l["client_id"])
                if not r.get("ok"):
                    erro = r.get("erro") or "troca recusada"
            except fora as e:  # Stokki caiu: para a passada sem gastar tentativa de ninguem
                logger.warning(f"trocas de NF: Stokki indisponivel no meio da passada ({type(e).__name__}: {e}) -- "
                               f"{len(lote) - idx} adiada(s).")
                resumo["adiados"] += len(lote) - idx
                break
            except Exception as e:  # noqa: BLE001 -- uma linha ruim nao para a fila
                erro = str(e) if isinstance(e, (LookupError, FileNotFoundError)) else f"{type(e).__name__}: {e}"
            if not erro:
                l.update(status=STATUS_FEITA, concluido_em=quando, ultima_tentativa_em=quando, erro=None)
                resumo["feitas"] += 1
            else:
                l["tentativas"] = int(l["tentativas"] or 0) + 1
                l.update(erro=erro[:900], ultima_tentativa_em=quando,
                         status=STATUS_ERRO if l["tentativas"] >= MAX_TENTATIVAS else STATUS_PENDENTE)
                resumo["erros" if l["status"] == STATUS_ERRO else "pendentes"] += 1
                logger.warning(f"troca de NF {l['numero_nf']} (tentativa {l['tentativas']}): {erro}")
            conn.execute("""UPDATE trocas_nf_pendentes SET status = ?, tentativas = ?, erro = ?, ultima_tentativa_em = ?,
                            concluido_em = ? WHERE id = ?""",
                         (l["status"], l["tentativas"], l["erro"], l["ultima_tentativa_em"], l.get("concluido_em"), l["id"]))
            espelhar_em_portal_envios(conn, l)
            conn.commit()
            if l["status"] == STATUS_ERRO:
                try:
                    avisar_equipe(config, l, _nome_embarcador(conn, l["cnpj_embarcador"]))
                except Exception as e:  # noqa: BLE001
                    logger.error(f"aviso da troca de NF {l['numero_nf']} falhou: {e}")
        return resumo
    finally:
        sessao_uso.liberar(DONO_TRAVA)
