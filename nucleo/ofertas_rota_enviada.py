# -*- coding: utf-8 -*-
"""
nucleo/ofertas_rota_enviada.py

Rota ja enviada que o motorista NAO aceitou vira oferta pros outros
(Hugo, 17/11). Dois gatilhos:
  RECUSA  ele recusou no app                       -> publica na rodada seguinte
  PRAZO   passou do horario limite (05h30) sem aceite, nem aqui nem na Vuupt
          (so rota atribuida ANTES do limite; depois disso, so RECUSA)

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
- No PRAZO o motorista original continua com a rota na lista e ainda pode
  aceitar enquanto ninguem escolheu (ai a oferta e retirada). Quem RECUSOU
  deixa de ver a rota.
- A escolha NAO troca o motorista sozinha: a equipe e avisada e troca na
  Vuupt; quando o sync ve a rota com o motorista que escolheu, a oferta vira
  APLICADA.
- Valor aproximado = tabela de tarifas (veiculo + km), como no financeiro.

Liga em api_motorista.rota_sem_aceite:
    ativo: true
    horario_limite: "05:30"
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
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from nucleo import banco, normalizacao, ponte_vuupt

logger = logging.getLogger(__name__)

ID_BASE = 1_000_000_000

MOTIVO_RECUSA = "RECUSA"
MOTIVO_PRAZO = "PRAZO"
ABERTA, ESCOLHIDA, APLICADA, RETIRADA = "ABERTA", "ESCOLHIDA", "APLICADA", "RETIRADA"

LINK_PADRAO = "https://app.freshhub.com.br/motorista/abrir/ofertas"
GRUPO_ROTAS = "Rotas FRESHLOG"
# Pagina do link (Hugo, 19/11): coleta sempre na base; pagamento ainda sem regra no sistema.
LOCAL_COLETA = ("Base Freshlog - Rua Zilda, 288, Portão Lateral Ouro Grosso, Casa Verde Alta, "
                "São Paulo-SP, 02545-000")
TEXTO_PAGAMENTO = "Alinhar no WhatsApp."
JANELA_TARDE = "12:00"   # entrega com janela abrindo a partir daqui ganha alerta na pagina
MAX_MENSAGEM = 200
# Servico que a Vuupt ainda nao viu o motorista aceitar.
_VUUPT_SEM_ACEITE = ("", "assigned", "not_assigned")


def config_ofertas(config: dict | None = None) -> dict:
    projeto = config if config is not None else ponte_vuupt._config_projeto()
    bruto = (projeto.get("api_motorista") or {}).get("rota_sem_aceite") or {}
    return {
        "ativo": bool(bruto.get("ativo")),
        "horario_limite": str(bruto.get("horario_limite") or "05:30")[:5],
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


def _aceita_na_vuupt(conn: sqlite3.Connection, rota_id: int) -> bool:
    return conn.execute(
        f"SELECT 1 FROM nucleo_paradas WHERE rota_id = ? AND situacao != ? "
        f"AND COALESCE(status_provedor, '') NOT IN ({','.join('?' * len(_VUUPT_SEM_ACEITE))}) LIMIT 1",
        (rota_id, banco.PARADA_CANCELADA, *_VUUPT_SEM_ACEITE)).fetchone() is not None


def _atribuida_em(rota) -> str | None:
    """Quando a rota chegou ao motorista atual (hora local): assigned_at da
    Vuupt (muda na troca de motorista); sem ele (rota gravada pelo envio, antes
    do sync), a hora em que entrou no nucleo."""
    try:
        dados = json.loads(rota["dados_json"] or "{}")
    except ValueError:
        dados = {}
    return normalizacao.vuupt_para_local(dados.get("assigned_at")) or rota["criado_em"]


def candidatas(conn: sqlite3.Connection, cfg: dict, agora: datetime) -> list[tuple[sqlite3.Row, str]]:
    """Rotas de HOJE, da Vuupt, com motorista, ainda planejadas e nunca
    ofertadas, que ele recusou ou deixou passar do horario limite.
    Rota atribuida no horario limite ou depois (coleta, rota da tarde, troca de
    motorista) so vira oferta se ele recusar (Hugo, 19/11)."""
    passou_do_prazo = agora.strftime("%H:%M") >= cfg["horario_limite"]
    limite = f"{agora.date().isoformat()} {cfg['horario_limite']}"
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
        elif passou_do_prazo and (_atribuida_em(rota) or "") < limite and not _aceita_na_vuupt(conn, rota["id"]):
            saida.append((rota, MOTIVO_PRAZO))
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

def publicar(conn: sqlite3.Connection, rota, motivo: str, elegiveis: list[int], resumo: dict) -> bool:
    """Sem commit. False se a rota ja tinha oferta (outra rodada chegou antes)."""
    cur = conn.execute("""
        INSERT OR IGNORE INTO nucleo_ofertas_rota (rota_id, data_rota, agent_original, motivo, elegiveis_json, resumo_json,
                                                   criado_em, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (rota["id"], rota["data_rota"], rota["agent_id"], motivo, json.dumps(sorted(set(elegiveis))),
          json.dumps(resumo, ensure_ascii=False, default=str), banco.agora(), banco.agora()))
    return cur.rowcount == 1


def _retirar(conn: sqlite3.Connection, oferta_id: int, motivo: str) -> None:
    conn.execute("UPDATE nucleo_ofertas_rota SET status = ?, motivo_retirada = ?, atualizado_em = ? WHERE id = ?",
                 (RETIRADA, motivo, banco.agora(), oferta_id))


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
        elif o["status"] == ABERTA and (o["rota_status"] == banco.ROTA_ACEITA or
                                        (o["motivo"] == MOTIVO_PRAZO and _aceita_na_vuupt(conn, o["rota_id"]))):
            motivo = "motorista original aceitou"
        if motivo:
            _retirar(conn, o["id"], motivo)
            stats["retiradas"] += 1
    return stats


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
    if o["status"] == ABERTA:
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
        aberta = o["status"] == ABERTA and agent_id != o["agent_original"]
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
    if not o or agent_id == o["agent_original"]:
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
    """Aviso pra equipe: alguem escolheu, falta trocar o motorista na Vuupt."""
    dia = f"{oferta['data_rota'][8:10]}/{oferta['data_rota'][5:7]}"
    nome_rota = _cortar(str(rota["nome"] or f"rota {oferta['rota_id']}").split(" - ")[-1], 20)
    return _cortar(f"{_cortar(nome_escolheu, 40)} quer a rota {nome_rota} de {dia}, que era de {_cortar(nome_original, 40)}.\n"
                   "Troque o motorista na Vuupt para confirmar.", MAX_MENSAGEM)


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
    (grupo interno). So falha do gateway tenta de novo."""
    if despachar is None:
        from notificar_whatsapp import despachar
    stats = {"disponivel": 0, "escolhida": 0}
    destino_rotas = ...   # achado so se houver aviso de rota disponivel
    pendentes = conn.execute("""
        SELECT * FROM nucleo_ofertas_rota
        WHERE (status = ? AND aviso_grupo_em IS NULL) OR (status = ? AND aviso_escolha_em IS NULL) ORDER BY id""",
        (ABERTA, ESCOLHIDA)).fetchall()
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
        if situacao != "falhou":
            conn.execute(f"UPDATE nucleo_ofertas_rota SET {coluna} = ? WHERE id = ?", (banco.agora(), o["id"]))
            conn.commit()
            stats["disponivel" if o["status"] == ABERTA else "escolhida"] += 1
    return stats


# ── Rodada ────────────────────────────────────────────────────────────────────

def rodar(conn: sqlite3.Connection, config: dict, agora: datetime | None = None, modo_teste: bool = False,
          resumo=montar_resumo, despachar=None, listar_grupos=None) -> dict:
    cfg = config_ofertas(config)
    stats = {"publicadas": 0, "aplicadas": 0, "retiradas": 0, "disponivel": 0, "escolhida": 0}
    if not cfg["ativo"] and not modo_teste:      # o ensaio roda mesmo desligado
        return stats
    agora = agora or datetime.now()
    if not modo_teste:
        stats.update(manter(conn))
        conn.commit()
    gmaps_key = (config.get("google_maps") or {}).get("api_key", "")
    for rota, motivo in candidatas(conn, cfg, agora):
        quem = motoristas_com_app(conn, rota)
        dados = resumo(conn, rota, gmaps_key)
        logger.info("Rota %s (%s, agente %s) sem aceite: %s -> %d motorista(s) com app%s", rota["id"], rota["nome"],
                    rota["agent_id"], motivo, len(quem), " [modo teste, nao publicada]" if modo_teste else "")
        if modo_teste:
            print(f"[modo teste] publicaria rota {rota['id']} ({motivo}) pra {quem}: {dados}")
            continue
        if publicar(conn, rota, motivo, quem, dados):
            conn.commit()
            stats["publicadas"] += 1
    stats.update(enviar_avisos(conn, config, cfg, modo_teste, despachar, listar_grupos))
    return stats


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Publica pros outros motoristas a rota que ficou sem aceite.")
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
            print(f"ativo={cfg['ativo']} limite={cfg['horario_limite']} todos={cfg['todos']} agentes={sorted(cfg['agentes'])} "
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
