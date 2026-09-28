# -*- coding: utf-8 -*-
"""
vigia/regras.py

Regras PURAS do vigia (sem banco, sem rede): em que estado cada pedido
aberto está e quando o prazo dele vence. Prazos decididos pelo Hugo em
28/09:

  SEM_SERVICO          aberto na Stokki e sem serviço na Vuupt      4h
  NO_POOL              no pool, sem rota                           1 dia útil
  AGUARDANDO_CLIENTE   agendamento pedido ao embarcador            2 dias úteis
  EM_RASCUNHO          rascunho não enviado                        19h do último dia útil antes da data
  RASCUNHO_COM_ERRO    rascunho em ERRO_ENVIO                      na hora
  ROTA_PASSADA         em rota de dia anterior que não terminou    na hora
  INSUCESSO            insucesso sem reentrega nem decisão         1h depois da 1ª rodada da expedição
                                                                    passadas 12h (a reentrega automática
                                                                    roda nela; se passou, algo travou)
  RECUSADO             embarcador pediu pra não reenviar           2 dias úteis (cancelar/devolver na Stokki)

Estados sem prazo (só aparecem na lista): AGENDADO (data futura), EM_ROTA
(rota de hoje ou futura).
"""
from datetime import date, datetime, time, timedelta

SEM_SERVICO = "SEM_SERVICO"
NO_POOL = "NO_POOL"
AGENDADO = "AGENDADO"
AGUARDANDO_CLIENTE = "AGUARDANDO_CLIENTE"
EM_RASCUNHO = "EM_RASCUNHO"
RASCUNHO_COM_ERRO = "RASCUNHO_COM_ERRO"
EM_ROTA = "EM_ROTA"
ROTA_PASSADA = "ROTA_PASSADA"
INSUCESSO = "INSUCESSO"
RECUSADO = "RECUSADO"

ROTULOS = {
    SEM_SERVICO: "Aberto na Stokki sem serviço na Vuupt",
    NO_POOL: "No pool sem rota",
    AGENDADO: "Agendado pra frente",
    AGUARDANDO_CLIENTE: "Aguardando o embarcador",
    EM_RASCUNHO: "Rascunho não enviado",
    RASCUNHO_COM_ERRO: "Rascunho com erro de envio",
    EM_ROTA: "Em rota",
    ROTA_PASSADA: "Preso em rota de dia anterior",
    INSUCESSO: "Insucesso sem reentrega",
    RECUSADO: "Embarcador recusou o reenvio",
}

# Ordem de gravidade na tela e na Torre (primeiro = mais grave).
ORDEM = [ROTA_PASSADA, INSUCESSO, SEM_SERVICO, RASCUNHO_COM_ERRO, EM_RASCUNHO,
         NO_POOL, AGUARDANDO_CLIENTE, RECUSADO, AGENDADO, EM_ROTA]
CRITICOS = {ROTA_PASSADA, INSUCESSO, SEM_SERVICO, RASCUNHO_COM_ERRO}

HORAS_SEM_SERVICO = 4
HORAS_REENTREGA_AUTO = 12  # espelha expedir_pedidos.HORAS_REENTREGA_AUTO
DIAS_UTEIS_NO_POOL = 1
DIAS_UTEIS_CLIENTE = 2
HORA_LIMITE_RASCUNHO = time(19, 0)


def eh_dia_util(d: date) -> bool:
    return d.weekday() < 5


def somar_dias_uteis(inicio: datetime, dias: int) -> datetime:
    """Mesmo horário, `dias` dias úteis depois (sábado/domingo não contam;
    começar num fim de semana conta a partir da segunda)."""
    atual = inicio
    while not eh_dia_util(atual.date()):
        atual = datetime.combine(atual.date() + timedelta(days=1), time(0, 0))
    restantes = dias
    while restantes > 0:
        atual += timedelta(days=1)
        if eh_dia_util(atual.date()):
            restantes -= 1
    return atual


def proxima_rodada_expedicao(dt: datetime) -> datetime:
    """1ª rodada do expedir_pedidos.py a partir de dt (timers: 08:00-19:30
    de 30 em 30 min + 22:00 da sequência da noite, todo dia)."""
    t = dt.time()
    if time(8, 0) <= t <= time(19, 30):
        return dt
    if time(19, 30) < t <= time(22, 0):
        return datetime.combine(dt.date(), time(22, 0))
    dia = dt.date() if t < time(8, 0) else dt.date() + timedelta(days=1)
    return datetime.combine(dia, time(8, 0))


def ultimo_dia_util_antes(d: date) -> date:
    atual = d - timedelta(days=1)
    while not eh_dia_util(atual):
        atual -= timedelta(days=1)
    return atual


def prazo(estado: str, desde: datetime, *, data_rascunho: date | None = None) -> datetime | None:
    """Quando o prazo do estado vence (None = estado sem prazo)."""
    if estado == SEM_SERVICO:
        return desde + timedelta(hours=HORAS_SEM_SERVICO)
    if estado == NO_POOL:
        return somar_dias_uteis(desde, DIAS_UTEIS_NO_POOL)
    if estado in (AGUARDANDO_CLIENTE, RECUSADO):
        return somar_dias_uteis(desde, DIAS_UTEIS_CLIENTE)
    if estado == INSUCESSO:
        # A reentrega automática roda junto da expedição (30 em 30 min das
        # 08h às 19h30 e às 22h): o prazo é a 1ª rodada depois das 12h + 1h
        # de folga. Sem isso todo insucesso da tarde virava alerta de
        # madrugada, antes de a automática ter tido chance.
        return proxima_rodada_expedicao(desde + timedelta(hours=HORAS_REENTREGA_AUTO)) + timedelta(hours=1)
    if estado in (RASCUNHO_COM_ERRO, ROTA_PASSADA):
        return desde
    if estado == EM_RASCUNHO:
        if data_rascunho is None:
            return None
        return datetime.combine(ultimo_dia_util_antes(data_rascunho), HORA_LIMITE_RASCUNHO)
    return None


def classificar(p: dict, hoje: date) -> tuple[str, str] | None:
    """Estado de UM pedido a partir dos fatos já reunidos por vigiar.py.
    `p` tem as chaves (todas opcionais):
      status_nucleo      ABERTO | EM_ROTA | INSUCESSO | ENTREGUE | CANCELADO | None (sem serviço)
      aberto_stokki      bool -- está no retrato da Stokki
      acao_pipeline      última ação do pipeline pro pedido
      obs_pipeline
      agendamento        date | None -- scheduled_start do serviço
      agendamento_pendente bool -- pedimos data ao embarcador e ele não respondeu
      rascunho_status    RASCUNHO | OFERTADA | ERRO_ENVIO | None
      rascunho_data      date | None
      rota_data          date | None -- data da rota em que está
      tem_reentrega      bool -- insucesso já duplicado (ou agendado)
      recusado           bool -- embarcador pediu pra não reenviar
      tratado_na_torre   bool
      motivo_pool        str -- por que está fora da roteirização (dedicado, área não atendida)
    Retorna (estado, motivo) ou None quando não há nada a vigiar
    (entregue, cancelado e fechado na Stokki, insucesso já resolvido...).
    """
    status = p.get("status_nucleo")

    if status is None or status == "CANCELADO":
        if not p.get("aberto_stokki"):
            return None
        acao = p.get("acao_pipeline") or "ainda não processado pelo pipeline"
        obs = p.get("obs_pipeline")
        return SEM_SERVICO, f"{acao}" + (f" -- {obs}" if obs else "")

    if status == "ENTREGUE":
        return None

    if status == "INSUCESSO":
        if p.get("recusado"):
            if not p.get("aberto_stokki"):
                return None
            return RECUSADO, "embarcador pediu pra não reenviar -- cancelar/devolver na Stokki"
        if p.get("tem_reentrega") or p.get("tratado_na_torre"):
            return None
        return INSUCESSO, p.get("motivo_insucesso") or "sem reentrega nem decisão"

    if status == "EM_ROTA":
        rota_data = p.get("rota_data")
        if rota_data and rota_data < hoje:
            return ROTA_PASSADA, f"rota de {rota_data:%d/%m} não terminou"
        return EM_ROTA, f"rota de {rota_data:%d/%m}" if rota_data else "em rota"

    # ABERTO (not_assigned)
    if p.get("rascunho_status") == "ERRO_ENVIO":
        return RASCUNHO_COM_ERRO, f"rascunho de {p['rascunho_data']:%d/%m} falhou no envio"
    if p.get("rascunho_status") in ("RASCUNHO", "OFERTADA"):
        return EM_RASCUNHO, f"rascunho de {p['rascunho_data']:%d/%m} ainda não enviado"
    ag = p.get("agendamento")
    if ag and ag > hoje:
        return AGENDADO, f"agendado pra {ag:%d/%m}"
    # Data já no serviço (combinada por telefone, planilha...) vence o
    # pedido de data por e-mail que ficou PENDENTE.
    if p.get("agendamento_pendente") and not ag:
        return AGUARDANDO_CLIENTE, "embarcador ainda não informou a data de agendamento"
    # Dedicado / área não atendida ficam fora da roteirização de propósito,
    # mas continuam sendo pedido parado -- o motivo diz por quê.
    if p.get("motivo_pool"):
        return NO_POOL, p["motivo_pool"]
    if ag and ag < hoje:
        return NO_POOL, f"agendamento de {ag:%d/%m} venceu sem rota"
    return NO_POOL, "aguardando roteirização"


def vencido(vence_em: datetime | None, agora: datetime) -> bool:
    return vence_em is not None and agora >= vence_em
