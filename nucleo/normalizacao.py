# -*- coding: utf-8 -*-
"""
nucleo/normalizacao.py

Formato ÚNICO do núcleo pro dado que chega de fora (Vuupt, pipeline, app)
-- Etapa 2 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md. Dois defeitos medidos em
produção em 12/09 nasceram da falta disso:

  1. Código do pedido: a Vuupt usa "#PS-12345" desde 20/08 e "PS-12345"
     antes; o núcleo gravava cru, então o mesmo pedido virava duas linhas
     (22 pares) e a /consulta não achava "PS-38552". Chave do núcleo: sem
     "#", maiúscula, COM o sufixo de reentrega ("PS-1-R2" é outro pedido).

  2. Horário: a Vuupt devolve start_at / started_at / arrived_at /
     completed_at / scheduled_* / canceled_at SEM fuso e em UTC; o app e o
     painel gravam hora local. As duas coisas iam pra mesma coluna. No
     núcleo tudo é hora local de São Paulo, "YYYY-MM-DD HH:MM:SS".
"""
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    FUSO_LOCAL = ZoneInfo("America/Sao_Paulo")
except Exception:  # noqa: BLE001 -- Windows sem tzdata: SP não tem horário de verão desde 2019
    FUSO_LOCAL = timezone(timedelta(hours=-3))

FORMATO = "%Y-%m-%d %H:%M:%S"


def normalizar_codigo(codigo) -> str | None:
    """'#PS-12345' / ' ps-12345 ' -> 'PS-12345'. Vazio -> None."""
    texto = str(codigo if codigo is not None else "").strip().lstrip("#").strip().upper()
    return texto or None


def _parse(ts) -> datetime | None:
    texto = str(ts).strip()
    if not texto:
        return None
    texto = texto.replace("T", " ", 1)
    if texto.endswith(("Z", "z")):
        texto = texto[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(texto)
    except ValueError:
        return None


def vuupt_para_local(ts) -> str | None:
    """Horário vindo da Vuupt -> hora local. Sem fuso = UTC (é o que a API
    devolve); com offset ('-03:00', 'Z') o offset é respeitado. Texto que
    não é data volta como veio (nunca perder dado)."""
    if ts is None or str(ts).strip() == "":
        return None
    dt = _parse(ts)
    if dt is None:
        return str(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(FUSO_LOCAL).strftime(FORMATO)


def para_local(ts) -> str | None:
    """Horário de fonte que já trabalha em hora local (pipeline, app,
    painel): sem fuso fica como está (só normaliza o formato); com offset
    converte pra hora de SP."""
    if ts is None or str(ts).strip() == "":
        return None
    dt = _parse(ts)
    if dt is None:
        return str(ts)
    if dt.tzinfo is not None:
        dt = dt.astimezone(FUSO_LOCAL)
    return dt.strftime(FORMATO)


def janela_utc_do_dia(dia: date) -> tuple[str, str]:
    """Dia LOCAL [00:00, 24:00) como limites em UTC sem fuso -- o formato
    que o filtro de start_at da API da Vuupt entende. Uma rota que sai às
    22h de SP começa 01h UTC do dia seguinte e, filtrando por data UTC,
    caía no dia errado."""
    inicio = datetime.combine(dia, time(0, 0), tzinfo=FUSO_LOCAL)
    fim = datetime.combine(dia + timedelta(days=1), time(0, 0), tzinfo=FUSO_LOCAL)
    utc = timezone.utc
    return inicio.astimezone(utc).strftime(FORMATO), fim.astimezone(utc).strftime(FORMATO)
