# -*- coding: utf-8 -*-
"""
nucleo/ofertas_rota_enviada.py

Rota ja enviada fica aberta pros outros motoristas em dois casos, e so neles
(Hugo, 21/11: nunca por tempo ou horario):
  RECUSA  o motorista recusou no app          -> publica na rodada seguinte
  EQUIPE  a logistica clicou "Ofertar" no card da rota enviada (planejamento)

Publicada, a rota aparece na aba Ofertas do app pra TODO motorista com login
(menos o original) e sai um aviso no grupo "Rotas FRESHLOG" com entregas,
regiao, saida, valor aproximado e o link que abre o app.

Ajustes do Hugo (18/11):
- O aviso vai SEMPRE pro grupo "Rotas FRESHLOG" (achado pelo nome no gateway;
  grupo_id no config so troca o destino). Sem o grupo, o aviso espera.
- Sem filtro de elegiveis na publicacao: a equipe seleciona quando confirma
  (troca na Vuupt).
- Unica trava: veiculo menor que o da rota (ex.: Fiorino em rota VAN/HR) nao
  escolhe, e o app mostra o motivo.

Decisoes do Hugo (17/11):
- So pelo app: quem nao tem login nao ve nem escolhe.
- Na RECUSA quem recusou deixa de ver a rota. Na oferta da EQUIPE o motorista
  original continua com a rota ate alguem pegar e a equipe confirmar.
- A escolha NAO troca o motorista sozinha: a equipe e avisada e troca na
  Vuupt; quando o sync ve a rota com o motorista que escolheu, a oferta vira
  APLICADA.
- Valor aproximado = tabela de tarifas (veiculo + km), como no financeiro.

Liga em api_motorista.rota_sem_aceite:
    ativo: true
    todos: false          # false = so motoristas da ponte (api_motorista.ponte_vuupt.agentes)
    grupo_id: "...@g.us"  # vazio = grupo "Rotas FRESHLOG", achado pelo nome
    link: "https://app.freshhub.com.br/motorista/abrir/ofertas"

No app a oferta reaproveita a tela e os endpoints do marketplace: o id dela
e ID_BASE + rota_id (o marketplace usa o id do rascunho, sempre bem menor).

Uso (timer stokki-ofertar-rotas-sem-aceite, a cada 2 min):
    venv/bin/python nucleo/ofertas_rota_enviada.py [--modo-teste] [--situacao]
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from nucleo import banco, ponte_vuupt

logger = logging.getLogger(__name__)

ID_BASE = 1_000_000_000

MOTIVO_RECUSA = "RECUSA"
MOTIVO_PRAZO = "PRAZO"     # historico: ate 21/11 a rota abria sozinha as 05h30
MOTIVO_EQUIPE = "EQUIPE"
AGENDADA, ABERTA, ESCOLHIDA, APLICADA, RETIRADA = "AGENDADA", "ABERTA", "ESCOLHIDA", "APLICADA", "RETIRADA"

LINK_PADRAO = "https://app.freshhub.com.br/motorista/abrir/ofertas"
GRUPO_ROTAS = "Rotas FRESHLOG"
# Pagina do link (Hugo, 19/11): coleta sempre na base; pagamento ainda sem regra no sistema.
LOCAL_COLETA = ("Base Freshlog - Rua Zilda, 288, Portão Lateral Ouro Grosso, Casa Verde Alta, "
                "São Paulo-SP, 02545-000")
TEXTO_PAGAMENTO = "Alinhar no WhatsApp."
JANELA_TARDE = "12:00"   # entrega com janela abrindo a partir daqui ganha alerta na pagina
MAX_MENSAGEM = 200
# Etapas da mensagem particular: minutos contados do aviso no grupo (aviso_grupo_em).
ETAPAS = ("PERTO_DO_FIM", "FIXO", "ESPORADICO")
PRIMEIRA_ETAPA_MIN = 15
ENTRE_ETAPAS_MIN = 10
PERTO_DO_FIM_PARADAS = 2        # ate tantas paradas pendentes na rota de hoje
MAX_MENSAGENS_RODADA = 5        # tentativas de envio por rodada: o gateway espera 20 s entre envios e a rodada e de 2 em 2 min
MAX_TENTATIVAS = 3              # falha de envio pro mesmo motorista: depois disso desiste dele
_SITUACAO_TENTA_DE_NOVO = ("falhou", "indeterminado")
_SITUACAO_GRAVA = ("enviado", "sem_telefone", "numero_sem_whatsapp", *_SITUACAO_TENTA_DE_NOVO)
_PARADA_ABERTA = (banco.PARADA_PENDENTE, banco.PARADA_EM_DESLOCAMENTO, banco.PARADA_EM_ROTA)


def config_ofertas(config: dict | None = None) -> dict:
    projeto = config if config is not None else ponte_vuupt._config_projeto()
    bruto = (projeto.get("api_motorista") or {}).get("rota_sem_aceite") or {}
    return {
        "ativo": bool(bruto.get("ativo")),
        "todos": bool(bruto.get("todos")),
        "agentes": ponte_vuupt.config_ponte(projeto)["agentes"],
        "grupo_id": bruto.get("grupo_id") or None,
        "link": bruto.get("link") or LINK_PADRAO,
        "segredo": (projeto.get("api_motorista") or {}).get("secret_key") or None,   # assina o link de cada oferta
    }


def _tem_tabela(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nucleo_ofertas_rota'").fetchone() is not None


def oferta_da_rota(conn: sqlite3.Connection, rota_id: int):
    if not _tem_tabela(conn):
        return None
    return conn.execute("SELECT * FROM nucleo_ofertas_rota WHERE rota_id = ?", (rota_id,)).fetchone()


# ── Gatilhos ──────────────────────────────────────────────────────────────────

def _recusou(conn: sqlite3.Connection, rota) -> bool:
    ultimo = conn.execute(
        "SELECT tipo FROM nucleo_eventos WHERE rota_id = ? AND agent_id = ? AND tipo IN ('ROTA_ACEITA', 'ROTA_RECUSADA') "
        "ORDER BY id DESC LIMIT 1", (rota["id"], rota["agent_id"])).fetchone()
    return bool(ultimo and ultimo["tipo"] == "ROTA_RECUSADA")


def candidatas(conn: sqlite3.Connection, cfg: dict, agora: datetime) -> list[tuple[sqlite3.Row, str]]:
    """Rotas de HOJE, da Vuupt, com motorista, ainda planejadas e nunca
    ofertadas, que ele recusou. Nada abre por horario."""
    saida = []
    for rota in conn.execute("""
            SELECT r.* FROM nucleo_rotas r
            WHERE r.data_rota = ? AND r.provedor = ? AND r.status = ? AND r.agent_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM nucleo_ofertas_rota o WHERE o.rota_id = r.id)
            ORDER BY r.id""", (agora.date().isoformat(), banco.PROVEDOR_VUUPT, banco.ROTA_PLANEJADA)).fetchall():
        if not (cfg["todos"] or rota["agent_id"] in cfg["agentes"]):
            continue
        if _recusou(conn, rota):
            saida.append((rota, MOTIVO_RECUSA))
    return saida


# ── Resumo, valor e elegiveis ─────────────────────────────────────────────────

def _paradas(conn: sqlite3.Connection, rota_id: int) -> list[dict]:
    return [dict(p) for p in conn.execute(
        "SELECT * FROM nucleo_paradas WHERE rota_id = ? AND situacao != ? ORDER BY ordem",
        (rota_id, banco.PARADA_CANCELADA)).fetchall()]


def valor_aproximado(conn: sqlite3.Connection, rota, tipo_veiculo: str | None) -> float | None:
    """Mesma conta do extrato do motorista (nucleo/financeiro.linha_extrato)."""
    from regras import km_cobrado, tarifa_motorista
    km = km_cobrado.calcular_km_cobrado(dict(rota), _paradas(conn, rota["id"])).km
    r = tarifa_motorista.calcular_valor_rota(tipo_veiculo, km, tarifa_motorista.carregar_tarifas(conn))
    return r.valor_total if r else None


def _brl(valor: float) -> str:
    return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _tipo_veiculo_do(conn: sqlite3.Connection, agent_id) -> str | None:
    m = conn.execute("SELECT tipo_veiculo FROM motoristas WHERE agent_id = ?", (agent_id,)).fetchone()
    return m["tipo_veiculo"] if m else None


def montar_resumo(conn: sqlite3.Connection, rota, gmaps_key: str | None) -> dict:
    """Resumo do marketplace (regiao, cidades, bairros, caixas, peso real) --
    nunca a rota inteira -- mais a saida."""
    if str(_RAIZ / "roteirizacao") not in sys.path:
        sys.path.insert(0, str(_RAIZ / "roteirizacao"))
    from regras.resumo_oferta import montar_resumo as resumo_marketplace
    resumo = resumo_marketplace(_paradas(conn, rota["id"]), gmaps_key)
    resumo.pop("paradas_por_nivel", None)
    resumo.pop("peso_paradas_com_dado", None)
    resumo["saida"] = str(rota["start_at"] or "")[11:16] or None
    return resumo


def motoristas_com_app(conn: sqlite3.Connection, rota) -> list[int]:
    """Quem ve a oferta: todo motorista com login ativo, menos o original
    (Hugo, 18/11: a selecao e da equipe, na confirmacao). Fica gravado em
    elegiveis_json so como registro."""
    return [m["agent_id"] for m in conn.execute(
        "SELECT agent_id FROM motoristas WHERE ativo = 1 AND agent_id IS NOT NULL AND agent_id != ? ORDER BY agent_id",
        (rota["agent_id"] or 0,))]


def motivo_veiculo(conn: sqlite3.Connection, rota, agent_id: int) -> str | None:
    """Veiculo menor que o da rota nao pega (Hugo, 18/11: Fiorino em rota de
    VAN/HR). Rota comum (sem tipo) aceita qualquer um. Devolve o motivo
    pro app ou None."""
    from regras.tipo_veiculo import tipo_por_codigo
    precisa = tipo_por_codigo(rota["tipo_veiculo"])
    if precisa is None:
        return None
    tem = tipo_por_codigo(_tipo_veiculo_do(conn, agent_id))
    if tem is None:
        return f"Essa rota é para {precisa.nome} e seu cadastro está sem veículo. Fale com a operação."
    if tem.volume_maximo_cx < precisa.volume_maximo_cx:
        return (f"Essa rota é para {precisa.nome} e seu veículo é {tem.nome}: a carga não cabe. "
                "Escolha uma rota do porte do seu carro.")
    return None


# ── Publicar e manter ─────────────────────────────────────────────────────────

def publicar(conn: sqlite3.Connection, rota, motivo: str, elegiveis: list[int], resumo: dict,
             publicar_em: str | None = None) -> bool:
    """Sem commit. False se a rota ja tinha oferta (outra rodada chegou antes).
    `publicar_em` (AAAA-MM-DD HH:MM:SS): fica AGENDADA e so vira ABERTA nessa hora."""
    cur = conn.execute("""
        INSERT OR IGNORE INTO nucleo_ofertas_rota (rota_id, data_rota, agent_original, motivo, elegiveis_json, resumo_json,
                                                   criado_em, atualizado_em, status, publicar_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (rota["id"], rota["data_rota"], rota["agent_id"], motivo, json.dumps(sorted(set(elegiveis))),
          json.dumps(resumo, ensure_ascii=False, default=str), banco.agora(), banco.agora(),
          AGENDADA if publicar_em else ABERTA, publicar_em))
    return cur.rowcount == 1


def _retirar(conn: sqlite3.Connection, oferta_id: int, motivo: str) -> None:
    conn.execute("UPDATE nucleo_ofertas_rota SET status = ?, motivo_retirada = ?, atualizado_em = ? WHERE id = ?",
                 (RETIRADA, motivo, banco.agora(), oferta_id))


def abrir_agendadas(conn: sqlite3.Connection, agora: datetime) -> int:
    """AGENDADA que chegou a hora vira ABERTA (o aviso do grupo sai logo depois, na mesma rodada).
    Rota que ficou inelegivel nesse meio tempo e retirada. Commita."""
    n = 0
    for o in conn.execute("""
            SELECT o.id, o.publicar_em, r.status AS rota_status, r.agent_id AS rota_agente, o.agent_original
            FROM nucleo_ofertas_rota o LEFT JOIN nucleo_rotas r ON r.id = o.rota_id
            WHERE o.status = ? AND o.publicar_em <= ?""", (AGENDADA, agora.strftime("%Y-%m-%d %H:%M:%S"))).fetchall():
        if o["rota_status"] not in (banco.ROTA_PLANEJADA, banco.ROTA_ACEITA) or o["rota_agente"] != o["agent_original"]:
            _retirar(conn, o["id"], "rota mudou antes do horario agendado")
            continue
        conn.execute("UPDATE nucleo_ofertas_rota SET status = ?, atualizado_em = ? WHERE id = ? AND status = ?",
                     (ABERTA, banco.agora(), o["id"], AGENDADA))
        n += 1
    conn.commit()
    return n


def manter(conn: sqlite3.Connection) -> dict:
    """Fecha o que deixou de valer: rota que andou, foi cancelada, voltou pro
    original ou ja esta com quem escolheu. Sem commit."""
    stats = {"aplicadas": 0, "retiradas": 0}
    for o in conn.execute("""
            SELECT o.*, r.status AS rota_status, r.agent_id AS rota_agente
            FROM nucleo_ofertas_rota o LEFT JOIN nucleo_rotas r ON r.id = o.rota_id
            WHERE o.status IN (?, ?)""", (ABERTA, ESCOLHIDA)).fetchall():
        if o["status"] == ESCOLHIDA and o["rota_agente"] == o["escolhido_por"]:
            conn.execute("UPDATE nucleo_ofertas_rota SET status = ?, atualizado_em = ? WHERE id = ?",
                         (APLICADA, banco.agora(), o["id"]))
            stats["aplicadas"] += 1
            continue
        motivo = None
        if o["rota_status"] is None:
            motivo = "rota sumiu"
        elif o["rota_status"] in (banco.ROTA_CANCELADA, banco.ROTA_EM_ROTA, banco.ROTA_CONCLUIDA):
            motivo = f"rota {o['rota_status'].lower()}"
        elif o["rota_agente"] != o["agent_original"]:
            motivo = "rota trocou de motorista"
        elif o["status"] == ABERTA and o["motivo"] != MOTIVO_EQUIPE and o["rota_status"] == banco.ROTA_ACEITA:
            motivo = "motorista original aceitou"
        if motivo:
            _retirar(conn, o["id"], motivo)
            stats["retiradas"] += 1
    return stats


# ── Equipe (botoes do card da rota enviada, painel) ───────────────────────────

def ofertar_pela_equipe(conn: sqlite3.Connection, rota_id: int, usuario: str, gmaps_key: str | None,
                        resumo=montar_resumo, publicar_em: datetime | None = None, agora: datetime | None = None) -> dict:
    """Botao "Ofertar para motoristas". Uma oferta por rota. Commita.
    `publicar_em` (hora local, no futuro) agenda: a oferta so aparece no app e no grupo nessa hora."""
    agora = agora or datetime.now()
    if publicar_em is not None and publicar_em <= agora:
        publicar_em = None      # horario ja passou/e agora: publica na hora
    rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone()
    if rota is None or rota["provedor"] != banco.PROVEDOR_VUUPT:
        return {"ok": False, "erro": "Rota não encontrada."}
    if oferta_da_rota(conn, rota_id):
        return {"ok": False, "erro": "Essa rota já foi ofertada."}
    if rota["status"] not in (banco.ROTA_PLANEJADA, banco.ROTA_ACEITA):
        return {"ok": False, "erro": "Rota já iniciada, concluída ou cancelada: não dá pra ofertar."}
    if str(rota["data_rota"]) < date.today().isoformat():
        return {"ok": False, "erro": "Rota de dia que já passou: não dá pra ofertar."}
    if not publicar(conn, rota, MOTIVO_EQUIPE, motoristas_com_app(conn, rota), resumo(conn, rota, gmaps_key),
                   publicar_em.strftime("%Y-%m-%d %H:%M:%S") if publicar_em else None):
        conn.rollback()
        return {"ok": False, "erro": "Essa rota já foi ofertada."}
    conn.execute("UPDATE nucleo_ofertas_rota SET ofertada_por = ? WHERE rota_id = ?", (usuario, rota_id))
    conn.commit()
    logger.info("Rota %s (%s) ofertada pela equipe (%s)%s", rota_id, rota["nome"], usuario,
                f" para publicar as {publicar_em:%d/%m %H:%M}" if publicar_em else "")
    return {"ok": True, "oferta_id": oferta_da_rota(conn, rota_id)["id"], "agendada_para": publicar_em.strftime("%H:%M") if publicar_em else None}


def retirar_pela_equipe(conn: sqlite3.Connection, rota_id: int, usuario: str) -> dict:
    """Botao "Retirar oferta": so a oferta da equipe que ninguem pegou. Nao reabre. Commita."""
    o = oferta_da_rota(conn, rota_id)
    if not o or o["motivo"] != MOTIVO_EQUIPE:
        return {"ok": False, "erro": "Essa rota não tem oferta da equipe pra retirar."}
    if o["status"] in (ESCOLHIDA, APLICADA):
        return {"ok": False, "erro": "Alguém já pegou essa rota: confirme ou recuse o motorista no card."}
    cur = conn.execute("UPDATE nucleo_ofertas_rota SET status = ?, motivo_retirada = ?, atualizado_em = ? "
                       "WHERE id = ? AND status IN (?, ?)",
                       (RETIRADA, f"retirada pela equipe ({usuario})", banco.agora(), o["id"], ABERTA, AGENDADA))
    conn.commit()
    if cur.rowcount == 0:
        return {"ok": False, "erro": "Essa oferta já não está aberta."}
    return {"ok": True}


def rota_do_rascunho(conn: sqlite3.Connection, rascunho_id: int):
    """A rota do nucleo que nasceu desse rascunho enviado (a mais nova, se houve reenvio)."""
    return conn.execute("SELECT * FROM nucleo_rotas WHERE rascunho_id = ? AND provedor = ? ORDER BY id DESC LIMIT 1",
                        (rascunho_id, banco.PROVEDOR_VUUPT)).fetchone()


def situacao_do_card(conn: sqlite3.Connection, rascunho_id: int) -> dict | None:
    """O que o card da rota enviada mostra: None (sem oferta) ou status, motivo e quem pegou."""
    rota = rota_do_rascunho(conn, rascunho_id)
    o = oferta_da_rota(conn, rota["id"]) if rota else None
    if not o:
        return None
    return {"status": o["status"], "motivo": o["motivo"], "publicar_em": (o["publicar_em"] or "")[11:16] or None,
            "escolhido_nome": _nome(conn, o["escolhido_por"]) if o["escolhido_por"] else None}


def escolha_pendente(conn: sqlite3.Connection, rota_id: int) -> dict | None:
    """Quem pegou a rota e espera o "Confirmar motorista": agente, carro e nome do login dele."""
    o = oferta_da_rota(conn, rota_id)
    if not o or o["status"] != ESCOLHIDA:
        return None
    m = conn.execute("SELECT agent_id, vehicle_id, nome FROM motoristas WHERE agent_id = ?", (o["escolhido_por"],)).fetchone()
    return {"agent_id": o["escolhido_por"], "vehicle_id": m["vehicle_id"] if m else None,
            "nome": m["nome"] if m else f"agente {o['escolhido_por']}"}


def aplicar_escolha(conn: sqlite3.Connection, rota_id: int, usuario: str, escolha: dict | None = None) -> None:
    """A rota passa pra quem escolheu ja no app, sem esperar o sync, e volta a PLANEJADA
    (o novo motorista ainda da o aceite dele). Commita.

    `escolha` = o motorista que JA foi pra Vuupt (confirmar_escolha): vale ele, mesmo que
    nesse meio tempo tenha desistido no app ou outro tenha pego -- o app tem que ficar igual
    a Vuupt. Sem `escolha`, usa quem esta escolhido agora."""
    from nucleo import rotas
    escolha = escolha or escolha_pendente(conn, rota_id)
    o = oferta_da_rota(conn, rota_id)
    if escolha is None or o is None:
        raise ValueError("Ninguém escolheu essa rota.")
    conn.execute("UPDATE nucleo_rotas SET agent_id = ?, vehicle_id = ?, motorista_nome = ?, status = ?, atualizado_em = ? "
                 "WHERE id = ?", (escolha["agent_id"], escolha["vehicle_id"], escolha["nome"], banco.ROTA_PLANEJADA,
                                  banco.agora(), rota_id))
    rotas.registrar_evento(conn, "ROTA_TROCADA_OFERTA", banco.ORIGEM_PAINEL, rota_id=rota_id, agent_id=escolha["agent_id"],
                           dados={"de": o["agent_original"], "para": escolha["agent_id"], "usuario": usuario})
    conn.execute("UPDATE nucleo_ofertas_rota SET status = ?, escolhido_por = ?, escolhido_em = COALESCE(escolhido_em, ?), "
                 "atualizado_em = ? WHERE id = ?", (APLICADA, escolha["agent_id"], banco.agora(), banco.agora(), o["id"]))
    conn.commit()
    logger.info("Rota %s: escolha de %s confirmada por %s (era de %s)", rota_id, escolha["agent_id"], usuario, o["agent_original"])


def confirmar_escolha(conn: sqlite3.Connection, rota_id: int, usuario: str, trocar) -> dict:
    """Botao "Confirmar motorista". `trocar(escolha)` faz a troca na Vuupt (o painel passa
    rascunhos_rota.trocar_motorista) e levanta ValueError quando ela recusa; so depois do ok
    a rota passa pro motorista no app. Devolve {"ok": True, "motorista", "aviso"} ou
    {"ok": False, "erro"}.

    Vuupt recusou: nada muda. Vuupt trocou e o banco nao gravou (dados.db ocupado pelos
    timers): tenta mais uma vez e, se nao der, diz pra clicar de novo -- nunca erro mudo."""
    escolha = escolha_pendente(conn, rota_id)
    if escolha is None:
        return {"ok": False, "erro": "Ninguém escolheu essa rota (ou a escolha já foi confirmada)."}
    try:
        troca = trocar(escolha) or {}
    except ValueError as e:
        return {"ok": False, "erro": str(e)}
    except Exception:
        logger.exception("Rota %s: erro inesperado ao trocar o motorista pra %s", rota_id, escolha["agent_id"])
        return {"ok": False, "erro": "Não deu pra confirmar a troca. Ela pode já ter ido pra Vuupt: recarregue a página e "
                                     "confira o motorista no card antes de tentar de novo."}
    for tentativa in (1, 2):
        try:
            aplicar_escolha(conn, rota_id, usuario, escolha)
            break
        except sqlite3.Error:
            conn.rollback()
            if tentativa == 2:
                logger.exception("Rota %s: Vuupt trocou pra %s, mas o nucleo nao gravou", rota_id, escolha["agent_id"])
                return {"ok": False, "erro": "A Vuupt já trocou o motorista, mas o app ainda não recebeu a rota. "
                                             "Clique em Confirmar motorista de novo."}
    return {"ok": True, "motorista": escolha["nome"], "aviso": troca.get("aviso")}


def recusar_escolha(conn: sqlite3.Connection, rota_id: int, usuario: str) -> dict:
    """Botao "Recusar": a rota volta pra oferta e quem foi recusado deixa de ve-la. Commita."""
    o = oferta_da_rota(conn, rota_id)
    if not o or o["status"] != ESCOLHIDA:
        return {"ok": False, "erro": "Ninguém escolheu essa rota (ou a escolha já foi confirmada)."}
    recusados = sorted(set(json.loads(o["recusados_json"] or "[]")) | {o["escolhido_por"]})
    conn.execute("UPDATE nucleo_ofertas_rota SET status = ?, escolhido_por = NULL, escolhido_em = NULL, recusados_json = ?, "
                 "atualizado_em = ? WHERE id = ? AND status = ?", (ABERTA, json.dumps(recusados), banco.agora(), o["id"], ESCOLHIDA))
    conn.commit()
    logger.info("Rota %s: escolha de %s recusada por %s", rota_id, o["escolhido_por"], usuario)
    return {"ok": True}


def _recusado(oferta, agent_id: int) -> bool:
    return agent_id in json.loads(oferta["recusados_json"] or "[]")


# ── App (chamado por nucleo/operacao.py) ──────────────────────────────────────

def rotas_escondidas(conn: sqlite3.Connection, agent_id: int) -> set[int]:
    """Quem recusou deixa de ver a rota enquanto ela esta em oferta."""
    if not _tem_tabela(conn):
        return set()
    return {r["rota_id"] for r in conn.execute(
        "SELECT rota_id FROM nucleo_ofertas_rota WHERE agent_original = ? AND motivo = ? AND status IN (?, ?)",
        (agent_id, MOTIVO_RECUSA, ABERTA, ESCOLHIDA))}


def ao_aceitar(conn: sqlite3.Connection, rota, agent_id: int) -> str | None:
    """Motorista original aceitando rota em oferta. Devolve o motivo pra
    recusar o aceite, ou None (e retira a oferta aberta). Sem commit."""
    o = oferta_da_rota(conn, rota["id"])
    if not o:
        return None
    if o["status"] == ESCOLHIDA and o["escolhido_por"] != agent_id:
        return "Outro motorista já pegou essa rota. Fale com a operação."
    if o["status"] == ABERTA and o["motivo"] != MOTIVO_EQUIPE:      # oferta da equipe so sai por decisao da equipe
        _retirar(conn, o["id"], "motorista original aceitou")
    return None


def listar_para_motorista(conn: sqlite3.Connection, agent_id: int) -> dict:
    """No formato de operacao.listar_ofertas: {"abertas": [...], "minhas": [...]}."""
    saida = {"abertas": [], "minhas": []}
    if not _tem_tabela(conn):
        return saida
    tipo = _tipo_veiculo_do(conn, agent_id)
    for o in conn.execute("SELECT * FROM nucleo_ofertas_rota WHERE data_rota >= ? AND status IN (?, ?, ?) ORDER BY data_rota, id",
                          (date.today().isoformat(), ABERTA, ESCOLHIDA, APLICADA)).fetchall():
        minha = o["status"] in (ESCOLHIDA, APLICADA) and o["escolhido_por"] == agent_id
        aberta = o["status"] == ABERTA and agent_id != o["agent_original"] and not _recusado(o, agent_id)
        if not (minha or aberta):
            continue
        resumo = json.loads(o["resumo_json"] or "{}")
        rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (o["rota_id"],)).fetchone()
        valor = valor_aproximado(conn, rota, tipo) if rota else None
        if valor is not None:
            resumo["valor_aproximado"] = _brl(valor)
        item = {"rascunho_id": ID_BASE + o["rota_id"], "data_alvo": o["data_rota"], "resumo": resumo, "status": o["status"]}
        if minha:
            item["aplicada"] = o["status"] == APLICADA
            saida["minhas"].append(item)
        else:
            saida["abertas"].append(item)
    return saida


def escolher(conn: sqlite3.Connection, agent_id: int, rota_id: int) -> str | None:
    """Quem chega primeiro leva. Devolve o motivo da recusa ou None. Commita."""
    o = oferta_da_rota(conn, rota_id)
    if not o or agent_id == o["agent_original"] or _recusado(o, agent_id):
        return "Essa rota não está disponível pra você."
    rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone()
    veiculo = motivo_veiculo(conn, rota, agent_id) if rota else None
    if veiculo:
        return veiculo
    cur = conn.execute("""
        UPDATE nucleo_ofertas_rota SET status = ?, escolhido_por = ?, escolhido_em = ?, aviso_escolha_em = NULL, atualizado_em = ?
        WHERE id = ? AND status = ?""", (ESCOLHIDA, agent_id, banco.agora(), banco.agora(), o["id"], ABERTA))
    conn.commit()
    if cur.rowcount == 0:
        return "Essa rota não está mais disponível -- outro motorista já escolheu (ou ela foi retirada)."
    return None


def cancelar(conn: sqlite3.Connection, agent_id: int, rota_id: int) -> str | None:
    """Desiste antes de a equipe trocar o motorista. Commita."""
    o = oferta_da_rota(conn, rota_id)
    cur = conn.execute("""
        UPDATE nucleo_ofertas_rota SET status = ?, escolhido_por = NULL, escolhido_em = NULL, atualizado_em = ?
        WHERE id = ? AND status = ? AND escolhido_por = ?""",
        (ABERTA, banco.agora(), o["id"] if o else None, ESCOLHIDA, agent_id))
    conn.commit()
    if cur.rowcount == 0:
        return "Essa escolha já foi confirmada pela operação -- fale com a operação pra desfazer."
    return None


# ── Pagina publica do link (Hugo, 19/11) ──────────────────────────────────────

def _assinatura(segredo: str, oferta_id: int) -> str:
    import hashlib
    import hmac
    return hmac.new(segredo.encode(), f"oferta-rota:{oferta_id}".encode(), hashlib.sha256).hexdigest()[:16]


def token_oferta(segredo: str, oferta_id: int) -> str:
    """Id + assinatura: o link nao deixa adivinhar a oferta vizinha."""
    return f"{oferta_id}-{_assinatura(segredo, oferta_id)}"


def oferta_por_token(conn: sqlite3.Connection, segredo: str, token: str):
    import hmac
    oferta_id, _, assinatura = str(token or "").partition("-")
    if not (segredo and oferta_id.isdigit() and assinatura) or not _tem_tabela(conn):
        return None
    if not hmac.compare_digest(assinatura, _assinatura(segredo, int(oferta_id))):
        return None
    return conn.execute("SELECT * FROM nucleo_ofertas_rota WHERE id = ?", (int(oferta_id),)).fetchone()


def link_da_oferta(cfg: dict, oferta_id: int) -> str:
    """Link do aviso: a pagina da oferta; sem segredo no config, a pagina fixa que so abre o app."""
    return f"{cfg['link'].rstrip('/')}/{token_oferta(cfg['segredo'], oferta_id)}" if cfg.get("segredo") else cfg["link"]


def _sem_repetir(nomes) -> list[str]:
    """'SAO PAULO' e 'São Paulo' viram um so (fica a grafia com acento)."""
    vistos: dict[str, str] = {}
    for nome in nomes or []:
        chave = _so_letras(nome)
        if chave and (chave not in vistos or (vistos[chave].isascii() and not str(nome).isascii())):
            vistos[chave] = str(nome)
    return list(vistos.values())


def dados_pagina(conn: sqlite3.Connection, oferta) -> dict:
    """O que a pagina publica mostra: so o resumo (nunca endereco de entrega,
    destinatario ou pedido) + coleta, valor e pagamento."""
    from regras.tipo_veiculo import tipo_por_codigo
    resumo = json.loads(oferta["resumo_json"] or "{}")
    rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (oferta["rota_id"],)).fetchone()
    tarde = None
    if rota:
        tarde = conn.execute(
            "SELECT MIN(janela_inicio) FROM nucleo_paradas WHERE rota_id = ? AND situacao != ? AND janela_inicio >= ?",
            (rota["id"], banco.PARADA_CANCELADA, JANELA_TARDE)).fetchone()[0]
    valor = valor_aproximado(conn, rota, _tipo_veiculo_do(conn, oferta["agent_original"])) if rota else None
    veiculo = tipo_por_codigo(rota["tipo_veiculo"]) if rota else None
    encerrada = oferta["status"] != ABERTA or str(oferta["data_rota"]) < date.today().isoformat()
    return {
        "dia": f"{oferta['data_rota'][8:10]}/{oferta['data_rota'][5:7]}",
        "disponivel": not encerrada,
        "situacao": "Disponível" if not encerrada else (
            "Já foi escolhida por outro motorista" if oferta["status"] in (ESCOLHIDA, APLICADA) else "Não está mais disponível"),
        "entregas": int(resumo.get("total_paradas") or 0),
        "regiao": resumo.get("regiao"),
        "cidades": _sem_repetir(resumo.get("cidades")),
        "bairros": _sem_repetir(resumo.get("bairros")),
        "caixas": resumo.get("total_caixas"),
        "peso_kg": resumo.get("peso_kg"),
        "veiculo": veiculo.nome if veiculo else None,
        "valor": _brl(valor) if valor is not None else None,
        "local_coleta": LOCAL_COLETA,
        "horario_coleta": resumo.get("saida"),
        "janela_tarde": tarde,
        "pagamento": TEXTO_PAGAMENTO,
    }


# ── Avisos por WhatsApp ───────────────────────────────────────────────────────

def _cortar(texto: str, limite: int) -> str:
    return texto if len(texto) <= limite else texto[:max(0, limite - 1)].rstrip() + "…"


def texto_disponivel(oferta, valor: float | None, link: str) -> str:
    """Aviso pro grupo dos motoristas. Inteiro em 200 caracteres; o link nunca e cortado."""
    resumo = json.loads(oferta["resumo_json"] or "{}")
    dia = f"{oferta['data_rota'][8:10]}/{oferta['data_rota'][5:7]}"
    n = int(resumo.get("total_paradas") or 0)
    linhas = [f"Rota disponível {dia}", f"{n} entrega{'s' if n != 1 else ''}"
              + (f", saída {resumo['saida']}" if resumo.get("saida") else "")]
    if valor is not None:
        linhas.append(f"Valor aprox. {_brl(valor)}")
    fim = f"Veja e pegue: {link}"
    sobra = MAX_MENSAGEM - len("\n".join(linhas + [fim])) - 1
    regiao = str(resumo.get("regiao") or "")
    if regiao and sobra >= 8:
        linhas.insert(2, _cortar(regiao, sobra))
    return "\n".join(linhas + [fim])


def texto_escolhida(oferta, rota, nome_escolheu: str, nome_original: str) -> str:
    """Aviso pra equipe: alguem escolheu, falta confirmar no card (a troca na Vuupt vai sozinha)."""
    dia = f"{oferta['data_rota'][8:10]}/{oferta['data_rota'][5:7]}"
    nome_rota = _cortar(str(rota["nome"] or f"rota {oferta['rota_id']}").split(" - ")[-1], 20)
    return _cortar(f"{_cortar(nome_escolheu, 40)} quer a rota {nome_rota} de {dia}, que era de {_cortar(nome_original, 40)}.\n"
                   "Confirme no card da rota, no planejamento.", MAX_MENSAGEM)


def _nome(conn: sqlite3.Connection, agent_id) -> str:
    m = conn.execute("SELECT nome FROM motoristas WHERE agent_id = ?", (agent_id,)).fetchone()
    return m["nome"] if m else f"agente {agent_id}"


def _so_letras(nome) -> str:
    import unicodedata
    sem_acento = unicodedata.normalize("NFKD", str(nome or "")).encode("ascii", "ignore").decode()
    return "".join(c for c in sem_acento.upper() if c.isalnum())


def grupo_rotas(config: dict, cfg: dict, listar=None) -> str | None:
    """Destino do aviso de rota disponivel: grupo_id do config, senao o grupo
    "Rotas FRESHLOG" achado pelo nome no gateway. None quando o gateway nao
    respondeu ou o numero nao esta no grupo (o aviso espera a proxima rodada)."""
    if cfg["grupo_id"]:
        return cfg["grupo_id"]
    if listar is None:
        from integracao_openwa import listar_grupos as listar
    alvo = _so_letras(GRUPO_ROTAS)
    for g in listar(config.get("whatsapp_notificacoes") or {}) or []:
        if isinstance(g, dict) and g.get("id") and _so_letras(g.get("name")) == alvo:
            return g["id"]
    return None


def enviar_avisos(conn: sqlite3.Connection, config: dict, cfg: dict, modo_teste: bool = False, despachar=None,
                  listar_grupos=None) -> dict:
    """Um aviso por oferta aberta (grupo Rotas FRESHLOG) e um por escolha
    (grupo interno).

    O aviso do grupo Rotas so conta como dado quando SAIU de verdade: as mensagens
    particulares partem dele (grupo primeiro -- Hugo, 21/11), entao teto do dia, canal em
    pausa ou gateway fora ficam pra rodada seguinte, enquanto a rota for de hoje em diante.
    O aviso de escolha segue como era: so falha do gateway tenta de novo."""
    if despachar is None:
        from notificar_whatsapp import despachar
    stats = {"disponivel": 0, "escolhida": 0}
    destino_rotas = ...   # achado so se houver aviso de rota disponivel
    pendentes = conn.execute("""
        SELECT * FROM nucleo_ofertas_rota
        WHERE (status = ? AND aviso_grupo_em IS NULL AND data_rota >= ?) OR (status = ? AND aviso_escolha_em IS NULL)
        ORDER BY id""", (ABERTA, date.today().isoformat(), ESCOLHIDA)).fetchall()
    for o in pendentes:
        rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (o["rota_id"],)).fetchone()
        if rota is None:
            continue
        if o["status"] == ABERTA:
            if destino_rotas is ...:
                destino_rotas = grupo_rotas(config, cfg, listar_grupos)
                if destino_rotas is None:
                    logger.warning("Grupo %r nao encontrado no WhatsApp: aviso de rota disponivel fica pra proxima rodada",
                                   GRUPO_ROTAS)
            if destino_rotas is None:
                if modo_teste:
                    print(f"[modo teste] grupo {GRUPO_ROTAS!r} nao encontrado: aviso da oferta {o['id']} nao sairia")
                continue
            texto = texto_disponivel(o, valor_aproximado(conn, rota, _tipo_veiculo_do(conn, o["agent_original"])),
                                     link_da_oferta(cfg, o["id"]))
            tipo, coluna, grupo = "rota_disponivel", "aviso_grupo_em", destino_rotas
        else:
            texto = texto_escolhida(o, rota, _nome(conn, o["escolhido_por"]), _nome(conn, o["agent_original"]))
            tipo, coluna, grupo = "rota_escolhida", "aviso_escolha_em", None   # sempre o grupo interno
        situacao = despachar(config, "ofertas_rota_enviada", tipo, texto, f"{tipo}:{o['id']}:{o['escolhido_por'] or 0}",
                             modo_teste=modo_teste, grupo_id=grupo)
        if modo_teste:
            print(f"[modo teste] {tipo} (oferta {o['id']}):\n{texto}\n")
            continue
        if situacao == "enviado" or (tipo == "rota_escolhida" and situacao != "falhou"):
            conn.execute(f"UPDATE nucleo_ofertas_rota SET {coluna} = ? WHERE id = ?", (banco.agora(), o["id"]))
            conn.commit()
            stats["disponivel" if o["status"] == ABERTA else "escolhida"] += 1
    return stats


# ── Etapas por mensagem particular (Hugo, 21/11) ──────────────────────────────
# Depois do aviso no grupo, a rota que ninguem pegou vai por WhatsApp particular,
# nesta ordem: quem esta perto de terminar a rota de hoje, fixos, esporadicos.

def etapa_do_motorista(conn: sqlite3.Connection, agent_id: int, hoje: date) -> str:
    """PERTO_DO_FIM: rota de hoje concluida, ou em andamento com ate 2 paradas pendentes.
    Senao FIXO (bd_motoristas.POSICAO) ou ESPORADICO (qualquer outro valor, ou sem cadastro)."""
    marcas = ",".join("?" * len(_PARADA_ABERTA))
    for rota in conn.execute("SELECT id, status FROM nucleo_rotas WHERE data_rota = ? AND agent_id = ? AND status IN (?, ?)",
                             (hoje.isoformat(), agent_id, banco.ROTA_EM_ROTA, banco.ROTA_CONCLUIDA)).fetchall():
        if rota["status"] == banco.ROTA_CONCLUIDA:
            return ETAPAS[0]
        pendentes = conn.execute(f"SELECT COUNT(*) FROM nucleo_paradas WHERE rota_id = ? AND situacao IN ({marcas})",
                                 (rota["id"], *_PARADA_ABERTA)).fetchone()[0]
        if pendentes <= PERTO_DO_FIM_PARADAS:
            return ETAPAS[0]
    cadastro = conn.execute("SELECT POSICAO FROM bd_motoristas WHERE CAST(AGENT_ID_VUUPT AS INTEGER) = ? ORDER BY id LIMIT 1",
                            (agent_id,)).fetchone()
    return "FIXO" if cadastro and str(cadastro["POSICAO"] or "").strip().upper() == "FIXO" else "ESPORADICO"


def _telefone(conn: sqlite3.Connection, motorista) -> str:
    if str(motorista["telefone"] or "").strip():
        return str(motorista["telefone"])
    cadastro = conn.execute("SELECT TELEFONE_MOTORISTA FROM bd_motoristas WHERE CAST(AGENT_ID_VUUPT AS INTEGER) = ? "
                            "AND COALESCE(TELEFONE_MOTORISTA, '') != '' ORDER BY id LIMIT 1", (motorista["agent_id"],)).fetchone()
    return str(cadastro["TELEFONE_MOTORISTA"]) if cadastro else ""


def texto_particular(oferta, nome: str, valor: float | None, link: str) -> str:
    """Mensagem particular. Inteira em 200 caracteres; o link nunca e cortado."""
    resumo = json.loads(oferta["resumo_json"] or "{}")
    dia = f"{oferta['data_rota'][8:10]}/{oferta['data_rota'][5:7]}"
    n = int(resumo.get("total_paradas") or 0)
    primeiro = _cortar((str(nome or "").split() or ["motorista"])[0], 15)
    partes = [f"Olá, {primeiro}! Rota disponível {dia}: {n} entrega{'s' if n != 1 else ''}"
              + (f", saída {resumo['saida']}" if resumo.get("saida") else "") + "."]
    if valor is not None:
        partes.append(f"Valor aprox. {_brl(valor)}.")
    fim = f"Veja e pegue: {link}"
    sobra = MAX_MENSAGEM - len(" ".join(partes + [fim])) - 2
    regiao = str(resumo.get("regiao") or "")
    if regiao and sobra >= 8:
        partes.insert(1, _cortar(regiao, sobra) + ".")
    return " ".join(partes + [fim])


def _andamento_da_vuupt(config: dict):
    """Padrao de `atualizar_andamento`: refaz o sync das rotas de hoje, porque o do
    nucleo e de 30 em 30 min e "perto de terminar" com dado velho erra."""
    def atualizar(conn: sqlite3.Connection) -> None:
        token = (config.get("vuupt_api") or {}).get("token")
        if not token:
            return
        from nucleo import sincronizar_vuupt
        sincronizar_vuupt.sincronizar_rotas(sincronizar_vuupt._listar_rotas_do_dia(token, date.today()), conn)
    return atualizar


def enviar_etapas(conn: sqlite3.Connection, config: dict, cfg: dict, agora: datetime, modo_teste: bool = False,
                  enviar=None, atualizar_andamento=None) -> dict:
    """Uma mensagem por motorista e por oferta. A etapa seguinte so abre ENTRE_ETAPAS_MIN
    depois da anterior; etapa sem ninguem e pulada. No maximo MAX_MENSAGENS_RODADA tentativas
    de envio por rodada (deu certo ou nao).

    O que fica gravado em nucleo_ofertas_rota_mensagens:
      enviado, sem_telefone, numero_sem_whatsapp  -> definitivo, o motorista nao e tentado de novo
      falhou, indeterminado                       -> tenta de novo ate MAX_TENTATIVAS, depois desiste
                                                     (uma falha que nao passa nao segura as etapas)
      nao_enviado, desligado (teto, canal em pausa, chave desligada) -> NAO grava: a oferta para
                                                     nesta rodada e o motorista recebe quando o canal voltar"""
    if enviar is None:
        from notificar_whatsapp import avisar_motorista_oferta as enviar
    if atualizar_andamento is None:
        atualizar_andamento = _andamento_da_vuupt(config)
    stats = {"mensagens": 0}
    hoje, atualizado, tentativas = agora.date(), False, 0
    for o in conn.execute("SELECT * FROM nucleo_ofertas_rota WHERE status = ? AND aviso_grupo_em IS NOT NULL AND data_rota >= ? "
                          "ORDER BY id", (ABERTA, hoje.isoformat())).fetchall():
        if tentativas >= MAX_MENSAGENS_RODADA:
            break
        if agora < datetime.fromisoformat(o["aviso_grupo_em"]) + timedelta(minutes=PRIMEIRA_ETAPA_MIN):
            continue
        rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (o["rota_id"],)).fetchone()
        if rota is None:
            continue
        ja = {m["agent_id"]: m for m in conn.execute("SELECT * FROM nucleo_ofertas_rota_mensagens WHERE oferta_id = ?", (o["id"],))}
        resolvidos = {a for a, m in ja.items()
                      if m["situacao"] not in _SITUACAO_TENTA_DE_NOVO or m["tentativas"] >= MAX_TENTATIVAS}
        recusados = set(json.loads(o["recusados_json"] or "[]"))
        faltam = [m for m in conn.execute(
            "SELECT * FROM motoristas WHERE ativo = 1 AND agent_id IS NOT NULL AND agent_id != ? ORDER BY agent_id",
            (o["agent_original"] or 0,)).fetchall()
            if m["agent_id"] not in resolvidos and m["agent_id"] not in recusados
            and not motivo_veiculo(conn, rota, m["agent_id"])]
        if not faltam:
            continue
        if not atualizado and not modo_teste:
            atualizado = True
            try:
                atualizar_andamento(conn)
            except Exception as exc:      # Vuupt fora: segue com o andamento que o nucleo tem
                conn.rollback()           # sync que quebrou no meio nao fica pela metade na conexao
                logger.warning("Andamento das rotas nao atualizado na Vuupt (segue com o nucleo): %s", exc)
        ordem = {m["agent_id"]: ETAPAS.index(etapa_do_motorista(conn, m["agent_id"], hoje)) for m in faltam}
        for k, etapa in enumerate(ETAPAS):
            da_vez = [m for m in faltam if ordem[m["agent_id"]] <= k]
            if da_vez:
                for m in da_vez:
                    if tentativas >= MAX_MENSAGENS_RODADA:
                        break
                    telefone = _telefone(conn, m)
                    texto = texto_particular(o, m["nome"], valor_aproximado(conn, rota, m["tipo_veiculo"]), link_da_oferta(cfg, o["id"]))
                    if modo_teste:
                        print(f"[modo teste] {etapa}: mensagem pra {m['nome']} ({telefone or 'SEM TELEFONE'}):\n{texto}\n")
                        continue
                    if telefone:
                        tentativas += 1
                        situacao = enviar(o["id"], m["agent_id"], telefone, texto, config)
                    else:
                        situacao = "sem_telefone"
                    if situacao not in _SITUACAO_GRAVA:      # teto, canal em pausa ou desligado: espera o canal voltar
                        logger.info("Oferta %s: mensagens particulares esperam (%s)", o["id"], situacao)
                        break
                    conn.execute("""
                        INSERT INTO nucleo_ofertas_rota_mensagens (oferta_id, agent_id, etapa, situacao, criado_em)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(oferta_id, agent_id) DO UPDATE SET situacao = excluded.situacao, tentativas = tentativas + 1
                    """, (o["id"], m["agent_id"], etapa, situacao, agora.strftime("%Y-%m-%d %H:%M:%S")))
                    conn.commit()
                    if situacao == "enviado":
                        stats["mensagens"] += 1
                break
            aberta_em = min((m["criado_em"] for m in ja.values() if m["etapa"] == etapa), default=None)
            if aberta_em and agora < datetime.fromisoformat(aberta_em) + timedelta(minutes=ENTRE_ETAPAS_MIN):
                break       # a etapa seguinte espera
    return stats


# ── Rodada ────────────────────────────────────────────────────────────────────

def rodar(conn: sqlite3.Connection, config: dict, agora: datetime | None = None, modo_teste: bool = False,
          resumo=montar_resumo, despachar=None, listar_grupos=None, enviar=None, atualizar_andamento=None) -> dict:
    cfg = config_ofertas(config)
    stats = {"publicadas": 0, "aplicadas": 0, "retiradas": 0, "disponivel": 0, "escolhida": 0, "mensagens": 0}
    if not cfg["ativo"] and not modo_teste:      # o ensaio roda mesmo desligado
        return stats
    agora = agora or datetime.now()
    if not modo_teste:
        stats.update(manter(conn))
        stats["abertas_agendadas"] = abrir_agendadas(conn, agora)
        conn.commit()
    gmaps_key = (config.get("google_maps") or {}).get("api_key", "")
    for rota, motivo in candidatas(conn, cfg, agora):
        quem = motoristas_com_app(conn, rota)
        dados = resumo(conn, rota, gmaps_key)
        logger.info("Rota %s (%s, agente %s) aberta: %s -> %d motorista(s) com app%s", rota["id"], rota["nome"],
                    rota["agent_id"], motivo, len(quem), " [modo teste, nao publicada]" if modo_teste else "")
        if modo_teste:
            print(f"[modo teste] publicaria rota {rota['id']} ({motivo}) pra {quem}: {dados}")
            continue
        if publicar(conn, rota, motivo, quem, dados):
            conn.commit()
            stats["publicadas"] += 1
    stats.update(enviar_avisos(conn, config, cfg, modo_teste, despachar, listar_grupos))
    stats.update(enviar_etapas(conn, config, cfg, agora, modo_teste, enviar, atualizar_andamento))
    return stats


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Divulga pros outros motoristas a rota recusada ou ofertada pela equipe.")
    parser.add_argument("--modo-teste", action="store_true", help="mostra o que publicaria e avisaria; nao grava nem envia")
    parser.add_argument("--situacao", action="store_true", help="mostra a config e as ofertas de hoje e sai")
    parser.add_argument("--ensaiar-rota", type=int, metavar="ROTA_ID",
                        help="com --modo-teste: mostra quem ve, quem o veiculo barra, resumo e aviso dessa rota; nao grava nem envia")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    config = ponte_vuupt._config_projeto()
    cfg = config_ofertas(config)
    conn = banco.conectar()
    try:
        if args.situacao:
            print(f"ativo={cfg['ativo']} todos={cfg['todos']} agentes={sorted(cfg['agentes'])} "
                  f"grupo_do_config={bool(cfg['grupo_id'])} grupo_rotas_achado={grupo_rotas(config, cfg) is not None}")
            for o in conn.execute("SELECT * FROM nucleo_ofertas_rota WHERE data_rota >= ? ORDER BY id", (date.today().isoformat(),)):
                print(f"  rota={o['rota_id']} {o['motivo']} {o['status']} original={o['agent_original']} "
                      f"escolhido_por={o['escolhido_por']} elegiveis={o['elegiveis_json']} retirada={o['motivo_retirada']}")
            return 0
        if args.ensaiar_rota:
            if not args.modo_teste:
                parser.error("--ensaiar-rota so com --modo-teste")
            rota = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (args.ensaiar_rota,)).fetchone()
            if rota is None:
                print("rota nao encontrada")
                return 1
            quem = motoristas_com_app(conn, rota)
            barrados = [_nome(conn, a) for a in quem if motivo_veiculo(conn, rota, a)]
            dados = montar_resumo(conn, rota, (config.get("google_maps") or {}).get("api_key", ""))
            print(f"rota {rota['id']} {rota['nome']} | veiculo da rota: {rota['tipo_veiculo'] or 'comum'} "
                  f"| original: {_nome(conn, rota['agent_id'])}")
            print(f"veem a oferta (com app): {len(quem)}; barrados pelo veiculo ao escolher ({len(barrados)}): {barrados}")
            print(f"resumo: {dados}")
            aviso = texto_disponivel({"data_rota": rota["data_rota"], "resumo_json": json.dumps(dados, ensure_ascii=False, default=str)},
                                     valor_aproximado(conn, rota, _tipo_veiculo_do(conn, rota["agent_id"])), cfg["link"])
            print(f"aviso ({len(aviso)} caracteres):")
            print(aviso)
            return 0
        if not cfg["ativo"] and not args.modo_teste:
            return 0
        stats = rodar(conn, config, modo_teste=args.modo_teste)
        if any(stats.values()):
            logger.info("Ofertas de rota sem aceite: %s", stats)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
