# -*- coding: utf-8 -*-
"""
relatorio_operacional.py

Relatorio operacional diario/semanal por e-mail, com base nos servicos
do VUUPT (pedido do Hugo, 28/07 - incrementado com graficos e mais
metricas em seguida, a pedido dele: "deixar mais moderno... graficos e
outras informacoes relevantes").

Capacidades da API confirmadas via debug/investigar_api_relatorio*.py:
  - Filtro por periodo: scheduled_start com operadores 'gte'/'lte'
    (valor 'YYYY-MM-DD'), 2 filtros = intervalo (AND). scheduled_for e
    scheduled_start tem o mesmo valor nos registros recentes -- usamos
    scheduled_start por ser o campo que este projeto preenche.
  - Status reais confirmados com dado: not_assigned, accepted, on_route,
    done, canceled. status_done (so quando status=done): success/failed.
  - Paginacao: meta.pagination.{total,total_pages}.

Design: e-mail nao roda JS/SVG de forma confiavel, entao os graficos
(graficos_relatorio.py) sao gerados como PNG e embutidos via Content-ID
-- mesma tecnica ja usada pra logo em email_utils.py. Layout em cards
(nao tabela simples), numeros grandes em destaque, cores por status,
comparacao com o periodo anterior e tendencia dos ultimos 7 dias --
seguindo as praticas de dashboards logisticos (KPIs em destaque,
comparacao de periodo, poucos indicadores bem escolhidos) e as
tendencias de e-mail 2026 (cards, clareza, hierarquia visual guiada).

COMO USAR:
    py -3.11 relatorio_operacional.py --diario
    py -3.11 relatorio_operacional.py --semanal
    py -3.11 relatorio_operacional.py --diario --teste   # gera e mostra, nao envia
"""
import argparse
import logging
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from email.mime.image import MIMEImage
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
(_RAIZ / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ / "dados" / "relatorio_operacional.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("relatorio_operacional")

from vuupt_client import VuuptClient
from email_utils import (
    enviar_email, COR_PRIMARIA, COR_PRIMARIA_CLARA, COR_ACENTO, COR_DESTAQUE,
    COR_TEXTO, COR_TEXTO_SUAVE, COR_FUNDO, COR_BORDA, LOGO_PATH,
)
from graficos_relatorio import CORES_STATUS, grafico_status_donut, grafico_tendencia_barras

DESTINATARIO_PADRAO = "hugo@freshlogbr.com"
DB_PATH = _RAIZ / "dados" / "dados.db"

# Status confirmados com dado real na conta (28/07) -- ordem = ordem de
# exibicao no relatorio. "on_route" e o status usado pra "em rota".
STATUS_RELATORIO = ["not_assigned", "accepted", "on_route", "done", "canceled"]

ROTULOS_STATUS = {
    "not_assigned": "Não atribuídos",
    "accepted":     "Aceitos",
    "on_route":     "Em rota",
    "done":         "Entregues",
    "canceled":     "Cancelados",
    "outros":       "Outros",
}


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _carregar_mapa_embarcadores() -> dict:
    """{sender_id: apelido} a partir da tabela interno -- pra nomear o
    top de embarcadores do periodo. Ausencia do banco nao e fatal
    (retorna {} e o relatorio mostra 'Sender #<id>' no lugar)."""
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT sender_id, apelido, nome_remetente FROM interno "
            "WHERE sender_id IS NOT NULL"
        ).fetchall()
        conn.close()
        return {
            r["sender_id"]: (r["nome_remetente"] or r["apelido"] or f"Sender #{r['sender_id']}")
            for r in rows
        }
    except Exception as e:
        logger.debug(f"Erro ao carregar mapa de embarcadores: {e}")
        return {}


def _filtro_periodo(data_inicio, data_fim_exclusiva):
    return [
        {"field": "scheduled_start", "operator": "gte", "value": data_inicio.strftime("%Y-%m-%d")},
        {"field": "scheduled_start", "operator": "lte", "value": data_fim_exclusiva.strftime("%Y-%m-%d")},
    ]


def coletar_dados(vuupt, data_inicio, data_fim_exclusiva) -> dict:
    """
    Coleta os dados operacionais do VUUPT no periodo [data_inicio,
    data_fim_exclusiva) -- data_fim_exclusiva NAO e incluida (ex: pra
    cobrir so o dia D, inicio=D e fim=D+1).

    Um UNICO fetch paginado (nao uma contagem por status separada) --
    mais simples e sem risco de inconsistencia entre contagens feitas
    em momentos diferentes. Qualquer status fora dos 5 conhecidos vira
    "outros" em vez de sumir silenciosamente.
    """
    filtro = _filtro_periodo(data_inicio, data_fim_exclusiva)
    todos = vuupt.listar_servicos(filtro, per_page=100)

    contagem = Counter()
    done_sucesso = done_falha = 0
    detalhe_nao_atribuidos = []
    contagem_sender = Counter()

    for s in todos:
        status = s.get("status") or "outros"
        chave = status if status in STATUS_RELATORIO else "outros"
        contagem[chave] += 1

        if status == "done":
            sd = s.get("status_done")
            if sd == "success":
                done_sucesso += 1
            elif sd == "failed":
                done_falha += 1

        if status == "not_assigned":
            detalhe_nao_atribuidos.append(s)

        sender_id = s.get("sender_id")
        if sender_id:
            contagem_sender[sender_id] += 1

    mapa_embarcadores = _carregar_mapa_embarcadores()
    top_embarcadores = [
        (mapa_embarcadores.get(sid, f"Sender #{sid}"), qtd)
        for sid, qtd in contagem_sender.most_common(5)
    ]

    return {
        "periodo_inicio": data_inicio,
        "periodo_fim_exclusiva": data_fim_exclusiva,
        "total": len(todos),
        "contagem": dict(contagem),
        "done_sucesso": done_sucesso,
        "done_falha": done_falha,
        "detalhe_nao_atribuidos": detalhe_nao_atribuidos,
        "top_embarcadores": top_embarcadores,
    }


def _eh_fim_de_semana(dia) -> bool:
    """True se sábado (5) ou domingo (6) — sem entrega, por isso são
    ignorados em gráficos de tendência e em qualquer média calculada
    sobre dias (pedido do Hugo, 28/07: 'ignorar sábados e domingos que
    não tem entrega tanto para gráficos quanto para médias')."""
    return dia.weekday() >= 5


def _dia_util_anterior(dia):
    """Dia útil mais recente ANTES de `dia` (pula fins de semana).
    Ex: dia=segunda -> retorna a sexta anterior; dia=terça -> retorna
    a segunda. Usado pra comparação 'vs dia anterior' do relatório
    diário não cair injustamente num domingo vazio."""
    anterior = dia - timedelta(days=1)
    while _eh_fim_de_semana(anterior):
        anterior -= timedelta(days=1)
    return anterior


def coletar_tendencia(vuupt, ultimo_dia, dias=7):
    """
    Total de pedidos POR DIA ÚTIL nos últimos `dias` dias úteis,
    terminando no dia útil mais recente até `ultimo_dia` (inclusive).
    Sábados e domingos são pulados por completo — não entram na lista
    nem contam pra atingir `dias` (sem entrega, só teriam zero e
    distorceriam o gráfico). Usa contar_servicos (só o total, sem
    trazer registros) — bem mais rápido que listar tudo dia a dia.
    Retorna lista de (rótulo 'DD/MM', total), do mais antigo pro mais
    recente.
    """
    resultado = []
    dia = ultimo_dia
    while len(resultado) < dias:
        if not _eh_fim_de_semana(dia):
            filtro = _filtro_periodo(dia, dia + timedelta(days=1))
            try:
                total = vuupt.contar_servicos(filtro)
            except Exception as e:
                logger.error(f"Falha ao contar tendencia do dia {dia}: {e}")
                total = 0
            resultado.append((dia.strftime("%d/%m"), total))
        dia -= timedelta(days=1)
    resultado.reverse()
    return resultado


def _variacao_html(atual, anterior, rotulo_anterior):
    """
    Pequeno indicador de tendência comparando com o período anterior.

    Quando a base de comparação é muito pequena (ex: 1 pedido ontem),
    percentual vira ruído enganoso ("+8900%") em vez de informação —
    achado em produção (28/07, primeiro teste real do Hugo). Abaixo de
    LIMIAR_BASE_PEQUENA, mostra a diferença ABSOLUTA em vez de %.
    """
    LIMIAR_BASE_PEQUENA = 5
    if anterior is None:
        return ""
    diff = atual - anterior
    seta = "&#9650;" if diff > 0 else ("&#9660;" if diff < 0 else "&bull;")
    cor = COR_ACENTO if diff > 0 else (COR_DESTAQUE if diff < 0 else COR_TEXTO_SUAVE)
    sinal = "+" if diff > 0 else ""

    if diff == 0:
        variacao_str = "sem variação"
    elif not anterior or anterior < LIMIAR_BASE_PEQUENA:
        # base pequena/zero: percentual não é confiável -- mostra a
        # diferença em pedidos mesmo, sem esconder o dado
        variacao_str = f"{sinal}{diff} pedido(s)"
    else:
        pct = 100 * diff / anterior
        variacao_str = f"{sinal}{pct:.0f}%"

    return (
        f'<span style="color:{cor};font-size:13px;font-weight:600;">'
        f'{seta} {variacao_str}</span> '
        f'<span style="color:{COR_TEXTO_SUAVE};font-size:12px;">vs {rotulo_anterior} ({anterior})</span>'
    )


def _card_kpi(rotulo, valor, cor, sub_html=""):
    return f"""
    <td style="padding:6px;" valign="top">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background-color:#FFFFFF;border:1px solid {COR_BORDA};border-radius:10px;">
        <tr><td style="padding:16px 18px;">
          <p style="margin:0 0 4px 0;font-size:12px;font-weight:600;color:{COR_TEXTO_SUAVE};
                    text-transform:uppercase;letter-spacing:0.4px;">{rotulo}</p>
          <p style="margin:0;font-size:28px;font-weight:800;color:{cor};line-height:1.1;">{valor}</p>
          {f'<p style="margin:6px 0 0 0;">{sub_html}</p>' if sub_html else ''}
        </td></tr>
      </table>
    </td>"""


def _formatar_data_agendada(valor) -> str:
    """
    Mostra só a data (DD/MM/YYYY) do scheduled_start na tabela de
    pendentes. A API do VUUPT devolve esse campo sempre com o mesmo
    horário fixo (ex: '03:00:00', visto em produção 28/07) — não é a
    janela real de entrega (08:00-16:00 ou a que a mensagem definiu),
    então mostrar o horário ali só confundiria. Se o valor vier num
    formato inesperado, devolve como veio em vez de quebrar.
    """
    if not valor:
        return ""
    try:
        return datetime.strptime(str(valor)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return str(valor)


def gerar_html(dados, tendencia, titulo, dados_anterior, rotulo_anterior,
              caminho_donut, caminho_barras):
    contagem = dados["contagem"]

    variacao_total = ""
    if dados_anterior:
        variacao_total = _variacao_html(dados["total"], dados_anterior["total"], rotulo_anterior)

    total_done = dados["done_sucesso"] + dados["done_falha"]
    taxa_sucesso = (100 * dados["done_sucesso"] / total_done) if total_done else None

    cards = _card_kpi("Total no período", dados["total"], COR_PRIMARIA, variacao_total)
    cards += _card_kpi("Não atribuídos", contagem.get("not_assigned", 0), CORES_STATUS["not_assigned"])
    cards += _card_kpi("Em rota", contagem.get("on_route", 0), CORES_STATUS["on_route"])
    sub_entregues = ""
    if taxa_sucesso is not None:
        cor_taxa = COR_ACENTO if taxa_sucesso >= 90 else COR_DESTAQUE
        sub_entregues = (f'<span style="font-size:12px;color:{cor_taxa};font-weight:600;">'
                        f'{taxa_sucesso:.0f}% de sucesso</span>')
    cards += _card_kpi("Entregues", contagem.get("done", 0), CORES_STATUS["done"], sub_entregues)

    linhas_cards = f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      {cards}
    </tr></table>
    """

    bloco_donut = ""
    if caminho_donut:
        bloco_donut = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:24px;">
          <tr><td style="padding:20px;background-color:#FFFFFF;border:1px solid {COR_BORDA};border-radius:10px;">
            <p style="margin:0 0 12px 0;font-size:14px;font-weight:700;color:{COR_PRIMARIA};">
              Distribuição por status
            </p>
            <img src="cid:grafico_donut" alt="Distribuição por status" width="440" style="display:block;max-width:100%;">
          </td></tr>
        </table>"""

    bloco_barras = ""
    if caminho_barras:
        bloco_barras = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;">
          <tr><td style="padding:20px;background-color:#FFFFFF;border:1px solid {COR_BORDA};border-radius:10px;">
            <p style="margin:0 0 12px 0;font-size:14px;font-weight:700;color:{COR_PRIMARIA};">
              Volume — últimos 7 dias
            </p>
            <img src="cid:grafico_barras" alt="Tendência dos últimos 7 dias" width="600" style="display:block;max-width:100%;">
          </td></tr>
        </table>"""

    bloco_embarcadores = ""
    if dados["top_embarcadores"]:
        linhas_emb = ""
        maior = max(q for _, q in dados["top_embarcadores"])
        for nome, qtd in dados["top_embarcadores"]:
            largura_pct = max(6, int(100 * qtd / maior))
            linhas_emb += f"""
            <tr>
              <td style="padding:8px 0;font-size:13px;color:{COR_TEXTO};width:45%;">{nome}</td>
              <td style="padding:8px 0;">
                <table role="presentation" cellpadding="0" cellspacing="0" width="100%"><tr>
                  <td style="background-color:{COR_ACENTO};height:10px;border-radius:5px;width:{largura_pct}%;"></td>
                  <td style="width:{100-largura_pct}%;"></td>
                </tr></table>
              </td>
              <td style="padding:8px 0 8px 10px;font-size:13px;font-weight:700;color:{COR_PRIMARIA};text-align:right;">{qtd}</td>
            </tr>"""
        bloco_embarcadores = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;">
          <tr><td style="padding:20px;background-color:#FFFFFF;border:1px solid {COR_BORDA};border-radius:10px;">
            <p style="margin:0 0 8px 0;font-size:14px;font-weight:700;color:{COR_PRIMARIA};">
              Top embarcadores do período
            </p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{linhas_emb}</table>
          </td></tr>
        </table>"""

    aviso_cancelados = ""
    if contagem.get("canceled"):
        aviso_cancelados = (
            f'<p style="margin:16px 0 0 0;font-size:13px;color:{COR_TEXTO_SUAVE};">'
            f'<strong style="color:{CORES_STATUS["canceled"]};">{contagem["canceled"]}</strong> '
            f'pedido(s) cancelado(s) no período.</p>'
        )
    aviso_outros = ""
    if contagem.get("outros"):
        aviso_outros = (
            f'<p style="margin:8px 0 0 0;padding:10px 14px;background-color:#FFF8E1;'
            f'border-left:3px solid {COR_DESTAQUE};font-size:12px;color:{COR_TEXTO_SUAVE};">'
            f'{contagem["outros"]} pedido(s) com status fora das categorias conhecidas — '
            f'vale conferir manualmente no VUUPT.</p>'
        )

    tabela_pendentes = ""
    pendentes = dados.get("detalhe_nao_atribuidos") or []
    if pendentes:
        linhas_pend = ""
        for s in pendentes[:30]:
            linhas_pend += (
                f"<tr>"
                f"<td style='padding:8px 12px;border-bottom:1px solid {COR_BORDA};"
                f"font-size:12px;color:{COR_TEXTO};font-weight:600;'>{s.get('code', '')}</td>"
                f"<td style='padding:8px 12px;border-bottom:1px solid {COR_BORDA};"
                f"font-size:12px;color:{COR_TEXTO_SUAVE};'>{s.get('title', '')}</td>"
                f"<td style='padding:8px 12px;border-bottom:1px solid {COR_BORDA};"
                f"font-size:12px;color:{COR_TEXTO_SUAVE};'>{_formatar_data_agendada(s.get('scheduled_start'))}</td>"
                f"</tr>"
            )
        aviso_corte = ""
        if len(pendentes) > 30:
            aviso_corte = (f"<p style='margin:8px 0 0 0;font-size:11px;color:{COR_TEXTO_SUAVE};'>"
                          f"Mostrando 30 de {len(pendentes)} — ver todos no VUUPT.</p>")
        tabela_pendentes = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;">
          <tr><td style="padding:20px;background-color:#FFFFFF;border:1px solid {COR_BORDA};border-radius:10px;">
            <p style="margin:0 0 8px 0;font-size:14px;font-weight:700;color:{CORES_STATUS['not_assigned']};">
              Pedidos ainda não atribuídos
            </p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr style="background-color:{COR_FUNDO};">
                <td style="padding:8px 12px;font-size:11px;font-weight:700;color:{COR_PRIMARIA};text-transform:uppercase;">Código</td>
                <td style="padding:8px 12px;font-size:11px;font-weight:700;color:{COR_PRIMARIA};text-transform:uppercase;">Título</td>
                <td style="padding:8px 12px;font-size:11px;font-weight:700;color:{COR_PRIMARIA};text-transform:uppercase;">Agendado para</td>
              </tr>
              {linhas_pend}
            </table>
            {aviso_corte}
          </td></tr>
        </table>"""

    periodo_fmt = (
        f"{dados['periodo_inicio'].strftime('%d/%m/%Y')} a "
        f"{(dados['periodo_fim_exclusiva'] - timedelta(days=1)).strftime('%d/%m/%Y')}"
    )

    conteudo = f"""
    <p style="margin:0 0 4px 0;font-size:22px;font-weight:800;color:{COR_PRIMARIA};">{titulo}</p>
    <p style="margin:0 0 20px 0;font-size:13px;color:{COR_TEXTO_SUAVE};">Período: {periodo_fmt}</p>
    {linhas_cards}
    {bloco_donut}
    {bloco_barras}
    {bloco_embarcadores}
    {aviso_cancelados}
    {aviso_outros}
    {tabela_pendentes}
    """

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:{COR_FUNDO};font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{COR_FUNDO};padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="680" cellpadding="0" cellspacing="0"
             style="max-width:680px;width:100%;background-color:{COR_FUNDO};">
        <tr>
          <td style="background-color:#FFFFFF;padding:24px 32px;border-bottom:1px solid {COR_BORDA};border-radius:10px 10px 0 0;">
            <img src="cid:logo_freshlog" alt="Freshlog Logistica" width="150" style="display:block;border:0;">
          </td>
        </tr>
        <tr><td style="background-color:{COR_ACENTO};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
        <tr><td style="background-color:{COR_FUNDO};padding:24px 16px;">{conteudo}</td></tr>
        <tr>
          <td style="padding:20px 32px;background-color:#FFFFFF;border-top:1px solid {COR_BORDA};border-radius:0 0 10px 10px;">
            <p style="margin:0;font-size:12px;color:{COR_TEXTO_SUAVE};line-height:1.6;">
              Relatório automático — Agente Stokki Eventos (VUUPT).
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _janela_diaria(referencia):
    return referencia, referencia + timedelta(days=1)


def _janela_semanal(referencia):
    """Ultimos 7 dias corridos terminando no dia anterior a referencia
    (ex: rodando numa segunda, cobre a segunda a domingo anteriores)."""
    fim = referencia
    inicio = fim - timedelta(days=7)
    return inicio, fim


def enviar_email_relatorio(destinatario, titulo, html, config_email,
                           caminho_donut, caminho_barras):
    """Envia o relatorio com os graficos + logo embutidos via Content-ID."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    try:
        msg = MIMEMultipart("related")
        msg["Subject"] = titulo
        msg["From"] = config_email.get("remetente", DESTINATARIO_PADRAO)
        msg["To"] = destinatario

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)

        for cid, caminho in [("logo_freshlog", LOGO_PATH),
                             ("grafico_donut", caminho_donut),
                             ("grafico_barras", caminho_barras)]:
            if caminho and Path(caminho).exists():
                with open(caminho, "rb") as f:
                    img = MIMEImage(f.read())
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=Path(caminho).name)
                msg.attach(img)

        usuario = config_email.get("remetente", DESTINATARIO_PADRAO)
        senha = config_email.get("senha_app") or config_email.get("senha", "")
        host = config_email.get("smtp_host", "smtp.gmail.com")
        port = int(config_email.get("smtp_port", 587))

        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(usuario, senha)
            smtp.send_message(msg)
        logger.info(f"Relatório enviado para {destinatario}: {titulo!r}")
        return True
    except Exception as e:
        logger.error(f"Falha ao enviar relatório para {destinatario}: {e}")
        return False


def executar(periodo, teste=False, destinatario=DESTINATARIO_PADRAO, referencia=None):
    """
    Gera (e envia, a menos que teste=True) o relatorio do periodo dado.
    periodo: "diario" ou "semanal". Retorna os dados coletados (util pra teste).
    """
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    if not token:
        raise RuntimeError("vuupt_api.token ausente no config.yaml")
    vuupt = VuuptClient(token)

    referencia = referencia or date.today()
    if periodo == "diario":
        inicio, fim = _janela_diaria(referencia)
        titulo = f"Relatório Operacional Diário — {inicio.strftime('%d/%m/%Y')}"
        # Comparação com o ÚLTIMO DIA ÚTIL, não literalmente "ontem" --
        # numa segunda-feira, "ontem" seria domingo (sem entrega), o que
        # geraria uma variação sem sentido (pedido do Hugo, 28/07).
        dia_comparacao = _dia_util_anterior(referencia)
        inicio_anterior, fim_anterior = _janela_diaria(dia_comparacao)
        if (referencia - dia_comparacao).days == 1:
            rotulo_anterior = "ontem"
        else:
            rotulo_anterior = f"último dia útil ({dia_comparacao.strftime('%d/%m')})"
    elif periodo == "semanal":
        inicio, fim = _janela_semanal(referencia)
        titulo = (f"Relatório Operacional Semanal — "
                 f"{inicio.strftime('%d/%m')} a {(fim - timedelta(days=1)).strftime('%d/%m/%Y')}")
        inicio_anterior, fim_anterior = _janela_semanal(referencia - timedelta(days=7))
        rotulo_anterior = "semana anterior"
    else:
        raise ValueError(f"periodo invalido: {periodo!r} (use 'diario' ou 'semanal')")

    logger.info(f"Coletando dados do VUUPT: {inicio} a {fim} (exclusiva)...")
    dados = coletar_dados(vuupt, inicio, fim)
    logger.info(f"Total do periodo: {dados['total']} | Contagem: {dados['contagem']}")

    logger.info("Coletando total do periodo anterior (comparacao)...")
    try:
        total_anterior = vuupt.contar_servicos(_filtro_periodo(inicio_anterior, fim_anterior))
    except Exception as e:
        logger.error(f"Falha ao contar periodo anterior: {e}")
        total_anterior = None
    dados_anterior = {"total": total_anterior} if total_anterior is not None else None

    logger.info("Coletando tendencia dos ultimos 7 dias...")
    tendencia = coletar_tendencia(vuupt, fim - timedelta(days=1), dias=7)

    pasta_graficos = _RAIZ / "dados" / "graficos_tmp"
    pasta_graficos.mkdir(parents=True, exist_ok=True)
    dados_donut = [
        (ROTULOS_STATUS[s], dados["contagem"].get(s, 0), CORES_STATUS[s])
        for s in STATUS_RELATORIO + ["outros"]
        if dados["contagem"].get(s, 0) > 0
    ]
    caminho_donut = grafico_status_donut(dados_donut, pasta_graficos / f"donut_{periodo}.png")
    rotulos_tend = [r for r, _ in tendencia]
    valores_tend = [v for _, v in tendencia]
    caminho_barras = grafico_tendencia_barras(
        rotulos_tend, valores_tend, pasta_graficos / f"barras_{periodo}.png",
        indice_destaque=len(valores_tend) - 1,
    )

    html = gerar_html(dados, tendencia, titulo, dados_anterior, rotulo_anterior,
                      caminho_donut, caminho_barras)

    if teste:
        logger.info("[TESTE] Relatorio gerado, e-mail NAO enviado.")
        caminho_preview = _RAIZ / "dados" / f"preview_relatorio_{periodo}.html"
        caminho_preview.write_text(html, encoding="utf-8")
        logger.info(f"Previa HTML salva em: {caminho_preview.resolve()}")
        if caminho_donut:
            logger.info(f"Grafico donut salvo em: {Path(caminho_donut).resolve()}")
        if caminho_barras:
            logger.info(f"Grafico de barras salvo em: {Path(caminho_barras).resolve()}")
    else:
        enviado = enviar_email_relatorio(destinatario, titulo, html, config.get("email", {}),
                                         caminho_donut, caminho_barras)
        if not enviado:
            logger.error("Falha ao enviar o relatorio por e-mail (ver log acima).")

    return dados


def main():
    parser = argparse.ArgumentParser(description="Relatorio operacional diario/semanal (VUUPT) por e-mail")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--diario", action="store_true", help="Relatorio do dia (hoje)")
    grupo.add_argument("--semanal", action="store_true", help="Relatorio dos ultimos 7 dias")
    parser.add_argument("--teste", action="store_true",
                        help="Gera e salva a previa em HTML, mas NAO envia o e-mail")
    parser.add_argument("--destinatario", default=DESTINATARIO_PADRAO,
                        help=f"E-mail de destino (padrao: {DESTINATARIO_PADRAO})")
    args = parser.parse_args()

    periodo = "diario" if args.diario else "semanal"
    executar(periodo, teste=args.teste, destinatario=args.destinatario)


if __name__ == "__main__":
    main()
