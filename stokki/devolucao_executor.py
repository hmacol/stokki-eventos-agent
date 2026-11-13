# -*- coding: utf-8 -*-
"""Executor da fila de devolucao de estoque: pega o pedido PENDENTE, monta o plano (admin, so leitura),
move pelo provider e avisa. Regras puras, leituras, plano e a fila ficam em stokki/devolucao_estoque.py."""
import contextlib
import json
import logging
from datetime import datetime

from stokki import contas, sessao_uso
from stokki.devolucao_estoque import garantir_tabela, gravar, ligado, montar_plano, pendentes

logger = logging.getLogger(__name__)


# --- Executor: pega a fila, monta o plano, move e avisa ---

MAX_TENTATIVAS = 3          # so para erro tecnico; conta ocupada nao conta
ESPERA_CONTA_SEGUNDOS = 20  # espera curta: o ciclo do worker nao pode ficar preso
MAX_POR_CICLO = 5
DONO_PROVIDER = "devolucao-estoque"
SEM_PLANO_DE_MOVER = ("NADA", "JA_DEVOLVIDO", "ANTIGO")
TOL_QTD = 0.001


def _ids_dos_itens(itens) -> set:
    """adjust_id (int) de todos os movimentos gravados em uma lista de itens da fila."""
    ids = set()
    for item in itens if isinstance(itens, list) else []:
        for m in (item.get("movimentos") or []) if isinstance(item, dict) else []:
            if isinstance(m, dict) and isinstance(m.get("adjust_id"), int):
                ids.add(m["adjust_id"])
    return ids


def adjust_ids_do_robo(conn, exceto_id_stokki: int) -> frozenset:
    """adjust_id das movimentacoes que o proprio robo fez em OUTROS pedidos (qualquer status da fila):
    nao provam devolucao deste pedido."""
    garantir_tabela(conn)
    ids = set()
    for (texto,) in conn.execute("SELECT itens_json FROM stokki_devolucoes WHERE id_stokki != ?",
                                 (int(exceto_id_stokki),)).fetchall():
        try:
            ids |= _ids_dos_itens(json.loads(texto or "[]"))
        except ValueError:
            continue
    return frozenset(ids)


def _itens_da_fila(itens_plano: list, anteriores: list) -> list:
    """Itens do plano com o que tentativas anteriores deste pedido ja moveram (por produto)."""
    antes = {i.get("product_id"): i for i in anteriores if isinstance(i, dict)}
    saida = []
    for it in itens_plano:
        movs = list((antes.get(it["product_id"]) or {}).get("movimentos") or [])
        saida.append({"product_id": it["product_id"], "sku": it["sku"], "nome": it.get("nome", ""),
                      "qtd": it["qtd"], "situacao": it["situacao"], "destinos": [list(d) for d in it["destinos"]],
                      "movimentos": movs, "movido": sum(m.get("movido") or 0 for m in movs)})
    return saida


def _avisar(funcao, *args) -> None:
    """Aviso nunca desfaz o status ja gravado nem derruba a fila."""
    try:
        funcao(*args)
    except Exception as e:  # noqa: BLE001
        logger.error(f"devolucao_estoque: aviso falhou (PS-{args[0] if args else '?'}): {e}")


def _resumo_parcial(itens_fila: list) -> str:
    partes = []
    for i in itens_fila:
        if i["situacao"] in ("mover", "sem_destino") and i["movido"] < i["qtd"] - TOL_QTD:
            erros = "; ".join(sorted({m["erro"] for m in i["movimentos"] if m.get("erro")}))
            partes.append(f"{i['sku']}: ficaram {i['qtd'] - i['movido']:g} de {i['qtd']:g} no Virtual"
                          + (f" ({erros})" if erros else ""))
    return "devolucao parcial: " + " | ".join(partes)


def _processar_um(conn, config, row, simular, abrir_admin, mover, avisar_galpao, avisar_operacao, agora, cont) -> bool:
    """Um pedido da fila. True = conta ocupada (nada gravado, quem chama para o ciclo). Erro tecnico sobe."""
    id_stokki = row["id_stokki"]
    anteriores = row.get("itens") or []
    ignorar = adjust_ids_do_robo(conn, id_stokki) | _ids_dos_itens(anteriores)
    pilha = contextlib.ExitStack()
    try:
        sess = pilha.enter_context(abrir_admin())
    except RuntimeError as e:
        logger.info(f"devolucao_estoque: conta admin ocupada, PS-{id_stokki} espera o proximo ciclo ({e})")
        return True
    with pilha:   # fecha a admin antes de pegar a provider
        plano = montar_plano(sess, id_stokki, agora, bool(row.get("ja_estava")), ignorar)
    cliente_id, status = plano["cliente_id"], plano["status"]
    row["cliente_id"] = cliente_id   # para o aviso de falha tecnica, se vier depois
    itens_fila = _itens_da_fila(plano["itens"], anteriores)
    if simular:
        resumo = "; ".join(f"{i['sku']} qtd {i['qtd']:g} {i['situacao']} -> {i['destinos']}" for i in itens_fila)
        logger.info(f"devolucao_estoque SIMULACAO: PS-{id_stokki} plano {status} ({plano['motivo']}) {resumo}")
        return False
    if status in SEM_PLANO_DE_MOVER:
        gravar(conn, id_stokki, status=status, itens=itens_fila)
        cont["nada"] += 1
        return False
    if status != "MOVER":   # DUVIDA
        gravar(conn, id_stokki, status="DUVIDA", itens=itens_fila, erro=plano["motivo"])
        cont["falhas"] += 1
        _avisar(avisar_operacao, id_stokki, cliente_id, plano["motivo"])
        return False
    movimentos = [{"product_id": i["product_id"], "sku": i["sku"], "qtd": i["qtd"] - i["movido"],
                   "destinos": list(p["destinos"])}
                  for i, p in zip(itens_fila, plano["itens"])
                  if i["situacao"] == "mover" and i["qtd"] - i["movido"] > TOL_QTD]
    if movimentos:
        por_produto = {i["product_id"]: i for i in itens_fila}
        aplicados = set()

        def aplicar(r):
            if id(r) in aplicados:
                return
            aplicados.add(id(r))
            item = por_produto[r["product_id"]]
            item["movimentos"].append({k: r.get(k) for k in ("destino", "pedido", "movido", "adjust_id", "erro")})
            item["movido"] = sum(m.get("movido") or 0 for m in item["movimentos"])
            gravar(conn, id_stokki, itens=itens_fila)   # a cada movimento: nova tentativa nao repete o feito

        conta = sessao_uso.adquirir(DONO_PROVIDER, ttl_segundos=600, esperar_segundos=ESPERA_CONTA_SEGUNDOS,
                                    conta=contas.PROVIDER)
        if not conta:
            logger.info(f"devolucao_estoque: conta provider ocupada, PS-{id_stokki} espera o proximo ciclo")
            return True
        try:
            resultados = mover(config, cliente_id, movimentos, f"Retorno ao estoque do PS-{id_stokki} cancelado",
                               ao_concluir=aplicar)
        finally:
            sessao_uso.liberar(DONO_PROVIDER)
        for r in resultados or []:
            aplicar(r)
    sem_confirmacao = [m["adjust_id"] for i in itens_fila for m in i["movimentos"]
                       if m.get("adjust_id") and not (m.get("movido") or 0)]
    passou = [i["sku"] for i in itens_fila if i["movido"] > i["qtd"] + TOL_QTD]
    if sem_confirmacao or passou:
        motivo = (f"movimentacao {', '.join(str(a) for a in sem_confirmacao)} enviada mas sem confirmacao; "
                  "conferir na Stokki antes de repetir" if sem_confirmacao
                  else f"moveu mais que o bloqueio do pedido ({', '.join(passou)}): conferir na Stokki")
        gravar(conn, id_stokki, status="DUVIDA", itens=itens_fila, erro=motivo)
        cont["falhas"] += 1
        _avisar(avisar_operacao, id_stokki, cliente_id, motivo)
        return False
    feitos = [{"sku": i["sku"], "nome": i["nome"], "movido": m["movido"], "destino": m["destino"]}
              for i in itens_fila for m in i["movimentos"] if (m.get("movido") or 0) > 0]
    completo = all(i["movido"] >= i["qtd"] - TOL_QTD for i in itens_fila if i["situacao"] in ("mover", "sem_destino"))
    gravar(conn, id_stokki, status="FEITA" if completo else "PARCIAL", itens=itens_fila)
    if feitos:
        _avisar(avisar_galpao, id_stokki, cliente_id, feitos)
    if completo:
        cont["feitas"] += 1
    else:
        cont["parciais"] += 1
        _avisar(avisar_operacao, id_stokki, cliente_id, _resumo_parcial(itens_fila))
    return False


def processar_pendentes(conn, config, *, simular: bool, abrir_admin, mover, avisar_galpao, avisar_operacao,
                        agora=None) -> dict:
    """Atende a fila (ate MAX_POR_CICLO pedidos por chamada). `abrir_admin()` e um gerenciador de contexto
    que entrega a sessao admin ou levanta RuntimeError se a conta esta ocupada. Conta ocupada (admin ou
    provider) = pedido continua PENDENTE sem gastar tentativa e o ciclo para. Erro tecnico: conta tentativa;
    na MAX_TENTATIVAS vira FALHA com um aviso. Um pedido com erro nunca impede os outros. Plano DUVIDA e
    movimentacao sem confirmacao contam em `falhas` (pedem a operacao)."""
    cont = {"feitas": 0, "parciais": 0, "nada": 0, "falhas": 0, "esperando": 0}
    if not ligado(config):
        return cont
    agora = agora or datetime.now()
    for row in pendentes(conn)[:MAX_POR_CICLO]:
        id_stokki = row["id_stokki"]
        try:
            if _processar_um(conn, config, row, simular, abrir_admin, mover, avisar_galpao, avisar_operacao,
                             agora, cont):
                cont["esperando"] += 1
                break   # conta ocupada vale para os proximos tambem
        except Exception as e:  # noqa: BLE001 -- um pedido nunca derruba a fila
            logger.error(f"devolucao_estoque: PS-{id_stokki} falhou: {e}")
            try:
                tentativa = (row.get("tentativas") or 0) + 1
                if tentativa >= MAX_TENTATIVAS:
                    msg = (f"devolucao falhou {tentativa} vezes ({e}); conferir na Stokki o que ja foi movido "
                           f"(fila stokki_devolucoes, PS-{id_stokki})")
                    gravar(conn, id_stokki, status="FALHA", erro=str(e), mais_uma_tentativa=True)
                    cont["falhas"] += 1
                    _avisar(avisar_operacao, id_stokki, row.get("cliente_id") or 0, msg)
                else:
                    gravar(conn, id_stokki, erro=str(e), mais_uma_tentativa=True)
            except Exception as e2:  # noqa: BLE001
                logger.error(f"devolucao_estoque: nao gravou a falha de PS-{id_stokki}: {e2}")
    return cont
