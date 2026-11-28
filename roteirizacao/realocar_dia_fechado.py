# -*- coding: utf-8 -*-
"""
realocar_dia_fechado.py

Cliente que nao recebe em certos dias da semana (ajustes_complexidade_cliente.
dias_semana, modal "Nivel / horario" do /planejamento). Decisoes do Hugo, 27/11:
  - pedido que cai num dia fechado e REALOCADO pra data mais proxima em que o
    cliente recebe (dia util, e dentro do dia fixo da regiao, quando houver);
  - a data que o CLIENTE informou vence: nao mexe (so o selo na tela) e a rota
    sai normal nesse dia. Data gravada por nos (automatica, dia fixo, equipe =
    registro_dia_fixo.ORIGENS_NAO_CLIENTE) ou pedido sem data: realoca;
  - ninguem e avisado: a data nova fica com origem AUTOMATICO e vai pro log.
Quem chama: criar_rotas_diarias / incrementar_rotas (rodadas) e o salvar do
modal (planejamento_rotas.realocar_pedidos_do_cliente).
"""
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
for _p in (str(_RAIZ), str(Path(__file__).parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from nucleo.normalizacao import vuupt_para_local  # noqa: E402
from regras import feriados  # noqa: E402
from regras.complexidade_entrega import NOMES_DIAS, dias_efetivos, _so_digitos  # noqa: E402
import registro_dia_fixo  # noqa: E402
from regioes_dia_fixo import data_valida_na_regiao, regra_dia_fixo_do_servico  # noqa: E402

logger = logging.getLogger(__name__)

# Regiao quinzenal + cliente que so recebe 1 dia pode empurrar a data pra longe.
HORIZONTE_DIAS = 35
DIA_TODO = ("00:00", "23:59")


def documento(servico: dict) -> str:
    return _so_digitos((servico.get("customer") or {}).get("code", ""))


def _local(ts) -> datetime | None:
    texto = vuupt_para_local(ts)
    try:
        return datetime.strptime(texto, "%Y-%m-%d %H:%M:%S") if texto else None
    except ValueError:
        return None


def data_agendada(servico: dict) -> date | None:
    """Data do scheduled_start em hora LOCAL (a Vuupt devolve UTC sem fuso)."""
    dt = _local(servico.get("scheduled_start"))
    return dt.date() if dt else None


def dia_do_pedido(servico: dict, data_alvo: date) -> date:
    """Dia em que o pedido sairia se roteirizado em `data_alvo`: a propria data
    agendada quando ela e futura; senao `data_alvo` (sem data ou atrasado,
    mesma regra de roteirizacao_dados.elegivel_para_data)."""
    ag = data_agendada(servico)
    return ag if ag and ag > data_alvo else data_alvo


def fechado_no_dia(servico: dict, dia: date, ajustes: dict) -> bool:
    return dia.weekday() not in dias_efetivos(documento(servico), ajustes)


def texto_dia_fechado(servico: dict, dia: date, ajustes: dict) -> str:
    recebe = ", ".join(NOMES_DIAS[d][:3] for d in dias_efetivos(documento(servico), ajustes))
    return f"Não recebe {NOMES_DIAS[dia.weekday()].lower()} (recebe: {recebe})"


def data_e_do_cliente(servico: dict, dia: date, conn) -> bool:
    """O pedido esta agendado pra `dia` e essa data NAO foi gravada por nos."""
    return data_agendada(servico) == dia and not registro_dia_fixo.data_nao_e_do_cliente(conn, servico, dia)


def nova_data(servico: dict, dia: date, ajustes: dict, hoje: date | None = None) -> date | None:
    """Primeira data a partir de `dia` (nunca hoje nem antes) em que o cliente
    recebe, que e dia util e, se o endereco tem dia fixo de regiao, e dia de
    visita. None se nao achar em HORIZONTE_DIAS."""
    hoje = hoje or date.today()
    dias = dias_efetivos(documento(servico), ajustes)
    regra = regra_dia_fixo_do_servico(servico)
    base = max(dia, hoje + timedelta(days=1))
    for i in range(HORIZONTE_DIAS):
        d = base + timedelta(days=i)
        if d.weekday() in dias and feriados.eh_dia_util(d) and (regra is None or data_valida_na_regiao(regra, d)):
            return d
    return None


def janela(servico: dict, ajustes: dict) -> tuple[str, str]:
    """Mantem o horario do agendamento atual; sem ele, o horario do cliente
    (ajuste manual); sem nada, o dia todo."""
    ini, fim = _local(servico.get("scheduled_start")), _local(servico.get("scheduled_end"))
    if ini and fim and ini.strftime("%H:%M") < fim.strftime("%H:%M"):
        return ini.strftime("%H:%M"), fim.strftime("%H:%M")
    aj = ajustes.get(documento(servico)) or {}
    a, b = aj.get("horario_atendimento_inicio"), aj.get("horario_atendimento_fim")
    if a and b and a < b:
        return a, b
    return DIA_TODO


def classificar(servicos: list[dict], data_alvo: date, ajustes: dict, conn) -> tuple[list, list, list]:
    """(livres, realocar, mantidos). livres: cliente recebe no dia do pedido.
    mantidos: dia fechado, mas a data e do cliente (vence, sai normal).
    realocar: dia fechado e data nossa ou sem data."""
    livres, realocar, mantidos = [], [], []
    for s in servicos:
        dia = dia_do_pedido(s, data_alvo)
        if not fechado_no_dia(s, dia, ajustes):
            livres.append(s)
        elif data_e_do_cliente(s, dia, conn):
            mantidos.append(s)
        else:
            realocar.append(s)
    return livres, realocar, mantidos


def reagendar(vuupt, servico: dict, data: date, ajustes: dict, modo_teste: bool = False,
              db_path=None) -> None:
    """Grava a data nova na Vuupt (mesma janela) e registra a origem AUTOMATICO.
    modo_teste: nao grava nada."""
    from vuupt_client import _converter_data_para_iso
    ini, fim = janela(servico, ajustes)
    inicio = _converter_data_para_iso(f"{data.isoformat()} {ini}:00")
    termino = _converter_data_para_iso(f"{data.isoformat()} {fim}:00")
    if not inicio or not termino:
        raise ValueError(f"janela invalida {data} {ini}-{fim}")
    if modo_teste:
        return
    from nucleo import pool as nucleo_pool
    if nucleo_pool.eh_sem_servico(servico["id"]):
        # pool direto da Stokki: pedido sem servico na Vuupt (id sintetico) -- a data vai pras colunas do
        # nucleo como edicao local (o pipeline nao regrava; a materializacao manda pra Vuupt). Nunca PUT com
        # id negativo. Linha que nao e pedido sem servico legitimo levanta ValueError (fica no pool, operacao).
        from nucleo import pedidos as nucleo_pedidos
        from nucleo.normalizacao import para_local
        nucleo_pedidos.editar_sem_servico(servico["id"], {"agendamento_inicio": para_local(inicio),
                                                          "agendamento_fim": para_local(termino)})
    else:
        vuupt.atualizar_servico(servico["id"], {"scheduled_start": inicio, "scheduled_end": termino})
    registro_dia_fixo.registrar_origens([{"servico": servico, "data": data}], registro_dia_fixo.ORIGEM_AUTOMATICO,
                                        **({"db_path": db_path} if db_path else {}))


def tirar_de_rascunhos(service_id: int, hoje: date | None = None) -> list[str]:
    """Tira o pedido realocado das rotas ainda nao enviadas (rascunho/ofertada)
    de hoje em diante. Enviada nao aparece aqui: o pedido dela nao esta no pool
    da rodada. Devolve os nomes das rotas de onde saiu."""
    import rascunhos_rota
    conn = rascunhos_rota._conectar()
    try:
        rows = conn.execute(
            "SELECT r.id, r.nome FROM rascunhos_parada p JOIN rascunhos_rota r ON r.id = p.rascunho_id "
            "WHERE p.service_id = ? AND r.data_alvo >= ? AND r.status IN (?, ?)",
            (service_id, (hoje or date.today()).isoformat(),
             rascunhos_rota.STATUS_RASCUNHO, rascunhos_rota.STATUS_OFERTADA)).fetchall()
    finally:
        conn.close()
    for rid, _nome in rows:
        rascunhos_rota.remover_parada(rid, service_id)
    return [nome for _rid, nome in rows]


def realocar_lote(servicos: list[dict], data_alvo: date, ajustes: dict, vuupt, modo_teste: bool = False,
                  hoje: date | None = None, db_path=None) -> tuple[list[dict], dict]:
    """Rodada automatica: devolve (os que seguem na roteirizacao, resumo).
    Realocados (ou que nao deu pra realocar) ficam fora da rota de data_alvo."""
    if not any(aj.get("dias_semana") for aj in ajustes.values()):
        return servicos, {"realocados": [], "mantidos": [], "sem_data": [], "falhas": []}
    conn = registro_dia_fixo.conectar(**({"db_path": db_path} if db_path else {}))
    try:
        livres, realocar, mantidos = classificar(servicos, data_alvo, ajustes, conn)
    finally:
        conn.close()
    resumo = {"realocados": [], "mantidos": [s.get("code") for s in mantidos], "sem_data": [], "falhas": []}
    for s in realocar:
        dia = dia_do_pedido(s, data_alvo)
        nova = nova_data(s, dia, ajustes, hoje)
        if nova is None:
            resumo["sem_data"].append(s.get("code"))
            logger.warning(f"  {s.get('code')}: cliente nao recebe {NOMES_DIAS[dia.weekday()].lower()} e nao achei "
                           f"data valida em {HORIZONTE_DIAS} dias -- fica no pool.")
            continue
        try:
            reagendar(vuupt, s, nova, ajustes, modo_teste, db_path)
        except Exception as e:
            resumo["falhas"].append({"codigo": s.get("code"), "erro": str(e)})
            logger.error(f"  {s.get('code')}: falha ao realocar pra {nova:%d/%m} ({e}) -- fica no pool.")
            continue
        rotas = []
        if not modo_teste:
            try:
                rotas = tirar_de_rascunhos(s["id"], hoje)
            except Exception as e:
                logger.error(f"  {s.get('code')}: reagendado, mas nao saiu do rascunho ({e}).")
        resumo["realocados"].append({"codigo": s.get("code"), "de": dia.isoformat(), "para": nova.isoformat(),
                                     **({"rotas": rotas} if rotas else {})})
        logger.info(f"  {s.get('code')}: cliente nao recebe {NOMES_DIAS[dia.weekday()].lower()} -> "
                    f"{'(teste) realocaria' if modo_teste else 'realocado'} pra {nova:%d/%m}.")
    if realocar or mantidos:
        logger.info(f"Dias fechados do cliente: {len(resumo['realocados'])} realocado(s), {len(mantidos)} mantido(s) "
                    f"(data do cliente), {len(resumo['sem_data'])} sem data valida, {len(resumo['falhas'])} falha(s).")
    return livres + mantidos, resumo
