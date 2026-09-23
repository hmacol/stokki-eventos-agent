# -*- coding: utf-8 -*-
"""
painel_agentes/wms_faltas_recebimento.py

Relatorio de faltas do recebimento, por e-mail pro embarcador (pedido do
Hugo, 23/09/2026). Quando o galpao encerra um recebimento COM DIVERGENCIA
(chegou menos do que foi anunciado -- carga parcial, avaria, item que nao
veio), o cliente recebe na hora o que foi anunciado, o que chegou e o que
faltou, linha a linha, com a observacao de quem descarregou.

QUANDO (decisao do Hugo): na hora do encerramento, nao num resumo do dia
seguinte -- o cliente fica sabendo no mesmo dia da descarga, quando ainda
da pra reclamar com o transportador dele.
PRA QUEM (decisao do Hugo): o contato do embarcador ja cadastrado, com
copia pra entregas@.

Mesmo desenho de notificar_nfs_em_rota.py, que e o padrao das rotinas que
mandam e-mail pro embarcador neste projeto:
  - Destinatarios e liga/desliga vem de preferencias_notificacao
    (tipo "faltas_recebimento"), o botao Notificacoes do portal do
    cliente. A chave aqui e o `stkkc_id` (#stkkc-<id> do embarcador na
    Stokki, que o proprio recebimento carrega), nao o sender_id da Vuupt.
  - notificacoes_automaticas.ativo (chave-mestra) vale pro envio real.
  - notificacoes_enviadas (tipo + chave): encerrar duas vezes, ou uma
    retentativa depois de resposta perdida, nao manda dois e-mails.

*** O ENVIO REAL PRO CLIENTE NASCE DESLIGADO ***
config.yaml -> notificacao_faltas_recebimento.forcar_destino: enquanto
preenchido, TODO e-mail vai so pra ele em vez de ir ao cliente. SEM a
secao no config o padrao e o e-mail do Hugo -- ou seja, o comportamento
seguro nao depende de ninguem lembrar de configurar nada. Ligar o envio
real e decisao do Hugo, com `forcar_destino: ""` explicito no config da
VPS (regra do projeto, CLAUDE.md).

Nao ha rotina de lote nem timer: quem chama e a rota
/api/wms/recebimentos/<id>/encerrar-divergencia do painel, logo depois de
wms_pedidos.encerrar_com_divergencia. Falha de e-mail nunca desfaz o
encerramento -- o recebimento ja esta encerrado, o e-mail se reenvia.
"""
import html
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import email_utils  # noqa: E402
import preferencias_notificacao  # noqa: E402

logger = logging.getLogger("wms_faltas_recebimento")

TIPO_NOTIFICACAO = "faltas_recebimento"
SECAO_CONFIG = "notificacao_faltas_recebimento"
EMAIL_TESTE = "hugo@freshlogbr.com"
COPIA_INTERNA = "entregas@freshlogbr.com"


# --- Config -------------------------------------------------------------------

def _secao(config: dict) -> dict:
    return (config or {}).get(SECAO_CONFIG) or {}


def rotina_ativa(config: dict) -> bool:
    return bool(_secao(config).get("ativo", True))


def forcar_destino_do_config(config: dict) -> str:
    """Sem a chave no config.yaml o padrao e redirecionar pro Hugo: ligar o
    envio real pro cliente e decisao dele, com forcar_destino: ""."""
    secao = _secao(config)
    if "forcar_destino" not in secao:
        return EMAIL_TESTE
    return str(secao.get("forcar_destino") or "").strip()


def resolver_destinos(emails: list, modo_teste: bool, forcar_destino: str) -> tuple:
    """(destinos, redirecionado) -- mesma funcao das outras rotinas."""
    if modo_teste:
        return [EMAIL_TESTE], True
    if forcar_destino:
        return [forcar_destino], True
    return list(emails), False


# --- Embarcador ---------------------------------------------------------------

def _caminho_do_banco(conn) -> Path:
    """O dados.db desta conexao -- as preferencias moram no mesmo banco."""
    for _, nome, arquivo in conn.execute("PRAGMA database_list"):
        if nome == "main" and arquivo:
            return Path(arquivo)
    return preferencias_notificacao.DB_PATH


def stkkc_id_do_recebimento(rec: dict, config: dict) -> str:
    """O #stkkc-<id> gravado no proprio recebimento. Recebimento antigo
    (gravado antes da coluna existir) cai no embarcador piloto do
    config.yaml, que e o unico que o WMS atende hoje."""
    do_registro = str((rec or {}).get("stkkc_id") or "").strip()
    if do_registro:
        return do_registro
    return str(((config or {}).get("wms", {}) or {}).get("embarcador_piloto_id", "48")).strip()


def embarcador_do_recebimento(conn, rec: dict, config: dict) -> dict:
    """
    {"nome", "emails", "notificar"} do embarcador dono do recebimento, do
    jeito que o cliente configurou no botao Notificacoes do portal.

    Devolve {} quando o #stkkc-<id> do recebimento nao existe no cadastro
    (interno.stkkc_id) -- e melhor nao mandar nada do que mandar falta de
    mercadoria pro cliente errado.
    """
    alvo = stkkc_id_do_recebimento(rec, config)
    embs = preferencias_notificacao.carregar_embarcadores(
        TIPO_NOTIFICACAO, chave="stkkc_id", db_path=_caminho_do_banco(conn))
    for chave, emb in embs.items():
        if str(chave).strip() == alvo:
            return {"nome": emb["nome"] or rec.get("embarcador") or f"Embarcador {alvo}",
                    "emails": emb["emails"], "notificar": not emb["desligado"]}
    return {}


# --- Idempotencia -------------------------------------------------------------

def _garantir_tabela(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notificacoes_enviadas (
            tipo       TEXT NOT NULL,
            chave      TEXT NOT NULL,
            enviado_em TEXT NOT NULL,
            PRIMARY KEY (tipo, chave)
        )""")


def _chave(id_stokki, redirecionado: bool) -> str:
    # O redirecionado (piloto) tem chave propria: ligar o envio real depois
    # ainda manda pro cliente o relatorio que so o Hugo tinha visto.
    return f"{id_stokki}" + ("|redirecionado" if redirecionado else "")


def ja_enviado(conn, chave: str) -> bool:
    _garantir_tabela(conn)
    return conn.execute("SELECT 1 FROM notificacoes_enviadas WHERE tipo = ? AND chave = ?",
                        (TIPO_NOTIFICACAO, chave)).fetchone() is not None


def registrar_envio(conn, chave: str) -> None:
    _garantir_tabela(conn)
    conn.execute("INSERT OR REPLACE INTO notificacoes_enviadas (tipo, chave, enviado_em) VALUES (?,?,?)",
                 (TIPO_NOTIFICACAO, chave, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


# --- E-mail -------------------------------------------------------------------

def _num(valor) -> str:
    """Numero do jeito que o cliente le: 27,5 -- nao 27.5."""
    return f"{float(valor or 0):g}".replace(".", ",")


def _data_amigavel(bruto: str) -> str:
    """'2026-09-23 14:32:05' -> '23/09/2026 as 14:32'. Data que ja vem no
    formato do cliente (a chegada da Stokki, '22/09/2026') passa direto."""
    texto = str(bruto or "").strip()
    for formato, saida in (("%Y-%m-%d %H:%M:%S", "%d/%m/%Y às %H:%M"), ("%Y-%m-%d", "%d/%m/%Y")):
        try:
            return datetime.strptime(texto, formato).strftime(saida)
        except ValueError:
            continue
    return texto


def _td(conteudo: str, extra: str = "") -> str:
    return (f"<td style='padding:9px 12px;border-bottom:1px solid {email_utils.COR_BORDA};font-size:13px;"
            f"color:{email_utils.COR_TEXTO};vertical-align:top;{extra}'>{conteudo}</td>")


def montar_email(rec: dict, faltas: list, nome_emb: str,
                 destino_original: list = None) -> tuple:
    """(assunto, corpo_html) do relatorio de faltas. Responde, pra quem
    recebe: qual recebimento, de quando, o que foi anunciado, o que
    chegou, o que faltou e o que o operador viu."""
    codigo = str(rec.get("codigo") or f"Recebimento {rec.get('id_stokki', '')}")
    quantos = "1 item" if len(faltas) == 1 else f"{len(faltas)} itens"
    chegada = _data_amigavel(rec.get("chegada") or "")
    encerrado = _data_amigavel(rec.get("encerrado_em") or "")
    assunto = f"Freshlog | Falta no recebimento {codigo}: {quantos}"

    linhas = ""
    for f in faltas:
        unidade = html.escape(str(f.get("unidade") or "UN"))
        linhas += ("<tr>"
                   + _td(html.escape(str(f.get("descricao") or "")).upper()
                         + f"<br><span style='font-size:12px;color:{email_utils.COR_TEXTO_SUAVE};'>"
                           f"SKU {html.escape(str(f.get('sku') or '-'))} · linha {f.get('linha', '')}</span>",
                         "font-weight:600;")
                   + _td(f"{_num(f.get('qtd_un'))} {unidade}", "white-space:nowrap;text-align:right;")
                   + _td(f"{_num(f.get('qtd_enderecada'))} {unidade}", "white-space:nowrap;text-align:right;")
                   + _td(f"{_num(f.get('falta_un'))} {unidade}",
                         f"white-space:nowrap;text-align:right;font-weight:700;color:{email_utils.COR_ERRO};")
                   + "</tr>")
    cab = "".join(
        f"<td style='padding:8px 12px;font-size:11px;font-weight:600;color:{email_utils.COR_PRIMARIA};"
        f"text-transform:uppercase;letter-spacing:0.4px;{extra}'>{t}</td>"
        for t, extra in (("Item", ""), ("Anunciado", "text-align:right;"),
                         ("Recebido", "text-align:right;"), ("Faltou", "text-align:right;")))

    observacao = ""
    if (rec.get("observacao_divergencia") or "").strip():
        observacao = (
            f"<p style='margin:0 0 20px;padding:12px 14px;font-size:13px;color:{email_utils.COR_TEXTO};"
            f"background-color:{email_utils.COR_FUNDO};border-left:3px solid {email_utils.COR_DESTAQUE};"
            f"border-radius:4px;line-height:1.6;'><strong>Observação de quem recebeu:</strong><br>"
            f"{html.escape(rec['observacao_divergencia'])}</p>")

    aviso = ""
    if destino_original is not None:
        aviso = (f"<p style='margin:0 0 20px;padding:10px 14px;font-size:12px;color:{email_utils.COR_TEXTO};"
                 f"background-color:#FEF3C7;border-radius:6px;'><strong>Redirecionado (piloto).</strong> "
                 f"Destino real: {html.escape(', '.join(destino_original) or 'sem e-mail cadastrado')}</p>")

    conferencia = []
    if encerrado:
        conferencia.append(f"em {html.escape(encerrado)}")
    if (rec.get("encerrado_por") or "").strip():
        conferencia.append(f"por {html.escape(rec['encerrado_por'])}")

    conteudo = f"""
    {aviso}
    <h2 style="margin:0 0 4px;font-size:20px;color:{email_utils.COR_TEXTO};">Falta no recebimento {html.escape(codigo)}</h2>
    <p style="margin:0 0 24px;font-size:14px;color:{email_utils.COR_TEXTO_SUAVE};line-height:1.6;">
      Olá, <strong>{html.escape(str(nome_emb))}</strong>!<br>
      Terminamos a conferência do recebimento <strong>{html.escape(codigo)}</strong>
      {f"(chegada em {html.escape(chegada)})" if chegada else ""} e
      <strong>{quantos}</strong> chegaram em quantidade menor do que a anunciada.
      O que chegou já está no estoque; abaixo está a diferença.
    </p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid {email_utils.COR_BORDA};border-radius:8px;overflow:hidden;margin-bottom:20px;">
      <tr style="background-color:{email_utils.COR_PRIMARIA_CLARA};">{cab}</tr>
      {linhas}
    </table>
    {observacao}
    <p style="margin:0 0 24px;font-size:12px;color:{email_utils.COR_TEXTO_SUAVE};line-height:1.6;">
      Quantidades conferidas na descarga, no galpão da Freshlog{(', ' + ', '.join(conferencia)) if conferencia else ''}.
      Itens que chegaram completos não aparecem nesta lista.<br>
      Se a diferença não bater com o que você enviou, responda este e-mail
      que verificamos junto com você.
    </p>
    <p style="margin:0;font-size:14px;color:{email_utils.COR_TEXTO};line-height:1.6;">
      Atenciosamente,<br><strong>Freshlog Logística</strong>
    </p>"""
    return assunto, email_utils.envelope_html(
        conteudo, "Freshlog Logística - relatório automático de faltas na conferência do recebimento.",
        cor_acento=email_utils.COR_DESTAQUE)


# --- Execucao -----------------------------------------------------------------

def notificar_faltas(conn, recebimento_id: int, config: dict, enviar=None,
                     modo_teste: bool = False) -> dict:
    """
    Manda o relatorio de faltas de UM recebimento ja encerrado com
    divergencia. Nunca levanta por causa do e-mail -- quem chama e uma
    rota HTTP que ja encerrou o recebimento; devolve o que aconteceu, pra
    tela poder dizer a verdade ao operador.

    `motivo` explica o que impediu o envio, quando nao houve:
      sem_faltas | nao_encerrado | desativado | sem_embarcador |
      desligado_pelo_cliente | sem_email | ja_enviado | falha_no_envio
    """
    enviar = enviar or email_utils.enviar_email
    r = {"enviado": False, "destinos": [], "redirecionado": False, "motivo": "", "faltas": 0}

    rec_row = conn.execute("SELECT * FROM wms_recebimentos WHERE id = ?", (int(recebimento_id),)).fetchone()
    if not rec_row:
        r["motivo"] = "nao_encerrado"
        return r
    rec = dict(rec_row)
    if rec.get("estado") != "DIVERGENCIA":
        r["motivo"] = "nao_encerrado"
        return r

    faltas = [dict(x) for x in conn.execute("""
        SELECT i.*, COALESCE(p.unidade, 'UN') AS unidade
          FROM wms_recebimento_itens i
          LEFT JOIN wms_produtos p ON p.id = i.produto_id
         WHERE i.recebimento_id = ? AND i.falta_un > 0 ORDER BY i.linha""", (int(recebimento_id),))]
    r["faltas"] = len(faltas)
    if not faltas:
        r["motivo"] = "sem_faltas"
        return r

    forcar = forcar_destino_do_config(config)
    if not modo_teste:
        if not rotina_ativa(config):
            r["motivo"] = "desativado"
        elif not forcar and not email_utils.notificacoes_automaticas_ativas(config):
            r["motivo"] = "desativado"
        if r["motivo"]:
            return r

    try:
        emb = embarcador_do_recebimento(conn, rec, config)
    except (sqlite3.Error, FileNotFoundError, ValueError) as e:
        logger.warning("Recebimento %s: nao consegui resolver o embarcador -- %s", rec.get("codigo"), e)
        emb = {}
    if not emb:
        r["motivo"] = "sem_embarcador"
        return r
    if not emb["notificar"]:
        r["motivo"] = "desligado_pelo_cliente"
        return r
    if not emb["emails"]:
        r["motivo"] = "sem_email"
        return r

    destinos, redirecionado = resolver_destinos(emb["emails"], modo_teste, forcar)
    r["destinos"], r["redirecionado"] = destinos, redirecionado
    chave = _chave(rec.get("id_stokki"), redirecionado)
    if not modo_teste and ja_enviado(conn, chave):
        r["motivo"] = "ja_enviado"
        return r

    assunto, corpo = montar_email(rec, faltas, emb["nome"],
                                  destino_original=emb["emails"] if redirecionado else None)
    # A copia interna so faz sentido no envio real: redirecionado, tudo vai
    # pro destino unico e nao sai de casa.
    copia = [] if redirecionado else [COPIA_INTERNA]
    if enviar(destinos, assunto, corpo, (config or {}).get("email", {}), cc=copia):
        r["enviado"] = True
        if not modo_teste:
            registrar_envio(conn, chave)
        logger.info("Relatorio de faltas do %s enviado pra %s%s", rec.get("codigo"), ", ".join(destinos),
                    " (redirecionado)" if redirecionado else "")
    else:
        r["motivo"] = "falha_no_envio"
    return r
