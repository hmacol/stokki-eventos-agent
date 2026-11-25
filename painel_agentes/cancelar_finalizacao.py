# -*- coding: utf-8 -*-
"""
cancelar_finalizacao.py

Botao "Cancelar finalizacao" da tela /acompanhamento (pedido do Hugo, 22/11):
o motorista deu a baixa errada e precisa refazer do inicio.

A Vuupt nao reabre servico concluido: o caminho de la e DUPLICAR o servico
(servico novo, com recreated_order_origin_id apontando pro original). Entao:

  1. duplica o servico na Vuupt (a mesma duplicar_servico_por_insucesso da
     Torre e do fluxo de insucesso: codigo ganha -R1, -R2...);
  2. inclui o servico novo na MESMA rota da Vuupt;
  3. cria a parada nova no nucleo, limpa (PENDENTE): o motorista ve no nosso
     app e refaz a baixa; a ponte repete no servico novo. Rota feita no app
     da Vuupt (liberado em 24/11): o servico novo aparece no app da Vuupt e
     a sincronizacao atualiza a parada nova.

A parada antiga NAO e mexida: fica com o resultado, os horarios e as fotos
da primeira baixa (e o historico da tentativa), aqui e na Vuupt. Quem le a
Vuupt (Torre, portal, expedicao na Stokki) continua vendo o resultado antigo
no codigo original -- cancelar a finalizacao nao desfaz e-mail ja enviado
nem expedicao ja feita.

Travas (decisoes do Hugo): so com a rota em andamento, sempre com motivo,
uma vez por baixa. Vale pro nosso app e pro app da Vuupt (24/11).

As chamadas a Vuupt entram por parametro (duplicar/adicionar) pra testar sem
rede; quem liga as de verdade e a rota Flask em painel_agentes.py.
"""
import json
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from nucleo import banco  # noqa: E402
from nucleo.normalizacao import normalizar_codigo  # noqa: E402
from nucleo.rotas import registrar_evento  # noqa: E402

NIVEIS = ("total", "operador", "logistica")
EVENTO = "FINALIZACAO_CANCELADA"
MOTIVO_MINIMO = 5
_FINALIZADAS = (banco.PARADA_ENTREGUE, banco.PARADA_PARCIAL, banco.PARADA_INSUCESSO)
# O que a parada nova herda da antiga: o que identifica a entrega, nunca o
# resultado nem os horarios.
_COPIA = ("rota_id", "titulo", "destinatario_nome", "endereco", "latitude", "longitude", "sender_id",
          "remetente_nome", "nivel_dificuldade", "volume_caixas", "janela_inicio", "janela_fim", "customer_id")


class Recusado(Exception):
    """Nao da pra cancelar a finalizacao; a mensagem vai pra tela."""


def _id_do_servico(novo: dict):
    for origem in (novo, novo.get("service"), novo.get("data")):
        if isinstance(origem, dict) and origem.get("id"):
            return int(origem["id"])
    return None


def _contar(conn, rota_id: int) -> None:
    r = conn.execute("""
        SELECT COUNT(*) FILTER (WHERE situacao != 'CANCELADA') AS total,
               COUNT(*) FILTER (WHERE situacao IN ('ENTREGUE', 'PARCIAL')) AS entregues,
               COUNT(*) FILTER (WHERE situacao = 'INSUCESSO') AS insucessos
        FROM nucleo_paradas WHERE rota_id = ?""", (rota_id,)).fetchone()
    conn.execute("UPDATE nucleo_rotas SET total_paradas = ?, entregues = ?, insucessos = ?, atualizado_em = ? WHERE id = ?",
                 (r["total"], r["entregues"], r["insucessos"], banco.agora(), rota_id))


def canceladas(conn, rota_id: int) -> set:
    """ids das paradas cuja finalizacao ja foi cancelada (o botao some nelas)."""
    return {r["parada_id"] for r in conn.execute(
        "SELECT parada_id FROM nucleo_eventos INDEXED BY idx_nucleo_eventos_rota WHERE rota_id = ? AND tipo = ?",
        (rota_id, EVENTO))}


def cancelar_finalizacao(conn, parada_id: int, usuario: str, motivo: str, *, vuupt, duplicar, adicionar,
                         ja_duplicado, marcar_duplicado) -> dict:
    """
    duplicar(vuupt, servico_original) -> servico novo (dict com "code") ou None
    adicionar(route_id, [service_id]) -> {"ok", "erro"?, "falharam"}
    ja_duplicado(service_id) -> codigo novo se esse servico ja foi duplicado, senao None
    marcar_duplicado(service_id, novo_code)
    Levanta Recusado com a mensagem pra tela.
    """
    motivo = (motivo or "").strip()
    if len(motivo) < MOTIVO_MINIMO:
        raise Recusado("Escreva o motivo do cancelamento da finalização.")
    p = conn.execute("SELECT * FROM nucleo_paradas WHERE id = ?", (parada_id,)).fetchone()
    if p is None:
        raise Recusado("Parada não encontrada.")
    rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (p["rota_id"],)).fetchone()
    if rota["status"] in (banco.ROTA_CONCLUIDA, banco.ROTA_CANCELADA):
        raise Recusado("Só dá pra cancelar a finalização com a rota em andamento.")
    if not p["service_id"] or not rota["vuupt_route_id"]:
        raise Recusado("Essa parada não tem serviço numa rota da Vuupt: não dá pra cancelar a finalização por aqui.")
    feito = ja_duplicado(p["service_id"])
    if feito:
        raise Recusado(f"Esse pedido já foi duplicado antes ({normalizar_codigo(feito) or feito}): a nova baixa é nele.")

    bruto = vuupt.buscar_servico_por_id(p["service_id"])
    original = (bruto.get("service", bruto) if isinstance(bruto, dict) else None) or None
    if not original:
        raise Recusado("A Vuupt não devolveu o serviço agora. Tente de novo em instantes.")
    # Baixa pelo app da Vuupt (24/11): o nucleo so fica sabendo na sincronizacao (15-30 min),
    # entao a baixa e a rota em andamento tambem valem pelo que a Vuupt diz ao vivo.
    if p["situacao"] not in _FINALIZADAS and original.get("status") != "done":
        raise Recusado("Essa parada ainda não foi finalizada.")
    if rota["status"] != banco.ROTA_EM_ROTA and original.get("status") != "done":
        raise Recusado("Só dá pra cancelar a finalização com a rota em andamento.")
    if int(original.get("route_id") or 0) != int(rota["vuupt_route_id"]):
        raise Recusado("Na Vuupt esse serviço está em outra rota: confira por lá antes de cancelar a finalização.")

    novo = duplicar(vuupt, original)
    novo_id = _id_do_servico(novo) if novo else None
    if not novo or not novo_id:
        raise Recusado("A Vuupt não criou o serviço novo. Nada foi alterado; tente de novo.")
    novo_code = novo.get("code") or ""
    codigo = normalizar_codigo(novo_code) or novo_code
    marcar_duplicado(p["service_id"], novo_code)   # o servico novo ja existe: nunca duplicar por cima

    inclusao = adicionar(int(rota["vuupt_route_id"]), [novo_id])
    na_rota = bool(inclusao.get("ok")) and not inclusao.get("falharam")
    dados = {"usuario": usuario, "motivo": motivo, "codigo": p["codigo"], "service_id": p["service_id"],
             "resultado_anterior": p["situacao"], "concluida_em": p["completed_at"],
             "motivo_anterior": p["motivo_texto"], "novo_codigo": codigo, "novo_service_id": novo_id}
    if not na_rota:
        registrar_evento(conn, EVENTO, banco.ORIGEM_PAINEL, rota_id=rota["id"], parada_id=p["id"],
                         agent_id=rota["agent_id"], dados={**dados, "fora_da_rota": inclusao.get("erro") or "recusado"})
        conn.commit()
        raise Recusado(f"O serviço novo {codigo} foi criado na Vuupt, mas ficou fora da rota "
                       f"({inclusao.get('erro') or 'a Vuupt recusou incluir'}). Inclua na rota pela Vuupt ou cancele o serviço.")

    ordem = conn.execute("SELECT COALESCE(MAX(ordem), 0) + 1 AS prox FROM nucleo_paradas WHERE rota_id = ?",
                         (rota["id"],)).fetchone()["prox"]
    colunas = list(_COPIA) + ["ordem", "service_id", "codigo", "situacao", "dados_json"]
    valores = [p[c] for c in _COPIA] + [ordem, novo_id, codigo, banco.PARADA_PENDENTE,
                                        json.dumps({"refaz_parada_id": p["id"]})]
    nova_id = conn.execute(
        f"INSERT INTO nucleo_paradas ({', '.join(colunas)}) VALUES ({', '.join('?' * len(colunas))})", valores).lastrowid
    registrar_evento(conn, EVENTO, banco.ORIGEM_PAINEL, rota_id=rota["id"], parada_id=p["id"],
                     agent_id=rota["agent_id"], dados={**dados, "nova_parada_id": nova_id})
    _contar(conn, rota["id"])
    conn.commit()
    return {"ok": True, "novo_codigo": codigo, "nova_parada_id": nova_id}
