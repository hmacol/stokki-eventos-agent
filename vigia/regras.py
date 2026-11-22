# -*- coding: utf-8 -*-
"""
vigia/regras.py

Regras PURAS do vigia (sem banco, sem rede): em que estado cada pedido
aberto está e quando o prazo dele vence. Prazos decididos pelo Hugo em
28/09:

  SEM_SERVICO          aberto na Stokki e sem serviço na Vuupt      4h
  NO_POOL              no pool, sem rota                           1 dia útil
                       (segurado pra consolidar rota fraca)       19h do último dia útil antes do
                                                                    prazo de 3 dias úteis (Hugo, 29/09)
                       (região semanal/quinzenal sem agendamento) 7 / 15 dias corridos (Hugo, 03/10)
  AGUARDANDO_CLIENTE   agendamento pedido ao embarcador            2 dias úteis
  EM_RASCUNHO          rascunho não enviado                        05h30 do dia da rota (Hugo, 21/11: as
                                                                    rotas saem de madrugada, 19h da véspera
                                                                    alarmava toda noite)
  RASCUNHO_COM_ERRO    rascunho em ERRO_ENVIO                      na hora
  ROTA_PASSADA         em rota de dia anterior que não terminou    na hora
  INSUCESSO            insucesso sem reentrega nem decisão         1h depois da 1ª rodada da expedição
                                                                    passadas 12h (a reentrega automática
                                                                    roda nela; se passou, algo travou)
  RECUSADO             embarcador pediu pra não reenviar           2 dias úteis (cancelar/devolver na Stokki)

Estados sem prazo (só aparecem na lista): AGENDADO (data futura), EM_ROTA
(rota de hoje ou futura) e AGUARDANDO_RETIRADA (retirada no galpão: não sobe
pra Vuupt desde 06/11, então não é "sem serviço"; prazo a definir, Hugo 21/11).

Cada estado também diz O QUE FAZER e QUEM cuida (acao_e_dono, Hugo 21/11:
dono fixo por situação) -- é o que a tela /vigia mostra em cada pedido.
"""
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
from regras import feriados  # noqa: E402

SEM_SERVICO = "SEM_SERVICO"
NO_POOL = "NO_POOL"
AGENDADO = "AGENDADO"
AGUARDANDO_RETIRADA = "AGUARDANDO_RETIRADA"
AGUARDANDO_CLIENTE = "AGUARDANDO_CLIENTE"
EM_RASCUNHO = "EM_RASCUNHO"
RASCUNHO_COM_ERRO = "RASCUNHO_COM_ERRO"
EM_ROTA = "EM_ROTA"
ROTA_PASSADA = "ROTA_PASSADA"
INSUCESSO = "INSUCESSO"
RECUSADO = "RECUSADO"
# Não vem do vigia: é a divergência ENTREGUE_NAO_EXPEDIDO do batimento, listada junto.
ENTREGUE_NAO_EXPEDIDO = "ENTREGUE_NAO_EXPEDIDO"

ROTULOS = {
    SEM_SERVICO: "Aberto na Stokki sem serviço na Vuupt",
    NO_POOL: "No pool sem rota",
    AGENDADO: "Agendado pra frente",
    AGUARDANDO_RETIRADA: "Aguardando retirada no galpão",
    AGUARDANDO_CLIENTE: "Aguardando o embarcador",
    EM_RASCUNHO: "Rascunho não enviado",
    RASCUNHO_COM_ERRO: "Rascunho com erro de envio",
    EM_ROTA: "Em rota",
    ROTA_PASSADA: "Preso em rota de dia anterior",
    INSUCESSO: "Insucesso sem reentrega",
    RECUSADO: "Embarcador recusou o reenvio",
    ENTREGUE_NAO_EXPEDIDO: "Entregue e não expedido na Stokki",
}

# Ordem de gravidade na tela e na Torre (primeiro = mais grave).
ORDEM = [ROTA_PASSADA, INSUCESSO, SEM_SERVICO, RASCUNHO_COM_ERRO, EM_RASCUNHO,
         NO_POOL, AGUARDANDO_CLIENTE, RECUSADO, AGUARDANDO_RETIRADA, AGENDADO, EM_ROTA]
CRITICOS = {ROTA_PASSADA, INSUCESSO, SEM_SERVICO, RASCUNHO_COM_ERRO}

HORAS_SEM_SERVICO = 4
HORAS_REENTREGA_AUTO = 12  # espelha expedir_pedidos.HORAS_REENTREGA_AUTO
DIAS_UTEIS_NO_POOL = 1
DIAS_UTEIS_CLIENTE = 2
HORA_LIMITE_RASCUNHO = time(19, 0)  # só do pedido segurado (véspera do prazo final)
HORA_LIMITE_ROTA = time(5, 30)      # rascunho não enviado: vence 05h30 do dia da rota
LOGISTICA, ATENDIMENTO, EXPEDICAO = "Logística", "Atendimento", "Expedição"


def eh_dia_util(d: date) -> bool:
    return feriados.eh_dia_util(d)


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


def prazo(estado: str, desde: datetime, *, data_rascunho: date | None = None,
          prazo_segurado: date | None = None, prazo_dias_regiao: int | None = None) -> datetime | None:
    """Quando o prazo do estado vence (None = estado sem prazo).
    `prazo_segurado`: prazo final de entrega do pedido segurado de
    propósito pela regra de rota fraca (roteirizacao/rotas_fracas.py).
    `prazo_dias_regiao`: dias corridos do nível da região de dia fixo
    (semanal 7, quinzenal 15 -- roteirizacao/regioes_dia_fixo.py), só pra
    pedido sem agendamento."""
    if estado == SEM_SERVICO:
        return desde + timedelta(hours=HORAS_SEM_SERVICO)
    if estado == NO_POOL:
        if prazo_segurado:
            return datetime.combine(ultimo_dia_util_antes(prazo_segurado), HORA_LIMITE_RASCUNHO)
        if prazo_dias_regiao:
            return desde + timedelta(days=prazo_dias_regiao)
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
        return datetime.combine(data_rascunho, HORA_LIMITE_ROTA)
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
        if acao == "ignorado_retirada":
            # Retirada no galpão nunca sobe pra Vuupt (06/11): não é "sem serviço".
            return AGUARDANDO_RETIRADA, p.get("obs_pipeline") or "retirada no galpão"
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


def acao_e_dono(estado: str, motivo: str | None = None) -> tuple[str, str]:
    """(o que fazer, quem cuida) de um pedido no estado `estado`. `motivo` é o
    texto gravado em vigia_pedidos: no insucesso é o motivo do motorista
    ("Não coletado"...), no sem serviço começa pela ação do pipeline
    ("pulado_cancelado_vuupt -- ...")."""
    m = (motivo or "").strip().lower()
    if estado == ROTA_PASSADA:
        return "Confirmar a baixa com o motorista (Entregue ou Sem sucesso)", LOGISTICA
    if estado == INSUCESSO:
        if m.startswith("não coletado") or m.startswith("nao coletado"):
            return "Criar a reentrega: a carga ficou no galpão", LOGISTICA
        return "Decidir com o embarcador: reentregar ou devolver", ATENDIMENTO
    if estado == SEM_SERVICO:
        if m.startswith("pulado_cancelado_vuupt"):
            return "Recriar o serviço na Vuupt ou cancelar na Stokki", ATENDIMENTO
        return "Corrigir o motivo e reimportar o pedido", LOGISTICA
    if estado in (NO_POOL, EM_RASCUNHO):
        return "Roteirizar: pôr em rota ou enviar o rascunho", LOGISTICA
    if estado == RASCUNHO_COM_ERRO:
        return "Corrigir o erro e enviar o rascunho de novo", LOGISTICA
    if estado == AGUARDANDO_CLIENTE:
        return "Cobrar a data de agendamento do embarcador", ATENDIMENTO
    if estado == RECUSADO:
        return "Cancelar e devolver o pedido na Stokki", ATENDIMENTO
    if estado == AGUARDANDO_RETIRADA:
        return "Acompanhar a retirada (Coletas)", EXPEDICAO
    if estado == ENTREGUE_NAO_EXPEDIDO:
        return "Expedir o pedido na Stokki", EXPEDICAO
    return "Acompanhar", LOGISTICA


def vencido(vence_em: datetime | None, agora: datetime) -> bool:
    return vence_em is not None and agora >= vence_em
