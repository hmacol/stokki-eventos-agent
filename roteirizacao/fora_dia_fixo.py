# -*- coding: utf-8 -*-
"""
fora_dia_fixo.py

Data do embarcador fora do dia de visita da região (Hugo, 03/10/2026 --
spec docs/superpowers/specs/2026-10-03-dias-fixos-v2-design.md, seção 5):
a data do cliente manda, o pedido vira DEDICADO (sai da rota compartilhada
pelo fluxo de roteirizacao/dedicados.py) com o valor da calculadora de
frete dedicado, e o embarcador é avisado uma vez (notificar_fora_dia_fixo.py).

Quem chama: criar_rotas_diarias.main e incrementar_rotas.main, logo depois
de aplicar_regioes_dia_fixo e antes de separar_dedicados
(tratar_fora_dia_fixo -- nunca levanta).

Data "do cliente" = scheduled_start sem linha DIA_FIXO nem EQUIPE em
agendamentos_origem (registro_dia_fixo.py) -- decisão do Hugo, 03/10: data
posta pela equipe no Planejamento não vira dedicado. Edição direta na tela
da Vuupt não passa pelo sistema e conta como do cliente. Pedido criado antes
de DETECCAO_A_PARTIR_DE fica de fora: o dia fixo antigo gravou datas que a
tabela não conhece (ABCD quarta/sexta, Transfrios segunda/quarta).

Km do valor: linha reta (haversine) da base ao destino e volta. A Google
Routes devolve 403 desde 30/09 (faturamento fechado), então nem é chamada;
o valor sai menor que o rodoviário e o financeiro corrige.
"""
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import km_rodoviario  # noqa: E402
import pedidos_dedicados  # noqa: E402
import registro_dia_fixo  # noqa: E402
from regioes_dia_fixo import data_valida_na_regiao, regra_dia_fixo_do_servico  # noqa: E402
from roteirizacao_dados import coordenada_embutida, extrair_volume_caixas  # noqa: E402
from rotas_fracas import data_entrada  # noqa: E402
from regras.tipo_carga_embarcador import carregar_tipos_carga_por_sender, classificar_tipo_carga  # noqa: E402
from nucleo.normalizacao import vuupt_para_local  # noqa: E402

logger = logging.getLogger(__name__)

# Segunda do deploy (Hugo): pedido criado antes não é marcado.
DETECCAO_A_PARTIR_DE = date(2026, 10, 5)
POR = pedidos_dedicados.POR_FORA_DIA_FIXO
POR_VALOR_PENDENTE = POR + " (valor pendente)"
# reagendado pela pagina de ciencia ha menos que isso: nao remarca (a rodada pode ter
# buscado os servicos antes do reagendamento e ainda ver a data velha)
SEM_REMARCAR_MIN = 60


def data_agendada(servico: dict) -> date | None:
    """Dia de Brasilia do scheduled_start. A Vuupt devolve sem fuso e em UTC
    ("2026-10-08 01:00:00" = quarta 07/10 22:00); offset explicito e respeitado."""
    try:
        return date.fromisoformat(str(vuupt_para_local(servico.get("scheduled_start")) or "")[:10])
    except ValueError:
        return None


def detectar(servico: dict, hoje: date, conn_registro) -> tuple[dict, date] | None:
    """(regra, data) quando a data do pedido é do cliente (não foi gravada
    pelo dia fixo nem pela equipe) e cai fora do dia de visita da região;
    None caso contrário."""
    data = data_agendada(servico)
    if not data or data < hoje:
        return None
    entrada = data_entrada(servico)
    if not entrada or entrada < DETECCAO_A_PARTIR_DE:
        return None
    regra = regra_dia_fixo_do_servico(servico)
    if not regra or data_valida_na_regiao(regra, data):
        return None
    if registro_dia_fixo.data_nao_e_do_cliente(conn_registro, servico, data):
        return None
    return regra, data


def calcular_valor(servico: dict, config: dict, tipos_carga: dict) -> float | None:
    """Total da calculadora de frete dedicado (portal_cliente/cotacao.py):
    base -> destino -> base em linha reta, caixas do pedido, tipo de carga do
    embarcador. None quando não dá pra calcular (sem coordenada, carga acima
    da tabela, entrada inválida, erro inesperado -- nunca derruba a rodada)."""
    from portal_cliente import cotacao
    coords = coordenada_embutida(servico)
    if not coords:
        return None
    try:
        regras = cotacao.regras_de(config)
        trajeto = km_rodoviario.calcular_trajeto(tuple(regras["origem_coords"]), [coords], None,
                                                 voltar=bool(regras.get("considerar_retorno", True)))
        if trajeto is None:
            return None
        tipo, _ = classificar_tipo_carga(servico.get("sender_id"), tipos_carga)
        r = cotacao.calcular({"caixas": extrair_volume_caixas(servico), "peso_kg": 0, "tipo_carga": tipo.upper(),
                              "urgente": False, "valor_nf": None, "km_total": trajeto.km_total, "pedagio": None},
                             regras)
    except cotacao.ErroCotacao as e:
        logger.info(f"  {servico.get('code')}: calculadora de frete dedicado sem valor ({e})")
        return None
    except Exception as e:
        logger.warning(f"  {servico.get('code')}: erro na calculadora de frete dedicado ({e}); valor pendente")
        return None
    return r["total"]


def carregar_tipos_carga(db_path) -> dict:
    """{sender_id: tipo de carga}; falha -> {} (valor sai como carga seca).
    Pública: o Planejamento usa no aviso de reagendamento (Task 4A)."""
    try:
        return carregar_tipos_carga_por_sender(db_path)
    except Exception as e:
        logger.warning(f"nao carregou o tipo de carga dos embarcadores ({e}); valor sai como carga seca")
        return {}


def _nomes_remetentes(db_path) -> dict:
    try:
        import preferencias_notificacao
        return {k: v.get("nome") for k, v in
                preferencias_notificacao.carregar_embarcadores("agendamento", db_path=db_path).items()}
    except Exception as e:
        logger.warning(f"nao carregou o nome dos remetentes ({e}); dedicado sai sem o nome")
        return {}


def marcar_fora_dia_fixo(servicos: list[dict], config: dict, hoje: date | None = None,
                         db_path=None) -> list[dict]:
    """Marca como dedicado cada pedido detectado. Pedido que já teve dedicado
    (qualquer código do serviço ou o mesmo serviço), ativo OU removido pela
    equipe, não é remarcado; removido pela página de ciência (embarcador
    reagendou) só volta a ser marcado depois de SEM_REMARCAR_MIN. Devolve os
    marcados nesta rodada: [{"servico", "regra", "data", "valor", "valor_pendente"}]."""
    hoje = hoje or date.today()
    db = db_path or pedidos_dedicados.DB_PATH
    tipos = carregar_tipos_carga(db)
    nomes = _nomes_remetentes(db)
    marcados: list[dict] = []
    conn_reg = registro_dia_fixo.conectar(db)
    conn_ded = pedidos_dedicados.conectar(db)
    try:
        # linha removida tambem conta: "Remover dedicado" da equipe nao e desfeito
        # na rodada seguinte. Codigo combinado grava so o primeiro: o servico
        # tambem conta, senao o mesmo servico com outro codigo virava 2a linha
        ja_marcados, servicos_marcados = pedidos_dedicados.codigos_e_servicos_ja_marcados(conn_ded)
        desde = (datetime.now() - timedelta(minutes=SEM_REMARCAR_MIN)).strftime("%Y-%m-%d %H:%M:%S")
        reagendados, servicos_reagendados = pedidos_dedicados.removidos_pela_ciencia_desde(conn_ded, desde)
        ja_marcados |= reagendados
        servicos_marcados |= servicos_reagendados
        for s in servicos:
            codigos = pedidos_dedicados.codigos_do_servico(s)
            if not codigos or any(c in ja_marcados for c in codigos) or s.get("id") in servicos_marcados:
                continue
            achado = detectar(s, hoje, conn_reg)
            if not achado:
                continue
            regra, data = achado
            valor = calcular_valor(s, config, tipos)
            por = POR if valor is not None else POR_VALOR_PENDENTE
            pedidos_dedicados.marcar(conn_ded, [{
                "codigo_pedido": codigos[0], "service_id": s.get("id"), "sender_id": s.get("sender_id"),
                "remetente_nome": nomes.get(s.get("sender_id")),
            }], valor or 0.0, por)
            ja_marcados.add(codigos[0])
            if s.get("id") is not None:
                servicos_marcados.add(s.get("id"))
            logger.info(f"  {s.get('code')}: data {data:%d/%m} fora dos dias de "
                        f"{regra.get('regiao') or regra['nome']} -- marcado como dedicado "
                        f"({'valor pendente' if valor is None else f'R$ {valor:.2f}'}).")
            marcados.append({"servico": s, "regra": regra, "data": data, "valor": valor or 0.0,
                             "valor_pendente": valor is None})
    finally:
        conn_reg.close()
        conn_ded.close()
    return marcados


def pendentes_de_aviso(servicos: list[dict], hoje: date | None = None, db_path=None) -> list[dict]:
    """Pedidos da lista marcados por ESTA regra e ainda não avisados
    (inclui os de rodadas anteriores cujo aviso falhou). Data agendada já
    passada fica de fora: não adianta avisar depois do dia."""
    hoje = hoje or date.today()
    db = db_path or pedidos_dedicados.DB_PATH
    conn_reg = registro_dia_fixo.conectar(db)
    conn_ded = pedidos_dedicados.conectar(db)
    try:
        ativos = pedidos_dedicados.ativos_por_codigo(conn_ded)
        itens = []
        for s in servicos:
            linha = next((ativos[c] for c in pedidos_dedicados.codigos_do_servico(s) if c in ativos), None)
            por = str((linha or {}).get("marcado_por") or "")
            if not por.startswith(POR) or registro_dia_fixo.ja_avisado(conn_reg, s):
                continue
            regra, data = regra_dia_fixo_do_servico(s), data_agendada(s)
            if not regra or not data or data < hoje:
                continue
            itens.append({"servico": s, "regra": regra, "data": data, "valor": float(linha["valor"] or 0.0),
                          "valor_pendente": por == POR_VALOR_PENDENTE})
        return itens
    finally:
        conn_reg.close()
        conn_ded.close()


ORFAO_HORAS = 48   # marcado ha mais que isso sem ciencia nao e reaberto (pedido anterior a chave)


def _orfaos_sem_ciencia(servicos: list[dict], conn_cd, hoje: date, db_path=None) -> list[dict]:
    """Pedidos marcados por ESTA regra nas ultimas 48h, ainda ativos e sem nenhuma
    ciencia (a abertura falhou numa rodada anterior). Mesmo item do pendentes_de_aviso."""
    import ciencia_dedicado
    limite = (datetime.now() - timedelta(hours=ORFAO_HORAS)).strftime("%Y-%m-%d %H:%M:%S")
    conn_ded = pedidos_dedicados.conectar(db_path or pedidos_dedicados.DB_PATH)
    try:
        ativos = pedidos_dedicados.ativos_por_codigo(conn_ded)
        itens = []
        for s in servicos:
            linha = next((ativos[c] for c in pedidos_dedicados.codigos_do_servico(s) if c in ativos), None)
            por = str((linha or {}).get("marcado_por") or "")
            if not por.startswith(POR) or str(linha["marcado_em"] or "") < limite:
                continue
            regra, data = regra_dia_fixo_do_servico(s), data_agendada(s)
            if not regra or not data or data < hoje or ciencia_dedicado.existe_para_servico(conn_cd, s):
                continue
            itens.append({"servico": s, "regra": regra, "data": data, "valor": float(linha["valor"] or 0.0),
                          "valor_pendente": por == POR_VALOR_PENDENTE})
        return itens
    finally:
        conn_ded.close()


def _abrir_ciencias(marcados: list[dict], config: dict, db_path=None, servicos: list[dict] | None = None,
                    hoje: date | None = None) -> dict:
    """Abre a ciencia de cada pedido marcado nesta rodada (mais os orfaos recentes de
    rodadas anteriores) e, se abriu alguma, dispara a rotina de envio so dessas (o
    primeiro aviso sai agora, respeitando a janela; lembretes, encerramentos e alerta
    da equipe ficam pro timer). Falha num item e logada e nao para os outros."""
    import preferencias_notificacao
    import notificar_ciencia_dedicado
    import ciencia_dedicado
    try:
        embarcadores = preferencias_notificacao.carregar_embarcadores("agendamento", db_path=db_path)
    except Exception as e:
        embarcadores = {}
        logger.warning(f"Ciencia do dedicado: CNPJ dos embarcadores indisponivel ({e}); abrindo sem CNPJ.")
    abertas = falhas = 0
    ids_abertos: set[int] = set()
    try:
        conn = ciencia_dedicado.conectar(db_path or ciencia_dedicado.DB_PATH)
    except Exception as e:
        logger.error(f"Ciencia do dedicado: banco indisponivel, {len(marcados)} pedido(s) marcado(s) sem ciencia: {e}")
        return {"ciencias": 0, "ciencias_falhas": max(len(marcados), 1)}
    try:
        itens = list(marcados)
        try:
            ids = {i["servico"].get("id") for i in marcados}
            itens += [o for o in _orfaos_sem_ciencia(servicos or [], conn, hoje or date.today(), db_path=db_path)
                      if o["servico"].get("id") not in ids]
        except Exception as e:
            logger.error(f"Ciencia do dedicado: falha ao procurar pedidos marcados sem ciencia: {e}")
        for item in itens:
            try:
                cnpj = (embarcadores.get(item["servico"].get("sender_id")) or {}).get("cnpj")
                ciencia_id = ciencia_dedicado.abrir(conn, item, cnpj, datetime.now())
                if ciencia_id:
                    abertas += 1
                    ids_abertos.add(ciencia_id)
            except Exception as e:
                falhas += 1
                logger.error(f"Ciencia do dedicado: falha ao abrir a ciencia de {item['servico'].get('code')}: {e}")
    finally:
        conn.close()
    if abertas:
        notificar_ciencia_dedicado.executar(config, db_path=db_path, somente_ids=ids_abertos)
    logger.info(f"Fora do dia fixo: {len(marcados)} marcado(s) como dedicado; {abertas} ciencia(s) aberta(s), "
                f"{falhas} falha(s).")
    return {"ciencias": abertas, **({"ciencias_falhas": falhas} if falhas else {})}


def tratar_fora_dia_fixo(servicos: list[dict], config: dict, modo_teste: bool = False,
                         hoje: date | None = None, db_path=None) -> dict:
    """Ponto de chamada do criar_rotas_diarias / incrementar_rotas: marca os
    pedidos novos e avisa os pendentes (inclusive de rodadas anteriores).
    Em modo teste só loga os candidatos. Nunca levanta; se o aviso falhar
    depois da marcação, o resumo mantém "marcados" junto com "erro"."""
    resumo: dict = {}
    try:
        hoje = hoje or date.today()
        if modo_teste:
            conn = registro_dia_fixo.conectar(db_path or registro_dia_fixo.DB_PATH)
            try:
                achados = [(s, *a) for s in servicos if (a := detectar(s, hoje, conn))]
            finally:
                conn.close()
            for s, regra, data in achados:
                logger.info(f"[MODO TESTE] {s.get('code')}: data {data:%d/%m} fora dos dias de "
                            f"{regra.get('regiao') or regra['nome']} -- seria marcado como dedicado (não marcado).")
            return {"marcados": 0, "candidatos": len(achados)}
        marcados = marcar_fora_dia_fixo(servicos, config, hoje=hoje, db_path=db_path)
        resumo["marcados"] = len(marcados)
        import ciencia_dedicado
        if ciencia_dedicado.cfg(config)["ativo"]:
            # fluxo novo: o pedido marcado agora abre ciencia e o aviso unico antigo nao sai
            return {"marcados": len(marcados), **_abrir_ciencias(marcados, config, db_path, servicos, hoje)}
        import notificar_fora_dia_fixo
        avisos = notificar_fora_dia_fixo.avisar(pendentes_de_aviso(servicos, hoje=hoje, db_path=db_path), config,
                                                db_path=db_path)
        if marcados or avisos.get("registrados") or avisos.get("falhas"):
            logger.info(f"Fora do dia fixo: {len(marcados)} marcado(s) como dedicado; avisos {avisos}.")
        return {"marcados": len(marcados), "avisos": avisos}
    except Exception as e:
        logger.error(f"Falha na regra de data fora do dia fixo (não afeta a roteirização): {e}")
        return {**resumo, "erro": str(e)}
