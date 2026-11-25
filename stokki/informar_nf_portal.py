# -*- coding: utf-8 -*-
"""
stokki/informar_nf_portal.py

Etapa da Estacao de Impressao (Hugo, 22/11/2026): o pedido criado no portal
por planilha nasce na Stokki sem nota e fica em "Aguardando faturamento". O
cliente anexa o XML da NF-e depois, pelo portal (portal_envios_anexos, tipo
'XML da NF-e'), e ninguem levava esse XML pra Stokki: o pedido nunca virava
faturado e a Estacao nao imprimia.

Esta etapa roda antes da impressao (imprimir_pedidos_pendentes chama):
  1. acha no banco os pedidos do portal com XML anexado ainda nao informado;
     sem nenhum, nao toca na Stokki;
  2. le a aba "Aguardando faturamento" da Estacao (conta provider);
  3. confere o XML com o pedido (emitente do embarcador, mesmo destinatario);
  4. envia pelo "Informar Nota Fiscal" (conta admin, stokki/trocar_nf.enviar_nf);
  5. rele a aba: so vale como feito se o pedido saiu de "Aguardando faturamento".

Chave: estacao_impressao.informar_nf_portal.ativo (padrao desligado) e, pro
piloto, .so_codigos: [PS-n]. Com dry_run so lista, mesmo com a chave desligada.
Estado em portal_xml_stokki (uma linha por anexo): INFORMADA | PENDENTE | ERRO.
Falha da Stokki tenta em 3 rodadas; XML que nao bate vira ERRO direto. Ao
virar ERRO, um e-mail pra equipe (nunca pro cliente).
"""
import html
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).resolve().parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"
PASTA_ANEXOS = _RAIZ / "dados" / "portal_envios" / "_anexos"

TIPO_XML = "XML da NF-e"          # portal_cliente/anexos_pedido.TIPO_XML
ABA_AGUARDANDO = "awaiting_billing"
STATUS_INFORMADA, STATUS_PENDENTE, STATUS_ERRO = "INFORMADA", "PENDENTE", "ERRO"
MAX_TENTATIVAS = 3
DIAS_JANELA = 30                  # anexo mais velho que isso nao e mais procurado
MAX_POR_RODADA = 30
DONO_TRAVA = "estacao-informar-nf"
ESPERA_TRAVA_S = 5 * 60

_RE_EMIT = re.compile(rb"<(?:\w+:)?emit>.*?<(?:\w+:)?(?:CNPJ|CPF)>(\d+)<", re.S)
_RE_DEST = re.compile(rb"<(?:\w+:)?dest>.*?<(?:\w+:)?(?:CNPJ|CPF)>(\d+)<", re.S)


def _digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _id_do_codigo(codigo) -> int | None:
    m = re.fullmatch(r"#?PS-?(\d+)", str(codigo or "").strip(), re.IGNORECASE)
    return int(m.group(1)) if m else None


def garantir_tabela(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portal_xml_stokki (
            anexo_id      INTEGER PRIMARY KEY,
            envio_id      INTEGER NOT NULL,
            codigo_pedido TEXT NOT NULL,
            status        TEXT NOT NULL,
            tentativas    INTEGER NOT NULL DEFAULT 0,
            erro          TEXT,
            atualizado_em TEXT NOT NULL
        )""")


def candidatos(conn: sqlite3.Connection, agora: datetime) -> list[dict]:
    """XMLs anexados no portal (pedido ja criado na Stokki) que ainda nao
    foram informados nem desistidos. So leitura."""
    tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"portal_envios", "portal_envios_anexos"} <= tabelas:
        return []
    estado = ("LEFT JOIN portal_xml_stokki s ON s.anexo_id = a.id" if "portal_xml_stokki" in tabelas
              else "LEFT JOIN (SELECT NULL anexo_id, NULL status, 0 tentativas) s ON s.anexo_id = a.id")
    rows = conn.execute(f"""
        SELECT a.id, a.envio_id, a.arquivo, a.numero_nf, e.codigo_pedido, e.cnpj_embarcador,
               e.destinatario_doc, COALESCE(s.tentativas, 0)
        FROM portal_envios_anexos a JOIN portal_envios e ON e.id = a.envio_id {estado}
        WHERE a.tipo = ? AND e.status = 'CRIADO' AND e.codigo_pedido IS NOT NULL
          AND a.criado_em >= datetime(?, ?)
          AND (s.status IS NULL OR s.status = ?)
        ORDER BY a.id""", (TIPO_XML, agora.strftime("%Y-%m-%d %H:%M:%S"), f"-{DIAS_JANELA} days",
                           STATUS_PENDENTE)).fetchall()
    cols = ("anexo_id", "envio_id", "arquivo", "numero_nf", "codigo_pedido", "cnpj_embarcador",
            "destinatario_doc", "tentativas")
    return [c for c in (dict(zip(cols, r)) for r in rows) if _id_do_codigo(c["codigo_pedido"])]


def conferir_xml(conteudo: bytes, cand: dict) -> tuple[str, str]:
    """(chave, problema). problema "" = o XML e deste pedido: emitente da mesma
    empresa do embarcador (raiz do CNPJ, pra valer filial) e o mesmo
    destinatario do pedido (quando o pedido tem documento do destinatario)."""
    from stokki.trocar_nf import chave_do_xml
    chave = chave_do_xml(conteudo) or ""
    if not chave:
        return "", "XML sem chave de acesso"
    emit = _RE_EMIT.search(conteudo)
    dest = _RE_DEST.search(conteudo)
    emit, dest = (emit.group(1).decode() if emit else ""), (dest.group(1).decode() if dest else "")
    emb = _digitos(cand["cnpj_embarcador"])
    if not emit or emit[:8] != emb[:8]:
        return chave, f"emitente da nota ({emit or 'não lido'}) não é o embarcador do pedido ({emb})"
    doc = _digitos(cand["destinatario_doc"])
    if doc and dest != doc:
        return chave, f"destinatário da nota ({dest or 'não lido'}) diferente do pedido ({doc})"
    return chave, ""


def _gravar(conn, cand: dict, status: str, erro: str, agora: datetime) -> None:
    garantir_tabela(conn)
    conn.execute("INSERT OR REPLACE INTO portal_xml_stokki (anexo_id, envio_id, codigo_pedido, status, tentativas, "
                 "erro, atualizado_em) VALUES (?,?,?,?,?,?,?)",
                 (cand["anexo_id"], cand["envio_id"], cand["codigo_pedido"], status, cand["tentativas"],
                  (erro or None) and erro[:900], agora.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


def _falhou(conn, cand: dict, erro: str, agora: datetime, definitivo: bool, resultado: dict) -> None:
    cand["tentativas"] += 1
    desistiu = definitivo or cand["tentativas"] >= MAX_TENTATIVAS
    _gravar(conn, cand, STATUS_ERRO if desistiu else STATUS_PENDENTE, erro, agora)
    logger.warning(f"  {cand['codigo_pedido']} (NF {cand['numero_nf']}): {erro}"
                   + ("" if desistiu else f" -- tenta de novo (tentativa {cand['tentativas']})"))
    resultado["falhas"].append(f"{cand['codigo_pedido']}: {erro}")
    if desistiu:
        resultado["desistidos"].append({**cand, "erro": erro})


def avisar_equipe(config: dict, desistidos: list[dict]) -> None:
    email_cfg = (config or {}).get("email", {}) or {}
    destino = email_cfg.get("email_atendimento") or email_cfg.get("email_responsavel")
    if not destino or not desistidos:
        return
    from email_utils import enviar_email, envelope_html
    linhas = "".join(f"<li><b>{html.escape(str(d['codigo_pedido']))}</b> · NF {html.escape(str(d['numero_nf'] or '?'))}: "
                     f"{html.escape(d['erro'])}</li>" for d in desistidos)
    corpo = ("<p>O cliente anexou o XML da nota no portal, mas não foi possível informar a nota na Stokki. "
             "O pedido continua em <b>Aguardando faturamento</b> e não vai para a impressão.</p>"
             f"<ul>{linhas}</ul>"
             "<p>Confira o XML no portal e informe a nota à mão na Stokki: Pedidos de Saída, Opções, "
             "Informar Nota Fiscal. O sistema não tenta de novo com este mesmo arquivo.</p>")
    enviar_email([destino], f"[Estação de Impressão] {len(desistidos)} nota(s) do portal não informada(s) na Stokki",
                 envelope_html(corpo), email_cfg)


def _codigos_aguardando(config: dict) -> set[int]:
    from stokki import estacao_impressao as ei
    fila = ei.ler_filas(config, abas=(ABA_AGUARDANDO,))[ABA_AGUARDANDO]
    return {i for i in (_id_do_codigo(p.get("codigo_ps")) for p in fila) if i}


def informar_nfs_do_portal(config: dict, dry_run: bool = False, agora: datetime | None = None,
                           db_path: Path | None = None, pasta_anexos: Path | None = None,
                           ler_aguardando=None, fabrica_sessao=None) -> dict:
    """Uma passada. Devolve {"candidatos", "aguardando", "informadas": [codigo],
    "falhas": [texto], "desistidos": [dict], "simulados": [codigo], "motivo"}."""
    resultado = {"candidatos": 0, "aguardando": 0, "informadas": [], "falhas": [], "desistidos": [],
                 "simulados": [], "motivo": ""}
    cfg = ((config or {}).get("estacao_impressao", {}) or {}).get("informar_nf_portal", {}) or {}
    if not dry_run and not cfg.get("ativo"):
        resultado["motivo"] = "desligada (estacao_impressao.informar_nf_portal.ativo)"
        return resultado
    agora = agora or datetime.now()
    so_codigos = {_id_do_codigo(c) for c in (cfg.get("so_codigos") or [])} - {None}
    pasta_anexos = pasta_anexos or PASTA_ANEXOS

    conn = sqlite3.connect(db_path or DB_PATH, timeout=30)
    try:
        cands = candidatos(conn, agora)
        if so_codigos:
            cands = [c for c in cands if _id_do_codigo(c["codigo_pedido"]) in so_codigos]
        resultado["candidatos"] = len(cands)
        if not cands:
            return resultado

        ler_aguardando = ler_aguardando or _codigos_aguardando
        aguardando = ler_aguardando(config)
        cands = [c for c in cands if _id_do_codigo(c["codigo_pedido"]) in aguardando][:MAX_POR_RODADA]
        resultado["aguardando"] = len(cands)
        logger.info(f"Informar NF do portal: {resultado['candidatos']} XML(s) anexado(s) no portal, "
                    f"{len(cands)} com o pedido em 'Aguardando faturamento'.")

        prontos = []
        for c in cands:
            arquivo = pasta_anexos / c["arquivo"]
            if not arquivo.is_file():
                chave, problema, conteudo = "", f"arquivo do XML não encontrado ({c['arquivo']})", b""
            else:
                conteudo = arquivo.read_bytes()
                chave, problema = conferir_xml(conteudo, c)
            if problema:
                if dry_run:
                    logger.info(f"  [TESTE] {c['codigo_pedido']} (NF {c['numero_nf']}): NÃO enviaria -- {problema}")
                    resultado["falhas"].append(f"{c['codigo_pedido']}: {problema}")
                else:
                    _falhou(conn, c, problema, agora, True, resultado)
                continue
            if dry_run:
                logger.info(f"  [TESTE] {c['codigo_pedido']}: enviaria o XML da NF {c['numero_nf']} (chave {chave})")
                resultado["simulados"].append(c["codigo_pedido"])
                continue
            prontos.append((c, chave, conteudo))

        if prontos:
            aceitos = _enviar(config, prontos, conn, agora, resultado, fabrica_sessao)
            if aceitos:
                ainda = ler_aguardando(config)
                for c in aceitos:
                    if _id_do_codigo(c["codigo_pedido"]) in ainda:
                        _falhou(conn, c, "a Stokki aceitou a nota, mas o pedido continua em Aguardando faturamento",
                                agora, False, resultado)
                    else:
                        _gravar(conn, c, STATUS_INFORMADA, "", agora)
                        resultado["informadas"].append(c["codigo_pedido"])
                        logger.info(f"  {c['codigo_pedido']}: NF {c['numero_nf']} informada, pedido faturado.")
        if resultado["desistidos"]:
            try:
                avisar_equipe(config, resultado["desistidos"])
            except Exception as e:  # noqa: BLE001 -- o aviso nao pode derrubar a impressao
                logger.error(f"Informar NF do portal: aviso da equipe falhou: {e}")
        return resultado
    finally:
        conn.close()


def _enviar(config, prontos, conn, agora, resultado, fabrica_sessao) -> list[dict]:
    """POSTs dentro da vez da conta admin. Devolve os candidatos que a Stokki
    aceitou. Stokki ocupada ou sessao fora: ninguem gasta tentativa."""
    from stokki import sessao_uso, trocar_nf as tn
    conta, peguei = sessao_uso.conta_do_processo(), False
    if not conta:
        conta = sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=10 * 60, esperar_segundos=ESPERA_TRAVA_S, alternativa=True)
        if not conta:
            resultado["motivo"] = f"Stokki ocupada por '{sessao_uso.em_uso()}': {len(prontos)} nota(s) ficam pra próxima rodada"
            logger.info(f"Informar NF do portal: {resultado['motivo']}.")
            return []
        peguei = True
    try:
        try:
            if fabrica_sessao is None:
                from stokki.auth import StokkiSession
                fabrica_sessao = lambda: StokkiSession(config, conta=conta)  # noqa: E731
            sessao = fabrica_sessao()
            token = tn.token_csrf(sessao)
        except Exception as e:  # noqa: BLE001
            resultado["motivo"] = f"sessão da Stokki indisponível ({e}): {len(prontos)} nota(s) ficam pra próxima rodada"
            logger.warning(f"Informar NF do portal: {resultado['motivo']}.")
            return []
        if not token:
            resultado["motivo"] = "token da página de pedidos não encontrado"
            logger.warning(f"Informar NF do portal: {resultado['motivo']}.")
            return []
        aceitos = []
        for c, chave, conteudo in prontos:
            try:
                erro = tn.enviar_nf(sessao, _id_do_codigo(c["codigo_pedido"]), chave, conteudo, token)
            except Exception as e:  # noqa: BLE001 -- um pedido ruim nao para os outros
                erro = f"{type(e).__name__}: {e}"
            if erro:
                _falhou(conn, c, erro, agora, False, resultado)
            else:
                aceitos.append(c)
        return aceitos
    finally:
        if peguei:
            sessao_uso.liberar(DONO_TRAVA)
