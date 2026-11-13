# -*- coding: utf-8 -*-
"""Executor da fila de devolucao de estoque: pega o pedido PENDENTE, monta o plano (admin, so leitura),
move pelo provider e avisa. Regras puras, leituras, plano e a fila ficam em stokki/devolucao_estoque.py.

Movimento nao confirmado (adjust_id sem confirmacao, ou marcado `incerto`) nunca e repetido: o pedido vai
a DUVIDA e a operacao confere na Stokki. Erro tecnico (sessao caiu etc.) conta tentativa e volta pra fila.
"""
import contextlib
import json
import logging
import os
from datetime import datetime

from stokki import contas, sessao_uso
from stokki.devolucao_estoque import garantir_tabela, gravar, ligado, montar_plano, pendentes

logger = logging.getLogger(__name__)

MAX_TENTATIVAS = 3          # so para erro tecnico; conta ocupada nao conta
ESPERA_CONTA_SEGUNDOS = 20  # espera curta: o ciclo do worker nao pode ficar preso
MAX_POR_CICLO = 5
SEM_PLANO_DE_MOVER = ("NADA", "JA_DEVOLVIDO", "ANTIGO")
TOL_QTD = 0.001


class ContaOcupada(RuntimeError):
    """A conta da Stokki (admin) esta em uso por outro: o pedido espera o proximo ciclo, sem gastar tentativa."""


class _ErroTecnico(Exception):
    """Resultado tecnico do mover (sessao caiu, tela fora do ar): conta tentativa e o pedido volta pra fila."""


def _dono_provider() -> str:
    """Um dono por processo: o `adquirir` ignora o proprio dono, entao dono fixo deixaria dois processos
    executarem o mesmo pedido."""
    return f"devolucao-estoque-{os.getpid()}"


def _int_id(v):
    if isinstance(v, bool):
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _movs(item) -> list:
    """Movimentos gravados de um item (os proprios dicts, para poder marcar `avisado`)."""
    if not isinstance(item, dict):
        return []
    lista = [m for m in (item.get("movimentos") or []) if isinstance(m, dict)]
    if item.get("adjust_id") is not None:   # item gravado com o id no proprio item
        lista.append({"adjust_id": item["adjust_id"], "movido": item.get("movido") or 0})
    return lista


def _confirmado(m) -> bool:
    return (m.get("movido") or 0) > 0


def _incertos(itens) -> list:
    """[(item, movimento)] que a Stokki pode ter gravado sem confirmar: marcado `incerto`, ou com adjust_id e movido 0."""
    return [(i, m) for i in (itens if isinstance(itens, list) else []) for m in _movs(i)
            if m.get("incerto") or (m.get("adjust_id") is not None and not _confirmado(m))]


def _texto_incertos(incertos) -> str:
    partes = []
    for item, m in incertos:
        t = (f"movimentacao {m['adjust_id']} enviada sem confirmacao" if m.get("adjust_id") is not None
             else f"movimentacao do produto {item.get('sku', '?')} enviada sem resposta clara da Stokki")
        if t not in partes:
            partes.append(t)
    return "; ".join(partes) + "; conferir na Stokki antes de qualquer nova devolucao"


def _ids_dos_itens(itens) -> set:
    """adjust_id (int) dos movimentos CONFIRMADOS gravados em uma lista de itens da fila. Id nao confirmado
    nunca entra (nao prova devolucao); id em texto e normalizado."""
    ids = set()
    for item in itens if isinstance(itens, list) else []:
        for m in _movs(item):
            n = _int_id(m.get("adjust_id"))
            if n is not None and _confirmado(m):
                ids.add(n)
    return ids


def adjust_ids_do_robo(conn, exceto_id_stokki: int) -> frozenset:
    """adjust_id das movimentacoes confirmadas que o proprio robo fez em OUTROS pedidos (qualquer status
    da fila): nao provam devolucao deste pedido."""
    garantir_tabela(conn)
    ids = set()
    for (texto,) in conn.execute("SELECT itens_json FROM stokki_devolucoes WHERE id_stokki != ?",
                                 (int(exceto_id_stokki),)).fetchall():
        try:
            ids |= _ids_dos_itens(json.loads(texto or "[]"))
        except ValueError:
            continue
    return frozenset(ids)


def _cliente_dos_itens(itens) -> int:
    for i in itens if isinstance(itens, list) else []:
        if isinstance(i, dict) and _int_id(i.get("cliente_id")):
            return int(i["cliente_id"])
    return 0


def _itens_da_fila(itens_plano: list, anteriores: list, cliente_id: int) -> list:
    """Itens do plano com o que tentativas anteriores deste pedido ja moveram (por produto)."""
    antes = {i.get("product_id"): i for i in anteriores if isinstance(i, dict)}
    saida = []
    for it in itens_plano:
        movs = list((antes.get(it["product_id"]) or {}).get("movimentos") or [])
        saida.append({"product_id": it["product_id"], "sku": it["sku"], "nome": it.get("nome", ""),
                      "qtd": it["qtd"], "situacao": it["situacao"], "destinos": [list(d) for d in it["destinos"]],
                      "cliente_id": cliente_id, "movimentos": movs,
                      "movido": sum(m.get("movido") or 0 for m in movs)})
    return saida


def _avisar(funcao, *args) -> bool:
    """Aviso nunca desfaz o status ja gravado nem derruba a fila. True se saiu."""
    try:
        funcao(*args)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"devolucao_estoque: aviso falhou (PS-{args[0] if args else '?'}): {e}")
        return False


def _resumo(itens_fila: list) -> str:
    """O que foi movido e o que ficou no Virtual, por produto."""
    partes = []
    for i in itens_fila:
        if not isinstance(i, dict) or i.get("qtd") is None or i.get("situacao", "mover") not in ("mover", "sem_destino"):
            continue
        movido = sum(m.get("movido") or 0 for m in _movs(i))
        t = f"{i.get('sku', '?')}: movido {movido:g} de {i['qtd']:g}"
        if movido < i["qtd"] - TOL_QTD:
            erros = "; ".join(sorted({m["erro"] for m in _movs(i) if m.get("erro")}))
            t += f" (ficaram {i['qtd'] - movido:g} no Virtual" + (f"; {erros}" if erros else "") + ")"
        partes.append(t)
    return " | ".join(partes)


def _encerrar(conn, id_stokki, cliente_id, status, itens, motivo, avisar_galpao, avisar_operacao,
              avisar_op=True, tentativa=False) -> None:
    """Status final (FEITA, PARCIAL, DUVIDA, FALHA): grava, avisa o galpao do que foi movido e ainda nao foi
    avisado (marca `avisado` nos movimentos) e, fora FEITA, a operacao com o que foi movido e o que ficou."""
    texto = motivo
    if any(_movs(i) for i in itens):
        texto = f"{motivo} | {_resumo(itens)}" if _resumo(itens) else motivo
    gravar(conn, id_stokki, status=status, itens=itens, erro=texto if status in ("DUVIDA", "FALHA") else None,
           mais_uma_tentativa=tentativa)
    novos = [(i, m) for i in itens for m in _movs(i) if _confirmado(m) and not m.get("avisado")]
    if novos:
        feitos = [{"sku": i["sku"], "nome": i.get("nome", ""), "movido": m["movido"], "destino": m["destino"]}
                  for i, m in novos]
        if _avisar(avisar_galpao, id_stokki, cliente_id, feitos):
            for _, m in novos:
                m["avisado"] = True
            gravar(conn, id_stokki, itens=itens)
    if avisar_op:
        _avisar(avisar_operacao, id_stokki, cliente_id, texto)


def _provider_ocupada_por_outro() -> bool:
    try:
        return bool(sessao_uso.ocupante_externo(contas.PROVIDER))
    except Exception as e:  # noqa: BLE001 -- nao saber nao impede
        logger.warning(f"devolucao_estoque: nao consegui consultar a ocupacao da provider: {e}")
        return False


def _linha_mudou(conn, id_stokki, row) -> bool:
    """Outro processo pegou o pedido entre o plano e a provider: status mudou ou itens gravados mudaram."""
    atual = conn.execute("SELECT status, itens_json FROM stokki_devolucoes WHERE id_stokki = ?",
                         (int(id_stokki),)).fetchone()
    return atual is None or atual[0] != "PENDENTE" or (atual[1] or None) != (row.get("itens_json") or None)


def _processar_um(conn, config, row, simular, abrir_admin, mover, avisar_galpao, avisar_operacao, agora, cont) -> str:
    """Um pedido da fila. Devolve 'ok', 'ocupada' (conta em uso: quem chama para o ciclo) ou 'mudou' (outro
    processo mexeu na linha: pula este pedido). Erro tecnico sobe como excecao."""
    id_stokki = row["id_stokki"]
    anteriores = row["itens"] = row.get("itens") or []
    cliente_id = row["cliente_id"] = _cliente_dos_itens(anteriores)
    incertos = _incertos(anteriores)
    if incertos:   # nunca repetir o que pode ja ter sido movido: sem plano, sem sessao
        motivo = _texto_incertos(incertos)
        if simular:
            logger.info(f"devolucao_estoque SIMULACAO: PS-{id_stokki} iria a DUVIDA: {motivo}")
            return "ok"
        _encerrar(conn, id_stokki, cliente_id, "DUVIDA", anteriores, motivo, avisar_galpao, avisar_operacao)
        cont["falhas"] += 1
        return "ok"
    if _provider_ocupada_por_outro():
        logger.info(f"devolucao_estoque: conta provider em uso por outro, PS-{id_stokki} espera o proximo ciclo")
        return "ocupada"
    ignorar = adjust_ids_do_robo(conn, id_stokki) | _ids_dos_itens(anteriores)
    pilha = contextlib.ExitStack()
    try:
        sess = pilha.enter_context(abrir_admin())
    except ContaOcupada as e:
        logger.info(f"devolucao_estoque: conta admin ocupada, PS-{id_stokki} espera o proximo ciclo ({e})")
        return "ocupada"
    with pilha:   # fecha a admin antes de pegar a provider
        plano = montar_plano(sess, id_stokki, agora, bool(row.get("ja_estava")), ignorar)
    cliente_id = row["cliente_id"] = plano["cliente_id"]
    status = plano["status"]
    itens_fila = row["itens"] = _itens_da_fila(plano["itens"], anteriores, cliente_id)
    if simular:
        resumo = "; ".join(f"{i['sku']} qtd {i['qtd']:g} {i['situacao']} -> {i['destinos']}" for i in itens_fila)
        logger.info(f"devolucao_estoque SIMULACAO: PS-{id_stokki} plano {status} ({plano['motivo']}) {resumo}")
        return "ok"
    if status in SEM_PLANO_DE_MOVER:
        gravar(conn, id_stokki, status=status, itens=itens_fila)
        cont["nada"] += 1
        return "ok"
    if status != "MOVER":   # DUVIDA
        _encerrar(conn, id_stokki, cliente_id, "DUVIDA", itens_fila, plano["motivo"], avisar_galpao, avisar_operacao)
        cont["falhas"] += 1
        return "ok"
    movimentos = [{"product_id": i["product_id"], "sku": i["sku"], "qtd": i["qtd"] - i["movido"],
                   "destinos": list(p["destinos"])}
                  for i, p in zip(itens_fila, plano["itens"])
                  if i["situacao"] == "mover" and i["qtd"] - i["movido"] > TOL_QTD]
    rodada = []
    if movimentos:
        por_produto = {i["product_id"]: i for i in itens_fila}
        aplicados = set()

        def aplicar(r):
            if id(r) in aplicados:
                return
            aplicados.add(id(r))
            rodada.append(r)
            item = por_produto[r["product_id"]]
            mov = {"destino": r.get("destino"), "pedido": r.get("pedido"), "movido": r.get("movido") or 0,
                   "adjust_id": _int_id(r.get("adjust_id")), "erro": r.get("erro") or ""}
            for marca in ("incerto", "tecnico"):
                if r.get(marca):
                    mov[marca] = True
            item["movimentos"].append(mov)
            item["movido"] = sum(m.get("movido") or 0 for m in item["movimentos"])
            gravar(conn, id_stokki, itens=itens_fila)   # a cada movimento: nova tentativa nao repete o feito

        dono = _dono_provider()
        conta = sessao_uso.adquirir(dono, ttl_segundos=600, esperar_segundos=ESPERA_CONTA_SEGUNDOS,
                                    conta=contas.PROVIDER)
        if not conta:
            logger.info(f"devolucao_estoque: conta provider ocupada, PS-{id_stokki} espera o proximo ciclo")
            return "ocupada"
        try:
            if _linha_mudou(conn, id_stokki, row):
                logger.info(f"devolucao_estoque: PS-{id_stokki} mudou na fila entre o plano e a provider; nao move")
                return "mudou"
            resultados = mover(config, cliente_id, movimentos, f"Retorno ao estoque do PS-{id_stokki} cancelado",
                               ao_concluir=aplicar)
        finally:
            sessao_uso.liberar(dono)
        for r in resultados or []:
            aplicar(r)
    incertos = _incertos(itens_fila)
    passou = [i["sku"] for i in itens_fila if i["movido"] > i["qtd"] + TOL_QTD]
    if incertos or passou:
        motivo = (_texto_incertos(incertos) if incertos
                  else f"moveu mais que o bloqueio do pedido ({', '.join(passou)}): conferir na Stokki")
        _encerrar(conn, id_stokki, cliente_id, "DUVIDA", itens_fila, motivo, avisar_galpao, avisar_operacao)
        cont["falhas"] += 1
        return "ok"
    tecnicos = [r for r in rodada if r.get("tecnico")]
    if tecnicos:
        raise _ErroTecnico(tecnicos[0].get("erro") or "erro tecnico ao mover")
    completo = all(i["movido"] >= i["qtd"] - TOL_QTD for i in itens_fila if i["situacao"] in ("mover", "sem_destino"))
    if completo:
        _encerrar(conn, id_stokki, cliente_id, "FEITA", itens_fila, "devolucao feita", avisar_galpao,
                  avisar_operacao, avisar_op=False)
        cont["feitas"] += 1
    else:
        _encerrar(conn, id_stokki, cliente_id, "PARCIAL", itens_fila, "devolucao parcial", avisar_galpao,
                  avisar_operacao)
        cont["parciais"] += 1
    return "ok"


def processar_pendentes(conn, config, *, simular: bool, abrir_admin, mover, avisar_galpao, avisar_operacao,
                        agora=None) -> dict:
    """Atende a fila (ate MAX_POR_CICLO pedidos por chamada). `abrir_admin()` e um gerenciador de contexto
    que entrega a sessao admin ou levanta ContaOcupada se a conta esta ocupada (qualquer outro erro e tecnico).
    Conta ocupada (admin ou provider) = pedido continua PENDENTE sem gastar tentativa e o ciclo para. Erro
    tecnico: conta tentativa; na MAX_TENTATIVAS vira FALHA com um aviso. Um pedido com erro nunca impede os
    outros. Plano DUVIDA e movimentacao sem confirmacao contam em `falhas` (pedem a operacao). Em simulacao
    nada e gravado, nem em excecao."""
    cont = {"feitas": 0, "parciais": 0, "nada": 0, "falhas": 0, "esperando": 0}
    if not ligado(config):
        return cont
    agora = agora or datetime.now()
    for row in pendentes(conn)[:MAX_POR_CICLO]:
        id_stokki = row["id_stokki"]
        try:
            res = _processar_um(conn, config, row, simular, abrir_admin, mover, avisar_galpao, avisar_operacao,
                                agora, cont)
            if res == "ocupada":
                cont["esperando"] += 1
                break   # conta ocupada vale para os proximos tambem
            if res == "mudou":
                cont["esperando"] += 1
        except Exception as e:  # noqa: BLE001 -- um pedido nunca derruba a fila
            logger.error(f"devolucao_estoque: PS-{id_stokki} falhou: {e}")
            if simular:
                continue
            try:
                tentativa = (row.get("tentativas") or 0) + 1
                if tentativa >= MAX_TENTATIVAS:
                    msg = (f"devolucao falhou {tentativa} vezes ({e}); conferir na Stokki o que ja foi movido "
                           f"(fila stokki_devolucoes, PS-{id_stokki})")
                    _encerrar(conn, id_stokki, row.get("cliente_id") or 0, "FALHA", row.get("itens") or [], msg,
                              avisar_galpao, avisar_operacao, tentativa=True)
                    cont["falhas"] += 1
                else:
                    gravar(conn, id_stokki, erro=str(e), mais_uma_tentativa=True)
            except Exception as e2:  # noqa: BLE001
                logger.error(f"devolucao_estoque: nao gravou a falha de PS-{id_stokki}: {e2}")
    return cont
